const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Settings interaction foundation -- items 5, 6, 7, 8 and 10.
 *
 * The Services page against the REAL backend. Every interactive control has
 * exactly ONE commit class, decided by the semantics and risk of the setting
 * and never by the page it happens to appear on:
 *
 *   immediate       provider / group Enable
 *   changed-blur    ordinary scalars AND credential entry/replacement
 *   explicit-action Test, Clear, Add / Remove Server, Browse
 *
 * DP 1.0.13 credential contract: entering or replacing a credential is an
 * ordinary value change and commits on blur through the integration's existing
 * scoped mutation; ERASING one is an explicit, confirmed Clear; Test tests and
 * never saves. The browser never retains a secret as an accepted baseline.
 */

const RATE_LIMIT = '#dp-settings-field-alldebrid-rate-limit-per-minute';
const POLL_INTERVAL = '#dp-settings-field-poll-interval-seconds';
const FULL_SYNC = '#dp-settings-field-full-sync-interval-minutes';
const API_KEY = '#dp-settings-field-alldebrid-api-key';

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

async function openSources(page) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator('#view-settings [data-tab="sources"]').click();
  await expect(page.locator('.dp-settings-panel[data-panel="sources"]')).toBeVisible();
}

/* The AllDebrid card body is hidden while the integration is switched off.
 * Expanding the card is LOCAL presentation and writes no canonical state, so
 * this spec never depends on another spec's enable/disable timing against the
 * shared backend. */
async function revealAllDebrid(page) {
  const card = page.locator('.dp-settings-provider-card--alldebrid');
  const disclosure = card.locator('.dp-settings-disclosure');
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
  await expect(card.locator(':scope > .card-body')).toBeVisible();
}

/* Services cards render COLLAPSED: expansion is LOCAL presentation state,
 * never a projection of enabled/configured/verified state. Opening one through
 * the canonical disclosure writes no canonical state, so this spec never
 * depends on another spec's enable/disable timing against the shared backend.
 * The Network Sources members live inside that group's body. */
async function revealNetworkSources(page) {
  const group = page.locator('.dp-settings-general-sources');
  const disclosure = group.locator('.dp-settings-disclosure');
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
  await expect(group.locator('.dp-settings-provider-card--general-http')).toBeVisible();
}

/** Open the AllDebrid card's Additional Settings disclosure. */
async function openAdditional(page) {
  const details = page.locator('.dp-settings-provider-card--alldebrid .dp-settings-additional');
  if (!(await details.evaluate(el => el.open))) {
    await page.locator('.dp-settings-provider-card--alldebrid .dp-settings-additional > summary').click();
  }
  await expect(page.locator(RATE_LIMIT)).toBeVisible();
}

const settings = page => page.request.get('/api/settings').then(r => r.json());
const toasts = page => page.locator('#toasts .toast');
const clearButton = page => page.locator('[data-action="clear-alldebrid-key"]');

/* The ONE canonical Settings confirmation (ui-settings-modal.js). A destructive
 * action opens it; this spec drives that one dialog and never a card-local
 * confirmation, because there no longer is one. */
const dangerDialog = page => page.locator('.dp-modal-overlay .dp-modal-dialog[data-tone="danger"]');
const acceptDanger = page => dangerDialog(page).locator('[data-modal-accept]');
const cancelDanger = page => dangerDialog(page).locator('[data-modal-cancel]');

async function clearToasts(page) {
  await page.evaluate(() => { const host = document.getElementById('toasts'); if (host) host.innerHTML = ''; });
}

/* Record every canonical SETTINGS MUTATION the page issues, in order.
 *
 * Only the canonical mutation surfaces count. Diagnostic probes -- the status
 * bar's periodic `POST /settings/test-aria2`, `validate-alldebrid`, and the
 * like -- write nothing, so a test asserting "this changed no setting" must
 * not see them. */
const MUTATION_SURFACES = [
  /^\/api\/settings$/,
  /^\/api\/integrations\/[^/]+\/configuration$/,
  /^\/api\/integration-groups\/[^/]+\/configuration$/,
  /^\/api\/transfer-policy$/,
  /^\/api\/execution\/runtime-limits$/,
];

