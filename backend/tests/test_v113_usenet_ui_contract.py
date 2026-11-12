"""1.0.13 Usenet UI contract: Services, Downloads, sidebar.

Static-analysis contract over the bounded owners. Operator-facing surfaces name
capabilities ("Usenet", "Network Sources"), never daemon
implementations ("SABnzbd", "aria2").
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from i18n_node import english, says

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"
SETTINGS = (STATIC / "ui-settings-page.js").read_text(encoding="utf-8")
SERVERS = (STATIC / "ui-settings-usenet-servers.js").read_text(encoding="utf-8")
STATUS = (STATIC / "ui-provider-status.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
STYLE = (STATIC / "style.css").read_text(encoding="utf-8")


def sources_panel() -> str:
    start = SETTINGS.index("function sourcesPanel(s)")
    return SETTINGS[start:SETTINGS.index("const EXECUTOR_WORK_FILTERS")]


def downloads_panel() -> str:
    start = SETTINGS.index("function downloadsPanel(s)")
    return SETTINGS[start:SETTINGS.index("function extractionPanel(s)")]


# --- Services -------------------------------------------------

def test_master_groups_are_renamed():
    panel = sources_panel()
    # 1.0.14: said from the catalog by key (the words unchanged).
    assert "groupCard(say('settings.services.premium')" in panel and english("settings.services.premium") == "Premium Services"
    assert "'Debrid Services'" not in panel
    assert says(SETTINGS, "Network Sources")
    assert "groupCard('Direct Sources'" not in panel


def test_usenet_is_the_first_card_under_premium_services():
    panel = sources_panel()
    assert "groupCard(say('settings.services.premium')," in panel
    # The one Services order owner puts the service family (Usenet) first;
    # test_v113_debridlink_presentation runs it on the real catalogue.
    assert "premiumServiceOrder([" in panel and "['usenet', usenetCard]" in panel


def test_usenet_renders_collapsed_and_only_an_accepted_enable_can_open_it():
    """Both behaviors come from the ONE shared provider-card owner.

    `providerCard` renders every expandable card collapsed -- navigation is not
    an opinion about what should be open, and enabled state is not expansion
    state. The single automatic expansion belongs to the operator ACTION that
    admits a provider with nothing configured, and it still follows what the
    server accepted rather than the operator's click.
    """
    card = SETTINGS[SETTINGS.index("function providerCard("):SETTINGS.index("function sourcesPanel(")]
    assert "dp-settings-provider-card--collapsed" in card
    assert "settingsDisclosure(bodyId, false," in card
    assert "${enabled ? '' : ' hidden'}" not in card
    behavior = SETTINGS[SETTINGS.index("function renderIntegrationState("):
                        SETTINGS.index("function bindEvents", SETTINGS.index("function renderIntegrationState("))]
    assert "if (enabled) setDisclosureExpanded(disclosure, true);" not in behavior
    assert "state.settings?.integrations?.[identity]" in behavior
    handler = SETTINGS[SETTINGS.index("async function providerEnableChanged("):
                       SETTINGS.index("function adoptIntegrationGroup(")]
    assert "setDisclosureExpanded(" in handler and ".configured" in handler
    # Usenet opts into that premium card shape.
    from integrations.usenet.definition import definition as usenet
    assert usenet.presentation.premium is True


def test_enabled_but_unconfigured_usenet_reports_unconfigured():
    status = SETTINGS[SETTINGS.index("function providerStatus("):
                      SETTINGS.index("function providerCard(")]
    assert "{text: say('settings.status.unconfigured'), tone: 'error'}" in status
    assert english("settings.status.unconfigured") == "Unconfigured"
    assert "Configuration required" not in status


def test_usenet_is_not_enabled_until_an_operator_turns_it_on():
    from integrations.usenet.definition import definition as usenet
    assert usenet.default_enabled is False


def test_an_absent_usenet_namespace_renders_the_toggle_off():
    """`providerCard` treats an absent entry as enabled; Usenet must not.

    Without an explicit OFF default the toggle would render checked for a
    settings payload carrying no `integrations.usenet`, and an ordinary Save
    would then persist `enabled: true` the operator never chose.
    """
    panel = sources_panel()
    assert "const usenet = integrations.usenet || {enabled: false};" in panel


def test_no_daemon_name_in_operator_facing_settings_markup():
    for surface in (sources_panel(), downloads_panel(), SERVERS):
        assert "SABnzbd" not in surface
        assert "sabnzbd" not in surface


def test_usenet_card_renders_an_add_server_tile_and_server_collection():
    assert "function usenetAddTile()" in SETTINGS
    assert 'data-usenet-action="add"' in SETTINGS
    assert says(SETTINGS, "Add Server")
    assert "data-usenet-collection" in SETTINGS


def test_add_tile_uses_a_lucide_plus_and_centres_its_group():
    assert "/icons/lucide/plus.svg" in SETTINGS
    css = (STATIC / "ui-settings-usenet-servers.css").read_text(encoding="utf-8")
    block = css[css.index(".dp-usenet-add {"):]
    assert "align-items: center" in block and "justify-content: center" in block
    inner = css[css.index(".dp-usenet-add-inner {"):]
    assert "flex-direction: column" in inner and "align-items: center" in inner


def test_host_then_port_with_ssl_as_one_group():
    """DP 1.0.14: Host, then Port with SSL as ONE group -- one row while the
    three fit, the group beneath Host when they do not (the flex line's own
    fit, never a viewport width). The group is a two-row grid, labels above
    their controls, so SSL's label is always above its toggle and the toggle
    is centred against the Port input box itself."""
    assert 'dp-usenet-row--host' in SETTINGS and 'dp-usenet-port-ssl' in SETTINGS
    for field in ("host", "port", "ssl"):
        assert f'data-usenet-field="{field}"' in SETTINGS
    css = (STATIC / "ui-settings-usenet-servers.css").read_text(encoding="utf-8")
    row = css[css.index(".dp-usenet-row--host {"):]
    row = row[:row.index("}") + 1]
    assert "flex-wrap: wrap" in row
    group = css[css.index(".dp-usenet-port-ssl {"):]
    group = group[:group.index("}") + 1]
    assert "display: grid" in group and "grid-template-rows: auto auto" in group
    assert ".dp-usenet-field--ssl > .form-label { grid-column: 2; grid-row: 1;" in css
    ssl = css[css.index(".dp-usenet-ssl {"):]
    ssl = ssl[:ssl.index("}") + 1]
    assert "grid-row: 2" in ssl


def test_the_ssl_toggle_follows_only_a_conventional_port():
    """563/119 follow the SSL toggle; a deliberate port is never overwritten."""
    assert "const SSL_PORT = 563;" in SERVERS
    assert "const PLAIN_PORT = 119;" in SERVERS
    assert "if (current === SSL_PORT || current === PLAIN_PORT || !current)" in SERVERS


def test_a_card_is_addressed_by_canonical_id_never_by_index():
    assert "data-usenet-server-id" in SETTINGS and "data-usenet-server-id" in SERVERS
    assert 'data-usenet-server="' not in SETTINGS
    assert 'data-usenet-server="' not in SERVERS
    # Save/remove address one canonical server.
    assert "'/usenet/servers/${encodeURIComponent(id)}'".replace("'", "`") in SERVERS or \
           "`/usenet/servers/${encodeURIComponent(id)}`" in SERVERS


def test_a_blank_password_is_omitted_so_the_stored_one_survives():
    assert "if (password) payload.password = password;" in SERVERS
    # And there is no shadow secret kept anywhere in the owner.
    assert "localStorage" not in SERVERS and "sessionStorage" not in SERVERS


def test_clearing_a_stored_password_is_explicit():
    # The explicit destructive ACTION; what it means is asked by the one
    # canonical Settings confirmation at the moment the operator presses it.
    assert 'data-usenet-action="clear-password"' in SETTINGS
    assert "clear_password" in SERVERS


def test_a_failed_native_application_is_surfaced_on_the_card():
    assert "native" in SERVERS and says(SERVERS, "did not accept it")


def test_connections_and_priority_share_one_aligned_row():
    """DP 1.0.14: a two-equal-column grid of labels, then inputs, so the
    Priority column carries its own help text beneath its input, a label
    needing two lines at a narrow card sits on its input, and the two inputs
    stay on one band -- a 2x2 at every width, never wider than the card."""
    assert "dp-usenet-row--tuning" in SETTINGS
    css = (STATIC / "ui-settings-usenet-servers.css").read_text(encoding="utf-8")
    tuning = css[css.index(".dp-usenet-row--tuning {"):]
    tuning = tuning[:tuning.index("}")]
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in tuning
    assert ".dp-usenet-row--tuning .input { grid-row: 2; min-width: 0; }" in css


def test_no_api_key_field_is_exposed_on_a_news_server():
    """SAB's NNTP server auth is username/password only (characterized)."""
    start = SETTINGS.index("function usenetServerCard(")
    card = SETTINGS[start:SETTINGS.index("function usenetAddTile()")]
    for field in ("host", "ssl", "username", "password", "connections", "priority"):
        assert f'data-usenet-field="{field}"' in card
    assert 'data-usenet-field="api_key"' not in card
    assert 'data-usenet-field="api"' not in card


