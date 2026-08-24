from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_duplicate_mirror_reason_is_subdued_operational_note():
    styles = (ROOT / "frontend/static/style.css").read_text()
    index = (ROOT / "frontend/static/index.html").read_text()

    assert ".badge-duplicate" in styles
    assert "#modal-body tr:has(.badge-duplicate)" in styles
    assert "color: var(--text3) !important;" in styles
    assert '<link rel="stylesheet" href="/style.css?v=15">' in index
