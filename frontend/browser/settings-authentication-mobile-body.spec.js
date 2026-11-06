const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Username & Password and OpenID Connect bodies below 780px.
 *
 * Each setting is its title over its control at the field gap -- the row's
 * flex bases no longer become heights in the handheld column -- and its hint
 * is said by its title through the one tooltip owner (fitAuthBody,
 * promoteHints). Help, lent by the OpenID Connect header, sits in the
 * identity group's top-right corner. Access Control: no rule before it, its
 * heading centred, the policy's explanation under the toggle (which says
 * "Allow Any Identity" where its full words would not fit), the role
 * centred, the allowlists' guidance kept visible and centred. From 780px
 * nothing changes.
 *
 * The auth document is SERVED with synthetic values; every write is refused. */

const AUTH = {mode: 'Username & Password', authentication_required: true, password_enabled: true, password_ready: true,
  password_configured: true, username: 'synthetic-admin', oidc_configured: true, oidc_enabled: false,
  oidc_client_secret_configured: true, oidc_issuer_url: 'https://id.example.invalid/realms/synthetic',
  oidc_client_id: 'debridpulse-synthetic-client-0123456789abcdef-0123456789abcdef',
  public_base_url: 'https://debridpulse.example.invalid', oidc_allow_all: false,
  oidc_allowed_subjects: ['synthetic-subject-1'], oidc_allowed_emails: [], oidc_allowed_groups: ['synthetic-group']};

const FIELDS = ['Username', 'Password', 'Provider Name', 'Public DebridPulse Base URL', 'OIDC Callback URL', 'Issuer URL',
  'Client ID', 'Client Secret', 'Scopes', 'Group Claim', 'Default Role for New OIDC Users'];
const ALLOWLISTS = ['Allowed Subjects', 'Allowed Emails', 'Allowed Groups'];

const settle = page => page.evaluate(() => Promise.all(document.getAnimations()
  .filter(animation => Number.isFinite(animation.effect?.getComputedTiming().endTime))
  .map(animation => animation.finished.catch(() => null)))
  .then(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))));

async function open(page, auth = AUTH) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  const live = await page.request.get('/api/auth/config').then(response => response.json());
  page.__writes = [];
  await page.route(url => url.pathname.startsWith('/api/') && !/^\/api\/(events|stream|health)/.test(url.pathname), route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'GET' && path === '/api/auth/config') return route.fulfill({json: {...live, ...auth}});
    if (request.method() === 'GET' && path === '/api/auth/oidc/runtime-status') return route.fulfill({json: {oidc_available: true}});
    if (request.method() === 'GET' || /\/test-aria2$/.test(path)) return route.fallback();
    page.__writes.push(`${request.method()} ${path}`);
    return route.abort();
  });
  await page.setViewportSize({width: 1280, height: 1000});
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await page.locator('#view-settings [data-tab="authentication"]').first().click();
  await expect(page.locator('#view-settings [data-panel="authentication"]')).toBeVisible();
  await settle(page);
}

async function resize(page, width) {
  await page.setViewportSize({width, height: 1000});
  await settle(page);
}

// Every Username & Password and OpenID Connect setting as drawn.
const fields = page => page.evaluate(() => {
  const r = node => node && node.getClientRects().length ? node.getBoundingClientRect() : null;
  const panel = document.querySelector('#view-settings [data-panel="authentication"]');
  const inline = [...panel.querySelectorAll('.dp-settings-username-password-card .dp-settings-inline-field, .dp-settings-oidc-card .dp-settings-inline-field')]
    .map(field => {
      const title = field.querySelector('.dp-settings-inline-field-info > .form-label');
      const hint = field.querySelector('.dp-settings-inline-field-info > .form-hint');
      const control = field.querySelector(':scope > .dp-settings-inline-field-control');
      const action = field.querySelector(':scope > .dp-settings-inline-field-action');
      const t = r(title), c = r(control), f = r(field);
      const region = field.closest('.dp-settings-oidc-group');
      return {
        name: title.textContent.trim(), hint: hint.textContent.trim(), hintDrawn: !!r(hint) && r(hint).width > 2,
        tooltip: title.getAttribute('data-tooltip'), touch: title.hasAttribute('data-tooltip-touch'), tabindex: title.getAttribute('tabindex'),
        gap: c.top - t.bottom, below: c.top >= t.bottom - 0.5,
        slack: f.height - (t.height + c.height + (action ? r(action).height + parseFloat(getComputedStyle(field).rowGap || 0) : 0))
          - parseFloat(getComputedStyle(field).rowGap || 0),
        last: region ? region.lastElementChild === field || region.querySelector(':scope > .dp-settings-inline-field:last-of-type') === field : false,
        regionBottom: region ? r(region).bottom - c.bottom - (action ? r(action).height : 0) : null,
        inside: f.left >= r(field.parentElement).left - 0.5 && f.right <= r(field.parentElement).right + 0.5,
      };
    });
  const lists = [...panel.querySelectorAll('.dp-settings-oidc-sandwich')].map(field => {
    const hint = field.querySelector(':scope > .form-hint');
    return {name: field.querySelector('.form-label').textContent.trim(), hintDrawn: !!r(hint) && r(hint).width > 2,
      align: getComputedStyle(hint).textAlign, below: r(hint).top >= r(field.querySelector('textarea')).bottom,
      tooltip: field.querySelector('.form-label').getAttribute('data-tooltip')};
  });
  return {inline, lists, overflow: document.documentElement.scrollWidth > innerWidth};
});

