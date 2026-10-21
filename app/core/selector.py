# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""任务感知模型选择器（P0）。

职责：在“候选模型（provider+model）”之间做打分与排序，支持：
- 免费/收费（type: free|paid）偏好，带混用提醒（notice）；
- 任务上下文（task_type / required_tags / prefer_free），从请求体解析；
- 质量分级（tier）权重——高质量大模型优先，小模型只兜底；
- 可插拔策略：打分函数可由外部 policy 模块覆盖（见 policy.py）。

设计约束（不得破坏既有行为）：未提供任务上下文时，打分必须退化为
`capability 分 + tier 权重`，与历史路由结果完全一致。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

_TIER_BOOST_DEFAULT = {"high": 200.0, "medium": 100.0, "low": 0.0}

# 任务类型 → 需要的标签/能力（用于加权）
TASK_TAG_HINTS: Dict[str, List[str]] = {
    "code_generation": ["code", "reasoning"],
    "code_review": ["code", "reasoning"],
    "reasoning": ["reasoning"],
    "image_description": ["vision"],
    "vision": ["vision"],
    "chat": ["chat", "general"],
    "writing": ["chat", "general"],
    "summarization": ["long", "chat"],
    "translation": ["chat", "general"],
}

# 声明这些标签时按「硬能力」处理：不满足的候选直接排除（列表清空时退回，不中断任务）
HARD_CAPS = ("vision",)

def cap_unmet_msg(caps) -> str:
    return ("no available model satisfies required capabilities "
            f"{sorted(caps)} (candidates cooling or unconfigured); "
            "using the best available model instead")


TAG_MATCH_BONUS = 25.0          # 每个命中标签的加分
TASK_HIT_BONUS = 30.0           # 任务类型命中模型标签的加分
FREE_AFFINITY_BONUS = 10.0      # 偏好免费时给免费模型的微幅加分
PRIORITY_UNIT = 5.0             # 每个 priority 单位的加分
# 用户收藏（常用模型）：给一个足够大的加成，使「常用模型一恢复就立刻回到队首」；
# 列表越靠前加成越高，但整体低于 tier 旗舰权重，避免把低质模型强推上来。
FAVORITE_BONUS = 150.0
FAVORITE_STEP = 5.0
# 长文本任务加权：上下文窗口越大加分越多（封顶，避免长窗小模型霸榜）
LONG_CONTEXT_TASKS = ("summarization", "long_context", "translation", "writing")
LONG_CONTEXT_STEP = 32768.0          # 每 32K 上下文
LONG_CONTEXT_UNIT = 6.0              # 每 32K 加 6 分
LONG_CONTEXT_MAX_BONUS = 24.0        # 封顶


def parse_task_context(body: dict) -> Dict[str, Any]:
    """从请求体解析任务上下文。

    位置（按优先级）：
      1) body["free_router"]        —— 显式结构（推荐）
      2) body["user"]               —— OpenAI 兼容字段，值为 JSON 字符串或 dict
      3) body["metadata"]           —— 部分框架使用
    支持键：task_type / prefer_free / required_tags / tenant / model_type
    """
    ctx: Dict[str, Any] = {}
    for key in ("free_router", "metadata", "user"):
        raw = body.get(key)
        if isinstance(raw, dict):
            ctx.update(raw)
        elif isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    ctx.update(obj)
            except Exception:
                pass
    return ctx


