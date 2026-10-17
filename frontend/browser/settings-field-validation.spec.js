const { test, expect } = require('@playwright/test');

/* Settings Batch 1, Item 3 -- a Settings number is written EXACTLY or not at all.
 *
 * The canonical persistence owner (ui-settings-persistence.js) reads a number
 * control's draft exactly before any request: "18EGGS", exponent forms ("1e3"),
 * a fraction in a whole-number field, an empty field with no meaning of its
 * own, and a value outside the control's declared range (the server's own
 * bounds, pinned by backend/tests/test_settings_validation_parity.py) are
 * refused in the field's own words, nothing is sent, and the control returns
 * to its accepted value. It used to send 0 for the first and empty cases --
 * silently switching recovery features off -- and 1000 for "1e3".
 *
 * Every case here either sends NOTHING or answers its own write in the page,
 * so this file mutates no shared backend key.
 */

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

async function openTab(page, tab) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator(`#view-settings [data-tab="${tab}"]`).click();
  await expect(page.locator(`.dp-settings-panel[data-panel="${tab}"]`)).toBeVisible();
}

/* Every request this page makes to a Settings write surface. */
function watchWrites(page) {
  const writes = [];
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (request.method() !== 'GET' && /^\/api\/(settings|transfer-policy|integrations\/|auth\/config)/.test(path)) {
      writes.push({method: request.method(), path, body: request.postData()});
    }
  });
  return writes;
}

async function typeAndLeave(page, field, text) {
  await field.click();
  await field.press('Control+a');
  await field.press('Backspace');
  if (text) await page.keyboard.type(text);
  await field.evaluate(node => node.blur());
}

const lastError = page => page.locator('#toasts .toast.error').last();

test.beforeEach(async ({page}) => {
  await isolateExternalFonts(page);
  await page.goto('/');
});

test('a whole-number Settings field refuses malformed drafts without a request and keeps its value',
  async ({page}) => {
    await openTab(page, 'downloads');
    const field = page.locator('[data-setting="stuck_download_timeout_hours"]');
    const accepted = await field.inputValue();
    const writes = watchWrites(page);
    for (const [typed, words] of [
      ['18EGGS', 'must be a whole number'], ['1e3', 'must be a whole number'], ['1e1', 'must be a whole number'],
      ['2.5', 'must be a whole number'], ['', 'is required'], ['-5', 'must be 0 or more'],
      ['169', 'must be 168 or less'],
    ]) {
      await typeAndLeave(page, field, typed);
      await expect(field, typed).toHaveValue(accepted);
      await expect(lastError(page)).toHaveText(`Stalled Download Timeout (hours) ${words}.`);
    }
    expect(writes).toEqual([]);
  });

test('a fractional field accepts an ordinary decimal and refuses exponent forms', async ({page}) => {
  await openTab(page, 'downloads');
  const field = page.locator('[data-setting="min_free_disk_gb"]');
  const accepted = await field.inputValue();
  const writes = watchWrites(page);
  await typeAndLeave(page, field, '1e1');
  await expect(field).toHaveValue(accepted);
  await expect(lastError(page)).toHaveText('Minimum Free Disk Space (GB) must be a number.');
  expect(writes).toEqual([]);

  // An ordinary decimal is sent exactly, as a number (answered here).
  await page.route(url => url.pathname === '/api/settings', route =>
    route.request().method() === 'PUT' ? route.abort() : route.fallback());
  await typeAndLeave(page, field, '12.5');
  await expect.poll(() => writes.filter(write => write.method === 'PUT').length).toBe(1);
  expect(JSON.parse(writes.find(write => write.method === 'PUT').body).min_free_disk_gb).toBe(12.5);
});

test('a field whose emptiness means one canonical value commits that value', async ({page}) => {
  await openTab(page, 'downloads');
  const chip = page.locator('[data-executor-tuning="webdav"] .dp-settings-disclosure');
  if ((await chip.getAttribute('aria-expanded')) !== 'true') await chip.click();
  const field = page.locator('[data-setting="webdav_collection_scan_timeout_seconds"]');
  await expect(field).toHaveAttribute('data-commit-blank', '');
  const writes = watchWrites(page);
  await page.route(url => url.pathname === '/api/integrations/general_webdav/configuration', route =>
    route.request().method() === 'PATCH' ? route.abort() : route.fallback());
  // Make it differ from the accepted value, then empty it: "no limit" is 0.
  const accepted = await field.inputValue();
  if (accepted === '') {
    await typeAndLeave(page, field, '1e2');
    await expect(lastError(page)).toHaveText('Collection Scan Timeout (seconds) must be a whole number.');
    expect(writes).toEqual([]);
    return;
  }
  await typeAndLeave(page, field, '');
  await expect.poll(() => writes.length).toBe(1);
  expect(JSON.parse(writes[0].body)).toEqual({options: {collection_scan_timeout_seconds: 0}});
});

test('Browser Session Lifetime is read exactly, never truncated', async ({page}) => {
  await openTab(page, 'authentication');
  const field = page.locator('[data-setting="auth_session_lifetime_hours"]');
  const accepted = await field.inputValue();
  const writes = watchWrites(page);
  for (const typed of ['1e2', '12.7']) {
    await typeAndLeave(page, field, typed);
    await expect(field).toHaveValue(accepted);
    await expect(lastError(page)).toContainText('must be a whole number.');
  }
  await typeAndLeave(page, field, '169');
  await expect(lastError(page)).toContainText('must be 168 or less.');
  expect(writes).toEqual([]);
});

test('a selector whose value is a whole number commits that number', async ({page}) => {
  await openTab(page, 'maintenance');
  const writes = watchWrites(page);
  await page.route(url => url.pathname === '/api/settings', route =>
    route.request().method() === 'PUT' ? route.abort() : route.fallback());
  const target = await page.locator('select[data-setting="activity_log_page_size"]').evaluate(select => {
    const next = [...select.options].map(option => option.value).find(value => value !== select.value);
    select.value = next;
    select.dispatchEvent(new Event('change', {bubbles: true}));
    return next;
  });
  await expect.poll(() => writes.filter(write => write.method === 'PUT').length).toBe(1);
  expect(JSON.parse(writes.find(write => write.method === 'PUT').body).activity_log_page_size).toBe(Number(target));
});
