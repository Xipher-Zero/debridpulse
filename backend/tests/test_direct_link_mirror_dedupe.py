from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import services.dispatch_coordinator as dispatch_module
from services.dispatch_coordinator import (
    DispatchCoordinator,
    plan_direct_link_mirror_suppression,
)


def _row(
    file_id,
    host,
    *,
    filename="GF200826-TMNTSFS-RN.rar",
    size=123456789,
    status="pending",
    download_id=None,
    torrent_id=42,
):
    return {
        "file_id": file_id,
        "torrent_id": torrent_id,
        "filename": filename,
        "size_bytes": size,
        "source_url": f"https://{host}/file/{file_id}",
        "status": status,
        "download_id": download_id,
    }


def test_cross_hoster_same_name_and_exact_size_collapses_to_one_logical_file():
    rows = [
        _row(1, "1fichier.com"),
        _row(2, "rapidgator.net"),
    ]

    plan = plan_direct_link_mirror_suppression(rows)

    assert len(plan) == 1
    duplicate, primary = plan[0]
    assert primary["file_id"] == 1
    assert duplicate["file_id"] == 2


def test_five_working_mirror_hosts_dispatch_only_first_logical_file():
    rows = [
        _row(1, "1fichier.com"),
        _row(2, "rapidgator.net"),
        _row(3, "gofile.io"),
        _row(4, "send.now"),
        _row(5, "ddownload.com"),
    ]

    plan = plan_direct_link_mirror_suppression(rows)

    assert [duplicate["file_id"] for duplicate, _primary in plan] == [2, 3, 4, 5]
    assert {primary["file_id"] for _duplicate, primary in plan} == {1}


def test_same_filename_with_different_exact_size_is_not_collapsed():
    rows = [
        _row(1, "1fichier.com", size=100),
        _row(2, "rapidgator.net", size=101),
    ]

    assert plan_direct_link_mirror_suppression(rows) == []


def test_same_size_with_different_resolved_filename_is_not_collapsed():
    rows = [
        _row(1, "1fichier.com", filename="part-a.rar"),
        _row(2, "rapidgator.net", filename="part-b.rar"),
    ]

    assert plan_direct_link_mirror_suppression(rows) == []


def test_unknown_size_is_never_treated_as_verified_mirror_identity():
    rows = [
        _row(1, "1fichier.com", size=0),
        _row(2, "rapidgator.net", size=0),
    ]

    assert plan_direct_link_mirror_suppression(rows) == []


def test_same_hoster_url_variants_are_not_collapsed_by_metadata_alone():
    rows = [
        _row(1, "rapidgator.net"),
        _row(2, "rapidgator.net"),
    ]

    assert plan_direct_link_mirror_suppression(rows) == []


def test_existing_running_mirror_is_preferred_over_pending_alternate():
    rows = [
        _row(1, "1fichier.com", status="pending"),
        _row(2, "rapidgator.net", status="downloading", download_id="gid-2"),
    ]

    plan = plan_direct_link_mirror_suppression(rows)

    assert len(plan) == 1
    duplicate, primary = plan[0]
    assert primary["file_id"] == 2
    assert duplicate["file_id"] == 1


def test_already_dispatched_duplicate_is_never_removed():
    rows = [
        _row(1, "1fichier.com", status="downloading", download_id="gid-1"),
        _row(2, "rapidgator.net", status="queued", download_id="gid-2"),
    ]

    assert plan_direct_link_mirror_suppression(rows) == []


class _Cursor:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount


class _FakeDb:
    def __init__(self, rows):
        self.rows = list(rows)
        self.deleted = []
        self.parent_sizes = []
        self.events = []
        self.committed = False

    async def fetchall(self, sql, params=()):
        assert "t.source=?" in sql
        assert params == ("direct_link",)
        return list(self.rows)

    async def fetchone(self, sql, params=()):
        assert "SUM(size_bytes)" in sql
        surviving = [row for row in self.rows if row["file_id"] not in self.deleted]
        return {"total": sum(int(row.get("size_bytes") or 0) for row in surviving)}

    async def execute(self, sql, params=()):
        if sql.lstrip().startswith("DELETE FROM download_files"):
            file_id = int(params[0])
            row = next((item for item in self.rows if item["file_id"] == file_id), None)
            if (
                row
                and row["status"] == "pending"
                and not row.get("download_id")
                and file_id not in self.deleted
            ):
                self.deleted.append(file_id)
                return _Cursor(1)
            return _Cursor(0)
        if "UPDATE torrents" in sql:
            self.parent_sizes.append((int(params[1]), int(params[0])))
            return _Cursor(1)
        if "INSERT INTO events" in sql:
            self.events.append((int(params[0]), str(params[1])))
            return _Cursor(1)
        raise AssertionError(sql)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_dispatch_collapses_mirror_before_delegating_to_aria2(monkeypatch):
    rows = [
        _row(1, "1fichier.com", size=777),
        _row(2, "rapidgator.net", size=777),
    ]
    db = _FakeDb(rows)

    @asynccontextmanager
    async def _fake_get_db():
        yield db

    monkeypatch.setattr(dispatch_module, "get_db", _fake_get_db)

    coordinator = SimpleNamespace(dispatch_queue=AsyncMock(return_value="dispatched"))
    control = SimpleNamespace(coordinator=coordinator)
    dispatch = DispatchCoordinator(engine=object(), control=control, ownership=object())

    result = await dispatch.dispatch_queue(snapshot=["snapshot"])

    assert result == "dispatched"
    assert db.deleted == [2]
    assert db.parent_sizes == [(42, 777)]
    assert len(db.events) == 1
    assert "Suppressed 1 cross-hoster mirror link" in db.events[0][1]
    assert db.committed is True
    coordinator.dispatch_queue.assert_awaited_once_with(["snapshot"])
