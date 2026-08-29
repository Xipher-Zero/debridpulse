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
    limit = css.split(".dp-settings-download-limit {", 1)[1].split("}", 1)[0]
    assert "max-width: 320px;" in limit
    assert "justify-self: end;" in limit


def test_external_mode_uses_one_connection_row_and_alldebrid_style_secret_semantics():
    downloads = downloads_runtime()
    css = source(SETTINGS_PAGE_CSS)

    assert "dp-settings-external-connection-row" in downloads
    assert "External RPC URL" in downloads
    assert "JSON-RPC endpoint DebridPulse uses to connect to your external aria2 server." in downloads
    assert "aria2 RPC Secret" in downloads
    assert "••••••••••••••••" in downloads
    assert "Secret Present" in downloads
    assert "Enter a new RPC secret to replace the stored secret when you click Apply Settings. Leave this field blank to keep the current secret." in downloads
    assert "Enter the RPC secret used by your external aria2 server. It will be saved only when you click Apply Settings." in downloads
    assert "Clear stored aria2 RPC Secret" in downloads
    assert "Remove the saved RPC secret when you click Apply Settings." in downloads
    assert 'data-clear-secret="${key}"' in downloads

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

    assert '<details class="dp-settings-additional dp-settings-engine-tuning"' in downloads
    assert '<details class="dp-settings-additional dp-settings-engine-tuning" open' not in downloads
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

    order = [
        downloads.index("'aria2_lowest_speed_limit'"),
        downloads.index("'aria2_continue_downloads'"),
        downloads.index("'aria2_split'"),
        downloads.index("'aria2_max_connection_per_server'"),
        downloads.index("'aria2_min_split_size'"),
        downloads.index("'aria2_disk_cache'"),
        downloads.index("'aria2_file_allocation'"),
    ]
    assert order == sorted(order)

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
