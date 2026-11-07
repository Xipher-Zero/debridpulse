const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Settings header Test control (ui-settings-page.js
 * headerTestControls).
 *
 * Below 780px a card header's status and direct Test give way to one compact
 * outlined group -- a textual Test trigger beside the toggle -- whose menu
 * reports the configuration and holds the card's own Test. From 780px the
 * header is what it was, except: no "Enable" word, and an off section's Test
 * is muted (never disabled). Every Settings card header's toggle loses the
 * visible "Enable" word at every width.
 *
 * Canonical state is the server's own documents with the facts a case needs
 * patched in; every Test endpoint is answered here, so nothing reaches a
 * provider, an identity provider or Discord. The User case is a real
 * password-auth session (DP_AUTH_BASE_URL) on a synthetic account. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const ADMIN = {username: 'browser', password: 'browser-secret'};
const run = Date.now().toString(36);
const adminSession = require('./acl-admin-session');

async function fonts(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

// The server's documents, with the facts this case needs.
async function withState(page, {integrations = {}, settings = {}, auth = {}} = {}) {
  await page.route(url => url.pathname === '/api/settings', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    const body = await (await route.fetch()).json();
    body.integrations = {...body.integrations};
    for (const [id, patch] of Object.entries(integrations)) body.integrations[id] = {...body.integrations[id], ...patch};
    return route.fulfill({json: {...body, ...settings}});
  });
  await page.route(url => url.pathname === '/api/auth/config', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    return route.fulfill({json: {...await (await route.fetch()).json(), ...auth}});
  });
}

// An answered endpoint: every call is counted; ``gate`` holds the answer.
function answer(page, path, respond, {gate = null} = {}) {
  const calls = [];
  page.route(url => url.pathname === path, async route => {
    calls.push(route.request().method());
    if (gate) await gate;
    const [status, json] = respond();
    await route.fulfill({status, json});
  });
  return calls;
}

async function openTab(page, tab) {
  // The app's own navigation (the sidebar entry is off-canvas below 780px).
  await page.waitForFunction(() => typeof window.nav === 'function' && typeof window.loadSettings === 'function');
  await page.evaluate(() => window.nav(document.querySelector('#sidebar .nav-item[data-view="settings"]')));
  // The page's own tab (not a motion still), however the tab strip is fitted.
  await page.locator(`#view-settings .dp-settings-tabs [data-tab="${tab}"]:not(.dp-motion-still *)`).first()
    .dispatchEvent('click');
  await expect(page.locator(`#view-settings [data-panel="${tab}"]`)).toBeVisible();
  await page.evaluate(id => document.querySelectorAll(
    `#view-settings [data-panel="${id}"] .dp-settings-group-card > .card-header .dp-settings-disclosure[aria-expanded="false"]`)
    .forEach(button => button.click()), tab);
}

const header = (page, card) => page.locator(`#view-settings ${card} > .card-header`);
const trigger = head => head.locator('.dp-settings-header-test-trigger');
const menu = head => head.locator('.dp-settings-header-test-menu');
const isOpen = locator => locator.evaluate(el => el.matches(':popover-open'));
const token = (page, name, property = 'color', read = property) => page.evaluate(([variable, prop, out]) => {
  const probe = document.createElement('span');
  probe.style[prop] = `var(${variable})`;
  document.querySelector('#view-settings').appendChild(probe);
  const color = getComputedStyle(probe)[out];
  probe.remove();
  return color;
}, [name, property, read]);
// An off section's Test: the neutral DP ghost button -- no status colour in its
// text, border or background, drawn at full strength (not faded) and enabled.
async function expectNeutral(page, button) {
  await expect(button).toHaveCSS('color', await token(page, '--dp-text-muted'));
  await expect(button).toHaveCSS('border-top-color', await token(page, '--dp-secondary-border', 'borderTopColor'));
  // The DP secondary surface (a gradient in some palettes), not a tinted fill.
  await expect(button).toHaveCSS('background-image', await token(page, '--dp-secondary-surface', 'background', 'backgroundImage'));
  await expect(button).toHaveCSS('background-color', await token(page, '--dp-secondary-surface', 'background', 'backgroundColor'));
  await expect(button).toHaveCSS('opacity', '1');
  await expect(button).toBeEnabled();
}

