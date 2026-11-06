const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Premium Services handheld headers: ONE row at every width, every
 * title in full, with the least compression the room needs.
 *
 * Below 780px each Premium Services provider header is identity, full title,
 * disclosure, then the Test group with its toggle, and Help where it fits.
 * The row never wraps and the title never shrinks: the group's
 * data-header-fit step (fitHeaderSteps, in fitHeaderHints) takes back the
 * gaps and the Services panel padding (1), the header and group-body padding
 * (2), the identity marks (3), lends a header's Help -- the SAME element --
 * to its card's body slot wherever that header still does not fit (4), and
 * the last of the group-body padding (5), choosing the first step at which
 * every header fits in the rendered layout, and giving each back as soon as
 * the room holds it. The lent chip covers nothing its body shows, the API
 * Key and Account Connection boxes keep their geometry, and an Account
 * Connection rebuilt by a state change carries the chip with it.
 *
 * Every settings, status and authorization answer this file needs is served
 * here, so it writes no canonical state and owns no settings key. */

const DEBRID = ['alldebrid', 'debridlink', 'premiumize', 'realdebrid', 'torbox'];
const ALL = ['usenet', ...DEBRID];
const TITLES = {usenet: 'Usenet', alldebrid: 'AllDebrid', debridlink: 'Debrid-Link', premiumize: 'Premiumize',
                realdebrid: 'Real-Debrid', torbox: 'TorBox'};
const EXPIRES = Date.parse('2027-03-14T12:00:00Z') / 1000;
const PREMIUM = {entitlement: 'ready', service_class: 'premium', functional: 'usable', plan: 'Premium',
                 request_types: ['direct_link'], expires_at: EXPIRES};
const TB_PENDING = {state: 'pending', user_code: 'TB1234', verification_url: 'https://torbox.app/oauth/device',
                    interval: 5, expires_in: 600};
const RD_PENDING = {state: 'pending', user_code: 'ABCD1234', verification_url: 'https://real-debrid.com/device',
                    interval: 5, expires_in: 600};
const RD_ACCOUNT = {username: 'alice', account_type: 'premium', premium: true, premium_seconds: 9,
                    expiration: '2027-01-31T10:00:00.000Z',
                    account: {entitlement: 'ready', service_class: 'premium', functional: 'usable', plan: 'Premium',
                              expires_at: Date.parse('2027-01-31T10:00:00.000Z') / 1000}};
const rdProjection = connected => ({
  enabled: true, configured: connected, verified: connected,
  options: {client_id: '', client_id_configured: connected, client_secret: '', client_secret_configured: connected,
            refresh_token: '', refresh_token_configured: connected, rate_limit_per_minute: 240},
  presentation: {status_name: 'Real-Debrid', premium: true, status_endpoint: '/integration-status/realdebrid',
                 display_order: 11, status_tier: 'premium_service', status_tier_label: 'Premium Services'},
});

