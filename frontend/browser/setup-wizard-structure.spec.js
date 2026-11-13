const { test, expect } = require('@playwright/test');

/* DP 1.0.14 setup wizard: page structure and nested views.
 *
 * The six pages' structure inside the one wizard shell: Welcome's product
 * logo, explanation and Change Language row, whose list is a view of the same
 * dialog (no second dialog); Download Directory's Browse as the Settings
 * folder browser shown as a view of the same dialog; Deployment's centred
 * cards; Administrator's explanation and centred notes, only where an
 * account is being created; Premium Services' Optional badge and its
 * services wearing their own artwork; Getting Started's steps and its
 * sign-in note, only where Finish really signs the Administrator in again.
 * A view chooses a draft only: the page's own Next is still the one commit,
 * and the preview commits nothing.
 *
 * Real open runtime; the setup record and deployment type are emulated in
 * this browser where a new installation is needed (labelled mocks of the
 * server's facts: backend/tests/test_setup_lifecycle.py proves its own rules),
 * and a not-selectable folder is one synthetic listing. Every change a case
 * makes is put back. Locators are roles, data hooks and catalog keys. */

async function load(page, {width = 1280, height = 900} = {}) {
  await page.setViewportSize({width, height});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeAttached();
  await page.evaluate(() => window.DPI18n.ready);
}

const wizard = page => page.locator('.dp-modal-dialog.dp-setup-dialog');
const t = (page, key, values) => page.evaluate(([key, values]) => window.DPI18n.t(key, values), [key, values]);
const setLocale = (page, locale) => page.evaluate(locale => window.DPI18n.setLocale(locale), locale);
const call = (page, method, path, body) => page.evaluate(([method, path, body]) => window.debridPulseAuth.fetch(path, {
  method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined,
}).then(async response => ({status: response.status, body: await response.json().catch(() => null)})), [method, path, body]);
const accept = page => wizard(page).locator('[data-modal-accept]').click();
const title = async (page, key) => expect(wizard(page).locator('.dp-modal-title')).toHaveText(await t(page, key));

/* Every write the page sends, except the page's own aria2 reachability probe. */
function writes(page) {
  const sent = [];
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (request.method() !== 'GET' && path !== '/api/settings/test-aria2') sent.push(`${request.method()} ${path}`);
  });
  return sent;
}

/* An Admin's rerun, from Welcome. */
async function rerun(page) {
  await call(page, 'DELETE', '/api/setup/rerun');
  await page.evaluate(() => window.DPSetupWizard.rerun());
  await expect(wizard(page)).toBeVisible();
  await expect(wizard(page).locator('.dp-setup-welcome')).toBeVisible();
}

/* The development preview (Test First-Run Wizard), from a rerun's Welcome. */
async function preview(page) {
  await rerun(page);
  await wizard(page).locator('[data-setup-preview]').click();
  await expect(wizard(page).locator('.dp-setup-preview-note')).toBeVisible();
}

/* A preview walked to a page of the chosen deployment's roster. */
async function previewAt(page, pageClass, {type = 'server'} = {}) {
  await preview(page);
  for (let i = 0; i < 6 && !(await wizard(page).locator(pageClass).count()); i += 1) {
    if (await wizard(page).locator('.dp-setup-deployment').count()) await wizard(page).locator(`[data-deployment="${type}"]`).click();
    await accept(page);
  }
  await expect(wizard(page).locator(pageClass)).toBeVisible();
}

/* Close every wizard: a preview's Close returns to the rerun it came from, which closes too. */
async function end(page) {
  for (let i = 0; i < 3 && await wizard(page).count(); i += 1) {
    await page.evaluate(() => document.querySelector('.dp-setup-dialog [data-modal-cancel]')?.click());
    await page.waitForTimeout(400);
  }
  await expect(wizard(page)).toHaveCount(0);
  await call(page, 'DELETE', '/api/setup/rerun');
}

/* A new installation (emulated): the setup record and the deployment type
 * this browser is shown, for a page only a new installation reaches. */
async function newInstallation(page, {deployment, phase, administrator = 'none'}) {
  const state = {initial: {complete: false, phase, administrator}, rerun: null};
  await page.route(url => url.pathname === '/app.js', async route => {
    const body = (await (await route.fetch()).text()).replace(/^window\.debridPulseAuthority=Object\.freeze\((.*)\);$/m,
      (_, json) => `window.debridPulseAuthority=Object.freeze(${JSON.stringify({...JSON.parse(json), deployment_type: deployment})});`);
    await route.fulfill({status: 200, contentType: 'application/javascript', body});
  });
  await page.route(url => url.pathname === '/api/setup/state', route => route.fulfill({status: 200,
    contentType: 'application/json', body: JSON.stringify(state)}));
}

const rect = locator => locator.evaluate(node => { const r = node.getBoundingClientRect(); return {top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height}; });

