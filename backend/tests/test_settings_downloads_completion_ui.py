"""Contract coverage for the final v1.0.11 Downloads Settings completion pass."""

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RECOVERY_ICON = STATIC / "icons" / "dp" / "download-safety-recovery.svg"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")




def test_configured_secret_mask_is_fixed_and_tripled_without_secret_length_leakage():
    runtime = read("ui-settings-page.js")
    # One mask, emitted by the owner directly into every configured-secret field.
    assert "const CONFIGURED_SECRET_MASK = '•'.repeat(48);" in runtime
    # The AllDebrid row reaches it through its one placeholder declaration,
    # which both the render and the convergence path use.
    assert "ALLDEBRID_KEY_PLACEHOLDER = configured =>" in runtime
    assert runtime.count("CONFIGURED_SECRET_MASK : 'Your AllDebrid API key'") == 1
    assert "•••••" not in runtime


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
    runtime = read("ui-settings-page.js")
    css = read("ui-settings-card-icons.css")
    icon = RECOVERY_ICON.read_text(encoding="utf-8")

    # The card's icon is part of its emitted title (CARD_ICONS); nothing prepends it later.
    assert "'Download Safety & Recovery': ['downloads', '/icons/dp/settings/download-safety-recovery.svg?v=1']" in runtime
    assert "ensureRecoveryIdentity" not in runtime and "dp-settings-download-recovery-icon" not in runtime
    assert ".dp-settings-inner-card-icon" in css
    assert "width: 34px;" in css
    assert "height: 34px;" in css

    assert 'viewBox="0 0 256 256"' in icon
    assert icon.count("<path") >= 10
    assert "<linearGradient" in icon
    assert "<image" not in icon.lower()
    assert "data:image" not in icon.lower()


def test_file_filters_are_physically_retired_from_active_settings_runtime():
    css = read("ui-settings-downloads-completion.css")
    page = read("ui-settings-page.js")

    assert "File Filters" not in page
    for key in (
        "filters_enabled",
        "blocked_extensions",
        "blocked_keywords",
        "min_file_size_mb",
        "block_samples",
        "block_extras",
        "torrent_labels_raw",
    ):
        assert key not in page

    assert "dp-settings-file-filters-retired" not in page
    assert ".dp-settings-file-filters-retired" not in css


def test_safety_recovery_copy_uses_user_facing_titles_and_explanations():
    runtime = read("ui-settings-page.js")
    expected = (
        "Minimum Free Disk Space (GB)",
        "Stops new downloads from starting when free disk space falls below this amount. Set to 0 to disable the disk-space guard.",
        "Resume Free Space Buffer (GB)",
        "Extra free space required above the minimum before DebridPulse starts downloads again. Helps prevent repeated stop/start behavior near the limit.",
        "Stalled Download Timeout (hours)",
        "How long a download can remain stalled before DebridPulse attempts automatic recovery. Set to 0 to disable stalled-download recovery.",
        "Download Error Retries",
        "How many times DebridPulse retries a download after an error. Set to 0 to disable automatic retries.",
        "Retry Delay (seconds)",
        "How long DebridPulse waits before retrying a download after an error. Set to 0 to retry immediately.",
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


def test_sources_and_downloads_use_the_one_canonical_field_datum():
    """DP 1.0.13 work item J: no panel carries its own label/hint offset.

    This layer previously declared the shared "3px content datum" for Sources,
    Downloads and Extraction; five other files declared their own copies, and
    the Usenet server cards received none, which is exactly why their labels
    read as sitting left of every other Settings label. One owner now.
    """
    css = read("ui-settings-downloads-completion.css")
    assert "inset-inline-start" not in css
    form_layout = read("ui-settings-form-layout.css")
    assert "#view-settings .dp-settings-field > .form-label" in form_layout
    assert "#view-settings .dp-usenet-field > .form-label" in form_layout
    assert "margin-inline-start: 0" in form_layout
    assert "padding-inline-start: 0" in form_layout
