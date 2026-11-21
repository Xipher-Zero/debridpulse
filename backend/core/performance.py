"""Lightweight process-local performance instrumentation for DownloadPulse.

This intentionally avoids a new dependency. Counters are diagnostic only and
are not used to make correctness decisions. They reset with the process.
"""
from __future__ import annotations

import time
from collections import defaultdict
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Dict

_counts: dict[str, int] = defaultdict(int)
_seconds: dict[str, float] = defaultdict(float)
_max_seconds: dict[str, float] = defaultdict(float)


def increment(name: str, amount: int = 1) -> None:
    _counts[str(name)] += int(amount)


def observe(name: str, elapsed_seconds: float, amount: int = 1) -> None:
    key = str(name)
    elapsed = max(0.0, float(elapsed_seconds))
    _counts[key] += int(amount)
    _seconds[key] += elapsed
    if elapsed > _max_seconds[key]:
        _max_seconds[key] = elapsed


@contextmanager
def timer(name: str):
    started = time.monotonic()
    try:
        yield
    finally:
        observe(name, time.monotonic() - started)


@asynccontextmanager
async def async_timer(name: str):
    started = time.monotonic()
    try:
        yield
    finally:
        observe(name, time.monotonic() - started)


def snapshot() -> Dict[str, Any]:
    keys = sorted(set(_counts) | set(_seconds) | set(_max_seconds))
    items: Dict[str, Any] = {}
    for key in keys:
        count = int(_counts.get(key, 0))
        total = float(_seconds.get(key, 0.0))
        maximum = float(_max_seconds.get(key, 0.0))
        items[key] = {
            "count": count,
            "total_seconds": round(total, 6),
            "average_ms": round((total / count) * 1000.0, 3) if count else 0.0,
            "max_ms": round(maximum * 1000.0, 3),
        }
    return items


def reset() -> None:
    _counts.clear()
    _seconds.clear()
    _max_seconds.clear()
