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

    assert "card('Download Location & Limits'" in downloads
    assert "aria2 Delivery" not in downloads
    # Operator-facing tuning labels name capabilities, never daemons.
    assert "card('Download Engine'" not in downloads
    # The card's icon is its CARD_ICONS entry; no separate legacy icon is emitted.
    assert "wrapTitle: true" in downloads
    assert "dp-settings-download-engine-icon" not in downloads
    assert "'Download Location & Limits': ['downloads', '/icons/dp/settings/download-engine.svg?v=1']" in source(SETTINGS_PAGE_JS)
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
    assert "Maximum downloads DebridPulse runs at once." in downloads
    # No topology control exists, visible or hidden, in the global card.
    # (Collapsed Transfer Method Settings child cards legitimately use `hidden`,
    # so the check is scoped to the Download Location & Limits card it is about.)
    global_card = downloads[downloads.index("card('Download Location & Limits'"):
                            downloads.index("const tuning = groupCard('Transfer Method Settings'")]
    assert " hidden" not in global_card and "'hidden'" not in global_card
    for absent in ("aria2_mode", "aria2_url", "aria2_secret", "aria2_download_path",
                   "data-download-path-mode", "data-builtin-only-tuning", "External aria2", "External RPC",
                   "external aria2", "Built-in", "builtIn"):
        assert absent not in runtime, absent
    assert "function updateModeState" not in runtime
    assert "function aria2RpcSecretFields" not in runtime

    # Every aria2 option is a declared field-boundary control of the one
    # `integration:aria2` scope; there is no deferred payload carrying them.
    table = runtime[runtime.index("const COMMIT_FIELDS"):]
    table = table[:table.index("});") + 3]
    declared = {line.split("option: '", 1)[1].split("'", 1)[0]
                for line in table.splitlines() if "'integration:aria2'" in line}
    assert declared == ARIA2_PAYLOAD_OPTIONS
    secrets = runtime[runtime.index("const INTEGRATION_SECRET_CONTROLS"):runtime.index("});", runtime.index("const INTEGRATION_SECRET_CONTROLS"))]
    assert "aria2" not in secrets

    # The Download Engine test is removed from the UI entirely -- not
    # relocated, not replaced. Nothing here reaches its backend route.
    assert "/settings/test-aria2" not in runtime
    assert "Test Download Engine" not in runtime
    assert "test-aria2" not in runtime
    tests = runtime[runtime.index("function connectionTestPayload"):runtime.index("async function testConnection")]
    assert "aria2" not in tests

    css = "\n".join(path.read_text(encoding="utf-8") for path in STATIC.glob("*.css"))
    for selector in ("dp-settings-mode-external", "dp-settings-external-connection-row", "dp-settings-aria2-secret",
                     "dp-settings-clear-secret--aria2", "dp-settings-download-engine-mode",
                     "dp-settings-engine-tuning-builtin-only", "dp-settings-builtin-only-label", "external-control"):
        assert selector not in css, selector

    css = source(SETTINGS_PAGE_CSS)
    # DP 1.0.13: both settings are the shared INLINE field -- a stacked title +
    # hint with the control beside it -- so the row states only how the two sit
    # next to one another. The three-row spine the stacked zones used to share
    # is gone with the grammar that needed it.
    row = css.split(".dp-settings-download-engine-row {", 1)[1].split("}", 1)[0]
    assert "display: flex" in row
    assert "align-items: center" in row
    assert "grid-template-rows" not in row
    # DP 1.0.13: the two settings form ONE centred, content-bounded island with
    # the same relationship container and padding density as the accepted
    # Extraction behaviour island, rather than stretching across the card.
    assert "justify-content: center;" in row
    assert "width: max-content;" in row
    assert "max-width: min(100%, 1040px);" in row
    assert "margin: 0 auto;" in row
    assert "border: 1px solid var(--dp-divider);" in row
    assert "border-radius: 12px;" in row
    assert "padding: 16px 22px 17px;" in row
    assert "gap: 18px 44px;" in row
    # The zone wrappers stay names, not boxes.
    assert (
        "#view-settings .dp-settings-download-path-stack,\n"
        "#view-settings .dp-settings-download-limit {\n"
        "  display: contents;\n"
        "}"
    ) in css
    # The folder takes the row's spare width -- bounded, because a path field
    # wide enough to read is not one as wide as the card happens to be.
    # Inside a content-bounded island the path field states its OWN readable
    # width; taking the row's spare width is what a stretched band did.
    folder = css.split(
        "#view-settings .dp-settings-download-folder-field > .dp-settings-inline-field-control {", 1)[1].split("}", 1)[0]
    assert "flex: 0 1 auto" in folder
    assert "width: 380px" in folder
    assert "max-width: 100%" in folder

    # Browse sits INSIDE the field, through the one universal compound-field
    # material -- the same primitive the OIDC Callback and one-time token Copy
    # actions use. It is opt-in, so Backup Folder keeps the bare pairing.
    assert "embedAction: true" in runtime
    assert "function embeddedActionField(control, action" in runtime
    assert runtime.count("class=\"dp-action-field") == 1
    universal = source(STATIC / "ui-universal-language.css")
    assert ".dp-action-field {" in universal
    assert ":is(.dp-field, .input, .dp-action-field)," in universal
    assert ".dp-action-field:focus-within" in universal
    # Settings adds only the embedded button's geometry; it restates no field
    # material, which belongs to the universal owner alone.
    chip = css.split("#view-settings .dp-action-field > .dp-settings-directory-field-browse {", 1)[1].split("}", 1)[0]
    for material in ("border", "background", "box-shadow", "color"):
        assert material not in chip, material
    assert "justify-self: end;" not in css.split(".dp-settings-download-engine-row", 1)[1][:1200]