async function prepare(page, {connected = false, lifecycle = false} = {}) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.addInitScript(() => { window.open = () => null; });
  if (connected) {
    // AllDebrid keyed with a premium account (its line above the key box),
    // Real-Debrid connected with a premium expiry in its reserved row.
    await page.route(url => url.pathname === '/api/settings', async route => {
      if (route.request().method() !== 'GET') return route.continue();
      const body = await (await route.fetch()).json();
      const keyed = (id, options) => ({...body.integrations[id], enabled: true, configured: true, verified: true,
        options: {...(body.integrations[id]?.options || {}), ...options}});
      body.integrations = {...body.integrations,
        alldebrid: keyed('alldebrid', {api_key: '', api_key_configured: true}),
        realdebrid: keyed('realdebrid', rdProjection(true).options)};
      return route.fulfill({json: body});
    });
    await page.route(url => /^\/api\/integration-status\/(alldebrid|realdebrid)$/.test(url.pathname), route => {
      const id = new URL(route.request().url()).pathname.split('/').pop();
      return route.fulfill({json: {integration: id, state: 'healthy', checked: true, username: 'alice', account: PREMIUM}});
    });
    // TorBox waits for its authorization.
    await page.route(url => url.pathname.startsWith('/api/integrations/torbox/'), route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (path.endsWith('/authorization/poll')) return route.fulfill({json: TB_PENDING});
      if (path.endsWith('/authorization')) return route.fulfill({json: request.method() === 'POST' ? TB_PENDING : {state: 'idle'}});
      return route.fulfill({json: {ok: true}});
    });
  }
  if (lifecycle) {
    // Real-Debrid's own device authorization: pending, then connected on the
    // first poll; a disconnect leaves it unconfigured.
    await page.route(url => url.pathname.startsWith('/api/integrations/realdebrid/'), route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (path.endsWith('/authorization/poll')) {
        return route.fulfill({json: {state: 'connected', ...RD_ACCOUNT, integration_id: 'realdebrid', integration: rdProjection(true)}});
      }
      if (path.endsWith('/authorization')) return route.fulfill({json: request.method() === 'POST' ? RD_PENDING : {state: 'idle'}});
      if (path.endsWith('/disconnect')) {
        return route.fulfill({json: {ok: true, revoked: true, integration_id: 'realdebrid', integration: rdProjection(false)}});
      }
      return route.fulfill({json: {ok: true, ...RD_ACCOUNT}});
    });
    await page.clock.install();
  }
  await page.goto('/');
  await page.waitForFunction(() => typeof window.nav === 'function' && typeof window.loadSettings === 'function');
  await page.evaluate(() => window.nav(document.querySelector('#sidebar .nav-item[data-view="settings"]')));
  await page.locator('#view-settings .dp-settings-tabs [data-tab="sources"]:not(.dp-motion-still *)').first()
    .dispatchEvent('click');
  await expect(page.locator('#view-settings [data-panel="sources"]')).toBeVisible();
  await page.evaluate(() => document.querySelectorAll(
    '#view-settings [data-panel="sources"] .dp-settings-group-card > .card-header .dp-settings-disclosure[aria-expanded="false"]')
    .forEach(button => button.click()));
  if (connected) {
    await page.evaluate(() => window.DPProviderStatus?.refresh());
    await expect(card(page, 'alldebrid').locator('[data-alldebrid-account] .dp-settings-account-expiry')).not.toBeEmpty();
    await expect(card(page, 'realdebrid').locator('.dp-settings-account-expiry')).not.toBeEmpty();
  }
}

const card = (page, id) => page.locator(`#view-settings .dp-settings-provider-card--${id}`);
const step = page => page.locator('#view-settings .dp-settings-debrid-services').getAttribute('data-header-fit');

async function width(page, value) {
  await page.setViewportSize({width: value, height: 900});
  // The fit is the ResizeObserver's: settled after two frames.
  await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
}

async function expand(page, ids) {
  for (const id of ids) {
    const disclosure = card(page, id).locator(':scope > .card-header .dp-settings-disclosure');
    if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
    await expect(card(page, id).locator(':scope > .card-body')).toBeVisible();
  }
  await page.evaluate(() => Promise.all([...document.querySelectorAll('#view-settings .dp-settings-provider-card > .card-body')]
    .flatMap(body => body.getAnimations().map(motion => motion.finished))));
}

