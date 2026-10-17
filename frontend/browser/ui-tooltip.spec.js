const { test, expect } = require('@playwright/test');

/* DP 1.0.13 informational tooltips: one application owner (ui-tooltip.js,
 * DPTooltip), never the browser's title popup. Every case reads the live
 * application; Downloads rows and an Activity Log refusal are answered by the
 * page where the case needs them. Motion is the browser default except where a
 * case is about reduced motion. */

const tip = page => page.locator('#dp-tooltip');
async function boot(page, {width = 1440, height = 900} = {}) {
  await page.setViewportSize({width, height});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route('**/api/events/stream', () => {});
  await page.goto('/');
  await expect(page.locator('#s-total')).not.toHaveText('—');
}
// Nothing the application renders carries a native title tooltip.
const nativeTitles = page => page.evaluate(() => [...document.querySelectorAll('[title]')].map(node => node.outerHTML.slice(0, 120)));
const shown = page => tip(page).evaluate(node => !node.hidden && node.matches(':popover-open') && getComputedStyle(node).display !== 'none');
const boxOf = locator => locator.evaluate(node => { const r = node.getBoundingClientRect(); return {x: r.left, y: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height}; });

test('Dashboard: a card\'s tooltip appears after the hover delay, in DP\'s material, beside it, and leaves at once', async ({page}) => {
  await boot(page);
  expect(await nativeTitles(page)).toEqual([]);
  const card = page.locator('.dash-hero-stat[data-c="green"]');
  await card.hover();
  await page.waitForTimeout(250);
  expect(await tip(page).count() === 0 || !(await shown(page))).toBe(true);              // not before the delay
  await expect.poll(() => shown(page), {timeout: 1500}).toBe(true);
  await expect(tip(page)).toHaveText('Completed: transfers that have finished downloading.');
  await expect(tip(page)).toHaveAttribute('role', 'tooltip');
  const [at, t] = [await boxOf(card), await boxOf(tip(page))];
  expect(t.bottom).toBeLessThanOrEqual(at.y + 0.5);                                       // above it ...
  expect(at.y - t.bottom).toBeLessThanOrEqual(10);                                        // ... just above
  expect(Math.abs((t.x + t.right) / 2 - (at.x + at.right) / 2)).toBeLessThanOrEqual(1);  // centred on it
  await expect(card).toHaveAttribute('aria-describedby', 'dp-tooltip');                  // described while shown
  await page.mouse.move(4, 4);
  await expect.poll(() => shown(page)).toBe(false);
  await expect(card).not.toHaveAttribute('aria-describedby');                            // given back
});

test('passing over neighbouring targets shows each at once, one tooltip at a time, with no flicker', async ({page}) => {
  await boot(page);
  const cards = page.locator('.dash-hero-stat');
  await cards.nth(0).hover();
  await expect.poll(() => shown(page), {timeout: 1500}).toBe(true);
  const texts = [];
  for (let i = 1; i < 6; i += 1) {
    await cards.nth(i).hover();
    expect(await shown(page)).toBe(true);                                                // warm: shown at once
    texts.push(await tip(page).textContent());
    expect(await page.locator('.dp-tooltip').count()).toBe(1);
    expect(await page.locator('[aria-describedby~="dp-tooltip"]').count()).toBe(1);
  }
  expect(new Set(texts).size).toBe(5);
});

for (const theme of ['dark', 'light']) {
  test(`theme ${theme}: the tooltip is the theme's own floating surface, readable, and clear of the viewport's edges`, async ({page}) => {
    await boot(page, {width: 1280, height: 800});
    await page.evaluate(light => { document.body.classList.toggle('light', light); document.dispatchEvent(new CustomEvent('debridpulse:theme-changed')); }, theme === 'light');
    const toggle = page.locator('#theme-toggle');                                         // the top right corner
    await toggle.hover();
    await expect.poll(() => shown(page), {timeout: 1500}).toBe(true);
    const look = await page.evaluate(() => {
      const node = document.getElementById('dp-tooltip'), probe = document.createElement('div');
      document.body.appendChild(probe);
      const token = name => { probe.style.color = `var(${name})`; return getComputedStyle(probe).color; };
      const style = getComputedStyle(node);
      const out = {background: style.backgroundColor, color: style.color, border: style.borderTopColor,
        overlay: token('--dp-surface-overlay'), text: token('--dp-text-primary'), divider: token('--dp-border-default'),
        radius: style.borderTopLeftRadius, font: style.fontSize};
      probe.remove();
      return out;
    });
    expect(look.background).toBe(look.overlay);
    expect(look.color).toBe(look.text);
    expect(look.border).toBe(look.divider);
    expect([look.radius, look.font]).toEqual(['6px', '11px']);
    // Too close to the top for above: below it, and kept inside the right edge.
    const [at, t] = [await boxOf(toggle), await boxOf(tip(page))];
    expect(t.y).toBeGreaterThanOrEqual(at.bottom - 0.5);
    expect(t.right).toBeLessThanOrEqual(1280 - 7.5);
    expect(t.x).toBeGreaterThanOrEqual(7.5);
    await page.screenshot({path: test.info().outputPath(`tooltip-${theme}.png`)});
  });
}

