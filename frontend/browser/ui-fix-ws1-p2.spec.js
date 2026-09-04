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

function fixture(base, {adEnabled = true, adConfigured = false, httpEnabled = true, extraProviders = {}} = {}) {
  const result = clone(base);
  result.integrations ||= {};
  result.integrations.alldebrid = {
    ...(result.integrations.alldebrid || {}),
    enabled: adEnabled,
    priority: result.integrations.alldebrid?.priority || 0,
    name: 'AllDebrid',
    kind: 'provider',
    configured: adConfigured,
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
    presentation: {
      status_name: 'General Downloads', premium: false,
      status_endpoint: null, static_status: 'healthy', display_order: 100,
    },
    options: {},
  };
  Object.assign(result.integrations, clone(extraProviders));
  result.alldebrid_api_key_configured = adConfigured;
  result.alldebrid_rate_limit_per_minute ??= 60;
  result.poll_interval_seconds ??= 30;
  result.full_sync_interval_minutes ??= 5;
  result.upload_fail_retry_count ??= 3;
  result.upload_fail_retry_delay_minutes ??= 5;
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
      const body = route.request().postDataJSON() || {};
      const submitted = body.integrations?.alldebrid || {};
      const enabled = submitted.enabled == null ? current.integrations.alldebrid.enabled : !!submitted.enabled;
      const clearSecrets = Array.isArray(submitted.clear_secrets) ? submitted.clear_secrets : [];
      const submittedKey = String(submitted.options?.api_key || body.alldebrid_api_key || '').trim();
      let configured = !!current.integrations.alldebrid.configured;
      if (clearSecrets.includes('api_key')) configured = false;
      if (submittedKey) configured = true;
      current = fixture({...current, ...body}, {
        adEnabled: enabled,
        adConfigured: configured,
        httpEnabled: submitted.enabled == null
          ? (body.integrations?.general_http?.enabled ?? current.integrations.general_http.enabled)
          : (body.integrations?.general_http?.enabled ?? current.integrations.general_http.enabled),
      });
      return route.fulfill({status:200, contentType:'application/json', body:JSON.stringify(current)});
    }
    return route.fallback();
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
  await expect(page.locator('.dp-settings-provider-card--alldebrid .dp-settings-provider-disclosure')).toHaveCount(1);
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

  const text = (await page.locator('#provider-status-list').innerText()).toLowerCase();
  expect(text).not.toContain('available');
  expect(text).not.toContain('disabled');
});

