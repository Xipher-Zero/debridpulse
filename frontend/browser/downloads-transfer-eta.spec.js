const { test, expect } = require('@playwright/test');

// DP 1.0.14: a downloading transfer's estimated time remaining, beneath its
// primary bar in Downloads. The browser renders the backend's one estimate
// (``acquisition_eta``) -- HH:MM:SS, held, or "—" -- and never counts it down
// on its own. Mocked rows: no backend state is read or written.

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

function observeRuntime(page) {
  const errors = [];
  page.on('pageerror', error => errors.push(`pageerror: ${error.message}`));
  page.on('console', message => { if (message.type() === 'error') errors.push(`console: ${message.text()}`); });
  return errors;
}

function item(id, eta, {status = 'downloading', progress = 40} = {}) {
  return {
    id, name: `Estimate ${id}`, status, presentation_status: status,
    presentation_label: status, presentation_badge_status: status, attention_required: false,
    progress, active_execution_progress: null, active_execution_basis: null, size_bytes: 1000, source: 'manual',
    hash: '', label: '', created_at: '2026-10-23T00:00:00Z', current_source_identity: {kind: 'link'},
    providers: [], historical_providers: [], delivering_provider_ids: [], input_required: null,
    acquisition_eta: eta,
  };
}

let served = [];

async function downloads(page, items) {
  served = items;
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({items: served, total: served.length}),
  }));
  await page.goto('/');
  await page.evaluate(async () => {
    nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]'));
    await loadTorrents();
  });
}

function cell(page, id) {
  return page.locator(`#t-tbody tr[data-torrent-id="${id}"] [data-role="transfer-progress"]`);
}

test('the estimate renders as HH:MM:SS, held, or unavailable, and only while downloading', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  await downloads(page, [
    item(9801, {seconds: 3725, held: false}),
    item(9802, {seconds: 3725, held: true}),
    item(9803, null),
    item(9804, {seconds: 100 * 3600 + 65, held: false}),          // 100 h never wraps at 24
    item(9805, {seconds: 9999 * 3600 + 59 * 60 + 59, held: false}), // the ceiling
    item(9806, {seconds: 9999 * 3600 + 59 * 60 + 60, held: false}), // past it
    item(9807, {seconds: -5, held: false}),
    item(9808, {seconds: 12.5, held: false}),
    item(9809, {seconds: 30, held: false}, {status: 'queued'}),
    item(9810, {seconds: 30, held: false}, {status: 'paused'}),
  ]);
  const eta = id => cell(page, id).locator('[data-role="transfer-eta"]');
  await expect(eta(9801)).toHaveText('01:02:05');
  await expect(eta(9801)).toHaveAttribute('aria-label', 'Estimated time remaining 01:02:05');
  await expect(eta(9801)).not.toHaveClass(/is-held/);
  await expect(eta(9802)).toHaveText('01:02:05');
  await expect(eta(9802)).toHaveClass(/is-held/);
  await expect(eta(9802)).toHaveAttribute('aria-label', 'Estimated time remaining 01:02:05, held');
  await expect(eta(9803)).toHaveText('—');
  await expect(eta(9804)).toHaveText('100:01:05');
  await expect(eta(9805)).toHaveText('9999:59:59');
  for (const id of [9806, 9807, 9808]) await expect(eta(id)).toHaveText('—');
  for (const id of [9809, 9810]) await expect(eta(id)).toHaveCount(0);
  // The primary percentage label is unchanged beside it.
  await expect(cell(page, 9801).locator('.prog-pct')).toHaveText('40%');
  expect(errors).toEqual([]);
});

