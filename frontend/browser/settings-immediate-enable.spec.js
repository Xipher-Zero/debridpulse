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
