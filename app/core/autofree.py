"""Autofree 核心调度模块：评分、榜单、定期重算。

设计目标：
- 从 70+ 免费模型中自动选出 Top 10「最强、最稳、最快」
- 前 10 耗尽自动顺延到第 11~20 名
- 冷却恢复后下一请求立即切回（零感知）
- 静态能力分 + 运行时统计双权重，冷启动仅用静态分
"""
from __future__ import annotations

import math
import time
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .registry import ModelEntry

logger = logging.getLogger("free_router.autofree")

# ===================== 配置常量 =====================
TIER_WEIGHT = {"high": 1.2, "medium": 1.0, "low": 0.8}
TAG_BONUS = {"vision": 5, "reasoning": 5, "code": 5}
CONTEXT_STEP = 32768
CONTEXT_UNIT = 6
CONTEXT_MAX_BONUS = 24
SUCCESS_RATE_WEIGHT = 30
LATENCY_WEIGHT = 10
USAGE_WEIGHT = 2
MIN_RUNTIME_REQUESTS = 5  # 运行时统计生效门槛


@dataclass
class AutofreeConfig:
    enabled: bool = True
    top_n: int = 10
    fallback_n: int = 10
    recompute_interval_hours: int = 1
    sticky_ttl_seconds: int = 60
    max_retries: int = 30
    min_runtime_requests: int = MIN_RUNTIME_REQUESTS


@dataclass
class RuntimeStats:
    """运行时统计（从 Quota DB 聚合）"""
    requests_total: int = 0
    success_total: int = 0
    total_latency_ms: float = 0.0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    consecutive_failures: int = 0

    @property
    def success_rate(self) -> float:
        if self.requests_total == 0:
            return 0.0
        return self.success_total / self.requests_total

    @property
    def avg_latency_ms(self) -> float:
        if self.success_total == 0:
            return 0.0
        return self.total_latency_ms / self.success_total


@dataclass
class ScoredModel:
    entry: ModelEntry
    static_score: float
    runtime_score: float
    final_score: float
    runtime_stats: RuntimeStats
    rank: int = 0
    tier: str = "top10"  # top10 | fallback | cooling | recovered


