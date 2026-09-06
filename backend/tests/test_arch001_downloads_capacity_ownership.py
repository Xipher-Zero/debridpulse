from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def test_downloads_capacity_is_owned_by_canonical_request_path():
    app = (STATIC / "app.js").read_text()
    runtime = (STATIC / "ui-correction-batch1.js").read_text()
    provider_status = (STATIC / "ui-provider-status.js").read_text()

    assert "Math.min(Math.max(parseInt(v)||25,1),100)" in app
    assert "Math.min(Math.max(parseInt(torrentPageSize)||25,1),100)" in app
    assert "parseInt(v)||25,15" not in app
    assert "parseInt(torrentPageSize)||25,15" not in app
    assert "useMeasured" not in runtime
    assert "measuredLimit < 15" not in runtime
    assert "ui-correction-batch1-capacity.js" not in provider_status
    assert not (STATIC / "ui-correction-batch1-capacity.js").exists()