test.describe('below 780px', () => {
  test.use({viewport: {width: 390, height: 844}});

  test('Test is a trigger beside the toggle; the status moves into its menu; opening tests nothing', async ({page}) => {
    await fonts(page);
    await withState(page, {integrations: {usenet: {enabled: true, configured: true, verified: false}}});
    const tests = answer(page, '/api/usenet/test', () => [200, {}]);
    await page.goto('/');
    await openTab(page, 'sources');
    const head = header(page, '.dp-settings-provider-card--usenet');
    await expect(head).not.toContainText('Enable');
    await expect(head.locator('.dp-settings-header-action')).toBeHidden();
    await expect(trigger(head)).toBeVisible();
    await expect(trigger(head)).toHaveAccessibleName('Test Usenet');
    await expect(trigger(head)).toHaveText('Test');
    await expect(trigger(head)).toHaveAttribute('data-verification', 'unverified');
    await expect(head.getByRole('checkbox', {name: 'Enable Usenet provider route', exact: true})).toHaveCount(1);
    // One outlined group, Test then toggle, no gratuitous gap.
    const group = await head.locator('.dp-settings-header-test-group').evaluate(el => {
      const box = el.getBoundingClientRect();
      const t = el.querySelector('.dp-settings-header-test-trigger').getBoundingClientRect();
      const s = el.querySelector('.toggle').getBoundingClientRect();
      return {border: getComputedStyle(el).borderTopWidth, style: getComputedStyle(el).borderTopStyle,
              inside: t.left >= box.left && s.right <= box.right, gap: s.left - t.right};
    });
    expect(group).toMatchObject({border: '1px', style: 'solid', inside: true});
    expect(group.gap).toBeLessThanOrEqual(8);
    // The rail's live status is still the page's announcement, just not drawn.
    const live = head.locator('.dp-settings-card-header-controls > .dp-settings-provider-config-status');
    await expect(live).toHaveAttribute('role', 'status');
    await expect(live).toHaveText('Unverified');
    expect((await live.boundingBox()).width).toBeLessThanOrEqual(1);

    await trigger(head).click();
    await expect(menu(head)).toBeVisible();
    await expect(trigger(head)).toHaveAttribute('aria-expanded', 'true');
    await expect(menu(head).locator('> *').first()).toHaveText('Unverified');
    const inner = menu(head).getByRole('button', {name: 'Test', exact: true});
    await expect(inner).toBeFocused();
    await expect(inner.locator('.dp-settings-action-glyph')).toHaveCount(1);
    const box = await menu(head).boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(390);
    expect(tests).toEqual([]);
    await page.keyboard.press('Escape');
    await expect(menu(head)).toBeHidden();
    await expect(trigger(head)).toBeFocused();
    await expect(trigger(head)).toHaveAttribute('aria-expanded', 'false');
    // A closed menu holds nothing: the page keeps one Test per card.
    await expect(page.locator('[data-action="test-usenet"]')).toHaveCount(1);
  });

  test("the menu's Test runs the card's own action once and the menu stays through it", async ({page}) => {
    await fonts(page);
    await withState(page, {integrations: {usenet: {enabled: true, configured: true, verified: false}}});
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    const tests = answer(page, '/api/usenet/test', () => [200, {
      ok: true, tested: 1, passed: 1, failed: 0, servers: [],
      integration_id: 'usenet', integration: {enabled: true, configured: true, verified: true}}], {gate});
    await page.goto('/');
    await openTab(page, 'sources');
    const head = header(page, '.dp-settings-provider-card--usenet');
    await trigger(head).click();
    const inner = menu(head).locator('button[data-action="test-usenet"]');
    await inner.click();
    await expect(inner).toHaveText('Testing…');
    await expect(inner).toBeDisabled();
    await inner.click({force: true});
    expect(await isOpen(menu(head))).toBe(true);
    await expect.poll(() => tests.length).toBe(1);
    release();
    await expect(page.locator('.toast').last()).toContainText('Usenet: 1 of 1 news server(s) verified');
    expect(tests).toEqual(['POST']);
    expect(await isOpen(menu(head))).toBe(true);
    await expect(inner).toBeEnabled();
    // The accepted state, not the click, says Verified.
    await expect(menu(head).locator('.dp-settings-header-test-status')).toHaveText('Verified');
    await expect(trigger(head)).toHaveAttribute('data-verification', 'verified');
  });

  test('Discord and Statistics Reporting keep their own flows; a failure is reported as the handler reports it', async ({page}) => {
    await fonts(page);
    await withState(page, {settings: {discord_notifications_configured: true, discord_notifications_verified: false,
                                      stats_reporting_configured: false, stats_reporting_verified: false}});
    const discord = answer(page, '/api/settings/validate-discord', () => [400, {detail: 'Webhook refused'}]);
    const report = answer(page, '/api/settings/send-stats-report', () => [200, {notifications: {}, hours: 24}]);
    await page.goto('/');
    await openTab(page, 'notifications');
    const discordHead = header(page, '.dp-settings-discord-card');
    const reportHead = header(page, '.dp-settings-statistics-reporting-card');
    await expect(trigger(discordHead)).toHaveAttribute('data-verification', 'unverified');
    await expect(trigger(reportHead)).toHaveAttribute('data-verification', 'unconfigured');
    await trigger(discordHead).click();
    await expect(menu(discordHead).locator('.dp-settings-header-test-status')).toHaveText('Configured');
    await menu(discordHead).locator('button[data-action="test-discord"]').click();
    await expect(page.locator('.toast').last()).toContainText('Webhook Notifications: Webhook refused');
    expect(discord).toEqual(['POST']);
    expect(await isOpen(menu(discordHead))).toBe(true);
    await page.keyboard.press('Escape');
    await trigger(reportHead).click();
    await expect(menu(reportHead).locator('.dp-settings-header-test-status')).toHaveText('Unconfigured');
    await menu(reportHead).locator('button[data-action="send-report"]').click();
    await expect(page.locator('.toast').last()).toContainText('Test report sent (24h)');
    expect(report).toEqual(['POST']);
  });

  test('the trigger is a DP button coloured by the configuration; an off section is the neutral ghost yet still opens and tests', async ({page}) => {
    await fonts(page);
    await withState(page, {integrations: {
      alldebrid: {enabled: true, configured: true, verified: true},
      debridlink: {enabled: true, configured: true, verified: false},
      realdebrid: {enabled: true, configured: false, verified: false},
      torbox: {enabled: false, configured: true, verified: true},
      premiumize: {enabled: false, configured: false, verified: false}}});
    const tests = answer(page, '/api/settings/validate-premiumize', () => [400, {detail: 'Premiumize is disabled'}]);
    await page.goto('/');
    await openTab(page, 'sources');
    const cases = [['alldebrid', 'verified', '--dp-state-success'], ['debridlink', 'unverified', '--dp-state-caution'],
                   ['realdebrid', 'unconfigured', '--dp-accent-purple-bright']];
    for (const theme of ['dark', 'light']) {
      if (theme === 'light') await page.evaluate(() => document.body.classList.add('light'));
      for (const [id, state, variable] of cases) {
        const head = header(page, `.dp-settings-provider-card--${id}`);
        await expect(trigger(head)).toHaveAttribute('data-verification', state);
        await expect(trigger(head)).toHaveClass(/\bbtn\b.*\bbtn-sm\b/);
        await expect(trigger(head)).toHaveText('Test');
        await expect(trigger(head).locator('svg')).toHaveCount(0);
        await expect(trigger(head)).toHaveCSS('color', await token(page, variable));
        await expect(trigger(head)).toHaveCSS('opacity', '1');
      }
      // Off: verification stays (verified, unconfigured), the colour does not.
      for (const [id, state] of [['torbox', 'verified'], ['premiumize', 'unconfigured']]) {
        const head = header(page, `.dp-settings-provider-card--${id}`);
        await expect(trigger(head)).toHaveAttribute('data-verification', state);
        await expectNeutral(page, trigger(head));
      }
    }
    const off = header(page, '.dp-settings-provider-card--premiumize');
    // Off is participation, not proof: the rail hides Unconfigured, the menu says it.
    await trigger(off).click();
    await expect(menu(off).locator('.dp-settings-header-test-status')).toHaveText('Unconfigured');
    // The menu centres its report, and its Test's mark and word as one unit.
    const centred = await menu(off).evaluate(el => {
      const box = el.getBoundingClientRect(), mid = box.left + box.width / 2;
      const status = el.querySelector('.dp-settings-header-test-status');
      const range = document.createRange();
      range.selectNodeContents(status);
      const text = range.getBoundingClientRect();
      const test = el.querySelector('.dp-settings-provider-test');
      const parts = [...test.children].map(child => child.getBoundingClientRect()).filter(r => r.width);
      const left = Math.min(...parts.map(r => r.left)), right = Math.max(...parts.map(r => r.right));
      return {status: Math.abs(text.left + text.width / 2 - mid), test: Math.abs((left + right) / 2 - mid), parts: parts.length};
    });
    expect(centred.parts).toBeGreaterThanOrEqual(2);
    expect(centred.status).toBeLessThanOrEqual(1.5);
    expect(centred.test).toBeLessThanOrEqual(1.5);
    await menu(off).locator('button[data-action="test-premiumize"]').click();
    await expect(page.locator('.toast').last()).toContainText('Premiumize is disabled');
    expect(tests).toEqual(['POST']);
  });

  test('OpenID Connect keeps its own verification flow and reports its configuration while off', async ({page}) => {
    await fonts(page);
    await withState(page, {auth: {oidc_enabled: false, oidc_configured: true, oidc_verified: false}});
    const verify = answer(page, '/api/auth/oidc/verify-config', () => [400, {detail: 'Issuer unreachable'}]);
    page.on('popup', popup => popup.close().catch(() => {}));
    await page.goto('/');
    await openTab(page, 'authentication');
    const head = header(page, '.dp-settings-oidc-card');
    await expect(head).not.toContainText('Enable');
    await expect(head.getByRole('checkbox', {name: 'Enable OpenID Connect', exact: true})).toHaveCount(1);
    await expect(trigger(head)).toHaveAttribute('data-verification', 'unverified');
    await expectNeutral(page, trigger(head));
    await trigger(head).click();
    await expect(menu(head).locator('.dp-settings-header-test-status')).toHaveText('Configured');
    await menu(head).locator('button[data-action="verify-oidc"]').click();
    await expect(page.locator('.toast').last()).toContainText('Issuer unreachable');
    expect(verify).toEqual(['POST']);
  });
});

