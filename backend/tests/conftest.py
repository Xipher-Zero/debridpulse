"""Shared pytest bootstrap for the DebridPulse backend suite.

Some inherited unittest modules conditionally install lightweight dependency
stubs during module import. Import the real SQLite driver first so those legacy
guards cannot replace the process-wide ``aiosqlite`` module and leak a fake
``connect`` implementation into later persistence tests.

It also owns the suite's two layers (docs/QUALIFICATION_DETERMINISM.md):
``real_runtime`` marks a test that drives a real external executable -- rsync
(>= 3.4.0, the product minimum), aria2c or the openssl CLI -- and runs in the
runtime layer, ``-m real_runtime``; everything else is the deterministic
contract layer, ``-m "not real_runtime"``. ``DP_TEST_SHARD=k/n`` keeps only
the modules of shard ``k`` of ``n``: a fixed partition by module path, so the
shards of one layer together run each of its tests exactly once.
"""
import os
import zlib

import aiosqlite  # noqa: F401
import pytest

RUNTIME_MARKER = "real_runtime"


def pytest_configure(config):
    config.addinivalue_line(
        "markers", f"{RUNTIME_MARKER}: drives a real external executable; runs in the runtime layer")


def shard_of(module_path: str, shards: int) -> int:
    """The 1-based shard a test module belongs to: a pure function of its path."""
    return zlib.crc32(module_path.replace(os.sep, "/").encode("utf-8")) % shards + 1


def parse_shard(value: str) -> tuple[int, int] | None:
    if not value:
        return None
    index, _, total = value.partition("/")
    shard, shards = int(index), int(total)
    if not 1 <= shard <= shards:
        raise pytest.UsageError(f"DP_TEST_SHARD must be k/n with 1 <= k <= n, not {value!r}")
    return shard, shards


def pytest_collection_modifyitems(config, items):
    selected = parse_shard(os.environ.get("DP_TEST_SHARD", ""))
    if selected is None:
        return
    shard, shards = selected
    kept, dropped = [], []
    for item in items:
        module = item.nodeid.split("::", 1)[0]
        (kept if shard_of(module, shards) == shard else dropped).append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept
