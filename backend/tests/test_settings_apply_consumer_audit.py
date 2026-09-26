"""Who still depends on the generic Apply Settings path.

The footer's Apply is the last deferred write on Settings. Four of the six tabs
-- Downloads, Extraction, Notifications and Authentication -- now commit every
control at its own field boundary, and two of those migrations happened by
deleting a page-specific deferred mechanism rather than by hiding it.

This case is the standing inventory of what is left, so a future field cannot
quietly reacquire an Apply dependency without saying so here. It is deliberately
NOT a regex sweep of the whole runtime: it reads the semantic registry the page
already keeps (``COMMIT_FIELDS``) and the one deferred payload builder
(``nonAuthPayload``), because those are the actual owners of the answer.

Outcome as of this workstream: generic Apply has exactly one consumer left --
the Data & Maintenance tab. The footer itself is intentionally untouched.
"""
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = (ROOT / "frontend" / "static" / "ui-settings-page.js").read_text(encoding="utf-8")


def block(start: str, end: str) -> str:
    return RUNTIME[RUNTIME.index(start):RUNTIME.index(end, RUNTIME.index(start))]


# Every control the page reads back out of the FORM when the footer saves. This
# is the whole population of Apply-dependent settings: anything not named here
# is carried forward from canonical truth and cannot be replayed by a save.
DEFERRED_FIELDS = frozenset({
    "backup_enabled", "backup_folder", "backup_interval_hours", "backup_keep_days",
    "stats_snapshot_interval_minutes", "stats_snapshot_keep_days", "events_keep_days",
    "db_wipe_enabled", "db_backup_before_wipe",
})


def _payload_reads() -> set:
    payload = block("function nonAuthPayload()", "// Each scoped surface answers")
    body = payload[payload.index("return {"):]
    return set(re.findall(r"(?:boolOf|intOf|valueOf|floatOf)\('([a-z0-9_]+)'", body))


def test_the_deferred_payload_reads_exactly_the_enumerated_survivors():
    assert _payload_reads() == DEFERRED_FIELDS


def test_every_survivor_belongs_to_data_and_maintenance():
    """One page, named explicitly, so the next boundary is the operator's to
    choose rather than something this audit quietly moved."""
    panel = block("function maintenancePanel(s)", "function panel(name, body)")
    for field in DEFERRED_FIELDS:
        assert f"'{field}'" in panel, field


def test_no_survivor_is_also_a_declared_field_boundary_control():
    """A field with two persistence paths is the defect this audit exists to
    catch: the footer would replay a stale page snapshot over a committed
    value."""
    declared = set(re.findall(r"^    ([a-z0-9_]+): \{scope:", block("const COMMIT_FIELDS", "});"),
                              re.M))
    assert declared
    assert declared & DEFERRED_FIELDS == set()


def test_every_migrated_tab_declares_all_of_its_own_controls():
    """The other five tabs are field-boundary surfaces, so nothing they render
    may reach the footer. `data-setting` without a commit declaration is a
    control the persistence owner does not know about."""
    declared = set(re.findall(r"^    ([a-z0-9_]+): \{scope:", block("const COMMIT_FIELDS", "});"),
                              re.M))
    for panel_start, panel_end in (
        ("function sourcesPanel(s)", "function downloadsPanel(s)"),
        ("function downloadsPanel(s)", "/* The two extraction behaviour controls"),
        ("function extractionPanel(s)", "/* ── Notifications"),
        ("function notificationsPanel(s)", "/* Converge the Notifications surface"),
        ("function authenticationPanel(a)", "function maintenancePanel(s)"),
    ):
        panel = block(panel_start, panel_end)
        rendered = set(re.findall(r"data-setting=\"\$\{html\(([a-z0-9_]+)\)\}\"", panel))
        rendered |= set(re.findall(r"(?:input|toggle|tuningToggle|selectField|directoryField)\('([a-z0-9_]+)'", panel))
        rendered |= set(re.findall(r"data-setting=\"([a-z0-9_]+)\"", panel))
        missing = {name for name in rendered
                   if name not in declared and f"{name}: {{scope:" not in RUNTIME
                   and f"commit: false" not in panel}
        assert not (missing & DEFERRED_FIELDS), (panel_start, missing)


def test_no_notifications_control_reaches_the_footer_at_all():
    payload = block("function nonAuthPayload()", "// Each scoped surface answers")
    for owned in ("discord_", "stats_report", "update_check_interval_hours", "clear_secrets"):
        assert owned not in payload, owned
    declared = block("const COMMIT_FIELDS", "});")
    for owned in ("discord_notifications_enabled", "discord_username", "discord_avatar_url",
                  "discord_webhook_url", "discord_webhook_added", "discord_notify_added",
                  "discord_notify_finished", "discord_notify_error", "discord_notify_extract",
                  "discord_notify_update", "update_check_interval_hours",
                  "stats_reporting_enabled", "stats_report_webhook_url",
                  "stats_report_interval_hours", "stats_report_window_hours"):
        assert f"{owned}: {{scope: 'settings-document'" in declared, owned


def test_the_deferred_marker_and_its_hint_have_one_owner_each():
    """`data-deferred-apply` is the ONE marker the tab lifecycle hides, and it
    marks exactly the footer's Apply button."""
    assert RUNTIME.count("data-deferred-apply") == 2      # the button, and the selector
    assert 'data-action="save" data-deferred-apply' in RUNTIME
    assert "[data-deferred-apply], .dp-settings-save-hint" in RUNTIME
    assert RUNTIME.count("dp-settings-save-hint") == 2


def test_no_page_specific_action_routes_through_the_footer():
    """The contextual footer region is gone with its last two members, so no
    page can reacquire a footer action without reintroducing the mechanism."""
    for retired in ("data-context-action", "dp-settings-context-actions", "contextAction"):
        assert retired not in RUNTIME, retired


def test_the_whole_settings_surface_has_exactly_two_writers():
    """The deferred footer payload, and the canonical single-field commit that
    reads canonical truth and overrides exactly one option. A third would be a
    second Apply path by another name."""
    assert RUNTIME.count("request('PUT', '/settings'") == 2
    assert RUNTIME.count("async function writeSettingsDocument(") == 1
    assert RUNTIME.count("async function persistNonAuth(") == 1
    assert RUNTIME.count("persistNonAuth(") == 2          # declaration + Apply


def test_no_backend_field_is_reachable_only_through_the_generic_apply_action():
    """`clear_secrets` is the one payload field the footer used to be the sole
    carrier of. It is still part of the whole-settings contract -- the explicit
    destructive clears use it -- but they reach it through the canonical
    single-field write, not through Apply."""
    payload = block("function nonAuthPayload()", "// Each scoped surface answers")
    assert "clear_secrets" not in payload
    writer = block("async function writeSettingsDocument(", "/* Proof of what a successful Test")
    assert "clear_secrets: clears" in writer
    for caller in ("writeSettingsDocument({}, ['extraction_password'])",
                   "writeSettingsDocument({}, [key])"):
        assert caller in RUNTIME, caller