test('at 780px an idle menu closes and the direct Test takes over', async ({page}) => {
  await fonts(page);
  await page.setViewportSize({width: 779, height: 900});
  await withState(page, {settings: {discord_notifications_configured: true, discord_notifications_verified: true}});
  const discord = answer(page, '/api/settings/validate-discord', () => [200, {notifications: {}}]);
  await page.goto('/');
  await openTab(page, 'notifications');
  const head = header(page, '.dp-settings-discord-card');
  await trigger(head).click();
  expect(await isOpen(menu(head))).toBe(true);
  await page.setViewportSize({width: 780, height: 900});
  await expect.poll(() => isOpen(menu(head))).toBe(false);
  await expect(trigger(head)).toBeHidden();
  const direct = head.locator('.dp-settings-header-action > [data-action="test-discord"]');
  await expect(direct).toBeVisible();
  await direct.click();
  await expect(page.locator('.toast').last()).toContainText('Test notification sent');
  expect(discord).toEqual(['POST']);
});

test('from 780px: no dropdown, the direct flask Test, the neutral ghost (never disabled) while its section is off', async ({page}) => {
  await fonts(page);
  await page.setViewportSize({width: 1440, height: 900});
  await withState(page, {integrations: {premiumize: {enabled: false, configured: true, verified: false},
                                        alldebrid: {enabled: true, configured: true, verified: false}}});
  const tests = answer(page, '/api/settings/validate-premiumize', () => [400, {detail: 'Premiumize is disabled'}]);
  await page.goto('/');
  await openTab(page, 'sources');
  for (const id of ['premiumize', 'alldebrid']) {
    const head = header(page, `.dp-settings-provider-card--${id}`);
    await expect(trigger(head)).toBeHidden();
    await expect(head.locator('.dp-settings-provider-config-status')).toHaveText('Unverified');
    await expect(head).not.toContainText('Enable');
    const direct = head.locator('.dp-settings-header-action > .dp-settings-provider-test');
    await expect(direct).toBeVisible();
    await expect(direct).toHaveText('Test');
    await expect(direct.locator('.dp-settings-action-glyph')).toBeVisible();
    await expect(direct).toBeEnabled();
    if (id === 'premiumize') {
      await expectNeutral(page, direct);
      await expect(direct.locator('.dp-settings-action-glyph')).toHaveCSS('filter', 'grayscale(1)');
    } else {
      await expect(direct).toHaveCSS('opacity', '1');
      await expect(direct).not.toHaveCSS('color', await token(page, '--dp-text-muted'));
    }
  }
  await header(page, '.dp-settings-provider-card--premiumize').locator('[data-action="test-premiumize"]').click();
  await expect(page.locator('.toast').last()).toContainText('Premiumize is disabled');
  expect(tests).toEqual(['POST']);
});

