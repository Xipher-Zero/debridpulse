from pathlib import Path

from packaging.version import Version

from core.branding import APP_METADATA_TITLE, APP_NAME, APP_SHORT_NAME, REPOSITORY_URL
from core.config import AppSettings
from main import app


REPO_ROOT = Path(__file__).resolve().parents[2]

REMOVED_SETTINGS = {
    "sonarr_enabled",
    "radarr_enabled",
    "jackett_enabled",
    "prowlarr_enabled",
    "flexget_enabled",
    "rules_enabled",
    "saved_searches_interval_minutes",
    "plex_url",
    "jellyfin_url",
    "on_torrent_complete",
    "download_profiles",
    "priority_aging_interval_minutes",
    "watch_folder",
    "processed_folder",
    "watch_interval_seconds",
}

REMOVED_ROUTE_MARKERS = {
    "/api/v2",
    "/jackett",
    "/prowlarr",
    "/flexget",
    "/saved-searches",
    "/rules/",
    "/download-profiles",
    "/webhooks/test",
}


def test_v1_identity_is_debridpulse_everywhere_it_is_centralized():
    assert APP_NAME == "DebridPulse"
    assert APP_SHORT_NAME == "DebridPulse"
    assert APP_METADATA_TITLE == "DebridPulse — AllDebrid + aria2 Download Manager"
    assert REPOSITORY_URL == "https://github.com/Xipher-Zero/debridpulse"


def test_removed_services_and_qbit_router_are_not_shipped():
    removed_files = (
        "backend/api/qbit.py",
        "backend/services/flexget.py",
        "backend/services/integrations.py",
        "backend/services/jackett.py",
        "backend/services/learning.py",
        "backend/services/media_server.py",
        "backend/services/prowlarr.py",
        "backend/services/rules.py",
        "backend/services/webhook_actions.py",
    )
    assert not [path for path in removed_files if (REPO_ROOT / path).exists()]


def test_removed_settings_are_not_accepted_by_v1_model():
    assert REMOVED_SETTINGS.isdisjoint(AppSettings.model_fields)


def test_removed_api_routes_are_not_registered():
    routes = {getattr(route, "path", "") for route in app.routes}
    assert not {
        path
        for path in routes
        if any(marker in path for marker in REMOVED_ROUTE_MARKERS)
    }


def test_api_has_no_duplicate_method_and_path_registrations():
    registrations = [
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", set())
        if method not in {"HEAD", "OPTIONS"}
    ]
    assert len(registrations) == len(set(registrations))


def test_frontend_and_runtime_lock_have_no_legacy_surface():
    frontend = "\n".join(
        (REPO_ROOT / path).read_text().casefold()
        for path in ("frontend/static/index.html", "frontend/static/app.js")
    )
    for marker in (
        "qbittorrent",
        "jackett",
        "prowlarr",
        "flexget",
        "saved-search",
        "sonarr",
        "radarr",
    ):
        assert marker not in frontend

    requirements = (REPO_ROOT / "backend/requirements.txt").read_text().casefold()
    assert "bencode2==0.3.33" in requirements
    assert "bencodepy" not in requirements


def test_release_surfaces_do_not_advertise_removed_watch_folder_workflow():
    release_surfaces = "\n".join(
        (REPO_ROOT / path).read_text().casefold()
        for path in (
            "README.md",
            "index.html",
            "Dockerfile",
            "docker-compose.yml",
            "entrypoint.sh",
            "frontend/static/index.html",
        )
    )
    assert "watch folder" not in release_surfaces
    assert "/app/data/watch" not in release_surfaces
    assert "/app/data/processed" not in release_surfaces


def test_v1_runtime_database_scope_is_sqlite_only():
    assert not (REPO_ROOT / "docker-compose.postgres.yml").exists()
    surfaces = "\n".join(
        (REPO_ROOT / path).read_text().casefold()
        for path in (
            "backend/api/routes.py",
            "backend/core/config.py",
            "frontend/static/app.js",
            "backend/db/database.py",
        )
    )
    assert "postgres_internal" not in surfaces
    assert "postgresql" not in surfaces
    assert "asyncpg" not in surfaces


