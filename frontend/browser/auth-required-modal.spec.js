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

function keyMethod() {
  return {
    method: 'username_private_key',
    fields: [
      { name: 'username', required: true },
      { name: 'private_key', required: true },
      { name: 'passphrase', required: false },
    ],
  };
}

function authItem(id, challengeId, generation, methods, status = 'input_required') {
  return {
    id,
    name: `Authentication fixture ${id}`,
    status,
    progress: 0,
    size_bytes: 0,
    source: 'fixture',
    hash: '',
    label: '',
    created_at: '2026-09-02T00:00:00Z',
    error: null,
    error_message: null,
    input_required: status === 'input_required' ? {
      id: challengeId,
      generation,
      reason: 'auth_required',
      origin: 'provider',
      methods,
    } : null,
  };
}

async function installFixture(page, initialItems, onSubmit) {
  const items = new Map(initialItems.map(item => [Number(item.id), structuredClone(item)]));
  const submissions = [];
  const cancellations = [];

  const state = {
    item(id) { return items.get(Number(id)); },
    set(item) { items.set(Number(item.id), structuredClone(item)); },
    remove(id) { items.delete(Number(id)); },
    submissions,
    cancellations,
  };

  await page.route('**/api/torrents**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === '/api/torrents' && method === 'GET') {
      const requestedStatus = url.searchParams.get('status');
      const all = [...items.values()].filter(item => !requestedStatus || item.status === requestedStatus);
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({items: all, total: all.length}),
      });
    }

    const detail = path.match(/^\/api\/torrents\/(\d+)$/);
    if (detail && method === 'GET') {
      const item = items.get(Number(detail[1]));
      return route.fulfill({
        status: item ? 200 : 404,
        contentType: 'application/json',
        body: JSON.stringify(item || {detail: 'Transfer not found'}),
      });
    }

    const input = path.match(/^\/api\/torrents\/(\d+)\/input$/);
    if (input && method === 'POST') {
      const id = Number(input[1]);
      const body = request.postDataJSON();
      submissions.push({id, body});
      if (onSubmit) await onSubmit({id, body, state});
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ok: true, accepted: true, id, challenge_id: body.challenge_id}),
      });
    }

    const cancel = path.match(/^\/api\/torrents\/(\d+)\/cancel$/);
    if (cancel && method === 'POST') {
      const id = Number(cancel[1]);
      cancellations.push(id);
      const item = items.get(id);
      if (item) state.set({...item, status: 'cancelled', input_required: null});
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ok: true, id}),
      });
    }

    return route.continue();
  });

  return state;
}

async function openModal(page) {
  await page.goto('/');
  const modal = page.locator('[data-dp-input-required-modal]');
  await expect(modal).toBeVisible();
  await expect(modal.locator('#dp-auth-required-title')).toHaveText('Authentication Required');
  return modal;
}

async function chooseKey(page, contents, name = 'id_test') {
  const chooserPromise = page.waitForEvent('filechooser');
  await page.locator('[data-dp-auth-key]').click();
  const chooser = await chooserPromise;
  await chooser.setFiles({
    name,
    mimeType: 'text/plain',
    buffer: Buffer.from(contents),
  });
}

const validKeyA = '-----BEGIN OPENSSH PRIVATE KEY-----\nZmFrZS1rZXktQQ==\n-----END OPENSSH PRIVATE KEY-----\n';
const validKeyB = '-----BEGIN OPENSSH PRIVATE KEY-----\nZmFrZS1rZXktQg==\n-----END OPENSSH PRIVATE KEY-----\n';

test('password-only AUTH_REQUIRED renders the generic neutral modal without key selection', async ({ page }) => {
  await isolateExternalFonts(page);
  await installFixture(page, [authItem(1001, 'challenge-password-only', 1, [passwordMethod()])]);
  const modal = await openModal(page);

  await expect(modal.locator('[data-dp-auth-username]')).toBeVisible();
  await expect(modal.locator('[data-dp-auth-secret-label]')).toHaveText('Password');
  await expect(modal.locator('[data-dp-auth-secret]')).toHaveAttribute('type', 'password');
  await expect(modal.locator('[data-dp-auth-key]')).toHaveCount(0);
  await expect(modal.locator('[data-dp-auth-cancel]')).toBeVisible();
  await expect(modal.locator('[data-dp-auth-continue]')).toBeVisible();
});

