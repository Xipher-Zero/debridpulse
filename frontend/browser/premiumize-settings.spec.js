const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Premiumize: the Premium Services card beside Debrid-Link.
 *
 * Every Premiumize endpoint is answered here, so no account is needed and no
 * canonical state is written: this file owns no settings key. What it proves
 * is that Premiumize is the Debrid-Link API-key card's structure -- the
 * shipped mark, the same credential island, Test and Enable as independent
 * controls, exactly the scoped Additional Settings in the one tuning grid --
 * and that the one premium-account owner composes a healthy Premiumize
 * account under the Provider Status crown and nothing for a free one. */

async function prepare(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  const calls = [];
  await page.route(url => url.pathname === '/api/settings/validate-premiumize', route => {
    calls.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`);
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true})});
  });
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await page.locator('#view-settings [data-tab="sources"]').click();
  const card = page.locator('.dp-settings-provider-card--premiumize');
  await card.locator('.dp-settings-disclosure').click();
  return {card, calls};
}

test('the Premiumize card is the API-key premium card with exactly the scoped Additional Settings', async ({page}) => {
  const {card, calls} = await prepare(page);
  await expect(page.locator('.dp-settings-debrid-services .dp-settings-provider-card--premiumize')).toHaveCount(1);
  const logo = card.locator('.dp-settings-provider-logo--premiumize');
  await expect(logo).toHaveAttribute('src', '/icons/providers/premiumize.svg');
  expect(await logo.evaluate(image => image.complete && image.naturalWidth > 0)).toBe(true);
  await expect(card.locator('.dp-settings-provider-header-copy'))
    .toHaveText('Resolve supported links, torrents and NZBs through your Premiumize account.');

  // The credential island is Debrid-Link's, exactly.
  const geometry = async id => {
    const owner = page.locator(`.dp-settings-provider-card--${id}`);
    const disclosure = owner.locator('.dp-settings-disclosure');
    if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
    return owner.locator(`.dp-settings-${id}-key-row`).evaluate(row => {
      const style = getComputedStyle(row);
      return {border: style.borderTopStyle, radius: style.borderTopLeftRadius, padding: style.padding,
              width: Math.round(row.getBoundingClientRect().width)};
    });
  };
  expect(await geometry('premiumize')).toEqual(await geometry('debridlink'));
  await expect(card.locator('[data-setting="premiumize_api_key"]')).toHaveAttribute('type', 'password');
  await expect(card.locator('[data-setting="premiumize_api_key"]')).toHaveAttribute('data-commit-scope',
                                                                                   'integration:premiumize');

  // Test and Enable are independent: proving the key enables nothing.
  const enable = card.locator('[data-integration-enabled="premiumize"]');
  await expect(enable).not.toBeChecked();
  await card.locator('.dp-settings-card-header [data-action="test-premiumize"]').click();
  await expect.poll(() => calls).toContain('POST /api/settings/validate-premiumize');
  await expect(enable).not.toBeChecked();

  await card.locator('.dp-settings-additional > summary').click();
  const tuning = card.locator('.dp-settings-additional-body');
  await expect(tuning.locator('.dp-settings-tuning-grid')).toHaveCount(1);
  await expect(tuning.locator('[data-setting]')).toHaveCount(5);
  const nzb = tuning.locator('[data-setting="premiumize_use_before_usenet"]');
  await expect(nzb).not.toBeChecked();
  await expect(tuning).toContainText('Use Premiumize Before Usenet');
  await expect(tuning).toContainText(
    'Try Premiumize first when resolving NZB downloads. Your configured Usenet service remains available as fallback.');
  const backups = tuning.locator('[data-setting="premiumize_prepare_backup_torrents"]');
  await expect(backups).not.toBeChecked();
  await expect(tuning).toContainText('Prepare Backup Torrents');
  for (const toggle of [nzb, backups]) {
    await expect(toggle).toHaveAttribute('data-commit', 'immediate');
    await expect(toggle).toHaveAttribute('data-commit-scope', 'integration:premiumize');
  }
  for (const [key, value, min, max] of [
    ['premiumize_request_timeout_seconds', '30', '5', '300'],
    ['premiumize_upload_timeout_seconds', '120', '30', '900'],
    ['premiumize_host_refresh_interval_hours', '24', '1', '168'],
  ]) {
    const control = tuning.locator(`[data-setting="${key}"]`);
    await expect(control).toHaveValue(value);
    await expect(control).toHaveAttribute('min', min);
    await expect(control).toHaveAttribute('max', max);
    await expect(control).toHaveAttribute('data-commit-scope', 'integration:premiumize');
    await expect(control).not.toHaveAttribute('data-commit', 'immediate');
  }
});

test('the Premiumize card stays inside a phone-width viewport', async ({page}) => {
  const {card} = await prepare(page);
  await page.setViewportSize({width: 390, height: 900});
  await card.locator('.dp-settings-additional > summary').click();
  const box = await card.evaluate(node => ({overflow: node.scrollWidth - node.clientWidth,
                                            right: node.getBoundingClientRect().right,
                                            viewport: document.documentElement.clientWidth}));
  expect(box.overflow).toBeLessThanOrEqual(0);
  expect(box.right).toBeLessThanOrEqual(box.viewport);
});

const account = (serviceClass, days) => ({entitlement: 'ready', service_class: serviceClass, functional: 'usable',
  plan: '', ...(days ? {expires_at: Math.floor(Date.now() / 1000) + days * 86400} : {})});

test('a healthy premium Premiumize joins the crown and a free or unhealthy one adds nothing', async ({page}) => {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  await page.waitForFunction(() => !!window.DPPremiumAccount);
  const compose = entries => page.evaluate(next => {
    document.dispatchEvent(new CustomEvent('debridpulse:provider-status', {detail: {entries: next}}));
    return [...document.querySelectorAll('#lbl-premium .dp-provider-premium-account')].map(block => block.textContent);
  }, entries);
  const entry = (state, status) => ({id: 'premiumize', name: 'Premiumize', state, status});

  const [alone] = await compose([entry('healthy', {account: account('premium', 40)})]);
  expect(alone).toMatch(/^Premiumize Premium until \d\d\.\d\d\.\d{4}\(\d+ days remaining\)$/);
  expect(await compose([entry('healthy', {account: account('standard')})])).toEqual([]);
  expect(await compose([entry('auth_required', {account: account('premium', 40)})])).toEqual([]);
});
