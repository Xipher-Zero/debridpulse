const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Users & Access: one Edit entry per person, and the one Edit User
 * dialog -- role and local password together, lifecycle in its footer, the
 * Disable/Remove impact confirmation as a view of the same dialog.
 *
 * The account API is ISOLATED: every /api/accounts* and /api/requests call is
 * answered by an in-test directory, so nothing here touches a real account,
 * and every request the page sends is recorded and asserted exactly. The
 * passwords are synthetic test values. The signed-in account is the open
 * runtime's own (its authority); the directory gives the protected
 * Administrator a separate row so its Authentication hint is rendered. */

const SELF = '00000000-0000-4000-8000-000000000001';
const SECRET = 'synthetic-pass-1';

function person(id, name, extra = {}) {
  return {id, role: 'user', origin: 'local', display_name: name, actual_name: '', email: '', email_verified: false,
    email_verified_known: false, email_self_entered: false, enabled: true, protected: null, generation: 1,
    merged_into: null, deleted_at: null, username: name.toLowerCase(), has_password: true, oidc_identities: 0,
    active_tokens: 0, ...extra};
}

function directory() {
  return [
    person(SELF, 'Operator', {role: 'admin'}),
    person('acct-admin', 'Administrator', {role: 'admin', protected: 'default_admin', username: 'admin'}),
    person('acct-ann', 'Ann', {email: 'ann@example.test'}),
    person('acct-bob', 'Bob', {role: 'readonly', username: '', has_password: false, origin: 'oidc', oidc_identities: 1}),
    person('acct-cid', 'Cid', {enabled: false}),
    person('acct-bot', 'Automation', {role: 'integration', origin: 'integration', protected: 'default_integration',
      username: '', has_password: false}),
  ];
}

/* The isolated account API. `answer` may replace any write's answer:
 * {status, detail} refuses it, {abort: true} leaves it unanswered. `listing`
 * may refuse a directory read the same way ({status, detail}). */
async function isolate(page, {answer = () => null, listing = () => null, impact = {outstanding_requests: 3, shared_transfers_protected: 1,
  completed_unaffected: 2, bounded: false}} = {}) {
  const accounts = directory();
  const sent = [];
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route('**/api/requests?**', route => route.fulfill({json: {requests: []}}));
  await page.route(/\/api\/accounts(\/.*)?(\?.*)?$/, async route => {
    const request = route.request();
    const method = request.method();
    const path = new URL(request.url()).pathname.replace(/^\/api/, '');
    const body = request.postData() ? JSON.parse(request.postData()) : undefined;
    if (method === 'GET' && path === '/accounts') {
      const refused = listing();
      if (refused?.status) return route.fulfill({status: refused.status, json: {detail: refused.detail}});
      return route.fulfill({json: {accounts}});
    }
    if (method === 'GET' && /\/tokens$/.test(path)) return route.fulfill({json: {tokens: []}});
    sent.push({method, path, body});
    if (method === 'GET' && /\/impact$/.test(path)) {
      const custom = answer({method, path, body});
      if (custom?.status) return route.fulfill({status: custom.status, json: {detail: custom.detail}});
      return route.fulfill({json: impact});
    }
    const custom = answer({method, path, body});
    if (custom?.abort) return route.abort('failed');
    if (custom?.status) return route.fulfill({status: custom.status, json: {detail: custom.detail}});
    const id = decodeURIComponent(path.split('/')[2] || '');
    const account = accounts.find(item => item.id === id);
    if (method === 'PATCH') Object.assign(account, body);
    if (method === 'POST' && path.endsWith('/password')) Object.assign(account, {has_password: true, username: body.username || account.username});
    if (method === 'PUT' && path.endsWith('/username')) Object.assign(account, {username: body.username});
    if (method === 'DELETE') accounts.splice(accounts.indexOf(account), 1);
    return route.fulfill({json: {ok: true, withdrawn_transfers: [], cancelled_transfers: []}});
  });
  return {accounts, sent};
}

async function openUsers(page) {
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await page.locator('#view-settings [data-tab="users"]').click();
  await expect(page.locator('.dp-settings-users-table')).toBeVisible();
}

