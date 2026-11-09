// @ts-check
const {test, expect} = require('@playwright/test');

/* DebridPulse 1.0.14 localization, WS3b: the download operations --
 * Downloads, Details, file selection, provider and source switching and the
 * folder picker -- say every word through the catalog, and an open dialog,
 * picker or chooser is re-worded in place on a live language change, keeping
 * focus and asking the server nothing again.
 *
 * Shown in the test-only pseudo locale en-XA (every message accented and
 * bracketed ⟦ … ⟧). Synthetic accounts and synthetic answers only
 * (page.route): no provider is invoked and nothing is switched. */

// Proper names, and what the fixtures wrote (transfer, file, engine and folder names).
const NAMES = ['DebridPulse', 'AllDebrid', 'Debrid-Link', 'Real-Debrid', 'TorBox', 'aria2', 'synthetic-30.bin', 'one.bin',
  'collection 41', 'Song 1.webm', 'Song 2.webm', '/download/Alpha', '/download/broken', '/download/odd', '/download', 'Alpha',
  'broken', 'odd', 'host.example', 'synthetic-user'];

async function boot(page) {
  await page.setViewportSize({width: 1440, height: 900});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeAttached();
  await page.evaluate(() => window.DPI18n.ready);
}

const setLocale = (page, locale) => page.evaluate(locale => window.DPI18n.setLocale(locale), locale);
// Long enough for any request a language change would wrongly make.
const settle = page => page.evaluate(() => new Promise(resolve => setTimeout(resolve, 400)));

/* Visible words still in English inside `selector` (text and human
 * attributes), leaving out proper names, figures, dates and what the server or
 * the person wrote (transfer, file and folder names). */
const englishLeft = (page, selector) => page.evaluate(([selector, names]) => {
  const visible = element => element.getClientRects().length > 0 && getComputedStyle(element).visibility !== 'hidden';
  const userData = element => element.closest('.t-name, .dp-downloads-date-value, .dp-root-provider-name, .dp-provider-chip-label, ' +
    '.dp-settings-directory-name, [data-dp-fs-subtitle], .dp-fs-name, .dp-modal-code');
  const english = text => !text.includes('⟦') &&
    /[A-Za-z]{3,}/.test(names.reduce((left, name) => left.split(name).join(' '), text));
  const found = [];
  for (const root of document.querySelectorAll(selector)) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const element = node.parentElement;
      const text = node.textContent.trim();
      if (!text || !element || !visible(element) || userData(element) || element.closest('script, style, svg, template')) continue;
      if (english(text)) found.push(`text: ${text}`);
    }
    for (const element of [root, ...root.querySelectorAll('*')]) {
      if (!visible(element) || userData(element)) continue;
      for (const attribute of ['aria-label', 'data-tooltip', 'placeholder', 'title']) {
        const value = element.getAttribute(attribute);
        if (value && english(value)) found.push(`[${attribute}]: ${value}`);
      }
    }
  }
  return [...new Set(found)];
}, [selector, NAMES]);

function row(id) {
  return {
    id, name: `synthetic-${id}.bin`, display_name: `synthetic-${id}.bin`, hash: `magnet:${id}`, status: 'downloading',
    presentation_status: 'downloading', presentation_badge_status: 'downloading', progress: 40, size_bytes: 4096,
    created_at: '2026-11-09T10:00:00Z', current_source_identity: {kind: 'magnet', host: ''}, request_kinds: ['magnet'],
    source: 'manual', current_provider_id: 'alldebrid', current_provider_name: 'AllDebrid',
    route_provider_id: 'alldebrid', route_provider_name: 'AllDebrid', route_switch_available: true,
    common_candidate_count: 0, group_remaining_count: 0, candidate_action_scope: 'none',
  };
}

function detail(id) {
  return {
    id, name: `synthetic-${id}.bin`, status: 'downloading', progress: 40, size_bytes: 4096, source: 'manual', label: '',
    hash: '', created_at: '2026-11-09T10:00:00Z', current_source_identity: {kind: 'magnet', host: ''}, request_kinds: ['magnet'],
    route_provider_id: 'alldebrid', route_provider_name: 'AllDebrid', route_attempts: [], execution_attempts: [],
    executors: ['aria2'], source_outcomes: [], events: [],
    files: [{id: 1, filename: 'one.bin', size_bytes: 4, status: 'downloading', blocked: false}],
  };
}