def test_unified_submission_input_expands_to_five_lines():
    frontend = (REPO_ROOT / "frontend/static/index.html").read_text()
    scripts = (REPO_ROOT / "frontend/static/app.js").read_text()
    styles = (REPO_ROOT / "frontend/static/style.css").read_text()

    assert 'id="q-transfer-input" rows="2"' in frontend
    assert 'id="q-debrid-links"' not in frontend
    assert 'id="q-magnet"' not in frontend
    assert 'oninput="resizeDebridLinkInput(this)"' in frontend
    assert "function resizeDebridLinkInput(input)" in scripts
    assert "const minimum = Math.ceil((lineHeight * 2) + chrome);" in scripts
    assert "const maximum = Math.ceil((lineHeight * 5) + chrome);" in scripts
    assert "resizeDebridLinkInput(input);" in scripts
    assert ".direct-link-input" in styles
    assert "overflow-y: hidden" in styles


def test_release_workflow_accepts_public_v1_tags():
    workflow = (REPO_ROOT / ".github/workflows/fork-image.yml").read_text()
    release_helper = (REPO_ROOT / "release.py").read_text()
    assert "- 'v*'" in workflow
    assert "startsWith(github.ref, 'refs/tags/v')" in workflow
    assert "DB_PATH=/app/data/debridpulse.db" in workflow
    version = (REPO_ROOT / "VERSION").read_text().strip()
    parsed = Version(version)
    assert parsed.release[:2] == (1, 0)
    assert 'tag = f"v{version}"' in release_helper
    assert "internal-v{version}" not in release_helper


def test_operator_tab_title_uses_short_active_identity_and_queue_average():
    frontend = (REPO_ROOT / "frontend/static/app.js").read_text()
    routes = (REPO_ROOT / "backend/api/routes.py").read_text()

    assert "document.title = 'DebridPulse';" in frontend
    assert "document.title = `DP | ${speed} (${_operatorTitleState.progress}%)`;" in frontend
    assert "document.title = `DebridPulse | (${active} Active)" not in frontend
    assert "renderOperatorTitle();" in frontend

    assert "AVG(CASE WHEN status='downloading' THEN COALESCE(progress, 0)" in routes
    assert "AS operator_active_progress_pct" in routes
    assert "AS weighted_progress" not in routes


def test_dashboard_recent_activity_exposes_pause_resume_but_not_remove():
    frontend = (REPO_ROOT / "frontend/static/app.js").read_text()
    index = (REPO_ROOT / "frontend/static/index.html").read_text()

    recent_renderer = frontend.split("async function loadRecent()", 1)[1].split(
        "function openTorrentFilePicker()", 1
    )[0]
    recent_markup = index.split('id="dash-activity-card"', 1)[1].split(
        "</table>", 1
    )[0]

    assert "pauseT(${t.id},this)" in recent_renderer
    assert "resumeT(${t.id},this)" in recent_renderer
    assert "Pause this download" in recent_renderer
    assert "Resume this download" in recent_renderer
    assert "deleteT(" not in recent_renderer
    assert "Remove" not in recent_markup
    assert 'colspan="6"' in recent_markup

    pause_item_handler = frontend.split(
        "async function pauseT(id, button)", 1
    )[1].split(
        "async function resumeT(id, button)", 1
    )[0]

    resume_item_handler = frontend.split(
        "async function resumeT(id, button)", 1
    )[1].split(
        "// ── Detail Modal", 1
    )[0]

    for handler in (pause_item_handler, resume_item_handler):
        assert "loadTorrents();" in handler
        assert "loadStats();" in handler
        assert "loadRecent();" in handler


