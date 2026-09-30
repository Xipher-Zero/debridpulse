const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Backups manager -- the operator-facing backup lifecycle.
 *
 * `Backups` opens one manager dialog whose single state owner is
 * ui-settings-backup-manager.js. A backup is one restore point (timestamp,
 * contents, size), never loose files; exactly one may be selected with the
 * single-checkmark idiom; Save / Restore need a selection, Add never does, and
 * the destructive Remove appears only on the selected row. Every backend
 * answer here is mocked: this file owns no shared backend state at all.
 */

const BACKUPS = [
  {id: '20260928_223553_bcd036da21ce4b3b94ae9a92192388e2', created_at: '2026-09-28T22:35:53+00:00',
   size_bytes: 19293798, contents: 'DP State'},
  {id: '20260928_075601_0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a0a', created_at: '2026-09-28T07:56:01+00:00',
   size_bytes: 19083673, contents: 'DP State'},
  {id: '20260927_214402_1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b', created_at: '2026-09-27T21:44:02+00:00',
   size_bytes: 18769100, contents: 'DP State'},
];
const ADDED = {id: '20260801_101010_2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c', created_at: '2026-08-01T10:10:10+00:00',
  size_bytes: 1048576, contents: 'DP State'};

async function mockBackups(page, {addResult, restoreResult} = {}) {
  const state = {inventory: BACKUPS.map(item => ({...item})), added: [], removed: [], restored: [], saved: []};
  await page.route('**/api/admin/backups**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (path === '/api/admin/backups' && method === 'GET') {
      return route.fulfill({status: 200, contentType: 'application/json',
        body: JSON.stringify({backups: state.inventory})});
    }
    if (path === '/api/admin/backups' && method === 'POST') {
      state.added.push({bytes: request.postDataBuffer()?.length || 0, type: request.headers()['content-type']});
      const result = addResult || {status: 200, body: {backup: ADDED}};
      if (result.status === 200) state.inventory = [...state.inventory, result.body.backup];
      return route.fulfill({status: result.status, contentType: 'application/json', body: JSON.stringify(result.body)});
    }
    if (path === '/api/admin/backups/restore' && method === 'POST') {
      state.restored.push(request.postDataJSON());
      const result = restoreResult || {status: 200, body: {ok: true}};
      return route.fulfill({status: result.status, contentType: 'application/json', body: JSON.stringify(result.body)});
    }
    const pkg = path.match(/^\/api\/admin\/backups\/([^/]+)\/package$/);
    if (pkg && method === 'GET') {
      state.saved.push(decodeURIComponent(pkg[1]));
      return route.fulfill({status: 200, contentType: 'application/zip',
        headers: {'content-disposition': `attachment; filename="debridpulse-backup-${pkg[1]}.zip"`},
        body: Buffer.from('PK-portable-backup-bytes')});
    }
    const one = path.match(/^\/api\/admin\/backups\/([^/]+)$/);
    if (one && method === 'DELETE') {
      const id = decodeURIComponent(one[1]);
      state.removed.push(id);
      state.inventory = state.inventory.filter(item => item.id !== id);
      return route.fulfill({status: 200, contentType: 'application/json', body: '{"ok":true}'});
    }
    return route.continue();
  });
  return state;
}

async function openManager(page) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await page.locator('#view-settings [data-tab="maintenance"]').click();
  await page.locator('#view-settings .dp-settings-backups-retention-card [data-action="backups"]').click();
  const dialog = page.locator('.dp-modal-dialog.dp-backup-manager-dialog');
  await expect(dialog).toBeVisible();
  return dialog;
}

const rows = dialog => dialog.locator('tbody tr');
const action = (dialog, id) => dialog.locator(`[data-modal-action="${id}"]`);
const selectBox = (dialog, index) => rows(dialog).nth(index).locator('input[type="checkbox"]');

test.beforeEach(async ({page}) => {
  await page.goto('/');
});