test('no Settings card header shows an "Enable" word at any width; every header toggle keeps a name', async ({page}) => {
  await fonts(page);
  await page.goto('/');
  for (const width of [320, 779, 780, 1440]) {
    await page.setViewportSize({width, height: 900});
    for (const tab of ['sources', 'extraction', 'authentication', 'notifications', 'maintenance']) {
      await openTab(page, tab);
      const found = await page.evaluate(id => {
        const panel = document.querySelector(`#view-settings [data-panel="${id}"]`);
        const headers = [...panel.querySelectorAll('.card-header')];
        return {
          words: headers.filter(h => h.offsetParent && /\bEnable\b/.test(h.innerText)).map(h => h.querySelector('.card-title')?.textContent.trim()),
          toggles: headers.flatMap(h => [...h.querySelectorAll('input[type="checkbox"]')])
            .map(input => input.getAttribute('aria-label') || ''),
          boxes: [...panel.querySelectorAll('.dp-settings-source-box')].filter(box => box.offsetParent)
            .map(box => /\bEnable\b/.test(box.innerText)),
        };
      }, tab);
      expect(found.words, `${tab} @ ${width}`).toEqual([]);
      for (const name of found.toggles) expect(name, `${tab} @ ${width}`).toMatch(/^Enable \S/);
      // A Network Source box's toggle is its body's one control: it keeps the word.
      for (const shown of found.boxes) expect(shown).toBe(true);
    }
  }
});