function mutations(page) {
  const seen = [];
  page.on('request', request => {
    const url = new URL(request.url());
    if (!MUTATION_SURFACES.some(surface => surface.test(url.pathname))) return;
    if (!['PATCH', 'PUT', 'POST'].includes(request.method())) return;
    let body = null;
    try { body = request.postDataJSON(); } catch (_) {}
    seen.push({method: request.method(), path: url.pathname, body});
  });
  return seen;
}

const scoped = (seen, path) => seen.filter(entry => entry.path === path);

/* What the canonical persistence owner currently believes the server has
 * accepted for one control -- the comparison every dirty check is made
 * against. */
const committedBaseline = (page, selector) => page.evaluate(sel =>
  window.DPSettingsPersistence.baseline(document.querySelector(sel)), selector);

/* Hold the NEXT AllDebrid configuration write, so gated intent can be created
 * while that request is genuinely in flight. Later writes pass through. */
async function delayNextAllDebridWrite(page, ms) {
  let held = false;
  await page.route(url => url.pathname === '/api/integrations/alldebrid/configuration',
    async route => {
      if (route.request().method() === 'PATCH' && !held) {
        held = true;
        await new Promise(resolve => setTimeout(resolve, ms));
      }
      await route.continue();
    });
}

let baseline = null;

test.beforeEach(async ({page}) => {
  await isolateExternalFonts(page);
  await page.goto('/');
  await openSources(page);
  await revealAllDebrid(page);
  const current = await settings(page);
  baseline = {
    rate_limit_per_minute: current.integrations.alldebrid.options.rate_limit_per_minute,
    provider_poll_interval_seconds: current.transfer_policy.provider_poll_interval_seconds,
    full_sync_interval_minutes: current.full_sync_interval_minutes,
  };
  await clearToasts(page);
});

test.afterEach(async ({page}) => {
  await page.unrouteAll({behavior: 'ignoreErrors'}).catch(() => {});
  await page.request.patch('/api/integrations/alldebrid/configuration',
    {data: {options: {rate_limit_per_minute: baseline.rate_limit_per_minute},
            clear_secrets: ['api_key']}});
  await page.request.patch('/api/transfer-policy',
    {data: {provider_poll_interval_seconds: baseline.provider_poll_interval_seconds}});
});

// --- 6.2 changed-blur -----------------------------------------------------

test('focusing and leaving an unchanged field causes zero mutation', async ({page}) => {
  const seen = mutations(page);
  await openAdditional(page);
  await page.locator(RATE_LIMIT).focus();
  await page.locator(RATE_LIMIT).blur();
  await page.locator(POLL_INTERVAL).focus();
  await page.locator(POLL_INTERVAL).blur();
  await page.waitForTimeout(700);
  expect(seen).toEqual([]);
});

test('a changed field persists on blur through its own scoped owner alone', async ({page}) => {
  const seen = mutations(page);
  await openAdditional(page);
  const next = Number(baseline.rate_limit_per_minute) + 7;
  await page.locator(RATE_LIMIT).fill(String(next));
  await page.locator(RATE_LIMIT).blur();

  await expect.poll(async () =>
    (await settings(page)).integrations.alldebrid.options.rate_limit_per_minute).toBe(next);
  const writes = scoped(seen, '/api/integrations/alldebrid/configuration');
  expect(writes).toHaveLength(1);
  // Only the field that changed travels; nothing unrelated is replayed.
  expect(Object.keys(writes[0].body.options)).toEqual(['rate_limit_per_minute']);
  expect(seen.filter(entry => entry.path === '/api/settings')).toHaveLength(0);
  // Ordinary success is silent -- no second notification system, no toast spam.
  await expect(toasts(page)).toHaveCount(0);
});

test('the accepted value becomes the baseline, so a second blur writes nothing', async ({page}) => {
  await openAdditional(page);
  const next = Number(baseline.rate_limit_per_minute) + 9;
  await page.locator(RATE_LIMIT).fill(String(next));
  await page.locator(RATE_LIMIT).blur();
  await expect.poll(async () =>
    (await settings(page)).integrations.alldebrid.options.rate_limit_per_minute).toBe(next);

  const seen = mutations(page);
  await page.locator(RATE_LIMIT).focus();
  await page.locator(RATE_LIMIT).blur();
  await page.waitForTimeout(700);
  expect(seen).toEqual([]);
});