// Each header as drawn: its one Help's place, one row, the full title, and
// whether it would fit with Help in its rail (the chip at its own width).
const headers = page => page.evaluate(ids => Object.fromEntries(ids.map(id => {
  const section = document.querySelector(`#view-settings .dp-settings-provider-card--${id}`);
  const header = section.querySelector(':scope > .card-header');
  const style = getComputedStyle(header);
  const group = header.querySelector(':scope > .dp-settings-card-title-group');
  const title = group.getBoundingClientRect();
  const text = group.querySelector('.dp-settings-card-title-text');
  const rail = header.querySelector(':scope > .dp-settings-card-header-controls');
  const railBox = rail.getBoundingClientRect();
  const chips = document.querySelectorAll(`#view-settings [data-settings-help="${id}"]`);
  const chip = chips[0];
  const inRail = chip?.parentElement === rail;
  const help = 34 + parseFloat(getComputedStyle(rail).columnGap);
  const right = header.getBoundingClientRect().left + header.clientLeft + header.clientWidth - parseFloat(style.paddingRight);
  const room = header.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight)
    - title.width - parseFloat(style.columnGap);
  const without = railBox.width - (inRail ? help : 0);
  const range = document.createRange(); range.selectNodeContents(text);
  return [id, {count: chips.length,
    place: !chip ? 'missing' : inRail ? 'header' : chip.closest('.card-body .dp-settings-help-slot') ? 'body' : 'elsewhere',
    oneRow: Math.abs((railBox.top + railBox.bottom) / 2 - (title.top + title.bottom) / 2) < 2,
    // The title's whole text run fits its own box, so the shared title
    // clipping (overflow hidden, text-overflow ellipsis) never elides it.
    text: text.textContent, fullTitle: range.getBoundingClientRect().width <= text.getBoundingClientRect().width + 0.5,
    clearance: railBox.left - title.right - parseFloat(style.columnGap), withinRow: railBox.right <= right + 0.5,
    fitsWith: without + help <= room, overflow: header.scrollWidth - header.clientWidth, height: header.getBoundingClientRect().height}];
})), ALL);

// Every drawn header part, pairwise: no two intersect (containment aside),
// none leaves the header, each control is the element at its own centre and
// keeps its own size.
const collisions = page => page.evaluate(ids => {
  const found = [];
  for (const id of ids) {
    const header = document.querySelector(`#view-settings .dp-settings-provider-card--${id} > .card-header`);
    header.scrollIntoView({block: 'center'});
    const parts = {
      chip: header.querySelector('.dp-settings-protocol-chip, .dp-settings-provider-chip'),
      title: header.querySelector('.dp-settings-card-title-text'),
      disclosure: header.querySelector('.dp-settings-disclosure'),
      help: header.querySelector('.dp-settings-help-chip'),
      test: header.querySelector('.dp-settings-header-test-trigger'),
      toggle: header.querySelector('.dp-settings-header-test-group .toggle'),
    };
    const sizes = {disclosure: [32, 32], help: [34, 28], test: [null, 30]};
    const box = header.getBoundingClientRect();
    const boxes = Object.entries(parts).filter(([, n]) => n && n.getClientRects().length)
      .map(([name, n]) => [name, n, n.getBoundingClientRect()]);
    for (const [name, n, b] of boxes) {
      if (b.left < box.left - 0.5 || b.right > box.right + 0.5) found.push(`${id} ${name} outside the header`);
      const [w, h] = sizes[name] || [];
      if ((w && Math.abs(b.width - w) > 0.5) || (h && Math.abs(b.height - h) > 0.5)) found.push(`${id} ${name} ${b.width}x${b.height}`);
      if (['disclosure', 'help', 'test', 'toggle'].includes(name)) {
        const hit = document.elementFromPoint((b.left + b.right) / 2, (b.top + b.bottom) / 2);
        if (!n.contains(hit) && !hit?.contains(n)) found.push(`${id} ${name} not reachable at its centre`);
      }
    }
    for (let i = 0; i < boxes.length; i += 1) for (let j = i + 1; j < boxes.length; j += 1) {
      const [an, a, ab] = boxes[i]; const [bn, b, bb] = boxes[j];
      if (a.contains(b) || b.contains(a)) continue;
      const w = Math.min(ab.right, bb.right) - Math.max(ab.left, bb.left);
      const h = Math.min(ab.bottom, bb.bottom) - Math.max(ab.top, bb.top);
      if (w > 0.5 && h > 0.5) found.push(`${id} ${an} ~ ${bn} ${w.toFixed(1)}x${h.toFixed(1)}`);
    }
  }
  if (document.documentElement.scrollWidth > document.documentElement.clientWidth) found.push('page overflows horizontally');
  return found;
}, ALL);