test('R01: Welcome: the product logo, the explanation, one rule above the Change Language row, Next, and the preview entry beneath', async ({page}) => {
  await load(page);
  await rerun(page);
  const mark = wizard(page).locator('.dp-modal-title-icon > dp-logo');
  await expect(mark).toHaveCount(1);
  await expect(mark.locator('svg .dp-logo-surface')).toHaveCount(1);
  // Its own gradients: the sidebar's copy keeps the logo's ids, this copy has its own.
  const ids = await mark.evaluate(node => [...node.querySelectorAll('[id]')].map(element => element.id));
  expect(ids.length).toBeGreaterThan(0);
  for (const id of ids) expect(await page.locator(`[id="${id}"]`).count()).toBe(1);
  await expect(wizard(page).locator('.dp-modal-title-hint')).toHaveText(await t(page, 'setup.welcome.tagline'));
  const paragraphs = wizard(page).locator('.dp-setup-welcome .dp-setup-copy p');
  await expect(paragraphs).toHaveCount(3);
  for (const [index, key] of ['setup.welcome.body.thanks', 'setup.welcome.body.guide', 'setup.welcome.body.next'].entries()) {
    await expect(paragraphs.nth(index)).toHaveText(await t(page, key));
  }
  // No list on the page itself: the row, its button, one rule above and none beneath.
  await expect(wizard(page).locator('.dp-setup-welcome .dp-language-list')).toHaveCount(0);
  const row = wizard(page).locator('.dp-setup-language-row');
  await expect(row.locator('.dp-setup-label')).toHaveText(await t(page, 'setup.welcome.change_language'));
  await expect(row.locator('.dp-setup-hint')).toHaveText(await t(page, 'setup.welcome.change_language_hint'));
  const button = row.locator('[data-setup-language]');
  await expect(button).toHaveAttribute('aria-label', await t(page, 'setup.welcome.language_button', {language: 'English'}));
  const rules = await row.evaluate(node => ({above: getComputedStyle(node).borderTopWidth, beneath: getComputedStyle(node).borderBottomWidth}));
  expect(rules).toEqual({above: '1px', beneath: '0px'});
  await expect(wizard(page).locator('[data-modal-accept]')).toHaveText(await t(page, 'setup.action.next'));
  // The development preview's entry: centred, beneath the language row.
  const entry = wizard(page).locator('.dp-setup-preview-entry');
  const [rowBox, entryBox, buttonBox] = [await rect(row), await rect(entry), await rect(entry.locator('button'))];
  expect(entryBox.top).toBeGreaterThanOrEqual(rowBox.bottom - 1);
  expect(Math.abs((buttonBox.left + buttonBox.right) / 2 - (entryBox.left + entryBox.right) / 2)).toBeLessThan(2);
  await end(page);
});

test('R02: the language view is a view of the same dialog: Back and Escape return unchanged, a row is shown at once and saved only by Next', async ({page}) => {
  await load(page);
  const before = (await call(page, 'GET', '/api/settings/default-locale')).body;
  await rerun(page);
  const sent = writes(page);
  const overlays = await page.locator('.dp-modal-overlay').count();
  const button = wizard(page).locator('[data-setup-language]');
  await button.click();
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(overlays);
  await title(page, 'setup.welcome.change_language');
  await expect(wizard(page).locator('.dp-modal-title-hint')).toHaveText(await t(page, 'setup.welcome.change_language_hint'));
  await expect(wizard(page).locator('[data-modal-accept]')).toBeHidden();
  await expect(wizard(page).locator('[data-modal-cancel]')).toHaveText(await t(page, 'settings.common.back'));
  await expect(wizard(page).locator('[data-modal-action="back"]')).toBeHidden();
  await expect(wizard(page).locator('.dp-language-option[aria-checked="true"]')).toBeFocused();
  // The progress stays on Welcome.
  await expect(wizard(page).locator('.dp-setup-progress-step').first()).toHaveAttribute('aria-current', 'step');
  // A language change while it is open re-says the list's name in place.
  expect(await setLocale(page, 'en-XA')).toBe('en-XA');
  await expect(wizard(page).locator('.dp-language-list')).toHaveAttribute('aria-label', await t(page, 'setup.welcome.change_language'));
  expect(await setLocale(page, 'en')).toBe('en');
  // Back: Welcome again, nothing chosen, focus on the button that opened it.
  await wizard(page).locator('[data-modal-cancel]').click();
  await expect(wizard(page).locator('.dp-setup-welcome')).toBeVisible();
  await expect(button).toBeFocused();
  await expect(button).toHaveAttribute('data-language', before.configured || before.locale);
  // Escape (a rerun's dialog is dismissible) leaves the view, not the wizard.
  await button.click();
  await page.keyboard.press('Escape');
  await expect(wizard(page).locator('.dp-setup-welcome')).toBeVisible();
  // A row: shown at once, back on Welcome, nothing written yet.
  await button.click();
  await wizard(page).locator('.dp-language-option[data-value="de"]').click();
  await expect(wizard(page).locator('.dp-setup-welcome')).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('lang', 'de');
  await expect(button).toHaveAttribute('data-language', 'de');
  expect(sent).toEqual([]);
  // Next saves it, once, as the installation's default.
  await accept(page);
  await expect(wizard(page).locator('.dp-setup-directory')).toBeVisible();
  expect(sent.filter(entry => entry === 'PUT /api/settings/default-locale')).toHaveLength(1);
  expect((await call(page, 'GET', '/api/settings/default-locale')).body.configured).toBe('de');
  await end(page);
  await call(page, 'PUT', '/api/settings/default-locale', {locale: before.configured || before.locale});
  await page.evaluate(() => window.debridPulseAuth.fetch('/api/account/locale').then(response => response.json())
    .then(own => window.DPLanguagePicker.language(own.locale)).then(window.DPLanguagePicker.apply));
});