test('a policy field writes only the universal transfer-policy namespace', async ({page}) => {
  const seen = mutations(page);
  await openAdditional(page);
  const next = Number(baseline.provider_poll_interval_seconds) + 5;
  await page.locator(POLL_INTERVAL).fill(String(next));
  await page.locator(POLL_INTERVAL).blur();

  await expect.poll(async () =>
    (await settings(page)).transfer_policy.provider_poll_interval_seconds).toBe(next);
  const writes = scoped(seen, '/api/transfer-policy');
  expect(writes).toHaveLength(1);
  expect(Object.keys(writes[0].body)).toEqual(['provider_poll_interval_seconds']);
});

test('a failed write converges the control to canonical state and says so', async ({page}) => {
  await openAdditional(page);
  await page.route(url => url.pathname === '/api/integrations/alldebrid/configuration',
    route => route.fulfill({status: 502, contentType: 'application/json',
      body: JSON.stringify({detail: 'integration configuration rejected'})}));

  await page.locator(RATE_LIMIT).fill('123');
  await page.locator(RATE_LIMIT).blur();

  await expect(toasts(page).first()).toContainText(/reject|error|fail/i);
  // No false optimistic value is left on screen.
  await expect(page.locator(RATE_LIMIT)).toHaveValue(String(baseline.rate_limit_per_minute));
  expect((await settings(page)).integrations.alldebrid.options.rate_limit_per_minute)
    .toBe(baseline.rate_limit_per_minute);
});

test('an older in-flight response can never overwrite a newer edit', async ({page}) => {
  await openAdditional(page);
  let first = true;
  await page.route(url => url.pathname === '/api/transfer-policy', async route => {
    if (route.request().method() === 'PATCH' && first) {
      first = false;
      await new Promise(resolve => setTimeout(resolve, 1500));
    }
    await route.continue();
  });

  const older = Number(baseline.provider_poll_interval_seconds) + 11;
  const newer = Number(baseline.provider_poll_interval_seconds) + 12;
  await page.locator(POLL_INTERVAL).fill(String(older));
  await page.locator(POLL_INTERVAL).blur();
  await page.locator(POLL_INTERVAL).fill(String(newer));
  await page.locator(POLL_INTERVAL).blur();

  await expect.poll(async () =>
    (await settings(page)).transfer_policy.provider_poll_interval_seconds,
    {timeout: 15000}).toBe(newer);
  await page.waitForTimeout(1200);
  await expect(page.locator(POLL_INTERVAL)).toHaveValue(String(newer));
  expect((await settings(page)).transfer_policy.provider_poll_interval_seconds).toBe(newer);
});

// --- 11/12/13 the credential contract ------------------------------------

/* Establish a stored key through the canonical scoped mutation, so a case that
 * needs "already configured" never depends on the UI path it is testing. */
async function preconfigure(page, key = 'DP-PRESET') {
  await page.request.patch('/api/integrations/alldebrid/configuration',
    {data: {options: {api_key: key}}});
  await page.reload();
  await openSources(page);
  await revealAllDebrid(page);
}

test('no localized Save survives, and Test is the card header rail\'s one action', async ({page}) => {
  await expect(page.locator('[data-action="save-alldebrid"]')).toHaveCount(0);
  const card = page.locator('.dp-settings-provider-card--alldebrid');
  // The provider action footer is gone entirely; Test is a header action.
  await expect(card.locator('.dp-settings-provider-actions')).toHaveCount(0);
  await expect(card.locator(':scope > .card-header .dp-settings-header-action button'))
    .toHaveCount(1);
  await expect(card.locator(':scope > .card-header [data-action="test-alldebrid"]'))
    .toHaveCount(1);
  await expect(card.locator(':scope > .card-body [data-action="test-alldebrid"]'))
    .toHaveCount(0);
});

