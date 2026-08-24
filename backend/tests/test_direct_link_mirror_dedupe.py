import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import services.dispatch_coordinator as dispatch_module
from services.dispatch_coordinator import (
    MirrorAwareTransferControlCoordinator,
    collapse_direct_link_mirrors,
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
async def test_collapse_removes_duplicate_before_physical_dispatch(monkeypatch):
    rows = [
        _row(1, "1fichier.com", size=777),
        _row(2, "rapidgator.net", size=777),
    ]
    db = _FakeDb(rows)

    @asynccontextmanager
    async def _fake_get_db():
        yield db

    monkeypatch.setattr(dispatch_module, "get_db", _fake_get_db)

    removed = await collapse_direct_link_mirrors()

    assert removed == 1
    assert db.deleted == [2]
    assert db.parent_sizes == [(42, 777)]
    assert len(db.events) == 1
    assert "Suppressed 1 cross-hoster mirror link" in db.events[0][1]
    assert db.committed is True


class _ManagerStub:
    async def _engine_dispatch_pending_aria2_queue(self, *_args, **_kwargs):
        return 0

    def _engine_schedule_ready_parent_download(self, *_args, **_kwargs):
        return False

    async def _engine_start_download(self, *_args, **_kwargs):
        return None

    async def _engine_download(self, *_args, **_kwargs):
        return None

    async def _engine_reset_torrent_for_redownload(self, *_args, **_kwargs):
        return None

    async def _engine_update_aria2_parent_progress(self, *_args, **_kwargs):
        return None

    async def _engine_sync_download_clients(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_concurrent_queue_kicks_cannot_dispatch_between_collapse_passes(monkeypatch):
    coordinator = MirrorAwareTransferControlCoordinator(_ManagerStub())
    timeline = []
    first_dispatch_entered = asyncio.Event()
    release_first_dispatch = asyncio.Event()
    base_calls = 0

    async def fake_collapse():
        timeline.append("collapse")
        return 0

    async def fake_base_dispatch(_self, _snapshot=None):
        nonlocal base_calls
        base_calls += 1
        timeline.append("dispatch")
        if base_calls == 1:
            first_dispatch_entered.set()
            await release_first_dispatch.wait()
        return base_calls

    monkeypatch.setattr(dispatch_module, "collapse_direct_link_mirrors", fake_collapse)
    monkeypatch.setattr(
        dispatch_module.TransferControlCoordinator,
        "dispatch_queue",
        fake_base_dispatch,
    )

    first = asyncio.create_task(coordinator.dispatch_queue(["first"]))
    await first_dispatch_entered.wait()
    second = asyncio.create_task(coordinator.dispatch_queue(["second"]))
    await asyncio.sleep(0)

    assert timeline == ["collapse", "dispatch"]

    release_first_dispatch.set()
    await asyncio.gather(first, second)

    assert timeline == ["collapse", "dispatch", "collapse", "dispatch"]


def test_transfer_control_service_uses_mirror_aware_authoritative_queue():
    root = Path(__file__).resolve().parents[2]
    source = (root / "backend/services/transfer_control_service.py").read_text()

    assert "MirrorAwareTransferControlCoordinator" in source
    assert "self.coordinator = MirrorAwareTransferControlCoordinator(engine)" in source
