const { test, expect } = require('@playwright/test');

/* DP 1.0.14 responsive Settings batch, read from the rendered geometry:
 *
 *  - the two bounded islands (Download Behavior & Limits, Automatic
 *    Extraction) hug what they hold once their settings no longer share a row,
 *    with equal room on both sides, and stay one row where it fits;
 *  - Usenet leads with Direct Write (2 x 2 two a row), Media Downloads with
 *    YouTube Player Client (2 x 3), Disk Space & Recovery with Stalled
 *    Download Timeout, every row centred;
 *  - Retry Delay says everything in about a third fewer words;
 *  - Automatic Extraction: Delete Archives first, and its header is the
 *    hinted header -- the hint on the header's own midpoint while it fits,
 *    otherwise its exact words carried by Help; Help then Enable at the
 *    trailing inset. */

const settle = page => page.evaluate(() => new Promise(resolve => {
  const still = () => !document.querySelector('.dp-motion-surface') && document.getAnimations()
    .every(animation => animation.playState !== 'running' || animation.effect?.getTiming().iterations === Infinity);
  const wait = () => (still() ? requestAnimationFrame(() => requestAnimationFrame(resolve)) : setTimeout(wait, 30));
  wait();
}));

async function open(page, tab, width) {
  await page.setViewportSize({width, height: 1000});
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]:not([data-title])').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator(`#view-settings [data-tab="${tab}"]`).first().click();
  await page.evaluate(() => document.fonts.ready);
  await settle(page);
}

async function resize(page, width) {
  await page.setViewportSize({width, height: 1000});
  await page.waitForFunction(() => document.getElementById('sidebar').getAnimations().every(a => a.playState !== 'running'));
  await settle(page);
}

/* A bounded island: its outline, the settings in it, and the room around it. */
const island = (page, selector) => page.locator(selector).evaluate(node => {
  const r = el => el.getBoundingClientRect();
  const box = r(node), body = r(node.parentElement), style = getComputedStyle(node);
  const fields = [...node.querySelectorAll('.dp-settings-inline-field')].map(r);
  const inset = parseFloat(style.paddingLeft) + parseFloat(style.borderLeftWidth);
  return {
    stacked: node.dataset.island === 'stacked',
    oneRow: fields.every(field => Math.abs(field.top - fields[0].top) < 3 || Math.abs(field.bottom - fields[0].bottom) < 3)
      && fields.every((field, index) => !index || field.left >= fields[index - 1].right),
    width: box.width, bodyWidth: body.width,
    inner: [Math.min(...fields.map(f => f.left)) - box.left, box.right - Math.max(...fields.map(f => f.right))],
    outer: [box.left - body.left, body.right - box.right],
    // How much wider the outline is than its widest setting and its own inset.
    slack: box.width - Math.max(...fields.map(f => f.width)) - 2 * inset,
    labels: [...node.querySelectorAll('.dp-settings-inline-field .form-label')].map(label => label.textContent.trim()),
  };
});

test('the two bounded islands hug their settings once those no longer share a row, and keep one row where they fit', async ({page}) => {
  await open(page, 'downloads', 1440);
  for (const [width, behaviour, extraction] of [[1440, 'row', 'row'], [1024, 'stacked', 'row'], [900, 'stacked', 'stacked'], [780, 'stacked', 'stacked']]) {
    await resize(page, width);
    await page.locator('#view-settings [data-tab="downloads"]').first().click();
    await settle(page);
    const behaviourIsland = await island(page, '.dp-settings-download-engine-row');
    await page.locator('#view-settings [data-tab="extraction"]').first().click();
    await settle(page);
    const extractionIsland = await island(page, '.dp-settings-extraction-behavior');
    for (const [name, measured, expected] of [['Download Behavior & Limits', behaviourIsland, behaviour], ['Automatic Extraction', extractionIsland, extraction]]) {
      const at = `${name} at ${width}px`;
      expect(measured.stacked ? 'stacked' : 'row', at).toBe(expected);
      if (expected === 'row') expect(measured.oneRow, `${at}: not one row`).toBe(true);
      else expect(measured.slack, `${at}: the outline is wider than its widest setting`).toBeLessThanOrEqual(1);
      expect(Math.abs(measured.inner[0] - measured.inner[1]), `${at}: unequal inner sides ${measured.inner}`).toBeLessThanOrEqual(1);
      expect(Math.abs(measured.outer[0] - measured.outer[1]), `${at}: not centred ${measured.outer}`).toBeLessThanOrEqual(1);
      expect(measured.width, `${at}: wider than its card`).toBeLessThanOrEqual(measured.bodyWidth + 0.5);
    }
    expect(extractionIsland.labels, `Automatic Extraction order at ${width}px`).toEqual(['Delete Archives After Extraction', 'Concurrent Extractions']);
  }
  // Nothing is cut: the folder path stays a readable field at the narrowest width.
  await page.locator('#view-settings [data-tab="downloads"]').first().click();
  await settle(page);
  expect(await page.locator('.dp-settings-download-engine-row .dp-settings-directory-field-control > .input')
    .evaluate(node => node.getBoundingClientRect().width)).toBeGreaterThan(200);
});

