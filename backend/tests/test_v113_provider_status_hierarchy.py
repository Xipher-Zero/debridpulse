"""DP 1.0.13 Provider Status tiers.

Provider Status presents two tiers -- Premium Services, then Standard Services
-- driven entirely by neutral, integration-owned presentation metadata. Usenet
is a PREMIUM service and is the reserved LAST row of that tier; the aggregate
Network Sources family is the whole of Standard Services. The renderer never
names an integration, and both the tier order and the order within a tier are
derived from the one ordering authority that already exists, ``display_order``.

The reserved-band invariant and the absence audits are owned by
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
DEFAULT_ORDER = IntegrationPresentation().display_order


# --- neutral metadata ------------------------------------------------------

def test_presentation_metadata_can_express_the_neutral_tiers():
    presentation = IntegrationPresentation()
    assert presentation.status_tier is None
    assert presentation.status_tier_label is None
    assert "status_tier" in presentation.public()
    assert "status_tier_label" in presentation.public()


def test_every_current_integration_declares_its_tier():
    """Usenet is a PREMIUM service; only the aggregate family is Standard.

    The internal tier identities are unchanged -- ``premium_service`` and
    ``general_family`` -- and only the operator-facing label of the lower tier
    moved.
    """
    for definition in (alldebrid_definition, usenet_definition):
        assert definition.presentation.status_tier == PREMIUM_SERVICE, definition.id
        assert definition.presentation.status_tier_label == "Premium Services", definition.id
    for definition in (general_http_definition, general_ftp_definition):
        assert definition.presentation.status_tier == GENERAL_FAMILY, definition.id
        assert definition.presentation.status_tier_label == "Standard Services", definition.id


def test_tier_order_is_deterministic_and_derived_from_existing_ordering_metadata():
    """Premium Services -> Standard Services falls out of display_order, and
    inside PREMIUM so does debrid -> Usenet; no tier-order table and no
    renderer switch exists."""
    orders = {d.id: d.presentation.display_order for d in
              (alldebrid_definition, usenet_definition, general_http_definition, general_ftp_definition)}
    assert orders["alldebrid"] < orders["usenet"] < orders["general_http"] <= orders["general_ftp"]


def test_usenet_is_the_reserved_last_premium_row():
    """Reserved BANDS, not merely today's values.

    Ordinary premium entries -- including every future one that declares no
    order at all and therefore takes the presentation model's default -- sort
    before Usenet, and Usenet sorts before the Standard band. This is what makes
    "debrid providers, then Usenet" true of integrations that do not exist yet
    without the renderer, or this batch, naming any of them.
    """
    usenet = usenet_definition.presentation.display_order
    assert alldebrid_definition.presentation.display_order < DEFAULT_ORDER
    assert DEFAULT_ORDER < usenet, "a future default-order premium entry would sort after Usenet"
    for definition in (general_http_definition, general_ftp_definition):
        assert usenet < definition.presentation.display_order, definition.id


def test_premium_remains_before_standard_even_with_usenet_as_the_sole_premium_row():
    """A tier takes the position of its FIRST entry, so with AllDebrid absent
    the premium tier is created by Usenet -- which must still precede the
    Standard band."""
    premium = [d.presentation.display_order for d in (usenet_definition,)]
    standard = [d.presentation.display_order
                for d in (general_http_definition, general_ftp_definition)]
    assert max(premium) < min(standard)


def test_the_aggregate_network_sources_family_is_the_standard_tier():
    for definition in (general_http_definition, general_ftp_definition):
        assert definition.presentation.status_group == "direct_sources"
        assert definition.presentation.status_group_label == "Network Sources"


def test_the_network_source_members_carry_the_new_operator_facing_names():
    """One name owner, two fields of the same definition: the transfer-list
    badge reads ``name`` and Provider Status reads ``status_name``."""
    assert general_http_definition.name == "HTTP(S)"
    assert general_http_definition.presentation.status_name == "HTTP(S)"
    assert general_ftp_definition.name == "(S)FTP"
    assert general_ftp_definition.presentation.status_name == "(S)FTP"


def test_the_durable_identities_are_untouched_by_the_rename():
    assert general_http_definition.id == "general_http"
    assert general_ftp_definition.id == "general_ftp"
    assert usenet_definition.id == "usenet"
    assert alldebrid_definition.id == "alldebrid"


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
                  "Premium Services", "Standard Services", "Network Sources",
                  "premium_service", "premium_family", "general_family"):
        assert named not in STATUS_JS, named


def test_the_renderer_groups_by_tier_before_group():
    assert "dp-provider-status-tier" in STATUS_JS
    assert "dp-provider-status-tier-label" in STATUS_JS
    assert STATUS_JS.index("tier") < STATUS_JS.index("groups.get(")


def test_the_renderer_declares_no_second_tier_order_table():
    """Tier order is the position of a tier's first entry and nothing else."""
    render = STATUS_JS[STATUS_JS.index("function render("):]
    render = render[:render.index("\n  async function observe(")]
    for banned in ("TIER_ORDER", "tierOrder", "TIERS =", "tierRank"):
        assert banned not in render, banned
    # The one sort in the file is the display_order sort in candidates().
    assert STATUS_JS.count(".sort(") == 1


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
