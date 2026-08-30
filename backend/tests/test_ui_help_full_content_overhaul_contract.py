from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HELP_RUNTIME = ROOT / "frontend" / "static" / "ui-help-page.js"


def _source() -> str:
    return HELP_RUNTIME.read_text(encoding="utf-8")


def _panel(function_name: str, next_function_name: str) -> str:
    source = _source()
    start = source.index(f"  function {function_name}() {{")
    end = source.index(f"\n  function {next_function_name}() {{", start)
    return source[start:end]


def test_how_it_works_explains_current_multi_stage_pipeline_without_legacy_fixed_polling():
    panel = _panel("howItWorksPanel", "aria2Panel")

    for phrase in (
        "How DebridPulse moves a download",
        "1. Intake",
        "2. Provider preparation",
        "3. Transfer planning",
        "4. aria2 delivery",
        "5. Verification and finish",
        "6. Optional extraction",
        "verified alternate mirror links",
        "Recover All",
        "Pause All is a processing gate",
    ):
        assert phrase in panel

    for legacy in (
        "Polls every 30 s",
        "status: uploading",
        "upload_fail_retry_count",
        "stuck_download_timeout_hours",
    ):
        assert legacy not in panel


def test_aria2_help_uses_current_downloads_ui_and_safe_external_ownership_model():
    panel = _panel("aria2Panel", "integrationsPanel")

    for phrase in (
        "Settings → Downloads → Download Engine",
        "Built-in aria2",
        "External aria2",
        "External RPC URL",
        "/jsonrpc",
        "aria2 RPC Secret",
        "External aria2 Download Path",
        "does not use external mode as a general administration interface",
        "Maximum Concurrent Downloads",
        "Continue Partial Downloads",
        "Segments per File",
        "Connections per Server",
        "Download Safety &amp; Recovery",
        "speed-cap control",
        "Test aria2",
        "current draft values",
    ):
        assert phrase in panel

    for legacy in (
        "Settings → Download Client",
        "aria2 Config",
        "Auto-memory tuning",
        "Downloads → ↓ Limit",
    ):
        assert legacy not in panel


def test_integrations_cover_current_discord_prometheus_api_and_oidc_workflows():
    panel = _panel("integrationsPanel", "settingsPanel")

    for phrase in (
        "Settings → Notifications → Discord Notifications",
        "Added-event Webhook",
        "Statistics Reports",
        "Automatic Report Interval",
        "GET /api/metrics",
        "metrics_path: /api/metrics",
        "Settings → Authentication → API Access",
        "Authorization: Bearer &lt;token&gt;",
        "API Access does not replace browser authentication",
        "Public DebridPulse Base URL",
        "/auth/oidc/callback",
        "Test OIDC Sign-In",
        "PUBLIC_BASE_URL",
    ):
        assert phrase in panel

    assert "upload failed, no peers" not in panel.casefold()


def test_settings_reference_matches_the_six_current_settings_sections_and_save_semantics():
    panel = _panel("settingsPanel", "troubleshootingPanel")

    for section in (
        "Sources &amp; Providers",
        "Downloads",
        "Extraction",
        "Notifications",
        "Authentication",
        "Data &amp; Maintenance",
    ):
        assert f"<summary>{section}</summary>" in panel

    for phrase in (
        "Apply Settings",
        "leaving its replacement field blank keeps the stored value",
        "Test AllDebrid",
        "Download Safety &amp; Recovery",
        "Archive Passwords",
        "Discord Notifications",
        "Authentication Status",
        "Username &amp; Password",
        "OpenID Connect",
        "Public DebridPulse Base URL",
        "API Access",
        "Run Backup Now",
        "Allow Database Wipe",
        "processing to be paused",
    ):
        assert phrase in panel

    for retired in (
        "<summary>General</summary>",
        "<summary>Download Client</summary>",
        "<summary>AllDebrid API</summary>",
        "<summary>Advanced / Maintenance</summary>",
        "Optional HTTP Basic Auth",
        "Leave either empty to disable",
    ):
        assert retired not in panel


def test_troubleshooting_is_user_facing_and_covers_current_recovery_paths():
    panel = _panel("troubleshootingPanel", "licensePanel")

    for phrase in (
        "Before changing anything",
        "I added something, but no work starts",
        "Pause All",
        "Test AllDebrid",
        "Provider processing appears stuck",
        "Recover All",
        "A download is stalled or aria2 reports an error",
        "Download Safety &amp; Recovery",
        "Files are not appearing in the expected folder",
        "External aria2 will not connect",
        "Automatic extraction did not run or failed",
        "Discord or Prometheus integration is not working",
        "Server-Sent Events",
        "Username, password, or OIDC sign-in is failing",
        "Public DebridPulse Base URL",
        "auth_password_enabled",
        "auth_oidc_enabled",
        "Database maintenance will not run",
    ):
        assert phrase in panel

    for retired in (
        "/api/torrents/diagnose",
        "min_free_disk_gb",
        "upload_fail_retry_count",
        "Upload Failed (code 5)",
        "No peers (code 8)",
        '"auth_username": ""',
        '"auth_password": ""',
        "15-second polling",
    ):
        assert retired not in panel


def test_license_help_explains_bundled_documents_and_preserves_all_legal_actions():
    panel = _panel("licensePanel", "panel")

    for phrase in (
        "Licensing and source",
        "GPL-2.0-or-later",
        "use, study, modify, and redistribute",
        "without warranty",
        "kroeberd/alldebrid-client v1.9.9",
        "Upstream MIT license",
        "Source offer",
        "Third-party licenses",
        "exact legal documents bundled with the running DebridPulse build",
        "latest repository copy",
        "/LICENSE",
        "/NOTICE",
        "/LICENSES/MIT.txt",
        "/SOURCE_OFFER.md",
        "/docs/DEPENDENCY_LICENSES.md",
    ):
        assert phrase in panel


def test_remaining_help_overhaul_removes_known_legacy_user_copy():
    source = _source()

    for legacy in (
        "Settings → Download Client → aria2 Config",
        "Settings → AllDebrid → Test",
        "Polls every 30 s",
        "upload_fail_retry_count",
        "stuck_download_timeout_hours",
        "min_free_disk_gb",
        "Auth is enabled but I'm locked out",
    ):
        assert legacy not in source
