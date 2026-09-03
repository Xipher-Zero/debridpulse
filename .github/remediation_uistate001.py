from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match, found {count}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")

APP = "frontend/static/app.js"
SPEC = Path("frontend/browser/provider-status-generation.spec.js")

replace_once(
    APP,
    "let pausedTransferCount = 0;\n",
    "let pausedTransferCount = 0;\nlet allDebridStatusGeneration = 0;\n\nfunction invalidateAllDebridStatus() {\n  allDebridStatusGeneration += 1;\n  return allDebridStatusGeneration;\n}\n",
)

replace_once(
    APP,
    "function nav(el) {\n  if (!el) return;\n",
    "function nav(el) {\n  if (!el) return;\n  // Navigation establishes a new presentation generation. An HTTP response\n  // initiated by an older surface may finish later, but may not become truth.\n  invalidateAllDebridStatus();\n",
)

old = """async function loadAllDebridStatus() {
  try {
    const status = await api('GET', '/integration-status/alldebrid');
    renderAllDebridStatus(status);
    return status;
  } catch (_) {
    // Failure of the generic application/API path cannot establish provider
    // failure. Preserve that distinction by rendering a neutral unknown state.
    renderAllDebridStatus({state:'unknown'});
    return null;
  }
}
"""
new = """async function loadAllDebridStatus() {
  // UISTATE-001: request completion order is not temporal authority. Each
  // refresh owns a generation and only the newest still-valid observation may
  // update the provider presentation.
  const generation = invalidateAllDebridStatus();
  try {
    const status = await api('GET', '/integration-status/alldebrid');
    if (generation !== allDebridStatusGeneration) return null;
    renderAllDebridStatus(status);
    return status;
  } catch (_) {
    if (generation !== allDebridStatusGeneration) return null;
    // Failure of the generic application/API path cannot establish provider
    // failure. Preserve that distinction by rendering a neutral unknown state.
    renderAllDebridStatus({state:'unknown'});
    return null;
  }
}
"""
replace_once(APP, old, new)

replace_once(
    APP,
    "async function saveSettings(button) {\n  setButtonPending(button, true, 'Saving…');\n",
    "async function saveSettings(button) {\n  setButtonPending(button, true, 'Saving…');\n  // A pre-save status observation describes the old provider configuration.\n  // Invalidate it before the settings mutation begins; the post-save refresh\n  // will establish the next authoritative generation.\n  invalidateAllDebridStatus();\n",
)

SPEC.write_text(r'''const { test, expect } = require('@playwright/test');

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({
    status: 200, contentType: 'text/css', body: '',
  }));
}

async function controlledStatus(page) {
  const pending = [];
  await page.route('**/api/integration-status/alldebrid', route => {
    pending.push(route);
  });
  const start = () => page.evaluate(() => loadAllDebridStatus());
  const count = async n => expect.poll(() => pending.length).toBe(n);
  const resolve = async (index, body, status = 200) => {
    await pending[index].fulfill({
      status, contentType: 'application/json', body: JSON.stringify(body),
    });
  };
  return { pending, start, count, resolve };
}

async function label(page) {
  return page.locator('#lbl-api').textContent();
}

async function bootstrap(page) {
  await isolateExternalFonts(page);
  await page.goto('/');
  await expect(page.locator('#lbl-api')).toBeVisible();
}

test('UISTATE-001-A older healthy response cannot overwrite newer disabled state', async ({ page }) => {
  await bootstrap(page);
  const c = await controlledStatus(page);
  const r1 = c.start(); await c.count(1);
  const r2 = c.start(); await c.count(2);
  await c.resolve(1, {state:'disabled'}); await r2;
  expect(await label(page)).toBe('AllDebrid: disabled');
  await c.resolve(0, {state:'healthy', username:'old-user'}); await r1;
  expect(await label(page)).toBe('AllDebrid: disabled');
});

test('UISTATE-001-B configuration generation wins over older unconfigured response', async ({ page }) => {
  await bootstrap(page);
  const c = await controlledStatus(page);
  const r1 = c.start(); await c.count(1);
  await page.evaluate(() => invalidateAllDebridStatus());
  const r2 = c.start(); await c.count(2);
  await c.resolve(1, {state:'healthy', username:'configured'}); await r2;
  await c.resolve(0, {state:'unconfigured'}); await r1;
  expect(await label(page)).toBe('AllDebrid: configured');
});

test('UISTATE-001-C rapid overlapping polling permits only newest generation to render', async ({ page }) => {
  await bootstrap(page);
  const c = await controlledStatus(page);
  const r1 = c.start(); await c.count(1);
  const r2 = c.start(); await c.count(2);
  const r3 = c.start(); await c.count(3);
  await c.resolve(2, {state:'healthy', username:'newest'}); await r3;
  await c.resolve(1, {state:'disabled'}); await r2;
  await c.resolve(0, {state:'unhealthy'}); await r1;
  expect(await label(page)).toBe('AllDebrid: newest');
});

test('UISTATE-001-D navigation invalidates observation started by prior surface', async ({ page }) => {
  await bootstrap(page);
  const c = await controlledStatus(page);
  const r1 = c.start(); await c.count(1);
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await c.resolve(0, {state:'healthy', username:'stale-nav'}); await r1;
  expect(await label(page)).not.toBe('AllDebrid: stale-nav');
});

test('UISTATE-001-E settings save invalidates pre-save provider observation', async ({ page }) => {
  await bootstrap(page);
  const c = await controlledStatus(page);
  const r1 = c.start(); await c.count(1);
  await page.route('**/api/settings', async route => {
    if (route.request().method() === 'POST' || route.request().method() === 'PUT') {
      return route.fulfill({status:200, contentType:'application/json', body:'{}'});
    }
    return route.fallback();
  });
  await page.evaluate(() => invalidateAllDebridStatus());
  const r2 = c.start(); await c.count(2);
  await c.resolve(1, {state:'disabled'}); await r2;
  await c.resolve(0, {state:'healthy', username:'pre-save'}); await r1;
  expect(await label(page)).toBe('AllDebrid: disabled');
});

test('UISTATE-001-F obsolete request error cannot replace newer provider truth', async ({ page }) => {
  await bootstrap(page);
  const c = await controlledStatus(page);
  const r1 = c.start(); await c.count(1);
  const r2 = c.start(); await c.count(2);
  await c.resolve(1, {state:'healthy', username:'authoritative'}); await r2;
  await c.resolve(0, {detail:'old failure'}, 503); await r1;
  expect(await label(page)).toBe('AllDebrid: authoritative');
});
''', encoding="utf-8")
