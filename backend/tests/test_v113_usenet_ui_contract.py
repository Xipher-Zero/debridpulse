"""1.0.13 Usenet UI contract: Sources & Providers, Downloads, sidebar.

Static-analysis contract over the bounded owners. Operator-facing surfaces name
capabilities ("Usenet", "General Sources"), never daemon
implementations ("SABnzbd", "aria2").
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"
SETTINGS = (STATIC / "ui-settings-page.js").read_text(encoding="utf-8")
SERVERS = (STATIC / "ui-settings-usenet-servers.js").read_text(encoding="utf-8")
STATUS = (STATIC / "ui-provider-status.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
STYLE = (STATIC / "style.css").read_text(encoding="utf-8")


def sources_panel() -> str:
    start = SETTINGS.index("function sourcesPanel(s)")
    return SETTINGS[start:SETTINGS.index("const ARIA2_LIVE_FILTERS")]


def downloads_panel() -> str:
    start = SETTINGS.index("function downloadsPanel(s)")
    return SETTINGS[start:SETTINGS.index("function extractionPanel(s)")]


# --- Sources & Providers -------------------------------------------------

def test_master_groups_are_renamed():
    panel = sources_panel()
    assert "groupCard('External Providers'" in panel
    assert "'Debrid Services'" not in panel
    assert "General Sources" in SETTINGS
    assert "groupCard('Direct Sources'" not in panel


def test_usenet_is_the_first_card_under_external_providers():
    panel = sources_panel()
    assert "groupCard('External Providers', usenetCard + provider," in panel


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
    assert "{text: 'Unconfigured', tone: 'error'}" in status
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
    assert "Add Server" in SETTINGS
    assert "data-usenet-collection" in SETTINGS


def test_add_tile_uses_a_lucide_plus_and_centres_its_group():
    assert "/icons/lucide/plus.svg" in SETTINGS
    css = (STATIC / "ui-settings-usenet-servers.css").read_text(encoding="utf-8")
    block = css[css.index(".dp-usenet-add {"):]
    assert "align-items: center" in block and "justify-content: center" in block
    inner = css[css.index(".dp-usenet-add-inner {"):]
    assert "flex-direction: column" in inner and "align-items: center" in inner


def test_host_port_and_ssl_share_one_aligned_row():
    assert 'dp-usenet-row--host' in SETTINGS
    for field in ("host", "port", "ssl"):
        assert f'data-usenet-field="{field}"' in SETTINGS
    css = (STATIC / "ui-settings-usenet-servers.css").read_text(encoding="utf-8")
    assert ".dp-usenet-row--host { align-items: center; }" in css
    assert ".dp-usenet-row--host .dp-usenet-field--port" in css


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
    assert "data-usenet-clear-password" in SETTINGS
    assert "clear_password" in SERVERS


def test_a_failed_native_application_is_surfaced_on_the_card():
    assert "native" in SERVERS and "did not accept it" in SERVERS


def test_connections_and_priority_share_one_aligned_row():
    assert "dp-usenet-row--tuning" in SETTINGS
    css = (STATIC / "ui-settings-usenet-servers.css").read_text(encoding="utf-8")
    assert ".dp-usenet-row--tuning { align-items: center; }" in css


def test_no_api_key_field_is_exposed_on_a_news_server():
    """SAB's NNTP server auth is username/password only (characterized)."""
    start = SETTINGS.index("function usenetServerCard(")
    card = SETTINGS[start:SETTINGS.index("function usenetAddTile()")]
    for field in ("host", "ssl", "username", "password", "connections", "priority"):
        assert f'data-usenet-field="{field}"' in card
    assert 'data-usenet-field="api_key"' not in card
    assert 'data-usenet-field="api"' not in card


def test_priority_helper_text_matches_characterized_semantics():
    assert "Lower values have priority." in SETTINGS


def test_server_actions_are_save_test_remove():
    for action in ("save", "test", "remove"):
        assert f'data-usenet-action="{action}"' in SETTINGS


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
    payload = SETTINGS[SETTINGS.index("function usenetConfigurationPayload()"):
                       SETTINGS.index("function transferPolicyPayload()")]
    options = payload[payload.index("options: {"):payload.index("};", payload.index("options: {"))]
    assert "servers" not in options
    # And no service endpoint or credential is written from here either.
    for forbidden in ("service_url", "api_key"):
        assert forbidden not in payload


# --- Downloads -----------------------------------------------------------

def test_downloads_has_the_three_master_cards_in_order():
    panel = downloads_panel()
    first = panel.index("'Global Download Settings'")
    second = panel.index("groupCard('Executor Tuning'")
    third = panel.index("'Download Safety & Recovery'")
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
    assert "executorTuningCard('direct', 'General Sources'" in panel
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
    assert "Tuning for HTTP(S), FTP/SFTP and other general source transfers." in panel
    assert "Tuning for Usenet download behavior." in panel
    # DP 1.0.13 work item E: the copy is centred against the FULL header by the
    # canonical three-region Settings card header, not against a flex remainder.
    css = (STATIC / "ui-settings-page.css").read_text(encoding="utf-8")
    header = css[css.index("#view-settings .card-header.dp-settings-card-header"):]
    header = header[:header.index("}") + 1]
    assert "grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr)" in header
    centre = css[css.index("#view-settings .dp-settings-card-header > .dp-settings-card-header-center"):]
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
