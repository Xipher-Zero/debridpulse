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
    assert "window.DPMotion = Object.freeze({slide, lead, disclose, enter, leave, restore, focusOrigin, reduced, TIMING});" in motion
    # The slide is scoped to the surface, so the rest of the page stays live.
    assert "element.startViewTransition(" in motion and "document.startViewTransition" not in motion
    # Reduced motion, or no scoped view transitions: the commit, at once.
    assert "if (!element || !direction || reduced() || typeof element.startViewTransition !== 'function') {" in motion
    assert "transition.ready.catch(() => {});" in motion          # a superseded slide is no error
    css = read("ui-motion.css")
    assert "!important" not in css and "transition: all" not in css
    # Farther navigation travels longer and farther: 1, 2-3, 4+ positions.
    for token in ("--dp-motion-adjacent: 280ms;", "--dp-motion-short: 380ms;", "--dp-motion-long: 450ms;",
                  "--dp-motion-expand: 250ms;", "--dp-motion-collapse: 200ms;"):
        assert token in css, token
    assert "TIMING = Object.freeze({adjacent: 280, short: 380, long: 450, expand: 250, collapse: 200," in motion
    assert "const reach = distance => (distance >= 4 ? 'long' : distance >= 2 ? 'short' : 'adjacent');" in motion
    travel = re.findall(r'data-dp-motion-step="(\w+)"\] \{[^}]*--dp-motion-travel: (\d+)%;[^}]*--dp-motion-fade: ([\d.]+);', css)
    assert travel == [('adjacent', '100', '1'), ('short', '120', '0.9'), ('long', '140', '0.85')]
    assert "jump" not in motion and "jump" not in css
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


def test_the_dependent_local_connection_cell_fades_only_itself() -> None:
    page = read("ui-settings-page.js")
    css = read("ui-settings-page.css")
    # Only a cell that states ``disabled`` (the one dependent tunable) is marked.
    assert "const dependent = 'disabled' in options ? ' dp-settings-dependent-field' : '';" in page
    assert page.count("{disabled: ") == 1
    assert "{disabled: !policy.private_lan_connections}" in page
    rule = css[css.index("#view-settings .dp-settings-dependent-field {"):]
    assert rule[:rule.index("}")].strip().endswith("transition: opacity 180ms cubic-bezier(0.4, 0, 0.2, 1);")
    assert "#view-settings .dp-settings-field.is-disabled {\n  opacity: .45;\n}" in css     # the endpoint is unchanged
    reduced = css[css.index("@media (prefers-reduced-motion: reduce) {\n  #view-settings .dp-settings-dependent-field"):]
    assert "transition: none;" in reduced[:reduced.index("}\n}")]
    # The functional state and the class change together, from accepted policy.
    projection = page[page.index("function projectPrivateLanDependency()"):]
    projection = projection[:projection.index("\n  }\n")]
    assert "control.disabled = !allowed;" in projection and "classList.toggle('is-disabled', !allowed)" in projection


def test_a_page_turn_answers_at_once_and_the_slide_continues_from_it() -> None:
    motion = read("ui-motion.js")
    assert "window.DPMotion = Object.freeze({slide, lead, disclose, enter, leave, restore, focusOrigin, reduced, TIMING});" in motion
    assert "lead: 140, modalEnter: 200, modalExit: 160" in motion and "const LEAD_SHARE = 0.08;" in motion
    # The lead moves the surface's own content and is dropped as the new page is committed.
    lead = motion[motion.index("function lead({element"):motion.index("function enter(")]
    assert "Array.from(element.children).map(child =>" in lead and "fill: 'forwards'" in lead
    slide = motion[motion.index("function slide({element"):motion.index("const NO_LEAD")]
    assert slide.index("if (lead) lead.cancel();\n      // A newer slide") < slide.index("commit();\n    });")
    downloads = read("ui-downloads.js")
    assert "torrentPageLead = window.DPMotion.lead({element: document.querySelector('#view-torrents .dp-downloads-table-wrap')," in downloads
    assert "distance: Math.abs(turn), lead}" in downloads and "dropTorrentPageLead('revert');" in downloads
    activity = read("ui-activity-log-runtime.js")
    assert "lead=window.DPMotion.lead({element:document.getElementById('event-list'),axis:'x',direction:way});" in activity
    assert "distance:Math.abs(to-from),lead:held}" in activity and "dropLead('revert');" in activity


