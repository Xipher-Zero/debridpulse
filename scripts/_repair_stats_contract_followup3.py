from pathlib import Path
import sys

repo = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
path = repo / "backend" / "tests" / "test_ui_dashboard_contract.py"
text = path.read_text(encoding="utf-8")
text = text.replace('(ROOT / "frontend" / "static" / "index.html")', '(REPO_ROOT / "frontend" / "static" / "index.html")')
path.write_text(text, encoding="utf-8")
print("Dashboard Statistics ownership contract root corrected")
