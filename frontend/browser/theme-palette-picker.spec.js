const { test, expect } = require('@playwright/test');

/* DP 1.0.14 contextual theme picker. The chevron beside the mode toggle opens
 * the active mode's palettes only; a choice applies at once to that mode
 * alone, is kept per mode in this browser, and is announced to theme
 * consumers (debridpulse:theme-changed) after it applies. Every read here is
 * the page's own computed style or storage; nothing is written to the shared
 * backend. */

const DARK = ['DebridPulse (Default)', 'Catppuccin Mocha', 'Dracula', 'Gruvbox Dark', 'Everforest Dark', 'Rosé Pine',
  'Kanagawa Wave', 'Ayu Mirage', "SynthWave '84", 'Oxocarbon Dark'];
const LIGHT = ['DebridPulse (Default)', 'Catppuccin Latte', 'Nord Light', 'Gruvbox Light', 'Rosé Pine Dawn', 'Everforest Light',
  'Ayu Light', 'Flexoki Light', 'Oxocarbon Light', 'Bluloco Light'];
// Retired in 1.0.14's catalog diversification: never offered, never accepted.
const RETIRED = {dark: ['nord-dark', 'solarized-dark', 'tokyo-night'], light: ['solarized-light', 'tokyo-night-day']};

const noFonts = page => page.route(/fonts\.(googleapis|gstatic)\.com/, route => route.abort());
const token = (page, name) => page.evaluate(n => getComputedStyle(document.body).getPropertyValue(n).trim(), name);
const state = page => page.evaluate(() => {
  const read = key => { try { return localStorage.getItem(key); } catch (_) { return 'unavailable'; } };
  return {light: document.body.classList.contains('light'), palette: document.body.getAttribute('data-dp-palette'),
    theme: read('theme'), darkChoice: read('dp-theme-palette-dark'), lightChoice: read('dp-theme-palette-light')};
});
const menuOpen = page => page.evaluate(() => document.getElementById('theme-picker').matches(':popover-open'));
const rows = page => page.locator('#theme-picker [role="menuitemradio"]');

async function openPicker(page) {
  await page.click('#theme-picker-toggle');
  await expect.poll(() => menuOpen(page)).toBe(true);
  return page.evaluate(() => ({
    heading: document.getElementById('theme-picker-title').textContent,
    note: document.getElementById('theme-picker-note').textContent,
    labels: [...document.querySelectorAll('#theme-picker [role="menuitemradio"]')].map(row => row.textContent.trim()),
    checked: [...document.querySelectorAll('#theme-picker [aria-checked="true"]')].map(row => row.textContent.trim()),
    expanded: document.getElementById('theme-picker-toggle').getAttribute('aria-expanded'),
  }));
}
// Colour changes ease over the controls' own short transitions; read once they end.
const settled = page => page.waitForFunction(() => !document.getAnimations().some(animation => animation instanceof CSSTransition && animation.playState === 'running'));
async function choose(page, label) {
  await openPicker(page);
  await rows(page).filter({hasText: label}).click();
  await expect.poll(() => menuOpen(page)).toBe(false);
  await settled(page);
}

