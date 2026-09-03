import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_public_serializers_strip_capability_urls():
    from api.serializers import public_download_file, public_payload, public_torrent

    torrent = public_torrent({"id": 1, "magnet": "magnet:?xt=secret", "download_url": "https://token", "name": "x"})
    assert torrent == {"id": 1, "name": "x"}
    file_row = public_download_file({"id": 2, "source_url": "https://source", "download_url": "https://unlocked", "filename": "x.mkv"})
    assert file_row == {"id": 2, "filename": "x.mkv"}
    nested = public_payload({"items": [{"magnet": "secret", "source_url": "secret", "id": 3}]})
    assert nested == {"items": [{"id": 3}]}


def test_backup_rotation_requires_ownership_manifest(tmp_path):
    from services import backup

    unrelated = tmp_path / "20000101_000000"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("keep")
    assert backup._rotate_backups(tmp_path, 1) == 0
    assert (unrelated / "keep.txt").exists()


def test_database_wipe_requires_verified_quiescence():
    import services.db_maintenance as maintenance

    with pytest.raises(RuntimeError, match="verified quiesced"):
        asyncio.run(maintenance.wipe_database())


def test_entrypoint_does_not_recursive_chown_downloads_by_default():
    source = (Path(__file__).resolve().parents[2] / "entrypoint.sh").read_text()
    assert "CHOWN_DOWNLOADS_RECURSIVE" in source
    assert "for DIR in /app/data /app/config /download" not in source


def test_scheduler_has_single_reconciliation_loop():
    source = (Path(__file__).resolve().parents[1] / "core" / "scheduler.py").read_text()
    assert "reconcile_download_client_cycle" not in source
    assert "async def recovery_loop" not in source
    assert "application.reconcile_executions()" in source


def test_zip_preflight_rejects_file_count_budget(tmp_path, monkeypatch):
    import zipfile
    import services.extraction_safety as safety
    from postprocessors.archive.extractor import _extract_zip

    archive = tmp_path / "many.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("one.txt", b"1")
        zf.writestr("two.txt", b"2")
    monkeypatch.setattr(
        safety, "get_settings",
        lambda: SimpleNamespace(
            extract_max_files=1,
            extract_max_expanded_gb=1,
            extract_max_compression_ratio=1000,
        ),
    )
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(ValueError, match="files"):
        _extract_zip(archive, dest)
    assert list(dest.iterdir()) == []


def test_external_staging_rejects_symlink_output(tmp_path, monkeypatch):
    import services.extraction_safety as safety

    archive = tmp_path / "archive.7z"
    archive.write_bytes(b"archive")
    dest = tmp_path / "dest"
    dest.mkdir()
    monkeypatch.setattr(
        safety, "get_settings",
        lambda: SimpleNamespace(
            extract_max_files=100,
            extract_max_expanded_gb=1,
            extract_max_compression_ratio=1000,
        ),
    )

    def malicious(stage):
        (stage / "escape").symlink_to(tmp_path / "outside")

    with pytest.raises(ValueError, match="symlink"):
        safety.staged_external_extract(archive, dest, malicious)
    assert list(dest.iterdir()) == []


def test_7z_listing_budget_is_validated_before_extraction(tmp_path, monkeypatch):
    import services.extraction_safety as safety

    archive = tmp_path / "archive.7z"
    archive.write_bytes(b"x" * 100)
    monkeypatch.setattr(
        safety, "get_settings",
        lambda: SimpleNamespace(
            extract_max_files=1,
            extract_max_expanded_gb=1,
            extract_max_compression_ratio=1000,
        ),
    )
    listing = "Header\n----------\nPath = one\nSize = 1\nAttributes = A\n\nPath = two\nSize = 1\nAttributes = A\n"
    with pytest.raises(ValueError, match="files"):
        safety.validate_7z_listing(archive, listing)


def test_rar_extraction_fails_closed_without_preflight_capable_7z():
    source = (Path(__file__).resolve().parents[1] / "postprocessors" / "archive" / "extractor.py").read_text()
    rar = source.split("def _extract_rar_to", 1)[1].split("def _extract_rar(", 1)[0]
    assert "_preflight_7z" in rar
    assert '"unrar"' not in rar
    assert '"unrar-free"' not in rar
    dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text()
    assert "7zip" in dockerfile
    assert "7zip-rar" in dockerfile
    assert "p7zip-full" not in dockerfile
    assert "unrar-free" not in dockerfile

