const { test, expect } = require('@playwright/test');

/* DP 1.0.13 -- Details' Original Resource: the backend's complete safe URL on
 * hover or keyboard focus, and Copy URL in the application's one menu only when
 * the backend allows it, copying exactly that value. */

const LONG_PATH = Array(6).fill('a-deliberately-long-release-directory').join('/') + '/Release.Name.2026.part01.rar';
const FULL = `https://files.example/${LONG_PATH}`;
const COMPACT = FULL.slice(0, 117) + '…' + FULL.slice(-60);

const detail = (id, extra) => ({
  id, name: 'Release', status: 'downloading', progress: 40, size_bytes: 1024, source: 'direct_link',
  created_at: '2026-01-01T00:00:00Z', completed_at: null, hash: '', files: [], source_outcomes: [],
  events: [], executors: ['aria2'], route_attempts: [], execution_attempts: [],
  original_resource: COMPACT, original_resource_sanitized_full: FULL, original_resource_copy_allowed: true,
  ...extra,
});

async function boot(page, details, {clipboard = 'resolve'} = {}) {
  await page.addInitScript(mode => {
    window.__copied = [];
    window.__copyGate = null;
    const writeText = text => {
      if (mode === 'reject') return Promise.reject(new DOMException('Write permission denied.', 'NotAllowedError'));
      if (mode === 'defer') return new Promise(resolve => { window.__copyGate = () => { window.__copied.push(text); resolve(); }; });
      window.__copied.push(text);
      return Promise.resolve();
    };
    Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {writeText}});
  }, clipboard);
  const byId = new Map(details.map(item => [String(item.id), item]));
  await page.route(url => /^\/api\/torrents\/\d+$/.test(url.pathname), route => {
    const item = byId.get(new URL(route.request().url()).pathname.split('/').pop());
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(item)});
  });
  await page.route(url => url.pathname === '/api/torrents',
    route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items: details, total: details.length})}));
  await page.goto('/');
  return {set: item => byId.set(String(item.id), item)};
}
const open = async (page, id) => {
  await page.evaluate(value => showDetail(value), id);
  await expect(page.locator('#modal-body .detail-grid')).toBeVisible();
};
const field = page => page.locator('.dp-detail-original-resource .dv');
const value = page => page.locator('.dp-detail-resource');
const tip = page => page.locator('.dp-detail-resource-full');
const menu = page => page.locator('.dp-row-menu');
const box = locator => locator.evaluate(node => { const r = node.getBoundingClientRect(); return {x: r.left, y: r.top, width: r.width, height: r.height, right: r.right, bottom: r.bottom}; });

test('the compact row is unchanged and hover shows the complete safe URL, text only, inside the row', async ({page}) => {
  await boot(page, [detail(1), detail(2, {original_resource_sanitized_full: undefined, original_resource_copy_allowed: false})]);
  await open(page, 2);
  const plain = await box(page.locator('.dp-detail-original-resource'));
  await expect(field(page)).toHaveText(COMPACT);
  await expect(value(page)).toHaveCount(0);
  await expect(tip(page)).toHaveCount(0);

  await open(page, 1);
  await expect(value(page)).toHaveText(COMPACT);
  expect(await box(page.locator('.dp-detail-original-resource'))).toEqual(plain);
  await expect(tip(page)).toBeHidden();
  await value(page).hover();
  await expect(tip(page)).toBeVisible();
  await expect(tip(page)).toHaveText(FULL);
  expect(FULL.length).toBeGreaterThan(180);
  await expect(tip(page)).toHaveAttribute('role', 'tooltip');
  await expect(value(page)).toHaveAttribute('aria-describedby', 'dp-detail-resource-full');
  // Hovering opens no URL, and the row never moved.
  expect(page.url()).toMatch(/\/$/);
  expect(await box(page.locator('.dp-detail-original-resource'))).toEqual(plain);
  const row = await box(page.locator('.dp-detail-original-resource'));
  const shown = await box(tip(page));
  expect(shown.x).toBeGreaterThanOrEqual(row.x - 0.5);
  expect(shown.right).toBeLessThanOrEqual(row.right + 0.5);
  // Leaving hides it.
  await page.mouse.move(2, 2);
  await expect(tip(page)).toBeHidden();
});

