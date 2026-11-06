const { test, expect } = require('@playwright/test');

/* DP 1.0.14 services correction: the API Key Remove of AllDebrid, Debrid-Link
 * and Premiumize is one control INSIDE the key field (the shared compound
 * field, .dp-action-field), right-aligned and vertically centred. There is no
 * "Key present" status and no standalone button beneath the field. With a key
 * persisted it is the red DP button; with none it is the neutral ghost and
 * disabled, so no destructive act is offered. The state is the persisted
 * credential's, never the draft's.
 *
 * The settings document is SERVED and every removal is answered here, so no
 * canonical state is written and no provider is contacted: all three providers
 * are served disabled, so the provider-status owner probes none of them.
 * settings-providers-persistence.spec.js owns the real AllDebrid lifecycle. */

const PROVIDERS = [
  {id: 'alldebrid', name: 'AllDebrid'},
  {id: 'debridlink', name: 'Debrid-Link'},
  {id: 'premiumize', name: 'Premiumize'},
];

async function prepare(page, configured, {fail = false} = {}) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  const live = await page.request.get('/api/settings').then(response => response.json());
  const integrations = {...live.integrations};
  for (const {id} of PROVIDERS) {
    integrations[id] = {...live.integrations[id], enabled: false,
      options: {...(live.integrations[id]?.options || {}), api_key_configured: configured}};
  }
  const served = {...live, integrations};
  page.__writes = [];
  await page.route(url => url.pathname === '/api/settings', route =>
    (route.request().method() === 'GET' ? route.fulfill({json: served}) : route.fallback()));
  await page.route(url => /^\/api\/integrations\/(alldebrid|debridlink|premiumize)\/configuration$/.test(url.pathname),
    route => {
      const request = route.request();
      const id = new URL(request.url()).pathname.split('/')[3];
      page.__writes.push({method: request.method(), id, body: request.postDataJSON?.() || null});
      if (fail) return route.fulfill({status: 502, json: {detail: 'removal rejected'}});
      const entry = {...served.integrations[id], options: {...served.integrations[id].options, api_key_configured: false}};
      served.integrations[id] = entry;
      return route.fulfill({json: {ok: true, ...entry}});
    });
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await page.locator('#view-settings [data-tab="sources"]').click();
  for (const {id} of PROVIDERS) {
    const disclosure = page.locator(`.dp-settings-provider-card--${id} .dp-settings-disclosure`);
    if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
    await expect(row(page, id)).toBeVisible();
  }
}

const row = (page, id) => page.locator(`.dp-settings-provider-card--${id} .dp-settings-${id}-key-row`);
const remove = (page, id) => row(page, id).locator(`[data-action="clear-${id}-key"]`);
const danger = page => page.locator('.dp-modal-overlay .dp-modal-dialog[data-tone="danger"]');

// The key field as drawn: Remove inside its border, and the key text's end.
const drawn = (page, id) => row(page, id).evaluate((island, id) => {
  const field = island.querySelector('.dp-settings-inline-field-control > .dp-action-field');
  const input = field?.querySelector(':scope > .input');
  const button = field?.querySelector(`:scope > [data-action="clear-${id}-key"]`);
  const r = node => node.getBoundingClientRect();
  const style = getComputedStyle(input);
  return {
    composite: !!(field && input && button),
    // A header Help lent to this island (narrow widths) is the header's, not
    // the field's; it must cover none of the field.
    buttons: island.querySelectorAll('button:not(.dp-settings-help-chip)').length,
    helpOverField: [...island.querySelectorAll('.dp-settings-help-chip')].some(help => {
      const h = r(help), f = r(field);
      return h.left < f.right && h.right > f.left && h.top < f.bottom && h.bottom > f.top;
    }),
    status: island.querySelectorAll('.dp-settings-key-present, [role="status"]').length,
    separateAction: !!island.querySelector('.dp-settings-inline-field-action'),
    statusText: /Key present/.test(island.textContent),
    trailing: r(field).right - r(button).right,
    centred: (r(button).top + r(button).bottom) / 2 - (r(field).top + r(field).bottom) / 2,
    inside: r(button).top >= r(field).top && r(button).bottom <= r(field).bottom,
    // The input's text box ends before Remove begins: text cannot run under it.
    overlap: r(input).right - parseFloat(style.paddingRight) - r(button).left,
  };
}, id);

