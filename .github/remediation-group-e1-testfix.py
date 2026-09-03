from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "backend" / "tests"


def read(name):
    return (TESTS / name).read_text(encoding="utf-8")


def write(name, content):
    (TESTS / name).write_text(content, encoding="utf-8")

# The runtime architecture contract must inspect the canonical owners after E1,
# not a file whose removal is the architectural requirement.
name = "test_ui_runtime_architecture_contract.py"
source = read(name)
pattern = re.compile(
    r"def test_runtime_icon_insertions_do_not_fall_back_to_emoji_or_plain_text\(\) -> None:\n.*?(?=\ndef |\Z)",
    re.S,
)
replacement = '''def test_canonical_icon_insertions_do_not_fall_back_to_emoji_or_plain_text() -> None:\n    index = read("index.html")\n    app = read("app.js")\n    icons = read("operator-title.js")\n    assert 'data-dp-lucide="upload"' in index\n    assert 'data-dp-lucide="refresh"' in index\n    assert 'data-dp-lucide="arrowRight"' in index\n    assert 'data-dp-lucide="pause"' in index\n    assert 'data-dp-lucide="play"' in index\n    assert 'data-dp-lucide="trash2"' in index\n    assert 'data-dp-lucide="x"' in index\n    assert "window.DPIcons.svg" in app\n    assert "const LUCIDE" in icons\n\n'''
source, count = pattern.subn(replacement, source, count=1)
if count != 1:
    raise RuntimeError("could not replace stale runtime icon contract")
write(name, source)

# Exact quote shape is not the ownership contract; the canonical row class is.
for name in ("test_uiarch001_e1_ownership.py", "test_ui_downloads_final_contract.py"):
    source = read(name)
    source = source.replace("'class=\"dp-downloads-detail-row\"'", "'dp-downloads-detail-row'")
    source = source.replace('"class=\\\"dp-downloads-detail-row\\\""', '"dp-downloads-detail-row"')
    write(name, source)

# operator-title stores Lucide geometry as data, not serialized SVG path text.
name = "test_ui_downloads_correction_batch_contract.py"
source = read(name)
source = source.replace(
    "    assert 'M20 11a8.1 8.1 0 0 0-15.5-2M4 4v5h5' in icons\n",
    "    assert 'const LUCIDE' in icons\n    assert 'refresh:' in icons\n",
)
write(name, source)
