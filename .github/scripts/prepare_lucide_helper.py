from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit("usage: prepare_lucide_helper.py <apply-script>")

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
old = "      .replace(/^[\\\\s\\\\u2000-\\\\u2BFF\\\\u{1F000}-\\\\u{1FAFF}]+/u, '')\n"
new = "      .replace(/^[^A-Za-z0-9]+/, '')\n"
if text.count(old) != 1:
    raise SystemExit(f"expected one Unicode prefix scrubber, found {text.count(old)}")
path.write_text(text.replace(old, new), encoding="utf-8")
