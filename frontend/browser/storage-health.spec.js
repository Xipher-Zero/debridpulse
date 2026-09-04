const { test, expect } = require('@playwright/test');

const GIB = 1024 ** 3;

function snapshot(domain, state, generation, options = {}) {
  const defaults = {
    configured_path: domain === 'application_state' ? '/app/data/debridpulse.db' : '/downloads',
    resolved_path: domain === 'application_state' ? '/app/data' : '/downloads',
    exists: true,
    is_directory: true,
    accessible: true,
    writable: state === 'read_only' || state === 'unavailable' ? false : true,
    fsync_supported: true,
    total_bytes: 100 * GIB,
    free_bytes: 80 * GIB,
    filesystem_id: domain === 'application_state' ? '11' : '22',
    transitioned_at: 1000 + generation,
    probed_at: 2000 + generation,
  };
  const reason = {
    healthy: 'none',
    low_space: 'low_space',
    full: 'capacity_exhausted',
    read_only: 'read_only',
    unavailable: 'io_error',
  }[state];

  return {
    domain,
    ...defaults,
    ...options,
    state,
    reason,
    generation,
    low_space_threshold_bytes: domain === 'download' ? 10 * GIB : null,
    recovery_threshold_bytes: domain === 'download' ? 11 * GIB : null,
  };
}

function health({
  applicationState = 'healthy',
  downloadState = 'healthy',
  applicationGeneration = 1,
  downloadGeneration = 1,
  applicationOptions = {},
  downloadOptions = {},
} = {}) {
  const application = snapshot(
    'application_state',
    applicationState,
    applicationGeneration,
    applicationOptions,
  );
  const download = snapshot(
    'download',
    downloadState,
    downloadGeneration,
    downloadOptions,
  );
  return {
    enabled: true,
    active: downloadState !== 'healthy',
    min_free_gb: 10,
    free_gb: download.free_bytes == null ? -1 : download.free_bytes / GIB,
    application_state: application,
    download,
    shared_filesystem: application.filesystem_id === download.filesystem_id,
  };
}

async function routeStorageHealth(page, getHealth) {
  await page.route(url => url.pathname === '/api/storage/health', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(getHealth()),
  }));
}

test('storage transition UX deduplicates stable faults, reports capacity, and clears on recovery', async ({ page }) => {
  let current = health();
  await routeStorageHealth(page, () => current);
  await page.goto('/');

  await expect.poll(() => page.evaluate(() => Boolean(window.DPStorageHealth))).toBeTruthy();
  await expect.poll(() => page.evaluate(() => window.DPStorageHealth.snapshot()?.download?.state)).toBe('healthy');
  await expect(page.locator('#dp-storage-health')).toBeHidden();

  current = health({
    downloadState: 'full',
    downloadGeneration: 2,
    downloadOptions: {free_bytes: 0, total_bytes: 100 * GIB},
  });
  await page.evaluate(() => window.DPStorageHealth.refresh());

  const warning = page.locator('.dp-storage-warning[data-storage-domain="download"]');
  await expect(warning).toBeVisible();
  await expect(warning.locator('.dp-storage-warning__domain')).toHaveText('Download Storage');
  await expect(warning.locator('.dp-storage-warning__state')).toHaveText('Full');
  await expect(warning.locator('.dp-storage-warning__capacity')).toHaveText('0 B free of 100.0 GB');
  await expect(page.locator('.toast.error .dp-toast-copy', {hasText: 'Download Storage is full'})).toHaveCount(1);

  await page.evaluate(() => Promise.all([
    window.DPStorageHealth.refresh(),
    window.DPStorageHealth.refresh(),
    window.DPStorageHealth.refresh(),
  ]));
  await expect(page.locator('.toast.error .dp-toast-copy', {hasText: 'Download Storage is full'})).toHaveCount(1);

  current = health({
    downloadState: 'read_only',
    downloadGeneration: 3,
    downloadOptions: {free_bytes: 25 * GIB, total_bytes: 100 * GIB, writable: false},
  });
  await page.evaluate(() => window.DPStorageHealth.refresh());
  await expect(warning.locator('.dp-storage-warning__state')).toHaveText('Read only');
  await expect(page.locator('.toast.error .dp-toast-copy', {hasText: 'Download Storage is read only'})).toHaveCount(1);

  current = health({downloadState: 'healthy', downloadGeneration: 4});
  await page.evaluate(() => window.DPStorageHealth.refresh());
  await expect(page.locator('#dp-storage-health')).toBeHidden();
  await expect(page.locator('.toast.success .dp-toast-copy', {hasText: 'Download Storage has recovered'})).toHaveCount(1);

  await page.evaluate(() => window.DPStorageHealth.refresh());
  await expect(page.locator('.toast.success .dp-toast-copy', {hasText: 'Download Storage has recovered'})).toHaveCount(1);
});

