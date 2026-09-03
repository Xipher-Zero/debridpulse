from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST = ROOT / "backend" / "tests" / "test_ui_statistics_contract.py"
source = TEST.read_text(encoding="utf-8")
old = 'combined = "\n".join(read(path) for path in (INDEX, APP, STATS))'
new = 'combined = chr(10).join(read(path) for path in (INDEX, APP, STATS))'
if source.count(old) != 1:
    raise RuntimeError(f"expected one malformed statistics join anchor, found {source.count(old)}")
TEST.write_text(source.replace(old, new, 1), encoding="utf-8")
