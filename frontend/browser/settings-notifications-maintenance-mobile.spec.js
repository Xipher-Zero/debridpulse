const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Notifications and Data & Maintenance below 780px.
 *
 * Webhook Notifications, Statistics Reporting, Backups & Retention and
 * Database Reset Controls take the wrapping one-row header (fitHeaderSteps):
 * the title on at most two lines, the rail whole on one line, Help lent to the
 * first region's top-right -- on its first title's row, 8px above its control
 * -- only while the header cannot hold it. Backups & Retention's two actions
 * are one Backups menu there (headerActionsMenu) whose items are the card's
 * own actions, and its title is "Backup Control" at the last step. Each
 * designated setting's hint leaves the layout and its title says it as a
 * tooltip (promoteHints). Two subsections draw short titles. Statistics
 * Reporting's cadence and window take their titles above their controls
 * together at the island's hint-suppressed step (data-island-columns), and
 * Database Reset is a content-bounded island whose toggle and action stack,
 * centred, where one row cannot hold them. From 780px only the approved
 * notification wording differs.
 *
 * Canonical state is the server's own; every write is refused, and every
 * action endpoint is answered here, so nothing is backed up, reset, or sent. */

const settle = page => page.evaluate(() => Promise.all(document.getAnimations()
  .filter(animation => Number.isFinite(animation.effect?.getComputedTiming().endTime))
  .map(animation => animation.finished.catch(() => null)))
  .then(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))));

async function open(page, tab) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  page.__writes = [];
  page.__actions = [];
  await page.route(url => url.pathname.startsWith('/api/'), route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'GET' && path === '/api/admin/backups') {
      page.__actions.push(`GET ${path}`);
      return route.fulfill({json: {backups: []}});
    }
    // GETs, and the aria2 probe the page runs for itself, are reads.
    if (request.method() === 'GET' || path === '/api/settings/test-aria2') return route.continue();
    if (['/api/admin/backup', '/api/admin/database/wipe'].includes(path)) {
      page.__actions.push(`${request.method()} ${path}`);
      return route.fulfill({json: {ok: true, skipped: false}});
    }
    page.__writes.push(`${request.method()} ${path}`);
    return route.abort();
  });
  await page.setViewportSize({width: 1280, height: 900});
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await page.locator(`#view-settings [data-tab="${tab}"]:not([inert])`).click();
  await settle(page);
}

async function resize(page, width) {
  await page.setViewportSize({width, height: 900});
  await settle(page);
}

const CARDS = {
  notifications: ['.dp-settings-discord-card', '.dp-settings-statistics-reporting-card'],
  maintenance: ['.dp-settings-backups-retention-card', '.dp-settings-database-wipe-card'],
};

// A header as drawn: its title's lines, its rail on one line beside it, and
// where its Help is, with that chip's place against the setting beneath it.
const header = (page, selector) => page.evaluate(selector => {
  const card = document.querySelector(selector);
  const R = node => node.getBoundingClientRect();
  const head = card.querySelector(':scope > .card-header');
  const text = head.querySelector('.dp-settings-card-title-text');
  const range = document.createRange();
  range.selectNodeContents(text);
  const lines = new Set([...range.getClientRects()].filter(box => box.width > 1 && box.height > 8)
    .map(box => Math.round(box.bottom))).size;
  const group = R(head.querySelector('.dp-settings-card-title-group'));
  const rail = head.querySelector('.dp-settings-card-header-controls');
  const drawn = [...rail.querySelectorAll('button, .toggle')].filter(node => node.getClientRects().length).map(R);
  const mid = box => (box.top + box.bottom) / 2;
  const chip = card.querySelector('.dp-settings-help-chip');
  const lent = !head.contains(chip);
  const out = {lines, beside: group.right <= R(rail).left + 0.5,
    oneLine: drawn.every(box => Math.abs(mid(box) - mid(drawn[0])) <= 2), inHeader: R(rail).bottom <= R(head).bottom + 0.5,
    lent, overflow: document.documentElement.scrollWidth > window.innerWidth};
  if (lent) {
    const slot = chip.parentElement;
    const field = slot.querySelector('.dp-settings-inline-field');
    const label = R(field.querySelector('.form-label'));
    const control = R(field.querySelector('.dp-settings-inline-field-control'));
    out.clearance = +(control.top - R(chip).bottom).toFixed(1);
    out.titleRow = Math.abs(mid(R(chip)) - mid(label)) <= 2;
    out.rightOnControl = Math.abs(R(chip).right - control.right) <= 1.5;
  }
  return out;
}, selector);