test('entering an API key commits on blur through the existing integration mutation',
  async ({page}) => {
    const seen = mutations(page);
    await page.locator(API_KEY).fill('DP-BLUR-COMMIT');
    await page.locator(API_KEY).blur();

    await expect.poll(async () =>
      (await settings(page)).integrations.alldebrid.options.api_key_configured).toBe(true);
    const writes = scoped(seen, '/api/integrations/alldebrid/configuration');
    expect(writes).toHaveLength(1);
    expect(writes[0].method).toBe('PATCH');
    expect(writes[0].body.options.api_key).toBe('DP-BLUR-COMMIT');
    // Only the credential travelled; nothing unrelated was replayed.
    expect(Object.keys(writes[0].body.options)).toEqual(['api_key']);
    expect(scoped(seen, '/api/settings')).toHaveLength(0);
  });

test('the accepted presentation is blank, and no secret becomes the baseline',
  async ({page}) => {
    await page.locator(API_KEY).fill('DP-NOT-RETAINED');
    await page.locator(API_KEY).blur();
    await expect.poll(async () =>
      (await settings(page)).integrations.alldebrid.options.api_key_configured).toBe(true);

    // The visible field returns to its blank / configured presentation ...
    await expect(page.locator(API_KEY)).toHaveValue('');
    // ... and the canonical persistence owner holds no secret for it.
    expect(await committedBaseline(page, API_KEY)).toBe('');
    // A second blur therefore writes nothing at all.
    const seen = mutations(page);
    await page.locator(API_KEY).focus();
    await page.locator(API_KEY).blur();
    await page.waitForTimeout(700);
    expect(seen).toEqual([]);
  });

/* That a credential change RETIRES durable verification is derived, not
 * asserted, and its behavioural owner is the canonical configuration merge --
 * proved against real evidence in
 * backend/tests/test_v113_provider_verification_evidence.py
 * (test_a_verification_relevant_change_retires_the_evidence). A browser cannot
 * establish real evidence without a real account, so what is proved HERE is
 * the part the browser owns: the accepted projection is what the card reports,
 * and it moves on every accepted credential write. */
test('the card reports the accepted projection after a credential write', async ({page}) => {
  const card = page.locator('.dp-settings-provider-card--alldebrid');
  const status = card.locator('.dp-settings-provider-config-status');

  await page.request.patch('/api/integrations/alldebrid/configuration',
    {data: {options: {}, clear_secrets: ['api_key']}});
  await page.reload();
  await openSources(page);
  await revealAllDebrid(page);
  expect((await settings(page)).integrations.alldebrid.configured).toBe(false);

  await page.locator(API_KEY).fill('DP-PROJECTED-KEY');
  await page.locator(API_KEY).blur();
  await expect.poll(async () =>
    (await settings(page)).integrations.alldebrid.configured).toBe(true);

  // Configured, never Verified: nothing has proven this credential works, and
  // no owner in the browser may claim otherwise.
  await expect(status).toHaveText('Configured');
  expect((await settings(page)).integrations.alldebrid.verified).toBe(false);
  // The row now offers the explicit Clear, because a key is present.
  await expect(clearButton(page)).toHaveCount(1);
});

test('a failed credential write rolls back to the safe blank state and says so',
  async ({page}) => {
    await page.route(url => url.pathname === '/api/integrations/alldebrid/configuration',
      route => route.fulfill({status: 502, contentType: 'application/json',
        body: JSON.stringify({detail: 'credential rejected'})}));
    await page.locator(API_KEY).fill('DP-REFUSED');
    await page.locator(API_KEY).blur();

    await expect(toasts(page).first()).toContainText(/reject|error|fail/i);
    await expect(page.locator(API_KEY)).toHaveValue('');
    expect((await settings(page)).integrations.alldebrid.options.api_key_configured).toBe(false);
  });

test('Clear is the explicit red action, and it asks the canonical confirmation', async ({page}) => {
  await preconfigure(page);
  // No card-local gate survives: the action is available, and the dialog asks.
  await expect(clearButton(page)).toBeEnabled();
  await expect(clearButton(page)).toHaveText('Clear Stored API Key');
  await expect(clearButton(page)).toHaveClass(/btn-danger/);
  await expect(page.locator('[data-alldebrid-clear-confirm]')).toHaveCount(0);

  const seen = mutations(page);
  await clearButton(page).click();
  // Opening the dialog is not a mutation.
  await expect(dangerDialog(page)).toBeVisible();
  await expect(dangerDialog(page).locator('.dp-modal-title')).toHaveText('Clear AllDebrid API key?');
  await expect(acceptDanger(page)).toHaveText('Clear AllDebrid API Key');
  await expect(acceptDanger(page)).toHaveClass(/btn-danger/);
  await expect(cancelDanger(page)).toHaveText('Cancel');
  await expect(dangerDialog(page).locator('.dp-modal-message')).toContainText(/removed/i);
  expect(scoped(seen, '/api/integrations/alldebrid/configuration')).toHaveLength(0);
});

