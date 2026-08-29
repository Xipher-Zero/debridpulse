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
    assert "Configure wipe safeguards and perform a database wipe." in js
    assert "Database Wipe is Destructive" in js
    assert (
        "Processing must be paused before the database can be wiped. "
        "A backup can be created automatically before the wipe begins."
    ) in js
    assert "Backup Database Before Wipe" in js
    assert "Create a backup before wiping the database. The wipe is aborted if the backup fails." in js
    assert "Allow Database Wipe" in js
    assert "Unlock the database wipe action." in js

    backup = js.index("row.append(backupToggle")
    allow = js.index("allowToggle", backup)
    wipe = js.index("wipeActions", allow)
    assert backup < allow < wipe


def test_database_wipe_card_reuses_existing_controls_and_is_idempotent():
    js = source(RUNTIME)

    assert 'input[data-setting="db_backup_before_wipe"]' in js
    assert 'input[data-setting="db_wipe_enabled"]' in js
    assert 'button[data-action="wipe-database"]' in js
    assert "dpWipeControlsPolished" in js
    assert "card.dataset[CARD_MARKER] = '1'" in js
    assert "MutationObserver" in js
    assert "mutation.type === 'childList'" in js
    assert "attributes: true" not in js


def test_database_wipe_card_layout_uses_true_center_and_compact_action():
    css = source(STYLE)

    assert ".dp-settings-database-wipe-card > .card-header" in css
    assert "grid-template-columns: minmax(240px, 1fr) minmax(340px, 1.25fr) minmax(240px, 1fr);" in css
    assert ".dp-settings-database-wipe-header-copy" in css
    assert "text-align: center;" in css
    assert "grid-template-columns: minmax(0, 1.35fr) minmax(0, .85fr) auto;" in css
    assert ".dp-settings-database-wipe-row > .dp-settings-toggle" in css
    assert "align-items: center;" in css
    assert ".dp-settings-database-wipe-action .btn" in css
    assert "width: auto;" in css
    assert "@media (max-width: 900px)" in css


def test_database_wipe_presentation_assets_are_loaded_after_settings_page():
    loader = source(LOADER)

    css_entry = "'/ui-settings-maintenance-wipe.css?v=1'"
    js_entry = "'/ui-settings-maintenance-wipe.js?v=1'"
    settings_entry = "'/ui-settings-page.js?v=4'"
    assert css_entry in loader
    assert js_entry in loader
    assert settings_entry in loader
    assert loader.index(js_entry) > loader.index(settings_entry)
