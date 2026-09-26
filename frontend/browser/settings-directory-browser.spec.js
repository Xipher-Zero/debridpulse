const { test, expect } = require('@playwright/test');

function directoryEntry(name, path, overrides = {}) {
  return {
    name,
    path,
    accessible: true,
    writable: null,
    selectable: null,
    reason: 'not_validated',
    ...overrides,
  };
}

function directoryResponse(path, overrides = {}) {
  const defaults = {
    '/download': {
      current: {
        name: 'download',
        path: '/download',
        accessible: true,
        writable: true,
        selectable: true,
        reason: 'none',
        capacity: { total_bytes: 2 * 1024 ** 3, free_bytes: 1024 ** 3 },
      },
      parent: '/',
      children: [
        directoryEntry('zeta', '/download/zeta'),
        directoryEntry('Alpha', '/download/Alpha'),
        directoryEntry('linked', '/resolved-target'),
        directoryEntry('blocked', '/download/blocked', {
          accessible: false,
          writable: false,
          selectable: false,
          reason: 'inaccessible',
        }),
      ],
      files: [{ name: 'must-not-render.txt', path: '/download/must-not-render.txt' }],
    },
    '/download/Alpha': {
      current: {
        name: 'Alpha',
        path: '/download/Alpha',
        accessible: true,
        writable: true,
        selectable: true,
        reason: 'none',
        capacity: { total_bytes: 4 * 1024 ** 3, free_bytes: 3 * 1024 ** 3 },
      },
      parent: '/download',
      children: [],
    },
    '/resolved-target': {
      current: {
        name: 'resolved-target',
        path: '/resolved-target',
        accessible: true,
        writable: true,
        selectable: true,
        reason: 'none',
        capacity: { total_bytes: 8 * 1024 ** 3, free_bytes: 6 * 1024 ** 3 },
      },
      parent: '/',
      children: [],
    },
    '/': {
      current: {
        name: '/',
        path: '/',
        accessible: true,
        writable: false,
        selectable: false,
        reason: 'read_only',
        capacity: { total_bytes: null, free_bytes: null },
      },
      parent: null,
      children: [directoryEntry('download', '/download')],
    },
    '/fallback': {
      current: {
        name: 'fallback',
        path: '/fallback',
        accessible: true,
        writable: true,
        selectable: true,
        reason: 'none',
        capacity: { total_bytes: 1024 ** 3, free_bytes: 512 * 1024 ** 2 },
      },
      parent: '/',
      children: [],
    },
  };
  return { ...(defaults[path] || defaults['/download']), ...overrides };
}

/* Accept every whole-settings write and echo it back, so a field-boundary
 * commit this case is not about cannot change what it measures. */
async function acceptSettingsWrites(page) {
  await page.route('**/api/settings', async route => {
    if (route.request().method() !== 'PUT') {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(route.request().postDataJSON()),
    });
  });
}

/* Count and, on demand, reject the canonical whole-settings writes. */
function trackSettingsWrites(page) {
  const state = { count: 0, last: null, reject: false };
  page.route('**/api/settings', async route => {
    if (route.request().method() !== 'PUT') {
      await route.continue();
      return;
    }
    state.count += 1;
    state.last = route.request().postDataJSON();
    if (state.reject) {
      await route.fulfill({
        status: 400, contentType: 'application/json',
        body: JSON.stringify({
          detail: { code: 'invalid_path', message: 'Selected Download Folder is no longer available' },
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify(state.last),
    });
  });
  return state;
}

async function installDirectoryFixture(page, { invalidInitial = false } = {}) {
  const requests = [];
  await page.route('**/api/settings/directories*', async route => {
    const url = new URL(route.request().url());
    const path = url.searchParams.has('path') ? url.searchParams.get('path') : null;
    requests.push(path);

    if (invalidInitial && path === '/missing/or/unavailable') {
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: { code: 'path_unavailable', message: 'Directory path does not exist' },
        }),
      });
      return;
    }

    const response = path === null && invalidInitial
      ? directoryResponse('/fallback')
      : directoryResponse(path || '/download');
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(response),
    });
  });
  return requests;
}

async function openDownloadsSettings(page) {
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  const downloads = page.locator('.dp-settings-tabs .stab[data-tab="downloads"]');
  await downloads.click();
  await expect(downloads).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('.dp-settings-download-engine-row')).toBeVisible();
}