test('R03: Browse shows the Settings folder browser as a view of the same dialog: Use This Folder fills the field, Cancel keeps it, Next alone saves', async ({page}) => {
  await load(page);
  const original = (await call(page, 'GET', '/api/settings')).body.download_folder;
  await rerun(page);
  await accept(page);
  await expect(wizard(page).locator('.dp-setup-directory')).toBeVisible();
  const copy = wizard(page).locator('.dp-setup-directory .dp-setup-copy p');
  await expect(copy.nth(0)).toHaveText(await t(page, 'setup.directory.body.purpose'));
  await expect(copy.nth(1)).toHaveText(await t(page, 'setup.directory.body.choose'));
  const field = wizard(page).locator('#dp-setup-download-folder');
  await expect(field).toHaveValue(original);
  const sent = writes(page);
  const overlays = await page.locator('.dp-modal-overlay').count();
  const browse = wizard(page).locator('[data-setup-browse]');

  await browse.click();
  await expect(page.locator('.dp-settings-directory-dialog')).toHaveCount(0);       // never a second dialog
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(overlays);
  const browser = wizard(page).locator('.dp-setup-directory-view .dp-settings-directory-browser');
  await expect(browser).toBeVisible();
  await title(page, 'directory_picker.download.title');
  await expect(wizard(page).locator('[data-modal-accept]')).toHaveText(await t(page, 'directory_picker.accept'));
  await expect(wizard(page).locator('[data-modal-cancel]')).toHaveText(await t(page, 'common.cancel'));
  await expect(browser.locator('[data-directory-current-path]')).toHaveText(original);
  await expect(wizard(page).locator('[data-modal-accept]')).toBeEnabled();
  // Into a folder, then Cancel: the field as it was.
  const child = browser.locator('[data-directory-row]').first();
  const childPath = await child.getAttribute('data-path');
  await child.click();
  await expect(browser.locator('[data-directory-current-path]')).toHaveText(childPath);
  await wizard(page).locator('[data-modal-cancel]').click();
  await expect(wizard(page).locator('.dp-setup-directory')).toBeVisible();
  await expect(field).toHaveValue(original);
  await expect(browse).toBeFocused();
  // Into the folder again, Use This Folder: the field holds it, nothing saved.
  await browse.click();
  await browser.locator(`[data-directory-row][data-path="${childPath}"]`).click();
  await expect(browser.locator('[data-directory-current-path]')).toHaveText(childPath);
  await accept(page);
  await expect(wizard(page).locator('.dp-setup-directory')).toBeVisible();
  await expect(field).toHaveValue(childPath);
  expect(sent).toEqual([]);
  expect((await call(page, 'GET', '/api/settings')).body.download_folder).toBe(original);
  // Next saves it.
  await accept(page);
  await expect(wizard(page).locator('.dp-setup-directory')).toHaveCount(0);
  expect((await call(page, 'GET', '/api/settings')).body.download_folder).toBe(childPath);
  await end(page);
  await page.evaluate(folder => window.DPSettingsPage.writeDocument({download_folder: folder}), original);
  expect((await call(page, 'GET', '/api/settings')).body.download_folder).toBe(original);
});

test('R04: a folder the server says cannot be chosen keeps Use This Folder disabled; Escape returns with the field unchanged (synthetic listing)', async ({page}) => {
  await load(page);
  await page.route(url => url.pathname === '/api/settings/directories', route => route.fulfill({status: 200,
    contentType: 'application/json', body: JSON.stringify({
      current: {path: '/synthetic/read-only', selectable: false, reason: 'read_only', capacity: null},
      parent: '/synthetic', children: [],
    })}));
  await rerun(page);
  await accept(page);
  const field = wizard(page).locator('#dp-setup-download-folder');
  const draft = await field.inputValue();
  await wizard(page).locator('[data-setup-browse]').click();
  const browser = wizard(page).locator('.dp-settings-directory-browser');
  await expect(browser.locator('[data-directory-current-state]')).toHaveText(
    await t(page, 'directory_picker.not_selectable', {reason: await t(page, 'directory_picker.reason.read_only')}));
  await expect(wizard(page).locator('[data-modal-accept]')).toBeDisabled();
  await page.keyboard.press('Escape');
  await expect(wizard(page).locator('.dp-setup-directory')).toBeVisible();
  await expect(field).toHaveValue(draft);
  // Back on the page, its Next is enabled again.
  await expect(wizard(page).locator('[data-modal-accept]')).toBeEnabled();
  await end(page);
});

test('R05: Deployment: the explanation, then two centred cards -- name, mark, meaning -- side by side, stacked at 320px; the radio group unchanged', async ({page}) => {
  await load(page);
  await previewAt(page, '.dp-setup-deployment');
  // The approved heading (it read Deployment Mode).
  await expect(wizard(page).locator('.dp-modal-title')).toHaveText('Deployment Type');
  await expect(wizard(page).locator('.dp-setup-choices')).toHaveAttribute('aria-label', 'Deployment Type');
  await expect(wizard(page).locator('.dp-modal-title-hint')).toHaveText(await t(page, 'setup.deployment.tagline'));
  await expect(wizard(page).locator('.dp-setup-deployment .dp-setup-copy p').first()).toHaveText(
    (await t(page, 'setup.deployment.body.types')));
  await expect(wizard(page).locator('.dp-setup-deployment .dp-setup-copy b')).toHaveText(
    [await t(page, 'settings.deployment.standalone'), await t(page, 'settings.deployment.server')]);
  for (const [type, glyph] of [['standalone', 'monitor'], ['server', 'server']]) {
    const card = wizard(page).locator(`[data-deployment="${type}"]`);
    const order = await card.evaluate(node => [...node.children].map(child => child.className));
    expect(order).toEqual(['dp-setup-choice-name', 'dp-setup-choice-glyph', 'dp-setup-choice-text']);
    await expect(card.locator(`svg[data-dp-lucide="${glyph}"]`)).toHaveCount(1);
    expect(await card.evaluate(node => getComputedStyle(node).textAlign)).toBe('center');
  }
  const [a, b] = [await rect(wizard(page).locator('[data-deployment="standalone"]')), await rect(wizard(page).locator('[data-deployment="server"]'))];
  expect(Math.abs(a.top - b.top)).toBeLessThan(1);
  expect(Math.abs(a.height - b.height)).toBeLessThan(1);
  // The radio group: arrows choose, one tab stop.
  await wizard(page).locator('[data-deployment="server"]').click();
  await wizard(page).locator('[data-deployment="server"]').press('ArrowLeft');
  await expect(wizard(page).locator('[data-deployment="standalone"]')).toHaveAttribute('aria-checked', 'true');
  await expect(wizard(page).locator('[data-deployment="standalone"]')).toBeFocused();
  await end(page);
  await page.setViewportSize({width: 320, height: 640});
  await previewAt(page, '.dp-setup-deployment');
  const [c, d] = [await rect(wizard(page).locator('[data-deployment="standalone"]')), await rect(wizard(page).locator('[data-deployment="server"]'))];
  expect(d.top).toBeGreaterThanOrEqual(c.bottom);
  await end(page);
});

