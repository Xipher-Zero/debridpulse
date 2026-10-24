const { test, expect } = require('@playwright/test');

// DP 1.0.14: a downloading transfer's estimated time remaining, beneath its
// primary bar in Downloads. The browser renders the backend's one estimate
// (``acquisition_eta``) -- H:MM:SS or M:SS, alike held or not, "ETA ∞" while it acquires
// without a number, or "—" -- and never counts it down
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

// A width change can move the shell between the drawer, the compact rail and
// the full sidebar, and the sidebar animates its width: geometry is read once
// that transition has finished (the real CSS transition, not a sleep).
async function resize(page, width) {
  await page.setViewportSize({width, height: 900});
  await page.waitForFunction(() =>
    document.getElementById('sidebar').getAnimations().every(animation => animation.playState !== 'running'));
}

function cell(page, id) {
  return page.locator(`#t-tbody tr[data-torrent-id="${id}"] [data-role="transfer-progress"]`);
}

test('the estimate renders compactly, held, infinite, or unavailable, and only while downloading', async ({ page }) => {
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
    item(9811, {seconds: 3725, held: false, estimated: true}),
    item(9812, {seconds: 30, held: false}),
    item(9813, {seconds: 210, held: false}),
    item(9814, {seconds: 1830, held: false}),
    item(9815, {seconds: 3599, held: false}),
    item(9816, {seconds: 3600, held: false}),
    item(9817, {seconds: 123 * 3600 + 30 * 60 + 30, held: false}),
    item(9818, {seconds: null, held: false, estimated: false, indeterminate: true}),
    item(9819, {seconds: null, held: false, estimated: true, indeterminate: true}),
    item(9820, {seconds: null, held: false, estimated: false, indeterminate: true}, {status: 'queued'}),
  ]);
  const eta = id => cell(page, id).locator('[data-role="transfer-eta"]');
  await expect(eta(9801)).toHaveText('ETA 1:02:05');
  await expect(eta(9801)).toHaveAttribute('aria-label', 'Estimated time remaining 1:02:05');
  // Held or best-effort, a figure reads and looks exactly like any other:
  // the same class, words, tooltip and type.
  const neutral = await page.evaluate(ids => ids.map(id => {
    const element = document.querySelector(`#t-tbody tr[data-torrent-id="${id}"] [data-role="transfer-eta"]`);
    const style = getComputedStyle(element);
    return [element.className, element.getAttribute('aria-label'), element.getAttribute('data-tooltip'),
            style.fontStyle, style.fontWeight, style.fontFamily, style.fontSize, style.color];
  }), [9801, 9802, 9811]);
  expect(neutral[1]).toEqual(neutral[0]);
  expect(neutral[2]).toEqual(neutral[0]);
  expect(neutral[0][0]).toBe('prog-eta');
  expect(neutral[0][3]).toBe('normal');
  expect(neutral[0][2]).toBe('Estimated time remaining for the download, a prediction; processing afterwards is not included');
  await expect(eta(9802)).toHaveText('ETA 1:02:05');
  await expect(eta(9803)).toHaveText('—');
  await expect(eta(9804)).toHaveText('ETA 100:01:05');
  await expect(eta(9805)).toHaveText('ETA 9999:59:59');
  for (const id of [9806, 9807, 9808]) await expect(eta(id)).toHaveText('—');
  for (const id of [9809, 9810, 9820]) await expect(eta(id)).toHaveCount(0);   // not downloading: none, not even ∞
  await expect(eta(9811)).toHaveText('ETA 1:02:05');
  // Compact: M:SS under an hour, H:MM:SS from one, hours never wrapping.
  const compact = {9812: 'ETA 0:30', 9813: 'ETA 3:30', 9814: 'ETA 30:30', 9815: 'ETA 59:59', 9816: 'ETA 1:00:00',
                   9817: 'ETA 123:30:30'};
  for (const [id, text] of Object.entries(compact)) await expect(eta(Number(id))).toHaveText(text);
  // Acquiring without a number: infinity, never a figure, and said so.
  for (const id of [9818, 9819]) {
    await expect(eta(id)).toHaveText('ETA ∞');
    await expect(eta(id)).toHaveClass(/is-indeterminate/);
    await expect(eta(id)).toHaveAttribute('aria-label', 'Estimated time remaining: cannot be estimated while downloading');
  }
  expect(await page.locator('#t-tbody [data-role="transfer-eta"]').allInnerTexts()).not.toContainEqual(
    expect.stringContaining('~'));
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
  // Every Downloads layout that shows the progress column (hidden at 901-1050 px and at 779 px and below).
  for (const width of [1440, 1100, 900, 780]) {
    await resize(page, width);
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
    // A changing value never moves the row: "—", "ETA ∞" and every estimate
    // under 1000 h take the reserved width.
    const heights = await page.evaluate(values => {
      const cell = document.querySelector('#t-tbody tr[data-torrent-id="9821"] [data-role="transfer-progress"]');
      const html = cell.innerHTML;
      const height = eta => { cell.innerHTML = progress(40, 'downloading', null, null, eta); return cell.getBoundingClientRect().height; };
      const result = {without: (cell.innerHTML = progress(40, 'downloading', null, null), cell.getBoundingClientRect().height),
                      values: values.map(height)};
      cell.innerHTML = html;
      return result;
    }, [null, {seconds: 5, held: false}, {seconds: null, held: false, indeterminate: true},
        {seconds: 999 * 3600 + 59 * 60 + 59, held: true, estimated: true}]);
    expect(new Set(heights.values).size, `width ${width}`).toBe(1);
    // Where the column has room, the row is exactly as tall as without an estimate.
    if (width === 1440) expect(Math.abs(heights.values[0] - heights.without)).toBeLessThanOrEqual(0.5);
  }
  // Where the layout hides the progress column, the estimate goes with its bar.
  for (const width of [1000, 390]) {
    await resize(page, width);
    await expect(cell(page, 9821)).toBeHidden();
    await expect(cell(page, 9821).locator('[data-role="transfer-eta"]')).toBeHidden();
  }
});

