const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Authentication reorganization -- proven against the RENDERED page
 * and the REAL backend rather than against selectors.
 *
 * Authentication joins Services, Downloads and Extraction: every control
 * commits at its own boundary through the canonical persistence owner, so the
 * tab has no Apply Settings responsibility of any kind, and no generic Apply
 * can replay a stale Authentication value from the page.
 *
 * The surfaces with real risk get real proof:
 *   - a secret left blank writes NOTHING, so an untouched field cannot erase a
 *     stored credential;
 *   - clearing a credential is one explicit confirmed act, and declining it
 *     mutates nothing;
 *   - Token Ready reflects DURABLE stored-token state, never a token value on
 *     screen and never the one-time disclosure block;
 *   - the freshly disclosed token is ephemeral: it is not configuration, and
 *     re-entering Settings does not bring it back.
 *
 * Every case restores what it changed, so the shared backend is left as found.
 */

const TOLERANCE = 2;

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

async function openSettings(page, tab) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator(`#view-settings [data-tab="${tab}"]`).click();
  await expect(page.locator(`.dp-settings-panel[data-panel="${tab}"]`)).toBeVisible();
}

/** Canonical authentication state, straight from the configuration boundary. */
async function auth(page) {
  const response = await page.request.get('/api/auth/config');
  expect(response.ok()).toBeTruthy();
  return response.json();
}

async function restoreAuth(page, before) {
  await page.request.put('/api/auth/config', {data: {
    auth_username: before.username,
    auth_session_lifetime_hours: before.session_lifetime_hours,
    oidc_provider_name: before.oidc_provider_name,
    oidc_issuer_url: before.oidc_issuer_url,
    oidc_client_id: before.oidc_client_id,
    oidc_scopes: before.oidc_scopes,
    oidc_group_claim: before.oidc_group_claim,
    oidc_allow_all: before.oidc_allow_all,
    oidc_allowed_subjects: before.oidc_allowed_subjects,
    oidc_allowed_emails: before.oidc_allowed_emails,
    oidc_allowed_groups: before.oidc_allowed_groups,
  }});
}

const field = (page, key) => page.locator(`#dp-settings-field-${key.replaceAll('_', '-')}`);

test('Authentication carries no Apply contract at all', async ({page}) => {
  await page.goto('/');
  await openSettings(page, 'authentication');

  await expect(page.locator('#view-settings [data-action="save"]')).toBeHidden();
  await expect(page.locator('#view-settings .dp-settings-save-hint')).toBeHidden();

  // The deferred clear-on-save secret mechanism is gone entirely, not hidden.
  await expect(page.locator('#dp-auth-clear-oidc-secret')).toHaveCount(0);
  await expect(page.locator('#view-settings [data-clear-secret="auth_password"]')).toHaveCount(0);

  // Its replacement is an explicit destructive button beside the secret field.
  await expect(page.locator('#view-settings [data-action="clear-oidc-secret"]'))
    .toHaveClass(/\bbtn-danger\b/);

  // The one surviving footer responsibility is the explicit OIDC probe.
  await expect(page.locator('#view-settings [data-action="verify-oidc"]')).toBeVisible();
});

test('the redundant Current Authentication Mechanism reading is gone, not hidden',
  async ({page}) => {
    await page.goto('/');
    await openSettings(page, 'authentication');

    await expect(page.locator('#view-settings [data-panel="authentication"]'))
      .not.toContainText('Current Authentication Mechanism');
    // The KPI it duplicated remains the one authority.
    await expect(page.locator('.dp-settings-auth-kpi[data-auth-kpi="mode"]')).toBeVisible();
    await expect(page.locator('.dp-settings-auth-kpi')).toHaveCount(4);
  });

