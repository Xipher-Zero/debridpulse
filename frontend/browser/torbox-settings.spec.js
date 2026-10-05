const { test, expect } = require('@playwright/test');

/* DP 1.0.13 TorBox: the Premium Services card beside Real-Debrid.
 *
 * Every TorBox endpoint is answered here, so no account is needed and no
 * canonical state is written: this file owns no settings key. What it proves
 * is that TorBox is the Real-Debrid card's structure through the one account
 * connection owner -- the shipped mark, one bordered Account Connection island
 * in every state, an authorization that opens in the operator's own browser
 * and connects and enables without a reload, the shared premium wording -- and
 * that the one premium-account owner composes TorBox under the Provider Status
 * crown exactly as it composes AllDebrid and Real-Debrid. */

const PENDING = {state: 'pending', user_code: 'TB1234', verification_url: 'https://torbox.app/oauth/device',
                 interval: 5, expires_in: 600};
const ACCOUNT = {email: 'alice@example.com', plan: 2, plan_name: 'Pro', premium: true,
                 premium_expires_at: '2027-01-31T10:00:00Z',
                 account: {entitlement: 'ready', service_class: 'premium', functional: 'usable', plan: 'Pro',
                           expires_at: Date.parse('2027-01-31T10:00:00Z') / 1000}};

function projection(connected) {
  return {
    enabled: true, configured: connected, verified: connected,
    options: {api_token: '', api_token_configured: connected, usenet_enabled: false, rate_limit_per_minute: 240},
    presentation: {status_name: 'TorBox', premium: true, status_endpoint: '/integration-status/torbox',
                   display_order: 12, status_tier: 'premium_service', status_tier_label: 'Premium Services'},
  };
}

async function prepare(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.addInitScript(() => {
    window.__opened = [];
    window.open = (...args) => { window.__opened.push(args); return null; };
  });
  const calls = [];
  const reply = (route, body) => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(body)});
  await page.route(url => url.pathname.startsWith('/api/integrations/torbox/')
      || url.pathname === '/api/settings/validate-torbox', route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    calls.push(`${request.method()} ${path}`);
    if (path.endsWith('/authorization/poll')) {
      return reply(route, {state: 'connected', ...ACCOUNT, integration_id: 'torbox', integration: projection(true)});
    }
    if (path.endsWith('/authorization')) {
      return reply(route, request.method() === 'POST' ? PENDING : {state: 'idle'});
    }
    if (path.endsWith('/disconnect')) {
      return reply(route, {ok: true, integration_id: 'torbox', integration: projection(false)});
    }
    return reply(route, {ok: true, ...ACCOUNT});
  });
  await page.clock.install();
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await page.locator('#view-settings [data-tab="sources"]').click();
  const card = page.locator('.dp-settings-provider-card--torbox');
  await card.locator('.dp-settings-disclosure').click();
  return {card, region: card.locator('[data-torbox-connection]'), calls};
}

async function islandGeometry(region) {
  return region.evaluate(node => {
    const island = node.querySelector('.dp-settings-account-island');
    const box = island.getBoundingClientRect();
    const body = node.getBoundingClientRect();
    return {
      compact: box.width < body.width - 40,
      centred: Math.abs((box.left - body.left) - (body.right - box.right)),
      border: getComputedStyle(island).borderTopStyle,
      heading: island.querySelector('.dp-settings-account-heading').textContent,
      overflow: island.scrollWidth - island.clientWidth,
      firstIsRow: node.firstElementChild.classList.contains('dp-settings-provider-status-line'),
      lastIsIsland: node.lastElementChild === island,
    };
  });
}

