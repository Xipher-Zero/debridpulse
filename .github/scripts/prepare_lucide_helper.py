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
    "ROOT = Path(__file__).resolve().parents[2]\n",
    "ROOT = Path.cwd()\n",
    "repo root",
)
replace_once(
    "      .replace(/^[\\\\s\\\\u2000-\\\\u2BFF\\\\u{1F000}-\\\\u{1FAFF}]+/u, '')\n",
    "      .replace(/^[^A-Za-z0-9]+/, '')\n",
    "Unicode prefix scrubber",
)

path.write_text(text, encoding="utf-8")