/* The rendered rows of a tuning set, each row's labels and its centring. */
const tuningRows = (page, selector) => page.locator(selector).evaluate(card => {
  const grid = card.querySelector('.dp-settings-tuning-grid');
  const g = grid.getBoundingClientRect();
  const rows = [];
  for (const cell of grid.querySelectorAll('.dp-settings-field')) {
    const b = cell.getBoundingClientRect();
    let row = rows.find(r => Math.abs(r.top - b.top) < 3);
    if (!row) rows.push(row = {top: b.top, left: b.left, right: b.right, labels: []});
    row.left = Math.min(row.left, b.left);
    row.right = Math.max(row.right, b.right);
    row.labels.push(cell.querySelector('.form-label').textContent.trim());
  }
  return rows.map(r => ({labels: r.labels, offset: (r.left - g.left) - (g.right - r.right)}));
});

test('the locked tunable orders give balanced rows, every row centred', async ({page}) => {
  await open(page, 'downloads', 780);
  for (const id of ['usenet', 'media']) await page.locator(`[data-executor-tuning="${id}"] .dp-settings-disclosure`).first().click();
  await settle(page);
  const usenet = '[data-executor-tuning="usenet"]', media = '[data-executor-tuning="media"]', recovery = '.dp-settings-download-recovery-card';
  const DW = 'Direct Write', AC = 'Article Cache Limit (MB)', MR = 'Maximum Retries', RT = 'Request Timeout (seconds)';
  const YC = 'YouTube Player Client', TR = 'Target Resolution', VQ = 'Video Quality', VC = 'Preferred Video Codec',
    SL = 'Preferred Subtitle Language', CF = 'Concurrent Fragment Downloads';
  const ST = 'Stalled Download Timeout (hours)', MF = 'Minimum Free Disk Space (GB)', RB = 'Resume Free Space Buffer (GB)',
    ER = 'Download Error Retries', RD = 'Retry Delay (seconds)';
  const expected = {
    780: {[usenet]: [[DW, AC], [MR, RT]], [media]: [[YC, TR], [VQ, VC], [SL, CF]], [recovery]: [[ST, MF, RB], [ER, RD]]},
    900: {[usenet]: [[DW, AC], [MR, RT]], [media]: [[YC, TR], [VQ, VC], [SL, CF]], [recovery]: [[ST, MF, RB], [ER, RD]]},
    1440: {[usenet]: [[DW, AC, MR, RT]], [recovery]: [[ST, MF, RB, ER, RD]]},
  };
  for (const [width, sets] of Object.entries(expected)) {
    await resize(page, Number(width));
    for (const [selector, rows] of Object.entries(sets)) {
      const drawn = await tuningRows(page, selector);
      expect(drawn.map(row => row.labels), `${selector} at ${width}px`).toEqual(rows);
      for (const row of drawn) expect(Math.abs(row.offset), `${selector} row ${row.labels} at ${width}px off centre`).toBeLessThanOrEqual(1.5);
    }
  }
  // Values and controls are those of the same settings: nothing was rebound.
  for (const [key, type] of [['usenet_direct_write', 'checkbox'], ['usenet_article_cache_megabytes', 'number'],
    ['stuck_download_timeout_hours', 'number'], ['aria2_error_retry_delay_seconds', 'number']]) {
    await expect(page.locator(`[data-setting="${key}"]`)).toHaveAttribute('type', type);
  }
  await expect(page.locator('[data-setting="media_youtube_player_clients"]')).toHaveCount(1);
});

test('Retry Delay states every fact in about a third fewer words, and its row is shorter', async ({page}) => {
  const OLD = 265;
  await open(page, 'downloads', 900);
  const hint = page.locator('#dp-settings-field-aria2-error-retry-delay-seconds').locator('xpath=ancestor::div[contains(@class,"dp-settings-field")][1]').locator('.form-hint');
  await expect(hint).toHaveText('Wait before checking or re-resolving a download after an error, or retrying a temporarily failed provider. Plain retries double the delay, up to 5 minutes. Set to 0 to retry immediately.');
  const text = await hint.textContent();
  expect(text.length).toBeLessThanOrEqual(Math.round(OLD * 0.72));
  // One description, no second accessible copy of it.
  await expect(page.getByText('Plain retries double the delay', {exact: false})).toHaveCount(1);
  // At this width the row holding it is no taller than its peers by more than a line or two.
  const heights = await page.locator('.dp-settings-download-recovery-card .dp-settings-tuning-grid .dp-settings-field')
    .evaluateAll(cells => cells.map(cell => cell.getBoundingClientRect().height));
  expect(Math.max(...heights) - Math.min(...heights)).toBeLessThanOrEqual(30);
});