for (const configured of [true, false]) {
  test(`the API Key Remove is inside the field and toned by the persisted key (${configured ? 'stored' : 'none'})`,
    async ({page}) => {
      await page.setViewportSize({width: 1280, height: 900});
      await prepare(page, configured);
      for (const viewport of [1280, 320]) {
        await page.setViewportSize({width: viewport, height: 900});
        for (const {id, name} of PROVIDERS) {
          const button = remove(page, id);
          await button.scrollIntoViewIfNeeded();
          const at = `${id} @${viewport}`;
          const geometry = await drawn(page, id);
          expect(geometry.composite, at).toBe(true);
          expect(geometry.buttons, at).toBe(1);
          expect(geometry.helpOverField, at).toBe(false);
          expect(geometry.status, at).toBe(0);
          expect(geometry.statusText, at).toBe(false);
          expect(geometry.separateAction, at).toBe(false);
          expect(geometry.inside, at).toBe(true);
          expect(geometry.trailing, at).toBeGreaterThanOrEqual(0);
          expect(geometry.trailing, at).toBeLessThanOrEqual(6);
          expect(Math.abs(geometry.centred), at).toBeLessThanOrEqual(1);
          expect(geometry.overlap, at).toBeLessThanOrEqual(0);
          await expect(button).toHaveText('Remove');
          await expect(button).toHaveAccessibleName(`Remove the stored ${name} API key`);
          if (configured) {
            await expect(button).toBeEnabled();
            await expect(button).toHaveClass(/\bbtn-danger\b/);
            await expect(button).not.toHaveClass(/\bbtn-ghost\b/);
          } else {
            await expect(button).toBeDisabled();
            await expect(button).toHaveClass(/\bbtn-ghost\b/);
            await expect(button).not.toHaveClass(/\bbtn-danger\b/);
          }
        }
      }
      // A typed, unsaved key changes nothing: the state is the persisted one.
      const field = row(page, 'debridlink').locator('[data-setting="debridlink_api_key"]');
      await field.fill('synthetic-unsaved-draft');
      await expect(remove(page, 'debridlink')).toHaveClass(configured ? /\bbtn-danger\b/ : /\bbtn-ghost\b/);
      expect(page.__writes).toEqual([]);
    });
}

test('Remove keeps its confirmation, cancel and confirmed removal for every provider, by keyboard', async ({page}) => {
  await prepare(page, true);
  for (const {id, name} of PROVIDERS) {
    const field = row(page, id).locator(`[data-setting="${id}_api_key"]`);
    const button = remove(page, id);
    // Keyboard: the field, then its own Remove.
    await field.focus();
    await page.keyboard.press('Tab');
    await expect(button).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(danger(page)).toBeVisible();
    await expect(danger(page).locator('.dp-modal-title')).toHaveText(`Clear ${name} API key?`);
    await expect(danger(page).locator('[data-modal-accept]')).toHaveText(`Clear ${name} API Key`);
    await danger(page).locator('[data-modal-cancel]').click();
    await expect(danger(page)).toHaveCount(0);
    expect(page.__writes.filter(write => write.id === id)).toEqual([]);
    await expect(button).toHaveClass(/\bbtn-danger\b/);

    await button.click();
    await danger(page).locator('[data-modal-accept]').click();
    await expect(button).toBeDisabled();
    await expect(button).toHaveClass(/\bbtn-ghost\b/);
    await expect(button).not.toHaveClass(/\bbtn-danger\b/);
    await expect(button).toHaveText('Remove');
    const writes = page.__writes.filter(write => write.id === id);
    expect(writes).toHaveLength(1);
    expect(writes[0]).toEqual({method: 'PATCH', id, body: {options: {}, clear_secrets: ['api_key']}});
    await expect(row(page, id).locator('.form-hint')).toHaveText(`Enter an API key to connect your ${name} account.`);
  }
});

test('a failed Remove keeps the stored-key state and says so', async ({page}) => {
  await prepare(page, true, {fail: true});
  for (const {id} of PROVIDERS) {
    const button = remove(page, id);
    await button.click();
    await danger(page).locator('[data-modal-accept]').click();
    await expect(page.locator('#toasts .toast').filter({hasText: 'removal rejected'}).first()).toBeVisible();
    await expect(button).toBeEnabled();
    await expect(button).toHaveText('Remove');
    await expect(button).toHaveClass(/\bbtn-danger\b/);
  }
  expect(page.__writes.map(write => write.id)).toEqual(PROVIDERS.map(({id}) => id));
});