test('each setting is its title over its control, its hint on its title, and every group fits what it holds', async ({page}) => {
  await open(page);
  for (const width of [779, 560, 390, 360, 320, 390, 779]) {
    await resize(page, width);
    const now = await fields(page);
    expect(now.overflow, `@${width}: horizontal overflow`).toBe(false);
    expect(now.inline.map(field => field.name), `@${width}`).toEqual(FIELDS);
    for (const field of now.inline) {
      const at = `${field.name} @${width}`;
      expect(field.below, `${at}: control beside its title`).toBe(true);
      expect(field.gap, `${at}: title to control`).toBeLessThanOrEqual(12);
      expect(field.slack, `${at}: empty height in the setting`).toBeLessThanOrEqual(2);
      expect(field.inside, `${at}: escapes`).toBe(true);
      expect(field.hintDrawn, `${at}: hint drawn`).toBe(false);
      expect([field.tooltip, field.touch, field.tabindex], `${at}: title tooltip`).toEqual([field.hint, true, '0']);
      // A group's last setting: its control, then the group's own padding.
      if (field.last) expect(field.regionBottom, `${at}: empty space under the group's last control`).toBeLessThanOrEqual(19);
    }
    for (const list of now.lists) {
      const at = `${list.name} @${width}`;
      expect([list.hintDrawn, list.align, list.below, list.tooltip], at).toEqual([true, 'center', true, null]);
    }
    expect(now.lists.map(list => list.name)).toEqual(ALLOWLISTS);
  }
  // From 780px: every hint drawn, no title a tooltip or a tab stop.
  for (const width of [780, 1280]) {
    await resize(page, width);
    const wide = await fields(page);
    for (const field of wide.inline) {
      expect([field.hintDrawn, field.tooltip, field.touch, field.tabindex], `${field.name} @${width}`).toEqual([true, null, false, null]);
    }
    for (const list of wide.lists) expect(list.align, `${list.name} @${width}`).not.toBe('center');
  }
  expect(page.__writes).toEqual([]);
});

test('Help lent by the OpenID Connect header sits in the identity group\'s top-right, on Provider Name\'s row, and goes back', async ({page}) => {
  await open(page);
  const card = page.locator('.dp-settings-oidc-card');
  const help = card.locator('.dp-settings-help-chip');
  await resize(page, 320);
  const g = await card.evaluate(node => {
    const r = n => n.getBoundingClientRect();
    const chip = node.querySelector('.dp-settings-help-chip');
    const group = node.querySelector('.dp-settings-oidc-group--identity');
    const title = group.querySelector('.dp-settings-oidc-provider-field .form-label');
    const input = group.querySelector('.dp-settings-oidc-provider-field .input');
    const c = r(chip), gr = r(group), t = r(title), i = r(input);
    return {inGroup: chip.parentElement === group, right: gr.right - c.right, top: c.top - gr.top,
      rowCentre: Math.abs((c.top + c.bottom) / 2 - (t.top + t.bottom) / 2),
      clearOfTitle: c.left >= t.right, clearOfInput: c.bottom <= i.top, rows: node.querySelectorAll(':scope > .card-body > .dp-settings-help-slot--row').length,
      firstChild: node.querySelector(':scope > .card-body').firstElementChild === group};
  });
  expect(g).toMatchObject({inGroup: true, clearOfTitle: true, clearOfInput: true, rows: 0, firstChild: true});
  expect(g.right).toBeLessThanOrEqual(14);
  expect(g.top).toBeLessThanOrEqual(12);
  expect(g.rowCentre).toBeLessThanOrEqual(4);
  await expect(help).toHaveCount(1);
  await expect(help).toHaveAccessibleName('Help: OpenID Connect');
  await help.click();
  const dialog = page.locator('.dp-modal-overlay .dp-modal-dialog').last();
  await expect(dialog).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await resize(page, 779);
  await expect(card.locator(':scope > .card-header .dp-settings-help-chip')).toHaveCount(1);
  await expect(help).toHaveCount(1);
  expect(page.__writes).toEqual([]);
});

