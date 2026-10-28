const { test, expect } = require('@playwright/test');

/* DP 1.0.14 cross-engine navigation motion (ui-motion.js DPMotion.slide).
 *
 * One Web Animations slide in every engine: the owner's commit runs at once;
 * what the surface showed leaves and the live content arrives, both clipped to
 * the surface. What leaves is the real element where the commit only hides it
 * (sidebar views, Settings and Help panels: kept, data-dp-motion-outgoing),
 * or an inert ID-free copy beside the surface where the commit replaces it
 * (compact navigator, Downloads, Activity Log). These cases run in Chromium,
 * Firefox and WebKit (playwright.cross-engine.config.js, @cross-engine) against
 * the real application, and read what is drawn: the computed transforms of the
 * copy and of the live content in every frame, never the owner's own values.
 * No real secret is used anywhere: SENTINEL is synthetic. */

const SENTINEL = 'dp-synthetic-sentinel-7f3a91';

async function boot(page, {width = 1440, height = 900} = {}) {
  await page.setViewportSize({width, height});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route('**/api/events/stream', () => {});
}

const settled = page => page.waitForFunction(() => !document.querySelector('.dp-motion-surface, .dp-motion-still')
  && document.getAnimations().every(a => a.playState !== 'running' || a.effect?.getTiming().iterations === Infinity));

async function openSettings(page, width = 1440) {
  await boot(page, {width});
  await page.goto('/');
  await page.evaluate(() => nav(document.querySelector('[data-view="settings"]')));
  await expect(page.locator('#view-settings.active .dp-settings-tabs [data-tab="sources"][aria-selected="true"]').first()).toBeVisible();
  await settled(page);
}

/* Starts recording every frame of the slides on `surfaces` (selectors), then
 * runs `act` in the page (in the same task, so the first frame is recorded).
 * Per frame and surface: whether it slides, the x of its copy's first part and
 * of its first live part (px), its box, client size and scrollbar room, and
 * the travel animations' durations. */
