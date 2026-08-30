from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RUNTIME = STATIC / "ui-settings-maintenance-wipe.js"
STYLE = STATIC / "ui-settings-maintenance-wipe.css"
LOADER = STATIC / "ui-presentation-loader.js"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_database_wipe_card_copy_and_operator_order_are_locked():
    js = source(RUNTIME)

    assert "Database Wipe Controls" in js
    assert "Configure database safeguards. Perform a destructive database reset when required." in js
    assert "Database Wipe is Destructive" in js
    assert (
        "Processing must be paused before the database can be wiped. "
        "A backup can be created automatically before the wipe begins."
    ) in js
    assert "Backup Database Before Wipe" in js
    assert "Create a backup before wiping the database. The wipe is aborted if the backup fails." in js
    assert "Allow Database Wipe" in js
    assert "Unlock the database wipe action." in js
    assert "row.append(backupToggle, allowToggle, wipeActions);" in js


def test_database_wipe_card_reuses_existing_controls_and_is_idempotent():
    js = source(RUNTIME)

    assert 'input[data-setting="db_backup_before_wipe"]' in js
    assert 'input[data-setting="db_wipe_enabled"]' in js
    assert 'button[data-action="wipe-database"]' in js
    assert "dpWipeControlsPolished" in js
    assert "card.dataset[WIPE_MARKER] = '1'" in js
    assert "MutationObserver" in js
    assert "mutation.type === 'childList'" in js
    assert "attributes: true" not in js


def test_database_wipe_toggle_text_is_stacked_and_controls_stay_adjacent():
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


def test_maintenance_presentation_assets_are_loaded_after_settings_page_with_cache_bump():
    loader = source(LOADER)

    css_entry = "'/ui-settings-maintenance-wipe.css?v=2'"
    js_entry = "'/ui-settings-maintenance-wipe.js?v=2'"
    settings_entry = "'/ui-settings-page.js?v=4'"
    assert css_entry in loader
    assert js_entry in loader
    assert settings_entry in loader
    assert loader.index(js_entry) > loader.index(settings_entry)
