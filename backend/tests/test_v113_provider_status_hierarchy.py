"""DP 1.0.13 Provider Status tiers.

Provider Status presents two tiers -- Premium Services, then General -- driven
entirely by neutral, integration-owned presentation metadata. The standalone
``premium_family`` tier is retired: Usenet belongs to GENERAL, whose first two
positions are permanently reserved (Usenet, then General Sources). The renderer
never names an integration, and ordering is derived from the ordering metadata
that already exists.

The reserved-position invariant and the retired tier's absence are owned by
``test_v113_settings_interaction_foundation.py``.
"""
import re
from pathlib import Path

from integrations.definition import IntegrationPresentation
from integrations.usenet.definition import definition as usenet_definition
from providers.alldebrid.definition import definition as alldebrid_definition
from providers.general_ftp.definition import definition as general_ftp_definition
from providers.general_http.definition import definition as general_http_definition

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"
STATUS_JS = (STATIC / "ui-provider-status.js").read_text(encoding="utf-8")
STATUS_CSS = (STATIC / "ui-shell-provider-status.css").read_text(encoding="utf-8")

PREMIUM_SERVICE, GENERAL_FAMILY = "premium_service", "general_family"


# --- neutral metadata ------------------------------------------------------

def test_presentation_metadata_can_express_the_neutral_tiers():
    presentation = IntegrationPresentation()
    assert presentation.status_tier is None
    assert presentation.status_tier_label is None
    assert "status_tier" in presentation.public()
    assert "status_tier_label" in presentation.public()


def test_every_current_integration_declares_its_tier():
    assert alldebrid_definition.presentation.status_tier == PREMIUM_SERVICE
    assert alldebrid_definition.presentation.status_tier_label == "Premium Services"
    for definition in (usenet_definition, general_http_definition, general_ftp_definition):
        assert definition.presentation.status_tier == GENERAL_FAMILY
        assert definition.presentation.status_tier_label == "General"


def test_tier_order_is_deterministic_and_derived_from_existing_ordering_metadata():
    """Premium Services -> General falls out of display_order, and inside
    GENERAL so does Usenet -> General Sources; no tier-order table and no
    renderer switch exists."""
    orders = {d.id: d.presentation.display_order for d in
              (alldebrid_definition, usenet_definition, general_http_definition, general_ftp_definition)}
    assert orders["alldebrid"] < orders["usenet"] < orders["general_http"] <= orders["general_ftp"]


def test_the_aggregate_general_family_survives_unchanged():
    for definition in (general_http_definition, general_ftp_definition):
        assert definition.presentation.status_group == "direct_sources"
        assert definition.presentation.status_group_label == "General Sources"


def test_usenet_remains_one_aggregate_row_and_never_lists_a_news_server():
    assert usenet_definition.presentation.status_name == "Usenet"
    assert usenet_definition.presentation.status_group is None
    # News servers are not integrations, so they can never be candidates.
    assert "servers" not in usenet_definition.presentation.public()


# --- the renderer is generic ----------------------------------------------

def test_the_renderer_consumes_tier_metadata_and_names_no_integration():
    assert "status_tier" in STATUS_JS
    assert "status_tier_label" in STATUS_JS
    for named in ("alldebrid", "usenet", "general_http", "general_ftp",
                  "Premium Services", "premium_service", "premium_family", "general_family"):
        assert named not in STATUS_JS, named


def test_the_renderer_groups_by_tier_before_group():
    assert "dp-provider-status-tier" in STATUS_JS
    assert "dp-provider-status-tier-label" in STATUS_JS
    assert STATUS_JS.index("tier") < STATUS_JS.index("groups.get(")


def test_an_empty_tier_is_not_rendered_as_a_bare_heading():
    render = STATUS_JS[STATUS_JS.index("function render("):]
    render = render[:render.index("\n  async function observe(")]
    assert "length" in render


def test_an_integration_without_a_tier_is_never_dropped():
    render = STATUS_JS[STATUS_JS.index("function render("):]
    render = render[:render.index("\n  async function observe(")]
    # There is an explicit untiered path, so a future integration that has not
    # declared a tier still renders.
    assert "untiered" in render or "tier || ''" in render or "!entry.tier" in render


def test_health_and_aggregate_semantics_are_untouched():
    assert "function dotClass(" in STATUS_JS
    assert "function aggregateState(" in STATUS_JS
    assert "auth_required" in STATUS_JS


def test_the_tier_heading_has_material_in_the_provider_status_css_owner():
    assert ".dp-provider-status-tier-label" in STATUS_CSS


def test_the_alldebrid_subscription_row_is_not_part_of_this_list():
    """It is sibling shell markup owned by ui-alldebrid-account-status.js."""
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    shell = index[index.index('<div class="dp-provider-status-heading">'):
                  index.index('<span id="lbl-db">')]
    assert shell.index('id="premium-row"') < shell.index('id="provider-status-list"')
    assert "premium-row" not in STATUS_JS
