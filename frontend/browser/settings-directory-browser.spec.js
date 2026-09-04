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

function builtinField(page) {
  return page.locator('#dp-settings-field-download-folder');
}

function browseButton(page) {
  return page.locator('button[data-action="browse-download-folder"]');
}

function directoryDialog(page) {
  return page.locator('.dp-settings-directory-dialog');
}

test('Browse is built-in only and preserves backend path, ordering, capacity, root, and symlink semantics', async ({ page }) => {
  const requests = await installDirectoryFixture(page);
  await openDownloadsSettings(page);

  await expect(browseButton(page)).toHaveCount(1);
  await expect(browseButton(page)).toBeVisible();
  await expect(page.locator('[data-download-path-mode="external"] button[data-action="browse-download-folder"]')).toHaveCount(0);

  await builtinField(page).fill('/download');
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
  await expect(dialog.locator('[data-directory-confirm]')).toBeDisabled();
  await expect(dialog.locator('[data-directory-capacity]')).toHaveText('Capacity unavailable');
});

test('invalid initial path falls back without repairing the field and Cancel/Escape are exact', async ({ page }) => {
  const requests = await installDirectoryFixture(page, { invalidInitial: true });
  await openDownloadsSettings(page);

  const field = builtinField(page);
  const browse = browseButton(page);
  await field.fill('/missing/or/unavailable');
  await browse.click();

  const dialog = directoryDialog(page);
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('[data-directory-notice]')).toBeVisible();
  await expect(dialog.locator('[data-directory-current-path]')).toHaveText('/fallback');
  await expect(field).toHaveValue('/missing/or/unavailable');
  expect(requests.slice(0, 2)).toEqual(['/missing/or/unavailable', null]);

  await dialog.locator('[data-directory-cancel]').click();
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

test('Confirm changes only the form field; Save remains the persistence boundary and rejections stay safe', async ({ page }) => {
  await installDirectoryFixture(page);
  let putCount = 0;
  let lastPut = null;
  let rejectSave = false;

  await page.route('**/api/settings', async route => {
    const request = route.request();
    if (request.method() !== 'PUT') {
      await route.continue();
      return;
    }
    putCount += 1;
    lastPut = request.postDataJSON();
    if (rejectSave) {
      await route.fulfill({
        status: 400,
        contentType: 'application/json',
        body: JSON.stringify({
          detail: { code: 'invalid_path', message: 'Selected Download Folder is no longer available' },
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(lastPut),
    });
  });

  await openDownloadsSettings(page);
  const field = builtinField(page);
  const browse = browseButton(page);
  await field.fill('/download');
  await browse.click();
  const dialog = directoryDialog(page);
  await dialog.locator('[data-directory-row][data-path="/download/Alpha"]').click();
  await expect(dialog.locator('[data-directory-current-path]')).toHaveText('/download/Alpha');
  await dialog.locator('[data-directory-confirm]').click();

  await expect(dialog).toHaveCount(0);
  await expect(field).toHaveValue('/download/Alpha');
  await expect(browse).toBeFocused();
  expect(putCount).toBe(0);

  const save = page.locator('button[data-action="save"]');
  await save.click();
  await expect.poll(() => putCount).toBe(1);
  expect(lastPut.download_folder).toBe('/download/Alpha');

  await field.fill('/download');
  await browse.click();
  await directoryDialog(page).locator('[data-directory-row][data-path="/download/Alpha"]').click();
  await directoryDialog(page).locator('[data-directory-confirm]').click();
  rejectSave = true;
  await save.click();
  await expect.poll(() => putCount).toBe(2);
  await expect(field).toHaveValue('/download/Alpha');
});

test('directory modal traps/restores focus and remains usable in dark, light, and narrow layouts', async ({ page }) => {
  await installDirectoryFixture(page);
  await openDownloadsSettings(page);
  await builtinField(page).fill('/download');
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
  await dialog.locator('[data-directory-cancel]').click();
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
    const footer = node.querySelector('.dp-settings-confirm-footer');
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
