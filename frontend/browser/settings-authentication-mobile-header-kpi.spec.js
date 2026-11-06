const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Authentication below 780px, and Disk Space & Recovery's handheld
 * disclosure.
 *
 * Headers: each Authentication header is one row whose title may take two
 * lines beside a rail kept whole on one line. They take back room in steps
 * (fitHeaderSteps, data-header-fit-wrap): 1 the informational status leaves,
 * 4 Help is lent to the row at the top of the body, 5 OpenID Connect says
 * OIDC. Test and Enable never leave the header.
 * KPIs: three across while their words fit, else the mode across the row
 * above its two mechanisms, closer, then with short words (fitAuthKpis); the
 * words are authKpiItems', repainted by paintAuthKpis.
 * Disk Space & Recovery: closed when first drawn below 780px, its chip only
 * there; open, without the chip, from 780px; the operator's choice kept.
 *
 * The auth document is SERVED and every write is refused. */

const settle = page => page.evaluate(() => Promise.all(document.getAnimations()
  .filter(animation => Number.isFinite(animation.effect?.getComputedTiming().endTime))
  .map(animation => animation.finished.catch(() => null)))
  .then(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))));

const STATES = {
  open: {},
  oidcOnly: {mode: 'OIDC', authentication_required: true, oidc_enabled: true, oidc_configured: true, oidc_ready: true, oidc_verified: true},
  passwordOnly: {mode: 'Username & Password', authentication_required: true, password_enabled: true, password_ready: true, password_configured: true},
  both: {mode: 'Username & Password + OIDC', authentication_required: true, password_enabled: true, password_ready: true,
    password_configured: true, oidc_enabled: true, oidc_configured: true, oidc_ready: true},
  configuredDisabled: {password_configured: true, oidc_configured: true},
  errors: {mode: 'Username & Password + OIDC', authentication_required: true, password_enabled: true, password_ready: false,
    oidc_enabled: true, oidc_configured: false},
  unavailable: {mode: 'OIDC', authentication_required: true, oidc_enabled: true, oidc_configured: true, oidc_ready: true,
    oidc_verified: true, oidc_available: false},
};

async function open(page, tab, auth = STATES.passwordOnly) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  const live = await page.request.get('/api/auth/config').then(response => response.json());
  page.__writes = [];
  await page.route(url => url.pathname.startsWith('/api/') && !/^\/api\/(events|stream|health)/.test(url.pathname), route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'GET' && path === '/api/auth/config') return route.fulfill({json: {...live, ...auth}});
    if (request.method() === 'GET' && path === '/api/auth/oidc/runtime-status') {
      return route.fulfill({json: {oidc_available: auth.oidc_available ?? true}});
    }
    if (request.method() === 'GET' || /\/test-aria2$/.test(path)) return route.fallback();
    page.__writes.push(`${request.method()} ${path}`);
    return route.abort();
  });
  await page.setViewportSize({width: 1280, height: 900});
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await page.locator(`#view-settings [data-tab="${tab}"]`).first().click();
  await expect(page.locator(`#view-settings [data-panel="${tab}"]`)).toBeVisible();
  await settle(page);
}

async function resize(page, width) {
  await page.setViewportSize({width, height: 900});
  await settle(page);
}

const CARDS = {status: '.dp-settings-auth-status-card', password: '.dp-settings-username-password-card', oidc: '.dp-settings-oidc-card'};