test('Backups opens the manager with restore points, never member files', async ({page}) => {
  await mockBackups(page);
  const dialog = await openManager(page);
  await expect(dialog.locator('.dp-modal-title')).toHaveText('Backups');
  await expect(dialog.locator('.dp-backup-instruction')).toHaveText('Select one backup to save, restore, or remove.');
  await expect(rows(dialog)).toHaveCount(3);
  await expect(rows(dialog).first().locator('.dp-backup-contents')).toHaveText('DP State');
  await expect(rows(dialog).first().locator('.dp-backup-when')).toContainText('2026');
  await expect(rows(dialog).first().locator('.dp-backup-size')).toHaveText('18.4 MB');
  const text = await dialog.innerText();
  for (const member of ['debridpulse.db', 'config.json', 'avatar']) expect(text).not.toContain(member);
  // Application terminology only: nothing transport-shaped anywhere.
  for (const word of ['Upload', 'Download', 'Import', 'Export', 'Server', 'Remote']) expect(text).not.toContain(word);
  const footer = await dialog.locator('.dp-modal-footer button').allInnerTexts();
  expect(footer.map(label => label.trim())).toEqual(['Add Backup', 'Save Backup', 'Restore Backup', 'Close']);
  await expect(action(dialog, 'restore')).toHaveClass(/btn-success/);
  await expect(dialog.locator('[data-modal-close]')).toBeVisible();
  await expect(dialog.locator('input[type="radio"]')).toHaveCount(0);
});

test('with no selection only Add is available and no row offers Remove', async ({page}) => {
  await mockBackups(page);
  const dialog = await openManager(page);
  await expect(action(dialog, 'add')).toBeEnabled();
  await expect(action(dialog, 'save')).toBeDisabled();
  await expect(action(dialog, 'restore')).toBeDisabled();
  await expect(dialog.locator('[data-modal-cancel]')).toBeEnabled();
  await expect(dialog.locator('[data-backup-remove]')).toHaveCount(0);
  await expect(dialog.locator('input[type="checkbox"]:checked')).toHaveCount(0);
});

test('one checkmark: selection enables Save and Restore, and Remove follows it to the selected row only',
  async ({page}) => {
    await mockBackups(page);
    const dialog = await openManager(page);
    await selectBox(dialog, 0).check();
    await expect(dialog.locator('input[type="checkbox"]:checked')).toHaveCount(1);
    await expect(action(dialog, 'save')).toBeEnabled();
    await expect(action(dialog, 'restore')).toBeEnabled();
    await expect(dialog.locator('[data-backup-remove]')).toHaveCount(1);
    await expect(rows(dialog).nth(0).locator('[data-backup-remove]')).toHaveText('Remove');
    await expect(dialog.locator('[data-backup-selection]')).toContainText(`Backup ID: ${BACKUPS[0].id}`);
    await expect(dialog.locator('[data-backup-selection]')).toContainText('Selected:');

    // Selecting another row MOVES the single checkmark and the contextual Remove.
    await rows(dialog).nth(2).locator('.dp-backup-when').click();
    await expect(dialog.locator('input[type="checkbox"]:checked')).toHaveCount(1);
    await expect(selectBox(dialog, 2)).toBeChecked();
    await expect(selectBox(dialog, 0)).not.toBeChecked();
    await expect(dialog.locator('[data-backup-remove]')).toHaveCount(1);
    await expect(rows(dialog).nth(2).locator('[data-backup-remove]')).toHaveCount(1);
    await expect(dialog.locator('[data-backup-selection]')).toContainText(BACKUPS[2].id);

    // Clearing the checkmark returns to "no selection".
    await selectBox(dialog, 2).uncheck();
    await expect(dialog.locator('input[type="checkbox"]:checked')).toHaveCount(0);
    await expect(action(dialog, 'save')).toBeDisabled();
    await expect(action(dialog, 'restore')).toBeDisabled();
    await expect(dialog.locator('[data-backup-remove]')).toHaveCount(0);
  });

