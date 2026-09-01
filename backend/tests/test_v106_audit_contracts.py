import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_transfer_service_has_no_transparent_legacy_fallback():
    source = (Path(__file__).resolve().parents[1] / "services" / "transfer_service.py").read_text()
    assert "def __getattr__" not in source
    assert "return getattr(self._engine" not in source


def test_state_machine_does_not_import_http_layer():
    source = (Path(__file__).resolve().parents[1] / "services" / "transfer_state_machine.py").read_text()
    assert "from api.routes" not in source
    assert "from services.event_bus import publish" in source


def test_state_machine_uses_repository_instead_of_database_layer():
    source = (Path(__file__).resolve().parents[1] / "services" / "transfer_state_machine.py").read_text()
    assert "from db.database" not in source
    assert "get_db(" not in source
    assert "self.repository.parent_progress_rows()" in source
    assert "self.repository.persist_parent_progress(updates)" in source


@pytest.mark.asyncio
async def test_external_gateway_rejects_foreign_gid(monkeypatch):
    import services.aria2_gateway as gateway_module

    monkeypatch.setattr(gateway_module, "is_builtin_mode", lambda: False)
    aria2 = SimpleNamespace(pause=AsyncMock(), resume=AsyncMock())
    engine = SimpleNamespace(aria2=lambda: aria2)
    ownership = SimpleNamespace(owns=AsyncMock(return_value=False))
    gateway = gateway_module.Aria2Gateway(engine, ownership)

    with pytest.raises(PermissionError):
        await gateway.pause("foreign")
    with pytest.raises(PermissionError):
        await gateway.resume("foreign")
    aria2.pause.assert_not_awaited()
    aria2.resume.assert_not_awaited()


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
    assert "transfer_service.reconciliation.reconcile()" in source


def test_zip_preflight_rejects_file_count_budget(tmp_path, monkeypatch):
    import zipfile
    import services.extraction_safety as safety
    from services.extractor import _extract_zip

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


def test_alldebrid_client_rate_limits_multipart_uploads():
    source = (Path(__file__).resolve().parents[1] / "providers" / "alldebrid" / "client.py").read_text()
    multipart = source.split("async def _multipart", 1)[1].split("# ── User", 1)[0]
    assert "await acquire_alldebrid_request_slot()" in multipart
    assert "services.manager_v2" not in source

def test_materialization_engine_publishes_without_importing_http_layer():
    source = (Path(__file__).resolve().parents[1] / "services" / "manager_v2.py").read_text()
    assert "from api.routes" not in source
    assert "from services.event_bus import publish" in source
    direct = source.split("async def _broadcast_direct_link_update", 1)[1].split("@staticmethod", 1)[0]
    assert 'await publish(' in direct


def test_rar_extraction_fails_closed_without_preflight_capable_7z():
    source = (Path(__file__).resolve().parents[1] / "services" / "extractor.py").read_text()
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


def test_alldebrid_retry_attempts_are_individually_rate_limited():
    source = (Path(__file__).resolve().parents[1] / "providers" / "alldebrid" / "client.py").read_text()
    post = source.split("async def _post", 1)[1].split("async def _multipart", 1)[0]
    loop_at = post.index("for attempt in range(1, attempts + 1):")
    limiter_at = post.index("await acquire_alldebrid_request_slot()")
    assert limiter_at > loop_at
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
    assert 'One item per line · Empty + Add opens a .torrent file' in html
    assert 'column-gap:14px' in html
    assert 'font-size:11px;font-weight:400;color:var(--text3)' in html
    assert 'style="display:flex;gap:6px;margin-left:auto"' in html
    assert 'One item per line. Leave empty and click Add to choose a .torrent file.' not in html
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
    extractor = (root / "extractor.py").read_text()
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
    block = routes.split('async def run_recovery():', 1)[1].split('# ──', 1)[0]
    assert 'transfer_service.reconciliation.recover()' in block
    assert 'services.recovery' not in block
    assert not (root / "services" / "recovery.py").exists()

