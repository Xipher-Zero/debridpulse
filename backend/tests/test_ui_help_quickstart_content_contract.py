from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HELP_RUNTIME = ROOT / "frontend" / "static" / "ui-help-page.js"


def _quick_start_source() -> str:
    source = HELP_RUNTIME.read_text(encoding="utf-8")
    start = source.index("  function quickStartPanel() {")
    end = source.index("\n  function usingPanel() {", start)
    return source[start:end]


def test_quick_start_needs_no_premium_account_to_reach_a_first_download():
    quick = _quick_start_source()

    for phrase in (
        "Getting started with DebridPulse",
        "DebridPulse is the application you use to submit, route, track, and manage downloads.",
        "You do not need a paid account to start.",
        "Ordinary web (HTTP and HTTPS), FTP, SFTP, SCP, rsync and WebDAV links",
        "is switched on in a new installation, so ordinary links work straight away",
    ):
        assert phrase in quick, phrase

    # A premium service is offered, never required, and AllDebrid is one of five.
    assert quick.index("Network Sources") < quick.index("To use a premium service")
    for retired in (
        "Connect your AllDebrid account",
        "AllDebrid is the current specialized debrid provider.",
        "You also need an <b>AllDebrid account</b>",
    ):
        assert retired not in quick


def test_quick_start_uses_current_settings_paths_and_account_connection_styles():
    quick = _quick_start_source()

    for phrase in (
        "Settings → Downloads → Download Behavior &amp; Limits",
        "Download Folder",
        "isolated environment called a <b>container</b>",
        "Settings → Services",
        "<b>AllDebrid, Debrid-Link and Premiumize:</b> paste the API key",
        "<b>Connect Real-Debrid</b> or <b>Connect TorBox</b>",
        "<b>Add Server</b>",
        "Settings → Authentication",
        "Quick Add",
        "Download Activity",
        "Activity Log",
    ):
        assert phrase in quick, phrase

    for retired in (
        "Apply Settings",
        "Sources &amp; Providers",
        "Downloads → Download Engine",
        "Click <b>Save</b>",
    ):
        assert retired not in quick


def test_quick_start_stays_proportionate():
    quick = _quick_start_source()

    # Four steps; protocol detail lives in the Services tab, not here.
    assert quick.count("${step(") == 4
    for detail in ("rsync+ssh://", "Directory Depth", "Collection Scan Timeout", "~name"):
        assert detail not in quick


def test_help_copy_does_not_use_em_dashes():
    source = HELP_RUNTIME.read_text(encoding="utf-8")
    assert "—" not in source
