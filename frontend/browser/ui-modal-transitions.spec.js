const { test, expect } = require('@playwright/test');

/* DP 1.0.13 fullscreen modals: they fade in and out in place
 * (ui-modal-contract.css, DPMotion.enter/leave), and Transfer Details opens
 * with its content -- never as a small loading shell that then grows. Every
 * frame is read from the live overlay and shell. The transfer is answered by
 * the page (a MOCK, at a chosen delay); everything else is the real
 * application. Motion is the browser default except in the reduced-motion case. */

const candidate = (id, source, active) => ({candidate_id: id, source_label: source, provider_id: 'alldebrid', relationship: 'Original',
  dispositions: active ? ['Active'] : [], is_selected: active, is_active: active, is_delivering: false, switch_eligible: !active});
const transfer = (id, name) => ({id, name, status: 'downloading', progress: 42, size_bytes: 1024, source: 'direct_link', label: '', hash: '',
  created_at: '2026-09-06T10:00:00Z', current_provider_id: 'alldebrid', current_provider_name: 'AllDebrid', active_provider_id: 'alldebrid',
  active_provider_name: 'AllDebrid', route_attempts: [], execution_attempts: [], executors: ['aria2'], source_outcomes: [], events: [],
  files: [{id: 502, filename: 'GF030926-M2SP-RN.rar', size_bytes: 1024, status: 'downloading', blocked: false, block_reason: null,
           candidate_count: 2, acquisition_candidates: [candidate('a', 'rapidgator.net', true), candidate('b', 'megaup.net', false)]}]});

async function boot(page, {delay = 0, reducedMotion} = {}) {
  if (reducedMotion) await page.emulateMedia({reducedMotion});
  await page.setViewportSize({width: 1440, height: 900});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route('**/api/events/stream', () => {});
  const state = {delay, fail: false, answered: []};
  await page.route(url => /^\/api\/torrents\/\d+$/.test(url.pathname), async route => {
    const id = Number(new URL(route.request().url()).pathname.split('/').pop());
    const wait = typeof state.delay === 'function' ? state.delay(id) : state.delay;
    if (wait) await new Promise(resolve => setTimeout(resolve, wait));
    state.answered.push(id);
    if (state.fail) return route.fulfill({status: 503, contentType: 'application/json', body: '{"detail":"busy"}'});
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(transfer(id, `Transfer ${id}`))});
  });
  await page.goto('/');
  await page.waitForFunction(() => window.showDetail && window.DPMotion);
  return state;
}

/* Every frame from just before `act` until `ms` after it has completed: the
 * overlay's display, opacity and running opacity transition, the panel's
 * opacity, box, transform and inertness, and what a point beside the panel
 * hits. */
