"""Contract coverage for the final v1.0.11 Downloads Settings completion pass."""

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RECOVERY_ICON = STATIC / "icons" / "dp" / "download-safety-recovery.svg"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_completion_assets_are_loaded_in_deterministic_order():
    loader = read("ui-presentation-loader.js")
    assert "/ui-settings-downloads-completion.css?v=4" in loader
    assert "/ui-settings-page.js?v=4" in loader
    assert "/ui-settings-downloads-completion.js?v=4" in loader
    assert loader.index("/ui-settings-page.js?v=4") < loader.index("/ui-settings-downloads-completion.js?v=4")


def test_configured_secret_mask_is_fixed_and_tripled_without_secret_length_leakage():
    runtime = read("ui-settings-downloads-completion.js")
    match = re.search(r"CONFIGURED_SECRET_MASK = '([^']+)'", runtime)
    assert match is not None
    assert match.group(1) == "•" * 48
    assert "placeholder && /^•+$/u.test(placeholder)" in runtime
    assert "input[type=\"password\"]" in runtime


def test_external_rpc_clear_secret_is_adjacent_to_copy_and_connection_band_is_rebalanced():
    page = read("ui-settings-page.js")
    css = read("ui-settings-downloads-completion.css")

    assert "Clear stored aria2 RPC Secret" in page
    assert "Remove the saved RPC secret when you click Apply Settings." in page
    assert "grid-template-columns: minmax(0, 1.35fr) minmax(300px, .9fr) minmax(320px, .75fr);" in css
    assert ".dp-settings-external-connection-row .dp-settings-clear-secret--aria2" in css
    assert "grid-template-columns: fit-content(340px) auto;" in css
    assert ".dp-settings-clear-secret--aria2 > .form-label" in css
    assert ".dp-settings-clear-secret--aria2 > small" in css
    assert ".dp-settings-clear-secret--aria2 > .dp-settings-clear-secret-control" in css
    assert "grid-row: 1 / 3;" in css
    assert "column-gap: 14px;" in css


def test_continue_partial_uses_copy_block_with_adjacent_centered_toggle_and_file_allocation_stays_grouped():
    css = read("ui-settings-downloads-completion.css")

    assert ".dp-settings-engine-tuning-toggle-field" in css
    assert "grid-template-columns: fit-content(620px) auto;" in css
    assert "grid-template-rows: auto auto;" in css
    assert ".dp-settings-engine-tuning-toggle-field > .form-label" in css
    assert ".dp-settings-engine-tuning-toggle-field > .form-hint" in css
    assert ".dp-settings-engine-tuning-toggle-field > .dp-settings-engine-tuning-toggle-control" in css
    assert "grid-row: 1 / 3;" in css
    assert "align-self: center;" in css

    assert ".dp-settings-engine-file-allocation" in css
    assert "width: min(100%, 680px);" in css
    assert "grid-template-columns: fit-content(380px) minmax(180px, 220px);" in css
    assert ".dp-settings-engine-file-allocation > .dp-settings-field > .dp-dropdown-shell" in css


def test_download_safety_recovery_has_vector_header_artwork_and_established_glow():
    runtime = read("ui-settings-downloads-completion.js")
    css = read("ui-settings-downloads-completion.css")
    icon = RECOVERY_ICON.read_text(encoding="utf-8")

    assert "ensureRecoveryIdentity" in runtime
    assert "dp-settings-download-recovery-icon" in runtime
    assert "/icons/dp/download-safety-recovery.svg?v=1" in runtime
    assert "titleNode.prepend(icon);" in runtime

    assert ".dp-settings-download-recovery-icon" in css
    assert ".dp-settings-download-recovery-icon img" in css
    assert "width: 34px;" in css
    assert "height: 34px;" in css
    assert "drop-shadow(0 0 4px rgba(184,102,245,.78))" in css
    assert "drop-shadow(0 0 9px rgba(184,102,245,.34))" in css
    assert "body.light.dp-v11-structural #view-settings .dp-settings-download-recovery-icon img" in css
    assert "drop-shadow(0 0 10px rgba(184,102,245,.44))" in css

    assert 'viewBox="0 0 256 256"' in icon
    assert icon.count("<path") >= 10
    assert "<linearGradient" in icon
    assert "<image" not in icon.lower()
    assert "data:image" not in icon.lower()