def test_additional_engine_tuning_keeps_reviewed_layout_order_and_copy():
    downloads = downloads_runtime()
    css = source(SETTINGS_PAGE_CSS)

    # Advanced direct-transfer tuning lives in the collapsed "Network Sources"
    # child card of the Executor Tuning master card (DP 1.0.13 Item 8 renamed
    # the operator-facing family; the executor id is unchanged).
    assert "executorTuningCard('direct', 'Network Sources'" in downloads
    assert "function directTransfersTuning(s)" in downloads
    # DP 1.0.13: one reusable tuning-cell collection, shared with Usenet,
    # Safety & Recovery and the AllDebrid tuning region. The competing
    # `dp-settings-engine-tuning-grid` matrix and the separate File Allocation
    # band are retired -- File Allocation is an ordinary cell.
    assert "tuningCells(" in downloads
    assert "dp-settings-engine-tuning-grid" not in downloads
    assert "dp-settings-engine-file-allocation" not in downloads

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

    # DP 1.0.13 adjacency: connections/segments/split size, then the two
    # in-progress controls, then the two disk controls.
    order = [
        downloads.index("'aria2_max_connection_per_server'"),
        downloads.index("'aria2_split'"),
        downloads.index("'aria2_min_split_size'"),
        downloads.index("'aria2_continue_downloads'"),
        downloads.index("'aria2_lowest_speed_limit'"),
        downloads.index("'aria2_disk_cache'"),
        downloads.index("'aria2_file_allocation'"),
    ]
    assert order == sorted(order)
    tuning = downloads[downloads.index("function directTransfersTuning("):
                       downloads.index("function usenetTuning(")]
    assert tuning.count("tuningGroup(") == 3

    assert "['trunc', 'Truncate']" in downloads
    assert "['falloc', 'Fallocate']" in downloads
    assert "['prealloc', 'Preallocate']" in downloads
    assert "['none', 'None']" in downloads

    grid = css.split(".dp-settings-tuning-grid {", 1)[1].split("}", 1)[0]
    # DP 1.0.13 consolidation: equal-width invisible lanes, one per cell of the
    # fixed set, derived from the set's own declared cardinality -- never a
    # column count written into a region's own rule.
    assert "grid-template-columns: repeat(auto-fill, minmax(" in grid
    assert "var(--dp-tuning-lanes)" in grid and "var(--dp-tuning-lane-min)" in grid
    assert "max-width: var(--dp-tuning-card-max);" in css
    # The relationship outline is drawn only where the set still holds all of
    # its lanes, and it owns no track of its own.
    assert "container-type: inline-size;" in grid
    assert "@container dp-tuning (min-width:" in css
    assert "grid-template-columns: subgrid;" in css
    group = css.split("#view-settings .dp-settings-tuning-group {", 1)[1].split("}", 1)[0]
    assert "display: contents;" in group


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
