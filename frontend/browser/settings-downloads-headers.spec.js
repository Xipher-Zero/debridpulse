const { test, expect } = require('@playwright/test');

/* Settings -> Downloads: the nine card headers -- Download Behavior & Limits,
 * Transfer Method Settings and its five method cards (Network Sources, rsync,
 * WebDAV, Usenet, Media Downloads), Disk Space & Recovery and Download Engine
 * Activity -- in the Services header grammar. One row at every width from the
 * desktop down to 780px: the identity at the leading inset (a method card's
 * disclosure beside its title), the rail at the trailing inset (each card's
 * Help -- Download Engine Activity updates by itself), and the card's one-line purpose
 * hint on the header's own midpoint only while it fits there clear of both;
 * otherwise the hint is out of the layout and the card's Help chip says it
 * instead. Every card's Help opens its own topic.
 *
 * Everything is read from the rendered geometry, the DP tooltip and the open
 * dialog -- never from the page's own decision. */

const HEADERS = ['Download Behavior & Limits', 'Transfer Method Settings', 'Network Sources', 'rsync', 'WebDAV', 'Usenet',
  'Media Downloads', 'Disk Space & Recovery', 'Download Engine Activity'];
const METHODS = {'Network Sources': 'tuning-direct', 'rsync': 'tuning-rsync', 'WebDAV': 'tuning-webdav', 'Usenet': 'tuning-usenet',
  'Media Downloads': 'tuning-media'};
const TOPICS = {'Download Behavior & Limits': 'download-behavior', 'Transfer Method Settings': 'transfer-methods',
  'Disk Space & Recovery': 'disk-recovery', 'Download Engine Activity': 'engine-activity', ...METHODS};
const HELP = name => `Help: ${name}${METHODS[name] ? ' Tuning' : ''}`;
const DIALOG = name => (METHODS[name] ? `${name} Tuning` : name);

async function open(page, width, {fallbackFonts = false} = {}) {
  if (fallbackFonts) await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.setViewportSize({width, height: 1000});
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]:not([data-title])').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator('#view-settings [data-tab="downloads"]').first().click();
  await expect(page.locator('#view-settings [data-panel="downloads"] .dp-executor-work-card')).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await frames(page);
}

// Two frames drawn after every slide (the view's and the panel's own included)
// and finite animation have finished: the geometry and hit-testing are the page's.
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

/* The nine headers as drawn. */
function headers(page) {
  return page.evaluate(() => {
    const panel = document.querySelector('#view-settings [data-panel="downloads"]');
    return [...panel.querySelectorAll('.card > .card-header')].map(h => {
      const st = getComputedStyle(h), hb = h.getBoundingClientRect();
      const left = hb.left + h.clientLeft + parseFloat(st.paddingLeft), right = hb.left + h.clientLeft + h.clientWidth - parseFloat(st.paddingRight);
      const group = h.querySelector(':scope > .dp-settings-card-title-group');
      const title = (group || h).querySelector('.card-title').getBoundingClientRect();
      const identity = group ? group.getBoundingClientRect() : title;
      const rail = h.querySelector(':scope > .dp-settings-card-header-controls');
      const r = rail?.getBoundingClientRect();
      const hint = h.querySelector(':scope > .dp-settings-card-header-center > :only-child');
      const shown = !!hint && hint.getClientRects().length > 0;
      const c = shown ? hint.getBoundingClientRect() : null;
      const disclosure = group?.querySelector(':scope > .dp-settings-disclosure');
      const d = disclosure?.getBoundingClientRect();
      const chips = [...h.querySelectorAll('.dp-settings-help-chip')];
      const mid = (title.top + title.bottom) / 2;
      const items = rail ? [...rail.children].map(node => node.getBoundingClientRect()) : [];
      return {name: h.querySelector('.dp-settings-card-title-text').textContent.trim(), height: hb.height, minHeight: parseFloat(st.minHeight),
        gap: parseFloat(st.columnGap) || 0, hinted: h.classList.contains('dp-settings-card-header--hinted'),
        titleInset: identity.left - left, railInset: r ? right - r.right : null,
        oneRow: !!rail && [r, ...items].every(item => Math.abs((item.top + item.bottom) / 2 - mid) < 2),
        hint: hint ? hint.textContent.trim() : null, shown,
        lines: shown ? Math.round(c.height / parseFloat(getComputedStyle(hint).lineHeight)) : 0,
        hintRow: shown ? Math.abs((c.top + c.bottom) / 2 - mid) < 2 : null,
        offset: shown ? (c.left + c.right) / 2 - (left + right) / 2 : null,
        clearLeft: shown ? c.left - identity.right : null, clearRight: shown ? r.left - c.right : null,
        disclosure: disclosure ? {afterTitle: d.left >= title.right - 0.5 && d.left - title.right <= 12, row: Math.abs((d.top + d.bottom) / 2 - mid) < 2} : null,
        chips: chips.length, topic: chips[0]?.dataset.settingsHelp, label: chips[0]?.getAttribute('aria-label'), tooltip: chips[0]?.getAttribute('data-tooltip'),
        order: rail ? [...rail.children].map(node => node.classList.contains('dp-settings-help-chip') ? 'help'
          : node.className) : []};
    });
  });
}