test('every Premium Services header is one row with its full title at every required width', async ({page}) => {
  await prepare(page);
  const ledger = {};
  for (const value of [320, 360, 375, 390, 440, 779, 780]) {
    await width(page, value);
    const state = await headers(page);
    for (const id of ALL) {
      const s = state[id];
      const at = `${id} ${value}px`;
      expect(s.count, at).toBe(1);
      expect(s.oneRow, at).toBe(true);
      expect(s.withinRow, at).toBe(true);
      expect(s.overflow, at).toBeLessThanOrEqual(0);
      expect(s.text, at).toBe(TITLES[id]);
      expect(s.fullTitle, at).toBe(true);
      expect(s.clearance, at).toBeGreaterThanOrEqual(0);
    }
    expect(await collisions(page), `${value}px`).toEqual([]);
    ledger[value] = {step: await step(page), lent: ALL.filter(id => state[id].place === 'body')};
  }
  // The least compression the room needs, given back as the room grows.
  expect(ledger[320]).toEqual({step: '4', lent: ['alldebrid', 'debridlink', 'premiumize', 'realdebrid']});
  expect(ledger[360]).toEqual({step: '2', lent: []});
  expect(ledger[440]).toEqual({step: '0', lent: []});
  expect(ledger[779]).toEqual({step: '0', lent: []});
});

test('each step is the least that fits, Help is lent only where its header needs it, and nothing moves at a stable width', async ({page}) => {
  await prepare(page);
  await expand(page, ALL);
  await page.evaluate(ids => { window.__chips = ids.map(id => document.querySelector(`[data-settings-help="${id}"]`)); }, ALL);
  for (const value of [440, 410, 390, 360, 340, 320, 340, 360, 410, 440]) {
    await width(page, value);
    const chosen = Number(await step(page));
    const state = await headers(page);
    for (const id of ALL) {
      expect(state[id].count, `${id} ${value}px`).toBe(1);
      expect(state[id].oneRow, `${id} ${value}px`).toBe(true);
      expect(state[id].place, `${id} ${value}px`).toBe(chosen >= 4 && !state[id].fitsWith ? 'body' : 'header');
    }
    // One step less is a step at which some header would not fit (read in
    // the page with the lighter step drawn, then put back).
    if (chosen > 0) {
      const lighterFits = await page.evaluate(([ids, lighter]) => {
        const group = document.querySelector('#view-settings .dp-settings-debrid-services');
        const kept = group.dataset.headerFit;
        group.dataset.headerFit = String(lighter);
        const fits = ids.every(id => {
          const header = document.querySelector(`#view-settings .dp-settings-provider-card--${id} > .card-header`);
          const style = getComputedStyle(header);
          const rail = header.querySelector(':scope > .dp-settings-card-header-controls');
          const title = header.querySelector(':scope > .dp-settings-card-title-group').getBoundingClientRect().width;
          const chip = document.querySelector(`[data-settings-help="${id}"]`);
          const help = 34 + parseFloat(getComputedStyle(rail).columnGap);
          const without = rail.getBoundingClientRect().width - (chip.parentElement === rail ? help : 0);
          const room = header.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight) - title - parseFloat(style.columnGap);
          const hasSlot = !!header.parentElement.querySelector(':scope > .card-body .dp-settings-help-slot');
          return without + help <= room || (lighter >= 4 && hasSlot && without <= room);
        });
        group.dataset.headerFit = kept;
        return fits;
      }, [ALL, chosen - 1]);
      expect(lighterFits, `step ${chosen - 1} at ${value}px`).toBe(false);
    }
    expect(await collisions(page), `${value}px`).toEqual([]);
  }
  expect(await page.evaluate(ids => ids.every((id, n) => document.querySelector(`[data-settings-help="${id}"]`) === window.__chips[n]), ALL)).toBe(true);

  // At a stable width the step and the chips settle: no further moves.
  for (const value of [359, 360, 361, 409, 410]) {
    await width(page, value);
    const moves = await page.evaluate(() => new Promise(done => {
      let count = 0;
      const group = document.querySelector('#view-settings .dp-settings-debrid-services');
      const observer = new MutationObserver(records => { count += records.length; });
      observer.observe(group, {attributes: true, attributeFilter: ['data-header-fit']});
      group.querySelectorAll('.dp-settings-provider-card').forEach(n => observer.observe(n, {childList: true, subtree: true}));
      setTimeout(() => { observer.disconnect(); done(count); }, 400);
    }));
    expect(moves, `${value}px`).toBe(0);
  }
});