test('Cancelling the Clear confirmation performs no mutation', async ({page}) => {
  await preconfigure(page);
  const seen = mutations(page);
  await clearButton(page).click();
  await expect(dangerDialog(page)).toBeVisible();
  await cancelDanger(page).click();
  await expect(dangerDialog(page)).toHaveCount(0);

  await page.waitForTimeout(500);
  expect(scoped(seen, '/api/integrations/alldebrid/configuration')).toHaveLength(0);
  // Accepted state is untouched, and so is the action that offers it.
  expect((await settings(page)).integrations.alldebrid.options.api_key_configured).toBe(true);
  await expect(clearButton(page)).toBeEnabled();
});

test('a confirmed Clear erases the stored key exactly once', async ({page}) => {
  await preconfigure(page);
  const seen = mutations(page);
  await clearButton(page).click();
  await acceptDanger(page).click();

  await expect.poll(async () =>
    (await settings(page)).integrations.alldebrid.options.api_key_configured).toBe(false);
  const writes = scoped(seen, '/api/integrations/alldebrid/configuration');
  expect(writes).toHaveLength(1);
  expect(writes[0].body.clear_secrets).toEqual(['api_key']);
  // Clear never also saves a replacement key.
  expect(writes[0].body.options).toEqual({});
  // The row now reports "no key", so the action it offered is gone with it.
  await expect(clearButton(page)).toHaveCount(0);
});

test('a failed Clear renders no false cleared state', async ({page}) => {
  await preconfigure(page);
  await page.route(url => url.pathname === '/api/integrations/alldebrid/configuration',
    route => route.request().method() === 'PATCH'
      ? route.fulfill({status: 502, contentType: 'application/json',
          body: JSON.stringify({detail: 'clear rejected'})})
      : route.continue());
  await clearButton(page).click();
  await acceptDanger(page).click();

  await expect(toasts(page).first()).toContainText(/reject|error|fail/i);
  // The row still says a key is stored, because one still is.
  await expect(clearButton(page)).toBeVisible();
  await expect(clearButton(page)).toBeEnabled();
  await page.unrouteAll({behavior: 'ignoreErrors'}).catch(() => {});
  expect((await settings(page)).integrations.alldebrid.options.api_key_configured).toBe(true);
});

test('Clear is the only path that erases a stored key', async ({page}) => {
  await preconfigure(page);
  // Leaving the credential field blank is "no replacement", never a removal.
  const seen = mutations(page);
  await page.locator(API_KEY).focus();
  await page.locator(API_KEY).blur();
  await page.waitForTimeout(700);
  expect(seen).toEqual([]);
  expect((await settings(page)).integrations.alldebrid.options.api_key_configured).toBe(true);
});

// --- 7 / 8 draft Test and deterministic ordering --------------------------

test('Test settles the pending credential commit first, and never saves it itself',
  async ({page}) => {
    const order = [];
    page.on('requestfinished', request => {
      if (new URL(request.url()).pathname === '/api/integrations/alldebrid/configuration') {
        order.push('commit-finished');
      }
    });
    let probed = null;
    await page.route(url => url.pathname === '/api/settings/validate-alldebrid', async route => {
      probed = route.request().postDataJSON();
      order.push('test-sent');
      await route.fulfill({status: 200, contentType: 'application/json',
        body: JSON.stringify({ok: true, username: 'settled-account'})});
    });

    await page.locator(API_KEY).fill('DP-SETTLED-KEY');
    // No explicit blur: clicking Test is what removes focus.
    await page.locator('[data-action="test-alldebrid"]').click();

    await expect(toasts(page).first()).toContainText('settled-account');
    expect(order).toEqual(['commit-finished', 'test-sent']);
    // The credential was persisted by its OWN boundary, so Test read the saved
    // configuration rather than carrying a draft secret.
    expect((await settings(page)).integrations.alldebrid.options.api_key_configured).toBe(true);
    expect(probed.api_key).toBe('');
    expect(probed.clear_api_key).toBeUndefined();
  });