def test_priority_helper_text_matches_characterized_semantics():
    assert says(SETTINGS, "Lower values have priority.")


def test_server_actions_are_test_remove_and_a_confirmed_clear():
    """DP 1.0.13: there is no Save. Entering or replacing a credential commits
    on changed blur; erasing one is an explicit confirmed Clear."""
    for action in ("remove", "clear-password"):
        assert f'data-usenet-action="{action}"' in SETTINGS
    # Test is the canonical Settings Test control, addressed to this owner.
    # Text-only: the card's Test carries no flask chip (1.0.14).
    assert "providerTestAction('test', 'data-usenet-action', {mark: false})" in SETTINGS
    assert 'data-usenet-action="save"' not in SETTINGS


def test_server_test_and_remove_use_the_canonical_settings_actions():
    """DP 1.0.13 cleanup: Remove deletes the whole server, so it is the
    canonical destructive action; Test is the one canonical Test control,
    text-only on a server card (1.0.14: no chip, no flask glyph). The blank card
    the behaviour owner creates says the same."""
    # Its word is the catalog's (1.0.14), keyed after these attributes in both renderings.
    remove = 'type="button" class="btn btn-danger btn-sm dp-usenet-remove" data-usenet-action="remove"'
    assert remove in SETTINGS and remove in SERVERS and english("settings.usenet.remove") == "Remove"
    assert "btn-ghost btn-sm dp-usenet-remove" not in SETTINGS + SERVERS
    blank_test = SERVERS[SERVERS.index('data-usenet-action="test"') - 80:SERVERS.index('data-usenet-action="test"') + 400]
    assert 'class="btn btn-ghost btn-sm dp-settings-provider-test"' in blank_test
    blank_test = blank_test[:blank_test.index("</button>")]
    assert "said('span', 'settings.test')" in blank_test and english("settings.test") == "Test"
    for part in ("dp-settings-action-chip", "flask-conical.svg"):
        assert part not in blank_test, part
    # No second Test style: the Usenet stylesheet states nothing about either.
    css = (STATIC / "ui-settings-usenet-servers.css").read_text(encoding="utf-8")
    for selector in ("dp-settings-provider-test", "dp-settings-action-chip", "dp-usenet-remove", "btn-danger"):
        assert selector not in css, selector