// Geometry is read once the page's own motion has finished.
const settled = page => page.waitForFunction(() => document.getAnimations()
  .every(animation => animation.playState !== 'running' || animation.effect?.getTiming().iterations === Infinity));

// How far a control's right edge (or a text's) sits from its cell's content edge, in one read.
const rightGap = (locator, text = false) => locator.evaluate((node, text) => {
  const cell = node.closest('td');
  let edge = node.getBoundingClientRect().right;
  if (text) { const range = document.createRange(); range.selectNodeContents(node); edge = range.getBoundingClientRect().right; }
  return Math.abs(cell.getBoundingClientRect().right - parseFloat(getComputedStyle(cell).paddingRight) - edge);
}, text);

const row = (page, id) => page.locator(`tr[data-account-row="${id}"]`);
const editor = page => page.locator('.dp-modal-dialog.dp-users-edit-dialog');

async function edit(page, id) {
  await row(page, id).locator('[data-user-action="edit"]').click();
  await expect(editor(page)).toBeVisible();
  return editor(page);
}

async function chooseRole(page, label) {
  await editor(page).locator('.dp-dropdown__trigger').click();
  await page.locator('.dp-dropdown-menu .dp-dropdown__option').filter({hasText: new RegExp(`^${label}$`)}).click();
}

const writes = sent => sent.filter(entry => entry.method !== 'GET');

test('each person has one right-aligned Edit; You and the protected Administrator keep their words', async ({page}) => {
  await isolate(page);
  await openUsers(page);
  await settled(page);
  for (const id of ['acct-ann', 'acct-bob', 'acct-cid']) {
    const actions = row(page, id).locator('.dp-settings-users-actions');
    await expect(actions.locator('button')).toHaveText(['Edit']);
    expect(await rightGap(actions.locator('button')), `${id}: Edit is not at the cell's right`).toBeLessThanOrEqual(1);
    // One control: the row is no taller than its name and email need.
    const height = await row(page, id).evaluate(node => node.getBoundingClientRect().height);
    expect(height, `${id}: row height`).toBeLessThanOrEqual(64);
  }
  await expect(row(page, SELF).locator('.dp-settings-users-actions')).toHaveText('You');
  await expect(row(page, SELF).locator('[data-user-action="edit"]')).toHaveCount(0);
  const hint = row(page, 'acct-admin').locator('.dp-settings-users-hint');
  await expect(hint).toHaveText('Password managed in Authentication');
  await expect(hint).toHaveCSS('text-align', 'right');
  expect(await rightGap(hint, true), 'the hint is not right-aligned').toBeLessThanOrEqual(1);
  await expect(row(page, 'acct-admin').locator('.dp-settings-users-actions button')).toHaveCount(0);
  // The name still opens Account Details.
  await row(page, 'acct-ann').locator('[data-user-action="details"]').click();
  await expect(page.locator('.dp-modal-dialog.dp-account-details-dialog .dp-modal-title')).toHaveText('Account Details: Ann');
});

test('role only: the current role and its description; Cancel sends nothing; Apply sends the role alone', async ({page}) => {
  const {sent} = await isolate(page);
  await openUsers(page);
  let dialog = await edit(page, 'acct-ann');
  await expect(dialog.locator('.dp-modal-title')).toHaveText('Edit User: Ann');
  await expect(dialog.locator('.dp-users-edit-identity')).toHaveText('ann@example.test · User · Enabled');
  await expect(dialog.locator('.dp-dropdown__value')).toHaveText('User');
  await expect(dialog.locator('[data-field-description="role"]'))
    .toHaveText('Adds and operates downloads, and can delete or redirect a transfer only when no other account still needs it.');
  await expect(dialog.locator('.dp-users-choice-description [data-settings-help="user-accounts"]')).toHaveCount(1);
  await expect(dialog).toContainText('The new role applies to the account’s next request. Its open browsers reload with the new role; it is not signed out.');
  const accept = dialog.locator('[data-modal-accept]');
  await expect(accept).toHaveText('Apply');
  await expect(accept).toBeDisabled();                                    // nothing changed yet
  await chooseRole(page, 'ReadOnly User');
  await expect(dialog.locator('[data-field-description="role"]')).toHaveText('Views downloads and Statistics, but cannot add, change, or remove anything.');
  await expect(accept).toBeEnabled();
  await dialog.locator('[data-modal-cancel]').click();                     // Cancel discards
  await expect(editor(page)).toHaveCount(0);
  expect(writes(sent)).toEqual([]);

  dialog = await edit(page, 'acct-ann');
  await expect(dialog.locator('.dp-dropdown__value')).toHaveText('User');  // the draft did not survive Cancel
  await chooseRole(page, 'ReadOnly User');
  await accept.click();
  await expect(editor(page)).toHaveCount(0);
  expect(writes(sent)).toEqual([{method: 'PATCH', path: '/accounts/acct-ann', body: {role: 'readonly'}}]);
  await expect(row(page, 'acct-ann')).toContainText('ReadOnly User');
  await expect(row(page, 'acct-ann').locator('[data-user-action="edit"]')).toBeFocused();
});