function downloadFolderField(page) {
  return page.locator('#dp-settings-field-download-folder');
}

function browseButton(page) {
  return page.locator('button[data-action="browse-download-folder"]');
}

function directoryDialog(page) {
  return page.locator('.dp-settings-directory-dialog');
}

test('Browse is offered once and preserves backend path, ordering, capacity, root, and symlink semantics', async ({ page }) => {
  const requests = await installDirectoryFixture(page);
  await openDownloadsSettings(page);

  await expect(browseButton(page)).toHaveCount(1);
  await expect(browseButton(page)).toBeVisible();

  await downloadFolderField(page).fill('/download');
  await browseButton(page).click();
  const dialog = directoryDialog(page);
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveAttribute('role', 'dialog');
  await expect(dialog.locator('[data-directory-current-path]')).toHaveText('/download');
  await expect(dialog.locator('[data-directory-capacity]')).toContainText('free of');
  expect(requests[0]).toBe('/download');

  const rows = dialog.locator('[data-directory-row]');
  await expect(rows).toHaveCount(4);
  await expect(rows.nth(0).locator('[data-directory-name]')).toHaveText('zeta');
  await expect(rows.nth(1).locator('[data-directory-name]')).toHaveText('Alpha');
  await expect(dialog.getByText('must-not-render.txt')).toHaveCount(0);
  await expect(dialog.locator('[data-directory-row][data-path="/download/blocked"]')).toBeDisabled();

  await dialog.locator('[data-directory-row][data-path="/resolved-target"]').click();
  await expect(dialog.locator('[data-directory-current-path]')).toHaveText('/resolved-target');
  await expect(dialog.locator('[data-directory-current-path]')).not.toContainText('/download/linked');

  await dialog.locator('[data-directory-up]').click();
  await expect(dialog.locator('[data-directory-current-path]')).toHaveText('/');
  await expect(dialog.locator('[data-directory-up]')).toBeDisabled();
  await expect(dialog.locator('[data-modal-accept]')).toBeDisabled();
  await expect(dialog.locator('[data-directory-capacity]')).toHaveText('Capacity unavailable');
});

test('invalid initial path falls back without repairing the field and Cancel/Escape are exact', async ({ page }) => {
  const requests = await installDirectoryFixture(page, { invalidInitial: true });
  // DP 1.0.13: Download Folder is a changed-blur field, so leaving it to click
  // Browse IS its commit boundary. That commit is not what this case is about,
  // so it is accepted here and the picker's own behaviour is what is measured.
  await acceptSettingsWrites(page);
  await openDownloadsSettings(page);

  const field = downloadFolderField(page);
  const browse = browseButton(page);
  await field.fill('/missing/or/unavailable');
  await browse.click();

  const dialog = directoryDialog(page);
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('[data-directory-notice]')).toBeVisible();
  await expect(dialog.locator('[data-directory-current-path]')).toHaveText('/fallback');
  await expect(field).toHaveValue('/missing/or/unavailable');
  expect(requests.slice(0, 2)).toEqual(['/missing/or/unavailable', null]);

  await dialog.locator('[data-modal-cancel]').click();
  await expect(dialog).toHaveCount(0);
  await expect(field).toHaveValue('/missing/or/unavailable');
  await expect(browse).toBeFocused();

  await browse.click();
  await expect(directoryDialog(page)).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(directoryDialog(page)).toHaveCount(0);
  await expect(field).toHaveValue('/missing/or/unavailable');
  await expect(browse).toBeFocused();
});

/* DP 1.0.13: Browse is a non-destructive VALUE-SELECTION action.
 *
 * It chooses what the Download Folder field holds and commits it through the
 * ONE canonical field owner, exactly as the operator typing it and leaving
 * would. It is not a second save path, it names no endpoint of its own, and it
 * never waits for a deferred Apply -- Downloads has none. Cancelling mutates
 * nothing at all, and a rejected write rolls the field back to the canonical
 * truth the server still holds. */
