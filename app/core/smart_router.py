"""Smart Router - Task-aware intelligent routing engine."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import math
import time


class TaskType(Enum):
    CODE = "code"
    REASONING = "reason"
    CHAT = "chat"
    VISION = "vision"
    LONG = "long"
    GENERAL = "general"


@dataclass
class TaskRequirement:
    task_type: TaskType
    need_vision: bool = False
    min_context: int = 0
    max_latency_ms: float = 0.0


@dataclass
class ProviderStats:
    provider: str
    health_score: float = 100.0
    avg_latency_ms: float = 0.0
    success_rate: float = 1.0
    requests_total: int = 0
    last_used: float = 0.0


class SmartRouter:
    def __init__(self, registry, quota, config=None):
        self.registry = registry
        self.quota = quota
        self._load_balance_strategy = (config or {}).get("load_balance_strategy", "weighted_round_robin")
        self._sticky_ttl = (config or {}).get("sticky_ttl_seconds", 300)
        self._max_retries = (config or {}).get("max_retries", 50)
        self._min_runtime_requests = (config or {}).get("min_runtime_requests", 5)
        self._sticky_provider = None
        self._sticky_model = None
        self._sticky_ts = 0.0
        self._match_cache = {}
        self._last_recompute = 0.0

    def infer_task_type(self, body):
        for msg in body.get("messages", []):
            if msg.get("role") == "system":
                s = str(msg.get("content", ""))
                if any(k in s for k in ("code", "代码", "函数", "bug", "程序", "python", "def ")):
                    return TaskType.CODE
                if any(k in s for k in ("reason", "推理", "逻辑", "证明")):
                    return TaskType.REASONING
        for msg in body.get("messages", []):
            if isinstance(msg.get("content"), list):
                for c in msg["content"]:
                    if c.get("type") == "image_url":
                        return TaskType.VISION
        return TaskType.GENERAL

    def select_candidates(self, task_type, need_vision=False):
        all_models = [m for m in self.registry.list_active() if getattr(m, "type", "free") == "free"]
        if need_vision:
            all_models = [m for m in all_models if (m.capability or {}).get("vision")]
        return all_models

    def rank_candidates(self, candidates, task_type):
        def match_score(entry):
            cap = entry.capability or {}
            task_key = task_type.value
            task_cap = cap.get(task_key, 50)
            base = cap.get("score", 50)
            tier_w = {"high": 1.2, "medium": 1.0, "low": 0.8}.get(entry.tier, 1.0)
            return base * tier_w * 0.5 + task_cap * 0.5
        return sorted(candidates, key=lambda e: -match_score(e))

    def get_match_score(self, provider, model, task_type):
        return self._match_cache.get(f"{provider}/{model}/{task_type.value}", 50.0)

    def get_top_candidates(self, task_type, limit=20):
        candidates = self.select_candidates(task_type, False)
        ranked = self.rank_candidates(candidates, task_type)
        return [{"provider": e.provider, "model": e.id, "score": 50.0} for e in ranked[:limit]]

    def get_provider_stats(self):
        return {}

    def recompute_match_scores(self, force=False):
        self._last_recompute = time.time()