test('each header is one row with its title on at most two lines, and lends Help clear of the first control', async ({page}) => {
  for (const [tab, cards] of Object.entries(CARDS)) {
    await open(page, tab);
    for (const width of [320, 340, 360, 390, 560, 779]) {
      await resize(page, width);
      for (const selector of cards) {
        const at = `${selector} @${width}`;
        const fit = await header(page, selector);
        expect([fit.lines <= 2, fit.beside, fit.oneLine, fit.inHeader, fit.overflow], at).toEqual([true, true, true, true, false]);
        if (fit.lent) {
          expect([fit.titleRow, fit.rightOnControl], at).toEqual([true, true]);
          expect(fit.clearance, at).toBeGreaterThanOrEqual(8);
          expect(fit.clearance, at).toBeLessThanOrEqual(10);
        }
      }
    }
    // Help is lent at the narrow widths and back in the header where it fits.
    await resize(page, 320);
    for (const selector of cards.filter(card => !card.includes('database-wipe'))) {
      expect((await header(page, selector)).lent, `${selector} @320`).toBe(true);
    }
    await resize(page, 779);
    for (const selector of cards) expect((await header(page, selector)).lent, `${selector} @779`).toBe(false);
  }
  expect(page.__writes).toEqual([]);
});

test('the notification wording is the webhook wording at every width', async ({page}) => {
  await open(page, 'notifications');
  for (const width of [1280, 320]) {
    await resize(page, width);
    await expect(page.locator('.dp-settings-discord-card .card-title')).toHaveText('Webhook Notifications');
    await expect(page.locator('.dp-settings-discord-card [data-setting="discord_webhook_url"]')).toHaveAccessibleName('Primary Webhook');
    await expect(page.locator('.dp-settings-discord-card [data-setting="discord_notifications_enabled"]')).toHaveAccessibleName('Enable Webhook Notifications');
    await expect(page.locator('[data-panel="notifications"]')).not.toContainText(/Discord Webhook|Discord Notifications/);
  }
  await page.locator('.dp-settings-discord-card [data-settings-help="discord"]').click();
  await expect(page.getByRole('dialog')).toContainText('Webhook Notifications');
  await expect(page.getByRole('dialog')).toContainText('The Primary Webhook receives every alert.');
});

test('the designated hints are their titles\' tooltips below 780px and drawn hints from 780px', async ({page}) => {
  const fields = {
    notifications: ['discord_username', 'discord_avatar_url', 'discord_webhook_url', 'discord_webhook_added', 'stats_report_webhook_url'],
    maintenance: ['backup_folder'],
  };
  const read = (page, keys) => page.evaluate(keys => keys.map(key => {
    const field = document.querySelector(`[data-setting="${key}"]`).closest('.dp-settings-inline-field');
    const title = field.querySelector('.form-label');
    const hint = field.querySelector('.form-hint');
    return {key, hintDrawn: hint.getBoundingClientRect().width > 1, tooltip: title.getAttribute('data-tooltip'),
      touch: title.hasAttribute('data-tooltip-touch'), tabindex: title.getAttribute('tabindex'), hint: hint.textContent.trim()};
  }), keys);
  for (const [tab, keys] of Object.entries(fields)) {
    await open(page, tab);
    for (const width of [320, 390, 779]) {
      await resize(page, width);
      for (const field of await read(page, keys)) {
        expect([field.hintDrawn, field.tooltip, field.touch, field.tabindex], `${field.key} @${width}`)
          .toEqual([false, field.hint, true, '0']);
      }
    }
    await resize(page, 1280);
    for (const field of await read(page, keys)) {
      expect([field.hintDrawn, field.tooltip, field.touch, field.tabindex], `${field.key} @1280`).toEqual([true, null, false, null]);
    }
  }
  // Keyboard: the title is the stop before its control, and says its hint.
  await open(page, 'notifications');
  await resize(page, 390);
  const tooltip = page.locator('#dp-tooltip');
  await page.locator('[data-setting="discord_webhook_url"]').focus();
  await page.keyboard.press('Shift+Tab');
  await expect(page.locator('.dp-settings-inline-field').filter({has: page.locator('[data-setting="discord_webhook_url"]')})
    .locator('.form-label')).toBeFocused();
  await expect(tooltip).toHaveText('Primary destination for enabled notifications.');
  await page.keyboard.press('Escape');
  await expect(tooltip).toBeHidden();
  expect(page.__writes).toEqual([]);
});

