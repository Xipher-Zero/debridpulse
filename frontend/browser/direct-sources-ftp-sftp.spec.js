const { test, expect } = require('@playwright/test');

// DP 1.0.13 final interaction pass: Network Sources exposes one compact
// protocol BOX per real registered member, on a centred bounded grid. The boxes
// are derived from the group the integrations themselves declare and labelled
// from their own presentation metadata, so this spec asserts what is rendered
// for the members that actually exist -- and that nothing is rendered for
// protocols that do not. The one INPUT_REQUIRED modal owner presents the
// neutral server-identity challenge. The backend is the authority for every
// persisted toggle.

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status:200, contentType:'text/css', body:''}));
}
// Runtime exceptions and script console errors only: the browser's own
// "Failed to load resource" status lines report unrelated endpoints (for example
// aria2 still starting) and are not this surface's oracle.
function observeRuntime(page) {
  const errors = [];
  page.on('pageerror', error => errors.push(`pageerror: ${error.message}`));
  page.on('console', message => {
    if (message.type() === 'error' && !message.text().startsWith('Failed to load resource:')) errors.push(`console: ${message.text()}`);
  });
  return errors;
}
async function openSettings(page) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await expect(page.locator('.dp-settings-panel[data-panel="sources"]')).toBeVisible();
  await revealNetworkSources(page);
}

/* Services cards render COLLAPSED: expansion is LOCAL presentation
 * state, never a projection of enabled/configured/verified state. Opening one
 * through the canonical disclosure writes no canonical state, so this spec
 * never depends on another spec's enable/disable timing against the shared
 * backend. The Network Sources members live inside that group's body. */
async function revealNetworkSources(page) {
  const group = page.locator('.dp-settings-general-sources');
  const disclosure = group.locator('.dp-settings-disclosure');
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
  await expect(group.locator('.dp-settings-provider-card--general-http')).toBeVisible();
}

const integrationInput = (page, identity) => page.locator(`[data-integration-enabled="${identity}"]`);
const integrationControl = (page, identity) => page.locator(`label[for="dp-settings-integration-${identity}-enabled"]`);
async function setIntegrationChecked(page, identity, value) {
  const input = integrationInput(page, identity);
  if ((await input.isChecked()) !== value) await integrationControl(page, identity).click();
  await expect(input).toBeChecked({checked:value});
}
/* Participation is IMMEDIATE: each Enable committed itself through its own
 * scoped mutation as it was clicked, and generic Apply no longer exists. This
 * only makes sure the group this spec operates is still open before the next
 * interaction, through the same canonical disclosure. */
async function settleSettings(page) {
  await revealNetworkSources(page);
}

/* The two real registered members, with the labels their own integrations
 * publish and the two lines each box presents. */
const CARDS = [
  ['general_http', '.dp-settings-provider-card--general-http', 'HTTP(S)',
   ['Direct downloads from', 'HTTP and HTTPS URLs.']],
  ['general_ftp', '.dp-settings-provider-card--general-ftp', '(S)FTP',
   ['Direct downloads from', 'FTP and SFTP URLs.']],
];

/* Protocols that do not exist yet. They appear when a real provider is
 * registered through the canonical machinery, and never before. */
const UNREGISTERED = ['WebDAV', 'SCP', 'rsync', 'Multilink'];

const grid = page => page.locator('.dp-settings-general-sources .dp-settings-source-box-grid');

