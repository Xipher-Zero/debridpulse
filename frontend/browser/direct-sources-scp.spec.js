const { test, expect } = require('@playwright/test');

/* DP 1.0.13 SCP Network Source.
 *
 * The SCP member is one more box rendered by the existing Network Sources
 * composer from its own published metadata, with the existing protocol chip
 * (Lucide FileDown, lavender #A78BFA). Transfer surfaces show SCP through the
 * existing provider chip and Route History, while the Route History location
 * truthfully names the SFTP endpoint aria2 executed.
 *
 * This file writes no canonical state. `integrations.general_scp.enabled` has
 * one spec owner, general-sources-master.spec.js, which operates the SCP Enable
 * as an immediate canonical control beside the other Network Sources members. */

const SCP = 'general_scp';

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

async function openNetworkSources(page) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await expect(page.locator('.dp-settings-panel[data-panel="sources"]')).toBeVisible();
  const group = page.locator('.dp-settings-general-sources');
  const disclosure = group.locator('.dp-settings-disclosure');
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
  await expect(group.locator('.dp-settings-provider-card--general-scp')).toBeVisible();
}

test('the SCP box is a Network Sources member with the FileDown lavender chip', async ({page}) => {
  await isolateExternalFonts(page);
  await page.goto('/');
  await openNetworkSources(page);
  const box = page.locator('.dp-settings-provider-card--general-scp');
  await expect(box).toHaveClass(/dp-settings-source-box/);
  await expect(box.locator('.card-title')).toHaveText('SCP');
  const chip = box.locator('.dp-settings-source-box-head .dp-settings-protocol-chip');
  await expect(chip).toHaveCount(1);
  await expect(chip).toHaveAttribute('data-protocol', SCP);
  const rendered = await chip.evaluate(node => {
    const probe = document.createElement('span');
    probe.style.color = getComputedStyle(node).getPropertyValue('--dp-protocol-color').trim();
    document.body.appendChild(probe);
    const colour = getComputedStyle(probe).color;
    probe.remove();
    return {src: new URL(node.querySelector('img').getAttribute('src'), location.origin).pathname,
            colour, className: node.className};
  });
  expect(rendered).toEqual({src: '/icons/lucide/file-down.svg', colour: 'rgb(167, 139, 250)',
                            className: 'dp-settings-protocol-chip'});
  // The shared chip primitive: the same geometry as its (S)FTP sibling.
  const size = locator => locator.evaluate(node => {
    const r = node.getBoundingClientRect();
    return `${Math.round(r.width)}x${Math.round(r.height)}`;
  });
  expect(await size(chip)).toBe(await size(page.locator(
    '.dp-settings-provider-card--general-ftp .dp-settings-protocol-chip')));
  // Operator wording names SCP, never the execution internals.
  const text = await box.innerText();
  for (const internal of ['SFTP', 'aria2', 'normaliz', 'adapter', 'route']) expect(text).not.toContain(internal);
  await expect(box.locator('input')).toHaveCount(1);
  await expect(box.locator('button')).toHaveCount(0);
});

const SUBMITTED = 'scp://files.example.org:2222/home/xipher/file.bin';
const EXECUTED = 'sftp://files.example.org:2222/home/xipher/file.bin';

function scpItem(overrides = {}) {
  return {
    id: 991, name: 'file.bin', status: 'downloading', presentation_status: 'downloading', progress: 40,
    size_bytes: 2048, source: 'direct_link', request_kinds: ['scp'], label: '', hash: '',
    created_at: '2026-09-26T00:00:00Z', provider_provenance_status: 'recorded',
    current_source_identity: {kind: 'host', host: 'files.example.org'},
    current_provider_id: SCP, current_provider_name: 'SCP',
    delivering_provider_id: null, delivering_provider_name: null,
    ...overrides,
  };
}

