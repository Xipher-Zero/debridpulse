from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _block(css: str, selector: str) -> str:
    start = css.index(selector)
    end = css.index("\n}", start)
    return css[start:end]




def test_license_closing_packaging_note_is_centered_and_emphasized():
    css = _read("ui-help-page.css")
    block = _block(css, "#view-help .dp-help-license-note p:last-child {")

    assert "text-align: center" in block
    assert "font-weight: 650" in block
    assert "color: var(--dp-text-primary)" in block




def test_help_section_selector_matches_settings_geometry_and_emphasis():
    css = _read("ui-help-chrome.css")

    # The strip's own geometry is the shared section navigator's
    # (ui-section-nav.css): no Help copy of it, and Help's chrome sets no
    # width on the strip that would hold the compact window open.
    assert "#view-help .dp-help-tabs {" not in css
    nav_css = _read("ui-section-nav.css")
    strip = _block(nav_css, ".dp-section-nav-strip {")
    assert "margin: 0" in strip and "min-width: 0" in strip
    assert "max-width: 100%" in _block(nav_css, ".dp-section-nav-control {")
    # Centred as Settings' strip is: a trailing space that grows as the copy
    # does; the header is one row through the desktop, never stacked at 1699px.
    spacer = _block(css, "#view-help .dp-help-master-header::after {")
    assert "flex: 1 1 0" in spacer and "margin-left: -16px" in spacer
    assert "@media (max-width: 1699px)" not in css

    tab = _block(css, "#view-help .dp-help-tab {")
    assert "appearance: none" in tab
    assert "min-height: 32px" in tab
    assert "flex: 0 0 auto" in tab
    assert "gap: 7px" in tab
    assert "padding-inline: 10px" in tab
    assert "border: 0" in tab
    assert "font: inherit" in tab

    chip = _block(css, "#view-help .dp-help-tab-chip {")
    glyph = _block(css, "#view-help .dp-help-tab-glyph {")
    assert "width: 22px" in chip
    assert "height: 22px" in chip
    assert "border-radius: 6px" in chip
    assert "width: 13px" in glyph
    assert "height: 13px" in glyph
    assert "saturate(1.5)" in glyph
    assert "contrast(1.05)" in glyph

    # Where the compact navigator cannot otherwise hold an end section and its
    # neighbour whole, the tabs give up the runtime's measured trim, as Settings'.
    trim = _block(css, "  #view-help .dp-help-master-header[data-presentation='compact'] .dp-help-tab {")
    assert "padding-inline: calc(10px - var(--dp-section-nav-trim, 0px))" in trim


def test_help_header_is_one_row_through_the_shared_section_navigator():
    page = _read("ui-help-page.css")
    runtime = _read("ui-help-page.js")
    # 780px and wider: one row; the handheld header (779px and below) keeps
    # its wrapping, full-width scrolling strip beneath the copy.
    assert "@media (min-width: 780px) {\n  #view-help .dp-help-master-header {\n    flex-wrap: nowrap;\n  }\n}" in page
    handheld = page[page.index("@media (max-width: 779px) {"):]
    handheld = handheld[:handheld.index("\n}\n")]
    # 1.0.14: the handheld header is one row too -- the identity gives way (ui-section-nav.js levels) and
    # the same compact navigator stays -- no longer the complete strip on a line of its own.
    assert "min-height: 0;" in handheld and "flex-wrap: nowrap;" in handheld and "align-items: stretch;" not in handheld
    assert "#view-help :is(.dp-help-nav, .dp-help-nav-control, .dp-help-tabs) {\n    width: 100%;" not in handheld
    assert "#view-help .dp-help-master-header[data-dp-identity='none'] .dp-help-header-copy," in handheld
    tablet = page[page.index("@media (max-width: 900px) {"):]
    tablet = tablet[:tablet.index("\n}\n")]
    assert ".dp-help-master-header" not in tablet and ".dp-help-tabs" not in tablet
    # The one navigation mechanism: Help creates the shared navigator for its
    # own header and keeps no copy of its fit, window or placement.
    assert "window.DPSectionNav.create({\n    nav: () => root()?.querySelector('.dp-help-nav'),\n    copy: '.dp-help-header-copy',\n    identity: {title: '.dp-help-header-title', hint: '.dp-help-header-subtitle', levels: [" in runtime
    # The approved handheld identity levels, longest first (the last, none, is the owner's).
    for level in ("{title: 'Field Manual', hint: 'When intuition fails.'}", "{title: 'Field Manual', hint: 'When stuck.'}",
                  "{title: 'Manual', hint: 'When stuck.'}", "{title: 'Help', hint: ''}", "{title: 'Help', hint: '', artwork: false}"):
        assert level in runtime, level
    for absent in ("function fitNavigation(", "function navLayout(", "navFit", "NAV_TRIM", "scrollLeft"):
        assert absent not in runtime, absent
    assert runtime.count('role="tablist" aria-label="Help sections"') == 1
    assert 'class="dp-tabs dp-help-tabs dp-section-nav-strip" role="tablist"' in runtime
    assert "'chevronFirst'" in runtime and "'chevronLast'" in runtime
    assert "aria-label=\"${end === 'first' ? 'First' : 'Last'} section: ${TABS.at(index)[1]}\"" in runtime
    assert "new ResizeObserver(() => navigation.fit()).observe(view);" in runtime
    assert "addEventListener?.('loadingdone', () => navigation.fit({measure: true}))" in runtime
    # The window moves with the choice's own direction, from the section
    # chosen last, never from the panel still on screen.
    choose = runtime[runtime.index("function chooseTab("):]
    choose = choose[:choose.index("\n  }\n")]
    assert "selectTabControl(name, focus, {direction: Math.sign(step), distance: Math.abs(step), via});" in choose
    assert "order.indexOf(state.activeTab)" in choose and ":not([hidden])" not in choose
    select = runtime[runtime.index("function selectTabControl("):]
    select = select[:select.index("\n  }\n")]
    assert "navigation.place(motion);" in select
    assert "selected?.focus({preventScroll: navigation.placing()});" in select
    # First / Last reach the ends through the same choice.
    travel = runtime[runtime.index("const travel = event.target.closest('.dp-help-nav-end');"):]
    assert "chooseTab(target.dataset.tab, document.activeElement === travel, 'end');" in travel[:travel.index("});")]
    # Every entry still opens Quick Start.
    assert "function load() {\n    state.activeTab = 'quickstart';\n    render();\n  }" in runtime