def test_update_check_loop_has_failure_safe_backoff():
    source = (Path(__file__).resolve().parents[1] / "core" / "scheduler.py").read_text()
    loop = source.split("async def update_check_loop", 1)[1].split("async def events_ttl_loop", 1)[0]
    assert "while True:\n        # Keep a valid backoff even if settings retrieval itself fails.\n        interval_h = 12\n        try:" in loop
    assert "await asyncio.sleep(max(3600, interval_h * 3600))" in loop

def test_v106_transitional_and_mediainfo_residue_removed():
    root = Path(__file__).resolve().parents[1]
    for relative in ("services/mediainfo.py", "services/reconcile_cycle.py", "services/recovery.py"):
        assert not (root / relative).exists()
    routes = (root / "api" / "routes.py").read_text()
    assert '/mediainfo' not in routes
    assert 'services.mediainfo' not in routes


def test_alldebrid_native_operation_is_rate_limited_without_hidden_retry():
    source = (Path(__file__).resolve().parents[1] / "providers" / "alldebrid" / "client.py").read_text()
    post = source.split("async def _post", 1)[1].split("async def _multipart", 1)[0]
    assert "for attempt" not in post
    assert post.index("await acquire_alldebrid_request_slot()") < post.index("session.post(")
    assert post.count("await acquire_alldebrid_request_slot()") == 1


def test_dashboard_has_one_mixed_submission_control():
    root = Path(__file__).resolve().parents[2]
    html = (root / "frontend" / "static" / "index.html").read_text()
    js = (root / "frontend" / "static" / "app.js").read_text()
    assert html.count('id="q-transfer-input"') == 1
    assert 'id="q-debrid-links"' not in html
    assert 'id="q-magnet"' not in html
    assert 'id="btn-add-transfer"' in html
    assert 'https://example-hoster.com/file/' in html
    assert 'magnet:?xt=urn:btih:' in html
    assert 'Add links, magnets, or torrent files to the queue.' in html
    assert 'when empty, choose a .torrent file' in html
    assert 'addDashboardEntries()' in html
    assert "function classifyDashboardEntries" in js
    assert "openTorrentFilePicker();" in js
    assert "'/links/add'" in js
    assert "'/torrents/add-magnet'" in js
    assert "async function quickAdd()" not in js
    assert "async function addDebridLinks()" not in js
    assert "failureMessages.length === 1" in js
    assert "sanitizeErrorMsg(failureMessages[0])" in js


def test_dashboard_recent_activity_uses_viewport_slack():
    js = (Path(__file__).resolve().parents[2] / "frontend" / "static" / "app.js").read_text()
    assert "window.matchMedia('(max-width: 700px)').matches ? 4 : 6" in js
    assert "`/torrents?limit=${recentLimit}`" in js


def test_dead_indexer_css_is_physically_removed():
    css = (Path(__file__).resolve().parents[2] / "frontend" / "static" / "style.css").read_text()
    assert ".idx-picker" not in css
    assert ".idx-dropdown" not in css
    assert ".search-tags-row" not in css


def test_external_extractor_has_live_staging_budget_watch():
    root = Path(__file__).resolve().parents[1] / "services"
    safety = (root / "extraction_safety.py").read_text()
    extractor = (root.parent / "postprocessors" / "archive" / "extractor.py").read_text()
    assert "def validate_staging_tree" in safety
    assert "watch_dir: Path | None" in extractor
    assert "validate_staging_tree(watch_dir, watch_archive)" in extractor
    assert extractor.count("watch_dir=dest, watch_archive=archive") == 2


def test_security_and_compose_document_current_boundaries():
    root = Path(__file__).resolve().parents[2]
    security = (root / "SECURITY.md").read_text()
    compose = (root / "docker-compose.yml").read_text()
    assert "if auth is added in future" not in security
    assert "supports optional HTTP Basic Authentication" in security
    assert "network_mode: host" not in compose
    assert '"8080:8080"' in compose


def test_scheduler_exception_logging_is_sanitized():
    source = (Path(__file__).resolve().parents[1] / "core" / "scheduler.py").read_text()
    assert 'sanitize_exception' in source
    assert 'error: {e}' not in source
    assert 'error: %s", e)' not in source
    assert 'error: %s", exc)' not in source

def test_recovery_route_survives_without_transitional_wrapper():
    root = Path(__file__).resolve().parents[1]
    routes = (root / "api" / "routes.py").read_text()
    block = routes.split('async def run_recovery(', 1)[1].split('# ──', 1)[0]
    assert 'application.recover()' in block
    assert 'services.recovery' not in block
    assert not (root / "services" / "recovery.py").exists()