const FAMILIES = [['sources', '.dp-settings-provider-card--usenet'], ['sources', '.dp-settings-provider-card--alldebrid'],
  ['sources', '.dp-settings-provider-card--debridlink'], ['sources', '.dp-settings-provider-card--premiumize'],
  ['sources', '.dp-settings-provider-card--realdebrid'], ['sources', '.dp-settings-provider-card--torbox'],
  ['authentication', '.dp-settings-oidc-card'], ['notifications', '.dp-settings-discord-card'],
  ['notifications', '.dp-settings-statistics-reporting-card']];

test('below 780px every Test-group header keeps title, disclosure, Help, Test and toggle apart and operable', async ({page}) => {
  await fonts(page);
  await page.goto('/');
  for (const width of [320, 360, 390, 779]) {
    await page.setViewportSize({width, height: 900});
    for (const [tab, card] of FAMILIES) {
      await openTab(page, tab);
      const head = header(page, card);
      await head.scrollIntoViewIfNeeded();
      const found = await head.evaluate(el => {
        // The identity (its mark, title and disclosure), each of its own
        // controls, and each control of the rail -- the Test group's outline
        // included: no two of them may share a pixel, unless one holds the other.
        const parts = {identity: el.querySelector('.dp-settings-card-title-group'), title: el.querySelector('.card-title'),
          disclosure: el.querySelector('.dp-settings-disclosure'), help: el.querySelector('.dp-settings-help-chip'),
          group: el.querySelector('.dp-settings-header-test-group'), test: el.querySelector('.dp-settings-header-test-trigger'),
          toggle: el.querySelector('.dp-settings-card-header-controls .toggle')};
        const box = node => node && node.getClientRects().length ? node.getBoundingClientRect() : null;
        const names = Object.keys(parts);
        const overlaps = [];
        for (let i = 0; i < names.length; i++) {
          for (let j = i + 1; j < names.length; j++) {
            const a = box(parts[names[i]]);
            const b = box(parts[names[j]]);
            if (!a || !b || parts[names[i]].contains(parts[names[j]]) || parts[names[j]].contains(parts[names[i]])) continue;
            const x = Math.min(a.right, b.right) - Math.max(a.left, b.left);
            const y = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
            if (x > 0.5 && y > 0.5) overlaps.push(`${names[i]} ~ ${names[j]}: ${x.toFixed(1)} x ${y.toFixed(1)}px`);
          }
        }
        // Each control is what a press at its centre reaches.
        const blocked = names.filter(name => {
          const b = box(parts[name]);
          if (!b || ['identity', 'title', 'group'].includes(name)) return false;
          const at = document.elementFromPoint((b.left + b.right) / 2, (b.top + b.bottom) / 2);
          return !at || !parts[name].contains(at);
        });
        // A Premium Services Help may be lent to its own card's body while it
        // alone keeps the header off one row (settings-premium-header-help.spec.js).
        const lent = !parts.help && el.parentElement.querySelector(':scope > .card-body .dp-settings-help-slot > .dp-settings-help-chip');
        return {overlaps, blocked, missing: ['identity', 'title', 'help', 'group', 'test', 'toggle']
                  .filter(name => !box(parts[name]) && !(name === 'help' && lent)),
                headerOverflow: el.scrollWidth - el.clientWidth,
                pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth};
      });
      const at = `${card} @ ${width}px`;
      expect(found.overlaps, at).toEqual([]);
      expect(found.blocked, at).toEqual([]);
      expect(found.missing, at).toEqual([]);
      expect(found.headerOverflow, at).toBeLessThanOrEqual(0);
      expect(found.pageOverflow, at).toBeLessThanOrEqual(0);
    }
  }
  // The long titles' disclosure is the real control: a press opens and closes it.
  await page.setViewportSize({width: 320, height: 900});
  await openTab(page, 'sources');
  for (const card of ['.dp-settings-provider-card--debridlink', '.dp-settings-provider-card--realdebrid']) {
    const disclosure = header(page, card).locator('.dp-settings-disclosure');
    await disclosure.scrollIntoViewIfNeeded();
    const before = await disclosure.getAttribute('aria-expanded');
    await disclosure.click();
    await expect(disclosure).not.toHaveAttribute('aria-expanded', before);
    await disclosure.click();
    await expect(disclosure).toHaveAttribute('aria-expanded', before);
  }
});

