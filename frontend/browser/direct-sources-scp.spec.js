const { test, expect } = require('@playwright/test');

/* DP 1.0.13 SCP Network Source (exact remote files).
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