test('keyboard focus shows it at once; Escape dismisses it until focus leaves; the name stays the control\'s own', async ({page}) => {
  await boot(page);
  const toggle = page.locator('#theme-toggle');
  await expect(page.getByRole('button', {name: 'Switch to light mode'})).toHaveCount(1);  // its name, from aria-label
  await page.keyboard.press('Shift');                                                     // keyboard modality
  await toggle.focus();
  expect(await shown(page)).toBe(true);
  await expect(tip(page)).toHaveText('Switch to light mode');
  await expect(toggle).not.toHaveAttribute('aria-describedby');                          // it only repeats the name
  await page.keyboard.press('Escape');
  expect(await shown(page)).toBe(false);
  await expect(toggle).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await page.keyboard.press('Tab');
  await expect(toggle).toBeFocused();
  expect(await shown(page)).toBe(true);
});

test('a control whose tooltip adds to its name is described by it while it shows (icon and text controls alike)', async ({page}) => {
  await boot(page);
  const add = page.locator('#btn-add-transfer');
  await expect(page.getByRole('button', {name: 'Add', exact: true})).toHaveCount(1);
  await add.hover();
  await expect.poll(() => shown(page), {timeout: 1500}).toBe(true);
  await expect(add).toHaveAttribute('aria-describedby', 'dp-tooltip');
  expect(await page.evaluate(() => document.getElementById('dp-tooltip').textContent)).toBe(await add.getAttribute('data-tooltip'));
  await page.mouse.move(4, 4);
  await expect.poll(() => shown(page)).toBe(false);
  await expect(add).not.toHaveAttribute('aria-describedby');
});

test('Downloads and Activity Log: shell controls and rows rendered after load use the same owner', async ({page}) => {
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({status: 200, contentType: 'application/json',
    body: JSON.stringify({total: 2, items: [1, 2].map(id => ({id, name: `Transfer ${id}`, hash: 'h', status: 'downloading', progress: 10,
      size_bytes: 1024, created_at: '2026-10-01T12:00:00Z', source: 'direct_link', current_provider_id: 'general_http', current_provider_name: 'HTTP & HTTPS'}))})}));
  await boot(page);
  await page.evaluate(() => nav(document.querySelector('[data-view="torrents"]')));
  const date = page.locator('#t-tbody tr[data-torrent-id="1"] .dp-downloads-date-value');
  await expect(date).toBeVisible();
  expect(await nativeTitles(page)).toEqual([]);
  await page.keyboard.press('Shift');
  await date.focus();                                                                     // a row's exact date, by keyboard
  expect(await shown(page)).toBe(true);
  await expect(tip(page)).toHaveText(await date.getAttribute('data-tooltip'));
  await page.locator('.dp-downloads-refresh').hover();
  expect(await shown(page)).toBe(true);
  await expect(tip(page)).toHaveText('Refresh downloads');
  // A re-render that removes the described row hides its tooltip (the pointer clear of every target).
  await page.mouse.move(4, 4);
  await page.locator('.dp-downloads-refresh').focus();
  await date.focus();
  expect(await shown(page)).toBe(true);
  await page.evaluate(() => { document.getElementById('t-tbody').innerHTML = ''; });
  await expect.poll(() => shown(page)).toBe(false);
  // Navigation dismisses a shown tooltip (the pointer moved clear first, so nothing new lies beneath it).
  await page.mouse.move(4, 4);
  await page.keyboard.press('Shift');
  await page.locator('.dp-downloads-refresh').focus();
  expect(await shown(page)).toBe(true);
  await page.evaluate(() => nav(document.querySelector('[data-view="events"]')));
  await expect.poll(() => shown(page)).toBe(false);
  // The Activity Log's own shell control.
  await page.locator('.dp-activity-refresh').hover();
  await expect.poll(() => shown(page), {timeout: 1500}).toBe(true);
  await expect(tip(page)).toHaveText('Refresh activity log');
});