// Access Control as drawn.
const access = page => page.locator('.dp-settings-oidc-access').evaluate(section => {
  const r = node => node.getBoundingClientRect();
  const s = r(section);
  const title = r(section.querySelector('.dp-settings-oidc-section-title'));
  const policy = section.querySelector('.dp-settings-oidc-allow-all');
  const toggle = r(policy.querySelector('.toggle'));
  const copy = r(section.querySelector('.dp-settings-oidc-section-copy'));
  const role = section.querySelector('.dp-settings-oidc-default-role');
  const select = r(role.querySelector('.dp-settings-inline-field-control > :is(.dp-dropdown-shell, select)'));
  const short = policy.querySelector('.dp-settings-oidc-allow-all-short');
  const words = policy.dataset.words === 'short' ? getComputedStyle(short, '::before').content.replace(/"/g, '') : policy.querySelector('.dp-settings-oidc-allow-all-full').textContent;
  const wordsBox = policy.dataset.words === 'short' ? r(short) : r(policy.querySelector('.dp-settings-oidc-allow-all-full'));
  return {
    rule: getComputedStyle(section, '::before').display !== 'none' && getComputedStyle(section, '::before').content !== 'none',
    titleCentre: (title.left + title.right) / 2 - (s.left + s.right) / 2,
    words, wordsInside: wordsBox.left >= r(policy).left - 0.5 && wordsBox.right <= toggle.left + 0.5,
    toggleRow: Math.abs((wordsBox.top + wordsBox.bottom) / 2 - (toggle.top + toggle.bottom) / 2) <= 3,
    order: [title.bottom <= toggle.top, toggle.bottom <= copy.top, copy.bottom <= r(role).top],
    selectCentre: (select.left + select.right) / 2 - (s.left + s.right) / 2,
  };
});

test('Access Control: no rule before it, its heading centred, the explanation under the toggle, the role centred', async ({page}) => {
  await open(page);
  const seen = {};
  for (const width of [779, 560, 390, 360, 320, 360, 390, 560]) {
    await resize(page, width);
    const a = await access(page);
    const at = `@${width}`;
    expect(a.rule, `${at}: rule before Access Control`).toBe(false);
    expect(Math.abs(a.titleCentre), `${at}: heading off centre`).toBeLessThanOrEqual(1);
    expect(a.order, `${at}: heading, toggle, explanation, role`).toEqual([true, true, true]);
    expect(Math.abs(a.selectCentre), `${at}: role selector off centre`).toBeLessThanOrEqual(1);
    expect(a.wordsInside, `${at}: toggle words overrun`).toBe(true);
    expect(a.toggleRow, `${at}: words and toggle not one row`).toBe(true);
    if (width in seen) expect(a.words, `${at}: narrowing and widening differ`).toBe(seen[width]);
    seen[width] = a.words;
  }
  // The full words wherever they fit; the short ones only where they do not.
  expect(seen[779]).toBe('Allow Any Authenticated OIDC Identity');
  expect(seen[390]).toBe('Allow Any Authenticated OIDC Identity');
  expect(seen[320]).toBe('Allow Any Identity');
  // The toggle keeps its full name, and toggling it is unchanged (refused here).
  await expect(page.locator('[data-setting="oidc_allow_all"]')).toHaveAccessibleName('Allow Any Authenticated OIDC Identity');
  // From 780px the section is as it was: its rule, its heading at the start.
  await resize(page, 1280);
  const wide = await access(page);
  expect(wide.rule).toBe(true);
  expect(wide.words).toBe('Allow Any Authenticated OIDC Identity');
  expect(page.__writes).toEqual([]);
});

test('a title says its hint on hover and by keyboard, and the controls keep their names and actions', async ({page}) => {
  await open(page);
  await resize(page, 320);
  const tooltip = page.locator('#dp-tooltip');
  const username = page.locator('.dp-settings-auth-username-field .form-label');
  const hint = await page.locator('.dp-settings-auth-username-field .form-hint').textContent();
  await username.hover();
  await expect(tooltip).toHaveText(hint.trim());
  await page.mouse.move(2, 2);
  await expect(tooltip).toBeHidden();
  // Keyboard: the title is the stop before its control.
  await page.locator('[data-setting="auth_username"]').focus();
  await page.keyboard.press('Shift+Tab');
  await expect(username).toBeFocused();
  await expect(tooltip).toHaveText(hint.trim());
  await page.keyboard.press('Escape');
  await expect(tooltip).toBeHidden();
  // An OpenID Connect title and the role's, the same way.
  for (const selector of ['.dp-settings-oidc-client-id-field', '.dp-settings-oidc-default-role']) {
    const title = page.locator(`${selector} .form-label`);
    const text = (await page.locator(`${selector} .form-hint`).textContent()).trim();
    await title.scrollIntoViewIfNeeded();
    await title.hover();
    await expect(tooltip, selector).toHaveText(text);
    await page.mouse.move(2, 2);
  }
  // The controls are what they were.
  await expect(page.locator('[data-setting="auth_username"]')).toHaveAccessibleName('Username');
  await expect(page.locator('[data-setting="auth_password"]')).toHaveAccessibleName('Password');
  await expect(page.locator('[data-action="clear-password"]')).toBeEnabled();
  await expect(page.locator('[data-action="apply-credentials"]')).toBeDisabled();
  await page.locator('[data-setting="auth_password"]').fill('synthetic-new-password');
  await expect(page.locator('[data-action="apply-credentials"]')).toBeEnabled();
  await expect(page.locator('[data-action="clear-oidc-secret"]')).toBeEnabled();
  await expect(page.locator('[data-action="copy-oidc-callback"]')).toBeEnabled();
  await expect(page.locator('[data-setting="oidc_client_id"]')).toHaveValue(AUTH.oidc_client_id);
  // Leaving the tab leaves no tooltip; from 780px the titles are plain again.
  await username.scrollIntoViewIfNeeded();
  await username.hover();
  await expect(tooltip).toBeVisible();
  await page.locator('#view-settings [data-tab="downloads"]').first().click();
  await expect(tooltip).toBeHidden();
  await page.locator('#view-settings [data-tab="authentication"]').first().click();
  await resize(page, 1280);
  await expect(username).not.toHaveAttribute('data-tooltip', /.*/);
  await expect(page.locator('.dp-settings-auth-username-field .form-hint')).toBeVisible();
  expect(page.__writes).toEqual([]);
});

test.describe('on a touch screen', () => {
  test.use({hasTouch: true});

  test('a tap on a title shows its hint and leaves its control alone; a tap elsewhere dismisses it', async ({page}) => {
    await open(page);
    await resize(page, 320);
    const tooltip = page.locator('#dp-tooltip');
    for (const [selector, control] of [['.dp-settings-auth-username-field', '[data-setting="auth_username"]'],
      ['.dp-settings-oidc-default-role', '[data-setting="auth_oidc_default_role"]']]) {
      const title = page.locator(`${selector} .form-label`);
      const text = (await page.locator(`${selector} .form-hint`).textContent()).trim();
      await title.scrollIntoViewIfNeeded();
      const before = await page.locator(control).inputValue();
      await title.tap();
      await expect(tooltip, selector).toHaveText(text);
      // The label activated nothing: its control neither took focus nor changed.
      await expect(page.locator(control)).not.toBeFocused();
      await expect(page.locator(control)).toHaveValue(before);
      await page.waitForTimeout(1000);
      await expect(tooltip, `${selector}: gone too soon`).toBeVisible();
      await page.locator('.dp-settings-oidc-section-title').tap();
      await expect(tooltip, `${selector}: not dismissed`).toBeHidden();
    }
    expect(page.__writes).toEqual([]);
  });
});
