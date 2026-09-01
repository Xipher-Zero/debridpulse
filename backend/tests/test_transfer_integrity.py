"""Regression coverage for filesystem/aria2 transfer integrity."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from executors.aria2.client import Aria2DownloadStatus


class _Cursor:
    def __init__(self, rows=None):
        self._rows = list(rows or [])

    async def fetchall(self):
        return list(self._rows)

    async def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeDb:
    def __init__(self):
        self.statements = []
        self.manifest_rows = []

    async def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if "SELECT download_id FROM download_files" in sql:
            return _Cursor([])
        return _Cursor([])

    async def executemany(self, sql, rows):
        materialized = list(rows)
        self.statements.append((sql, materialized))
        if "INSERT INTO download_files" in sql:
            self.manifest_rows.extend(materialized)
        return _Cursor([])

    async def fetchone(self, sql, params=()):
        self.statements.append((sql, params))
        if "SELECT source FROM torrents" in sql:
            return {"source": "manual"}
        return None

    async def commit(self):
        return None


def _cfg(tmp_path):
    return SimpleNamespace(
        download_folder=str(tmp_path),
        min_free_disk_gb=0,
        filters_enabled=False,
        discord_notify_finished=False,
        aria2_operation_timeout_seconds=15,
    )


def _parent_status_updates(db):
    return [
        params
        for sql, params in db.statements
        if isinstance(params, tuple)
        and "UPDATE torrents SET status=?, local_path=?" in sql
    ]


