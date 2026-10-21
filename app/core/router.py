# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""路由选择器：过滤 → 任务感知打分排序 → 粘性 → 重试(最多20次) → 跨提供商切换。

P0：免费/收费模型选择。候选池里带 type(free|paid) 与 tags；请求可携带任务上下文
（body.free_router / body.user / body.metadata），选择器据此打分并给出「正在用收费
模型」之类的提醒（响应头 X-Free-Router-Notice / X-Free-Router-Model-Type）。
无任务上下文时打分退化为 capability+tier，与历史行为一致。

P10：Autofree 模式 —— 用户选 `model: "autofree"` 即可自动调度最强免费模型，
前 10 耗尽顺延，恢复即切回，彻底消除 token 焦虑。

P11：Smart-Free 智能路由 —— 用户选 `model: "smart-free"` 即可自动识别任务类型、
匹配最优免费模型、负载均衡调度、跨 16 家提供商配额池管理，实现无限 token 畅聊。
"""
import time
import math
import logging
from typing import Optional, List, Tuple

from .registry import has_vision, cap_score, extend_registry_for_autofree
from .selector import ModelSelector, parse_task_context
from .observability import (
    provider_call_span, router_select_span,
    record_fallback, record_model_selection, get_logger,
)
from .policy import load_policy
from .alerter import Alerter
from .quota import DEFAULT_TENANT
from .smart_router import SmartRouter, TaskType
from .sustainability import SustainabilityManager
from . import metrics

logger = get_logger("free_router.router")


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

        # ---- Autofree 初始化 ----
        self._autofree_enabled = getattr(cfg, "autofree", None) and getattr(cfg.autofree, "enabled", True)
        self._autofree_cfg = getattr(cfg, "autofree", None)
        self._autofree_session = SessionState(
            getattr(self._autofree_cfg, "sticky_ttl_seconds", 60) if self._autofree_cfg else 60
        )
        self._autofree_max_retries = (
            getattr(self._autofree_cfg, "max_retries", 30) if self._autofree_cfg else 30
        )
        self._autofree_top_n = getattr(self._autofree_cfg, "top_n", 10) if self._autofree_cfg else 10
        self._autofree_fallback_n = getattr(self._autofree_cfg, "fallback_n", 10) if self._autofree_cfg else 10

        # 绑定 registry 的 autofree 方法（注入 quota）
        extend_registry_for_autofree(self.registry)
        if hasattr(self.registry, "bind_autofree_quota"):
            self.registry.bind_autofree_quota(self.quota)

        # ---- Smart-Free 智能路由初始化 ----
        self._smart_cfg = getattr(cfg, "smart_free", None)
        self._smart_enabled = self._smart_cfg and getattr(self._smart_cfg, "enabled", True)
        self._smart_session = SessionState(
            getattr(self._smart_cfg, "sticky_ttl_seconds", 300) if self._smart_cfg else 300
        )
        self._smart_max_retries = (
            getattr(self._smart_cfg, "max_retries", 50) if self._smart_cfg else 50
        )

        # 初始化智能路由引擎
        smart_cfg_dict = {}
        if self._smart_cfg:
            smart_cfg_dict = {
                "load_balance_strategy": getattr(self._smart_cfg, "load_balance_strategy", "weighted_round_robin"),
                "sticky_ttl_seconds": getattr(self._smart_cfg, "sticky_ttl_seconds", 300),
                "max_retries": getattr(self._smart_cfg, "max_retries", 50),
                "min_runtime_requests": getattr(self._smart_cfg, "min_runtime_requests", 5),
            }
        self._smart_router = SmartRouter(self.registry, self.quota, smart_cfg_dict)

        # 初始化可持续性管理器
        sus_cfg_dict = {}
        if hasattr(cfg, "sustainability"):
            sus_cfg = cfg.sustainability
            sus_cfg_dict = {
                "enabled": getattr(sus_cfg, "enabled", True),
                "global_quota_threshold": getattr(sus_cfg, "global_quota_threshold", 0.15),
                "health_check_interval": getattr(sus_cfg, "health_check_interval", 60),
                "auto_failover": getattr(sus_cfg, "auto_failover", True),
                "learning_enabled": getattr(sus_cfg, "learning_enabled", True),
            }
        self._sustainability = SustainabilityManager(self.registry, self.quota, sus_cfg_dict)

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
        model = body.get("model")

        # ===== Smart-Free 智能路由分支 =====
        if model == "smart-free" and self._smart_enabled:
            return await self._route_smart_free(body, task_ctx, tenant)

        # ===== Autofree 模式分支 =====
        if model == "autofree" and self._autofree_enabled:
            return await self._route_autofree(body, task_ctx, tenant)

        # ===== 标准 free-router 模式 =====
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
                with router_select_span(candidates=len(pairs)):
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
                    with provider_call_span(provider=m.provider, model=m.id):
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
                    record_fallback(attempted[0], m.provider, "fallback")
                    logger.info("免费模型切换",
                                from_provider=attempted[0], to_provider=m.provider,
                                model=m.id, tenant=tenant, attempts=attempt + 1)
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

    # ===================== Autofree 专用路由 =====================
    async def _route_autofree(self, body, task_ctx: Optional[dict] = None,
                               tenant: Optional[str] = None):
        """Autofree 模式：Top10 → Fallback → 恢复即切回"""
        if not self._autofree_enabled:
            raise AllExhausted("Autofree 未启用", 0)

        ctx = dict(task_ctx) if task_ctx else parse_task_context(body)
        if tenant:
            ctx.setdefault("tenant", tenant)
        tenant = str(ctx.get("tenant") or DEFAULT_TENANT)

        # 强制只用免费模型
        ctx["prefer_free"] = True

        attempted = []
        notice = None
        autofree_mode = "top10"  # top10 | fallback | recovered

        for attempt in range(self._autofree_max_retries):
            # 1. 获取候选池
            if autofree_mode == "top10":
                scored_models = self.registry.get_autofree_top10()
                candidates = [sm.entry for sm in scored_models]
            elif autofree_mode == "fallback":
                scored_models = self.registry.get_autofree_fallback()
                candidates = [sm.entry for sm in scored_models]
            else:  # recovered 模式：优先尝试刚恢复的 Top10 模型
                # 找出刚从冷却中恢复的 Top10 模型
                recovered = []
                for sm in self.registry.get_autofree_top10():
                    if not self.quota.in_cooldown(sm.entry.provider, sm.entry.id, tenant=tenant):
                        if self.quota.predict_exhausted(sm.entry.provider, sm.entry.id, tenant=tenant):
                            continue
                        recovered.append(sm.entry)
                if recovered:
                    candidates = recovered
                    autofree_mode = "recovered"
                else:
                    candidates = [sm.entry for sm in self.registry.get_autofree_top10()]
                    autofree_mode = "top10"

            # 2. 能力/冷却过滤
            pairs = self._filter_capability(candidates, body)
            pairs = self._filter_cooldown(pairs, tenant)

            if not pairs:
                # 当前层级全不可用 → 降级
                if autofree_mode == "top10":
                    autofree_mode = "fallback"
                    continue
                elif autofree_mode == "fallback":
                    autofree_mode = "top10"  # 兜底再试一次 Top10
                    continue
                else:
                    raise AllExhausted("Autofree 所有层级均不可用", attempt)

            # 3. 按评分排序（Autofree 已预排序，直接用）
            ordered = [e for e, _ in pairs]

            # 4. 粘性（TTL 更短，60s）
            sticky = self._autofree_session.get_sticky()
            if sticky:
                for i, e in enumerate(ordered):
                    if e.provider == sticky[0] and e.id == sticky[1]:
                        ordered.insert(0, ordered.pop(i))
                        break

            if not ordered:
                continue

            # 5. 逐个尝试
            for m in ordered:
                provider = self.providers.get(m.provider)
                if not provider:
                    continue
                t0 = time.time()
                try:
                    with provider_call_span(provider=m.provider, model=m.id):
                        resp = await provider.chat(body, model=m.id)
                except QuotaExhausted as e:
                    metrics.count_request(m.provider, m.id, "quota")
                    self.quota.apply_cooldown(m.provider, m.id, "429", tenant=tenant)
                    self.alerter.record(False)
                    attempted.append(m.provider)
                    # 记录 autofree 失败
                    if hasattr(self.registry, "record_autofree_failure"):
                        self.registry.record_autofree_failure(m.provider, m.id)
                    continue
                except ProviderError as e:
                    metrics.count_request(m.provider, m.id, "error")
                    reason = self.quota.learn_from_error(m.provider, m.id, e, tenant=tenant)
                    if reason == "model_not_found":
                        self.registry.remove(m.id)
                    self.alerter.record(False)
                    attempted.append(m.provider)
                    if hasattr(self.registry, "record_autofree_failure"):
                        self.registry.record_autofree_failure(m.provider, m.id)
                    continue

                ms = (time.time() - t0) * 1000.0
                metrics.observe_latency(m.provider, m.id, ms)
                metrics.count_request(m.provider, m.id, "success")
                metrics.count_quota(m.provider, m.id)
                if attempted and attempted[0] != m.provider:
                    metrics.count_fallback(attempted[0], m.provider)
                    record_fallback(attempted[0], m.provider, "fallback")
                    logger.info("免费模型切换",
                                from_provider=attempted[0], to_provider=m.provider,
                                model=m.id, tenant=tenant, attempts=attempt + 1)
                self.alerter.record(True)

                usage = resp.get("usage") if isinstance(resp, dict) else None
                self.quota.record(m.provider, m.id, usage=usage, tenant=tenant)

                # 记录 autofree 命中与成功
                if hasattr(self.registry, "record_autofree_hit"):
                    self.registry.record_autofree_hit(m.provider, m.id)
                if hasattr(self.registry, "record_autofree_success"):
                    self.registry.record_autofree_success(m.provider, m.id, ms)

                self._autofree_session.set_sticky(m.provider, m.id)

                # 构建响应元信息
                rank = self.registry.get_autofree_rank(m.provider, m.id) or 0
                tier = self.registry.get_autofree_tier(m.provider, m.id)
                notice = f"Autofree [{tier}] rank={rank}: {m.provider}/{m.id}"
                if autofree_mode == "recovered":
                    notice += " (recovered)"

                meta = {
                    "provider": m.provider,
                    "model": m.id,
                    "type": "free",
                    "tier": getattr(m, "tier", "medium"),
                    "tags": list(getattr(m, "tags", []) or []),
                    "notice": notice,
                    "tenant": tenant,
                    "attempts": attempt + 1,
                    "fallbacks": attempted[:],
                    "latency_ms": round(ms, 1),
                    "autofree_mode": autofree_mode,
                    "autofree_rank": rank,
                }

                if hasattr(resp, "__aiter__"):
                    return StreamResult(resp, meta)
                return ChatResult(resp, meta=meta)

        raise AllExhausted(f"Autofree 耗尽所有重试 (mode={autofree_mode})", self._autofree_max_retries)

    # ===================== Smart-Free 智能路由 =====================
    async def _route_smart_free(self, body, task_ctx: Optional[dict] = None,
                                 tenant: Optional[str] = None):
        """Smart-Free 模式：任务感知 → 匹配最优模型 → 负载均衡调度 → 跨商配额池管理"""
        if not self._smart_enabled:
            raise AllExhausted("Smart-Free 未启用", 0)

        ctx = dict(task_ctx) if task_ctx else parse_task_context(body)
        if tenant:
            ctx.setdefault("tenant", tenant)
        tenant = str(ctx.get("tenant") or DEFAULT_TENANT)

        # 1. 推断任务类型
        task_type = self._smart_router.infer_task_type(body)
        need_vision = self._needs_vision(body)

        # 2. 获取智能路由推荐
        candidates = self._smart_router.select_candidates(task_type, need_vision)
        if not candidates:
            # 回退到标准模式
            candidates = self.registry.list_active()

        # 3. 能力/冷却过滤
        pairs = self._filter_capability(candidates, body)
        pairs = self._filter_cooldown(pairs, tenant)

        if not pairs:
            raise AllExhausted("Smart-Free 无可用候选", 0)

        # 4. 按智能路由匹配分排序
        ordered = self._smart_router.rank_candidates(pairs, task_type)

        # 5. 粘性（TTL 300s）
        sticky = self._smart_session.get_sticky()
        if sticky:
            for i, e in enumerate(ordered):
                if e.provider == sticky[0] and e.id == sticky[1]:
                    ordered.insert(0, ordered.pop(i))
                    break

        # 6. 逐个尝试（最多重试 50 次）
        attempted = []
        notice = None

        for attempt in range(self._smart_max_retries):
            if not ordered:
                # 尝试从可持续性管理器获取备用模型
                fallback = self._sustainability.get_fallback_provider()
                if fallback:
                    fb_candidates = self.registry.list_active(provider=fallback)
                    fb_pairs = self._filter_capability(fb_candidates, body)
                    fb_pairs = self._filter_cooldown(fb_pairs, tenant)
                    if fb_pairs:
                        ordered = self._smart_router.rank_candidates(fb_pairs, task_type)
                        continue
                raise AllExhausted("Smart-Free 耗尽所有候选", attempt)

            for m in ordered:
                provider = self.providers.get(m.provider)
                if not provider:
                    continue
                t0 = time.time()
                try:
                    with provider_call_span(provider=m.provider, model=m.id):
                        resp = await provider.chat(body, model=m.id)
                except QuotaExhausted as e:
                    metrics.count_request(m.provider, m.id, "quota")
                    self.quota.apply_cooldown(m.provider, m.id, "429", tenant=tenant)
                    self.alerter.record(False)
                    attempted.append(m.provider)
                    # 更新可持续性健康分
                    self._sustainability.record_failure(m.provider)
                    # 从候选列表移除
                    ordered = [x for x in ordered if x.id != m.id or x.provider != m.provider]
                    continue
                except ProviderError as e:
                    metrics.count_request(m.provider, m.id, "error")
                    reason = self.quota.learn_from_error(m.provider, m.id, e, tenant=tenant)
                    if reason == "model_not_found":
                        self.registry.remove(m.id)
                    self.alerter.record(False)
                    attempted.append(m.provider)
                    self._sustainability.record_failure(m.provider)
                    ordered = [x for x in ordered if x.id != m.id or x.provider != m.provider]
                    continue

                ms = (time.time() - t0) * 1000.0
                metrics.observe_latency(m.provider, m.id, ms)
                metrics.count_request(m.provider, m.id, "success")
                metrics.count_quota(m.provider, m.id)
                if attempted and attempted[0] != m.provider:
                    metrics.count_fallback(attempted[0], m.provider)
                    record_fallback(attempted[0], m.provider, "fallback")
                    logger.info("免费模型切换",
                                from_provider=attempted[0], to_provider=m.provider,
                                model=m.id, tenant=tenant, attempts=attempt + 1)
                self.alerter.record(True)

                # 更新可持续性健康分
                self._sustainability.record_success(m.provider, ms)

                usage = resp.get("usage") if isinstance(resp, dict) else None
                self.quota.record(m.provider, m.id, usage=usage, tenant=tenant)
                self._smart_session.set_sticky(m.provider, m.id)

                # 构建响应元信息
                match_score = self._smart_router.get_match_score(m.provider, m.id, task_type)
                health = self._sustainability.get_provider_health(m.provider)
                global_quota = self._sustainability.get_global_quota_remaining()
                notice = (f"Smart-Free [{task_type.value}] {m.provider}/{m.id} "
                          f"(match={match_score:.1f}, health={health:.0f}, "
                          f"global_quota={global_quota:.0%})")

                meta = {
                    "provider": m.provider,
                    "model": m.id,
                    "type": "free",
                    "tier": getattr(m, "tier", "medium"),
                    "tags": list(getattr(m, "tags", []) or []),
                    "notice": notice,
                    "tenant": tenant,
                    "attempts": attempt + 1,
                    "fallbacks": attempted[:],
                    "latency_ms": round(ms, 1),
                    "smart_free_task_type": task_type.value,
                    "smart_free_match_score": round(match_score, 1),
                    "provider_health": round(health, 1),
                    "global_quota_remaining": round(global_quota, 4),
                }

                if hasattr(resp, "__aiter__"):
                    return StreamResult(resp, meta)
                return ChatResult(resp, meta=meta)

        raise AllExhausted(f"Smart-Free 耗尽所有重试 (attempted={attempted})", self._smart_max_retries)