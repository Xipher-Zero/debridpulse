const { test, expect } = require('@playwright/test');

/* 1.0.13 ACL live qualification, Batch 1: account administration and the
 * session row. Real sessions on the password-auth runtime; every account is a
 * synthetic fixture this file creates under a run-unique name and removes. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const ADMIN = {username: 'browser', password: 'browser-secret'};
const run = Date.now().toString(36);

// The Admin signs in once per worker (acl-admin-session.js: the login
// challenge limit is shared by every auth-runtime file).
const adminSession = require('./acl-admin-session');
async function signIn(browser, {username, password}, viewport = {width: 1440, height: 914}, {fresh = false} = {}) {
  const shared = username === ADMIN.username && !fresh;
  const reuse = shared && adminSession.get() !== null;
  const context = await browser.newContext({baseURL: AUTH, viewport, ...(reuse ? {storageState: adminSession.get()} : {})});
  if (!shared) signedIn.push(context);
  const page = await context.newPage();
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  const errors = [];
  page.on('pageerror', error => errors.push(String(error.message)));
  if (reuse) {
    await page.goto('/');
  } else {
    await page.goto('/login');
    await page.locator('#username').fill(username);
    await page.locator('#password').fill(password);
    await page.locator('button[type="submit"]').click();
  }
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.locator('#sidebar-auth-row .dp-sidebar-profile')).toBeVisible();
  if (shared && !reuse) adminSession.remember(await context.storageState());
  return {context, page, errors};
}

// Every account page a test signs in (never the shared Admin): closed before
// those accounts are removed, so no stale page is sent to the login page --
// each one would be one more sign-in challenge against the shared limit.
const signedIn = [];
async function closeSignedIn() {
  await Promise.all(signedIn.splice(0).map(context => context.close()));
}

const api = (page, method, path, body) => page.evaluate(async ([method, path, body]) => {
  const response = await window.debridPulseAuth.fetch(`/api${path}`, {
    method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  return {status: response.status, body: await response.json().catch(() => null)};
}, [method, path, body]);

async function person(admin, name, role = 'user') {
  const account = {username: `b1-${name}-${run}`, password: `b1-${name}-${run}-pass`};
  const created = await api(admin.page, 'POST', '/accounts', {kind: 'local', role, display_name: `${name} ${run}`, ...account});
  expect(created.status).toBe(200);
  return {...account, id: created.body.id, name: `${name} ${run}`};
}

async function usersTab(page) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await page.locator('#view-settings [data-tab="users"]').click();
  await expect(page.locator('.dp-settings-users-table')).toBeVisible();
}

test('the session row holds Log Out and the account as two separate controls, at every width', async ({browser}) => {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  // One session, three widths: sign-in pages are rate limited per client.
  const admin = await signIn(browser, ADMIN, undefined, {fresh: true});  // it logs out
  // The full sidebar: one line, Log Out then the account. The compact rail
  // (780-1559px): both glyphs on the icon rail, the account above and Log
  // Out at the bottom. Below 780px there is no sidebar: the handheld header's
  // account control carries both (mobile-shell-framework.spec.js).
  for (const [width, layout] of [[1600, 'line'], [1440, 'rail'], [1024, 'rail'], [800, 'rail']]) {
    await admin.page.setViewportSize({width, height: 900});
    await admin.page.waitForFunction(() => document.getElementById('sidebar').getAnimations().length === 0);
    const facts = await admin.page.evaluate(() => {
      const row = document.querySelector('#sidebar-auth-row');
      const box = node => node.getBoundingClientRect();
      const logout = row.querySelector('.dp-sidebar-logout');
      const profile = row.querySelector('.dp-sidebar-profile');
      const glyph = node => { const r = box(node.querySelector('.icon')); return r.left + r.width / 2; };
      const middle = node => box(node).top + box(node).height / 2;
      return {
        rows: document.querySelectorAll('#sidebar-account-row').length,
        controls: [...row.querySelectorAll('button')].map(button => button.className),
        rowIsControl: row.getAttribute('role'),
        separate: box(logout).right <= box(profile).left || box(profile).bottom <= box(logout).top,
        inside: box(logout).left >= box(row).left - 0.5 && box(profile).left >= box(row).left - 0.5
          && box(logout).bottom <= box(row).bottom + 0.5,
        layout: Math.abs(middle(logout) - middle(profile)) < 2 ? 'line'
          : box(profile).bottom <= box(logout).top && Math.abs(glyph(logout) - glyph(profile)) < 0.5 ? 'rail' : 'other',
      };
    });
    expect(facts, `${width}px`).toEqual({rows: 0, controls: ['dp-sidebar-logout', 'dp-sidebar-profile'], rowIsControl: null,
      separate: true, inside: true, layout});
  }
  await admin.page.setViewportSize({width: 1600, height: 914});
  // The full sidebar's reflow has ended, its rows included (the row is
  // measured below).
  await admin.page.waitForFunction(() => document.getElementById('sidebar').getAnimations({subtree: true}).length === 0);
  await expect(admin.page.locator('.dp-sidebar-profile .dp-sidebar-account-role')).toHaveText('Admin');
  // The account side opens My Account; it never logs out.
  await admin.page.locator('.dp-sidebar-profile .dp-sidebar-account-name').click();
  await expect(admin.page.locator('.dp-modal-dialog.dp-account-dialog')).toBeVisible();
  await expect(admin.page).not.toHaveURL(/\/login/);
  await admin.page.locator('.dp-account-dialog [data-modal-cancel]').click();
  // Log Out is quiet. While the server has not answered, its label and the
  // row's measure are unchanged, a second press sends nothing, and the account
  // beside it still opens My Account; a refused logout says so and may be
  // pressed again.
  const logout = admin.page.locator('.dp-sidebar-logout');
  const row = () => admin.page.evaluate(() => {
    const node = document.querySelector('#sidebar-auth-row');
    const box = element => { const r = element.getBoundingClientRect(); return [r.left, r.top, r.width, r.height].map(Math.round); };
    const name = node.querySelector('.dp-sidebar-account-name');
    return {row: box(node), logout: box(node.querySelector('.dp-sidebar-logout')),
      profile: box(node.querySelector('.dp-sidebar-profile')), label: node.querySelector('.dp-sidebar-logout').textContent.trim(),
      nameClipped: name.scrollWidth > name.clientWidth + 1};
  });
  const settled = await row();
  expect(settled.label).toBe('Log Out');
  const answers = [];
  await admin.page.route('**/api/auth/logout', route => { answers.push(route); });
  await logout.click();
  await expect.poll(() => answers.length).toBe(1);
  await expect(logout).toHaveAttribute('aria-disabled', 'true');
  expect(await row()).toEqual(settled);
  // A second press reaches the control (a pointer would see it as disabled)
  // and sends nothing.
  await logout.dispatchEvent('click');
  await admin.page.locator('.dp-sidebar-profile').click();
  await expect(admin.page.locator('.dp-modal-dialog.dp-account-dialog')).toBeVisible();
  await admin.page.locator('.dp-account-dialog [data-modal-cancel]').click();
  expect(answers.length).toBe(1);
  await answers[0].fulfill({status: 500, contentType: 'application/json', body: JSON.stringify({detail: 'fixture refusal'})});
  await expect(logout).toHaveAttribute('aria-disabled', 'false');
  await expect(admin.page.locator('.toast').last()).toHaveText('Log Out failed. Try again.');
  await expect(admin.page).not.toHaveURL(/\/login/);
  expect(await row()).toEqual(settled);
  await admin.page.unroute('**/api/auth/logout');
  // The Log Out side, by its label, logs out.
  await admin.page.locator('.dp-sidebar-logout .nav-label').click();
  await expect(admin.page).toHaveURL(/\/login/);
});