test('password only: two empty fields, a live agreement mark, mismatch blocks Apply, one password request', async ({page}) => {
  const {sent} = await isolate(page);
  await openUsers(page);
  const dialog = await edit(page, 'acct-ann');
  const next = dialog.locator('[data-edit-field="password"]');
  const confirm = dialog.locator('[data-edit-field="confirm"]');
  const check = dialog.locator('.dp-users-password-check');
  const status = dialog.locator('.dp-users-password-status');
  const accept = dialog.locator('[data-modal-accept]');
  await expect(dialog.locator('.dp-users-edit-heading').nth(1)).toHaveText('Reset Local Password');
  await expect(dialog.locator('[data-edit-field="username"]')).toHaveCount(0);
  for (const field of [next, confirm]) {
    await expect(field).toHaveValue('');
    await expect(field).toHaveAttribute('type', 'password');
    await expect(field).toHaveAttribute('autocomplete', 'new-password');
  }
  await expect(dialog).toContainText('Leave both password fields blank to keep the existing password.');
  await expect(dialog).toContainText('The account is signed out everywhere and signs in again with this password.');

  await next.fill(SECRET);                                                  // one field: incomplete
  await expect(check).toBeHidden();
  await expect(status).toHaveText('Enter the new password in both fields.');
  await expect(accept).toBeDisabled();
  await confirm.fill('synthetic-pass-');                                    // started and different
  await expect(status).toHaveText('Passwords do not match');
  await expect(confirm).toHaveAttribute('aria-invalid', 'true');
  await expect(check).toBeHidden();
  await expect(accept).toBeDisabled();
  await confirm.fill(SECRET);                                               // agree
  await expect(check).toBeVisible();
  await expect(status).toHaveText('Passwords match');
  await expect(confirm).toHaveAttribute('aria-invalid', 'false');
  await expect(accept).toBeEnabled();
  const box = await confirm.boundingBox(), mark = await check.boundingBox();
  expect(mark.x >= box.x && mark.x + mark.width <= box.x + box.width, 'the mark is inside the field').toBe(true);
  await next.press('End');
  await next.press('Backspace');                                            // edited after the match
  await expect(check).toBeHidden();
  await expect(status).toHaveText('Passwords do not match');
  await expect(accept).toBeDisabled();
  await next.pressSequentially('1');
  await expect(check).toBeVisible();
  await confirm.press('Enter');                                             // Enter applies
  await expect(editor(page)).toHaveCount(0);
  expect(writes(sent)).toEqual([{method: 'POST', path: '/accounts/acct-ann/password', body: {password: SECRET}}]);
  expect(page.url()).not.toContain(SECRET);
});

const usernameRow = dialog => dialog.locator('.dp-users-username');

async function pickUsername(page, value, {back = false} = {}) {
  const dialog = editor(page);
  await dialog.getByRole('button', {name: 'Edit Username', exact: true}).click();
  await expect(dialog.locator('.dp-modal-title')).toHaveText('Edit Username');
  const input = dialog.locator('[data-username-input]');
  await expect(input).toBeFocused();
  await input.fill(value);
  if (back) await dialog.locator('[data-modal-cancel]').click();
  else await input.press('Enter');                                          // Done
  await expect(dialog.locator('.dp-modal-title')).not.toHaveText('Edit Username');
}

