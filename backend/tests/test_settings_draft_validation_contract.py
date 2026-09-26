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
    bound = section(runtime, "function bindEvents(view)", "function fieldFor(key)")

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
    test_connection = section(runtime, "async function testConnection", "async function testDiscordDelivery")

    assert "api_key: valueOf('alldebrid_api_key')" in payload
    # DP 1.0.13: credential removal became an explicit confirmed action, so no
    # removal intent is ever pending at Test time and none is carried.
    assert "clear_api_key" not in payload
    assert "/settings/validate-alldebrid" in test_connection
    # DP 1.0.13: the Download Engine test is removed from the UI, so no draft
    # validation path reaches it at all.
    assert "test-aria2" not in test_connection
    assert "connectionTestPayload(kind)" in test_connection
    assert "persistNonAuth" not in test_connection
    assert "render();" not in test_connection
    assert "setDot(" not in test_connection


def test_notification_tests_exercise_the_saved_configuration_and_carry_no_draft() -> None:
    """DP 1.0.13 Notifications migration.

    Every Notifications field commits at its own boundary, so by the time a
    Test runs there is no draft left to send: the actions settle any pending
    write and then ask the backend to prove what is SAVED. They persist
    nothing, they adopt the derived verification state the backend returns, and
    a failure re-reads canonical truth rather than guessing at it.
    """
    runtime = read(RUNTIME)
    discord = section(runtime, "async function testDiscordDelivery", "async function sendStatsReport")
    report = section(runtime, "async function sendStatsReport", "async function uploadAvatar")

    for action in (discord, report):
        assert "await window.DPSettingsPersistence.settle(root());" in action
        assert ", undefined, 20000)" in action          # no payload at all
        assert "adoptNotifications(result.notifications);" in action
        assert "await refreshNotificationState();" in action
        assert "persistNonAuth" not in action
        assert "render();" not in action
        assert "valueOf(" not in action

    assert "'/settings/validate-discord'" in discord
    assert "'/settings/send-stats-report'" in report

    # Verification is canonical and derived; the page holds no bit of its own.
    assert "state.verified" not in runtime
    assert "verified = true" not in runtime
    status = section(runtime, "function notificationStatus(", "/* The three stored webhooks")
    assert "{text: 'Unconfigured', tone: 'error'}" in status
    assert "{text: 'Verified', tone: 'success'}" in status
    assert "{text: 'Configured', tone: 'warning'}" in status


def test_apply_settings_is_the_only_deferred_whole_settings_commit_boundary() -> None:
    runtime = read(RUNTIME)
    # Two writers of the whole-settings surface, and only two: the deferred
    # footer payload, and the canonical single-field settings-document commit
    # (which reads canonical truth and overrides exactly one field).
    assert runtime.count("request('PUT', '/settings'") == 2
    scope = section(runtime, "async function writeSettingsDocument", "/* Proof of what a successful Test")
    assert scope.count("request('PUT', '/settings'") == 1
    document_scope = section(runtime, "persistence.defineScope('settings-document'",
                             "/* The ONE whole-settings write.")
    assert "writeSettingsDocument({[option]: committedValue(key, draft)})" in document_scope
    assert runtime.count("persistNonAuth(") == 2  # declaration + Apply Settings path

    save_current = section(runtime, "async function saveCurrent", "function connectionTestPayload")
    assert "await persistNonAuth();" in save_current

    for start, end in (
        ("async function sendStatsReport", "async function uploadAvatar"),
        ("async function clearWebhook", "async function runBackup"),
        ("async function runBackup", "async function listBackups"),
        ("async function wipeDatabaseClean", "async function clearPassword"),
    ):
        assert "persistNonAuth" not in section(runtime, start, end)