test('keyboard focus shows it, Escape dismisses it without leaving or closing Details', async ({page}) => {
  await boot(page, [detail(3, {original_resource_copy_allowed: false})]);
  await open(page, 3);
  await page.keyboard.press('Shift');
  await value(page).focus();
  await expect(tip(page)).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(tip(page)).toBeHidden();
  await expect(value(page)).toBeFocused();
  await expect(page.locator('#overlay.open')).toHaveCount(1);
  await page.keyboard.press('Tab');
  await page.keyboard.press('Shift+Tab');
  await expect(value(page)).toBeFocused();
  await expect(tip(page)).toBeVisible();
});

for (const theme of ['dark', 'light']) {
  test(`a very long URL stays bounded and scrollable at a narrow viewport (${theme})`, async ({page}) => {
    const huge = 'https://files.example/' + Array(160).fill('segment-without-spaces').join('/') + '/file.bin';
    await page.setViewportSize({width: 390, height: 760});
    await boot(page, [detail(4, {original_resource: huge.slice(0, 117) + '…' + huge.slice(-60), original_resource_sanitized_full: huge})]);
    await page.evaluate(mode => { document.body.classList.toggle('light', mode === 'light'); }, theme);
    await open(page, 4);
    await value(page).hover();
    await expect(tip(page)).toBeVisible();
    const shown = await box(tip(page));
    expect(shown.x).toBeGreaterThanOrEqual(0);
    expect(shown.right).toBeLessThanOrEqual(390);
    expect(shown.bottom).toBeLessThanOrEqual(760);
    const scroll = await tip(page).evaluate(node => ({scroll: node.scrollHeight, client: node.clientHeight, text: node.textContent}));
    expect(scroll.text).toBe(huge);
    expect(scroll.scroll).toBeGreaterThan(scroll.client);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    // The pointer can reach the tooltip and scroll it.
    await tip(page).hover();
    await page.mouse.wheel(0, 200);
    await expect.poll(() => tip(page).evaluate(node => node.scrollTop)).toBeGreaterThan(0);
    await expect(tip(page)).toBeVisible();
    await page.screenshot({path: test.info().outputPath(`long-hover-${theme}.png`)});
  });
}

test('an allowed URL offers Copy URL in the DP menu and copies exactly the approved value, after success', async ({page}) => {
  await boot(page, [detail(5)], {clipboard: 'defer'});
  await open(page, 5);
  await value(page).click({button: 'right'});
  await expect(menu(page)).toHaveCount(1);
  await expect(menu(page)).toHaveAttribute('role', 'menu');
  await expect(menu(page).locator('[role="menuitem"]')).toHaveText(['Copy URL']);
  await expect(menu(page).locator('[role="menuitem"]')).toBeFocused();
  // Inside the dialog, above it, and the tooltip steps aside for the menu.
  expect(await menu(page).evaluate(node => node.parentElement.id)).toBe('overlay');
  await expect(tip(page)).toBeHidden();
  const at = await box(menu(page));
  expect(await page.evaluate(([x, y]) => !!document.elementFromPoint(x, y)?.closest('.dp-row-menu'), [at.x + 8, at.y + 8])).toBe(true);
  for (const theme of ['dark', 'light']) {
    await page.evaluate(mode => document.body.classList.toggle('light', mode === 'light'), theme);
    await page.screenshot({path: test.info().outputPath(`copy-menu-${theme}.png`)});
  }
  await page.locator('.dp-row-menu [role="menuitem"]').click();
  await expect(menu(page)).toHaveCount(0);
  // Pending: nothing claimed yet.
  await page.waitForTimeout(150);
  await expect(page.locator('.toast', {hasText: 'URL copied'})).toHaveCount(0);
  await page.evaluate(() => window.__copyGate());
  await expect(page.locator('.toast', {hasText: 'URL copied'})).toHaveCount(1);
  expect(await page.evaluate(() => window.__copied)).toEqual([FULL]);
  await expect(value(page)).toBeFocused();
  expect(page.url()).toMatch(/\/$/);
});

test('a rejected clipboard write reports failure and never success', async ({page}) => {
  await boot(page, [detail(6)], {clipboard: 'reject'});
  await open(page, 6);
  await value(page).click({button: 'right'});
  await page.locator('.dp-row-menu [role="menuitem"]').click();
  await expect(page.locator('.toast', {hasText: 'Could not copy the URL'})).toHaveCount(1);
  await expect(page.locator('.toast', {hasText: 'URL copied'})).toHaveCount(0);
});

