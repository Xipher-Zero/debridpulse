from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit("usage: prepare_lucide_helper.py <apply-script>")

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")


def replace_once(old: str, new: str, description: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{description}: expected one occurrence, found {count}")
    text = text.replace(old, new)


replace_once(
    'ROOT = Path(__file__).resolve().parents[2]\nSTATIC = ROOT / "frontend" / "static"\nTESTS = ROOT / "backend" / "tests"\nPIN = "23f9abc4ed0146cffededd3d7f94c1018bfdf693"\n',
    'ROOT = Path.cwd()\nSTATIC = ROOT / "frontend" / "static"\nTESTS = ROOT / "backend" / "tests"\nPIN = "23f9abc4ed0146cffededd3d7f94c1018bfdf693"\n',
    "top-level repo root",
)
replace_once(
    "      .replace(/^[\\\\s\\\\u2000-\\\\u2BFF\\\\u{1F000}-\\\\u{1FAFF}]+/u, '')\n",
    "      .replace(/^[^A-Za-z0-9]+/, '')\n",
    "Unicode prefix scrubber",
)
replace_once(
    '    "chevron-down", "chevron-left", "chevron-right", "circle-check", "circle-help",\n',
    '    "chevron-down", "chevron-left", "chevron-right", "circle-check", "circle-question-mark",\n',
    "pinned Lucide help icon name",
)
replace_once(
    "    help: 'circle-help',\n",
    "    help: 'circle-question-mark',\n",
    "help alias",
)

path.write_text(text, encoding="utf-8")