test('an accepted Browse commits Download Folder through the canonical field owner', async ({ page }) => {
  await installDirectoryFixture(page);
  const writes = trackSettingsWrites(page);

  await openDownloadsSettings(page);
  const field = downloadFolderField(page);
  const browse = browseButton(page);

  // No Settings page carries a deferred Apply contract: the control is gone.
  await expect(page.locator('#view-settings button[data-action="save"]')).toHaveCount(0);
  await expect(page.locator('#view-settings .dp-settings-save-hint')).toHaveCount(0);

  // Cancelling performs no mutation whatsoever.
  await browse.click();
  await directoryDialog(page).locator('[data-modal-cancel]').click();
  await expect(directoryDialog(page)).toHaveCount(0);
  await page.waitForTimeout(500);
  expect(writes.count).toBe(0);

  await browse.click();
  const dialog = directoryDialog(page);
  await dialog.locator('[data-directory-row][data-path="/download/Alpha"]').click();
  await expect(dialog.locator('[data-directory-current-path]')).toHaveText('/download/Alpha');
  await dialog.locator('[data-modal-accept]').click();

  await expect(dialog).toHaveCount(0);
  await expect(field).toHaveValue('/download/Alpha');
  await expect(browse).toBeFocused();
  // Committed immediately, by the field's own owner, carrying the chosen value.
  await expect.poll(() => writes.count).toBe(1);
  expect(writes.last.download_folder).toBe('/download/Alpha');

  // A rejected write converges nothing: the field returns to what the server
  // still holds, and no second save path is involved.
  writes.reject = true;
  await browse.click();
  // The picker now opens on the accepted folder; step up to its parent, which
  // is itself selectable, and choose that instead.
  await directoryDialog(page).locator('[data-directory-up]').click();
  await expect(directoryDialog(page).locator('[data-directory-current-path]')).toHaveText('/download');
  await directoryDialog(page).locator('[data-modal-accept]').click();
  await expect.poll(() => writes.count).toBe(2);
  expect(writes.last.download_folder).toBe('/download');
  // Nothing was accepted, so the field returns to the canonical truth.
  await expect(field).toHaveValue('/download/Alpha');
});

function backupField(page) {
  return page.locator('[data-setting="backup_folder"]');
}

function backupBrowseButton(page) {
  return page.locator('button[data-action="browse-backup-folder"]');
}

async function openMaintenanceSettings(page) {
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  const maintenance = page.locator('.dp-settings-tabs .stab[data-tab="maintenance"]');
  await maintenance.click();
  await expect(maintenance).toHaveAttribute('aria-selected', 'true');
  await expect(backupField(page)).toBeVisible();
}

async function installBackupDirectoryFixture(page) {
  const requests = [];
  const responses = {
    '/backups': {
      current: {
        name: 'backups', path: '/backups', accessible: true, writable: true,
        selectable: true, reason: 'none', capacity: { total_bytes: null, free_bytes: null },
      },
      parent: '/', children: [{ name: 'old', path: '/backups/old', accessible: true, writable: null, selectable: null, reason: 'not_validated' }],
    },
  };
  await page.route('**/api/settings/directories*', async route => {
    const url = new URL(route.request().url());
    const purpose = url.searchParams.get('purpose');
    const path = url.searchParams.has('path') ? url.searchParams.get('path') : null;
    requests.push({ purpose, path });
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify(responses[path || '/backups']),
    });
  });
  return requests;
}

