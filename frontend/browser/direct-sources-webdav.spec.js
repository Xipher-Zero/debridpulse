const { test, expect } = require('@playwright/test');

/* DP 1.0.13 WebDAV Network Source, its Transfer Method Settings, and the
 * origin it keeps in every compact transfer presentation.
 *
 * The WebDAV member is one more box rendered by the existing Network Sources
 * composer from its own published metadata, with the existing protocol chip
 * (Lucide CloudSync, Sky Blue #38BDF8). Its three tunables are one more
 * Transfer Method Settings card built by the canonical executor-tuning card,
 * disclosure, tuning grid, relationship group, bordered tuning cells and
 * closing sentence -- nothing here is WebDAV-specific markup, CSS, disclosure
 * code or persistence.
 *
 * Canonical state: this file is the ONE spec file that writes
 * `integrations.general_webdav.options.*` (directory_depth, max_files,
 * collection_scan_timeout_seconds), and it restores exactly what it found. `integrations.general_webdav.enabled` has one spec
 * owner, general-sources-master.spec.js. */

const PROVIDER = 'general_webdav';
const SKY = 'rgb(56, 189, 248)';
const DEPTHS = [
  ['current', 'Current directory only'],
  ['1', '1 subdirectory level'],
  ['2', '2 subdirectory levels'],
  ['3', '3 subdirectory levels'],
  ['all', 'All subdirectories'],
];
// Protocol mechanics never reach the operator.
const JARGON = ['PROPFIND', 'Depth:', 'infinity', 'multistatus', 'HTTP method', 'aria2', 'executor', 'general_http'];

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
  await expect(group.locator('.dp-settings-provider-card--general-webdav')).toBeVisible();
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

test('the WebDAV box is a registered Network Sources member with the CloudSync sky-blue chip', async ({page}) => {
  await isolateExternalFonts(page);
  await openNetworkSources(page);
  const box = page.locator('.dp-settings-provider-card--general-webdav');
  await expect(box).toHaveClass(/dp-settings-source-box/);
  await expect(box.locator('.card-title')).toHaveText('WebDAV');
  await expect(box).toContainText('WebDAV files and folders.');
  const order = await page.locator('.dp-settings-general-sources .dp-settings-source-box').evaluateAll(
    nodes => nodes.map(node => [...node.classList].find(name => name.startsWith('dp-settings-provider-card--'))));
  expect(order.indexOf('dp-settings-provider-card--general-webdav'))
    .toBe(order.indexOf('dp-settings-provider-card--general-rsync') + 1);
  const chip = box.locator('.dp-settings-source-box-head .dp-settings-protocol-chip');
  await expect(chip).toHaveAttribute('data-protocol', PROVIDER);
  const facts = await chipFacts(chip);
  const sibling = await chipFacts(page.locator('.dp-settings-provider-card--general-rsync .dp-settings-protocol-chip'));
  expect([facts.src, facts.colour, facts.className]).toEqual(['/icons/lucide/cloud-sync.svg', SKY,
    'dp-settings-protocol-chip']);
  // The one shared chip primitive and the one box treatment.
  expect(facts.size).toBe(sibling.size);
  expect(facts.glow).toContain('drop-shadow');
  const boxSize = await box.evaluate(node => [Math.round(node.getBoundingClientRect().height),
    getComputedStyle(node).fontFamily]);
  const peerSize = await page.locator('.dp-settings-provider-card--general-rsync').evaluate(node =>
    [Math.round(node.getBoundingClientRect().height), getComputedStyle(node).fontFamily]);
  expect(boxSize).toEqual(peerSize);
  // Enable is the only control; nothing to configure, test or save here.
  await expect(box.locator('input')).toHaveCount(1);
  await expect(box.locator(`input[data-integration-enabled="${PROVIDER}"]`)).toHaveCount(1);
  await expect(box.locator('button')).toHaveCount(0);
  const text = await box.innerText();
  for (const term of JARGON) expect(text).not.toContain(term);
});

