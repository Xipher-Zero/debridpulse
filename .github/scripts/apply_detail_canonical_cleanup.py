from pathlib import Path

ROOT = Path.cwd().resolve()
STATIC = ROOT / "frontend" / "static"
TESTS = ROOT / "backend" / "tests"


def replace_exact(path: Path, old: str, new: str, expected: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != expected:
        raise SystemExit(f"{path}: expected {expected} occurrence(s) of {old!r}, found {count}")
    path.write_text(text.replace(old, new), encoding="utf-8")


app = STATIC / "app.js"
replace_exact(
    app,
    "      const total = Object.values(bs).reduce((a,b)=>a+b,0);\n",
    "      // Soft-deleted rows remain in /stats for diagnostics/duplicate revival,\n"
    "      // but they are intentionally absent from the normal All Downloads view.\n"
    "      // User-facing totals and Queue Health therefore use the same visible universe.\n"
    "      const total = Object.entries(bs)\n"
    "        .filter(([status]) => status !== 'deleted')\n"
    "        .reduce((sum, [, count]) => sum + (Number(count) || 0), 0);\n",
)
replace_exact(
    app,
    "    cnt.textContent = _selectedIds.size + ' selected';\n",
    "    cnt.textContent = _selectedIds.size + ' Selected';\n",
)

old_files = '''      ${t.files&&t.files.length?`\n        <div class="sec-label">Files (${t.files.length})</div>\n        <div class="card">\n          <table>\n            <thead><tr><th>Filename</th><th>Size</th><th>Status</th></tr></thead>\n            <tbody>${t.files.map(f=>`<tr>\n              <td style="font-family:var(--mono);font-size:11px">${esc(f.filename)}\n                ${f.blocked\n                  ? `<span class="badge badge-error" style="font-size:9px;margin-left:6px">BLOCKED: ${esc(f.block_reason)}</span>`\n                  : (f.block_reason ? `<div style="font-size:10px;color:var(--red);margin-top:4px">${esc(f.block_reason)}</div>` : '')}\n              </td>\n              <td class="sz">${fmtSize(f.size_bytes)}</td>\n              <td>${badge(f.status)}</td>\n            </tr>`).join('')}</tbody>\n          </table>\n        </div>\n      `:''}\n'''
new_files = '''      ${t.files&&t.files.length?`\n        <div class="card dp-detail-section-card dp-detail-files-card">\n          <div class="card-header">\n            <span class="card-title">Files (${t.files.length})</span>\n          </div>\n          <div class="dp-detail-table-wrap">\n            <table class="t-table">\n              <thead><tr><th>Filename</th><th>Size</th><th>Status</th></tr></thead>\n              <tbody>${t.files.map(f=>`<tr>\n                <td class="dp-detail-filename">${esc(f.filename)}\n                  ${f.blocked\n                    ? `<span class="badge badge-error" style="font-size:9px;margin-left:6px">BLOCKED: ${esc(f.block_reason)}</span>`\n                    : (f.block_reason ? `<div style="font-size:10px;color:var(--red);margin-top:4px">${esc(f.block_reason)}</div>` : '')}\n                </td>\n                <td class="sz">${fmtSize(f.size_bytes)}</td>\n                <td>${badge(f.status)}</td>\n              </tr>`).join('')}</tbody>\n            </table>\n          </div>\n        </div>\n      `:''}\n'''
replace_exact(app, old_files, new_files)

old_events = '''      ${t.events&&t.events.length?`\n        <div class="sec-label">Events</div>\n        ${t.events.map(ev=>`\n          <div class="event-item">\n            <div class="elevel ${esc(ev.level)}"></div>\n            <div class="emsg">${esc(ev.message)}</div>\n            <div class="etime">${fmtDate(ev.created_at)}</div>\n          </div>`).join('')}\n      `:''}\n'''
new_events = '''      ${t.events&&t.events.length?`\n        <div class="card dp-detail-section-card dp-detail-events-card">\n          <div class="card-header">\n            <span class="card-title">Events</span>\n          </div>\n          <div class="dp-detail-events-list">\n            ${t.events.map(ev=>`\n              <div class="event-item">\n                <div class="elevel ${esc(ev.level)}"></div>\n                <div class="emsg">${esc(ev.message)}</div>\n                <div class="etime">${fmtDate(ev.created_at)}</div>\n              </div>`).join('')}\n          </div>\n        </div>\n      `:''}\n'''
replace_exact(app, old_events, new_events)

index = STATIC / "index.html"
old_modal = '''<div id="overlay" onclick="closeModal(event)">\n  <div id="modal">\n    <div class="modal-hdr">\n      <span class="modal-title" id="modal-title">Details</span>\n      <span class="modal-close" onclick="closeModal()">✕</span>\n    </div>\n    <div class="modal-body" id="modal-body"></div>\n  </div>\n</div>\n'''
new_modal = '''<div id="overlay" onclick="closeModal(event)">\n  <div id="modal" class="dp-card dp-detail-modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">\n    <div class="modal-hdr dp-card__header">\n      <span class="modal-title dp-card__title" id="modal-title">Details</span>\n      <button type="button" class="btn modal-close dp-detail-close" onclick="closeModal()" aria-label="Close details" title="Close details">\n        <svg class="lucide dp-utility-icon" xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>\n      </button>\n    </div>\n    <div class="modal-body dp-card__body" id="modal-body"></div>\n  </div>\n</div>\n'''
replace_exact(index, old_modal, new_modal)

runtime = STATIC / "ui-downloads-runtime.js"
replace_exact(
    runtime,
    "    setBulkButtonPresentation(find('clearSelection()'), 'Clear selection', 'x', 'dp-downloads-bulk-action--clear');\n",
    "    setBulkButtonPresentation(find('clearSelection()'), 'Clear Selections', 'x', 'dp-downloads-bulk-action--clear');\n",
)
replace_exact(
    runtime,
    "    actions.append(pause, resume, reset, separator, remove, clear);\n    status.appendChild(count);\n",
    "    actions.append(pause, resume, reset, separator, remove);\n    status.append(count, clear);\n",
)

page = STATIC / "ui-downloads-page.css"
replace_exact(
    page,
    "body.dp-v11-structural #view-torrents .dp-downloads-bulk-status {\n  margin-left: auto;\n",
    "body.dp-v11-structural #view-torrents .dp-downloads-bulk-status {\n  display: flex;\n  align-items: center;\n  gap: 10px;\n  margin-left: auto;\n",
)

modal = STATIC / "ui-modal-contract.css"
modal_text = modal.read_text(encoding="utf-8")
modal_append = r'''

/* ── Canonical Details card material / composition ───────────────────── */
/* The Details overlay is a modal presentation of the same card language used
   by Dashboard, Downloads and Activity. The universal dp-card primitives own
   the material; this layer supplies only dialog geometry and scoped composition. */
body.dp-v11-structural #overlay {
  padding: 24px;
  background: rgba(5, 8, 20, .74);
  backdrop-filter: blur(8px) saturate(.92);
}
body.light.dp-v11-structural #overlay {
  background: rgba(42, 34, 60, .28);
  backdrop-filter: blur(8px) saturate(.88);
}

body.dp-v11-structural #modal.dp-detail-modal {
  width: 90%;
  max-width: 620px;
  max-height: 82vh;
  margin: 0;
  border: 1px solid transparent !important;
  border-radius: var(--dp-radius-lg) !important;
  background: var(--dp-panel-surface) !important;
  box-shadow:
    var(--dp-panel-shadow),
    0 24px 70px rgba(0, 0, 12, .42) !important;
}
body.light.dp-v11-structural #modal.dp-detail-modal {
  box-shadow:
    var(--dp-panel-shadow),
    0 22px 58px rgba(45, 39, 68, .18) !important;
}

body.dp-v11-structural #modal .modal-hdr.dp-card__header {
  min-height: 58px;
  padding: 0 17px;
  border-bottom-color: var(--dp-panel-header-border);
  background: var(--dp-panel-header-surface);
}
body.dp-v11-structural #modal .modal-title.dp-card__title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
body.dp-v11-structural #modal .dp-detail-close.btn {
  width: 34px;
  min-width: 34px;
  height: 34px;
  min-height: 34px;
  flex: 0 0 34px;
  padding: 0;
  border-radius: 8px;
  color: var(--dp-text-secondary);
}
body.dp-v11-structural #modal .dp-detail-close .dp-utility-icon {
  width: 18px;
  height: 18px;
  stroke-width: 2;
}
body.dp-v11-structural #modal .dp-detail-close:hover {
  color: var(--dp-text-primary);
}

body.dp-v11-structural #modal .modal-body.dp-card__body {
  padding: var(--dp-card-padding);
  background: transparent;
}
body.dp-v11-structural #modal .detail-grid {
  gap: 14px 22px;
  margin-bottom: 18px;
}
body.dp-v11-structural #modal .dk {
  margin-bottom: 4px;
  color: var(--dp-text-secondary);
  font-family: var(--dp-font-sans);
  font-size: 10px;
  line-height: 1.2;
  font-weight: 700;
  letter-spacing: .09em;
}
body.dp-v11-structural #modal .dv {
  color: var(--dp-text-primary);
  font-family: var(--dp-font-mono);
  font-size: 12.5px;
  line-height: 1.45;
}

body.dp-v11-structural #modal .dp-detail-section-card {
  margin: 0 0 14px;
}
body.dp-v11-structural #modal .dp-detail-section-card:last-child {
  margin-bottom: 0;
}
body.dp-v11-structural #modal .dp-detail-section-card > .card-header {
  min-height: 44px;
}
body.dp-v11-structural #modal .dp-detail-section-card > .card-header .card-title {
  font-size: 13px;
  letter-spacing: .02em;
}

body.dp-v11-structural #modal .dp-detail-table-wrap {
  overflow-x: auto;
  background: transparent;
}
body.dp-v11-structural #modal .dp-detail-files-card .t-table {
  background: transparent;
}
body.dp-v11-structural #modal .dp-detail-files-card .dp-detail-filename {
  font-family: var(--dp-font-mono);
  font-size: 11px;
}

body.dp-v11-structural #modal .dp-detail-events-list {
  background: transparent;
}
body.dp-v11-structural #modal .dp-detail-events-list .event-item {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 42px;
  margin: 0;
  padding: 9px 14px;
  border: 0;
  border-bottom: 1px solid var(--dp-table-row-border);
  border-radius: 0;
  background: transparent;
  color: var(--dp-text-primary);
  box-shadow: none;
}
body.dp-v11-structural #modal .dp-detail-events-list .event-item:last-child {
  border-bottom: 0;
}
body.dp-v11-structural #modal .dp-detail-events-list .event-item:hover {
  background: var(--dp-table-row-hover);
}
body.dp-v11-structural #modal .dp-detail-events-list .emsg {
  color: var(--dp-text-primary);
  font-family: var(--dp-font-sans);
  font-size: 12px;
  line-height: 1.35;
}
body.dp-v11-structural #modal .dp-detail-events-list .etime {
  color: var(--dp-text-muted);
  font-family: var(--dp-font-mono);
  font-size: 10px;
}

@media (max-width: 700px) {
  body.dp-v11-structural #overlay { padding: 12px; }
  body.dp-v11-structural #modal.dp-detail-modal { width: 100%; max-height: 90vh; }
  body.dp-v11-structural #modal .detail-grid { grid-template-columns: 1fr; }
}
'''
if "Canonical Details card material / composition" in modal_text:
    raise SystemExit("canonical detail modal block already present")
modal.write_text(modal_text.rstrip() + modal_append + "\n", encoding="utf-8")

# Cache generations: every changed presentation asset advances explicitly.
style = STATIC / "style-v11.css"
replace_exact(style, "/ui-modal-contract.css?v=24", "/ui-modal-contract.css?v=25")
replace_exact(style, "/ui-downloads-page.css?v=26", "/ui-downloads-page.css?v=27")

operator = STATIC / "operator-title.js"
replace_exact(operator, "/ui-runtime.js?v=23", "/ui-runtime.js?v=24")
replace_exact(operator, "/ui-downloads-runtime.js?v=21", "/ui-downloads-runtime.js?v=22")

presentation_runtime = STATIC / "ui-runtime.js"
replace_exact(
    presentation_runtime,
    "if (!/style-v11\\.css\\?v=23$/.test(link.href)) link.href = '/style-v11.css?v=23';",
    "if (!/style-v11\\.css\\?v=24$/.test(link.href)) link.href = '/style-v11.css?v=24';",
)

replace_exact(index, "/style-v11.css?v=23", "/style-v11.css?v=24")
replace_exact(index, "/app.js?v=14", "/app.js?v=15")
replace_exact(index, "/operator-title.js?v=22", "/operator-title.js?v=23")
replace_exact(index, "/ui-runtime.js?v=23", "/ui-runtime.js?v=24")
replace_exact(index, "/ui-downloads-runtime.js?v=21", "/ui-downloads-runtime.js?v=22")

# Migrate cache-generation expectations in existing regression contracts.
version_replacements = (
    ("/style-v11.css?v=23", "/style-v11.css?v=24"),
    ("style-v11\\\\.css\\\\?v=23$", "style-v11\\\\.css\\\\?v=24$"),
    ("/app.js?v=14", "/app.js?v=15"),
    ("/operator-title.js?v=22", "/operator-title.js?v=23"),
    ("/ui-runtime.js?v=23", "/ui-runtime.js?v=24"),
    ("/ui-downloads-runtime.js?v=21", "/ui-downloads-runtime.js?v=22"),
    ("/ui-modal-contract.css?v=24", "/ui-modal-contract.css?v=25"),
    ("/ui-downloads-page.css?v=26", "/ui-downloads-page.css?v=27"),
    ('"/ui-modal-contract.css": "24"', '"/ui-modal-contract.css": "25"'),
    ('"/ui-downloads-page.css": "26"', '"/ui-downloads-page.css": "27"'),
)
for test in TESTS.rglob("test_*.py"):
    text = test.read_text(encoding="utf-8")
    updated = text
    for old, new in version_replacements:
        updated = updated.replace(old, new)
    if updated != text:
        test.write_text(updated, encoding="utf-8")

contract = TESTS / "test_ui_detail_overlay_cleanup_contract.py"
contract.write_text(r'''"""Canonical Details overlay and final Downloads cleanup contracts."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_visible_download_total_excludes_soft_deleted_rows() -> None:
    app = read("app.js")
    assert ".filter(([status]) => status !== 'deleted')" in app
    assert ".reduce((sum, [, count]) => sum + (Number(count) || 0), 0)" in app
    assert "const queuePct = pct(completed, total || 0);" in app


def test_bulk_toolbar_right_side_owns_count_and_clear_selection() -> None:
    app = read("app.js")
    runtime = read("ui-downloads-runtime.js")
    page = read("ui-downloads-page.css")
    assert "_selectedIds.size + ' Selected'" in app
    assert "'Clear Selections', 'x', 'dp-downloads-bulk-action--clear'" in runtime
    assert "actions.append(pause, resume, reset, separator, remove);" in runtime
    assert "status.append(count, clear);" in runtime
    assert "gap: 10px;" in page


def test_details_modal_consumes_canonical_card_primitives() -> None:
    index = read("index.html")
    modal = read("ui-modal-contract.css")
    assert 'id="modal" class="dp-card dp-detail-modal"' in index
    assert 'class="modal-hdr dp-card__header"' in index
    assert 'class="modal-title dp-card__title"' in index
    assert 'class="modal-body dp-card__body"' in index
    assert 'class="btn modal-close dp-detail-close"' in index
    assert 'M18 6 6 18' in index
    assert "background: var(--dp-panel-surface) !important;" in modal
    assert "background: var(--dp-panel-header-surface);" in modal
    assert "var(--dp-panel-shadow)" in modal


def test_details_files_and_events_are_canonical_section_cards() -> None:
    app = read("app.js")
    modal = read("ui-modal-contract.css")
    assert "card dp-detail-section-card dp-detail-files-card" in app
    assert "card dp-detail-section-card dp-detail-events-card" in app
    assert '<table class="t-table">' in app
    assert "dp-detail-events-list" in app
    assert "var(--dp-table-row-border)" in modal
    assert "var(--dp-table-row-hover)" in modal


def test_cleanup_cache_generations_are_explicit() -> None:
    index = read("index.html")
    style = read("style-v11.css")
    operator = read("operator-title.js")
    assert "/style-v11.css?v=24" in index
    assert "/app.js?v=15" in index
    assert "/operator-title.js?v=23" in index
    assert "/ui-runtime.js?v=24" in index
    assert "/ui-downloads-runtime.js?v=22" in index
    assert "/ui-modal-contract.css?v=25" in style
    assert "/ui-downloads-page.css?v=27" in style
    assert "/ui-runtime.js?v=24" in operator
    assert "/ui-downloads-runtime.js?v=22" in operator


def test_backend_version_remains_frozen() -> None:
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == "1.0.10"
''', encoding="utf-8")

if (ROOT / "VERSION").read_text(encoding="utf-8").strip() != "1.0.10":
    raise SystemExit("VERSION drifted from 1.0.10")
