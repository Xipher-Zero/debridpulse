"""Shared pytest bootstrap for the DebridPulse backend suite.

Some inherited unittest modules conditionally install lightweight dependency
stubs during module import. Import the real SQLite driver first so those legacy
guards cannot replace the process-wide ``aiosqlite`` module and leak a fake
``connect`` implementation into later persistence tests.
"""

import aiosqlite  # noqa: F401
import pytest


# WS4 P1 intentionally supersedes the old near-size mirror contract: known
# candidate sizes must now match exactly before any final consolidation. Keep
# the old expectations visible (and strict-xfailed) until the next ordinary
# test-file consolidation, while replacement WS4 tests enforce the new rule.
_SUPERSEDED_EXACT_SIZE_TESTS = {
    "test_near_size_mirrors_require_matching_sample_evidence",
    "test_mirror_size_boundaries_are_conservative",
}


def pytest_collection_modifyitems(items):
    for item in items:
        if item.name.split("[", 1)[0] in _SUPERSEDED_EXACT_SIZE_TESTS:
            item.add_marker(pytest.mark.xfail(
                reason="WS4 P1 exact-known-size identity contract supersedes near-size equivalence",
                strict=True,
            ))