test('session information and its control are one centred island with Log Out inside the field',
  async ({page}) => {
    await isolateExternalFonts(page);
    await page.goto('/');
    await openSettings(page, 'authentication');

    const measured = await page.locator('.dp-settings-auth-session-row').evaluate(el => {
      const rect = node => node.getBoundingClientRect();
      const body = el.closest('.card-body');
      const island = rect(el);
      const parent = rect(body);
      const count = el.querySelector('.dp-settings-auth-session-count');
      const logout = el.querySelector('[data-action="logout-session"]');
      const value = el.querySelector('[data-auth-session-count]');
      const style = getComputedStyle(el);
      return {
        bordered: style.borderTopWidth !== '0px',
        // Bounded: it does not span the body it sits in.
        bounded: island.width < parent.width - 40,
        centred: Math.abs((island.left + island.right) / 2 - (parent.left + parent.right) / 2),
        fields: el.querySelectorAll(':scope > .dp-settings-inline-field').length,
        oneLine: Math.abs(rect(el.children[0]).top - rect(el.children[1]).top) < 3,
        logoutInsideField: count.contains(logout),
        // The count and the action are flex siblings, so no reserved width
        // stands in for a button whose size the font decides.
        clear: rect(logout).left - rect(value).right,
        ghost: logout.className.includes('btn-ghost'),
      };
    });

    expect(measured.bordered).toBe(true);
    expect(measured.bounded, 'the session island stretches across the card').toBe(true);
    expect(measured.centred, 'the session island is not centred').toBeLessThanOrEqual(TOLERANCE);
    expect(measured.fields).toBe(2);
    expect(measured.oneLine).toBe(true);
    expect(measured.logoutInsideField, 'Log Out is not inside the sessions field').toBe(true);
    expect(measured.clear, 'the session count can collide with Log Out').toBeGreaterThan(0);
    expect(measured.ghost).toBe(true);
  });

test('ordinary Authentication values commit at their own boundary, with no Apply anywhere',
  async ({page}) => {
    await page.goto('/');
    const before = await auth(page);
    try {
      await openSettings(page, 'authentication');

      // Changed blur: nothing is written before the boundary is crossed.
      const username = before.username === 'dp-probe-a' ? 'dp-probe-b' : 'dp-probe-a';
      await field(page, 'auth_username').fill(username);
      expect((await auth(page)).username,
        'the username was written before its commit boundary').toBe(before.username);
      await field(page, 'auth_username').blur();
      await expect.poll(async () => (await auth(page)).username).toBe(username);

      // A list value keeps its canonical list shape across the boundary.
      const scopes = before.oidc_scopes.join(' ') === 'openid email'
        ? 'openid profile' : 'openid email';
      await field(page, 'oidc_scopes').fill(scopes);
      await field(page, 'oidc_scopes').blur();
      await expect.poll(async () => (await auth(page)).oidc_scopes.join(' ')).toBe(scopes);

      // A multiline allowlist keeps its line semantics.
      await field(page, 'oidc_allowed_emails').fill('one@example.com\ntwo@example.com');
      await field(page, 'oidc_allowed_emails').blur();
      await expect.poll(async () => (await auth(page)).oidc_allowed_emails)
        .toEqual(['one@example.com', 'two@example.com']);

      // An ordinary boolean is immediate.
      await page.locator('.dp-settings-oidc-allow-all .ttrack').click();
      await expect.poll(async () => (await auth(page)).oidc_allow_all).toBe(!before.oidc_allow_all);

      // All of it survives a reload, with no Apply pressed anywhere.
      await page.reload();
      await openSettings(page, 'authentication');
      await expect(field(page, 'auth_username')).toHaveValue(username);
      await expect(field(page, 'oidc_scopes')).toHaveValue(scopes);
    } finally {
      await restoreAuth(page, before);
    }
  });

