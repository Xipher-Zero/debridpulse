from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HELP_RUNTIME = ROOT / "frontend" / "static" / "ui-help-page.js"


def _quick_start_source() -> str:
    source = HELP_RUNTIME.read_text(encoding="utf-8")
    start = source.index("  function quickStartPanel() {")
    end = source.index("\n  function howItWorksPanel() {", start)
    return source[start:end]


def test_quick_start_is_rewritten_for_normal_application_users():
    quick = _quick_start_source()

    required_orientation = (
        "Getting started with DebridPulse",
        "DebridPulse is the application you use to submit, route, track, and manage downloads.",
        "AllDebrid is the online service",
        "API key is a private credential",
        "aria2 is the component that performs the actual file transfer.",
        "isolated environment called a <b>container</b>",
        "Login protection:",
    )
    for phrase in required_orientation:
        assert phrase in quick


def test_quick_start_uses_current_ui_names_and_first_download_flow():
    quick = _quick_start_source()

    required_current_ui = (
        "Settings → Sources &amp; Providers",
        "Debrid Services → AllDebrid",
        "Apply Settings",
        "Settings → Downloads → Download Engine",
        "Built-in aria2",
        "Built-in Download Folder",
        "External RPC URL",
        "Dashboard",
        "Downloads",
        "Activity Log",
        "Statistics",
        "Data &amp; Maintenance",
    )
    for phrase in required_current_ui:
        assert phrase in quick

    retired_legacy_copy = (
        "Settings → AllDebrid",
        "Click <b>Save</b>",
        "Settings → Download</b>",
        "polls AllDebrid every 30 s",
        "Five steps to your first download",
    )
    for phrase in retired_legacy_copy:
        assert phrase not in quick


def test_quick_start_explains_source_types_and_safe_defaults_before_advanced_options():
    quick = _quick_start_source()

    for phrase in (
        "HTTP or HTTPS link:",
        "Magnet link:",
        ".torrent file:",
        "Direct-link batches can contain up to <b>100 unique links</b>",
        "You do not need to manually unlock links on AllDebrid first.",
        "Your DebridPulse aria2 process does <b>not</b> need to join the torrent swarm.",
        "The defaults are appropriate for a normal installation",
        "Choose <b>External aria2</b> only if you already have one.",
    ):
        assert phrase in quick


def test_quick_start_teaches_optional_features_and_recovery_without_internal_config_keys():
    quick = _quick_start_source()

    for phrase in (
        "Useful settings to explore next",
        "webhook",
        "OpenID Connect, usually shortened to OIDC",
        "Pause All stops processing, not intake",
        "Most failed transfers can be investigated or retried",
        "Recover All",
    ):
        assert phrase in quick

    for internal_name in (
        "poll_interval_seconds",
        "upload_fail_retry_count",
        "stuck_download_timeout_hours",
        "min_free_disk_gb",
    ):
        assert internal_name not in quick


def test_help_copy_does_not_use_em_dashes():
    source = HELP_RUNTIME.read_text(encoding="utf-8")
    assert "—" not in source