for (const connected of [false, true]) {
  test(`a lent Help covers nothing in the expanded body and its box keeps its geometry (${connected ? 'keyed, connected, authorizing' : 'unconfigured'})`,
    async ({page}) => {
      await prepare(page, {connected});
      await width(page, 390);
      if (connected) {
        const torbox = card(page, 'torbox');
        await torbox.locator(':scope > .card-header .dp-settings-disclosure').click();
        await torbox.locator('[data-action="connect-torbox"]').click();
        await expect(torbox.locator('.dp-settings-account-waiting')).toHaveText('Waiting for authorization…');
      }
      await expand(page, ALL);
      for (const value of [320, 340]) {
        await width(page, value);
        const state = await headers(page);
        const lent = ALL.filter(id => state[id].place === 'body');
        expect(lent.length, `${value}px`).toBeGreaterThan(0);
        for (const theme of ['dark', 'light']) {
          await page.evaluate(light => document.body.classList.toggle('light', light), theme === 'light');
          // The chips' .15s colour transitions, finished.
          await page.waitForTimeout(300);
          const report = await page.evaluate(ids => ids.map(id => {
            const body = document.querySelector(`#view-settings .dp-settings-provider-card--${id} > .card-body`);
            const chip = body.querySelector('.dp-settings-help-slot > .dp-settings-help-chip');
            const slot = chip.parentElement;
            chip.scrollIntoView({block: 'center'});
            const c = chip.getBoundingClientRect();
            const problems = [];
            const s = slot.getBoundingClientRect();
            if (c.left < s.left || c.right > s.right || c.top < s.top || c.bottom > s.bottom) problems.push('outside its box');
            const covered = [];
            const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
            for (let n = walker.nextNode(); n; n = walker.nextNode()) {
              if (!n.textContent.trim() || chip.contains(n)) continue;
              const range = document.createRange(); range.selectNodeContents(n);
              for (const r of range.getClientRects()) covered.push([`text "${n.textContent.trim().slice(0, 20)}"`, r]);
            }
            for (const n of body.querySelectorAll('input, button, a, select, textarea, summary, img, .toggle, .dp-settings-provider-status-line')) {
              if (chip.contains(n) || !n.getClientRects().length) continue;
              covered.push([n.className || n.tagName, n.getBoundingClientRect()]);
            }
            for (const [name, r] of covered) {
              const w = Math.min(c.right, r.right) - Math.max(c.left, r.left);
              const h = Math.min(c.bottom, r.bottom) - Math.max(c.top, r.top);
              if (w > 0 && h > 0) problems.push(`covers ${name} ${w.toFixed(1)}x${h.toFixed(1)}`);
            }
            const hit = document.elementFromPoint((c.left + c.right) / 2, (c.top + c.bottom) / 2);
            if (!chip.contains(hit)) problems.push('not reachable at its centre');
            const geometry = () => {
              const o = body.getBoundingClientRect();
              return [o.height.toFixed(2), ...[...body.querySelectorAll('*')].filter(n => !chip.contains(n)).map(n => {
                const b = n.getBoundingClientRect();
                return [b.left - o.left, b.top - o.top, b.width, b.height].map(v => v.toFixed(2)).join(',');
              })].join('|');
            };
            const lentGeometry = geometry();
            const next = chip.nextSibling;
            chip.remove();
            const bare = geometry();
            slot.insertBefore(chip, next);
            if (lentGeometry !== bare) problems.push('body geometry changed');
            const usenet = document.querySelector('#view-settings .dp-settings-provider-card--usenet .dp-settings-help-chip');
            const paint = n => ['color', 'backgroundColor', 'borderTopColor', 'width', 'height', 'borderRadius'].map(p => getComputedStyle(n)[p]).join(' ');
            if (paint(chip) !== paint(usenet)) problems.push(`painted ${paint(chip)} not ${paint(usenet)}`);
            return [id, problems];
          }), lent);
          for (const [id, problems] of report) expect(problems, `${id} ${value}px ${theme}`).toEqual([]);
        }
        await page.evaluate(() => document.body.classList.remove('light'));
      }
    });
}