test('Add Backup admits a valid backup, refreshes, and selects it', async ({page}) => {
  const state = await mockBackups(page);
  const dialog = await openManager(page);
  const chooser = page.waitForEvent('filechooser');
  await action(dialog, 'add').click();
  await (await chooser).setFiles({name: 'debridpulse-backup.zip', mimeType: 'application/zip',
    buffer: Buffer.from('PK-valid-backup')});
  await expect(rows(dialog)).toHaveCount(4);
  // The file itself is the body -- streamed, never wrapped in a form.
  expect(state.added).toEqual([{bytes: Buffer.from('PK-valid-backup').length, type: 'application/zip'}]);
  const added = dialog.locator(`tr[data-backup-id="${ADDED.id}"]`);
  await expect(added.locator('input[type="checkbox"]')).toBeChecked();
  await expect(added.locator('[data-backup-remove]')).toHaveCount(1);
  await expect(page.locator('#toasts .toast').last()).toContainText('Backup added');
});

test('an invalid Add Backup is explained and adds no row', async ({page}) => {
  const detail = 'Backup could not be added. The selected file is not a valid DebridPulse backup.';
  const state = await mockBackups(page, {addResult: {status: 400, body: {detail}}});
  const dialog = await openManager(page);
  const chooser = page.waitForEvent('filechooser');
  await action(dialog, 'add').click();
  await (await chooser).setFiles({name: 'holiday.zip', mimeType: 'application/zip', buffer: Buffer.from('nope')});
  await expect(page.locator('#toasts .toast').last()).toContainText(detail);
  expect(state.added).toHaveLength(1);
  await expect(rows(dialog)).toHaveCount(3);
  await expect(dialog.locator('input[type="checkbox"]:checked')).toHaveCount(0);
});

test('Save Backup writes the selected portable backup through the native save dialog', async ({page}) => {
  const state = await mockBackups(page);
  await page.evaluate(() => {
    window.__dpSaved = {name: '', bytes: 0, closed: false};
    window.showSaveFilePicker = async options => {
      window.__dpSaved.name = options.suggestedName;
      return {createWritable: async () => new WritableStream({
        write(chunk) { window.__dpSaved.bytes += chunk.byteLength; },
        close() { window.__dpSaved.closed = true; },
      })};
    };
  });
  const dialog = await openManager(page);
  await selectBox(dialog, 1).check();
  await action(dialog, 'save').click();
  await expect.poll(() => page.evaluate(() => window.__dpSaved.closed)).toBe(true);
  const saved = await page.evaluate(() => window.__dpSaved);
  expect(saved.name).toBe(`debridpulse-backup-${BACKUPS[1].id}.zip`);
  expect(saved.bytes).toBe(Buffer.from('PK-portable-backup-bytes').length);
  expect(state.saved).toEqual([BACKUPS[1].id]);
  await expect(page.locator('#toasts .toast').last()).toContainText('Backup saved');
});

test('Save Backup falls back to the browser save flow without a native save dialog', async ({page}) => {
  await mockBackups(page);
  await page.evaluate(() => { delete window.showSaveFilePicker; window.showSaveFilePicker = undefined; });
  const dialog = await openManager(page);
  await selectBox(dialog, 0).check();
  const pending = page.waitForEvent('download');
  await action(dialog, 'save').click();
  const download = await pending;
  // The browser's own save flow fetches exactly the selected portable unit.
  expect(new URL(download.url()).pathname).toBe(`/api/admin/backups/${BACKUPS[0].id}/package`);
  expect(download.suggestedFilename()).toBe(`debridpulse-backup-${BACKUPS[0].id}.zip`);
});

