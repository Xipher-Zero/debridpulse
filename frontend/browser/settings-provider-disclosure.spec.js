const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Services final corrective pass -- Defect 1.
 *
 * Expansion is LOCAL PRESENTATION STATE.
 *
 * Enabled state is not expansion state. Configured state is not expansion
 * state. Verified state is not expansion state. Arriving at Services
 * therefore shows every expandable card closed, whatever the providers'
 * canonical state happens to be -- which is why every assertion below holds no
 * matter what another spec is doing to the shared installation at the time.
 *
 * The ONE automatic expansion is an operator ACTION: admitting a provider that
 * has nothing configured opens it so the configuration can be supplied. That
 * case is proven against the real backend in usenet-server-cards.spec.js,
 * where a provider can actually be driven into "enabled and unconfigured".
 */

/* Provider cards: closed on arrival, and a navigation-local choice about one
 * is never restored. */
const EXPANDABLE = [
  ['.dp-settings-provider-card--alldebrid', 'AllDebrid'],
  ['.dp-settings-provider-card--usenet', 'Usenet'],
];

/* DP 1.0.13: the Network Sources group declares a starting state of its own --
 * it is what an operator arriving at Services most often needs to see -- and
 * the operator's own later choice about it survives a canonical refresh.
 * Enable/disable remains a different question entirely. */
const GROUP = ['.dp-settings-general-sources', 'Network Sources'];

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

const canonical = page => page.request.get('/api/settings').then(r => r.json());

/** The real disclosure and the real body it controls, never a source string. */
async function disclosureState(page, selector) {
  return page.evaluate(root => {
    const card = document.querySelector(root);
    if (!card) return {missing: true};
    const button = card.querySelector('.dp-settings-disclosure');
    if (!button) return {missing: true};
    const body = document.getElementById(button.getAttribute('aria-controls'));
    return {
      expanded: button.getAttribute('aria-expanded') === 'true',
      hidden: !!body?.hidden,
      visible: !!body && body.getClientRects().length > 0,
    };
  }, selector);
}

let original = null;

test.beforeAll(async ({request}) => {
  const settings = await request.get('/api/settings').then(r => r.json());
  original = {
    alldebrid: settings.integrations.alldebrid?.enabled !== false,
    usenet: settings.integrations.usenet?.enabled === true,
  };
});

test.afterAll(async ({request}) => {
  if (!original) return;
  for (const [id, enabled] of Object.entries(original)) {
    await request.patch(`/api/integrations/${id}/configuration`, {data: {enabled}});
  }
});

test.beforeEach(async ({page}) => {
  await isolateExternalFonts(page);
});

test('every expandable Services card is collapsed on navigation', async ({page}) => {
  // Drive the providers ENABLED first: the whole point is that an enabled
  // provider is not thereby an expanded one.
  for (const id of ['alldebrid', 'usenet']) {
    await page.request.patch(`/api/integrations/${id}/configuration`, {data: {enabled: true}});
  }
  const settings = await canonical(page);
  expect(settings.integrations.alldebrid.enabled).toBe(true);
  expect(settings.integrations.usenet.enabled).toBe(true);

  await page.goto('/');
  await openSources(page);

  for (const [selector, label] of EXPANDABLE) {
    const state = await disclosureState(page, selector);
    expect(state.missing, `${label} has no canonical disclosure`).toBeFalsy();
    expect(state.expanded, `${label} opened itself on navigation`).toBe(false);
    expect(state.hidden, `${label} body is not hidden on navigation`).toBe(true);
    expect(state.visible, `${label} body is still rendered on navigation`).toBe(false);
  }
});

test('Network Sources starts expanded and respects a later manual collapse', async ({page}) => {
  const [selector, label] = GROUP;
  await page.goto('/');
  await openSources(page);

  const initial = await disclosureState(page, selector);
  expect(initial.missing, `${label} has no canonical disclosure`).toBeFalsy();
  expect(initial.expanded, `${label} did not start expanded`).toBe(true);
  expect(initial.hidden).toBe(false);

  // The operator closes it. A canonical Settings refresh re-renders the whole
  // page from canonical state, and must not reopen what they just closed.
  await page.locator(`${selector} .dp-settings-disclosure`).click();
  expect((await disclosureState(page, selector)).expanded, `${label} did not close`).toBe(false);

  await page.evaluate(() => window.DPSettingsPage.load());
  await expect(page.locator('.dp-settings-panel[data-panel="sources"]')).toBeVisible();
  expect((await disclosureState(page, selector)).expanded,
    `${label} reopened itself on a canonical refresh`).toBe(false);

  // And reopening it is remembered in the same way.
  await page.locator(`${selector} .dp-settings-disclosure`).click();
  await page.evaluate(() => window.DPSettingsPage.load());
  await expect(page.locator('.dp-settings-panel[data-panel="sources"]')).toBeVisible();
  expect((await disclosureState(page, selector)).expanded,
    `${label} did not keep the operator's reopening`).toBe(true);
});

test('the disclosure still opens and closes each card, and never persists', async ({page}) => {
  await page.goto('/');
  await openSources(page);

  for (const [selector, label] of EXPANDABLE) {
    const button = page.locator(`${selector} .dp-settings-disclosure`);
    await button.click();
    expect((await disclosureState(page, selector)).expanded, `${label} did not open`).toBe(true);
    expect((await disclosureState(page, selector)).hidden).toBe(false);
    await button.click();
    expect((await disclosureState(page, selector)).expanded, `${label} did not close`).toBe(false);
  }

  // Open everything, then leave and come back: a navigation-local choice is
  // not durable state and must not be restored.
  for (const [selector] of EXPANDABLE) {
    await page.locator(`${selector} .dp-settings-disclosure`).click();
  }
  await page.locator('#sidebar .nav-item[data-view="dashboard"]').click();
  await openSources(page);
  for (const [selector, label] of EXPANDABLE) {
    expect((await disclosureState(page, selector)).expanded,
      `${label} remembered an expansion across navigation`).toBe(false);
  }
});

test('there is exactly one disclosure control per expandable card', async ({page}) => {
  await page.goto('/');
  await openSources(page);
  for (const [selector, label] of EXPANDABLE) {
    await expect(page.locator(`${selector} > .card-header .dp-settings-disclosure`),
      `${label} does not carry exactly one canonical disclosure in its header`).toHaveCount(1);
  }
  // Premium Services is not an expandable owner: this pass gave a disclosure
  // only to the group that needed one.
  await expect(page.locator('.dp-settings-debrid-services > .card-header .dp-settings-disclosure'))
    .toHaveCount(0);
});