test('key selection is challenge-driven, validates locally, becomes textual green state, and switches Password to optional Passphrase', async ({ page }) => {
  await isolateExternalFonts(page);
  const fixture = await installFixture(page, [authItem(1002, 'challenge-both', 1, [passwordMethod(), keyMethod()])]);
  const modal = await openModal(page);

  await expect(modal.locator('[data-dp-auth-secret-label]')).toHaveText('Password');
  await expect(modal.locator('[data-dp-auth-key]')).toHaveText('Select Keyfile');

  await chooseKey(page, 'definitely not a private key', 'invalid.txt');
  await expect(modal.locator('[data-dp-auth-error]')).toHaveText('The selected key is not valid.');
  await expect(modal.locator('[data-dp-auth-key]')).toHaveAttribute('aria-pressed', 'false');
  await expect(modal.locator('[data-dp-auth-secret-label]')).toHaveText('Password');
  expect(fixture.submissions).toHaveLength(0);

  await chooseKey(page, validKeyA, 'id_a');
  await expect(modal.locator('[data-dp-auth-key]')).toHaveClass(/is-selected/);
  await expect(modal.locator('[data-dp-auth-key]')).toHaveAttribute('aria-pressed', 'true');
  await expect(modal.locator('[data-dp-auth-key]')).toContainText('Key supplied');
  await expect(modal.locator('[data-dp-auth-secret-label]')).toHaveText('Passphrase');
  await expect(modal.locator('[data-dp-auth-secret]')).not.toHaveAttribute('required', '');
});

test('password authentication keeps the modal open while busy and closes when the challenge resolves even if the transfer is only queued', async ({ page }) => {
  await isolateExternalFonts(page);
  const fixture = await installFixture(
    page,
    [authItem(1003, 'challenge-password-success', 1, [passwordMethod()])],
    async ({id, state}) => {
      await new Promise(resolve => setTimeout(resolve, 180));
      state.set(authItem(id, 'resolved', 2, [], 'queued'));
    },
  );
  const modal = await openModal(page);
  await modal.locator('[data-dp-auth-username]').fill('success-user-sentinel');
  await modal.locator('[data-dp-auth-secret]').fill('success-password-sentinel');
  await modal.locator('[data-dp-auth-continue]').click();

  await expect(modal).toBeVisible();
  await expect(modal).toHaveAttribute('aria-busy', 'true');
  await expect(modal.locator('[data-dp-auth-continue]')).toHaveText('Authenticating…');
  await expect(modal).toBeHidden();

  expect(fixture.submissions).toHaveLength(1);
  expect(fixture.submissions[0].body).toEqual({
    challenge_id: 'challenge-password-success',
    method: 'username_password',
    username: 'success-user-sentinel',
    password: 'success-password-sentinel',
  });
  expect(fixture.item(1003).status).toBe('queued');
});

test('rejected password authentication preserves session fields, adopts the regenerated challenge, and retries successfully', async ({ page }) => {
  await isolateExternalFonts(page);
  let attempt = 0;
  const fixture = await installFixture(
    page,
    [authItem(1004, 'challenge-A', 1, [passwordMethod()])],
    async ({id, state}) => {
      attempt += 1;
      if (attempt === 1) state.set(authItem(id, 'challenge-B', 2, [passwordMethod()]));
      else state.set(authItem(id, 'resolved', 3, [], 'queued'));
    },
  );
  const modal = await openModal(page);
  await modal.locator('[data-dp-auth-username]').fill('retry-user-sentinel');
  await modal.locator('[data-dp-auth-secret]').fill('wrong-password-sentinel');
  await modal.locator('[data-dp-auth-continue]').click();

  await expect(modal.locator('[data-dp-auth-error]')).toContainText('Authentication failed');
  await expect(modal.locator('[data-dp-auth-username]')).toHaveValue('retry-user-sentinel');
  await expect(modal.locator('[data-dp-auth-secret]')).toHaveValue('wrong-password-sentinel');
  await expect(modal.locator('[data-dp-auth-continue]')).toBeEnabled();

  await modal.locator('[data-dp-auth-secret]').fill('correct-password-sentinel');
  await modal.locator('[data-dp-auth-continue]').click();
  await expect(modal).toBeHidden();

  expect(fixture.submissions.map(entry => entry.body.challenge_id)).toEqual(['challenge-A', 'challenge-B']);
});

