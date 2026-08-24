from pathlib import Path


def replace_once(path: str, old: str, new: str, label: str) -> None:
    p = Path(path)
    text = p.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    p.write_text(text.replace(old, new, 1))


replace_once(
    "frontend/static/style.css",
    ".badge-imported   { background: rgba(var(--accent-rgb),.12); color: var(--accent); }\n",
    ".badge-imported   { background: rgba(var(--accent-rgb),.12); color: var(--accent); }\n"
    ".badge-duplicate  { background: rgba(100,116,139,.10); color: var(--text3); }\n\n"
    "/* Duplicate mirror explanations are operational notes, not failures. */\n"
    "#modal-body tr:has(.badge-duplicate) td:first-child > div[style*=\"color:var(--red)\"] {\n"
    "  color: var(--text3) !important;\n"
    "}\n",
    "duplicate note style",
)

replace_once(
    "frontend/static/index.html",
    '<link rel="stylesheet" href="/style.css?v=14">',
    '<link rel="stylesheet" href="/style.css?v=15">',
    "stylesheet cache bust",
)

Path("backend/tests/test_duplicate_note_style.py").write_text(
    '''from pathlib import Path\n\n\nROOT = Path(__file__).resolve().parents[2]\n\n\ndef test_duplicate_mirror_reason_is_subdued_operational_note():\n    styles = (ROOT / "frontend/static/style.css").read_text()\n    index = (ROOT / "frontend/static/index.html").read_text()\n\n    assert ".badge-duplicate" in styles\n    assert "#modal-body tr:has(.badge-duplicate)" in styles\n    assert "color: var(--text3) !important;" in styles\n    assert '<link rel="stylesheet" href="/style.css?v=15">' in index\n'''
)
print("Duplicate note presentation patch applied")