test('the username is shown with its pencil; a new username alone keeps the password', async ({page}) => {
  const {sent} = await isolate(page);
  await openUsers(page);
  const dialog = await edit(page, 'acct-ann');
  const value = dialog.locator('[data-username-value]');
  const draft = dialog.locator('[data-username-draft]');
  const pencil = dialog.getByRole('button', {name: 'Edit Username', exact: true});
  await expect(value).toHaveText('ann');
  await expect(pencil).toHaveAttribute('data-tooltip', 'Edit Username');
  await expect(pencil.locator('img[src="/icons/lucide/pencil.svg"]')).toHaveCount(1);
  await expect(dialog.locator('[data-edit-view="edit"] input[type="text"]')).toHaveCount(0);   // no username textbox
  await expect(draft).toBeHidden();
  const [shown, button] = await Promise.all([value.boundingBox(), pencil.boundingBox()]);
  expect(button.x - (shown.x + shown.width)).toBeLessThan(12);              // immediately beside the value

  await pickUsername(page, 'annie', {back: true});                          // Back: unchanged
  await expect(value).toHaveText('ann');
  await expect(pencil).toBeFocused();
  await expect(dialog.locator('[data-modal-accept]')).toBeDisabled();
  await pickUsername(page, '  annie ');                                      // Done: a draft, not a write
  await expect(value).toHaveText('annie');
  await expect(draft).toHaveText('Not yet applied');
  await expect(pencil).toBeFocused();
  expect(writes(sent)).toEqual([]);
  await expect(dialog).toContainText('A new username alone keeps the password, and also signs the account out everywhere.');
  await dialog.locator('[data-modal-accept]').click();
  await expect(editor(page)).toHaveCount(0);
  expect(writes(sent)).toEqual([{method: 'PUT', path: '/accounts/acct-ann/username', body: {username: 'annie'}}]);
  await expect(row(page, 'acct-ann')).toContainText('Password (annie)');
});

test('no local credential: "Not set", and the username travels with the first password, never alone', async ({page}) => {
  let refuse = true;
  const {sent} = await isolate(page, {answer: ({path}) => (path.endsWith('/password') && refuse
    ? {status: 409, detail: 'That username belongs to another account'} : null)});
  await openUsers(page);
  const dialog = await edit(page, 'acct-bob');
  const accept = dialog.locator('[data-modal-accept]');
  await expect(dialog.locator('.dp-users-edit-heading').nth(1)).toHaveText('Set Local Password');
  await expect(dialog.locator('[data-username-value]')).toHaveText('Not set');
  await expect(dialog).toContainText('A local password needs a username: set both here, and they are saved together.');
  await dialog.locator('[data-edit-field="password"]').fill(SECRET);
  await dialog.locator('[data-edit-field="confirm"]').fill(SECRET);
  await expect(dialog.locator('.dp-users-password-check')).toBeVisible();           // the two agree
  await expect(dialog.locator('.dp-users-password-status')).toHaveText('Passwords match. Set a username too: a first password needs one.');
  await expect(accept).toBeDisabled();
  await dialog.locator('[data-edit-field="password"]').fill('');
  await dialog.locator('[data-edit-field="confirm"]').fill('');
  await pickUsername(page, 'ann');
  await expect(dialog.locator('.dp-users-password-status')).toHaveText('Enter the new password in both fields.');
  await expect(accept).toBeDisabled();                                       // a username alone sets nothing
  await dialog.locator('[data-edit-field="password"]').fill(SECRET);
  await dialog.locator('[data-edit-field="confirm"]').fill(SECRET);
  await accept.click();
  await expect(dialog.locator('.dp-users-edit-outcome')).toHaveText('Password not saved: That username belongs to another account');
  refuse = false;
  await pickUsername(page, 'bob');
  await accept.click();
  await expect(editor(page)).toHaveCount(0);
  expect(writes(sent)).toEqual([
    {method: 'POST', path: '/accounts/acct-bob/password', body: {password: SECRET, username: 'ann'}},
    {method: 'POST', path: '/accounts/acct-bob/password', body: {password: SECRET, username: 'bob'}},
  ]);
  await expect(row(page, 'acct-bob')).toContainText('Password (bob)');
});