test('My Account: one fixed dialog with a profile view and a password view, no scrollbars', async ({browser}) => {
  const admin = await signIn(browser, ADMIN);
  const user = await person(admin, 'Mya');
  try {
    const signed = await signIn(browser, user);
    const page = signed.page;
    await page.locator('#sidebar-auth-row .dp-sidebar-profile').click();
    const dialog = page.locator('.dp-modal-dialog.dp-account-dialog');
    await expect(dialog).toBeVisible();
    // The DP Username, read-only, then Display Name, Actual Name, Email -- in
    // that order; an account with no email may enter one (Batch 2), so the
    // last three are inputs here.
    expect(await dialog.locator('[data-account-view="profile"] .form-label').allTextContents())
      .toEqual(['DP Username', 'Display Name', 'Actual Name', 'Email']);
    await expect(dialog.locator('[data-account-username]')).toHaveText(user.username);
    await expect(dialog.locator('.dp-account-username')).toContainText(
      'Your DebridPulse account username. Used for local password sign-in when configured. Separate from your display name.');
    await expect(dialog.locator('[data-account-view="profile"] input')).toHaveCount(3);
    await expect(dialog.locator('.dp-account-username :is(input, button)')).toHaveCount(0);
    await expect(dialog.locator('[data-field="email"]')).toHaveValue('');
    // Change Password at the bottom left; Cancel then Save at the bottom right.
    const footer = await dialog.evaluate(node => [...node.querySelectorAll('.dp-modal-footer .btn')]
      .filter(button => !button.hidden).map(button => ({text: button.textContent.trim(), left: button.getBoundingClientRect().left})));
    expect(footer.map(button => button.text)).toEqual(['Change Password', 'Cancel', 'Save']);
    const geometry = () => dialog.evaluate(node => {
      const r = node.getBoundingClientRect();
      const scrolls = [node, ...node.querySelectorAll('*')].some(element =>
        element.scrollHeight > element.clientHeight + 1 && getComputedStyle(element).overflowY !== 'visible'
        || element.scrollWidth > element.clientWidth + 1 && getComputedStyle(element).overflowX !== 'visible');
      return {x: r.x, y: r.y, width: r.width, height: r.height, scrolls,
        clipped: [...node.querySelectorAll('input, .btn')].filter(element => !element.hidden && element.getClientRects().length)
          .some(element => element.getBoundingClientRect().bottom > r.bottom + 0.5)};
    });
    const profileGeometry = await geometry();
    expect(profileGeometry.scrolls).toBe(false);
    expect(profileGeometry.clipped).toBe(false);

    // An unsaved profile edit, then Change Password: the same dialog swaps views.
    await dialog.locator('[data-field="actual_name"]').fill('Unsaved Edit');
    const handle = await dialog.elementHandle();
    await dialog.locator('.dp-modal-footer .btn', {hasText: 'Change Password'}).click();
    await expect(dialog.locator('[data-account-view="password"]')).toBeVisible();
    expect(await dialog.locator('[data-account-view="password"] .form-label').allTextContents())
      .toEqual(['Current Password', 'New Password', 'Confirm New Password']);
    expect(await handle.evaluate(node => node.isConnected)).toBe(true);
    await expect(page.locator('.dp-modal-dialog')).toHaveCount(1);
    expect(await geometry()).toEqual(profileGeometry);                     // same bounds, no scrollbars
    await expect(dialog.locator('.dp-modal-footer .btn', {hasText: 'Change Password'})).toBeHidden();

    // Cancel goes back, keeping the profile edit and clearing the secrets.
    await dialog.locator('[data-field="current"]').fill('typed-secret');
    await dialog.locator('[data-modal-cancel]').click();
    await expect(dialog.locator('[data-account-view="profile"]')).toBeVisible();
    await expect(dialog.locator('[data-field="actual_name"]')).toHaveValue('Unsaved Edit');
    await expect(dialog.locator('[data-field="current"]')).toHaveValue('');
    expect((await api(page, 'GET', '/account')).body.actual_name).toBe('');   // nothing was saved

    // A refused password change keeps the password view; a real one signs out.
    await dialog.locator('.dp-modal-footer .btn', {hasText: 'Change Password'}).click();
    await dialog.locator('[data-field="current"]').fill('not-the-password');
    await dialog.locator('[data-field="next"]').fill('b1-new-pass');
    await dialog.locator('[data-field="confirm"]').fill('b1-new-pass');
    await dialog.locator('[data-modal-accept]').click();
    await expect(page.locator('.toast').last()).toContainText('current password is not correct');
    await expect(dialog.locator('[data-account-view="password"]')).toBeVisible();
    await dialog.locator('[data-field="current"]').fill(user.password);
    await dialog.locator('[data-modal-accept]').click();
    await expect(page).toHaveURL(/\/login/);

    // An account with no local password is not offered Change Password. The
    // profile is the server's; only its has_password fact is varied here.
    await admin.page.route('**/api/account', async route => {
      const response = await route.fetch();
      await route.fulfill({response, json: {...await response.json(), has_password: false}});
    });
    await admin.page.locator('#sidebar-auth-row .dp-sidebar-profile').click();
    const plain = admin.page.locator('.dp-modal-dialog.dp-account-dialog');
    await expect(plain).toBeVisible();
    expect(await plain.locator('.dp-modal-footer .btn').allTextContents()).toEqual(['Cancel', 'Save']);
    await plain.locator('[data-modal-cancel]').click();
  } finally {
    await closeSignedIn();
    await api(admin.page, 'DELETE', `/accounts/${user.id}`);
  }
});

