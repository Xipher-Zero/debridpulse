import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_live_refresh_keeps_action_nodes_stable_and_coalesces_core_loaders():
    js = (REPO_ROOT / "frontend/static/app.js").read_text()
    # DP 1.0.12 canonical flattening: loadTorrents is ui-downloads.js's own
    # self-wrap (the sole Downloads owner), not app.js's.
    downloads_js = (REPO_ROOT / "frontend/static/ui-downloads.js").read_text()

    assert "el.dataset.initialized !== '1'" in js
    assert 'id="btn-pause-all"' in js
    assert 'id="btn-resume-all"' in js
    assert "loadStats = coalesceAsync(loadStats);" in js
    assert "loadRecent = coalesceAsync(loadRecent);" in js
    assert "loadTorrents = coalesceAsync(loadTorrents);" in downloads_js




def test_async_controls_acknowledge_clicks_immediately():
    js = (REPO_ROOT / "frontend/static/app.js").read_text()
    downloads_js = (REPO_ROOT / "frontend/static/ui-downloads.js").read_text()
    # DP 1.0.12 canonical flattening: style.css is now a pure @import list;
    # the universal .btn material (including this active-state transform) is
    # the :is(.dp-btn, .btn) bridge in ui-universal-language.css, not a
    # second copy (the retired ui-legacy-foundation.css's own .btn material
    # was fully superseded by that bridge and was deleted, not migrated).
    css = (REPO_ROOT / "frontend/static/ui-universal-language.css").read_text()

    # Pause/Resume are shared with Dashboard Recent and stay in app.js;
    # Retry/Delete are Downloads-only and live in ui-downloads.js.
    for label in ("Pausing…", "Resuming…"):
        assert label in js
    for label in ("Retrying…", "Deleting…"):
        assert label in downloads_js

    assert ':is(.dp-btn, .btn):not(:disabled):active' in css
    assert 'aria-busy' in js


def test_detail_modal_opens_before_detail_request_finishes():
    js = (REPO_ROOT / "frontend/static/app.js").read_text()

    detail = js.split(
        "async function showDetail(id)", 1
    )[1].split(
        "function closeModal", 1
    )[0]

    # The shared modal coordinator opens the overlay synchronously before the
    # detail fetch is awaited, so the modal chrome is visible immediately.
    assert detail.index(
        "DPModal.open({mode: 'details'"
    ) < detail.index(
        "await api('GET',`/torrents/${id}`)"
    )

    coordinator = js.split("const DPModal = (function", 1)[1]
    assert coordinator.index("overlay.classList.add('open')") < coordinator.index(
        "function requestModalClose"
    )

    assert "Loading transfer details…" in detail


def test_settings_put_response_is_reused_without_followup_get():
    js = (REPO_ROOT / "frontend/static/app.js").read_text()
    routes = (
        REPO_ROOT / "backend/api/routes.py"
    ).read_text()

    assert "data = _public_settings(clean, application.definitions)" in routes
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