test('a Test running across 779 to 780px closes its menu when it answers; focus goes to the direct Test', async ({page}) => {
  await fonts(page);
  await page.setViewportSize({width: 779, height: 900});
  await withState(page, {settings: {discord_notifications_configured: true, discord_notifications_verified: false}});
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const calls = answer(page, '/api/settings/validate-discord',
    () => [200, {notifications: {discord_notifications_configured: true, discord_notifications_verified: true}}], {gate});
  await page.goto('/');
  await openTab(page, 'notifications');
  const head = header(page, '.dp-settings-discord-card');
  await trigger(head).click();
  await menu(head).locator('button[data-action="test-discord"]').click();
  await expect.poll(() => calls.length).toBe(1);
  await page.setViewportSize({width: 780, height: 900});
  expect(await isOpen(menu(head))).toBe(true);           // running: kept until it answers
  release();
  await expect(page.locator('.toast').last()).toContainText('Test notification sent');
  await expect.poll(() => isOpen(menu(head))).toBe(false);
  await expect(trigger(head)).toHaveAttribute('aria-expanded', 'false');
  await expect(menu(head).locator('*')).toHaveCount(0);
  await expect(page.locator('[data-action="test-discord"]')).toHaveCount(1);
  await expect(head.locator('.dp-settings-header-action > [data-action="test-discord"]')).toBeFocused();
  await expect(head.locator('.dp-settings-provider-config-status')).toHaveText('Verified');
  expect(calls).toEqual(['POST']);
});