test('User Accounts: name opens Account Details, Remove is in Edit User, Merge is card-level', async ({browser}) => {
  const admin = await signIn(browser, ADMIN);
  const ann = await person(admin, 'Ann');
  const bob = await person(admin, 'Bob');
  const cid = await person(admin, 'Cid');
  try {
    const page = admin.page;
    await usersTab(page);
    const row = page.locator(`tr[data-account-row="${ann.id}"]`);
    // No per-row Activity, Merge or Link OIDC; the signed-in account has no Remove.
    for (const action of ['activity', 'merge', 'link']) {
      await expect(page.locator(`.dp-settings-users-table [data-user-action="${action}"]`)).toHaveCount(0);
    }
    await expect(page.locator('.dp-settings-users-table').getByRole('button', {name: 'Link OIDC'})).toHaveCount(0);
    const self = await api(page, 'GET', '/account');
    expect(self.status).toBe(200);
    // Card header: Merge immediately before Add Local Account.
    expect(await page.locator('.dp-settings-users-card:not(.dp-settings-users-integrations-card) > .card-header .dp-settings-header-action .btn')
      .allTextContents()).toEqual(['Merge', 'Add Local Account']);

    // The row's one action is Edit; Remove lives in its footer.
    await expect(row.locator('.dp-settings-users-actions button')).toHaveText(['Edit']);

    // Account Details: profile and Activity, from the account's name.
    await row.locator('[data-user-action="details"]').click();
    const details = page.locator('.dp-modal-dialog.dp-account-details-dialog');
    await expect(details.locator('.dp-account-details-profile dt')).toHaveText(['Display Name', 'Actual Name', 'Email']);
    await expect(details.locator('.dp-account-details-profile dd')).toHaveText([ann.name, 'Not set', 'None provided']);
    await expect(details.locator('.dp-account-details-heading')).toHaveText('Activity');
    await details.locator('[data-modal-cancel]').click();

    // Remove: Edit User's impact confirmation, with the server's figures;
    // Back and Cancel change nothing.
    await row.locator('[data-user-action="edit"]').click();
    const impact = page.locator('.dp-modal-dialog.dp-users-edit-dialog');
    const remove = impact.getByRole('button', {name: 'Remove', exact: true});
    await expect(remove).toHaveClass(/\bbtn-danger\b/);
    await remove.click();
    await expect(impact.locator('.dp-account-impact-lead'))
      .toHaveText('Removing this account will revoke its access and withdraw its outstanding download requests.');
    await expect(impact.locator('.dp-account-impact-figures dd')).toHaveText(['0', '0', '0']);
    await expect(impact.locator('[data-modal-accept]')).toHaveText('Remove account');
    await expect(impact.locator('[data-modal-accept]')).toHaveClass(/\bbtn-danger\b/);
    await impact.locator('[data-modal-cancel]').click();                    // Back
    await impact.locator('[data-modal-cancel]').click();                    // Cancel
    await expect(impact).toHaveCount(0);
    await expect(row).toHaveCount(1);
    await row.locator('[data-user-action="edit"]').click();
    await impact.getByRole('button', {name: 'Remove', exact: true}).click();
    await impact.locator('[data-modal-accept]').click();
    await expect(page.locator(`tr[data-account-row="${ann.id}"]`)).toHaveCount(0);

    // Merge: choose two or more, then -- in the same dialog -- the survivor.
    await page.locator('.dp-settings-users-card .card-header [data-user-action="merge"]').click();
    const merge = page.locator('.dp-modal-dialog.dp-merge-dialog');
    await expect(merge.locator('.dp-modal-title')).toHaveText('Merge Accounts');
    const accept = merge.locator('[data-modal-accept]');
    await expect(accept).toHaveText('Merge');
    await expect(accept).toHaveClass(/\bbtn-danger\b/);
    await expect(accept).toBeDisabled();
    await merge.locator(`[data-merge-account="${bob.id}"]`).check();
    await expect(accept).toBeDisabled();                                   // one is not a merge
    await merge.locator(`[data-merge-account="${cid.id}"]`).check();
    const instance = await merge.elementHandle();
    await accept.click();
    expect(await instance.evaluate(node => node.isConnected)).toBe(true);
    await expect(accept).toHaveText('Confirm Merge');
    await expect(accept).toBeDisabled();                                   // no survivor chosen yet
    await expect(merge.locator('[data-modal-cancel]')).toHaveText('Back');
    await merge.locator('[data-modal-cancel]').click();                    // Back: nothing merged
    await expect(merge.locator(`[data-merge-account="${bob.id}"]`)).toBeChecked();
    expect((await api(page, 'GET', '/accounts')).body.accounts.find(item => item.id === cid.id).merged_into).toBeNull();
    await accept.click();
    await merge.locator(`[data-merge-survivor="${bob.id}"]`).check();
    await expect(merge.locator('.dp-merge-absorbed')).toContainText(cid.name);
    await expect(accept).toBeEnabled();
    // Both are local accounts with passwords: the server refuses the whole
    // merge, the dialog says why, and nothing changed.
    await accept.click();
    await expect(merge.locator('.dp-merge-refusal'))
      .toHaveText('Not merged: More than one of these accounts holds a local credential; remove all but one first');
    expect(await instance.evaluate(node => node.isConnected)).toBe(true);
    const unmerged = (await api(page, 'GET', '/accounts')).body.accounts.filter(item => [bob.id, cid.id].includes(item.id));
    expect(unmerged.map(item => item.merged_into)).toEqual([null, null]);
    await merge.locator('[data-modal-cancel]').click();                    // Back
    await merge.locator('[data-modal-cancel]').click();                    // Cancel
    await expect(merge).toHaveCount(0);
    await expect(page.locator(`tr[data-account-row="${cid.id}"]`)).toHaveCount(1);
    expect(admin.errors).toEqual([]);
  } finally {
    await closeSignedIn();
    for (const item of [ann, bob, cid]) await api(admin.page, 'DELETE', `/accounts/${item.id}`);
  }
});

