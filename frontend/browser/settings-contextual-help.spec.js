const { test, expect } = require('@playwright/test');

/* 1.0.13: every Settings subsection card carries one icon-only help chip in
 * its header rail, naming that card; it opens that card's read-only help in
 * the canonical dialog shell (DPSettingsModal), and changes nothing.
 *
 * The full card inventory is proven statically
 * (backend/tests/test_settings_contextual_help_contract.py); this file proves
 * the shared mechanism on the live page and representative cards: one chip
 * per top-level card and none inside a group's members, the titled dialog,
 * keyboard activation, Escape and focus return, no write, the stacked role
 * help from a dialog, and the header geometry at a narrow width. */

const PANELS = ['sources', 'downloads', 'extraction', 'authentication', 'users', 'notifications', 'maintenance'];

async function openSettings(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  // The sidebar is off-canvas at a narrow width; navigation is its own owner.
  await page.locator('#sidebar .nav-item[data-view="settings"]').evaluate(element => element.click());
  await expect(page.locator('#view-settings .dp-settings-tabs')).toBeVisible();
  await expect(page.locator('[data-users-mount] .dp-settings-users-card').first()).toBeAttached();
}

async function chooseTab(page, name) {
  await page.locator(`#view-settings [data-tab="${name}"]`).click();
  await expect(page.locator(`.dp-settings-panel[data-panel="${name}"]`)).toBeVisible();
}

test('every top-level Settings card has exactly one help chip naming it, and of group members only Usenet has one', async ({page}) => {
  await openSettings(page);
  const census = await page.evaluate(() => Array.from(document.querySelectorAll('.dp-settings-panel')).map(panel => ({
    panel: panel.dataset.panel,
    cards: Array.from(panel.querySelectorAll('section.card'))
      .filter(card => !card.parentElement.closest('section.card.dp-settings-group-card'))
      .map(card => {
        const header = card.querySelector(':scope > .card-header');
        const title = (header.querySelector('.dp-settings-card-title-text') || header.querySelector('.card-title')).textContent.trim();
        const chips = Array.from(header.querySelectorAll('.dp-settings-help-chip'));
        return {title, labels: chips.map(chip => chip.getAttribute('aria-label')), text: chips.map(chip => chip.textContent.trim())};
      }),
    memberChips: Array.from(panel.querySelectorAll('section.card.dp-settings-group-card section.card .dp-settings-help-chip'),
      chip => chip.getAttribute('aria-label')),
  })));
  expect(census.map(entry => entry.panel).sort()).toEqual([...PANELS].sort());
  const cards = census.flatMap(entry => entry.cards);
  expect(cards).toHaveLength(18);
  for (const card of cards) {
    expect(card.labels, card.title).toEqual([`Help: ${card.title}`]);
    expect(card.text, card.title).toEqual(['']);
  }
  // Usenet is the one explicit provider exception; no other provider,
  // protocol tile or transfer-method card has a chip.
  for (const entry of census) {
    expect(entry.memberChips, entry.panel).toEqual(entry.panel === 'sources' ? ['Help: Usenet'] : []);
  }
});

test('every header rail reads status, then help, then the card’s own controls in their order', async ({page}) => {
  await openSettings(page);
  const rails = await page.evaluate(() => Array.from(document.querySelectorAll(
    '.dp-settings-panel .card-header .dp-settings-help-chip')).map(chip => {
    const rail = chip.parentElement;
    const header = rail.closest('.card-header');
    const kinds = [...rail.children].map(node => node.classList.contains('dp-settings-provider-config-status') ? 'status'
      : node.classList.contains('dp-settings-help-chip') ? 'help' : 'control');
    return {label: chip.getAttribute('aria-label'), kinds,
      rightEdge: kinds.length === 1 ? Math.round(header.getBoundingClientRect().right - chip.getBoundingClientRect().right) : null,
      paddingRight: Math.round(parseFloat(getComputedStyle(header).paddingRight))};
  }));
  expect(rails).toHaveLength(19);
  for (const rail of rails) {
    const expected = rail.kinds[0] === 'status' ? 1 : 0;
    expect(rail.kinds.indexOf('help'), rail.label).toBe(expected);
    expect(rail.kinds.filter(k => k === 'help'), rail.label).toHaveLength(1);
    if (rail.rightEdge !== null) expect(rail.rightEdge, rail.label).toBeLessThanOrEqual(rail.paddingRight + 1);
  }
  // Representative rails, as established, with help in its new place.
  const usenet = await page.locator('.dp-settings-provider-card--usenet > .card-header .dp-settings-card-header-controls')
    .evaluate(el => [...el.children].map(node => node.className.split(' ')[0]));
  expect(usenet).toEqual(['dp-settings-provider-config-status', 'dp-settings-help-chip', 'dp-settings-header-action',
    'toggle-row']);
});