test('a new username with a new password is one password request; a refused rename after an acknowledged role resends neither', async ({page}) => {
  let refuseRename = true;
  let failNextListing = false;
  const {sent} = await isolate(page, {
    answer: ({path}) => {
      if (!path.endsWith('/username') || !refuseRename) return null;
      failNextListing = true;
      return {status: 409, detail: 'That username belongs to another account'};
    },
    listing: () => (failNextListing ? (failNextListing = false, {status: 503, detail: 'Storage is unavailable'}) : null),
  });
  await openUsers(page);
  let dialog = await edit(page, 'acct-ann');
  await chooseRole(page, 'Admin');
  await pickUsername(page, 'bob');
  await dialog.locator('[data-modal-accept]').click();
  await expect(dialog.locator('.dp-users-edit-outcome')).toHaveText('Role changed to Admin. Username not changed: That username belongs to another account');
  await expect(dialog.locator('.dp-users-edit-identity')).toHaveText('ann@example.test · Admin · Enabled');
  await expect(dialog.locator('.dp-users-edit-stale')).toBeVisible();
  await expect(dialog.locator('[data-username-value]')).toHaveText('bob');        // still the unapplied draft
  await expect(dialog.locator('[data-username-draft]')).toBeVisible();
  refuseRename = false;
  await pickUsername(page, 'annie');
  await dialog.locator('[data-modal-accept]').click();
  await expect(editor(page)).toHaveCount(0);
  expect(writes(sent)).toEqual([
    {method: 'PATCH', path: '/accounts/acct-ann', body: {role: 'admin'}},         // exactly once
    {method: 'PUT', path: '/accounts/acct-ann/username', body: {username: 'bob'}},
    {method: 'PUT', path: '/accounts/acct-ann/username', body: {username: 'annie'}},
  ]);
  // A new username and a new password together: one request, never a separate rename.
  dialog = await edit(page, 'acct-cid');
  await pickUsername(page, 'cyd');
  await dialog.locator('[data-edit-field="password"]').fill(SECRET);
  await dialog.locator('[data-edit-field="confirm"]').fill(SECRET);
  await dialog.locator('[data-modal-accept]').click();
  await expect(editor(page)).toHaveCount(0);
  expect(writes(sent).slice(3)).toEqual([{method: 'POST', path: '/accounts/acct-cid/password', body: {password: SECRET, username: 'cyd'}}]);
});

test('role and password: role first, one request each; a refused password is reported alone and the role is not resent', async ({page}) => {
  let refuse = true;
  const {sent} = await isolate(page, {answer: ({path}) => (path.endsWith('/password') && refuse
    ? {status: 409, detail: 'A password must be at least 8 characters'} : null)});
  await openUsers(page);
  const dialog = await edit(page, 'acct-ann');
  await chooseRole(page, 'Admin');
  await dialog.locator('[data-edit-field="password"]').fill('short');
  await dialog.locator('[data-edit-field="confirm"]').fill('short');
  await dialog.locator('[data-modal-accept]').click();
  const outcome = dialog.locator('.dp-users-edit-outcome');
  await expect(outcome).toHaveText('Role changed to Admin. Password not saved: A password must be at least 8 characters');
  await expect(dialog.locator('.dp-users-edit-identity')).toHaveText('ann@example.test · Admin · Enabled');  // re-read
  await expect(row(page, 'acct-ann')).toContainText('Admin');
  expect(writes(sent)).toEqual([
    {method: 'PATCH', path: '/accounts/acct-ann', body: {role: 'admin'}},
    {method: 'POST', path: '/accounts/acct-ann/password', body: {password: 'short'}},
  ]);
  refuse = false;
  await dialog.locator('[data-edit-field="password"]').fill(SECRET);
  await dialog.locator('[data-edit-field="confirm"]').fill(SECRET);
  await dialog.locator('[data-modal-accept]').click();
  await expect(editor(page)).toHaveCount(0);
  expect(writes(sent).slice(2)).toEqual([{method: 'POST', path: '/accounts/acct-ann/password', body: {password: SECRET}}]);
});