test('the estimate sits right-aligned beneath the bar without moving the row', async ({ page }) => {
  await isolateExternalFonts(page);
  await downloads(page, [item(9821, {seconds: 9999 * 3600 + 59 * 60 + 59, held: false})]);
  const measure = () => page.evaluate(() => {
    const cell = document.querySelector('#t-tbody tr[data-torrent-id="9821"] [data-role="transfer-progress"]');
    const box = element => { const r = element.getBoundingClientRect(); return {left: r.left, right: r.right, top: r.top, bottom: r.bottom}; };
    const eta = cell.querySelector('[data-role="transfer-eta"]');
    const result = {bar: box(cell.querySelector('.prog')), pct: box(cell.querySelector('.prog-pct')),
                    eta: eta && box(eta), cellHeight: cell.getBoundingClientRect().height,
                    overflow: cell.scrollWidth > cell.clientWidth,
                    whiteSpace: eta && getComputedStyle(eta).whiteSpace,
                    numeric: eta && getComputedStyle(eta).fontVariantNumeric};
    return result;
  });
  // Every Downloads layout that shows the progress column (hidden at 901-1050 px and at 700 px and below).
  for (const width of [1440, 1100, 900, 701]) {
    await page.setViewportSize({width, height: 900});
    const withEta = await measure();
    expect(Math.abs(withEta.eta.right - withEta.bar.right)).toBeLessThanOrEqual(1.5);   // right-aligned to the bar
    expect(withEta.eta.top).toBeGreaterThanOrEqual(withEta.bar.bottom - 0.5);          // beneath it
    expect(withEta.eta.top, `width ${width}`).toBeLessThan(withEta.pct.bottom);       // on the label's line
    // The block label spans the line, so overlap is judged on its text.
    const textOverlap = await page.evaluate(() => {
      const cell = document.querySelector('#t-tbody tr[data-torrent-id="9821"] [data-role="transfer-progress"]');
      const range = document.createRange();
      range.selectNodeContents(cell.querySelector('.prog-pct'));
      const text = range.getBoundingClientRect(), eta = cell.querySelector('[data-role="transfer-eta"]').getBoundingClientRect();
      return !(eta.left >= text.right || eta.right <= text.left || eta.top >= text.bottom || eta.bottom <= text.top);
    });
    expect(textOverlap, `width ${width}`).toBe(false);
    expect(withEta.overflow, `width ${width}`).toBe(false);
    expect(withEta.whiteSpace).toBe('nowrap');
    expect(withEta.numeric).toContain('tabular-nums');
    // A changing value never moves the row: "—" and every estimate under 100 h
    // take the reserved width.
    const heights = await page.evaluate(values => {
      const cell = document.querySelector('#t-tbody tr[data-torrent-id="9821"] [data-role="transfer-progress"]');
      const html = cell.innerHTML;
      const height = eta => { cell.innerHTML = progress(40, 'downloading', null, null, eta); return cell.getBoundingClientRect().height; };
      const result = {without: (cell.innerHTML = progress(40, 'downloading', null, null), cell.getBoundingClientRect().height),
                      values: values.map(height)};
      cell.innerHTML = html;
      return result;
    }, [null, {seconds: 5, held: false}, {seconds: 99 * 3600 + 59 * 60 + 59, held: true}]);
    expect(new Set(heights.values).size, `width ${width}`).toBe(1);
    // Where the column has room, the row is exactly as tall as without an estimate.
    if (width === 1440) expect(Math.abs(heights.values[0] - heights.without)).toBeLessThanOrEqual(0.5);
  }
  // Where the layout hides the progress column, the estimate goes with its bar.
  for (const width of [1000, 390]) {
    await page.setViewportSize({width, height: 900});
    await expect(cell(page, 9821)).toBeHidden();
    await expect(cell(page, 9821).locator('[data-role="transfer-eta"]')).toBeHidden();
  }
});

