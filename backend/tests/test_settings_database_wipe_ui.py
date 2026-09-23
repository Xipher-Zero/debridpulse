from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RUNTIME = STATIC / "ui-settings-page.js"   # the one Settings owner emits this markup
STYLE = STATIC / "ui-settings-maintenance-wipe.css"
LOADER = STATIC / "ui-presentation-loader.js"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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
    # Operator order: backup safeguard, explicit unlock, destructive action.
    row = js.split('<div class="dp-settings-database-wipe-row">', 1)[1]
    assert row.index("'db_backup_before_wipe'") < row.index("'db_wipe_enabled'") < row.index('data-action="wipe-database"')
    # The card is emitted in its final form: nothing rewrites it after render.
    assert "Database Destructive Actions" not in js and "dpWipeControlsPolished" not in js




def test_database_reset_toggle_text_is_stacked_and_controls_stay_adjacent():
    css = source(STYLE)

    assert ".dp-settings-database-wipe-row > .dp-settings-toggle" in css
    assert "display: flex;" in css
    assert "gap: 14px;" in css
    assert "width: fit-content;" in css
    assert ".dp-settings-database-wipe-row .toggle-info" in css
    assert "flex-direction: column;" in css
    assert "align-items: flex-start;" in css
    assert ".dp-settings-database-wipe-row .toggle" in css
    assert "flex: 0 0 auto;" in css
    assert ".dp-settings-database-wipe-action .btn" in css
    assert "width: auto;" in css


def test_backups_retention_header_copy_and_enable_control_are_locked():
    js = source(RUNTIME)

    assert "Backups & Retention" in js
    assert "Configure automated backups and retention for backups, statistics snapshots, and event logs." in js
    assert "dp-settings-backups-header-toggle" in js
    header = js.split("dp-settings-backups-header-toggle", 1)[1].split("</label>", 1)[0]
    assert '<span class="tl">Enable</span>' in header and "data-setting=\"backup_enabled\"" in header
    assert "dpBackupsRetentionPolished" not in js


def test_backups_retention_fields_use_requested_titles_and_flavor_copy():
    js = source(RUNTIME)

    expected = [
        ("Backup Folder", "Choose where DebridPulse stores database and configuration backups."),
        ("Backup Interval (Hours Between Backups)", "Set how often an automatic backup is created."),
        ("Backup Retention (Days to Keep)", "Delete backup files older than the configured number of days."),
        ("Statistics Snapshot Interval (Minutes Between Snapshots)", "Set how often DebridPulse records a statistics snapshot."),
        ("Statistics Snapshot Retention (Days to Keep)", "Delete statistics snapshots older than the configured number of days."),
        ("Event Log Retention (Days to Keep)", "Delete event log entries older than the configured number of days."),
    ]
    for title, flavor in expected:
        assert title in js
        assert flavor in js


def test_backups_retention_field_text_uses_the_one_canonical_field_datum():
    """DP 1.0.13 work item J: the field datum is declared ONCE.

    The label, the control and the helper copy start at the same inline edge --
    the control's outer box -- and that rule lives in the canonical Settings
    form-layout owner. This panel declared its own 3px copy of it, which is the
    duplication the correction removed; it must not reappear here.
    """
    css = source(STYLE)
    assert "inset-inline-start" not in css
    form_layout = (STATIC / "ui-settings-form-layout.css").read_text(encoding="utf-8")
    assert "#view-settings .dp-settings-field > .form-label" in form_layout
    assert "margin-inline-start: 0" in form_layout


def test_run_backup_now_uses_scoped_success_semantics_and_list_remains_unchanged():
    js = source(RUNTIME)
    css = source(STYLE)

    assert 'class="btn btn-sm dp-settings-run-backup-success" type="button" data-action="run-backup"' in js
    assert 'data-action="list-backups"' in js
    assert ".dp-settings-run-backup-success" in css
    assert "color: var(--green);" in css
    assert "color-mix(in srgb, var(--green) 12%, transparent)" in css
    assert "color-mix(in srgb, var(--green) 22%, transparent)" in css
    assert 'class="btn btn-ghost btn-sm" type="button" data-action="list-backups"' in js


def test_backups_retention_layout_is_three_by_two_with_centered_actions_and_responsive_collapse():
    css = source(STYLE)

    assert ".dp-settings-backups-field-grid" in css
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css
    assert "@media (max-width: 700px)" in css
    assert ".dp-settings-backups-actions" in css
    assert "justify-content: center;" in css
    assert ".dp-settings-backups-actions .btn" in css
    assert "width: auto;" in css
