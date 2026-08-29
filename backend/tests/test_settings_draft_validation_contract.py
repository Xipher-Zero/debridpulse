"""Contract tests for Settings draft validation and single-owner events."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RUNTIME = STATIC / "ui-settings-page.js"
VALIDATION = ROOT / "backend" / "api" / "settings_validation_routes.py"
MAIN = ROOT / "backend" / "main.py"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def section(source: str, start: str, end: str) -> str:
    return source[source.index(start):source.index(end, source.index(start))]


def test_settings_events_are_delegated_once_on_the_persistent_root() -> None:
    runtime = read(RUNTIME)
    bound = section(runtime, "function bindEvents(view)", "function updateModeState()")

    assert "if (view.dataset.dpSettingsEventsBound === '1') return;" in bound
    assert "view.dataset.dpSettingsEventsBound = '1';" in bound
    assert "view.addEventListener('keydown'" in bound
    assert "view.addEventListener('change'" in bound
    assert "view.addEventListener('click'" in bound
    assert "event.target.closest('.dp-settings-tabs [data-tab]')" in bound
    assert "view.querySelector('.dp-settings-tabs')?.addEventListener" not in bound


def test_connection_tests_use_transient_drafts_without_saving_or_rerendering() -> None:
    runtime = read(RUNTIME)
    payload = section(runtime, "function connectionTestPayload(kind)", "async function testConnection")
    test_connection = section(runtime, "async function testConnection", "async function uploadAvatar")

    assert "api_key: valueOf('alldebrid_api_key')" in payload
    assert "clear_api_key: clears.has('alldebrid_api_key')" in payload
    assert "mode: valueOf('aria2_mode'" in payload
    assert "secret: valueOf('aria2_secret')" in payload
    assert "clear_secret: clears.has('aria2_secret')" in payload
    assert "webhook_url: valueOf('discord_webhook_url')" in payload
    assert "clear_webhook: clears.has('discord_webhook_url')" in payload

    for endpoint in (
        "/settings/validate-alldebrid",
        "/settings/validate-aria2",
        "/settings/validate-discord",
    ):
        assert endpoint in test_connection
    assert "connectionTestPayload(kind)" in test_connection
    assert "persistNonAuth" not in test_connection
    assert "render();" not in test_connection
    assert "setDot(" not in test_connection


def test_apply_settings_is_the_only_general_non_auth_settings_commit_boundary() -> None:
    runtime = read(RUNTIME)
    assert runtime.count("request('PUT', '/settings'") == 1
    assert runtime.count("persistNonAuth(") == 2  # declaration + Apply Settings path

    save_current = section(runtime, "async function saveCurrent", "function connectionTestPayload")
    assert "await persistNonAuth();" in save_current

    for start, end in (
        ("async function sendReport", "async function runBackup"),
        ("async function runBackup", "async function listBackups"),
        ("async function wipeDatabaseClean", "async function clearPassword"),
    ):
        assert "persistNonAuth" not in section(runtime, start, end)


def test_sources_copy_is_operator_facing_and_additional_fields_have_explanations() -> None:
    runtime = read(RUNTIME)
    sources = section(runtime, "function allDebridApiKeyField", "function downloadsPanel")

    expected = (
        "Connect DebridPulse to AllDebrid for direct links, magnets, and torrent files.",
        "Enter a new API key to replace the stored key when you click Apply Settings. Leave this field blank to keep the current key.",
        "Remove the saved API key when you click Apply Settings.",
        "Limits how many requests DebridPulse sends to AllDebrid each minute. Set to 0 for no local limit.",
        "How often DebridPulse checks AllDebrid for updates to active transfers. Shorter intervals provide faster status updates but increase API traffic.",
        "How often DebridPulse performs a complete reconciliation with AllDebrid. Set to 0 to disable scheduled full syncs.",
        "How many times DebridPulse retries a failed provider upload before giving up. Set to 0 to disable retries.",
        "How long DebridPulse waits between failed upload attempts. Set to 0 to retry immediately.",
    )
    for text in expected:
        assert text in sources


def test_transient_validation_routes_never_persist_candidate_secrets() -> None:
    validation = read(VALIDATION)
    main = read(MAIN)

    for route in (
        '@router.post("/settings/validate-alldebrid")',
        '@router.post("/settings/validate-aria2")',
        '@router.post("/settings/validate-discord")',
    ):
        assert route in validation

    assert "AllDebridService(api_key, cfg.alldebrid_agent)" in validation
    assert "Aria2Service(" in validation
    assert "NotificationService(webhook_url).test()" in validation
    assert "clear_api_key" in validation
    assert "clear_secret" in validation
    assert "clear_webhook" in validation

    for forbidden in ("save_settings", "apply_settings", "persistNonAuth", "PUT /settings"):
        assert forbidden not in validation

    assert "from api.settings_validation_routes import router as settings_validation_router" in main
    assert 'app.include_router(settings_validation_router, prefix="/api")' in main
    for path in (
        '"/api/settings/validate-alldebrid"',
        '"/api/settings/validate-aria2"',
        '"/api/settings/validate-discord"',
    ):
        assert path in main
