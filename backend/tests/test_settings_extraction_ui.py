from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_extraction_passwords_keep_backend_secret_boundary() -> None:
    validation = read("backend/api/settings_validation_routes.py")
    routes = read("backend/api/routes.py")
    completion = read("frontend/static/ui-settings-page.js")
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
    for token in ("Escape", "Enter", "clipboardData"):
        assert token in archive
    # DP 1.0.12 UI Finishing (Correction 7): empty Backspace used to delete
    # the row and jump focus to the previous one as a navigation side
    # effect. That special-cased keydown branch was removed entirely (not
    # replaced by a second interceptor), so a bare "Backspace" no longer
    # needs to appear in this file at all -- plain Backspace-on-empty is
    # now a true no-op, exactly as browsers already do for empty inputs.
    assert "Backspace" not in archive
    compact = css.replace(" ", "")
    assert "max-height:none!important" in compact
    assert "overflow:visible!important" in compact
    assert "padding:8px11px50px!important" in compact
    assert "box-shadow:var(--dp-focus-ring)!important" in compact


def test_archive_password_editor_is_the_sole_owner() -> None:
    completion = read("frontend/static/ui-settings-page.js")
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

    # The page renders the whole field -- the hidden form-field textarea, the
    # editor container, the reveal button and the hint. The archive-password
    # owner adds behavior only: it creates no scaffold, hides nothing the page
    # rendered and rewrites none of its copy.
    assert "function archivePasswordField(" in page
    for token in ("dp-settings-extraction-password-editor", "dp-settings-extraction-password-source",
                  "dp-settings-password-rows", "dp-settings-password-eye", 'aria-hidden="true" tabindex="-1"'):
        assert token in page, token
    for token in ("scaffold", "insertAdjacentElement", "hint.textContent", "classList.add('dp-settings-extraction",
                  "cloneNode", "replaceWith"):
        assert token not in archive, token
    assert "dp-settings-password-rows" in archive  # its own row host, filled by the owner

    # DP 1.0.13 Settings consolidation: the field is an ordinary declared
    # changed-blur control of the canonical `settings-document` scope, and the
    # deferred clear-on-save checkbox it used to need is gone entirely -- not
    # hidden, and with no payload semantics left behind.
    assert "extraction_password: {scope: 'settings-document', option: 'extraction_password'" in page
    assert 'data-clear-secret="extraction_password"' not in page
    assert "extractionPasswordValue" not in page
    assert "Erase the stored extraction password list on Save." not in page
    assert "dp-settings-clear-secret\">\n          <span><b>Clear stored archive" not in page
    assert 'data-action="clear-archive-passwords"' in page

    assert ".dp-settings-extraction-password-source" in css
    assert "display: none !important;" in css


def test_archive_passwords_never_commit_before_the_stored_list_is_read() -> None:
    """The hydration gate survived the move to field-boundary persistence.

    An empty editor before hydration means "not read yet", never "no
    passwords", so the composite control's commit boundary is suppressed until
    the authoritative list has been read -- and what the server holds becomes
    the canonical baseline, so an unchanged editor writes nothing.
    """
    archive = read("frontend/static/ui-settings-archive-passwords.js")
    page = read("frontend/static/ui-settings-page.js")
    routes = read("backend/api/routes.py")

    # The editor exposes whether it has read the authoritative stored list.
    assert "get hydrated(){return hydrated;}" in archive
    assert "hydrated=true;" in archive
    assert "function commitSource(){if(refocusing||!hydrated||!sourceNode)return;" in archive
    assert "window.DPSettingsPersistence?.commit(sourceNode)" in archive
    assert "acceptSource(remote)" in archive
    # Persistence itself is not reimplemented here: no write, no endpoint.
    for token in ("PUT", "clear_secrets", "'/settings'"):
        assert token not in archive, token
    # The page declares the control; the canonical owner supplies the machinery.
    assert "extraction_password: {scope: 'settings-document'" in page

    # A supplied non-empty secret overrides a contradictory clear request.
    merge = routes.split("def _merge_secret_settings", 1)[1].split("\n\n", 1)[0]
    assert 'if str(merged.get(field) or "").strip():' in merge
    assert merge.index('if str(merged.get(field) or "").strip():') < merge.index("if field in requested_clears:")
