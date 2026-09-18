from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def test_downloads_capacity_is_owned_by_canonical_request_path():
    # DP 1.0.12 canonical flattening: ui-downloads.js is the sole Downloads
    # controller/renderer owner, including bounded page-size clamping and
    # measured desktop capacity -- app.js no longer carries a second
    # Downloads list-state/pagination implementation.
    app = (STATIC / "app.js").read_text()
    downloads = (STATIC / "ui-downloads.js").read_text()
    provider_status = (STATIC / "ui-provider-status.js").read_text()

    assert "torrentPageSize" not in app
    assert "torrentPage" not in app
    assert "Math.min(Math.max(parseInt(v)||25,1),100)" in downloads.replace(" ", "")
    assert "Math.min(Math.max(parseInt(torrentPageSize)||25,1),100)" in downloads.replace(" ", "")
    assert "parseInt(v)||25,15" not in downloads
    assert "parseInt(torrentPageSize)||25,15" not in downloads

    assert "function measuredSize()" in downloads
    assert "Math.max(1,Math.min(100" in downloads.replace(" ", "")
    assert "loadTorrents" in downloads
    assert "ResizeObserver" in downloads
    assert "useMeasured" not in downloads
    assert "measuredLimit < 15" not in downloads

    assert "ui-correction-batch1-capacity.js" not in provider_status
    assert not (STATIC / "ui-correction-batch1-capacity.js").exists()
    assert not (STATIC / "ui-correction-batch1.js").exists()