test('the picker lists only the active mode; each mode keeps its own palette through toggles and a reload', async ({page}) => {
  await noFonts(page);
  await page.goto('/');
  // A new browser: DebridPulse Dark exactly, no palette on the page, picker closed.
  expect(await state(page)).toMatchObject({light: false, palette: null, darkChoice: null, lightChoice: null});
  expect(await token(page, '--dp-bg-app')).toBe('#050814');
  expect(await menuOpen(page)).toBe(false);

  let menu = await openPicker(page);
  expect(menu).toEqual({heading: 'Dark themes', note: 'Changes dark appearance only', labels: DARK, checked: ['DebridPulse (Default)'], expanded: 'true'});
  // A swatch shows the palette's own values: Dracula's background and purple.
  const swatch = await page.evaluate(() => {
    const cells = [...document.querySelectorAll('#theme-picker [role="menuitemradio"]')][2].querySelectorAll('.dp-theme-swatch i');
    return [...cells].map(cell => getComputedStyle(cell).backgroundColor);
  });
  expect(swatch).toEqual(['rgb(25, 26, 33)', 'rgb(68, 71, 90)', 'rgb(189, 147, 249)', 'rgb(255, 121, 198)']);
  await page.keyboard.press('Escape');

  await choose(page, 'Catppuccin Mocha');
  expect(await state(page)).toMatchObject({light: false, palette: 'catppuccin-mocha', darkChoice: 'catppuccin-mocha', lightChoice: null});
  // Applied, not merely named: the sidebar is painted from Mocha's mantle.
  expect(await page.evaluate(() => getComputedStyle(document.getElementById('sidebar')).backgroundImage)).toContain('rgb(24, 24, 37)');

  await page.click('#theme-toggle');
  expect(await state(page)).toMatchObject({light: true, palette: null, theme: 'light', darkChoice: 'catppuccin-mocha', lightChoice: null});
  expect(await token(page, '--dp-bg-app')).toBe('#f4f7fc');

  menu = await openPicker(page);
  expect(menu).toMatchObject({heading: 'Light themes', note: 'Changes light appearance only', labels: LIGHT, checked: ['DebridPulse (Default)']});
  await page.keyboard.press('Escape');
  await choose(page, 'Flexoki Light');
  expect(await state(page)).toMatchObject({light: true, palette: 'flexoki-light', darkChoice: 'catppuccin-mocha', lightChoice: 'flexoki-light'});

  for (const [light, palette] of [[false, 'catppuccin-mocha'], [true, 'flexoki-light'], [false, 'catppuccin-mocha']]) {
    await page.click('#theme-toggle');
    expect(await state(page)).toMatchObject({light, palette});
  }
  await page.reload();
  expect(await state(page)).toMatchObject({light: false, palette: 'catppuccin-mocha'});
  expect((await openPicker(page)).checked).toEqual(['Catppuccin Mocha']);
  await page.keyboard.press('Escape');
  await page.click('#theme-toggle');
  expect(await state(page)).toMatchObject({light: true, palette: 'flexoki-light'});
});

test('a browser with only theme=light, stale palette names or no storage at all gets the native palettes', async ({page, context}) => {
  await noFonts(page);
  await page.addInitScript(() => {
    localStorage.setItem('theme', 'light');
    localStorage.setItem('dp-theme-palette-light', 'retired-palette');
    localStorage.setItem('dp-theme-palette-dark', 'dracula-classic');
  });
  await page.goto('/');
  expect(await state(page)).toMatchObject({light: true, palette: null});
  expect(await token(page, '--dp-bg-app')).toBe('#f4f7fc');
  expect((await openPicker(page)).checked).toEqual(['DebridPulse (Default)']);
  await page.keyboard.press('Escape');
  await page.click('#theme-toggle');
  expect(await state(page)).toMatchObject({light: false, palette: null});

  const blocked = await context.newPage();
  const errors = [];
  blocked.on('pageerror', error => errors.push(error.message));
  await noFonts(blocked);
  await blocked.addInitScript(() => Object.defineProperty(window, 'localStorage', {get() { throw new Error('storage denied'); }}));
  await blocked.goto('/');
  expect(await state(blocked)).toMatchObject({light: false, palette: null, theme: 'unavailable'});
  await choose(blocked, 'Dracula');
  expect(await state(blocked)).toMatchObject({light: false, palette: 'dracula'});
  await blocked.click('#theme-toggle');
  expect(await state(blocked)).toMatchObject({light: true, palette: null});
  await blocked.click('#theme-toggle');
  expect(await state(blocked)).toMatchObject({light: false, palette: 'dracula'});
  expect(errors).toEqual([]);
});