test('an edited ordinary field is committed before Test reads the form', async ({page}) => {
  const order = [];
  page.on('requestfinished', request => {
    if (new URL(request.url()).pathname === '/api/integrations/alldebrid/configuration') {
      order.push('commit-finished');
    }
  });
  page.on('request', request => {
    if (new URL(request.url()).pathname === '/api/settings/validate-alldebrid') {
      order.push('test-sent');
    }
  });
  await page.route(url => url.pathname === '/api/settings/validate-alldebrid',
    route => route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({ok: true, username: 'ordered'})}));

  await openAdditional(page);
  const next = Number(baseline.rate_limit_per_minute) + 13;
  await page.locator(RATE_LIMIT).fill(String(next));
  // No explicit blur: clicking Test is what removes focus.
  await page.locator('[data-action="test-alldebrid"]').click();

  await expect(toasts(page).first()).toContainText('ordered');
  expect(order).toEqual(['commit-finished', 'test-sent']);
  expect((await settings(page)).integrations.alldebrid.options.rate_limit_per_minute).toBe(next);
});

// --- 6.1 immediate + 10 the footer cannot replay -------------------------

test('the provider Enable toggle stays immediate and is never replayed by the footer',
  async ({page}) => {
    const before = (await settings(page)).integrations.general_http.enabled;
    await revealNetworkSources(page);
    await page.locator('label[for="dp-settings-integration-general_http-enabled"]').click();
    await expect.poll(async () => (await settings(page)).integrations.general_http.enabled)
      .toBe(!before);

    // Canonical state moves underneath the rendered toggle; a later footer
    // Apply must not replay what the page still shows.
    await page.request.patch('/api/integrations/general_http/configuration',
      {data: {enabled: before}});
    await page.locator('#view-settings button[data-action="save"]:visible').first().click();
    await expect(toasts(page).first()).toBeVisible();
    expect((await settings(page)).integrations.general_http.enabled).toBe(before);
  });

test('the footer cannot replay a migrated policy field over newer canonical state',
  async ({page}) => {
    await openAdditional(page);
    const committed = Number(baseline.provider_poll_interval_seconds) + 15;
    await page.locator(POLL_INTERVAL).fill(String(committed));
    await page.locator(POLL_INTERVAL).blur();
    await expect.poll(async () =>
      (await settings(page)).transfer_policy.provider_poll_interval_seconds).toBe(committed);

    const newer = committed + 1;
    await page.request.patch('/api/transfer-policy',
      {data: {provider_poll_interval_seconds: newer}});

    await page.locator('#view-settings button[data-action="save"]:visible').first().click();
    await expect(toasts(page).first()).toBeVisible();
    await page.waitForTimeout(500);
    expect((await settings(page)).transfer_policy.provider_poll_interval_seconds).toBe(newer);
  });

test('the footer cannot replay a migrated top-level field over newer canonical state',
  async ({page}) => {
    await openAdditional(page);
    const committed = Number(baseline.full_sync_interval_minutes) + 3;
    await page.locator(FULL_SYNC).fill(String(committed));
    await page.locator(FULL_SYNC).blur();
    await expect.poll(async () => (await settings(page)).full_sync_interval_minutes).toBe(committed);

    const newer = committed + 1;
    const document = await settings(page);
    for (const drop of ['integrations', 'integration_groups', 'transfer_policy',
                        'execution_runtime_limits']) delete document[drop];
    for (const name of document.compatibility_fields || []) delete document[name];
    delete document.compatibility_fields;
    document.full_sync_interval_minutes = newer;
    await page.request.put('/api/settings', {data: document});
    expect((await settings(page)).full_sync_interval_minutes).toBe(newer);

    await page.locator('#view-settings button[data-action="save"]:visible').first().click();
    await expect(toasts(page).first()).toBeVisible();
    await page.waitForTimeout(500);
    expect((await settings(page)).full_sync_interval_minutes).toBe(newer);
  });