/* The header contract at this width; returns the problems. Desktop/tablet
 * (>= 780px): one row. Handheld (< 780px): the established handheld header --
 * identity and rail on the first row, the hint carried by Help. */
function problems(list, width) {
  const out = [];
  if (list.map(h => h.name).join() !== HEADERS.join()) out.push(`headers ${list.map(h => h.name)}`);
  for (const h of list) {
    const at = `${h.name} at ${width}px`;
    if (!h.hinted) out.push(`${at}: not the hinted header grammar`);
    if (width >= 780 && (Math.abs(h.height - h.minHeight) > 0.5 || !h.oneRow)) out.push(`${at}: not one row (${h.height}px)`);
    if (Math.abs(h.titleInset) > 0.5) out.push(`${at}: identity off its inset (${h.titleInset.toFixed(1)})`);
    if (h.railInset === null || Math.abs(h.railInset) > 0.5) out.push(`${at}: rail off the trailing inset (${h.railInset})`);
    if (h.chips !== 1) out.push(`${at}: ${h.chips} help chips`);
    if (h.topic !== TOPICS[h.name] || h.label !== HELP(h.name)) out.push(`${at}: help ${h.topic} / ${h.label}`);
    if (h.order.join() !== 'help') out.push(`${at}: rail ${h.order}`);
    if (METHODS[h.name] && !(h.disclosure?.afterTitle && h.disclosure?.row)) out.push(`${at}: disclosure not beside its title`);
    if (!h.hint) out.push(`${at}: no hint`);
    if (h.shown) {
      if (width < 780) out.push(`${at}: hint shown on the handheld header`);
      if (h.lines !== 1 || !h.hintRow) out.push(`${at}: hint not one line on the header's row`);
      if (Math.abs(h.offset) > 0.5) out.push(`${at}: hint off the midpoint (${h.offset.toFixed(1)})`);
      if (h.clearLeft < h.gap - 0.5 || h.clearRight < h.gap - 0.5) out.push(`${at}: hint touches (${h.clearLeft.toFixed(1)}, ${h.clearRight.toFixed(1)})`);
      if (h.tooltip !== h.label) out.push(`${at}: help tooltip "${h.tooltip}" while the hint shows`);
    } else if (h.tooltip !== h.hint) out.push(`${at}: help tooltip "${h.tooltip}", hidden hint "${h.hint}"`);
  }
  return out;
}

for (const fonts of ['web fonts', 'fallback fonts']) {
  test(`with ${fonts}, every Downloads header is one row with its identity and rail at their insets, the hint centred while it fits and otherwise carried by Help`, async ({page}) => {
    await open(page, 1920, {fallbackFonts: fonts === 'fallback fonts'});
    const report = {};
    const states = [];
    for (const width of [1920, 1560, 1440, 1300, 1181, 1180, 1100, 1000, 901, 900, 850, 800, 780, 779, 600]) {
      await resize(page, width);
      const list = await headers(page);
      const found = problems(list, width);
      if (found.length) report[width] = found;
      states.push(`${width}: ${list.filter(h => h.shown).length}/9 shown`);
      if (width === 1920) expect(list.every(h => h.shown), 'every hint shows on the wide desktop').toBe(true);
    }
    expect(report).toEqual({});
    test.info().annotations.push({type: `hints (${fonts})`, description: states.join('; ')});
  });
}

