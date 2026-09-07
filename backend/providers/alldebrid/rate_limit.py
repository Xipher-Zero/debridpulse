"""Provider-local AllDebrid request rate limiting."""
from __future__ import annotations

import asyncio
import time
from collections import deque


class TokenBucketRateLimiter:
    def __init__(self, rate: int = 60, window: float = 60.0):
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()
        self.reconfigure(rate, window)

    def reconfigure(self, rate: int, window: float = 60.0) -> None:
        normalized = int(rate)
        self._rate = 1_000_000 if normalized <= 0 else normalized
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
