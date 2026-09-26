const { test, expect } = require('@playwright/test');

/* DP 1.0.13 work item A -- every Services Enable toggle is an
 * IMMEDIATE canonical operational control against the REAL backend. The
 * visible toggle can never report ON while canonical state is OFF. */

const TOGGLES = ['usenet', 'alldebrid', 'general_http', 'general_ftp'];

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

async function openSources(page) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator('#view-settings [data-tab="sources"]').click();
  await expect(page.locator('.dp-settings-panel[data-panel="sources"]')).toBeVisible();
}

const persisted = async (page, id) =>
  (await page.request.get('/api/settings').then(r => r.json())).integrations[id]?.enabled;

/* Services cards arrive COLLAPSED: expansion is local presentation
 * state, never a projection of enabled/configured/verified state. A General
 * Sources member toggle lives inside that group's body, so operating one means
 * opening the card first -- exactly what the operator does, through the one
 * canonical disclosure. The master's own toggle is in the header and is always
 * reachable. */
async function reveal(page, id) {
  const label = page.locator(`label[for="dp-settings-integration-${id}-enabled"]`);
  if (await label.isVisible()) return;
  const disclosure = page.locator('.dp-settings-general-sources .dp-settings-disclosure');
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
  await expect(label).toBeVisible();
}

/** Click the toggle the way an operator does: on its label. */
async function flip(page, id) {
  await reveal(page, id);
  await page.locator(`label[for="dp-settings-integration-${id}-enabled"]`).click();
}

test.beforeEach(async ({page}) => {
  await isolateExternalFonts(page);
  await page.goto('/');
  await openSources(page);
});

for (const id of TOGGLES) {
  test(`${id} persists canonical enabled state immediately, without any page-level save`, async ({page}) => {
    const toggle = page.locator(`[data-integration-enabled="${id}"]`);
    const before = await persisted(page, id);
    await flip(page, id);
    await expect.poll(() => persisted(page, id)).toBe(!before);
    await expect(toggle).toBeChecked({checked: !before});

    // And back again, still with no page-level save.
    await flip(page, id);
    await expect.poll(() => persisted(page, id)).toBe(!!before);
    await expect(toggle).toBeChecked({checked: !!before});
  });
}

test('a committed enable survives a full reload without any page-level save', async ({page}) => {
  const before = await persisted(page, 'usenet');
  if (before) { await flip(page, 'usenet'); await expect.poll(() => persisted(page, 'usenet')).toBe(false); }
  await flip(page, 'usenet');
  await expect.poll(() => persisted(page, 'usenet')).toBe(true);
  await page.reload();
  await openSources(page);
  await expect(page.locator('[data-integration-enabled="usenet"]')).toBeChecked();
  // Restore.
  await flip(page, 'usenet');
  await expect.poll(() => persisted(page, 'usenet')).toBe(false);
});

test('a failed enable mutation restores the committed state and reports it', async ({page}) => {
  const before = await persisted(page, 'general_http');
  await page.route(url => /\/api\/integrations\/general_http\/configuration$/.test(url.pathname),
    route => route.fulfill({status: 502, contentType: 'application/json',
      body: JSON.stringify({detail: 'integration configuration rejected'})}));
  await flip(page, 'general_http');
  // The visible control returns to committed truth, and the error is surfaced.
  await expect(page.locator('[data-integration-enabled="general_http"]'))
    .toBeChecked({checked: !!before});
  await expect(page.locator('#toasts .toast')).toContainText(/reject|error|fail/i);
  expect(await persisted(page, 'general_http')).toBe(before);
});

test('the visible toggle never reports ON while canonical state is OFF', async ({page}) => {
  for (const id of TOGGLES) {
    const visible = await page.locator(`[data-integration-enabled="${id}"]`).isChecked();
    expect(visible).toBe((await persisted(page, id)) !== false);
  }
});

test('an ordinary settings field commits at its OWN boundary, not on every keystroke',
  async ({page}) => {
    /* Participation is immediate because of what it IS. An ordinary value is
     * not: it crosses its changed-blur boundary, so typing alone writes
     * nothing and leaving the field writes exactly once. */
    await page.locator('#view-settings [data-tab="downloads"]').click();
    const field = page.locator('#dp-settings-field-min-free-disk-gb');
    await expect(field).toBeVisible();
    const canonical = async () =>
      (await page.request.get('/api/settings').then(r => r.json())).min_free_disk_gb;
    const original = await canonical();
    const target = Number(original || 0) + 3;
    await field.fill(String(target));
    await page.waitForTimeout(600);
    expect(await canonical(), 'typing wrote before the boundary was crossed').toBe(original);

    await field.blur();
    await expect.poll(canonical).toBe(target);

    await field.fill(String(original ?? 0));
    await field.blur();
    await expect.poll(canonical).toBe(original);
  });