function scpDetail() {
  return scpItem({
    original_resource: SUBMITTED, files: [], source_outcomes: [], events: [], executors: ['aria2'],
    execution_attempts: [],
    route_attempts: [{ordinal: 1, presentation_ordinal: 1, provider_id: SCP, provider_name: 'SCP',
      outcome: 'active', relation: 'original', route_identity: EXECUTED, route_location: EXECUTED}],
  });
}

test('Recent Items, Downloads and Details present an SCP-claimed transfer as SCP, truthfully', async ({page}) => {
  await isolateExternalFonts(page);
  const item = scpItem();
  const detail = scpDetail();
  await page.route(url => url.pathname === '/api/torrents',
    route => route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({items: [item], total: 1})}));
  await page.route(url => url.pathname === `/api/torrents/${item.id}`,
    route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(detail)}));

  await page.goto('/');
  await expect(page.locator(`#dash-tbody tr[data-torrent-id="${item.id}"] .dp-provider-chip`)).toHaveText('SCP');

  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await expect(page.locator('#view-torrents')).toHaveClass(/\bactive\b/);
  const row = page.locator(`#t-tbody tr[data-torrent-id="${item.id}"]`);
  await expect(row.locator('.dp-provider-chip')).toHaveText('SCP');
  await expect(row).not.toContainText('SFTP');

  await page.evaluate(id => showDetail(id), item.id);
  await expect(page.locator('.dp-detail-original-resource .dv')).toHaveText(SUBMITTED);
  const route = page.locator('.dp-detail-route-row');
  await expect(route).toHaveCount(1);
  await expect(route.locator('.dp-detail-route-provider')).toHaveText('SCP');
  // The execution endpoint is shown as what it is; the provider stays SCP.
  await expect(route.locator('.dp-detail-route-identity')).toContainText(EXECUTED);
});

/* The one INPUT_REQUIRED modal owner presents an identity-only answer when the
 * backend already holds credentials for the transfer: identity is shown, no
 * credential field exists, and nothing secret is submitted. */
test('an identity-only challenge asks for the fingerprint and sends no credentials', async ({page}) => {
  await isolateExternalFonts(page);
  const fingerprint = '208c2653f8ed2c0d7b62d69b304e8016e4151f60';
  const item = scpItem({
    id: 992, status: 'input_required', presentation_status: 'input_required',
    input_required: {
      id: 'challenge-992', generation: 1, reason: 'server_identity_required', origin: 'provider',
      methods: [{method: 'server_identity', fields: []}],
      facts: [{name: 'server_host', value: 'files.example.org'},
              {name: 'server_identity_algorithm', value: 'sha-1'},
              {name: 'server_identity_fingerprint', value: fingerprint}],
    },
  });
  const submissions = [];
  await page.route('**/api/torrents**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/api/torrents' && request.method() === 'GET') {
      return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items: [item], total: 1})});
    }
    if (url.pathname === `/api/torrents/${item.id}/input` && request.method() === 'POST') {
      submissions.push(request.postDataJSON());
      item.status = 'downloading';
      item.input_required = null;
      return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true, id: item.id})});
    }
    if (url.pathname === `/api/torrents/${item.id}` && request.method() === 'GET') {
      return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(item)});
    }
    return route.continue();
  });
  await page.goto('/');
  const modal = page.locator('[data-dp-input-required-modal]');
  await expect(modal).toBeVisible();
  await expect(modal.locator('#dp-auth-required-title')).toHaveText('Verify Server Identity');
  await expect(modal.locator('[data-dp-identity-fingerprint]')).toHaveText(fingerprint);
  await expect(modal.locator('[data-dp-auth-credentials-held]')).toBeVisible();
  await expect(modal.locator('[data-dp-auth-username]')).toHaveCount(0);
  await expect(modal.locator('[data-dp-auth-secret]')).toHaveCount(0);
  await modal.locator('[data-dp-auth-continue]').click();
  await expect(modal).toHaveCount(0);
  expect(submissions).toEqual([{challenge_id: 'challenge-992', method: 'server_identity'}]);
});
