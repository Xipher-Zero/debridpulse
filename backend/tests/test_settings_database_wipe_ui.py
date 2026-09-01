from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RUNTIME = STATIC / "ui-settings-maintenance-wipe.js"
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
    assert "wipeButton.textContent = 'Reset Database';" in js
    assert "row.append(backupToggle, allowToggle, wipeActions);" in js




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
    assert "setToggleCopy(enabledToggle, 'Enable', '');" in js
    assert "header.appendChild(enabledToggle);" in js
    assert "dp-settings-backups-header-toggle" in js
    assert "dpBackupsRetentionPolished" in js


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


def test_backups_retention_field_text_uses_shared_three_pixel_input_datum():
    css = source(STYLE)

    assert '[data-panel="maintenance"] .dp-settings-field > .form-label' in css
    assert '[data-panel="maintenance"] .dp-settings-field > .form-hint' in css
    assert "position: relative;" in css
    assert "inset-inline-start: 3px;" in css


def test_run_backup_now_uses_scoped_success_semantics_and_list_remains_unchanged():
    js = source(RUNTIME)
    css = source(STYLE)

    assert "runButton.classList.remove('btn-ghost');" in js
    assert "runButton.classList.add('dp-settings-run-backup-success');" in js
    assert ".dp-settings-run-backup-success" in css
    assert "color: var(--green);" in css
    assert "color-mix(in srgb, var(--green) 12%, transparent)" in css
    assert "color-mix(in srgb, var(--green) 22%, transparent)" in css
    assert "listButton.classList.remove" not in js


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
