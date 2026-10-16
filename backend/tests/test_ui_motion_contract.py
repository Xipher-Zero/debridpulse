"""Navigation and disclosure motion: one presentation primitive, used only by
the operator's own navigation, never owning what is shown.

``ui-motion.js`` moves what each surface's canonical owner commits -- a page,
a tab panel, the main view, a disclosure body -- and decides nothing itself.
These static checks keep it that way; the browser proof of the motion is
``frontend/browser/ui-motion-navigation.spec.js``.
"""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_the_motion_primitive_presents_only() -> None:
    motion = read("ui-motion.js")
    # It fetches nothing, renders nothing, keeps no page/tab/view state and
    # opens no stream: it runs the owner's commit, or animates a body's height.
    for absent in ("api(", "fetch(", "EventSource", "innerHTML", "setInterval", "localStorage", "sessionStorage"):
        assert absent not in motion, absent
    assert "window.DPMotion = Object.freeze({slide, disclose, reduced, TIMING});" in motion
    # The slide is scoped to the surface, so the rest of the page stays live.
    assert "element.startViewTransition(" in motion and "document.startViewTransition" not in motion
    # Reduced motion, or no scoped view transitions: the commit, at once.
    assert "if (!element || !direction || reduced() || typeof element.startViewTransition !== 'function') {" in motion
    assert "transition.ready.catch(() => {});" in motion          # a superseded slide is no error
    css = read("ui-motion.css")
    assert "!important" not in css and "transition: all" not in css
    for token in ("--dp-motion-adjacent: 220ms;", "--dp-motion-jump: 170ms;",
                  "--dp-motion-expand: 250ms;", "--dp-motion-collapse: 200ms;"):
        assert token in css, token
    assert "TIMING = Object.freeze({adjacent: 220, jump: 170, expand: 250, collapse: 200})" in motion
    assert "@import url('/ui-motion.css?v=1');" in read("style.css")
    index = read("index.html")
    assert index.index('/ui-motion.js?v=1') < index.index('/ui-pager.js?v=1')


def test_only_the_operators_own_navigation_moves_anything() -> None:
    app = read("app.js")
    index = read("index.html")
    # Every sidebar entry hands its event over; nav() slides only for a click.
    assert index.count('onclick="nav(this, event)"') == index.count('class="nav-item')
    assert "onclick=\"nav(this)\"" not in index
    nav = app[app.index("function nav(el, event) {"):app.index("function navigateTo(el) {")]
    assert "const chosen = event?.type === 'click' && from >= 0 && to >= 0 && !sameView;" in nav
    assert "direction: chosen ? Math.sign(to - from) : 0" in nav
    assert "debridpulse:navigation-settled" in nav
    # One slide call per owner, each on its operator path only.
    downloads = read("ui-downloads.js")
    assert downloads.count("window.DPMotion.slide(") == 1
    assert downloads.count("{turn: true}") == 4                    # the pager's four actions, nothing else
    assert "goToTorrentPage(1);" in app                            # sidebar entry: no page turn
    activity = read("ui-activity-log-runtime.js")
    assert activity.count("window.DPMotion.slide(") == 1
    assert "['first','previous','next','last'].includes(request.kind)" in activity
    for owner in ("ui-settings-page.js", "ui-help-page.js"):
        source = read(owner)
        assert source.count("window.DPMotion.slide(") == 1, owner
        choose = source[source.index("function chooseTab("):]
        choose = choose[:choose.index("\n  }\n")]
        assert "selectTabControl(name" in choose and "showTabPanel" in choose, owner
        # Render and restore activate in place.
        assert "activateTab(state.activeTab);" in source, owner


def test_every_settings_disclosure_mechanism_rolls_and_only_the_operators_does() -> None:
    page = read("ui-settings-page.js")
    usenet = read("ui-settings-usenet-servers.js")
    # 1. The canonical chip: the operator's click and the accepted-enable auto
    #    open roll; a reconciliation (renderIntegrationState) does not.
    disclosure = page[page.index("function setDisclosureExpanded("):page.index("function renderIntegrationState(")]
    assert "window.DPMotion.disclose(body, !!expanded, {animate, focusReturn: button})" in disclosure
    assert "setDisclosureExpanded(disclosure, disclosure.getAttribute('aria-expanded') !== 'true', {animate: true});" in page
    assert "setDisclosureExpanded(card.querySelector('.dp-settings-disclosure'), true, {animate: true});" in page
    assert "if (!enabled && !dirty) setDisclosureExpanded(disclosure, false);" in page
    # 2. A Usenet server's Advanced panel, in its own owner.
    assert "window.DPMotion.disclose(body, !expanded, {animate: true, focusReturn: button})" in usenet
    # 3. Additional Settings (<details>): open stays the one state.
    assert "function toggleAdditional(details, summary) {" in page
    assert "event.target.closest('.dp-settings-additional > summary')" in page
    css = read("ui-settings-page.css")
    assert ".dp-settings-additional[open]:not([data-dp-closing]) > summary::after" in css
    # No other expand/collapse surface in Settings has an owner of its own.
    expanders = set(re.findall(r'aria-expanded="[^"]*"', page + usenet))
    assert expanders <= {'aria-expanded="${expanded}"', 'aria-expanded="false"'}
