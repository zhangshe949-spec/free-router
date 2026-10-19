"""Circuit Breaker implementation for provider fault tolerance.

Implements a sliding window circuit breaker pattern:
- CLOSED: Normal operation, requests pass through
- OPEN: Too many failures, requests fail fast
- HALF_OPEN: Testing if service recovered, limited requests allowed
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional

from .observability import get_logger, set_circuit_breaker_state, circuit_breaker_span

logger = get_logger("circuit_breaker")


class CircuitState(Enum):
    CLOSED = 0
    HALF_OPEN = 1
    OPEN = 2


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker behavior."""
    failure_threshold: float = 0.5      # Failure rate to open (0.5 = 50%)
    success_threshold: int = 3          # Successes in half-open to close
    window_size: int = 100              # Sliding window size (requests)
    window_time: float = 60.0           # Time window in seconds
    half_open_max_requests: int = 3     # Max concurrent requests in half-open
    min_requests: int = 10              # Min requests before evaluating


@dataclass
class CircuitBreakerState:
    """Internal state of a circuit breaker."""
    state: CircuitState = CircuitState.CLOSED
    failures: deque = field(default_factory=deque)  # (timestamp, success: bool)
    half_open_successes: int = 0
    half_open_requests: int = 0
    last_state_change: float = field(default_factory=time.time)
    config: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    
    def record_result(self, success: bool) -> None:
        """Record a request result and update state."""
        now = time.time()
        self.failures.append((now, success))
        self._cleanup_window(now)
        
        if self.state == CircuitState.CLOSED:
            if self._should_open():
                self._transition_to_open()
        elif self.state == CircuitState.HALF_OPEN:
            if success:
                self.half_open_successes += 1
                if self.half_open_successes >= self.config.success_threshold:
                    self._transition_to_closed()
            else:
                self._transition_to_open()
    
    def _cleanup_window(self, now: float) -> None:
        """Remove old entries outside the time window."""
        cutoff = now - self.config.window_time
        while self.failures and self.failures[0][0] < cutoff:
            self.failures.popleft()
    
    def _should_open(self) -> bool:
        """Check if failure rate exceeds threshold."""
        if len(self.failures) < self.config.min_requests:
            return False
        failures = sum(1 for _, success in self.failures if not success)
        return (failures / len(self.failures)) >= self.config.failure_threshold
    
    def _transition_to_open(self) -> None:
        """Transition to OPEN state."""
        with circuit_breaker_span(state="open"):
            self.state = CircuitState.OPEN
            self.last_state_change = time.time()
            logger.warning("circuit_breaker_opened",
                          failure_rate=self._failure_rate())
    
    def _transition_to_half_open(self) -> None:
        """Transition to HALF_OPEN state."""
        self.state = CircuitState.HALF_OPEN
        self.half_open_successes = 0
        self.half_open_requests = 0
        self.last_state_change = time.time()
        logger.info("circuit_breaker_half_open")
    
    def _transition_to_closed(self) -> None:
        """Transition to CLOSED state."""
        self.state = CircuitState.CLOSED
        self.failures.clear()
        self.half_open_successes = 0
        self.half_open_requests = 0
        self.last_state_change = time.time()
        logger.info("circuit_breaker_closed")
    
    def _failure_rate(self) -> float:
        if not self.failures:
            return 0.0
        failures = sum(1 for _, success in self.failures if not success)
        return failures / len(self.failures)
    
    def can_execute(self) -> bool:
        """Check if a request can be executed."""
        if self.state == CircuitState.CLOSED:
            return True
        elif self.state == CircuitState.OPEN:
            # Check if we should try half-open
            if time.time() - self.last_state_change >= self.config.window_time:
                self._transition_to_half_open()
                return True
            return False
        else:  # HALF_OPEN
            return self.half_open_requests < self.config.half_open_max_requests
    
    def on_request_start(self) -> None:
        """Call when a request starts in HALF_OPEN state."""
        if self.state == CircuitState.HALF_OPEN:
            self.half_open_requests += 1


class CircuitBreakerManager:
    """Manages circuit breakers for multiple providers/models."""
    
    def __init__(self, default_config: Optional[CircuitBreakerConfig] = None):
        self.default_config = default_config or CircuitBreakerConfig()
        self._breakers: Dict[str, CircuitBreakerState] = {}
        self._lock = asyncio.Lock()
    
    def _get_key(self, provider: str, model: str) -> str:
        return f"{provider}/{model}"
    
    async def get_breaker(self, provider: str, model: str) -> CircuitBreakerState:
        """Get or create a circuit breaker for a provider/model."""
        key = self._get_key(provider, model)
        async with self._lock:
            if key not in self._breakers:
                self._breakers[key] = CircuitBreakerState(config=self.default_config)
            return self._breakers[key]
    
    async def record_success(self, provider: str, model: str) -> None:
        """Record a successful request."""
        breaker = await self.get_breaker(provider, model)
        breaker.record_result(True)
        set_circuit_breaker_state(provider, model, breaker.state.value)
    
    async def record_failure(self, provider: str, model: str) -> None:
        """Record a failed request."""
        breaker = await self.get_breaker(provider, model)
        breaker.record_result(False)
        set_circuit_breaker_state(provider, model, breaker.state.value)
    
    async def can_execute(self, provider: str, model: str) -> bool:
        """Check if a request can be executed (circuit not open)."""
        breaker = await self.get_breaker(provider, model)
        return breaker.can_execute()
    
    async def on_request_start(self, provider: str, model: str) -> None:
        """Notify breaker that a request is starting."""
        breaker = await self.get_breaker(provider, model)
        breaker.on_request_start()
    
    async def get_state(self, provider: str, model: str) -> Dict:
        """Get current state of a circuit breaker."""
        breaker = await self.get_breaker(provider, model)
        return {
            "state": breaker.state.name,
            "failure_rate": breaker._failure_rate(),
            "total_requests": len(breaker.failures),
            "failures": sum(1 for _, s in breaker.failures if not s),
            "last_change": breaker.last_state_change,
        }
    
    async def get_all_states(self) -> Dict[str, Dict]:
        """Get states of all circuit breakers."""
        result = {}
        async with self._lock:
            for key, breaker in self._breakers.items():
                result[key] = {
                    "state": breaker.state.name,
                    "failure_rate": breaker._failure_rate(),
                    "total_requests": len(breaker.failures),
                }
        return result


# Global instance
_circuit_breaker_manager: Optional[CircuitBreakerManager] = None


def get_circuit_breaker_manager(config: Optional[CircuitBreakerConfig] = None) -> CircuitBreakerManager:
    """Get the global circuit breaker manager."""
    global _circuit_breaker_manager
    if _circuit_breaker_manager is None:
        _circuit_breaker_manager = CircuitBreakerManager(config)
    return _circuit_breaker_manager


async def reset_circuit_breakers() -> None:
    """Reset all circuit breakers (for testing/admin)."""
    global _circuit_breaker_manager
    async with _circuit_breaker_manager._lock if _circuit_breaker_manager else asyncio.Lock():
        if _circuit_breaker_manager:
            _circuit_breaker_manager._breakers.clear()