test('WS1-P2 premium card implements the exact persisted four-state matrix', async ({ page }) => {
  const base = await loadBaseSettings(page);
  const router = await installStatefulSettings(page, fixture(base, {adEnabled:false, adConfigured:false}));
  const cases = [
    {enabled:false, configured:false, expanded:false, status:''},
    {enabled:false, configured:true, expanded:false, status:'Provider configured', tone:'info'},
    {enabled:true, configured:false, expanded:true, status:'Configuration required', tone:'warning'},
    {enabled:true, configured:true, expanded:true, status:''},
  ];

  for (const item of cases) {
    router.set(fixture(base, {adEnabled:item.enabled, adConfigured:item.configured}));
    await page.reload();
    await openSources(page);
    const card = page.locator('.dp-settings-provider-card--alldebrid');
    const body = card.locator(':scope > .card-body');
    const disclosure = card.locator('.dp-settings-provider-disclosure');
    const status = card.locator('.dp-settings-provider-config-status');
    await expect(disclosure).toHaveAttribute('aria-expanded', item.expanded ? 'true' : 'false');
    if (item.expanded) await expect(body).toBeVisible(); else await expect(body).toBeHidden();
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
  await card.locator('.dp-settings-provider-disclosure').click();
  await expect(card.locator('.dp-settings-key-present')).toHaveText('Key present');
  await expect(page.locator('.dp-settings-provider-card--general-http .dp-settings-provider-disclosure')).toHaveCount(0);
});

test('WS1-P2 disclosure and staged Enable controls remain independent and protect unsaved edits', async ({ page }) => {
  const base = await loadBaseSettings(page);
  await installStatefulSettings(page, fixture(base, {adEnabled:false, adConfigured:false}));
  await page.reload();
  await openSources(page);

  const card = page.locator('.dp-settings-provider-card--alldebrid');
  const body = card.locator(':scope > .card-body');
  const disclosure = card.locator('.dp-settings-provider-disclosure');
  const enable = card.locator('input[data-integration-enabled="alldebrid"]');
  const key = card.locator('#dp-settings-field-alldebrid-api-key');
  const status = card.locator('.dp-settings-provider-config-status');

  await disclosure.click();
  await expect(body).toBeVisible();
  await key.fill('typed-but-unsaved-key');
  await expect(status).toHaveText('');
  await expect(card).toHaveAttribute('data-provider-configured', 'false');

  await enable.check();
  await expect(body).toBeVisible();
  await expect(status).toHaveText('Configuration required');

  await enable.uncheck();
  await expect(body).toBeVisible();
  await expect(status).toHaveText('');

  await disclosure.focus();
  await disclosure.press('Enter');
  await expect(disclosure).toHaveAttribute('aria-expanded', 'false');
  await expect(body).toBeHidden();
  await expect(key).not.toBeFocused();

  await disclosure.press('Enter');
  await expect(body).toBeVisible();
  await enable.check();
  await page.locator('#view-settings [data-action="save"]').click();
  await expect(card.locator('.dp-settings-key-present')).toHaveText('Key present');
  await expect(card.locator('.dp-settings-provider-config-status')).toBeHidden();

  const freshCard = page.locator('.dp-settings-provider-card--alldebrid');
  const freshEnable = freshCard.locator('input[data-integration-enabled="alldebrid"]');
  const freshBody = freshCard.locator(':scope > .card-body');
  await freshEnable.uncheck();
  await expect(freshBody).toBeHidden();
  await expect(freshCard.locator('.dp-settings-provider-config-status')).toHaveText('Provider configured');

  await page.locator('#view-settings [data-action="save"]').click();
  await expect(page.locator('.dp-settings-provider-card--alldebrid :scope > .card-body')).toBeHidden();
  await expect(page.locator('.dp-settings-provider-card--alldebrid .dp-settings-provider-config-status')).toHaveText('Provider configured');

  const persistedEnable = page.locator('.dp-settings-provider-card--alldebrid input[data-integration-enabled="alldebrid"]');
  await persistedEnable.check();
  await expect(page.locator('.dp-settings-provider-card--alldebrid :scope > .card-body')).toBeVisible();
  await expect(page.locator('.dp-settings-provider-card--alldebrid .dp-settings-key-present')).toHaveText('Key present');
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
      return {rectangles, headerCenter:h.left + h.width / 2, statusCenter:s.left + s.width / 2};
    });
    for (let i = 0; i < data.rectangles.length; i += 1) {
      for (let j = i + 1; j < data.rectangles.length; j += 1) {
        const a = data.rectangles[i], b = data.rectangles[j];
        const overlapX = Math.min(a.right,b.right) - Math.max(a.left,b.left);
        const overlapY = Math.min(a.bottom,b.bottom) - Math.max(a.top,b.top);
        expect(overlapX > 1 && overlapY > 1).toBe(false);
      }
    }
    if (width > 1180) expect(Math.abs(data.headerCenter - data.statusCenter)).toBeLessThan(2);
  };

  await assertGeometry(1440);
  const warningColor = await status.evaluate(node => getComputedStyle(node).color);
  const cautionColor = await page.evaluate(() => {
    const node = document.createElement('span');
    node.style.color = 'var(--dp-state-caution)';
    document.body.appendChild(node);
    const value = getComputedStyle(node).color;
    node.remove();
    return value;
  });
  expect(warningColor).toBe(cautionColor);

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
  await expect(card.locator('.dp-settings-provider-config-status')).toHaveText('Provider configured');

  router.set(fixture(base, {adEnabled:true, adConfigured:false, httpEnabled:false}));
  await page.reload();
  await expect.poll(() => page.evaluate(() => !!window.DPProviderStatus)).toBeTruthy();
  await page.evaluate(() => window.DPProviderStatus.refresh());
  await expect(page.locator('#provider-status-list [data-provider-id="alldebrid"]')).toHaveAttribute('data-provider-state', 'unconfigured');
  await openSources(page);
  card = page.locator('.dp-settings-provider-card--alldebrid');
  await expect(card.locator(':scope > .card-body')).toBeVisible();
  await expect(card.locator('.dp-settings-provider-config-status')).toHaveText('Configuration required');
});
