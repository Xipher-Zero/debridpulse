const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Real-Debrid: the Premium Services card beside AllDebrid.
 *
 * Every Real-Debrid endpoint is answered here, so no account is needed and no
 * canonical state is written: this file owns no settings key. What it proves
 * is the card's own contract -- the shipped mark, a main body that is one
 * bordered Account Connection island in every connection state (what it is on
 * the left, its action on the right) with the state's one line centred
 * beneath it, an authorization that opens in the operator's own browser and
 * never navigates DebridPulse, a connection that switches the provider on
 * without a reload, and an Additional Settings region that holds the four
 * tunables and nothing else. */

const PENDING = {state: 'pending', user_code: 'ABCD1234', verification_url: 'https://real-debrid.com/device',
                 interval: 5, expires_in: 600};
const ACCOUNT = {username: 'alice', account_type: 'premium', premium: true, premium_seconds: 9,
                 expiration: '2027-01-31T10:00:00.000Z'};

/* A successful connection comes back configured, verified AND enabled -- the
 * server's auto-enable; a disconnect leaves the operator's enable state alone. */
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

/* The island in this state: the Extraction island's geometry -- bordered,
 * content-bounded and centred in the card body (never stretched across spare
 * width) -- with its copy and its controls side by side, the controls centred
 * against the island, nothing overlapping and nothing overflowing. */
async function expectIsland(region) {
  const geometry = await region.evaluate(node => {
    const island = node.querySelector('.dp-settings-realdebrid-island');
    const copy = island.querySelector('.dp-settings-realdebrid-island-copy').getBoundingClientRect();
    const actions = island.querySelector('.dp-settings-realdebrid-island-actions').getBoundingClientRect();
    const box = island.getBoundingClientRect();
    const body = node.getBoundingClientRect();
    return {
      compact: box.width < body.width - 40,
      centred: Math.abs((box.left - body.left) - (body.right - box.right)),
      border: getComputedStyle(island).borderTopStyle,
      heading: island.querySelector('.dp-settings-realdebrid-heading').textContent,
      copyLeft: copy.left - box.left,
      gap: actions.left - copy.right,
      rightInset: box.right - actions.right,
      centre: Math.abs((actions.top + actions.bottom) / 2 - (box.top + box.bottom) / 2),
      overflow: island.scrollWidth - island.clientWidth,
    };
  });
  expect(geometry.border).toBe('solid');
  expect(geometry.compact).toBe(true);
  expect(geometry.centred).toBeLessThan(2);
  expect(geometry.heading).toBe('Account Connection');
  expect(geometry.copyLeft).toBeLessThan(30);
  expect(geometry.gap).toBeGreaterThan(0);
  expect(geometry.rightInset).toBeLessThan(30);
  expect(geometry.centre).toBeLessThan(2);
  expect(geometry.overflow).toBeLessThanOrEqual(0);
}

/* The card's reserved top row: the region's first line, directly above the
 * island, centred on it, and one line tall whatever it says. Returns its text
 * and the card's height so a caller can prove the card never changes size. */
async function reservedRow(card, region) {
  const row = await region.evaluate(node => {
    const line = node.firstElementChild;
    const island = node.querySelector('.dp-settings-realdebrid-island').getBoundingClientRect();
    const box = line.getBoundingClientRect();
    const range = document.createRange();
    range.selectNodeContents(line);
    const text = range.getBoundingClientRect();
    return {
      reserved: line.classList.contains('dp-settings-provider-status-line'),
      // The island ends the region: no blank row beneath it.
      last: node.lastElementChild.classList.contains('dp-settings-realdebrid-island'),
      text: line.textContent,
      above: island.top - box.bottom,
      height: box.height,
      off: line.textContent ? Math.abs((text.left + text.right) / 2 - (island.left + island.right) / 2) : 0,
    };
  });
  expect(row.reserved).toBe(true);
  expect(row.last).toBe(true);
  expect(row.above).toBeGreaterThanOrEqual(0);
  expect(row.height).toBeGreaterThan(0);
  expect(row.off).toBeLessThan(2);
  // Island -> ordinary section spacing -> Additional Settings: no separator
  // drawn above the disclosure and no balancing band between them.
  const below = await card.evaluate(node => {
    const island = node.querySelector('.dp-settings-realdebrid-island').getBoundingClientRect();
    const additional = node.querySelector('.dp-settings-additional');
    return {gap: additional.getBoundingClientRect().top - island.bottom,
            border: parseFloat(getComputedStyle(additional).borderTopWidth)};
  });
  expect(below.border).toBe(0);
  expect(below.gap).toBeGreaterThan(0);
  expect(below.gap).toBeLessThanOrEqual(16);
  return {...row, card: (await card.boundingBox()).height};
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

  // No provider description: the card's reserved top row is there, and blank.
  await expect(card.locator('.card-body .dp-settings-copy', {hasText: 'Connect DebridPulse to'})).toHaveCount(0);
  const blank = await reservedRow(card, region);
  expect(blank.text).toBe('');
  await expect(region.locator('.dp-settings-realdebrid-island-actions [data-action="connect-realdebrid"]'))
    .toHaveText('Connect Real-Debrid');
  await expect(region.locator('[data-realdebrid-code], .dp-settings-realdebrid-waiting')).toHaveCount(0);
  await expect(region.locator('[data-action="disconnect-realdebrid"]')).toHaveCount(0);
  await expectIsland(region);

  await card.locator('.dp-settings-additional > summary').click();
  const tuning = card.locator('.dp-settings-additional-body');
  await expect(tuning.locator('.dp-settings-tuning-grid')).toHaveCount(1);
  await expect(tuning.locator('[data-setting]')).toHaveCount(4);
  for (const [key, value, min, max] of [
    ['realdebrid_rate_limit_per_minute', '240', '1', '250'],
    ['realdebrid_request_timeout_seconds', '30', '5', '300'],
    ['realdebrid_torrent_upload_timeout_seconds', '120', '30', '900'],
    ['realdebrid_host_refresh_interval_hours', '24', '1', '168'],
  ]) {
    const control = tuning.locator(`[data-setting="${key}"]`);
    await expect(control).toHaveValue(value);
    await expect(control).toHaveAttribute('min', min);
    await expect(control).toHaveAttribute('max', max);
    // Field-boundary persistence through the one scoped integration owner.
    await expect(control).toHaveAttribute('data-commit', 'changed-blur');
    await expect(control).toHaveAttribute('data-commit-scope', 'integration:realdebrid');
  }
  await expect(tuning.locator('button')).toHaveCount(0);
  expect(await tuning.innerText()).not.toMatch(/authori|connect|token|code/i);
});