// One Authentication header as drawn.
const header = (page, selector) => page.locator(selector).evaluate(card => {
  const r = node => node && node.getClientRects().length ? node.getBoundingClientRect() : null;
  const hit = (a, b) => !!(a && b && a.left < b.right - 0.5 && a.right > b.left + 0.5 && a.top < b.bottom - 0.5 && a.bottom > b.top + 0.5);
  const head = card.querySelector(':scope > .card-header');
  const group = head.querySelector(':scope > .dp-settings-card-title-group');
  const rail = head.querySelector(':scope > .dp-settings-card-header-controls');
  // The title as drawn: the short one where it is drawn, else the full one.
  const short = head.querySelector('.dp-settings-card-title-short');
  const drawnShort = !!(short && r(short));
  const full = head.querySelector('.dp-settings-card-title-full') || head.querySelector('.dp-settings-card-title-text');
  // The short title's words are drawn by the stylesheet (its ::before), so its
  // box is the line; the full title's lines are its text's.
  const range = document.createRange();
  range.selectNodeContents(full);
  const lines = drawnShort ? [r(short)] : [...range.getClientRects()].filter(box => box.width > 1 && box.height > 8);
  const help = card.querySelector('.dp-settings-help-chip');
  const status = rail.querySelector(':scope > .dp-settings-provider-config-status');
  const test = rail.querySelector('.dp-settings-header-test-trigger');
  const toggle = rail.querySelector('.toggle');
  // The body box the card lends Help to: a row of its own (Authentication
  // Status, Username & Password), or OpenID Connect's identity group.
  const slot = card.querySelector(':scope > .card-body .dp-settings-help-slot');
  const parts = [help, test, toggle, status].map(r).filter(Boolean);
  return {
    step: card.dataset.headerFit,
    shown: drawnShort ? short.dataset.shortTitle : full.textContent.trim(),
    text: head.querySelector('.dp-settings-card-title-text').textContent.trim(),
    lines: new Set(lines.map(box => Math.round(box.bottom))).size,
    collides: lines.some(box => parts.some(part => hit(box, part))),
    oneRow: r(rail).top < r(group).bottom && r(rail).left >= r(group).right - 0.5,
    railRows: new Set([help, test, toggle, status].filter(node => node && rail.contains(node)).map(r).filter(Boolean)
      .map(p => Math.round((p.top + p.bottom) / 2 / 4))).size,
    overflow: head.scrollWidth > head.clientWidth + 1 || group.scrollWidth > group.clientWidth + 1,
    helpIn: help.parentElement === rail ? 'header' : help.parentElement === slot ? 'body' : 'elsewhere',
    // Lent, it sits at its box's trailing edge, covering nothing in the body:
    // above everything after a row, clear of every control in a group.
    helpCorner: help.parentElement === slot ? {right: r(slot).right - r(help).right - (slot.matches('.dp-settings-help-slot--row') ? 0 : 12),
      aboveNext: slot.matches('.dp-settings-help-slot--row') ? r(help).bottom <= r(slot.nextElementSibling).top
        : [...slot.querySelectorAll('.input, .dp-action-field')].map(r)
          .concat([...slot.querySelectorAll('.form-label')].flatMap(label => {
            const words = document.createRange();
            words.selectNodeContents(label);
            return [...words.getClientRects()];
          })).every(box => !hit(box, r(help)))} : null,
    status: status ? !!r(status) && r(status).width > 2 : null,
    test: test ? (rail.contains(test) && !!r(test)) : null,
    toggle: !!toggle && rail.contains(toggle) && !!r(toggle),
    testToggleRow: test && toggle ? Math.abs((r(test).top + r(test).bottom) - (r(toggle).top + r(toggle).bottom)) / 2 <= 2 : true,
  };
});

