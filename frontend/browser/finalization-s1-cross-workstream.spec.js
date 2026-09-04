const { test, expect } = require('@playwright/test');

const GIB = 1024 ** 3;

function storageSnapshot(domain, state, generation) {
  const application = domain === 'application_state';
  return {
    domain,
    configured_path: application ? '/app/data/debridpulse.db' : '/download',
    resolved_path: application ? '/app/data' : '/download',
    exists: true,
    is_directory: true,
    accessible: true,
    writable: state === 'healthy',
    fsync_supported: true,
    total_bytes: 100 * GIB,
    free_bytes: state === 'full' ? 0 : 80 * GIB,
    filesystem_id: application ? 'application-fs' : 'download-fs',
    state,
    reason: state === 'full' ? 'capacity_exhausted' : 'none',
    generation,
    low_space_threshold_bytes: application ? null : 10 * GIB,
    recovery_threshold_bytes: application ? null : 11 * GIB,
    transitioned_at: 1000 + generation,
    probed_at: 2000 + generation,
  };
}

async function installIntegratedFixture(page) {
  await page.route(url => url.pathname === '/api/storage/health', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      enabled: true,
      active: true,
      min_free_gb: 10,
      free_gb: 0,
      application_state: storageSnapshot('application_state', 'healthy', 1),
      download: storageSnapshot('download', 'full', 2),
      shared_filesystem: false,
    }),
  }));

  await page.route('**/api/settings/directories*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      current: {
        name: 'download',
        path: '/download',
        accessible: true,
        writable: true,
        selectable: true,
        reason: 'none',
        capacity: { total_bytes: 100 * GIB, free_bytes: 0 },
      },
      parent: '/',
      children: [
        {
          name: 'child',
          path: '/download/child',
          accessible: true,
          writable: null,
          selectable: null,
          reason: 'not_validated',
        },
      ],
    }),
  }));
}

async function openDownloads(page) {
  await page.goto('/');
  await expect(page.locator('.dp-storage-warning[data-storage-domain="download"]')).toBeVisible();
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  const downloads = page.locator('.dp-settings-tabs .stab[data-tab="downloads"]');
  await downloads.click();
  await expect(downloads).toHaveAttribute('aria-selected', 'true');
}

async function openBrowser(page) {
  const field = page.locator('#dp-settings-field-download-folder');
  const browse = page.locator('button[data-action="browse-download-folder"]');
  await field.fill('/download');
  await browse.click();
  const dialog = page.locator('.dp-settings-directory-dialog');
  await expect(dialog).toBeVisible();
  return { browse, dialog };
}

test('storage warning and Browse modal remain truthful and usable together in dark/light layouts', async ({ page }) => {
  await installIntegratedFixture(page);
  await openDownloads(page);

  const warning = page.locator('.dp-storage-warning[data-storage-domain="download"]');
  await expect(warning.locator('.dp-storage-warning__state')).toHaveText('Full');

  let { browse, dialog } = await openBrowser(page);
  await expect(warning).toBeVisible();
  await expect(dialog).toHaveAttribute('role', 'dialog');
  expect(await page.evaluate(() => {
    const modal = document.querySelector('.dp-settings-directory-dialog');
    return Boolean(modal && modal.contains(document.activeElement));
  })).toBe(true);
  await page.screenshot({
    path: 'test-results/finalization-s1-storage-warning-browse-dark.png',
    fullPage: true,
  });

  await dialog.locator('[data-directory-cancel]').click();
  await expect(browse).toBeFocused();
  await page.locator('#theme-toggle').click();
  await expect.poll(() => page.evaluate(() => document.body.classList.contains('light'))).toBeTruthy();
  await page.setViewportSize({ width: 520, height: 720 });

  ({ browse, dialog } = await openBrowser(page));
  await expect(warning).toBeVisible();
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
      listOverflow: getComputedStyle(list).overflowY,
      footerBottom: footer.getBoundingClientRect().bottom,
    };
  });
  expect(geometry.left).toBeGreaterThanOrEqual(0);
  expect(geometry.right).toBeLessThanOrEqual(geometry.viewportWidth + 1);
  expect(geometry.top).toBeGreaterThanOrEqual(0);
  expect(geometry.bottom).toBeLessThanOrEqual(geometry.viewportHeight + 1);
  expect(['auto', 'scroll']).toContain(geometry.listOverflow);
  expect(geometry.footerBottom).toBeLessThanOrEqual(geometry.viewportHeight + 1);
  await page.screenshot({
    path: 'test-results/finalization-s1-storage-warning-browse-light-narrow.png',
    fullPage: true,
  });
});
