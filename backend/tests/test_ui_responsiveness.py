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











