const { test, expect } = require('@playwright/test');

const validKey = '-----BEGIN OPENSSH PRIVATE KEY-----\nZmFrZS1hc3luYy1yYWNl\n-----END OPENSSH PRIVATE KEY-----\n';

function keyChallenge() {
  return {
    id: 1099,
    name: 'Async key ingestion fixture',
    status: 'input_required',
    progress: 0,
    size_bytes: 0,
    source: 'fixture',
    hash: '',
    label: '',
    created_at: '2026-09-02T00:00:00Z',
    error: null,
    error_message: null,
    input_required: {
      id: 'async-key-race',
      generation: 1,
      reason: 'auth_required',
      origin: 'provider',
      methods: [{
        method: 'username_private_key',
        fields: [
          {name: 'username', required: true},
          {name: 'private_key', required: true},
          {name: 'passphrase', required: false},
        ],
      }],
    },
  };
}

test('typing while a private key is asynchronously read survives the key-selection rerender', async ({ page }) => {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({
    status: 200,
    contentType: 'text/css',
    body: '',
  }));

  const item = keyChallenge();
  await page.route('**/api/torrents**', route => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === 'GET' && url.pathname === '/api/torrents') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({items: [item], total: 1}),
      });
    }
    return route.continue();
  });

  await page.addInitScript(() => {
    const originalText = File.prototype.text;
    File.prototype.text = function delayedText() {
      const file = this;
      return new Promise((resolve, reject) => {
        window.setTimeout(() => {
          originalText.call(file).then(resolve, reject);
        }, 300);
      });
    };
  });

  await page.goto('/');
  const modal = page.locator('[data-dp-input-required-modal]');
  await expect(modal).toBeVisible();
  await expect(modal.locator('[data-dp-auth-secret-label]')).toHaveText('Passphrase');

  const chooserPromise = page.waitForEvent('filechooser');
  await modal.locator('[data-dp-auth-key]').click();
  const chooser = await chooserPromise;
  await chooser.setFiles({
    name: 'id_async_race',
    mimeType: 'text/plain',
    buffer: Buffer.from(validKey),
  });

  await modal.locator('[data-dp-auth-username]').fill('async-user-sentinel');
  await modal.locator('[data-dp-auth-secret]').fill('async-passphrase-sentinel');

  await expect(modal.locator('[data-dp-auth-key]')).toHaveAttribute('aria-pressed', 'true');
  await expect(modal.locator('[data-dp-auth-username]')).toHaveValue('async-user-sentinel');
  await expect(modal.locator('[data-dp-auth-secret]')).toHaveValue('async-passphrase-sentinel');
});
