"""The Help manual says what the 1.0.13 server actually does (1.0.13 reconciliation).

Each assertion first derives the fact from the owner that implements it --
authorization policy, routing, submission limits, the status vocabulary, the
statistics query, the legal-document map, the image's packaging -- and only
then checks the manual. A Help sentence is never its own oracle. Exhaustive
sentence-by-sentence reconciliation lives in the Gate 9 ledger; these are the
consequential claims.
"""
from __future__ import annotations

import re
from pathlib import Path

from auth.accounts import Role
from auth.permissions import ACTIONS, Capability, ROLE_CAPABILITIES

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
BACKEND = ROOT / "backend"
HELP = (STATIC / "ui-help-page.js").read_text(encoding="utf-8")


def _panel(name: str) -> str:
    start = HELP.index(f"function {name}()")
    return HELP[start:HELP.index("\n  function ", start + 10)]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


# ── License tab: two GUI documents removed, distribution intact ─────────────

def test_help_offers_exactly_the_three_surviving_legal_documents():
    buttons = re.findall(r'data-legal-document="([a-z-]+)"', HELP)
    assert buttons == ["gpl", "source-offer", "third-party"]
    lowered = HELP.lower()
    for gone in ("attribution notice", "upstream mit license", 'data-legal-document="notice"',
                 'data-legal-document="upstream-mit"'):
        assert gone not in lowered, gone


def test_the_removed_documents_still_ship_and_are_still_served():
    from api.settings_validation_routes import _LEGAL_DOCUMENTS

    # The backend legal-document API keeps every document, the two no longer
    # offered by Help included, and each file it names exists.
    assert {"gpl", "notice", "upstream-mit", "source-offer", "third-party"} <= set(_LEGAL_DOCUMENTS)
    for meta in _LEGAL_DOCUMENTS.values():
        assert (ROOT.joinpath(*meta["path"])).is_file(), meta["path"]
    dockerfile = _source("Dockerfile")
    assert "COPY LICENSE NOTICE SOURCE_OFFER.md /app/" in dockerfile
    assert "COPY LICENSES/ /app/LICENSES/" in dockerfile
    assert (ROOT / "LICENSES" / "MIT.txt").is_file() and (ROOT / "NOTICE").is_file()


def test_every_surviving_help_document_maps_to_a_bundled_file():
    from api.settings_validation_routes import _LEGAL_DOCUMENTS

    for document in re.findall(r'data-legal-document="([a-z-]+)"', HELP):
        assert document in _LEGAL_DOCUMENTS, document


# ── Roles and ownership: from the authorization policy ──────────────────────

def test_role_statements_follow_the_capability_table():
    integration = ROLE_CAPABILITIES[Role.INTEGRATION]
    user = ROLE_CAPABILITIES[Role.USER]
    readonly = ROLE_CAPABILITIES[Role.READONLY]
    everyday = _panel("usingPanel")
    settings = _panel("settingsPanel")

    # Integration: no browser interface, but it may submit and read metrics.
    assert Capability.INTERFACE not in integration and Capability.TRANSFERS_SUBMIT in integration
    assert "cannot sign in to these pages" in everyday and "cannot sign in to these pages" in settings
    assert "add downloads through the API" in settings
    # User: operates and may destroy only under the ownership precondition.
    assert {Capability.TRANSFERS_OPERATE, Capability.TRANSFERS_DESTROY} <= user
    for panel in (everyday, settings, _panel("howItWorksPanel")):
        assert "only when no other account still needs" in panel
    # ReadOnly: reads only.
    assert readonly.isdisjoint({Capability.TRANSFERS_SUBMIT, Capability.TRANSFERS_OPERATE,
                                Capability.TRANSFERS_DESTROY, Capability.ADMINISTER})
    assert "changes nothing" in everyday
    # Personal notifications are a User's alone.
    holders = {role for role, caps in ROLE_CAPABILITIES.items() if Capability.NOTIFICATIONS_SELF in caps}
    assert holders == {Role.USER}
    assert "Admins, ReadOnly Users and Integration accounts have no personal notifications." in settings


def test_admin_only_controls_are_labelled_as_such():
    # The policy's action owners for these controls require ADMINISTER.
    for action in ("api.operational_downloads.list_activity_events", "api.routes.pause_processing",
                   "api.routes.recover_all_ready", "api.routes.export_transfer_trace"):
        assert ACTIONS[action] == Capability.ADMINISTER, action
    everyday = _panel("usingPanel")
    for label in ("Pause All</b> (Admin only)", "Provider Status (sidebar, Admin only)",
                  "Recover All</b> (Admin only)", "Activity Log (Admin only)",
                  "Transfer Trace Log</b> (Admin only)"):
        assert label in everyday, label


# ── Submission, routing and identity ───────────────────────────────────────

def test_quick_add_limits_match_the_submission_owner():
    from transfers.requests import MAX_DIRECT_LINKS_PER_BATCH, MAX_LINK_FILE_BYTES
    from transfers.staged_input import MAX_STAGED_INPUT_BYTES

    everyday = _panel("usingPanel")
    assert MAX_DIRECT_LINKS_PER_BATCH == 100 and "<b>100 different links</b> at once (magnets are not counted)" in everyday
    assert MAX_LINK_FILE_BYTES == 1024 * 1024 and "up to 1 MB" in everyday
    assert MAX_STAGED_INPUT_BYTES == 1024 ** 3 and "a Usenet download, up to 1 GB" in everyday