async function record(page, act, ms = 600, overlaySelector = '#overlay', panelSelector = '#modal') {
  await page.evaluate(([o, m]) => {
    window.__frames = [];
    window.__recording = true;
    const t0 = performance.now();
    const sample = () => {
      const overlay = document.querySelector(o), panel = overlay && (overlay.matches(m) ? overlay : overlay.querySelector(m));
      if (overlay) {
        const style = getComputedStyle(overlay), box = panel ? panel.getBoundingClientRect() : null;
        const fading = overlay.getAnimations().filter(a => a instanceof CSSTransition && a.transitionProperty === 'opacity');
        window.__frames.push({t: performance.now() - t0, shown: style.display !== 'none', overlay: Number(style.opacity),
          panel: panel ? Number(getComputedStyle(panel).opacity) : 0, transform: panel ? getComputedStyle(panel).transform : 'none',
          box: box ? [box.left, box.top, box.width, box.height].map(Math.round) : null, inert: !!panel?.inert,
          fade: fading.map(a => a.effect.getTiming().duration)[0] ?? null,
          hit: document.elementFromPoint(20, 450)?.closest?.(o) ? 'overlay' : 'page'});
      } else window.__frames.push({t: performance.now() - t0, shown: false, overlay: 0, panel: 0, box: null, fade: null});
      if (window.__recording) requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  }, [overlaySelector, panelSelector]);
  await act();
  await page.waitForTimeout(ms);
  return page.evaluate(() => { window.__recording = false; return window.__frames; });
}
const seen = frame => frame.shown && frame.overlay * frame.panel > 0.02;
// The overlay's fade: its own CSS transition's duration, and the drawn opacities
// strictly between its ends, which must move one way only.
function fade(frames, direction) {
  const moving = frames.filter(frame => frame.shown && frame.overlay > 0.02 && frame.overlay < 0.98);
  const values = moving.map(frame => frame.overlay);
  const monotonic = values.every((value, index) => index === 0 || (direction > 0 ? value >= values[index - 1] : value <= values[index - 1]));
  return {duration: frames.map(frame => frame.fade).find(Boolean) ?? null, between: moving.length, monotonic, moving};
}

test('Transfer Details: the backdrop rises at once and the shell fades in, in place, at its final size', async ({page}) => {
  await boot(page);
  const frames = await record(page, () => page.evaluate(() => { void showDetail(990); }));
  await expect(page.locator('#modal-title')).toHaveText('Transfer 990');
  const visible = frames.filter(seen);
  const final = visible.at(-1).box;
  expect(visible.every(frame => frame.box.join() === final.join())).toBe(true);      // never smaller, never moved
  expect(visible.every(frame => frame.transform === 'none')).toBe(true);             // no translation or scaling
  // The backdrop's own fade starts with the opening: a 200 ms rise from transparent.
  expect(frames.find(frame => frame.shown).overlay).toBeLessThan(0.5);
  const rising = fade(frames, 1);
  expect(rising.duration).toBe(200);
  expect(rising.between).toBeGreaterThanOrEqual(2);
  expect(rising.monotonic).toBe(true);
  expect(frames.at(-1).overlay).toBe(1);
  expect(frames.at(-1).panel).toBe(1);
  await expect(page.locator('#modal-body')).not.toContainText('Loading transfer details');
});

test('Transfer Details, slow: the loading line sits in the full-size shell, and the content fills it without a resize or a second fade', async ({page}) => {
  await boot(page, {delay: 900});
  const frames = await record(page, () => page.evaluate(() => { void showDetail(990); }), 1400);
  await expect(page.locator('#modal-title')).toHaveText('Transfer 990');
  const visible = frames.filter(seen);
  expect(new Set(visible.map(frame => frame.box.join())).size).toBe(1);              // one size from the first visible frame
  const maxHeight = await page.locator('#modal').evaluate(node => Math.round(parseFloat(getComputedStyle(node).maxHeight)));
  expect(visible[0].box[3]).toBe(maxHeight);
  // Once fully shown, it never fades again when the content arrives.
  const full = visible.findIndex(frame => frame.overlay === 1 && frame.panel === 1);
  expect(visible.slice(full).every(frame => frame.overlay === 1 && frame.panel === 1)).toBe(true);
});

test('Closing fades in place, still covering the page and taking no input, then restores focus', async ({page}) => {
  await boot(page);
  await page.evaluate(() => {
    const opener = document.createElement('button');
    opener.id = 'probe-opener';
    opener.textContent = 'open';
    document.body.appendChild(opener);
    opener.focus();
    void showDetail(990);
  });
  await expect(page.locator('#modal-title')).toHaveText('Transfer 990');
  await page.waitForFunction(() => Number(getComputedStyle(document.getElementById('overlay')).opacity) === 1);
  const before = await page.locator('#modal').boundingBox();
  const frames = await record(page, () => page.locator('#modal .modal-close').click(), 500);
  const falling = fade(frames, -1);
  expect(falling.duration).toBe(160);                                                  // a 160 ms fade
  expect(falling.between).toBeGreaterThanOrEqual(2);
  expect(falling.monotonic).toBe(true);
  const leaving = falling.moving;
  expect(leaving.every(frame => frame.hit === 'overlay' && frame.inert)).toBe(true); // blocks the page; the panel takes nothing
  expect(leaving.every(frame => frame.box.join() === [before.x, before.y, before.width, before.height].map(Math.round).join())).toBe(true);
  expect(frames.at(-1).shown).toBe(false);
  await expect(page.locator('#probe-opener')).toBeFocused();
  // The backdrop still closes it, as before.
  await page.evaluate(() => { void showDetail(990); });
  await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);
  await page.mouse.click(10, 450);
  await expect(page.locator('#overlay')).toBeHidden();
});

