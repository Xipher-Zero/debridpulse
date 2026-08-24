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
    """.badge-processing { background: rgba(168,85,247,.14); color: var(--purple); }\n.badge-queued     { background: rgba(59,130,246,.14); color: var(--blue); }\n""",
    """.badge-processing { background: rgba(168,85,247,.14); color: var(--purple); }\n.badge-extracting { animation: pulse 1s ease-in-out infinite; }\n.badge-queued     { background: rgba(59,130,246,.14); color: var(--blue); }\n""",
    "extracting badge animation",
)

replace_once(
    "frontend/static/style.css",
    """.badge-imported   { background: rgba(var(--accent-rgb),.12); color: var(--accent); }\n\n/* ── Progress ── */\n""",
    """.badge-imported   { background: rgba(var(--accent-rgb),.12); color: var(--accent); }\n\n@media (prefers-reduced-motion: reduce) {\n  .badge-extracting { animation: none; }\n}\n\n/* ── Progress ── */\n""",
    "reduced motion extraction badge rule",
)

replace_once(
    "frontend/static/index.html",
    '<link rel="stylesheet" href="/style.css?v=13">',
    '<link rel="stylesheet" href="/style.css?v=14">',
    "stylesheet cache bust",
)

# Keep any source-contract tests that pin the stylesheet cache token in sync.
for test_path in Path("backend/tests").glob("test_*.py"):
    text = test_path.read_text()
    updated = text.replace('/style.css?v=13', '/style.css?v=14')
    if updated != text:
        test_path.write_text(updated)

Path("backend/tests/test_extraction_pulse.py").write_text('''from pathlib import Path\n\n\ndef test_extracting_status_badge_pulses_and_respects_reduced_motion():\n    root = Path(__file__).resolve().parents[2]\n    css = (root / "frontend/static/style.css").read_text()\n    html = (root / "frontend/static/index.html").read_text()\n\n    assert ".badge-extracting { animation: pulse 1s ease-in-out infinite; }" in css\n    assert "@media (prefers-reduced-motion: reduce)" in css\n    assert ".badge-extracting { animation: none; }" in css\n    assert '/style.css?v=14' in html\n''')

print("Extracting status pulse applied")
