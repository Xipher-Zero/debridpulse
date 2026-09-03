const { test, expect } = require('@playwright/test');

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({
    status: 200,
    contentType: 'text/css',
    body: '',
  }));
}

function passwordMethod() {
  return {
    method: 'username_password',
    fields: [
      { name: 'username', required: true },
      { name: 'password', required: true },
    ],
  };
}

function authItem(id, challengeId, generation, status = 'input_required') {
  return {
    id,
    name: `Authentication visual fixture ${id}`,
    status,
    progress: 0,
    size_bytes: 0,
    source: 'fixture',
    hash: '',
    label: '',
    created_at: '2026-09-03T00:00:00Z',
    error: null,
    error_message: null,
    input_required: status === 'input_required' ? {
      id: challengeId,
      generation,
      reason: 'auth_required',
      origin: 'provider',
      methods: [passwordMethod()],
    } : null,
  };
}

async function installBusyFixture(page) {
  const item = authItem(1099, 'checkpoint-auth-busy', 1);
  let releaseSubmission;
  const submissionGate = new Promise(resolve => { releaseSubmission = resolve; });

  await page.route('**/api/torrents**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();

    if (url.pathname === '/api/torrents' && method === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [item], total: 1 }),
      });
    }

    if (url.pathname === '/api/torrents/1099' && method === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(item),
      });
    }

    if (url.pathname === '/api/torrents/1099/input' && method === 'POST') {
      await submissionGate;
      item.status = 'queued';
      item.input_required = null;
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ok: true,
          accepted: true,
          id: 1099,
          challenge_id: 'checkpoint-auth-busy',
        }),
      });
    }

    return route.continue();
  });

  return () => releaseSubmission();
}

test('checkpoint visually captures Authentication Required busy state', async ({ page }) => {
  await isolateExternalFonts(page);
  const releaseSubmission = await installBusyFixture(page);
  await page.goto('/');

  const modal = page.locator('[data-dp-input-required-modal]');
  await expect(modal).toBeVisible();
  await modal.locator('[data-dp-auth-username]').fill('busy-user-sentinel');
  await modal.locator('[data-dp-auth-secret]').fill('busy-password-sentinel');
  await modal.locator('[data-dp-auth-continue]').click();

  await expect(modal).toHaveAttribute('aria-busy', 'true');
  await expect(modal.locator('[data-dp-auth-continue]')).toHaveText('Authenticating…');
  await expect(modal.locator('[data-dp-auth-cancel]')).toBeDisabled();
  await page.screenshot({ path: 'test-results/checkpoint-auth-busy-dark-desktop.png', fullPage: true });

  releaseSubmission();
  await expect(modal).toBeHidden();
});