// ── Downloads -> Transfer Method Settings ──────────────────────────────────

test('the WebDAV Transfer Method card shares the identity and the canonical disclosure', async ({page}) => {
  await isolateExternalFonts(page);
  await openTab(page, 'downloads');
  const card = page.locator('#view-settings [data-executor-tuning="webdav"]');
  await expect(card).toHaveCount(1);
  await expect(card).toHaveClass(/dp-executor-tuning-card/);
  await expect(card.locator('.card-title')).toContainText('WebDAV');
  const chip = card.locator('.card-title .dp-settings-protocol-chip');
  await expect(chip).toHaveAttribute('data-protocol', PROVIDER);
  const facts = await chipFacts(chip);
  expect([facts.src, facts.colour]).toEqual(['/icons/lucide/cloud-sync.svg', SKY]);
  const rsync = await chipFacts(page.locator('#view-settings [data-executor-tuning="rsync"] .dp-settings-protocol-chip'));
  expect(facts.size).toBe(rsync.size);
  // It sits with its peers, after rsync and before Usenet.
  const order = await page.locator('#view-settings [data-executor-tuning]').evaluateAll(
    nodes => nodes.map(node => node.dataset.executorTuning));
  expect(order).toEqual(['direct', 'rsync', 'webdav', 'usenet', 'media']);
  const disclosure = card.locator('.dp-settings-disclosure');
  await expect(disclosure).toHaveAttribute('aria-expanded', 'false');
  await expect(card.locator('.card-body')).toBeHidden();
  await disclosure.click();
  await expect(card.locator('.card-body')).toBeVisible();
  const sameChevron = await page.evaluate(() => {
    const own = document.querySelector('[data-executor-tuning="webdav"] .dp-settings-disclosure');
    const other = document.querySelector('[data-executor-tuning="rsync"] .dp-settings-disclosure');
    return own.className === other.className && own.innerHTML === other.innerHTML;
  });
  expect(sameChevron).toBe(true);
});

