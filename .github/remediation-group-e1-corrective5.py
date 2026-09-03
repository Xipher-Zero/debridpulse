from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST = ROOT / "backend" / "tests" / "test_ui_dashboard_contract.py"
source = TEST.read_text(encoding="utf-8")
old = '    assert "addDashboardEntries" not in app\n'
new = '    assert "function addDashboardEntries()" in app\n'
if source.count(old) != 1:
    raise RuntimeError(f"expected one quick-add ownership assertion, found {source.count(old)}")
TEST.write_text(source.replace(old, new, 1), encoding="utf-8")
