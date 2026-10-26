"""Reviewed Settings chrome batch: identity, tabs, provider art, and action glyphs."""

from __future__ import annotations

import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
CHROME = STATIC / "ui-settings-chrome.css"
PAGE = STATIC / "ui-settings-page.css"
RUNTIME = STATIC / "ui-settings-page.js"
FEATURE = STATIC / "ui-feature-icon-contract.css"
ICONS = STATIC / "ui-settings-card-icons.css"
STYLE = STATIC / "style.css"
MANIFEST = STATIC / "icons" / "dp" / "manifest.json"
LUCIDE = STATIC / "icons" / "lucide"
PROVIDERS = STATIC / "icons" / "providers"
PIN = "23f9abc4ed0146cffededd3d7f94c1018bfdf693"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_settings_chrome_is_a_scoped_presentation_layer_after_settings_geometry() -> None:
    overlay = read(STYLE)
    chrome = read(CHROME)

    settings = overlay.index("/ui-settings-page.css?v=3")
    settings_chrome = overlay.index("/ui-settings-chrome.css?v=4")
    help_page = overlay.index("/ui-help-page.css?v=23")
    feature = overlay.index("/ui-feature-icon-contract.css?v=5")
    assert settings < settings_chrome < help_page < feature

    assert "#view-settings" in chrome
    assert "#sidebar" not in chrome
    assert "#main" not in chrome
    assert "#topbar" not in chrome
    assert ".dp-settings-master-card::after" not in chrome
    assert ".dp-settings-card::after" not in chrome
    assert "--dp-panel-frame" not in chrome
    assert "--dp-panel-surface" not in chrome