def test_add_tile_fills_its_grid_row_without_stretching_the_server_cards():
    css = (STATIC / "ui-settings-usenet-servers.css").read_text(encoding="utf-8")
    grid = css[css.index(".dp-usenet-servers {"):]
    assert "align-items: start" in grid[:grid.index("}")]
    tile = css[css.index(".dp-usenet-add {\n  align-self"):]
    assert "align-self: stretch" in tile[:tile.index("}")]


def test_display_name_derives_from_host_and_honours_an_override():
    assert "function refreshDerivedName(card)" in SERVERS
    assert "usenetNameOverride === '1'" in SERVERS
    # Clearing the override returns to derived behavior.
    assert "card.dataset.usenetNameOverride = trimmed ? '1' : '0';" in SERVERS


def test_removed_servers_pack_left_with_the_tile_last():
    assert "function reindex(host)" in SERVERS
    assert "host.appendChild(tile)" in SERVERS


def test_server_collection_has_exactly_one_writer():
    """Only the bounded owner writes servers; the page payload must not.

    Asserted against the emitted option KEYS, not prose: the payload's option
    object must carry no ``servers`` key.
    """
    # Servers are written ONLY through the per-server routes, by canonical id.
    assert "/usenet/servers" in SERVERS
    assert "/integrations/usenet/configuration" not in SERVERS
    # DP 1.0.13: the page carries no deferred Usenet payload at all -- every
    # option of that namespace is a declared field-boundary control, so there
    # is no page-level object that could carry a server list.
    assert "function usenetConfigurationPayload()" not in SETTINGS
    payload = SETTINGS[SETTINGS.index("const COMMIT_FIELDS"):]
    payload = payload[:payload.index("});") + 3]
    usenet = [line for line in payload.splitlines() if "'integration:usenet'" in line]
    assert usenet, "the Usenet namespace declares no controls at all"
    declared = {line.split("option: '", 1)[1].split("'", 1)[0] for line in usenet}
    assert "servers" not in declared
    # And no service endpoint or credential is written from here either.
    for forbidden in ("service_url", "api_key"):
        assert forbidden not in declared


