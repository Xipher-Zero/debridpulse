const { test, expect } = require('@playwright/test');

/* 1.0.13 ACL live qualification, Batch 2: five focused UX refinements. Real
 * sessions on the password-auth runtime; every account is a synthetic fixture
 * this file creates under a run-unique name and removes. No case here touches
 * a setting: the OIDC layout is measured, never saved. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const EVIDENCE = process.env.DP_EVIDENCE_DIR || '';
const ADMIN = {username: 'browser', password: 'browser-secret'};
const run = Date.now().toString(36);

async function signIn(browser, {username, password}, viewport = {width: 1440, height: 914}) {
  const context = await browser.newContext({baseURL: AUTH, viewport});
  const page = await context.newPage();
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  const errors = [];
  page.on('pageerror', error => errors.push(String(error.message)));
  await page.goto('/login');
  await page.locator('#username').fill(username);
  await page.locator('#password').fill(password);
  await page.locator('button[type="submit"]').click();
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.locator('#sidebar-auth-row .dp-sidebar-profile')).toBeVisible();
  return {context, page, errors};
}

const api = (page, method, path, body) => page.evaluate(async ([method, path, body]) => {
  const response = await window.debridPulseAuth.fetch(`/api${path}`, {
    method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  return {status: response.status, body: await response.json().catch(() => null)};
}, [method, path, body]);

async function person(admin, name, role = 'user') {
  const account = {username: `b2-${name}-${run}`, password: `b2-${name}-${run}-pass`};
  const created = await api(admin.page, 'POST', '/accounts', {kind: 'local', role, display_name: `${name} ${run}`, ...account});
  expect(created.status).toBe(200);
  return {...account, id: created.body.id, name: `${name} ${run}`};
}

async function settingsTab(page, tab) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await page.locator(`#view-settings [data-tab="${tab}"]`).click();
  await expect(page.locator(`#view-settings [data-panel="${tab}"]`)).toBeVisible();
}

async function shot(page, name) {
  if (EVIDENCE) await page.screenshot({path: `${EVIDENCE}/${name}.png`, fullPage: false});
}

test('a revoked token leaves the active list once the server confirms it, and a refused Revoke keeps it', async ({browser}) => {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const admin = await signIn(browser, ADMIN);
  const page = admin.page;
  const name = `Batch2 Bot ${run}`;
  const created = await api(page, 'POST', '/accounts', {kind: 'integration', display_name: name});
  expect(created.status).toBe(200);
  const id = created.body.id;
  try {
    const kept = await api(page, 'POST', `/accounts/${id}/tokens`, {label: 'kept'});
    const retired = await api(page, 'POST', `/accounts/${id}/tokens`, {label: 'retired'});
    expect(retired.status).toBe(200);
    const bearer = token => page.evaluate(async raw => (await fetch('/api/stats', {
      credentials: 'omit', headers: {Authorization: `Bearer ${raw}`}})).status, token);
    expect(await bearer(retired.body.token)).toBe(200);

    await settingsTab(page, 'users');
    const account = page.locator('.dp-settings-users-integration', {hasText: name});
    const rows = account.locator('.dp-settings-users-tokens tbody tr');
    await expect(rows).toHaveCount(2);
    const revoke = label => rows.filter({hasText: label}).locator('[data-user-action="revoke-token"]');

    // Refused by the server: the row stays, still active and still revocable.
    await page.route(`**/api/accounts/${id}/tokens/${retired.body.id}`, route =>
      route.fulfill({status: 500, contentType: 'application/json', body: JSON.stringify({detail: 'Refused for the test'})}));
    await revoke('retired').click();
    await page.locator('.dp-modal-overlay [data-modal-accept]').click();
    await expect(page.locator('.toast').last()).toContainText('Refused for the test');
    await expect(rows).toHaveCount(2);
    await expect(revoke('retired')).toBeVisible();
    expect(await bearer(retired.body.token)).toBe(200);
    await page.unroute(`**/api/accounts/${id}/tokens/${retired.body.id}`);

    // Confirmed by the server: the row leaves; the other token is untouched.
    await revoke('retired').click();
    await page.locator('.dp-modal-overlay [data-modal-accept]').click();
    await expect(rows).toHaveCount(1);
    await expect(rows).toContainText(['kept']);
    await expect(account).not.toContainText('Revoked');
    expect(await bearer(retired.body.token)).toBe(401);
    expect(await bearer(kept.body.token)).toBe(200);

    // It never comes back: not on a re-read, not after a reload.
    expect((await api(page, 'GET', `/accounts/${id}/tokens`)).body.tokens.map(token => token.label)).toEqual(['kept']);
    await page.reload();
    await settingsTab(page, 'users');
    await expect(page.locator('.dp-settings-users-integration', {hasText: name})
      .locator('.dp-settings-users-tokens tbody tr')).toHaveCount(1);
    expect(admin.errors).toEqual([]);
  } finally {
    await api(page, 'DELETE', `/accounts/${id}`);
  }
});

test('a User\'s Notifications page is headed The Grapevine in the canonical master-card header', async ({browser}) => {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const admin = await signIn(browser, ADMIN);
  const user = await person(admin, 'Grape');
  try {
    // The canonical master header this page reuses: the Admin's Settings header.
    await admin.page.locator('#sidebar .nav-item[data-view="settings"]').click();
    await expect(admin.page.locator('#view-settings .dp-settings-header-title')).toHaveText('Tuning Deck');
    const canonical = await admin.page.locator('#view-settings .dp-settings-master-header').evaluate(node => {
      const r = node.getBoundingClientRect();
      const title = getComputedStyle(node.querySelector('.dp-settings-header-title'));
      const icon = node.querySelector('.dp-settings-header-icon').getBoundingClientRect();
      // The canonical header's one-row height (its tab strip wraps beneath the
      // copy below 1700px; this page has no tab strip).
      return {height: parseFloat(getComputedStyle(node).minHeight), font: [title.fontSize, title.fontWeight],
        icon: Math.round(icon.width)};
    });

    const signed = await signIn(browser, user);
    const page = signed.page;
    await page.locator('#sidebar .nav-item', {hasText: 'Notifications'}).click();
    const header = page.locator('#view-settings .dp-settings-master-card--own > .dp-settings-master-header');
    await expect(header.locator('.dp-settings-header-title')).toHaveText('The Grapevine');
    await expect(header.locator('.dp-settings-header-subtitle')).toHaveText('Good news travels fast. Download news travels faster.');
    // The one Notifications family chip: Lucide BellRing on the cyan identity.
    const chip = header.locator('.dp-settings-header-copy > .dp-settings-protocol-chip.dp-settings-header-chip[data-section="notifications"]');
    await expect(chip).toHaveCount(1);
    await expect(chip.locator('img')).toHaveAttribute('src', '/icons/lucide/bell-ring.svg');
    // No controls on the right, and the two existing cards beneath it.
    await expect(header.locator('button, select, input, [role="tab"], .stabs')).toHaveCount(0);
    const view = page.locator('#view-settings');
    await expect(view.locator('.dp-settings-discord-card')).toBeVisible();
    await expect(view.locator('.dp-settings-statistics-reporting-card')).toBeVisible();
    expect(await view.locator('.card-title').allTextContents()).toEqual(
      expect.arrayContaining([expect.stringContaining('Discord Notifications'), expect.stringContaining('Statistics Reporting')]));

    for (const [width, theme] of [[1440, 'dark'], [1024, 'light'], [800, 'dark']]) {
      await page.setViewportSize({width, height: 900});
      await page.evaluate(light => { document.body.classList.toggle('light', light); window.closeSidebar?.(); }, theme === 'light');
      const facts = await header.evaluate(node => {
        const box = element => element.getBoundingClientRect();
        const chipNode = node.querySelector('.dp-settings-protocol-chip');
        const text = chipNode.nextElementSibling;
        const style = getComputedStyle(chipNode);
        const glyph = getComputedStyle(chipNode.querySelector('img'));
        const color = getComputedStyle(document.body).getPropertyValue('color');
        return {
          height: Math.round(box(node).height),
          chip: Math.round(box(chipNode).width),
          offset: Math.abs((box(chipNode).top + box(chipNode).height / 2) - (box(text).top + box(text).height / 2)),
          textLeftOfChipRight: box(text).left - box(chipNode).right,
          protocolColor: style.getPropertyValue('--dp-protocol-color').trim().toLowerCase(),
          innerGlow: style.boxShadow.includes('inset'),
          outerGlow: style.filter.includes('drop-shadow'),
          glyphGlow: glyph.filter.includes('drop-shadow'),
          overflow: node.scrollWidth > node.clientWidth + 1, color,
          title: getComputedStyle(node.querySelector('.dp-settings-header-title')).fontSize,
        };
      });
      expect(facts.protocolColor).toBe('#04d7fe');
      expect(facts.innerGlow && facts.outerGlow && facts.glyphGlow).toBe(true);
      expect(facts.offset).toBeLessThanOrEqual(1);              // the text block centred on the chip
      expect(facts.textLeftOfChipRight).toBeGreaterThan(0);
      expect(facts.overflow).toBe(false);
      expect(facts.chip).toBe(canonical.icon);                  // the shared page feature footprint
      expect(facts.title).toBe(canonical.font[0]);
      expect(facts.height).toBe(canonical.height);
      await shot(page, `grapevine-${width}-${theme}`);
    }
    expect(signed.errors).toEqual([]);
  } finally {
    await api(admin.page, 'DELETE', `/accounts/${user.id}`);
  }
});

test('My Account takes an email once when it has none; it is recorded unverified and the Admin sees that', async ({browser}) => {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const admin = await signIn(browser, ADMIN);
  const user = await person(admin, 'Mail');
  const email = `mail-${run}@example.com`;
  try {
    const signed = await signIn(browser, user);
    const page = signed.page;
    const open = async () => {
      await page.locator('#sidebar-auth-row .dp-sidebar-profile').click();
      const dialog = page.locator('.dp-modal-dialog.dp-account-dialog');
      await expect(dialog.locator('[data-field="display_name"]')).toHaveValue(/.+/);
      return dialog;
    };
    const geometry = dialog => dialog.evaluate(node => {
      const r = node.getBoundingClientRect();
      const scrolls = [node, ...node.querySelectorAll('*')].some(element =>
        element.scrollHeight > element.clientHeight + 1 && getComputedStyle(element).overflowY !== 'visible'
        || element.scrollWidth > element.clientWidth + 1 && getComputedStyle(element).overflowX !== 'visible');
      return {width: r.width, height: r.height, scrolls,
        clipped: [...node.querySelectorAll('input, .btn')].filter(element => !element.hidden && element.getClientRects().length)
          .some(element => element.getBoundingClientRect().bottom > r.bottom + 0.5)};
    });

    // No email: an optional input, styled as the two names are.
    let dialog = await open();
    const input = dialog.locator('[data-field="email"]');
    await expect(input).toBeEditable();
    await expect(input).toHaveAttribute('placeholder', 'you@example.com');
    await expect(input).toHaveClass(/\binput\b/);
    const editable = await geometry(dialog);
    expect(editable.scrolls).toBe(false);
    expect(editable.clipped).toBe(false);
    await shot(page, 'my-account-email-empty');

    // A blank email with a name edit saves the name and invents nothing.
    await dialog.locator('[data-field="actual_name"]').fill('Mail Person');
    let reloaded = page.waitForEvent('load');
    await dialog.locator('[data-modal-accept]').click();
    await reloaded;
    expect((await api(page, 'GET', '/account')).body).toMatchObject({actual_name: 'Mail Person', email: ''});

    // An invalid address is refused by the server; the dialog and its edits stay.
    dialog = await open();
    await dialog.locator('[data-field="display_name"]').fill(`Mail Edited ${run}`);
    await dialog.locator('[data-field="email"]').fill('not-an-address@nowhere');
    await dialog.locator('[data-modal-accept]').click();
    await expect(page.locator('.toast').last()).toContainText('Enter a valid email address');
    await expect(dialog).toBeVisible();
    await expect(dialog.locator('[data-field="email"]')).toHaveValue('not-an-address@nowhere');
    await expect(dialog.locator('[data-field="display_name"]')).toHaveValue(`Mail Edited ${run}`);
    expect((await api(page, 'GET', '/account')).body.email).toBe('');

    // The first valid address is saved and is read-only from then on.
    await dialog.locator('[data-field="email"]').fill(email);
    reloaded = page.waitForEvent('load');
    await dialog.locator('[data-modal-accept]').click();
    await reloaded;
    expect((await api(page, 'GET', '/account')).body).toMatchObject(
      {display_name: `Mail Edited ${run}`, email, email_verification: 'unverified'});
    dialog = await open();
    await expect(dialog.locator('[data-field="email"]')).toHaveCount(0);
    await expect(dialog.locator('.dp-account-email-value')).toHaveText(`${email} (Not verified)`);
    expect(await geometry(dialog)).toEqual(editable);           // the same fixed dialog, no scrollbars
    await shot(page, 'my-account-email-read-only');
    await dialog.locator('[data-modal-cancel]').click();
    expect((await api(page, 'PUT', '/account/email', {email: `other-${run}@example.com`})).status).toBe(409);

    // A stale form (opened before another save) is refused and shows the stored address.
    await api(admin.page, 'DELETE', `/accounts/${user.id}`);
    const stale = await person(admin, 'Stale');
    const staleSigned = await signIn(browser, stale);
    await staleSigned.page.locator('#sidebar-auth-row .dp-sidebar-profile').click();
    const staleDialog = staleSigned.page.locator('.dp-modal-dialog.dp-account-dialog');
    await staleDialog.locator('[data-field="email"]').fill(`late-${run}@example.com`);
    expect((await api(staleSigned.page, 'PUT', '/account/email', {email: `first-${run}@example.com`})).status).toBe(200);
    await staleDialog.locator('[data-modal-accept]').click();
    await expect(staleSigned.page.locator('.toast').last()).toContainText('already has an email');
    await expect(staleDialog.locator('.dp-account-email-value')).toHaveText(`first-${run}@example.com (Not verified)`);
    await staleDialog.locator('[data-modal-cancel]').click();

    // The Admin: the list shows the email alone; Account Details says it is
    // not verified and was entered by the account -- never "Verified".
    const plain = await person(admin, 'Plain');
    await settingsTab(admin.page, 'users');
    const row = admin.page.locator(`tr[data-account-row="${stale.id}"]`);
    await expect(row.locator('td').first().locator('.dp-settings-users-detail')).toHaveText(`first-${run}@example.com`);
    await expect(admin.page.locator('.dp-settings-users-table')).not.toContainText(/\((un)?verified\)|verification not stated/i);
    await expect(admin.page.locator(`tr[data-account-row="${plain.id}"] .dp-settings-users-detail`)).toHaveCount(1); // Sign-In only
    await row.locator('[data-user-action="details"]').click();
    const details = admin.page.locator('.dp-account-details-dialog');
    const shown = details.locator('.dp-account-details-profile dd').nth(2);
    await expect(shown).toHaveText(`first-${run}@example.com (Not verified, entered by the account)`);
    await expect(shown).not.toContainText(/\(Verified\)/);
    await shot(admin.page, 'account-details-self-entered-email');
    await details.locator('[data-modal-cancel]').click();
    await api(admin.page, 'DELETE', `/accounts/${stale.id}`);
    await api(admin.page, 'DELETE', `/accounts/${plain.id}`);
    expect(signed.errors).toEqual([]);
  } finally {
    await api(admin.page, 'DELETE', `/accounts/${user.id}`);
  }
});

test('OIDC Access Control: heading, centred Default Role island and Allow Any on one row; the hint centred over the allowlists', async ({browser}) => {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const admin = await signIn(browser, ADMIN);
  const page = admin.page;
  await settingsTab(page, 'authentication');
  const access = page.locator('#view-settings .dp-settings-oidc-access');
  await access.scrollIntoViewIfNeeded();
  const before = await access.evaluate(node => [...node.querySelectorAll('select, input, textarea')]
    .map(control => [control.dataset.setting || control.id, control.type === 'checkbox' ? control.checked : control.value]));
  await expect(access.locator('.dp-settings-oidc-section-copy')).toHaveText(
    'Choose whether any authenticated OIDC identity is accepted or restrict sign-in to the allowlists below.');

  const measure = () => access.evaluate(node => {
    const box = element => element.getBoundingClientRect();
    const mid = r => ({x: r.left + r.width / 2, y: r.top + r.height / 2});
    const heading = node.querySelector('.dp-settings-oidc-section-heading');
    const title = node.querySelector('.dp-settings-oidc-section-title');
    const island = node.querySelector('.dp-settings-oidc-default-role');
    const toggle = heading.querySelector('.dp-settings-oidc-allow-all');
    const hint = node.querySelector('.dp-settings-oidc-section-copy');
    const lists = node.querySelector('.dp-settings-oidc-allowlists');
    const card = node.closest('.card');
    const style = getComputedStyle(island);
    const overlap = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
    const [t, i, g, h, l, s] = [title, island, toggle, hint, lists, node].map(box);
    return {
      parents: [title, island, toggle].every(element => element.parentElement === heading),
      rowCentres: [mid(t).y, mid(i).y, mid(g).y],
      order: t.right <= i.left && i.right <= g.left,
      islandOffset: Math.abs(mid(i).x - mid(s).x),
      cardOffset: Math.abs(mid(i).x - mid(box(card)).x),
      toggleRight: Math.abs(g.right - s.right),
      bordered: style.borderTopStyle === 'solid' && parseFloat(style.borderTopWidth) >= 1 && style.borderRadius === '12px',
      hintBelowRow: h.top >= Math.max(t.bottom, i.bottom, g.bottom) - 0.5,
      hintAboveLists: h.bottom <= l.top + 0.5,
      hintOffset: Math.abs(mid(h).x - mid(l).x),
      hintAlign: getComputedStyle(hint).textAlign,
      collisions: overlap(t, i) || overlap(i, g) || overlap(t, g),
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1
        || node.scrollWidth > node.clientWidth + 1,
      reachable: [...node.querySelectorAll('select, .toggle, textarea')].every(element => {
        const r = box(element); return r.width > 0 && r.right <= document.documentElement.clientWidth + 0.5;
      }),
    };
  });

  for (const width of [1920, 1440, 1280, 1180]) {
    await page.setViewportSize({width, height: 1000});
    await access.scrollIntoViewIfNeeded();
    const facts = await measure();
    expect(facts.parents).toBe(true);
    const [title, island, toggle] = facts.rowCentres;
    expect(Math.abs(title - island)).toBeLessThanOrEqual(1);
    expect(Math.abs(toggle - island)).toBeLessThanOrEqual(1);
    expect(facts.order).toBe(true);
    expect(facts.islandOffset).toBeLessThanOrEqual(1);        // true centre of the section
    expect(facts.cardOffset).toBeLessThanOrEqual(1);          // ...and of the whole card
    expect(facts.toggleRight).toBeLessThanOrEqual(1);
    expect(facts.bordered).toBe(true);
    expect(facts.hintBelowRow && facts.hintAboveLists).toBe(true);
    expect(facts.hintOffset).toBeLessThanOrEqual(1);
    expect(facts.hintAlign).toBe('center');
    expect(facts.collisions || facts.overflow).toBe(false);
    await shot(page, `oidc-access-control-${width}`);
  }
  for (const width of [1024, 800, 390]) {
    await page.setViewportSize({width, height: 1000});
    await page.evaluate(() => window.closeSidebar?.());
    await access.scrollIntoViewIfNeeded();
    const facts = await measure();
    expect(facts.collisions).toBe(false);
    expect(facts.overflow).toBe(false);
    expect(facts.reachable).toBe(true);
    expect(facts.hintOffset).toBeLessThanOrEqual(1);
    expect(facts.hintAlign).toBe('center');
    await shot(page, `oidc-access-control-${width}`);
  }
  // Nothing was written: every control holds exactly what it held.
  expect(await access.evaluate(node => [...node.querySelectorAll('select, input, textarea')]
    .map(control => [control.dataset.setting || control.id, control.type === 'checkbox' ? control.checked : control.value])))
    .toEqual(before);
  expect(admin.errors).toEqual([]);
});
