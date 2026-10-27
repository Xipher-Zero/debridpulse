const { test, expect } = require('@playwright/test');

/* Settings -> Services: the eight governing and premium-provider headers
 * (Premium Services, Usenet, AllDebrid, Debrid-Link, Premiumize, Real-Debrid,
 * TorBox, Network Sources). One row at every width from the desktop down to
 * 780px: the identity at the leading inset, the operational rail at the
 * trailing inset, and the card's one-line purpose hint on the header's own
 * midpoint only while it fits there clear of both; otherwise the hint is out
 * of the layout and the card's Help chip says it instead. Each card's Help
 * opens its own topic. The Network Sources protocol tiles are untouched.
 *
 * Everything is read from the rendered geometry and the DP tooltip -- never
 * from the page's own decision. */

const HEADERS = ['Premium Services', 'Usenet', 'AllDebrid', 'Debrid-Link', 'Premiumize', 'Real-Debrid', 'TorBox', 'Network Sources'];
const TOPICS = {'Premium Services': 'premium-services', 'Usenet': 'usenet', 'AllDebrid': 'alldebrid', 'Debrid-Link': 'debridlink',
  'Premiumize': 'premiumize', 'Real-Debrid': 'realdebrid', 'TorBox': 'torbox', 'Network Sources': 'network-sources'};

async function open(page, width, {fallbackFonts = false} = {}) {
  if (fallbackFonts) await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.setViewportSize({width, height: 1000});
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]:not([data-title])').click();
  await expect(page.locator('#view-settings [data-panel="sources"] .dp-settings-debrid-services')).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await frames(page);
}

// Two frames drawn after every slide (the view's own entry included) and
// finite animation have finished: the geometry and hit-testing are the page's.
const frames = async page => {
  await page.waitForFunction(() => !document.querySelector('.dp-motion-surface') && document.getAnimations()
    .every(animation => animation.playState !== 'running' || animation.effect?.getTiming().iterations === Infinity));
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
};

async function resize(page, width) {
  await page.setViewportSize({width, height: 1000});
  await page.waitForFunction(() => document.getElementById('sidebar').getAnimations().every(animation => animation.playState !== 'running'));
  await frames(page);
}

/* The eight headers as drawn. */
function headers(page) {
  return page.evaluate(() => {
    const panel = document.querySelector('#view-settings [data-panel="sources"]');
    const nodes = [...panel.querySelectorAll('.dp-settings-source-group > .card-header, .dp-settings-debrid-services .dp-settings-provider-card:not(.dp-settings-source-box) > .card-header')];
    return nodes.map(h => {
      const st = getComputedStyle(h), hb = h.getBoundingClientRect();
      const left = hb.left + h.clientLeft + parseFloat(st.paddingLeft), right = hb.left + h.clientLeft + h.clientWidth - parseFloat(st.paddingRight);
      const title = h.querySelector(':scope > .dp-settings-card-title-group').getBoundingClientRect();
      const rail = h.querySelector(':scope > .dp-settings-card-header-controls');
      const r = rail.getBoundingClientRect();
      const hint = h.querySelector(':scope > .dp-settings-card-header-center > .dp-settings-provider-header-copy');
      const shown = !!hint && hint.getClientRects().length > 0;
      const c = shown ? hint.getBoundingClientRect() : null;
      const chips = [...rail.querySelectorAll('.dp-settings-help-chip')];
      const items = [...rail.children].map(node => node.getBoundingClientRect());
      return {name: h.querySelector('.dp-settings-card-title-text').textContent.trim(), height: hb.height, minHeight: parseFloat(st.minHeight),
        gap: parseFloat(st.columnGap), titleInset: title.left - left, railInset: right - r.right,
        oneRow: items.every(item => Math.abs((item.top + item.bottom) / 2 - (title.top + title.bottom) / 2) < 2),
        hint: hint ? hint.textContent.trim() : null, shown,
        lines: shown ? Math.round(c.height / parseFloat(getComputedStyle(hint).lineHeight)) : 0,
        offset: shown ? (c.left + c.right) / 2 - (left + right) / 2 : null,
        clearLeft: shown ? c.left - title.right : null, clearRight: shown ? r.left - c.right : null,
        chips: chips.length, topic: chips[0]?.dataset.settingsHelp, name2: chips[0]?.getAttribute('aria-label'), tooltip: chips[0]?.getAttribute('data-tooltip'),
        order: [...rail.children].map(node => node.classList.contains('dp-settings-provider-config-status') ? 'status'
          : node.classList.contains('dp-settings-help-chip') ? 'help' : node.classList.contains('dp-settings-header-action') ? 'test' : 'enable')};
    });
  });
}