test('a help chip opens its own card’s read-only help; Escape returns focus and nothing is written', async ({page}) => {
  await openSettings(page);
  const writes = [];
  page.on('request', request => { if (request.method() !== 'GET' && /\/api\/(settings|auth|accounts|integrations|transfer-policy)/.test(request.url()) && !/test-aria2/.test(request.url())) writes.push(request.url()); });

  await chooseTab(page, 'notifications');
  const chip = page.locator('.dp-settings-statistics-reporting-card [data-settings-help="statistics-reporting"]');
  const shape = await chip.evaluate(el => {
    const box = el.getBoundingClientRect();
    return {wide: box.width > box.height, radius: getComputedStyle(el).borderTopLeftRadius, glyph: !!el.querySelector('svg[data-dp-lucide="help"][aria-hidden="true"]')};
  });
  expect(shape).toEqual({wide: true, radius: '6px', glyph: true});
  // Keyboard activation.
  await chip.focus();
  await page.keyboard.press('Enter');
  const dialog = page.locator('.dp-modal-dialog.dp-settings-help-dialog');
  await expect(dialog).toHaveAttribute('role', 'dialog');
  await expect(dialog).toHaveAttribute('aria-modal', 'true');
  await expect(dialog.locator('.dp-modal-title')).toHaveText('Statistics Reporting');
  await expect(dialog).toContainText('Reporting Webhook, or the Discord Webhook');
  await expect(dialog).toContainText('It is not sent to DebridPulse users.');
  await expect(dialog.locator('input, select, textarea')).toHaveCount(0);
  await expect(dialog.locator('button')).toHaveCount(2);
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(chip).toBeFocused();

  // A card whose rail also holds Test and Enable: the chip operates neither.
  await chooseTab(page, 'authentication');
  const enable = page.locator('#view-settings [data-setting="auth_oidc_enabled"]');
  const before = await enable.isChecked();
  await page.locator('[data-settings-help="oidc"]').click();
  await expect(dialog.locator('.dp-modal-title')).toHaveText('OpenID Connect');
  await expect(dialog).toContainText('a user must match every list that is filled in');
  await dialog.locator('[data-modal-close]').click();
  await expect(dialog).toHaveCount(0);
  await expect(enable).toBeChecked({checked: before});
  expect(writes).toEqual([]);
});

test('the role help opens above Add Local Account and closes back into it', async ({page}) => {
  await openSettings(page);
  await chooseTab(page, 'users');
  await page.locator('[data-user-action="create-local"]').click();
  const account = page.locator('.dp-modal-dialog.dp-users-dialog');
  const role = account.locator('select[data-field="role"]');
  const description = account.locator('[data-field-description="role"]');
  await expect(role).toHaveAttribute('aria-describedby', 'dp-users-role-description');
  await expect(description).toContainText('only when no other account still needs it');
  await role.selectOption('readonly');
  await expect(description).toHaveText('Views downloads and Statistics, but cannot add, change, or remove anything.');

  const roleHelp = account.locator('[data-settings-help="user-accounts"]');
  await expect(roleHelp).toHaveAttribute('aria-label', 'Help: User Account Roles');
  await roleHelp.click();
  const help = page.locator('.dp-modal-dialog.dp-settings-help-dialog');
  await expect(help.locator('.dp-modal-title')).toHaveText('User Accounts');
  await expect(help.locator('table.dp-settings-help-table th[scope="col"]'))
    .toHaveText(['Capability', 'Admin', 'User', 'ReadOnly User', 'Integration']);
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(2);
  await page.keyboard.press('Escape');
  await expect(help).toHaveCount(0);
  await expect(account).toBeVisible();
  await expect(roleHelp).toBeFocused();
  await expect(role).toHaveValue('readonly');
  await page.keyboard.press('Escape');
  await expect(account).toHaveCount(0);
});