test('Rapid open, close and reopen: the newest transfer, one live shell, nothing stale or stuck', async ({page}) => {
  const state = await boot(page);
  state.delay = id => (id === 990 ? 500 : 0);                       // the first answer arrives last
  await page.evaluate(() => { void showDetail(990); });
  await page.evaluate(() => closeModal());
  await page.evaluate(() => { void showDetail(991); });
  await expect(page.locator('#modal-title')).toHaveText('Transfer 991');
  await expect.poll(() => state.answered.includes(990)).toBe(true);  // the late 990 answer has arrived
  await page.evaluate(() => new Promise(requestAnimationFrame));
  await expect(page.locator('#modal-title')).toHaveText('Transfer 991');
  expect(await page.evaluate(() => {
    const overlay = document.getElementById('overlay'), modal = document.getElementById('modal');
    return {overlays: document.querySelectorAll('#overlay').length, open: overlay.classList.contains('open'),
            closing: overlay.classList.contains('is-closing'), inert: modal.inert, pending: 'dpModalPending' in modal.dataset,
            opacity: getComputedStyle(overlay).opacity};
  })).toEqual({overlays: 1, open: true, closing: false, inert: false, pending: false, opacity: '1'});
  // Closing and reopening mid-fade fades back from where it was.
  await page.evaluate(() => closeModal());
  await page.waitForTimeout(60);
  await page.evaluate(() => { void showDetail(991); });
  await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);
  await page.waitForFunction(() => Number(getComputedStyle(document.getElementById('overlay')).opacity) === 1);
  expect(await page.evaluate(() => [document.getElementById('overlay').classList.contains('is-closing'), document.getElementById('modal').inert,
    getComputedStyle(document.getElementById('overlay')).display])).toEqual([false, false, 'flex']);
});

test('Settings dialogs fade in and out in place; Escape still cancels; a dialog that follows another continues its backdrop', async ({page}) => {
  await boot(page);
  let handle;
  const opening = await record(page, () => page.evaluate(() => {
    window.__answer = window.DPSettingsModal.confirm({title: 'Probe', message: 'Fade?', confirmLabel: 'Yes'});
  }), 500, '.dp-modal-overlay', '.dp-modal-dialog');
  const shown = opening.filter(frame => frame.shown);
  expect(shown[0].overlay).toBeLessThan(0.5);
  expect(fade(opening, 1)).toMatchObject({duration: 200, monotonic: true});
  expect(fade(opening, 1).between).toBeGreaterThanOrEqual(2);
  const box = shown.at(-1).box;
  expect(shown.filter(frame => frame.overlay > 0.02).every(frame => frame.box.join() === box.join() && frame.transform === 'none')).toBe(true);
  const closing = await record(page, () => page.keyboard.press('Escape'), 500, '.dp-modal-overlay', '.dp-modal-dialog');
  const leaving = fade(closing, -1);
  expect(leaving).toMatchObject({duration: 160, monotonic: true});
  expect(leaving.between).toBeGreaterThanOrEqual(2);
  expect(leaving.moving.every(frame => frame.inert && frame.hit === 'overlay')).toBe(true);
  expect(await page.evaluate(() => window.__answer)).toBe(false);                    // Escape cancels, as before
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);

  // A confirm accepted straight into a progress dialog: never two overlays covering the page at once.
  await page.evaluate(() => {
    window.__counts = [];
    const tick = () => { window.__counts.push(document.querySelectorAll('.dp-modal-overlay').length); if (window.__counts.length < 60) requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
    // The application's own pattern: once a confirmation is accepted, its progress dialog opens.
    window.DPSettingsModal.confirm({title: 'Step one', message: 'Go on?', confirmLabel: 'Go'})
      .then(accepted => { if (accepted) window.__progress = window.DPSettingsModal.progress({title: 'Working'}); });
  });
  await page.locator('.dp-modal-overlay:not(.is-closing) [data-modal-accept]').click();
  await expect(page.locator('.dp-modal-overlay .dp-modal-progress')).toBeVisible();
  await page.waitForTimeout(400);
  expect(Math.max(...await page.evaluate(() => window.__counts))).toBe(1);
  expect(await page.locator('.dp-modal-overlay').evaluate(node => getComputedStyle(node).opacity)).toBe('1');
  await page.evaluate(() => window.__progress.close());
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
});

