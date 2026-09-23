const { test, expect } = require('@playwright/test');

// DP 1.0.13: General Sources exposes two equal, header-only provider cards
// (HTTP & HTTPS, FTP & SFTP) through the one shared providerCard() owner, and
// the one INPUT_REQUIRED modal owner presents the neutral server-identity
// challenge. The backend is the authority for every persisted toggle.

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
}
const integrationInput = (page, identity) => page.locator(`[data-integration-enabled="${identity}"]`);
const integrationControl = (page, identity) => page.locator(`label[for="dp-settings-integration-${identity}-enabled"]`);
async function setIntegrationChecked(page, identity, value) {
  const input = integrationInput(page, identity);
  if ((await input.isChecked()) !== value) await integrationControl(page, identity).click();
  await expect(input).toBeChecked({checked:value});
}
async function saveSettings(page) {
  const responsePromise = page.waitForResponse(
    response => response.url().endsWith('/api/settings') && response.request().method() === 'PUT', {timeout:20000});
  await page.locator('#view-settings [data-action="save"]').click();
  expect((await responsePromise).ok()).toBeTruthy();
  await expect(page.locator('#view-settings [data-action="save"]')).toBeEnabled();
}

const CARDS = [
  ['general_http', '.dp-settings-provider-card--general-http', 'HTTP & HTTPS', 'Direct downloads from standard HTTP and HTTPS URLs.'],
  ['general_ftp', '.dp-settings-provider-card--general-ftp', 'FTP & SFTP', 'Direct downloads from FTP and SFTP URLs.'],
];

async function assertHeaderOnlyCards(page) {
  const group = page.locator('.dp-settings-general-sources');
  await expect(group).toContainText('General Sources');
  await expect(group.locator('.dp-settings-provider-card')).toHaveCount(2);
  const order = await group.locator('.dp-settings-provider-card .card-title').allTextContents();
  expect(order.map(text => text.trim())).toEqual(['HTTP & HTTPS', 'FTP & SFTP']);
  const heights = [];
  for (const [identity, selector, title, copy] of CARDS) {
    const card = page.locator(selector);
    await expect(card).toBeVisible();
    await expect(card).toHaveClass(/dp-settings-direct-source-card/);
    await expect(card.locator(':scope > .card-body')).toHaveCount(0);
    await expect(card.locator(':scope > *')).toHaveCount(1);
    await expect(card.locator('.dp-settings-provider-disclosure')).toHaveCount(0);
    const header = card.locator(':scope > .card-header');
    await expect(header.locator('.card-title')).toHaveText(title);
    await expect(header.locator('.dp-settings-provider-header-copy')).toHaveText(copy);
    await expect(integrationControl(page, identity)).toBeVisible();
    await expect(integrationControl(page, identity)).toContainText('Enable');
    await expect(card.locator('input')).toHaveCount(1);
    for (const text of ['private key', 'Private key', 'password', 'Password', 'fingerprint']) await expect(card).not.toContainText(text);
    const headerBox = await header.boundingBox();
    const copyBox = await header.locator('.dp-settings-provider-header-copy').boundingBox();
    const titleBox = await header.locator('.card-title').boundingBox();
    const toggleBox = await integrationControl(page, identity).boundingBox();
    const headerCenter = headerBox.x + headerBox.width / 2;
    const copyCenter = copyBox.x + copyBox.width / 2;
    expect(Math.abs(copyCenter - headerCenter)).toBeLessThanOrEqual(2);
    expect(titleBox.x).toBeLessThan(copyBox.x);
    expect(toggleBox.x).toBeGreaterThan(copyBox.x + copyBox.width - 1);
    const cardBox = await card.boundingBox();
    expect(cardBox.height).toBeLessThanOrEqual(headerBox.height + 4);
    heights.push(Math.round(cardBox.height));
  }
  expect(Math.abs(heights[0] - heights[1])).toBeLessThanOrEqual(1);
}

test('General Sources renders two equal header-only cards with truly centered copy in dark and light themes', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  await page.goto('/'); await openSettings(page);
  await assertHeaderOnlyCards(page);
  await page.locator('.dp-settings-general-sources').screenshot({path:'test-results/checkpoint-direct-sources-dark.png'});
  await page.locator('#theme-toggle').click();
  await expect.poll(() => page.evaluate(() => document.body.classList.contains('light'))).toBeTruthy();
  await assertHeaderOnlyCards(page);
  await page.locator('.dp-settings-general-sources').screenshot({path:'test-results/checkpoint-direct-sources-light.png'});
  await page.setViewportSize({width:680, height:900});
  for (const [identity, selector] of CARDS) {
    const card = page.locator(selector);
    await expect(card.locator(':scope > .card-body')).toHaveCount(0);
    await expect(card.locator('.dp-settings-provider-header-copy')).toBeVisible();
    await expect(integrationControl(page, identity)).toBeVisible();
    const box = await card.boundingBox();
    expect(box.width).toBeLessThanOrEqual(680);
  }
  await page.locator('.dp-settings-general-sources').screenshot({path:'test-results/checkpoint-direct-sources-light-narrow.png'});
  expect(errors).toEqual([]);
});

test('HTTP & HTTPS and FTP & SFTP toggles persist independently and never touch aria2', async ({ page }) => {
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
      await saveSettings(page);
      await expect.poll(read).toEqual([http, ftp, originalAria2]);
      await page.reload(); await openSettings(page);
      await expect(integrationInput(page, 'general_http')).toBeChecked({checked:http});
      await expect(integrationInput(page, 'general_ftp')).toBeChecked({checked:ftp});
    }
  } finally {
    await setIntegrationChecked(page, 'general_http', originalHttp);
    await setIntegrationChecked(page, 'general_ftp', originalFtp);
    await saveSettings(page);
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