test('two subsections draw their short titles below 780px, their full titles staying their names', async ({page}) => {
  for (const [tab, card, full, short] of [
    ['notifications', '.dp-settings-discord-card', 'Notification Events & Delivery Options', 'Delivery Options'],
    ['maintenance', '.dp-settings-backups-retention-card', 'Additional Backup & Retention Settings', 'Backup Settings']]) {
    await open(page, tab);
    const title = page.locator(`${card} .dp-settings-subsection-title`);
    const drawn = () => title.evaluate(node => node.querySelector('.dp-settings-subsection-title-short').getClientRects().length
      ? getComputedStyle(node.querySelector('.dp-settings-subsection-title-short'), '::before').content.replace(/"/g, '')
      : node.querySelector('.dp-settings-subsection-title-full').innerText);
    for (const [width, words] of [[390, short], [779, short], [780, full], [1280, full]]) {
      await resize(page, width);
      expect(await drawn(), `${card} @${width}`).toBe(words);
      await expect(title).toHaveText(full);
      await expect(page.locator(`${card} .dp-settings-subsection .dp-settings-disclosure`)).toHaveAccessibleName(new RegExp(full.replace(/[&]/g, '\\&')));
    }
    // The disclosure still opens and closes.
    await resize(page, 390);
    const disclosure = page.locator(`${card} .dp-settings-subsection .dp-settings-disclosure`);
    const body = page.locator(`${card} .dp-settings-subsection-body`);
    const was = await disclosure.getAttribute('aria-expanded');
    await disclosure.click();
    await expect(disclosure).toHaveAttribute('aria-expanded', was === 'true' ? 'false' : 'true');
    await expect(body).toBeVisible({visible: was !== 'true'});
  }
});

test('Report Window and the interval take their titles above their controls together at the hint-suppressed step', async ({page}) => {
  await open(page, 'notifications');
  const read = () => page.evaluate(() => {
    const island = document.querySelector('.dp-settings-statistics-cadence-group');
    return {fit: island.dataset.islandFit, fields: [...island.querySelectorAll('.dp-settings-inline-field')].map(field => {
      const box = node => field.querySelector(node).getBoundingClientRect();
      const info = box('.dp-settings-inline-field-info');
      const control = box('.dp-settings-inline-field-control .input, .dp-settings-inline-field-control .dp-action-field');
      return {above: info.bottom <= control.top + 0.5,
        centred: Math.abs((control.left + control.right) / 2 - (field.getBoundingClientRect().left + field.getBoundingClientRect().right) / 2) <= 1};
    })};
  });
  for (const width of [320, 340, 360]) {
    await resize(page, width);
    const {fit, fields} = await read();
    expect(fit, `@${width}`).toBe('4');
    expect(fields, `@${width}`).toEqual([{above: true, centred: true}, {above: true, centred: true}]);
  }
  for (const width of [390, 560, 779]) {
    await resize(page, width);
    const {fields} = await read();
    expect(fields.map(field => field.above), `@${width}`).toEqual([false, false]);
  }
});

test('Backups & Retention: one Backups menu below 780px holding the card\'s own two actions; both actions from 780px', async ({page}) => {
  await open(page, 'maintenance');
  const card = page.locator('.dp-settings-backups-retention-card');
  const trigger = card.locator('.dp-settings-header-test-trigger');
  const menu = card.locator('.dp-settings-header-test-menu');
  const direct = card.locator('.dp-settings-header-action > button');
  await expect(trigger).toBeHidden();
  await expect(direct).toHaveText(['Backups', 'Run Backup']);
  await resize(page, 390);
  await expect(direct.first()).toBeHidden();
  await expect(trigger).toHaveText('Backups');
  await expect(card.locator('.dp-settings-header-test-group .toggle')).toBeVisible();
  // Keyboard: Enter opens it on its first action; Escape closes it to the trigger.
  await trigger.focus();
  await page.keyboard.press('Enter');
  await expect(menu).toBeVisible();
  await expect(menu.locator('button')).toHaveText(['Backup Management', 'Run Backup']);
  await expect(menu.locator('button').first()).toBeFocused();
  const placed = await menu.evaluate(node => { const box = node.getBoundingClientRect(); return box.left >= 0 && box.right <= innerWidth && box.top >= 0 && box.bottom <= innerHeight; });
  expect(placed).toBe(true);
  await page.keyboard.press('Escape');
  await expect(menu).toBeHidden();
  await expect(trigger).toBeFocused();
  await expect(menu.locator('button')).toHaveCount(0);
  // Run Backup runs the card's own action, once.
  await trigger.click();
  await menu.locator('button', {hasText: 'Run Backup'}).click();
  await expect.poll(() => page.__actions).toEqual(['POST /api/admin/backup']);
  await expect(page.locator('.toast, .dp-toast, #toast-container').filter({hasText: 'Backup completed'}).first()).toBeVisible();
  // Backup Management closes the menu and opens the one Backups manager.
  await page.mouse.click(5, 5);
  await expect(menu).toBeHidden();
  await trigger.click();
  await menu.locator('button', {hasText: 'Backup Management'}).click();
  await expect(menu).toBeHidden();
  await expect(page.locator('.dp-backup-manager-dialog')).toBeVisible();
  expect(page.__actions).toEqual(['POST /api/admin/backup', 'GET /api/admin/backups']);
  await page.keyboard.press('Escape');
  await expect(page.locator('.dp-backup-manager-dialog')).toBeHidden();
  // The toggle is its own control; the menu never holds it.
  await expect(card.locator('[data-setting="backup_enabled"]')).toHaveAccessibleName('Enable Backups & Retention');
  // Back at 780px: the two actions, no trigger.
  await resize(page, 780);
  await expect(trigger).toBeHidden();
  await expect(direct).toHaveText(['Backups', 'Run Backup']);
  await expect(direct.first()).toBeVisible();
  expect(page.__writes).toEqual([]);
});

test('Backups & Retention draws "Backup Control" only at its last step, its full title its tooltip and name', async ({page}) => {
  await open(page, 'maintenance');
  const card = page.locator('.dp-settings-backups-retention-card');
  const text = card.locator('.dp-settings-card-title-text');
  await resize(page, 320);
  await expect(card).toHaveAttribute('data-header-fit', '5');
  expect(await text.evaluate(node => getComputedStyle(node.querySelector('.dp-settings-card-title-short'), '::before').content)).toBe('"Backup Control"');
  await expect(text).toHaveAttribute('data-tooltip', 'Backups & Retention');
  await expect(text).toHaveAttribute('tabindex', '0');
  await expect(card.locator('.card-title')).toHaveText('Backups & Retention');
  await resize(page, 390);
  await expect(text).not.toHaveAttribute('data-tooltip', /.*/);
  await expect(card.locator('.dp-settings-card-title-short')).toBeHidden();
});

test('Database Reset: a content-bounded, centred island that stacks its toggle and action only where one row cannot hold them', async ({page}) => {
  await open(page, 'maintenance');
  const read = () => page.evaluate(() => {
    const R = node => node.getBoundingClientRect();
    const island = document.querySelector('.dp-settings-database-wipe-row');
    const body = island.parentElement;
    const title = island.querySelector('.tl');
    const toggle = island.querySelector('.toggle');
    const button = island.querySelector('[data-action="wipe-database"]');
    const centre = box => (box.left + box.right) / 2;
    const bodyCentre = R(body).left + body.clientLeft + body.clientWidth / 2;
    const members = [...island.querySelectorAll('.dp-settings-island-member')].map(R);
    return {stacked: island.dataset.island === 'stacked', centred: Math.abs(centre(R(island)) - bodyCentre) <= 1,
      bounded: R(island).width < body.clientWidth - 1 || island.dataset.island !== 'stacked',
      titleOver: R(title).bottom <= R(toggle).top + 0.5, titleCentred: Math.abs(centre(R(title)) - centre(R(toggle))) <= 1,
      buttonBelow: R(button).top >= R(toggle).bottom, buttonCentred: Math.abs(centre(R(button)) - centre(R(island))) <= 1,
      inside: members.every(box => box.left >= R(island).left && box.right <= R(island).right),
      hint: island.querySelector('.td').getBoundingClientRect().width > 1, tooltip: title.getAttribute('data-tooltip'),
      overflow: document.documentElement.scrollWidth > innerWidth};
  });
  for (const width of [320, 360]) {
    await resize(page, width);
    expect(await read(), `@${width}`).toEqual({stacked: true, centred: true, bounded: true, titleOver: true, titleCentred: true,
      buttonBelow: true, buttonCentred: true, inside: true, hint: false, tooltip: 'Unlock the database reset action.', overflow: false});
  }
  for (const width of [560, 779]) {
    await resize(page, width);
    const fit = await read();
    expect([fit.stacked, fit.centred, fit.inside, fit.hint, fit.tooltip, fit.overflow], `@${width}`)
      .toEqual([false, true, true, false, 'Unlock the database reset action.', false]);
  }
  await resize(page, 1280);
  const desktop = await read();
  expect([desktop.stacked, desktop.hint, desktop.tooltip], '@1280').toEqual([false, true, null]);
  // The destructive action keeps its safeguard: with reset not allowed, it
  // only says so and sends nothing.
  await resize(page, 320);
  await expect(page.locator('[data-setting="db_wipe_enabled"]')).not.toBeChecked();
  await page.locator('[data-action="wipe-database"]').click();
  await page.waitForTimeout(300);
  expect(page.__actions).toEqual([]);
  expect(page.__writes).toEqual([]);
});

test.describe('on a touch screen', () => {
  test.use({hasTouch: true});

  test('a first tap on a promoted title shows its hint and activates nothing', async ({page}) => {
    await open(page, 'maintenance');
    await resize(page, 320);
    const tooltip = page.locator('#dp-tooltip');
    const toggle = page.locator('[data-setting="db_wipe_enabled"]');
    const title = page.locator('.dp-settings-database-wipe-toggle .tl');
    await title.scrollIntoViewIfNeeded();
    await title.tap();
    await expect(tooltip).toHaveText('Unlock the database reset action.');
    await expect(toggle).not.toBeChecked();
    await page.locator('.dp-settings-database-wipe-card .dp-settings-caution').tap();
    await expect(tooltip).toBeHidden();
    const folder = page.locator('.dp-settings-backup-folder-field .form-label');
    await folder.scrollIntoViewIfNeeded();
    await folder.tap();
    await expect(tooltip).toHaveText('Choose where DebridPulse stores database and configuration backups.');
    await expect(page.locator('[data-setting="backup_folder"]')).not.toBeFocused();
    // The Backups menu answers a tap.
    const trigger = page.locator('.dp-settings-backups-retention-card .dp-settings-header-test-trigger');
    await trigger.scrollIntoViewIfNeeded();
    await trigger.tap();
    await expect(page.locator('.dp-settings-backups-retention-card .dp-settings-header-test-menu button')).toHaveText(['Backup Management', 'Run Backup']);
    expect(page.__writes).toEqual([]);
    expect(page.__actions).toEqual([]);
  });
});
