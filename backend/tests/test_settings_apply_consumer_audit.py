"""The terminal Apply-consumer audit: generic Apply Settings has ZERO consumers.

The footer's Apply was the last deferred write on Settings. Data & Maintenance
was its final consumer; with that page migrated onto the canonical
field-persistence owner, nothing on the Settings surface semantically depends on
a page-level save any more -- and the footer was removed only after that was
true, never before it.

This case is the standing proof of that, and the fence against reacquiring the
dependency. It is deliberately NOT a regex sweep for a mood: it reads the
semantic registry the page keeps (``COMMIT_FIELDS``), the controls each panel
actually renders, and the writers that exist, because those are the owners of
the answer.
"""
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RUNTIME = (STATIC / "ui-settings-page.js").read_text(encoding="utf-8")
STYLE = (STATIC / "ui-settings-page.css").read_text(encoding="utf-8")
PERSISTENCE = (STATIC / "ui-settings-persistence.js").read_text(encoding="utf-8")


def block(start: str, end: str) -> str:
    return RUNTIME[RUNTIME.index(start):RUNTIME.index(end, RUNTIME.index(start))]


def declared_fields() -> set:
    return set(re.findall(r"^    ([a-z0-9_]+): \{scope:", block("const COMMIT_FIELDS", "});"), re.M))


# Every panel, and what follows it in source. The whole editable Settings
# surface is enumerated here; a seventh panel cannot be added without landing
# in this list.
PANELS = (
    ("function sourcesPanel(s)", "function downloadsPanel(s)"),
    ("function downloadsPanel(s)", "/* The two extraction behaviour controls"),
    ("function extractionPanel(s)", "/* ── Notifications"),
    ("function notificationsPanel(s)", "/* Converge the Notifications surface"),
    ("function authenticationPanel(a)", "/* ── Data & Maintenance"),
    ("function maintenancePanel(s)", "function panel(name, body)"),
)


COMPOSER = re.compile(r"(?:input|toggle|tuningToggle|selectField|directoryField)\('([a-z0-9_]+)'")


def rendered_controls(panel: str) -> tuple:
    """(controls the page persists, controls it declares it does not).

    ``commit: false`` is the page's own declared opt-out -- a READING, or a
    value the environment manages -- so this reads that declaration rather than
    carrying a hand-maintained list of exceptions.
    """
    names = set(re.findall(r"data-setting=\"\$\{html\(([a-z0-9_]+)\)\}\"", panel))
    names |= set(re.findall(r"data-setting=\"([a-z0-9_]+)\"", panel))
    calls = [(m.start(), m.group(1)) for m in COMPOSER.finditer(panel)]
    unpersisted = set()
    for index, (start, name) in enumerate(calls):
        names.add(name)
        end = calls[index + 1][0] if index + 1 < len(calls) else len(panel)
        if "commit: false" in panel[start:end]:
            unpersisted.add(name)
    return names - unpersisted, unpersisted


def test_no_generic_apply_control_exists_anywhere_on_settings():
    """The button, the sentence beside it, the marker the tab lifecycle used to
    hide, the container and the space it reserved. Removed, not hidden."""
    for retired in (
        'data-action="save"', "data-deferred-apply", "Apply Settings",
        "Changes remain unsaved", "dp-settings-master-footer",
        "dp-settings-save-hint", "FIELD_BOUNDARY_TABS",
    ):
        assert retired not in RUNTIME, retired
    for retired in ("dp-settings-master-footer", "dp-settings-save-hint"):
        assert retired not in STYLE, retired


def test_no_whole_settings_payload_builder_or_page_level_save_survives():
    """The three functions that WERE generic Apply: the form collector, the
    deferred write and the click handler. None has a non-Apply owner, so all
    three are gone rather than kept for one caller."""
    for retired in ("nonAuthPayload", "persistNonAuth", "saveCurrent", "intOf(", "boolOf("):
        assert retired not in RUNTIME, retired