test('key replacement discards key A from submission and unencrypted key authentication permits an empty passphrase', async ({ page }) => {
  await isolateExternalFonts(page);
  const fixture = await installFixture(
    page,
    [authItem(1005, 'challenge-key', 1, [keyMethod()])],
    async ({id, state}) => state.set(authItem(id, 'resolved', 2, [], 'queued')),
  );
  const modal = await openModal(page);

  await chooseKey(page, validKeyA, 'key-a');
  await expect(modal.locator('[data-dp-auth-key]')).toContainText('Key supplied');
  await chooseKey(page, validKeyB, 'key-b');
  await modal.locator('[data-dp-auth-username]').fill('key-user-sentinel');
  await expect(modal.locator('[data-dp-auth-secret-label]')).toHaveText('Passphrase');
  await expect(modal.locator('[data-dp-auth-secret]')).toHaveValue('');
  await modal.locator('[data-dp-auth-continue]').click();
  await expect(modal).toBeHidden();

  expect(fixture.submissions).toHaveLength(1);
  const body = fixture.submissions[0].body;
  expect(body.method).toBe('username_private_key');
  expect(body.private_key).toBe(validKeyB);
  expect(body.private_key).not.toBe(validKeyA);
  expect(body).not.toHaveProperty('passphrase');
});

test('wrong passphrase preserves username, selected key, and passphrase until a corrected retry succeeds', async ({ page }) => {
  await isolateExternalFonts(page);
  let attempt = 0;
  const fixture = await installFixture(
    page,
    [authItem(1006, 'key-pass-A', 1, [keyMethod()])],
    async ({id, state}) => {
      attempt += 1;
      if (attempt === 1) state.set(authItem(id, 'key-pass-B', 2, [keyMethod()]));
      else state.set(authItem(id, 'resolved', 3, [], 'queued'));
    },
  );
  const modal = await openModal(page);
  await chooseKey(page, validKeyA);
  await modal.locator('[data-dp-auth-username]').fill('passphrase-user-sentinel');
  await modal.locator('[data-dp-auth-secret]').fill('wrong-passphrase-sentinel');
  await modal.locator('[data-dp-auth-continue]').click();

  await expect(modal.locator('[data-dp-auth-error]')).toContainText('Authentication failed');
  await expect(modal.locator('[data-dp-auth-username]')).toHaveValue('passphrase-user-sentinel');
  await expect(modal.locator('[data-dp-auth-key]')).toHaveAttribute('aria-pressed', 'true');
  await expect(modal.locator('[data-dp-auth-secret]')).toHaveValue('wrong-passphrase-sentinel');

  await modal.locator('[data-dp-auth-secret]').fill('correct-passphrase-sentinel');
  await modal.locator('[data-dp-auth-continue]').click();
  await expect(modal).toBeHidden();
  expect(fixture.submissions.map(entry => entry.body.challenge_id)).toEqual(['key-pass-A', 'key-pass-B']);
});

