from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit("usage: prepare_lucide_helper.py <apply-script>")

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")


def replace_once(old: str, new: str, description: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{description}: expected one occurrence, found {count}")
    text = text.replace(old, new)


replace_once(
    'ROOT = Path(__file__).resolve().parents[2]\nSTATIC = ROOT / "frontend" / "static"\nTESTS = ROOT / "backend" / "tests"\nPIN = "23f9abc4ed0146cffededd3d7f94c1018bfdf693"\n',
    'ROOT = Path.cwd()\nSTATIC = ROOT / "frontend" / "static"\nTESTS = ROOT / "backend" / "tests"\nPIN = "23f9abc4ed0146cffededd3d7f94c1018bfdf693"\n',
    "top-level repo root",
)
replace_once(
    "      .replace(/^[\\\\s\\\\u2000-\\\\u2BFF\\\\u{1F000}-\\\\u{1FAFF}]+/u, '')\n",
    "      .replace(/^[^A-Za-z0-9]+/, '')\n",
    "Unicode prefix scrubber",
)
replace_once(
    '    "chevron-down", "chevron-left", "chevron-right", "circle-check", "circle-help",\n',
    '    "chevron-down", "chevron-left", "chevron-right", "circle-check", "circle-question-mark",\n',
    "pinned Lucide help icon name",
)
replace_once(
    "    help: 'circle-help',\n",
    "    help: 'circle-question-mark',\n",
    "help alias",
)
replace_once(
    "# New contract tests focus on the architectural invariant rather than snapshots.\ntest = '''",
    """# Keep startup-order regression guards focused on behavior rather than cache-bust versions.
startup_test = TESTS / "test_dashboard_startup_surface.py"
replace_exact(
    startup_test,
    "    assert index.index('<script src=\\\"/app.js?v=15\\\" defer></script>') < index.index(\\n        '<script src=\\\"/operator-title.js?v=23\\\" defer></script>'\\n    )\\n",
    "    assert index.index('src=\\\"/app.js?') < index.index(\\n        'src=\\\"/operator-title.js?'\\n    )\\n",
)
operator_test = TESTS / "test_operator_title_state.py"
replace_exact(
    operator_test,
    '''    core = '<script src="/app.js?v=15" defer></script>'
    operator = '<script src="/operator-title.js?v=23" defer></script>'
''',
    '''    core = 'src="/app.js?'
    operator = 'src="/operator-title.js?'
''',
)

# Existing unified-transfer tests must assert the new Lucide vocabulary, not retired emoji.
direct_links_test = TESTS / "test_direct_links.py"
replace_exact(
    direct_links_test,
    '        unified_heading = "⬇️ Add Links, Magnets, or Torrent File"\\n',
    '        unified_heading = "Add Links, Magnets, or Torrent File"\\n',
)
replace_exact(
    direct_links_test,
    '        self.assertIn("🔗 Direct link", js)\\n',
    '        self.assertIn("window.dpLucideSvg", js)\\n        self.assertIn("Direct link", js)\\n',
)
replace_exact(
    direct_links_test,
    '''        self.assertIn("missing:'❌ Missing file'", js)
        self.assertIn("downloading_with_errors:'⬇ Downloading'", js)
        self.assertIn("completed_with_errors:'⚠ Completed with errors'", js)
''',
    '''        self.assertIn("window.dpLucideStatusDefinition", js)
        self.assertIn("window.dpLucideStatusIcon", js)
        self.assertNotIn("missing:'❌ Missing file'", js)
        self.assertNotIn("downloading_with_errors:'⬇ Downloading'", js)
        self.assertNotIn("completed_with_errors:'⚠ Completed with errors'", js)
''',
)
replace_exact(
    direct_links_test,
    '''        for glyph in ("&#x25EB;&#xFE0E;", "&#x25BD;&#xFE0E;", "&#x2263;&#xFE0E;",
                      "&#x2206;&#xFE0E;", "&#x2699;&#xFE0E;", "&#x003F;&#xFE0E;"):
            self.assertIn(glyph, html)
''',
    '''        for retired_glyph in ("&#x25EB;&#xFE0E;", "&#x25BD;&#xFE0E;", "&#x2263;&#xFE0E;",
                              "&#x2206;&#xFE0E;", "&#x2699;&#xFE0E;", "&#x003F;&#xFE0E;"):
            self.assertNotIn(retired_glyph, html)
        self.assertIn("/ui-lucide-runtime.js", html)
''',
)
replace_exact(
    direct_links_test,
    '        self.assertIn("font-variant-emoji:text", css)\\n',
    '        self.assertNotIn("font-variant-emoji:text", css)\\n',
)

# Extraction lifecycle remains behaviorally identical; only its operator glyph source changes.
extraction_test = TESTS / "test_extraction_lifecycle.py"
replace_exact(
    extraction_test,
    "extracting:'📦 Extracting'",
    "window.dpLucideStatusDefinition",
)

# Activity Log keeps its exact cascade-generation contract and now includes the final Lucide layer.
activity_test = TESTS / "test_ui_activity_log_page_contract.py"
replace_exact(
    activity_test,
    '''    transfer = overlay.index("/ui-transfer-contract.css?v=31")

    assert shell < dashboard < controls < stats < activity < downloads < transfer
''',
    '''    transfer = overlay.index("/ui-transfer-contract.css?v=32")
    lucide = overlay.index("/ui-lucide-iconography.css?v=1")

    assert shell < dashboard < controls < stats < activity < downloads < transfer < lucide
''',
)

# New contract tests focus on the architectural invariant rather than snapshots.
test = '''""",
    "existing UI regression contracts",
)

path.write_text(text, encoding="utf-8")