test('the three WebDAV tunables are one relationship group of bordered tuning cells, read whole', async ({page}) => {
  await isolateExternalFonts(page);
  for (const width of [1440, 1280, 1024]) {
    await page.setViewportSize({width, height: 900});
    await openTab(page, 'downloads');
    const card = page.locator('#view-settings [data-executor-tuning="webdav"]');
    await card.locator('.dp-settings-disclosure').click();
    const grid = card.locator('.dp-settings-tuning-grid');
    await expect(grid).toHaveCount(1);
    await expect(grid).toHaveAttribute('data-tuning-lanes', '3');
    // One relationship of exactly three cells: TITLE -> CONTROL -> HELP each.
    const group = grid.locator(':scope > .dp-settings-tuning-group');
    await expect(group).toHaveCount(1);
    await expect(group).toHaveAttribute('data-tuning-span', '3');
    expect(await group.locator(':scope > .dp-settings-field').evaluateAll(nodes => nodes.map(node => [
      node.querySelector(':scope > .form-label').textContent.trim(),
      node.querySelector('[data-setting]').dataset.setting,
      !!node.querySelector(':scope > .form-hint')?.textContent.trim(),
    ]))).toEqual([
      ['Directory Depth', 'webdav_directory_depth', true],
      ['Maximum Files', 'webdav_max_files', true],
      ['Collection Scan Timeout (seconds)', 'webdav_collection_scan_timeout_seconds', true],
    ]);
    await expect(card.locator('[data-setting]')).toHaveCount(3);
    const cell = group.locator(':scope > .dp-settings-field').first();
    const select = cell.locator('select[data-setting="webdav_directory_depth"]');
    await expect(select).toHaveCount(1);
    await expect(card.locator(`label[for="${await select.getAttribute('id')}"]`)).toHaveText('Directory Depth');
    const bounds = await card.locator('input[type="number"]').evaluateAll(nodes => nodes.map(node =>
      [node.dataset.setting, node.min, node.max]));
    expect(bounds).toEqual([['webdav_max_files', '1', '10000'],
                            ['webdav_collection_scan_timeout_seconds', '0', '3600']]);
    const options = await select.locator('option').evaluateAll(nodes => nodes.map(node => [node.value, node.textContent]));
    expect(options).toEqual(DEPTHS);
    const live = await page.request.get('/api/settings').then(r => r.json());
    const stored = live.integrations.general_webdav.options;
    await expect(select).toHaveValue(stored.directory_depth || 'current');
    await expect(card.locator('[data-setting="webdav_max_files"]')).toHaveValue(String(stored.max_files ?? 10000));
    // No limit (the canonical 0, and the default) is the empty field reading
    // "No limit" -- never a number of seconds.
    const scan = card.locator('[data-setting="webdav_collection_scan_timeout_seconds"]');
    await expect(scan).toHaveValue(stored.collection_scan_timeout_seconds ? String(stored.collection_scan_timeout_seconds) : '');
    await expect(scan).toHaveAttribute('placeholder', 'No limit');
    // The cell is the canonical bordered tuning cell its peers use.
    const sameCell = await page.evaluate(() => {
      const own = document.querySelector('[data-executor-tuning="webdav"] .dp-settings-field');
      const other = document.querySelector('[data-executor-tuning="rsync"] .dp-settings-field');
      const a = getComputedStyle(own), b = getComputedStyle(other);
      return a.borderTopStyle === b.borderTopStyle && a.borderTopWidth === b.borderTopWidth
        && a.borderRadius === b.borderRadius && a.backgroundColor === b.backgroundColor;
    });
    expect(sameCell).toBe(true);
    await expect(card.locator('.dp-settings-tuning-footer')).toHaveCount(1);
    // Every offered value is read whole in the closed control: nothing is cut
    // to an ellipsis. The value box is the trigger's flexible filler, so its
    // room is the same whichever value is shown.
    for (const [, label] of DEPTHS) {
      const fit = await card.evaluate((node, text) => {
        const shown = node.querySelector('.dp-dropdown__value');
        const probe = document.createElement('span');
        probe.style.cssText = 'position:absolute;visibility:hidden;white-space:nowrap';
        probe.style.font = getComputedStyle(shown).font;
        probe.textContent = text;
        document.body.appendChild(probe);
        const needed = probe.getBoundingClientRect().width;
        probe.remove();
        return {needed, room: shown.getBoundingClientRect().width};
      }, label);
      expect(fit.room + 0.5, `${label} is cut at ${width}px`).toBeGreaterThanOrEqual(fit.needed);
    }
    const text = await card.innerText();
    for (const term of JARGON) expect(text).not.toContain(term);
    const overflow = await card.evaluate(node => node.scrollWidth - node.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  }
});

test('the WebDAV relationship is outlined only while all three cells fit together', async ({page}) => {
  await isolateExternalFonts(page);
  await page.setViewportSize({width: 1440, height: 900});
  await openTab(page, 'downloads');
  const card = page.locator('#view-settings [data-executor-tuning="webdav"]');
  await card.locator('.dp-settings-disclosure').click();
  const grid = card.locator('.dp-settings-tuning-grid');
  const group = grid.locator('.dp-settings-tuning-group');
  const facts = () => group.evaluate(node => ({
    rows: new Set(Array.from(node.querySelectorAll('.dp-settings-field'),
      cell => Math.round(cell.getBoundingClientRect().top))).size,
    outline: parseFloat(getComputedStyle(node).outlineWidth),
  }));
  expect(await facts()).toEqual({rows: 1, outline: 1});
  // Region widths around the group's own minimum (two 160px cells, one 236px
  // selector cell, two gaps = 588px): whole and outlined at or above it,
  // split and NOT outlined below it -- never an outline across two rows.
  for (const [width, whole] of [[700, true], [588, true], [587, false], [420, false]]) {
    await grid.evaluate((node, value) => { node.style.width = `${value}px`; }, width);
    const measured = await facts();
    expect(measured.rows === 1, `rows at ${width}px`).toBe(whole);
    expect(measured.outline > 0, `outline at ${width}px`).toBe(whole);
  }
  // The selector keeps its whole selector lane even when split.
  const selectorCell = await grid.locator('select[data-setting="webdav_directory_depth"]')
    .evaluate(node => node.closest('.dp-settings-field').getBoundingClientRect().width);
  expect(selectorCell).toBeGreaterThanOrEqual(235.5);
  await grid.evaluate(node => { node.style.width = ''; });
});

test('the WebDAV cells carry no geometry of their own', async ({page}) => {
  await isolateExternalFonts(page);
  await page.setViewportSize({width: 1440, height: 900});
  await openTab(page, 'downloads');
  for (const id of ['direct', 'webdav']) {
    await page.locator(`[data-executor-tuning="${id}"] .dp-settings-disclosure`).click();
  }
  // The shared grammar, read off the rendered style: the WebDAV selector cell
  // and File Allocation's have the same selector lane minimum; its number
  // cells take its set's ordinary minimum, like any other 3-cell set.
  const minimums = await page.evaluate(() => Object.fromEntries(
    ['aria2_file_allocation', 'webdav_directory_depth', 'webdav_max_files', 'webdav_collection_scan_timeout_seconds']
      .map(key => [key, getComputedStyle(document.querySelector(`[data-setting="${key}"]`)
        .closest('.dp-settings-field')).minWidth])));
  expect(minimums).toEqual({aria2_file_allocation: '236px', webdav_directory_depth: '236px',
                            webdav_max_files: '160px', webdav_collection_scan_timeout_seconds: '160px'});
  const groupStyle = await page.evaluate(() => ['direct', 'webdav'].map(id => {
    const style = getComputedStyle(document.querySelector(`[data-executor-tuning="${id}"] .dp-settings-tuning-group`));
    return [style.display, style.flexWrap, style.outlineOffset, style.borderRadius, style.outlineColor];
  }));
  expect(groupStyle[1]).toEqual(groupStyle[0]);
});

test('the open Directory Depth menu shows every choice whole', async ({page}) => {
  await isolateExternalFonts(page);
  await page.setViewportSize({width: 1440, height: 900});
  await openTab(page, 'downloads');
  const card = page.locator('#view-settings [data-executor-tuning="webdav"]');
  await card.locator('.dp-settings-disclosure').click();
  await card.locator('.dp-dropdown__trigger').click();
  const options = page.locator('.dp-dropdown__option');
  await expect(options).toHaveCount(DEPTHS.length);
  const cut = await options.evaluateAll(nodes => nodes.filter(node => node.scrollWidth > node.clientWidth + 1)
    .map(node => node.textContent));
  expect(cut).toEqual([]);
  expect(await options.allTextContents()).toEqual(DEPTHS.map(([, label]) => label));
  await page.keyboard.press('Escape');
});

test('every tuning selector shares the one selector geometry', async ({page}) => {
  await isolateExternalFonts(page);
  await page.setViewportSize({width: 1920, height: 900});
  await openTab(page, 'downloads');
  for (const id of ['direct', 'webdav']) {
    await page.locator(`[data-executor-tuning="${id}"] .dp-settings-disclosure`).click();
  }
  const widths = await page.evaluate(() => ['aria2_file_allocation', 'webdav_directory_depth'].map(key => {
    const field = document.querySelector(`[data-setting="${key}"]`).closest('.dp-settings-field');
    const shell = field.querySelector('.dp-dropdown-shell');
    const content = field.clientWidth - parseFloat(getComputedStyle(field).paddingLeft)
      - parseFloat(getComputedStyle(field).paddingRight);
    return {key, trigger: Math.round(shell.getBoundingClientRect().width), content: Math.round(content)};
  }));
  // Both fill their cell up to the one bound (214px); neither keeps a number field's 112px.
  for (const item of widths) {
    expect(item.trigger, item.key).toBe(Math.min(item.content, 214));
    expect(item.trigger, item.key).toBeGreaterThan(112);
  }
});

test('an inheriting selector reads every value whole at every desktop width', async ({page}) => {
  // File Allocation is the existing consumer of the shared tuning selector:
  // the selector-aware lane minimum must give it room for its own values too.
  await isolateExternalFonts(page);
  for (const width of [1180, 1280, 1440, 1920]) {
    await page.setViewportSize({width, height: 900});
    await openTab(page, 'downloads');
    const card = page.locator('#view-settings [data-executor-tuning="direct"]');
    await card.locator('.dp-settings-disclosure').click();
    const select = card.locator('select[data-setting="aria2_file_allocation"]');
    const labels = await select.locator('option').allTextContents();
    expect(labels.length).toBeGreaterThan(1);
    const cut = await card.evaluate((node, texts) => {
      const shown = node.querySelector('[data-setting="aria2_file_allocation"]')
        .closest('.dp-settings-field').querySelector('.dp-dropdown__value');
      const room = shown.getBoundingClientRect().width;
      const probe = document.createElement('span');
      probe.style.cssText = 'position:absolute;visibility:hidden;white-space:nowrap';
      probe.style.font = getComputedStyle(shown).font;
      document.body.appendChild(probe);
      const over = texts.filter(text => { probe.textContent = text; return probe.getBoundingClientRect().width > room + 0.5; });
      probe.remove();
      return over;
    }, labels);
    expect(cut, `File Allocation values cut at ${width}px`).toEqual([]);
    const overflow = await card.evaluate(node => node.scrollWidth - node.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  }
});

test.describe.serial('Directory Depth persists through the canonical integration owner', () => {
  let original = null;

  test.beforeAll(async ({request}) => {
    original = (await request.get('/api/settings').then(r => r.json())).integrations.general_webdav.options;
  });

  test.afterAll(async ({request}) => {
    if (!original) return;
    await request.patch(`/api/integrations/${PROVIDER}/configuration`,
      {data: {options: {directory_depth: original.directory_depth || 'current',
                        max_files: original.max_files ?? 10000,
                        collection_scan_timeout_seconds: original.collection_scan_timeout_seconds ?? 0}}});
  });

  test('a choice commits through the WebDAV integration scope, never a page-level save', async ({page}) => {
    await isolateExternalFonts(page);
    const writes = [];
    page.on('request', request => {
      if (['PATCH', 'PUT'].includes(request.method()) && request.url().includes('/api/')) {
        writes.push(`${request.method()} ${new URL(request.url()).pathname}`);
      }
    });
    await openTab(page, 'downloads');
    const card = page.locator('#view-settings [data-executor-tuning="webdav"]');
    await card.locator('.dp-settings-disclosure').click();
    const select = card.locator('select[data-setting="webdav_directory_depth"]');
    const wanted = (original.directory_depth || 'current') === '2' ? 'all' : '2';
    const written = page.waitForResponse(response => response.request().method() === 'PATCH'
      && new URL(response.url()).pathname === `/api/integrations/${PROVIDER}/configuration`, {timeout: 15000});
    await select.selectOption(wanted);
    await select.blur();
    const accepted = await (await written).json();
    // Acceptance, not readback: what the server answered for this write.
    expect(JSON.stringify(accepted)).toContain(`"directory_depth":"${wanted}"`);
    await expect(select).toHaveValue(wanted);
    expect(writes.filter(write => write !== `PATCH /api/integrations/${PROVIDER}/configuration`)).toEqual([]);
    await expect(page.locator('#view-settings [data-action="save"]')).toHaveCount(0);
  });

  test('Maximum Files and Collection Scan Timeout commit at changed blur through the same scope', async ({page}) => {
    await isolateExternalFonts(page);
    const writes = [];
    page.on('request', request => {
      if (['PATCH', 'PUT'].includes(request.method()) && request.url().includes('/api/')) {
        writes.push(`${request.method()} ${new URL(request.url()).pathname}`);
      }
    });
    await openTab(page, 'downloads');
    const card = page.locator('#view-settings [data-executor-tuning="webdav"]');
    await card.locator('.dp-settings-disclosure').click();
    for (const [key, option, wanted] of [
      ['webdav_max_files', 'max_files', (original.max_files ?? 10000) === 250 ? 251 : 250],
      ['webdav_collection_scan_timeout_seconds', 'collection_scan_timeout_seconds',
       (original.collection_scan_timeout_seconds ?? 0) === 45 ? 46 : 45],
    ]) {
      const field = card.locator(`[data-setting="${key}"]`);
      const written = page.waitForResponse(response => response.request().method() === 'PATCH'
        && new URL(response.url()).pathname === `/api/integrations/${PROVIDER}/configuration`, {timeout: 15000});
      await field.fill(String(wanted));
      await field.blur();
      const accepted = await (await written).json();
      expect(accepted.options[option], key).toBe(wanted);
      await expect(field).toHaveValue(String(wanted));
    }
    // Emptying the timeout is No limit: it commits the canonical 0 and the
    // field reads "No limit" again, never "0".
    const scan = card.locator('[data-setting="webdav_collection_scan_timeout_seconds"]');
    const cleared = page.waitForResponse(response => response.request().method() === 'PATCH'
      && new URL(response.url()).pathname === `/api/integrations/${PROVIDER}/configuration`, {timeout: 15000});
    await scan.fill('');
    await scan.blur();
    const accepted = await (await cleared).json();
    expect(JSON.parse((await cleared).request().postData())).toEqual(
      {options: {collection_scan_timeout_seconds: 0}});
    expect(accepted.options.collection_scan_timeout_seconds).toBe(0);
    await expect(scan).toHaveValue('');
    await expect(scan).toHaveAttribute('placeholder', 'No limit');
    expect(await card.innerText()).not.toMatch(/\b0 seconds\b/);
    expect(writes.filter(write => write !== `PATCH /api/integrations/${PROVIDER}/configuration`)).toEqual([]);
  });
});

// ── Transfer presentation: the origin badge stays WebDAV ───────────────────

const SUBMITTED = 'webdavs://files.example.org/dav/Album/';

function webdavItem(overrides = {}) {
  return {
    id: 995, name: 'Album', status: 'downloading', presentation_status: 'downloading', progress: 40,
    size_bytes: 2048, source: 'direct_link', request_kinds: ['webdavs'], label: '', hash: '',
    created_at: '2026-09-30T00:00:00Z', provider_provenance_status: 'recorded',
    current_source_identity: {kind: 'host', host: 'files.example.org'},
    origin_provider_id: PROVIDER, origin_provider_name: 'WebDAV',
    current_provider_id: 'general_http', current_provider_name: 'HTTP(S)',
    delivering_provider_id: 'general_http', delivering_provider_name: 'HTTP(S)',
    ...overrides,
  };
}

test('a decomposed WebDAV collection shows its WebDAV origin while HTTP(S) delivery stays visible',
  async ({page}) => {
    await isolateExternalFonts(page);
    const item = webdavItem();
    const detail = webdavItem({
      original_resource: SUBMITTED, files: [], source_outcomes: [], events: [], executors: ['aria2'],
      execution_attempts: [],
      route_attempts: [
        {ordinal: 1, presentation_ordinal: 1, provider_id: PROVIDER, provider_name: 'WebDAV', outcome: 'resolved',
         relation: 'original', route_identity: 'https://files.example.org', route_location: SUBMITTED},
        {ordinal: 2, presentation_ordinal: 2, provider_id: 'general_http', provider_name: 'HTTP(S)',
         outcome: 'active', relation: 'original', route_identity: 'https://files.example.org',
         route_location: 'https://files.example.org/dav/Album/01.flac'},
      ],
    });
    await page.route(url => url.pathname === '/api/torrents',
      route => route.fulfill({status: 200, contentType: 'application/json',
        body: JSON.stringify({items: [item], total: 1})}));
    await page.route(url => url.pathname === `/api/torrents/${item.id}`,
      route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(detail)}));

    await page.goto('/');
    await expect(page.locator(`#dash-tbody tr[data-torrent-id="${item.id}"] .dp-provider-chip`)).toHaveText('WebDAV');
    await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
    await expect(page.locator('#view-torrents')).toHaveClass(/\bactive\b/);
    await expect(page.locator(`#t-tbody tr[data-torrent-id="${item.id}"] .dp-provider-chip`)).toHaveText('WebDAV');
    await page.evaluate(id => showDetail(id), item.id);
    await expect(page.locator('.dp-detail-original-resource .dv')).toHaveText(SUBMITTED);
    // Details still explains the real route: the WebDAV root, then HTTP(S).
    const providers = page.locator('.dp-detail-route-row .dp-detail-route-provider');
    await expect(providers).toHaveText(['WebDAV', 'HTTP(S)']);
    await expect(page.getByText('Origin Provider ID').locator('..')).toContainText(PROVIDER);
    await expect(page.getByText('Current Provider ID').locator('..')).toContainText('general_http');
    await expect(page.getByText('Delivering Provider ID').locator('..')).toContainText('general_http');
  });