test('at a narrow width the chip stays inside its header and the role table scrolls in its own region', async ({page}) => {
  await page.setViewportSize({width: 390, height: 800});
  await openSettings(page);
  for (const name of ['sources', 'maintenance', 'users']) {
    await chooseTab(page, name);
    const issues = await page.evaluate(panelName => {
      const found = [];
      const panel = document.querySelector(`.dp-settings-panel[data-panel="${panelName}"]`);
      panel.querySelectorAll('.dp-settings-help-chip').forEach(chip => {
        const c = chip.getBoundingClientRect();
        const header = chip.closest('.card-header').getBoundingClientRect();
        if (c.left < header.left - 0.5 || c.right > header.right + 0.5) found.push(`${chip.getAttribute('aria-label')} outside`);
        chip.closest('.card-header').querySelectorAll('button, label.toggle-row').forEach(other => {
          if (other === chip) return;
          const o = other.getBoundingClientRect();
          if (Math.min(c.right, o.right) - Math.max(c.left, o.left) > 0.5
            && Math.min(c.bottom, o.bottom) - Math.max(c.top, o.top) > 0.5) found.push(`${chip.getAttribute('aria-label')} overlaps`);
        });
      });
      if (document.documentElement.scrollWidth > innerWidth + 1) found.push('page overflow');
      return found;
    }, name);
    expect(issues, name).toEqual([]);
  }
  await page.locator('[data-settings-help="user-accounts"]').click();
  const help = page.locator('.dp-modal-dialog.dp-settings-help-dialog');
  await expect(help.locator('.dp-modal-title')).toHaveText('User Accounts');
  const fit = await help.evaluate(el => {
    const box = el.getBoundingClientRect();
    const region = el.querySelector('.dp-settings-help-table-scroll');
    return {fits: box.right <= innerWidth && box.bottom <= innerHeight, scrolls: region.scrollWidth > region.clientWidth};
  });
  expect(fit).toEqual({fits: true, scrolls: true});
  await page.keyboard.press('Escape');
});

test('Usenet has its own help, between its status and Test; Premium Services points to it', async ({page}) => {
  await openSettings(page);
  await chooseTab(page, 'sources');
  const card = page.locator('.dp-settings-provider-card--usenet');
  const chip = card.locator(':scope > .card-header [data-settings-help="usenet"]');
  await expect(chip).toHaveAttribute('aria-label', 'Help: Usenet');
  const enable = card.locator('[data-integration-enabled="usenet"]');
  const enabled = await enable.isChecked();
  await chip.focus();
  await page.keyboard.press('Enter');
  const dialog = page.locator('.dp-modal-dialog.dp-settings-help-dialog');
  await expect(dialog.locator('.dp-modal-title')).toHaveText('Usenet');
  await expect(dialog).toContainText('a lower value is preferred, and 0 is the highest');
  await expect(dialog).toContainText('By default an NZB goes to Usenet first');
  await expect(dialog.locator('input, select, textarea')).toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(chip).toBeFocused();
  // The chip neither toggled the provider nor opened its card.
  await expect(enable).toBeChecked({checked: enabled});
  await expect(card.locator(':scope > .card-body')).toBeHidden();

  await page.locator('[data-settings-help="premium-services"]').click();
  await expect(dialog.locator('.dp-modal-title')).toHaveText('Premium Services');
  await expect(dialog).toContainText('Usenet uses your configured news servers rather than a debrid account. '
    + 'Open the Usenet card’s help for server configuration and download behavior.');
  await expect(dialog).not.toContainText('connection count, and priority');
  await dialog.locator('[data-modal-close]').click();
  await expect(dialog).toHaveCount(0);
});
