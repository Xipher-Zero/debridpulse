"""Contract coverage for the final v1.0.11 Downloads Settings completion pass."""

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_completion_assets_are_loaded_in_deterministic_order():
    loader = read("ui-presentation-loader.js")
    assert "/ui-settings-downloads-completion.css?v=2" in loader
    assert "/ui-settings-page.js?v=4" in loader
    assert "/ui-settings-downloads-completion.js?v=1" in loader
    assert loader.index("/ui-settings-page.js?v=4") < loader.index("/ui-settings-downloads-completion.js?v=1")


def test_configured_secret_mask_is_fixed_and_tripled_without_secret_length_leakage():
    runtime = read("ui-settings-downloads-completion.js")
    match = re.search(r"CONFIGURED_SECRET_MASK = '([^']+)'", runtime)
    assert match is not None
    assert match.group(1) == "•" * 48
    assert "placeholder && /^•+$/u.test(placeholder)" in runtime
    assert "input[type=\"password\"]" in runtime


def test_external_rpc_clear_secret_is_one_compact_centered_control_block():
    page = read("ui-settings-page.js")
    css = read("ui-settings-downloads-completion.css")

    assert "Clear stored aria2 RPC Secret" in page
    assert "Remove the saved RPC secret when you click Apply Settings." in page
    assert "flex-direction: column;" in css
    assert ".dp-settings-clear-secret--aria2 > .dp-settings-clear-secret-control" in css
    assert "justify-content: center;" in css
    assert ".dp-settings-clear-secret--aria2 > small" in css
    assert "text-align: center;" in css


def test_continue_partial_uses_title_control_helper_sandwich_and_file_allocation_stays_grouped():
    css = read("ui-settings-downloads-completion.css")

    assert ".dp-settings-engine-tuning-toggle-field" in css
    assert "grid-template-columns: minmax(0, 1fr);" in css
    assert "grid-template-rows: auto auto auto;" in css
    assert ".dp-settings-engine-tuning-toggle-field > .dp-settings-engine-tuning-toggle-control" in css
    assert "grid-row: 2;" in css
    assert ".dp-settings-engine-tuning-toggle-field > .form-hint" in css
    assert "grid-row: 3;" in css
    assert "fit-content(620px) auto" not in css

    assert ".dp-settings-engine-file-allocation" in css
    assert "width: min(100%, 680px);" in css
    assert "grid-template-columns: fit-content(380px) minmax(180px, 220px);" in css
    assert ".dp-settings-engine-file-allocation > .dp-settings-field > .dp-dropdown-shell" in css


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


def test_completion_runtime_is_idempotently_bound_to_the_persistent_settings_root():
    runtime = read("ui-settings-downloads-completion.js")

    assert "dpSettingsDownloadsCompletionBound" in runtime
    assert "if (view.dataset.dpSettingsDownloadsCompletionBound === '1')" in runtime
    assert "observer.observe(view, {childList: true, subtree: true});" in runtime
    assert "addEventListener('click'" not in runtime
    assert "addEventListener('change'" not in runtime