def test_every_fullscreen_modal_fades_in_place_through_its_own_owner() -> None:
    css = read("ui-modal-contract.css")
    fades = css[css.index("/* ── Fullscreen modal entry and exit"):]
    for selector in ("#overlay.open", ".dp-modal-overlay", ".dp-backdrop"):
        assert selector in fades, selector
    assert "@starting-style" in fades and "transition: opacity var(--dp-modal-enter) ease-out;" in fades
    assert "transition: opacity var(--dp-modal-exit) ease-in;" in fades
    assert "transform" not in fades and "scale" not in fades and "!important" not in fades   # in place: no movement
    assert "#modal[data-dp-modal-pending] {\n  opacity: 0;" in fades
    reduced = fades[fades.index("@media (prefers-reduced-motion: reduce)"):]
    assert "transition: none;" in reduced
    motion_css = read("ui-motion.css")
    assert "--dp-modal-enter: 200ms;" in motion_css and "--dp-modal-exit: 160ms;" in motion_css
    # Each owner keeps the whole lifecycle -- mount, page lock, key handling --
    # through the exit, and releases it, with focus, once it has gone.
    settings = read("ui-settings-modal.js")
    settle = settings[settings.index("function settle(accepted) {"):settings.index("resolveClosed(Object.freeze(")]
    assert "window.DPMotion.leave(overlay, dialog, resolveFocusTarget(origin))" in settle
    done = settle[settle.index("leaving.then(() => {"):]
    for release in ("mounted.splice(", "window.DPMotion.restore(overlay, target)", "document.body.classList.remove(BODY_OPEN_CLASS)",
                    "document.removeEventListener('keydown', onKeydown)"):
        assert release in done, release
    assert "if (!entry || entry.leaving || event.defaultPrevented) return;" in settings
    assert "window.DPMotion.enter(overlay, `${OVERLAY_SELECTOR}:not(.dp-auth-required-overlay)`);" in settings
    auth = read("ui-auth-required.js")
    finish = auth[auth.index("function finishActive() {"):auth.index("function newActive(")]
    assert "window.DPMotion.leave(overlay, overlay.querySelector('.dp-modal-dialog'), previous)" in finish
    done = finish[finish.index("leaving.then(() => {"):finish.index("presentNext();")]
    for release in ("window.DPMotion.restore(overlay, previous)", "document.body.classList.remove('dp-modal-open')",
                    "document.removeEventListener('keydown', handleDocumentKeydown, true)"):
        assert release in done, release
    assert finish.count("removeEventListener") == 1 and finish.count("classList.remove('dp-modal-open')") == 1
    assert "window.DPMotion.enter(overlay, '.dp-auth-required-overlay');" in auth
    legal = read("ui-help-license-documents.js")
    close = legal[legal.index("function closeModal(options) {"):legal.index("function trapDialogKeydown(")]
    assert "window.DPMotion.leave(backdrop, backdrop.querySelector('.dp-dialog'), target)" in close
    done = close[close.index("leaving.then(function () {"):]
    assert "window.DPMotion.restore(backdrop, target)" in done and "classList.remove('dp-help-legal-modal-open')" in done
    assert close.count("classList.remove('dp-help-legal-modal-open')") == 1
    app = read("app.js")
    close = app[app.index("function finishClose(reason) {"):app.index("// The pending shell's content has arrived")]
    assert "window.DPMotion.leave(overlay, modal, returnTarget)" in close and "if (exit !== exits) return;" in close
    done = close[close.index("leaving.then(() => {"):close.index("if (closedMode === 'details')")]
    assert "document.removeEventListener('keydown', containTab);" in done
    assert "window.DPMotion.restore(overlay, returnTarget, {remove: false})" in done
    assert close.count("removeEventListener") == 1 and close.count(".focus(") == 1          # only in the no-motion fallback
    # A dialog opened while another leaves returns where that one would have.
    for owner in (settings, auth, app):
        assert "window.DPMotion.focusOrigin()" in owner
    # Details: the shell appears with its content (or, slow, held at full height), one opening at a time.
    detail = app[app.index("const DETAIL_LOADING_GRACE_MS = 300;"):app.index("// Requested By:")]
    assert "DPModal.open({mode: 'details', title: 'Loading…', closeLabel: 'Close details', pending: true});" in detail
    assert "DPModal.reveal({hold: true})" in detail and "if (!current()) return;" in detail
    assert "modal.style.height = getComputedStyle(modal).maxHeight;" in app