def test_file_filters_are_retired_from_presentation_without_destructive_ui_pass_rewrite():
    runtime = read("ui-settings-downloads-completion.js")
    css = read("ui-settings-downloads-completion.css")
    page = read("ui-settings-page.js")

    assert "cardByTitle(panel, 'File Filters')" in runtime
    assert "dp-settings-file-filters-retired" in runtime
    assert ".dp-settings-file-filters-retired" in css
    assert "display: none !important;" in css

    # This is intentionally a UI-only retirement. Keeping the legacy controls
    # rendered but hidden preserves their loaded values when Apply Settings is
    # used; physical config/backend pruning happens in the later cleanup pass.
    assert "filters_enabled" in page
    assert "blocked_extensions" in page
    assert "torrent_labels_raw" in page


def test_safety_recovery_copy_uses_user_facing_titles_and_explanations():
    runtime = read("ui-settings-downloads-completion.js")
    expected = (
        "Minimum Free Disk Space (GB)",
        "Stops new downloads from starting when free disk space falls below this amount. Set to 0 to disable the disk-space guard.",
        "Resume Free Space Buffer (GB)",
        "Extra free space required above the minimum before DebridPulse starts downloads again. Helps prevent repeated stop/start behavior near the limit.",
        "Stalled Download Timeout (hours)",
        "How long a download can remain stalled before DebridPulse attempts automatic recovery. Set to 0 to disable stalled-download recovery.",
        "Download Error Retries",
        "How many times DebridPulse retries a download after aria2 reports an error. Set to 0 to disable automatic retries.",
        "Retry Delay (seconds)",
        "How long DebridPulse waits before retrying a download after an aria2 error. Set to 0 to retry immediately.",
    )
    for text in expected:
        assert text in runtime


def test_safety_recovery_is_equal_width_three_over_two_inverted_pyramid():
    css = read("ui-settings-downloads-completion.css")

    assert ".dp-settings-download-recovery-card > .card-body" in css
    assert "grid-template-columns: repeat(6, minmax(0, 1fr));" in css
    assert "grid-column: span 2;" in css
    assert ".dp-settings-download-recovery-card > .card-body > .dp-settings-field:nth-child(4)" in css
    assert "grid-column: 2 / span 2;" in css
    assert ".dp-settings-download-recovery-card > .card-body > .dp-settings-field:nth-child(5)" in css
    assert "grid-column: 4 / span 2;" in css


def test_alldebrid_additional_settings_matches_three_over_two_inverted_pyramid():
    css = read("ui-settings-downloads-completion.css")
    page = read("ui-settings-page.js")

    assert "Additional Settings" in page
    for key in (
        "alldebrid_rate_limit_per_minute",
        "poll_interval_seconds",
        "full_sync_interval_minutes",
        "upload_fail_retry_count",
        "upload_fail_retry_delay_minutes",
    ):
        assert key in page

    selector = '[data-panel="sources"] .dp-settings-provider-card--alldebrid .dp-settings-additional-body'
    assert selector in css
    assert f"{selector} > .dp-settings-field:nth-child(4)" in css
    assert f"{selector} > .dp-settings-field:nth-child(5)" in css
    assert "grid-template-columns: repeat(6, minmax(0, 1fr));" in css
    assert "grid-column: 2 / span 2;" in css
    assert "grid-column: 4 / span 2;" in css


def test_sources_and_downloads_shift_complete_copy_blocks_not_first_lines():
    css = read("ui-settings-downloads-completion.css")

    assert '[data-panel="sources"] .dp-settings-field > .form-label' in css
    assert '[data-panel="downloads"] .dp-settings-field > .form-label' in css
    assert '[data-panel="sources"] .dp-settings-alldebrid-key-meta > .form-hint:first-child' in css
    assert '[data-panel="downloads"] .dp-settings-aria2-secret-meta > .form-hint:first-child' in css
    assert "position: relative;" in css
    assert "inset-inline-start: 3px;" in css
    assert "padding-inline-start: 6px;" not in css


def test_completion_runtime_is_idempotently_bound_and_suppresses_its_own_mutations():
    runtime = read("ui-settings-downloads-completion.js")

    assert "dpSettingsDownloadsCompletionBound" in runtime
    assert "if (view.dataset.dpSettingsDownloadsCompletionBound === '1')" in runtime
    assert "let observer = null;" in runtime
    assert "if (observer) observer.disconnect();" in runtime
    assert "function applyWithoutSelfObservation()" in runtime
    assert "observer.observe(view, {childList: true, subtree: true});" in runtime
    assert "applyWithoutSelfObservation();" in runtime
    assert "addEventListener('click'" not in runtime
    assert "addEventListener('change'" not in runtime
