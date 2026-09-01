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

# Existing Statistics contracts used the retired Changelog comment only as a
# convenient slice boundary. Help is now the next deliberate view boundary.
tests = Path("backend/tests/test_v1111_canonical_frontend_contract.py")
test_source = tests.read_text(encoding="utf-8")
old = "html.index('<!-- Changelog -->')"
if test_source.count(old) != 2:
    raise SystemExit(f"unexpected Statistics boundary count: {test_source.count(old)}")
test_source = test_source.replace(old, "html.index('<!-- Help -->')")
tests.write_text(test_source, encoding="utf-8")

print("frontend reachability cleanup applied")