test('Usenet lends its Help to the row above its servers, which is drawn only while it holds it', async ({page}) => {
  await prepare(page);
  await expand(page, ['usenet']);
  const usenet = card(page, 'usenet');
  const row = usenet.locator('.card-body > .dp-settings-help-slot--row');
  await width(page, 320);
  // At the minimum supported width Usenet's whole row fits with its Help.
  expect((await headers(page)).usenet.place).toBe('header');
  await expect(row).toBeHidden();
  const before = await usenet.locator('[data-usenet-collection]').evaluate(n => n.getBoundingClientRect().top - n.closest('.card-body').getBoundingClientRect().top);
  // Narrower than supported, its Help is lent to the row, at its trailing edge.
  await width(page, 300);
  const state = (await headers(page)).usenet;
  expect(state.place).toBe('body');
  expect(state.oneRow).toBe(true);
  const geometry = await usenet.evaluate(node => {
    const body = node.querySelector(':scope > .card-body');
    const chip = body.querySelector('.dp-settings-help-slot--row > .dp-settings-help-chip');
    const row = chip.parentElement.getBoundingClientRect();
    const c = chip.getBoundingClientRect();
    const collection = body.querySelector('[data-usenet-collection]').getBoundingClientRect();
    return {chipRight: row.right - c.right, aboveServers: c.bottom <= collection.top,
            shift: collection.top - body.getBoundingClientRect().top};
  });
  expect(geometry.chipRight).toBeLessThanOrEqual(0.5);
  expect(geometry.aboveServers).toBe(true);
  // The servers move down by the row and nothing else: 28px chip + 8px gap.
  expect(Math.abs(geometry.shift - before - 36)).toBeLessThanOrEqual(0.5);
  await usenet.locator('[data-settings-help="usenet"]').click();
  await expect(page.locator('.dp-modal-dialog.dp-settings-help-dialog')).toHaveCount(1);
  await page.keyboard.press('Escape');
  await width(page, 320);
  expect((await headers(page)).usenet.place).toBe('header');
  await expect(row).toBeHidden();
});