def test_sources_copy_is_operator_facing_and_additional_fields_have_explanations() -> None:
    runtime = read(RUNTIME)
    # DP 1.0.13: the configured/unconfigured copy of the credential row is
    # declared just above the field helper, so the operator-facing copy of the
    # whole Services surface starts there.
    sources = section(runtime, "const ALLDEBRID_KEY_PLACEHOLDER", "function downloadsPanel")

    expected = (
        "Connect DebridPulse to AllDebrid for direct links, magnets, and torrent files.",
        # Entry/replacement is changed-blur and removal is an explicit action
        # behind the canonical confirmation, so neither sentence mentions a Save
        # any more, and the removal names exactly what it erases.
        "Enter a new API key to replace the stored key. Leave this field blank to keep the current key.",
        "Clear Stored API Key",
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
        '@router.post("/settings/validate-discord")',
    ):
        assert route in validation

    assert "AllDebridService(api_key, alldebrid.agent)" in validation
    assert "alldebrid_canonical_options(get_settings())" in validation
    # DP 1.0.13 transport consolidation: the Discord Test has no sender and no
    # endpoint classifier of its own -- it asks the ONE transport, strictly, so
    # the operator still gets the real delivery failure.
    assert "NotificationService(webhook_url).test(strict=True)" in validation
    for retired in ("_send_discord_test", "_is_discord_webhook"):
        assert retired not in validation, retired
    assert "clear_api_key" in validation
    # DP 1.0.13 Notifications migration: the Discord and statistics-report
    # operations exercise the SAVED configuration through the one
    # effective-destination owner, so no draft secret, and no draft clear
    # intent, reaches this file at all.
    for retired in ("clear_webhook", "_resolve_secret_candidate", "DiscordValidationRequest",
                    "StatisticsReportDraftRequest", "_draft_discord_identity"):
        assert retired not in validation, retired
    assert "notifications.discord_destination(cfg)" in validation
    assert "notifications.reporting_destination(cfg)" in validation
    assert "_record_notification_outcome(" in validation

    for forbidden in ("persistNonAuth", "PUT /settings"):
        assert forbidden not in validation

    # A validation route never persists the CANDIDATE it tested. It may record
    # the OUTCOME of a test about the configuration that is already saved --
    # DP 1.0.13 Services final corrective pass, Defect 3: a
    # successful test of exactly the saved configuration establishes durable
    # verification, and a failed one retires a proof that has stopped being
    # true rather than leaving the provider claiming "Verified". That is
    # metadata about canonical configuration, never canonical configuration.
    #
    # So the ban is on a second persistence path, not on the word: there is
    # exactly ONE save site in this file, it is that outcome recorder, and what
    # it writes is whatever the generic evidence owner returned -- never
    # anything assembled from the request.
    assert validation.count("save_settings(") == 1
    assert validation.count("apply_settings(") == 1
    writer = validation[validation.index("def _persist_evidence(updated)"):]
    writer = writer[:writer.index("\nasync def ")]
    assert "save_settings(updated)" in writer and "apply_settings(updated)" in writer
    assert "payload" not in writer and "api_key" not in writer

    # Both recorders hand that one writer whatever their generic evidence owner
    # returned -- an integration namespace's, or the notification boundary's --
    # and never anything assembled from a request.
    recorder = validation[validation.index("async def _record_verification_outcome("):]
    recorder = recorder[:recorder.index("\ndef _persist_evidence")]
    assert "record_verification_outcome(load_settings(), definition, fingerprint, ok)" in recorder
    assert "_persist_evidence(updated)" in recorder
    assert "payload" not in recorder and "api_key" not in recorder

    notifications = validation[validation.index("async def _record_notification_outcome("):]
    notifications = notifications[:notifications.index("\ndef ")]
    assert "record_notification(load_settings(), subject, fingerprint, ok)" in notifications
    assert "_persist_evidence(updated)" in notifications
    assert "payload" not in notifications and "webhook_url" not in notifications

    assert "from api.settings_validation_routes import router as settings_validation_router" in main
    assert 'app.include_router(settings_validation_router, prefix="/api")' in main
    for path in (
        '"/api/settings/validate-alldebrid"',
        '"/api/settings/validate-discord"',
    ):
        assert path in main
