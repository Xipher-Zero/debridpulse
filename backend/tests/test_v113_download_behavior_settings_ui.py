"""Downloads -> Download Behavior & Limits -> Advanced Settings (1.0.13
continuation + private-LAN policy): one card, one canonical disclosure, four
ordinary tuning cells, canonical field persistence, projected dependency."""
from __future__ import annotations

import re
from pathlib import Path

from transfers.settings import TransferSettings

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"
PAGE = (STATIC / "ui-settings-page.js").read_text()
CSS = (STATIC / "ui-settings-page.css").read_text()
KEYS = ("material_checkpoint_interval_seconds", "graceful_stop_timeout_seconds",
        "private_lan_connections", "skip_private_lan_confirmation")


def _function(name: str) -> str:
    start = PAGE.index(f"function {name}(")
    following = re.compile(r"\n  (?:/\*|function |const |async function )").search(PAGE, start + 10)
    return PAGE[start:following.start() if following else len(PAGE)]


def test_card_is_renamed_and_hosts_the_advanced_subsection_below_its_primary_row():
    panel = _function("downloadsPanel")
    assert "card('Download Behavior & Limits'" in panel and "Download Location & Limits" not in PAGE
    row, advanced = panel.index("dp-settings-download-engine-row"), panel.index("downloadBehaviorAdvanced(policy)")
    assert row < advanced < panel.index("className: 'dp-settings-download-engine-card'")


def test_advanced_settings_is_the_canonical_collapsed_disclosure_with_an_adjacent_chevron():
    advanced = _function("downloadBehaviorAdvanced")
    # The one subsection owner, collapsed by default (no expanded argument).
    assert re.search(r"disclosureSection\('Advanced Settings', 'download-behavior-advanced', tuningCells\(", advanced)
    assert not re.search(r"disclosureSection\([^)]*,\s*true\)", advanced)
    section = _function("disclosureSection")
    assert section.index("dp-settings-subsection-title") < section.index("settingsDisclosure(bodyId")
    header = CSS[CSS.index("#view-settings .dp-settings-subsection-header {"):]
    header = header[:header.index("}")]
    assert "display: flex" in header and "space-between" not in header and "margin-left: auto" not in header


def test_four_separate_cells_in_the_one_tuning_grammar():
    advanced = _function("downloadBehaviorAdvanced")
    assert [re.search(rf"(input|tuningToggle)\('{key}'", advanced).group(1) for key in KEYS] == [
        "input", "input", "tuningToggle", "tuningToggle"]
    assert "tuningGroup(" not in advanced  # four independent cards, not one combined LAN card
    for label in ("Material Checkpoint Interval", "Graceful Stop Timeout", "Local Network Connections",
                  "Skip Local Connection Confirmation"):
        assert label in advanced
    # No second grid, matrix or card style.
    assert "grid-template-columns" not in advanced and "style=" not in advanced
    assert "download-behavior-advanced" not in CSS
    for jargon in ("writer generation", "material generation", "fdatasync", "fencing", "bitmap", "geometry"):
        assert jargon not in advanced.lower()


def test_canonical_field_persistence_one_owner_per_field():
    declarations = {key: re.search(rf"    {key}: \{{([^}}]*)\}}", PAGE).group(1) for key in KEYS}
    for key, declaration in declarations.items():
        assert "scope: 'transfer-policy'" in declaration and f"option: '{key}'" in declaration
    assert "commit: 'immediate'" not in declarations["material_checkpoint_interval_seconds"]
    assert "commit: 'immediate'" not in declarations["graceful_stop_timeout_seconds"]
    assert "commit: 'immediate'" in declarations["private_lan_connections"]
    assert "commit: 'immediate'" in declarations["skip_private_lan_confirmation"]
    for key in KEYS:  # one control each, and no custom fetch path for these cards
        assert len(re.findall(rf"(?:input|tuningToggle)\('{key}'", PAGE)) == 1
        assert not re.search(rf"(?:request|fetch|api)\([^)]*{key}", PAGE)


def test_skip_confirmation_is_a_projection_of_accepted_policy_and_keeps_its_value():
    advanced = _function("downloadBehaviorAdvanced")
    assert "{disabled: !policy.private_lan_connections}" in advanced
    assert "!!policy.skip_private_lan_confirmation" in advanced  # rendered from its own stored value
    projection = _function("projectPrivateLanDependency")
    assert "policyOf(state.settings).private_lan_connections" in projection
    assert ".checked" not in projection and "request(" not in projection  # never erases or writes
    assert "projectPrivateLanDependency();" in _function("adoptTransferPolicy")
    assert "#view-settings .dp-settings-field.is-disabled" in CSS


def test_policy_owner_defaults_and_bounds():
    defaults = TransferSettings()
    assert (defaults.material_checkpoint_interval_seconds, defaults.graceful_stop_timeout_seconds) == (5, 10)
    assert defaults.private_lan_connections is False and defaults.skip_private_lan_confirmation is False
    fields = TransferSettings.model_fields
    for key in ("material_checkpoint_interval_seconds", "graceful_stop_timeout_seconds"):
        bounds = {type(item).__name__: item for item in fields[key].metadata}
        assert bounds["Ge"].ge == 1 and bounds["Le"].le == 60
