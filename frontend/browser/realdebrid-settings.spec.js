const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Real-Debrid: the Premium Services card beside AllDebrid.
 *
 * Every Real-Debrid endpoint is answered here, so no account is needed and no
 * canonical state is written: this file owns no settings key. What it proves
 * is the card's own contract -- the shipped mark, a main body that is one
 * centred column in every connection state, an authorization that opens in
 * the operator's own browser and never navigates DebridPulse, and an
 * Additional Settings region that holds tuning and nothing else. */

const PENDING = {state: 'pending', user_code: 'ABCD1234', verification_url: 'https://real-debrid.com/device',
                 interval: 5, expires_in: 600};
const ACCOUNT = {username: 'alice', account_type: 'premium', premium: true, premium_seconds: 9,
                 expiration: '2027-01-31T10:00:00.000Z'};

/* An operator enables Real-Debrid and then connects it: accepting a projection
 * of a DISABLED provider puts its card away (renderIntegrationState), which is
 * the shared Settings rule, not this card's. */
function projection(connected) {
  return {
    enabled: true, configured: connected, verified: connected,
    options: {client_id: '', client_id_configured: connected, client_secret: '', client_secret_configured: connected,
              refresh_token: '', refresh_token_configured: connected, rate_limit_per_minute: 240},
    presentation: {status_name: 'Real-Debrid', premium: true, status_endpoint: '/integration-status/realdebrid',
                   display_order: 11, status_tier: 'premium_service', status_tier_label: 'Premium Services'},
  };
}

async function prepare(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  // The operator's browser, observed: nothing may navigate DebridPulse itself.
  await page.addInitScript(() => {
    window.__opened = [];
    window.open = (...args) => { window.__opened.push(args); return null; };
  });
  const calls = [];
  const reply = (route, body) => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(body)});
  await page.route(url => url.pathname.startsWith('/api/integrations/realdebrid/')
      || url.pathname === '/api/settings/validate-realdebrid', route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    calls.push(`${request.method()} ${path}`);
    if (path.endsWith('/authorization/poll')) {
      return reply(route, {state: 'connected', ...ACCOUNT, integration_id: 'realdebrid', integration: projection(true)});
    }
    if (path.endsWith('/authorization')) {
      return reply(route, request.method() === 'POST' ? PENDING : {state: 'idle'});
    }
    if (path.endsWith('/disconnect')) {
      return reply(route, {ok: true, revoked: true, integration_id: 'realdebrid', integration: projection(false)});
    }
    return reply(route, {ok: true, ...ACCOUNT});
  });
  // Time still flows; the poll interval is jumped deterministically, never slept.
  await page.clock.install();
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await page.locator('#view-settings [data-tab="sources"]').click();
  const card = page.locator('.dp-settings-provider-card--realdebrid');
  await card.locator('.dp-settings-disclosure').click();
  return {card, region: card.locator('[data-realdebrid-connection]'), calls};
}

/* Every visible text line and button of the main body sits on the body's own
 * centre line, and the body itself centres its text. */
async function expectCentred(region) {
  const offsets = await region.evaluate(node => {
    const box = node.getBoundingClientRect();
    const centre = box.left + box.width / 2;
    return {
      align: getComputedStyle(node).textAlign,
      off: [...node.children].filter(child => child.getClientRects().length).map(child => {
        const own = child.getBoundingClientRect();
        // A paragraph spans the column; what is centred is its text.
        const range = document.createRange();
        range.selectNodeContents(child);
        const content = child.tagName === 'BUTTON' ? own : range.getBoundingClientRect();
        return Math.abs(content.left + content.width / 2 - centre);
      }),
    };
  });
  expect(offsets.align).toBe('center');
  expect(offsets.off.length).toBeGreaterThan(0);
  for (const off of offsets.off) expect(off).toBeLessThan(2);
}

test('the Real-Debrid card uses the shipped mark and keeps Additional Settings for tuning alone', async ({page}) => {
  const {card, region} = await prepare(page);
  const logo = card.locator('.dp-settings-provider-logo--realdebrid');
  await expect(logo).toHaveAttribute('src', '/icons/providers/real-debrid.svg');
  expect(await logo.evaluate(image => image.complete && image.naturalWidth > 0)).toBe(true);
  await expect(card.locator('.dp-settings-provider-header-copy'))
    .toHaveText('Resolve supported links and torrents through your Real-Debrid account.');
  await expect(card.locator('.dp-settings-card-header [data-action="test-realdebrid"]')).toBeVisible();
  // Real-Debrid is opt-in: unconnected and switched off until the operator says so.
  await expect(card.locator('[data-integration-enabled="realdebrid"]')).not.toBeChecked();

  await expect(region.locator('[data-action="connect-realdebrid"]')).toHaveText('Connect Real-Debrid');
  await expectCentred(region);

  await card.locator('.dp-settings-additional > summary').click();
  const tuning = card.locator('.dp-settings-additional-body');
  await expect(tuning.locator('.dp-settings-tuning-grid')).toHaveCount(1);
  await expect(tuning.locator('[data-setting]')).toHaveCount(1);
  await expect(tuning.locator('[data-setting="realdebrid_rate_limit_per_minute"]')).toHaveValue('240');
  await expect(tuning.locator('button')).toHaveCount(0);
  expect(await tuning.innerText()).not.toMatch(/authori|connect|token|code/i);
});

test('authorization opens in the operator browser, polls, connects and disconnects in one centred column',
  async ({page}) => {
    const {region, calls} = await prepare(page);
    const before = page.url();
    await region.locator('[data-action="connect-realdebrid"]').click();
    await expect(region.locator('.dp-settings-realdebrid-code')).toHaveText('ABCD1234');
    await expect(region).toContainText('Waiting for authorization…');
    await expectCentred(region);

    await region.locator('[data-action="open-realdebrid"]').click();
    expect(await page.evaluate(() => window.__opened)).toEqual(
      [['https://real-debrid.com/device', '_blank', 'noopener,noreferrer']]);
    expect(page.url()).toBe(before);

    // The browser polls no faster than Real-Debrid asked (interval 5 s).
    expect(calls).not.toContain('POST /api/integrations/realdebrid/authorization/poll');
    await page.clock.fastForward(5000);
    await expect(region).toContainText('Connected as alice');
    await expect(region).toContainText('Premium · Expires 31.01.2027');
    expect(calls).toContain('POST /api/integrations/realdebrid/authorization/poll');
    await expectCentred(region);

    await region.locator('[data-action="disconnect-realdebrid"]').click();
    await page.locator('.dp-modal-dialog [data-modal-accept]').click();
    await expect(region.locator('[data-action="connect-realdebrid"]')).toBeVisible();
    expect(calls).toContain('POST /api/integrations/realdebrid/disconnect');
  });

test('Cancel abandons the authorization without polling, and Test proves the stored credential', async ({page}) => {
  const {card, region, calls} = await prepare(page);
  await region.locator('[data-action="connect-realdebrid"]').click();
  await region.locator('[data-action="cancel-realdebrid"]').click();
  await expect(region.locator('[data-action="connect-realdebrid"]')).toBeVisible();
  await page.clock.fastForward(6000);
  expect(calls).toContain('DELETE /api/integrations/realdebrid/authorization');
  expect(calls).not.toContain('POST /api/integrations/realdebrid/authorization/poll');

  await card.locator('[data-action="test-realdebrid"]').click();
  await expect.poll(() => calls).toContain('POST /api/settings/validate-realdebrid');
});