test('an acknowledged role is never resent when the re-read fails: only the corrected password follows', async ({page}) => {
  let refusePassword = true;
  let failNextListing = false;
  const {sent} = await isolate(page, {
    answer: ({path}) => {
      if (!path.endsWith('/password') || !refusePassword) return null;
      failNextListing = true;                                   // the read right after this refusal fails
      return {status: 409, detail: 'A password must be at least 8 characters'};
    },
    listing: () => (failNextListing ? (failNextListing = false, {status: 503, detail: 'Storage is unavailable'}) : null),
  });
  await openUsers(page);
  const dialog = await edit(page, 'acct-ann');
  await chooseRole(page, 'Admin');
  await dialog.locator('[data-edit-field="password"]').fill('short');
  await dialog.locator('[data-edit-field="confirm"]').fill('short');
  await dialog.locator('[data-modal-accept]').click();
  await expect(dialog.locator('.dp-users-edit-outcome'))
    .toHaveText('Role changed to Admin. Password not saved: A password must be at least 8 characters');
  // The role is the server's acknowledged one; the directory itself was not re-read, and says so.
  await expect(dialog.locator('.dp-users-edit-identity')).toHaveText('ann@example.test · Admin · Enabled');
  await expect(dialog.locator('.dp-users-edit-stale')).toBeVisible();
  await expect(page.locator('.dp-settings-load-error')).toContainText('Accounts could not be loaded.');
  refusePassword = false;
  await dialog.locator('[data-edit-field="password"]').fill(SECRET);
  await dialog.locator('[data-edit-field="confirm"]').fill(SECRET);
  await dialog.locator('[data-modal-accept]').click();
  await expect(editor(page)).toHaveCount(0);
  expect(writes(sent)).toEqual([
    {method: 'PATCH', path: '/accounts/acct-ann', body: {role: 'admin'}},          // exactly once
    {method: 'POST', path: '/accounts/acct-ann/password', body: {password: 'short'}},
    {method: 'POST', path: '/accounts/acct-ann/password', body: {password: SECRET}},
  ]);
  await expect(row(page, 'acct-ann')).toContainText('Admin');                      // re-read once it can be
});

test('an acknowledged Enable or Disable is shown as such when the re-read fails', async ({page}) => {
  let failListing = false;
  const {sent} = await isolate(page, {listing: () => (failListing ? {status: 503, detail: 'Storage is unavailable'} : null)});
  await openUsers(page);
  const dialog = await edit(page, 'acct-cid');
  const footer = dialog.locator('.dp-modal-footer .btn:visible');
  failListing = true;
  await dialog.getByRole('button', {name: 'Enable', exact: true}).click();
  await expect(dialog.locator('.dp-users-edit-outcome')).toHaveText('Account enabled.');
  await expect(dialog.locator('.dp-users-edit-identity')).toHaveText('User · Enabled');
  await expect(dialog.locator('.dp-users-edit-stale')).toBeVisible();
  await expect(footer).toHaveText(['Disable', 'Remove', 'Cancel', 'Apply']);
  await dialog.getByRole('button', {name: 'Disable', exact: true}).click();
  await dialog.locator('[data-modal-accept]').click();
  await expect(dialog.locator('.dp-users-edit-outcome')).toHaveText('Account disabled. It had no outstanding requests.');
  await expect(dialog.locator('.dp-users-edit-identity')).toHaveText('User · Disabled');
  await expect(footer).toHaveText(['Enable', 'Remove', 'Cancel', 'Apply']);
  expect(writes(sent).filter(entry => entry.method === 'PATCH')).toEqual([
    {method: 'PATCH', path: '/accounts/acct-cid', body: {enabled: true}},
    {method: 'PATCH', path: '/accounts/acct-cid', body: {enabled: false}},
  ]);
});