def test_settings_master_header_uses_supplied_feature_asset_and_true_centered_tabs() -> None:
    chrome = read(CHROME)
    feature = read(FEATURE)
    manifest = json.loads(read(MANIFEST))

    # True-centred tabs while the copy fits beside them: a trailing space that
    # grows exactly as the copy does, taking no room of its own. The header is
    # one row through the desktop; nothing stacks it at a fixed width.
    spacer = chrome[chrome.index("#view-settings .dp-settings-master-header::after {"):]
    spacer = spacer[:spacer.index("}")]
    assert "flex: 1 1 0;" in spacer and "min-width: 0;" in spacer and "margin-left: -16px;" in spacer
    assert ".dp-settings-tabs" in chrome
    assert "@media (max-width: 1699px)" not in chrome
    page = read(CHROME.parent / "ui-settings-page.css")
    header = page[page.index("#view-settings .dp-settings-master-header {"):]
    header = header[:header.index("}")]
    assert "display: flex;" in header and "flex-wrap: wrap;" in header and "gap: 16px;" in header
    copy = page[page.index("#view-settings .dp-settings-header-copy {"):]
    copy = copy[:copy.index("}")]
    assert "flex: 1 1 0;" in copy and "min-width: max-content;" in copy

    # 780px and wider the row never wraps, and the compact navigator keeps the
    # strip's own place: no layout of its own for the copy, the navigation or
    # the trailing space. The navigation's geometry and chrome are the shared
    # section navigator's (ui-section-nav.css, Settings' and Help's).
    desktop = page[page.index("@media (min-width: 780px) {\n  #view-settings .dp-settings-master-header {"):]
    desktop = desktop[:desktop.index("\n}\n")]
    assert "flex-wrap: nowrap;" in desktop
    compact = "#view-settings .dp-settings-master-header[data-presentation='compact']"
    assert f"{compact} .dp-settings-header-copy" not in page
    assert f"{compact} .dp-settings-nav {{" not in page
    assert f"{compact}::after" not in chrome
    # No Settings copy of the navigator's rules remains.
    assert ".dp-settings-nav" not in page and ".dp-settings-nav" not in chrome
    assert "data-nav-cut" not in chrome and "data-nav-at" not in page
    nav_css = read(STATIC / "ui-section-nav.css")
    assert "@import url('/ui-section-nav.css?v=1');" in read(STYLE)
    shared = "[data-presentation='compact'] > .dp-section-nav"
    assert "header-copy" not in nav_css and "::after" not in nav_css and "#view-" not in nav_css
    assert f"{shared} {{" not in nav_css
    window = nav_css[nav_css.index(f"{shared} .dp-section-nav-strip {{"):]
    window = window[:window.index("}")]
    assert "width: calc(var(--dp-section-nav-window) + 2 * var(--dp-section-nav-slot));" in window
    assert "overflow: hidden;" in window and "clip-path: inset(0 var(--dp-section-nav-slot));" in window
    # First and Last stand over the strip's ends (one grid cell), and at
    # their own end are gone, taking no room: the window, clipped by the
    # same committed state, takes their place; the control never resizes.
    assert f"{shared} .dp-section-nav-control > * {{\n    grid-area: 1 / 1;" in nav_css
    assert f"{shared} [data-nav-at='first'] > .dp-section-nav-strip {{\n    clip-path: inset(0 var(--dp-section-nav-slot) 0 0);" in nav_css
    assert f"{shared} [data-nav-at='last'] > .dp-section-nav-strip {{\n    clip-path: inset(0 0 0 var(--dp-section-nav-slot));" in nav_css
    gone = nav_css[nav_css.index(f"{shared} :is([data-nav-at='first'], [data-nav-at='only'])"):].split("}", 1)[0]
    assert "display: none;" in gone and "visibility: hidden" not in nav_css
    assert ":has(" not in nav_css
    nav = nav_css[nav_css.index(".dp-section-nav {"):]
    assert "justify-content: center;" in nav[:nav.index("}")]
    # The window's own element is DPMotion's slide surface, which cannot carry
    # a mask: the outward fade belongs to the section an edge cuts.
    assert "mask-image" not in window
    assert f"{shared} [role='tab'][data-nav-cut='start']" in nav_css and f"{shared} [role='tab'][data-nav-cut='end']" in nav_css
    # A sliver too small to be a position is not shown; the compact tabs give
    # up side padding only by the runtime's measured trim (none by default).
    sliver = nav_css[nav_css.index(f"{shared} [role='tab'][data-nav-cut='sliver'] {{"):].split("}", 1)[0]
    assert "opacity: 0;" in sliver and "pointer-events: none;" in sliver
    trim = chrome[chrome.index(f"{compact} .dp-settings-tabs .stab {{"):].split("}", 1)[0]
    assert "padding-inline: calc(10px - var(--dp-section-nav-trim, 0px));" in trim

    # Complete strip or compact navigator, decided by the shared navigator
    # from the rendered tabs and copy (no assumed tab count, no fixed
    # breakpoint besides the handheld edge); the compact one is a bounded
    # window onto the same tabs with the pager's own First / Last glyphs,
    # opens tabs through chooseTab, and slides only through DPMotion when a
    # choice moves it. Settings keeps no copy of those mechanics.
    runtime = read(RUNTIME)
    mechanics = read(STATIC / "ui-section-nav.js")
    for moved in ("function fitNavigation(", "function navWindow(", "function navLayout(", "function placeNavigation(", "NAV_TRIM", "navFit"):
        assert moved not in runtime, moved
    assert "window.DPSectionNav.create({\n    nav: () => root()?.querySelector('.dp-settings-nav'),\n    copy: '.dp-settings-header-copy',\n  });" in runtime
    fit = mechanics[mechanics.index("function fitNavigation("):mechanics.index("function navWindow(")]
    assert "getBoundingClientRect().width" in fit and "NAV_SPARE" in fit
    assert "navFit.strip + navFit.copy + gap + NAV_SPARE <= width ? 'full' : 'compact'" in fit
    # The copy is measured at every fit (a page may change it with its width).
    assert "navFit.copy = copyWidth(header);" in fit
    assert "const NAV_SPARE = 4;" in mechanics
    assert "const NAV_TRIM = 5;" in mechanics and "navFit.trim = navTrim(navFit.window);" in fit
    assert "nav.style.removeProperty('--dp-section-nav-trim');" in fit
    assert "window.matchMedia('(min-width: 780px)')" in mechanics
    assert "'chevronFirst'" in runtime and "'chevronLast'" in runtime
    assert runtime.count('role="tablist" aria-label="Settings sections"') == 1
    assert "dp-settings-tab-face" not in runtime
    travel = runtime[runtime.index("const travel = event.target.closest('.dp-settings-nav-end');"):]
    assert "chooseTab(target.dataset.tab);" in travel[:travel.index("return;\n      }")]
    assert "addEventListener?.('loadingdone', () => navigation.fit({measure: true}))" in runtime
    assert "new ResizeObserver(() => navigation.fit())" in runtime
    place = mechanics[mechanics.index("function placeNavigation("):]
    place = place[:place.index("\n    }\n")]
    assert "window.DPMotion.slide({element: strip, axis: 'x'" in place and "motion?.direction" in place
    assert "tab.dataset.navCut = part < NAV_TARGET ? 'sliver' : cut;" in place
    # The window slides the way the choice goes (the panel's own direction),
    # compared with where it was last placed, never with a scroll offset a
    # pending slide has yet to draw; the end state is written with it.
    assert "direction: motion.direction," in place and "Math.sign(target - strip.scrollLeft)" not in place
    assert "const placed = navFit.left ?? strip.scrollLeft;" in place and "control.dataset.navAt = at;" in place
    assert "const room = navRoom(navFit.window, index);" in place
    assert "navigation.place(motion);" in runtime
    choose = runtime[runtime.index("function chooseTab("):]
    choose = choose[:choose.index("\n  }\n")]
    assert "selectTabControl(name, {direction: Math.sign(step), distance: Math.abs(step)});" in choose
    assert "order.indexOf(state.activeTab)" in choose and ":not([hidden])" not in choose

    # The explicit query generation prevents a browser-cached synthetic glyph
    # from masking the supplied Settings asset after a staging image update.
    assert "url('/icons/dp/settings-header.svg?v=2')" in chrome
    assert "background-size: 51px 51px;" in chrome
    assert manifest["icons"]["settingsHeader"] == "settings-header.svg"
    gear = read(STATIC / "icons" / "dp" / "settings-header.svg")
    root = ET.fromstring(gear)
    assert root.tag.endswith("svg")
    assert root.attrib.get("viewBox") == "0 0 2048 2048"
    assert gear.count("<path") > 20
    assert "#b053ec" in gear
    assert "#fce6fd" in gear
    assert "#080116" in gear
    assert "<image" not in gear.lower()
    assert "data:image" not in gear.lower()

    assert "#view-settings .dp-settings-header-icon" in feature
    assert "--dp-feature-icon-size: 51px" in feature
    assert "--dp-feature-icon-glow: #a95bff;" in feature
    assert "var(--dp-feature-icon-glow) 62%" in feature
    assert "var(--dp-feature-icon-glow) 70%" in feature

    assert "content: 'To Be Determined';" in chrome