const ROUTE = {
  transfer_id: 30, current_provider_id: 'alldebrid', current_provider_name: 'AllDebrid', switchable: true,
  providers: [
    {provider_id: 'alldebrid', provider_name: 'AllDebrid', status: 'current', selectable: false, reason: null},
    {provider_id: 'realdebrid', provider_name: 'Real-Debrid', status: 'prepared', selectable: true, reason: null},
    {provider_id: 'debridlink', provider_name: 'Debrid-Link', status: 'retrying', selectable: true, reason: null,
      retry_in_seconds: 125},
    {provider_id: 'torbox', provider_name: 'TorBox', status: 'unavailable', selectable: false, reason: 'disabled'},
  ],
};

// Synthetic transfer 30: the list, its Details and its route status, each read counted.
async function transferFixture(page) {
  const reads = {list: 0, detail: 0, route: 0, posts: 0};
  await page.route('**/api/torrents?*', route => { reads.list += 1; return route.fulfill({status: 200,
    contentType: 'application/json', body: JSON.stringify({items: [row(30)], total: 1})}); });
  await page.route(url => url.pathname === '/api/torrents/30', route => { reads.detail += 1; return route.fulfill({status: 200,
    contentType: 'application/json', body: JSON.stringify(detail(30))}); });
  await page.route(url => /\/api\/torrents\/30\/route$/.test(url.pathname), route => {
    if (route.request().method() !== 'GET') { reads.posts += 1; return route.fulfill({status: 500, body: '{}'}); }
    reads.route += 1;
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(ROUTE)});
  });
  return reads;
}

test('L10-WS3b: in en-XA the Downloads page, its row and its provider picker are the catalog\'s; an open picker keeps focus and is not re-read', async ({page}) => {
  const reads = await transferFixture(page);
  await boot(page);
  await page.evaluate(() => window.nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]')));
  const launcher = page.locator('#view-torrents tr[data-torrent-id="30"] .dp-root-provider-launcher');
  await expect(launcher).toBeVisible();
  expect(await setLocale(page, 'en-XA')).toBe('en-XA');
  await expect(page.locator('#view-torrents tr[data-torrent-id="30"] button[onclick*="pauseT(30"]')).toHaveAttribute('data-tooltip', /⟦/);
  expect(await englishLeft(page, '#view-torrents')).toEqual([]);
  await expect(launcher).toHaveAttribute('aria-label', /⟦/);

  await launcher.click();
  const menu = page.locator('.dp-root-provider-menu');
  await expect(menu).toBeVisible();
  expect(await englishLeft(page, '.dp-root-provider-menu')).toEqual([]);
  await expect(menu.locator('[data-dp-provider-status="retrying"] .dp-root-provider-reason')).toContainText('⟦');
  const switchTo = menu.locator('.dp-root-provider-switch[data-dp-provider-id="debridlink"]');
  await switchTo.focus();
  const before = reads.route;
  expect(await setLocale(page, 'en')).toBe('en');
  await settle(page);
  await expect(menu).toBeVisible();
  await expect(switchTo).toBeFocused();
  await expect(switchTo).toHaveText('Switch');
  await expect(menu.locator('.dp-root-provider-title')).toHaveText('Provider');
  await expect(menu.locator('[data-dp-provider-status="retrying"]')).toContainText('Retrying… — next attempt in 2 min 5 s');
  expect(reads.route).toBe(before);                                                // re-said, never re-read
  expect(reads.posts).toBe(0);
});