test('storage warning keeps domains independent and never renders unknown capacity as zero', async ({ page }) => {
  let current = health({
    applicationState: 'unavailable',
    applicationGeneration: 5,
    applicationOptions: {
      exists: null,
      is_directory: null,
      accessible: false,
      writable: null,
      fsync_supported: null,
      total_bytes: null,
      free_bytes: null,
      filesystem_id: null,
    },
    downloadState: 'low_space',
    downloadGeneration: 6,
    downloadOptions: {free_bytes: 5 * GIB, total_bytes: 100 * GIB},
  });
  await routeStorageHealth(page, () => current);
  await page.goto('/');

  const region = page.locator('#dp-storage-health');
  await expect(region).toBeVisible();
  await expect(region.locator('.dp-storage-warning')).toHaveCount(2);

  const application = region.locator('[data-storage-domain="application_state"]');
  const download = region.locator('[data-storage-domain="download"]');
  await expect(application.locator('.dp-storage-warning__domain')).toHaveText('Application Storage');
  await expect(application.locator('.dp-storage-warning__state')).toHaveText('Unavailable');
  await expect(application.locator('.dp-storage-warning__capacity')).toHaveCount(0);
  await expect(download.locator('.dp-storage-warning__domain')).toHaveText('Download Storage');
  await expect(download.locator('.dp-storage-warning__state')).toHaveText('Low space');
  await expect(download.locator('.dp-storage-warning__capacity')).toHaveText('5.0 GB free of 100.0 GB');
  expect(await application.textContent()).not.toContain('0 B');

  current = health({
    applicationState: 'full',
    applicationGeneration: 7,
    downloadState: 'unavailable',
    downloadGeneration: 8,
  });
  await page.evaluate(() => window.DPStorageHealth.refresh());
  await expect(region.locator('.dp-storage-warning')).toHaveCount(2);
  await expect(region.locator('[data-storage-domain="application_state"] .dp-storage-warning__state')).toHaveText('Full');
  await expect(region.locator('[data-storage-domain="download"] .dp-storage-warning__state')).toHaveText('Unavailable');
});

test('download-only degradation leaves navigation and Settings usable while warning remains global', async ({ page }) => {
  const current = health({
    applicationState: 'healthy',
    downloadState: 'full',
    downloadGeneration: 9,
  });
  await routeStorageHealth(page, () => current);
  await page.goto('/');

  const warning = page.locator('.dp-storage-warning[data-storage-domain="download"]');
  await expect(warning).toBeVisible();

  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await expect(page.locator('.dp-settings-master-card')).toBeVisible();
  await expect(warning).toBeVisible();

  await page.locator('#sidebar .nav-item[data-view="stats"]').click();
  await expect(page.locator('#view-stats')).toHaveClass(/\bactive\b/);
  await expect(warning).toBeVisible();

  await page.locator('#sidebar .nav-item[data-view="dashboard"]').click();
  await expect(page.locator('#view-dashboard')).toHaveClass(/\bactive\b/);
  await expect(warning).toBeVisible();
});

test('application storage degradation stays visible independently of DB-backed API availability', async ({ page }) => {
  const current = health({
    applicationState: 'unavailable',
    applicationGeneration: 10,
    applicationOptions: {
      total_bytes: null,
      free_bytes: null,
      filesystem_id: null,
      accessible: false,
      writable: null,
    },
    downloadState: 'healthy',
  });
  await routeStorageHealth(page, () => current);
  await page.route(url => url.pathname === '/api/stats', route => route.fulfill({
    status: 503,
    contentType: 'application/json',
    body: JSON.stringify({detail: 'Application Storage is unavailable'}),
  }));

  await page.goto('/');

  const warning = page.locator('.dp-storage-warning[data-storage-domain="application_state"]');
  await expect(warning).toBeVisible();
  await expect(warning.locator('.dp-storage-warning__domain')).toHaveText('Application Storage');
  await expect(warning.locator('.dp-storage-warning__state')).toHaveText('Unavailable');
  await expect(warning.locator('.dp-storage-warning__copy')).toContainText('cannot safely persist state');
  await expect(page.locator('#view-dashboard')).toHaveClass(/\bactive\b/);
  expect(await warning.textContent()).not.toMatch(/sqlite|operationalerror|disk i\/o error/i);
});
