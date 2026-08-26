from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
TESTS = ROOT / "backend" / "tests"


def replace_exact(path: Path, old: str, new: str, expected: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != expected:
        raise SystemExit(f"{path}: expected {expected} occurrence(s) of {old!r}, found {count}")
    path.write_text(text.replace(old, new), encoding="utf-8")


def insert_before(path: Path, marker: str, insertion: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(marker)
    if count != 1:
        raise SystemExit(f"{path}: expected one marker {marker!r}, found {count}")
    path.write_text(text.replace(marker, insertion + marker), encoding="utf-8")


# 1) Activity Log keeps the universal 51px box but optically scales only the
# single-document artwork to match the painted footprint of the other feature icons.
feature = STATIC / "ui-feature-icon-contract.css"
insert_before(
    feature,
    "/* Dominant SVG colors, taken from the actual custom artwork. */\n",
    "/* document.svg paints a larger square tile inside the same source canvas than\n"
    "   the other feature artwork. Preserve the universal 51px layout box while\n"
    "   reducing only its internal painted footprint by roughly twelve percent. */\n"
    "body.dp-v11-structural #view-events .dp-activity-title-icon {\n"
    "  padding: 3px !important;\n"
    "}\n\n",
)

# 2) Downloads Refresh joins the existing Recover All / Activity Refresh visual
# contract. Downloads owns only its header placement; the shared layer owns material.
controls = STATIC / "ui-dashboard-control-polish.css"
replace_exact(
    controls,
    "body.dp-v11-structural #view-dashboard #btn-import-existing,\n"
    "body.dp-v11-structural #view-dashboard #btn-recover-all,\n"
    "body.dp-v11-structural #view-events .dp-activity-refresh {\n",
    "body.dp-v11-structural #view-dashboard #btn-import-existing,\n"
    "body.dp-v11-structural #view-dashboard #btn-recover-all,\n"
    "body.dp-v11-structural #view-events .dp-activity-refresh,\n"
    "body.dp-v11-structural #view-torrents .dp-downloads-refresh {\n",
)
replace_exact(
    controls,
    "body.dp-v11-structural #view-dashboard #btn-recover-all,\n"
    "body.dp-v11-structural #view-events .dp-activity-refresh {\n",
    "body.dp-v11-structural #view-dashboard #btn-recover-all,\n"
    "body.dp-v11-structural #view-events .dp-activity-refresh,\n"
    "body.dp-v11-structural #view-torrents .dp-downloads-refresh {\n",
)
replace_exact(
    controls,
    "body.light.dp-v11-structural #view-dashboard #btn-recover-all,\n"
    "body.light.dp-v11-structural #view-events .dp-activity-refresh {\n",
    "body.light.dp-v11-structural #view-dashboard #btn-recover-all,\n"
    "body.light.dp-v11-structural #view-events .dp-activity-refresh,\n"
    "body.light.dp-v11-structural #view-torrents .dp-downloads-refresh {\n",
)
replace_exact(
    controls,
    "body.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon {\n",
    "body.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon,\n"
    "body.dp-v11-structural #view-torrents .dp-downloads-refresh .dp-utility-icon {\n",
)
replace_exact(
    controls,
    "body.light.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon {\n",
    "body.light.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon,\n"
    "body.light.dp-v11-structural #view-torrents .dp-downloads-refresh .dp-utility-icon {\n",
)
replace_exact(
    controls,
    "body.dp-v11-structural #view-dashboard #btn-recover-all:hover,\n"
    "body.dp-v11-structural #view-events .dp-activity-refresh:hover {\n",
    "body.dp-v11-structural #view-dashboard #btn-recover-all:hover,\n"
    "body.dp-v11-structural #view-events .dp-activity-refresh:hover,\n"
    "body.dp-v11-structural #view-torrents .dp-downloads-refresh:hover {\n",
)
replace_exact(
    controls,
    "body.light.dp-v11-structural #view-dashboard #btn-recover-all:hover,\n"
    "body.light.dp-v11-structural #view-events .dp-activity-refresh:hover {\n",
    "body.light.dp-v11-structural #view-dashboard #btn-recover-all:hover,\n"
    "body.light.dp-v11-structural #view-events .dp-activity-refresh:hover,\n"
    "body.light.dp-v11-structural #view-torrents .dp-downloads-refresh:hover {\n",
)
replace_exact(
    controls,
    "body.dp-v11-structural #view-dashboard #btn-recover-all .dp-utility-icon,\n"
    "body.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon {\n",
    "body.dp-v11-structural #view-dashboard #btn-recover-all .dp-utility-icon,\n"
    "body.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon,\n"
    "body.dp-v11-structural #view-torrents .dp-downloads-refresh .dp-utility-icon {\n",
)
replace_exact(
    controls,
    "body.light.dp-v11-structural #view-dashboard #btn-recover-all .dp-utility-icon,\n"
    "body.light.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon {\n",
    "body.light.dp-v11-structural #view-dashboard #btn-recover-all .dp-utility-icon,\n"
    "body.light.dp-v11-structural #view-events .dp-activity-refresh .dp-utility-icon,\n"
    "body.light.dp-v11-structural #view-torrents .dp-downloads-refresh .dp-utility-icon {\n",
)

page = STATIC / "ui-downloads-page.css"
replace_exact(
    page,
    "/* Refresh is an icon-only header action. Material remains universal. */\n"
    "body.dp-v11-structural #view-torrents .dp-downloads-refresh {\n"
    "  display: inline-grid !important;\n"
    "  place-items: center;\n"
    "  width: 38px;\n"
    "  min-width: 38px;\n"
    "  height: 34px;\n"
    "  min-height: 34px;\n"
    "  margin-left: 8px !important;\n"
    "  padding: 0 !important;\n"
    "}\n\n"
    "body.dp-v11-structural #view-torrents .dp-downloads-refresh svg {\n"
    "  width: 16px;\n"
    "  height: 16px;\n"
    "  stroke-width: 2;\n"
    "}\n\n",
    "/* Refresh consumes the shared Recover All / Activity Refresh material.\n"
    "   Downloads owns placement only. */\n"
    "body.dp-v11-structural #view-torrents .dp-downloads-refresh {\n"
    "  margin-left: 8px !important;\n"
    "}\n\n",
)

insert_before(
    page,
    "/* ── Search band ─────────────────────────────────────────────────────── */\n",
    "/* ── Contextual multi-selection toolbar ─────────────────────────────── */\n"
    "/* Promote the inherited flat accent banner into a standard header-only\n"
    "   card. The universal dp-card / dp-card__header language owns material and\n"
    "   framing; this page layer owns only Downloads composition. */\n"
    "body.dp-v11-structural #view-torrents #bulk-bar.dp-downloads-bulk-card {\n"
    "  display: none;\n"
    "  flex: 0 0 auto;\n"
    "  margin: 0 0 10px !important;\n"
    "  padding: 0 !important;\n"
    "  gap: 0 !important;\n"
    "  background: var(--dp-panel-surface) !important;\n"
    "  color: var(--dp-text-primary) !important;\n"
    "  font-size: inherit !important;\n"
    "  font-weight: inherit !important;\n"
    "}\n"
    "body.dp-v11-structural #view-torrents #bulk-bar.dp-downloads-bulk-card.visible {\n"
    "  display: block !important;\n"
    "}\n"
    "body.dp-v11-structural #view-torrents .dp-downloads-bulk-toolbar {\n"
    "  width: 100%;\n"
    "  min-height: 54px;\n"
    "  justify-content: space-between;\n"
    "  gap: 16px;\n"
    "}\n"
    "body.dp-v11-structural #view-torrents .dp-downloads-bulk-actions {\n"
    "  display: flex;\n"
    "  align-items: center;\n"
    "  gap: 7px;\n"
    "  min-width: 0;\n"
    "}\n"
    "body.dp-v11-structural #view-torrents .dp-downloads-bulk-separator {\n"
    "  width: 1px;\n"
    "  height: 24px;\n"
    "  flex: 0 0 1px;\n"
    "  margin: 0 3px;\n"
    "  background: var(--dp-divider);\n"
    "}\n"
    "body.dp-v11-structural #view-torrents .dp-downloads-bulk-action {\n"
    "  min-height: 36px !important;\n"
    "  height: 36px !important;\n"
    "  padding: 0 12px !important;\n"
    "  border-radius: 8px !important;\n"
    "  font-size: 11.5px !important;\n"
    "  line-height: 1 !important;\n"
    "  font-weight: 600 !important;\n"
    "}\n"
    "body.dp-v11-structural #view-torrents .dp-downloads-bulk-action .dp-utility-icon {\n"
    "  width: 16px;\n"
    "  height: 16px;\n"
    "  stroke-width: 2;\n"
    "}\n"
    "body.dp-v11-structural #view-torrents .dp-downloads-bulk-action--clear {\n"
    "  color: var(--dp-text-secondary);\n"
    "}\n"
    "body.dp-v11-structural #view-torrents .dp-downloads-bulk-status {\n"
    "  margin-left: auto;\n"
    "  padding-left: 12px;\n"
    "  color: var(--dp-text-muted);\n"
    "  font-size: 12px;\n"
    "  line-height: 1;\n"
    "  font-weight: 600;\n"
    "  font-variant-numeric: tabular-nums;\n"
    "  white-space: nowrap;\n"
    "}\n\n",
)

# 3) Bulk semantic buttons consume the exact same Pause / Resume / Retry material
# as list-row actions. Delete remains the standard btn-danger material.
transfer = STATIC / "ui-transfer-contract.css"
replace_exact(
    transfer,
    "body.dp-v11-structural :is(#dash-tbody, #t-tbody) button:is([onclick*=\"pauseT(\"], [onclick*=\"pauseTorrent(\"]) {\n",
    "body.dp-v11-structural :is(#dash-tbody, #t-tbody) button:is([onclick*=\"pauseT(\"], [onclick*=\"pauseTorrent(\"]),\n"
    "body.dp-v11-structural #view-torrents .dp-downloads-bulk-action--pause {\n",
)
replace_exact(
    transfer,
    "body.light.dp-v11-structural :is(#dash-tbody, #t-tbody) button:is([onclick*=\"pauseT(\"], [onclick*=\"pauseTorrent(\"]) {\n",
    "body.light.dp-v11-structural :is(#dash-tbody, #t-tbody) button:is([onclick*=\"pauseT(\"], [onclick*=\"pauseTorrent(\"]),\n"
    "body.light.dp-v11-structural #view-torrents .dp-downloads-bulk-action--pause {\n",
)
replace_exact(
    transfer,
    "body.light.dp-v11-structural :is(#dash-tbody, #t-tbody) button:is([onclick*=\"pauseT(\"], [onclick*=\"pauseTorrent(\"]):hover {\n",
    "body.light.dp-v11-structural :is(#dash-tbody, #t-tbody) button:is([onclick*=\"pauseT(\"], [onclick*=\"pauseTorrent(\"]):hover,\n"
    "body.light.dp-v11-structural #view-torrents .dp-downloads-bulk-action--pause:hover {\n",
)
replace_exact(
    transfer,
    "body.dp-v11-structural :is(#dash-tbody, #t-tbody) button:is([onclick*=\"resumeT(\"], [onclick*=\"resumeTorrent(\"]) {\n",
    "body.dp-v11-structural :is(#dash-tbody, #t-tbody) button:is([onclick*=\"resumeT(\"], [onclick*=\"resumeTorrent(\"]),\n"
    "body.dp-v11-structural #view-torrents .dp-downloads-bulk-action--resume {\n",
)
replace_exact(
    transfer,
    "body.light.dp-v11-structural :is(#dash-tbody, #t-tbody) button:is([onclick*=\"resumeT(\"], [onclick*=\"resumeTorrent(\"]) {\n",
    "body.light.dp-v11-structural :is(#dash-tbody, #t-tbody) button:is([onclick*=\"resumeT(\"], [onclick*=\"resumeTorrent(\"]),\n"
    "body.light.dp-v11-structural #view-torrents .dp-downloads-bulk-action--resume {\n",
)
replace_exact(
    transfer,
    "body.dp-v11-structural :is(#dash-tbody, #t-tbody) button:is([onclick*=\"retryT(\"], [onclick*=\"retryTorrent(\"]) {\n",
    "body.dp-v11-structural :is(#dash-tbody, #t-tbody) button:is([onclick*=\"retryT(\"], [onclick*=\"retryTorrent(\"]),\n"
    "body.dp-v11-structural #view-torrents .dp-downloads-bulk-action--reset {\n",
)

# 4) Downloads presentation runtime: exact shared refresh glyph, normalized bulk
# composition/glyphs, and state-aware Previous/current/Next pagination only.
runtime = STATIC / "ui-downloads-runtime.js"
replace_exact(
    runtime,
    "      chevronLeft: '<path d=\"m15 18-6-6 6-6\"/>',\n"
    "      chevronRight: '<path d=\"m9 18 6-6-6-6\"/>',\n"
    "      refresh: '<path d=\"M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16\"/><path d=\"M3 21v-5h5\"/><path d=\"M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8\"/><path d=\"M21 3v5h-5\"/>'\n",
    "      chevronLeft: '<path d=\"m15 18-6-6 6-6\"/>',\n"
    "      chevronRight: '<path d=\"m9 18 6-6-6-6\"/>',\n"
    "      refresh: '<path d=\"M20 11a8.1 8.1 0 0 0-15.5-2M4 4v5h5\"/><path d=\"M4 13a8.1 8.1 0 0 0 15.5 2M20 20v-5h-5\"/>',\n"
    "      pause: '<rect x=\"14\" y=\"3\" width=\"5\" height=\"18\" rx=\"1\"/><rect x=\"5\" y=\"3\" width=\"5\" height=\"18\" rx=\"1\"/>',\n"
    "      play: '<path d=\"M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z\"/>',\n"
    "      trash: '<path d=\"M3 6h18\"/><path d=\"M8 6V4h8v2\"/><path d=\"m19 6-1 14H6L5 6\"/><path d=\"M10 11v5\"/><path d=\"M14 11v5\"/>',\n"
    "      x: '<path d=\"M18 6 6 18\"/><path d=\"m6 6 12 12\"/>'\n",
)
insert_before(
    runtime,
    "  function filterStatusFromTab(tab) {\n",
    "  function setBulkButtonPresentation(button, label, iconName, semanticClass) {\n"
    "    if (!button || button.dataset.pending === '1' || button.getAttribute('aria-busy') === 'true') return;\n"
    "    button.removeAttribute('style');\n"
    "    button.classList.remove('btn-sm', 'btn-blue', 'btn-ghost', 'btn-danger');\n"
    "    button.classList.add('btn', 'dp-downloads-bulk-action', semanticClass);\n"
    "    if (semanticClass === 'dp-downloads-bulk-action--delete') button.classList.add('btn-danger');\n"
    "    button.dataset.defaultLabel = label;\n"
    "    const expectedIcon = button.querySelector('[data-dp-bulk-icon=\"' + iconName + '\"]');\n"
    "    const expectedLabel = button.querySelector('[data-dp-bulk-label]');\n"
    "    if (expectedIcon && expectedLabel && expectedLabel.textContent === label) return;\n"
    "    button.innerHTML = utilitySvg(iconName).replace('class=\"lucide dp-utility-icon\"', 'class=\"lucide dp-utility-icon\" data-dp-bulk-icon=\"' + iconName + '\"');\n"
    "    const span = document.createElement('span');\n"
    "    span.dataset.dpBulkLabel = '1';\n"
    "    span.textContent = label;\n"
    "    button.appendChild(span);\n"
    "  }\n\n"
    "  function syncBulkButtonPresentation(bar) {\n"
    "    if (!bar) return;\n"
    "    const buttons = Array.from(bar.querySelectorAll('button'));\n"
    "    const find = needle => buttons.find(button => (button.getAttribute('onclick') || '').includes(needle));\n"
    "    setBulkButtonPresentation(find(\"bulkAction('pause'\"), 'Pause', 'pause', 'dp-downloads-bulk-action--pause');\n"
    "    setBulkButtonPresentation(find(\"bulkAction('resume'\"), 'Resume', 'play', 'dp-downloads-bulk-action--resume');\n"
    "    setBulkButtonPresentation(find(\"bulkAction('reset'\"), 'Reset', 'refresh', 'dp-downloads-bulk-action--reset');\n"
    "    setBulkButtonPresentation(find(\"bulkAction('delete'\"), 'Delete', 'trash', 'dp-downloads-bulk-action--delete');\n"
    "    setBulkButtonPresentation(find('clearSelection()'), 'Clear selection', 'x', 'dp-downloads-bulk-action--clear');\n"
    "  }\n\n"
    "  function decorateBulkSelectionToolbar() {\n"
    "    const bar = document.getElementById('bulk-bar');\n"
    "    const count = document.getElementById('bulk-count');\n"
    "    if (!bar || !count) return;\n"
    "    if (bar.dataset.dpDownloadsBulk === '1') {\n"
    "      syncBulkButtonPresentation(bar);\n"
    "      return;\n"
    "    }\n"
    "    const buttons = Array.from(bar.querySelectorAll('button'));\n"
    "    const find = needle => buttons.find(button => (button.getAttribute('onclick') || '').includes(needle));\n"
    "    const pause = find(\"bulkAction('pause'\");\n"
    "    const resume = find(\"bulkAction('resume'\");\n"
    "    const reset = find(\"bulkAction('reset'\");\n"
    "    const remove = find(\"bulkAction('delete'\");\n"
    "    const clear = find('clearSelection()');\n"
    "    if (![pause, resume, reset, remove, clear].every(Boolean)) return;\n"
    "    bar.classList.add('dp-card', 'dp-downloads-bulk-card');\n"
    "    const header = document.createElement('div');\n"
    "    header.className = 'dp-card__header dp-downloads-bulk-toolbar';\n"
    "    const actions = document.createElement('div');\n"
    "    actions.className = 'dp-downloads-bulk-actions';\n"
    "    const separator = document.createElement('span');\n"
    "    separator.className = 'dp-downloads-bulk-separator';\n"
    "    separator.setAttribute('aria-hidden', 'true');\n"
    "    const status = document.createElement('div');\n"
    "    status.className = 'dp-downloads-bulk-status';\n"
    "    count.classList.add('dp-downloads-bulk-count');\n"
    "    actions.append(pause, resume, reset, separator, remove, clear);\n"
    "    status.appendChild(count);\n"
    "    header.append(actions, status);\n"
    "    bar.replaceChildren(header);\n"
    "    bar.dataset.dpDownloadsBulk = '1';\n"
    "    syncBulkButtonPresentation(bar);\n"
    "    new MutationObserver(function () { syncBulkButtonPresentation(bar); })\n"
    "      .observe(header, {childList: true, subtree: true, characterData: true});\n"
    "  }\n\n",
)
replace_exact(
    runtime,
    "      refresh.innerHTML = utilitySvg('refresh');\n",
    "      refresh.dataset.defaultLabel = 'Refresh';\n"
    "      refresh.innerHTML = utilitySvg('refresh') + '<span>Refresh</span>';\n",
)
replace_exact(
    runtime,
    "      const pages = [];\n"
    "      if (totalPages <= 7) {\n"
    "        for (let i = 1; i <= totalPages; i += 1) pages.push(i);\n"
    "      } else {\n"
    "        pages.push(1);\n"
    "        const start = Math.max(2, cur - 2);\n"
    "        const end = Math.min(totalPages - 1, cur + 2);\n"
    "        if (start > 2) pages.push('...');\n"
    "        for (let i = start; i <= end; i += 1) pages.push(i);\n"
    "        if (end < totalPages - 1) pages.push('...');\n"
    "        pages.push(totalPages);\n"
    "      }\n\n"
    "      const previous =\n"
    "        '<button type=\"button\" class=\"dp-pager-btn\" aria-label=\"Previous page\"' +\n"
    "        (cur <= 1 ? ' disabled' : '') +\n"
    "        ' onclick=\"goToTorrentPage(' + (cur - 1) + ')\">' + utilitySvg('chevronLeft') + '</button>';\n\n"
    "      const numbered = pages.map(function (page) {\n"
    "        if (page === '...') return '<span class=\"dp-pager-ellipsis\" aria-hidden=\"true\">…</span>';\n"
    "        const current = page === cur;\n"
    "        return '<button type=\"button\" class=\"dp-pager-btn' + (current ? ' dp-pager-current' : '') + '\"' +\n"
    "          (current ? ' aria-current=\"page\"' : '') +\n"
    "          ' aria-label=\"Page ' + page + '\" onclick=\"goToTorrentPage(' + page + ')\">' + page + '</button>';\n"
    "      }).join('');\n\n"
    "      const next =\n"
    "        '<button type=\"button\" class=\"dp-pager-btn\" aria-label=\"Next page\"' +\n"
    "        (cur >= totalPages ? ' disabled' : '') +\n"
    "        ' onclick=\"goToTorrentPage(' + (cur + 1) + ')\">' + utilitySvg('chevronRight') + '</button>';\n\n"
    "      btns.innerHTML = previous + numbered + next;\n",
    "      const controls = [];\n"
    "      if (cur > 1) {\n"
    "        controls.push(\n"
    "          '<button type=\"button\" class=\"dp-pager-btn\" aria-label=\"Previous page\"' +\n"
    "          ' onclick=\"goToTorrentPage(' + (cur - 1) + ')\">' + utilitySvg('chevronLeft') + '</button>'\n"
    "        );\n"
    "      }\n"
    "      controls.push(\n"
    "        '<button type=\"button\" class=\"dp-pager-btn dp-pager-current\" aria-current=\"page\"' +\n"
    "        ' aria-label=\"Page ' + cur + ', current page\">' + cur + '</button>'\n"
    "      );\n"
    "      if (cur < totalPages) {\n"
    "        controls.push(\n"
    "          '<button type=\"button\" class=\"dp-pager-btn\" aria-label=\"Next page\"' +\n"
    "          ' onclick=\"goToTorrentPage(' + (cur + 1) + ')\">' + utilitySvg('chevronRight') + '</button>'\n"
    "        );\n"
    "      }\n"
    "      btns.innerHTML = controls.join('');\n",
)
replace_exact(
    runtime,
    "    installFilterWrapper();\n    ensureDownloadFilters();\n    decorateDownloadsStructure();\n",
    "    installFilterWrapper();\n    ensureDownloadFilters();\n    decorateBulkSelectionToolbar();\n    decorateDownloadsStructure();\n",
)

# Targeted cache invalidation for all changed presentation assets/runtimes.
style = STATIC / "style-v11.css"
for old, new in (
    ("/ui-dashboard-control-polish.css?v=22", "/ui-dashboard-control-polish.css?v=23"),
    ("/ui-downloads-page.css?v=25", "/ui-downloads-page.css?v=26"),
    ("/ui-feature-icon-contract.css?v=1", "/ui-feature-icon-contract.css?v=2"),
    ("/ui-transfer-contract.css?v=30", "/ui-transfer-contract.css?v=31"),
):
    replace_exact(style, old, new)

operator = STATIC / "operator-title.js"
replace_exact(operator, "/ui-downloads-runtime.js?v=20", "/ui-downloads-runtime.js?v=21")
index = STATIC / "index.html"
replace_exact(index, "/operator-title.js?v=21", "/operator-title.js?v=22")

# Update cache-generation regression contracts globally within backend tests.
version_replacements = (
    ("/ui-dashboard-control-polish.css?v=22", "/ui-dashboard-control-polish.css?v=23"),
    ("/ui-downloads-page.css?v=25", "/ui-downloads-page.css?v=26"),
    ("/ui-feature-icon-contract.css?v=1", "/ui-feature-icon-contract.css?v=2"),
    ("/ui-transfer-contract.css?v=30", "/ui-transfer-contract.css?v=31"),
    ("/operator-title.js?v=21", "/operator-title.js?v=22"),
    ("/ui-downloads-runtime.js?v=20", "/ui-downloads-runtime.js?v=21"),
    ('"/ui-dashboard-control-polish.css": "22"', '"/ui-dashboard-control-polish.css": "23"'),
    ('"/ui-downloads-page.css": "25"', '"/ui-downloads-page.css": "26"'),
    ('"/ui-feature-icon-contract.css": "1"', '"/ui-feature-icon-contract.css": "2"'),
    ('"/ui-transfer-contract.css": "30"', '"/ui-transfer-contract.css": "31"'),
)
for test in TESTS.rglob("test_*.py"):
    text = test.read_text(encoding="utf-8")
    updated = text
    for old, new in version_replacements:
        updated = updated.replace(old, new)
    if updated != text:
        test.write_text(updated, encoding="utf-8")

# Add explicit contracts for this reviewed correction batch.
contract = TESTS / "test_ui_downloads_correction_batch_contract.py"
contract.write_text('''"""Final Dashboard / Downloads / Activity correction batch contracts."""\n\nfrom pathlib import Path\n\nROOT = Path(__file__).resolve().parents[2]\nSTATIC = ROOT / "frontend" / "static"\n\n\ndef read(name: str) -> str:\n    return (STATIC / name).read_text(encoding="utf-8")\n\n\ndef test_activity_document_keeps_51px_box_with_optical_padding_only() -> None:\n    css = read("ui-feature-icon-contract.css")\n    assert "--dp-feature-icon-size: 51px" in css\n    assert "#view-events .dp-activity-title-icon" in css\n    assert "padding: 3px !important" in css\n\n\ndef test_downloads_refresh_uses_shared_recovery_control_and_exact_glyph() -> None:\n    controls = read("ui-dashboard-control-polish.css")\n    page = read("ui-downloads-page.css")\n    downloads = read("ui-downloads-runtime.js")\n    runtime = read("ui-runtime.js")\n    geometry = 'M20 11a8.1 8.1 0 0 0-15.5-2M4 4v5h5'\n    assert geometry in runtime\n    assert geometry in downloads\n    assert "#view-torrents .dp-downloads-refresh" in controls\n    assert "refresh.innerHTML = utilitySvg('refresh') + '<span>Refresh</span>';" in downloads\n    assert "width: 38px" not in page\n    assert "height: 34px" not in page\n\n\ndef test_bulk_selection_is_header_only_card_with_reviewed_action_order() -> None:\n    runtime = read("ui-downloads-runtime.js")\n    page = read("ui-downloads-page.css")\n    transfer = read("ui-transfer-contract.css")\n    assert "bar.classList.add('dp-card', 'dp-downloads-bulk-card')" in runtime\n    assert "header.className = 'dp-card__header dp-downloads-bulk-toolbar'" in runtime\n    assert "actions.append(pause, resume, reset, separator, remove, clear);" in runtime\n    assert "status.appendChild(count);" in runtime\n    for icon in ("pause", "play", "refresh", "trash", "x"):\n        assert f"data-dp-bulk-icon=\\\"' + icon" not in runtime  # dynamic marker remains generic\n    for cls in (\n        "dp-downloads-bulk-action--pause",\n        "dp-downloads-bulk-action--resume",\n        "dp-downloads-bulk-action--reset",\n        "dp-downloads-bulk-action--delete",\n        "dp-downloads-bulk-action--clear",\n    ):\n        assert cls in runtime\n    assert "#bulk-bar.dp-downloads-bulk-card.visible" in page\n    assert "dp-downloads-bulk-separator" in page\n    assert "dp-downloads-bulk-status" in page\n    assert "dp-downloads-bulk-action--pause" in transfer\n    assert "dp-downloads-bulk-action--resume" in transfer\n    assert "dp-downloads-bulk-action--reset" in transfer\n\n\ndef test_pagination_renders_only_applicable_neighbors_and_current_page() -> None:\n    runtime = read("ui-downloads-runtime.js")\n    assert "if (cur > 1)" in runtime\n    assert "if (cur < totalPages)" in runtime\n    assert "aria-current=\\\"page\\\"" in runtime\n    assert "btns.innerHTML = controls.join('');" in runtime\n    assert "const pages = []" not in runtime\n    assert "cur <= 1 ? ' disabled'" not in runtime\n    assert "cur >= totalPages ? ' disabled'" not in runtime\n\n\ndef test_batch_cache_generations_are_explicit() -> None:\n    style = read("style-v11.css")\n    operator = read("operator-title.js")\n    index = read("index.html")\n    assert "/ui-dashboard-control-polish.css?v=23" in style\n    assert "/ui-downloads-page.css?v=26" in style\n    assert "/ui-feature-icon-contract.css?v=2" in style\n    assert "/ui-transfer-contract.css?v=31" in style\n    assert "/ui-downloads-runtime.js?v=21" in operator\n    assert "/operator-title.js?v=22" in index\n''', encoding="utf-8")

# Backend version is explicitly frozen for this UI line.
version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
if version != "1.0.10":
    raise SystemExit(f"VERSION drifted: expected 1.0.10, got {version}")