test('an untouched blank secret writes nothing, and a typed one is never echoed back',
  async ({page}) => {
    await page.goto('/');
    const before = await auth(page);
    const writes = [];
    await page.route('**/api/auth/config', route => {
      if (route.request().method() === 'PUT') writes.push(route.request().postDataJSON());
      return route.continue();
    });
    try {
      await openSettings(page, 'authentication');

      // Both secret fields render blank: blank IS their canonical baseline.
      await expect(field(page, 'auth_password')).toHaveValue('');
      await expect(field(page, 'oidc_client_secret')).toHaveValue('');

      // Focusing and leaving one writes NOTHING -- blank means keep.
      await field(page, 'auth_password').click();
      await field(page, 'oidc_client_secret').click();
      await field(page, 'oidc_client_id').click();
      await page.waitForTimeout(400);
      expect(writes, 'an untouched blank secret was serialized').toEqual([]);
      expect((await auth(page)).password_configured).toBe(before.password_configured);
      expect((await auth(page)).oidc_client_secret_configured)
        .toBe(before.oidc_client_secret_configured);

      // A typed secret commits, carrying ONLY itself, and converges to blank:
      // the stored value is never projected back into the browser.
      await field(page, 'oidc_client_secret').fill('probe-client-secret');
      await field(page, 'oidc_client_id').click();
      await expect.poll(async () => (await auth(page)).oidc_client_secret_configured).toBe(true);
      expect(writes).toEqual([{oidc_client_secret: 'probe-client-secret'}]);
      await expect(field(page, 'oidc_client_secret')).toHaveValue('');

      // Declining the destructive clear mutates nothing.
      await page.locator('[data-action="clear-oidc-secret"]').click();
      await page.locator('.dp-modal-overlay [data-modal-cancel]').click();
      await page.waitForTimeout(300);
      expect((await auth(page)).oidc_client_secret_configured).toBe(true);

      // Accepting it is the ONE clear path, and it is confirmed.
      await page.locator('[data-action="clear-oidc-secret"]').click();
      await page.locator('.dp-modal-overlay [data-modal-accept]').click();
      await expect.poll(async () => (await auth(page)).oidc_client_secret_configured).toBe(false);
    } finally {
      await page.request.put('/api/auth/config', {data: {clear_oidc_client_secret: true}});
      await restoreAuth(page, before);
    }
  });

test('Token Ready is durable stored-token state, and the disclosure is ephemeral',
  async ({page}) => {
    await page.goto('/');
    const before = await auth(page);
    const badge = page.locator('[data-auth-token-ready]');
    const disclosure = page.locator('.dp-settings-api-token-disclosure');
    try {
      await openSettings(page, 'authentication');

      if (before.api_token_configured) {
        await page.locator('[data-action="clear-token"]').click();
        await page.locator('.dp-modal-overlay [data-modal-accept]').click();
        await expect.poll(async () => (await auth(page)).api_token_configured).toBe(false);
      }
      await expect(badge).toBeHidden();
      await expect(disclosure).toHaveCount(0);

      await page.locator('[data-action="generate-token"]').click();
      await expect.poll(async () => (await auth(page)).api_token_configured).toBe(true);

      // Both appear, but they are different things.
      await expect(badge).toBeVisible();
      await expect(disclosure).toBeVisible();
      await expect(page.locator('.dp-settings-api-token-warning'))
        .toHaveText('Copy this token now. DebridPulse will not display it again.');

      // The disclosed value is NOT configuration: the persistence owner, every
      // baseline and every payload this page builds are blind to it.
      const shown = page.locator('#dp-settings-api-token-once');
      await expect(shown).not.toHaveAttribute('data-setting', /.*/);
      await expect(shown).not.toHaveAttribute('data-commit', /.*/);
      await expect(shown).toHaveAttribute('readonly', '');
      expect(await shown.inputValue()).not.toBe('');

      // Copy is embedded in the field, inside its border.
      const copyInside = await disclosure.evaluate(el => {
        const rect = node => node.getBoundingClientRect();
        const compound = el.querySelector('.dp-action-field');
        const copy = el.querySelector('[data-action="copy-token"]');
        const input = el.querySelector('.input');
        return rect(copy).right <= rect(compound).right + 0.5
          && rect(copy).left >= rect(input).right - 0.5;
      });
      expect(copyInside, 'Copy is not embedded at the field\'s trailing edge').toBe(true);

      // Leaving Settings and returning is not the act that produced the token.
      await page.locator('#sidebar .nav-item[data-view="dashboard"]').click();
      await openSettings(page, 'authentication');
      await expect(disclosure).toHaveCount(0);
      // Token Ready is unchanged, because a token IS still stored -- it was
      // never derived from the disclosure block or from a value on screen.
      await expect(badge).toBeVisible();

      // A reload cannot reconstruct it either.
      await page.reload();
      await openSettings(page, 'authentication');
      await expect(disclosure).toHaveCount(0);
      await expect(badge).toBeVisible();

      // Revoking removes the durable state, and the badge with it.
      await page.locator('[data-action="clear-token"]').click();
      await page.locator('.dp-modal-overlay [data-modal-accept]').click();
      await expect.poll(async () => (await auth(page)).api_token_configured).toBe(false);
      await expect(badge).toBeHidden();
    } finally {
      if (!before.api_token_configured && (await auth(page)).api_token_configured) {
        await page.request.delete('/api/auth/api-token');
      }
      await page.request.put('/api/auth/api-token', {data: {enabled: before.api_token_enabled}});
    }
  });