test('every Authentication header is one row with its title beside a whole rail, taking room back in order and giving it back', async ({page}) => {
  await open(page, 'authentication');
  const widths = [779, 720, 640, 560, 480, 430, 390, 360, 340, 320, 300, 280, 260];
  const seen = {};
  for (const width of [...widths, ...widths.slice(0, -1).reverse()]) {
    await resize(page, width);
    for (const [name, selector] of Object.entries(CARDS)) {
      const h = await header(page, selector);
      const at = `${name} @${width} (step ${h.step})`;
      expect(h.collides, `${at}: title under a control`).toBe(false);
      expect(h.oneRow, `${at}: rail left the row`).toBe(true);
      expect(h.railRows, `${at}: rail wrapped`).toBeLessThanOrEqual(1);
      expect(h.overflow, `${at}: overflow`).toBe(false);
      expect(h.toggle || name === 'status', `${at}: Enable left the header`).toBe(true);
      if (name === 'oidc') expect([h.test, h.testToggleRow], `${at}: Test and Enable`).toEqual([true, true]);
      // The informational status leaves from step 1; Help is lent from 4.
      // A card with the handheld Test group carries its report on Test at every
      // handheld width (headerTestControls); another drops it from step 1.
      if (h.status !== null) expect(h.status, `${at}: status`).toBe(h.test === null && h.step === '0');
      expect(h.helpIn, `${at}: Help`).toBe(Number(h.step) >= 4 ? 'body' : 'header');
      if (h.helpIn === 'body') {
        expect(h.helpCorner.right, `${at}: lent Help not at the trailing edge`).toBeLessThanOrEqual(12.5);
        expect(h.helpCorner.aboveNext, `${at}: lent Help over body content`).toBe(true);
      }
      if (width >= 320) expect(h.lines, `${at}: title lines`).toBeLessThanOrEqual(2);
      const key = `${name}@${width}`;
      if (key in seen) expect(h, `${at}: narrowing and widening differ`).toEqual(seen[key]);
      else seen[key] = h;
    }
  }
  // The supported range, in these fonts: the full titles throughout, the
  // password status kept while it fits and given up only where it must, and
  // OpenID Connect two lines beside Test and Enable at 320px, Help lent.
  for (const width of [779, 430]) {
    expect(seen[`password@${width}`].status, `password status @${width}`).toBe(true);
  }
  expect(seen['password@320'].status).toBe(false);
  expect(seen['oidc@320']).toMatchObject({shown: 'OpenID Connect', lines: 2, helpIn: 'body', test: true});
  expect(seen['status@320']).toMatchObject({shown: 'Authentication Status', lines: 2, helpIn: 'header'});
  // The last resort, only below the supported width here.
  for (const width of [779, 560, 390, 320]) expect(seen[`oidc@${width}`].shown, `oidc @${width}`).toBe('OpenID Connect');
  expect(seen['oidc@280']).toMatchObject({shown: 'OIDC', step: '5', text: 'OpenID Connect'});
  expect(page.__writes).toEqual([]);
});