test('an unanswered password write is never resent: the fields clear and the outcome says it is unknown', async ({page}) => {
  const {sent} = await isolate(page, {answer: ({path}) => (path.endsWith('/password') ? {abort: true} : null)});
  await openUsers(page);
  const dialog = await edit(page, 'acct-ann');
  await dialog.locator('[data-edit-field="password"]').fill(SECRET);
  await dialog.locator('[data-edit-field="confirm"]').fill(SECRET);
  await dialog.locator('[data-modal-accept]').click();
  await expect(dialog.locator('.dp-users-edit-outcome')).toHaveText(
    'The password request got no response, so the password may or may not have changed. It is not sent again; enter it again to set it.');
  await expect(dialog.locator('[data-edit-field="password"]')).toHaveValue('');
  await expect(dialog.locator('[data-edit-field="confirm"]')).toHaveValue('');
  await expect(dialog.locator('[data-modal-accept]')).toBeDisabled();
  expect(writes(sent)).toHaveLength(1);
});

test('Disable and Remove confirm in the same dialog: server figures, Back keeps the draft, only the act is sent', async ({page}) => {
  const {sent} = await isolate(page);
  await openUsers(page);
  const dialog = await edit(page, 'acct-ann');
  const instance = await dialog.elementHandle();
  await chooseRole(page, 'Admin');
  await dialog.locator('[data-edit-field="password"]').fill(SECRET);
  const footer = dialog.locator('.dp-modal-footer .btn:visible');
  await expect(footer).toHaveText(['Disable', 'Remove', 'Cancel', 'Apply']);
  await dialog.getByRole('button', {name: 'Disable', exact: true}).click();
  await expect(dialog.locator('.dp-modal-title')).toHaveText('Disable Ann?');
  expect(await instance.evaluate(node => node.isConnected)).toBe(true);
  await expect(page.locator('.dp-modal-dialog')).toHaveCount(1);             // one physical dialog
  await expect(dialog.locator('.dp-account-impact-lead'))
    .toHaveText('Disabling this account will revoke its access and withdraw its outstanding download requests.');
  await expect(dialog.locator('.dp-account-impact-figures dd')).toHaveText(['3', '1', '2']);
  await expect(dialog.locator('.dp-account-impact-qualifier'))
    .toHaveText('These figures reflect the current account and transfer state. They may change before confirmation.');
  await expect(footer).toHaveText(['Back', 'Disable account']);
  await expect(dialog.locator('[data-modal-accept]')).toHaveClass(/\bbtn-warn\b/);
  await page.keyboard.press('Escape');                                       // Escape is Back here
  await expect(dialog.locator('.dp-modal-title')).toHaveText('Edit User: Ann');
  await expect(dialog.locator('.dp-dropdown__value')).toHaveText('Admin');
  await expect(dialog.locator('[data-edit-field="password"]')).toHaveValue(SECRET);
  expect(writes(sent)).toEqual([]);

  await dialog.getByRole('button', {name: 'Disable', exact: true}).click();
  await dialog.locator('[data-modal-accept]').click();
  await expect(dialog.locator('.dp-modal-title')).toHaveText('Edit User: Ann');
  await expect(dialog.locator('.dp-users-edit-outcome')).toHaveText('Account disabled. It had no outstanding requests.');
  await expect(dialog.locator('.dp-users-edit-identity')).toHaveText('ann@example.test · User · Disabled');
  await expect(footer).toHaveText(['Enable', 'Remove', 'Cancel', 'Apply']);
  await expect(dialog.locator('[data-edit-field="password"]')).toHaveValue(SECRET);   // the draft is still only a draft
  expect(writes(sent)).toEqual([{method: 'PATCH', path: '/accounts/acct-ann', body: {enabled: false}}]);

  await dialog.getByRole('button', {name: 'Remove', exact: true}).click();
  await expect(dialog.locator('.dp-modal-title')).toHaveText('Remove Ann?');
  await expect(dialog.locator('[data-modal-accept]')).toHaveText('Remove account');
  await expect(dialog.locator('[data-modal-accept]')).toHaveClass(/\bbtn-danger\b/);
  await expect(dialog.locator('.dp-account-impact-note')).toHaveText('Transfers other accounts still need continue, and completed downloads are not undone. '
    + 'The account’s history and the provenance of its past requests are retained.');
  await dialog.locator('[data-modal-accept]').click();
  await expect(editor(page)).toHaveCount(0);
  await expect(row(page, 'acct-ann')).toHaveCount(0);
  expect(writes(sent).slice(1)).toEqual([{method: 'DELETE', path: '/accounts/acct-ann', body: undefined}]);
});

