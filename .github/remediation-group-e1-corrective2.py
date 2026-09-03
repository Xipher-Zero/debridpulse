from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST = ROOT / "backend" / "tests" / "test_ui_downloads_polish_contract.py"
source = TEST.read_text(encoding="utf-8")
old = '''    assert "api('POST'" in app
    assert "api('DELETE'" in app'''
new = '''    assert "api('POST'" in app
    assert "'DELETE'," in app
    assert "`/torrents/${id}?from_alldebrid=true`" in app'''
if source.count(old) != 1:
    raise RuntimeError(f"expected one multiline delete assertion anchor, found {source.count(old)}")
TEST.write_text(source.replace(old, new, 1), encoding="utf-8")