async function assertProtocolBoxes(page) {
  const group = page.locator('.dp-settings-general-sources');
  await expect(group).toContainText('Network Sources');
  await expect(grid(page)).toHaveCount(1);
  await expect(grid(page).locator(':scope > .dp-settings-source-box')).toHaveCount(CARDS.length);
  const order = await grid(page).locator('.dp-settings-source-box .card-title').allTextContents();
  expect(order.map(text => text.trim())).toEqual(CARDS.map(entry => entry[2]));

  // No placeholder, no disabled future card, no fabricated capability.
  for (const absent of UNREGISTERED) await expect(group).not.toContainText(absent);
  await expect(group).not.toContainText(/coming soon/i);

  const heights = [];
  for (const [identity, selector, title, lines] of CARDS) {
    const box = page.locator(selector);
    await expect(box).toBeVisible();
    await expect(box).toHaveClass(/dp-settings-source-box/);
    await expect(box.locator('.dp-settings-disclosure')).toHaveCount(0);
    await expect(box.locator('.card-title')).toHaveText(title);

    // Icon top-left, title at the top, centred on the BOX.
    const mark = box.locator('.dp-settings-source-box-head .dp-settings-protocol-chip');
    await expect(mark).toHaveCount(1);
    const boxRect = await box.boundingBox();
    const markRect = await mark.boundingBox();
    const titleRect = await box.locator('.card-title').boundingBox();
    expect(markRect.x).toBeLessThan(titleRect.x);
    expect(Math.abs((titleRect.x + titleRect.width / 2) - (boxRect.x + boxRect.width / 2)))
      .toBeLessThanOrEqual(2);

    // Exactly two centred descriptive lines, beneath a centred Enable + toggle.
    const copy = box.locator('.dp-settings-source-box-copy > span');
    await expect(copy).toHaveCount(2);
    expect((await copy.allTextContents()).map(text => text.trim())).toEqual(lines);
    const toggleRect = await integrationControl(page, identity).boundingBox();
    const copyRect = await box.locator('.dp-settings-source-box-copy').boundingBox();
    expect(Math.abs((toggleRect.x + toggleRect.width / 2) - (boxRect.x + boxRect.width / 2)))
      .toBeLessThanOrEqual(3);
    expect(Math.abs((copyRect.x + copyRect.width / 2) - (boxRect.x + boxRect.width / 2)))
      .toBeLessThanOrEqual(2);
    expect(toggleRect.y).toBeGreaterThan(titleRect.y);
    expect(copyRect.y).toBeGreaterThanOrEqual(toggleRect.y + toggleRect.height - 1);

    // The toggle is the ONLY action, and the box holds exactly that one control.
    await expect(integrationControl(page, identity)).toBeVisible();
    await expect(integrationControl(page, identity)).toContainText('Enable');
    await expect(box.locator('input')).toHaveCount(1);
    await expect(box.locator('button')).toHaveCount(0);
    for (const text of ['Test', 'Save', 'private key', 'Private key', 'password',
                        'Password', 'fingerprint']) {
      await expect(box).not.toContainText(text);
    }
    // Compact and bounded rather than full-width.
    expect(boxRect.width).toBeLessThanOrEqual(220);
    heights.push(Math.round(boxRect.height));
  }
  expect(Math.abs(heights[0] - heights[1])).toBeLessThanOrEqual(1);
}

/* DP 1.0.13 Settings consolidation: the collection stopped sizing itself from
 * the population and became a CAPACITY grid. The available width alone decides
 * how many equal tracks exist, the real members populate them left to right,
 * and the tracks a sparse population does not reach stay empty so a protocol
 * registered later consumes the next one. `auto-fill` is what preserves that
 * trailing capacity; `auto-fit` would collapse it and recentre the members,
 * which is exactly the population-centred sizing this replaced. */
async function assertCapacityLanes(page, width) {
  const measured = await grid(page).evaluate(el => {
    const host = el.getBoundingClientRect();
    const tracks = getComputedStyle(el).gridTemplateColumns
      .split(' ').filter(Boolean).map(parseFloat);
    const byTop = new Map();
    for (const child of el.children) {
      const r = child.getBoundingClientRect();
      const key = Math.round(r.top);
      if (!byTop.has(key)) byTop.set(key, []);
      byTop.get(key).push(r);
    }
    return {
      capacity: tracks.length,
      lane: tracks[0],
      spread: Math.max(...tracks) - Math.min(...tracks),
      rows: Array.from(byTop.values()).map(boxes => ({
        count: boxes.length,
        leading: Math.min(...boxes.map(b => b.left)) - host.left,
        box: Math.min(...boxes.map(b => b.width)),
      })),
    };
  });
  expect(measured.capacity, `no capacity at ${width}px`).toBeGreaterThanOrEqual(1);
  expect(measured.spread, `the tracks are not equal at ${width}px`).toBeLessThanOrEqual(1);
  expect(measured.rows.length).toBeGreaterThan(0);
  for (const row of measured.rows) {
    // Left-filled: the row occupies the FIRST lane. A bounded box centred
    // inside its own lane is the lane's slack, not a sparse-row offset.
    expect(row.leading,
      `a row of ${row.count} does not start at the first lane at ${width}px`)
      .toBeLessThanOrEqual((measured.lane - row.box) / 2 + 2);
  }
  expect(await page.evaluate(() =>
    document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1),
    `horizontal overflow at ${width}px`).toBeTruthy();
}