def test_the_settings_document_has_exactly_one_writer_and_it_is_per_field():
    """What remains is the canonical single-field write: a read-modify-write
    against FRESHLY read canonical truth that overrides exactly the option that
    changed. It is reached by a field commit and by the explicit destructive
    clears -- never by collecting the page."""
    assert RUNTIME.count("async function writeSettingsDocument(") == 1
    assert RUNTIME.count("request('PUT', '/settings'") == 1
    writer = block("async function writeSettingsDocument(", "/* Proof of what a successful Test")
    assert "await request('GET', '/settings', null, 15000)" in writer
    assert "clear_secrets: clears" in writer
    # The destructive clears are the other legitimate consumers of that
    # contract, so the backend surface is not reachable only through Apply.
    for caller in ("writeSettingsDocument({}, ['extraction_password'])",
                   "writeSettingsDocument({}, [key])"):
        assert caller in RUNTIME, caller


def test_every_editable_control_on_every_panel_declares_its_own_boundary():
    """A control rendered with a `data-setting` that is not declared in
    COMMIT_FIELDS is a control nothing persists. With no page-level save left,
    that is not a deferred field -- it is an orphan, and there are none."""
    declared = declared_fields()
    assert declared
    seen = set()
    for start, end in PANELS:
        panel = block(start, end)
        found, unpersisted = rendered_controls(panel)
        assert found, start          # a panel that renders nothing is a bad probe
        for name in found:
            assert name in declared, (start, name)
        # A declared opt-out is a reading, never a field left to a save.
        for name in unpersisted:
            assert name not in declared, (start, name)
        seen |= found
    # The probe really does reach every tab, not just the one being changed.
    for witness in ("alldebrid_rate_limit_per_minute", "download_folder", "extract_enabled",
                    "discord_username", "auth_username", "backup_folder"):
        assert witness in seen, witness


def test_data_and_maintenance_declares_every_one_of_its_own_controls():
    """The final migration, named explicitly: the six values and three booleans
    that were the last Apply consumers now each have exactly one owner."""
    declared = declared_fields()
    for field in (
        "backup_enabled", "backup_folder", "backup_interval_hours", "backup_keep_days",
        "stats_snapshot_interval_minutes", "stats_snapshot_keep_days", "events_keep_days",
        "db_wipe_enabled", "db_backup_before_wipe",
    ):
        assert field in declared, field


def test_the_maintenance_booleans_commit_immediately_and_the_values_on_blur():
    table = block("const COMMIT_FIELDS", "});")
    for immediate in ("backup_enabled", "db_backup_before_wipe", "db_wipe_enabled"):
        row = re.search(rf"^    {immediate}: \{{(.+)\}},$", table, re.M).group(1)
        assert "scope: 'settings-document'" in row, immediate
        assert "commit: 'immediate'" in row, immediate
    for changed_blur in ("backup_folder", "backup_interval_hours", "backup_keep_days",
                         "stats_snapshot_interval_minutes", "stats_snapshot_keep_days",
                         "events_keep_days"):
        row = re.search(rf"^    {changed_blur}: \{{(.+)\}},$", table, re.M).group(1)
        assert "scope: 'settings-document'" in row, changed_blur
        assert "commit:" not in row, changed_blur


def test_no_field_has_two_persistence_owners():
    """A field written both by its scope and by something that collects the
    page is the defect this audit exists to catch. With no collector left there
    is nothing that could do it -- and no second whole-settings write."""
    assert "settingsDocument(state.settings)" not in RUNTIME
    assert RUNTIME.count("function settingsDocument(") == 1


def test_no_apply_before_action_guard_or_copy_remains():
    """An action may settle pending writes; none may require an Apply that no
    longer exists, and no copy may tell an operator to press one."""
    assert "before running a wipe" not in RUNTIME
    for action in ("run-backup", "list-backups", "wipe-database"):
        assert f"action === '{action}'" in RUNTIME, action
    # The one settle owner, reused; no Maintenance timer, queue or flush flag.
    assert "const settlePendingWrites = () => window.DPSettingsPersistence.settle(root());" in RUNTIME
    for invented in ("setTimeout(", "flushPending", "pendingMaintenance", "maintenanceQueue"):
        assert invented not in block("/* ── Data & Maintenance", "function panel(name, body)"), invented


def test_the_persistence_owner_still_exposes_only_the_two_boundaries():
    """Apply retirement removed a page-level write; it did not add a third
    commit class, a page-local persistence path or a new global."""
    assert "window.DPSettingsPersistence = Object.freeze({" in PERSISTENCE
    assert PERSISTENCE.count("document.addEventListener('focusout'") == 1
    assert PERSISTENCE.count("document.addEventListener('change'") == 1
    assert "DPMaintenancePersistence" not in RUNTIME