test('the lent Help, the short title and the header controls keep their names and their behaviour', async ({page}) => {
  await open(page, 'authentication');
  await resize(page, 280);
  const card = page.locator(CARDS.oidc);
  const help = card.locator('.dp-settings-help-chip');
  await expect(card.locator(':scope > .card-body > .dp-settings-oidc-group--identity > .dp-settings-help-chip')).toHaveCount(1);
  await expect(help).toHaveAccessibleName('Help: OpenID Connect');
  // The card is still OpenID Connect to assistive technology; the drawn short
  // words are not in its tree and are not a tooltip host or a tab stop.
  await expect(card.locator('.dp-settings-card-title-full')).toHaveText('OpenID Connect');
  const short = card.locator('.dp-settings-card-title-short');
  await expect(short).toHaveAttribute('aria-hidden', 'true');
  for (const attribute of ['data-tooltip', 'tabindex']) await expect(short).not.toHaveAttribute(attribute, /.*/);
  // The title carries the full title as its tooltip while the short one is drawn.
  const title = card.locator('.dp-settings-card-title-text');
  await expect(title).toHaveAttribute('data-tooltip', 'OpenID Connect');
  await expect(title).toHaveAttribute('data-tooltip-touch', '');
  await expect(title).toHaveAttribute('tabindex', '0');
  // Its own text -- what is read at it -- is the full title, exposed.
  await expect(title).toHaveText('OpenID Connect');
  await expect(title).not.toHaveAttribute('aria-hidden', /.*/);
  await expect(card.locator('.dp-settings-card-title-full')).not.toHaveAttribute('aria-hidden', /.*/);
  await title.hover();
  await expect(page.locator('#dp-tooltip')).toHaveText('OpenID Connect');
  await page.mouse.move(2, 2);
  await expect(page.locator('#dp-tooltip')).toBeHidden();
  // Keyboard: the title is the stop before Test; its tooltip shows on focus,
  // Escape dismisses it, and the title keeps focus.
  await card.locator('.dp-settings-header-test-trigger').focus();
  await page.keyboard.press('Shift+Tab');
  await expect(title).toBeFocused();
  await expect(page.locator('#dp-tooltip')).toHaveText('OpenID Connect');
  await page.keyboard.press('Escape');
  await expect(page.locator('#dp-tooltip')).toBeHidden();
  await expect(title).toBeFocused();
  await expect(card.locator('.dp-settings-header-test-trigger')).toHaveAccessibleName('Test OpenID Connect');
  await expect(card.locator('[data-setting="auth_oidc_enabled"]')).toHaveAccessibleName('Enable OpenID Connect');
  // The lent Help is the same control: keyboard reaches it and it opens its own topic.
  await help.focus();
  await page.keyboard.press('Enter');
  const dialog = page.locator('.dp-modal-overlay .dp-modal-dialog').last();
  await expect(dialog).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  // Given back where the room returns, focus and all; the full title drawn,
  // the title is a plain title again: no tooltip, no tab stop.
  for (const width of [779, 390, 320]) {
    await resize(page, width);
    await expect(card.locator('.dp-settings-card-title-short'), `@${width}`).toBeHidden();
    for (const attribute of ['data-tooltip', 'data-tooltip-touch', 'tabindex']) {
      await expect(title, `@${width}`).not.toHaveAttribute(attribute, /.*/);
    }
    await title.hover();
    await page.waitForTimeout(600);
    await expect(page.locator('#dp-tooltip'), `@${width}: a redundant tooltip`).toBeHidden();
    await page.mouse.move(2, 2);
  }
  await expect(card.locator(':scope > .card-header .dp-settings-help-chip')).toHaveCount(0);
  await resize(page, 779);
  await expect(card.locator(':scope > .card-header .dp-settings-help-chip')).toHaveCount(1);
  expect(page.__writes).toEqual([]);
});

test.describe('on a touch screen', () => {
  test.use({hasTouch: true});

  test('a tap on the short OIDC title shows the full title and activates nothing; a tap elsewhere dismisses it', async ({page}) => {
    await open(page, 'authentication');
    await resize(page, 280);
    const card = page.locator(CARDS.oidc);
    const title = card.locator('.dp-settings-card-title-text');
    await expect(card.locator('.dp-settings-card-title-short')).toBeVisible();
    const enable = card.locator('[data-setting="auth_oidc_enabled"]');
    const before = await enable.isChecked();
    await title.tap();
    await expect(page.locator('#dp-tooltip')).toBeVisible();
    await expect(page.locator('#dp-tooltip')).toHaveText('OpenID Connect');
    // Inside the viewport, and still there a moment later.
    const box = await page.locator('#dp-tooltip').boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(8);
    expect(box.x + box.width).toBeLessThanOrEqual(280 - 8);
    await page.waitForTimeout(1200);
    await expect(page.locator('#dp-tooltip')).toBeVisible();
    // A tap elsewhere dismisses it; nothing was toggled or written.
    await page.locator('.dp-settings-auth-status-card .dp-settings-card-title-text').tap();
    await expect(page.locator('#dp-tooltip')).toBeHidden();
    await expect(enable).toBeChecked({checked: before});
    // Restored: no tooltip on a tap at a width that draws the full title.
    await resize(page, 390);
    await title.tap();
    await page.waitForTimeout(300);
    await expect(page.locator('#dp-tooltip')).toBeHidden();
    expect(page.__writes).toEqual([]);
  });
});