test('Edit is offered for every other eligible person, never the signed-in Admin or the protected Administrator', async ({browser}) => {
  const admin = await signIn(browser, ADMIN);                              // the protected default Administrator
  const operator = await person(admin, 'Ops', 'admin');                    // a non-protected Admin
  const local = await person(admin, 'Lou');
  try {
    const signed = await signIn(browser, operator);
    const page = signed.page;
    const defaultAdmin = (await api(page, 'GET', '/accounts')).body.accounts.find(item => item.protected === 'default_admin');
    // This runtime has no identity provider to sign an OIDC identity in, so
    // one OIDC-provisioned row -- exactly as a first OIDC sign-in leaves it --
    // is added to the server's own account list; the renderer is what is tested.
    const oidc = {id: `oidc-${run}`, role: 'user', origin: 'oidc', display_name: `Olivia ${run}`, actual_name: '',
      email: '', email_verified: false, email_verified_known: false, enabled: true, protected: null, generation: 1,
      merged_into: null, deleted_at: null, username: `olivia-${run}`, has_password: false, oidc_identities: 1,
      active_tokens: 0};
    await page.route('**/api/accounts', async route => {
      if (route.request().method() !== 'GET') return route.fallback();
      const response = await route.fetch();
      const listed = await response.json();
      await route.fulfill({response, json: {...listed, accounts: [...listed.accounts, oidc]}});
    });
    await usersTab(page);
    const editIn = id => page.locator(`tr[data-account-row="${id}"] [data-user-action="edit"]`);
    await expect(page.locator(`tr[data-account-row="${local.id}"]`)).toHaveCount(1);
    await expect(editIn(operator.id)).toHaveCount(0);                      // never the signed-in account
    await expect(page.locator(`tr[data-account-row="${operator.id}"] .dp-settings-users-self`)).toHaveText('You');
    await expect(editIn(defaultAdmin.id)).toHaveCount(0);                  // never the protected Administrator
    await expect(editIn(local.id)).toHaveCount(1);                         // another local person
    await expect(editIn(oidc.id)).toHaveCount(1);                          // an OIDC person
    // The protected Administrator's password is Authentication's: its row
    // says so instead of offering an Edit.
    const protectedRow = page.locator(`tr[data-account-row="${defaultAdmin.id}"]`);
    await expect(protectedRow.locator('.dp-settings-users-hint')).toHaveText('Password managed in Authentication');
    await expect(protectedRow.locator('.dp-settings-users-hint')).not.toHaveAttribute('role', /.+/);
    await expect(protectedRow.locator('button')).toHaveText([/.+/]);                // only its name opens details
    // Another local person's Edit resets its password; the OIDC person's sets
    // one, under the username its title names beside the pencil.
    await editIn(local.id).click();
    const editor = page.locator('.dp-modal-dialog.dp-users-edit-dialog');
    await expect(editor.locator('.dp-users-edit-heading').nth(1)).toHaveText('Reset Local Password');
    await editor.locator('[data-modal-cancel]').click();
    await editIn(oidc.id).click();
    await expect(editor.locator('.dp-users-edit-heading').nth(1)).toHaveText('Set Local Password');
    await expect(editor.locator('.dp-modal-title')).toHaveText(`Edit User: olivia-${run}`);
    await expect(editor.getByRole('button', {name: 'Edit Username', exact: true})).toBeVisible();
    await editor.locator('[data-modal-cancel]').click();
    await page.locator('#view-settings [data-tab="authentication"]').click();
    await expect(page.locator('.dp-settings-username-password-card [data-setting="auth_username"]')).toBeVisible();
    await page.locator('#view-settings [data-tab="users"]').click();
    // The server's own self-removal refusal is unchanged.
    await page.unroute('**/api/accounts');
    expect((await api(page, 'DELETE', `/accounts/${operator.id}`)).status).toBe(409);
    expect(signed.errors).toEqual([]);
  } finally {
    await closeSignedIn();
    for (const item of [local, operator]) await api(admin.page, 'DELETE', `/accounts/${item.id}`);
  }
});

