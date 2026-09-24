const { test, expect } = require('@playwright/test');

/* DP 1.0.13 work items J and K -- RENDERED geometry, not selector equality.
 *
 *  J: a field label's left edge equals its control's OUTER BOX left edge.
 *  K: a single-line checkbox shares its label text's visual centreline.
 *
 * The Usenet cards are rendered from an injected canonical settings document
 * rather than by persisting servers: geometry is what is under test, and this
 * way the spec leaves no shared backend state behind for another spec. */

const TOLERANCE = 0.5;

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

async function openSettings(page, tab) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator(`#view-settings [data-tab="${tab}"]`).click();
  await expect(page.locator(`.dp-settings-panel[data-panel="${tab}"]`)).toBeVisible();
}

const USENET_FIXTURE = {
  enabled: true, priority: 0, name: 'Usenet', kind: 'provider_executor', configured: true,
  presentation: {status_name: 'Usenet', premium: true, status_endpoint: null,
    static_status: 'healthy', display_order: 20, status_group: null, status_group_label: null,
    status_tier: 'general_family', status_tier_label: 'General'},
  options: {
    operation_timeout_seconds: 30, article_cache_megabytes: 1024,
    direct_write: true, max_acquisition_retries: 3,
    servers: [{
      id: 'geometry-fixture', host: 'news.example.com', port: 563, ssl: true,
      username: 'u', password: '', password_configured: true,
      connections: 8, priority: 0, articles_per_request: 2, timeout_seconds: 60,
      enabled: true, display_name: '',
    }],
  },
};

/** Render the Sources panel with exactly one configured Usenet server.
 *
 * The settings document is served to the page rather than persisted, so the
 * Settings owner's own reload keeps returning the same fixture and nothing is
 * left behind in the shared backend. */
async function usenetServerCard(page) {
  const live = await page.request.get('/api/settings').then(response => response.json());
  const document_ = {...live, integrations: {...live.integrations, usenet: USENET_FIXTURE}};
  await page.route(url => url.pathname === '/api/settings', route =>
    (route.request().method() === 'GET'
      ? route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(document_)})
      : route.fallback()));
  await page.goto('/');
  await openSettings(page, 'sources');
  const card = page.locator('[data-usenet-collection] [data-usenet-server-id]');
  await expect(card).toHaveCount(1);
  await card.locator('[data-usenet-advanced-toggle]').click();
  await expect(card.locator('[data-usenet-field="connections"]')).toBeVisible();
  return card;
}

test('every Usenet server field label aligns with its control outer box', async ({page}) => {
  await isolateExternalFonts(page);
  await usenetServerCard(page);

  const deltas = await page.evaluate(() => {
    const out = {};
    const card = document.querySelector('[data-usenet-collection] [data-usenet-server-id]');
    for (const field of card.querySelectorAll('.dp-usenet-field')) {
      const label = field.querySelector('.form-label');
      const control = field.querySelector('.input');
      if (!label || !control || control.getBoundingClientRect().width === 0) continue;
      out[control.dataset.usenetField] =
        label.getBoundingClientRect().left - control.getBoundingClientRect().left;
    }
    return out;
  });
  for (const name of ['host', 'port', 'username', 'password', 'connections', 'priority',
                      'articles_per_request', 'timeout_seconds']) {
    expect(deltas, `missing ${name}`).toHaveProperty(name);
    expect(Math.abs(deltas[name]), `${name} delta ${deltas[name]}`).toBeLessThanOrEqual(TOLERANCE);
  }
});

test('representative pre-existing Settings fields use the same canonical datum', async ({page}) => {
  await isolateExternalFonts(page);
  await page.goto('/');
  const cases = [
    ['sources', 'dp-settings-field-alldebrid-api-key'],
    ['downloads', 'dp-settings-field-min-free-disk-gb'],
    ['notifications', 'dp-settings-field-discord-username'],
    ['maintenance', 'dp-settings-field-backup-interval-hours'],
    ['authentication', 'dp-settings-field-auth-username'],
  ];
  let checked = 0;
  for (const [tab, id] of cases) {
    await openSettings(page, tab);
    const delta = await page.evaluate(fieldId => {
      const control = document.getElementById(fieldId);
      if (!control) return null;
      const field = control.closest('.dp-settings-field');
      const label = field && field.querySelector(':scope > .form-label');
      if (!label || control.getBoundingClientRect().width === 0) return null;
      return label.getBoundingClientRect().left - control.getBoundingClientRect().left;
    }, id);
    if (delta === null) continue;   // not rendered in this configuration
    checked += 1;
    expect(Math.abs(delta), `${tab}/${id} delta ${delta}`).toBeLessThanOrEqual(TOLERANCE);
  }
  // The point of the case list is that the datum is SHARED, so it has to prove
  // itself on more than one pre-existing surface.
  expect(checked).toBeGreaterThanOrEqual(3);
});

test('the clear-stored-password checkbox is optically centred on its label text', async ({page}) => {
  await isolateExternalFonts(page);
  const card = await usenetServerCard(page);
  const row = card.locator('.dp-usenet-clear-password');
  await expect(row).toHaveCount(1);

  for (const light of [false, true]) {
    await page.evaluate(isLight => document.body.classList.toggle('light', isLight), light);
    const geometry = await page.evaluate(() => {
      const target = document.querySelector('.dp-usenet-clear-password');
      const box = target.querySelector('input[type="checkbox"]');
      const text = target.querySelector('span');
      // The TEXT's optical centreline, derived from the rendered font's own
      // metrics: the baseline (a zero-size baseline-aligned strut) minus half
      // the x-height (a 1ex-tall strut). A line box's geometric centre is NOT
      // that line -- which is precisely the defect being measured.
      const baseline = document.createElement('span');
      baseline.style.cssText = 'display:inline-block;width:0;height:0;vertical-align:baseline';
      const exHeight = document.createElement('span');
      exHeight.style.cssText = 'display:inline-block;width:0;height:1ex;vertical-align:baseline';
      text.appendChild(baseline);
      text.appendChild(exHeight);
      const baselineY = baseline.getBoundingClientRect().top;
      const xHeight = exHeight.getBoundingClientRect().height;
      baseline.remove();
      exHeight.remove();
      const b = box.getBoundingClientRect();
      return {delta: ((b.top + b.bottom) / 2) - (baselineY - xHeight / 2), xHeight};
    });
    expect(geometry.xHeight).toBeGreaterThan(0);
    expect(Math.abs(geometry.delta), `theme light=${light} delta ${geometry.delta}`).toBeLessThanOrEqual(0.75);
  }
  await page.evaluate(() => document.body.classList.remove('light'));

  // Interaction truth is unchanged.
  const box = row.locator('input[type="checkbox"]');
  await expect(box).not.toBeChecked();
  await row.locator('span').click();
  await expect(box).toBeChecked();
  await box.focus();
  await expect(box).toBeFocused();
  await page.keyboard.press('Space');
  await expect(box).not.toBeChecked();
});
