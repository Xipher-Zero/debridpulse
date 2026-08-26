from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_downloads_desktop_filter_contract_and_details_removal():
    runtime = read("ui-downloads-runtime.js")
    expected = [
        "{status: '', label: 'All'}",
        "{status: 'downloading', label: 'Downloading'}",
        "{status: 'paused', label: 'Paused'}",
        "{status: 'processing', label: 'Processing'}",
        "{status: 'ready', label: 'Ready'}",
        "{status: 'completed', label: 'Done'}",
        "{status: 'error', label: 'Error'}",
    ]
    for fragment in expected:
        assert fragment in runtime
    assert "normalizeDownloadRowActions" in runtime
    assert "onclick.includes('showDetail(')" in runtime


def test_downloads_desktop_column_rebalance_expands_progress():
    css = read("ui-downloads-desktop.css")
    assert "nth-child(2) { width: 25%; }" in css
    assert "nth-child(3) { width: 8%; }" in css
    assert "nth-child(4) { width: 13%; }" in css
    assert "nth-child(5) { width: 25%; }" in css
    assert "nth-child(7) { width: 8%; }" in css
    assert "nth-child(8) { width: 190px; }" in css
    assert "table-layout: fixed" in css


def test_provider_subscription_is_one_centered_crown_and_copy_unit():
    css = read("ui-shell-provider-status-v2.css")
    assert "grid-template-columns: 36px max-content" in css
    assert "justify-content: center !important" in css
    assert "column-gap: 3px !important" in css
    assert "width: max-content !important" in css
    assert "transform: none !important" in css
    assert ".dp-provider-premium-days" in css
    assert "white-space: nowrap !important" in css


def test_detail_modal_scrolls_inside_frame_with_thicker_scrollbar():
    css = read("ui-modal-contract.css")
    assert "#modal {" in css
    assert "overflow: hidden !important" in css
    assert ".modal-body {" in css
    assert "overflow-y: auto" in css
    assert "scrollbar-gutter: stable" in css
    assert "::-webkit-scrollbar" in css
    assert "width: 14px" in css


def test_new_contract_layers_live_in_correct_cascade_sections():
    style = read("style-v11.css")
    modal = style.index("ui-modal-contract.css?v=24")
    shell = style.index("ui-shell.css?v=20")
    provider_base = style.index("ui-shell-provider-status.css?v=23")
    provider_v2 = style.index("ui-shell-provider-status-v2.css?v=27")
    downloads_base = style.index("ui-downloads-page.css?v=25")
    downloads_desktop = style.index("ui-downloads-desktop.css?v=28")
    transfer = style.index("ui-transfer-contract.css?v=28")

    assert modal < shell
    assert provider_base < provider_v2
    assert downloads_base < downloads_desktop < transfer