test('R06: Administrator Setup explains why only where an account is created, with the notes centred; a resubmission says so in its heading (emulated record)', async ({page}) => {
  await load(page);
  await previewAt(page, '.dp-setup-administrator');
  await expect(wizard(page).locator('.dp-modal-title-hint')).toHaveText(await t(page, 'setup.administrator.tagline'));
  await expect(wizard(page).locator('.dp-setup-administrator .dp-setup-copy p')).toHaveText(await t(page, 'setup.administrator.body'));
  // Creating the account: only what Next does (the accounts and OIDC pointers were retired here).
  const notes = wizard(page).locator('.dp-setup-administrator .dp-setup-notes p');
  await expect(notes).toHaveCount(1);
  await expect(notes.first()).toHaveText(await t(page, 'setup.administrator.sign_in_on'));
  expect(await notes.first().evaluate(node => getComputedStyle(node).textAlign)).toBe('center');
  await expect(wizard(page).locator('.dp-setup-administrator [data-i18n="setup.administrator.more_accounts"], .dp-setup-administrator [data-i18n="setup.administrator.oidc"]')).toHaveCount(0);
  // The fields come after the explanation, the notes after the fields.
  const [copy, last, firstNote] = [await rect(wizard(page).locator('.dp-setup-administrator .dp-setup-copy')),
    await rect(wizard(page).locator('[data-setup-field="confirm"]')), await rect(notes.first())];
  expect(last.top).toBeGreaterThan(copy.bottom);
  expect(firstNote.top).toBeGreaterThan(last.bottom);
  await end(page);
  // The rerun's guidance: no creation explanation.
  if (await page.evaluate(() => window.debridPulseAuth.authority.deployment_type) === 'server') {
    await rerun(page);
    await accept(page); await accept(page);
    await expect(wizard(page).locator('.dp-setup-administrator')).toBeVisible();
    await expect(wizard(page).locator('.dp-setup-administrator .dp-setup-copy')).toHaveCount(0);
    // Its guidance and its way to Users & Access are intact.
    await expect(wizard(page).locator('.dp-setup-administrator [data-i18n="setup.administrator.oidc"]')).toBeVisible();
    await expect(wizard(page).locator('[data-setup-users]')).toBeVisible();
    await end(page);
  }
  // A new Server installation whose Administrator was created but not confirmed (not signed in as it).
  await newInstallation(page, {deployment: 'server', phase: 'administrator', administrator: 'pending'});
  await load(page);
  await expect(wizard(page).locator('.dp-setup-administrator')).toBeVisible();
  await expect(wizard(page).locator('.dp-modal-title-hint')).toHaveText(await t(page, 'setup.administrator.resubmit'));
  await expect(wizard(page).locator('.dp-setup-administrator .dp-setup-copy')).toHaveCount(0);
  await expect(wizard(page).locator('[data-i18n="setup.administrator.tagline"]')).toHaveCount(0);
});

test('R07: Premium Services: the Optional badge between the progress and the title, on this page and its provider views only; each service wears its own artwork', async ({page}) => {
  await load(page);
  await previewAt(page, '.dp-setup-premium');
  const badge = wizard(page).locator('.dp-setup-optional');
  await expect(badge).toBeVisible();
  await expect(badge).toHaveText(await t(page, 'setup.premium.optional'));
  const [progress, badgeBox, titleLine] = [await rect(wizard(page).locator('.dp-setup-progress')), await rect(badge),
    await rect(wizard(page).locator('.dp-modal-title-line'))];
  expect(badgeBox.top).toBeGreaterThanOrEqual(progress.bottom);
  expect(badgeBox.bottom).toBeLessThanOrEqual(titleLine.top);
  // Yellow: the theme's caution colour.
  const colours = await badge.evaluate(node => {
    const probe = document.createElement('span');
    probe.style.color = 'var(--dp-state-caution)';
    node.after(probe);
    const expected = getComputedStyle(probe).color;
    probe.remove();
    return {shown: getComputedStyle(node).color, expected};
  });
  expect(colours.shown).toBe(colours.expected);
  await expect(wizard(page).locator('.dp-setup-premium .dp-setup-copy p').nth(1).locator('b')).toHaveText(
    await page.evaluate(() => window.DPI18n.rich('setup.premium.body.optional').querySelector('b').textContent));
  const rows = await wizard(page).locator('[data-service]').evaluateAll(nodes => nodes.map(node => {
    const art = node.querySelector('.dp-setup-service-art');
    const copy = node.querySelector('.dp-setup-service-copy');
    const name = copy.querySelector('.dp-setup-service-name').getBoundingClientRect();
    const state = copy.querySelector('.dp-setup-service-state').getBoundingClientRect();
    const end = getComputedStyle(node).direction === 'rtl'
      ? art.getBoundingClientRect().left >= copy.getBoundingClientRect().right - 1
      : art.getBoundingClientRect().right <= copy.getBoundingClientRect().left + 1;
    return {id: node.dataset.service, logo: art.querySelector('img')?.getAttribute('src') || null,
      chip: art.querySelector('.dp-settings-protocol-chip')?.dataset.protocol || null, artFirst: end, nameAbove: name.bottom <= state.top + 1};
  }));
  expect(rows).toEqual([
    {id: 'usenet', logo: '/icons/lucide/newspaper.svg', chip: 'usenet', artFirst: true, nameAbove: true},
    {id: 'alldebrid', logo: '/icons/providers/alldebrid.svg', chip: null, artFirst: true, nameAbove: true},
    {id: 'debridlink', logo: '/icons/providers/debrid-link.svg', chip: null, artFirst: true, nameAbove: true},
    {id: 'premiumize', logo: '/icons/providers/premiumize.svg', chip: null, artFirst: true, nameAbove: true},
    {id: 'realdebrid', logo: '/icons/providers/real-debrid.svg', chip: null, artFirst: true, nameAbove: true},
    {id: 'torbox', logo: '/icons/providers/torbox.svg', chip: null, artFirst: true, nameAbove: true},
  ]);
  // The provider's artwork is its Settings card's own mark.
  await wizard(page).locator('[data-service="alldebrid"]').click();
  await expect(wizard(page).locator('.dp-setup-provider-surface .dp-settings-provider-card--alldebrid')).toBeVisible();
  await expect(badge).toBeVisible();
  await wizard(page).locator('[data-modal-cancel]').click();
  await accept(page);
  await expect(wizard(page).locator('.dp-setup-getting-started')).toBeVisible();
  await expect(badge).toBeHidden();
  await end(page);
});