def test_settings_tabs_use_reviewed_lucide_glyphs_with_theme_specific_glyph_glow() -> None:
    chrome = read(CHROME)
    runtime = read(RUNTIME)
    # The locked Settings family colours, each tab glyph stroked in its family's.
    expected = {
        "sources": ("zap.svg", "#D657FF"),
        "downloads": ("download-downloads.svg", "#2563EB"),
        "extraction": ("package-open.svg", "#FF9D00"),
        "notifications": ("bell-ring.svg", "#04D7FE"),
        "authentication": ("shield-user.svg", "#1DDB69"),
        "maintenance": ("database.svg", "#6366F1"),
    }

    assert 'class="dp-settings-tab-chip"' in runtime
    assert 'class="dp-settings-tab-glyph"' in runtime
    assert 'src="/icons/lucide/${html(icon)}.svg"' in runtime
    assert ".dp-settings-tab-chip" in chrome
    assert "width: 22px;" in chrome
    assert "height: 22px;" in chrome
    assert ".dp-settings-tab-glyph" in chrome
    assert "width: 13px;" in chrome
    assert "height: 13px;" in chrome
    assert "gap: 7px;" in chrome

    dark_glyph = chrome.split(".dp-settings-tab-glyph {", 1)[1].split("}", 1)[0]
    light_glyph = chrome.split("body.light #view-settings .dp-settings-tab-glyph {", 1)[1].split("}", 1)[0]
    chip = chrome.split(".dp-settings-tab-chip {", 1)[1].split("}", 1)[0]
    assert "saturate(1.5)" in dark_glyph
    assert "drop-shadow" in dark_glyph
    assert "saturate(2.15)" in light_glyph
    assert light_glyph.count("drop-shadow") == 3
    assert "filter:" not in chip

    for tab, (filename, color) in expected.items():
        assert f".stab[data-tab='{tab}']" in chrome
        assert color in chrome
        assert f"'{Path(filename).stem}'" in runtime

        raw = read(LUCIDE / filename)
        assert f'stroke="{color}"' in raw
        icon_root = ET.fromstring(raw)
        assert icon_root.tag.endswith("svg")
        assert icon_root.attrib.get("viewBox") == "0 0 24 24"
        assert PIN in raw
        assert "<image" not in raw.lower()

    assert ".stab[data-tab='advanced']" not in chrome
    assert "sliders-horizontal" not in runtime