test('challenge regeneration removes key UI and clears key/passphrase when the new challenge no longer advertises key authentication', async ({ page }) => {
  await isolateExternalFonts(page);
  const fixture = await installFixture(
    page,
    [authItem(1007, 'method-A', 1, [passwordMethod(), keyMethod()])],
    async ({id, state}) => state.set(authItem(id, 'method-B', 2, [passwordMethod()])),
  );
  const modal = await openModal(page);
  await chooseKey(page, validKeyA);
  await modal.locator('[data-dp-auth-username]').fill('method-user-sentinel');
  await modal.locator('[data-dp-auth-secret]').fill('key-passphrase-sentinel');
  await modal.locator('[data-dp-auth-continue]').click();

  await expect(modal.locator('[data-dp-auth-error]')).toContainText('Authentication failed');
  await expect(modal.locator('[data-dp-auth-key]')).toHaveCount(0);
  await expect(modal.locator('[data-dp-auth-secret-label]')).toHaveText('Password');
  await expect(modal.locator('[data-dp-auth-username]')).toHaveValue('method-user-sentinel');
  await expect(modal.locator('[data-dp-auth-secret]')).toHaveValue('');
  expect(fixture.submissions[0].body.challenge_id).toBe('method-A');
});

test('successful authentication closes on challenge resolution while the same logical transfer remains paused', async ({ page }) => {
  await isolateExternalFonts(page);
  const fixture = await installFixture(
    page,
    [authItem(1008, 'paused-auth', 1, [passwordMethod()])],
    async ({id, state}) => state.set(authItem(id, 'resolved', 2, [], 'paused')),
  );
  const modal = await openModal(page);
  await modal.locator('[data-dp-auth-username]').fill('paused-user-sentinel');
  await modal.locator('[data-dp-auth-secret]').fill('paused-password-sentinel');
  await modal.locator('[data-dp-auth-continue]').click();
  await expect(modal).toBeHidden();
  expect(fixture.item(1008).id).toBe(1008);
  expect(fixture.item(1008).status).toBe('paused');
});

test('Cancel and Escape resolve the canonical pending challenge instead of only hiding the dialog', async ({ page }) => {
  await isolateExternalFonts(page);
  const fixture = await installFixture(page, [authItem(1009, 'cancel-A', 1, [passwordMethod()])]);
  let modal = await openModal(page);
  await modal.locator('[data-dp-auth-username]').fill('cancel-user-sentinel');
  await modal.locator('[data-dp-auth-secret]').fill('cancel-password-sentinel');
  await modal.locator('[data-dp-auth-cancel]').click();
  await expect(modal).toBeHidden();
  expect(fixture.cancellations).toEqual([1009]);

  fixture.set(authItem(1010, 'cancel-B', 1, [passwordMethod()]));
  await page.evaluate(() => window.DPAuthRequired.scan());
  modal = page.locator('[data-dp-input-required-modal]');
  await expect(modal).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(modal).toBeHidden();
  expect(fixture.cancellations).toEqual([1009, 1010]);
});

test('multiple simultaneous challenges are queued deterministically and never carry credentials into the next transfer', async ({ page }) => {
  await isolateExternalFonts(page);
  const fixture = await installFixture(page, [
    authItem(1012, 'queue-B', 1, [passwordMethod()]),
    authItem(1011, 'queue-A', 1, [passwordMethod()]),
  ]);
  let modal = await openModal(page);
  await expect(modal).toHaveAttribute('data-dp-auth-transfer-id', '1011');
  await modal.locator('[data-dp-auth-username]').fill('transfer-A-user-sentinel');
  await modal.locator('[data-dp-auth-secret]').fill('transfer-A-password-sentinel');
  await modal.locator('[data-dp-auth-cancel]').click();

  modal = page.locator('[data-dp-input-required-modal]');
  await expect(modal).toBeVisible();
  await expect(modal).toHaveAttribute('data-dp-auth-transfer-id', '1012');
  await expect(modal.locator('[data-dp-auth-username]')).toHaveValue('');
  await expect(modal.locator('[data-dp-auth-secret]')).toHaveValue('');
  expect(fixture.cancellations).toEqual([1011]);
});