test('a menu dismissed while its Test runs lets go of it when it answers, keeping the result', async ({page}) => {
  await fonts(page);
  await page.setViewportSize({width: 390, height: 844});
  await withState(page, {integrations: {usenet: {enabled: true, configured: true, verified: false}}});
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const calls = answer(page, '/api/usenet/test', () => [200, {
    ok: true, tested: 1, passed: 1, failed: 0, servers: [],
    integration_id: 'usenet', integration: {enabled: true, configured: true, verified: true}}], {gate});
  await page.goto('/');
  await openTab(page, 'sources');
  const head = header(page, '.dp-settings-provider-card--usenet');
  await trigger(head).click();
  await menu(head).locator('button[data-action="test-usenet"]').click();
  await expect.poll(() => calls.length).toBe(1);
  await page.keyboard.press('Escape');
  await expect.poll(() => isOpen(menu(head))).toBe(false);
  await expect(trigger(head)).toHaveAttribute('aria-expanded', 'false');
  release();
  await expect(page.locator('.toast').last()).toContainText('Usenet: 1 of 1 news server(s) verified');
  await expect(menu(head).locator('*')).toHaveCount(0);
  await expect(page.locator('[data-action="test-usenet"]')).toHaveCount(1);
  await expect(trigger(head)).toHaveAttribute('data-verification', 'verified');
  expect(calls).toEqual(['POST']);
  // Opened again, the menu is drawn afresh from the accepted state.
  await trigger(head).click();
  await expect(menu(head).locator('.dp-settings-header-test-status')).toHaveText('Verified');
  await expect(menu(head).locator('button[data-action="test-usenet"]')).toBeEnabled();
});

