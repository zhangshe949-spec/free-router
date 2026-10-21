# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""路由选择器：过滤 → 任务感知打分排序 → 粘性 → 重试(最多20次) → 跨提供商切换。

P0：免费/收费模型选择。候选池里带 type(free|paid) 与 tags；请求可携带任务上下文
（body.free_router / body.user / body.metadata），选择器据此打分并给出「正在用收费
模型」之类的提醒（响应头 X-Free-Router-Notice / X-Free-Router-Model-Type）。
无任务上下文时打分退化为 capability+tier，与历史行为一致。
"""
import time
from typing import Optional

from .registry import has_vision, cap_score
from .selector import ModelSelector, parse_task_context
from .policy import load_policy
from .alerter import Alerter
from .quota import DEFAULT_TENANT
from . import metrics


class RouteError(Exception): pass
class QuotaExhausted(RouteError): pass


class ProviderError(RouteError):
    def __init__(self, msg, status_code=None, retry_after=None):
        super().__init__(msg)
        self.status_code = status_code
        self.retry_after = retry_after
        self.message = msg
        # 兼容 quota.learn_from_error 的属性读取
        self.code = status_code


class AllExhausted(RouteError):
    def __init__(self, last_error=None, attempts=0):
        super().__init__(f"所有免费模型均不可用 (尝试{attempts}次, 最后错误: {last_error})")
        self.last_error = last_error
        self.attempts = attempts


class SessionState:
    """会话粘性：多轮保持同一模型（30分钟窗口）"""
    def __init__(self, ttl_seconds: int = 1800):
        self.ttl = ttl_seconds
        self.model = None
        self.provider = None
        self.last_ts = 0.0
        self.context_summary = ""     # 切换时注入的对话摘要

    def get_sticky(self):
        if self.model and (time.time() - self.last_ts) < self.ttl:
            return (self.provider, self.model)
        return None

    def set_sticky(self, provider, model):
        self.provider, self.model = provider, model
        self.last_ts = time.time()


class ChatResult(dict):
    """非流式结果：仍是 dict（兼容调用方索引），额外带 .meta。"""
    def __init__(self, mapping=None, meta=None):
        super().__init__(mapping or {})
        self.meta = meta or {}


class StreamResult:
    """流式结果：异步可迭代，额外带 .meta。"""
    def __init__(self, agen, meta=None):
        self._agen = agen
        self.meta = meta or {}

    def __aiter__(self):
        return self._agen.__aiter__()

    async def __anext__(self):
        return await self._agen.__anext__()

    async def aclose(self):
        close = getattr(self._agen, "aclose", None)
        if close:
            await close()


class Router:
    def __init__(self, cfg, registry, quota, providers):
        self.cfg = cfg
        self.registry = registry
        self.quota = quota
        self.providers = providers          # name -> provider adapter
        self.session = SessionState(cfg.router.sticky_ttl_seconds)
        self._forced = None                 # (provider, model) 手动强制
        self.selector = ModelSelector(cfg, policy=load_policy(cfg))
        self.alerter = Alerter(cfg)

    # ---------- 过滤 ----------
    def _filter_capability(self, candidates, body):
        # 若含图片消息，要求视觉能力
        need_vision = False
        for m in body.get("messages", []):
            if isinstance(m.get("content"), list):
                for c in m["content"]:
                    if c.get("type") == "image_url":
                        need_vision = True
        # 任务类型推断（用于加权）
        task = self._infer_task(body)
        out = []
        for c in candidates:
            cap = c.capability
            if need_vision and not has_vision(cap):
                continue
            # 质量分级加权：high > medium > low，保证高质量大模型优先，
            # 免费小模型（low）只在没有更好候选时作为兜底被选中。
            boost = self.tier_boost.get(c.tier, 100.0)
            out.append((c, cap_score(cap, task) + boost))
        return out

    @property
    def tier_boost(self) -> dict:
        return getattr(self.cfg.router, "tier_boost",
                       {"high": 200.0, "medium": 100.0, "low": 0.0})

    def _infer_task(self, body) -> Optional[str]:
        # 简单启发式：从 system 提示推断 code/reason
        for m in body.get("messages", []):
            if m.get("role") == "system":
                s = str(m.get("content", ""))
                if any(k in s for k in ("代码", "code", "函数", "bug", "程序")):
                    return "code"
                if any(k in s for k in ("推理", "逻辑", "reason", "证明")):
                    return "reason"
        return None

    def _filter_cooldown(self, candidates, tenant: str = DEFAULT_TENANT):
        out = []
        for c, score in candidates:
            if self.quota.in_cooldown(c.provider, c.id, tenant=tenant):
                continue
            if self.quota.is_quota_exceeded(c.provider, c.id, tenant=tenant):
                continue
            # P3：预计即将耗尽 → 提前避开（自愈，无需冷却态）
            if self.quota.predict_exhausted(c.provider, c.id, tenant=tenant):
                continue
            if not self._provider_usable(c.provider):
                continue
            out.append((c, score))
        return out

    def _provider_usable(self, provider_name: str) -> bool:
        """需要 key 但未配置的提供商直接跳过：否则每次 failover 都白试一次，
        还留下永久 key_unhealthy 冷却，挤占高质量模型的名额。"""
        pc = self.cfg.providers.get(provider_name)
        if pc is None:
            return False
        if getattr(pc, "key_env", None) and not pc.resolved_key:
            return False
        return True

    # ---------- 排序 ----------
    def _apply_priority(self, ordered, body, ctx=None):
        """强制切换 / 显式指定模型 / 会话粘性 → 提到最前。

        规则：显式指定（body.model）与强制切换是用户意图，可越权（含收费模型）；
        会话粘性只是「尽量保持同一个模型」，**不得绕过**硬能力过滤（如 vision）
        与免费/收费策略——故粘性仅在它本身仍留在候选里时才生效。
        """
        front = []
        if self._forced:
            front.append((self._forced, True))
        explicit = body.get("model")
        if isinstance(explicit, str) and explicit:
            front.append(((None, explicit), True))
        sticky = self.session.get_sticky()
        allow_fallback = {}
        if sticky:
            front.append((sticky, False))          # 不允许越权插入

        out = list(ordered)
        for (prov, mid), may_insert in reversed(front):
            idx = None
            for i, e in enumerate(out):
                if e.id == mid and (prov is None or e.provider == prov):
                    idx = i
                    break
            if idx is not None:
                out.insert(0, out.pop(idx))
                continue
            if not may_insert:
                continue
            # 不在候选里（如被免费偏好过滤掉、或处于冷却）→ 显式/强制时仍放行
            e = self.registry.get(mid)
            if e is not None and getattr(e, "status", "active") == "active" \
                    and (prov is None or e.provider == prov):
                out.insert(0, e)
        return out

    def force_switch(self, provider, model):
        self._forced = (provider, model)
        self.session.set_sticky(provider, model)

    def clear_force(self):
        self._forced = None

    # ---------- 重试 + 跨商切换 ----------
    async def route(self, body, task_ctx: Optional[dict] = None,
                    tenant: Optional[str] = None):
        max_retries = self.cfg.router.max_retries
        ctx = dict(task_ctx) if task_ctx else parse_task_context(body)
        if tenant:
            ctx.setdefault("tenant", tenant)
        tenant = str(ctx.get("tenant") or DEFAULT_TENANT)

        attempted = []          # 本次请求失败的提供商（用于 fallback 指标）
        notice = None

        for attempt in range(max_retries):
            candidates = self.registry.list_active()
            pairs = self._filter_capability(candidates, body)
            pairs = self._filter_cooldown(pairs, tenant)

            if getattr(self.cfg.router, "use_selector", True):
                ordered, sel_notice = self.selector.order(pairs, ctx)
                notice = sel_notice
            else:
                ordered = [e for e, _ in sorted(pairs, key=lambda x: -x[1])]
            ordered = self._apply_priority(ordered, body, ctx)

            if not ordered:
                raise AllExhausted("无可用候选模型", attempt)

            for m in ordered:
                provider = self.providers.get(m.provider)
                if not provider:
                    continue
                t0 = time.time()
                try:
                    resp = await provider.chat(body, model=m.id)
                except QuotaExhausted as e:
                    metrics.count_request(m.provider, m.id, "quota")
                    self.quota.apply_cooldown(m.provider, m.id, "429", tenant=tenant)
                    self.alerter.record(False)
                    attempted.append(m.provider)
                    continue
                except ProviderError as e:
                    metrics.count_request(m.provider, m.id, "error")
                    reason = self.quota.learn_from_error(m.provider, m.id, e,
                                                         tenant=tenant)
                    # model_not_found 从注册表移除
                    if reason == "model_not_found":
                        self.registry.remove(m.id)
                    self.alerter.record(False)
                    attempted.append(m.provider)
                    continue

                ms = (time.time() - t0) * 1000.0
                metrics.observe_latency(m.provider, m.id, ms)
                metrics.count_request(m.provider, m.id, "success")
                metrics.count_quota(m.provider, m.id)
                if attempted and attempted[0] != m.provider:
                    metrics.count_fallback(attempted[0], m.provider)
                self.alerter.record(True)

                usage = resp.get("usage") if isinstance(resp, dict) else None
                self.quota.record(m.provider, m.id, usage=usage, tenant=tenant)
                self.session.set_sticky(m.provider, m.id)
                if self._forced and self._forced == (m.provider, m.id):
                    self._forced = None

                # 提醒以「实际命中的模型」为准（顺延/粘性后仍准确）
                notice = self.selector.notice_for(m, ctx) or notice
                meta = self.selector.describe(m, ctx)
                meta.update({"notice": notice, "tenant": tenant,
                             "attempts": attempt + 1,
                             "fallbacks": attempted[:],
                             "latency_ms": round(ms, 1)})

                # 流式：适配器返回异步生成器 → 包一层（保留 meta）
                if hasattr(resp, "__aiter__"):
                    return StreamResult(resp, meta)
                return ChatResult(resp, meta=meta)

        raise AllExhausted(attempts=max_retries)