/* The header contract at this width; returns the problems. */
function problems(list, width) {
  const out = [];
  if (list.map(h => h.name).join() !== HEADERS.join()) out.push(`headers ${list.map(h => h.name)}`);
  for (const h of list) {
    const at = `${h.name} at ${width}px`;
    if (Math.abs(h.height - h.minHeight) > 0.5 || !h.oneRow) out.push(`${at}: not one row (${h.height}px)`);
    if (Math.abs(h.titleInset) > 0.5) out.push(`${at}: identity off its inset (${h.titleInset.toFixed(1)})`);
    if (Math.abs(h.railInset) > 0.5) out.push(`${at}: rail off the trailing inset (${h.railInset.toFixed(1)})`);
    if (h.chips !== 1) out.push(`${at}: ${h.chips} help chips`);
    if (h.topic !== TOPICS[h.name] || h.name2 !== `Help: ${h.name}`) out.push(`${at}: help ${h.topic} / ${h.name2}`);
    if (!h.hint) out.push(`${at}: no hint`);
    if (h.shown) {
      if (h.lines !== 1) out.push(`${at}: hint on ${h.lines} lines`);
      if (Math.abs(h.offset) > 0.5) out.push(`${at}: hint off the midpoint (${h.offset.toFixed(1)})`);
      if (h.clearLeft < h.gap - 0.5 || h.clearRight < h.gap - 0.5) out.push(`${at}: hint touches (${h.clearLeft.toFixed(1)}, ${h.clearRight.toFixed(1)})`);
      if (h.tooltip !== h.name2) out.push(`${at}: help tooltip "${h.tooltip}" while the hint shows`);
    } else if (h.tooltip !== h.hint) out.push(`${at}: help tooltip "${h.tooltip}", hidden hint "${h.hint}"`);
  }
  return out;
}

for (const fonts of ['web fonts', 'fallback fonts']) {
  test(`with ${fonts}, every Services header is one row with its identity and rail at their insets, the hint centred while it fits and otherwise carried by Help`, async ({page}) => {
    await open(page, 1920, {fallbackFonts: fonts === 'fallback fonts'});
    const report = {};
    const states = [];
    for (const width of [1920, 1440, 1300, 1181, 1180, 1100, 1000, 901, 900, 850, 800, 780]) {
      await resize(page, width);
      const list = await headers(page);
      const found = problems(list, width);
      if (found.length) report[width] = found;
      states.push(`${width}: ${list.filter(h => h.shown).length}/8 shown`);
      if (width === 1920) expect(list.every(h => h.shown), 'every hint shows on the wide desktop').toBe(true);
      if (width === 780) expect(list.every(h => !h.shown), 'no hint fits at 780px').toBe(true);
    }
    expect(report).toEqual({});
    // Usenet, the full-control reference: status, Help, Test, Enable.
    const usenet = (await headers(page)).find(h => h.name === 'Usenet');
    expect(usenet.order).toEqual(['status', 'help', 'test', 'enable']);
    test.info().annotations.push({type: `hints (${fonts})`, description: states.join('; ')});
  });
}

test('across one hint\'s fit both ways, it hides before it would touch, comes back after, and the Help tooltip follows with no stale text', async ({page}) => {
  await open(page, 1300);
  const allDebrid = async () => (await headers(page)).find(h => h.name === 'AllDebrid');
  // The widest width at which AllDebrid's hint no longer fits, from the geometry itself.
  let width = 1300;
  while ((await allDebrid()).shown) { width -= 4; await resize(page, width); expect(width).toBeGreaterThan(780); }
  while (!(await allDebrid()).shown) { width += 1; await resize(page, width); }
  const edge = width;   // the first width, coming up, at which it fits
  for (const [w, shown] of [[edge, true], [edge - 1, false], [edge - 2, false], [edge, true], [edge + 1, true], [edge - 1, false]]) {
    await resize(page, w);
    const list = await headers(page);
    expect(problems(list, w), `${w}px`).toEqual([]);
    const h = list.find(entry => entry.name === 'AllDebrid');
    expect(h.shown, `${w}px`).toBe(shown);
    expect(h.tooltip, `${w}px`).toBe(shown ? 'Help: AllDebrid' : h.hint);
    if (shown) expect(Math.min(h.clearLeft, h.clearRight), `${w}px`).toBeGreaterThanOrEqual(h.gap - 0.5);
  }
  test.info().annotations.push({type: 'AllDebrid hint', description: `fits from ${edge}px (web fonts)`});
});

test('the Help chip shows the hidden hint as its DP tooltip, on hover and on keyboard focus, and the ordinary Help tooltip where the hint shows; it opens the card\'s own help', async ({page}) => {
  await open(page, 780);
  const card = page.locator('#view-settings .dp-settings-provider-card--torbox');
  const chip = card.locator(':scope > .card-header .dp-settings-help-chip');
  const hint = await card.locator('.dp-settings-provider-header-copy').textContent();
  const tip = page.locator('#dp-tooltip.is-shown');
  await chip.hover();
  await expect(tip).toHaveText(hint.trim());
  await page.mouse.move(2, 990);
  await expect(tip).toHaveCount(0);
  await card.locator(':scope > .card-header .dp-settings-disclosure').focus();
  await page.keyboard.press('Tab');
  await expect(chip).toBeFocused();
  await expect(tip).toHaveText(hint.trim());
  await expect(chip).toHaveAccessibleName('Help: TorBox');
  await page.keyboard.press('Enter');
  const dialog = page.locator('.dp-modal-dialog.dp-settings-help-dialog');
  await expect(dialog.locator('.dp-modal-title')).toHaveText('TorBox');
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(chip).toBeFocused();

  await resize(page, 1920);
  await page.mouse.move(2, 990);
  await chip.hover();
  await expect(tip).toHaveText('Help: TorBox');
  await expect(card.locator('.dp-settings-provider-header-copy')).toBeVisible();
});