/* The substantive island of each provider's card: its credential row, its
 * account island, or Usenet's server collection. */
const ISLAND = '.dp-settings-alldebrid-key-row, .dp-settings-debridlink-key-row, .dp-settings-premiumize-key-row, '
  + '.dp-settings-account-island, .dp-usenet-servers';

for (const [width, height] of [[1280, 900], [320, 640]]) {
  test(`R08: at ${width}px each provider's card takes the body's width and its island sits evenly in the card body: the same space above as below`, async ({page}) => {
    await load(page, {width, height});
    await previewAt(page, '.dp-setup-premium');
    for (const id of ['usenet', 'alldebrid', 'debridlink', 'premiumize', 'realdebrid', 'torbox']) {
      await wizard(page).locator(`[data-service="${id}"]`).click();
      const card = wizard(page).locator(`.dp-setup-provider-surface .dp-settings-provider-card--${id}`);
      await expect(card).toBeVisible();
      const g = await card.evaluate((node, selector) => {
        const r = element => element.getBoundingClientRect();
        const body = node.closest('.dp-setup-body');
        const style = getComputedStyle(body);
        const cardBody = node.querySelector(':scope > .card-body');
        const island = cardBody.querySelector(selector);
        const field = node.querySelector('.dp-action-field');
        const islandStyle = getComputedStyle(island);
        const islandContent = r(island).width - ['paddingLeft', 'paddingRight', 'borderLeftWidth', 'borderRightWidth']
          .reduce((sum, name) => sum + parseFloat(islandStyle[name]), 0);
        return {islandContent, card: r(node).width, inner: r(body).width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight),
          cardBody: r(cardBody).width, island: r(island).width, field: field ? r(field).width : null,
          above: r(island).top - r(cardBody).top, below: r(cardBody).bottom - r(island).bottom,
          padding: parseFloat(getComputedStyle(cardBody).paddingTop),
          cardTop: r(node).top - r(body).top, bodyPadding: parseFloat(style.paddingTop),
          marginBottom: parseFloat(getComputedStyle(node).marginBottom)};
      }, ISLAND);
      // The card spans the body; the island spans the card body.
      expect(Math.abs(g.card - g.inner), id).toBeLessThan(2);
      expect(g.island, id).toBeGreaterThan(g.cardBody * 0.9);
      // The key field fills its island's content width (never squeezed beside its title).
      if (['alldebrid', 'debridlink', 'premiumize'].includes(id)) expect(Math.abs(g.field - g.islandContent), id).toBeLessThan(1);
      // The island: the card body's own padding above and below, nothing else.
      expect(Math.abs(g.above - g.below), `${id} above ${g.above} below ${g.below}`).toBeLessThan(1);
      expect(Math.abs(g.above - g.padding), id).toBeLessThan(1);
      // The card: the body's padding above, no margin of its own below.
      expect(Math.abs(g.cardTop - g.bodyPadding), id).toBeLessThan(1);
      expect(g.marginBottom, id).toBe(0);
      await wizard(page).locator('[data-modal-cancel]').click();
    }
    await end(page);
  });
}

test('R08b: Settings keeps its content-bounded credential island and the account card\'s reserved status row', async ({page}) => {
  await load(page);
  await page.evaluate(() => window.nav(document.querySelector('#sidebar .nav-item[data-view="settings"]')));
  const allDebrid = page.locator('#view-settings .dp-settings-provider-card--alldebrid');
  await expect(allDebrid).toBeVisible();
  await allDebrid.locator('.dp-settings-disclosure').click();
  await expect(allDebrid.locator('.dp-settings-alldebrid-key-row')).toBeVisible();
  const row = await allDebrid.locator('.dp-settings-alldebrid-key-row').evaluate(node => ({
    direction: getComputedStyle(node).flexDirection, ratio: node.getBoundingClientRect().width / node.closest('.card-body').getBoundingClientRect().width}));
  expect(row.direction).toBe('row');
  expect(row.ratio).toBeLessThan(0.75);
  const reserved = await page.locator('#view-settings .dp-settings-provider-card--realdebrid .dp-settings-provider-status-line').evaluate(node => ({
    empty: node.childNodes.length === 0, height: parseFloat(getComputedStyle(node).height), gap: parseFloat(getComputedStyle(node).marginBottom)}));
  expect(reserved.empty).toBe(true);
  expect(reserved.height).toBe(18);          // 1.5em of its 12px
  expect(reserved.gap).toBe(14);
});