async function record(page, surfaces, act, arg = null) {
  await page.evaluate(({surfaces, act, arg}) => {
    window.__frames = [];
    const x = el => (el ? new DOMMatrixReadOnly(getComputedStyle(el).transform).m41 : null);
    const tick = () => {
      const frame = {t: performance.now(), surfaces: {}};
      for (const selector of surfaces) {
        const surface = document.querySelector(`${selector}:not(.dp-motion-still)`);
        if (!surface) continue;
        const ghost = surface.nextElementSibling?.classList.contains('dp-motion-still') ? surface.nextElementSibling : null;
        const kept = surface.querySelector('[data-dp-motion-outgoing]');
        const live = [...(kept?.parentElement || surface).children].find(node => node.getClientRects().length
          && !node.classList.contains('dp-motion-still') && !node.hasAttribute('data-dp-motion-outgoing'));
        const box = surface.getBoundingClientRect();
        const clips = el => !!el && ['overflowX', 'overflowY'].every(key => getComputedStyle(el)[key] !== 'visible');
        const y = el => (el ? new DOMMatrixReadOnly(getComputedStyle(el).transform).m42 : null);
        const along = surface.dataset.dpMotionAxis === 'y' ? y : x;
        frame.surfaces[selector] = {sliding: surface.classList.contains('dp-motion-surface'), old: along(kept || ghost?.firstElementChild), now: along(live),
          kept: kept ? {id: kept.id || kept.dataset.panel || '', inert: kept.inert, hidden: kept.getAttribute('aria-hidden'), active: kept.classList.contains('active'), ownerHidden: kept.hidden} : null,
          actives: document.querySelectorAll('.view.active').length,
          ghostBox: ghost ? [ghost.getBoundingClientRect().left, ghost.getBoundingClientRect().top] : null, box: [box.left, box.top, box.width, box.height],
          client: [surface.clientWidth, surface.clientHeight], bar: surface.offsetWidth - surface.clientWidth, clip: clips(surface) && (!ghost || clips(ghost)),
          durations: [...new Set(document.getAnimations().filter(a => a.effect?.target instanceof Node && a.effect.getKeyframes().some(k => k.transform)
            && (surface.contains(a.effect.target) || !!ghost?.contains(a.effect.target))).map(a => a.effect.getTiming().duration))]};
      }
      window.__frames.push(frame);
      if (window.__frames.length < 120 && (document.querySelector('.dp-motion-surface') || window.__frames.length < 3)) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
    (0, eval)(`(${act})`)(arg);
  }, {surfaces, act: act.toString(), arg});
  await settled(page);
  return page.evaluate(() => window.__frames);
}

const along = (frames, selector) => frames.map(frame => frame.surfaces[selector]).filter(Boolean);

function expectTravel(frames, {direction, share, duration, axis = 'x'}) {
  const moving = frames.filter(frame => frame.sliding && (Math.abs(frame.old ?? 0) > 0.5 || Math.abs(frame.now ?? 0) > 0.5));
  expect(moving.length, 'drawn travel over several frames').toBeGreaterThanOrEqual(3);
  const sign = direction === 'forward' ? -1 : 1;
  expect(moving.every(frame => frame.old === null || Math.sign(Math.round(frame.old)) !== -sign), 'the old content leaves one way only').toBe(true);
  expect(moving.some(frame => Math.sign(Math.round(frame.old)) === sign), 'the old content leaves toward the start going forward').toBe(true);
  expect(moving.some(frame => Math.sign(Math.round(frame.now)) === -sign), 'the destination arrives from the other side').toBe(true);
  const width = frames[0].client[axis === 'y' ? 1 : 0];
  const far = Math.max(...moving.map(frame => Math.abs(frame.old ?? 0))) / width;
  expect(far).toBeGreaterThanOrEqual(share[0]);
  expect(far).toBeLessThanOrEqual(share[1]);
  expect([...new Set(moving.flatMap(frame => frame.durations))]).toEqual([duration]);
  expect(moving.every(frame => frame.clip), 'both presentations clipped to the surface').toBe(true);
  // The surface itself never moves or resizes, and gains or loses no scrollbar.
  for (const key of ['box', 'client', 'bar']) expect(new Set(frames.map(frame => JSON.stringify(frame[key]))).size, key).toBe(1);
  // The copy stands exactly over the surface's box.
  expect(moving.every(frame => !frame.ghostBox || (Math.abs(frame.ghostBox[0] - frame.box[0]) < 0.6 && Math.abs(frame.ghostBox[1] - frame.box[1]) < 0.6))).toBe(true);
  const last = frames.at(-1);
  expect(last.sliding).toBe(false);
}

test('@cross-engine a Settings or Help choice slides only its panel, by direction and distance, clipped, with the chrome live', async ({page, browserName}) => {
  await openSettings(page);
  const frames = await record(page, ['#view-settings .dp-settings-scroll'], () => {
    window.__hits = [];
    const entry = document.querySelector('#sidebar .nav-item[data-view="events"]');
    const box = entry.getBoundingClientRect();
    const probe = () => {
      if (!document.querySelector('.dp-motion-surface')) return;
      window.__hits.push(document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2)?.closest('.nav-item') === entry);
      requestAnimationFrame(probe);
    };
    document.querySelector('#view-settings .dp-settings-tabs [data-tab="extraction"]').click();
    requestAnimationFrame(probe);
  });
  const panel = along(frames, '#view-settings .dp-settings-scroll');
  expectTravel(panel, {direction: 'forward', share: [1.0, 1.22], duration: 380});
  // What leaves is the real Services panel, still hidden and unselected by
  // its owner, inert and silent while it is drawn.
  const kept = panel.filter(frame => frame.kept).map(frame => frame.kept);
  expect(kept.length).toBeGreaterThan(0);
  expect(kept.every(k => k.id === 'sources')).toBe(true);
  expect(kept.every(k => k.inert && k.hidden === 'true' && k.ownerHidden && !k.active)).toBe(true);
  const hits = await page.evaluate(() => window.__hits);
  expect(hits.length).toBeGreaterThan(0);
  expect(hits.every(Boolean), 'the sidebar stays live and hit-testable throughout').toBe(true);
  await expect(page.locator('#view-settings [data-panel="extraction"]')).toBeVisible();

  // Help: the next section back, the adjacent travel.
  await page.evaluate(() => nav(document.querySelector('[data-view="help"]')));
  await expect(page.locator('#view-help.active')).toBeVisible();
  await settled(page);
  const order = await page.locator('#view-help .dp-help-tab:not(.dp-motion-still *)').evaluateAll(tabs => tabs.map(tab => tab.dataset.tab));
  await page.locator(`#view-help .dp-help-tab[data-tab="${order[1]}"]:not(.dp-motion-still *)`).click();
  await settled(page);
  const back = await record(page, ['#view-help .dp-help-scroll'], first => {
    document.querySelector(`#view-help .dp-help-tab[data-tab="${first}"]`).click();
  }, order[0]);
  expectTravel(along(back, '#view-help .dp-help-scroll'), {direction: 'backward', share: [0.8, 1.02], duration: 280});
  test.info().annotations.push({type: 'engine', description: `${browserName} ${page.context().browser().version()}`});
});