test('the generic Apply carries no Authentication value read from the page', async ({page}) => {
  /* Moving to another tab already crosses the changed-blur boundary, so an
   * Authentication draft is committed by its OWN scope long before any Apply.
   * To prove the footer owns none of it, this holds the auth scope open --
   * every /auth/config write fails -- so a draft genuinely cannot be committed.
   * If the footer had any Authentication path at all, that draft would reach
   * the server through the whole-settings write instead. */
  await page.goto('/');
  const before = await auth(page);
  const settings = await (await page.request.get('/api/settings')).json();
  const applied = [];

  await page.route('**/api/auth/config', route => route.request().method() === 'PUT'
    ? route.fulfill({status: 503, contentType: 'application/json',
        body: JSON.stringify({detail: 'auth scope held open by the test'})})
    : route.continue());
  await page.route('**/api/settings', route => {
    if (route.request().method() === 'PUT') applied.push(route.request().postDataJSON());
    return route.continue();
  });

  try {
    await openSettings(page, 'authentication');
    await field(page, 'auth_username').fill('stale-apply-draft');
    await field(page, 'oidc_provider_name').fill('stale-provider-draft');

    // A deferred write on a tab that still has an Apply contract.
    await page.locator('#view-settings [data-tab="notifications"]').click();
    await page.locator('#dp-settings-field-discord-username').fill('AuthReplayProbe');
    await page.locator('#view-settings [data-action="save"]').click();
    await expect.poll(async () => (await (await page.request.get('/api/settings')).json()).discord_username)
      .toBe('AuthReplayProbe');

    expect(applied.length).toBeGreaterThan(0);
    for (const body of applied) {
      expect(body.auth_username, 'Apply carried an Authentication draft from the page')
        .toBe(before.username);
      expect(body.oidc_provider_name, 'Apply carried an Authentication draft from the page')
        .toBe(before.oidc_provider_name);
      expect(body.clear_secrets || [], 'Apply carried an Authentication secret clear')
        .not.toContain('auth_password');
    }

    const after = await auth(page);
    expect(after.username, 'Apply mutated Authentication').toBe(before.username);
    expect(after.oidc_provider_name, 'Apply mutated Authentication').toBe(before.oidc_provider_name);
    expect(after.password_configured, 'Apply erased the stored password')
      .toBe(before.password_configured);
    expect(after.oidc_client_secret_configured, 'Apply erased the stored client secret')
      .toBe(before.oidc_client_secret_configured);
  } finally {
    await page.unroute('**/api/auth/config');
    await page.unroute('**/api/settings');
    await page.request.put('/api/settings', {data: {
      ...settings,
      integrations: undefined, integration_groups: undefined,
      transfer_policy: undefined, execution_runtime_limits: undefined,
      compatibility_fields: undefined,
      clear_secrets: [],
      discord_username: settings.discord_username,
    }});
    await restoreAuth(page, before);
  }
});