test('L12-WS3b: open Details is re-said in place, keeping focus, without asking the server again', async ({page}) => {
  const reads = await transferFixture(page);
  await boot(page);
  await page.evaluate(() => window.showDetail(30));
  await expect(page.locator('#modal-title')).toHaveText('synthetic-30.bin');
  const mount = page.locator('#modal-body [data-dp-root-provider-mount] .dp-root-provider-launcher');
  await expect(mount).toBeVisible();                                               // the route read made it a launcher
  await mount.focus();
  const seen = {...reads};
  expect(await setLocale(page, 'en-XA')).toBe('en-XA');
  await settle(page);
  await expect(page.locator('#modal-body')).toContainText('⟦');
  expect(await englishLeft(page, '#modal')).toEqual([]);
  await expect(mount).toBeFocused();                                               // the re-rendered launcher, same place
  await expect(mount).toHaveAttribute('aria-label', /⟦/);
  expect([reads.detail, reads.route]).toEqual([seen.detail, seen.route]);
  expect(await setLocale(page, 'en')).toBe('en');
  await expect(page.locator('#modal-body')).not.toContainText('⟦');
});

test('L12-WS3b: the discard confirmation is re-worded while open, and Keep switches nothing', async ({page}) => {
  const posts = [];
  await page.route(url => url.pathname === '/api/torrents/30/artifacts/1/candidate', route => {
    posts.push(route.request().postDataJSON());
    return route.fulfill({status: 409, contentType: 'application/json', body: JSON.stringify({detail: {
      confirmation: 'discard_material', discarded_bytes: 1048576, retained_bytes: 2097152, abandoned_bytes: 0,
      material_generation: 1, message: 'synthetic refusal'}})});
  });
  await boot(page);
  await page.evaluate(() => {
    window.__switch = window.DPGroupCandidates.switchArtifact(30, 1, 'c2', 'one.bin', 'host.example');
  });
  const dialog = page.locator('.dp-modal-overlay .dp-modal-dialog');
  await expect(dialog).toContainText('Discard downloaded progress?');
  await expect(dialog).toContainText('of verified material is kept.');
  const keep = dialog.locator('[data-modal-cancel]');
  await keep.focus();
  expect(await setLocale(page, 'en-XA')).toBe('en-XA');
  await expect(dialog).not.toContainText('Discard downloaded progress?');
  expect(await englishLeft(page, '.dp-modal-overlay .dp-modal-dialog')).toEqual([]);
  await expect(keep).toBeFocused();
  await expect(keep).toContainText('⟦');
  await keep.click();
  expect(await page.evaluate(() => window.__switch)).toBeNull();
  expect(posts.length).toBe(1);                                                    // the refusal only; nothing confirmed
  expect(await setLocale(page, 'en')).toBe('en');
});

test('L12-WS3b: an open file chooser and an open folder picker are re-worded in place, keeping focus', async ({page}) => {
  const members = [1, 2].map(n => ({entry_id: `m-${n}`, name: `Song ${n}.webm`, relative_path: `Song ${n}.webm`, size_bytes: 0}));
  await page.route(url => url.pathname === '/api/torrents/41/file-selection', route => route.fulfill({status: 200,
    contentType: 'application/json', body: JSON.stringify({eligible: true, mutable: true, manifest_id: 'manifest-41',
      decision: 'pending', file_count: 2, entries: members, selected_entry_ids: [], auto_offer: true, decision_deadline: null,
      server_now: 1, explicit_only: true, file_selection_affordance: 'choose'})}));
  await page.route(url => url.pathname === '/api/torrents/41', route => route.fulfill({status: 200,
    contentType: 'application/json', body: JSON.stringify({id: 41, name: 'collection 41'})}));
  let folderReads = 0;
  await page.route('**/api/settings/directories*', route => { folderReads += 1; return route.fulfill({status: 200,
    contentType: 'application/json', body: JSON.stringify({
      current: {name: 'download', path: '/download', accessible: true, writable: true, selectable: true, reason: 'none',
        capacity: {total_bytes: 2 * 1024 ** 3, free_bytes: 1024 ** 3}},
      parent: '/', children: [{name: 'Alpha', path: '/download/Alpha', accessible: true, writable: null, selectable: null,
        reason: 'not_validated'}]})}); });
  await boot(page);

  await page.evaluate(() => document.dispatchEvent(new CustomEvent('debridpulse:file-selection-available', {detail: {transfer_id: 41}})));
  await expect(page.locator('#modal[data-dp-modal-mode="file-selection"]')).toBeVisible();
  const box = page.locator('#modal-body input[type="checkbox"]').first();
  await box.focus();
  expect(await setLocale(page, 'en-XA')).toBe('en-XA');
  await expect(page.locator('#modal-footer .dp-fs-confirm')).toContainText('⟦');
  expect(await englishLeft(page, '#modal')).toEqual([]);
  await expect(box).toBeFocused();
  expect(await setLocale(page, 'en')).toBe('en');
  await page.locator('#modal .modal-close').click();
  await expect(page.locator('#modal[data-dp-modal-mode="file-selection"]')).toHaveCount(0);

  await page.evaluate(() => window.nav(document.querySelector('#sidebar .nav-item[data-view="settings"]')));
  const tab = page.locator('.dp-settings-tabs .stab[data-tab="downloads"]');
  await tab.click();
  await page.locator('#dp-settings-field-download-folder').fill('/download');
  await page.locator('button[data-action="browse-download-folder"]').click();
  const picker = page.locator('.dp-settings-directory-dialog');
  const alpha = picker.locator('.dp-settings-directory-row[data-path="/download/Alpha"]');
  await expect(alpha).toBeVisible();
  await alpha.focus();
  const seen = folderReads;
  expect(await setLocale(page, 'en-XA')).toBe('en-XA');
  await settle(page);
  expect(await englishLeft(page, '.dp-settings-directory-dialog')).toEqual([]);
  await expect(alpha).toBeFocused();
  expect(folderReads).toBe(seen);
  expect(await setLocale(page, 'en')).toBe('en');
  await expect(picker).not.toContainText('⟦');
});

