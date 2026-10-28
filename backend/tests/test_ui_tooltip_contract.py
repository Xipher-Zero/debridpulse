"""Informational tooltips: one application owner, never the browser's.

Every application-owned informational tooltip is declared as ``data-tooltip``
by the owner that renders its element and presented by ``ui-tooltip.js``
(``DPTooltip``) in DP's ``.dp-tooltip`` material. The browser proof is
``frontend/browser/ui-tooltip.spec.js``.
"""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"

# Document titles, not tooltips: the shell's own, the OIDC completion page's, and
# the OIDC test popup's (written by ui-settings-page.js).
DOCUMENT_TITLES = {
    ("index.html", "<title>DebridPulse</title>"),
    ("oidc-verify-complete.html", "<title>OIDC verification · DebridPulse</title>"),
    ("ui-settings-page.js", "<title>Testing OIDC · DebridPulse</title>"),
}
NATIVE = (
    re.compile(r"""(?<![\w-])title=(?=["'$\\])"""),          # a title attribute in markup or a template
    re.compile(r"""(?<!document)\.title\s*=(?!=)"""),          # element.title = ...
    re.compile(r"""(?:set|remove)Attribute\(\s*['"]title"""),
    re.compile(r"""<title>[^<]*</title>"""),                    # an SVG (or document) title
)


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_no_owner_writes_a_native_title_tooltip() -> None:
    found = set()
    for path in sorted(list(STATIC.glob("*.js")) + list(STATIC.glob("*.html"))):
        text = path.read_text(encoding="utf-8")
        for pattern in NATIVE:
            for match in pattern.finditer(text):
                found.add((path.name, match.group(0)))
    leftovers = {item for item in found if item not in DOCUMENT_TITLES}
    assert leftovers == set(), sorted(leftovers)
    assert {item for item in found if item[1].startswith("<title>")} == DOCUMENT_TITLES


def test_the_one_tooltip_owner_presents_text_only_and_keeps_to_presentation() -> None:
    owner = read("ui-tooltip.js")
    assert "window.DPTooltip = Object.freeze({hide});" in owner
    assert "const SELECTOR = '[data-tooltip]';" in owner
    assert "tip.textContent = text;" in owner and "innerHTML" not in owner and "insertAdjacentHTML" not in owner
    assert "tip.setAttribute('role', 'tooltip');" in owner and "tip.popover = 'manual';" in owner
    assert "const DELAY_MS = 450;" in owner
    # No polling, no loader, no rewriting of what other owners rendered.
    for absent in ("setInterval", "requestAnimationFrame(", "fetch(", "api(", "createElement('script')",
                   "getAttribute('title')", "removeAttribute('title')", "localStorage", "sessionStorage"):
        assert absent not in owner, absent
    # The described element is touched only for aria-describedby while shown, and given it back.
    assert owner.count("setAttribute('aria-describedby'") == 2 and owner.count("removeAttribute('aria-describedby')") == 1
    # Touch gets no hover tooltip; keyboard focus shows it; Escape is not swallowed.
    assert "if (event.pointerType === 'touch') return;" in owner
    # (The focused element itself: a section index is also shown for focus
    # within the element it describes.)
    assert "keyboard = event.target.matches(':focus-visible');" in owner
    assert "if (!host || (host !== event.target && !host.hasAttribute('data-tooltip-index'))) return;" in owner
    # A section index is built of text nodes from the tablist's own tabs and
    # selection, never markup, and the element's own scrolling leaves it be.
    index = owner[owner.index("function renderIndex(host) {"):owner.index("function fill(host, text) {")]
    assert "host.querySelector(host.getAttribute('data-tooltip-index'))" in index
    assert "tab.getAttribute('aria-selected') === 'true'" in index and "part.textContent = text;" in index
    assert "tip.replaceChildren(head, all);" in index
    assert "(event.target instanceof Element && target.contains(event.target))) return;" in owner
    assert "stopPropagation" not in owner and "preventDefault" not in owner
    # Hidden on every lifecycle edge, and while shown its element leaving the document hides it.
    for edge in ("document.addEventListener('pointerdown', dismiss, true);", "document.addEventListener('contextmenu', dismiss, true);",
                 "window.addEventListener('resize', hide);", "document.addEventListener('debridpulse:navigation', hide);",
                 "if (!target.isConnected || !now) return hide();"):
        assert edge in owner, edge
    # The Settings commit boundary (the bubbling focus-out) stays the persistence owner's alone.
    assert "focusout" not in owner and "document.addEventListener('blur', event => {" in owner