test('Network Sources renders one compact protocol box per real provider, left-filling its capacity, in dark and light themes', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  await page.goto('/'); await openSettings(page);
  await assertProtocolBoxes(page);
  await assertCapacityLanes(page, 1440);
  // Two real members at the wide viewport leave trailing capacity for the
  // source providers that do not exist yet, rather than expanding to fill it.
  const wide = await grid(page).evaluate(el => ({
    capacity: getComputedStyle(el).gridTemplateColumns.split(' ').filter(Boolean).length,
    members: el.children.length,
  }));
  expect(wide.capacity).toBeGreaterThan(wide.members);
  await page.locator('.dp-settings-general-sources').screenshot({path:'test-results/checkpoint-direct-sources-dark.png'});
  await page.locator('#theme-toggle').click();
  await expect.poll(() => page.evaluate(() => document.body.classList.contains('light'))).toBeTruthy();
  await assertProtocolBoxes(page);
  await page.locator('.dp-settings-general-sources').screenshot({path:'test-results/checkpoint-direct-sources-light.png'});

  // The boxes stay centred and bounded as the viewport narrows toward one column.
  for (const width of [900, 680, 480, 360]) {
    await page.setViewportSize({width, height:900});
    await expect(grid(page)).toBeVisible();
    for (const [identity, selector] of CARDS) {
      const box = page.locator(selector);
      await expect(box.locator('.dp-settings-source-box-copy > span')).toHaveCount(2);
      await expect(integrationControl(page, identity)).toBeVisible();
      expect((await box.boundingBox()).width).toBeLessThanOrEqual(Math.min(220, width));
    }
    await assertCapacityLanes(page, width);
  }
  await page.locator('.dp-settings-general-sources').screenshot({path:'test-results/checkpoint-direct-sources-light-narrow.png'});
  expect(errors).toEqual([]);
});

/* The boxes are DERIVED: whoever declares the group is a member, labelled from
 * their own presentation metadata. What is rendered is exactly what the backend
 * currently registers -- no more, and nothing invented. */
test('only the really registered Network Source providers render, with their own labels',
  async ({ page }) => {
    await isolateExternalFonts(page);
    await page.goto('/'); await openSettings(page);
    const canonical = await page.request.get('/api/settings').then(r => r.json());
    const groupId = canonical.integrations.general_http.presentation.status_group;
    const expected = Object.entries(canonical.integrations)
      .filter(([, entry]) => entry?.presentation?.status_group === groupId)
      .sort(([leftId, left], [rightId, right]) =>
        (left.presentation.display_order - right.presentation.display_order)
        || leftId.localeCompare(rightId));
    expect(expected.length).toBeGreaterThan(0);

    const rendered = await grid(page).locator(':scope > .dp-settings-source-box').evaluateAll(
      boxes => boxes.map(box => ({
        identity: box.querySelector('[data-integration-enabled]').dataset.integrationEnabled,
        label: box.querySelector('.card-title').textContent.trim(),
      })));
    expect(rendered).toEqual(expected.map(([id, entry]) =>
      ({identity: id, label: entry.presentation.status_name})));
  });