def test_global_pause_control_exposes_mixed_selective_pause_state():
    frontend = (REPO_ROOT / "frontend/static/app.js").read_text()
    index = (REPO_ROOT / "frontend/static/index.html").read_text()
    styles = (REPO_ROOT / "frontend/static/style.css").read_text()
    routes = (REPO_ROOT / "backend/api/routes.py").read_text()

    assert "Resume Paused (${selectivelyPaused})" in frontend
    assert 'onclick="resumePausedDownloads()"' in frontend
    assert 'id="btn-pause-all"' in frontend
    assert 'onclick="pauseProcessing()"' in frontend
    assert 'id="btn-resume-all"' in frontend
    assert 'onclick="resumeProcessing()"' in frontend
    assert "el.dataset.initialized !== '1'" in frontend
    assert "pausedTransferCount = Math.max(0, Number(bs.paused) || 0)" in frontend
    resume_handler = frontend.split(
        "async function resumeT(id, button)", 1
    )[1].split(
        "// ── Detail Modal", 1
    )[0]

    assert "settingsData.paused = result.paused" in resume_handler
    assert "pausedTransferCount" in resume_handler
    assert "Math.max(0, pausedTransferCount - 1)" in resume_handler
    assert index.index('id="topbar-actions"') < index.index('id="aria2-speed-badge"')
    assert "#aria2-speed-badge" in styles
    assert "white-space: nowrap" in styles
    assert "flex: 0 0 230px" in styles
    assert "width: 64px" in styles
    assert "font-variant-numeric: tabular-nums" in styles

    pause_handler = frontend.split("async function pauseProcessing()", 1)[1].split(
        "async function resumeProcessing()", 1
    )[0]
    assert "loadRecent();" in pause_handler
    assert "loadTorrents()" in pause_handler

    pause_route = routes.split("async def pause_processing():", 1)[1].split(
        '@router.post("/processing/resume")', 1
    )[0]
    resume_route = routes.split("async def resume_processing():", 1)[1].split(
        "# ── Changelog", 1
    )[0]
    assert "await transfer_service.pause_all_downloads()" in pause_route
    assert "await transfer_service.resume_all_downloads()" in resume_route
    assert "save_settings" not in pause_route
    assert "apply_settings" not in resume_route

    control_service = (REPO_ROOT / "backend/services/transfer_control_service.py").read_text()
    assert "self._set_global_paused(True)" in control_service
    assert "self._set_global_paused(False)" in control_service
    assert "self.coordinator._schedule_queue()" in control_service

    manager = (REPO_ROOT / "backend/services/manager_v2.py").read_text()
    sync_handler = manager.split("async def sync_aria2_downloads(self):", 1)[1].split(
        "async def _reset_torrent_for_redownload", 1
    )[0]
    dispatch_handler = manager.split(
        "async def _engine_dispatch_pending_aria2_queue", 1
    )[1].split("async def _schedule_ready_aria2_parents", 1)[0]
    ready_handler = manager.split(
        "async def _schedule_ready_aria2_parents", 1
    )[1].split("async def _engine_advance_aria2_queue_locked", 1)[0]
    advance_handler = manager.split(
        "async def _engine_advance_aria2_queue_locked", 1
    )[1].split("async def _remove_owned_aria2_gid", 1)[0]
    assert "if self.is_paused()" not in sync_handler.split("all_downloads =", 1)[0]
    assert 'self.download_client_name() != "aria2" or self.is_paused()' in dispatch_handler
    assert "status='ready'" in ready_handler
    assert "provider_status='ready'" in ready_handler
    assert "targeted_manual_resume" not in dispatch_handler + ready_handler + advance_handler
    assert "allow_while_paused" not in dispatch_handler + ready_handler + advance_handler


def test_topbar_uses_live_aria2_speed_with_human_download_units():
    frontend = (REPO_ROOT / "frontend/static/app.js").read_text()
    index = (REPO_ROOT / "frontend/static/index.html").read_text()
    styles = (REPO_ROOT / "frontend/static/style.css").read_text()
    routes = (REPO_ROOT / "backend/api/routes.py").read_text()
    aria2_service = (REPO_ROOT / "backend/services/aria2.py").read_text()

    assert frontend.count("function fmtSpeed(bps)") == 1
    assert frontend.count("function fmtSpeedCap(bps)") == 1
    assert "fmtTransferRate(speed, 100)" in frontend
    assert "fmtTransferRate(speed, 1000)" in frontend
    assert "Number(value.toFixed(2)) >= rollover" in frontend
    assert "return '0 KB/s'" in frontend
    assert "return '<1 KB/s'" in frontend
    assert "return 'Unlimited'" in frontend
    assert "const units = ['KB', 'MB', 'GB', 'TB']" in frontend
    assert "'Externally Controlled'" in frontend
    assert "externalControl" in frontend
    assert "if (_aria2BadgeState.externalControl) return;" in frontend
    assert '<span id="aria2-badge-limit">Unlimited</span>' in index

    runtime_handler = frontend.split("async function loadAria2Runtime()", 1)[1].split(
        "async function aria2RuntimeAction", 1
    )[0]
    assert "active: Number(data.active) || 0" in runtime_handler
    assert "liveBps: Number(data.download_speed) || 0" in runtime_handler
    assert "api('GET', '/aria2/global-stat', null, 3000)" in frontend
    assert "}, 1000);" in frontend
    assert "_aria2TopbarStatBusy" in frontend
    assert 'id="aria2-badge-limit"' in index
    assert 'id="aria2-cap-menu"' in index
    assert 'id="aria2-cap-custom-mbps"' in index
    assert "Custom cap (MB/s)" in index
    assert "applyAria2TopbarSpeedCap(104857600)" in index
    assert "Math.round(mbps * 1048576)" in frontend
    assert "updateAria2TopbarBadge({limitBps: bps})" in frontend
    assert ".aria2-cap-menu" in styles
    assert ".aria2-cap-options" in styles
    assert "#aria2-speed-badge.external-control" in styles

    assert "async def get_active(self)" in aria2_service
    assert "owned_active = await transfer_service.owned_aria2_downloads(active_downloads)" in routes
    assert "downloads = await transfer_service.owned_aria2_downloads(downloads)" in routes
    assert '"external_control": True' in routes


