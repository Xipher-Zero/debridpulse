const { test, expect } = require('@playwright/test');

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({
    status: 200, contentType: 'text/css', body: '',
  }));
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

async function loadBaseSettings(page) {
  await isolateExternalFonts(page);
  await page.goto('/');
  await expect.poll(() => page.evaluate(() => !!window.DPProviderStatus)).toBeTruthy();
  await expect.poll(() => page.evaluate(() => !!settingsData?.integrations?.alldebrid?.presentation)).toBeTruthy();
  return page.evaluate(() => JSON.parse(JSON.stringify(settingsData)));
}

function fixture(base, {adEnabled = true, adConfigured = false, adVerified = false,
                        httpEnabled = true, extraProviders = {}} = {}) {
  const result = clone(base);
  result.integrations ||= {};
  result.integrations.alldebrid = {
    ...(result.integrations.alldebrid || {}),
    enabled: adEnabled,
    priority: result.integrations.alldebrid?.priority || 0,
    name: 'AllDebrid',
    kind: 'provider',
    configured: adConfigured,
    // Durable canonical truth published by the backend: the CURRENT SAVED
    // verification-relevant configuration is covered by successful test
    // evidence. The page renders it; it never decides it.
    verified: adConfigured && adVerified,
    presentation: {
      status_name: 'AllDebrid', premium: true,
      status_endpoint: '/integration-status/alldebrid', static_status: null, display_order: 10,
    },
    options: {
      ...(result.integrations.alldebrid?.options || {}),
      api_key: '', api_key_configured: adConfigured,
    },
  };
  result.integrations.general_http = {
    ...(result.integrations.general_http || {}),
    enabled: httpEnabled,
    priority: result.integrations.general_http?.priority || 0,
    name: 'HTTP & HTTPS',
    kind: 'provider',
    configured: true,
    verified: false,
    presentation: {
      status_name: 'General Downloads', premium: false,
      status_endpoint: null, static_status: 'healthy', display_order: 100,
    },
    options: {},
  };
  // The fixture owns its whole provider universe (AllDebrid, General Downloads
  // and explicit extras); later live providers such as FTP & SFTP and Usenet
  // are not inherited.
  delete result.integrations.general_ftp;
  delete result.integrations.usenet;
  Object.assign(result.integrations, clone(extraProviders));
  result.full_sync_interval_minutes ??= 5;
  return result;
}

function statusFixture(settings) {
  const ad = settings.integrations.alldebrid;
  if (!ad.enabled) return {state:'disabled', checked:false};
  if (!ad.configured) return {state:'unconfigured', checked:false};
  return {state:'healthy', checked:true, username:'fixture', isPremium:true, premiumUntil:1893456000};
}

async function installStatefulSettings(page, initial) {
  let current = clone(initial);

  await page.route('**/api/settings', async route => {
    const method = route.request().method();
    if (method === 'GET') {
      return route.fulfill({status:200, contentType:'application/json', body:JSON.stringify(current)});
    }
    if (method === 'PUT' || method === 'POST') {
      // The broad document never writes a provider namespace; it answers with
      // the current canonical state.
      const body = route.request().postDataJSON() || {};
      const {integrations: _ignored, transfer_policy: _policy, execution_runtime_limits: _limits, ...broad} = body;
      current = fixture({...current, ...broad}, {
        adEnabled: current.integrations.alldebrid.enabled,
        adConfigured: !!current.integrations.alldebrid.configured,
        httpEnabled: current.integrations.general_http.enabled,
      });
      return route.fulfill({status:200, contentType:'application/json', body:JSON.stringify(current)});
    }
    return route.fallback();
  });

  // Provider enablement and credentials are written only through each
  // provider's scoped configuration surface.
  await page.route(/\/api\/integrations\/(alldebrid|general_http)\/configuration$/, async route => {
    if (route.request().method() !== 'PATCH') return route.fallback();
    const identity = new URL(route.request().url()).pathname.split('/integrations/')[1].split('/')[0];
    const body = route.request().postDataJSON() || {};
    let adEnabled = current.integrations.alldebrid.enabled;
    let adConfigured = !!current.integrations.alldebrid.configured;
    let httpEnabled = current.integrations.general_http.enabled;
    if (identity === 'alldebrid') {
      if (body.enabled != null) adEnabled = !!body.enabled;
      if ((body.clear_secrets || []).includes('api_key')) adConfigured = false;
      if (String(body.options?.api_key || '').trim()) adConfigured = true;
    } else if (body.enabled != null) {
      httpEnabled = !!body.enabled;
    }
    current = fixture(current, {adEnabled, adConfigured, httpEnabled});
    return route.fulfill({status:200, contentType:'application/json',
      body:JSON.stringify({ok:true, ...current.integrations[identity]})});
  });

  await page.route('**/api/integration-status/alldebrid', route => route.fulfill({
    status:200, contentType:'application/json', body:JSON.stringify(statusFixture(current)),
  }));

  return {
    get: () => clone(current),
    set: value => { current = clone(value); },
  };
}