test('a live progress update and a full refresh show the same estimate', async ({ page }) => {
  await isolateExternalFonts(page);
  await downloads(page, [item(9831, {seconds: 90, held: false})]);
  const eta = cell(page, 9831).locator('[data-role="transfer-eta"]');
  await expect(eta).toHaveText('00:01:30');
  const patch = update => page.evaluate(update => patchProgressOnlyTransferEvent(
    {progress_only: true, items: [{id: 9831, status: 'downloading', progress: 45, active_execution_progress: null,
                                   active_execution_basis: null, status_changed: false, ...update}]}), update);
  expect(await patch({acquisition_eta: {seconds: 85, held: false}})).toBe(true);
  await expect(eta).toHaveText('00:01:25');
  const live = await cell(page, 9831).innerHTML();
  served[0] = {...served[0], progress: 45, acquisition_eta: {seconds: 85, held: false}};
  await page.evaluate(() => loadTorrents());
  expect(await cell(page, 9831).innerHTML()).toBe(live);           // one renderer, one truth
  await patch({acquisition_eta: {seconds: 85, held: true}});
  await expect(eta).toHaveText('00:01:25');
  await expect(eta).toHaveClass(/is-held/);
  await patch({acquisition_eta: null});                             // past the hold: none
  await expect(eta).toHaveText('—');
  // Nothing counts it down between updates.
  await patch({acquisition_eta: {seconds: 60, held: false}});
  await page.waitForTimeout(2200);
  await expect(eta).toHaveText('00:01:00');
  // A status change is never patched: the authoritative refresh decides.
  expect(await patch({status_changed: true, status: 'paused'})).toBe(false);
});

test('the estimate stays legible in both themes', async ({ page }) => {
  await isolateExternalFonts(page);
  await downloads(page, [item(9841, {seconds: 754, held: false}), item(9842, {seconds: 754, held: true})]);
  const contrast = () => page.evaluate(() => {
    const parse = value => (value.match(/[\d.]+/g) || []).map(Number);
    const luminance = ([r, g, b]) => [r, g, b].map(channel => {
      const c = channel / 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    }).reduce((sum, c, index) => sum + c * [0.2126, 0.7152, 0.0722][index], 0);
    const background = element => {
      for (let node = element; node; node = node.parentElement) {
        const [r, g, b, a = 1] = parse(getComputedStyle(node).backgroundColor);
        if (a >= 0.99) return [r, g, b];
      }
      return parse(getComputedStyle(document.body).backgroundColor);
    };
    return [...document.querySelectorAll('#t-tbody [data-role="transfer-eta"]')].map(element => {
      const fg = luminance(parse(getComputedStyle(element).color)), bg = luminance(background(element));
      return (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05);
    });
  });
  for (const theme of ['dark', 'light']) {
    if (theme === 'light') await page.evaluate(() => { document.body.classList.add('light'); });
    const ratios = await contrast();
    expect(ratios, theme).toHaveLength(2);                          // the measured and the held estimate
    for (const ratio of ratios) expect(ratio, theme).toBeGreaterThanOrEqual(4.5);
  }
});

test('a live update never adds an estimate to the Dashboard, which shows none', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  await downloads(page, [item(9851, {seconds: 90, held: false})]);
  await page.evaluate(async () => {
    nav(document.querySelector('#sidebar .nav-item[data-view="dashboard"]'));
    await loadTorrents();
  });
  const dash = page.locator('#dash-tbody tr[data-torrent-id="9851"]');
  await expect(dash).toHaveCount(1);
  await expect(dash.locator('[data-role="transfer-eta"]')).toHaveCount(0);
  await page.evaluate(() => patchProgressOnlyTransferEvent({progress_only: true, items: [{
    id: 9851, status: 'downloading', progress: 45, active_execution_progress: null, active_execution_basis: null,
    status_changed: false, acquisition_eta: {seconds: 85, held: false}}]}));
  await expect(dash.locator('[data-role="transfer-eta"]')).toHaveCount(0);
  // The Downloads row of the same transfer still carries it.
  await expect(cell(page, 9851).locator('[data-role="transfer-eta"]')).toHaveText('00:01:25');
  expect(errors).toEqual([]);
});
