from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_extraction_passwords_keep_backend_secret_boundary() -> None:
    validation = read("backend/api/settings_validation_routes.py")
    routes = read("backend/api/routes.py")
    completion = read("frontend/static/ui-settings-downloads-completion.js")
    archive = read("frontend/static/ui-settings-archive-passwords.js")
    assert '@router.get("/settings/extraction-passwords")' in validation
    assert 'return {"passwords": str(get_settings().extraction_password or "")}' in validation
    assert '"extraction_password",' in routes
    assert 'data[f"{field}_configured"]' in routes
    assert 'data[field] = ""' in routes
    assert "api('GET','/settings/extraction-passwords')" in archive
    assert "extraction-passwords" not in completion
    assert "settingsData.extraction_password =" not in completion
    assert "settingsData.extraction_password =" not in archive


def test_archive_password_editor_uses_click_reveal_and_line_editing() -> None:
    archive = read("frontend/static/ui-settings-archive-passwords.js")
    css = read("frontend/static/ui-settings-archive-passwords.css")
    assert "Show all passwords" in archive
    assert "Hide all passwords" in archive
    assert "Hold to reveal all archive passwords" not in archive
    assert "dp-settings-password-eye--ghost" in archive
    assert "window.DPArchivePasswords" in archive
    for token in ("Escape", "Enter", "Backspace", "clipboardData"):
        assert token in archive
    compact = css.replace(" ", "")
    assert "max-height:none!important" in compact
    assert "overflow:visible!important" in compact
    assert "padding:8px11px50px!important" in compact
    assert "box-shadow:var(--dp-focus-ring)!important" in compact


def test_archive_password_editor_is_the_sole_owner() -> None:
    completion = read("frontend/static/ui-settings-downloads-completion.js")
    archive = read("frontend/static/ui-settings-archive-passwords.js")
    page = read("frontend/static/ui-settings-page.js")
    app = read("frontend/static/app.js")
    css = read("frontend/static/ui-settings-downloads-completion.css")

    # The completion runtime and app.js must not carry a competing archive
    # password editor: exactly one owner writes the extraction_password field.
    for token in (
        "extractionPasswords",
        "renderPasswordRows",
        "buildPasswordEditor",
        "loadExtractionPasswords",
        "syncExtractionPasswordSource",
        "dp-extraction-clear-compat",
        "data-password-index",
    ):
        assert token not in completion
    for token in ("_extractionPasswords", "initExtractionPasswordList", "s-extraction_password"):
        assert token not in app

    # The sole owner builds its own editor scaffold and hides the raw textarea.
    assert "dp-settings-extraction-password-editor" in archive
    assert "dp-settings-extraction-password-source" in archive
    assert "dp-settings-password-rows" in archive
    assert "insertAdjacentElement('afterend'" in archive

    # The serializer reads the field through the hydration gate and the visible
    # clear checkbox is the only clear-secret control for archive passwords.
    assert "extraction_password: extractionPasswordValue()" in page
    assert 'data-clear-secret="extraction_password"' in page

    assert ".dp-settings-extraction-password-source" in css
    assert "display: none !important;" in css


def test_archive_password_serializer_gates_on_editor_hydration() -> None:
    archive = read("frontend/static/ui-settings-archive-passwords.js")
    page = read("frontend/static/ui-settings-page.js")
    routes = read("backend/api/routes.py")

    # The editor exposes whether it has read the authoritative stored list.
    assert "get hydrated(){return hydrated;}" in archive
    assert "hydrated=true;" in archive

    # The serializer will not submit extraction_password (value or clear) while
    # the editor is mounted but has not hydrated.
    assert "window.DPArchivePasswords.hydrated" in page
    assert "if (archivePasswordEditorMounted() && !archivePasswordsHydrated()) return '';" in page
    assert "return checked.filter(name => name !== 'extraction_password');" in page

    # A supplied non-empty secret overrides a contradictory clear request.
    merge = routes.split("def _merge_secret_settings", 1)[1].split("\n\n", 1)[0]
    assert 'if str(merged.get(field) or "").strip():' in merge
    assert merge.index('if str(merged.get(field) or "").strip():') < merge.index("if field in requested_clears:")
