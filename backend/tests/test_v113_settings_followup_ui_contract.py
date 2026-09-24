"""1.0.13 settings follow-up: source-level ownership contracts.

Geometry itself is proven in the browser (``frontend/browser/*.spec.js``);
these assert the ONE-owner properties a rendered measurement cannot:
that a correction lives in its canonical file and nowhere else, and that a
retired control really is gone rather than merely superseded.
"""
from __future__ import annotations

from pathlib import Path
import re

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


SETTINGS = read("ui-settings-page.js")
TRANSFER_CONTRACT = read("ui-transfer-contract.css")
PROVIDER_SHELL = read("ui-shell-provider-status.css")
FORM_LAYOUT = read("ui-settings-form-layout.css")
CARD_ICONS = read("ui-settings-card-icons.css")
UNIVERSAL = read("ui-universal-language.css")
USENET_CSS = read("ui-settings-usenet-servers.css")
PROVIDER_STATUS = read("ui-provider-status.js")


def body(source: str, name: str) -> str:
    """One top-level function body, by name, without its neighbours."""
    start = source.index(f"function {name}(")
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unterminated function {name}")


# --- A. Route History: one shared provenance column ---------------------------

def test_route_rows_share_one_column_definition():
    assert ".dp-detail-route-list {" in TRANSFER_CONTRACT
    listing = TRANSFER_CONTRACT[TRANSFER_CONTRACT.index(".dp-detail-route-list {"):]
    listing = listing[:listing.index("}")]
    assert "grid-template-columns" in listing, \
        "the route list does not own the column tracks, so rows cannot share them"
    row = TRANSFER_CONTRACT[TRANSFER_CONTRACT.index(".dp-detail-route-row {"):]
    row = row[:row.index("\n}")]
    assert "subgrid" in row, "route rows still size their own trailing columns"


def test_route_mobile_stacking_is_preserved():
    assert "@media (max-width:700px)" in TRANSFER_CONTRACT
    mobile = TRANSFER_CONTRACT[TRANSFER_CONTRACT.index("@media (max-width:700px)"):]
    assert ".dp-detail-route-row { grid-template-columns:24px minmax(0,1fr); }" in mobile


def test_route_markup_is_unchanged_semantics():
    app = read("app.js")
    for element in ("dp-detail-route-order", "dp-detail-route-provider", "dp-detail-route-identity",
                    "dp-detail-route-relation", "dp-detail-route-outcome"):
        assert app.count(f'class="{element}"') == 1


# --- B. Provider Status tier labels -------------------------------------------

def test_tier_label_centring_has_exactly_one_owner():
    rule = PROVIDER_SHELL[PROVIDER_SHELL.index(".sidebar-footer .dp-provider-status-tier-label {"):]
    rule = rule[:rule.index("}")]
    assert "text-align: center" in rule
    others = [name for name in ("ui-provider-state.css", "ui-settings-page.css", "style.css")
              if "dp-provider-status-tier-label" in read(name)]
    assert others == [], f"a second tier-label style owner appeared: {others}"


def test_provider_rows_are_not_centred_with_the_tier_label():
    assert ".dp-provider-status-row" not in PROVIDER_SHELL[
        PROVIDER_SHELL.index(".dp-provider-status-tier-label"):
        PROVIDER_SHELL.index(".dp-provider-status-tier-label") + 400]


# --- C. one Settings field spine ----------------------------------------------

def test_the_field_spine_targets_the_control_left_edge_not_its_text():
    """The frozen invariant is ``label.left == control.left``.

    A previous revision of this pass moved the datum to the control's TEXT
    edge, indenting every label and hint by the control's border plus padding.
    That changes the requirement rather than meeting it, so the datum is
    pinned here: the one owner zeroes the inline offsets and introduces none.
    """
    block = FORM_LAYOUT[FORM_LAYOUT.index("#view-settings .dp-settings-field > .form-label"):]
    block = block[:block.index("}") + 1]
    assert "margin-inline-start: 0" in block
    assert "padding-inline-start: 0" in block
    assert "--dp-field-text-inset" not in FORM_LAYOUT
    assert "--dp-settings-field-spine" not in FORM_LAYOUT


def test_the_field_datum_has_exactly_one_owner():
    stylesheets = [path.name for path in STATIC.glob("ui-*.css")]
    owners = [name for name in stylesheets
              if re.search(r"\.dp-settings-field > \.form-label[^{]*\{[^}]*padding-inline-start",
                           read(name), flags=re.S)]
    assert owners == ["ui-settings-form-layout.css"], \
        f"the Settings field datum has more than one owner: {owners}"