test('across one hint\'s fit both ways, it hides before it would touch, comes back after, and the Help tooltip follows with no stale text', async ({page}) => {
  await open(page, 1300);
  const name = 'Disk Space & Recovery';
  const card = async () => (await headers(page)).find(h => h.name === name);
  // The widest width at which its hint no longer fits, from the geometry itself.
  let width = 1300;
  while ((await card()).shown) { width -= 4; await resize(page, width); expect(width).toBeGreaterThan(780); }
  while (!(await card()).shown) { width += 1; await resize(page, width); }
  const edge = width;   // the first width, coming up, at which it fits
  for (const [w, shown] of [[edge, true], [edge - 1, false], [edge - 2, false], [edge, true], [edge + 1, true], [edge - 1, false]]) {
    await resize(page, w);
    const list = await headers(page);
    expect(problems(list, w), `${w}px`).toEqual([]);
    const h = list.find(entry => entry.name === name);
    expect(h.shown, `${w}px`).toBe(shown);
    expect(h.tooltip, `${w}px`).toBe(shown ? `Help: ${name}` : h.hint);
    expect(h.height, `${w}px: the hint leaves no row behind`).toBe(h.minHeight);
    if (shown) expect(Math.min(h.clearLeft, h.clearRight), `${w}px`).toBeGreaterThanOrEqual(h.gap - 0.5);
  }
  test.info().annotations.push({type: `${name} hint`, description: `fits from ${edge}px (web fonts)`});
});