test('Backup Folder shares the same modal/runtime and endpoint, with backup purpose and semantics, and commits at its own field boundary', async ({ page }) => {
  const requests = await installBackupDirectoryFixture(page);
  // A field commits only what CHANGED, so the accepted folder has to differ
  // from the stored one for this to be a write at all. Seeded before the
  // route, so the seed itself is not counted.
  const stored = await page.request.get('/api/settings').then(r => r.json());
  await page.request.put('/api/settings', {data: {
    ...stored,
    integrations: undefined, integration_groups: undefined,
    transfer_policy: undefined, execution_runtime_limits: undefined,
    compatibility_fields: undefined, clear_secrets: [],
    backup_folder: '/backups/seeded-elsewhere',
  }});

  let putCount = 0;
  let lastPut = null;
  await page.route('**/api/settings', async route => {
    const request = route.request();
    if (request.method() !== 'PUT') { await route.continue(); return; }
    putCount += 1;
    lastPut = request.postDataJSON();
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(lastPut) });
  });

  await openMaintenanceSettings(page);
  const field = backupField(page);
  const browse = backupBrowseButton(page);
  await field.fill('/backups');
  await browse.click();

  // Same modal/runtime as Download Folder -- the exact same dialog class.
  const dialog = directoryDialog(page);
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('[data-directory-current-path]')).toHaveText('/backups');
  await expect(dialog.locator('[data-directory-current-state]')).toHaveText('Selectable as Backup Folder');

  // Every request for this field carries purpose=backup, never plain/download.
  expect(requests.every(r => r.purpose === 'backup')).toBe(true);

  await dialog.locator('[data-modal-accept]').click();
  await expect(dialog).toHaveCount(0);
  await expect(field).toHaveValue('/backups');

  // The chosen folder is COMMITTED, and it waits for no Apply -- there is
  // none. Browse is an explicit action, so it settles the pending draft before
  // it opens; accepting the same path then has nothing left to write.
  await expect.poll(() => putCount).toBe(1);
  expect(lastPut.backup_folder).toBe('/backups');
  await expect(page.locator('#view-settings button[data-action="save"]')).toHaveCount(0);

  // A second Browse settles the newly typed draft in exactly the same way...
  await field.fill('/manually-typed-path');
  await browse.click();
  await expect(directoryDialog(page)).toBeVisible();
  await expect.poll(() => putCount).toBe(2);
  expect(lastPut.backup_folder).toBe('/manually-typed-path');

  // ...and cancelling the dialog itself mutates nothing at all.
  await page.locator('[data-modal-cancel]').click();
  await expect(directoryDialog(page)).toHaveCount(0);
  await expect(field).toHaveValue('/manually-typed-path');
  expect(putCount).toBe(2);

  // Manual text editing still works after using the browser.
  await field.fill('/typed-again');
  await expect(field).toHaveValue('/typed-again');
  // Put back only what this case changed, against FRESHLY read canonical
  // truth: the suite shares one backend, so restoring a whole snapshot would
  // overwrite whatever a concurrent spec file committed in the meantime.
  await page.unroute('**/api/settings');
  const now = await page.request.get('/api/settings').then(r => r.json());
  await page.request.put('/api/settings', {data: {
    ...now,
    integrations: undefined, integration_groups: undefined,
    transfer_policy: undefined, execution_runtime_limits: undefined,
    compatibility_fields: undefined, clear_secrets: [],
    backup_folder: stored.backup_folder,
  }});

  // Vertically centered with the field, and the field yields width to the button.
  const geometry = await page.evaluate(() => {
    const input = document.querySelector('[data-setting="backup_folder"]');
    const button = document.querySelector('[data-action="browse-backup-folder"]');
    const ir = input.getBoundingClientRect();
    const br = button.getBoundingClientRect();
    return { inputCenter: ir.top + ir.height / 2, buttonCenter: br.top + br.height / 2, inputWidth: ir.width, buttonWidth: br.width };
  });
  expect(Math.abs(geometry.inputCenter - geometry.buttonCenter)).toBeLessThan(2);
  expect(geometry.buttonWidth).toBeGreaterThan(60); // normal button padding preserved, not crushed
});

test('Backup Folder browsing never invokes Download Storage validation semantics', async ({ page }) => {
  const requests = await installBackupDirectoryFixture(page);
  await openMaintenanceSettings(page);
  await backupField(page).fill('/backups');
  await backupBrowseButton(page).click();
  await expect(directoryDialog(page)).toBeVisible();
  // Download Storage's own wording/reason set (e.g. "Selectable as Download
  // Storage", capacity-based reasons) never appears for the backup purpose.
  await expect(directoryDialog(page).locator('[data-directory-current-state]')).toHaveText('Selectable as Backup Folder');
  await expect(directoryDialog(page).locator('[data-directory-current-state]')).not.toHaveText('Selectable as Download Storage');
  expect(requests.every(r => r.purpose === 'backup')).toBe(true);
  expect(requests.some(r => r.purpose === 'download' || r.purpose === null)).toBe(false);
});