class ModelSelector:
    """打分 + 排序 + 提醒。"""

    def __init__(self, cfg, policy=None):
        self.cfg = cfg
        self.policy = policy            # 可插拔策略（需实现 score(entry, ctx, base)）

    # ---------- 打分 ----------
    @property
    def tier_boost(self) -> dict:
        return getattr(self.cfg.router, "tier_boost", None) or _TIER_BOOST_DEFAULT

    def score(self, entry, ctx: Dict[str, Any], base: float) -> float:
        """base = 调用方算好的 capability + tier 权重分。"""
        if self.policy is not None:
            try:
                return float(self.policy.score(entry, ctx, base))
            except Exception:
                pass                        # 策略异常 → 退回内置算法，不中断任务
        s = float(base)
        if not ctx:
            return s                        # 无上下文：与历史行为完全一致
        tags = set(getattr(entry, "tags", []) or [])
        req = ctx.get("required_tags") or []
        if isinstance(req, str):
            req = [t.strip() for t in req.split(",") if t.strip()]
        for t in req:
            if t in tags or getattr(entry, "capability", {}).get(t):
                s += TAG_MATCH_BONUS
        task = ctx.get("task_type") or ""
        for hint in TASK_TAG_HINTS.get(str(task), []):
            if hint in tags or getattr(entry, "capability", {}).get(hint):
                s += TASK_HIT_BONUS
                break
        if ctx.get("prefer_free", True) and getattr(entry, "type", "free") == "free":
            s += FREE_AFFINITY_BONUS
        s += PRIORITY_UNIT * float(getattr(entry, "priority", 0) or 0)
        # 长文本任务：按上下文窗口大小加权（越大加分越多，有上限）
        if task and task in LONG_CONTEXT_TASKS:
            cw = int(getattr(entry, "context_window", 0) or 0)
            if cw > 0:
                s += min(LONG_CONTEXT_MAX_BONUS, cw / LONG_CONTEXT_STEP * LONG_CONTEXT_UNIT)
        # 用户收藏（常用模型）：越靠前加成越高 —— 常用模型一旦恢复立即回到队首
        favs = getattr(self.cfg, "favorites", None) or []
        if favs:
            key = f"{getattr(entry, 'provider', '')}/{getattr(entry, 'id', '')}"
            try:
                s += FAVORITE_BONUS - favs.index(key) * FAVORITE_STEP
            except ValueError:
                pass
        return s

    # ---------- 硬能力过滤 ----------
    def _hard_filter(self, pairs, ctx):
        """required_tags / task_type 里声明的硬能力（如 vision）不满足 → 排除。"""
        if not ctx:
            return pairs, False
        req = ctx.get("required_tags") or []
        if isinstance(req, str):
            req = [t.strip() for t in req.split(",") if t.strip()]
        hints = TASK_TAG_HINTS.get(str(ctx.get("task_type") or ""), [])
        need = {c for c in (list(req) + list(hints)) if c in HARD_CAPS}
        if not need:
            return pairs, False
        keep = [p for p in pairs
                if all(getattr(p[0], "capability", {}).get(c) for c in need)]
        if keep:
            return keep, False
        # 一个都不满足（全部在冷却/未配置）→ 退回原候选保证不中断，但如实提示
        return pairs, True

    # ---------- 提醒（以实际命中的模型为准）----------
    def notice_for(self, entry, ctx) -> Optional[str]:
        if not ctx or entry is None:
            return None
        t = getattr(entry, "type", "free")
        prefer_free = bool(ctx.get("prefer_free", True))
        if prefer_free and t != "free":
            return (f"using PAID model {entry.provider}/{entry.id} "
                    f"(free pool unavailable or paid model explicitly requested)")
        if ctx.get("prefer_free") is False and t == "free":
            return (f"prefer_free=false but the model actually used is FREE "
                    f"{entry.provider}/{entry.id}")
        return None

    # ---------- 排序 ----------
    def order(self, pairs: List[Tuple[Any, float]], ctx: Optional[Dict] = None
              ) -> Tuple[List[Any], Optional[str]]:
        """pairs: [(ModelEntry, base_score)] → (有序 entries, notice)。

        免费/收费规则：
        - ctx.prefer_free 缺省 True：优先只走免费模型；免费模型全不可用时，
          才允许用收费模型兜底，并给出明确提醒。
        - ctx.prefer_free False 或 ctx.model_type == "paid"：两者同场竞争。
        """
        ctx = ctx or {}
        pairs, cap_unmet = self._hard_filter(pairs, ctx)
        prefer_free = bool(ctx.get("prefer_free", True))
        want_paid = ctx.get("model_type") == "paid" or prefer_free is False
        scored = [(self.score(e, ctx, base), e) for e, base in pairs]

        frees = [x for x in scored if getattr(x[1], "type", "free") == "free"]
        paids = [x for x in scored if getattr(x[1], "type", "free") != "free"]

        def _sort(xs):
            return [e for _, e in sorted(xs, key=lambda x: -x[0])]

        unmet = cap_unmet_msg(sorted(c for c in
                                     ((ctx.get("required_tags") or []) and
                                      [t for t in ctx["required_tags"]])
                                     if c in HARD_CAPS)) if cap_unmet else None

        if not want_paid:
            if frees:
                return _sort(frees), unmet
            if paids:
                best = _sort(paids)[0]
                return _sort(paids), (
                    f"free pool exhausted: falling back to PAID model "
                    f"{best.provider}/{best.id}")
            return [], None

        ordered = _sort(scored)
        if not ordered:
            return [], None
        best = ordered[0]
        if unmet:
            return ordered, unmet
        if prefer_free is False and getattr(best, "type", "free") == "free":
            notice = (f"prefer_free=false but best candidate is FREE "
                      f"{best.provider}/{best.id} (no paid model ranked higher)")
        elif best.type != "free":
            notice = (f"using PAID model {best.provider}/{best.id} "
                      f"(model_type={ctx.get('model_type') or 'auto'})")
        else:
            notice = None
        return ordered, notice

    # ---------- 元信息 ----------
    @staticmethod
    def describe(entry, ctx: Optional[Dict] = None) -> Dict[str, Any]:
        return {
            "provider": entry.provider,
            "model": entry.id,
            "type": getattr(entry, "type", "free"),
            "tier": getattr(entry, "tier", "medium"),
            "tags": list(getattr(entry, "tags", []) or []),
        }