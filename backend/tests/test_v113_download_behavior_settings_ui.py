"""Downloads -> Download Behavior & Limits -> Advanced Settings (1.0.13
continuation, private-LAN and global provider-resolution policy): one card,
one canonical disclosure, ordinary tuning cells, canonical field persistence,
projected dependency."""
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
    row, advanced = panel.index("dp-settings-download-engine-row"), panel.index("downloadBehaviorAdvanced(policy, s)")
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
    # The two continuation numbers stay ungrouped; the local-network pair, the
    # two provider intervals and the resolution retry pair are each one group.
    assert advanced.count("tuningGroup(") == 3
    group = advanced[advanced.index("tuningGroup("):]
    assert group.index("'private_lan_connections'") < group.index("'skip_private_lan_confirmation'")
    before = advanced[:advanced.index("tuningGroup(")]
    assert "'material_checkpoint_interval_seconds'" in before and "'graceful_stop_timeout_seconds'" in before
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
    # It READS what the Local Network switch shows (Settings Batch 1, Item 2:
    # the fade starts on intent), and never sets a checked state or writes.
    assert ".checked =" not in projection and "request(" not in projection  # never erases or writes
    assert "control.disabled = !(accepted && shown);" in projection
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


def test_subsection_has_ordinary_spacing_and_no_divider():
    """DP 1.0.13 cleanup: island -> ordinary section spacing -> Advanced
    Settings, through the one canonical subsection owner: no empty band above
    the header and no divider beneath it."""
    section = CSS[CSS.index("#view-settings .dp-settings-subsection {"):]
    assert "margin-top: 14px" in section[:section.index("}")]
    header = CSS[CSS.index("#view-settings .dp-settings-subsection-header {"):]
    header = header[:header.index("}")]
    assert "border" not in header and "padding-bottom" not in header
    assert "download-engine-card .dp-settings-subsection" not in CSS


def test_group_cards_share_the_one_sibling_card_gap():
    """Transfer Method Settings is a group card; it keeps the same 12px gap from
    Disk Space & Recovery as every other top-level Settings card, from the one
    gap owner rather than a margin on either card."""
    owner = CSS[CSS.index("#view-settings .dp-settings-card,\n#view-settings .dp-settings-group-card {"):]
    assert "margin: 0 0 12px" in owner[:owner.index("}")]
    assert "#view-settings .dp-settings-group-card:last-child {" in CSS
    assert ".dp-settings-source-group + .dp-settings-source-group" not in CSS
    for card in ("dp-settings-download-recovery-card", "dp-executor-tuning-group"):
        assert not re.search(rf"\.{card}\s*\{{[^}}]*(margin|padding-bottom)", CSS), card


GLOBAL = (("poll_interval_seconds", "Provider Poll Interval (seconds)", "provider_poll_interval_seconds", 30, 5, 3600),
          ("full_sync_interval_minutes", "Full Sync Interval (minutes)", "full_sync_interval_minutes", 5, 0, 1440),
          ("upload_fail_retry_count", "Resolution Failure Retries", "resolution_retry_count", 3, 0, 20),
          ("upload_fail_retry_delay_minutes", "Retry Delay (minutes)", "resolution_retry_delay_minutes", 5, 0, 1440))


def test_the_global_provider_controls_live_here_with_their_keys_defaults_and_bounds():
    """DP 1.0.13 final settings cleanup: the provider intervals and resolution
    retries apply to every service, so they moved off the AllDebrid card into
    this disclosure -- same controls, keys, defaults, bounds and commit scopes."""
    advanced = _function("downloadBehaviorAdvanced")
    for key, label, option, default, low, high in GLOBAL:
        assert len(re.findall(rf"input\('{key}'", PAGE)) == 1, key
        cell = advanced[advanced.index(f"input('{key}', '{label}', "):]
        cell = cell[:cell.index("}),")]
        assert f"?? {default}, {{" in cell and f"min: {low}, max: {high}," in cell, key
        scope = "settings-document" if key == "full_sync_interval_minutes" else "transfer-policy"
        assert re.search(rf"    {key}: \{{scope: '{scope}', option: '{option}'\}}", PAGE), key
    # After the local-network pair: the two intervals as one group, then the
    # retry count and its delay as one group.
    starts = [m.start() for m in re.finditer(r"tuningGroup\(", advanced)] + [len(advanced)]
    intervals, retries = advanced[starts[1]:starts[2]], advanced[starts[2]:starts[3]]
    assert intervals.index("'poll_interval_seconds'") < intervals.index("'full_sync_interval_minutes'")
    assert retries.index("'upload_fail_retry_count'") < retries.index("'upload_fail_retry_delay_minutes'")
    assert "Upload Failure Retries" not in PAGE
    alldebrid = PAGE[PAGE.index("providerCard('alldebrid'"):]
    alldebrid = alldebrid[:alldebrid.index("          )}")]
    for key, *_ in GLOBAL:
        assert f"'{key}'" not in alldebrid, key


def test_the_relocated_hints_name_no_one_provider_and_no_upload():
    advanced = _function("downloadBehaviorAdvanced")
    tail = advanced[advanced.index("input('poll_interval_seconds'"):]
    assert "AllDebrid" not in tail and "upload" not in tail.replace("upload_fail_retry", "").lower()
    assert tail.count("Network Sources") == 1