test('the sidebar names the account by its Display Name, never its Actual Name or sign-in name', async ({browser}) => {
  const admin = await signIn(browser, ADMIN);
  const user = await person(admin, 'Xipher');                             // Display Name differs from its username
  try {
    const context = await browser.newContext({baseURL: AUTH, viewport: {width: 1440, height: 914}});
    const page = await context.newPage();
    await page.route('https://fonts.googleapis.com/**', route =>
      route.fulfill({status: 200, contentType: 'text/css', body: ''}));
    // Hold the profile read: the row must not paint the session's name meanwhile.
    let release;
    const held = new Promise(resolve => { release = resolve; });
    await page.route('**/api/account', async route => { await held; await route.fallback(); });
    await page.goto('/login');
    await page.locator('#username').fill(user.username);
    await page.locator('#password').fill(user.password);
    await page.locator('button[type="submit"]').click();
    const name = page.locator('#sidebar-auth-row .dp-sidebar-account-name');
    await expect(page.locator('#sidebar-auth-row .dp-sidebar-account-role')).toHaveText('User');
    await expect(name).toHaveText('');                                    // never the sign-in name first
    release();
    await expect(name).toHaveText(user.name);
    await page.unroute('**/api/account');

    // An Actual Name never replaces the Display Name.
    expect((await api(page, 'PUT', '/account/actual-name', {actual_name: 'Chris Moore'})).status).toBe(200);
    await page.reload();
    await expect(name).toHaveText(user.name);

    // A Display Name saved in My Account is what the row shows next, signed in still.
    await page.locator('#sidebar-auth-row .dp-sidebar-profile').click();
    await page.locator('.dp-account-dialog [data-field="display_name"]').fill(`Renamed ${run}`);
    const reloaded = page.waitForEvent('load');
    await page.locator('.dp-account-dialog [data-modal-accept]').click();
    await reloaded;
    await expect(name).toHaveText(`Renamed ${run}`);
    await expect(page.locator('#sidebar-auth-row .dp-sidebar-profile')).toHaveAttribute('data-tooltip', `Renamed ${run} (User): My Account`);
    await expect(page.locator('#sidebar-auth-row .dp-sidebar-profile')).not.toHaveAttribute('title');
    await expect(page.locator('#sidebar-auth-row .dp-sidebar-account-role')).toHaveText('User');
    await expect(page).not.toHaveURL(/\/login/);
    // ... and the DP Username is still the one it signs in with.
    await page.locator('#sidebar-auth-row .dp-sidebar-profile').click();
    await expect(page.locator('.dp-account-dialog [data-account-username]')).toHaveText(user.username);
    await expect(page.locator('.dp-account-dialog [data-field="display_name"]')).toHaveValue(`Renamed ${run}`);
    await page.locator('.dp-account-dialog [data-modal-cancel]').click();

    // Only when the profile cannot be read does the session's own name stand in.
    await page.route('**/api/account', route => route.fulfill({status: 500, body: '{}'}));
    await page.reload();
    await expect(name).toHaveText(user.username);
    await context.close();
  } finally {
    await closeSignedIn();
    await api(admin.page, 'DELETE', `/accounts/${user.id}`);
  }
});
