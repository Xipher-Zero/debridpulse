const { test, expect } = require('@playwright/test');

/* DP 1.0.13 rsync Network Source and its Transfer Method Settings.
 *
 * The rsync member is one more box rendered by the existing Network Sources
 * composer from its own published metadata, with the existing protocol chip
 * (Lucide FolderSync, Deep Violet #7C3AED). Its executor tuning is one more
 * Transfer Method Settings card built by the canonical executor-tuning card,
 * disclosure and tuning grid -- nothing here is rsync-specific markup, CSS,
 * disclosure code or persistence.
 *
 * Canonical state: this file is the ONE spec file that writes the rsync
 * executor's options (`integrations.rsync.options.*`), and it restores exactly
 * what it found. `integrations.general_rsync.enabled` has one spec owner,
 * general-sources-master.spec.js. */

const PROVIDER = 'general_rsync';
const VIOLET = 'rgb(124, 58, 237)';
const TUNABLES = {
  transfer: ['rsync_partial_transfers', 'rsync_compression', 'rsync_preserve_modification_time'],
  timeouts: ['rsync_connection_timeout_seconds', 'rsync_transfer_timeout_seconds'],
};

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

async function openTab(page, tab) {
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator(`#view-settings [data-tab="${tab}"]`).click();
  await expect(page.locator(`.dp-settings-panel[data-panel="${tab}"]`)).toBeVisible();
}

async function openNetworkSources(page) {
  await openTab(page, 'sources');
  const group = page.locator('.dp-settings-general-sources');
  const disclosure = group.locator('.dp-settings-disclosure');
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
  await expect(group.locator('.dp-settings-provider-card--general-rsync')).toBeVisible();
}

const chipFacts = chip => chip.evaluate(node => {
  const probe = document.createElement('span');
  probe.style.color = getComputedStyle(node).getPropertyValue('--dp-protocol-color').trim();
  document.body.appendChild(probe);
  const colour = getComputedStyle(probe).color;
  probe.remove();
  const box = node.getBoundingClientRect();
  return {src: new URL(node.querySelector('img').getAttribute('src'), location.origin).pathname, colour,
          className: node.className, size: `${Math.round(box.width)}x${Math.round(box.height)}`,
          glow: getComputedStyle(node.querySelector('img')).filter};
});

// ── Services -> Network Sources ────────────────────────────────────────────

test('the rsync box is a registered Network Sources member after SCP with the FolderSync violet chip',
  async ({page}) => {
    await isolateExternalFonts(page);
    await openNetworkSources(page);
    const box = page.locator('.dp-settings-provider-card--general-rsync');
    await expect(box).toHaveClass(/dp-settings-source-box/);
    await expect(box.locator('.card-title')).toHaveText('rsync');
    await expect(box).toContainText('rsync sources.');
    // One operator source: the transport is the server's to decide, never a label.
    await expect(box).not.toContainText('SSH');
    const order = await page.locator('.dp-settings-general-sources .dp-settings-source-box').evaluateAll(
      nodes => nodes.map(node => [...node.classList].find(name => name.startsWith('dp-settings-provider-card--'))));
    expect(order.indexOf('dp-settings-provider-card--general-rsync'))
      .toBe(order.indexOf('dp-settings-provider-card--general-scp') + 1);
    const chip = box.locator('.dp-settings-source-box-head .dp-settings-protocol-chip');
    await expect(chip).toHaveCount(1);
    await expect(chip).toHaveAttribute('data-protocol', PROVIDER);
    const facts = await chipFacts(chip);
    const sibling = await chipFacts(page.locator('.dp-settings-provider-card--general-scp .dp-settings-protocol-chip'));
    expect(facts.src).toBe('/icons/lucide/folder-sync.svg');
    expect(facts.colour).toBe(VIOLET);
    expect(facts.className).toBe('dp-settings-protocol-chip');
    // The one shared chip primitive: identical geometry and glow treatment.
    expect(facts.size).toBe(sibling.size);
    expect(facts.glow).toContain('drop-shadow');
    // Enable is the only direct action; no auth field, no Test, no internals.
    await expect(box.locator('input')).toHaveCount(1);
    await expect(box.locator(`input[data-integration-enabled="${PROVIDER}"]`)).toHaveCount(1);
    await expect(box.locator('button')).toHaveCount(0);
    const text = await box.innerText();
    for (const internal of ['aria2', 'daemon', 'executor', 'module', '--', 'password']) {
      expect(text).not.toContain(internal);
    }
  });