// ── Activity Log ───────────────────────────────────────────────────────────
function journal(limit) {
  const pages = 10, total = (pages - 1) * limit + 7, snapshot = total;
  const ids = Array.from({length: total}, (_, index) => total - index);
  const event = id => ({id, severity: 'info', category: 'transfer', type: 'transfer.synthetic', message: `Event ${id}`,
                        transfer_id: id, occurred_at: '2026-10-09T12:00:00Z'});
  return {answer(q) {
    let start = 0;
    if (q.after) { const above = ids.filter(id => id > Number(q.after)); start = Math.max(0, above.length - limit); }
    else if (q.before) start = ids.findIndex(id => id < Number(q.before));
    const items = ids.slice(start, start + limit).map(event);
    const older = ids.length - (start + items.length);
    return {items, limit, snapshot, has_more: older > 0, next_before: older > 0 ? items.at(-1).id : null,
            more_after_next: older > limit, newer_available: false, search: {text: 'none', pending: 0, complete: true}, window_start: null};
  }};
}

test('@cross-engine Activity Log: an answer arriving mid-slide joins the running travel; a rapid next turn wins; nothing is fetched or shown twice', async ({page}) => {
  await boot(page);
  await page.addInitScript(() => sessionStorage.setItem('dp-activity-auto-refresh', 'off'));
  const requests = [];
  const control = {delay: 0};
  await page.route('**/api/events?**', async route => {
    const q = Object.fromEntries(new URL(route.request().url()).searchParams);
    if (q.limit !== '1') requests.push(q.before || q.after || 'first');
    if (q.limit !== '1' && control.delay) await new Promise(resolve => setTimeout(resolve, control.delay));
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(journal(Number(q.limit)).answer(q))});
  });
  await page.goto('/');
  await page.evaluate(() => { settingsData.activity_log_page_size = 50; });
  await page.evaluate(() => nav(document.querySelector('[data-view="events"]')));
  const current = page.locator('#dp-activity-page-btns .dp-pager-current');
  await expect(current).toHaveText('1');
  await settled(page);

  // The answer lands about halfway through the 280 ms turn.
  control.delay = 120;
  await page.evaluate(() => {
    window.__phase = [];
    const list = document.getElementById('event-list');
    const x = el => new DOMMatrixReadOnly(getComputedStyle(el).transform).m41;
    const tick = () => {
      const still = list.querySelector(':scope > .dp-motion-still');
      const row = list.querySelector(':scope > .dp-activity-row');
      window.__phase.push({sliding: list.classList.contains('dp-motion-surface'), still: still ? x(still) : null, stillOpacity: still ? +getComputedStyle(still).opacity : null,
        row: row ? x(row) : null, rows: list.querySelectorAll(':scope > .dp-activity-row').length,
        placeholder: !!list.querySelector(':scope > .dp-activity-placeholder'), ghost: !!list.nextElementSibling?.classList.contains('dp-motion-still')});
      if (window.__phase.length < 90) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
  const asked = requests.length;
  await page.locator('#dp-activity-page-btns [aria-label="Next page"]').click();
  await expect(current).toHaveText('2');
  await settled(page);
  const phase = await page.evaluate(() => window.__phase);
  // The real rows arrive while the slide is still travelling, and the
  // placeholder's fading still and the rows are carried at the same place.
  const joined = phase.filter(frame => frame.sliding && frame.row !== null && frame.still !== null);
  expect(joined.length, JSON.stringify(phase.slice(0, 30))).toBeGreaterThan(0);
  expect(joined.some(frame => Math.abs(frame.row) > 1), 'the rows join while still travelling').toBe(true);
  expect(joined.every(frame => Math.abs(frame.row - frame.still) <= 1), 'the still travels in phase with the rows').toBe(true);
  expect(phase.filter(frame => frame.sliding).every(frame => frame.ghost), 'the outgoing copy stays beside the list for the whole slide').toBe(true);
  expect(requests.length - asked, 'one request for the turn').toBe(1);
  const messages = await page.locator('#event-list .dp-activity-message').allTextContents();
  expect(messages.length).toBe(50);
  expect(new Set(messages).size).toBe(50);
  expect(messages[0]).toBe('Event 407');

  // A rapid second turn while the first is still sliding: the latest page wins.
  control.delay = 60;
  await page.evaluate(() => {
    const next = () => document.querySelector('#dp-activity-page-btns [aria-label="Next page"]');
    next().click();
    setTimeout(() => next()?.click(), 90);
  });
  await expect(current).toHaveText('4', {timeout: 5000}).catch(() => {});
  await settled(page);
  const final = await page.locator('#dp-activity-page-btns .dp-pager-current').textContent();
  const shown = await page.locator('#event-list .dp-activity-message').allTextContents();
  expect(new Set(shown).size).toBe(shown.length);
  const expectedFirst = `Event ${457 - 50 * (Number(final) - 1)}`;
  expect(shown[0], 'the page shown is the page the pager names').toBe(expectedFirst);
  expect(await page.locator('.dp-motion-still, .dp-motion-surface').count()).toBe(0);
});


test('@cross-engine a kept Settings panel is the only copy of its secrets and is released; a copy never carries a secret, in any form, at any frame', async ({page}) => {
  await openSettings(page);
  // Kept originals: the panel leaving is the real one; no second secret-bearing node exists, and it is released.
  const kept = [];
  for (const [from, to, which] of [['extraction', 'authentication', 'archive'], ['authentication', 'users', 'passwords'],
                                   ['notifications', 'maintenance', 'passwords']]) {
    await page.locator(`#view-settings .dp-settings-tabs [data-tab="${from}"]:not(.dp-motion-still *)`).click();
    await settled(page);
    kept.push(await page.evaluate(({from, to, which, SENTINEL}) => new Promise(resolve => {
      const panel = document.querySelector(`#view-settings [data-panel="${from}"]`);
      const drawn = selector => [...panel.querySelectorAll(selector)].filter(node => node.getClientRects().length);
      const seeded = which === 'archive' ? drawn('.dp-settings-password-line') : drawn('input[type="password"]');
      seeded.forEach(node => { node.value = SENTINEL; if (which === 'archive') node.dataset.passwordEditStart = SENTINEL; });
      const count = () => [...document.querySelectorAll('input, textarea')].filter(node => node.value === SENTINEL).length;
      const frames = [];
      const baseline = count();                     // every live original, this panel's and earlier ones'
      const look = () => {
        if (document.querySelector('.dp-motion-surface')) {
          frames.push({count: count(), kept: panel.hasAttribute('data-dp-motion-outgoing'), inert: panel.inert, hidden: panel.getAttribute('aria-hidden'),
                       copies: document.querySelectorAll('#view-settings .dp-motion-still').length,
                       focusIn: panel.contains(document.activeElement), hit: (() => { const r = seeded[0]?.getBoundingClientRect();
                         return r && r.width ? panel.contains(document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)) : false; })()});
          requestAnimationFrame(look);
        } else {
          const where = [...document.querySelectorAll('input, textarea')].filter(node => node.value === SENTINEL)
            .map(node => `${node.closest('[data-panel]')?.dataset.panel || node.closest('[id]')?.id}:${node.className}:${node.type}`);
          resolve({from, seeded: seeded.length, baseline, frames, where, after: {kept: panel.hasAttribute('data-dp-motion-outgoing'), inert: panel.inert,
            hidden: panel.getAttribute('aria-hidden'), style: panel.getAttribute('style'), values: seeded.every(node => node.value === SENTINEL)}});
        }
      };
      document.querySelector(`#view-settings .dp-settings-tabs [data-tab="${to}"]`).click();
      look();
    }), {from, to, which, SENTINEL}));
    await settled(page);
  }
  for (const k of kept) {
    if (!k.seeded) continue;
    expect(k.frames.length, k.from).toBeGreaterThan(0);
    expect(k.frames.every(f => f.count === k.baseline), `${k.from}: no second secret-bearing node ${JSON.stringify({baseline: k.baseline, counts: [...new Set(k.frames.map(f => f.count))], where: k.where})}`).toBe(true);
    expect(k.frames.every(f => f.kept && f.inert && f.hidden === 'true' && f.copies === 0 && !f.focusIn && !f.hit), k.from).toBe(true);
    expect(k.after, k.from).toEqual({kept: false, inert: false, hidden: null, style: null, values: true});
  }
  expect(kept.filter(k => k.seeded).length).toBeGreaterThanOrEqual(3);

  // A copy (the Activity list is replaced by its owner's commit): every DP
  // secret form, seeded as a fixture in the real list, is absent from the copy.
  await page.evaluate(() => nav(document.querySelector('[data-view="events"]')));
  await expect(page.locator('#view-events.active')).toBeVisible();
  await settled(page);
  const copied = await page.evaluate(SENTINEL => new Promise(resolve => {
    const list = document.getElementById('event-list');
    const fixture = document.createElement('div');
    fixture.innerHTML = `<input type="password" value="${SENTINEL}"><div class="dp-settings-extraction-password-editor">`
      + `<input class="dp-settings-password-line" type="text" data-password-edit-start="${SENTINEL}" value="${SENTINEL}"><textarea>${SENTINEL}</textarea></div>`
      + `<textarea data-setting="extraction_password">${SENTINEL}</textarea><div class="dp-token-disclosure"><input type="text" value="${SENTINEL}"></div>`;
    fixture.querySelectorAll('input, textarea').forEach(node => { node.value = SENTINEL; });
    list.prepend(fixture);
    const findings = [];
    let frames = 0, blanks = null;
    const look = () => {
      const ghost = list.nextElementSibling?.classList.contains('dp-motion-still') ? list.nextElementSibling : null;
      if (ghost) {
        frames += 1;
        if (ghost.outerHTML.includes(SENTINEL)) findings.push('markup');
        for (const node of [ghost, ...ghost.querySelectorAll('*')]) {
          for (const key of ['value', 'defaultValue', 'textContent']) if (typeof node[key] === 'string' && node[key].includes(SENTINEL)) findings.push(`${node.localName}.${key}`);
          for (const name of node.getAttributeNames()) if (node.getAttribute(name).includes(SENTINEL)) findings.push(`${node.localName}[${name}]`);
        }
        blanks ??= [...ghost.querySelectorAll('input, textarea')].filter(node => node.value === '').length;
      }
      if (document.querySelector('.dp-motion-surface')) requestAnimationFrame(look);
      else resolve({frames, findings, blanks, left: document.querySelectorAll('.dp-motion-still').length});
    };
    const source = Array.from(list.childNodes);
    window.DPMotion.slide({element: list, axis: 'x', direction: 1, distance: 1}, () => list.replaceChildren(...source.slice(1)));
    look();
  }), SENTINEL);
  expect(copied.frames).toBeGreaterThan(0);
  expect(copied.findings).toEqual([]);
  expect(copied.blanks).toBe(5);
  expect(copied.left).toBe(0);
});