test('a live progress update and a full refresh show the same estimate', async ({ page }) => {
  await isolateExternalFonts(page);
  await downloads(page, [item(9831, {seconds: 90, held: false})]);
  const eta = cell(page, 9831).locator('[data-role="transfer-eta"]');
  await expect(eta).toHaveText('ETA 1:30');
  const patch = update => page.evaluate(update => patchProgressOnlyTransferEvent(
    {progress_only: true, items: [{id: 9831, status: 'downloading', progress: 45, active_execution_progress: null,
                                   active_execution_basis: null, status_changed: false, ...update}]}), update);
  expect(await patch({acquisition_eta: {seconds: 85, held: false}})).toBe(true);
  await expect(eta).toHaveText('ETA 1:25');
  const live = await cell(page, 9831).innerHTML();
  served[0] = {...served[0], progress: 45, acquisition_eta: {seconds: 85, held: false}};
  await page.evaluate(() => loadTorrents());
  expect(await cell(page, 9831).innerHTML()).toBe(live);           // one renderer, one truth
  // Held and best-effort toggling while the bar moves leaves the estimate's
  // element exactly as it was: never replaced, never touched.
  await page.evaluate(() => {
    window.__eta = document.querySelector('#t-tbody tr[data-torrent-id="9831"] [data-role="transfer-eta"]');
    window.__etaMutations = 0;
    new MutationObserver(records => { window.__etaMutations += records.length; })
      .observe(window.__eta, {attributes: true, childList: true, characterData: true, subtree: true});
  });
  let progressValue = 45;
  for (const flags of [{held: true}, {held: false, estimated: true}, {held: true, estimated: true}, {held: false}]) {
    progressValue += 0.5;
    await patch({progress: progressValue, acquisition_eta: {seconds: 85, ...flags}});
    await expect(cell(page, 9831).locator('.prog-pct')).toHaveText(`${Math.round(progressValue)}%`);
  }
  expect(await page.evaluate(() => [
    window.__eta === document.querySelector('#t-tbody tr[data-torrent-id="9831"] [data-role="transfer-eta"]'),
    window.__etaMutations, window.__eta.className, window.__eta.getAttribute('aria-label'),
    getComputedStyle(window.__eta).fontStyle])).toEqual([true, 0, 'prog-eta', 'Estimated time remaining 1:25', 'normal']);
  // Hovered, its tooltip stays open through live updates that leave the
  // estimate as it reads, even though the open tooltip marks its host.
  await eta.hover();
  const tip = page.locator('#dp-tooltip');
  await expect(tip).toBeVisible();
  await expect(eta).toHaveAttribute('aria-describedby', 'dp-tooltip');
  for (const flags of [{held: true}, {held: false, estimated: true}, {held: false}]) {
    progressValue += 0.5;
    await patch({progress: progressValue, acquisition_eta: {seconds: 85, ...flags}});
    await expect(cell(page, 9831).locator('.prog-pct')).toHaveText(`${Math.round(progressValue)}%`);
  }
  expect(await page.evaluate(() =>
    window.__eta === document.querySelector('#t-tbody tr[data-torrent-id="9831"] [data-role="transfer-eta"]'))).toBe(true);
  await expect(tip).toBeVisible();
  await expect(tip).toHaveText('Estimated time remaining for the download, a prediction; processing afterwards is not included');
  await page.mouse.move(0, 0);
  await patch({progress: progressValue, acquisition_eta: {seconds: 82, held: false}});
  await expect(eta).toHaveText('ETA 1:22');                         // a new number is put up at once
  await patch({acquisition_eta: {seconds: null, held: false, estimated: false, indeterminate: true}});
  await expect(eta).toHaveText('ETA ∞');                            // acquiring, no number
  const infinite = await cell(page, 9831).innerHTML();
  served[0] = {...served[0], acquisition_eta: {seconds: null, held: false, estimated: false, indeterminate: true}};
  await page.evaluate(() => loadTorrents());
  expect(await cell(page, 9831).innerHTML()).toBe(infinite);       // the refresh agrees
  await patch({acquisition_eta: null});                             // past the hold: none
  await expect(eta).toHaveText('—');
  // Nothing counts it down between updates.
  await patch({acquisition_eta: {seconds: 60, held: false}});
  await page.waitForTimeout(2200);
  await expect(eta).toHaveText('ETA 1:00');
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

test('Download Activity and Downloads show the same estimate, on refresh and on live updates', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  const usenet = {...item(9851, {seconds: 90, held: false}, {progress: 0}), active_execution_progress: 47.9,
                  active_execution_basis: 'bytes'};
  await downloads(page, [usenet, item(9852, {seconds: 4000, held: false, estimated: true}), item(9853, null),
                         item(9854, {seconds: 30, held: false}, {status: 'paused'}),
                         item(9855, {seconds: null, held: false, estimated: false, indeterminate: true})]);
  await page.evaluate(async () => { await loadRecent(); });
  const both = id => [cell(page, id), page.locator(`#dash-tbody tr[data-torrent-id="${id}"] [data-role="transfer-progress"]`)];
  const etaText = async id => Promise.all(both(id).map(surface => surface.locator('[data-role="transfer-eta"]').allInnerTexts()));
  const same = async (id, expected) => expect(await etaText(id), `transfer ${id}`).toEqual([expected, expected]);
  await same(9851, ['ETA 1:30']);
  await same(9852, ['ETA 1:06:40']);                                  // approximate: no glyph
  await same(9855, ['ETA ∞']);                                        // acquiring, no number
  await same(9853, ['—']);                                            // unavailable
  await same(9854, []);                                               // not downloading: none on either
  for (const surface of both(9851)) await expect(surface.locator('.prog-pct')).toHaveText('47.9% estimated progress');
  const patch = (id, update) => page.evaluate(([id, update]) => patchProgressOnlyTransferEvent(
    {progress_only: true, items: [{id, status: 'downloading', progress: 0, active_execution_progress: 48.2,
                                   active_execution_basis: 'bytes', status_changed: false, ...update}]}), [id, update]);
  // A live update reaches both surfaces alike: a new figure, held, then none.
  expect(await patch(9851, {acquisition_eta: {seconds: 85, held: false}})).toBe(true);
  await same(9851, ['ETA 1:25']);
  const looks = () => Promise.all(both(9851).map(surface => surface.locator('[data-role="transfer-eta"]').evaluate(element =>
    [element.className, element.getAttribute('aria-label'), element.getAttribute('data-tooltip'),
     getComputedStyle(element).fontStyle])));
  const measured = await looks();
  await patch(9851, {acquisition_eta: {seconds: 85, held: true, estimated: true}});
  await same(9851, ['ETA 1:25']);
  expect(await looks()).toEqual(measured);                            // held or rough: the same on both
  expect(measured[0]).toEqual(measured[1]);
  expect(measured[0][3]).toBe('normal');
  await patch(9851, {acquisition_eta: {seconds: null, held: false, estimated: false, indeterminate: true}});
  await same(9851, ['ETA ∞']);
  await patch(9851, {acquisition_eta: null});
  await same(9851, ['—']);
  // A full refresh of each surface presents exactly what the live update left.
  served[0] = {...served[0], active_execution_progress: 48.2, acquisition_eta: null};
  const live = await Promise.all(both(9851).map(surface => surface.innerHTML()));
  await page.evaluate(async () => { await loadTorrents(); await loadRecent(); });
  expect(await Promise.all(both(9851).map(surface => surface.innerHTML()))).toEqual(live);
  // A lifecycle change is never patched: both refresh, and the paused row has no estimate.
  expect(await patch(9851, {status: 'paused', status_changed: true})).toBe(false);
  served[0] = {...served[0], status: 'paused', presentation_status: 'paused', presentation_badge_status: 'paused',
               acquisition_eta: {seconds: 85, held: false}};
  await page.evaluate(async () => { await loadTorrents(); await loadRecent(); });
  await same(9851, []);
  expect(errors).toEqual([]);
});

test('the percentage and its ETA share one line beneath the bar at every width that shows Progress', async ({ page }) => {
  await isolateExternalFonts(page);
  // The reported Usenet row (nothing verified, 47.9% acquired by the service),
  // the widest line ("100.0%" and the widest estimate under 1000 h) and a
  // verified row with an in-flight lane.
  const usenet = {...item(9861, {seconds: 2318, held: false}, {progress: 0}), active_execution_progress: 47.9,
                  active_execution_basis: 'bytes'};
  const widest = {...item(9862, {seconds: 999 * 3600 + 59 * 60 + 59, held: false, estimated: true}, {progress: 0}),
                  active_execution_progress: 100, active_execution_basis: 'bytes'};
  const verified = {...item(9863, {seconds: 2318, held: true}, {progress: 41}), active_execution_progress: 76,
                    active_execution_basis: 'bytes'};
  await downloads(page, [usenet, widest, verified]);
  const geometry = id => page.evaluate(id => {
    const cell = document.querySelector(`#t-tbody tr[data-torrent-id="${id}"] [data-role="transfer-progress"]`);
    if (getComputedStyle(cell).display === 'none') return null;
    const pct = cell.querySelector('.prog-pct'), words = pct.querySelector('.prog-words');
    const range = document.createRange();
    range.selectNodeContents(pct);
    const text = [...range.getClientRects()].filter(rect => rect.width > 0);
    const eta = cell.querySelector('[data-role="transfer-eta"]').getBoundingClientRect();
    const bar = cell.querySelector('.prog').getBoundingClientRect();
    return {lines: new Set(text.map(rect => Math.round(rect.top))).size, sameLine: Math.abs(text[0].top - eta.top) <= 2,
            apart: Math.max(...text.map(rect => rect.right)) <= eta.left, aligned: Math.abs(eta.right - bar.right) <= 1.5,
            inside: eta.right <= bar.right + 1.5 && text[0].left >= bar.left - 1.5, overflow: cell.scrollWidth > cell.clientWidth + 1,
            words: words ? getComputedStyle(words).display !== 'none' : null, visible: pct.innerText.trim(),
            label: pct.getAttribute('aria-label')};
  }, id);
  // Downloads shows Progress at <=779 never, at 780-900 (content-sized), and from 1051.
  for (const width of [780, 800, 900, 1051, 1100, 1179, 1180, 1300, 1439, 1440, 1500, 1599, 1600, 1920, 2560]) {
    await resize(page, width);
    for (const id of [9861, 9862, 9863]) {
      const g = await geometry(id);
      expect(g, `width ${width}`).not.toBeNull();
      const where = `row ${id} at ${width}px`;
      expect(g.lines, where).toBe(1);                                   // one line, never two
      expect(g.sameLine && g.apart && g.aligned && g.inside, where).toBe(true);   // words left, ETA right, no overlap
      expect(g.overflow, where).toBe(false);                            // nothing cut or spilling
      expect(g.words, where).toBe(width >= 1600);                       // the words where they fit, else the figure
    }
    const usenetLine = await geometry(9861);
    expect(usenetLine.visible).toBe(width >= 1600 ? '47.9% estimated progress' : '47.9%');
    expect(usenetLine.label).toBe('47.9% estimated progress, not yet verified');   // the distinction stays announced
    expect((await geometry(9863)).label).toBe('41% verified');
  }
  for (const width of [390, 700, 1000, 1050]) {
    await resize(page, width);
    expect(await geometry(9861), `width ${width}`).toBeNull();          // Progress steps aside there, as before
  }
});

test('Download Activity keeps the percentage and its ETA on one line at every width', async ({ page }) => {
  await isolateExternalFonts(page);
  const usenet = {...item(9871, {seconds: 2318, held: false}, {progress: 0}), active_execution_progress: 47.9,
                  active_execution_basis: 'bytes'};
  const widest = {...item(9872, {seconds: 999 * 3600 + 59 * 60 + 59, held: false, estimated: true}, {progress: 0}),
                  active_execution_progress: 100, active_execution_basis: 'bytes'};
  const verified = {...item(9873, {seconds: 2318, held: true}, {progress: 41}), active_execution_progress: 76,
                    active_execution_basis: 'bytes'};
  await downloads(page, [usenet, widest, verified]);
  await page.evaluate(async () => { nav(document.querySelector('#sidebar .nav-item[data-view="dashboard"]')); await loadRecent(); });
  const geometry = id => page.evaluate(id => {
    const cell = document.querySelector(`#dash-tbody tr[data-torrent-id="${id}"] [data-role="transfer-progress"]`);
    const pct = cell.querySelector('.prog-pct'), words = pct.querySelector('.prog-words');
    const range = document.createRange();
    range.selectNodeContents(pct);
    const text = [...range.getClientRects()].filter(rect => rect.width > 0);
    const eta = cell.querySelector('[data-role="transfer-eta"]').getBoundingClientRect();
    const bar = cell.querySelector('.prog').getBoundingClientRect();
    return {lines: new Set(text.map(rect => Math.round(rect.top))).size, sameLine: Math.abs(text[0].top - eta.top) <= 2,
            apart: Math.max(...text.map(rect => rect.right)) <= eta.left, aligned: Math.abs(eta.right - bar.right) <= 1.5,
            overflow: cell.scrollWidth > cell.clientWidth + 1, words: getComputedStyle(words).display !== 'none',
            visible: pct.innerText.trim()};
  }, id);
  // The card clips what does not fit it: from 1179px nothing may (below that
  // the activity table already scrolls sideways, as before this estimate).
  const clipped = () => page.evaluate(() => {
    const wrap = document.querySelector('#view-dashboard .dash-activity-table-wrap');
    const last = document.querySelector('#dash-tbody tr[data-torrent-id] td:last-child').getBoundingClientRect();
    return wrap.scrollWidth > wrap.clientWidth + 1 || last.right > wrap.getBoundingClientRect().right + 1;
  });
  for (const width of [701, 900, 1100, 1179, 1180, 1300, 1439, 1440, 1500, 1599, 1600, 1920, 2560]) {
    await resize(page, width);
    if (width >= 1179) expect(await clipped(), `width ${width}`).toBe(false);
    for (const id of [9871, 9872, 9873]) {
      const g = await geometry(id), where = `row ${id} at ${width}px`;
      expect(g.lines, where).toBe(1);
      expect(g.sameLine && g.apart && g.aligned, where).toBe(true);
      expect(g.overflow, where).toBe(false);
      expect(g.words, where).toBe(width >= 1600);
    }
    expect((await geometry(9871)).visible).toBe(width >= 1600 ? '47.9% estimated progress' : '47.9%');
  }
});