test('the Network Sources capacity grid left-fills with the rsync member', async ({page}) => {
  await isolateExternalFonts(page);
  await page.setViewportSize({width: 1400, height: 900});
  await openNetworkSources(page);
  for (const width of [1400, 900, 560]) {
    await page.setViewportSize({width, height: 900});
    const lefts = await page.locator('.dp-settings-general-sources .dp-settings-source-box').evaluateAll(
      nodes => nodes.map(node => Math.round(node.getBoundingClientRect().left)));
    const columns = [...new Set(lefts)].sort((a, b) => a - b);
    // Every box sits in a column the first row established: no stray offsets
    // and no gap before a wrapped member.
    expect(columns.length).toBeGreaterThan(0);
    const firstRow = lefts.slice(0, columns.length);
    expect(firstRow).toEqual(columns);
    for (const left of lefts) expect(columns).toContain(left);
  }
});

// ── Downloads -> Transfer Method Settings ──────────────────────────────────

test('the rsync Transfer Method card shares the identity and the canonical disclosure', async ({page}) => {
  await isolateExternalFonts(page);
  await openTab(page, 'downloads');
  const card = page.locator('#view-settings [data-executor-tuning="rsync"]');
  await expect(card).toHaveCount(1);
  await expect(card).toHaveClass(/dp-executor-tuning-card/);
  await expect(card.locator('.card-title')).toContainText('rsync');
  await expect(card).toContainText('How DebridPulse transfers files using rsync.');
  const chip = card.locator('.card-title .dp-settings-protocol-chip');
  await expect(chip).toHaveAttribute('data-protocol', PROVIDER);
  const facts = await chipFacts(chip);
  expect([facts.src, facts.colour]).toEqual(['/icons/lucide/folder-sync.svg', VIOLET]);
  const usenet = await chipFacts(page.locator('#view-settings [data-executor-tuning="usenet"] .dp-settings-protocol-chip'));
  expect(facts.size).toBe(usenet.size);
  // Collapsed by default, through the one disclosure owner shared with its siblings.
  const disclosure = card.locator('.dp-settings-disclosure');
  await expect(disclosure).toHaveAttribute('aria-expanded', 'false');
  await expect(card.locator('.card-body')).toBeHidden();
  await disclosure.click();
  await expect(disclosure).toHaveAttribute('aria-expanded', 'true');
  await expect(card.locator('.card-body')).toBeVisible();
  const sameChevron = await page.evaluate(() => {
    const own = document.querySelector('[data-executor-tuning="rsync"] .dp-settings-disclosure');
    const other = document.querySelector('[data-executor-tuning="usenet"] .dp-settings-disclosure');
    return own.className === other.className && own.innerHTML === other.innerHTML;
  });
  expect(sameChevron).toBe(true);
});

test('five canonical tuning cells in a 3-card and a 2-card relationship group', async ({page}) => {
  await isolateExternalFonts(page);
  await openTab(page, 'downloads');
  const card = page.locator('#view-settings [data-executor-tuning="rsync"]');
  await card.locator('.dp-settings-disclosure').click();
  const grid = card.locator('.dp-settings-tuning-grid');
  await expect(grid).toHaveCount(1);
  await expect(grid).toHaveAttribute('data-tuning-lanes', '5');
  const groups = grid.locator(':scope > .dp-settings-tuning-group');
  await expect(groups).toHaveCount(2);
  const members = await groups.evaluateAll(nodes => nodes.map(node => ({
    span: node.dataset.tuningSpan,
    keys: [...node.querySelectorAll('[data-setting]')].map(input => input.dataset.setting),
  })));
  expect(members).toEqual([
    {span: '3', keys: TUNABLES.transfer},
    {span: '2', keys: TUNABLES.timeouts},
  ]);
  // Exactly the five tunables -- no free-form native arguments anywhere.
  const settings = await card.locator('[data-setting]').evaluateAll(nodes => nodes.map(node => node.dataset.setting));
  expect(settings).toEqual([...TUNABLES.transfer, ...TUNABLES.timeouts]);
  await expect(card.locator('textarea, input[type="text"]')).toHaveCount(0);
  const text = await card.innerText();
  expect(text.toLowerCase()).not.toContain('argument');
  // Defaults: Partial Transfers on, Compression off, Preserve Modification Time on.
  const live = await page.request.get('/api/settings').then(r => r.json());
  const options = live.integrations.rsync.options;
  expect(await card.locator('[data-setting="rsync_partial_transfers"]').isChecked()).toBe(options.partial_transfers);
  expect(await card.locator('[data-setting="rsync_compression"]').isChecked()).toBe(options.compression);
  expect(await card.locator('[data-setting="rsync_preserve_modification_time"]').isChecked())
    .toBe(options.preserve_modification_time);
  // No rsync-specific layout: the cells are the grid's own tuning fields.
  const wrap = await page.evaluate(() => {
    const own = document.querySelector('[data-executor-tuning="rsync"] .dp-settings-tuning-grid');
    const other = document.querySelector('[data-executor-tuning="direct"] .dp-settings-tuning-grid');
    return getComputedStyle(own).display === getComputedStyle(other).display;
  });
  expect(wrap).toBe(true);
});