test('authorization opens in the operator browser, polls, connects, enables and disconnects through the island',
  async ({page}) => {
    const {card, region, calls} = await prepare(page);
    const before = page.url();
    const disconnected = await reservedRow(card, region);
    await region.locator('[data-action="connect-realdebrid"]').click();
    const code = region.locator('.dp-settings-realdebrid-island-actions .dp-action-field input[data-realdebrid-code]');
    await expect(code).toHaveValue('ABCD1234');
    await expect(code).toHaveAttribute('readonly', '');
    await expect(region.locator('.dp-action-field [data-action="copy-realdebrid-code"]')).toHaveText('Copy');
    // Keyboard-reachable and selectable: the code is a focusable read-only field.
    await code.focus();
    expect(await code.evaluate(field => { field.select(); return field.selectionEnd - field.selectionStart; })).toBe(8);
    await expect(region.locator('.dp-settings-realdebrid-island-actions [data-action="open-realdebrid"]')).toBeVisible();
    await expect(region.locator('.dp-settings-realdebrid-island-actions [data-action="cancel-realdebrid"]')).toBeVisible();
    await expect(region.locator('.dp-settings-realdebrid-waiting')).toHaveText('Waiting for authorization…');
    await expectIsland(region);
    // The wait is the reserved row's text: same row, same card height.
    const connecting = await reservedRow(card, region);
    expect(connecting.text).toBe('Waiting for authorization…');
    // Larger than the row's own line, and still inside the row's height.
    const wait = await region.locator('.dp-settings-realdebrid-waiting').evaluate(node => ({
      size: parseFloat(getComputedStyle(node).fontSize),
      row: parseFloat(getComputedStyle(node.parentElement).fontSize),
      inside: node.getBoundingClientRect().height <= node.parentElement.getBoundingClientRect().height + 0.5,
    }));
    expect(wait.size).toBeGreaterThan(wait.row);
    expect(wait.inside).toBe(true);
    expect(connecting.height).toBeCloseTo(disconnected.height, 0);
    expect(connecting.card).toBeCloseTo(disconnected.card, 0);

    await region.locator('[data-action="open-realdebrid"]').click();
    expect(await page.evaluate(() => window.__opened)).toEqual(
      [['https://real-debrid.com/device', '_blank', 'noopener,noreferrer']]);
    expect(page.url()).toBe(before);

    // The browser polls no faster than Real-Debrid asked (interval 5 s).
    expect(calls).not.toContain('POST /api/integrations/realdebrid/authorization/poll');
    await page.clock.fastForward(5000);
    await expect(region.locator('.dp-settings-realdebrid-island')).toContainText('Connected as alice');
    await expect(region.locator('.dp-settings-realdebrid-expiry')).toHaveText(/^Premium until 31\.01\.2027 \(\d+ days remaining\)$/);
    await expect(region.locator('[data-realdebrid-code], .dp-settings-realdebrid-waiting')).toHaveCount(0);
    await expect(region.locator('[data-action="connect-realdebrid"], [data-action="cancel-realdebrid"]'))
      .toHaveCount(0);
    expect(calls).toContain('POST /api/integrations/realdebrid/authorization/poll');
    // The accepted projection switches the provider on with no reload.
    await expect(card.locator('[data-integration-enabled="realdebrid"]')).toBeChecked();
    await expectIsland(region);
    const connected = await reservedRow(card, region);
    // Only the wait is emphasised: the expiry is ordinary card copy.
    const sizes = await card.evaluate(node => ({
      expiry: parseFloat(getComputedStyle(node.querySelector('.dp-settings-realdebrid-expiry')).fontSize),
      copy: parseFloat(getComputedStyle(node.querySelector('.dp-settings-realdebrid-island-copy > .dp-settings-copy')).fontSize),
    }));
    expect(sizes.expiry).toBe(sizes.copy);
    expect(connected.text).toMatch(/^Premium until 31\.01\.2027 \(\d+ days remaining\)$/);
    expect(connected.height).toBeCloseTo(disconnected.height, 0);
    expect(connected.card).toBeCloseTo(disconnected.card, 0);

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