# --- Downloads -----------------------------------------------------------

def test_downloads_has_the_three_master_cards_in_order():
    panel = downloads_panel()
    first = panel.index("say('settings.downloads.behavior')")
    second = panel.index("groupCard(say('settings.downloads.methods')")
    third = panel.index("say('settings.downloads.recovery')")
    assert [english(key) for key in ("settings.downloads.behavior", "settings.downloads.methods", "settings.downloads.recovery")] == \
        ["Download Behavior & Limits", "Transfer Method Settings", "Disk Space & Recovery"]
    assert first < second < third


def test_global_row_keeps_download_folder_and_max_concurrent_on_one_line():
    panel = downloads_panel()
    row = panel[panel.index("dp-settings-download-engine-row"):panel.index("dp-settings-download-engine-card")]
    assert "directoryField('download_folder'" in row
    assert "aria2_max_active_downloads" in row
    assert "dp-settings-download-path-stack" in row and "dp-settings-download-limit" in row


def test_executor_tuning_has_general_sources_and_usenet_children():
    """DP 1.0.13 Item 8: the operator-facing family name matches Sources &
    Providers. The executor id stays 'direct' -- nothing about the executor
    changed, and renaming a durable identity for copy would be churn."""
    panel = downloads_panel()
    assert "executorTuningCard('direct', say('integration.group.direct_sources')" in panel
    assert english("integration.group.direct_sources") == "Network Sources"
    assert "executorTuningCard('usenet', 'Usenet'" in panel


def test_child_tuning_cards_default_collapsed_with_the_control_beside_the_label():
    block = SETTINGS[SETTINGS.index("function executorTuningCard("):
                     SETTINGS.index("function directTransfersTuning(")]
    assert "<div class=\"card-body\" id=\"${bodyId}\" hidden>" in block
    # DP 1.0.13 work item C: ONE canonical disclosure component, rendered by
    # settingsDisclosure() inside the title group -- after the title, before
    # the centred copy, never at the far edge beside the operational controls.
    assert block.index("card-title") < block.index("settingsDisclosure(")
    assert block.index("settingsDisclosure(") < block.index("dp-settings-card-header-center")
    component = SETTINGS[SETTINGS.index("function settingsDisclosure("):
                         SETTINGS.index("function providerStatus(")]
    assert 'aria-expanded="${expanded}"' in component