def test_it_loads_once_as_a_direct_script_and_is_styled_by_the_shared_material() -> None:
    index = read("index.html")
    assert index.count('<script src="/ui-tooltip.js?v=1" defer></script>') == 1
    assert index.index("/ui-motion.js?v=1") < index.index("/ui-tooltip.js?v=1") < index.index("/ui-pager.js?v=1")
    css = read("ui-components.css")
    block = css[css.index(".dp-tooltip {"):css.index(".dp-tooltip[hidden]")]
    for token in ("var(--dp-surface-overlay)", "var(--dp-border-default)", "var(--dp-shadow-control)",
                  "var(--dp-radius-xs)", "var(--dp-text-primary)", "var(--dp-font-sans)", "overflow-wrap: anywhere;",
                  "pointer-events: none;", "max-height: min(40vh, 240px);"):
        assert token in block, token
    assert "!important" not in block and "::after" not in css[css.index("/* ── Tooltip"):css.index("/* ── Context menu")]
    assert ".dp-tooltip.is-scrollable { overflow: auto; overscroll-behavior: contain; pointer-events: auto; }" in css
    reduced = css[css.index("@media (prefers-reduced-motion: reduce)", css.index(".dp-tooltip {")):]
    assert ".dp-tooltip { transition: none; }" in reduced[:reduced.index("}\n}") + 3]


def test_original_resource_is_one_more_described_value_not_a_tooltip_of_its_own() -> None:
    app = read("app.js")
    start = app.index("function dpRenderOriginalResource(modalBody, t) {")
    render = app[start:app.index("\n}\n", start)]
    assert "value.dataset.tooltip = full;" in render and "value.tabIndex = 0;" in render
    for retired in ("dp-detail-resource-full", "role', 'tooltip'", "is-dismissed", "pointerenter"):
        assert retired not in render, retired
    assert "dp-detail-resource-full" not in read("ui-transfer-contract.css")


def test_the_profile_button_is_named_from_its_first_render() -> None:
    auth = read("auth.js")
    # Its name is its own from the markup that creates it -- never the tooltip, never hydration --
    # and the session then names it fully (``showAccount``), exactly as before.
    assert '<button type="button" class="dp-sidebar-profile" aria-label="My Account" data-tooltip="My Account">' in auth
    assert "profile.setAttribute('aria-label', display ? `My account: ${display}, ${roleLabel(data.role)}`" in auth


def test_one_observer_at_most_and_a_reachable_scrollable_tooltip() -> None:
    owner = read("ui-tooltip.js")
    show = owner[owner.index("function show(host) {"):owner.index("function release() {")]
    assert owner.count("new MutationObserver(") == 1
    assert show.index("unwatch();") < show.index("watch = new MutationObserver(")
    hide = owner[owner.index("function hide() {"):owner.index("function cancel() {")]
    assert "unwatch();" in hide and "bridging = false;" in hide
    # The gap to a scrollable tooltip may be crossed; anywhere else outside both dismisses it.
    assert "if (target === host && crossing(event)) { bridging = true; return; }" in owner
    assert "if (bridging && !crossing(event)) hide();" in owner
    assert "if (!target || !tip?.classList.contains('is-scrollable')) return false;" in owner


def test_a_dwell_element_waits_for_a_resting_pointer_and_never_takes_the_warm_path() -> None:
    """The compact navigators' section index (and only an element that asks
    for it): shown after the pointer has rested over it, never through the
    hover delay or the warm path; ordinary tooltips keep both."""
    owner = read("ui-tooltip.js")
    assert "const DELAY_MS = 450;" in owner and "const WARM_MS = 300;" in owner   # ordinary timing unchanged
    assert "const JITTER = 4;" in owner
    assert "const dwellOf = host => Number(host?.getAttribute('data-tooltip-dwell')) || 0;" in owner
    over = owner[owner.index("document.addEventListener('pointerover'"):owner.index("document.addEventListener('pointerout'")]
    # A dwell element returns before hover(): no delay timer, no warm path.
    assert over.index("if (host && dwellOf(host)) {") < over.index("if (host) hover(host);")
    assert "return;\n    }\n    if (host) hover(host);" in over
    # Moving off the rest restarts it; a use settles before it restarts;
    # leaving ends it; the owner's own busy marker is waited for.
    assert "Math.hypot(event.clientX - dwell.x, event.clientY - dwell.y) > JITTER" in owner
    assert "settle(host, event.clientX, event.clientY);" in owner
    assert "if (dwell?.host === host) stopDwell();" in owner
    assert "host.hasAttribute('data-tooltip-busy')" in owner and "document.addEventListener('dp-tooltip-ready'" in owner
    nav = read("ui-section-nav.js")
    assert "const INDEX_DWELL_MS = 1000;" in nav and "control.dataset.tooltipDwell = String(INDEX_DWELL_MS);" in nav
    assert "control.dataset.tooltipBusy = '';" in nav and "new Event('dp-tooltip-ready', {bubbles: true})" in nav
    # The mark belongs to the slide that set it, not to the fit a re-measure
    # replaces: its own completion clears it unless a newer slide holds it.
    assert "const mine = busy = {slide, control};" in nav and "if (busy !== mine) return;" in nav
    assert "navFit?.slide !== slide" not in nav
