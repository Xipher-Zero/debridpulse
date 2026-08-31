"""Final-state contracts for shared transfer progress geometry."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
STYLE = STATIC / "style-v11.css"
TRANSFER = STATIC / "ui-transfer-contract.css"
HISTORICAL = STATIC / "ui-dashboard-progress-weight.css"


def test_superseded_dashboard_progress_weight_layer_is_not_shipped() -> None:
    overlay = STYLE.read_text(encoding="utf-8")
    assert "/ui-dashboard-progress-weight.css" not in overlay
    assert not HISTORICAL.exists()


def test_shared_transfer_contract_is_final_progress_geometry_owner() -> None:
    overlay = STYLE.read_text(encoding="utf-8")
    transfer_path = "/ui-transfer-contract.css?v=31"
    dashboard_consistency = "/ui-dashboard-consistency.css?v=23"
    downloads = "/ui-downloads-page.css?v=27"

    for layer in (dashboard_consistency, downloads, transfer_path):
        assert layer in overlay
    assert overlay.index(dashboard_consistency) < overlay.index(downloads) < overlay.index(transfer_path)

    css = TRANSFER.read_text(encoding="utf-8")
    assert "body.dp-v11-structural :is(#dash-tbody, #t-tbody) .prog," in css
    assert "body.dp-v11-structural :is(#dash-tbody, #t-tbody) .prog-fill" in css
    assert "height: 7px !important" in css
    assert "border-radius: 999px !important" in css
    assert "height: 3.5px !important" not in css
