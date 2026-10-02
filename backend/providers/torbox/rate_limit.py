"""Provider-local TorBox request rate limiting.

TorBox documents 300 requests per minute per API token. The limiter only paces
native calls; it never retries one.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque

# The published service ceiling. A local limit above it would only produce
# refusals that count against the account anyway.
SERVICE_LIMIT_PER_MINUTE = 300


class SlidingWindowRateLimiter:
    def __init__(self, rate: int = 240, window: float = 60.0):
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._rate = max(1, min(SERVICE_LIMIT_PER_MINUTE, int(rate)))
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