test('a retired palette stored in this browser is its own mode\'s native palette, keeps the other mode\'s choice and is never rewritten', async ({page, context}) => {
  await noFonts(page);
  // Dark holds a retired palette, Light a current one.
  await page.addInitScript(([dark]) => {
    if (sessionStorage.getItem('seeded')) return;
    sessionStorage.setItem('seeded', '1');
    localStorage.setItem('theme', 'dark');
    localStorage.setItem('dp-theme-palette-dark', dark);
    localStorage.setItem('dp-theme-palette-light', 'flexoki-light');
  }, [RETIRED.dark[2]]);
  await page.goto('/');
  expect(await state(page)).toMatchObject({light: false, palette: null, darkChoice: 'tokyo-night', lightChoice: 'flexoki-light'});
  expect(await token(page, '--dp-bg-app')).toBe('#050814');
  const menu = await openPicker(page);
  expect(menu.checked).toEqual(['DebridPulse (Default)']);
  for (const retired of [...RETIRED.dark, ...RETIRED.light]) await expect(page.locator(`#theme-picker [data-palette="${retired}"]`)).toHaveCount(0);
  await page.keyboard.press('Escape');
  await page.click('#theme-toggle');
  // The other mode's own choice, untouched; the stored retired name is not rewritten.
  expect(await state(page)).toMatchObject({light: true, palette: 'flexoki-light', darkChoice: 'tokyo-night', lightChoice: 'flexoki-light'});
  await page.reload();
  expect(await state(page)).toMatchObject({light: true, palette: 'flexoki-light', darkChoice: 'tokyo-night'});

  // The same for Light: a retired light palette beside a current dark one.
  const other = await context.newPage();
  await noFonts(other);
  await other.addInitScript(([light]) => {
    if (sessionStorage.getItem('seeded-light')) return;
    sessionStorage.setItem('seeded-light', '1');
    localStorage.setItem('theme', 'light');
    localStorage.setItem('dp-theme-palette-light', light);
    localStorage.setItem('dp-theme-palette-dark', 'kanagawa-wave');
  }, [RETIRED.light[0]]);
  await other.goto('/');
  expect(await state(other)).toMatchObject({light: true, palette: null, lightChoice: 'solarized-light', darkChoice: 'kanagawa-wave'});
  expect(await token(other, '--dp-bg-app')).toBe('#f4f7fc');
  await other.click('#theme-toggle');
  expect(await state(other)).toMatchObject({light: false, palette: 'kanagawa-wave', lightChoice: 'solarized-light'});
  // Choosing replaces it, as any choice does.
  await other.click('#theme-toggle');
  await choose(other, 'Bluloco Light');
  expect(await state(other)).toMatchObject({light: true, palette: 'bluloco-light', lightChoice: 'bluloco-light', darkChoice: 'kanagawa-wave'});
});

test('ten palettes per mode fit the menu at the narrowest desktop width without moving the topbar', async ({page}) => {
  await noFonts(page);
  await page.setViewportSize({width: 780, height: 600});
  await page.goto('/');
  const fixed = () => page.evaluate(() => ['#theme-toggle', '#theme-picker-toggle', '#runtime-speed-badge', '#topbar-actions']
    .map(selector => { const box = document.querySelector(selector).getBoundingClientRect(); return [selector, box.x, box.y, box.width, box.height]; }));
  const before = await fixed();
  for (const mode of ['dark', 'light']) {
    const menu = await openPicker(page);
    expect(menu.labels).toEqual(mode === 'dark' ? DARK : LIGHT);
    const fit = await page.evaluate(() => {
      const panel = document.getElementById('theme-picker').getBoundingClientRect();
      const list = [...document.querySelectorAll('#theme-picker [role="menuitemradio"]')];
      list[list.length - 1].scrollIntoView({block: 'nearest'});
      const last = list[list.length - 1].getBoundingClientRect();
      const names = list.map(row => { const name = row.querySelector('.dp-theme-picker-name'); return name.scrollWidth <= name.clientWidth + 1; });
      return {inside: panel.top >= 0 && panel.bottom <= innerHeight && panel.left >= 0 && panel.right <= innerWidth,
        lastVisible: last.top >= panel.top - 1 && last.bottom <= panel.bottom + 1, namesWhole: names.every(Boolean)};
    });
    expect(fit).toEqual({inside: true, lastVisible: true, namesWhole: true});
    expect(await fixed()).toEqual(before);
    await page.keyboard.press('Escape');
    if (mode === 'dark') { await page.click('#theme-toggle'); await settled(page); }
  }
});