class AutofreeScheduler:
    """Autofree 调度器：评分、榜单维护、定期重算"""

    def __init__(self, registry, quota, cfg: AutofreeConfig):
        self.registry = registry
        self.quota = quota
        self.cfg = cfg
        self._top_list: List[ScoredModel] = []
        self._fallback_list: List[ScoredModel] = []
        self._last_recompute = 0.0
        self._model_stats_cache: Dict[str, RuntimeStats] = {}

    # ---------- 静态评分 ----------
    def _static_score(self, entry: ModelEntry) -> float:
        """纯静态能力评分（不依赖运行时数据）"""
        cap = getattr(entry, "capability", {}) or {}
        base = float(cap.get("score", 50))
        tier = getattr(entry, "tier", "medium")
        score = base * TIER_WEIGHT.get(tier, 1.0)

        # 长上下文加分
        cw = int(getattr(entry, "context_window", 0) or 0)
        if cw > 0:
            score += min(CONTEXT_MAX_BONUS, cw / CONTEXT_STEP * CONTEXT_UNIT)

        # 关键标签加分
        tags = set(getattr(entry, "tags", []) or [])
        for tag, bonus in TAG_BONUS.items():
            if tag in tags or cap.get(tag):
                score += bonus

        return score

    # ---------- 运行时评分 ----------
    def _runtime_score(self, stats: RuntimeStats) -> float:
        """运行时统计评分（成功率、延迟、使用量）"""
        if stats.requests_total < self.cfg.min_runtime_requests:
            return 0.0

        # 成功率权重（0~1 -> 0~30）
        sr_score = stats.success_rate * SUCCESS_RATE_WEIGHT

        # 延迟权重（越低越好，封顶 10）
        lat_score = 0.0
        if stats.avg_latency_ms > 0:
            lat_score = (1.0 / (1.0 + stats.avg_latency_ms / 1000.0)) * LATENCY_WEIGHT

        # 使用量对数权重
        usage_score = math.log(stats.requests_total + 1) * USAGE_WEIGHT

        return sr_score + lat_score + usage_score

    # ---------- 综合评分 ----------
    def _compute_final_score(self, entry: ModelEntry, stats: RuntimeStats) -> Tuple[float, float, float]:
        static = self._static_score(entry)
        runtime = self._runtime_score(stats)
        # 运行时权重：请求数越多权重越大，线性增至 1.0
        weight = min(1.0, stats.requests_total / (self.cfg.min_runtime_requests * 5))
        final = static * (1 - weight * 0.3) + runtime * weight
        return final, static, runtime

    # ---------- 榜单重算 ----------
    def recompute(self, force: bool = False) -> None:
        """重新计算 Top10 + Fallback 榜单"""
        now = time.time()
        if not force and (now - self._last_recompute) < self.cfg.recompute_interval_hours * 3600:
            return

        # 1. 拿所有免费模型
        all_models = [
            m for m in self.registry.list_active()
            if getattr(m, "type", "free") == "free"
        ]

        # 2. 批量拉取运行时统计
        self._refresh_stats_cache(all_models)

        # 3. 逐个评分
        scored = []
        for entry in all_models:
            stats = self._model_stats_cache.get(f"{entry.provider}/{entry.id}", RuntimeStats())
            final, static, runtime = self._compute_final_score(entry, stats)
            scored.append(ScoredModel(
                entry=entry,
                static_score=static,
                runtime_score=runtime,
                final_score=final,
                runtime_stats=stats,
            ))

        # 4. 按 final_score 降序排序
        scored.sort(key=lambda x: -x.final_score)

        # 5. 切分 Top10 / Fallback
        top_n = self.cfg.top_n
        fallback_n = self.cfg.fallback_n

        for i, sm in enumerate(scored[:top_n]):
            sm.rank = i + 1
            sm.tier = "top10"
        for i, sm in enumerate(scored[top_n:top_n + fallback_n]):
            sm.rank = top_n + i + 1
            sm.tier = "fallback"

        self._top_list = scored[:top_n]
        self._fallback_list = scored[top_n:top_n + fallback_n]
        self._last_recompute = now

        logger.info(f"[Autofree] 榜单重算完成：Top{len(self._top_list)} + Fallback{len(self._fallback_list)}，耗时 {(time.time()-now)*1000:.1f}ms")

    def _refresh_stats_cache(self, models: List[ModelEntry]) -> None:
        """从 Quota 批量刷新运行时统计缓存"""
        # Quota 记录的是 provider+model 维度的聚合数据
        # 这里简单实现：逐个查询 quota.get_stats(provider, model)
        for entry in models:
            key = f"{entry.provider}/{entry.id}"
            try:
                stats = self.quota.get_model_stats(entry.provider, entry.id)
                if stats:
                    self._model_stats_cache[key] = RuntimeStats(**stats)
            except Exception as e:
                logger.debug(f"[Autofree] 获取 {key} 统计失败: {e}")
                self._model_stats_cache[key] = RuntimeStats()

    # ---------- 对外查询 ----------
    def get_top10(self) -> List[ScoredModel]:
        """获取前 10 名（含状态标记）"""
        self.recompute()
        return self._top_list

    def get_fallback(self) -> List[ScoredModel]:
        """获取 Fallback 名单（第 11~20 名）"""
        self.recompute()
        return self._fallback_list

    def get_all_ranked(self) -> List[ScoredModel]:
        """获取完整榜单（用于管理界面）"""
        self.recompute()
        return self._top_list + self._fallback_list

    def mark_hit(self, provider: str, model: str) -> None:
        """记录命中（用于运行时统计权重）"""
        key = f"{provider}/{model}"
        # 实际统计由 Quota.record() 写入 DB，这里只刷新缓存
        if key in self._model_stats_cache:
            stats = self._model_stats_cache[key]
            stats.requests_total += 1

    def mark_success(self, provider: str, model: str, latency_ms: float) -> None:
        key = f"{provider}/{model}"
        if key in self._model_stats_cache:
            stats = self._model_stats_cache[key]
            stats.success_total += 1
            stats.total_latency_ms += latency_ms
            stats.last_success_at = time.time()
            stats.consecutive_failures = 0

    def mark_failure(self, provider: str, model: str) -> None:
        key = f"{provider}/{model}"
        if key in self._model_stats_cache:
            stats = self._model_stats_cache[key]
            stats.last_failure_at = time.time()
            stats.consecutive_failures += 1

    def is_in_top10(self, provider: str, model: str) -> bool:
        return any(sm.entry.provider == provider and sm.entry.id == model for sm in self._top_list)

    def is_in_fallback(self, provider: str, model: str) -> bool:
        return any(sm.entry.provider == provider and sm.entry.id == model for sm in self._fallback_list)

    def get_rank(self, provider: str, model: str) -> Optional[int]:
        for sm in self._top_list + self._fallback_list:
            if sm.entry.provider == provider and sm.entry.id == model:
                return sm.rank
        return None

    def get_tier(self, provider: str, model: str) -> str:
        for sm in self._top_list:
            if sm.entry.provider == provider and sm.entry.id == model:
                return "top10"
        for sm in self._fallback_list:
            if sm.entry.provider == provider and sm.entry.id == model:
                return "fallback"
        return "none"


# ===================== 便捷函数 =====================
def create_autofree_scheduler(registry, quota, config_dict=None) -> AutofreeScheduler:
    """工厂函数：从配置创建调度器

    config_dict 可以是 dict 或 AutofreeCfg dataclass（由 registry.bind_quota 传入）。
    """
    from dataclasses import asdict
    if config_dict is None:
        cfg = AutofreeConfig()
    elif isinstance(config_dict, AutofreeConfig):
        cfg = config_dict
    elif isinstance(config_dict, dict):
        cfg = AutofreeConfig(**config_dict)
    else:
        # AutofreeCfg 或其他 dataclass -> 转 dict
        cfg = AutofreeConfig(**asdict(config_dict))
    return AutofreeScheduler(registry, quota, cfg)