test('a Downloads hint that stops fitting when fonts finish loading is handed to Help, and comes back when it fits again', async ({page}) => {
  await open(page, 1300);
  expect((await headers(page)).every(h => h.shown)).toBe(true);
  const wide = '#view-settings [data-panel="downloads"] .dp-settings-card-header-center > * { letter-spacing: 3px; }';
  await page.addStyleTag({content: wide});
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

test('the Help chip shows a hidden hint as its DP tooltip on hover and keyboard focus, opens its own topic, and a method card\'s disclosure still works beside its title', async ({page}) => {
  await open(page, 780);
  const card = page.locator('#view-settings [data-executor-tuning="media"]');
  const header = card.locator(':scope > .card-header');
  const chip = header.locator('.dp-settings-help-chip');
  const hint = (await header.locator('.dp-settings-card-header-center > *').textContent()).trim();
  expect((await headers(page)).find(h => h.name === 'Media Downloads').shown).toBe(false);
  const tip = page.locator('#dp-tooltip.is-shown');
  await chip.hover();
  await expect(tip).toHaveText(hint);
  await page.mouse.move(2, 990);
  await expect(tip).toHaveCount(0);
  const disclosure = header.locator('.dp-settings-disclosure');
  await disclosure.focus();
  await page.keyboard.press('Enter');
  await expect(disclosure).toHaveAttribute('aria-expanded', 'true');
  await expect(card.locator(':scope > .card-body')).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(chip).toBeFocused();
  await expect(tip).toHaveText(hint);
  await expect(chip).toHaveAccessibleName('Help: Media Downloads Tuning');
  await page.keyboard.press('Enter');
  const dialog = page.locator('.dp-modal-dialog.dp-settings-help-dialog');
  await expect(dialog.locator('.dp-modal-title')).toHaveText('Media Downloads Tuning');
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(chip).toBeFocused();
  await disclosure.click();
  await expect(disclosure).toHaveAttribute('aria-expanded', 'false');
  await expect(card.locator(':scope > .card-body')).toBeHidden();

  await resize(page, 1920);
  await page.mouse.move(2, 990);
  await chip.hover();
  await expect(tip).toHaveText('Help: Media Downloads Tuning');
  await expect(header.locator('.dp-settings-card-header-center > *')).toBeVisible();
});

test('Download Engine Activity keeps its Help alone at the trailing edge, and reads the engine jobs without being asked', async ({page}) => {
  const request = page.waitForRequest(r => new URL(r.url()).pathname === '/api/executor-work');
  await open(page, 780);
  await request;
  const header = page.locator('#view-settings .dp-executor-work-card > .card-header');
  const rail = header.locator(':scope > .dp-settings-card-header-controls');
  await expect(rail.locator(':scope > *')).toHaveCount(1);
  await expect(rail.locator(':scope > :first-child')).toHaveAttribute('data-settings-help', 'engine-activity');
  await expect(header.getByRole('button', {name: 'Refresh'})).toHaveCount(0);
});

test('at 780px every header control is whole and is its own target: the disclosures and every Help', async ({page}) => {
  await open(page, 780);
  const report = await page.evaluate(() => {
    const out = [];
    for (const header of document.querySelectorAll('#view-settings [data-panel="downloads"] .card > .card-header')) {
      const name = header.querySelector('.dp-settings-card-title-text').textContent.trim();
      header.scrollIntoView({block: 'center'});
      const hb = header.getBoundingClientRect();
      for (const node of header.querySelectorAll('.dp-settings-disclosure, .dp-settings-help-chip')) {
        const r = node.getBoundingClientRect();
        if (r.left < hb.left || r.right > hb.right || r.width < 24 || r.height < 24) out.push(`${name}: ${node.className} ${r.width}x${r.height}`);
        const hit = document.elementFromPoint((r.left + r.right) / 2, (r.top + r.bottom) / 2);
        if (!node.contains(hit)) out.push(`${name}: ${node.className} covered by ${hit?.className}`);
      }
    }
    return out;
  });
  expect(report).toEqual([]);
});

/* Every control a method card renders, named by its own label. */
const controls = (page, id) => page.evaluate(id => [...document.querySelectorAll(`#view-settings [data-executor-tuning="${id}"] [data-setting]`)]
  .map(node => ({key: node.dataset.setting, label: (node.labels?.[0] || document.querySelector(`label[for="${node.id}"]`))?.textContent.trim()
    || node.closest('.dp-settings-field, .dp-settings-inline-field')?.querySelector('.form-label, .tl')?.textContent.trim(),
    options: node.tagName === 'SELECT' ? [...node.options].map(option => option.value) : null})), id);

test('each of the nine Help chips opens its own read-only topic, and each method topic names every control its card renders', async ({page}) => {
  await open(page, 1920);
  const bodies = {};
  for (const name of HEADERS) {
    const chip = page.locator(`#view-settings [data-panel="downloads"] [data-settings-help="${TOPICS[name]}"]`);
    await expect(chip).toHaveCount(1);
    await chip.click();
    const dialog = page.locator('.dp-modal-dialog.dp-settings-help-dialog');
    await expect(dialog.locator('.dp-modal-title')).toHaveText(DIALOG(name));
    bodies[name] = (await dialog.locator('.dp-settings-help-content').innerText()).trim();
    expect(bodies[name].length, name).toBeGreaterThan(200);
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(chip).toBeFocused();
  }
  expect(new Set(Object.keys(METHODS).map(name => bodies[name])).size).toBe(5);
  const ids = {'Network Sources': 'direct', 'rsync': 'rsync', 'WebDAV': 'webdav', 'Usenet': 'usenet', 'Media Downloads': 'media'};
  for (const [name, id] of Object.entries(ids)) {
    const rendered = await controls(page, id);
    expect(rendered.length, name).toBeGreaterThan(2);
    // The label as the card shows it, less its unit ("(seconds)", "(MB)").
    for (const control of rendered) {
      const label = control.label.replace(/ \((seconds|MB)\)$/, '');
      expect(bodies[name], `${name} describes ${label} (${control.key})`).toContain(label);
    }
  }
  // Media Downloads: exactly the six rendered controls; the codec is a
  // preference among what the site offers; no container choice, no conversion.
  const media = await controls(page, 'media');
  expect(media.map(control => control.key).sort()).toEqual(['media_concurrent_fragment_downloads', 'media_target_resolution',
    'media_video_codec', 'media_video_quality', 'media_youtube_player_clients', 'preferred_subtitle_language']);
  expect(media.find(control => control.key === 'media_video_codec').options).toEqual(['auto', 'av1', 'hevc', 'h264']);
  for (const control of media) for (const value of control.options || []) expect(['mkv', 'mp4', 'webm']).not.toContain(value);
  for (const phrase of ['never re-encodes or transcodes', 'There is no setting for the output container', 'never a conversion',
    'among the bitrates the site offers', 'cannot make a video better than the site offers']) expect(bodies['Media Downloads']).toContain(phrase);
});