test('a sanitized or unproven resource offers no Copy URL and keeps its safe text only', async ({page}) => {
  const redacted = 'https://files.example/a.bin?…';
  await boot(page, [
    detail(7, {original_resource: redacted, original_resource_sanitized_full: redacted, original_resource_copy_allowed: false}),
    detail(8, {original_resource_copy_allowed: 'true'}),
    detail(9, {original_resource_sanitized_full: undefined}),
  ]);
  for (const id of [7, 8, 9]) {
    await open(page, id);
    const target = id === 9 ? field(page) : value(page);
    await target.click({button: 'right'});
    await expect(menu(page)).toHaveCount(0);
  }
  await open(page, 7);
  await value(page).hover();
  await expect(tip(page)).toHaveText(redacted);
  expect(await page.evaluate(() => window.__copied)).toEqual([]);
  await page.screenshot({path: test.info().outputPath('ineligible-hover-dark.png')});
});

test('selection change, refresh, closing and navigation retire the menu and what it would copy', async ({page}) => {
  const other = 'https://other.example/second.bin';
  const fixture = await boot(page, [detail(10), detail(11, {original_resource: other, original_resource_sanitized_full: other})]);
  // Another transfer's Details.
  await open(page, 10);
  await value(page).click({button: 'right'});
  await expect(menu(page)).toHaveCount(1);
  await open(page, 11);
  await expect(menu(page)).toHaveCount(0);
  await value(page).click({button: 'right'});
  await page.locator('.dp-row-menu [role="menuitem"]').click();
  await expect.poll(() => page.evaluate(() => window.__copied)).toEqual([other]);
  // A refresh of the same transfer that withdraws Copy URL.
  await open(page, 10);
  await value(page).click({button: 'right'});
  await expect(menu(page)).toHaveCount(1);
  fixture.set(detail(10, {original_resource_copy_allowed: false}));
  await open(page, 10);
  await expect(menu(page)).toHaveCount(0);
  await value(page).click({button: 'right'});
  await expect(menu(page)).toHaveCount(0);
  fixture.set(detail(10));
  // Closing Details and navigating close it.
  await open(page, 10);
  await value(page).click({button: 'right'});
  await page.evaluate(() => window.DPModal.requestModalClose('close'));
  await expect(menu(page)).toHaveCount(0);
  await open(page, 10);
  await value(page).click({button: 'right'});
  await page.evaluate(() => document.dispatchEvent(new CustomEvent('debridpulse:navigation')));
  await expect(menu(page)).toHaveCount(0);
  expect(await page.evaluate(() => window.__copied)).toEqual([other]);
});

test('keyboard: Shift+F10 and the Context Menu key open it at the value; Enter copies; Escape returns focus', async ({page}) => {
  await boot(page, [detail(12)]);
  await open(page, 12);
  for (const key of ['Shift+F10', 'ContextMenu']) {
    await value(page).focus();
    await page.keyboard.press(key);
    await expect(menu(page)).toBeVisible();
    await expect(menu(page).locator('[role="menuitem"]')).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(menu(page)).toHaveCount(0);
    await expect(value(page)).toBeFocused();
    await expect(page.locator('#overlay.open')).toHaveCount(1);
  }
  await page.keyboard.press('Shift+F10');
  await page.keyboard.press('Enter');
  await expect.poll(() => page.evaluate(() => window.__copied)).toEqual([FULL]);
  await expect(value(page)).toBeFocused();
});

test('a hostile-looking safe value is text, never markup, and nothing navigates', async ({page}) => {
  const hostile = 'https://files.example/"><img src=x onerror="window.__pwned=1">&amp;<script>window.__pwned=2</script>';
  await boot(page, [detail(13, {original_resource: hostile, original_resource_sanitized_full: hostile})]);
  await open(page, 13);
  await value(page).hover();
  await expect(tip(page)).toHaveText(hostile);
  await expect(value(page)).toHaveText(hostile);
  await expect(page.locator('#modal-body img, #modal-body script')).toHaveCount(0);
  await value(page).click();
  await value(page).click({button: 'right'});
  await page.locator('.dp-row-menu [role="menuitem"]').click();
  await expect.poll(() => page.evaluate(() => window.__copied)).toEqual([hostile]);
  expect(await page.evaluate(() => window.__pwned)).toBeUndefined();
  expect(page.url()).toMatch(/\/$/);
});
