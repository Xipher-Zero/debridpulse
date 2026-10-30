from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HELP_RUNTIME = ROOT / "frontend" / "static" / "ui-help-page.js"


def _source() -> str:
    return HELP_RUNTIME.read_text(encoding="utf-8")


def _panel(function_name: str, next_function_name: str) -> str:
    source = _source()
    start = source.index(f"  function {function_name}() {{")
    end = source.index(f"\n  function {next_function_name}(", start)
    return source[start:end]


def _manual() -> str:
    """Every documentation panel except the preserved License panel."""
    source = _source()
    start = source.index("  function quickStartPanel() {")
    end = source.index("\n  function licensePanel(", start)
    return source[start:end]


def test_everyday_use_covers_the_pages_controls_and_prompts_an_operator_meets():
    panel = _panel("usingPanel", "howItWorksPanel")

    for phrase in (
        "Pause All", "Resume All", "Resume Paused (N)",
        "Custom cap (MB/s)", "Set Custom",
        "Provider Status (sidebar, Admin only)", "Network Sources share one row",
        "Quick Add", "<b>100 different links</b>", "separated by a Tab",
        "<code>.torrent</code>: a torrent file, up to 16 MB.",
        "<code>.nzb</code>: a Usenet download, up to 1 GB.",
        "<code>.meta4</code>: a Metalink file, up to 16 MB.",
        "read as a list of links, up to 1 MB",
        "Recover All", "Download Activity",
        "Connect to a local network address?",
        "Download the item or its collection?",
        "Authentication Required", "Verify Server Identity",
        "Select files", "Cancel Transfer",
        "Priority", "Newest", "Move to Top", "Start Now",
        "Submitted As", "Route History", "Advanced acquisition details",
        "Discard downloaded progress?", "Transfer Trace Log",
        "Activity Log", "Event Type",
        "Transfer Methods", "Unattributed",
    ):
        assert phrase in panel, phrase
    # 1.0.14: Statistics no longer has the Overall & Rolling Metrics section.
    assert "Rolling Metrics" not in panel


def test_how_it_works_explains_routing_failure_scope_and_controls_without_guarantees():
    panel = _panel("howItWorksPanel", "servicesPanel")

    for phrase in (
        "1. Add", "2. Choose a service", "3. Prepare", "4. Download",
        "5. Check and finish", "6. Extract (optional)",
        "Specialist services come first.",
        # A single link may fall back to HTTP(S) once every specialist that
        # could take it is exhausted or unavailable (transfers/registry.py:257,
        # _engine_recovery.py:54); a multi-link submission never reopens it.
        "A single web link can still be downloaded directly over HTTP(S)",
        "It waits rather than guesses.",
        "No ranking setting.",
        "Enabled is not the same as usable.",
        "A temporary problem", "A problem with one service", "A problem with the download itself",
        "it does not replace a service that is working",
        "never assumes two files match because of their names or positions",
        "Prepare Backup Torrents", "on by default for AllDebrid, Debrid-Link, Premiumize and Real-Debrid",
        "off by default for TorBox", "A choice you already saved is kept.",
        "Files that finished downloading stay in your folder.",
        "Already in queue", "Consolidated",
        "What survives a restart",
        "are removed from the link before it is saved",
    ):
        assert phrase in panel, phrase

    # Failover is conditional; byte-resume across a switch is never promised.
    assert "does not always keep the progress already made" in panel
    for promise in ("seamless", "always resumes", "every provider supports"):
        assert promise not in panel.casefold()