test('@cross-engine a copy keeps form state, a canvas and scroll as drawn, with no IDs, names or tab stops', async ({page}) => {
  await boot(page);
  await page.goto('/');
  await page.evaluate(() => nav(document.querySelector('[data-view="events"]')));
  await expect(page.locator('#view-events.active')).toBeVisible();
  await settled(page);
  const result = await page.evaluate(() => {
    const list = document.getElementById('event-list');
    const fixture = document.createElement('div');
    fixture.id = 'dp-test-fixture';
    fixture.innerHTML = '<select class="dp-test-select" name="s"><option>a</option><option>b</option><option>c</option></select>'
      + '<input class="dp-test-box" type="checkbox" name="b"><input class="dp-test-text" type="text" name="t" value="attribute">'
      + '<div class="dp-test-scroll" style="height:40px;overflow:auto"><div style="height:200px">scrolled</div></div>'
      + '<canvas class="dp-test-canvas" width="40" height="20"></canvas>';
    list.prepend(fixture);
    fixture.querySelector('select').selectedIndex = 2;
    fixture.querySelector('[type="checkbox"]').checked = true;
    fixture.querySelector('[type="text"]').value = '4242';
    fixture.querySelector('.dp-test-scroll').scrollTop = 30;
    const context = fixture.querySelector('canvas').getContext('2d');
    context.fillStyle = 'rgb(255, 0, 0)'; context.fillRect(0, 0, 40, 20);
    const source = Array.from(list.childNodes);
    window.DPMotion.slide({element: list, axis: 'x', direction: 1, distance: 1}, () => list.replaceChildren(...source.slice(1)));
    const ghost = list.nextElementSibling;
    const copy = selector => ghost.querySelector(selector);
    return {
      isCopy: ghost.classList.contains('dp-motion-still'), inert: ghost.inert, hidden: ghost.getAttribute('aria-hidden'),
      select: copy('.dp-test-select')?.selectedIndex, box: copy('.dp-test-box')?.checked, text: copy('.dp-test-text')?.value,
      scroll: copy('.dp-test-scroll')?.scrollTop, pixel: [...copy('.dp-test-canvas').getContext('2d').getImageData(10, 10, 1, 1).data],
      ids: ghost.querySelectorAll('[id]').length + (ghost.id ? 1 : 0), names: ghost.querySelectorAll('[name]').length,
      duplicateIds: [...document.querySelectorAll('[id]')].map(node => node.id).filter((id, index, all) => all.indexOf(id) !== index).length,
      hitGhost: (() => { const r = list.getBoundingClientRect(); return !!document.elementFromPoint(r.left + r.width / 2, r.top + 20)?.closest('.dp-motion-still'); })(),
    };
  });
  expect(result).toEqual({isCopy: true, inert: true, hidden: 'true', select: 2, box: true, text: '4242', scroll: 30, pixel: [255, 0, 0, 255],
                          ids: 0, names: 0, duplicateIds: 0, hitGhost: false});
  await page.keyboard.press('Tab');
  expect(await page.evaluate(() => !!document.activeElement?.closest('.dp-motion-still'))).toBe(false);
  await settled(page);
});

