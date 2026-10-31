const { test, expect } = require('@playwright/test');

/* DP 1.0.14 handheld sheets: every close presents its whole exit. A closing
 * sheet stays drawn while its panel travels back to its own bar, and is
 * hidden only once the panel is there -- in every engine, whatever the
 * engine does with a dismissed popover's display (Firefox hid it at once),
 * and whichever path closes it: its control, its scrim, a press outside it,
 * Escape, Tab, or a page chosen from More (that page opens after the exit,
 * so its own work never takes the exit's frames). Each close is sampled on
 * every frame from the request until the sheet is no longer drawn. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const noFonts = page => page.route(/fonts\.(googleapis|gstatic)\.com/, route => route.abort());
const noPulse = page => page.route('**/api/events/stream', () => {});
const SHEETS = {
  more: {open: '#shell-dock .dp-shell-dock-more', box: '#shell-more', panel: '#shell-more .dp-shell-sheet-panel', toward: 1},
  theme: {open: '#theme-picker-toggle', box: '#theme-picker', panel: '#theme-picker .dp-theme-picker-panel', toward: -1},
  cap: {open: '#runtime-cap-toggle', box: '#runtime-cap-menu', panel: '#runtime-cap-menu .runtime-cap-panel', toward: -1},
  account: {open: '#topbar-account-button', box: '#topbar-account-sheet', panel: '#topbar-account-sheet .dp-shell-sheet-panel', toward: -1},
};

async function open(page, {width = 390, height = 844, base} = {}) {
  await noFonts(page);
  await noPulse(page);
  await page.setViewportSize({width, height});
  await page.goto(base ? `${base}/` : '/');
  await page.waitForFunction(() => document.querySelector('#shell-dock > *') && window.DPTheme);
}
const drawn = (page, sheet) => page.evaluate(box => {
  const node = document.querySelector(box);
  return !!node && getComputedStyle(node).display !== 'none';
}, SHEETS[sheet].box);
const rested = (page, sheet) => page.waitForFunction(panel => document.querySelector(panel)?.getAnimations().length === 0, SHEETS[sheet].panel);
async function openSheet(page, sheet) {
  await page.locator(SHEETS[sheet].open).click();
  await expect.poll(() => drawn(page, sheet)).toBe(true);
  await rested(page, sheet);
}

/* From now until the sheet is no longer drawn, each frame: how far its panel
 * has travelled toward its bar, as a share of its height (0 at rest, 1 back
 * at the bar's edge), from its drawn box clipped to the sheet's box. */