test('an impact that cannot be read changes nothing and offers no confirmation', async ({page}) => {
  const {sent} = await isolate(page, {answer: ({path}) => (path.endsWith('/impact') ? {status: 503, detail: 'Storage is unavailable'} : null)});
  await openUsers(page);
  const dialog = await edit(page, 'acct-ann');
  await dialog.getByRole('button', {name: 'Remove', exact: true}).click();
  await expect(dialog.locator('.dp-users-edit-outcome'))
    .toHaveText("The account's impact could not be read, so nothing was changed: Storage is unavailable");
  await expect(dialog.locator('.dp-modal-title')).toHaveText('Edit User: Ann');
  await expect(dialog.locator('.dp-account-impact-figures')).toHaveCount(0);
  expect(writes(sent)).toEqual([]);
});

test('a disabled person is edited and enabled directly, without a confirmation or the draft', async ({page}) => {
  const {sent} = await isolate(page);
  await openUsers(page);
  const dialog = await edit(page, 'acct-cid');
  await expect(dialog.locator('.dp-users-edit-identity')).toHaveText('User · Disabled');
  const footer = dialog.locator('.dp-modal-footer .btn:visible');
  await expect(footer).toHaveText(['Enable', 'Remove', 'Cancel', 'Apply']);
  await chooseRole(page, 'ReadOnly User');
  await dialog.getByRole('button', {name: 'Enable', exact: true}).click();
  await expect(dialog.locator('.dp-users-edit-outcome')).toHaveText('Account enabled.');
  await expect(dialog.locator('.dp-account-impact-figures')).toHaveCount(0);
  await expect(footer).toHaveText(['Disable', 'Remove', 'Cancel', 'Apply']);
  await expect(dialog.locator('.dp-dropdown__value')).toHaveText('ReadOnly User');      // still a draft
  await expect(row(page, 'acct-cid')).toContainText('Enabled');
  expect(writes(sent)).toEqual([{method: 'PATCH', path: '/accounts/acct-cid', body: {enabled: true}}]);
});

test('the footer: lifecycle at the start, Cancel and Apply at the end; keyboard and narrow layout', async ({page}) => {
  await isolate(page);
  await openUsers(page);
  const dialog = await edit(page, 'acct-ann');
  await settled(page);
  const box = name => dialog.getByRole('button', {name, exact: true}).boundingBox();
  const [disable, remove, cancel, apply, frame] = await Promise.all([box('Disable'), box('Remove'), box('Cancel'), box('Apply'),
    dialog.locator('.dp-modal-footer').boundingBox()]);
  expect(disable.x - frame.x).toBeLessThan(40);                             // far start
  const together = remove.x - (disable.x + disable.width);
  expect(together).toBeLessThan(16);                                         // together
  expect(cancel.x - (remove.x + remove.width)).toBeGreaterThan(3 * together); // separated
  expect(frame.x + frame.width - (apply.x + apply.width)).toBeLessThan(40);  // far end
  await expect(dialog.locator('.dp-dropdown__trigger')).toBeFocused();       // the editor starts at its first field
  await page.keyboard.press('Escape');
  await expect(editor(page)).toHaveCount(0);
  await expect(row(page, 'acct-ann').locator('[data-user-action="edit"]')).toBeFocused();

  await page.setViewportSize({width: 390, height: 844});
  const narrow = await edit(page, 'acct-ann');
  const fit = await narrow.evaluate(node => ({width: node.getBoundingClientRect().width, overflow: node.scrollWidth - node.clientWidth}));
  expect(fit.width).toBeLessThanOrEqual(390);
  expect(fit.overflow).toBe(0);
  await expect(narrow.locator('.dp-modal-footer .btn:visible')).toHaveCount(4);
});