test('@cross-engine lifecycle: done, settled and skip; supersession; two surfaces at once; reduced motion; nothing left behind', async ({page}) => {
  await boot(page);
  await page.goto('/');
  await page.evaluate(() => nav(document.querySelector('[data-view="events"]')));
  await expect(page.locator('#view-events.active')).toBeVisible();
  await settled(page);
  const result = await page.evaluate(async () => {
    const out = {};
    const list = document.getElementById('event-list');
    const styleBefore = list.getAttribute('style');
    const rows = Array.from(list.childNodes);
    let commits = 0;
    const slide = DPMotion.slide({element: list, axis: 'x', direction: 1, distance: 1}, () => { commits += 1; list.replaceChildren(...rows); });
    out.syncCommit = commits;
    out.surfaceMarked = list.classList.contains('dp-motion-surface');
    await slide.done;
    slide.skip(); slide.skip();
    await slide.settled;
    slide.skip();
    out.afterSkip = {commits, marked: list.classList.contains('dp-motion-surface'), stills: document.querySelectorAll('.dp-motion-still').length,
                     style: list.getAttribute('style') === styleBefore};
    const order = [];
    const a = DPMotion.slide({element: list, axis: 'x', direction: 1, distance: 1}, () => { order.push('A'); list.replaceChildren(...rows); });
    await new Promise(requestAnimationFrame);
    await new Promise(requestAnimationFrame);
    const b = DPMotion.slide({element: list, axis: 'x', direction: -1, distance: 1}, () => { order.push('B'); list.replaceChildren(...rows); });
    out.copiesDuring = document.querySelectorAll('.dp-motion-still').length;
    await a.settled;
    out.aSettledWhileB = list.classList.contains('dp-motion-surface');
    await b.settled;
    out.order = order;
    const late = document.createElement('div');
    list.append(late);
    await new Promise(requestAnimationFrame);
    out.lateAnimated = late.getAnimations().length;
    late.remove();
    out.styleRestored = list.getAttribute('style') === styleBefore;
    out.rowsAnimated = rows.filter(node => node instanceof Element && node.getAnimations().length).length;
    return out;
  });
  expect(result).toEqual({syncCommit: 1, surfaceMarked: true, afterSkip: {commits: 1, marked: false, stills: 0, style: true},
                          copiesDuring: 1, aSettledWhileB: true, order: ['A', 'B'], lateAnimated: 0, styleRestored: true, rowsAnimated: 0});

  // Two surfaces at once where the Settings header is compact: the navigator's
  // window (copied) and the panel (kept), each released; busy, then ready.
  await page.setViewportSize({width: 1000, height: 900});
  await page.evaluate(() => nav(document.querySelector('[data-view="settings"]')));
  await expect(page.locator('#view-settings.active')).toBeVisible();
  await settled(page);
  const compact = await page.evaluate(() => document.querySelector('#view-settings .dp-settings-master-header')?.dataset.presentation);
  if (compact === 'compact') {
    const both = await page.evaluate(() => new Promise(resolve => {
      const control = document.querySelector('#view-settings .dp-settings-tabs').parentElement;
      let ready = 0, max = 0, busySeen = false;
      control.addEventListener('dp-tooltip-ready', () => { ready += 1; });
      document.querySelectorAll('#view-settings .dp-settings-tabs [data-tab]')[5].click();
      const look = () => {
        max = Math.max(max, document.querySelectorAll('.dp-motion-surface').length);
        busySeen ||= control.hasAttribute('data-tooltip-busy');
        if (document.querySelector('.dp-motion-surface')) requestAnimationFrame(look);
        else setTimeout(() => resolve({max, busySeen, ready, busyLeft: control.hasAttribute('data-tooltip-busy')}), 50);
      };
      look();
    }));
    expect(both.max).toBe(2);
    expect(both.busySeen).toBe(both.ready > 0);
    expect(both.busyLeft).toBe(false);
  }
  test.info().annotations.push({type: 'compact navigator', description: String(compact)});
  await settled(page);
  expect(await page.locator('.dp-motion-still, .dp-motion-surface, [data-dp-motion-outgoing]').count()).toBe(0);

  // Reduced motion: the choice lands at once, with nothing kept, copied or moving.
  await page.emulateMedia({reducedMotion: 'reduce'});
  const reduced = await page.evaluate(() => {
    document.querySelectorAll('#view-settings .dp-settings-tabs [data-tab]')[1].click();
    return {surface: document.querySelectorAll('.dp-motion-surface').length, kept: document.querySelectorAll('[data-dp-motion-outgoing]').length,
            copies: document.querySelectorAll('.dp-motion-still').length, mq: matchMedia('(prefers-reduced-motion: reduce)').matches};
  });
  test.info().annotations.push({type: 'reduced motion reported', description: String(reduced.mq)});
  expect({surface: reduced.surface, kept: reduced.kept, copies: reduced.copies}).toEqual({surface: 0, kept: 0, copies: 0});
});