test('the TorBox card uses the shipped mark, the shared island, and concise Additional Settings', async ({page}) => {
  const {card, region} = await prepare(page);
  const logo = card.locator('.dp-settings-provider-logo--torbox');
  await expect(logo).toHaveAttribute('src', '/icons/providers/torbox.svg');
  expect(await logo.evaluate(image => image.complete && image.naturalWidth > 0)).toBe(true);
  await expect(card.locator('.dp-settings-provider-header-copy'))
    .toHaveText('Resolve supported links, torrents and NZBs through your TorBox account.');
  await expect(card.locator('.dp-settings-card-header [data-action="test-torbox"]')).toBeVisible();
  await expect(card.locator('[data-integration-enabled="torbox"]')).not.toBeChecked();

  const geometry = await islandGeometry(region);
  expect(geometry).toMatchObject({compact: true, border: 'solid', heading: 'Account Connection',
                                  firstIsRow: true, lastIsIsland: true});
  expect(geometry.centred).toBeLessThan(2);
  expect(geometry.overflow).toBeLessThanOrEqual(0);
  await expect(region.locator('[data-action="connect-torbox"]')).toHaveText('Connect TorBox');
  expect(await region.innerText()).not.toMatch(/api token|bearer/i);

  // No separator above the disclosure and no balancing band beneath the island.
  const below = await card.evaluate(node => {
    const island = node.querySelector('.dp-settings-account-island').getBoundingClientRect();
    const additional = node.querySelector('.dp-settings-additional');
    return {gap: additional.getBoundingClientRect().top - island.bottom,
            border: parseFloat(getComputedStyle(additional).borderTopWidth)};
  });
  // The disclosure follows the island directly: its own summary row is the
  // separation (the shared grammar, settings-providers-layout.spec.js).
  expect(below.border).toBe(0);
  expect(Math.abs(below.gap)).toBeLessThanOrEqual(1);

  await card.locator('.dp-settings-additional > summary').click();
  const tuning = card.locator('.dp-settings-additional-body');
  await expect(tuning.locator('[data-setting]')).toHaveCount(6);
  const toggle = tuning.locator('[data-setting="torbox_usenet_enabled"]');
  await expect(toggle).not.toBeChecked();
  await expect(toggle).toHaveAttribute('data-commit', 'immediate');
  await expect(toggle).toHaveAttribute('data-commit-scope', 'integration:torbox');
  await expect(tuning).toContainText('Usenet via TorBox');
  await expect(tuning).toContainText('Let TorBox process NZB downloads remotely.');
  const backups = tuning.locator('[data-setting="torbox_prepare_backup_torrents"]');
  await expect(backups).not.toBeChecked();
  await expect(backups).toHaveAttribute('data-commit', 'immediate');
  await expect(backups).toHaveAttribute('data-commit-scope', 'integration:torbox');
  await expect(tuning).toContainText('Prepare Backup Torrents');
  await expect(tuning).toContainText('Uses TorBox create limits and active slots.');
  for (const [key, value, min, max] of [
    ['torbox_rate_limit_per_minute', '240', '1', '300'],
    ['torbox_request_timeout_seconds', '30', '5', '300'],
    ['torbox_upload_timeout_seconds', '120', '30', '900'],
    ['torbox_host_refresh_interval_hours', '24', '1', '168'],
  ]) {
    const control = tuning.locator(`[data-setting="${key}"]`);
    await expect(control).toHaveValue(value);
    await expect(control).toHaveAttribute('min', min);
    await expect(control).toHaveAttribute('max', max);
    await expect(control).toHaveAttribute('data-commit-scope', 'integration:torbox');
  }
});

test('authorization opens in the operator browser, polls, connects, enables and disconnects', async ({page}) => {
  const {card, region, calls} = await prepare(page);
  const before = page.url();
  await region.locator('[data-action="connect-torbox"]').click();
  await expect(region.locator('input[data-torbox-code]')).toHaveValue('TB1234');
  await expect(region.locator('.dp-settings-account-waiting')).toHaveText('Waiting for authorization…');
  await region.locator('[data-action="open-torbox"]').click();
  expect(await page.evaluate(() => window.__opened)).toEqual(
    [['https://torbox.app/oauth/device', '_blank', 'noopener,noreferrer']]);
  expect(page.url()).toBe(before);

  expect(calls).not.toContain('POST /api/integrations/torbox/authorization/poll');
  await page.clock.fastForward(5000);
  await expect(region.locator('.dp-settings-account-island')).toContainText('Connected as alice@example.com');
  // The shared premium owner's wording and TorBox's own plan as the tier.
  await expect(region.locator('.dp-settings-account-expiry')).toHaveText(/^Pro until 31\.01\.2027 \(\d+ days remaining\)$/);
  await expect(card.locator('[data-integration-enabled="torbox"]')).toBeChecked();
  expect((await islandGeometry(region)).overflow).toBeLessThanOrEqual(0);

  await region.locator('[data-action="disconnect-torbox"]').click();
  await page.locator('.dp-modal-dialog [data-modal-accept]').click();
  await expect(region.locator('[data-action="connect-torbox"]')).toBeVisible();
  expect(calls).toContain('POST /api/integrations/torbox/disconnect');
});

