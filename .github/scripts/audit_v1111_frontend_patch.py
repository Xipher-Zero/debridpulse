from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one anchor, found {count}: {old[:100]!r}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


# ARCH-001: remove the three unreachable views from the packaged document.
index = Path("frontend/static/index.html")
html = index.read_text(encoding="utf-8")
start = html.index("    <!-- Changelog -->")
end = html.index("    <!-- Help -->", start)
index.write_text(html[:start] + html[end:], encoding="utf-8")

# Remove only handlers exclusively owned by those retired views.
app = Path("frontend/static/app.js")
source = app.read_text(encoding="utf-8")
source = source.replace("  if (v === 'aria2queue') loadAria2QueueView();\n", "", 1)
start = source.index("async function loadChangelog()")
end = source.index("function toggleFilterFields()", start)
source = source[:start] + source[end:]
start = source.index("// ── Downloads View (aria2 Queue)")
end = source.index("// ── Extraction Password List", start)
source = source[:start] + source[end:]
app.write_text(source, encoding="utf-8")

replace_once(
    "frontend/static/style.css",
    "  /* Downloads panel controls wrap */\n"
    "  #view-aria2queue .view-header { flex-direction: column; align-items: flex-start; gap: 8px; }\n"
    "  #view-aria2queue .view-header > div:last-child { width: 100%; flex-wrap: wrap; }\n\n",
    "",
)

# Statistics contracts used the retired Changelog comment only as a convenient
# slice boundary. Help is now the next deliberate supported view boundary.
for test_path in (
    "backend/tests/test_v1111_canonical_frontend_contract.py",
    "backend/tests/test_ui_dashboard_contract.py",
):
    p = Path(test_path)
    test_source = p.read_text(encoding="utf-8")
    count = test_source.count("index.index('<!-- Changelog -->')") + test_source.count("html.index('<!-- Changelog -->')")
    if count <= 0:
        raise SystemExit(f"{test_path}: no stale Changelog slice boundary found")
    test_source = test_source.replace("index.index('<!-- Changelog -->')", "index.index('<!-- Help -->')")
    test_source = test_source.replace("html.index('<!-- Changelog -->')", "html.index('<!-- Help -->')")
    p.write_text(test_source, encoding="utf-8")

# Permanent reachability regression: these views and their exclusively-owned
# browser machinery must not re-enter the packaged application.
contract = Path("backend/tests/test_v1111_canonical_frontend_contract.py")
contract_source = contract.read_text(encoding="utf-8")
marker = "def test_retired_unreachable_views_and_owners_are_not_packaged() -> None:"
if marker not in contract_source:
    contract_source += '''\n\n\ndef test_retired_unreachable_views_and_owners_are_not_packaged() -> None:\n    html = read(INDEX)\n    app = read(APP)\n    legacy_css = read(STATIC / "style.css")\n    for fragment in (\n        "view-changelog",\n        "view-aria2queue",\n        "view-support",\n        "changelog-content",\n        "aria2q-",\n    ):\n        assert fragment not in html\n        assert fragment not in legacy_css\n    for fragment in (\n        "loadChangelog",\n        "loadAria2QueueView",\n        "renderAria2QueueView",\n        "aria2QueueAction",\n        "_aria2qTimer",\n        "aria2q-",\n    ):\n        assert fragment not in app\n    supported = re.findall(r'class="nav-item(?: active)?" data-view="([^"]+)"', html)\n    assert supported == ["dashboard", "torrents", "events", "stats", "settings", "help"]\n'''
    contract.write_text(contract_source, encoding="utf-8")

print("frontend reachability cleanup applied")
