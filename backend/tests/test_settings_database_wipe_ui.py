"""Data & Maintenance: the terminal Settings page contract.

Backups & Retention and Database Reset Controls in the canonical Settings
grammar -- the operational actions in the card's own right-aligned header rail,
the one setting each card is about as a centred bordered island, the policy
numbers behind the canonical disclosure as compact tuning cells, and every
editable control committed by the canonical field-persistence owner.

Nothing here is a Maintenance invention. Each assertion names the shared
primitive the page reuses, so a lookalike built beside one of them fails.
"""
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RUNTIME = STATIC / "ui-settings-page.js"   # the one Settings owner emits this markup
STYLE = STATIC / "ui-settings-maintenance-wipe.css"
MODAL = STATIC / "ui-settings-modal.js"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def panel() -> str:
    js = source(RUNTIME)
    return js[js.index("  function maintenancePanel(s) {"):js.index("  function panel(name, body)")]


# ── Backups & Retention header rail ────────────────────────────────────────

def test_both_backup_actions_live_in_the_card_header_rail_in_order():
    body = panel()
    rail = body[body.index("headerAction:"):body.index("</label>`,")]
    assert rail.index('data-action="list-backups"') < rail.index('data-action="run-backup"')
    assert rail.index('data-action="run-backup"') < rail.index('data-setting="backup_enabled"')
    # Quiet/secondary for the reading, the existing positive treatment for the act.
    assert '<button class="btn btn-ghost btn-sm" type="button" data-action="list-backups">List Backups</button>' in rail
    assert 'class="btn btn-sm dp-settings-run-backup-success" type="button" data-action="run-backup">Run Backup<' in rail


def test_run_backup_now_and_the_old_body_action_row_are_gone():
    js = source(RUNTIME)
    assert "Run Backup Now" not in js
    for retired in ("dp-settings-backups-actions", "dp-settings-backup-list\"",
                    "dp-settings-result-list", "dp-settings-result-row",
                    "byId('dp-settings-backup-list')"):
        assert retired not in js, retired
    assert "dp-settings-backups-actions" not in source(STYLE)


def test_backups_retention_header_copy_and_enable_control_are_locked():
    js = source(RUNTIME)
    assert "Backups & Retention" in js
    assert "Configure automated backups and retention for backups, statistics snapshots, and event logs." in js
    header = js.split("dp-settings-backups-header-toggle", 1)[1].split("</label>", 1)[0]
    assert '<span class="tl">Enable</span>' in header and 'data-setting="backup_enabled"' in header
    assert "dpBackupsRetentionPolished" not in js


# ── The backup listing modal ───────────────────────────────────────────────

def test_the_backup_listing_uses_the_shared_dialog_owner_and_is_bounded():
    js = source(RUNTIME)
    listing = js[js.index("  async function listBackups(button) {"):js.index("  /* The destructive reset.")]
    # The ONE canonical dialog owner, with nothing to accept.
    assert "window.DPSettingsModal.open({" in listing
    assert "dismiss: true," in listing
    assert "title: 'Backups'," in listing
    # Listing semantics are the backend's: same endpoint, same order, same
    # entries, same files. No restore/delete management is added.
    assert "request('GET', '/admin/backups', undefined, 15000)" in listing
    assert "backups.map(item =>" in listing
    assert "html(item.name || 'backup')" in listing
    assert "html((item.files || []).join(', '))" in listing
    assert "No backups found." in listing
    # A READING adds no management: it issues no mutating request and renders
    # no control of its own inside the dialog.
    for absent in ("request('POST'", "request('DELETE'", "request('PUT'",
                   "request('PATCH'", "<button"):
        assert absent not in listing, absent
    # Bounded and internally scrollable, in the page's own list material.
    css = source(STYLE)
    bounds = css.split(".dp-settings-backup-list {", 1)[1].split("}", 1)[0]
    assert "max-height:" in bounds
    assert "overflow-y: auto;" in bounds


def test_the_shared_dialog_owner_composes_the_dismiss_only_footer_itself():
    """A read-only dialog has nothing to accept. That footer is composed by the
    one dialog owner, never by a client hiding a control it does not own."""
    modal = source(MODAL)
    assert "const dismissOnly = spec.dismiss === true;" in modal
    assert "${dismissOnly ? '' : `<button class=\"btn ${tone === 'danger' ? 'btn-danger' : 'btn-primary'}\" type=\"button\" data-modal-accept></button>`}" in modal
    # Every other consumer is untouched: accept still exists and still settles.
    assert "if (accept) accept.addEventListener('click', () => settle(true));" in modal
    assert "entry.accept && !entry.accept.disabled" in modal


# ── The Backup Folder island ───────────────────────────────────────────────