test('HTTP(S) and (S)FTP toggles persist independently and never touch aria2', async ({ page }) => {
  await isolateExternalFonts(page); await page.goto('/');
  const original = await page.request.get('/api/settings').then(response => response.json());
  const originalHttp = original.integrations.general_http.enabled;
  const originalFtp = original.integrations.general_ftp.enabled;
  const originalAria2 = original.integrations.aria2.enabled;
  const read = async () => {
    const s = await page.request.get('/api/settings').then(r => r.json());
    return [s.integrations.general_http.enabled, s.integrations.general_ftp.enabled, s.integrations.aria2.enabled];
  };
  await openSettings(page);
  try {
    for (const [http, ftp] of [[true, false], [false, true], [true, true]]) {
      await setIntegrationChecked(page, 'general_http', http);
      await setIntegrationChecked(page, 'general_ftp', ftp);
      await settleSettings(page);
      await expect.poll(read).toEqual([http, ftp, originalAria2]);
      await page.reload(); await openSettings(page);
      await expect(integrationInput(page, 'general_http')).toBeChecked({checked:http});
      await expect(integrationInput(page, 'general_ftp')).toBeChecked({checked:ftp});
    }
  } finally {
    await setIntegrationChecked(page, 'general_http', originalHttp);
    await setIntegrationChecked(page, 'general_ftp', originalFtp);
    await settleSettings(page);
    await expect.poll(read).toEqual([originalHttp, originalFtp, originalAria2]);
  }
});

const PASSWORD_METHOD = {method:'username_password', fields:[{name:'username', required:true}, {name:'password', required:true}]};
const FINGERPRINT = '208c2653f8ed2c0d7b62d69b304e8016e4151f60';

function challengeItem(id, reason, facts = [], origin = 'executor') {
  return {
    id, name:`Challenge fixture ${id}`, status:'input_required', progress:0, size_bytes:0, source:'direct_link',
    hash:'', label:'', created_at:'2026-09-21T00:00:00Z', error:null, error_message:null,
    input_required:{id:`challenge-${id}`, generation:1, reason, origin, methods:[PASSWORD_METHOD], facts},
  };
}

async function installChallenges(page, initial) {
  const items = new Map(initial.map(item => [item.id, structuredClone(item)]));
  const submissions = [];
  await page.route('**/api/torrents**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    if (url.pathname === '/api/torrents' && method === 'GET') {
      const status = url.searchParams.get('status');
      const all = [...items.values()].filter(item => !status || item.status === status);
      return route.fulfill({status:200, contentType:'application/json', body:JSON.stringify({items:all, total:all.length})});
    }
    const detail = url.pathname.match(/^\/api\/torrents\/(\d+)$/);
    if (detail && method === 'GET') {
      const item = items.get(Number(detail[1]));
      return route.fulfill({status:item ? 200 : 404, contentType:'application/json', body:JSON.stringify(item || {})});
    }
    const input = url.pathname.match(/^\/api\/torrents\/(\d+)\/input$/);
    if (input && method === 'POST') {
      const id = Number(input[1]);
      submissions.push({id, body:request.postDataJSON()});
      const item = items.get(id);
      items.set(id, {...item, status:'downloading', input_required:null});
      return route.fulfill({status:200, contentType:'application/json', body:JSON.stringify({ok:true, id})});
    }
    return route.continue();
  });
  return {submissions};
}