def test_specialist_authority_is_documented_for_the_whole_submission_not_per_link():
    """One Quick Add (or link-list file) of direct links is ONE transfer with
    one root per link (``ApplicationService.submit_links``); a specialized
    claim on any root closes generic routing for every root
    (``IntegrationRegistry.collection_route_authority``), while each root
    keeps its own specialized claimants. Magnets are separate submissions,
    and Media Downloads claims do not carry that authority."""
    panel = _panel("howItWorksPanel", "servicesPanel")
    rule = panel[panel.index("<b>Links added together are one download.</b>"):]
    rule = rule[:rule.index("</li>")]

    for phrase in (
        "All the links you add at once in Quick Add, or in one link-list file, become a single download",
        "only magnets each become a download of their own",
        "If a debrid service supports any link in a download of two or more links, the whole download is left to debrid services",
        "each of its links goes to a debrid service that supports that particular link",
        "a link that none of them supports is not downloaded as an ordinary web file",
        "add them separately from links meant for a debrid service",
        "Media Downloads links do not have this effect",
    ):
        assert phrase in rule, phrase
    # The retired per-download reduction must not return.
    assert "When a specialist service claims a download, DebridPulse does not also hand it" not in panel


def test_services_document_every_registered_user_facing_integration():
    panel = _panel("servicesPanel", "enginesPanel")

    for name in (
        "AllDebrid", "Debrid-Link", "Premiumize", "Real-Debrid", "TorBox", "Usenet",
        "HTTP(S)", "(S)FTP", "SCP", "rsync", "WebDAV", "Metalink (Multimeta)", "Media Downloads",
    ):
        assert f"accordion('{name}'" in panel, name
    assert "<h3>Premium Services</h3>" in panel
    assert "<h3>Network Sources</h3>" in panel


def test_services_distinguish_api_key_and_device_authorization_accounts():
    panel = _panel("servicesPanel", "enginesPanel")

    def entry(name: str) -> str:
        start = panel.index(f"accordion('{name}'")
        return panel[start:panel.index("`)}", start)]

    for api_key_service in ("AllDebrid", "Debrid-Link", "Premiumize"):
        assert "<b>API Key</b>" in entry(api_key_service), api_key_service
    for device_service, button in (("Real-Debrid", "Connect Real-Debrid"), ("TorBox", "Connect TorBox")):
        text = entry(device_service)
        assert f"<b>{button}</b>" in text
        assert "API Key</b>" not in text
    assert "There is no key to paste." in entry("Real-Debrid")
    assert "<b>Add Server</b>" in entry("Usenet")


def test_nzb_preferences_and_plan_entitlement_are_not_presented_as_participation_or_guarantees():
    panel = _panel("servicesPanel", "enginesPanel")

    assert "<b>Use Premiumize Before Usenet</b>" in panel
    assert "<b>Use TorBox Before Usenet</b>" in panel
    assert "That switch only changes the order for NZBs" in panel
    assert "<b>Requires TorBox Pro.</b>" in panel
    # Plan and limit facts that come from the provider are qualified as such.
    for qualified in (
        "per TorBox's documentation",
        "per Premiumize's documentation",
        "per AllDebrid's documentation",
        "Per Real-Debrid's documentation",
    ):
        assert qualified in panel, qualified


def test_media_downloads_guidance_claims_neither_transcoding_nor_authenticated_media():
    panel = _panel("servicesPanel", "enginesPanel")
    media = panel[panel.index("accordion('Media Downloads'"):]

    for phrase in (
        "without re-encoding", "DebridPulse never converts media",
        "Preferred Subtitle Language", "Current Video", "Playlist",
        "media that needs a sign-in, cookies or a subscription",
        "starts that item again from the beginning",
    ):
        assert phrase in media, phrase


def test_engines_are_presented_as_four_managed_engines_not_one():
    panel = _panel("enginesPanel", "settingsPanel")

    for engine in ("aria2", "rsync", "Usenet (SABnzbd)", "Media Downloads (yt-dlp)"):
        assert f"inset('{engine}'" in panel, engine
    for phrase in (
        "there is nothing to install or connect",
        "one limit for all engines together",
        "shared equally between the engines that are downloading",
        "Transfer Method Settings → Network Sources",
        "Download Engine Activity",
    ):
        assert phrase in panel, phrase


