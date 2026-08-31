import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_live_refresh_keeps_action_nodes_stable_and_coalesces_core_loaders():
    js = (REPO_ROOT / "frontend/static/app.js").read_text()

    assert "el.dataset.initialized !== '1'" in js
    assert 'id="btn-pause-all"' in js
    assert 'id="btn-resume-all"' in js
    assert "loadStats = coalesceAsync(loadStats);" in js
    assert "loadRecent = coalesceAsync(loadRecent);" in js
    assert "loadTorrents = coalesceAsync(loadTorrents);" in js


def test_progress_only_sse_updates_rows_without_forcing_full_render():
    js = (REPO_ROOT / "frontend/static/app.js").read_text()
    manager = (
        REPO_ROOT / "backend/services/manager_v2.py"
    ).read_text()

    assert "function patchProgressOnlyTransferEvent(data)" in js
    assert 'data-role="transfer-progress"' in js
    assert 'data-status="${esc(t.status)}"' in js

    assert (
        '"progress_only": not any('
        in manager
    )
    assert (
        'item["status_changed"]'
        in manager
    )
    assert (
        "for item in changed_updates"
        in manager
    )
    assert '"items": changed_updates' in manager
    assert '"status_changed": status_changed' in manager


def test_async_controls_acknowledge_clicks_immediately():
    js = (REPO_ROOT / "frontend/static/app.js").read_text()
    css = (REPO_ROOT / "frontend/static/style.css").read_text()

    for label in (
        "Pausing…",
        "Resuming…",
        "Retrying…",
        "Deleting…",
        "Queuing…",
    ):
        assert label in js

    assert '.btn:not(:disabled):active' in css
    assert 'aria-busy' in js


def test_detail_modal_opens_before_detail_request_finishes():
    js = (REPO_ROOT / "frontend/static/app.js").read_text()

    detail = js.split(
        "async function showDetail(id)", 1
    )[1].split(
        "function closeModal", 1
    )[0]

    assert detail.index(
        "overlay.classList.add('open')"
    ) < detail.index(
        "await api('GET',`/torrents/${id}`)"
    )

    assert "Loading transfer details…" in detail


def test_settings_put_response_is_reused_without_followup_get():
    js = (REPO_ROOT / "frontend/static/app.js").read_text()
    routes = (
        REPO_ROOT / "backend/api/routes.py"
    ).read_text()

    assert "data = _public_settings(clean)" in routes
    assert 'data["ok"] = True' in routes

    settings_put_assignments = re.findall(
        r"settingsData\s*=\s*await\s+api\(\s*'PUT'\s*,\s*'/settings'",
        js,
    )

    assert len(settings_put_assignments) == 5

    assert (
        "await api('PUT','/settings',d);\n"
        "    settingsData = await api('GET','/settings');"
        not in js
    )


def test_dashboard_unified_add_button_has_its_own_pending_target():
    js = (REPO_ROOT / "frontend/static/app.js").read_text()
    html = (REPO_ROOT / "frontend/static/index.html").read_text()

    assert 'id="btn-add-transfer"' in html
    assert "document.getElementById('btn-add-transfer')" in js
    assert "setButtonPending(button, true, 'Adding…')" in js



def test_settings_remote_tests_hold_pending_state_through_remote_test():
    js = (REPO_ROOT / "frontend/static/app.js").read_text()

    discord = js.split(
        "async function testDiscord(button)", 1
    )[1].split(
        "async function testAD(button)", 1
    )[0]

    assert discord.index(
        "'/settings/test-discord'"
    ) < discord.index(
        "renderSettings();"
    )

    aria2 = js.split(
        "async function testAria2(button)", 1
    )[1].split(
        "function renderAria2Diagnostics", 1
    )[0]

    assert aria2.index(
        "'/settings/test-aria2'"
    ) < aria2.index(
        "renderSettings();"
    )


def test_settings_aria2_queue_refresh_is_coalesced_and_actions_acknowledge():
    js = (REPO_ROOT / "frontend/static/app.js").read_text()

    assert (
        "loadAria2Downloads =\n"
        "  coalesceAsync(loadAria2Downloads);"
        in js
    )

    assert (
        "async function refreshAria2Downloads(button)"
        in js
    )

    assert (
        "async function aria2DownloadAction(gid, action, button)"
        in js
    )

    assert "Refreshing…" in js
    assert "Removing…" in js

    wipe = js.split(
        "async function wipeDatabase(button)", 1
    )[1].split(
        "async function sendStatsReport", 1
    )[0]

    assert wipe.index(
        "if (confirmText !== 'WIPE') return;"
    ) < wipe.index(
        "'Wiping…'"
    )






def test_pass3_polling_noops_do_not_refresh_transfer_freshness():
    manager = (REPO_ROOT / "backend/services/manager_v2.py").read_text()

    provider = manager.split(
        "async def _apply_provider_update", 1
    )[1].split(
        "async def _set_provider_missing", 1
    )[0]

    assert "meaningful_changed = (" in provider
    assert "if meaningful_changed:" in provider
    assert "if visible_changed:" in provider
    assert "persisted_progress = current_progress if local_delivery_active else progress" in provider
    assert "stable provider polling" in provider.lower()

    aggregate = (REPO_ROOT / "backend/services/transfer_state_machine.py").read_text()
    repository = (REPO_ROOT / "backend/services/transfer_repository.py").read_text()

    assert "if progress != current_progress or status != current_status:" in aggregate
    assert "if int(progress) != int(current_progress) or status != current_status:" in aggregate
    assert "updates.append((progress, status, transfer_id))" in aggregate
    assert "self.repository.persist_parent_progress(updates)" in aggregate
    assert "await db.executemany(" in repository

    sync = manager.split(
        "async def sync_aria2_downloads", 1
    )[1].split(
        "async def _reset_torrent_for_redownload", 1
    )[0]

    assert "f.download_id, f.status, f.blocked, f.size_bytes" in sync
    assert "def file_state_needs_update(desired_status: str)" in sync


def test_pass3_import_reconciliation_does_not_touch_stable_rows():
    manager = (REPO_ROOT / "backend/services/manager_v2.py").read_text()
    imported = manager.split(
        "async def import_existing_magnets", 1
    )[1].split(
        "async def delete_torrent", 1
    )[0]

    assert "metadata_changed = (" in imported
    assert imported.count("if metadata_changed:") == 2
    assert "stuck-transfer watchdog" in imported
    assert "Stable provider" in imported




def test_pass3_provider_noop_handles_zero_status_code_and_paused_delivery():
    manager = (REPO_ROOT / "backend/services/manager_v2.py").read_text()
    provider = manager.split(
        "async def _apply_provider_update", 1
    )[1].split(
        "async def _increment_poll_failure", 1
    )[0]

    assert 'current_provider_code = row.get("provider_status_code")' in provider
    assert "if current_provider_code is not None" in provider
    assert "TorrentStatus.PAUSED" in provider