async function openSources(page) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await expect(page.locator('.dp-settings-provider-card--alldebrid')).toBeVisible();
  await expect(page.locator('.dp-settings-provider-card--alldebrid .dp-settings-disclosure')).toHaveCount(1);
}

async function applySettings(page) {
  const apply = page.locator('#view-settings [data-action="save"]');
  await apply.click();
  // The click handler marks the Apply control busy synchronously; the settings owner replaces it
  // with a fresh, enabled control only after the persisted state is adopted and re-rendered. Wait
  // for that boundary so no later staged edit races the save-completion render.
  await expect(apply).toBeEnabled();
}

/* DP 1.0.13 Services cleanup: an AllDebrid credential is an ORDINARY value.
 * Entering or replacing one commits on changed blur through the integration's
 * own scoped mutation -- there is no localized Save, and the footer still
 * writes no AllDebrid namespace at all. Anything that removes focus from the
 * field is therefore its commit boundary, including clicking Enable. */
async function commitAllDebridKey(page, value) {
  const key = page.locator('.dp-settings-provider-card--alldebrid #dp-settings-field-alldebrid-api-key');
  await key.fill(value);
  await key.blur();
  // The accepted presentation of a secret is blank, so convergence is visible
  // as the field returning to its configured presentation.
  await expect(key).toHaveValue('');
}

async function setProviderEnabled(card, enabled) {
  const input = card.locator('input[data-integration-enabled="alldebrid"]');
  if ((await input.isChecked()) !== enabled) {
    await card.locator('.dp-settings-integration-header-enable').click();
  }
  if (enabled) await expect(input).toBeChecked();
  else await expect(input).not.toBeChecked();
}

async function statusNames(page) {
  return page.locator('#provider-status-list .dp-provider-status-row > span').allTextContents();
}

async function useStatusSettings(page, settings, {staticAllDebrid = true} = {}) {
  const value = clone(settings);
  if (staticAllDebrid && value.integrations.alldebrid?.enabled) {
    value.integrations.alldebrid.presentation.status_endpoint = null;
    value.integrations.alldebrid.presentation.static_status = value.integrations.alldebrid.configured ? 'healthy' : 'unconfigured';
  }
  await page.evaluate(next => { settingsData = next; }, value);
  await page.evaluate(() => window.DPProviderStatus.refresh());
}

test('WS1-P2 Provider Status is neutral across enabled-provider combinations', async ({ page }) => {
  const base = await loadBaseSettings(page);

  await useStatusSettings(page, fixture(base, {adEnabled:true, adConfigured:true, httpEnabled:false}));
  expect(await statusNames(page)).toEqual(['AllDebrid']);

  await useStatusSettings(page, fixture(base, {adEnabled:false, adConfigured:true, httpEnabled:true}));
  expect(await statusNames(page)).toEqual(['General Downloads']);

  await useStatusSettings(page, fixture(base, {adEnabled:true, adConfigured:true, httpEnabled:true}));
  expect(await statusNames(page)).toEqual(['AllDebrid', 'General Downloads']);

  const mockPremium = {
    mock_premium: {
      enabled:true, priority:0, name:'Future Premium', kind:'provider', configured:true, options:{},
      presentation:{status_name:'Future Premium', premium:true, status_endpoint:null, static_status:'healthy', display_order:20},
    },
  };
  await useStatusSettings(page, fixture(base, {adEnabled:true, adConfigured:true, httpEnabled:true, extraProviders:mockPremium}));
  expect(await statusNames(page)).toEqual(['AllDebrid', 'Future Premium', 'General Downloads']);

  await useStatusSettings(page, fixture(base, {adEnabled:false, adConfigured:true, httpEnabled:false}));
  expect(await statusNames(page)).toEqual(['No download providers enabled']);
  await expect(page.locator('#provider-status-list .dp-provider-status-row')).toHaveAttribute('data-provider-state', 'inactive');

  const statusText = (await page.locator('#provider-status-list').innerText()).toLowerCase();
  expect(statusText).not.toContain('available');
  expect(statusText).not.toContain('disabled');
});