def test_settings_reference_matches_the_seven_current_settings_tabs_and_save_semantics():
    panel = _panel("settingsPanel", "troubleshootingPanel")

    for section in ("Services", "Downloads", "Extraction", "Authentication", "Users &amp; Access",
                    "Notifications", "Data &amp; Maintenance"):
        assert f"accordion('{section}'" in panel, section

    for phrase in (
        "There is no Save button.",
        "leaving it blank keeps the stored value",
        "Download Behavior &amp; Limits", "Transfer Method Settings", "Disk Space &amp; Recovery",
        "Local Network Connections", "Skip Local Connection Confirmation",
        "Delete Archives After Extraction", "Archive Passwords", "Clear Passwords",
        "Authentication Status", "Username &amp; Password", "OpenID Connect",
        "Public DebridPulse Base URL", "OIDC Callback URL", "PUBLIC_BASE_URL",
        "Default Role for New OIDC Users", "Add Local Account", "Edit User",
        "Integration Accounts", "Issue Token", "Authorization: Bearer &lt;token&gt;",
        "finished downloads are never touched", "removing keeps the account's history",
        "Discord Notifications", "Download Added Webhook", "Statistics Reporting",
        "Automatic Report Interval", "/api/metrics",
        "Event Logging", "Backups &amp; Retention", "Run Backup",
        "Save Backup", "Restore Backup", "Add Backup",
        "Allow Database Reset", "Reset Database", "<code>WIPE</code>",
        "always makes a safety backup first",
    ):
        assert phrase in panel, phrase
    # The reset owns its own pause; the operator is never told to pause first.
    assert "processing to be paused" not in panel


def test_troubleshooting_is_symptom_first_and_covers_every_failure_family():
    panel = _panel("troubleshootingPanel", "licensePanel")

    for phrase in (
        "Before changing anything",
        "I added something, but nothing starts",
        "Quick Add refuses a link or file",
        "A service is amber or red, or its Test fails",
        "A download stays in Processing for a long time",
        "A download is slow, stalls or fails",
        "A server keeps asking for a password, or rejects its identity",
        "A Usenet download fails",
        "A media download fails",
        "Files are not where I expect, or disk space runs out",
        "Automatic extraction did not run or failed",
        "Discord or Prometheus is not working",
        "Server-Sent Events",
        "I cannot sign in",
        "auth_password_enabled", "auth_oidc_enabled",
        "Backup, restore or reset will not run",
    ):
        assert phrase in panel, phrase

    # The last-resort sign-in recovery stays qualified and non-destructive.
    recovery = panel[panel.index("Last resort, if no sign-in method works:"):]
    assert "back up its configuration file" in recovery
    assert "keeping the file valid JSON" in recovery
    assert "Your stored password and OIDC settings are kept" in recovery

    for retired in (
        "/api/torrents/diagnose",
        "min_free_disk_gb",
        "upload_fail_retry_count",
        '"auth_username": ""',
        '"auth_password": ""',
    ):
        assert retired not in panel


def test_manual_removes_retired_names_and_single_provider_or_single_engine_claims():
    manual = _manual()

    for stale in (
        "Sources &amp; Providers",
        "External Providers",
        "Downloads → Download Engine</b>",
        "Download Safety &amp; Recovery",
        "Additional Engine Tuning",
        "Allow Database Wipe",
        "Test AllDebrid",
        "Test aria2",
        "Test Discord",
        "Test OIDC Sign-In",
        "Send Test Report",
        "Statistics Reports",
        "Delete Archive After Extraction",
        "AllDebrid is the current specialized debrid provider",
        "current architecture has",
        "aria2 is the component that performs the actual file transfer",
        "Recent Activity",
        "Direct Sources",
        "Multi" + "link",
        "Polls every 30 s",
        "Settings → Download Client",
    ):
        assert stale not in manual, stale