test('DebridPulse (Default) puts back the exact native palette in each mode', async ({page}) => {
  await noFonts(page);
  await page.goto('/');
  const paint = () => page.evaluate(() => ['body', '#sidebar', '.nav-item.active', '#theme-toggle', '#runtime-speed-badge', '#page-title', '.dash-hero-stat', '#dash-activity-card']
    .map(selector => { const style = getComputedStyle(document.querySelector(selector)); return [selector, style.backgroundColor, style.backgroundImage, style.color, style.borderTopColor, style.boxShadow]; }));
  for (const [mode, other] of [['dark', "SynthWave '84"], ['light', 'Gruvbox Light']]) {
    const native = await paint();
    await choose(page, other);
    expect(await paint()).not.toEqual(native);
    await choose(page, 'DebridPulse (Default)');
    expect(await state(page)).toMatchObject({palette: null, [`${mode}Choice`]: `debridpulse-${mode}`});
    expect(await paint()).toEqual(native);
    if (mode === 'dark') { await page.click('#theme-toggle'); await settled(page); }
  }
});

test('the menu works from the keyboard, Escape and an outside press dismiss it, and a choice presses nothing beneath', async ({page}) => {
  await noFonts(page);
  // Pause/Resume All and the speed cap: the operational controls beside the chevron.
  const operations = [];
  page.on('request', request => { if (/\/api\/(processing\/|execution\/runtime-limits)/.test(request.url()) && request.method() !== 'GET') operations.push(request.url()); });
  await page.goto('/');
  await page.focus('#theme-picker-toggle');
  await page.keyboard.press('Enter');
  await expect.poll(() => menuOpen(page)).toBe(true);
  expect(await page.evaluate(() => document.activeElement.textContent.trim())).toBe('DebridPulse (Default)');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  expect(await page.evaluate(() => document.activeElement.textContent.trim())).toBe('Dracula');
  await page.keyboard.press('Enter');
  await expect.poll(() => menuOpen(page)).toBe(false);
  expect(await page.evaluate(() => document.activeElement.id)).toBe('theme-picker-toggle');
  expect(await page.getAttribute('#theme-picker-toggle', 'aria-expanded')).toBe('false');
  expect(await state(page)).toMatchObject({light: false, palette: 'dracula'});

  await page.keyboard.press('Enter');
  await expect.poll(() => menuOpen(page)).toBe(true);
  await page.keyboard.press('Escape');
  await expect.poll(() => menuOpen(page)).toBe(false);
  expect(await page.evaluate(() => document.activeElement.id)).toBe('theme-picker-toggle');

  const box = (await openPicker(page), await page.locator('#theme-picker').boundingBox());
  await page.mouse.click(20, 980);
  await expect.poll(() => menuOpen(page)).toBe(false);
  // Closed, it holds no space and catches no press.
  expect(await page.evaluate(([x, y]) => Boolean(document.elementFromPoint(x, y)?.closest('#theme-picker')), [box.x + box.width / 2, box.y + box.height / 2])).toBe(false);

  expect(await state(page)).toMatchObject({light: false, palette: 'dracula'});
  expect(await page.evaluate(() => document.getElementById('runtime-cap-menu').hidden)).toBe(true);
  expect(operations).toEqual([]);
});

