from pathlib import Path
import sys

repo = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
path = repo / "backend" / "tests" / "test_ui_shell_contract.py"
text = path.read_text(encoding="utf-8")
text = text.replace('"/ui-statistics-page.css": "21"', '"/ui-statistics-page.css": "22"')
path.write_text(text, encoding="utf-8")
print("Statistics cache-generation contract aligned")