@pytest.mark.asyncio
async def test_direct_link_intake_is_durable_while_pause_all_is_active(monkeypatch):
    import services.manager_v2 as manager_module

    class FakeDb:
        def __init__(self):
            self.statements = []

        async def execute_returning_id(self, sql, params=()):
            self.statements.append((sql, params))
            return 77

        async def execute(self, sql, params=()):
            self.statements.append((sql, params))

        async def fetchone(self, sql, params=()):
            return {
                "id": 77,
                "name": "sample.zip",
                "status": "paused",
                "source": "direct_link",
                "provider_status": "deferred",
            }

        async def commit(self):
            return None

    fake_db = FakeDb()

    @asynccontextmanager
    async def fake_get_db():
        yield fake_db

    settings = SimpleNamespace(paused=True, alldebrid_api_key="configured")
    manager = manager_module.TorrentManager()
    schedule = MagicMock()
    monkeypatch.setattr(manager_module, "get_settings", lambda: settings)
    monkeypatch.setattr(manager_module, "get_db", fake_get_db)
    monkeypatch.setattr(manager, "_schedule_direct_link_collection", schedule)
    monkeypatch.setattr(manager, "_broadcast_direct_link_update", AsyncMock())

    result = await manager.add_direct_links(["https://host.invalid/sample.zip"])

    assert result["accepted_links"] == 1
    assert result["_deferred"] is True
    assert result["status"] == "paused"
    schedule.assert_not_called()
    assert any("provider_status=?" in sql for sql, _ in fake_db.statements)


def test_pause_all_defers_provider_intake_instead_of_rejecting_it():
    root = Path(__file__).resolve().parents[2]
    manager = (root / "backend/services/manager_v2.py").read_text()
    database = (root / "backend/db/database.py").read_text()
    maintenance = (root / "backend/services/db_maintenance.py").read_text()
    control = (root / "backend/services/transfer_control_service.py").read_text()
    reconciliation = (root / "backend/services/reconciliation_service.py").read_text()
    app = (root / "frontend/static/app.js").read_text()

    assert "DEFERRED_PROVIDER_STATUS = \"deferred\"" in manager
    assert "resume_deferred_provider_submissions" in manager
    assert "deferred_provider_submissions" in database
    assert '"deferred_provider_submissions"' in maintenance
    assert "DELETE FROM deferred_provider_submissions" in maintenance
    assert "await self.engine.resume_deferred_provider_submissions()" in control
    assert 'async_timer("reconcile.deferred_provider")' in reconciliation
    assert "processing is paused" in app
    assert "waiting for Resume All" in app

    magnet_start = manager.index("async def add_magnet_direct")
    magnet_end = manager.index("async def add_torrent_file_direct", magnet_start)
    file_start = magnet_end
    file_end = manager.index("async def add_direct_links", file_start)
    link_start = file_end
    link_end = manager.index("def _schedule_direct_link_collection", link_start)
    for segment in (
        manager[magnet_start:magnet_end],
        manager[file_start:file_end],
        manager[link_start:link_end],
    ):
        assert 'raise Exception("Processing is paused")' not in segment


@pytest.mark.asyncio
async def test_paused_magnet_intake_does_not_contact_provider(monkeypatch):
    import services.duplicates as duplicates
    import services.manager_v2 as manager_module

    class FakeDb:
        async def fetchone(self, sql, params=()):
            return None

    @asynccontextmanager
    async def fake_get_db():
        yield FakeDb()

    settings = SimpleNamespace(paused=True, alldebrid_api_key="configured")
    decision = SimpleNamespace(action="allow", matches=[])
    manager = manager_module.TorrentManager()
    provider = SimpleNamespace(upload_magnet=AsyncMock())
    persisted = AsyncMock(
        return_value={
            "id": 81,
            "status": "paused",
            "provider_status": "deferred",
            "_deferred": True,
        }
    )

    monkeypatch.setattr(manager_module, "get_settings", lambda: settings)
    monkeypatch.setattr(manager_module, "get_db", fake_get_db)
    monkeypatch.setattr(duplicates, "check_before_add", AsyncMock(return_value=decision))
    monkeypatch.setattr(manager, "ad", lambda: provider)
    monkeypatch.setattr(manager, "_persist_deferred_magnet", persisted)

    result = await manager.add_magnet_direct(
        "magnet:?xt=urn:btih:" + ("a" * 40)
    )

    assert result["_deferred"] is True
    persisted.assert_awaited_once()
    provider.upload_magnet.assert_not_awaited()