def test_sources_panel_consolidates_primary_key_and_collapsed_additional_settings() -> None:
    runtime = read(RUNTIME)
    page = read(PAGE)
    chrome = read(CHROME)
    key_helper = runtime[runtime.index("function allDebridApiKeyField"):runtime.index("function tuningToggle")]
    sources = runtime[runtime.index("function sourcesPanel"):runtime.index("function downloadsPanel")]

    assert "function groupCard(" in runtime
    assert "groupCard('Premium Services'," in sources
    # One Services order owner (premiumServiceOrder) composes every premium card.
    assert "premiumServiceOrder([" in sources
    for card in ("['usenet', usenetCard]", "['alldebrid', provider]", "['debridlink', debridLinkCard]",
                 "['realdebrid', realDebridCard]", "['torbox', torBoxCard]"):
        assert card in sources, card
    assert "provider + recovery" not in sources
    assert "const recovery =" not in sources
    assert "dp-settings-provider-recovery-card" not in sources
    assert "dp-settings-source-group dp-settings-debrid-services" in sources
    assert "dp-settings-provider-card dp-settings-provider-card--alldebrid" in sources

    # The row still carries its configured state, and the credential still
    # renders blank -- both now through the canonical composer rather than
    # hand-written markup, so there is one control owner and one commit
    # declaration instead of two.
    assert "className: `dp-settings-alldebrid-key-row ${configured ? 'is-configured' : ''}`" in key_helper
    assert "return input(key, 'API Key', ''," in key_helper
    # The configured/unconfigured parts of the row are declared once and used
    # both to render it and to converge it.
    assert "ALLDEBRID_KEY_PLACEHOLDER(configured)" in key_helper
    assert "CONFIGURED_SECRET_MASK" in runtime
    assert "ALLDEBRID_KEY_HINT(configured)" in key_helper
    # The stored-key state moved INSIDE the field's trailing edge, so it is
    # carried by the control rather than by an external status row.
    assert "ALLDEBRID_KEY_PRESENT" in key_helper
    assert "Key present" in runtime
    # DP 1.0.13: the Save-oriented clear checkbox became an explicit Clear
    # action, and the final interaction pass moved the question it asked into
    # the ONE canonical Settings confirmation -- so the row owns the action and
    # nothing else, and no card-local confirmation representation survives.
    assert "Clear stored API Key" not in runtime
    assert 'data-action="clear-alldebrid-key"' in runtime
    assert "Remove API Key" in runtime
    assert "Clear Stored API Key" not in runtime
    assert "Confirm removal of the stored API key" not in runtime
    assert "data-alldebrid-clear-confirm" not in runtime
    # The generic secretField() helper still uses that phrasing for the other
    # secrets; the AllDebrid row has always stated its own.
    assert "configured — blank keeps current value" not in key_helper
    assert "Enter an API key to connect your AllDebrid account." in runtime
    assert "API key configured for your AllDebrid account." in runtime
    assert "Leave this field blank to keep the current key." not in runtime
    assert "api_key: valueOf('alldebrid_api_key')" in runtime

    assert '<details class="dp-settings-additional">' in sources
    assert '<details class="dp-settings-additional" open' not in sources
    assert '<summary><span>Additional Settings</span></summary>' in sources
    # Tuning-only, as compact cells in the reusable neutral tuning grid.
    additional = sources[sources.index('class="dp-settings-additional-body"'):]
    assert "tuningCells(" in additional
    assert "alldebrid_rate_limit_per_minute" in additional

    assert ".dp-settings-alldebrid-key-row.is-configured" in page
    # The row is the shared inline field: a stacked title + hint, the field as
    # the flexible element beside it, and the destructive action last -- three
    # peers on one centre line. The key still consumes every spare pixel before
    # an action sized by its own content; that is now a property of the
    # grammar rather than a template this row restates.
    control_rule = page.split(
        "#view-settings .dp-settings-alldebrid-key-row > .dp-settings-inline-field-control {", 1)[1].split("}", 1)[0]
    assert "flex: 1 1 auto" in control_rule
    action_rule = page.split("#view-settings .dp-settings-inline-field-action {", 1)[1].split("}", 1)[0]
    assert "flex: 0 0 auto;" in action_rule
    assert "align-items: center;" in action_rule
    assert "border-top" not in action_rule
    # The retired confirmation's own rule went with it.
    assert ".dp-settings-alldebrid-key-confirm" not in page
    assert ".dp-settings-key-present" in page
    assert "text-align: right;" in page
    assert ".dp-settings-additional > summary" in page
    # Open turns the chevron; it turns back as the roll-up begins (ui-motion.js
    # keeps the element open until the body has rolled away).
    assert ".dp-settings-additional[open]:not([data-dp-closing]) > summary::after" in page

    disclosure = chrome.split(".dp-settings-additional > summary {", 1)[1].split("}", 1)[0]
    assert "justify-content: flex-start;" in disclosure
    assert "gap: 8px;" in disclosure
    assert "padding-left: 16px;" in disclosure
    chevron = chrome.split(".dp-settings-additional > summary::after {", 1)[1].split("}", 1)[0]
    assert "margin-left: 2px;" in chevron
    additional_body = chrome.split(".dp-settings-additional-body {", 1)[1].split("}", 1)[0]
    assert "padding: 8px 0 4px 28px;" in additional_body

    # Premium Services is the Services family's Crown.
    raw = read(LUCIDE / "crown.svg")
    root = ET.fromstring(raw)
    assert root.tag.endswith("svg")
    assert root.attrib.get("viewBox") == "0 0 24 24"
    assert 'stroke="#D657FF"' in raw and PIN in raw
    assert "<image" not in raw.lower()
    assert "data:image" not in raw.lower()

    # It wears that glyph in the one Settings header chip (familyIcon), not a
    # pseudo-element picture, so it shares the chip's box, inner and outer glow.
    assert "titlePrefix: familyIcon('sources', 'crown')," in sources
    assert ".dp-settings-debrid-services > .card-header > .card-title::before" not in chrome
    glow = read(ICONS)
    owner = glow.split("#view-settings .dp-settings-header-chip {", 1)[1].split("}", 1)[0]
    assert owner.count("drop-shadow") == 2
    light_owner = glow.split("body.light #view-settings .dp-settings-header-chip {", 1)[1].split("}", 1)[0]
    assert light_owner.count("drop-shadow") == 2