test('R09: Getting Started: no empty illustration; the introduction and three steps; no sign-in note in a preview or a rerun', async ({page}) => {
  await load(page);
  await previewAt(page, '.dp-setup-getting-started');
  await expect(wizard(page).locator('.dp-setup-illustration, figure')).toHaveCount(0);
  await expect(wizard(page).locator('.dp-setup-getting-started .dp-setup-copy p')).toHaveText(await t(page, 'setup.getting_started.body'));
  const steps = wizard(page).locator('.dp-setup-steps > li');
  await expect(steps).toHaveCount(3);
  for (const [index, key] of ['setup.getting_started.step.open', 'setup.getting_started.step.add', 'setup.getting_started.step.monitor'].entries()) {
    await expect(steps.nth(index)).toHaveText(await t(page, key));
    await expect(steps.nth(index).locator('b')).toHaveCount(1);
  }
  await expect(wizard(page).locator('[data-i18n="setup.getting_started.sign_in"]')).toHaveCount(0);
  await expect(wizard(page).locator('[data-modal-accept]')).toHaveText(await t(page, 'setup.action.finish'));
  await end(page);
  await rerun(page);
  for (let i = 0; i < 5 && !(await wizard(page).locator('.dp-setup-getting-started').count()); i += 1) await accept(page);
  await expect(wizard(page).locator('[data-i18n="setup.getting_started.sign_in"]')).toHaveCount(0);
  await end(page);
});

for (const [deployment, administrator, shown] of [['server', 'created', true], ['standalone', 'none', false]]) {
  test(`R10: initial ${deployment} setup at Getting Started: the sign-in note ${shown ? 'at the bottom, apart from the steps' : 'is absent'} (emulated record)`, async ({page}) => {
    await newInstallation(page, {deployment, phase: 'getting_started', administrator});
    for (const [width, height] of [[1280, 900], [320, 568]]) {
      await load(page, {width, height});
      await expect(wizard(page).locator('.dp-setup-getting-started')).toBeVisible();
      const note = wizard(page).locator('.dp-setup-sign-in-note');
      await expect(note).toHaveCount(shown ? 1 : 0);
      if (!shown) continue;
      await expect(note).toHaveText(await t(page, 'setup.getting_started.sign_in'));
      const box = await note.evaluate(node => {
        const body = node.closest('.dp-setup-body');
        body.scrollTop = body.scrollHeight;
        const steps = body.querySelector('.dp-setup-steps').getBoundingClientRect();
        const footer = node.closest('.dp-modal-dialog').querySelector('.dp-modal-footer').getBoundingClientRect();
        const r = node.getBoundingClientRect();
        const b = body.getBoundingClientRect();
        return {apart: r.top >= steps.bottom + 8, inBody: r.bottom <= b.bottom + 1, aboveFooter: r.bottom <= footer.top + 1,
          // The page's last content: it ends one separation row above the body's end (.dp-setup-main).
          atBottom: body.scrollHeight > body.clientHeight || Math.abs(b.bottom - parseFloat(getComputedStyle(body).paddingBottom)
            - parseFloat(getComputedStyle(node.closest('.dp-setup-main')).marginBottom) - r.bottom) < 2,
          rule: getComputedStyle(node).borderTopWidth};
      });
      expect(box, `${width}`).toEqual({apart: true, inBody: true, aboveFooter: true, atBottom: true, rule: '1px'});
    }
  });
}

test('R11: the header: the same space between the progress and the heading on every page, the badge never over a bubble or the title', async ({page}) => {
  await load(page);
  await preview(page);
  const gaps = [];
  for (let i = 0; i < 6; i += 1) {
    const geometry = await wizard(page).evaluate(node => {
      const progress = node.querySelector('.dp-setup-progress').getBoundingClientRect();
      const line = node.querySelector('.dp-modal-title-line').getBoundingClientRect();
      const body = node.querySelector('.dp-setup-body').getBoundingClientRect();
      return {gap: Math.round(line.top - progress.bottom), bodyTop: Math.round(body.top)};
    });
    gaps.push(geometry);
    if (await wizard(page).locator('.dp-setup-deployment').count()) await wizard(page).locator('[data-deployment="server"]').click();
    if (await wizard(page).locator('.dp-setup-getting-started').count()) break;
    await accept(page);
  }
  expect(gaps.length).toBe(6);
  expect(new Set(gaps.map(entry => entry.gap)).size).toBe(1);
  expect(new Set(gaps.map(entry => entry.bodyTop)).size).toBe(1);
  expect(gaps[0].gap).toBeGreaterThanOrEqual(40);
  await end(page);
});