test('Cancel abandons the authorization without polling, and Test proves the saved token', async ({page}) => {
  const {card, region, calls} = await prepare(page);
  await region.locator('[data-action="connect-torbox"]').click();
  await region.locator('[data-action="cancel-torbox"]').click();
  await expect(region.locator('[data-action="connect-torbox"]')).toBeVisible();
  await page.clock.fastForward(6000);
  expect(calls).toContain('DELETE /api/integrations/torbox/authorization');
  expect(calls).not.toContain('POST /api/integrations/torbox/authorization/poll');
  await card.locator('[data-action="test-torbox"]').click();
  await expect.poll(() => calls).toContain('POST /api/settings/validate-torbox');
});

test('the TorBox card stays inside a phone-width viewport', async ({page}) => {
  const {card, region} = await prepare(page);
  await page.setViewportSize({width: 390, height: 900});
  await region.locator('[data-action="connect-torbox"]').click();
  await expect(region.locator('input[data-torbox-code]')).toHaveValue('TB1234');
  const box = await card.evaluate(node => ({overflow: node.scrollWidth - node.clientWidth,
                                            right: node.getBoundingClientRect().right,
                                            viewport: document.documentElement.clientWidth}));
  expect(box.overflow).toBeLessThanOrEqual(0);
  expect(box.right).toBeLessThanOrEqual(box.viewport);
});

// The neutral account truth every account-backed status surface publishes.
const premium = (plan, days) => ({entitlement: 'ready', service_class: 'premium', functional: 'usable', plan,
                                  expires_at: Math.floor(Date.now() / 1000) + days * 86400});

/* Provider Status: the one premium-account owner composes TorBox's healthy
 * account under the single crown; an unhealthy TorBox contributes nothing. */
const torboxAccount = (state = 'healthy') => ({id: 'torbox', name: 'TorBox', state,
  status: {account: premium('Pro', 120)}});
const rdAccount = () => ({id: 'realdebrid', name: 'Real-Debrid', state: 'healthy',
  status: {account: premium('Premium', 90)}});

const compose = (page, entries) => page.evaluate(next => {
  document.dispatchEvent(new CustomEvent('debridpulse:provider-status', {detail: {entries: next}}));
  const row = document.getElementById('premium-row');
  const blocks = [...document.querySelectorAll('#lbl-premium .dp-provider-premium-account')];
  return {visible: !!row && row.getClientRects().length > 0,
          blocks: blocks.map(block => ({compact: block.classList.contains('dp-provider-premium-account--compact'),
                                        lines: [...block.children].map(child => child.textContent),
                                        text: block.textContent}))};
}, entries);

test('TorBox joins the premium crown through the one premium-account owner', async ({page}) => {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  await page.waitForFunction(() => !!window.DPPremiumAccount);

  const alone = await compose(page, [torboxAccount()]);
  expect(alone.visible).toBe(true);
  expect(alone.blocks).toHaveLength(1);
  expect(alone.blocks[0].lines[0]).toMatch(/^TorBox Pro until \d\d\.\d\d\.\d{4}$/);
  expect(alone.blocks[0].lines[1]).toMatch(/^\(\d+ days remaining\)$/);

  const both = await compose(page, [rdAccount(), torboxAccount()]);
  expect(both.blocks.map(block => block.compact)).toEqual([true, true]);
  expect(both.blocks[1].text).toMatch(/^TorBox Pro \d+ days remaining$/);

  const stale = await compose(page, [torboxAccount('auth_required')]);
  expect(stale.visible).toBe(false);
  expect(stale.blocks).toHaveLength(0);
});