def test_routing_describes_the_single_link_fallback_and_the_collection_rule():
    recovery = _source("backend/transfers/_engine_recovery.py")
    registry = _source("backend/transfers/registry.py")
    # Collection affinity (generic closed for the whole submission) applies to
    # two or more roots only; an exhausted specialist lets generic compete.
    assert "if len(roots) <= 1:\n                return False" in recovery
    assert "a generic\n        # provider competes once no remaining specialized one claims" in registry
    how = _panel("howItWorksPanel")
    assert "A single web link can still be downloaded directly over HTTP(S)" in how
    assert "in a download of two or more links, the whole download is left to debrid services" in how
    assert "not even if that service later fails" not in how


def test_duplicate_detection_is_described_for_the_kinds_that_have_it():
    service = _source("backend/application/service.py")
    # Direct links are admitted with a fresh identity every time.
    assert "deduplicate=False" in service
    how = _panel("howItWorksPanel")
    assert "Web links are always added as a new download." in how
    assert "each account's request stays its own" in how


def test_status_words_follow_the_presentation_vocabulary():
    how = _panel("howItWorksPanel")
    # The raw 'unknown' state is labelled "Awaiting confirmation": it is the
    # engine/service state being confirmed, not a question for the user.
    assert '"unknown": ("unknown", "Awaiting confirmation", "unknown")' in _source(
        "backend/transfers/presentation_repository.py")
    assert "<b>Awaiting confirmation:</b> DebridPulse is confirming the download's real state" in how
    assert "<b>Input Required, Awaiting confirmation:</b>" not in how


def test_provider_status_checking_is_pulsing_amber():
    status = _source("frontend/static/ui-provider-status.js")
    shell = _source("frontend/static/ui-shell.css")
    assert "checking:'check'" in status
    assert ".dot.check  { background: var(--yellow); animation: pulse" in shell
    everyday = _panel("usingPanel")
    assert "<b>Pulsing amber:</b> still checking" in everyday and "<b>Grey:</b>" not in everyday


def test_transfer_methods_counts_downloads():
    routes = _source("backend/api/routes.py")
    assert "A transfer counts ONCE under each acquisition method" in routes
    everyday = _panel("usingPanel")
    assert "<b>Transfer Methods</b> counts finished downloads" in everyday
    assert "counts finished files" not in everyday


# ── Notifications, reports and snapshots ───────────────────────────────────

def test_report_destination_and_snapshot_semantics():
    from types import SimpleNamespace

    from services.notification_service import reporting_destination

    assert reporting_destination(SimpleNamespace(stats_report_webhook_url="", discord_webhook_url="https://d")) == "https://d"
    settings = _panel("settingsPanel")
    assert "They go only to <b>Reporting Webhook</b>, or to the Discord Webhook when that is empty" in settings
    assert "never to DebridPulse's users" in settings
    # No browser surface reads the stored snapshots.
    assert not any("/stats/snapshots" in path.read_text(encoding="utf-8") for path in STATIC.glob("*.js"))
    assert "not currently viewable in the interface" in settings


def test_database_reset_scope_follows_the_maintenance_owner():
    maintenance = _source("backend/services/db_maintenance.py")
    for kept in ("accounts", "account_tokens", "account_notifications"):
        assert kept in maintenance
    settings = _panel("settingsPanel")
    assert "Accounts, passwords, OIDC links, API tokens, personal notification settings and all settings are kept." in settings


# ── Names: the manual points at what really exists ─────────────────────────

def test_settings_locations_named_in_help_exist():
    page = _source("frontend/static/ui-settings-page.js")
    tabs = re.findall(r"\['[a-z]+', '([^']+)', '[^']+'\]", page.split("const TABS", 1)[1].split("]);", 1)[0])
    assert tabs == ["Services", "Downloads", "Extraction", "Authentication", "Users & Access",
                    "Notifications", "Data & Maintenance"]
    for card in ("Download Behavior & Limits", "Transfer Method Settings", "Disk Space & Recovery",
                 "Download Engine Activity", "Automatic Extraction", "Authentication Status",
                 "Username & Password", "OpenID Connect", "User Accounts", "Integration Accounts",
                 "Integration Delivery Profiles", "Discord Notifications", "Statistics Reporting",
                 "Event Logging", "Backups & Retention", "Database Reset Controls"):
        assert f"'{card}'" in page or f"'{card}'," in page, card
        assert card.replace("&", "&amp;") in HELP, card
    locations = re.findall(r"<b>Settings → ([A-Za-z &;]+?)(?: →|</b>)", HELP)
    assert len(locations) >= 10
    for location in locations:
        assert location.replace("&amp;", "&") in tabs, location
    # Every Settings card has a help chip; the manual says where to find it.
    assert "function helpChip(topic, subject)" in page
    assert "<b>Help</b> button (?)" in _panel("settingsPanel")


def test_terminology_and_scope_stay_current():
    lowered = HELP.lower()
    assert "multilink" not in lowered
    assert "Metalink (Multimeta)" in HELP
    # 1.0.14 contexts are future scope and are not described as present.
    for future in ("wizard", "browser plugin", "browser extension", "standalone", "easy mode", "advanced mode"):
        assert future not in lowered, future