test('a completed WebDAV collection keeps its origin; one without an origin keeps the old fallback',
  async ({page}) => {
    await isolateExternalFonts(page);
    const done = webdavItem({id: 996, status: 'completed', presentation_status: 'completed', progress: 100});
    const legacy = webdavItem({id: 997, origin_provider_id: null, origin_provider_name: null});
    await page.route(url => url.pathname === '/api/torrents',
      route => route.fulfill({status: 200, contentType: 'application/json',
        body: JSON.stringify({items: [done, legacy], total: 2})}));
    await page.goto('/');
    await expect(page.locator('#dash-tbody tr[data-torrent-id="996"] .dp-provider-chip')).toHaveText('WebDAV');
    await expect(page.locator('#dash-tbody tr[data-torrent-id="997"] .dp-provider-chip')).toHaveText('HTTP(S)');
  });

test('a question from the server a source moved to names that server, as text', async ({page}) => {
  await isolateExternalFonts(page);
  const item = webdavItem({
    id: 998, status: 'input_required', presentation_status: 'input_required',
    input_required: {id: 'challenge-998', generation: 1, reason: 'auth_required', origin: 'provider',
      methods: [{method: 'username_password', fields: [{name: 'username', required: true},
                                                        {name: 'password', required: true}]}],
      facts: [], authority: 'https://mirror.example:8443'},
  });
  await page.route('**/api/torrents**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/torrents') {
      return route.fulfill({status: 200, contentType: 'application/json',
        body: JSON.stringify({items: [item], total: 1})});
    }
    if (url.pathname === `/api/torrents/${item.id}`) {
      return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(item)});
    }
    return route.continue();
  });
  await page.goto('/');
  const modal = page.locator('[data-dp-input-required-modal]');
  await expect(modal).toBeVisible();
  await expect(modal.locator('[data-dp-auth-authority]')).toHaveText('Sign in to https://mirror.example:8443');
  await expect(modal).toHaveAttribute('aria-describedby', /dp-auth-required-authority/);
  await expect(modal.locator('[data-dp-auth-username]')).toBeVisible();
});