def test_alldebrid_card_uses_supplied_provider_art_on_brand_identity_chip_and_larger_logo() -> None:
    chrome = read(CHROME)
    runtime = read(RUNTIME)
    raw = read(PROVIDERS / "alldebrid.svg")
    root = ET.fromstring(raw)

    assert "dp-settings-provider-chip--alldebrid" in runtime
    assert "dp-settings-provider-logo--alldebrid" in runtime
    assert 'src="/icons/providers/alldebrid.svg"' in runtime
    # The locked AllDebrid colour is the chip's one datum.
    assert ("#view-settings .dp-settings-provider-chip--alldebrid { --dp-provider-color: var(--dp-identity-alldebrid); }"
            in chrome)
    assert "--dp-identity-alldebrid: #FFA34E;" in read(STATIC / "design-tokens.css")

    logo = chrome.split(".dp-settings-provider-logo--alldebrid {", 1)[1].split("}", 1)[0]
    assert "width: 34px;" in logo
    assert "height: 34px;" in logo
    assert "transform: translateY(1px);" in logo
    assert "drop-shadow(0 0 3px color-mix(in srgb, var(--dp-provider-color) 92%, transparent))" in logo
    assert "drop-shadow(0 0 7px color-mix(in srgb, var(--dp-provider-color) 52%, transparent))" in logo

    light_logo = chrome.split("body.light #view-settings .dp-settings-provider-logo--alldebrid {", 1)[1].split("}", 1)[0]
    assert "drop-shadow(0 0 8px color-mix(in srgb, var(--dp-provider-color) 62%, transparent))" in light_logo

    assert root.tag.endswith("svg")
    assert root.attrib.get("viewBox") == "0 0 2048 2048"
    assert "<image" not in raw.lower()
    assert "data:image" not in raw.lower()
    assert "rgb(25,25,25)" in raw
    assert "rgb(250,250,249)" in raw