def test_no_per_tab_label_or_hint_nudge_survives():
    offenders = []
    for path in STATIC.glob("ui-settings-*.css"):
        if path.name == "ui-settings-form-layout.css":
            continue
        for match in re.finditer(r"\.form-(?:label|hint)[^{]*\{([^}]*)\}", read(path.name)):
            block = match.group(1)
            for declaration in ("inset-inline-start", "margin-left", "padding-left"):
                if re.search(rf"{declaration}\s*:\s*(?!0)", block):
                    offenders.append((path.name, declaration))
    assert offenders == [], f"per-tab label/hint nudges reappeared: {offenders}"


# --- D. Usenet clear-password row ---------------------------------------------

def test_clear_password_row_is_centred_through_the_shared_primitive():
    assert ".dp-usenet-clear-password" in USENET_CSS
    rule = USENET_CSS[USENET_CSS.index(".dp-usenet-clear-password"):]
    rule = rule[:rule.index("}")]
    assert "text-align: center" in rule
    assert "display: flex" not in rule and "display: grid" not in rule, \
        "a second checkbox-row layout primitive was introduced"


# --- E. General Sources master -------------------------------------------------

def test_the_group_master_uses_a_generic_control_identity():
    assert "data-integration-group-enabled" in SETTINGS
    assert "groupEnableChanged" in SETTINGS


def test_the_group_master_is_derived_from_presentation_metadata():
    panel = body(SETTINGS, "sourcesPanel")
    assert "status_group" in panel, \
        "the group identity is not taken from integration presentation metadata"
    assert "'direct_sources'" not in panel and '"direct_sources"' not in panel, \
        "a group id was hardcoded in the UI"


def test_the_group_master_mutation_is_immediate_and_scoped():
    handler = body(SETTINGS, "groupEnableChanged")
    assert "'PATCH', `/integration-groups/" in handler
    assert "input.disabled = true" in handler, "the control is not disabled in flight"
    assert "renderGroupState" in handler, "failure does not roll back to canonical state"


def test_the_broad_settings_payload_never_carries_the_group_namespace():
    # The writable whole-settings document has one owner (settingsDocument),
    # shared by the footer payload and by the single-field settings-document
    # commit, so the canonical namespaces are stripped in exactly one place.
    document = body(SETTINGS, "settingsDocument")
    assert "delete document.integration_groups;" in document, \
        "Apply Settings can replay a stale group master"
    assert "delete document.integrations;" in document
    assert "delete document.transfer_policy;" in document
    assert "settingsDocument(state.settings)" in body(SETTINGS, "nonAuthPayload")


def test_provider_status_composes_the_gate_with_the_existing_health_model():
    aggregate = body(PROVIDER_STATUS, "aggregateState")
    assert "disabled" in aggregate and "mixed" in aggregate and "healthy" in aggregate, \
        "the health model was reduced to a toggle calculator"
    assert re.search(r"function aggregateState\(entries,\s*\w+", PROVIDER_STATUS), \
        "the group gate does not reach the aggregate state"
    assert "groupEnabled" in PROVIDER_STATUS, \
        "the renderer never learns the group master state"
    assert "general" not in PROVIDER_STATUS.lower().replace("generation", ""), \
        "the status renderer names an integration family"


# --- F. AllDebrid Test relocation ---------------------------------------------

def test_the_alldebrid_test_action_exists_exactly_once_and_not_in_the_footer():
    assert SETTINGS.count('data-action="test-alldebrid"') == 1
    footer = SETTINGS[SETTINGS.index('class="dp-settings-master-footer"'):]
    footer = footer[:footer.index("</section>")]
    assert "test-alldebrid" not in footer, "the footer still owns the AllDebrid test"
    assert "Test AllDebrid" not in footer
    assert SETTINGS.count("testConnection('alldebrid'") == 1, \
        "a second AllDebrid connection test implementation exists"


def test_the_relocated_test_button_lives_in_the_alldebrid_body():
    panel = body(SETTINGS, "sourcesPanel")
    assert 'data-action="test-alldebrid"' in panel, \
        "the AllDebrid test control is not rendered inside the provider card body"


# --- G. protocol iconography ---------------------------------------------------

PROTOCOL_COLOURS = {"globe": "#3B82F6", "arrow-up-down": "#2DD4BF", "newspaper": "#CBD5E1"}


def test_the_lucide_protocol_assets_are_vendored_locally():
    for glyph in PROTOCOL_COLOURS:
        asset = STATIC / "icons" / "lucide" / f"{glyph}.svg"
        assert asset.exists(), f"{glyph}.svg is not vendored"
        markup = asset.read_text(encoding="utf-8")
        assert 'viewBox="0 0 24 24"' in markup
        assert PROTOCOL_COLOURS[glyph].lower() in markup.lower(), \
            f"{glyph}.svg does not carry its frozen protocol colour"
    assert "cdn" not in CARD_ICONS.lower()


