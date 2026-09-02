const { test, expect } = require('@playwright/test');

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({
    status: 200,
    contentType: 'text/css',
    body: '',
  }));
}

function observeRuntime(page) {
  const errors = [];
  page.on('pageerror', error => errors.push(`pageerror: ${error.message}`));
  page.on('console', message => {
    if (message.type() === 'error') errors.push(`console: ${message.text()}`);
  });
  return errors;
}

test('INPUT_REQUIRED renders as a neutral nonterminal transfer state without a modal or error treatment', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  const item = {
    id: 903,
    name: 'Input required fixture',
    status: 'input_required',
    progress: 0,
    size_bytes: 0,
    source: 'manual',
    hash: '',
    label: '',
    created_at: '2026-09-01T00:00:00Z',
    error: null,
    error_message: null,
    input_required: {
      id: 'challenge-browser-public-id',
      generation: 1,
      reason: 'auth_required',
      origin: 'provider',
      methods: [
        {
          method: 'username_password',
          fields: [
            { name: 'username', required: true },
            { name: 'password', required: true },
          ],
        },
      ],
    },
  };

  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ items: [item], total: 1 }),
  }));

  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await expect(page.locator('#view-torrents')).toHaveClass(/\bactive\b/);

  const row = page.locator('#t-tbody tr[data-torrent-id="903"]');
  await expect(row).toBeVisible();
  await expect(row).toHaveAttribute('data-status', 'input_required');
  const status = row.locator('[data-role="transfer-status"]');
  await expect(status.locator('.badge-input_required')).toHaveCount(1);
  await expect(status).toContainText(/input required/i);
  await expect(status.locator('.badge-error')).toHaveCount(0);
  await expect(row.locator('.dp-terminal-error-progress')).toHaveCount(0);

  // Item 4 owns the generic authentication modal. Item 3 must not create one.
  await expect(page.locator('[data-dp-input-required-modal]')).toHaveCount(0);
  expect(errors).toEqual([]);
});