test('lent Help, disclosure, Test and toggle stay operable by pointer and keyboard; focus is kept', async ({page}) => {
  await prepare(page);
  await width(page, 320);
  await expand(page, ['debridlink']);
  const debridlink = card(page, 'debridlink');
  const help = debridlink.locator('[data-settings-help="debridlink"]');
  await expect(debridlink.locator('.card-body .dp-settings-help-slot [data-settings-help="debridlink"]')).toHaveCount(1);
  await expect(help).toHaveAttribute('aria-label', 'Help: Debrid-Link');

  // Keyboard: Help opens its own topic and focus comes back to it.
  await help.focus();
  await page.keyboard.press('Enter');
  const dialog = page.locator('.dp-modal-dialog.dp-settings-help-dialog');
  await expect(dialog).toHaveCount(1);
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(help).toBeFocused();

  // A focused lent Help returns to the header, still focused, and leaves again.
  await width(page, 440);
  await expect(debridlink.locator(':scope > .card-header [data-settings-help="debridlink"]')).toHaveCount(1);
  await expect(help).toBeFocused();
  await width(page, 320);
  await expect(debridlink.locator('.card-body [data-settings-help="debridlink"]')).toHaveCount(1);
  await expect(help).toBeFocused();

  // Keyboard: the disclosure closes the card and opens it again.
  const disclosure = debridlink.locator(':scope > .card-header .dp-settings-disclosure');
  await disclosure.focus();
  await page.keyboard.press('Enter');
  await expect(debridlink.locator(':scope > .card-body')).toBeHidden();
  await expect(help).toBeHidden();
  await page.keyboard.press('Enter');
  await expect(help).toBeVisible();
  expect(await collisions(page)).toEqual([]);

  // The header's Test opens its menu; the toggle is the element at its centre.
  const head = debridlink.locator(':scope > .card-header');
  await head.locator('.dp-settings-header-test-trigger').click();
  await expect.poll(() => head.locator('.dp-settings-header-test-menu').evaluate(n => n.matches(':popover-open'))).toBe(true);
  await page.keyboard.press('Escape');
  expect(await head.locator('.dp-settings-header-test-group .toggle').evaluate(n => {
    const b = n.getBoundingClientRect();
    return n.contains(document.elementFromPoint((b.left + b.right) / 2, (b.top + b.bottom) / 2));
  })).toBe(true);
});

for (const id of ['premiumize', 'realdebrid']) {
  test(`a focused Help in collapsed ${id}'s header moves out at once when the header needs its room; focus goes to the card's disclosure`,
    async ({page}) => {
      await prepare(page);
      const provider = card(page, id);
      const help = provider.locator(`[data-settings-help="${id}"]`);
      const disclosure = provider.locator(':scope > .card-header .dp-settings-disclosure');
      await width(page, 440);
      await expect(provider.locator(':scope > .card-body')).toBeHidden();
      await help.focus();
      await expect(help).toBeFocused();
      expect((await headers(page))[id].place).toBe('header');

      // Narrowed: at once one row, nothing over its edge, the full title.
      await width(page, 320);
      const s = (await headers(page))[id];
      expect(s).toMatchObject({count: 1, place: 'body', oneRow: true, withinRow: true, fullTitle: true});
      expect(s.overflow).toBeLessThanOrEqual(0);
      expect(await collisions(page)).toEqual([]);
      // Focus is on the card's own disclosure, drawn and operable, and the card stays closed.
      await expect(disclosure).toBeFocused();
      expect(await disclosure.evaluate(n => {
        const b = n.getBoundingClientRect();
        return b.width > 0 && n.contains(document.elementFromPoint((b.left + b.right) / 2, (b.top + b.bottom) / 2));
      })).toBe(true);
      await expect(provider.locator(':scope > .card-body')).toBeHidden();
      await page.keyboard.press('Enter');
      await expect(help).toBeVisible();
      await page.keyboard.press('Enter');
      await expect(provider.locator(':scope > .card-body')).toBeHidden();

      // Widened: Help back in its header, the one element, reachable again.
      await width(page, 440);
      expect((await headers(page))[id]).toMatchObject({count: 1, place: 'header', oneRow: true});
      await help.focus();
      await expect(help).toBeFocused();
      await page.keyboard.press('Enter');
      await expect(page.locator('.dp-modal-dialog.dp-settings-help-dialog')).toHaveCount(1);
      await page.keyboard.press('Escape');
      await expect(help).toBeFocused();
    });
}

