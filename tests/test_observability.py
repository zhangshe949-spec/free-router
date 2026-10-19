"""Integration tests for observability components (circuit breaker, health check, request queue)."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.circuit_breaker import CircuitBreakerConfig, CircuitBreakerState, CircuitState, CircuitBreakerManager
from app.core.health_check import HealthCheckConfig, ProviderHealthState, HealthCheckManager
from app.core.request_queue import QueueConfig, RequestQueue, PriorityRequestQueue


class TestCircuitBreaker:
    """Test circuit breaker functionality."""
    
    def test_closed_state_allows_execution(self):
        """CLOSED state should allow execution."""
        config = CircuitBreakerConfig(min_requests=5)
        state = CircuitBreakerState(config=config)
        assert state.can_execute() is True
    
    def test_failure_threshold_opens_circuit(self):
        """Exceeding failure threshold should open circuit."""
        config = CircuitBreakerConfig(failure_threshold=0.5, min_requests=4, window_size=100)
        state = CircuitBreakerState(config=config)
        
        # Record 2 successes, 2 failures = 50% failure rate (exactly at threshold, should open)
        state.record_result(True)
        state.record_result(True)
        state.record_result(False)
        state.record_result(False)
        # At exactly 50% failure rate (>= threshold), circuit opens
        assert state.state == CircuitState.OPEN
    
    def test_open_state_blocks_execution(self):
        """OPEN state should block execution."""
        config = CircuitBreakerConfig(failure_threshold=0.3, min_requests=2, window_time=60)
        state = CircuitBreakerState(config=config)
        
        # Force open
        state.record_result(False)
        state.record_result(False)
        assert state.state == CircuitState.OPEN
        assert state.can_execute() is False
    
    def test_half_open_after_window_time(self):
        """Circuit should go to HALF_OPEN after window_time."""
        config = CircuitBreakerConfig(failure_threshold=0.3, min_requests=2, window_time=0.1)
        state = CircuitBreakerState(config=config)
        
        state.record_result(False)
        state.record_result(False)
        assert state.state == CircuitState.OPEN
        
        # Wait for window_time
        import time
        time.sleep(0.15)
        
        # Next check should transition to HALF_OPEN
        assert state.can_execute() is True
        assert state.state == CircuitState.HALF_OPEN
    
    def test_half_open_successes_close_circuit(self):
        """Enough successes in HALF_OPEN should close circuit."""
        config = CircuitBreakerConfig(
            failure_threshold=0.3, 
            min_requests=2, 
            window_time=0.1,
            success_threshold=2,
            half_open_max_requests=5
        )
        state = CircuitBreakerState(config=config)
        
        # Open circuit
        state.record_result(False)
        state.record_result(False)
        import time
        time.sleep(0.15)
        
        # Enter HALF_OPEN and succeed twice
        assert state.can_execute() is True  # Triggers HALF_OPEN
        state.record_result(True)
        state.record_result(True)
        assert state.state == CircuitState.CLOSED
    
    def test_half_open_failure_reopens_circuit(self):
        """Failure in HALF_OPEN should reopen circuit."""
        config = CircuitBreakerConfig(
            failure_threshold=0.3, 
            min_requests=2, 
            window_time=0.1,
            success_threshold=3,
            half_open_max_requests=5
        )
        state = CircuitBreakerState(config=config)
        
        state.record_result(False)
        state.record_result(False)
        import time
        time.sleep(0.15)
        
        assert state.can_execute() is True  # HALF_OPEN
        state.record_result(False)  # Failure in half-open
        assert state.state == CircuitState.OPEN
    
    @pytest.mark.asyncio
    async def test_manager_record_success_failure(self):
        """Test manager recording results."""
        manager = CircuitBreakerManager()
        
        await manager.record_success("provider1", "model1")
        await manager.record_failure("provider1", "model1")
        
        state = await manager.get_state("provider1", "model1")
        assert state["total_requests"] == 2
        assert state["failures"] == 1


class TestHealthCheck:
    """Test health check functionality."""
    
    def test_health_state_initialization(self):
        """Test health state initialization."""
        state = ProviderHealthState(provider="test")
        assert state.healthy is True
        assert state.consecutive_failures == 0
        assert state.consecutive_successes == 0
    
    @pytest.mark.asyncio
    async def test_health_check_marks_unhealthy(self):
        """Test health check marks provider unhealthy after threshold."""
        config = HealthCheckConfig(
            consecutive_failures_threshold=2,
            timeout_seconds=1.0,
        )
        
        # Mock provider that always fails
        mock_provider = AsyncMock()
        mock_provider.chat.side_effect = Exception("Connection failed")
        
        mock_registry = MagicMock()
        mock_registry.list_active.return_value = [
            MagicMock(id="model1", provider="test")
        ]
        
        manager = HealthCheckManager(config, {"test": mock_provider}, mock_registry)
        manager._init_states()
        
        # Run checks
        await manager._check_provider("test", mock_provider)
        await manager._check_provider("test", mock_provider)
        
        state = manager._states["test"]
        assert state.healthy is False
        assert state.consecutive_failures == 2
    
    @pytest.mark.asyncio
    async def test_health_check_recovers(self):
        """Test health check recovers after successes."""
        config = HealthCheckConfig(
            consecutive_failures_threshold=2,
            consecutive_successes_threshold=2,
            timeout_seconds=1.0,
        )
        
        call_count = [0]
        
        async def mock_chat(body, model):
            call_count[0] += 1
            if call_count[0] <= 2:
                raise Exception("Failed")
            return {"choices": [{"message": {"content": "ok"}}]}
        
        mock_provider = AsyncMock()
        mock_provider.chat = mock_chat
        
        mock_registry = MagicMock()
        mock_registry.list_active.return_value = [
            MagicMock(id="model1", provider="test")
        ]
        
        manager = HealthCheckManager(config, {"test": mock_provider}, mock_registry)
        manager._init_states()
        
        # First two checks fail
        await manager._check_provider("test", mock_provider)
        await manager._check_provider("test", mock_provider)
        assert manager._states["test"].healthy is False
        
        # Next two succeed - should recover
        await manager._check_provider("test", mock_provider)
        await manager._check_provider("test", mock_provider)
        
        state = manager._states["test"]
        assert state.healthy is True
        assert state.consecutive_successes >= 2


class TestRequestQueue:
    """Test request queue functionality."""
    
    @pytest.mark.asyncio
    async def test_enqueue_and_execute(self):
        """Test basic enqueue and execution."""
        config = QueueConfig(max_size=10, max_wait_time=1.0)
        queue = RequestQueue(config)
        
        executed = []
        
        async def coro_factory():
            executed.append(True)
            return "result"
        
        result = await queue.enqueue(coro_factory)
        assert result == "result"
        assert len(executed) == 1
    
    @pytest.mark.asyncio
    async def test_queue_full_blocks(self):
        """Test queue blocks when full."""
        config = QueueConfig(max_size=1, max_wait_time=0.1)
        queue = RequestQueue(config)
        
        async def slow_coro():
            await asyncio.sleep(0.2)
            return "done"
        
        # Fill the queue
        task = asyncio.create_task(queue.enqueue(slow_coro))
        await asyncio.sleep(0.05)  # Let it start
        
        # Second request should timeout
        async def fast_coro():
            return "fast"
        
        with pytest.raises(asyncio.TimeoutError):
            await queue.enqueue(fast_coro)
        
        # Wait for first to complete
        await task
    
    @pytest.mark.asyncio
    async def test_try_enqueue_returns_none_when_full(self):
        """Test try_enqueue returns None when queue full."""
        config = QueueConfig(max_size=1, max_wait_time=1.0)
        queue = RequestQueue(config)
        
        # Fill queue
        queue._queue.put_nowait(lambda: "x")
        
        result = await queue.try_enqueue(lambda: "y")
        assert result is None
    
    @pytest.mark.asyncio
    async def test_priority_queue_high_priority_first(self):
        """Test priority queue processes high priority first."""
        from app.core.request_queue import PriorityRequestQueue
        
        config = QueueConfig(max_size=10, max_wait_time=1.0)
        queue = PriorityRequestQueue(config)
        
        order = []
        
        async def low():
            order.append("low")
            return "low"
        
        async def high():
            order.append("high")
            return "high"
        
        # Enqueue low then high
        task1 = asyncio.create_task(queue.enqueue(low, high_priority=False))
        task2 = asyncio.create_task(queue.enqueue(high, high_priority=True))
        
        await asyncio.gather(task1, task2)
        
        # High priority should execute first
        assert order == ["high", "low"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])