test('directory modal traps/restores focus and remains usable in dark, light, and narrow layouts', async ({ page }) => {
  await installDirectoryFixture(page);
  await openDownloadsSettings(page);
  await downloadFolderField(page).fill('/download');
  const browse = browseButton(page);
  await browse.click();

  let dialog = directoryDialog(page);
  await expect(dialog).toBeVisible();
  expect(await page.evaluate(() => {
    const modal = document.querySelector('.dp-settings-directory-dialog');
    const focusable = Array.from(modal.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'));
    focusable.at(-1).focus();
    return document.activeElement === focusable.at(-1);
  })).toBe(true);
  await page.keyboard.press('Tab');
  expect(await page.evaluate(() => document.querySelector('.dp-settings-directory-dialog').contains(document.activeElement))).toBe(true);

  await page.screenshot({ path: 'test-results/checkpoint-settings-directory-browser-dark-desktop.png', fullPage: true });
  await dialog.locator('[data-modal-cancel]').click();
  await expect(browse).toBeFocused();

  await page.locator('#theme-toggle').click();
  await expect.poll(() => page.evaluate(() => document.body.classList.contains('light'))).toBeTruthy();
  await page.setViewportSize({ width: 520, height: 720 });
  await browse.click();
  dialog = directoryDialog(page);
  await expect(dialog).toBeVisible();
  const geometry = await dialog.evaluate(node => {
    const rect = node.getBoundingClientRect();
    const list = node.querySelector('.dp-settings-directory-list');
    const footer = node.querySelector('.dp-modal-footer');
    return {
      left: rect.left,
      right: rect.right,
      top: rect.top,
      bottom: rect.bottom,
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight,
      listScrollable: list.scrollHeight >= list.clientHeight,
      footerVisible: !!footer && footer.getBoundingClientRect().bottom <= window.innerHeight + 1,
    };
  });
  expect(geometry.left).toBeGreaterThanOrEqual(0);
  expect(geometry.right).toBeLessThanOrEqual(geometry.viewportWidth + 1);
  expect(geometry.top).toBeGreaterThanOrEqual(0);
  expect(geometry.bottom).toBeLessThanOrEqual(geometry.viewportHeight + 1);
  expect(geometry.footerVisible).toBe(true);
  await page.screenshot({ path: 'test-results/checkpoint-settings-directory-browser-light-narrow.png', fullPage: true });
});

// ── Canonical modal lifecycle: directory requests race dialog close (Canonical Release Remediation) ────

async function installGatedDirectoryFixture(page) {
  const holds = new Map();
  const requests = [];
  await page.route('**/api/settings/directories*', async route => {
    const url = new URL(route.request().url());
    const path = url.searchParams.has('path') ? url.searchParams.get('path') : null;
    requests.push(path);
    const hold = holds.get(path);
    if (hold) {
      holds.delete(path);
      hold.markArrived();
      await hold.gate;
    }
    try {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(directoryResponse(path || '/download')),
      });
    } catch (_) {
      // The page aborted the request (the dialog closed): a late fulfil has nowhere to go.
    }
    if (hold) hold.markSettled();
  });
  return {
    requests,
    // Park the next request for `path` until release(); `arrived` resolves once it is parked and
    // `settled` once the (possibly late) response has been delivered or dropped.
    hold(path) {
      let release;
      let markArrived;
      let markSettled;
      const hold = {
        gate: new Promise(resolve => { release = resolve; }),
        arrived: new Promise(resolve => { markArrived = resolve; }),
        settled: new Promise(resolve => { markSettled = resolve; }),
        markArrived: () => markArrived(),
        markSettled: () => markSettled(),
      };
      holds.set(path, hold);
      return {arrived: hold.arrived, settled: hold.settled, release: () => release()};
    },
  };
}

function watchPageErrors(page) {
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  return errors;
}

const currentPath = dialog => dialog.locator('[data-directory-current-path]');

test('a directory response that arrives after the dialog closed cannot mutate the field, the DOM, or the page', async ({ page }) => {
  const errors = watchPageErrors(page);
  const fixture = await installGatedDirectoryFixture(page);
  await openDownloadsSettings(page);
  await downloadFolderField(page).fill('/download');
  await browseButton(page).click();
  const dialog = directoryDialog(page);
  await expect(currentPath(dialog)).toHaveText('/download');

  const held = fixture.hold('/download/Alpha');
  await dialog.locator('[data-directory-row][data-path="/download/Alpha"]').click();
  await held.arrived;
  await page.keyboard.press('Escape');
  await expect(directoryDialog(page)).toHaveCount(0);
  await expect(browseButton(page)).toBeFocused();

  held.release();
  await held.settled;
  await expect(directoryDialog(page)).toHaveCount(0);
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
  expect(await page.evaluate(() => document.body.classList.contains('dp-modal-open'))).toBe(false);
  await expect(downloadFolderField(page)).toHaveValue('/download');
  expect(errors).toEqual([]);
});

