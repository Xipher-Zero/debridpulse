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
  await revealUsenet(page);
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
  // DP 1.0.13: the Downloads tuning and safety controls became cells of the
  // compact tuning-cell collection, whose label/control/help are each CENTRED
  // on the cell's own axis -- a different, deliberate grammar. They are
  // therefore not cases for the left-hand form datum; their own geometry is
  // proven by settings-providers-layout.spec.js. The Download Folder field,
  // which is still an ordinary full-width field, keeps the datum.
  const cases = [
    ['sources', 'dp-settings-field-alldebrid-api-key'],
    ['downloads', 'dp-settings-field-download-folder'],
    // DP 1.0.13: every Notifications control joined the inline grammar, so
    // that tab carries no stacked field for this datum to sample -- its own
    // geometry is proven by settings-notifications-persistence.spec.js. The
    // stacked grammar now survives on two surfaces, so both contribute more
    // than one field and the datum is still proven across them.
    ['maintenance', 'dp-settings-field-backup-interval-hours'],
    ['maintenance', 'dp-settings-field-events-keep-days'],
    // DP 1.0.13: Username joined the inline grammar with the rest of the
    // Authentication card. The allowlists keep the stacked full-width field,
    // so they are what carries the datum on this tab.
    ['authentication', 'dp-settings-field-oidc-allowed-subjects'],
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

/* DP 1.0.13 final interaction pass: the clear-stored-password CHECKBOX is gone.
 * Whether the operator means a destructive action is now the one canonical
 * Settings confirmation's question, asked when they act -- so there is no
 * card-local checkbox to align against its own label text, and no datum here to
 * restate. What the group must still be is ONE explicit destructive control,
 * which is the shared-datum invariant below. */
test('the clear-stored-password group is one explicit destructive control', async ({page}) => {
  await isolateExternalFonts(page);
  const card = await usenetServerCard(page);
  const row = card.locator('.dp-usenet-clear-password');
  await expect(row).toHaveCount(1);

  const action = row.locator('[data-usenet-action="clear-password"]');
  await expect(action).toHaveCount(1);
  await expect(action).toHaveClass(/btn-danger/);
  await expect(action).toHaveText('Clear Password');
  await expect(action).toBeEnabled();
  // No confirmation representation survives on the card, in either theme.
  await expect(row.locator('input[type="checkbox"]')).toHaveCount(0);
  await expect(row.locator('label')).toHaveCount(0);

  for (const light of [false, true]) {
    await page.evaluate(isLight => document.body.classList.toggle('light', isLight), light);
    // The control is vertically centred on the group it is the whole of.
    const delta = await page.evaluate(() => {
      const target = document.querySelector('.dp-usenet-clear-password');
      const button = target.querySelector('[data-usenet-action="clear-password"]')
        .getBoundingClientRect();
      const host = target.getBoundingClientRect();
      return ((button.top + button.bottom) / 2) - ((host.top + host.bottom) / 2);
    });
    expect(Math.abs(delta), `theme light=${light} delta ${delta}`).toBeLessThanOrEqual(1);
  }
  await page.evaluate(() => document.body.classList.remove('light'));
});

/* The Usenet card renders COLLAPSED: expansion is LOCAL presentation state,
 * never a projection of enabled/configured/verified state. The server
 * collection lives inside its body, so opening it through the canonical
 * disclosure is how this spec reaches the collection -- exactly as an
 * operator does, and writing no canonical state. */
async function revealUsenet(page) {
  const card = page.locator('.dp-settings-provider-card--usenet');
  const disclosure = card.locator('.dp-settings-disclosure');
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
  await expect(card.locator(':scope > .card-body')).toBeVisible();
}