test.describe.serial('rsync tunables persist through the canonical integration owner', () => {
  let original = null;

  test.beforeAll(async ({request}) => {
    original = (await request.get('/api/settings').then(r => r.json())).integrations.rsync.options;
  });

  test.afterAll(async ({request}) => {
    if (!original) return;
    await request.patch('/api/integrations/rsync/configuration', {data: {options: {
      compression: original.compression, connection_timeout_seconds: original.connection_timeout_seconds}}});
  });

  test('a toggle commits immediately and a number at its field boundary', async ({page}) => {
    await isolateExternalFonts(page);
    const writes = [];
    page.on('request', request => {
      // Settings mutations only (the page's own POST status probes are reads).
      if (['PATCH', 'PUT'].includes(request.method()) && request.url().includes('/api/')) {
        writes.push(`${request.method()} ${new URL(request.url()).pathname}`);
      }
    });
    await openTab(page, 'downloads');
    const card = page.locator('#view-settings [data-executor-tuning="rsync"]');
    await card.locator('.dp-settings-disclosure').click();
    const canonical = () => page.request.get('/api/settings').then(r => r.json())
      .then(settings => settings.integrations.rsync.options);

    const compression = card.locator('[data-setting="rsync_compression"]');
    const wanted = !(await compression.isChecked());
    await card.locator('label[for="' + await compression.getAttribute('id') + '"].dp-settings-engine-tuning-toggle-control')
      .click();
    await expect.poll(async () => (await canonical()).compression).toBe(wanted);

    const timeout = card.locator('[data-setting="rsync_connection_timeout_seconds"]');
    const value = original.connection_timeout_seconds === 45 ? 46 : 45;
    await timeout.fill(String(value));
    await timeout.blur();
    await expect.poll(async () => (await canonical()).connection_timeout_seconds).toBe(value);
    // One canonical scope for both commits; never a page-level save.
    expect(writes.length).toBeGreaterThan(0);
    expect(writes.filter(write => write !== 'PATCH /api/integrations/rsync/configuration'), 'writes').toEqual([]);
    await expect(page.locator('#view-settings [data-action="save"]')).toHaveCount(0);
  });
});

// ── Transfer presentation ──────────────────────────────────────────────────

const SUBMITTED = 'rsync+ssh://files.example.org:2222/srv/media/file.bin';

function rsyncItem(overrides = {}) {
  return {
    id: 993, name: 'file.bin', status: 'downloading', presentation_status: 'downloading', progress: 40,
    size_bytes: 2048, source: 'direct_link', request_kinds: ['rsync+ssh'], label: '', hash: '',
    created_at: '2026-09-28T00:00:00Z', provider_provenance_status: 'recorded',
    current_source_identity: {kind: 'host', host: 'files.example.org'},
    current_provider_id: PROVIDER, current_provider_name: 'rsync',
    delivering_provider_id: null, delivering_provider_name: null,
    ...overrides,
  };
}