test('a disabled control still explains itself on hover and stays inoperable', async ({page}) => {
  await page.route('**/api/events?**', route => {
    const q = Object.fromEntries(new URL(route.request().url()).searchParams);
    if (q.last) return route.fulfill({status: 409, contentType: 'application/json', body: JSON.stringify({detail: {code: 'last_page_unavailable', message: 'The last page cannot be found yet.'}})});
    const ids = Array.from({length: 50}, (_, i) => 460 - i - (q.before ? 50 : 0));
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items: ids.map(id => ({id, severity: 'info', category: 'transfer', type: 't',
      message: `Event ${id}`, transfer_id: id, occurred_at: '2026-10-09T12:00:00Z'})), limit: 50, snapshot: 460, has_more: true, next_before: ids.at(-1),
      more_after_next: true, newer_available: false, search: {}, window_start: null})});
  });
  await boot(page);
  await page.evaluate(() => { settingsData.activity_log_page_size = 50; nav(document.querySelector('[data-view="events"]')); });
  const last = page.locator('#dp-activity-page-btns [aria-label^="Last page"]');
  await last.click();
  await expect(last).toBeDisabled();
  await expect(last).toHaveAttribute('data-tooltip', 'The last page cannot be found yet.');
  await expect(last).not.toHaveAttribute('title');
  await page.mouse.move(4, 4);
  await last.hover({force: true});
  await expect.poll(() => shown(page), {timeout: 1500}).toBe(true);
  await expect(tip(page)).toHaveText('The last page cannot be found yet.');
  await expect(page.getByRole('button', {name: /Last page unavailable: The last page cannot be found yet\./})).toHaveCount(1);
  await expect(last).toBeDisabled();
});

test('Settings and its dialogs: disclosure, provider and dialog controls use the owner; closing a dialog dismisses its tooltip', async ({page}) => {
  await boot(page);
  await page.evaluate(() => nav(document.querySelector('[data-view="settings"]')));
  await expect(page.locator('#view-settings.active')).toHaveCount(1);
  expect(await nativeTitles(page)).toEqual([]);
  const chip = page.locator('#view-settings .dp-settings-disclosure').first();
  await chip.hover();
  await expect.poll(() => shown(page), {timeout: 1500}).toBe(true);
  await expect(tip(page)).toHaveText(await chip.getAttribute('data-tooltip'));
  // A dialog with the upper-right close control (the application's own open({closeControl}) shape).
  await page.evaluate(() => { window.DPSettingsModal.open({title: 'Probe', dismiss: true, closeControl: true}); });
  const close = page.locator('.dp-modal-overlay:not(.is-closing) [data-modal-close]');
  await expect(close).toHaveCount(1);
  await expect(close).toHaveAccessibleName('Close');                                      // its own name, no title needed
  await page.keyboard.press('Shift');
  await close.focus();
  expect(await shown(page)).toBe(true);
  await expect(tip(page)).toHaveText('Close');
  await close.click();
  await expect.poll(() => shown(page)).toBe(false);
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
  expect(await nativeTitles(page)).toEqual([]);
});

test('scrolling and resizing dismiss it; reduced motion shows and hides it without a fade', async ({page}) => {
  await boot(page);
  const card = page.locator('.dash-hero-stat[data-c="blue"]');
  await card.hover();
  await expect.poll(() => shown(page), {timeout: 1500}).toBe(true);
  expect(await tip(page).evaluate(node => getComputedStyle(node).transitionDuration)).toBe('0.12s');
  await page.evaluate(() => window.dispatchEvent(new Event('resize')));
  await expect.poll(() => shown(page)).toBe(false);
  await page.mouse.move(4, 4);
  await card.hover();
  await expect.poll(() => shown(page), {timeout: 1500}).toBe(true);
  await page.evaluate(() => document.getElementById('content').dispatchEvent(new Event('scroll')));
  await expect.poll(() => shown(page)).toBe(false);
  await page.emulateMedia({reducedMotion: 'reduce'});
  await page.mouse.move(4, 4);
  await card.hover();
  await expect.poll(() => shown(page), {timeout: 1500}).toBe(true);
  expect(await tip(page).evaluate(node => getComputedStyle(node).transitionDuration)).toBe('0s');
});