// The KPI grid as drawn.
const kpis = page => page.locator('.dp-settings-auth-kpi-grid').evaluate(grid => {
  const r = node => node.getBoundingClientRect();
  // The words drawn: the short ones where they are, else the full ones.
  const drawn = box => {
    const short = box.querySelector(':scope > .dp-settings-kpi-words--short');
    const words = short && short.getClientRects().length ? short : box.querySelector(':scope > .dp-settings-kpi-words--full');
    const rest = [...box.querySelectorAll(':scope > .dp-settings-auth-kpi-secondary')].map(node => node.textContent.trim());
    return [(words || box).textContent.trim(), ...(words ? rest : [])].join('\n');
  };
  const g = r(grid);
  return {
    fit: grid.dataset.kpiFit, words: grid.dataset.kpiWords,
    grid: g.width,
    cards: [...grid.querySelectorAll('.dp-settings-auth-kpi')].map(kpi => ({
      id: kpi.dataset.authKpi, left: r(kpi).left - g.left, top: r(kpi).top - g.top, width: r(kpi).width, height: r(kpi).height,
      overflow: kpi.querySelector('.dhs-body').scrollWidth > kpi.querySelector('.dhs-body').clientWidth + 1,
      inside: r(kpi).left >= g.left - 0.5 && r(kpi).right <= g.right + 0.5,
      label: drawn(kpi.querySelector('.dhs-label')), value: drawn(kpi.querySelector('.dhs-val')),
      fullValue: (kpi.querySelector('.dhs-val .dp-settings-kpi-words--full') || kpi.querySelector('.dhs-val')).textContent.trim(),
    })),
  };
});

for (const [name, auth] of Object.entries(STATES)) {
  test(`KPIs (${name}): three across where they fit, else the mode above its two mechanisms, all three always drawn`, async ({page}) => {
    await open(page, 'authentication', auth);
    for (const width of [779, 640, 560, 520, 480, 430, 390, 360, 320, 360, 480, 560, 779]) {
      await resize(page, width);
      const k = await kpis(page);
      const at = `${name} @${width} (${k.fit}/${k.words})`;
      expect(k.cards.map(card => card.id), at).toEqual(['mode', 'password', 'oidc']);
      for (const card of k.cards) {
        expect(card.inside, `${at}: ${card.id} past the grid`).toBe(true);
        expect(card.overflow, `${at}: ${card.id} words overflow`).toBe(false);
      }
      const [mode, password, oidc] = k.cards;
      if (k.fit === 'row') {
        expect(Math.max(...k.cards.map(c => c.width)) - Math.min(...k.cards.map(c => c.width)), `${at}: unequal widths`).toBeLessThanOrEqual(1);
        expect(Math.max(...k.cards.map(c => c.height)) - Math.min(...k.cards.map(c => c.height)), `${at}: unequal heights`).toBeLessThanOrEqual(1);
        expect(new Set(k.cards.map(c => Math.round(c.top))).size, `${at}: not one row`).toBe(1);
      } else {
        expect(['split', 'close'], at).toContain(k.fit);
        expect(Math.abs(mode.width - k.grid), `${at}: mode not full width`).toBeLessThanOrEqual(1);
        expect(password.top, `${at}: mechanisms not beneath the mode`).toBeGreaterThan(mode.top + mode.height - 1);
        expect(Math.abs(password.top - oidc.top), at).toBeLessThanOrEqual(1);
        expect(password.left, `${at}: password not on the left`).toBeLessThan(oidc.left);
        expect(Math.abs(password.width - oidc.width), `${at}: unequal halves`).toBeLessThanOrEqual(1);
        expect(Math.abs(password.height - oidc.height), `${at}: unequal heights`).toBeLessThanOrEqual(1);
      }
      // Full words throughout the supported range in these fonts.
      expect(k.words, at).toBe('full');
      if (width === 779) expect(k.fit, at).toBe('row');
      if (width === 320) expect(k.fit, at).not.toBe('row');
    }
    expect(page.__writes).toEqual([]);
  });
}

