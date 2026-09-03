from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend/static"
BROWSER = ROOT / "frontend/browser"
PAGE_CSS = STATIC / "ui-settings-page.css"
STYLE_V11 = STATIC / "style-v11.css"
LEGACY_CSS = STATIC / "ui-settings-download-engine-spacing.css"
ARCH_TEST = ROOT / "backend/tests/test_settings_download_engine_spacing.py"
BROWSER_TEST = BROWSER / "settings-download-engine-spacing.spec.js"

IMPORT = "@import url('/ui-settings-download-engine-spacing.css?v=20');\n"
SELECTOR = "body.dp-v11-structural #view-settings .dp-settings-download-engine-row {"

page = PAGE_CSS.read_text()
if SELECTOR not in page:
    raise RuntimeError("canonical Download Engine row selector is missing")
before, tail = page.split(SELECTOR, 1)
rule, after = tail.split("}", 1)
if "margin-top: 18px;" not in rule:
    raise RuntimeError("canonical Download Engine top margin changed")
rule = rule.replace("margin-top: 18px;", "margin-top: 0;", 1)
PAGE_CSS.write_text(before + SELECTOR + rule + "}" + after)

styles = STYLE_V11.read_text()
if IMPORT not in styles:
    raise RuntimeError("historical Download Engine spacing import changed")
STYLE_V11.write_text(styles.replace(IMPORT, "", 1))

if not LEGACY_CSS.exists():
    raise RuntimeError("historical Download Engine spacing stylesheet is already absent")
LEGACY_CSS.unlink()

ARCH_TEST.write_text('''from pathlib import Path\n\n\nROOT = Path(__file__).resolve().parents[2]\nSTATIC = ROOT / "frontend" / "static"\nSTYLE_V11 = STATIC / "style-v11.css"\nSETTINGS_CSS = STATIC / "ui-settings-page.css"\nSPACING_CSS = STATIC / "ui-settings-download-engine-spacing.css"\n\n\ndef source(path: Path) -> str:\n    return path.read_text(encoding="utf-8")\n\n\ndef test_download_engine_spacing_is_owned_by_canonical_settings_stylesheet():\n    settings = source(SETTINGS_CSS)\n    imports = source(STYLE_V11)\n\n    selector = (\n        "body.dp-v11-structural #view-settings "\n        ".dp-settings-download-engine-row {"\n    )\n    assert settings.count(selector) == 1\n    rule = settings.split(selector, 1)[1].split("}", 1)[0]\n    assert "margin-top: 0;" in rule\n    assert "margin-top: 18px;" not in rule\n\n    assert "ui-settings-download-engine-spacing.css" not in imports\n    assert not SPACING_CSS.exists()\n''')

BROWSER_TEST.write_text('''const { test, expect } = require('@playwright/test');\n\nasync function openDownloadsSettings(page) {\n  await page.goto('/');\n  await page.locator('#sidebar .nav-item[data-view="settings"]').click();\n  await expect(page.locator('#view-settings')).toHaveClass(/\\bactive\\b/);\n  const downloads = page.locator('.dp-settings-tabs .stab[data-tab="downloads"]');\n  await downloads.click();\n  await expect(downloads).toHaveAttribute('aria-selected', 'true');\n  await expect(page.locator('.dp-settings-download-engine-row')).toBeVisible();\n}\n\nasync function expectCanonicalSpacing(page) {\n  const row = page.locator('.dp-settings-download-engine-row');\n  await expect.poll(() => row.evaluate(node => getComputedStyle(node).marginTop)).toBe('0px');\n}\n\ntest('Download Engine spacing is canonical in dark, light, and narrower responsive Settings', async ({ page }) => {\n  const correctionRequests = [];\n  page.on('request', request => {\n    if (request.url().includes('ui-settings-download-engine-spacing.css')) correctionRequests.push(request.url());\n  });\n\n  await openDownloadsSettings(page);\n  await expectCanonicalSpacing(page);\n\n  await page.locator('#theme-toggle').click();\n  await expect.poll(() => page.evaluate(() => document.body.classList.contains('light'))).toBeTruthy();\n  await expectCanonicalSpacing(page);\n\n  await page.setViewportSize({ width: 900, height: 900 });\n  await expect(page.locator('.dp-settings-download-engine-row')).toBeVisible();\n  await expectCanonicalSpacing(page);\n\n  expect(correctionRequests).toEqual([]);\n});\n''')