const watch = (page, sheet) => page.evaluate(([box, panel, toward]) => {
  const sheet = document.querySelector(box), body = document.querySelector(panel);
  const out = window.__exit = {frames: [], done: false, start: performance.now()};
  const share = () => {
    const shell = sheet.getBoundingClientRect(), part = body.getBoundingClientRect();
    const shown = Math.max(0, Math.min(shell.bottom, part.bottom) - Math.max(shell.top, part.top));
    return {shown: 1 - shown / part.height, sign: Math.sign(new DOMMatrixReadOnly(getComputedStyle(body).transform).m42)};
  };
  out.before = share().shown;
  // Where the panel was drawn when the close was asked for (the key or press arriving).
  const asked = () => { if (out.asked === undefined) { out.asked = Math.round((performance.now() - out.start)); out.at = share().shown; } };
  ['keydown', 'pointerdown'].forEach(type => window.addEventListener(type, asked, {capture: true, once: true}));
  const tick = () => {
    if (getComputedStyle(sheet).display === 'none') { out.done = true; out.end = performance.now(); return; }
    const {shown, sign} = share();
    out.frames.push({t: Math.round(performance.now() - out.start), travelled: Math.round(shown * 1000) / 1000, sign});
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}, [SHEETS[sheet].box, SHEETS[sheet].panel, SHEETS[sheet].toward]);
// Only what was drawn from the moment the close was asked for counts.
const exited = page => page.waitForFunction(() => window.__exit?.done, null, {timeout: 3000})
  .then(() => page.evaluate(() => window.__exit))
  .then(exit => ({...exit, frames: exit.frames.filter(frame => exit.asked !== undefined && frame.t >= exit.asked)}));

// The exit as drawn: several frames, travelling toward the bar only, the last
// one drawn close to the bar before the sheet goes -- never gone at once.
function expectExit(exit, label, {toward, from = 0} = {}) {
  const travel = exit.frames.map(frame => frame.travelled);
  expect(exit.frames.length, `${label}: frames drawn while closing (${travel.join(' ')})`).toBeGreaterThanOrEqual(4);
  expect(travel[0], `${label}: the exit starts where the panel was drawn`).toBeLessThanOrEqual(Math.max(from, 0) + 0.3);
  for (let i = 1; i < travel.length; i += 1) {
    expect(travel[i], `${label}: the panel moves back toward its bar only (${travel.join(' ')})`).toBeGreaterThanOrEqual(travel[i - 1] - 0.01);
  }
  expect(travel.at(-1), `${label}: the last frame before it hides (${travel.join(' ')})`).toBeGreaterThanOrEqual(0.8);
  expect(exit.frames.filter(frame => frame.travelled > 0.02).every(frame => frame.sign === toward), `${label}: direction`).toBe(true);
}

test.describe('handheld sheets present their whole exit', () => {
  test('More: its control, its scrim, a press outside it, Escape and Tab each roll it back down to the dock before it hides', async ({page}) => {
    await open(page);
    const more = page.locator(SHEETS.more.open);
    const closers = {
      control: () => more.click(),
      scrim: () => page.mouse.click(96, 200),
      outside: () => page.locator('#page-title').click(),
      escape: () => page.keyboard.press('Escape'),
      tab: () => page.keyboard.press('Tab'),
    };
    for (const [label, close] of Object.entries(closers)) {
      await openSheet(page, 'more');
      await watch(page, 'more');
      await close();
      const exit = await exited(page);
      expectExit(exit, `More ${label}`, {toward: 1});
      expect(await page.evaluate(() => ({open: document.getElementById('shell-more').matches(':popover-open'),
        closing: 'dpSheetExit' in document.getElementById('shell-more').dataset,
        expanded: document.querySelector('#shell-dock .dp-shell-dock-more').getAttribute('aria-expanded'),
        view: document.querySelector('.view.active').id})), label).toEqual({open: false, closing: false, expanded: 'false', view: 'view-dashboard'});
      if (['control', 'scrim', 'escape'].includes(label)) await expect(more, label).toBeFocused();
      if (label === 'tab') expect(await page.evaluate(() => !document.getElementById('shell-more').contains(document.activeElement))).toBe(true);
    }
  });

  test('each sheet still opens by travelling out from its own bar', async ({page}) => {
    await open(page);
    for (const sheet of ['more', 'theme', 'cap']) {
      const entry = page.evaluate(([box, panel]) => new Promise(done => {
        const out = [], end = performance.now() + 600;
        const tick = () => {
          const node = document.querySelector(box);
          if (getComputedStyle(node).display !== 'none') out.push(Math.round(new DOMMatrixReadOnly(getComputedStyle(document.querySelector(panel)).transform).m42));
          if (performance.now() < end) requestAnimationFrame(tick); else done(out);
        };
        requestAnimationFrame(tick);
      }), [SHEETS[sheet].box, SHEETS[sheet].panel]);
      await page.locator(SHEETS[sheet].open).click();
      const travel = await entry;
      expect(travel.filter(y => Math.abs(y) > 20).length, `${sheet} opening: ${travel.join(' ')}`).toBeGreaterThanOrEqual(3);
      expect(travel.filter(y => Math.abs(y) > 1).every(y => Math.sign(y) === SHEETS[sheet].toward), `${sheet} opening direction`).toBe(true);
      expect(travel.at(-1), `${sheet} opened at rest`).toBe(0);
      await page.keyboard.press('Escape');
      await expect.poll(() => drawn(page, sheet)).toBe(false);
    }
  });

  test('a page chosen from More opens once More has rolled back down, even when that page takes long to draw; it opens once', async ({page}) => {
    await open(page);
    // The chosen page's own work, made long and fixed (150 ms on the main
    // thread as it opens), and what was drawn of More at that moment.
    await page.evaluate(() => {
      window.__navigations = [];
      document.addEventListener('debridpulse:navigation', event => {
        window.__navigations.push({view: event.detail.view, moreDrawn: getComputedStyle(document.getElementById('shell-more')).display !== 'none'});
        const end = performance.now() + 150;
        while (performance.now() < end) { /* the page's work */ }
      });
    });
    for (const [label, view] of [['Activity Log', 'events'], ['Help & License', 'help']]) {
      await openSheet(page, 'more');
      await watch(page, 'more');
      await page.locator('#shell-more .dp-shell-sheet-item', {hasText: label}).click();
      expectExit(await exited(page), `More -> ${label}`, {toward: 1});
      await expect.poll(() => page.evaluate(() => window.__navigations.length)).toBe(1);
      await page.waitForTimeout(400);
      expect(await page.evaluate(() => window.__navigations.splice(0)), label).toEqual([{view, moreDrawn: false}]);
      await expect(page.locator(`#view-${view}`)).toHaveClass(/\bactive\b/);
      await page.waitForFunction(() => !document.querySelector('.dp-motion-surface'));
      await page.locator('#shell-dock .dp-shell-dock-item[data-view="dashboard"]').click();
      await page.waitForFunction(() => !document.querySelector('.dp-motion-surface'));
      await page.evaluate(() => window.__navigations.splice(0));
    }
  });

  test('More: opened again while closing, it returns from where it is drawn and nothing chosen meanwhile opens; quick choices open only the last; a close before it has opened starts where it is', async ({page}) => {
    await open(page);
    await page.evaluate(() => {
      window.__navigations = [];
      document.addEventListener('debridpulse:navigation', event => window.__navigations.push(event.detail.view));
    });
    const more = page.locator(SHEETS.more.open);
    // Reversed: a page chosen, then More pressed again before it is down.
    await openSheet(page, 'more');
    await page.locator('#shell-more .dp-shell-sheet-item', {hasText: 'Activity Log'}).click();
    await page.waitForFunction(() => {
      const panel = document.querySelector('#shell-more .dp-shell-sheet-panel');
      return new DOMMatrixReadOnly(getComputedStyle(panel).transform).m42 > 20;
    });
    const hidden = page.evaluate(() => new Promise(done => {
      let seen = false;
      const end = performance.now() + 700;
      const tick = () => {
        if (getComputedStyle(document.getElementById('shell-more')).display === 'none') seen = true;
        if (performance.now() < end) requestAnimationFrame(tick); else done(seen);
      };
      requestAnimationFrame(tick);
    }));
    await more.click();
    expect(await hidden, 'More hidden while it was reopened').toBe(false);
    await rested(page, 'more');
    expect(await page.evaluate(() => ({translate: new DOMMatrixReadOnly(getComputedStyle(document.querySelector('#shell-more .dp-shell-sheet-panel')).transform).m42,
      open: document.getElementById('shell-more').matches(':popover-open'), expanded: document.querySelector('#shell-dock .dp-shell-dock-more').getAttribute('aria-expanded'),
      inert: document.querySelector('#shell-more .dp-shell-sheet-panel').inert, navigations: window.__navigations.slice()})))
      .toEqual({translate: 0, open: true, expanded: 'true', inert: false, navigations: []});
    // Quick choices: one from More, then a dock page before More is down: only the last opens, once.
    await page.locator('#shell-more .dp-shell-sheet-item', {hasText: 'Help & License'}).click();
    await page.locator('#shell-dock .dp-shell-dock-item[data-view="stats"]').click();
    await expect.poll(() => drawn(page, 'more')).toBe(false);
    await page.waitForFunction(() => !document.querySelector('.dp-motion-surface'));
    await page.waitForTimeout(300);
    expect(await page.evaluate(() => window.__navigations.splice(0))).toEqual(['stats']);
    // A double press on a page in More opens it once (the closing panel takes no input).
    await openSheet(page, 'more');
    await page.locator('#shell-more .dp-shell-sheet-item', {hasText: 'Activity Log'}).dblclick();
    await expect.poll(() => drawn(page, 'more')).toBe(false);
    await page.waitForTimeout(400);
    expect(await page.evaluate(() => window.__navigations.splice(0))).toEqual(['events']);
    // Closed before it had opened: it starts back from where it is drawn.
    await page.locator('#shell-dock .dp-shell-dock-item[data-view="dashboard"]').click();
    await page.waitForFunction(() => !document.querySelector('.dp-motion-surface'));
    await more.click();
    await page.waitForFunction(() => {
      const panel = document.querySelector('#shell-more .dp-shell-sheet-panel');
      const y = new DOMMatrixReadOnly(getComputedStyle(panel).transform).m42;
      return y > 0 && y < panel.getBoundingClientRect().height * 0.7;
    });
    await watch(page, 'more');
    await page.keyboard.press('Escape');
    const exit = await exited(page);
    const from = exit.at;
    expect(from, 'closed while it was still opening').toBeGreaterThan(0.02);
    const travel = exit.frames.map(frame => frame.travelled);
    // The close takes effect at the next style update, so the entry may still
    // advance by at most one frame (its fastest frame is under 0.2 of the
    // panel); it never goes on to the open rest, and from its lowest point it
    // only travels back.
    const lowest = travel.indexOf(Math.min(...travel));
    expect(lowest, `turns back at once: ${travel.join(' ')}`).toBeLessThanOrEqual(1);
    expect(travel[lowest], `starts where drawn (${from}): ${travel.join(' ')}`).toBeGreaterThanOrEqual(from - 0.2);
    for (let i = lowest + 1; i < travel.length; i += 1) expect(travel[i]).toBeGreaterThanOrEqual(travel[i - 1] - 0.01);
    expect(travel.at(-1)).toBeGreaterThanOrEqual(0.8);
  });

  test('one sheet drawn at a time: asking for another lets the open one finish its exit first, then the other slides in; quick switches end on the last request, nothing else drawn', async ({page}) => {
    await open(page);
    const IDS = {theme: 'theme-picker', more: 'shell-more', cap: 'runtime-cap-menu'};
    // Every frame: which sheets are drawn, and how far each panel is from rest (0) toward its bar (1).
    const record = () => page.evaluate(ids => {
      const out = window.__handoff = {frames: [], start: performance.now()};
      const end = performance.now() + 1500;
      const tick = () => {
        const shown = {};
        for (const [name, id] of Object.entries(ids)) {
          const sheet = document.getElementById(id);
          if (getComputedStyle(sheet).display === 'none') continue;
          const box = sheet.getBoundingClientRect(), part = sheet.firstElementChild.getBoundingClientRect();
          const visible = Math.max(0, Math.min(box.bottom, part.bottom) - Math.max(box.top, part.top));
          shown[name] = Math.round((1 - visible / part.height) * 100) / 100;
        }
        out.frames.push({t: Math.round(performance.now() - out.start), shown});
        if (performance.now() < end) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    }, IDS);
    const recorded = async () => {
      await page.waitForFunction(() => performance.now() - window.__handoff.start > 1500);
      return page.evaluate(() => window.__handoff.frames);
    };
    const centre = async sheet => {
      const box = await page.locator(SHEETS[sheet].open).boundingBox();
      return [box.x + box.width / 2, box.y + box.height / 2];
    };
    const state = () => page.evaluate(ids => Object.fromEntries(Object.entries(ids).map(([name, id]) =>
      [name, getComputedStyle(document.getElementById(id)).display !== 'none'])), IDS);
    // In every frame at most one sheet; `from` leaves first, fully back at its bar, then `to` slides in from its own.
    function expectHandoff(frames, from, to, label) {
      const trace = frames.map(frame => `${frame.t}:${Object.entries(frame.shown).map(([k, v]) => `${k}${v}`).join('+') || '-'}`).join(' ');
      for (const frame of frames) expect(Object.keys(frame.shown).length, `${label}: two sheets drawn at ${frame.t} ms (${trace})`).toBeLessThanOrEqual(1);
      const out = frames.filter(frame => from in frame.shown), into = frames.filter(frame => to in frame.shown);
      expect(out.length, `${label}: ${from} drawn while leaving (${trace})`).toBeGreaterThanOrEqual(4);
      expect(out.at(-1).shown[from], `${label}: ${from} back at its bar before it goes (${trace})`).toBeGreaterThanOrEqual(0.8);
      expect(into.length, `${label}: ${to} drawn`).toBeGreaterThan(0);
      expect(into[0].t, `${label}: ${to} only after ${from} is gone (${trace})`).toBeGreaterThan(out.at(-1).t);
      expect(into[0].shown[to], `${label}: ${to} slides in from its bar (${trace})`).toBeGreaterThanOrEqual(0.5);
      expect(into.at(-1).shown[to], `${label}: ${to} at rest`).toBe(0);
    }
    for (const [from, to] of [['theme', 'more'], ['more', 'theme'], ['theme', 'cap'], ['cap', 'more']]) {
      await openSheet(page, from);
      await record();
      await page.locator(SHEETS[to].open).click();
      expectHandoff(await recorded(), from, to, `${from} -> ${to}`);
      expect(await state(), `${from} -> ${to}`).toEqual({theme: to === 'theme', more: to === 'more', cap: to === 'cap'});
      expect(await page.locator(SHEETS[to].open).getAttribute('aria-expanded'), `${to} expanded`).toBe('true');
      expect(await page.locator(SHEETS[from].open).getAttribute('aria-expanded'), `${from} collapsed`).toBe('false');
      await page.keyboard.press('Escape');
      await expect.poll(state).toEqual({theme: false, more: false, cap: false});
    }
    // Quick: theme, then the cap, then the theme again before the theme is back up -- the theme stays; the cap is never drawn.
    const [capAt, themeAt, moreAt] = [await centre('cap'), await centre('theme'), await centre('more')];
    await openSheet(page, 'theme');
    await record();
    await page.mouse.click(...capAt);
    await page.mouse.click(...themeAt);
    let frames = await recorded();
    let trace = frames.map(frame => `${frame.t}:${Object.keys(frame.shown).join('+') || '-'}`).join(' ');
    expect(frames.every(frame => Object.keys(frame.shown).length === 1 && 'theme' in frame.shown), `theme -> cap -> theme: only the theme, every frame (${trace})`).toBe(true);
    expect(frames.at(-1).shown.theme, 'the theme back at rest').toBe(0);
    expect(await state()).toEqual({theme: true, more: false, cap: false});
    expect(await page.evaluate(() => [document.getElementById('theme-picker-toggle').getAttribute('aria-expanded'),
      document.getElementById('runtime-cap-toggle').getAttribute('aria-expanded')])).toEqual(['true', 'false']);
    // Quick: theme, then the cap, then More -- More opens once the theme is up; the cap is never drawn.
    await record();
    await page.mouse.click(...capAt);
    await page.mouse.click(...moreAt);
    frames = await recorded();
    expectHandoff(frames, 'theme', 'more', 'theme -> cap -> More');
    trace = frames.map(frame => `${frame.t}:${Object.keys(frame.shown).join('+') || '-'}`).join(' ');
    expect(frames.some(frame => 'cap' in frame.shown), `the cap never drawn (${trace})`).toBe(false);
    expect(await state()).toEqual({theme: false, more: true, cap: false});
    // A sheet asked for while another leaves, then dismissed before it is drawn (its own control again): neither stays.
    await record();
    await page.mouse.click(...themeAt);
    await page.mouse.click(...themeAt);
    frames = await recorded();
    trace = frames.map(frame => `${frame.t}:${Object.keys(frame.shown).join('+') || '-'}`).join(' ');
    expect(frames.some(frame => 'theme' in frame.shown), `the theme never drawn (${trace})`).toBe(false);
    expect(await state()).toEqual({theme: false, more: false, cap: false});
  });

  test('the theme menu and the speed-cap menu roll back up to the header before they hide: Escape, the control, a choice', async ({page}) => {
    await open(page);
    for (const [sheet, label, close] of [
      ['theme', 'Escape', () => page.keyboard.press('Escape')],
      ['theme', 'control', () => page.locator(SHEETS.theme.open).click()],
      ['theme', 'a palette', () => page.locator('#theme-picker .dp-theme-picker-option').nth(1).click()],
      ['cap', 'Escape', () => page.keyboard.press('Escape')],
      ['cap', 'control', () => page.locator(SHEETS.cap.open).click()],
      ['cap', 'its scrim', () => page.mouse.click(195, 760)],
    ]) {
      await openSheet(page, sheet);
      await watch(page, sheet);
      await close();
      expectExit(await exited(page), `${sheet} ${label}`, {toward: -1});
      expect(await page.locator(SHEETS[sheet].open).getAttribute('aria-expanded'), `${sheet} ${label}`).toBe('false');
      if (sheet === 'cap') expect(await page.evaluate(() => document.getElementById('runtime-cap-menu').hidden)).toBe(true);
      if (label !== 'its scrim') await expect(page.locator(SHEETS[sheet].open), `${sheet} ${label}: focus back on its control`).toBeFocused();
    }
  });

  test('reduced motion: every sheet closes at once; crossing to 780px closes an open or closing sheet at once and the page chosen still opens once', async ({page}) => {
    await page.emulateMedia({reducedMotion: 'reduce'});
    await open(page);
    for (const sheet of ['more', 'theme', 'cap']) {
      await openSheet(page, sheet);
      await watch(page, sheet);
      await page.keyboard.press('Escape');
      const exit = await exited(page);
      expect(exit.frames.length, `${sheet}: drawn after the close`).toBeLessThanOrEqual(1);
      await expect(page.locator(SHEETS[sheet].open)).toBeFocused();
    }
    await page.emulateMedia({reducedMotion: 'no-preference'});
    await page.evaluate(() => {
      window.__navigations = [];
      document.addEventListener('debridpulse:navigation', event => window.__navigations.push(event.detail.view));
    });
    await openSheet(page, 'more');
    await page.locator('#shell-more .dp-shell-sheet-item', {hasText: 'Activity Log'}).click();
    await page.setViewportSize({width: 780, height: 844});
    await expect.poll(() => page.evaluate(() => window.__navigations.slice())).toEqual(['events']);
    expect(await page.evaluate(() => ({open: document.getElementById('shell-more').matches(':popover-open'),
      closing: 'dpSheetExit' in document.getElementById('shell-more').dataset, inert: document.querySelector('#shell-more .dp-shell-sheet-panel').inert})))
      .toEqual({open: false, closing: false, inert: false});
    await page.setViewportSize({width: 390, height: 844});
    await openSheet(page, 'theme');
    await page.keyboard.press('Escape');
    await page.setViewportSize({width: 780, height: 844});
    expect(await page.evaluate(() => ({open: document.getElementById('theme-picker').matches(':popover-open'),
      closing: 'dpSheetExit' in document.getElementById('theme-picker').dataset}))).toEqual({open: false, closing: false});
  });
});

test('the account sheet rolls back up to the header before it hides, and My Account opens its dialog at once', async ({browser}) => {
  test.skip(!AUTH, 'needs the password-auth runtime (DP_AUTH_BASE_URL)');
  const context = await browser.newContext();
  const page = await context.newPage();
  await noFonts(page);
  await noPulse(page);
  await page.setViewportSize({width: 390, height: 844});
  await page.goto(`${AUTH}/login`);
  await page.locator('#username').fill('browser');
  await page.locator('#password').fill('browser-secret');
  await page.locator('button[type="submit"]').click();
  await expect(page.locator('#topbar-account-button')).toBeVisible();
  for (const [label, close] of [['Escape', () => page.keyboard.press('Escape')], ['its control', () => page.locator(SHEETS.account.open).click()]]) {
    await openSheet(page, 'account');
    expect(await page.locator(SHEETS.account.open).getAttribute('aria-expanded')).toBe('true');
    await watch(page, 'account');
    await close();
    expectExit(await exited(page), `account ${label}`, {toward: -1});
    await expect(page.locator(SHEETS.account.open)).toBeFocused();
    expect(await page.locator(SHEETS.account.open).getAttribute('aria-expanded')).toBe('false');
  }
  await openSheet(page, 'account');
  await page.locator('#topbar-account-sheet .dp-shell-sheet-item', {hasText: 'My Account'}).click();
  await expect(page.locator('.dp-account-dialog')).toBeVisible();
  await expect.poll(() => drawn(page, 'account')).toBe(false);
  expect(await page.evaluate(() => !!document.activeElement.closest('.dp-account-dialog, #modal, [role="dialog"]'))).toBe(true);
  await page.keyboard.press('Escape');
  await context.close();
});