test('L12-WS3b: a folder-picker browse error stays shown and is re-worded across language changes, without a read or a write', async ({page}) => {
  const listing = {
    current: {name: 'download', path: '/download', accessible: true, writable: true, selectable: true, reason: 'none',
      capacity: {total_bytes: 2 * 1024 ** 3, free_bytes: 1024 ** 3}},
    parent: '/',
    children: ['Alpha', 'broken', 'odd'].map(name => ({name, path: `/download/${name}`, accessible: true, writable: null,
      selectable: null, reason: 'not_validated'})),
  };
  let folderReads = 0;
  await page.route('**/api/settings/directories*', route => {
    folderReads += 1;
    const path = new URL(route.request().url()).searchParams.get('path');
    if (path === '/download/broken') {                                           // a coded refusal
      return route.fulfill({status: 404, contentType: 'application/json',
        body: JSON.stringify({detail: {code: 'path_unavailable', message: 'Directory path does not exist'}})});
    }
    if (path === '/download/odd') {                                              // a code the catalog does not know
      return route.fulfill({status: 409, contentType: 'application/json',
        body: JSON.stringify({detail: {code: 'synthetic_unknown', message: 'Synthetic server words'}})});
    }
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(listing)});
  });
  const writes = [];
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    // The sidebar's aria2 health probe (POST, changes nothing) retries on its own clock from boot.
    if (path.startsWith('/api/') && request.method() !== 'GET' && path !== '/api/settings/test-aria2') writes.push(`${request.method()} ${path}`);
  });
  await boot(page);
  await page.evaluate(() => window.nav(document.querySelector('#sidebar .nav-item[data-view="settings"]')));
  await page.locator('.dp-settings-tabs .stab[data-tab="downloads"]').click();
  // No field edit (that would save Settings): the synthetic listing answers any path.
  await page.locator('button[data-action="browse-download-folder"]').click();
  const picker = page.locator('.dp-settings-directory-dialog');
  const error = picker.locator('[data-directory-error]');
  const accept = page.locator('.dp-modal-overlay [data-modal-accept]');
  const row = path => picker.locator(`.dp-settings-directory-row[data-path="${path}"]`);
  await expect(row('/download/Alpha')).toBeVisible();
  await row('/download/broken').click();
  await expect(error).toBeVisible();
  await expect(error).toHaveText('The requested directory is currently unavailable.');
  // What the error leaves shown: the listing it came from, still selectable.
  await expect(picker).toContainText('/download');
  await expect(accept).toBeEnabled();
  await row('/download/Alpha').focus();
  await settle(page);
  const reads = folderReads;
  writes.length = 0;                                                              // only what a language change would cause

  expect(await setLocale(page, 'en-XA')).toBe('en-XA');
  await settle(page);
  await expect(error).toBeVisible();
  await expect(error).toContainText('⟦');
  expect(await englishLeft(page, '.dp-settings-directory-dialog')).toEqual([]);
  await expect(row('/download/Alpha')).toBeFocused();
  await expect(accept).toBeEnabled();
  expect(await setLocale(page, 'en')).toBe('en');
  await settle(page);
  await expect(error).toBeVisible();
  await expect(error).toHaveText('The requested directory is currently unavailable.');
  await expect(row('/download/Alpha')).toBeFocused();
  await expect(row('/download/broken')).toBeVisible();
  expect(folderReads).toBe(reads);                                                // no read for the words alone
  expect(writes).toEqual([]);                                                     // and no write

  // A refusal the catalog has no words for keeps the server's own text.
  await row('/download/odd').click();
  await expect(error).toHaveText('Synthetic server words');
  const again = folderReads;
  expect(await setLocale(page, 'en-XA')).toBe('en-XA');
  await settle(page);
  await expect(error).toBeVisible();
  await expect(error).toHaveText('Synthetic server words');
  expect(await setLocale(page, 'en')).toBe('en');
  await expect(error).toHaveText('Synthetic server words');
  expect(folderReads).toBe(again);
  expect(writes).toEqual([]);
});