test('Real-Debrid pending, connected, disconnected and reconnecting: the one Help follows the current Account Connection, and resizes after each find it', async ({page}) => {
  await prepare(page, {lifecycle: true});
  await width(page, 320);
  await expand(page, ['realdebrid']);
  const realdebrid = card(page, 'realdebrid');
  const region = realdebrid.locator('[data-realdebrid-connection]');
  const where = () => page.evaluate(() => {
    const chips = document.querySelectorAll('[data-settings-help="realdebrid"]');
    const island = document.querySelector('[data-realdebrid-connection] > .dp-settings-account-island');
    const header = document.querySelector('.dp-settings-provider-card--realdebrid > .card-header');
    const chip = chips[0];
    return {count: chips.length, connected: !!chip?.isConnected,
            place: !chip ? 'missing' : island?.contains(chip) ? 'island' : header.contains(chip) ? 'header' : 'elsewhere'};
  });
  const resized = async () => {
    await width(page, 440);
    expect(await where()).toEqual({count: 1, connected: true, place: 'header'});
    await width(page, 320);
    expect(await where()).toEqual({count: 1, connected: true, place: 'island'});
  };
  expect(await where()).toEqual({count: 1, connected: true, place: 'island'});

  // Pending (the island rebuilt while Help is in it).
  await region.locator('[data-action="connect-realdebrid"]').click();
  await expect(region.locator('.dp-settings-account-waiting')).toHaveText('Waiting for authorization…');
  expect(await where()).toEqual({count: 1, connected: true, place: 'island'});
  await resized();

  // Connected (rebuilt again by the poll), then rebuilt while Help is in the header.
  await page.clock.fastForward(5000);
  await expect(region.locator('.dp-settings-account-island')).toContainText('Connected as alice');
  expect(await where()).toEqual({count: 1, connected: true, place: 'island'});
  await width(page, 440);
  await region.locator('[data-action="disconnect-realdebrid"]').click();
  await page.locator('.dp-modal-dialog [data-modal-accept]').click();
  await expect(region.locator('[data-action="connect-realdebrid"]')).toBeVisible();
  expect(await where()).toEqual({count: 1, connected: true, place: 'header'});
  // Narrowing after that rebuild finds the CURRENT island, never the one replaced.
  await width(page, 320);
  expect(await where()).toEqual({count: 1, connected: true, place: 'island'});

  // Reconnecting.
  await region.locator('[data-action="connect-realdebrid"]').click();
  await expect(region.locator('.dp-settings-account-waiting')).toHaveText('Waiting for authorization…');
  expect(await where()).toEqual({count: 1, connected: true, place: 'island'});
  await resized();
  await realdebrid.locator('[data-settings-help="realdebrid"]').click();
  await expect(page.locator('.dp-modal-dialog.dp-settings-help-dialog')).toHaveCount(1);
});

test('desktop headers keep Help, padding and spacing from 780px', async ({page}) => {
  await prepare(page);
  await expand(page, DEBRID);
  for (const value of [780, 1024, 1440]) {
    await width(page, value);
    expect(await step(page), `${value}px`).toBe('0');
    const state = await headers(page);
    for (const id of ALL) {
      expect(state[id].place, `${id} ${value}px`).toBe('header');
      expect(state[id].oneRow, `${id} ${value}px`).toBe(true);
    }
    const padding = await page.evaluate(() => [
      getComputedStyle(document.querySelector('#view-settings .dp-settings-provider-card--premiumize > .card-header')).paddingLeft,
      getComputedStyle(document.querySelector('#view-settings .dp-settings-debrid-services > .dp-settings-group-body')).paddingLeft]);
    expect(padding, `${value}px`).toEqual(['17px', await page.evaluate(() => getComputedStyle(
      document.querySelector('#view-settings .dp-settings-general-sources > .dp-settings-group-body')).paddingLeft)]);
  }
});