test('page reload restores the challenge but not username, key, or passphrase session material', async ({ page }) => {
  await isolateExternalFonts(page);
  await installFixture(page, [authItem(1013, 'reload-key', 1, [passwordMethod(), keyMethod()])]);
  let modal = await openModal(page);
  await chooseKey(page, validKeyA);
  await modal.locator('[data-dp-auth-username]').fill('reload-user-sentinel');
  await modal.locator('[data-dp-auth-secret]').fill('reload-passphrase-sentinel');
  await expect(modal.locator('[data-dp-auth-key]')).toHaveAttribute('aria-pressed', 'true');

  await page.reload();
  modal = page.locator('[data-dp-input-required-modal]');
  await expect(modal).toBeVisible();
  await expect(modal.locator('[data-dp-auth-username]')).toHaveValue('');
  await expect(modal.locator('[data-dp-auth-secret-label]')).toHaveText('Password');
  await expect(modal.locator('[data-dp-auth-secret]')).toHaveValue('');
  await expect(modal.locator('[data-dp-auth-key]')).toHaveAttribute('aria-pressed', 'false');
});

test('authentication sentinels never enter browser persistence, URL/history, or console output', async ({ page }) => {
  await isolateExternalFonts(page);
  await page.addInitScript(() => {
    window.__dpAuthLeakHits = [];
    const originalSet = Storage.prototype.setItem;
    Storage.prototype.setItem = function(key, value) {
      const probe = `${key}:${value}`;
      if (probe.includes('auth-storage-sentinel')) window.__dpAuthLeakHits.push(`storage:${probe}`);
      return originalSet.call(this, key, value);
    };
    for (const method of ['log', 'info', 'warn', 'error']) {
      const original = console[method];
      console[method] = function(...args) {
        const probe = args.map(value => String(value)).join(' ');
        if (probe.includes('auth-storage-sentinel')) window.__dpAuthLeakHits.push(`console:${probe}`);
        return original.apply(this, args);
      };
    }
  });
  await installFixture(page, [authItem(1014, 'leak-check', 1, [passwordMethod(), keyMethod()])]);
  const modal = await openModal(page);
  await chooseKey(page,
    '-----BEGIN OPENSSH PRIVATE KEY-----\nauth-storage-sentinel-key\n-----END OPENSSH PRIVATE KEY-----\n',
    'leak-key');
  await modal.locator('[data-dp-auth-username]').fill('auth-storage-sentinel-user');
  await modal.locator('[data-dp-auth-secret]').fill('auth-storage-sentinel-passphrase');

  const probe = await page.evaluate(async () => {
    const local = Object.fromEntries(Object.keys(localStorage).map(key => [key, localStorage.getItem(key)]));
    const session = Object.fromEntries(Object.keys(sessionStorage).map(key => [key, sessionStorage.getItem(key)]));
    const databases = indexedDB.databases ? await indexedDB.databases() : [];
    return {
      local: JSON.stringify(local),
      session: JSON.stringify(session),
      url: location.href,
      history: history.state == null ? '' : JSON.stringify(history.state),
      databases: JSON.stringify(databases),
      hits: window.__dpAuthLeakHits || [],
    };
  });
  for (const value of [probe.local, probe.session, probe.url, probe.history, probe.databases]) {
    expect(value).not.toContain('auth-storage-sentinel');
  }
  expect(probe.hits).toEqual([]);
});

test('modal keeps dialog semantics, focus containment, light-theme readability hooks, and mobile-width geometry', async ({ page }) => {
  await isolateExternalFonts(page);
  await page.setViewportSize({width: 390, height: 800});
  await installFixture(page, [authItem(1015, 'mobile-auth', 1, [passwordMethod(), keyMethod()])]);
  const modal = await openModal(page);

  await expect(modal).toHaveAttribute('role', 'dialog');
  await expect(modal).toHaveAttribute('aria-modal', 'true');
  await expect(modal.locator('[data-dp-auth-username]')).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(modal.locator('[data-dp-auth-continue]')).toBeFocused();

  await page.evaluate(() => document.body.classList.add('light'));
  const geometry = await modal.evaluate(node => {
    const rect = node.getBoundingClientRect();
    return {left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, width: rect.width};
  });
  expect(geometry.left).toBeGreaterThanOrEqual(0);
  expect(geometry.right).toBeLessThanOrEqual(390);
  expect(geometry.top).toBeGreaterThanOrEqual(0);
  expect(geometry.bottom).toBeLessThanOrEqual(800);
  expect(geometry.width).toBeGreaterThan(300);
});