test('R12: en-XA at 320px: taglines, badge, explanations, cards, service rows and both views fit without horizontal overflow; the footer stays put', async ({page}) => {
  await load(page, {width: 320, height: 640});
  expect(await setLocale(page, 'en-XA')).toBe('en-XA');
  await preview(page);
  const check = async label => {
    const fit = await wizard(page).evaluate(node => {
      const dialog = node.getBoundingClientRect();
      const overflowing = [...node.querySelectorAll('.dp-modal-header *, .dp-modal-body *, .dp-modal-footer *')]
        .filter(child => child.getClientRects().length && !child.closest('.dp-setup-progress')
          // The header's artwork is wider than the dialog by design and clipped by the header (R16).
          && !child.closest('.dp-signal-field')
          && (child.getBoundingClientRect().right > dialog.right + 1 || child.getBoundingClientRect().left < dialog.left - 1))
        .map(child => child.className || child.tagName);
      const hint = node.querySelector('.dp-modal-title-hint');
      const footer = node.querySelector('.dp-modal-footer').getBoundingClientRect();
      return {overflowing, hintFits: !hint || hint.hidden || hint.scrollHeight <= hint.clientHeight + 1,
        page: document.documentElement.scrollWidth, footerBottom: Math.round(footer.bottom)};
    });
    expect(fit.overflowing, label).toEqual([]);
    expect(fit.page, label).toBeLessThanOrEqual(320);
    return fit;
  };
  const footers = new Set();
  for (let i = 0; i < 6; i += 1) {
    footers.add((await check(`page ${i + 1}`)).footerBottom);
    if (await wizard(page).locator('.dp-setup-welcome').count()) {
      await wizard(page).locator('[data-setup-language]').click();
      await check('language view');
      await wizard(page).locator('[data-modal-cancel]').click();
    }
    if (await wizard(page).locator('.dp-setup-directory').count()) {
      await wizard(page).locator('[data-setup-browse]').click();
      await expect(wizard(page).locator('.dp-settings-directory-browser')).toBeVisible();
      await check('directory view');
      await wizard(page).locator('[data-modal-cancel]').click();
    }
    if (await wizard(page).locator('.dp-setup-premium').count()) {
      await expect(wizard(page).locator('.dp-setup-optional')).toBeVisible();
      await wizard(page).locator('[data-service="alldebrid"]').click();
      await expect(wizard(page).locator('.dp-setup-provider-surface')).toBeVisible();
      await check('provider view');
      await wizard(page).locator('[data-modal-cancel]').click();
    }
    if (await wizard(page).locator('.dp-setup-deployment').count()) await wizard(page).locator('[data-deployment="server"]').click();
    if (await wizard(page).locator('.dp-setup-getting-started').count()) break;
    await accept(page);
  }
  expect(footers.size).toBe(1);
  await end(page);
  expect(await setLocale(page, 'en')).toBe('en');
});

test('R13: Arabic (right to left): Welcome\'s language row and the service rows mirror, nothing overflows', async ({page}) => {
  // This account's own language (a page's previewed language is withdrawn when the
  // rerun's Welcome is left for the preview); put back at the end.
  await load(page, {width: 375, height: 740});
  const own = (await call(page, 'GET', '/api/account/locale')).body;
  expect((await call(page, 'PUT', '/api/account/locale', {locale: 'ar'})).status).toBe(200);
  await load(page, {width: 375, height: 740});
  await expect(page.locator('html')).toHaveAttribute('lang', 'ar');
  await preview(page);
  await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
  const row = await wizard(page).locator('.dp-setup-language-row').evaluate(node => {
    const copy = node.querySelector('.dp-setup-language-copy').getBoundingClientRect();
    const button = node.querySelector('[data-setup-language]').getBoundingClientRect();
    return {buttonAtEnd: button.right <= copy.left + 1 || button.top >= copy.bottom - 1};
  });
  expect(row.buttonAtEnd).toBe(true);
  await previewAtFrom(page);
  const mirrored = await wizard(page).locator('[data-service="usenet"]').evaluate(node =>
    node.querySelector('.dp-setup-service-art').getBoundingClientRect().left >= node.querySelector('.dp-setup-service-copy').getBoundingClientRect().right - 1);
  expect(mirrored).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375);
  await end(page);
  await call(page, 'PUT', '/api/account/locale', {locale: own.preference ?? null});
});

/* From the preview's Welcome on to Premium Services (Server roster). */
async function previewAtFrom(page) {
  for (let i = 0; i < 6 && !(await wizard(page).locator('.dp-setup-premium').count()); i += 1) {
    if (await wizard(page).locator('.dp-setup-deployment').count()) await wizard(page).locator('[data-deployment="server"]').click();
    await accept(page);
  }
  await expect(wizard(page).locator('.dp-setup-premium')).toBeVisible();
}

/* The one main-page composition (.dp-setup-main): a separation row, the
 * centred explanation, the spare room, the page's controls ending near the
 * footer, a separation row. The row is the viewport's spare height: a tenth
 * of the height beyond 560px, up to 20px. Measured, never read from CSS text. */
const expectedRow = height => Math.max(0, Math.min(20, (height - 560) * 0.1));
const PAGE_CLASSES = ['.dp-setup-welcome', '.dp-setup-deployment', '.dp-setup-directory', '.dp-setup-administrator',
  '.dp-setup-premium', '.dp-setup-getting-started'];

