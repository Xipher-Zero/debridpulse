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
    assert "box-shadow:var(--dp-focus-ring)" in compact
    # DP 1.0.13 responsive column flow: the editor is two structural regions --
    # a bounded, vertically scrolling list region and a footer row that is its
    # SIBLING. The footer therefore occupies space the list cannot take, which
    # is what replaced the absolute band plus 68px of reserved bottom padding;
    # neither that reserved constant nor the `overflow: visible` that let the
    # card grow without limit may come back.
    assert "padding:8px11px68px" not in compact
    assert "overflow:visible" not in compact
    editor = css.split("#view-settings .dp-settings-extraction-password-editor {", 1)[1].split("}", 1)[0]
    assert "display: flex;" in editor
    assert "flex-direction: column;" in editor
    assert "overflow: hidden;" in editor
    region = css.split("#view-settings .dp-settings-password-region {", 1)[1].split("}", 1)[0]
    assert "overflow-y: auto;" in region
    assert "overflow-x: hidden;" in region
    footer = css.split("#view-settings .dp-settings-password-footer {", 1)[1].split("}", 1)[0]
    assert "flex: 0 0 auto;" in footer
    assert "position: absolute" not in footer


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


def test_the_password_list_has_exactly_one_layout_owner() -> None:
    """DP 1.0.13 responsive column flow.

    Four decisions -- rows per column, visible column count, overflow mode and
    separator offsets -- are made in ONE place, from measured geometry. The
    stylesheet states material and the two gaps that owner reads back; it
    states no count. Two other stylesheets used to state the editor's geometry
    as well, and disagreed with this one through `!important`; neither may
    describe it again.
    """
    archive_js = read("frontend/static/ui-settings-archive-passwords.js")
    archive_css = read("frontend/static/ui-settings-archive-passwords.css")
    completion = read("frontend/static/ui-settings-downloads-completion.css")
    layout = read("frontend/static/ui-settings-form-layout.css")

    for owned in ("dp-settings-password-region", "dp-settings-password-canvas",
                  "dp-settings-password-rows", "dp-settings-password-footer",
                  "dp-settings-password-separator", "dp-settings-password-line",
                  "dp-settings-extraction-password-editor"):
        assert owned in archive_css, owned
        assert owned not in layout, f"{owned} is stated twice (form-layout)"
    for owned in ("dp-settings-password-region", "dp-settings-password-canvas",
                  "dp-settings-password-rows", "dp-settings-password-footer",
                  "dp-settings-password-separator", "dp-settings-password-line",
                  "dp-settings-extraction-password-editor {"):
        assert owned not in completion, f"{owned} is stated twice (downloads-completion)"

    # The capacity decisions, and only they, live in the JS owner.
    for decision in ("const perColumn=", "const maxColumns=", "const columns=", "const rows="):
        assert decision in archive_js, decision
    assert "gridTemplateRows" in archive_js and "gridTemplateColumns" in archive_js
    # ...and the stylesheet names neither count.
    assert "grid-template-rows: repeat(1, min-content);" in archive_css   # the inert default
    assert "grid-auto-flow: column;" in archive_css                       # vertical-first fill


def test_row_and_column_capacity_are_measured_never_written_down() -> None:
    """No row count, no column count and no breakpoint standing in for one."""
    archive_js = read("frontend/static/ui-settings-archive-passwords.js")

    # Capacity comes from the rendered entry row, the grid's own gaps, the
    # region's own box and the declared minimum useful column width.
    for measured in ("line.getBoundingClientRect().height", "parseFloat(style.rowGap)",
                     "parseFloat(style.columnGap)", "--dp-password-column-min",
                     "host.clientHeight", "grid.clientWidth"):
        assert measured in archive_js, measured
    # The two capacities are floors of real geometry, not constants.
    assert "Math.floor((g.available+g.rowGap)/(g.rowHeight+g.rowGap))" in archive_js
    assert "Math.floor((g.width+g.columnGap)/(g.minColumn+g.columnGap))" in archive_js
    # A fixed per-column row count -- the observed ~15 -- appears nowhere.
    assert "15" not in archive_js.replace("0.15", "")


def test_reflow_is_observed_once_and_separators_are_decorative() -> None:
    archive_js = read("frontend/static/ui-settings-archive-passwords.js")

    # ONE observer for the page's lifetime: re-applying re-points it rather
    # than adding another, so no rerender can accumulate them.
    assert archive_js.count("new ResizeObserver(") == 1
    assert "regionObserver.disconnect();" in archive_js
    assert "regionObserver.observe(host);" in archive_js
    # Coalesced on a frame, never polled.
    assert "requestAnimationFrame(()=>{layoutFrame=0;layout();})" in archive_js
    assert "setInterval" not in archive_js

    # Separators track the visible column count exactly, and leave nothing behind.
    assert "const want=Math.max(0,columns-1);" in archive_js
    assert "rules[i-1].remove();" in archive_js
    assert "rule.setAttribute('aria-hidden','true');" in archive_js


def test_the_footer_is_a_sibling_of_the_scroll_region_not_an_overlay() -> None:
    """Structurally reserved: the hint and the collection controls occupy real
    space the list cannot take, rather than being floated over it."""
    page = read("frontend/static/ui-settings-page.js")
    archive_css = read("frontend/static/ui-settings-archive-passwords.css")

    editor = page.split('class="input dp-settings-extraction-password-editor"', 1)[1].split("</div>\n", 1)[0]
    assert editor.index('dp-settings-password-region') < editor.index('dp-settings-password-footer')
    assert 'dp-settings-password-canvas' in editor
    # The footer is NOT inside the region.
    region_markup = editor.split('dp-settings-password-region', 1)[1].split('dp-settings-password-footer', 1)[0]
    assert 'dp-settings-password-clear' not in region_markup
    assert 'dp-settings-password-eye' not in region_markup
    assert 'dp-settings-password-guidance' not in region_markup

    footer = archive_css.split("#view-settings .dp-settings-password-footer {", 1)[1].split("}", 1)[0]
    assert "flex: 0 0 auto;" in footer
    for overlay in ("position: absolute", "position: fixed", "z-index"):
        assert overlay not in footer, overlay
