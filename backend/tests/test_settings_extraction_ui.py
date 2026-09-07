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
    assert "api('GET', '/settings/extraction-passwords')" in completion
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


def test_completion_runtime_keeps_hidden_source_compatibility() -> None:
    completion = read("frontend/static/ui-settings-downloads-completion.js")
    page = read("frontend/static/ui-settings-page.js")
    css = read("frontend/static/ui-settings-downloads-completion.css")
    assert "Archive Passwords (one per line)" in completion
    assert "data-password-index" in completion
    assert "extraction_password: valueOf('extraction_password')" in page
    assert "hiddenClear.dataset.clearSecret = 'extraction_password';" in completion
    assert "hiddenClear.dataset.dpExtractionClearCompat = '1';" in completion
    assert ".dp-settings-extraction-password-source" in css
    assert "display: none !important;" in css
