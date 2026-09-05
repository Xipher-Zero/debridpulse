from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def test_batch1_runtime_and_styles_are_wired_through_canonical_assets():
    provider_status = (STATIC / "ui-provider-status.js").read_text()
    styles = (STATIC / "style-v11.css").read_text()
    runtime = (STATIC / "ui-correction-batch1.js").read_text()
    batch_css = (STATIC / "ui-correction-batch1.css").read_text()

    assert "ui-correction-batch1.js" in provider_status
    assert "ui-correction-batch1.css" in styles
    assert "DebridPulse stared at that for a moment" in runtime
    assert "Checking transfers for recoverable work" in runtime
    assert "File Archive" not in runtime
    assert "torrent_file" in runtime
    assert "ResizeObserver" in runtime
    assert "Math.floor(oldOffset / measured) + 1" in runtime
    assert "Friendly" in runtime and "International" in runtime and "ISO" in runtime
    assert "12-hour" in runtime and "24-hour" in runtime
    assert "dp-pager-placeholder" in runtime
    assert "dp-downloads-pause-shim" in runtime
    assert "dp-global-pause-center" in runtime
    assert "width: 136px" in batch_css


def test_host_artwork_and_domain_matching_contract():
    runtime = (STATIC / "ui-correction-batch1.js").read_text()
    host_dir = STATIC / "icons" / "hosts"

    assert "host === domain || host.endsWith('.' + domain)" in runtime
    assert "rapidgator.net" in runtime
    assert "mega.nz" in runtime
    assert (host_dir / "rapidgator.png").is_file()
    assert (host_dir / "mega.svg").is_file()


def test_quick_add_import_capability_is_not_removed_from_backend():
    routes = (ROOT / "backend" / "api" / "routes.py").read_text()
    runtime = (STATIC / "ui-correction-batch1.js").read_text()

    assert "/torrents/import-existing" in routes
    assert "importExisting" in runtime