test('the short KPI words, below the supported width, say the same state and are repainted with it', async ({page}) => {
  // OIDC enabled but its provider unreachable: the runtime probe repaints the
  // OIDC reading after the page has drawn it.
  await open(page, 'authentication', STATES.unavailable);
  await resize(page, 260);
  await expect.poll(async () => (await kpis(page)).cards[2].fullValue).toBe('Runtime Unavailable');
  const k = await kpis(page);
  expect([k.fit, k.words]).toEqual(['close', 'short']);
  expect(k.cards.map(card => card.label)).toEqual(['Auth Mode', 'Password', 'OIDC']);
  expect(k.cards[2].value).toBe('Unavailable\n(Verified)');
  // The full words are still the reading's text.
  await expect(page.locator('[data-auth-kpi="oidc"] .dhs-val .dp-settings-kpi-words--full')).toHaveText('Runtime Unavailable');
  await expect(page.locator('[data-auth-kpi="oidc"] .dhs-label .dp-settings-kpi-words--full')).toHaveText('OIDC State');
  await expect(page.locator('[data-auth-kpi="oidc"] .dhs-val .dp-settings-kpi-words--short')).toHaveAttribute('aria-hidden', 'true');
  // Given back with the room.
  await resize(page, 779);
  const wide = await kpis(page);
  expect([wide.fit, wide.words]).toEqual(['row', 'full']);
  expect(wide.cards[2].value).toBe('Runtime Unavailable\n(Verified)');
});

test('Disk Space & Recovery starts closed below 780px and open above it, and keeps the operator\'s choice across the boundary', async ({page}) => {
  const card = '.dp-settings-download-recovery-card';
  const state = () => page.locator(card).evaluate(node => {
    const chip = node.querySelector(':scope > .card-header .dp-settings-disclosure');
    const body = node.querySelector(':scope > .card-body');
    return {chip: !!chip && chip.getClientRects().length > 0, expanded: chip?.getAttribute('aria-expanded'),
      body: body.getClientRects().length > 0 && body.getBoundingClientRect().height > 0,
      controls: chip?.getAttribute('aria-controls') === body.id};
  });
  // First drawn at a desktop width: open, no chip.
  await open(page, 'downloads');
  expect(await state()).toMatchObject({chip: false, body: true, controls: true});
  // Below 780px, untouched: closed.
  await resize(page, 390);
  expect(await state()).toMatchObject({chip: true, expanded: 'false', body: false});
  // Opened by keyboard, kept across the boundary both ways.
  const chip = page.locator(`${card} > .card-header .dp-settings-disclosure`);
  await expect(chip).toHaveAccessibleName('Expand Disk Space & Recovery');
  await chip.focus();
  await page.keyboard.press('Enter');
  await settle(page);
  expect(await state()).toMatchObject({expanded: 'true', body: true});
  await resize(page, 1280);
  expect(await state()).toMatchObject({chip: false, body: true});
  await resize(page, 390);
  expect(await state()).toMatchObject({chip: true, expanded: 'true', body: true});
  // Closed by pointer: still open from 780px, closed again below it.
  await chip.click();
  await settle(page);
  expect(await state()).toMatchObject({expanded: 'false', body: false});
  await resize(page, 1280);
  expect(await state()).toMatchObject({chip: false, body: true});
  await resize(page, 390);
  expect(await state()).toMatchObject({expanded: 'false', body: false});
  // Its settings are all still there.
  await chip.click();
  await settle(page);
  await expect(page.locator(`${card} [data-setting="min_free_disk_gb"]`)).toBeVisible();
  expect(page.__writes).toEqual([]);
});
