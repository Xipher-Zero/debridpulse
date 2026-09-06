from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
COMPLETION_JS = STATIC / "ui-settings-downloads-completion.js"
COMPLETION_CSS = STATIC / "ui-settings-downloads-completion.css"
FINAL_JS = STATIC / "ui-correction-batch1-final.js"
SETTINGS_PAGE_JS = STATIC / "ui-settings-page.js"
SETTINGS_VALIDATION = ROOT / "backend" / "api" / "settings_validation_routes.py"
SETTINGS_ROUTES = ROOT / "backend" / "api" / "routes.py"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_extraction_header_uses_reviewed_identity_copy_and_enable_grammar():
    runtime = source(COMPLETION_JS)
    css = source(COMPLETION_CSS)

    assert "Automatic Extraction" in runtime
    assert "/icons/dp/automatic-extraction.svg?v=1" in runtime
    assert "Automatically extract supported archives after a download completes." in runtime
    assert "dp-settings-extraction-header-copy" in runtime
    assert "dp-settings-extraction-enable" in runtime
    assert "label.textContent = 'Enable';" in runtime

    header = css.split(".dp-settings-extraction-card > .card-header {", 1)[1].split("}", 1)[0]
    assert "display: grid;" in header
    assert "minmax(380px, 780px)" in header
    assert ".dp-settings-extraction-header-copy" in css
    assert "text-align: center;" in css

    icon_rule = css.split(".dp-settings-extraction-icon img {", 1)[1].split("}", 1)[0]
    assert "width: 34px;" in icon_rule
    assert "height: 34px;" in icon_rule
    assert icon_rule.count("drop-shadow") == 2
    light_rule = css.split(
        "body.light.dp-v11-structural #view-settings .dp-settings-extraction-icon img {", 1
    )[1].split("}", 1)[0]
    assert light_rule.count("drop-shadow") == 2


def test_extraction_body_uses_reviewed_concurrency_and_delete_copy():
    runtime = source(COMPLETION_JS)
    css = source(COMPLETION_CSS)

    assert "Concurrent Extractions" in runtime
    assert "Maximum number of extraction jobs DebridPulse can run at the same time." in runtime
    assert "Delete Archives After Extraction" in runtime
    assert "Remove original archive files only after extraction completes successfully." in runtime
    assert "dp-settings-extraction-controls-row" in runtime
    assert "dp-settings-extraction-delete" in runtime

    controls = css.split(".dp-settings-extraction-controls-row {", 1)[1].split("}", 1)[0]
    assert "grid-template-columns: minmax(260px, 420px) minmax(0, 1fr);" in controls
    assert "align-items: center;" in controls

    delete = css.split(".dp-settings-extraction-delete {", 1)[1].split("}", 1)[0]
    assert "grid-template-columns: fit-content(720px) auto;" in delete
    assert "align-self: center;" in delete


def test_archive_passwords_use_reviewed_click_reveal_contract_without_secret_regression():
    runtime = source(COMPLETION_JS)
    final = source(FINAL_JS)
    css = source(COMPLETION_CSS)
    settings_page = source(SETTINGS_PAGE_JS)
    validation = source(SETTINGS_VALIDATION)
    routes = source(SETTINGS_ROUTES)

    assert '@router.get("/settings/extraction-passwords")' in validation
    assert 'return {"passwords": str(get_settings().extraction_password or "")}' in validation

    # The normal settings payload remains redacted server-side. Plaintext from
    # the dedicated editor endpoint stays in module-local extractionPasswords
    # state and is never copied into the global settingsData payload.
    assert '"extraction_password",' in routes
    assert 'data[f"{field}_configured"]' in routes
    assert 'data[field] = ""' in routes
    assert "const payload = await api('GET', '/settings/extraction-passwords');" in runtime
    assert "extractionPasswords.values = normalizePasswordLines(payload?.passwords || '');" in runtime
    assert "settingsData.extraction_password =" not in runtime
    assert "settingsData.extraction_password =" not in final

    assert "api('GET', '/settings/extraction-passwords')" in runtime
    assert "Archive Passwords (one per line)" in runtime
    assert "return '*'.repeat(String(value || '').length);" in runtime
    assert "data-password-index" in runtime
    assert "Show all passwords" in final
    assert "Hide all passwords" in final
    assert "Hold to reveal all archive passwords" not in final
    assert "event.key === 'Escape'" in final
    assert "event.key === 'Enter'" in final
    assert "clipboardData" in final
    assert "event.altKey" in final
    assert "max-height: none !important" in final
    assert "overflow: visible !important" in final

    assert "extraction_password: valueOf('extraction_password')" in settings_page
    assert "hiddenClear.dataset.clearSecret = 'extraction_password';" in runtime
    assert "hiddenClear.dataset.dpExtractionClearCompat = '1';" in runtime
    assert "clear.checked = value.length === 0;" in runtime
    assert ':not([data-dp-extraction-clear-compat="1"])' in runtime
    assert ".dp-settings-extraction-password-source" in css
    assert "display: none !important;" in css
    assert ".dp-settings-password-eye" in css


def test_archive_password_clear_action_is_not_a_visible_second_source_of_truth():
    runtime = source(COMPLETION_JS)
    css = source(COMPLETION_CSS)

    assert 'panel.querySelectorAll(\'[data-clear-secret="extraction_password"]:not([data-dp-extraction-clear-compat="1"])\')' in runtime
    assert 'control.closest(\'label\')?.remove()' in runtime
    assert '.dp-settings-clear-secret:has([data-clear-secret="extraction_password"])' in css


def test_sources_and_download_boolean_nits_use_compact_centered_units():
    css = source(COMPLETION_CSS)

    aria2 = css.split(
        ".dp-settings-external-connection-row .dp-settings-clear-secret--aria2 {", 1
    )[1].split("}", 1)[0]
    assert "align-self: center;" in aria2

    partial = css.split(".dp-settings-engine-tuning-toggle-field {", 1)[1].split("}", 1)[0]
    assert "width: fit-content;" in partial
    assert "align-self: center;" in partial
    assert "justify-self: start;" in partial

    alldebrid = css.split(
        '[data-panel="sources"] .dp-settings-clear-secret--alldebrid {', 1
    )[1].split("}", 1)[0]
    assert "width: max-content !important;" in alldebrid
    assert "column-gap: 14px;" in alldebrid
    assert "justify-self: end;" in alldebrid