def test_backup_folder_is_the_centred_bordered_island_with_browse_inside_the_field():
    body = panel()
    island = body[body.index('<div class="dp-settings-backup-folder-island">'):body.index("${disclosureSection(")]
    assert "directoryField('backup_folder', 'Backup Folder'" in island
    assert "Choose where DebridPulse stores database and configuration backups." in island
    # Title-over-Hint left + control right, centred against it: the shared
    # inline-field grammar. Browse INSIDE the field: the shared embedded action.
    assert "inline: true, embedAction: true" in island
    assert "browseAction: 'browse-backup-folder'" in island
    # Exactly one Browse for this field, and it is the embedded one.
    assert body.count("browse-backup-folder") == 1

    css = source(STYLE)
    geometry = css.split("#view-settings .dp-settings-backup-folder-island {", 1)[1].split("}", 1)[0]
    assert "width: 70%;" in geometry
    assert "margin-inline: auto;" in geometry
    assert "border: 1px solid var(--dp-divider);" in geometry
    assert "border-radius: 12px;" in geometry


def test_the_backup_folder_field_reserves_room_so_the_path_cannot_run_under_browse():
    """`.dp-action-field` composes control and action as SIBLINGS inside one
    field border, so the text physically ends where the button begins. This is
    the universal field language, not a Maintenance rule."""
    language = (STATIC / "ui-universal-language.css").read_text(encoding="utf-8")
    assert ".dp-action-field" in language
    js = source(RUNTIME)
    embedded = js.split("function embeddedActionField(control, action, className", 1)[1].split("}", 1)[0]
    assert 'class="dp-action-field' in embedded


# ── The disclosure and the five policy settings ────────────────────────────

def test_the_disclosure_is_the_canonical_one_with_the_exact_label():
    body = panel()
    assert "disclosureSection('Additional Backup & Retention Settings', 'backup-retention'," in body
    js = source(RUNTIME)
    # The ONE disclosure component, exposing expanded/collapsed state.
    section = js.split("function disclosureSection(title, key, body, expanded = false)", 1)[1]
    assert "settingsDisclosure(bodyId, open, title, persistKey)" in section.split("\n  }", 1)[0]
    assert 'aria-expanded="${expanded}"' in js


def test_all_five_remaining_settings_moved_inside_using_the_compact_cell_treatment():
    body = panel()
    disclosure = body[body.index("${disclosureSection("):body.index("    `, {")]
    # The #2 compact treatment from Downloads -> Disk Space & Recovery: the same
    # collection and the same relationship primitive, not a lookalike grid.
    assert "tuningCells(" in disclosure
    assert disclosure.count("tuningGroup(") == 2
    order = [
        "input('backup_interval_hours', 'Backup Interval'",
        "input('backup_keep_days', 'Backup Retention'",
        "input('stats_snapshot_interval_minutes', 'Statistics Snapshot Interval'",
        "input('stats_snapshot_keep_days', 'Statistics Snapshot Retention'",
        "input('events_keep_days', 'Event Log Retention'",
    ]
    positions = [disclosure.index(call) for call in order]
    assert positions == sorted(positions)
    # The backup pair and the snapshot pair are grouped; event-log stands alone.
    first, second = [m.start() for m in re.finditer(r"tuningGroup\(", disclosure)]
    assert first < positions[0] < positions[1] < second < positions[2] < positions[3] < positions[4]


def test_the_five_settings_keep_every_unit_range_and_hint_they_had():
    disclosure = panel()
    expected = (
        ("backup_interval_hours", "min: 1, max: 168", "hours",
         "Set how often an automatic backup is created."),
        ("backup_keep_days", "min: 1, max: 90", "days",
         "Delete backup files older than the configured number of days."),
        ("stats_snapshot_interval_minutes", "min: 0, max: 1440", "minutes",
         "Set how often DebridPulse records a statistics snapshot."),
        ("stats_snapshot_keep_days", "min: 1, max: 365", "days",
         "Delete statistics snapshots older than the configured number of days."),
        ("events_keep_days", "min: 1,", "days",
         "Delete event log entries older than the configured number of days."),
    )
    for key, bounds, unit, hint in expected:
        cell = disclosure.split(f"input('{key}'", 1)[1].split("}),", 1)[0]
        assert "type: 'number'" in cell, key
        assert bounds in cell, key
        assert hint in cell, key
        # The unit is carried by the shared in-field unit primitive, which is
        # why the title no longer has to repeat it in parentheses.
        assert f"embedAction: fieldUnit('{unit}')" in cell, key
    for verbose in ("(Hours Between Backups)", "(Days to Keep)", "(Minutes Between Snapshots)"):
        assert verbose not in source(RUNTIME), verbose


# ── Database Reset Controls ────────────────────────────────────────────────

