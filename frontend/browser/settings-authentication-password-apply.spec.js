const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Authentication: the Password field and its Apply below 780px.
 *
 * The password field (its Clear inside it) and Apply are one control group:
 * below 780px the setting is its Password title over that group, the field
 * taking the row Apply leaves and Apply its own size beside it, at every width
 * from 320px. From 780px nothing changes.
 *
 * The auth document is SERVED with synthetic values; every write is recorded
 * and refused, so no credential changes. */

const AUTH = {mode: 'Username & Password', authentication_required: true, password_enabled: true, password_ready: true,
  password_configured: true, username: 'synthetic-admin'};

const settle = page => page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));

async function open(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  page.__writes = [];
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'GET' && path === '/api/auth/config') {
      return route.fulfill({json: {...await (await route.fetch()).json(), ...AUTH}});
    }
    if (request.method() === 'GET' || path === '/api/settings/test-aria2') return route.continue();
    page.__writes.push({method: request.method(), path, body: request.postDataJSON?.() ?? null});
    return route.abort();
  });
  await page.setViewportSize({width: 1280, height: 900});
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await page.locator('#view-settings [data-tab="authentication"]:not([inert])').click();
  await settle(page);
}

async function resize(page, width) {
  await page.setViewportSize({width, height: 900});
  await settle(page);
}

// The group as drawn.
const group = page => page.evaluate(() => {
  const R = node => node.getBoundingClientRect();
  const field = document.querySelector('.dp-settings-auth-password-field');
  const label = R(field.querySelector('.form-label'));
  const composite = R(field.querySelector('.dp-action-field'));
  const input = R(field.querySelector('[data-setting="auth_password"]'));
  const clear = R(field.querySelector('[data-action="clear-password"]'));
  const apply = R(field.querySelector('[data-action="apply-credentials"]'));
  const card = R(field.closest('.card'));
  const mid = box => (box.top + box.bottom) / 2;
  return {
    sameRow: apply.top < composite.bottom && apply.bottom > composite.top,
    centres: Math.abs(mid(apply) - mid(composite)),
    applyRight: apply.left >= composite.right,
    gap: apply.left - composite.right,
    clearInside: clear.left >= composite.left && clear.right <= composite.right && clear.top >= composite.top && clear.bottom <= composite.bottom,
    inputBeforeClear: input.right <= clear.left + 0.5,
    input: input.width,
    apply: [Math.round(apply.width * 10) / 10, apply.height],
    titleAbove: label.bottom <= Math.min(composite.top, apply.top),
    titleGap: Math.min(composite.top, apply.top) - label.bottom,
    inCard: composite.left >= card.left && apply.right <= card.right,
    overflow: document.documentElement.scrollWidth > window.innerWidth,
  };
});

test('below 780px the field and Apply share one row at every width, the field yielding to Apply', async ({page}) => {
  await open(page);
  const desktop = await group(page);
  for (const width of [320, 326, 360, 390, 560, 779, 360, 320]) {
    await resize(page, width);
    const g = await group(page);
    const at = `@${width}`;
    expect([g.sameRow, g.applyRight, g.clearInside, g.inputBeforeClear, g.titleAbove, g.inCard, g.overflow], at)
      .toEqual([true, true, true, true, true, true, false]);
    expect(g.centres, at).toBeLessThanOrEqual(1);
    expect(g.gap, at).toBeGreaterThanOrEqual(7.5);
    expect(g.gap, at).toBeLessThanOrEqual(8.5);
    expect(g.titleGap, at).toBeLessThanOrEqual(8.5);
    expect(g.input, `${at}: room to type`).toBeGreaterThanOrEqual(100);
    // Apply keeps its own size: the one it has from 780px.
    expect(g.apply, at).toEqual(desktop.apply);
  }
  // A long value does not widen the row.
  await resize(page, 320);
  const before = await group(page);
  await page.locator('[data-setting="auth_password"]').fill('x'.repeat(240));
  const after = await group(page);
  expect([after.sameRow, after.input, after.overflow]).toEqual([true, before.input, false]);
  expect(page.__writes).toEqual([]);
});

test('every state of the group keeps the row, and each control keeps its own action', async ({page}) => {
  await open(page);
  await resize(page, 320);
  const password = page.locator('[data-setting="auth_password"]');
  const clear = page.locator('[data-action="clear-password"]');
  const apply = page.locator('[data-action="apply-credentials"]');
  // Stored, empty: the placeholder says so, Clear is available, Apply is not.
  await expect(password).toHaveValue('');
  await expect(password).toHaveAttribute('placeholder', /.+/);
  await expect(clear).toBeEnabled();
  await expect(apply).toBeDisabled();
  expect((await group(page)).sameRow).toBe(true);
  // Focused and typed: Apply becomes available; still one row.
  await password.focus();
  await page.keyboard.type('synthetic-new-password');
  await expect(password).toBeFocused();
  await expect(apply).toBeEnabled();
  expect((await group(page)).sameRow).toBe(true);
  // Keyboard order: the field, its Clear, then Apply.
  await page.keyboard.press('Tab');
  await expect(clear).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(apply).toBeFocused();
  // Apply routes through the one Apply handler: one write, carrying the password.
  await page.keyboard.press('Enter');
  await expect.poll(() => page.__writes.length).toBe(1);
  expect(page.__writes[0].body).toMatchObject({auth_password: 'synthetic-new-password'});
  // Refused: the draft stays for correction and Apply is available again.
  await expect(apply).toBeEnabled();
  await expect(password).toHaveValue('synthetic-new-password');
  // Clear is its own confirmed action; cancelled, it writes nothing.
  await clear.click();
  const dialog = page.getByRole('alertdialog');
  await expect(dialog).toContainText('Clear stored password?');
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  expect(page.__writes).toHaveLength(1);
  // Emptied again: Apply is disabled again, the row unchanged.
  await password.fill('');
  await expect(apply).toBeDisabled();
  expect((await group(page)).sameRow).toBe(true);
});
