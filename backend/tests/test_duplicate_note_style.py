from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_duplicate_mirror_reason_is_subdued_operational_note():
    # DP 1.0.12 canonical flattening: style.css is now a pure @import list;
    # .badge-duplicate lives in ui-shared-contract.css and the modal-body note
    # rule (migrated from the retired ui-legacy-foundation.css) lives in
    # ui-transfer-contract.css.
    shared = (ROOT / "frontend/static/ui-shared-contract.css").read_text()
    transfer = (ROOT / "frontend/static/ui-transfer-contract.css").read_text()
    index = (ROOT / "frontend/static/index.html").read_text()
    app = (ROOT / "frontend/static/app.js").read_text()

    # Duplicate equivalence is informational normalization, not an operator error.
    assert ".badge-duplicate" in shared
    assert "#modal-body tr:has(.badge-duplicate)" in transfer
    assert "color: var(--text3) !important;" in transfer
    # The default file-reason renderer stays red for genuine non-duplicate failures.
    assert 'f.block_reason ? `<div style="font-size:10px;color:var(--red)' in app
    assert '<link rel="stylesheet" href="/style.css?v=18">' in index