def test_alldebrid_test_action_uses_flaskconical_glyph_glow_and_apply_label() -> None:
    chrome = read(CHROME)
    runtime = read(RUNTIME)
    raw = read(LUCIDE / "flask-conical.svg")

    assert "providerTestAction('test-alldebrid')" in runtime
    # Test is a provider-level action and now lives in the card's operational
    # header rail. Its own treatment is unchanged; only its PLACE moved, so this
    # owner still declares the whole of it -- once, for every provider that has
    # one, which is why the treatment is keyed on the control and not on any
    # provider's action name.
    assert "headerAction: providerTest" in runtime
    assert 'class="btn btn-ghost btn-sm dp-settings-provider-test"' in runtime
    assert 'class="dp-settings-action-chip"' in runtime
    assert 'class="dp-settings-action-glyph" src="/icons/lucide/flask-conical.svg"' in runtime
    action_owner = chrome.split("button.dp-settings-provider-test {", 1)[1].split("}", 1)[0]
    action_glyph = chrome.split(".dp-settings-action-glyph {", 1)[1].split("}", 1)[0]
    light_action = chrome.split("body.light #view-settings .dp-settings-action-glyph {", 1)[1].split("}", 1)[0]
    action_chip = chrome.split(".dp-settings-action-chip {", 1)[1].split("}", 1)[0]
    assert "--dp-settings-action-color: #b866f5;" in action_owner
    assert "saturate(1.5)" in action_glyph
    assert "drop-shadow" in action_glyph
    assert "saturate(2.15)" in light_action
    assert light_action.count("drop-shadow") == 3
    assert "filter:" not in action_chip
    assert "content: url('/icons/lucide/flask-conical.svg?v=2');" in chrome
    # Test is the card's own header action; the retired generic footer control
    # is not what it is distinguished from any more, and it no longer exists.
    assert "Apply Settings" not in runtime
    assert ">Save Settings</button>" not in runtime
    assert PIN in raw
    assert "<path" in raw
    assert 'stroke="#B866F5"' in raw
    assert ".btn-primary" not in chrome


def test_provider_additional_settings_has_no_separator_and_real_debrid_no_spacer_row() -> None:
    """DP 1.0.13 cleanup: island -> Additional Settings, whose own 42px summary
    row is the separation. No divider above the disclosure, no spacer band
    before it, and no blank row under the Real-Debrid Account Connection island
    that only balanced against it."""
    page = read(PAGE)
    additional = page.split("#view-settings .dp-settings-additional {", 1)[1].split("}", 1)[0]
    assert "margin-top: 0;" in additional
    assert "border" not in additional
    connection = page.split("#view-settings .dp-settings-account-connection {", 1)[1].split("}", 1)[0]
    assert "margin: 0;" in connection
    summary = page.split("#view-settings .dp-settings-additional > summary {", 1)[1].split("}", 1)[0]
    assert "min-height: 42px" in summary
    # One shared rule: no provider card restates the disclosure's spacing.
    assert not re.search(r"dp-settings-provider-card--[\w-]+[^{]*\.dp-settings-additional\s*\{", page)
    assert not re.search(r"dp-settings-(alldebrid|debridlink)-key-row\s*\{[^}]*margin-bottom", page)
    assert ".dp-settings-account-island + .dp-settings-provider-status-line" not in page
    runtime = read(RUNTIME)
    assert "PROVIDER_STATUS_SPACER" not in runtime
    assert '<div class="dp-settings-provider-status-line" aria-hidden="true"></div>' not in runtime
    # The real contextual status row stays: one per connection state.
    connection = runtime[runtime.index("function deviceConnectionMarkup(id)"):
                         runtime.index("function renderDeviceConnection(id)")]
    assert connection.count("return providerStatusLine(") == 3