// ── Retained originals: A -> B -> A, the node leaving is the next destination ──
/* Chooses B, lets that slide be drawn (two frames), then chooses A again while
 * A is still kept and moving. The first flight must end and release A (no
 * inert, no aria-hidden, no outgoing mark, no inline style, no transform)
 * before the second captures; then B is the one kept; after both, nothing is
 * left on either. */
async function returnTrip(page, {pick, a, b}) {
  return page.evaluate(({pick, a, b}) => new Promise(resolve => {
    const node = selector => document.querySelector(selector);
    const state = el => ({inert: el.inert, hidden: el.getAttribute('aria-hidden'), kept: el.hasAttribute('data-dp-motion-outgoing'),
                          style: el.getAttribute('style'), transform: getComputedStyle(el).transform,
                          animations: el.getAnimations().filter(x => x.playState !== 'finished').length});
    const choose = name => (0, eval)(`(${pick})`)(name);
    const out = {};
    choose(b.name);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      out.firstKeptA = node(a.id).hasAttribute('data-dp-motion-outgoing');
      choose(a.name);
      out.duringA = state(node(a.id));
      out.duringB = state(node(b.id));
      const look = () => {
        if (document.querySelector('.dp-motion-surface')) return requestAnimationFrame(look);
        out.afterA = state(node(a.id));
        out.afterB = state(node(b.id));
        out.left = document.querySelectorAll('[data-dp-motion-outgoing], .dp-motion-still, .dp-motion-surface').length;
        resolve(out);
      };
      look();
    }));
  }), {pick: pick.toString(), a, b});
}
function expectReturn(trip, label) {
  const clean = {inert: false, hidden: null, kept: false, style: null, transform: 'none', animations: 0};
  expect(trip.firstKeptA, `${label}: A was kept by the first flight`).toBe(true);
  expect(trip.duringA, `${label}: A is released before it is shown again`).toMatchObject({inert: false, hidden: null, kept: false, style: null});
  expect(trip.duringB, `${label}: B is the one kept`).toMatchObject({inert: true, hidden: 'true', kept: true});
  expect(trip.afterA, label).toEqual(clean);
  expect(trip.afterB, label).toEqual(clean);
  expect(trip.left, label).toBe(0);
}