/* The Enable toggle beside the status already says whether the provider
 * participates, so the status never repeats it. What it reports is the only
 * thing the toggle cannot: whether there is a usable SAVED configuration, and
 * whether that exact saved configuration has been proven to work.
 *
 * Expansion is local presentation state. Navigating to Sources & Providers
 * renders every card collapsed, whatever the provider's canonical state. */
test('WS1-P2 premium card implements the exact persisted three-state status matrix', async ({ page }) => {
  const base = await loadBaseSettings(page);
  const router = await installStatefulSettings(page, fixture(base, {adEnabled:false, adConfigured:false}));
  const cases = [
    {enabled:false, configured:false, verified:false, status:''},
    {enabled:false, configured:true, verified:false, status:'Configured', tone:'warning'},
    {enabled:false, configured:true, verified:true, status:'Verified', tone:'success'},
    {enabled:true, configured:false, verified:false, status:'Unconfigured', tone:'error'},
    {enabled:true, configured:true, verified:false, status:'Configured', tone:'warning'},
    {enabled:true, configured:true, verified:true, status:'Verified', tone:'success'},
  ];

  for (const item of cases) {
    router.set(fixture(base, {adEnabled:item.enabled, adConfigured:item.configured,
                              adVerified:item.verified}));
    await page.reload();
    await openSources(page);
    const card = page.locator('.dp-settings-provider-card--alldebrid');
    const body = card.locator(':scope > .card-body');
    const disclosure = card.locator('.dp-settings-disclosure');
    const status = card.locator('.dp-settings-provider-config-status');
    // Never expanded on navigation, whatever the provider's canonical state.
    await expect(disclosure).toHaveAttribute('aria-expanded', 'false');
    await expect(body).toBeHidden();
    if (item.status) {
      await expect(status).toBeVisible();
      await expect(status).toHaveText(item.status);
      await expect(status).toHaveAttribute('data-tone', item.tone);
    } else {
      await expect(status).toBeHidden();
      await expect(status).toHaveText('');
    }
  }

  router.set(fixture(base, {adEnabled:false, adConfigured:true}));
  await page.reload();
  await openSources(page);
  const card = page.locator('.dp-settings-provider-card--alldebrid');
  await card.locator('.dp-settings-disclosure').click();
  await expect(card.locator('.dp-settings-key-present')).toHaveText('Key present');
  await expect(page.locator('.dp-settings-provider-card--general-http .dp-settings-disclosure')).toHaveCount(0);
});