for (const [width, height] of [[1280, 900], [375, 740], [375, 560], [320, 640]]) {
  test(`R14: at ${width}x${height} every page shares one composition: row, centred explanation, spare room, controls ending a row above the footer`, async ({page}) => {
    await load(page, {width, height});
    await preview(page);
    const row = expectedRow(height);
    const seen = [];
    for (let i = 0; i < 6; i += 1) {
      const g = await wizard(page).evaluate(node => {
        const body = node.querySelector('.dp-setup-body');
        const section = body.querySelector('.dp-setup-main');
        const style = getComputedStyle(body);
        const r = element => element.getBoundingClientRect();
        const copy = section.querySelector(':scope > .dp-setup-copy');
        const controls = [...section.children].filter(child => child !== copy && child.getClientRects().length);
        const last = controls[controls.length - 1];
        const bodyBottom = r(body).bottom - parseFloat(style.paddingBottom);
        const scrolls = body.scrollHeight > body.clientHeight + 1;
        return {page: section.className, mains: body.querySelectorAll('.dp-setup-main').length,
          top: parseFloat(getComputedStyle(section).marginTop), bottom: parseFloat(getComputedStyle(section).marginBottom),
          centred: [...copy.querySelectorAll('p')].every(p => getComputedStyle(p).textAlign === 'center'),
          controlsLeftAligned: controls.every(child => !child.matches('.dp-setup-steps') || getComputedStyle(child).textAlign !== 'center'),
          gapAfterCopy: r(controls[0]).top - r(copy).bottom,
          lastToSectionEnd: r(section).bottom - r(last).bottom,
          sectionToBody: bodyBottom - r(section).bottom, scrolls};
      });
      seen.push(g.page);
      expect(g.mains, g.page).toBe(1);
      expect(Math.abs(g.top - row), g.page).toBeLessThan(0.6);
      expect(Math.abs(g.bottom - row), g.page).toBeLessThan(0.6);
      expect(g.centred, g.page).toBe(true);
      expect(g.controlsLeftAligned, g.page).toBe(true);
      expect(g.gapAfterCopy, g.page).toBeGreaterThanOrEqual(9.5);
      // The controls end at the page's end; the page ends one row above the body's end (unless it scrolls).
      expect(Math.abs(g.lastToSectionEnd), g.page).toBeLessThan(1);
      if (!g.scrolls) expect(Math.abs(g.sectionToBody - row), g.page).toBeLessThan(1);
      if (await wizard(page).locator('.dp-setup-deployment').count()) await wizard(page).locator('[data-deployment="server"]').click();
      if (await wizard(page).locator('.dp-setup-getting-started').count()) break;
      await accept(page);
    }
    for (const selector of PAGE_CLASSES) expect(seen.some(name => name.includes(selector.slice(1))), selector).toBe(true);
    await end(page);
  });
}

test('R15: the views of a page stay content-first: no page composition, no rows, starting at the body\'s top', async ({page}) => {
  await load(page);
  await preview(page);
  const check = async label => {
    const g = await wizard(page).evaluate(node => {
      const body = node.querySelector('.dp-setup-body');
      const view = body.querySelector('.dp-setup-language-view, .dp-setup-directory-view, .dp-setup-provider');
      const note = body.querySelector('.dp-setup-preview-note');
      return {mains: body.querySelectorAll('.dp-setup-main').length, margin: getComputedStyle(view).marginTop,
        grow: getComputedStyle(view).flexGrow, top: view.getBoundingClientRect().top - (note ? note.getBoundingClientRect().bottom : body.getBoundingClientRect().top)};
    });
    expect(g.mains, label).toBe(0);
    expect(g.margin, label).toBe('0px');
    expect(g.grow, label).toBe('0');
    expect(g.top, label).toBeLessThan(25);
  };
  await wizard(page).locator('[data-setup-language]').click();
  await check('language');
  await wizard(page).locator('[data-modal-cancel]').click();
  await accept(page);
  await wizard(page).locator('[data-deployment="server"]').click();
  await accept(page);
  await wizard(page).locator('[data-setup-browse]').click();
  await expect(wizard(page).locator('.dp-settings-directory-browser')).toBeVisible();
  await check('directory');
  await wizard(page).locator('[data-modal-cancel]').click();
  await accept(page); await accept(page);
  await wizard(page).locator('[data-service="alldebrid"]').click();
  await expect(wizard(page).locator('.dp-setup-provider-surface')).toBeVisible();
  await check('provider');
  await wizard(page).locator('[data-modal-cancel]').click();
  await end(page);
});

for (const theme of ['dark', 'light']) {
  test(`R16: the header's artwork is the sidebar's signal field, behind the heading and beneath the indicator, clipped by the header (${theme})`, async ({page}) => {
    await load(page, {width: 375, height: 740});
    if (theme === 'light') await page.evaluate(() => document.body.classList.add('light'));
    await rerun(page);
    const g = await wizard(page).evaluate(node => {
      const header = node.querySelector('.dp-modal-header');
      const field = header.querySelector('.dp-signal-field');
      const svg = field?.querySelector('svg');
      const ids = svg ? [...svg.querySelectorAll('[id]')].map(element => element.id) : [];
      const z = element => Number(getComputedStyle(element).zIndex) || 0;
      return {field: Boolean(field), drawn: Boolean(svg), hidden: field?.getAttribute('aria-hidden'),
        pointer: getComputedStyle(field).pointerEvents, opacity: Number(getComputedStyle(field).opacity),
        behind: z(field) < z(node.querySelector('.dp-modal-heading')) && z(field) < z(node.querySelector('.dp-setup-progress')),
        clipped: getComputedStyle(header).overflow, rings: [getComputedStyle(header, '::before').content, getComputedStyle(header, '::after').content],
        uniqueIds: ids.every(id => document.querySelectorAll(`[id="${id}"]`).length === 1), dialogWidth: node.getBoundingClientRect().width,
        pageScroll: document.documentElement.scrollWidth};
    });
    expect(g).toMatchObject({field: true, drawn: true, hidden: 'true', pointer: 'none', behind: true, clipped: 'hidden',
      rings: ['none', 'none'], uniqueIds: true});
    expect(g.opacity).toBeLessThanOrEqual(theme === 'light' ? 0.2 : 0.34);
    expect(g.pageScroll).toBeLessThanOrEqual(375);
    // The heading and its controls stay reachable above it.
    await expect(wizard(page).locator('[data-setup-language]')).toBeVisible();
    await end(page);
  });
}