test('The Help legal document dialog fades in and out in place', async ({page}) => {
  await boot(page);
  await page.evaluate(() => nav(document.querySelector('[data-view="help"]')));
  await page.locator('#view-help .dp-help-tab[data-tab="license"]').click();
  const opener = page.locator('#view-help .dp-help-local-document-button[data-legal-document]').first();
  await expect(opener).toBeVisible();
  await page.waitForFunction(() => !document.querySelector('.dp-motion-surface'));   // the tab's slide has settled
  const opening = await record(page, () => opener.click(), 500, '.dp-backdrop', '.dp-dialog');
  const shown = opening.filter(frame => frame.shown);
  expect(shown[0].overlay).toBeLessThan(0.5);
  expect(fade(opening, 1)).toMatchObject({duration: 200, monotonic: true});
  const closing = await record(page, () => page.locator('.dp-help-legal-close').click(), 500, '.dp-backdrop', '.dp-dialog');
  expect(fade(closing, -1)).toMatchObject({duration: 160, monotonic: true});
  expect(fade(closing, -1).between).toBeGreaterThanOrEqual(2);
  await expect(page.locator('.dp-backdrop')).toHaveCount(0);
});

test('Reduced motion: modals open and close at once, correctly', async ({page}) => {
  await boot(page, {reducedMotion: 'reduce'});
  await page.evaluate(() => { void showDetail(990); });
  await expect(page.locator('#modal-title')).toHaveText('Transfer 990');
  expect(await page.evaluate(() => [getComputedStyle(document.getElementById('overlay')).opacity, getComputedStyle(document.getElementById('modal')).opacity,
    getComputedStyle(document.getElementById('overlay')).transitionDuration])).toEqual(['1', '1', '0s']);
  await page.locator('#modal .modal-close').click();
  await expect(page.locator('#overlay')).toBeHidden();
  await page.evaluate(() => { window.__answer = window.DPSettingsModal.confirm({title: 'Probe', message: 'Now?'}); });
  await page.keyboard.press('Escape');
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
});

test('A failed Details read still shows its shell and message, in place', async ({page}) => {
  const state = await boot(page);
  state.fail = true;
  await page.evaluate(() => { void showDetail(990); });
  await expect(page.locator('#modal-body')).toContainText('Failed to load details');
  await page.waitForFunction(() => Number(getComputedStyle(document.getElementById('modal')).opacity) === 1);
  await page.locator('#modal .modal-close').click();
  await expect(page.locator('#overlay')).toBeHidden();
});

/* ── The exit holds the modal's lifecycle until the dialog is gone ──────────
 * From the dismissal until the leaving dialog has gone (and 300 ms more), every
 * frame records whether a dialog is leaving, where focus is and the owner's page
 * lock; every key pressed is logged with its target and whether a dialog was
 * leaving when it arrived. The page control that opened the dialog (`opener`)
 * counts the focus, keys and clicks it receives, and the lock's releases are
 * counted as they happen. Keys are real key presses sent just after dismissal. */
