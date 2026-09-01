"""Process-local provider request rate limiting.

The AllDebrid client owns this concern; provider networking must not import the
legacy materialization engine merely to obtain a limiter.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque

from core.config import get_settings


class TokenBucketRateLimiter:
    def __init__(self, rate: int = 60, window: float = 60.0):
        self._rate = max(1, int(rate))
        self._window = max(0.001, float(window))
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    def reconfigure(self, rate: int, window: float = 60.0) -> None:
        self._rate = max(1, int(rate))
        self._window = max(0.001, float(window))

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            while self._timestamps and self._timestamps[0] < now - self._window:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._rate:
                sleep_for = self._window - (now - self._timestamps[0])
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                    now = time.monotonic()
                    while self._timestamps and self._timestamps[0] < now - self._window:
                        self._timestamps.popleft()
            self._timestamps.append(time.monotonic())


_alldebrid_rate_limiter = TokenBucketRateLimiter(rate=60, window=60.0)


async def get_alldebrid_rate_limiter() -> TokenBucketRateLimiter:
    try:
        limit = int(get_settings().alldebrid_rate_limit_per_minute)
    except Exception:
        limit = 60
    if limit <= 0:
        limit = 1_000_000
    _alldebrid_rate_limiter.reconfigure(rate=limit, window=60.0)
    return _alldebrid_rate_limiter


async def acquire_alldebrid_request_slot() -> None:
    limiter = await get_alldebrid_rate_limiter()
    await limiter.acquire()
