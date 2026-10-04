"""Debrid-Link presentation: the one premium renderer with no lifetime state, the unchanged
Provider Status order, and the Settings -> Services order.

The renderers run for real under node with a minimal DOM: what is asserted is
what the operator reads, not the source text that produces it.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from integrations.catalog import definitions

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"
PREMIUM_JS = (STATIC / "ui-premium-account-status.js").read_text(encoding="utf-8")
SETTINGS_JS = (STATIC / "ui-settings-page.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
DAY = 86400

needs_node = pytest.mark.skipif(NODE is None, reason="node is required to run the browser renderers")


def _node(script: str):
    completed = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(completed.stdout)


_DOM = r"""
const nodes = {};
function element(tag) {
  return {tag, className: '', textContent: '', children: [], style: {}, dataset: {},
          append(...items) { this.children.push(...items); },
          replaceChildren(...items) { this.children = items; }};
}
nodes['premium-row'] = element('div');
nodes['lbl-premium'] = element('span');
const listeners = {};
global.window = global;
global.document = {
  getElementById: id => nodes[id] || null,
  createElement: element,
  addEventListener: (name, handler) => { listeners[name] = handler; },
};
const text = node => node.children.length ? node.children.map(text) : node.textContent;
"""


def _render(entries):
    script = _DOM + PREMIUM_JS + f"""
listeners['debridpulse:provider-status']({{detail: {{entries: {json.dumps(entries)}}}}});
console.log(JSON.stringify({{lines: nodes['lbl-premium'].children.map(text),
                             shown: nodes['premium-row'].style.display !== 'none'}}));
"""
    return _node(script)


def _account(name, *, plan, expires_at=None, service_class="premium"):
    return {"id": name.casefold(), "name": name, "state": "healthy",
            "status": {"account": {"entitlement": "ready", "service_class": service_class, "plan": plan,
                                   "expires_at": expires_at}}}


@needs_node
def test_a_premium_account_with_an_unreported_end_renders_no_date_and_no_lifetime():
    """No lifetime state exists: an account whose end Debrid-Link does not
    report contributes no crown entry, exactly as any unknown expiry."""
    assert _render([_account("Debrid-Link", plan="Premium")]) == {"lines": [], "shown": False}
    assert _render([_account("Debrid-Link", plan="Free", service_class="standard")])["shown"] is False


@needs_node
def test_a_finite_debridlink_premium_uses_the_one_premium_renderer():
    import time
    rendered = _render([_account("Debrid-Link", plan="Premium", expires_at=time.time() + 10 * DAY + 60)])
    assert rendered["lines"][0][1] == "(11 days remaining)" and rendered["lines"][0][0].startswith(
        "Debrid-Link Premium until ")


def test_the_premium_owner_has_no_lifetime_wording():
    assert ".lifetime" not in PREMIUM_JS and "never expires" not in PREMIUM_JS.casefold()


def test_the_premium_owner_names_no_provider():
    assert not re.search(r"debrid-?link", PREMIUM_JS, re.IGNORECASE)


def test_provider_status_order_is_unchanged_and_debridlink_takes_the_default_slot():
    """Provider Status orders by ``display_order`` alone (ui-provider-status.js).
    The existing premium entries keep their declared order and Usenet its
    reserved tail; Debrid-Link declares nothing and takes the default."""
    premium = sorted((item.presentation.display_order, item.id) for item in definitions
                     if item.presentation.status_tier == "premium_service")
    assert premium == [(10, "alldebrid"), (11, "realdebrid"), (12, "torbox"), (100, "debridlink"), (900, "usenet")]
    status_js = (STATIC / "ui-provider-status.js").read_text(encoding="utf-8")
    assert not re.search(r"debrid-?link|usenet", status_js, re.IGNORECASE)


def _order_function() -> str:
    match = re.search(r"\n  function premiumServiceOrder\(cards\) \{\n.*?\n  \}\n", SETTINGS_JS, re.DOTALL)
    assert match, "the Services order has one owner"
    return match.group(0)


def _settings_order(cards):
    script = _order_function() + f"""
const ordered = premiumServiceOrder({json.dumps(cards)});
console.log(JSON.stringify([ordered.families.map(card => card.id), ordered.providers.map(card => card.id)]));
"""
    return _node(script)


def _catalog_cards():
    return [{"id": item.id, "presentation": item.presentation.public()} for item in definitions
            if item.presentation.status_tier == "premium_service"]


@needs_node
def test_settings_services_keeps_usenet_first_then_named_providers_alphabetically():
    assert _settings_order(_catalog_cards()) == [["usenet"], ["alldebrid", "debridlink", "realdebrid", "torbox"]]


@needs_node
def test_a_future_named_provider_sorts_into_place_by_its_name_alone():
    future = [{"id": "zz_new", "presentation": {"status_name": "Crate-Debrid",
                                                "standard_status_tier": "general_family"}},
              {"id": "aa_new", "presentation": {"status_name": "Zebra", "standard_status_tier": "general_family"}}]
    families, providers = _settings_order(_catalog_cards() + future)
    assert families == ["usenet"]
    assert providers == ["alldebrid", "zz_new", "debridlink", "realdebrid", "torbox", "aa_new"]


def test_the_services_order_is_metadata_not_identity_or_routing_priority():
    owner = _order_function()
    assert "usenet" not in owner.casefold() and "priority" not in owner and "display_order" not in owner
    assert "standard_status_tier" in owner and "status_name" in owner


def test_the_debridlink_card_uses_the_existing_settings_machinery():
    assert "'debridlink'" in SETTINGS_JS[SETTINGS_JS.index("const INTEGRATION_SCOPES"):][:200]
    assert "debridlink_api_key: {scope: 'integration:debridlink', option: 'api_key'}" in SETTINGS_JS
    assert "providerCard('debridlink', 'Debrid-Link'" in SETTINGS_JS
    assert "providerTestAction('test-debridlink')" in SETTINGS_JS
    assert "debridlink: '/settings/validate-debridlink'" in SETTINGS_JS
    # Its account line is the one neutral premium wording, never its own.
    assert "accountExpiry(id, account)" in SETTINGS_JS[SETTINGS_JS.index("function apiKeyAccountLine"):][:400]
    assert "rate_limit" not in SETTINGS_JS[SETTINGS_JS.index("const debridLinkCard"):][:2500]