async function exitLifecycle(page, {opener, leaving, lock = null, keys = ['Tab', 'Enter'], settled = null}, dismiss) {
  await page.evaluate(([openerSelector, leavingSelector, lockClass]) => {
    const target = document.querySelector(openerSelector);
    const probe = window.__probe = {focus: 0, keys: 0, clicks: 0, released: 0, frames: [], keys_seen: [], recording: true};
    target.addEventListener('focus', () => { if (probe.recording) probe.focus += 1; });
    target.addEventListener('keydown', () => { probe.keys += 1; });
    target.addEventListener('click', () => { probe.clicks += 1; });
    const where = node => {
      if (!node || node === document.body) return 'body';
      if (node === target) return 'opener';
      if (node.closest(leavingSelector)) return 'leaving';
      if (node.closest('.dp-modal-overlay:not(.is-closing), #overlay.open, .dp-backdrop:not(.is-closing)')) return 'dialog';
      return `page:${node.tagName.toLowerCase()}`;
    };
    window.addEventListener('keydown', event => {
      if (probe.recording) probe.keys_seen.push({key: event.key, target: where(event.target), leaving: !!document.querySelector(leavingSelector)});
    }, true);
    let locked = lockClass ? document.body.classList.contains(lockClass) : false;
    if (lockClass) new MutationObserver(() => {
      const now = document.body.classList.contains(lockClass);
      if (locked && !now) probe.released += 1;
      locked = now;
    }).observe(document.body, {attributes: true, attributeFilter: ['class']});
    const sample = () => {
      probe.frames.push({leaving: !!document.querySelector(leavingSelector), focus: where(document.activeElement),
        busy: !!document.querySelector('[aria-busy="true"]'),
        lock: lockClass ? document.body.classList.contains(lockClass) : null});
      if (probe.recording) requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  }, [opener, leaving, lock]);
  await dismiss();
  if (keys.length) {
    await page.waitForFunction(selector => document.querySelector(selector), leaving, {polling: 'raf'});
    for (const key of keys) await page.keyboard.press(key);
  }
  if (settled) await settled();
  await page.waitForFunction(selector => !document.querySelector(selector), leaving);
  await page.waitForTimeout(300);
  return page.evaluate(pressed => { window.__probe.recording = false; return {...window.__probe, pressed}; }, keys.length);
}

// During the exit: every key lands on the leaving dialog and reaches nothing
// beneath it, focus stays on it, the page lock holds. After: focus is on the
// opener, given once, and the lock released once.
function expectHeldThenReleased(result, {lock}) {
  const exiting = result.frames.filter(frame => frame.leaving);
  expect(exiting.length).toBeGreaterThanOrEqual(2);                                  // intermediate exit frames were seen
  const pressed = result.keys_seen.slice(-result.pressed);                            // the keys sent after dismissal
  expect(pressed.length).toBeGreaterThan(0);
  expect(pressed.every(key => key.leaving && key.target === 'leaving'), JSON.stringify(result.keys_seen)).toBe(true);
  expect(exiting.every(frame => frame.focus === 'leaving'), JSON.stringify(exiting)).toBe(true);
  if (lock) expect(exiting.every(frame => frame.lock === true)).toBe(true);
  expect([result.keys, result.clicks]).toEqual([0, 0]);                             // nothing beneath it reacted
  const after = result.frames.at(-1);
  expect(after.focus).toBe('opener');
  expect(result.focus).toBe(1);                                                      // focus restored exactly once
  if (lock) expect([after.lock, result.released]).toEqual([false, 1]);              // lock released exactly once
}

async function addOpener(page) {
  await page.evaluate(() => {
    const opener = document.createElement('button');
    opener.id = 'probe-opener';
    opener.textContent = 'open';
    document.querySelector('.main-content, main, body').appendChild(opener);
    opener.focus();
  });
}

test('Lifecycle: Transfer Details holds keyboard and focus through its exit, then returns focus once', async ({page}) => {
  await boot(page);
  await addOpener(page);
  await page.evaluate(() => { void showDetail(990); });
  await expect(page.locator('#modal-title')).toHaveText('Transfer 990');
  await page.waitForFunction(() => Number(getComputedStyle(document.getElementById('overlay')).opacity) === 1);
  await page.locator('#modal .modal-close').focus();
  const result = await exitLifecycle(page, {opener: '#probe-opener', leaving: '#overlay.is-closing', keys: ['Tab', 'Shift+Tab', 'Enter']},
    () => page.evaluate(() => closeModal()));
  expectHeldThenReleased(result, {lock: null});
});

test('Lifecycle: a Settings dialog holds keyboard, focus and the page lock through its exit, then releases them once', async ({page}) => {
  await boot(page);
  await addOpener(page);
  await page.evaluate(() => { window.__answer = window.DPSettingsModal.confirm({title: 'Probe', message: 'Hold?', confirmLabel: 'Yes'}); });
  await page.waitForFunction(() => Number(getComputedStyle(document.querySelector('.dp-modal-overlay')).opacity) === 1);
  const result = await exitLifecycle(page, {opener: '#probe-opener', leaving: '.dp-modal-overlay.is-closing', lock: 'dp-modal-open',
    keys: ['Tab', 'Enter', 'Escape']}, () => page.keyboard.press('Escape'));
  expectHeldThenReleased(result, {lock: 'dp-modal-open'});
  expect(await page.evaluate(() => window.__answer)).toBe(false);                    // settled once, by the first Escape
});

test('Lifecycle: a Settings dialog handed to the next keeps the lock and focus inside, then returns focus once', async ({page}) => {
  await boot(page);
  await addOpener(page);
  await page.evaluate(() => {
    window.DPSettingsModal.confirm({title: 'Step one', message: 'Go on?', confirmLabel: 'Go'})
      .then(accepted => { if (accepted) window.__progress = window.DPSettingsModal.progress({title: 'Working'}); });
  });
  await page.waitForFunction(() => Number(getComputedStyle(document.querySelector('.dp-modal-overlay')).opacity) === 1);
  const handoff = await exitLifecycle(page, {opener: '#probe-opener', leaving: '.dp-modal-overlay.is-closing', lock: 'dp-modal-open', keys: []},
    () => page.locator('.dp-modal-overlay [data-modal-accept]').click());
  // The next dialog took over at once: the lock never lapsed, focus never left the dialogs, the opener got nothing.
  expect(handoff.frames.every(frame => frame.lock === true && (frame.focus === 'dialog' || frame.focus === 'leaving')), JSON.stringify(handoff.frames)).toBe(true);
  expect([handoff.focus, handoff.released, handoff.keys, handoff.clicks]).toEqual([0, 0, 0, 0]);
  await expect(page.locator('.dp-modal-overlay .dp-modal-progress')).toBeVisible();
  // The handed-on dialog returns focus to the original opener when it, in turn, has gone.
  const result = await exitLifecycle(page, {opener: '#probe-opener', leaving: '.dp-modal-overlay.is-closing', lock: 'dp-modal-open'},
    () => page.evaluate(() => window.__progress.close()));
  expectHeldThenReleased(result, {lock: 'dp-modal-open'});
});

test('Lifecycle: the Help legal document dialog holds keyboard, focus and its page lock through its exit', async ({page}) => {
  await boot(page);
  await page.evaluate(() => nav(document.querySelector('[data-view="help"]')));
  await page.locator('#view-help .dp-help-tab[data-tab="license"]').click();
  const opener = page.locator('#view-help .dp-help-local-document-button[data-legal-document]').first();
  await expect(opener).toBeVisible();
  await page.waitForFunction(() => !document.querySelector('.dp-motion-surface'));
  await opener.evaluate(node => node.setAttribute('data-probe-opener', ''));
  await opener.click();
  await page.waitForFunction(() => Number(getComputedStyle(document.querySelector('.dp-backdrop')).opacity) === 1);
  const result = await exitLifecycle(page, {opener: '[data-probe-opener]', leaving: '.dp-backdrop.is-closing', lock: 'dp-help-legal-modal-open',
    keys: ['Tab', 'Enter', 'Escape']}, () => page.keyboard.press('Escape'));
  expectHeldThenReleased(result, {lock: 'dp-help-legal-modal-open'});
});

test('Lifecycle: an authentication challenge holds keyboard, focus and the page lock through its exit and hands over to the next', async ({page}) => {
  await boot(page);
  const challenge = id => ({id, name: `Auth ${id}`, status: 'input_required', progress: 0, size_bytes: 0, source: 'fixture', hash: '', label: '',
    created_at: '2026-09-02T00:00:00Z', error: null, error_message: null,
    input_required: {id: `c-${id}`, generation: 1, reason: 'auth_required', origin: 'provider',
      methods: [{method: 'username_password', fields: [{name: 'username', required: true}, {name: 'password', required: true}]}]}});
  const pending = new Map();
  await page.route(url => url.pathname === '/api/torrents' && url.searchParams.get('status') === 'input_required', route =>
    route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items: [...pending.values()], total: pending.size})}));
  await page.route(url => /^\/api\/torrents\/\d+\/cancel$/.test(url.pathname), route => {
    pending.delete(Number(new URL(route.request().url()).pathname.split('/')[3]));
    return route.fulfill({status: 200, contentType: 'application/json', body: '{"ok":true}'});
  });
  await page.waitForFunction(() => window.DPAuthRequired);
  await addOpener(page);
  pending.set(1011, challenge(1011));
  pending.set(1012, challenge(1012));
  await page.evaluate(() => window.DPAuthRequired.scan());
  const modal = page.locator('.dp-auth-required-overlay:not(.is-closing) [data-dp-input-required-modal]');
  await expect(modal).toHaveAttribute('data-dp-auth-transfer-id', '1011');
  // Cancelling the first hands over to the second: the lock never lapses, focus never reaches the page.
  const handoff = await exitLifecycle(page, {opener: '#probe-opener', leaving: '.dp-auth-required-overlay.is-closing', lock: 'dp-modal-open', keys: [],
    settled: () => expect(modal).toHaveAttribute('data-dp-auth-transfer-id', '1012')}, () => modal.locator('[data-dp-auth-cancel]').click());
  // (While its Cancel request is answered the first dialog is busy, its controls disabled -- the existing
  // busy state, before any exit; the hand-over is every frame after it.)
  const handed = handoff.frames.slice(handoff.frames.findLastIndex(frame => frame.busy) + 1);
  expect(handed.length).toBeGreaterThanOrEqual(2);
  expect(handoff.frames.every(frame => frame.lock === true && frame.focus !== 'opener')).toBe(true);
  expect(handed.every(frame => frame.focus === 'dialog' || frame.focus === 'leaving'), JSON.stringify(handoff.frames)).toBe(true);
  expect([handoff.focus, handoff.released, handoff.keys, handoff.clicks]).toEqual([0, 0, 0, 0]);
  expect(await page.locator('.dp-auth-required-overlay').count()).toBe(1);           // one backdrop: no flash between them
  // The last one's exit holds everything, then returns focus to the original opener once.
  await page.waitForFunction(() => Number(getComputedStyle(document.querySelector('.dp-auth-required-overlay')).opacity) === 1);
  const result = await exitLifecycle(page, {opener: '#probe-opener', leaving: '.dp-auth-required-overlay.is-closing', lock: 'dp-modal-open'},
    () => modal.locator('[data-dp-auth-cancel]').click());
  expectHeldThenReleased(result, {lock: 'dp-modal-open'});
});

test('Lifecycle, reduced motion: every dialog releases at once, focus returned and lock released once', async ({page}) => {
  await boot(page, {reducedMotion: 'reduce'});
  await addOpener(page);
  await page.evaluate(() => { window.__answer = window.DPSettingsModal.confirm({title: 'Probe', message: 'Now?'}); });
  await page.keyboard.press('Escape');
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
  await expect(page.locator('#probe-opener')).toBeFocused();
  expect(await page.evaluate(() => document.body.classList.contains('dp-modal-open'))).toBe(false);
  await page.evaluate(() => { void showDetail(990); });
  await expect(page.locator('#modal-title')).toHaveText('Transfer 990');
  await page.evaluate(() => closeModal());
  await expect(page.locator('#overlay')).toBeHidden();
  await expect(page.locator('#probe-opener')).toBeFocused();
});