def test_tuning_headers_have_centred_neutral_explanatory_text():
    panel = downloads_panel()
    # DP 1.0.13 consolidation: operator-facing flavour text, in the SAME
    # de-emphasised titlebar treatment the Download Behavior & Limits header
    # uses -- one declaration, two users, never a second heading style.
    assert says(panel, "How DebridPulse handles downloads from direct network sources.")
    assert says(panel, "Global download behavior shared by all Usenet servers.")
    assert "dp-settings-download-engine-header-copy" in SETTINGS[
        SETTINGS.index("function executorTuningCard("):SETTINGS.index("function directTransfersTuning(")]
    # DP 1.0.13 work item E: the copy is centred against the FULL header by the
    # canonical three-region Settings card header, not against a flex remainder.
    css = (STATIC / "ui-settings-page.css").read_text(encoding="utf-8")
    header = css[css.index(":is(#view-settings, .dp-setup-provider-surface) .card-header.dp-settings-card-header"):]
    header = header[:header.index("}") + 1]
    assert "grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr)" in header
    centre = css[css.index(":is(#view-settings, .dp-setup-provider-surface) .dp-settings-card-header > .dp-settings-card-header-center"):]
    centre = centre[:centre.index("}") + 1]
    assert "justify-self: center" in centre
    assert "text-align: center" in centre


def test_direct_transfers_keeps_every_existing_advanced_control():
    block = SETTINGS[SETTINGS.index("function directTransfersTuning("):
                     SETTINGS.index("function usenetTuning(")]
    for control in ("aria2_continue_downloads", "aria2_split", "aria2_max_connection_per_server",
                    "aria2_min_split_size", "aria2_lowest_speed_limit", "aria2_disk_cache",
                    "aria2_file_allocation"):
        assert control in block


def test_usenet_tuning_exposes_no_queue_or_concurrency_setting():
    """No CONTROL for native queue depth, concurrency, bandwidth or unpack.

    Asserted against the rendered control identifiers rather than prose, so the
    card may still explain where global admission actually lives.
    """
    block = SETTINGS[SETTINGS.index("function usenetTuning("):
                     SETTINGS.index("function downloadsPanel(")]
    controls = set(re.findall(r"input\('([a-z0-9_]+)'", block))
    controls |= set(re.findall(r"tuningToggle\(\s*'([a-z0-9_]+)'", block))
    controls |= set(re.findall(r"selectField\('([a-z0-9_]+)'", block))
    # DP 1.0.13 work item F widened this card to the approved acquisition
    # controls. What it may NEVER hold is a second owner for something
    # DebridPulse already owns globally.
    assert controls == {
        "usenet_operation_timeout_seconds",
        "usenet_article_cache_megabytes",
        "usenet_direct_write",
        "usenet_max_acquisition_retries",
    }
    for forbidden in ("queue", "max_active", "concurrent", "bandwidth", "speedlimit", "unpack"):
        assert not any(forbidden in name for name in controls)


def test_no_executor_card_enable_toggle():
    panel = downloads_panel()
    assert "data-integration-enabled" not in panel


# --- Sidebar -------------------------------------------------------------

def test_sidebar_includes_paired_provider_executor_integrations():
    assert "integration.kind === 'provider_executor'" in STATUS


def test_sidebar_never_labels_a_daemon():
    assert "SABnzbd" not in STATUS and "aria2" not in STATUS


# --- wiring --------------------------------------------------------------

def test_owner_is_loaded_and_styled_through_the_canonical_graph():
    assert "ui-settings-usenet-servers.js" in INDEX
    assert "ui-settings-usenet-servers.css" in STYLE