test('@cross-engine A -> B -> A: the kept node that is also the next destination is released first, on Settings, Help and primary navigation', async ({page}) => {
  await openSettings(page);
  expectReturn(await returnTrip(page, {pick: name => document.querySelector(`#view-settings .dp-settings-tabs [data-tab="${name}"]`).click(),
    a: {name: 'sources', id: '#view-settings [data-panel="sources"]'}, b: {name: 'downloads', id: '#view-settings [data-panel="downloads"]'}}), 'Settings');
  await settled(page);
  await page.evaluate(() => nav(document.querySelector('[data-view="help"]')));
  await expect(page.locator('#view-help.active')).toBeVisible();
  await settled(page);
  const order = await page.locator('#view-help .dp-help-tab:not(.dp-motion-still *)').evaluateAll(tabs => tabs.map(tab => tab.dataset.tab));
  expectReturn(await returnTrip(page, {pick: name => document.querySelector(`#view-help .dp-help-tab[data-tab="${name}"]`).click(),
    a: {name: order[0], id: `#dp-help-panel-${order[0]}`}, b: {name: order[1], id: `#dp-help-panel-${order[1]}`}}), 'Help');
  await settled(page);
  expectReturn(await returnTrip(page, {pick: name => document.querySelector(`#sidebar .nav-item[data-view="${name}"]`).click(),
    a: {name: 'help', id: '#view-help'}, b: {name: 'settings', id: '#view-settings'}}), 'primary navigation');
});

