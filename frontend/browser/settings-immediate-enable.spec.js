const { test, expect } = require('@playwright/test');

/* DP 1.0.13 work item A -- Settings commit boundaries against the REAL backend.
 *
 * Every Services Enable toggle is an IMMEDIATE canonical operational control,
 * and each is proven in the ONE spec file that owns its key: Usenet's in
 * usenet-server-cards.spec.js, AllDebrid's in
 * settings-providers-persistence.spec.js, and every Network Sources member's
 * -- HTTP(S) and (S)FTP included, with the refused-write and visible-state
 * cases -- in general-sources-master.spec.js. Spec files share one backend and
 * run concurrently, so a second file flipping or asserting any of those keys
 * races the file that owns it; this file writes none of them. What it proves
 * is the other half of the contract: an ordinary field commits at its own
 * boundary. */

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

test.beforeEach(async ({page}) => {
  await isolateExternalFonts(page);
  await page.goto('/');
  await openSources(page);
});

test('an ordinary settings field commits at its OWN boundary, not on every keystroke',
  async ({page}) => {
    /* Participation is immediate because of what it IS. An ordinary value is
     * not: it crosses its changed-blur boundary, so typing alone writes
     * nothing and leaving the field writes exactly once.
     *
     * Observed on the WIRE. The suite shares one backend and its files run
     * concurrently, so reading this value back out of the document would make
     * the case depend on no other file writing it in the same instant -- which
     * is neither what is being tested nor something this file owns. */
    const writes = [];
    await page.route('**/api/settings', async route => {
      if (route.request().method() === 'PUT') writes.push(route.request().postDataJSON());
      await route.continue();
    });
    try {
      await page.locator('#view-settings [data-tab="downloads"]').click();
      const field = page.locator('#dp-settings-field-min-free-disk-gb');
      await expect(field).toBeVisible();
      const original = Number(await field.inputValue()) || 0;
      const target = original + 3;

      await field.fill(String(target));
      await page.waitForTimeout(600);
      expect(writes, 'typing wrote before the boundary was crossed').toHaveLength(0);

      await field.blur();
      await expect.poll(() => writes.length).toBe(1);
      expect(writes[0].min_free_disk_gb).toBe(target);

      // Leaving it again without changing it crosses no boundary at all.
      await field.click();
      await field.blur();
      await page.waitForTimeout(400);
      expect(writes).toHaveLength(1);

      await field.fill(String(original));
      await field.blur();
      await expect.poll(() => writes.length).toBe(2);
      expect(writes[1].min_free_disk_gb).toBe(original);
    } finally {
      await page.unroute('**/api/settings');
    }
  });

/* Settings Batch 1, Item 2: a provider card's disclosure moves with the
 * operator's ACTION -- its existing motion begins on the click, not when the
 * server answers -- and turns back if the server refuses. Debrid-Link's write
 * is answered by the page itself (from the real public projection), so the
 * shared backend's participation never changes. */
async function answerDebridLink(page, {refuse = false} = {}) {
  const current = await page.evaluate(async () =>
    (await window.debridPulseAuth.fetch('/api/integrations/debridlink/configuration')).json());
  const held = [];
  await page.route(url => url.pathname === '/api/integrations/debridlink/configuration', async route => {
    if (route.request().method() !== 'PATCH') return route.fallback();
    const body = route.request().postDataJSON();
    await new Promise(resolve => held.push({body, resolve}));
    if (refuse) return route.fulfill({status: 500, contentType: 'application/json',
      body: JSON.stringify({detail: 'Refused by the test.'})});
    Object.assign(current, {enabled: body.enabled, configured: false});
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({...current, ok: true})});
  });
  return held;
}

const debridLink = page => page.locator('.dp-settings-provider-card--debridlink');
const bodyHeight = page => debridLink(page).locator('.dp-settings-disclosure').evaluate(button => {
  const body = document.getElementById(button.getAttribute('aria-controls'));
  return body.hidden ? 0 : body.getBoundingClientRect().height;
});
const enableLabel = page => debridLink(page).locator('label:has([data-integration-enabled="debridlink"])');

async function startCollapsedAndDisabled(page) {
  // An injected settings view: Debrid-Link shown disabled and collapsed.
  const input = debridLink(page).locator('[data-integration-enabled="debridlink"]');
  if (await input.isChecked()) {
    const held = await answerDebridLink(page);
    await enableLabel(page).click();
    await expect.poll(() => held.length).toBe(1);
    held[0].resolve();
    await expect(input).not.toBeChecked();
    await page.unrouteAll({behavior: 'ignoreErrors'});
    await isolateExternalFonts(page);
  }
  const chip = debridLink(page).locator('.dp-settings-disclosure');
  if ((await chip.getAttribute('aria-expanded')) === 'true') await chip.click();
  await expect.poll(() => bodyHeight(page)).toBe(0);
}

test('admitting an unconfigured provider opens its card from the click, and withdrawing it closes it from the click',
  async ({page}) => {
    await startCollapsedAndDisabled(page);
    const held = await answerDebridLink(page);
    const chip = debridLink(page).locator('.dp-settings-disclosure');

    await enableLabel(page).click();
    await expect.poll(() => held.length).toBe(1);
    // Unanswered, and the card is already opening.
    await expect(chip).toHaveAttribute('aria-expanded', 'true');
    await expect.poll(() => bodyHeight(page)).toBeGreaterThan(10);
    held[0].resolve();
    await expect(debridLink(page).locator('[data-integration-enabled="debridlink"]')).toBeChecked();
    await expect(chip).toHaveAttribute('aria-expanded', 'true');
    // The accepted answer does not restart the motion: the body only grows.
    const heights = await page.evaluate(() => new Promise(resolve => {
      const button = document.querySelector('.dp-settings-provider-card--debridlink .dp-settings-disclosure');
      const body = document.getElementById(button.getAttribute('aria-controls'));
      const seen = []; const end = performance.now() + 400;
      const tick = () => { seen.push(body.getBoundingClientRect().height); if (performance.now() < end) requestAnimationFrame(tick); else resolve(seen); };
      requestAnimationFrame(tick);
    }));
    heights.slice(1).forEach((height, index) => expect(height).toBeGreaterThanOrEqual(heights[index] - 0.5));

    await enableLabel(page).click();
    await expect.poll(() => held.length).toBe(2);
    await expect(chip).toHaveAttribute('aria-expanded', 'false');   // closing before any answer
    held[1].resolve();
    await expect.poll(() => bodyHeight(page)).toBe(0);
    await expect(debridLink(page).locator('[data-integration-enabled="debridlink"]')).not.toBeChecked();
  });

test('a refused admission turns the opening card back and leaves the provider as the server holds it',
  async ({page}) => {
    await startCollapsedAndDisabled(page);
    const held = await answerDebridLink(page, {refuse: true});
    const chip = debridLink(page).locator('.dp-settings-disclosure');
    await enableLabel(page).click();
    await expect.poll(() => held.length).toBe(1);
    await expect(chip).toHaveAttribute('aria-expanded', 'true');
    held[0].resolve();
    await expect(chip).toHaveAttribute('aria-expanded', 'false');
    await expect.poll(() => bodyHeight(page)).toBe(0);
    await expect(debridLink(page).locator('[data-integration-enabled="debridlink"]')).not.toBeChecked();
    await expect(debridLink(page).locator('[data-integration-enabled="debridlink"]')).toBeEnabled();
    await expect(page.locator('#toasts .toast.error').last()).toBeVisible();
  });
