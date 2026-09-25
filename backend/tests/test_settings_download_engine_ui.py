import re
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
    """The whole Downloads section.

    After the 1.0.13 reorganization the section is built from several bounded
    helpers (``executorTuningCard``, ``directTransfersTuning``, ``usenetTuning``)
    plus ``downloadsPanel`` itself, so the slice starts at the first of them.
    """
    runtime = source(SETTINGS_PAGE_JS)
    return runtime[runtime.index("function executorTuningCard"):runtime.index("function extractionPanel")]


def test_download_engine_header_matches_reviewed_identity_and_copy_contract():
    downloads = downloads_runtime()
    css = source(SETTINGS_PAGE_CSS)
    chrome = source(SETTINGS_CHROME_CSS)

    assert "card('Global Download Settings'" in downloads
    assert "aria2 Delivery" not in downloads
    # Operator-facing tuning labels name capabilities, never daemons.
    assert "card('Download Engine'" not in downloads
    # The card's icon is its CARD_ICONS entry; no separate legacy icon is emitted.
    assert "wrapTitle: true" in downloads
    assert "dp-settings-download-engine-icon" not in downloads
    assert "'Global Download Settings': ['downloads', '/icons/dp/settings/download-engine.svg?v=1']" in source(SETTINGS_PAGE_JS)
    assert "headerCenter:" in downloads
    assert "dp-settings-download-engine-header-copy" in downloads
    assert "Where DebridPulse saves downloads and how many it runs at once." in downloads
    assert "dp-settings-download-engine-copy" not in downloads
    # One engine: the header carries no selector action.
    assert "action:" not in downloads

    header = css.split(".dp-settings-download-engine-card > .card-header {", 1)[1].split("}", 1)[0]
    assert "display: grid;" in header
    assert "minmax(420px, 900px)" in header
    assert ".dp-settings-card-header-center" in css
    assert "text-align: center;" in css

    assert "dp-settings-download-engine-icon" not in chrome


# The aria2 options the Settings page writes: tuning of the one daemon only.
ARIA2_PAYLOAD_OPTIONS = {
    "split", "min_split_size", "max_connection_per_server", "continue_downloads",
    "disk_cache", "file_allocation", "lowest_speed_limit",
}


def test_download_engine_presents_one_aria2_with_one_download_folder():
    runtime = source(SETTINGS_PAGE_JS)
    downloads = downloads_runtime()

    assert "directoryField('download_folder', 'Download Folder'" in downloads
    assert "Browse server directories for Download Folder" in downloads
    assert "Where DebridPulse saves downloads." in downloads
    assert "Maximum number of downloads DebridPulse can run at the same time." in downloads
    # No topology control exists, visible or hidden, in the global card.
    # (Collapsed Executor Tuning child cards legitimately use `hidden`, so the
    # check is scoped to the Global Download Settings card it is about.)
    global_card = downloads[downloads.index("card('Global Download Settings'"):
                            downloads.index("const tuning = groupCard('Executor Tuning'")]
    assert " hidden" not in global_card and "'hidden'" not in global_card
    for absent in ("aria2_mode", "aria2_url", "aria2_secret", "aria2_download_path",
                   "data-download-path-mode", "data-builtin-only-tuning", "External aria2", "External RPC",
                   "external aria2", "Built-in", "builtIn"):
        assert absent not in runtime, absent
    assert "function updateModeState" not in runtime
    assert "function aria2RpcSecretFields" not in runtime

    payload = runtime[runtime.index("function aria2ConfigurationPayload"):runtime.index("function usenetConfigurationPayload")]
    options = payload[payload.index("options: {"):payload.index("},")]
    assert set(re.findall(r"^\s+(\w+):", options, re.M)) == ARIA2_PAYLOAD_OPTIONS
    assert "clear_secrets" not in payload
    secrets = runtime[runtime.index("const INTEGRATION_SECRET_CONTROLS"):runtime.index("});", runtime.index("const INTEGRATION_SECRET_CONTROLS"))]
    assert "aria2" not in secrets

    # The engine test is the owned daemon's own health check; there is no draft
    # connection payload to send.
    assert "aria2: '/settings/test-aria2'" in runtime
    tests = runtime[runtime.index("function connectionTestPayload"):runtime.index("async function testConnection")]
    assert "aria2" not in tests

    css = "\n".join(path.read_text(encoding="utf-8") for path in STATIC.glob("*.css"))
    for selector in ("dp-settings-mode-external", "dp-settings-external-connection-row", "dp-settings-aria2-secret",
                     "dp-settings-clear-secret--aria2", "dp-settings-download-engine-mode",
                     "dp-settings-engine-tuning-builtin-only", "dp-settings-builtin-only-label", "external-control"):
        assert selector not in css, selector

    css = source(SETTINGS_PAGE_CSS)
    row = css.split(".dp-settings-download-engine-row {", 1)[1].split("}", 1)[0]
    assert "grid-template-columns: minmax(0, 1fr) minmax(280px, 320px);" in row
    assert "gap: 32px;" in row
    assert (
        "#view-settings .dp-settings-download-limit {\n"
        "  width: 100%;\n"
        "  max-width: 320px;\n"
        "  justify-self: end;\n"
        "}"
    ) in css


def test_additional_engine_tuning_keeps_reviewed_layout_order_and_copy():
    downloads = downloads_runtime()
    css = source(SETTINGS_PAGE_CSS)

    # Advanced direct-transfer tuning lives in the collapsed "Network Sources"
    # child card of the Executor Tuning master card (DP 1.0.13 Item 8 renamed
    # the operator-facing family; the executor id is unchanged).
    assert "executorTuningCard('direct', 'Network Sources'" in downloads
    assert "function directTransfersTuning(s)" in downloads
    assert "dp-settings-engine-tuning-grid" in downloads
    assert "dp-settings-engine-file-allocation" in downloads

    required_copy = (
        "Stops a slow connection when its speed falls at or below this value. Set to 0 to disable the limit.",
        "Resume existing partial files when possible instead of restarting them from the beginning.",
        "Controls how many parallel segments a single file can use. Actual connections may be limited by the server and split-size settings.",
        "Maximum number of connections a single download can open to the same server.",
        "Controls how small file sections can become when a download is split. Larger values create fewer parallel segments.",
        "Amount of memory usable as a shared download cache to reduce disk I/O. Set to 0 to disable the cache.",
        "Controls how disk space is prepared for new files.",
    )
    for text in required_copy:
        assert text in downloads

    order = [
        downloads.index("'aria2_continue_downloads'"),
        downloads.index("'aria2_split'"),
        downloads.index("'aria2_max_connection_per_server'"),
        downloads.index("'aria2_min_split_size'"),
        downloads.index("'aria2_lowest_speed_limit'"),
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