@pytest.mark.asyncio
async def test_paused_torrent_file_intake_does_not_contact_provider(monkeypatch):
    import services.duplicates as duplicates
    import services.manager_v2 as manager_module

    settings = SimpleNamespace(paused=True, alldebrid_api_key="configured")
    decision = SimpleNamespace(action="allow", matches=[])
    manager = manager_module.TorrentManager()
    provider = SimpleNamespace(upload_torrent_file=AsyncMock())
    persisted = AsyncMock(
        return_value={
            "id": 82,
            "status": "paused",
            "provider_status": "deferred",
            "_deferred": True,
        }
    )

    monkeypatch.setattr(manager_module, "get_settings", lambda: settings)
    monkeypatch.setattr(duplicates, "check_before_add", AsyncMock(return_value=decision))
    monkeypatch.setattr(manager, "ad", lambda: provider)
    monkeypatch.setattr(manager, "_persist_deferred_torrent_file", persisted)

    result = await manager.add_torrent_file_direct(
        b"torrent-payload",
        "queued.torrent",
        preferred_hash="b" * 40,
    )

    assert result["_deferred"] is True
    persisted.assert_awaited_once_with(
        b"torrent-payload", "queued.torrent", "manual", "b" * 40
    )
    provider.upload_torrent_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_deferred_direct_link_drain_starts_after_resume(monkeypatch):
    import services.manager_v2 as manager_module

    deferred_row = {
        "id": 83,
        "status": "queued",
        "provider_status": "deferred",
        "source": "direct_link",
        "magnet": '["https://host.invalid/queued.bin"]',
        "deferred_kind": None,
        "deferred_payload": None,
        "deferred_filename": None,
        "deferred_source": None,
    }

    class FakeDb:
        def __init__(self):
            self.statements = []

        async def fetchall(self, sql, params=()):
            return [deferred_row]

        async def fetchone(self, sql, params=()):
            if "SELECT status, provider_status" in sql:
                return {"status": "queued", "provider_status": "deferred"}
            return None

        async def execute(self, sql, params=()):
            self.statements.append((sql, params))

        async def commit(self):
            return None

    fake_db = FakeDb()

    @asynccontextmanager
    async def fake_get_db():
        yield fake_db

    settings = SimpleNamespace(paused=False)
    manager = manager_module.TorrentManager()
    schedule = MagicMock()
    monkeypatch.setattr(manager_module, "get_settings", lambda: settings)
    monkeypatch.setattr(manager_module, "get_db", fake_get_db)
    monkeypatch.setattr(manager, "_schedule_direct_link_collection", schedule)

    result = await manager.resume_deferred_provider_submissions()

    assert result == {"started": 1, "failed": 0}
    schedule.assert_called_once_with(83, ["https://host.invalid/queued.bin"])
    assert any("provider_status='submitted'" in sql for sql, _ in fake_db.statements)


def test_deleting_deferred_torrent_purges_stored_payload():
    root = Path(__file__).resolve().parents[2]
    manager = (root / "backend/services/manager_v2.py").read_text()
    start = manager.index("async def delete_torrent")
    end = manager.index("async def test_aria2", start)
    segment = manager[start:end]
    assert "DELETE FROM deferred_provider_submissions WHERE torrent_id=?" in segment