test('touch: a tap shows no hover tooltip and the tap still acts', async ({browser}) => {
  const context = await browser.newContext({hasTouch: true, viewport: {width: 900, height: 900}});
  const page = await context.newPage();
  try {
    await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
    await page.goto('/');
    await expect(page.locator('#s-total')).not.toHaveText('—');
    await page.locator('.dash-hero-stat[data-c="green"]').tap();
    await page.waitForTimeout(700);
    expect(await tip(page).count() === 0 || !(await shown(page))).toBe(true);
    await page.locator('#theme-toggle').tap();                                            // the tap still toggles
    await expect.poll(() => page.evaluate(() => document.body.classList.contains('light'))).toBe(true);
    await page.waitForTimeout(700);
    expect(await tip(page).count() === 0 || !(await shown(page))).toBe(true);
  } finally { await context.close(); }
});

test('native file selection is untouched: Add with nothing typed still opens the platform file chooser', async ({page}) => {
  await boot(page);
  await page.locator('#btn-add-transfer').hover();
  await expect.poll(() => shown(page), {timeout: 1500}).toBe(true);
  const chooser = page.waitForEvent('filechooser');
  await page.locator('#btn-add-transfer').click();
  expect(await (await chooser).isMultiple()).toBeDefined();
  expect(await shown(page)).toBe(false);                                                  // the press dismissed the tooltip
});

test('one observer at most: traversal, redisplay, dismissal and removal never leave a second one live', async ({page}) => {
  // Test-side instrument: the live MutationObservers the tooltip owner created (observing, not yet disconnected).
  await page.addInitScript(() => {
    const Native = window.MutationObserver;
    window.__tipObservers = new Set();
    window.MutationObserver = class extends Native {
      constructor(callback) { super(callback); this.dpTooltip = /ui-tooltip\.js/.test(new Error().stack || ''); }
      observe(...args) { if (this.dpTooltip) window.__tipObservers.add(this); return super.observe(...args); }
      disconnect() { window.__tipObservers.delete(this); return super.disconnect(); }
    };
  });
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({status: 200, contentType: 'application/json',
    body: JSON.stringify({total: 1, items: [{id: 1, name: 'Transfer 1', hash: 'h', status: 'downloading', progress: 10, size_bytes: 1024,
      created_at: '2026-10-01T12:00:00Z', source: 'direct_link', current_provider_id: 'general_http', current_provider_name: 'HTTP & HTTPS'}]})}));
  await boot(page);
  const live = () => page.evaluate(() => window.__tipObservers.size);
  // Direct traversal: six targets in a row, each shown at once.
  const cards = page.locator('.dash-hero-stat');
  await cards.nth(0).hover();
  await expect.poll(() => shown(page), {timeout: 1500}).toBe(true);
  for (let i = 1; i < 6; i += 1) {
    await cards.nth(i).hover();
    expect(await shown(page)).toBe(true);
    expect(await live()).toBe(1);
  }
  // Redisplay of the element already shown (keyboard focus on the hovered toggle).
  await page.locator('#theme-toggle').hover();
  expect(await shown(page)).toBe(true);
  await page.keyboard.press('Shift');
  await page.locator('#theme-toggle').focus();
  expect(await shown(page)).toBe(true);
  expect(await live()).toBe(1);
  // Dismissal lets go of it.
  await page.mouse.move(4, 4);
  await page.locator('#theme-toggle').blur();
  await expect.poll(() => shown(page)).toBe(false);
  expect(await live()).toBe(0);
  await page.keyboard.press('Shift');
  await page.locator('#theme-toggle').focus();
  expect(await live()).toBe(1);
  await page.keyboard.press('Escape');
  expect(await live()).toBe(0);
  // Removal of the described element lets go of it too.
  await page.evaluate(() => nav(document.querySelector('[data-view="torrents"]')));
  const date = page.locator('#t-tbody tr[data-torrent-id="1"] .dp-downloads-date-value');
  await expect(date).toBeVisible();
  await page.keyboard.press('Shift');
  await date.focus();
  expect(await shown(page)).toBe(true);
  expect(await live()).toBe(1);
  await page.evaluate(() => { document.getElementById('t-tbody').innerHTML = ''; });
  await expect.poll(() => shown(page)).toBe(false);
  expect(await live()).toBe(0);
});
