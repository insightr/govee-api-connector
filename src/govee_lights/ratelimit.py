"""An async token bucket: `rate` tokens per second, up to `capacity` saved up."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class TokenBucket:
    def __init__(
        self,
        rate: float,
        capacity: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.rate = rate
        self.capacity = capacity
        self._clock = clock
        self._sleep = sleep
        self._tokens = capacity
        self._updated = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    async def acquire(self) -> None:
        """Take one token, waiting for it if the bucket is empty."""
        async with self._lock:
            self._refill()
            if self._tokens < 1:
                await self._sleep((1 - self._tokens) / self.rate)
                self._tokens = 1
                self._updated = self._clock()
            self._tokens -= 1
