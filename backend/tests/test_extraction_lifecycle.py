from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zipfile

import pytest

import db.database as database
from postprocessors.archive.sources import _archive_source_paths
from postprocessors.archive.sources import _canonical_archive_entries
from postprocessors.archive.sources import _cleanup_successful_sources
from postprocessors.archive.extractor import Extractor


async def _prepare_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "extraction.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()
    return db_path


def _settings(**overrides):
    values = {
        "extract_enabled": True,
        "extract_delete_archive": True,
        "extract_max_concurrent": 1,
        "discord_notify_extract": False,
        "discord_webhook_url": "",
        "discord_webhook_added": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _insert_completed(db_path: Path, local_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO torrents
               (hash, name, status, source, download_client, progress, completed_at)
               VALUES (?, ?, 'completed', 'direct_link', 'aria2', 100, CURRENT_TIMESTAMP)""",
            ("extract:" + local_path.name, local_path.name),
        )
        torrent_id = int(cur.lastrowid)
        conn.execute(
            """INSERT INTO download_files
               (torrent_id, filename, local_path, status, download_client, blocked)
               VALUES (?, ?, ?, 'completed', 'aria2', 0)""",
            (torrent_id, local_path.name, str(local_path)),
        )
        conn.commit()
        return torrent_id
    finally:
        conn.close()


def _parent(db_path: Path, torrent_id: int):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute(
            "SELECT extraction_status, extraction_error, status FROM torrents WHERE id=?",
            (torrent_id,),
        ).fetchone())
    finally:
        conn.close()


def _events(db_path: Path, torrent_id: int) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT message FROM event_journal WHERE transfer_id=? ORDER BY id",
                (torrent_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_extract_archives_reports_missing_archive(tmp_path):
    archive = tmp_path / "missing.rar"
    results = await Extractor(max_concurrent=1).extract_archives([archive])
    assert len(results) == 1
    path, ok, message = results[0]
    assert path == archive
    assert ok is False
    assert "not accessible to DebridPulse" in message


def test_part_rar_cleanup_uses_only_db_known_archive_set_members(tmp_path):
    part1 = tmp_path / "payload.part01.rar"
    part2 = tmp_path / "payload.part02.rar"
    part3 = tmp_path / "payload.part03.rar"
    unrelated = tmp_path / "other.part02.rar"
    for path in (part1, part2, part3, unrelated):
        path.write_bytes(b"archive-volume")

    known = [part1, part2, part3, unrelated]
    assert _canonical_archive_entries(known) == [part1]
    assert _archive_source_paths(part1, known) == [part1, part2, part3]

    sources = {str(part1): _archive_source_paths(part1, known)}
    existed_before = {str(path) for path in known}
    removed, total, failures = _cleanup_successful_sources(
        [part1],
        sources,
        existed_before,
    )

    assert (removed, total, failures) == (3, 3, [])
    assert not part1.exists()
    assert not part2.exists()
    assert not part3.exists()
    assert unrelated.exists()


def test_traditional_rar_set_uses_rar_root_and_owns_numbered_volumes(tmp_path):
    root = tmp_path / "payload.rar"
    r00 = tmp_path / "payload.r00"
    r01 = tmp_path / "payload.r01"
    for path in (root, r00, r01):
        path.write_bytes(b"archive-volume")

    known = [root, r00, r01]

    assert _canonical_archive_entries(known) == [root]
    assert _archive_source_paths(root, known) == [root, r00, r01]


def test_cleanup_failure_is_reported_separately_from_source_selection(tmp_path):
    part1 = tmp_path / "payload.part01.rar"
    part2 = tmp_path / "payload.part02.rar"
    part1.write_bytes(b"archive-volume")
    part2.mkdir()

    sources = {str(part1): [part1, part2]}
    existed_before = {str(part1), str(part2)}

    removed, total, failures = _cleanup_successful_sources(
        [part1],
        sources,
        existed_before,
    )

    assert removed == 1
    assert total == 2
    assert len(failures) == 1
    assert failures[0][0] == part2
    assert not part1.exists()
    assert part2.exists()


def test_extraction_state_is_persisted_and_operator_visible():
    root = Path(__file__).resolve().parents[2]
    database_source = (root / "backend/db/database.py").read_text()
    routes_source = (root / "backend/api/routes.py").read_text()
    app_source = (root / "frontend/static/app.js").read_text()
    icon_source = (root / "frontend/static/operator-title.js").read_text()
    assert "extraction_status" in database_source
    assert '("extraction_error", "TEXT")' in database_source
    assert "active_operations" in routes_source
    assert "extracting_count" in routes_source
    assert "extracting: {icon: 'packageOpen', label: 'Extracting'" in icon_source
    # The extracting badge is the backend's effective presentation, never
    # re-derived in the browser from the extraction fields.
    assert "completed_with_errors" not in app_source and "downloading_with_errors" not in app_source
    assert "return 'extracting'" not in app_source
    assert "t && t.presentation_status" in app_source


def test_external_extraction_stages_inside_destination(tmp_path, monkeypatch):
    from services.extraction_safety import staged_external_extract

    archive = tmp_path / "payload.rar"
    archive.write_bytes(b"archive")
    dest = tmp_path / "download"
    dest.mkdir()
    observed = {}

    def runner(stage):
        observed["stage"] = Path(stage)
        (Path(stage) / "payload.txt").write_text("ok")

    monkeypatch.setattr(
        "services.extraction_safety.validate_extracted_tree",
        lambda stage, archive: None,
    )
    staged_external_extract(archive, dest, runner)

    assert observed["stage"].parent == dest.resolve()
    assert (dest / "payload.txt").read_text() == "ok"


def _merge_case(tmp_path, existing: dict[str, bytes], staged: dict[str, bytes]):
    """A destination holding ``existing`` and a staging tree under it (same
    filesystem, as ``staged_external_extract`` creates it) holding ``staged``."""
    dest = tmp_path / "download"
    stage = dest / ".debridpulse-extract-test"
    for root, files in ((dest, existing), (stage, staged)):
        for name, data in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    return stage, dest


def test_merge_accepts_byte_identical_existing_output_without_rewriting_it(tmp_path):
    """Re-extracting over this tree's own earlier outputs (a Retry of a
    completed transfer, a later file selection): an existing regular file
    holding exactly the staged bytes is satisfied -- neither relinked nor
    rewritten -- and IS one of the outputs, so the recorded file set (and an
    *arr import) sees the whole tree; what was missing is committed. A
    hard-linked target (an *arr import) is compared by its bytes alike."""
    from services.extraction_safety import _merge_staged_tree

    stage, dest = _merge_case(
        tmp_path,
        {"movie.mkv": b"same bytes", "sub/extra.nfo": b"nfo"},
        {"movie.mkv": b"same bytes", "sub/extra.nfo": b"nfo", "sub/new.srt": b"srt"},
    )
    imported = tmp_path / "library.mkv"
    import os
    os.link(dest / "movie.mkv", imported)
    before = {name: (dest / name).stat().st_ino for name in ("movie.mkv", "sub/extra.nfo")}

    outputs = _merge_staged_tree(stage, dest)

    root = dest.resolve()
    assert sorted(outputs) == sorted(root / name for name in ("movie.mkv", "sub/extra.nfo", "sub/new.srt"))
    assert {name: (dest / name).stat().st_ino for name in before} == before
    assert (dest / "movie.mkv").stat().st_nlink == 2
    assert (dest / "sub/new.srt").read_bytes() == b"srt"


@pytest.mark.parametrize("existing", ["different", "truncated", "longer", "symlink"])
def test_merge_never_accepts_existing_material_that_is_not_the_staged_file(tmp_path, existing):
    """Only identical bytes in a regular file are satisfied. Same size but
    different bytes, a truncated or longer file, or a symlink (even to the
    identical bytes) is still an overwrite refusal, and nothing of the tree
    is committed."""
    from services.extraction_safety import _merge_staged_tree

    data = {
        "different": b"SAME BYTES",
        "truncated": b"same by",
        "longer": b"same bytes and more",
        "symlink": None,
    }[existing]
    stage, dest = _merge_case(tmp_path, {}, {"movie.mkv": b"same bytes", "zz-new.srt": b"srt"})
    target = dest / "movie.mkv"
    if data is None:
        twin = dest / "twin.mkv"
        twin.write_bytes(b"same bytes")
        target.symlink_to(twin)
    else:
        target.write_bytes(data)

    with pytest.raises(ValueError, match="overwrite"):
        _merge_staged_tree(stage, dest)

    assert not (dest / "zz-new.srt").exists()
    if data is None:
        assert target.is_symlink() and twin.read_bytes() == b"same bytes"
    else:
        assert target.read_bytes() == data


def test_merge_rollback_removes_only_its_own_commits_never_a_satisfied_file(tmp_path, monkeypatch):
    """A commit that fails part-way unlinks exactly what it linked; the
    satisfied file was never this operation's write and stays untouched."""
    import os
    import services.extraction_safety as safety

    stage, dest = _merge_case(
        tmp_path,
        {"a-kept.mkv": b"same"},
        {"a-kept.mkv": b"same", "b-new.srt": b"srt", "c-fails.nfo": b"nfo"},
    )
    kept_inode = (dest / "a-kept.mkv").stat().st_ino
    real_link = os.link

    def link(source, target, **kwargs):
        if Path(target).name == "c-fails.nfo":
            raise OSError("disk full")
        return real_link(source, target, **kwargs)

    monkeypatch.setattr(safety.os, "link", link)
    with pytest.raises(OSError, match="disk full"):
        safety._merge_staged_tree(stage, dest)

    assert not (dest / "b-new.srt").exists()
    assert (dest / "a-kept.mkv").read_bytes() == b"same"
    assert (dest / "a-kept.mkv").stat().st_ino == kept_inode


def test_the_extraction_failure_toast_is_shown_only_for_its_announcement():
    """FUNC-A13: ``notifyExtractionFailure``, exactly as ``app.js`` ships it,
    run under node: the claimed ``completed`` event of a failed extraction
    toasts once; every other ``torrent_updated`` of the same failed transfer
    (a control action, a refresh), and a completion that did not fail, toast
    nothing."""
    import json
    import re
    import subprocess
    root = Path(__file__).resolve().parents[2]
    source = (root / "frontend/static/app.js").read_text()
    function = re.search(r"function notifyExtractionFailure\(data\) \{.*?\n\}\n", source, re.S).group(0)
    harness = """
const toasts = [];
global.toast = (message, kind) => toasts.push([message, kind]);
global.sanitizeErrorMsg = (value) => value;
FUNCTION
const failed = {extraction_status: 'error', extraction_error: 'CRC failed'};
notifyExtractionFailure({...failed, lifecycle_event: 'completed'});
notifyExtractionFailure(failed);
notifyExtractionFailure({...failed, status_changed: true});
notifyExtractionFailure({extraction_status: 'completed', lifecycle_event: 'completed'});
console.log(JSON.stringify(toasts));
""".replace("FUNCTION", function)
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [["Extraction failed: CRC failed", "error"]]


def test_frontend_surfaces_extraction_failure_toast():
    root = Path(__file__).resolve().parents[2]
    app_source = (root / "frontend/static/app.js").read_text()
    assert "notifyExtractionFailure" in app_source
    # The toast is a notification only: no client-side status derivation paints
    # the row (the badge is the backend's effective presentation).
    assert "patchExtractionTransferEvent" not in app_source
    assert "Extraction failed:" in app_source
    assert "extraction_error" in app_source
