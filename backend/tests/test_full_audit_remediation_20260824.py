from __future__ import annotations

import asyncio
import io
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import db.database as database
from core.config import AppSettings, apply_settings
from postprocessors.archive.extractor import Extractor
from providers.alldebrid.client import AllDebridAPIError


@pytest.mark.asyncio
async def test_extraction_refuses_to_overwrite_existing_regular_file(tmp_path):
    dest = tmp_path / "download"
    dest.mkdir()
    existing = dest / "owned.txt"
    existing.write_text("keep-me")
    archive = dest / "incoming.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("owned.txt", "replace-me")

    extractor = Extractor()
    ok, message = await extractor.extract_archive(archive, dest, delete_after=False)

    assert ok is False
    assert "overwrite existing file" in message
    assert existing.read_text() == "keep-me"
    assert archive.exists()


@pytest.mark.asyncio
async def test_extraction_never_scans_unrelated_preexisting_nested_archive(tmp_path):
    dest = tmp_path / "download"
    dest.mkdir()
    unrelated_dir = dest / "other-transfer"
    unrelated_dir.mkdir()
    unrelated = unrelated_dir / "keep.zip"
    with zipfile.ZipFile(unrelated, "w") as zf:
        zf.writestr("unrelated.txt", "preserve")

    archive = dest / "incoming.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("created/payload.txt", "new")

    extractor = Extractor()
    ok, _message = await extractor.extract_archive(archive, dest, delete_after=True)

    assert ok is True
    assert unrelated.exists()
    assert not (unrelated_dir / "unrelated.txt").exists()
    assert (dest / "created" / "payload.txt").read_text() == "new"


@pytest.mark.asyncio
async def test_nested_archive_created_by_current_extraction_still_extracts(tmp_path):
    dest = tmp_path / "download"
    dest.mkdir()
    nested_bytes = io.BytesIO()
    with zipfile.ZipFile(nested_bytes, "w") as nested:
        nested.writestr("inside.txt", "nested-ok")
    archive = dest / "incoming.zip"
    with zipfile.ZipFile(archive, "w") as outer:
        outer.writestr("created/nested.zip", nested_bytes.getvalue())

    extractor = Extractor()
    ok, _message = await extractor.extract_archive(archive, dest, delete_after=True)

    assert ok is True
    assert (dest / "created" / "inside.txt").read_text() == "nested-ok"
    assert not (dest / "created" / "nested.zip").exists()


class _Cursor:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount


class _MirrorDb:
    def __init__(self, rows):
        self.rows = rows

    async def fetchall(self, _sql, _params=()):
        return self.rows

    async def fetchone(self, sql, _params=()):
        if "SUM(size_bytes)" in sql:
            return {"total": sum(int(r.get("size_bytes") or 0) for r in self.rows if r.get("blocked") == 0)}
        return None

    async def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        if "mirror_state=CASE" in normalized:
            group_id, file_id = params
            row = next(r for r in self.rows if r["file_id"] == int(file_id))
            row["mirror_group_id"] = int(group_id)
            row["mirror_state"] = "active"
            return _Cursor(1)
        if "status='duplicate'" in normalized:
            reason, group_id, file_id = params
            row = next(r for r in self.rows if r["file_id"] == int(file_id))
            row.update(status="duplicate", blocked=None, block_reason=reason,
                       mirror_group_id=int(group_id), mirror_state="standby",
                       download_url=None, local_path=None)
            return _Cursor(1)
        return _Cursor(1)

    async def commit(self):
        return None


def test_current_schema_contract_includes_extraction_and_mirror_columns():
    required_torrent = {name for name, _ in database._SCHEMA_COLUMNS_TORRENTS}
    required_files = {name for name, _ in database._SCHEMA_COLUMNS_FILES}
    assert {"extraction_status", "extraction_error"} <= required_torrent
    assert {"mirror_group_id", "mirror_state"} <= required_files


@pytest.mark.asyncio
async def test_concurrent_extractions_cannot_clobber_same_new_target(tmp_path):
    dest = tmp_path / "download"
    dest.mkdir()
    first = dest / "first.zip"
    second = dest / "second.zip"
    with zipfile.ZipFile(first, "w") as zf:
        zf.writestr("shared/payload.txt", "first")
    with zipfile.ZipFile(second, "w") as zf:
        zf.writestr("shared/payload.txt", "second")

    extractor = Extractor(max_concurrent=2)
    results = await asyncio.gather(
        extractor.extract_archive(first, dest, delete_after=False),
        extractor.extract_archive(second, dest, delete_after=False),
    )

    assert sorted(ok for ok, _message in results) == [False, True]
    assert (dest / "shared" / "payload.txt").read_text() in {"first", "second"}
    assert first.exists() and second.exists()


def test_sample_fingerprints_request_identity_bytes_with_manual_redirect_control():
    source = (Path(__file__).resolve().parents[1] / "services/network_safety.py").read_text()
    # Workspace 4 centralizes identity encoding and redirect control in reusable
    # helpers: both first/last samples inherit one identity header definition,
    # and every hop is explicit with aiohttp redirects disabled.
    assert source.count('"Accept-Encoding": "identity"') == 1
    assert source.count("allow_redirects=False") == 1
    assert "async def _range_request" in source
    assert "urljoin(validated, location)" in source
    assert "validate_resolved_public_destination(current)" in source