test('Restore Backup asks for an explicit confirmation that explains the consequence', async ({page}) => {
  const state = await mockBackups(page);
  const dialog = await openManager(page);
  await selectBox(dialog, 0).check();
  await action(dialog, 'restore').click();
  const confirm = page.locator('.dp-modal-dialog.dp-backup-restore-dialog');
  await expect(confirm).toBeVisible();
  await expect(confirm).toHaveAttribute('role', 'alertdialog');
  await expect(confirm.locator('.dp-modal-title')).toHaveText('Restore Backup?');
  const copy = await confirm.innerText();
  expect(copy).toContain('This will replace the current DebridPulse database and configuration');
  expect(copy).toContain('Processing will be paused');
  expect(copy).toContain('A safety backup of the current state will be created automatically');
  expect(copy).toContain('The selected backup will be validated again');
  expect(copy).toContain('If validation fails, the current installation will remain unchanged.');
  await expect(confirm.locator('[data-modal-accept]')).toHaveText('Restore Backup');
  await expect(confirm.locator('[data-modal-accept]')).toHaveClass(/btn-success/);
  await expect(confirm.locator('[data-modal-close]')).toBeVisible();

  // Cancel restores nothing and leaves the manager as it was.
  await confirm.locator('[data-modal-cancel]').click();
  await expect(confirm).toHaveCount(0);
  expect(state.restored).toEqual([]);
  await expect(selectBox(dialog, 0)).toBeChecked();

  // Confirming sends exactly the selected restore point.
  await action(dialog, 'restore').click();
  await page.locator('.dp-backup-restore-dialog [data-modal-accept]').click();
  await expect.poll(() => state.restored.length).toBe(1);
  expect(state.restored[0]).toEqual({id: BACKUPS[0].id});
  await expect(page.locator('#toasts .toast').last()).toContainText('Backup restored');
});

test('a refused restore reports that the current state was left unchanged', async ({page}) => {
  const detail = 'Backup could not be restored. This backup was created by an unsupported version. '
    + 'The current DebridPulse state was left unchanged.';
  await mockBackups(page, {restoreResult: {status: 400, body: {detail}}});
  const dialog = await openManager(page);
  await selectBox(dialog, 1).check();
  await action(dialog, 'restore').click();
  await page.locator('.dp-backup-restore-dialog [data-modal-accept]').click();
  await expect(page.locator('#toasts .toast').last()).toContainText('left unchanged');
  await expect(dialog).toBeVisible();
  await expect(rows(dialog)).toHaveCount(3);
});

test('Remove asks for a destructive confirmation and removes exactly the selected backup', async ({page}) => {
  const state = await mockBackups(page);
  const dialog = await openManager(page);
  await selectBox(dialog, 1).check();
  await rows(dialog).nth(1).locator('[data-backup-remove]').click();
  const confirm = page.locator('.dp-modal-dialog[data-tone="danger"]');
  await expect(confirm.locator('.dp-modal-title')).toHaveText('Remove Backup?');
  await expect(confirm.locator('.dp-modal-message')).toContainText('will be permanently removed from DebridPulse backup storage.');
  await expect(confirm.locator('[data-modal-accept]')).toHaveText('Remove Backup');
  await expect(confirm.locator('[data-modal-accept]')).toHaveClass(/btn-danger/);
  await confirm.locator('[data-modal-cancel]').click();
  expect(state.removed).toEqual([]);

  await rows(dialog).nth(1).locator('[data-backup-remove]').click();
  await page.locator('.dp-modal-dialog[data-tone="danger"] [data-modal-accept]').click();
  await expect(rows(dialog)).toHaveCount(2);
  expect(state.removed).toEqual([BACKUPS[1].id]);
  await expect(dialog.locator(`tr[data-backup-id="${BACKUPS[1].id}"]`)).toHaveCount(0);
  await expect(dialog.locator('[data-backup-remove]')).toHaveCount(0);
});

test('Close, the upper-right close control and Escape all close the manager', async ({page}) => {
  await mockBackups(page);
  for (const how of ['close', 'x', 'escape']) {
    const dialog = await openManager(page);
    if (how === 'close') await dialog.locator('[data-modal-cancel]').click();
    else if (how === 'x') await dialog.locator('[data-modal-close]').click();
    else await page.keyboard.press('Escape');
    await expect(page.locator('.dp-backup-manager-dialog')).toHaveCount(0);
    await expect(page.locator('#view-settings [data-action="backups"]')).toBeFocused();
  }
});