test('the open menu describes its Test with the configuration, also while OpenID Connect is off', async ({page}) => {
  await fonts(page);
  await page.setViewportSize({width: 390, height: 844});
  await withState(page, {auth: {oidc_enabled: false, oidc_configured: true, oidc_verified: false}});
  await page.goto('/');
  await openTab(page, 'authentication');
  const head = header(page, '.dp-settings-oidc-card');
  await expect(head.locator('.dp-settings-card-header-controls > .dp-settings-provider-config-status')).toHaveText('');
  await trigger(head).click();
  const inner = menu(head).locator('button[data-action="verify-oidc"]');
  await expect(inner).toBeFocused();
  await expect(inner).toHaveAccessibleName('Test');
  await expect(inner).toHaveAccessibleDescription('Configured');
});

/* ── A standard User's own Notifications, in a real User-role session ───── */
const api = (page, method, path, body) => page.evaluate(async ([method, path, body]) => {
  const response = await window.debridPulseAuth.fetch(`/api${path}`, {
    method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  return {status: response.status, body: await response.json().catch(() => null)};
}, [method, path, body]);

async function signIn(browser, {username, password}, viewport) {
  const shared = username === ADMIN.username;
  const reuse = shared && adminSession.get() !== null;
  const context = await browser.newContext({baseURL: AUTH, viewport, ...(reuse ? {storageState: adminSession.get()} : {})});
  const page = await context.newPage();
  await fonts(page);
  if (reuse) await page.goto('/');
  else {
    await page.goto('/login');
    await page.locator('#username').fill(username);
    await page.locator('#password').fill(password);
    await page.locator('button[type="submit"]').click();
  }
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.locator('#sidebar-auth-row .dp-sidebar-profile')).toBeAttached();
  await page.waitForFunction(() => Boolean(window.debridPulseAuth?.fetch));
  if (shared && !reuse) adminSession.remember(await context.storageState());
  return {context, page};
}

test('a User\'s own Notifications take the same header Test treatment through the User\'s own endpoints', async ({browser}) => {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const admin = await signIn(browser, ADMIN, {width: 1440, height: 900});
  const account = {username: `hdr-user-${run}`, password: `hdr-user-${run}-pass`};
  const created = await api(admin.page, 'POST', '/accounts', {kind: 'local', role: 'user', display_name: `Header ${run}`, ...account});
  expect(created.status).toBe(200);
  await admin.context.close();

  const {context, page} = await signIn(browser, account, {width: 390, height: 844});
  const own = answer(page, '/api/account/notifications/test-discord', () => [200, {notifications: {}}]);
  const ownReport = answer(page, '/api/account/notifications/send-report', () => [200, {notifications: {}, hours: 24}]);
  const global = answer(page, '/api/settings/validate-discord', () => [500, {}]);
  expect(await page.evaluate(() => window.debridPulseAuthority?.role)).toBe('user');
  await page.evaluate(() => window.nav([...document.querySelectorAll('#sidebar .nav-item')]
    .find(item => item.textContent.includes('Notifications'))));
  const view = page.locator('#view-settings');
  await expect(view).toHaveAttribute('data-settings-context', 'self');
  for (const [card, name, action, calls] of [['.dp-settings-discord-card', 'Enable Webhook Notifications', 'test-discord', own],
                                             ['.dp-settings-statistics-reporting-card', 'Enable Statistics Reporting', 'send-report', ownReport]]) {
    const head = view.locator(`${card} > .card-header`);
    await expect(head).not.toContainText('Enable');
    await expect(head.getByRole('checkbox', {name, exact: true})).toHaveCount(1);
    await expect(trigger(head)).toBeVisible();
    await trigger(head).click();
    await menu(head).locator(`button[data-action="${action}"]`).click();
    await expect.poll(() => calls.length).toBe(1);
    await page.keyboard.press('Escape');
  }
  expect(global).toEqual([]);
  // From 780px the User's header is the direct Test beside the toggle.
  await page.setViewportSize({width: 1024, height: 900});
  const head = view.locator('.dp-settings-discord-card > .card-header');
  await expect(trigger(head)).toBeHidden();
  await expect(head.locator('.dp-settings-header-action > [data-action="test-discord"]')).toBeVisible();
  await context.close();
});
