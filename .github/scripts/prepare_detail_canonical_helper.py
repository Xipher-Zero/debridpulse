from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit("usage: prepare_detail_canonical_helper.py <helper>")

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
old = 'modal.write_text(modal_text.rstrip() + modal_append + "\\n", encoding="utf-8")\n'
new = 'modal.write_text(modal_text.rstrip() + modal_append.rstrip() + "\\n", encoding="utf-8")\n'
count = text.count(old)
if count != 1:
    raise SystemExit(f"expected one modal EOF write, found {count}")
path.write_text(text.replace(old, new), encoding="utf-8")