def test_watch_folder_ingestion_is_not_shipped_in_v1():
    frontend = "\n".join(
        (REPO_ROOT / path).read_text()
        for path in ("frontend/static/index.html", "frontend/static/app.js")
    )
    scheduler = (REPO_ROOT / "backend/core/scheduler.py").read_text()
    manager = (REPO_ROOT / "backend/services/manager_v2.py").read_text()

    for marker in (
        'id="s-watch_folder"',
        'id="s-processed_folder"',
        'id="s-watch_interval_seconds"',
        "Watch Folder Scan",
    ):
        assert marker not in frontend

    assert "watch_folder_loop" not in scheduler
    assert "scan_watch_folder" not in manager
    assert "_handle_magnet_file" not in manager
    assert "_handle_torrent" not in manager


def test_dashboard_kpi_strip_omits_duplicate_database_tile_and_stays_centered():
    index = (REPO_ROOT / "frontend/static/index.html").read_text()
    frontend = (REPO_ROOT / "frontend/static/app.js").read_text()
    styles = (REPO_ROOT / "frontend/static/style.css").read_text()

    dashboard_strip = index.split(
        '<div class="dash-kpi-strip dash-kpi-strip--dashboard">', 1
    )[1].split('</div>\n\n      <div id="debug-status"', 1)[0]

    assert dashboard_strip.count('class="dash-kpi"') == 6
    assert 'id="i-db-type"' not in dashboard_strip
    assert '<div class="dash-kpi-lbl">Database</div>' not in dashboard_strip
    assert "getElementById('i-db-type')" not in frontend
    assert "setDot('db'" in frontend
    assert ".dash-kpi-strip--dashboard" in styles
    assert "width: 85.7142857%;" in styles


def test_v102_minor_ui_cleanup_contract():
    index = (REPO_ROOT / "frontend/static/index.html").read_text()
    frontend = (REPO_ROOT / "frontend/static/app.js").read_text()
    styles = (REPO_ROOT / "frontend/static/style.css").read_text()

    assert '<div class="metric-label">Downloads</div>' in frontend
    assert '<div class="metric-label">Torrents</div>' not in frontend
    assert '<span class="card-title">Download Status</span>' in index
    assert '<span class="card-title">Torrent Status</span>' not in index

    assert '<textarea class="input direct-link-input" id="q-transfer-input" rows="2"' in index
    assert 'oninput="resizeDebridLinkInput(this)"' in index
    assert "(event.ctrlKey||event.metaKey)&&event.key==='Enter'" in index
    assert "addDashboardEntries()" in index
    unified_add = frontend.split("async function addDashboardEntries()", 1)[1].split(
        "// ── Torrents", 1
    )[0]
    assert "document.getElementById('q-transfer-input')" in unified_add
    assert "document.getElementById('btn-add-transfer')" in unified_add
    assert "openTorrentFilePicker();" in unified_add
    assert "resizeDebridLinkInput(input);" in unified_add
    assert "async function quickAdd()" not in frontend

    assert '.aria2-queue { display: flex; flex-direction: column; gap: 10px; min-width: 0; width: 100%; }' in styles
    assert 'max-width: 100%' in styles.split('.aria2-job {', 1)[1].split('}', 1)[0]
    assert 'overflow-wrap: anywhere' in styles.split('.aria2-job-name {', 1)[1].split('}', 1)[0]
    assert 'overflow-wrap: anywhere' in styles.split('.aria2-job-meta {', 1)[1].split('}', 1)[0]
    assert '/style.css?v=15' in index
    assert '/app.js?v=15' in index


def test_inherited_file_preview_and_block_routes_are_hardened():
    routes = (REPO_ROOT / "backend/api/routes.py").read_text()
    assert "size_bytes, status, blocked, progress" not in routes
    block_route = routes.split('async def block_file(torrent_id: int, file_id: int, blocked: bool = True):', 1)[1].split('@router.get("/torrents/{torrent_id}")', 1)[0]
    assert "download_id" in block_route
    assert "status not in" in block_route
    assert "409" in block_route


def test_removed_postgres_migration_documentation_is_not_shipped():
    assert not (REPO_ROOT / "docs/migration.md").exists()
