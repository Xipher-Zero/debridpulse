from pathlib import Path

root = Path('.')
static = root / 'frontend' / 'static'
tests = root / 'backend' / 'tests'

# Remove the final remaining legacy title clobber from the data loader. The
# canonical Downloads presentation owner already owns Download Queue + subtitle;
# loadTorrents must not briefly restore the ADC-era title after each fetch.
app_path = static / 'app.js'
app = app_path.read_text(encoding='utf-8')
old = "    const title = document.getElementById('torrent-card-title');\n    if (title) title.textContent = `All Downloads (${torrentTotal})`;\n"
assert app.count(old) == 1, app.count(old)
app = app.replace(old, '', 1)
app = app.replace('normal All Downloads view', 'normal Downloads view')
assert '`All Downloads (${torrentTotal})`' not in app
app_path.write_text(app, encoding='utf-8')

# Cache generations are part of the final-owner contract; update all stale
# architecture tests in one guarded pass rather than retaining historical
# assertions for the superseded CSS generation.
for path in tests.glob('test_*.py'):
    text = path.read_text(encoding='utf-8')
    original = text
    text = text.replace('/ui-downloads-page.css?v=27', '/ui-downloads-page.css?v=28')
    text = text.replace('"/ui-downloads-page.css": "27"', '"/ui-downloads-page.css": "28"')
    text = text.replace('/style-v11.css?v=24', '/style-v11.css?v=25')
    if text != original:
        path.write_text(text, encoding='utf-8')

# Unified transfer contract: static accepted Downloads title is now direct.
path = tests / 'test_direct_links.py'
text = path.read_text(encoding='utf-8')
assert "self.assertIn('id=\"torrent-card-title\">All Downloads</span>', html)" in text
assert 'self.assertIn("`All Downloads (${torrentTotal})`", js)' in text
text = text.replace(
    "self.assertIn('id=\"torrent-card-title\">All Downloads</span>', html)",
    "self.assertIn('id=\"torrent-card-title\">Download Queue</span>', html)",
    1,
)
text = text.replace(
    'self.assertIn("`All Downloads (${torrentTotal})`", js)',
    'self.assertNotIn("`All Downloads (${torrentTotal})`", js)',
    1,
)
path.write_text(text, encoding='utf-8')

# Detail/bulk contract: toolbar structure is static index ownership. Runtime
# owns only icon/pending decoration, so it must not be required to construct it.
path = tests / 'test_ui_detail_overlay_cleanup_contract.py'
text = path.read_text(encoding='utf-8')
old_fn = '''def test_bulk_toolbar_right_side_owns_count_and_clear_selection() -> None:
    app = read("app.js")
    runtime = read("ui-downloads-runtime.js")
    page = read("ui-downloads-page.css")
    assert "_selectedIds.size + ' Selected'" in app
    assert "'Clear Selections', 'x', 'dp-downloads-bulk-action--clear'" in runtime
    assert "actions.append(pause, resume, reset, separator, remove);" in runtime
    assert "status.append(count, clear);" in runtime
    assert "gap: 10px;" in page
'''
new_fn = '''def test_bulk_toolbar_right_side_owns_count_and_clear_selection() -> None:
    app = read("app.js")
    runtime = read("ui-downloads-runtime.js")
    page = read("ui-downloads-page.css")
    index = read("index.html")
    view = index[index.index('id="view-torrents"'):index.index('<!-- Events -->')]
    assert "_selectedIds.size + ' Selected'" in app
    assert "'Clear Selections', 'x', 'dp-downloads-bulk-action--clear'" in runtime
    assert view.index("bulkAction('pause',this)") < view.index("bulkAction('resume',this)")
    assert view.index("bulkAction('resume',this)") < view.index("bulkAction('reset',this)")
    assert view.index("bulkAction('reset',this)") < view.index("bulkAction('delete',this)")
    assert 'class="dp-downloads-bulk-status"' in view
    assert 'id="bulk-count" class="dp-downloads-bulk-count"' in view
    assert 'onclick="clearSelection()">Clear Selections</button>' in view
    assert "actions.append(" not in runtime
    assert "status.append(" not in runtime
    assert "gap: 10px;" in page
'''
assert old_fn in text
text = text.replace(old_fn, new_fn, 1)
path.write_text(text, encoding='utf-8')

# Downloads integration contract: retire assertions for removed post-render
# fixes and assert direct static loading/ownership instead.
path = tests / 'test_ui_downloads_polish_contract.py'
text = path.read_text(encoding='utf-8')
text = text.replace('    downloads = "/ui-downloads-page.css?v=27"', '    downloads = "/ui-downloads-page.css?v=28"')
old_required = '''    required = (
        "card-download.svg?v=11",
        "All Downloads",
        "download tracked",
        "downloads tracked",
        "Search downloads…",
        "No downloads yet. Add a link, magnet, or torrent file to get started.",
        "No downloads match your current filters.",
        "No downloads match your search.",
        "document.getElementById('dash-tbody')",
        "torrent-page-size",
        "wrapper.remove()",
        "Showing all ",
        "matching downloads",
        "dp-pager-current",
        "chevronLeft",
        "chevronRight",
        "Refresh downloads",
        "dp-downloads-table-wrap",
    )
'''
new_required = '''    required = (
        "card-download.svg?v=11",
        "Download Queue",
        "download tracked",
        "downloads tracked",
        "Search downloads…",
        "No downloads yet. Add a link, magnet, or torrent file to get started.",
        "No downloads match your current filters.",
        "No downloads match your search.",
        "document.getElementById('dash-tbody')",
        "Showing all ",
        "matching downloads",
        "dp-pager-current",
        "chevronLeft",
        "chevronRight",
        "Refresh downloads",
        "dp-downloads-table-wrap",
    )
'''
assert old_required in text
text = text.replace(old_required, new_required, 1)
old_shim = '''def test_downloads_runtime_remains_a_presentation_shim() -> None:
    operator = OPERATOR.read_text(encoding="utf-8")
    assert "/ui-downloads-runtime.js?v=22" in operator
    assert "data-dp-downloads-runtime" in operator
'''
new_shim = '''def test_downloads_runtime_is_loaded_once_by_the_canonical_document() -> None:
    operator = OPERATOR.read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<script src="/ui-downloads-runtime.js?v=24" defer data-dp-downloads-runtime="1"></script>' in index
    assert "data-dp-downloads-runtime" not in operator
    assert "/ui-downloads-runtime.js" not in operator
'''
assert old_shim in text
text = text.replace(old_shim, new_shim, 1)
path.write_text(text, encoding='utf-8')

# Assert no test still encodes the specific superseded expectations that just
# failed the from-zero suite.
for path in tests.glob('test_*.py'):
    text = path.read_text(encoding='utf-8')
    for stale in (
        '/ui-downloads-page.css?v=27',
        '/style-v11.css?v=24',
        'actions.append(pause, resume, reset, separator, remove);',
        'status.append(count, clear);',
        '"torrent-page-size",\n        "wrapper.remove()",',
        'self.assertIn("`All Downloads (${torrentTotal})`", js)',
    ):
        assert stale not in text, f'{path}: stale contract {stale}'

# Final source invariant for this follow-up collapse.
app = app_path.read_text(encoding='utf-8')
assert '`All Downloads (${torrentTotal})`' not in app