test('the footer owns no AllDebrid credential write of its own', async ({page}) => {
  const seen = mutations(page);
  await page.locator(API_KEY).fill('DP-FOOTER-DRAFT');
  await page.locator('#view-settings button[data-action="save"]:visible').first().click();
  await expect(toasts(page).first()).toBeVisible();
  await page.waitForTimeout(500);
  // Apply Settings settles pending commits, so the credential is persisted by
  // its OWN changed-blur boundary -- through the integration mutation, never
  // through the whole-settings document.
  const writes = scoped(seen, '/api/integrations/alldebrid/configuration');
  expect(writes).toHaveLength(1);
  expect(writes[0].body.options.api_key).toBe('DP-FOOTER-DRAFT');
  const document = scoped(seen, '/api/settings');
  for (const write of document) {
    expect(JSON.stringify(write.body)).not.toContain('DP-FOOTER-DRAFT');
    expect(write.body.clear_secrets || []).not.toContain('alldebrid_api_key');
  }
});

test('the global Apply Settings control is still present', async ({page}) => {
  await expect(page.locator('#view-settings button[data-action="save"]')).toHaveCount(1);
  await expect(page.locator('#view-settings button[data-action="save"]')).toBeVisible();
});

// --- a credential write consumes only the draft it dispatched -------------

test('a credential write leaves a newer key typed while it was in flight alone',
  async ({page}) => {
    const seen = mutations(page);
    await delayNextAllDebridWrite(page, 1500);
    await page.locator(API_KEY).fill('DP-KEY-A');
    await page.locator(API_KEY).blur();
    // Newer intent, created while KEY-A is still on the wire.
    await page.locator(API_KEY).fill('DP-KEY-B');

    await expect.poll(async () =>
      (await settings(page)).integrations.alldebrid.options.api_key_configured,
      {timeout: 15000}).toBe(true);
    await page.waitForTimeout(700);

    // KEY-A is what was accepted ...
    const writes = scoped(seen, '/api/integrations/alldebrid/configuration');
    expect(writes[0].body.options.api_key).toBe('DP-KEY-A');
    // ... and KEY-B survived the row's re-render, dirty against the blank
    // accepted baseline, so it commits on its own blur.
    await expect(page.locator(API_KEY)).toHaveValue('DP-KEY-B');
    expect(await committedBaseline(page, API_KEY)).toBe('');

    await page.locator(API_KEY).blur();
    await expect.poll(() => scoped(seen, '/api/integrations/alldebrid/configuration').length).toBe(2);
    expect(scoped(seen, '/api/integrations/alldebrid/configuration')[1].body.options.api_key)
      .toBe('DP-KEY-B');
    await expect(page.locator(API_KEY)).toHaveValue('');
  });

/* A credential write converges the row. The destructive action is part of what
 * the row SHOWS about the stored credential, so the invariant is that the
 * re-render leaves it reachable and truthful -- there is no armed state to
 * preserve any more, because the question is asked when the operator acts. */
test('a credential write in flight leaves the Clear action reachable and truthful',
  async ({page}) => {
    await preconfigure(page);
    await delayNextAllDebridWrite(page, 1500);
    await page.locator(API_KEY).fill('DP-KEY-A');
    await page.locator(API_KEY).blur();          // dispatched with no removal intent

    await page.waitForTimeout(2200);
    // The row re-rendered on the accepted state; a key is still stored, so the
    // action it offers is still there and still enabled.
    await expect(clearButton(page)).toBeVisible();
    await expect(clearButton(page)).toBeEnabled();

    await clearButton(page).click();
    await acceptDanger(page).click();
    await expect.poll(async () =>
      (await settings(page)).integrations.alldebrid.options.api_key_configured).toBe(false);
  });

// --- an earlier queued write that SUCCEEDS is canonical knowledge ---------
//
// Supersession suppresses stale PRESENTATION, never what the server accepted.
// If an older queued write succeeds and a newer one then fails, the newer
// one's rollback must land on what the server actually holds -- not on the
// value it held before the older write was accepted.