test('a stale response from a closed dialog can never mutate, or be accepted into, a newer dialog', async ({ page }) => {
  const errors = watchPageErrors(page);
  const fixture = await installGatedDirectoryFixture(page);
  await openDownloadsSettings(page);
  await downloadFolderField(page).fill('/download');
  await browseButton(page).click();
  let dialog = directoryDialog(page);
  await expect(currentPath(dialog)).toHaveText('/download');

  const held = fixture.hold('/download/Alpha');
  await dialog.locator('[data-directory-row][data-path="/download/Alpha"]').click();
  await held.arrived;
  await page.keyboard.press('Escape');
  await expect(directoryDialog(page)).toHaveCount(0);

  // A newer dialog opens and loads its own state while the old request is still parked.
  await browseButton(page).click();
  dialog = directoryDialog(page);
  await expect(currentPath(dialog)).toHaveText('/download');
  held.release();
  await held.settled;

  await expect(currentPath(dialog)).toHaveText('/download');
  await expect(dialog.locator('[data-modal-accept]')).toBeEnabled();
  await dialog.locator('[data-modal-accept]').click();
  await expect(downloadFolderField(page)).toHaveValue('/download');
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('repeated open/cancel cycles settle once each, restore Browse every time, and Escape works after focus leaves the dialog', async ({ page }) => {
  const fixture = await installGatedDirectoryFixture(page);
  await openDownloadsSettings(page);
  await downloadFolderField(page).fill('/download');
  const browse = browseButton(page);

  for (let cycle = 0; cycle < 3; cycle += 1) {
    await browse.click();
    await expect(page.locator('.dp-modal-overlay')).toHaveCount(1);
    await expect(currentPath(directoryDialog(page))).toHaveText('/download');
    await expect(directoryDialog(page).locator('[data-modal-cancel]')).toBeFocused();
    if (cycle === 1) {
      // Focus leaves the dialog (backdrop click): Escape must still be owned by the dialog.
      await page.locator('.dp-modal-overlay').click({position: {x: 4, y: 4}});
      expect(await page.evaluate(() => document.activeElement === document.body)).toBe(true);
      await page.keyboard.press('Escape');
    } else {
      await directoryDialog(page).locator('[data-modal-cancel]').click();
    }
    await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
    await expect(browse).toBeFocused();
    expect(await page.evaluate(() => document.body.classList.contains('dp-modal-open'))).toBe(false);
  }
  expect(fixture.requests).toEqual(['/download', '/download', '/download']);
  await expect(downloadFolderField(page)).toHaveValue('/download');
});

test('the directory browser is a direct client of the dialog owner: its own dialog, no confirmation shell to mutate', async ({ page }) => {
  await installGatedDirectoryFixture(page);
  await openDownloadsSettings(page);
  await downloadFolderField(page).fill('/download');
  await browseButton(page).click();
  const dialog = directoryDialog(page);
  await expect(dialog).toBeVisible();
  const shell = await dialog.evaluate(node => ({
    role: node.getAttribute('role'),
    describedBy: node.getAttribute('aria-describedby'),
    tone: node.getAttribute('data-tone'),
    labelled: !!document.getElementById(node.getAttribute('aria-labelledby')),
    bodyIsSlot: node.querySelector(':scope > .dp-modal-body')?.classList.contains('dp-settings-directory-body'),
    confirmMessage: !!node.querySelector('.dp-modal-message, .dp-modal-typed'),
    dialogs: document.querySelectorAll('.dp-modal-dialog').length,
  }));
  expect(shell).toEqual({
    role: 'dialog', describedBy: null, tone: null, labelled: true,
    bodyIsSlot: true, confirmMessage: false, dialogs: 1,
  });
  await expect(dialog.locator('[data-modal-accept]')).toHaveText('Use This Folder');
});
