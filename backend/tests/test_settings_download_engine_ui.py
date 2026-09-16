from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
SETTINGS_PAGE_JS = STATIC / "ui-settings-page.js"
SETTINGS_PAGE_CSS = STATIC / "ui-settings-page.css"
SETTINGS_CHROME_CSS = STATIC / "ui-settings-chrome.css"
DOWNLOAD_ENGINE_ICON = STATIC / "icons" / "dp" / "download-engine.svg"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def downloads_runtime() -> str:
    runtime = source(SETTINGS_PAGE_JS)
    return runtime[runtime.index("function downloadsPanel"):runtime.index("function extractionPanel")]


def test_download_engine_header_matches_reviewed_identity_mode_and_copy_contract():
    downloads = downloads_runtime()
    css = source(SETTINGS_PAGE_CSS)
    chrome = source(SETTINGS_CHROME_CSS)

    assert "card('Download Engine'" in downloads
    assert "aria2 Delivery" not in downloads
    assert "dp-settings-download-engine-icon" in downloads
    assert 'src="/icons/dp/download-engine.svg?v=1"' in downloads
    assert "headerCenter:" in downloads
    assert "dp-settings-download-engine-header-copy" in downloads
    assert "Choose where DebridPulse sends downloads. Built-in aria2 runs with DebridPulse; External aria2 uses your existing aria2 server." in downloads
    assert "dp-settings-download-engine-copy" not in downloads
    assert "'Mode Selection'" in downloads
    assert "['builtin', 'Built-in aria2']" in downloads
    assert "['external', 'External aria2']" in downloads

    header = css.split(".dp-settings-download-engine-card > .card-header {", 1)[1].split("}", 1)[0]
    assert "display: grid;" in header
    assert "minmax(420px, 900px)" in header
    assert ".dp-settings-card-header-center" in css
    assert "text-align: center;" in css

    assert ".dp-settings-download-engine-icon img" in chrome
    icon_rule = chrome.split(".dp-settings-download-engine-icon img {", 1)[1].split("}", 1)[0]
    assert "width: 34px;" in icon_rule
    assert "height: 34px;" in icon_rule
    assert icon_rule.count("drop-shadow") == 2
    light_icon = chrome.split("body.light.dp-v11-structural #view-settings .dp-settings-download-engine-icon img {", 1)[1].split("}", 1)[0]
    assert light_icon.count("drop-shadow") == 2


def test_download_engine_mode_switch_preserves_contextual_paths_and_builtin_tuning():
    runtime = source(SETTINGS_PAGE_JS)
    downloads = downloads_runtime()

    assert 'data-download-path-mode="builtin"' in downloads
    assert 'data-download-path-mode="external"' in downloads
    assert "Where DebridPulse saves downloads." in downloads
    assert "Path your external aria2 server uses for the shared download folder on that server." in downloads
    assert "Maximum number of downloads DebridPulse can run at the same time." in downloads
    assert "el.dataset.downloadPathMode !== mode" in runtime
    assert "el.hidden = mode !== 'external';" in runtime
    assert "el.hidden = mode !== 'builtin';" in runtime

    css = source(SETTINGS_PAGE_CSS)
    row = css.split(".dp-settings-download-engine-row {", 1)[1].split("}", 1)[0]
    assert "grid-template-columns: minmax(0, 1fr) minmax(280px, 320px);" in row
    assert "gap: 32px;" in row
    assert (
        "body.dp-v11-structural #view-settings .dp-settings-download-limit {\n"
        "  width: 100%;\n"
        "  max-width: 320px;\n"
        "  justify-self: end;\n"
        "}"
    ) in css


def test_external_mode_uses_one_connection_row_and_alldebrid_style_secret_semantics():
    runtime = source(SETTINGS_PAGE_JS)
    downloads = downloads_runtime()
    css = source(SETTINGS_PAGE_CSS)
    secret_helper = runtime[
        runtime.index("function aria2RpcSecretFields"):
        runtime.index("function tuningToggle")
    ]

    assert "dp-settings-external-connection-row" in downloads
    assert "External RPC URL" in downloads
    assert "JSON-RPC endpoint DebridPulse uses to connect to your external aria2 server." in downloads
    assert "${aria2RpcSecretFields(!!s.aria2_secret_configured)}" in downloads
    assert "aria2 RPC Secret" in secret_helper
    assert "••••••••••••••••" in secret_helper
    assert "Secret Present" in secret_helper
    assert "Enter a new RPC secret to replace the stored secret when you click Apply Settings. Leave this field blank to keep the current secret." in secret_helper
    assert "Enter the RPC secret used by your external aria2 server. It will be saved only when you click Apply Settings." in secret_helper
    assert "Clear stored aria2 RPC Secret" in secret_helper
    assert "Remove the saved RPC secret when you click Apply Settings." in secret_helper
    assert 'data-clear-secret="${key}"' in secret_helper

    row = css.split(".dp-settings-external-connection-row.is-secret-configured {", 1)[1].split("}", 1)[0]
    assert "minmax(0, 1.7fr)" in row
    assert "minmax(260px, .65fr)" in row
    assert "minmax(280px, .65fr)" in row
    assert ".dp-settings-aria2-secret-meta" in css
    assert ".dp-settings-clear-secret--aria2" in css
    assert "justify-content: flex-end;" in css