test('an older write that succeeded is what a newer failed write rolls back to',
  async ({page}) => {
    await openAdditional(page);
    const first = Number(baseline.rate_limit_per_minute) + 21;
    const second = first + 1;

    let attempt = 0;
    await page.route(url => url.pathname === '/api/integrations/alldebrid/configuration',
      async route => {
        if (route.request().method() !== 'PATCH') return route.continue();
        attempt += 1;
        if (attempt === 1) {                       // the older write: slow, accepted
          await new Promise(resolve => setTimeout(resolve, 1500));
          return route.continue();
        }
        return route.fulfill({status: 502, contentType: 'application/json',   // the newer one: refused
          body: JSON.stringify({detail: 'integration configuration rejected'})});
      });

    await page.locator(RATE_LIMIT).fill(String(first));
    await page.locator(RATE_LIMIT).blur();
    // Queued behind the first while it is still in flight.
    await page.locator(RATE_LIMIT).fill(String(second));
    await page.locator(RATE_LIMIT).blur();

    await expect(toasts(page).first()).toContainText(/reject|error|fail/i, {timeout: 15000});
    await page.waitForTimeout(700);

    // The server holds the older, accepted value ...
    expect((await settings(page)).integrations.alldebrid.options.rate_limit_per_minute).toBe(first);
    // ... and so does the control.
    await expect(page.locator(RATE_LIMIT)).toHaveValue(String(first));

    // The baseline converged there too: an unchanged blur now writes nothing.
    const seen = mutations(page);
    await page.locator(RATE_LIMIT).focus();
    await page.locator(RATE_LIMIT).blur();
    await page.waitForTimeout(700);
    expect(seen).toEqual([]);
  });

// --- a failure must not erase a draft the operator is still typing --------
//
// A commit token only protects a draft that has already CROSSED its commit
// boundary. While a write is in flight the operator may keep typing, and that
// newer draft has not blurred yet, so no token exists for it. Its visible
// value is the evidence. A failed write accepted nothing, so it changes no
// canonical state -- and it must not repaint over newer intent either: the
// draft stays visible, stays dirty against the unchanged baseline, and
// commits on its own blur.

test('a failed write leaves a newer still-unblurred draft alone, and that draft persists on its own blur',
  async ({page}) => {
    await openAdditional(page);
    const canonical = Number(baseline.rate_limit_per_minute);   // A -- what the server holds
    const sent = canonical + 31;                                // B -- dispatched, and refused
    const typed = canonical + 32;                               // C -- typed while B was in flight

    let attempt = 0;
    await page.route(url => url.pathname === '/api/integrations/alldebrid/configuration',
      async route => {
        if (route.request().method() !== 'PATCH') return route.continue();
        attempt += 1;
        if (attempt === 1) {                       // B: slow enough to type over, then refused
          await new Promise(resolve => setTimeout(resolve, 1200));
          return route.fulfill({status: 502, contentType: 'application/json',
            body: JSON.stringify({detail: 'integration configuration rejected'})});
        }
        return route.continue();                   // C: an ordinary write
      });

    await page.locator(RATE_LIMIT).fill(String(sent));
    await page.locator(RATE_LIMIT).blur();
    // Still editing: this draft never reaches a commit boundary of its own
    // while B is in flight.
    await page.locator(RATE_LIMIT).fill(String(typed));

    await expect(toasts(page).first()).toContainText(/reject|error|fail/i, {timeout: 15000});
    await page.waitForTimeout(700);

    // A failure accepted nothing, so canonical state never moved ...
    expect((await settings(page)).integrations.alldebrid.options.rate_limit_per_minute).toBe(canonical);
    // ... the operator's newer draft is untouched ...
    await expect(page.locator(RATE_LIMIT)).toHaveValue(String(typed));
    // ... and it is still DIRTY against the unchanged accepted baseline.
    expect(await committedBaseline(page, RATE_LIMIT)).toBe(String(canonical));

    // Leaving the field now persists it exactly like any other changed blur.
    await page.locator(RATE_LIMIT).blur();
    await expect.poll(async () =>
      (await settings(page)).integrations.alldebrid.options.rate_limit_per_minute,
      {timeout: 15000}).toBe(typed);
    await expect(page.locator(RATE_LIMIT)).toHaveValue(String(typed));
    expect(await committedBaseline(page, RATE_LIMIT)).toBe(String(typed));

    // And the baseline converged, so an unchanged blur writes nothing.
    const seen = mutations(page);
    await page.locator(RATE_LIMIT).focus();
    await page.locator(RATE_LIMIT).blur();
    await page.waitForTimeout(700);
    expect(seen).toEqual([]);
  });