/* The Automatic Extraction header as drawn. */
const extractionHeader = page => page.evaluate(() => {
  const h = document.querySelector('#view-settings .dp-settings-extraction-card > .card-header');
  const st = getComputedStyle(h), hb = h.getBoundingClientRect();
  const left = hb.left + h.clientLeft + parseFloat(st.paddingLeft), right = hb.left + h.clientLeft + h.clientWidth - parseFloat(st.paddingRight);
  const title = h.querySelector('.dp-settings-card-title-group').getBoundingClientRect();
  const rail = h.querySelector(':scope > .dp-settings-card-header-controls');
  const r = rail.getBoundingClientRect();
  const hint = h.querySelector(':scope > .dp-settings-card-header-center > :only-child');
  const shown = hint.getClientRects().length > 0, c = shown ? hint.getBoundingClientRect() : null;
  const chip = rail.querySelector('.dp-settings-help-chip');
  const mid = (title.top + title.bottom) / 2;
  return {height: hb.height, minHeight: parseFloat(st.minHeight), hint: hint.textContent.trim(), shown,
    oneRow: [r, ...[...rail.children].map(n => n.getBoundingClientRect())].every(b => Math.abs((b.top + b.bottom) / 2 - mid) < 2),
    lines: shown ? Math.round(c.height / parseFloat(getComputedStyle(hint).lineHeight)) : 0,
    offset: shown ? (c.left + c.right) / 2 - (left + right) / 2 : null,
    clear: shown ? [c.left - title.right, r.left - c.right] : null, gap: parseFloat(st.columnGap),
    titleInset: title.left - left, railInset: right - r.right,
    rail: [...rail.children].map(n => n.matches('.dp-settings-help-chip') ? 'help' : n.matches('.dp-settings-extraction-enable') ? 'enable' : n.className),
    chips: h.querySelectorAll('.dp-settings-help-chip').length, topic: chip.dataset.settingsHelp, tooltip: chip.dataset.tooltip, label: chip.getAttribute('aria-label')};
});

test('Automatic Extraction takes the hinted header: centred hint while it fits, else its exact words on Help; Help then Enable at the trailing inset', async ({page}) => {
  const HINT = 'Automatically extract supported archives after a download completes.';
  await open(page, 'extraction', 1440);
  const seen = new Set();
  for (const width of [1440, 1100, 1080, 1060, 1040, 1020, 1000, 980, 960, 900, 780, 1440]) {
    await resize(page, width);
    const h = await extractionHeader(page);
    const at = `${width}px`;
    expect(h.rail, at).toEqual(['help', 'enable']);
    expect([h.chips, h.topic, h.label], at).toEqual([1, 'automatic-extraction', 'Help: Automatic Extraction']);
    expect(Math.abs(h.titleInset), `${at}: identity off its inset`).toBeLessThanOrEqual(0.5);
    expect(Math.abs(h.railInset), `${at}: rail off its inset`).toBeLessThanOrEqual(0.5);
    expect(h.oneRow && Math.abs(h.height - h.minHeight) <= 0.5, `${at}: not one row (${h.height})`).toBe(true);
    expect(h.hint, at).toBe(HINT);
    if (h.shown) {
      expect(h.lines, `${at}: hint not one line`).toBe(1);
      expect(Math.abs(h.offset), `${at}: hint off the header's midpoint`).toBeLessThanOrEqual(0.5);
      expect(Math.min(...h.clear), `${at}: hint not clear of identity and rail`).toBeGreaterThanOrEqual(h.gap - 0.5);
      expect(h.tooltip, at).toBe('Help: Automatic Extraction');
    } else {
      expect(h.tooltip, `${at}: the hidden hint is not carried by Help`).toBe(HINT);
    }
    seen.add(h.shown);
  }
  expect([...seen].sort(), 'the sweep never crossed the fit threshold').toEqual([false, true]);
  // Handheld keeps its header, the hint carried by Help.
  await resize(page, 700);
  const handheld = await extractionHeader(page);
  expect([handheld.shown, handheld.tooltip, handheld.rail]).toEqual([false, HINT, ['help', 'enable']]);
  await resize(page, 900);
  // Enable is still its own label-associated control in the rail, and Help opens the full topic.
  const enable = page.locator('.dp-settings-extraction-card > .card-header .dp-settings-extraction-enable');
  await expect(enable).toHaveAttribute('for', 'dp-settings-field-extract-enabled');
  expect(await enable.evaluate(node => { const r = node.querySelector('.ttrack').getBoundingClientRect();
    return node.contains(document.elementFromPoint((r.left + r.right) / 2, (r.top + r.bottom) / 2)); })).toBe(true);
  await page.locator('.dp-settings-extraction-card > .card-header .dp-settings-help-chip').click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('Automatic Extraction');
  await expect(dialog).toContainText('Delete Archives After Extraction removes');
});
