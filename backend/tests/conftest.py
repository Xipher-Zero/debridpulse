"""Shared pytest bootstrap for the DebridPulse backend suite.

Some inherited unittest modules conditionally install lightweight dependency
stubs during module import. Import the real SQLite driver first so those legacy
guards cannot replace the process-wide ``aiosqlite`` module and leak a fake
``connect`` implementation into later persistence tests.
"""

import aiosqlite  # noqa: F401