test('an rsync-over-SSH transfer is presented as rsync everywhere, Route History included', async ({page}) => {
  await isolateExternalFonts(page);
  const item = rsyncItem();
  const detail = rsyncItem({
    original_resource: SUBMITTED, files: [], source_outcomes: [], events: [], executors: ['rsync'],
    execution_attempts: [],
    route_attempts: [{ordinal: 1, presentation_ordinal: 1, provider_id: PROVIDER, provider_name: 'rsync',
      outcome: 'active', relation: 'original', route_identity: SUBMITTED, route_location: SUBMITTED}],
  });
  await page.route(url => url.pathname === '/api/torrents',
    route => route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({items: [item], total: 1})}));
  await page.route(url => url.pathname === `/api/torrents/${item.id}`,
    route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(detail)}));

  await page.goto('/');
  await expect(page.locator(`#dash-tbody tr[data-torrent-id="${item.id}"] .dp-provider-chip`)).toHaveText('rsync');
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await expect(page.locator('#view-torrents')).toHaveClass(/\bactive\b/);
  const row = page.locator(`#t-tbody tr[data-torrent-id="${item.id}"]`);
  await expect(row.locator('.dp-provider-chip')).toHaveText('rsync');
  await page.evaluate(id => showDetail(id), item.id);
  await expect(page.locator('.dp-detail-original-resource .dv')).toHaveText(SUBMITTED);
  const route = page.locator('.dp-detail-route-row');
  await expect(route).toHaveCount(1);
  await expect(route.locator('.dp-detail-route-provider')).toHaveText('rsync');
  await expect(route).not.toContainText('ssh -');
});

// ── One INPUT_REQUIRED modal, driven only by the advertised methods ─────────

const KEY_TEXT = [
  '-----BEGIN OPENSSH PRIVATE KEY-----',
  'b3BlbnNzaC1rZXktdjEAAAAACmFlczI1Ni1jdHIAAAAGYmNyeXB0AAAAGAAAABBrsyncmodalfixture',
  '-----END OPENSSH PRIVATE KEY-----', ''].join('\n');

function challengeItem(id, methods) {
  return rsyncItem({
    id, status: 'input_required', presentation_status: 'input_required',
    input_required: {id: `challenge-${id}`, generation: 1, reason: 'auth_required', origin: 'provider',
      methods, facts: []},
  });
}

const PASSWORD_METHOD = {method: 'username_password', fields: [
  {name: 'username', required: true}, {name: 'password', required: true}]};
const KEY_METHOD = {method: 'username_private_key', fields: [
  {name: 'username', required: true}, {name: 'private_key', required: true}, {name: 'passphrase', required: false}]};

async function serveChallenge(page, item, submissions) {
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
}

test('an rsync-over-SSH challenge offers password and Keyfile; a key switches the secret to Passphrase',
  async ({page}) => {
    await isolateExternalFonts(page);
    const submissions = [];
    await serveChallenge(page, challengeItem(994, [PASSWORD_METHOD, KEY_METHOD]), submissions);
    await page.goto('/');
    const modal = page.locator('[data-dp-input-required-modal]');
    await expect(modal).toBeVisible();
    await expect(modal.locator('[data-dp-auth-secret-label]')).toHaveText('Password');
    const keyfile = modal.locator('[data-dp-auth-key]');
    await expect(keyfile).toBeVisible();
    await expect(keyfile).toContainText('Select Keyfile');
    await modal.locator('[data-dp-auth-username]').fill('operator');
    await modal.locator('[data-dp-auth-key-input]').setInputFiles(
      {name: 'id_ed25519', mimeType: 'application/octet-stream', buffer: Buffer.from(KEY_TEXT)});
    await expect(modal.locator('[data-dp-auth-secret-label]')).toHaveText('Passphrase');
    await modal.locator('[data-dp-auth-secret]').fill('modal-passphrase');
    await modal.locator('[data-dp-auth-continue]').click();
    await expect(modal).toHaveCount(0);
    expect(submissions).toEqual([{challenge_id: 'challenge-994', method: 'username_private_key',
      username: 'operator', private_key: KEY_TEXT, passphrase: 'modal-passphrase'}]);
  });

test('a password-only challenge never offers a Keyfile', async ({page}) => {
  await isolateExternalFonts(page);
  await serveChallenge(page, challengeItem(995, [PASSWORD_METHOD]), []);
  await page.goto('/');
  const modal = page.locator('[data-dp-input-required-modal]');
  await expect(modal).toBeVisible();
  await expect(modal.locator('[data-dp-auth-secret-label]')).toHaveText('Password');
  await expect(modal.locator('[data-dp-auth-key]')).toHaveCount(0);
});