def test_one_protocol_chip_primitive_carries_the_whole_treatment():
    """DP 1.0.13 Revision 3: the protocol identity is a CHIP, declared once.

    The previous shape glued the identity onto the naked inner-card icon
    rules, so it rendered as a free-floating glowing SVG with no container.
    The chip now owns its own geometry, border, surface and dimensional
    treatment -- and each protocol contributes nothing but a colour.
    """
    assert ".dp-settings-protocol-chip {" in CARD_ICONS
    assert ".dp-settings-protocol-icon" not in CARD_ICONS, "the superseded class survives"
    # It no longer borrows the naked inner-card icon's geometry or glow.
    for rule in re.findall(r"([^{}]*)\{[^{}]*\}", CARD_ICONS):
        if "dp-settings-inner-card-icon" in rule:
            assert "protocol" not in rule, f"the chip shares an owner with the naked icon: {rule.strip()[:80]}"

    chip = CARD_ICONS[CARD_ICONS.index(".dp-settings-protocol-chip {"):]
    chip = chip[:chip.index("}")]
    for property_ in ("border:", "border-radius:", "background:", "box-shadow:"):
        assert property_ in chip, f"the chip has no {property_.strip(':')}"
    assert "--dp-protocol-color" in chip, "the treatment is not derived from one datum"


def test_a_protocol_contributes_only_its_colour():
    """No six hand-maintained copies of the same border/surface/glow."""
    blocks = re.findall(r"\[data-protocol='[a-z_]+'\][^{]*\{([^}]*)\}", CARD_ICONS)
    assert len(blocks) >= 3, "the per-protocol colour data went missing"
    for block in blocks:
        declarations = {line.split(":")[0].strip() for line in block.split(";") if line.strip()}
        assert declarations == {"--dp-protocol-color"}, \
            f"a protocol block declares more than its colour: {sorted(declarations)}"
    for colour in PROTOCOL_COLOURS.values():
        assert colour in CARD_ICONS, f"{colour} has no canonical colour datum"


def test_the_chip_glow_is_declared_once_per_theme_not_per_protocol():
    glows = re.findall(r"([^{}]*protocol-chip img[^{}]*)\{([^{}]*)\}", CARD_ICONS)
    assert len(glows) == 2, f"the chip glyph glow is declared {len(glows)} times, expected dark + light"
    for selector, body in glows:
        assert "drop-shadow" in body
        assert "--dp-protocol-color" in body, "the glow does not derive from the protocol colour"


def test_every_protocol_identity_uses_the_same_block():
    """One composer emits every protocol identity, and it is the only emitter.

    Which six places render one is proven where it is visible, in
    frontend/browser/settings-protocol-icons.spec.js.
    """
    assert SETTINGS.count("data-protocol=") == 1, \
        "a protocol identity block is emitted somewhere other than protocolIcon()"
    assert SETTINGS.count('class="dp-settings-protocol-chip"') == 1
    table = SETTINGS[SETTINGS.index("const PROTOCOL_GLYPHS"):]
    table = table[:table.index("});")]
    for protocol, glyph in (("direct_sources", "globe"), ("general_http", "globe"),
                            ("general_ftp", "arrow-up-down"), ("usenet", "newspaper")):
        assert f"{protocol}: '{glyph}'" in table, f"{protocol} has no frozen glyph"
    # Six appearances, one composer: the definition plus five call sites.
    assert SETTINGS.count("protocolIcon(") == 6


# --- H. Downloads terminology --------------------------------------------------

def _without_comments(source: str) -> str:
    """Source with its comments removed.

    A comment is not operator-facing, and the invariant under test is that no
    visible alias survives -- not that the word never appears in prose.
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", source, flags=re.M)


def test_no_operator_facing_direct_transfers_or_direct_sources_title_remains():
    code = _without_comments(SETTINGS)
    for alias in ("Direct Transfers", "Direct Sources", "Global Sources"):
        assert alias not in code, f"visible alias {alias!r} survives"
    assert "executorTuningCard('direct', 'General Sources'" in code


def test_downloads_tuning_cards_carry_the_same_protocol_identity():
    """Downloads renders the identity through the SAME composer as Sources.

    That the two renderings are pixel-identical is proven in the browser;
    what is proven here is that there is no second Downloads-only identity.
    """
    assert "executorTuningCard('direct', 'General Sources'," in SETTINGS
    assert "directTransfersTuning(s), 'direct_sources')" in SETTINGS
    assert "usenetTuning(s), 'usenet')" in SETTINGS
    tuning = body(SETTINGS, "executorTuningCard")
    assert "protocolIcon(protocol)" in tuning
    assert "dp-settings-protocol-icon" not in tuning, \
        "Downloads builds its own identity markup instead of using the composer"
