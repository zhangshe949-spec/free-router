"""Sustainability Manager - Cross-provider quota pool, health scoring, predictive rotation."""

from dataclasses import dataclass, field
from typing import Optional, Dict, List
import time
import math


@dataclass
class ProviderHealth:
    provider: str
    health_score: float = 100.0
    avg_latency_ms: float = 0.0
    success_rate: float = 1.0
    requests_total: int = 0
    last_used: float = 0.0
    last_failure: float = 0.0
    consecutive_failures: int = 0

    def update_success(self, latency_ms: float):
        self.requests_total += 1
        self.last_used = time.time()
        self.consecutive_failures = 0
        # Exponential moving average for latency
        if self.avg_latency_ms == 0:
            self.avg_latency_ms = latency_ms
        else:
            self.avg_latency_ms = self.avg_latency_ms * 0.7 + latency_ms * 0.3
        self._recompute_health()

    def update_failure(self):
        self.requests_total += 1
        self.last_failure = time.time()
        self.consecutive_failures += 1
        self._recompute_health()

    def _recompute_health(self):
        # Success rate weight: 40%
        sr_score = self.success_rate * 40
        # Latency weight: 30% (lower is better, cap at 1000ms)
        lat_score = max(0, 30 - (self.avg_latency_ms / 1000) * 30)
        # Error rate weight: 20%
        err_score = max(0, 20 - self.consecutive_failures * 5)
        # Recency weight: 10%
        rec_score = 10 if (time.time() - self.last_used) < 300 else 5
        self.health_score = min(100, sr_score + lat_score + err_score + rec_score)


@dataclass
class QuotaAlert:
    provider: str
    quota_type: str  # rpm | rpd | tpm | tpd
    remaining: float
    threshold: float
    timestamp: float = field(default_factory=time.time)


class SustainabilityManager:
    """Cross-provider quota pool management with predictive rotation."""

    def __init__(self, registry, quota, config=None):
        self.registry = registry
        self.quota = quota
        self._enabled = (config or {}).get("enabled", True)
        self._global_quota_threshold = (config or {}).get("global_quota_threshold", 0.15)
        self._health_check_interval = (config or {}).get("health_check_interval", 60)
        self._auto_failover = (config or {}).get("auto_failover", True)
        self._learning_enabled = (config or {}).get("learning_enabled", True)
        self._provider_health: Dict[str, ProviderHealth] = {}
        self._alerts: List[QuotaAlert] = []
        self._last_health_check = 0.0

    def _ensure_health(self, provider: str):
        if provider not in self._provider_health:
            self._provider_health[provider] = ProviderHealth(provider=provider)
        return self._provider_health[provider]

    def record_success(self, provider: str, latency_ms: float):
        health = self._ensure_health(provider)
        health.update_success(latency_ms)

    def record_failure(self, provider: str):
        health = self._ensure_health(provider)
        health.update_failure()

    def get_provider_health(self, provider: str) -> float:
        health = self._ensure_health(provider)
        return health.health_score

    def get_all_provider_health(self) -> Dict[str, float]:
        return {p: h.health_score for p, h in self._provider_health.items()}

    def get_provider_details(self) -> List[Dict]:
        return [
            {
                "provider": p,
                "health_score": h.health_score,
                "avg_latency_ms": round(h.avg_latency_ms, 1),
                "success_rate": round(h.success_rate, 4),
                "requests_total": h.requests_total,
                "consecutive_failures": h.consecutive_failures,
            }
            for p, h in self._provider_health.items()
        ]

    def get_global_quota_remaining(self) -> float:
        """Return estimated global quota remaining as fraction 0-1."""
        total = 0
        used = 0
        for provider_name in self._get_all_providers():
            try:
                rpm = self.quota.get_quota_remaining(provider_name, None, "rpm")
                rpd = self.quota.get_quota_remaining(provider_name, None, "rpd")
                total += 100
                used += (100 - rpm) + (100 - rpd)
            except Exception:
                pass
        if total == 0:
            return 1.0
        return max(0, 1.0 - (used / (total * 2)))

    def get_global_quota_total(self) -> float:
        return 1.0

    def get_fallback_provider(self) -> Optional[str]:
        """Return the healthiest provider that is not currently in cooldown."""
        best = None
        best_score = 0
        for provider_name in self._get_all_providers():
            health = self._ensure_health(provider_name)
            if health.health_score > best_score:
                best_score = health.health_score
                best = provider_name
        return best

    def get_alerts(self) -> List[Dict]:
        return [
            {
                "provider": a.provider,
                "quota_type": a.quota_type,
                "remaining": round(a.remaining, 4),
                "threshold": a.threshold,
                "timestamp": a.timestamp,
            }
            for a in self._alerts[-50:]
        ]

    def _get_all_providers(self) -> List[str]:
        return list(self.registry._providers.keys()) if hasattr(self.registry, "_providers") else []

    def reset(self):
        self._provider_health.clear()
        self._alerts.clear()

    async def force_health_check(self, provider: str = None):
        """Manual health check trigger."""
        self._last_health_check = time.time()
        if provider:
            self._ensure_health(provider)
        else:
            for p in self._get_all_providers():
                self._ensure_health(p)