test('a palette chosen within a mode is announced after it applies; Statistics and the Dashboard ECG follow without a reload', async ({page}) => {
  await noFonts(page);
  await page.route(url => url.pathname === '/api/stats/detail', route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
    period: '24h', totals: {torrent_total: 3, completed_count: 2, partial_total: 0, success_rate_pct: 66, completed_size: 0},
    torrent_status: [{status: 'completed', count: 2}], file_status: [], event_levels: [], transfer_methods: [],
    daily_completions: [0, 1, 2].map(i => ({date: `2026-10-2${i}`, count: i + 1, premium: i, network: 1, unattributed: 0})),
  })}));
  await page.goto('/');
  await page.evaluate(() => {
    window.__themeEvents = [];
    document.addEventListener('debridpulse:theme-changed', event => {
      const chart = document.getElementById('daily-chart')?._ci;
      window.__themeEvents.push({detail: event.detail, palette: document.body.getAttribute('data-dp-palette'),
        background: getComputedStyle(document.body).getPropertyValue('--dp-bg-app').trim(),
        grid: chart?.options.scales.x.grid.color, ticks: chart?.options.scales.y.ticks.color});
    });
  });
  const spark = () => page.evaluate(() => Object.fromEntries(['orange', 'green'].map(c => {
    const stop = document.querySelector(`#view-dashboard .dash-hero-stat[data-c="${c}"] .dp-card-spark-rest`);
    return [c, stop ? getComputedStyle(stop).stopColor : null];
  })));
  const ecgBefore = await spark();
  expect(ecgBefore.orange).toBeTruthy();

  await page.evaluate(() => nav(document.querySelector('[data-view="stats"]')));
  await page.waitForFunction(() => document.getElementById('daily-chart')?._ci);
  const chartFrame = () => page.evaluate(() => { const chart = document.getElementById('daily-chart')._ci; return {instance: chart === window.__chart, grid: chart.options.scales.x.grid.color, ticks: chart.options.scales.y.ticks.color, network: chart.data.datasets[0].dpRole, totalLegend: document.querySelector('#completions-legend [data-dp-role="total"] .dp-stats-chart-legend-swatch')?.style.background}; });
  await page.evaluate(() => { window.__chart = document.getElementById('daily-chart')._ci; });
  const native = await chartFrame();
  expect(native).toMatchObject({instance: true, grid: '#26324d', ticks: '#7c87aa'});

  await choose(page, 'Dracula');
  // One announcement, for the dark mode, made once the palette was in force.
  expect(await page.evaluate(() => window.__themeEvents)).toEqual([{detail: {light: false}, palette: 'dracula', background: '#191a21', grid: '#44475a', ticks: '#9aa3cc'}]);
  const recoloured = await chartFrame();
  expect(recoloured).toEqual({...native, grid: '#44475a', ticks: '#9aa3cc'});   // same chart, same locked series colours

  await page.evaluate(() => nav(document.querySelector('[data-view="dashboard"]')));
  const ecgAfter = await spark();
  expect(ecgAfter.orange).toBe('rgb(189, 147, 249)');      // Total follows the palette accent
  expect(ecgAfter.orange).not.toBe(ecgBefore.orange);
  expect(ecgAfter.green).toBe(ecgBefore.green);              // Completed keeps its locked success colour

  await page.click('#theme-toggle');                          // the mode toggle still announces as before
  expect((await page.evaluate(() => window.__themeEvents)).at(-1)).toMatchObject({detail: {light: true}, palette: null, background: '#f4f7fc'});
});

test('the chevron sits in the strip right of the unchanged mode toggle, and is absent below 780px', async ({page}) => {
  await noFonts(page);
  // One 22px shell inset from 780px up (1.0.14, af5496ef; shell-inset-artwork).
  for (const [width, inset] of [[1152, 22], [1440, 22]]) {
    await page.setViewportSize({width, height: 1000});
    await page.goto('/');
    const box = await page.evaluate(() => Object.fromEntries(['#theme-toggle', '#theme-picker-toggle'].map(id => { const r = document.querySelector(id).getBoundingClientRect(); return [id, [r.x, r.y, r.width, r.height]]; })));
    expect(box['#theme-toggle']).toEqual([width - inset - 37, 33, 37, 37]);
    expect(box['#theme-picker-toggle']).toEqual([width - inset + 3, 33, 16, 37]);
    expect(await page.getAttribute('#theme-picker-toggle', 'aria-label')).toBe('Choose theme');
  }
  await page.setViewportSize({width: 779, height: 900});
  expect(await page.locator('#theme-picker-toggle').isVisible()).toBe(false);
  expect(await page.locator('#theme-toggle').isVisible()).toBe(true);
});