test('WS1-P2 disclosure and Enable stay independent, and the credential commits on its own boundary',
  async ({ page }) => {
    const base = await loadBaseSettings(page);
    await installStatefulSettings(page, fixture(base, {adEnabled:false, adConfigured:false}));
    await page.reload();
    await openSources(page);

    let card = page.locator('.dp-settings-provider-card--alldebrid');
    let body = card.locator(':scope > .card-body');
    let disclosure = card.locator('.dp-settings-disclosure');
    let key = card.locator('#dp-settings-field-alldebrid-api-key');
    let status = card.locator('.dp-settings-provider-config-status');

    // Disclosure is LOCAL presentation: opening the card writes nothing and a
    // switched-off, unconfigured provider reports nothing.
    await disclosure.click();
    await expect(body).toBeVisible();
    await expect(status).toHaveText('');
    await expect(card).toHaveAttribute('data-provider-configured', 'false');

    // Admitting a provider that has nothing configured puts its configuration
    // in front of the operator -- the ONE automatic expansion there is.
    await setProviderEnabled(card, true);
    await expect(body).toBeVisible();
    await expect(status).toHaveText('Unconfigured');

    // Withdrawing it puts that configuration away again. Under the DP 1.0.13
    // credential contract there is no unsaved credential left to protect: the
    // key commits on its own boundary, so nothing suppresses this.
    await setProviderEnabled(card, false);
    await expect(body).toBeHidden();
    await expect(status).toHaveText('');

    // Expansion is LOCAL state: the operator can open a withdrawn provider,
    // and doing so writes nothing and changes no enable state.
    await disclosure.click();
    await expect(body).toBeVisible();
    await expect(card.locator('input[data-integration-enabled="alldebrid"]')).not.toBeChecked();

    // The keyboard contract on the disclosure is unchanged.
    await disclosure.focus();
    await disclosure.press('Enter');
    await expect(disclosure).toHaveAttribute('aria-expanded', 'false');
    await expect(body).toBeHidden();
    await expect(key).not.toBeFocused();
    await disclosure.press('Enter');
    await expect(body).toBeVisible();

    // The credential commits on its OWN boundary -- leaving the field -- and
    // is never carried by the footer.
    await commitAllDebridKey(page, 'typed-then-committed-key');
    await expect(status).toHaveText('Configured');

    /* A STATED CONSEQUENCE of the DP 1.0.13 credential contract.
     *
     * The card is open and the provider is still switched OFF. The accepted
     * credential re-renders this provider's state, and the pre-existing rule
     * there is "a withdrawn provider's configuration is put away again, unless
     * the operator has edits in it". Before this batch the key was a gated
     * draft, so it kept the card dirty and open; now it commits on its own
     * boundary, so by the time that render runs there is nothing pending and
     * the card closes.
     *
     * Both rules are behaving exactly as written -- this is where they now
     * meet. It is recorded here rather than left to be discovered. */
    await expect(body).toBeHidden();
    await disclosure.click();
    await expect(body).toBeVisible();
    await expect(card.locator('.dp-settings-key-present')).toHaveText('Key present');

    // Enabling a provider that IS configured has nothing to ask for, so it
    // opens nothing -- and it does not close what the operator already opened.
    await setProviderEnabled(card, true);
    await expect(status).toHaveText('Configured');
    await expect(body).toBeVisible();

    // Withdrawing a configured provider puts its configuration away again.
    await setProviderEnabled(card, false);
    await expect(body).toBeHidden();
    await expect(status).toHaveText('Configured');

    await applySettings(page);
    card = page.locator('.dp-settings-provider-card--alldebrid');
    body = card.locator(':scope > .card-body');
    status = card.locator('.dp-settings-provider-config-status');
    await expect(body).toBeHidden();
    await expect(status).toHaveText('Configured');

    // Enabling a provider that IS configured has nothing to ask the operator
    // for, so it opens nothing: the only automatic expansion belongs to an
    // accepted enable of an UNCONFIGURED provider.
    await setProviderEnabled(card, true);
    await expect(body).toBeHidden();
    await card.locator('.dp-settings-disclosure').click();
    await expect(card.locator('.dp-settings-key-present')).toHaveText('Key present');
  });

