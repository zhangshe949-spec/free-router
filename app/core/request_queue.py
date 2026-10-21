# Copyright (c) 2026 zhangshe949-spec. All rights reserved.
# Proprietary software - see LICENSE. Unauthorized copying, redistribution
# or commercial use of this file, via any medium, is strictly prohibited.
"""Request queue with backpressure support.

Provides a bounded async queue for incoming requests when all
providers are unhealthy or at capacity.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, TypeVar

from .observability import get_logger, set_queue_size, record_queue_wait, queue_wait_span

logger = get_logger("request_queue")

T = TypeVar("T")


@dataclass
class QueueConfig:
    """Configuration for request queue."""
    max_size: int = 50              # Maximum queued requests
    max_wait_time: float = 30.0     # Max time to wait in queue (seconds)
    timeout_error_message: str = "Request queued too long, all providers unavailable"


class RequestQueue:
    """Bounded async queue with backpressure."""
    
    def __init__(self, config: QueueConfig = None):
        self.config = config or QueueConfig()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=self.config.max_size)
        self._processing = False
    
    async def enqueue(self, coro_factory: Callable[[], Awaitable[T]]) -> T:
        """Add a request to the queue and wait for result.
        
        Args:
            coro_factory: A callable that returns an awaitable (the actual request logic)
            
        Returns:
            The result of the awaitable
            
        Raises:
            asyncio.TimeoutError: If request waits too long in queue
            asyncio.QueueFull: If queue is full (backpressure)
        """
        wait_start = time.time()
        set_queue_size(self._queue.qsize())
        
        try:
            with queue_wait_span():
                # Put the coroutine factory in the queue with timeout
                await asyncio.wait_for(
                    self._queue.put(coro_factory),
                    timeout=self.config.max_wait_time
                )
        except asyncio.TimeoutError:
            record_queue_wait(time.time() - wait_start)
            set_queue_size(self._queue.qsize())
            raise asyncio.TimeoutError(self.config.timeout_error_message)
        
        wait_duration = time.time() - wait_start
        record_queue_wait(wait_duration)
        set_queue_size(self._queue.qsize())
        
        # Execute the actual request
        return await coro_factory()
    
    async def try_enqueue(self, coro_factory: Callable[[], Awaitable[T]]) -> Optional[T]:
        """Try to enqueue without blocking (for non-critical requests).
        
        Returns None if queue is full.
        """
        set_queue_size(self._queue.qsize())
        
        if self._queue.full():
            logger.warning("request_queue_full", queue_size=self._queue.qsize())
            return None
        
        await self._queue.put(coro_factory)
        return await coro_factory()
    
    @property
    def size(self) -> int:
        return self._queue.qsize()
    
    @property
    def is_full(self) -> bool:
        return self._queue.full()
    
    @property
    def is_empty(self) -> bool:
        return self._queue.empty()


class PriorityRequestQueue:
    """Priority queue for requests (e.g., prioritize paying customers).

    A single background worker drains the queue, always preferring
    high-priority items (priority 0) over low-priority items (priority 1),
    so a high-priority request enqueued after a low-priority one still
    executes first.
    """

    def __init__(self, config: QueueConfig = None):
        self.config = config or QueueConfig()
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._worker_task: Optional[asyncio.Task] = None
        self._seq = 0

    def _ensure_worker(self) -> None:
        """Start the background worker if it isn't already running."""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker())

    async def _worker(self) -> None:
        """Drain the queue, running high-priority items first."""
        while True:
            _, _, coro_factory, future = await self._queue.get()
            set_queue_size(self._queue.qsize())
            try:
                result = await coro_factory()
            except BaseException as exc:  # noqa: BLE001
                if not future.done():
                    future.set_exception(exc)
            else:
                if not future.done():
                    future.set_result(result)
            finally:
                self._queue.task_done()
                set_queue_size(self._queue.qsize())

    async def enqueue(
        self,
        coro_factory: Callable[[], Awaitable[T]],
        high_priority: bool = False
    ) -> T:
        """Enqueue a request and return its result once executed.

        ``high_priority=True`` items run before low-priority items,
        regardless of enqueue order.
        """
        self._ensure_worker()

        wait_start = time.time()
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        priority = 0 if high_priority else 1
        self._seq += 1
        item = (priority, self._seq, coro_factory, future)
        set_queue_size(self._queue.qsize())

        try:
            await asyncio.wait_for(
                self._queue.put(item),
                timeout=self.config.max_wait_time
            )
        except asyncio.TimeoutError:
            record_queue_wait(time.time() - wait_start)
            set_queue_size(self._queue.qsize())
            raise asyncio.TimeoutError(self.config.timeout_error_message)

        record_queue_wait(time.time() - wait_start)
        set_queue_size(self._queue.qsize())
        return await future

    @property
    def size(self) -> int:
        return self._queue.qsize()

    async def close(self) -> None:
        """Cancel the background worker (for graceful shutdown)."""
        if self._worker_task is not None and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass


# Global instance
_request_queue: Optional[RequestQueue] = None


def get_request_queue(config: QueueConfig = None) -> RequestQueue:
    """Get the global request queue."""
    global _request_queue
    if _request_queue is None:
        _request_queue = RequestQueue(config)
    return _request_queue