test('L12-WS3b: an open authentication challenge is re-worded while typed credentials, focus and the challenge stay', async ({page}) => {
  const item = {
    id: 51, name: 'synthetic-30.bin', status: 'input_required', progress: 0, size_bytes: 0, source: 'fixture', hash: '', label: '',
    created_at: '2026-11-09T10:00:00Z', error: null, error_message: null,
    input_required: {id: 'challenge-51', generation: 1, reason: 'auth_required', origin: 'provider', authority: 'host.example',
      methods: [{method: 'username_password', fields: [{name: 'username', required: true}, {name: 'password', required: true}]}]},
  };
  const posts = [];
  await page.route('**/api/torrents**', route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() !== 'GET') { posts.push(path); return route.fulfill({status: 500, body: '{}'}); }
    if (path === '/api/torrents') return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items: [item], total: 1})});
    if (path === '/api/torrents/51') return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(item)});
    return route.continue();
  });
  await boot(page);
  const modal = page.locator('[data-dp-input-required-modal]');
  await expect(modal).toBeVisible();
  await expect(modal.locator('#dp-auth-required-title')).toHaveText('Authentication Required');
  const username = modal.locator('[data-dp-auth-username]');
  const secret = modal.locator('[data-dp-auth-secret]');
  await username.fill('synthetic-user');
  await secret.fill('synthetic-secret');
  await secret.focus();

  expect(await setLocale(page, 'en-XA')).toBe('en-XA');
  await expect(modal.locator('#dp-auth-required-title')).toContainText('⟦');
  await expect(modal.locator('[data-dp-auth-secret-label]')).toContainText('⟦');
  await expect(modal.locator('[data-dp-auth-continue]')).toContainText('⟦');
  await expect(modal.locator('[data-dp-auth-authority]')).toContainText('⟦');
  expect(await englishLeft(page, '[data-dp-input-required-modal]')).toEqual([]);
  await expect(modal).toBeVisible();                                              // the same challenge, still open
  await expect(username).toHaveValue('synthetic-user');
  await expect(secret).toHaveValue('synthetic-secret');
  await expect(secret).toBeFocused();
  expect(await setLocale(page, 'en')).toBe('en');
  await expect(modal.locator('#dp-auth-required-title')).toHaveText('Authentication Required');
  await expect(modal.locator('[data-dp-auth-authority]')).toHaveText('Sign in to host.example');
  await expect(username).toHaveValue('synthetic-user');
  await expect(secret).toHaveValue('synthetic-secret');
  await expect(secret).toBeFocused();
  expect(posts).toEqual([]);                                                      // nothing submitted or cancelled
});