test('WS1-P2 provider header stays centered/non-overlapping and semantic in dark/light/narrow layouts', async ({ page }) => {
  const base = await loadBaseSettings(page);
  await installStatefulSettings(page, fixture(base, {adEnabled:true, adConfigured:false}));
  await page.reload();
  await openSources(page);

  const card = page.locator('.dp-settings-provider-card--alldebrid');
  const status = card.locator('.dp-settings-provider-config-status');
  const header = card.locator(':scope > .card-header');

  const assertGeometry = async width => {
    await page.setViewportSize({width, height:900});
    await expect(status).toBeVisible();
    const data = await header.evaluate(node => {
      const rectangles = Array.from(node.children).map(child => {
        const r = child.getBoundingClientRect();
        return {left:r.left, right:r.right, top:r.top, bottom:r.bottom, cls:child.className};
      });
      const h = node.getBoundingClientRect();
      const s = node.querySelector('.dp-settings-provider-config-status').getBoundingClientRect();
      const copy = node.querySelector('.dp-settings-card-header-center').getBoundingClientRect();
      const enable = node.querySelector('.dp-settings-integration-header-enable').getBoundingClientRect();
      return {rectangles, headerCenter:h.left + h.width / 2,
              copyCenter:copy.left + copy.width / 2,
              statusLeft:s.left, enableLeft:enable.left, headerRight:h.right};
    });
    for (let i = 0; i < data.rectangles.length; i += 1) {
      for (let j = i + 1; j < data.rectangles.length; j += 1) {
        const a = data.rectangles[i], b = data.rectangles[j];
        const overlapX = Math.min(a.right,b.right) - Math.max(a.left,b.left);
        const overlapY = Math.min(a.bottom,b.bottom) - Math.max(a.top,b.top);
        expect(overlapX > 1 && overlapY > 1).toBe(false);
      }
    }
    // DP 1.0.13 work items D and E: the centre region now carries the card's
    // capability copy, centred against the FULL header, and the configuration
    // status joined the right-side controls beside Enable -- so the operator
    // reads what enabling the card does where the header was previously blank.
    if (width > 1180) {
      expect(Math.abs(data.headerCenter - data.copyCenter)).toBeLessThan(2);
      expect(data.statusLeft).toBeGreaterThan(data.headerCenter);
      expect(data.statusLeft).toBeLessThan(data.enableLeft);
    }
  };

  await assertGeometry(1440);
  // The status carries the canonical state colour for what it reports, never a
  // palette of its own. This fixture is enabled + unconfigured, which is the
  // error state: the provider was admitted and cannot do the job.
  const statusColor = await status.evaluate(node => getComputedStyle(node).color);
  const errorColor = await page.evaluate(() => {
    const node = document.createElement('span');
    node.style.color = 'var(--dp-state-error)';
    document.body.appendChild(node);
    const value = getComputedStyle(node).color;
    node.remove();
    return value;
  });
  await expect(status).toHaveText('Unconfigured');
  expect(statusColor).toBe(errorColor);

  await page.locator('#theme-toggle').click();
  await expect.poll(() => page.evaluate(() => document.body.classList.contains('light'))).toBeTruthy();
  await assertGeometry(1440);
  await assertGeometry(1050);
  await page.screenshot({path:'test-results/checkpoint-ui-fix-ws1-p2-provider-state-light.png', fullPage:true});
});

test('WS1-P2 cross-surface regression keeps persisted card state distinct from runtime visibility', async ({ page }) => {
  const base = await loadBaseSettings(page);
  const router = await installStatefulSettings(page, fixture(base, {adEnabled:false, adConfigured:true, httpEnabled:false}));
  await page.reload();
  await expect.poll(() => page.evaluate(() => !!window.DPProviderStatus)).toBeTruthy();
  await page.evaluate(() => window.DPProviderStatus.refresh());
  await expect(page.locator('#provider-status-list [data-provider-id="alldebrid"]')).toHaveCount(0);
  await openSources(page);
  let card = page.locator('.dp-settings-provider-card--alldebrid');
  await expect(card.locator(':scope > .card-body')).toBeHidden();
  await expect(card.locator('.dp-settings-provider-config-status')).toHaveText('Configured');

  router.set(fixture(base, {adEnabled:true, adConfigured:false, httpEnabled:false}));
  await page.reload();
  await expect.poll(() => page.evaluate(() => !!window.DPProviderStatus)).toBeTruthy();
  await page.evaluate(() => window.DPProviderStatus.refresh());
  await expect(page.locator('#provider-status-list [data-provider-id="alldebrid"]')).toHaveAttribute('data-provider-state', 'unconfigured');
  await openSources(page);
  card = page.locator('.dp-settings-provider-card--alldebrid');
  await expect(card.locator(':scope > .card-body')).toBeHidden();
  await expect(card.locator('.dp-settings-provider-config-status')).toHaveText('Unconfigured');
});
