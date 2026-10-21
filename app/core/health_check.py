# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""Health check and provider monitoring.

Provides periodic health checks for providers, with automatic
state updates and integration with circuit breaker.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .circuit_breaker import get_circuit_breaker_manager
from .observability import get_logger, set_provider_health

logger = get_logger("health_check")


@dataclass
class HealthCheckConfig:
    """Configuration for health checks."""
    interval_seconds: float = 30.0          # Check interval
    timeout_seconds: float = 5.0            # Request timeout
    ping_prompt: str = "ping"               # Test prompt
    max_tokens: int = 1                     # Minimal tokens
    temperature: float = 0.0
    consecutive_failures_threshold: int = 3  # Failures before marking unhealthy
    consecutive_successes_threshold: int = 2 # Successes to mark healthy


@dataclass
class ProviderHealthState:
    """Health state of a provider."""
    provider: str
    healthy: bool = True
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_check: float = 0.0
    last_latency_ms: Optional[float] = None
    error_message: Optional[str] = None
    model_results: Dict[str, Dict] = field(default_factory=dict)  # model -> {ok, latency, error}


class HealthCheckManager:
    """Manages periodic health checks for all providers."""
    
    def __init__(self, config: HealthCheckConfig, providers: Dict, registry):
        self.config = config
        self.providers = providers
        self.registry = registry
        self._states: Dict[str, ProviderHealthState] = {}
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._circuit_breaker = get_circuit_breaker_manager()
    
    def _init_states(self) -> None:
        """Initialize health states for all providers."""
        for name in self.providers.keys():
            self._states[name] = ProviderHealthState(provider=name)
    
    async def start(self) -> None:
        """Start the periodic health check task."""
        self._init_states()
        self._task = asyncio.create_task(self._run_loop())
        logger.info("health_check_started", interval=self.config.interval_seconds)
    
    async def stop(self) -> None:
        """Stop the health check task."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("health_check_stopped")
    
    async def _run_loop(self) -> None:
        """Main health check loop."""
        while True:
            try:
                await asyncio.sleep(self.config.interval_seconds)
                await self._check_all_providers()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("health_check_loop_error", error=str(e))
                await asyncio.sleep(5)  # Brief pause before retry
    
    async def _check_all_providers(self) -> None:
        """Check all providers concurrently."""
        tasks = [
            self._check_provider(name, provider)
            for name, provider in self.providers.items()
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _check_provider(self, name: str, provider) -> None:
        """Check a single provider's health."""
        state = self._states.get(name)
        if not state:
            return
        
        models = [m for m in self.registry.list_active() if m.provider == name]
        if not models:
            logger.debug("health_check_no_models", provider=name)
            return
        
        # Test first few models
        test_models = models[:min(3, len(models))]
        all_ok = True
        results = {}
        
        for model in test_models:
            t0 = time.time()
            try:
                await asyncio.wait_for(
                    provider.chat(
                        {"messages": [{"role": "user", "content": self.config.ping_prompt}],
                         "max_tokens": self.config.max_tokens,
                         "temperature": self.config.temperature},
                        model=model.id
                    ),
                    timeout=self.config.timeout_seconds
                )
                latency_ms = (time.time() - t0) * 1000
                results[model.id] = {"ok": True, "latency_ms": round(latency_ms, 1)}
            except Exception as e:
                latency_ms = (time.time() - t0) * 1000
                results[model.id] = {"ok": False, "latency_ms": round(latency_ms, 1), "error": str(e)[:160]}
                all_ok = False
        
        # Update state
        state.last_check = time.time()
        state.model_results = results
        state.last_latency_ms = min((r["latency_ms"] for r in results.values() if r["ok"]), default=None)
        
        if all_ok:
            state.consecutive_successes += 1
            state.consecutive_failures = 0
            state.error_message = None
            
            if state.consecutive_successes >= self.config.consecutive_successes_threshold:
                if not state.healthy:
                    state.healthy = True
                    logger.info("provider_recovered", provider=name)
                    await self._circuit_breaker.record_success(name, list(results.keys())[0])
        else:
            state.consecutive_failures += 1
            state.consecutive_successes = 0
            state.error_message = f"{sum(1 for r in results.values() if not r['ok'])}/{len(results)} models failed"
            
            if state.consecutive_failures >= self.config.consecutive_failures_threshold:
                if state.healthy:
                    state.healthy = False
                    logger.warning("provider_unhealthy", provider=name, error=state.error_message)
                    # Record failure for circuit breaker
                    for model_id, result in results.items():
                        if not result["ok"]:
                            await self._circuit_breaker.record_failure(name, model_id)
        
        # Update health score based on success rate
        total = state.consecutive_successes + state.consecutive_failures
        if total > 0:
            health_score = state.consecutive_successes / total
            set_provider_health(name, health_score)
    
    async def force_check(self, provider: str = None) -> Dict:
        """Force an immediate health check (for admin endpoint)."""
        if provider:
            if provider in self.providers:
                await self._check_provider(provider, self.providers[provider])
                return self.get_state(provider)
            raise ValueError(f"Unknown provider: {provider}")
        else:
            await self._check_all_providers()
            return self.get_all_states()
    
    def get_state(self, provider: str = None) -> Dict:
        """Get current health state(s)."""
        if provider:
            state = self._states.get(provider)
            if not state:
                return {}
            return {
                "provider": state.provider,
                "healthy": state.healthy,
                "consecutive_failures": state.consecutive_failures,
                "consecutive_successes": state.consecutive_successes,
                "last_check": state.last_check,
                "last_latency_ms": state.last_latency_ms,
                "error_message": state.error_message,
                "model_results": state.model_results,
            }
        return {
            name: {
                "provider": s.provider,
                "healthy": s.healthy,
                "consecutive_failures": s.consecutive_failures,
                "consecutive_successes": s.consecutive_successes,
                "last_check": s.last_check,
                "last_latency_ms": s.last_latency_ms,
                "error_message": s.error_message,
            }
            for name, s in self._states.items()
        }
    
    def is_healthy(self, provider: str) -> bool:
        """Quick check if a provider is healthy."""
        state = self._states.get(provider)
        return state.healthy if state else True


# Global instance
_health_check_manager: Optional[HealthCheckManager] = None


def get_health_check_manager(
    config: HealthCheckConfig = None,
    providers: Dict = None,
    registry = None
) -> HealthCheckManager:
    """Get or create the global health check manager."""
    global _health_check_manager
    if _health_check_manager is None and providers and registry:
        _health_check_manager = HealthCheckManager(config or HealthCheckConfig(), providers, registry)
    return _health_check_manager