test('SERVER_IDENTITY_REQUIRED uses the one modal owner with host, algorithm, fingerprint and credentials', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  const facts = [
    {name:'server_host', value:'files.example.org'},
    {name:'server_identity_algorithm', value:'sha-1'},
    {name:'server_identity_fingerprint', value:FINGERPRINT},
  ];
  const fixture = await installChallenges(page, [challengeItem(951, 'server_identity_required', facts)]);
  await page.goto('/');
  const modal = page.locator('[data-dp-input-required-modal]');
  await expect(modal).toBeVisible();
  await expect(page.locator('[data-dp-input-required-modal]')).toHaveCount(1);
  await expect(modal.locator('#dp-auth-required-title')).toHaveText('Verify Server Identity');
  await expect(modal.locator('[data-dp-identity-host]')).toHaveText('files.example.org');
  await expect(modal.locator('[data-dp-identity-algorithm]')).toHaveText('SHA-1 fingerprint');
  const fingerprint = modal.locator('[data-dp-identity-fingerprint]');
  await expect(fingerprint).toHaveText(FINGERPRINT);
  expect(await fingerprint.evaluate(node => node.tagName)).toBe('CODE');
  expect(await fingerprint.evaluate(node => getComputedStyle(node).userSelect)).not.toBe('none');
  await expect(modal).toContainText('Compare this fingerprint with the server');
  await expect(modal.locator('[data-dp-auth-username]')).toBeVisible();
  await expect(modal.locator('[data-dp-auth-secret-label]')).toHaveText('Password');
  await expect(modal.locator('[data-dp-auth-key]')).toHaveCount(0);
  await expect(modal.locator('[data-dp-auth-cancel]')).toHaveText('Cancel');
  await expect(modal.locator('[data-dp-auth-continue]')).toHaveText('Verify & Continue');
  for (const hidden of ['0000000000000000000000000000000000000000', 'general_ftp', 'aria2', 'errorCode', 'Unexpected SSH', 'verified']) {
    await expect(modal).not.toContainText(hidden);
  }
  await expect(modal.locator('[data-dp-auth-username]')).toBeFocused();
  await modal.locator('[data-dp-auth-username]').fill('operator');
  await modal.locator('[data-dp-auth-secret]').fill('secret-value');
  await modal.locator('[data-dp-auth-continue]').click();
  await expect(modal).toHaveCount(0);
  expect(fixture.submissions).toEqual([{id:951, body:{
    challenge_id:'challenge-951', method:'username_password', username:'operator', password:'secret-value',
  }}]);
  expect(errors).toEqual([]);
});

test('ordinary AUTH_REQUIRED presentation is unchanged', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  await installChallenges(page, [challengeItem(952, 'auth_required')]);
  await page.goto('/');
  const modal = page.locator('[data-dp-input-required-modal]');
  await expect(modal).toBeVisible();
  await expect(modal.locator('#dp-auth-required-title')).toHaveText('Authentication Required');
  await expect(modal.locator('[data-dp-identity-fingerprint]')).toHaveCount(0);
  await expect(modal.locator('[data-dp-auth-continue]')).toHaveText('Continue');
  await expect(modal.locator('[data-dp-auth-secret-label]')).toHaveText('Password');
  expect(errors).toEqual([]);
});

test('evidence-origin challenges use the same single modal and submission contract', async ({ page }) => {
  // 1.0.13 pre-writer evidence acquisition raises the SAME neutral challenges
  // with origin "evidence"; the one modal owner never branches on origin.
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  const facts = [
    {name:'server_host', value:'mirror.example.org'},
    {name:'server_identity_algorithm', value:'sha-1'},
    {name:'server_identity_fingerprint', value:FINGERPRINT},
  ];
  const fixture = await installChallenges(page, [challengeItem(954, 'server_identity_required', facts, 'evidence')]);
  await page.goto('/');
  const modal = page.locator('[data-dp-input-required-modal]');
  await expect(modal).toBeVisible();
  await expect(page.locator('[data-dp-input-required-modal]')).toHaveCount(1);
  await expect(modal.locator('#dp-auth-required-title')).toHaveText('Verify Server Identity');
  await expect(modal.locator('[data-dp-identity-fingerprint]')).toHaveText(FINGERPRINT);
  await expect(modal).not.toContainText('evidence');
  await modal.locator('[data-dp-auth-username]').fill('operator');
  await modal.locator('[data-dp-auth-secret]').fill('secret-value');
  await modal.locator('[data-dp-auth-continue]').click();
  await expect(modal).toHaveCount(0);
  expect(fixture.submissions).toEqual([{id:954, body:{
    challenge_id:'challenge-954', method:'username_password', username:'operator', password:'secret-value',
  }}]);
  expect(errors).toEqual([]);
});

test('an unknown challenge reason is not presented by the modal', async ({ page }) => {
  await isolateExternalFonts(page);
  await installChallenges(page, [challengeItem(953, 'future_reason')]);
  await page.goto('/');
  await page.waitForTimeout(500);
  await expect(page.locator('[data-dp-input-required-modal]')).toHaveCount(0);
});