def test_builtin_mode_contains_collapsed_additional_engine_tuning_with_reviewed_layout_and_copy():
    downloads = downloads_runtime()
    css = source(SETTINGS_PAGE_CSS)

    assert '<details class="dp-settings-additional dp-settings-engine-tuning">' in downloads
    assert '<summary><span>Additional Engine Tuning</span></summary>' in downloads
    assert "dp-settings-engine-tuning-grid" in downloads
    assert "dp-settings-engine-file-allocation" in downloads

    required_copy = (
        "Stops a slow HTTP/HTTPS/FTP connection when its speed falls at or below this value. Set to 0 to disable the limit.",
        "Resume existing partial files when possible instead of restarting them from the beginning.",
        "Controls how many parallel segments aria2 can use for a single file. Actual connections may be limited by the server and split-size settings.",
        "Maximum number of connections a single download can open to the same server.",
        "Controls how small file sections can become when aria2 splits a download. Larger values create fewer parallel segments.",
        "Amount of memory aria2 can use as a shared download cache to reduce disk I/O. Set to 0 to disable the cache.",
        "Controls how aria2 prepares disk space for new files.",
    )
    for text in required_copy:
        assert text in downloads

    # Specification section 9.9: per-job fields (apply to every download
    # regardless of built-in/external mode, per
    # Aria2Executor._options()) come first and are NEVER inside the
    # builtin-only-gated wrapper; built-in daemon/process-only fields
    # (only consumed by the built-in process's own global options) are
    # inside it.
    per_job_order = [
        downloads.index("'aria2_continue_downloads'"),
        downloads.index("'aria2_split'"),
        downloads.index("'aria2_max_connection_per_server'"),
        downloads.index("'aria2_min_split_size'"),
    ]
    assert per_job_order == sorted(per_job_order)
    builtin_only_marker = downloads.index("data-builtin-only-tuning")
    assert max(per_job_order) < builtin_only_marker
    builtin_only_order = [
        downloads.index("'aria2_lowest_speed_limit'"),
        downloads.index("'aria2_disk_cache'"),
        downloads.index("'aria2_file_allocation'"),
    ]
    assert builtin_only_order == sorted(builtin_only_order)
    assert builtin_only_marker < min(builtin_only_order)

    assert "['trunc', 'Truncate']" in downloads
    assert "['falloc', 'Fallocate']" in downloads
    assert "['prealloc', 'Preallocate']" in downloads
    assert "['none', 'None']" in downloads

    grid = css.split(".dp-settings-engine-tuning-grid {", 1)[1].split("}", 1)[0]
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in grid
    assert "gap: 16px 28px;" in grid
    allocation = css.split(".dp-settings-engine-file-allocation {", 1)[1].split("}", 1)[0]
    assert "width: min(100%, 520px);" in allocation
    assert "margin: 20px auto 0;" in allocation


def test_external_mode_keeps_per_job_tuning_visible_and_hides_only_builtin_only_tuning():
    """Gate 9 revision-3 rejection finding 5, specification section 9.9: the
    Settings UI previously hid the WHOLE "Additional Engine Tuning" section
    (including per-job continue/split/max-connection-per-server/min-split-
    size, which ``Aria2Executor._options()`` applies to every job regardless
    of mode) merely because external daemon-global mutation is read-only.
    Only the built-in daemon/process-only subsection (lowest-speed-limit,
    disk-cache, file-allocation -- consumed solely by
    ``executors/aria2/runtime.py``'s built-in process global-options
    builder) may be mode-gated."""
    downloads = downloads_runtime()
    runtime = source(SETTINGS_PAGE_JS)

    # The outer <details> itself is never mode-gated any more.
    assert '<details class="dp-settings-additional dp-settings-engine-tuning">' in downloads
    assert "${builtIn ? '' : 'hidden'}>\n        <summary><span>Additional Engine Tuning" not in downloads

    # Only the builtin-only wrapper carries the mode-conditional `hidden`.
    assert 'data-builtin-only-tuning ${builtIn ? \'\' : \'hidden\'}' in downloads

    # Per-job fields are declared OUTSIDE (before) that wrapper's opening tag.
    wrapper_open = downloads.index('data-builtin-only-tuning')
    for key in ("'aria2_continue_downloads'", "'aria2_split'", "'aria2_max_connection_per_server'", "'aria2_min_split_size'"):
        assert downloads.index(key) < wrapper_open
    for key in ("'aria2_lowest_speed_limit'", "'aria2_disk_cache'", "'aria2_file_allocation'"):
        assert downloads.index(key) > wrapper_open

    # The runtime mode-switch handler must only toggle the builtin-only
    # wrapper, never the whole engine-tuning section.
    handler = runtime[runtime.index("function updateModeState"):runtime.index("function fieldFor")]
    assert "[data-builtin-only-tuning]" in handler
    assert "querySelectorAll('.dp-settings-engine-tuning')" not in handler


def test_advanced_tab_and_old_transfer_tuning_card_are_removed_after_migration():
    runtime = source(SETTINGS_PAGE_JS)
    downloads = downloads_runtime()

    assert "['advanced', 'Advanced', 'sliders-horizontal']" not in runtime
    assert "function advancedPanel" not in runtime
    assert "panel('advanced'" not in runtime
    assert "aria2 Transfer Tuning" not in runtime

    for key in (
        "aria2_split",
        "aria2_min_split_size",
        "aria2_max_connection_per_server",
        "aria2_disk_cache",
        "aria2_file_allocation",
        "aria2_lowest_speed_limit",
        "aria2_continue_downloads",
    ):
        assert key in downloads


def test_download_engine_icon_is_pure_vector_svg():
    raw = source(DOWNLOAD_ENGINE_ICON)
    assert "<svg" in raw
    assert 'viewBox="0 0 2048 2048"' in raw
    assert raw.count("<path") > 10
    assert "<image" not in raw.lower()
    assert "data:image" not in raw.lower()