def test_database_reset_card_copy_and_operator_order_are_locked():
    js = source(RUNTIME)

    assert "Database Reset Controls" in js
    assert "Configure database safeguards. Perform a destructive database reset when required." in js
    assert "Database Reset is Destructive" in js
    assert (
        "Processing must be paused before the database can be reset. "
        "A backup can be created automatically before the reset begins."
    ) in js
    assert "Backup Database Before Reset" in js
    assert "Create a backup before resetting the database. The reset is aborted if the backup fails." in js
    assert "Allow Database Reset" in js
    assert "Unlock the database reset action." in js
    assert '>Reset Database</button>' in js
    row = js.split('<div class="dp-settings-database-wipe-row">', 1)[1]
    assert row.index("'db_backup_before_wipe'") < row.index("'db_wipe_enabled'") < row.index('data-action="wipe-database"')
    assert "Database Destructive Actions" not in js and "dpWipeControlsPolished" not in js


def test_a_deliberate_spacer_separates_the_warning_from_the_controls():
    body = panel()
    caution = body.index('class="dp-settings-caution"')
    spacer = body.index('class="dp-settings-database-reset-spacer"')
    controls = body.index('class="dp-settings-database-wipe-row"')
    assert caution < spacer < controls
    # Layout, not content: nothing for assistive technology to read.
    assert '<div class="dp-settings-database-reset-spacer" aria-hidden="true"></div>' in body
    height = source(STYLE).split("#view-settings .dp-settings-database-reset-spacer {", 1)[1].split("}", 1)[0]
    assert "height:" in height


def test_the_reset_controls_are_one_centred_bordered_island():
    css = source(STYLE)
    island = css.split("#view-settings .dp-settings-database-wipe-row {", 1)[1].split("}", 1)[0]
    assert "display: flex;" in island
    assert "justify-content: center;" in island
    assert "width: max-content;" in island
    assert "margin-inline: auto;" in island
    assert "border: 1px solid var(--dp-divider);" in island
    assert "border-radius: 12px;" in island
    # Never thrown to opposite edges, and never a full-width band.
    assert "space-between" not in island
    assert "max-width: min(100%, 980px);" in island
    # The toggle treatment itself is unchanged.
    toggle = css.split("#view-settings .dp-settings-database-wipe-row > .dp-settings-toggle {", 1)[1].split("}", 1)[0]
    assert "display: flex;" in toggle and "gap: 14px;" in toggle and "width: fit-content;" in toggle
    stack = css.split("#view-settings .dp-settings-database-wipe-row .toggle-info {", 1)[1].split("}", 1)[0]
    assert "flex-direction: column;" in stack and "align-items: flex-start;" in stack
    action = css.split("#view-settings .dp-settings-database-wipe-action .btn {", 1)[1].split("}", 1)[0]
    assert "width: auto;" in action


def test_reset_safety_is_unchanged_and_only_the_stale_apply_copy_moved():
    js = source(RUNTIME)
    wipe = js[js.index("  async function wipeDatabaseClean(button) {"):js.index("  /* Erasing a stored credential")]
    # The action settles pending writes, then reads CANONICAL truth.
    assert "await settlePendingWrites();" in wipe
    assert "if (!state.settings?.db_wipe_enabled) {" in wipe
    # Destructive confirmation, typed phrase and the paused requirement remain.
    assert "window.DPSettingsModal.confirm({" in wipe
    assert "typedPhrase: 'WIPE'," in wipe
    assert "tone: 'danger'," in wipe
    assert "Processing must be paused." in wipe
    assert "request('POST', '/admin/database/wipe', {confirm: true}, 60000)" in wipe
    assert "Pre-wipe backup created." in wipe
    # The one stale sentence -- and only it -- was corrected.
    assert "Apply" not in wipe
    assert "Turn on 'Allow Database Reset' before resetting the database" in wipe


# ── Persistence and operational actions ────────────────────────────────────

def test_every_maintenance_action_settles_pending_writes_before_it_runs():
    js = source(RUNTIME)
    assert "const settlePendingWrites = () => window.DPSettingsPersistence.settle(root());" in js
    for start, end in (
        ("  async function browseDirectory(purpose) {", "  async function runBackup(button) {"),
        ("  async function runBackup(button) {", "  /* The backup listing is a READING"),
        ("  async function listBackups(button) {", "  /* The destructive reset."),
        ("  async function wipeDatabaseClean(button) {", "  /* Erasing a stored credential"),
    ):
        action = js[js.index(start):js.index(end)]
        assert "await settlePendingWrites();" in action, start
    # Browse reaches the picker through that one settling owner, not directly.
    assert "action === 'browse-backup-folder') void browseDirectory('backup')" in js


def test_the_shared_toggle_composer_declares_its_commit_class():
    """Both reset booleans are rendered by the shared toggle composer, so the
    composer -- not this page -- is what enrols them with the persistence
    owner. A toggle whose key is undeclared still renders exactly as before."""
    js = source(RUNTIME)
    composer = js.split("  function toggle(key, label, detail, value, extraClass = '') {", 1)[1].split("\n  }", 1)[0]
    assert "${commitAttributes(key)}" in composer