test('a hint that stops fitting when fonts finish loading is handed to Help, and comes back when it fits again', async ({page}) => {
  await open(page, 1300);
  expect((await headers(page)).every(h => h.shown)).toBe(true);
  // The hints' own text wider (the headers, identities and rails unchanged),
  // re-measured as a font load re-measures.
  await page.addStyleTag({content: '#view-settings .dp-settings-provider-header-copy { letter-spacing: 3px; }'});
  await page.evaluate(() => document.fonts.dispatchEvent(new Event('loadingdone')));
  await frames(page);
  let list = await headers(page);
  expect(problems(list, 1300)).toEqual([]);
  expect(list.filter(h => !h.shown).length, 'wider hints that no longer fit are handed to Help').toBeGreaterThan(0);
  await page.evaluate(() => { for (const style of document.querySelectorAll('style')) if (style.textContent.includes('letter-spacing: 3px')) style.remove(); });
  await page.evaluate(() => document.fonts.dispatchEvent(new Event('loadingdone')));
  await frames(page);
  list = await headers(page);
  expect(problems(list, 1300)).toEqual([]);
  expect(list.every(h => h.shown)).toBe(true);
});

test('each of the eight Help chips opens its own read-only topic: five distinct provider topics, and the existing Usenet and group topics', async ({page}) => {
  await open(page, 1920);
  const bodies = {};
  for (const name of HEADERS) {
    const chip = page.locator(`#view-settings [data-panel="sources"] [data-settings-help="${TOPICS[name]}"]`);
    await expect(chip).toHaveCount(1);
    await chip.click();
    const dialog = page.locator('.dp-modal-dialog.dp-settings-help-dialog');
    await expect(dialog.locator('.dp-modal-title')).toHaveText(name);
    bodies[name] = (await dialog.locator('.dp-settings-help-content').innerText()).trim();
    expect(bodies[name].length, name).toBeGreaterThan(300);
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(chip).toBeFocused();
  }
  const providers = ['AllDebrid', 'Debrid-Link', 'Premiumize', 'Real-Debrid', 'TorBox'];
  expect(new Set(providers.map(name => bodies[name])).size).toBe(5);
  for (const name of providers) {
    expect(bodies[name], name).toContain(name);
    expect(bodies[name], name).toMatch(/Prepare Backup Torrents \((on|off) by default/);
  }
});

test('at 780px every rail control is whole and is its own target: status, Help, Test, Enable and the disclosure', async ({page}) => {
  await open(page, 780);
  const report = await page.evaluate(() => {
    const out = [];
    for (const header of document.querySelectorAll('#view-settings .dp-settings-debrid-services .dp-settings-provider-card:not(.dp-settings-source-box) > .card-header')) {
      const name = header.querySelector('.dp-settings-card-title-text').textContent.trim();
      header.scrollIntoView({block: 'center'});
      const hb = header.getBoundingClientRect();
      for (const selector of ['.dp-settings-disclosure', '.dp-settings-help-chip', '.dp-settings-header-action button', '.dp-settings-integration-header-enable']) {
        const node = header.querySelector(selector);
        if (!node) { out.push(`${name}: no ${selector}`); continue; }
        const r = node.getBoundingClientRect();
        if (r.left < hb.left || r.right > hb.right || r.width < 24 || r.height < 24) out.push(`${name}: ${selector} ${r.width}x${r.height}`);
        const hit = document.elementFromPoint((r.left + r.right) / 2, (r.top + r.bottom) / 2);
        if (!node.contains(hit)) out.push(`${name}: ${selector} covered by ${hit?.className}`);
      }
    }
    return out;
  });
  expect(report).toEqual([]);
});

test('the Network Sources protocol tiles carry no Help, tooltip or hint of their own', async ({page}) => {
  await open(page, 1440);
  const tiles = await page.evaluate(() => [...document.querySelectorAll('#view-settings .dp-settings-source-box')].map(tile => ({
    help: tile.querySelectorAll('.dp-settings-help-chip, [data-settings-help], [data-tooltip], .dp-settings-provider-header-copy, .dp-settings-card-header--hinted').length,
    toggle: tile.querySelectorAll('[data-integration-enabled]').length})));
  expect(tiles.length).toBeGreaterThanOrEqual(7);
  for (const tile of tiles) expect(tile).toEqual({help: 0, toggle: 1});
});