test('@cross-engine primary navigation keeps the real page leaving with its own styling and one selected page; a Downloads row copy keeps its status styling without its ID', async ({page}) => {
  await boot(page);
  await page.route('**/api/torrents**', route => {
    const url = new URL(route.request().url());
    if (url.pathname !== '/api/torrents' || !url.searchParams.has('offset')) return route.fallback();
    const limit = Math.max(1, Number(url.searchParams.get('limit')) || 25), offset = Number(url.searchParams.get('offset')) || 0;
    const items = Array.from({length: limit}, (_, index) => ({id: offset + index + 1, name: `Transfer ${offset + index + 1}`, hash: `h${offset + index}`,
      status: 'downloading', progress: 10, size_bytes: 1024, created_at: '2026-10-01T12:00:00Z', source: 'direct_link', label: null,
      current_provider_id: 'general_http', current_provider_name: 'HTTP & HTTPS', provider_provenance_status: 'known', extraction_status: null, source_failure_count: 0}));
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items, total: limit * 10})});
  });
  await page.goto('/');
  await settled(page);
  // Dashboard -> Settings: the Dashboard leaving is the real #view-dashboard,
  // drawn with its own layout, while exactly one page is selected.
  const before = await page.evaluate(() => {
    const view = document.getElementById('view-dashboard');
    return {display: getComputedStyle(view).display, direction: getComputedStyle(view).flexDirection, height: view.getBoundingClientRect().height};
  });
  const frames = await record(page, ['#content'], () => document.querySelector('#sidebar .nav-item[data-view="settings"]').click());
  const main = along(frames, '#content');
  // Dashboard to Settings is four entries down: the long travel.
  expectTravel(main, {direction: 'forward', share: [1.15, 1.42], duration: 450, axis: 'y'});
  const mid = main.filter(frame => frame.kept);
  expect(mid.length).toBeGreaterThan(0);
  expect(mid.every(frame => frame.kept.id === 'view-dashboard' && !frame.kept.active && frame.kept.inert && frame.actives === 1)).toBe(true);
  const styled = await page.evaluate(() => new Promise(resolve => {
    document.querySelector('#sidebar .nav-item[data-view="dashboard"]').click();
    requestAnimationFrame(() => {
      document.querySelector('#sidebar .nav-item[data-view="settings"]').click();
      const view = document.getElementById('view-dashboard');
      resolve({kept: view.hasAttribute('data-dp-motion-outgoing'), display: getComputedStyle(view).display, direction: getComputedStyle(view).flexDirection,
               height: view.getBoundingClientRect().height, actives: [...document.querySelectorAll('.view.active')].map(v => v.id),
               queried: document.querySelector('.view.active')?.id});
    });
  }));
  expect(styled).toMatchObject({kept: true, display: before.display, direction: before.direction, actives: ['view-settings'], queried: 'view-settings'});
  expect(Math.abs(styled.height - before.height)).toBeLessThan(1);
  await settled(page);

  // Downloads: a page turn's outgoing rows are a copy without #t-tbody, styled
  // by the same rules: the status badge and action cell look as they did.
  await page.evaluate(() => nav(document.querySelector('[data-view="torrents"]')));
  await expect(page.locator('#torrent-page-btns .dp-pager-current')).toHaveText('1');
  await settled(page);
  const rows = await page.evaluate(() => {
    const keys = ['backgroundColor', 'color', 'borderTopLeftRadius', 'paddingLeft', 'fontWeight', 'height'];
    const sample = el => el ? Object.fromEntries(keys.map(key => [key, getComputedStyle(el)[key]])) : null;
    const tb = document.getElementById('t-tbody');
    const live = {badge: sample(tb.querySelector('.badge')), cell: sample(tb.querySelector('.dp-downloads-action-cell')), row: sample(tb.querySelector('tr'))};
    document.querySelector('#torrent-page-btns [aria-label="Next page"]').click();
    const ghost = document.querySelector('#view-torrents .dp-downloads-table-wrap + .dp-motion-still');
    const copyBody = ghost?.querySelector('tbody');
    return {live, copy: {badge: sample(copyBody?.querySelector('.badge')), cell: sample(copyBody?.querySelector('.dp-downloads-action-cell')), row: sample(copyBody?.querySelector('tr'))},
            id: copyBody?.id ?? null, styledClass: copyBody?.classList.contains('dp-downloads-styled-tbody'), tbodies: document.querySelectorAll('#t-tbody').length};
  });
  expect(rows.live.badge, 'a status badge is drawn').not.toBeNull();
  expect(rows.copy).toEqual(rows.live);
  expect(rows.id).toBe('');
  expect(rows.styledClass).toBe(true);
  expect(rows.tbodies).toBe(1);
  await settled(page);
});
