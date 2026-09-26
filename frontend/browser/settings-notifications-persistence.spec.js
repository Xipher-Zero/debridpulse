const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Notifications normalization -- proven against the RENDERED page and
 * the REAL backend rather than against selectors.
 *
 * Notifications joins Services, Downloads, Extraction and Authentication: every
 * control commits at its own boundary through the canonical persistence owner,
 * so the tab has no Apply Settings responsibility of any kind and no generic
 * Apply can replay a stale Notifications value from the page.
 *
 * The surfaces with real risk get real proof:
 *   - a stored webhook is a SECRET, so its field is blank and blanking it must
 *     write nothing: erasing one is a separate explicit confirmed act;
 *   - clearing one destination must not touch another, and must never move
 *     either section's Enable participation;
 *   - Statistics Reporting's status follows the EFFECTIVE destination, so
 *     clearing its dedicated webhook can leave it configured by fallback;
 *   - an action settles pending field writes first, so Test exercises the value
 *     the operator just entered rather than stale stored configuration;
 *   - a rejected write converges the control back onto canonical truth.
 *
 * Every case restores what it changed, so the shared backend is left as found.
 */

const PRIMARY = 'https://discord.com/api/webhooks/1000/browser-primary';
const REPORT = 'https://discord.com/api/webhooks/2000/browser-report';
const ADDED = 'https://discord.com/api/webhooks/3000/browser-added';

async function openSettings(page, tab) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator(`#view-settings [data-tab="${tab}"]`).click();
  await expect(page.locator(`.dp-settings-panel[data-panel="${tab}"]`)).toBeVisible();
}

const field = (page, key) => page.locator(`#dp-settings-field-${key.replaceAll('_', '-')}`);
const clearFor = (page, key) =>
  page.locator(`#view-settings [data-action="clear-webhook"][data-webhook="${key}"]`);
const status = (page, card) =>
  page.locator(`#view-settings .${card} .dp-settings-provider-config-status`);

/** Canonical settings, straight from the configuration boundary. */
async function canonical(page) {
  const response = await page.request.get('/api/settings');
  expect(response.ok()).toBeTruthy();
  return response.json();
}

/** Commit a text/number field the way an operator does: type, then leave it.
 *  The value must differ from what is stored -- an unchanged field crosses no
 *  boundary and writes nothing, which is itself part of the contract. */
async function commit(page, key, value) {
  const written = page.waitForResponse(
    r => r.url().includes('/api/settings') && r.request().method() === 'PUT', {timeout: 15000});
  await field(page, key).fill(String(value));
  await field(page, key).blur();
  await written;
}

/** Accept a destructive confirmation and wait for the mutation it authorised. */
async function confirmDialog(page, label) {
  await expect(page.locator('.dp-modal-overlay')).toBeVisible();
  const written = page.waitForResponse(
    r => r.url().includes('/api/settings') && r.request().method() === 'PUT');
  await page.locator('.dp-modal-overlay').getByRole('button', {name: label}).click();
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
  await written;
}

/** Choose a select option the way an operator does: through the projected
 *  listbox the application renders over the native control. */
async function choose(page, key, label) {
  const shell = field(page, key).locator('xpath=following-sibling::span[1]');
  const written = page.waitForResponse(
    r => r.url().includes('/api/settings') && r.request().method() === 'PUT', {timeout: 15000});
  await shell.locator('.dp-dropdown__trigger').click();
  await page.locator('#dp-dropdown-layer .dp-dropdown__option', {hasText: label}).first().click();
  await written;
}

/** Put every Notifications value this suite touches back as it was found. */
async function restore(page, before) {
  await page.request.put('/api/settings', {data: {
    ...before,
    clear_secrets: ['discord_webhook_url', 'discord_webhook_added', 'stats_report_webhook_url'],
    discord_webhook_url: '', discord_webhook_added: '', stats_report_webhook_url: '',
  }});
}

test.beforeEach(async ({page}) => {
  await page.goto('/');
});

test('Notifications carries no Apply contract at all', async ({page}) => {
  await openSettings(page, 'notifications');

  await expect(page.locator('#view-settings [data-action="save"]')).toBeHidden();
  await expect(page.locator('#view-settings .dp-settings-save-hint')).toBeHidden();

  // The deferred clear-on-save checkboxes are gone entirely, not hidden.
  await expect(page.locator('#view-settings [data-clear-secret]')).toHaveCount(0);
  // ... and so is the footer's page-specific action region.
  await expect(page.locator('#view-settings [data-context-action]')).toHaveCount(0);

  // Every control on the tab is enrolled with the canonical persistence owner.
  const unmanaged = await page.locator('.dp-settings-panel[data-panel="notifications"]')
    .evaluate(panel => [...panel.querySelectorAll('[data-setting]')]
      .filter(node => !node.dataset.commitKey).map(node => node.dataset.setting));
  expect(unmanaged).toEqual([]);
});

test('both cards render the shared rail: status, then Test, then Enable', async ({page}) => {
  await openSettings(page, 'notifications');

  for (const card of ['dp-settings-discord-card', 'dp-settings-statistics-reporting-card']) {
    const rail = page.locator(`#view-settings .${card} .dp-settings-card-header-controls`);
    await expect(rail).toHaveCount(1);
    const order = await rail.evaluate(el => [...el.children].map(node => {
      if (node.classList.contains('dp-settings-provider-config-status')) return 'status';
      if (node.classList.contains('dp-settings-header-action')) return 'test';
      return node.querySelector('input[type=checkbox]') ? 'enable' : node.className;
    }));
    expect(order).toEqual(['status', 'test', 'enable']);
    await expect(rail.locator('.dp-settings-provider-test')).toHaveText('Test');
  }

  // The relocated footer actions have no surviving duplicate anywhere.
  await expect(page.getByRole('button', {name: 'Test Discord'})).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'Send Report Now'})).toHaveCount(0);
});

test('text, number, select and toggle all persist at their own boundary', async ({page}) => {
  const before = await canonical(page);
  await openSettings(page, 'notifications');

  // Each value is derived from what is stored, so every one of them is a real
  // change however the shared backend was left by an earlier case.
  const name = `${before.discord_username} probe`;
  const interval = (Number(before.stats_report_interval_hours) || 0) + 7;
  const [window, windowLabel] = before.stats_report_window_hours === 168
    ? [720, '30 days'] : [168, '7 days'];

  try {
    await commit(page, 'discord_username', name);
    await commit(page, 'stats_report_interval_hours', interval);

    // A select commits on choosing, with no blur an operator could perform.
    await choose(page, 'stats_report_window_hours', windowLabel);

    // A boolean's change IS its boundary. The event toggles live inside the
    // disclosure, so the operator opens it first.
    await page.locator('#view-settings .dp-settings-subsection .dp-settings-disclosure').click();
    await expect(page.locator('#view-settings .dp-settings-subsection-body')).toBeVisible();
    const toggled = page.waitForResponse(
      r => r.url().includes('/api/settings') && r.request().method() === 'PUT', {timeout: 15000});
    await page.locator('label[for="dp-settings-field-discord-notify-error"] .ttrack').click();
    await toggled;

    // Nothing was applied, and everything is durable: read canonical truth
    // rather than the page that wrote it, then prove a reload agrees.
    const saved = await canonical(page);
    expect(saved.discord_username).toBe(name);
    expect(saved.stats_report_interval_hours).toBe(interval);
    expect(saved.stats_report_window_hours).toBe(window);
    expect(saved.discord_notify_error).toBe(!before.discord_notify_error);

    await page.reload();
    await openSettings(page, 'notifications');
    await expect(field(page, 'discord_username')).toHaveValue(name);
    await expect(field(page, 'stats_report_interval_hours')).toHaveValue(String(interval));
    await expect(field(page, 'stats_report_window_hours')).toHaveValue(String(window));
  } finally {
    await restore(page, before);
  }
});

test('the update-check interval moved into the disclosure and persists there', async ({page}) => {
  const before = await canonical(page);
  await openSettings(page, 'notifications');

  try {
    const disclosure = page.locator('#view-settings .dp-settings-subsection .dp-settings-disclosure');
    await expect(page.locator('#view-settings .dp-settings-subsection-title'))
      .toHaveText('Notification Events & Delivery Options');
    await expect(disclosure).toHaveAttribute('aria-expanded', 'false');

    // Opening it must not disturb the destination row above it.
    const geometry = () => page.locator('#view-settings .dp-settings-notifications-delivery-row')
      .evaluate(row => {
        const base = row.getBoundingClientRect();
        return [...row.querySelectorAll('.input, button')].map(node => {
          const box = node.getBoundingClientRect();
          return `${Math.round(box.x - base.x)}:${Math.round(box.width)}`;
        }).join('|') + `|row:${Math.round(base.width)}`;
      });
    const collapsed = await geometry();
    await disclosure.click();
    await expect(disclosure).toHaveAttribute('aria-expanded', 'true');
    expect(await geometry()).toBe(collapsed);

    // The interval is inside it, labelled exactly, with its unit in the field.
    const cell = page.locator('#view-settings .dp-settings-subsection .dp-settings-field')
      .filter({has: field(page, 'update_check_interval_hours')});
    await expect(cell.locator('.form-label')).toHaveText('Update Check Interval');
    await expect(cell.locator('.dp-settings-field-unit')).toHaveText('hours');

    await commit(page, 'update_check_interval_hours', 6);
    expect((await canonical(page)).update_check_interval_hours).toBe(6);
  } finally {
    await restore(page, before);
  }
});

test('a blank webhook field writes nothing; erasing one is its own act', async ({page}) => {
  const before = await canonical(page);
  await openSettings(page, 'notifications');

  try {
    await commit(page, 'discord_webhook_url', PRIMARY);

    // A stored secret's accepted presentation is blank, and the field says so.
    await expect(field(page, 'discord_webhook_url')).toHaveValue('');
    await expect(field(page, 'discord_webhook_url'))
      .toHaveAttribute('placeholder', /configured/);
    await expect(clearFor(page, 'discord_webhook_url')).toBeEnabled();
    await expect(status(page, 'dp-settings-discord-card')).toHaveText('Configured');

    // Leaving that blank field is not a clear: it writes nothing at all.
    const writes = [];
    page.on('request', request => {
      if (request.url().includes('/api/settings') && request.method() === 'PUT') {
        writes.push(request.postDataJSON());
      }
    });
    await field(page, 'discord_webhook_url').click();
    await field(page, 'discord_webhook_url').blur();
    await page.waitForTimeout(600);
    expect(writes).toEqual([]);
    expect((await canonical(page)).discord_webhook_url_configured).toBe(true);

    // Declining the explicit act mutates nothing either.
    await clearFor(page, 'discord_webhook_url').click();
    await expect(page.locator('.dp-modal-overlay')).toBeVisible();
    await page.locator('.dp-modal-overlay').getByRole('button', {name: 'Cancel'}).click();
    await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
    expect((await canonical(page)).discord_webhook_url_configured).toBe(true);

    // Accepting it clears exactly that one destination.
    await clearFor(page, 'discord_webhook_url').click();
    await confirmDialog(page, 'Clear Webhook');
    await expect(clearFor(page, 'discord_webhook_url')).toBeDisabled();
    await expect(status(page, 'dp-settings-discord-card')).toHaveText('Unconfigured');
    expect((await canonical(page)).discord_webhook_url_configured).toBe(false);
  } finally {
    await restore(page, before);
  }
});

test('clearing one destination never touches another or either Enable', async ({page}) => {
  const before = await canonical(page);
  await openSettings(page, 'notifications');

  try {
    await commit(page, 'discord_webhook_url', PRIMARY);
    await commit(page, 'discord_webhook_added', ADDED);
    await commit(page, 'stats_report_webhook_url', REPORT);
    await expect(status(page, 'dp-settings-statistics-reporting-card')).toHaveText('Configured');

    // The dedicated reporting override goes; reporting stays configured because
    // the primary Discord webhook is still a valid effective destination.
    await clearFor(page, 'stats_report_webhook_url').click();
    await confirmDialog(page, 'Clear Webhook');
    let now = await canonical(page);
    expect(now.stats_report_webhook_url_configured).toBe(false);
    expect(now.discord_webhook_url_configured).toBe(true);
    expect(now.discord_webhook_added_configured).toBe(true);
    expect(now.stats_reporting_configured).toBe(true);
    await expect(status(page, 'dp-settings-statistics-reporting-card')).toHaveText('Configured');

    // The Download Added override goes; the primary is untouched.
    await clearFor(page, 'discord_webhook_added').click();
    await confirmDialog(page, 'Clear Webhook');
    now = await canonical(page);
    expect(now.discord_webhook_added_configured).toBe(false);
    expect(now.discord_webhook_url_configured).toBe(true);

    // The primary goes; reporting loses the fallback it was relying on and says
    // so honestly -- without either participation flag moving.
    await clearFor(page, 'discord_webhook_url').click();
    await confirmDialog(page, 'Clear Webhook');
    now = await canonical(page);
    expect(now.stats_reporting_configured).toBe(false);
    await expect(status(page, 'dp-settings-statistics-reporting-card')).toHaveText('Unconfigured');
    expect(now.discord_notifications_enabled).toBe(before.discord_notifications_enabled);
    expect(now.stats_reporting_enabled).toBe(before.stats_reporting_enabled);
  } finally {
    await restore(page, before);
  }
});

test('an Enable toggle gates participation without erasing configuration', async ({page}) => {
  const before = await canonical(page);
  await openSettings(page, 'notifications');

  try {
    await commit(page, 'discord_webhook_url', PRIMARY);

    await page.locator('label[for="dp-settings-field-discord-notifications-enabled"] .ttrack').click();
    await page.waitForResponse(r => r.url().includes('/api/settings') && r.request().method() === 'PUT');

    const off = await canonical(page);
    expect(off.discord_notifications_enabled).toBe(false);
    // Configuration survives, the status keeps reporting it, and the other
    // section is not touched.
    expect(off.discord_webhook_url_configured).toBe(true);
    expect(off.discord_notifications_configured).toBe(true);
    expect(off.stats_reporting_enabled).toBe(true);
    await expect(status(page, 'dp-settings-discord-card')).toHaveText('Configured');
    // And Test stays available, because it is about configuration.
    await expect(page.locator('#view-settings .dp-settings-discord-card [data-action="test-discord"]'))
      .toBeEnabled();
  } finally {
    await page.request.put('/api/settings', {data: {...before, clear_secrets: ['discord_webhook_url'],
      discord_webhook_url: ''}});
  }
});

test('Test exercises the value just committed, not stale stored configuration', async ({page}) => {
  const before = await canonical(page);
  await openSettings(page, 'notifications');

  try {
    await commit(page, 'discord_webhook_url', PRIMARY);

    // The operator types a NEW destination and reaches straight for Test
    // without leaving the field first. The action settles the pending write, so
    // what the backend tests is the value they just entered.
    let tested = '';
    await page.route('**/api/settings/validate-discord', async route => {
      tested = (await (await page.request.get('/api/settings')).json()).discord_webhook_url_configured;
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
        ok: true, notifications: {discord_notifications_configured: true,
          discord_notifications_verified: true, stats_reporting_configured: true,
          stats_reporting_verified: false}})});
    });

    const writes = [];
    page.on('request', request => {
      if (request.url().includes('/api/settings') && request.method() === 'PUT') {
        writes.push(request.postDataJSON().discord_webhook_url);
      }
    });
    await field(page, 'discord_webhook_url').fill(REPORT);
    await page.locator('#view-settings .dp-settings-discord-card [data-action="test-discord"]').click();
    await expect(status(page, 'dp-settings-discord-card')).toHaveText('Verified');

    // The pending draft reached the server BEFORE the test ran.
    expect(writes).toContain(REPORT);
    expect(tested).toBe(true);
  } finally {
    await restore(page, before);
  }
});

test('a rejected write converges the control back onto canonical truth', async ({page}) => {
  const before = await canonical(page);
  await openSettings(page, 'notifications');

  try {
    await page.route('**/api/settings', async route => {
      if (route.request().method() !== 'PUT') return route.fallback();
      await route.fulfill({status: 503, contentType: 'application/json',
        body: JSON.stringify({detail: 'Storage unavailable'})});
    });

    await field(page, 'discord_username').fill('Never Saved');
    await field(page, 'discord_username').blur();
    await expect(field(page, 'discord_username')).toHaveValue(before.discord_username);

    await page.unroute('**/api/settings');
    expect((await canonical(page)).discord_username).toBe(before.discord_username);
  } finally {
    await restore(page, before);
  }
});

test('Upload Avatar lives inside the Avatar URL field and Clear Avatar beside it', async ({page}) => {
  const before = await canonical(page);
  await openSettings(page, 'notifications');

  try {
    const placement = await page.locator('#view-settings .dp-settings-avatar-row').evaluate(row => {
      const input = row.querySelector('#dp-settings-field-discord-avatar-url');
      const compound = input.closest('.dp-action-field');
      const upload = compound?.querySelector('[data-action="upload-avatar"]');
      const clear = row.querySelector('[data-action="clear-avatar"]');
      const mid = node => { const b = node.getBoundingClientRect(); return b.y + b.height / 2; };
      return {
        uploadInsideField: !!upload,
        clearOutsideField: !clear.closest('.dp-action-field'),
        // Entered text ends where the button begins: they are siblings, not
        // one drawn over the other.
        textClearsTheButton: Math.round(upload.getBoundingClientRect().left
          - input.getBoundingClientRect().right) >= 0,
        uploadCentred: Math.abs(mid(upload) - mid(compound)) < 2,
        clearCentred: Math.abs(mid(clear) - mid(compound)) < 2,
        uploadIsAButton: upload.tagName,
        uploadFocusable: upload.tabIndex >= 0,
      };
    });
    expect(placement).toEqual({
      uploadInsideField: true, clearOutsideField: true, textClearsTheButton: true,
      uploadCentred: true, clearCentred: true, uploadIsAButton: 'BUTTON', uploadFocusable: true,
    });

    // Nothing to clear -> present, disabled, and still occupying its track.
    await expect(page.locator('#view-settings [data-action="clear-avatar"]')).toBeDisabled();
    await expect(page.locator('#view-settings [data-action="clear-avatar"]')).toBeVisible();

    await commit(page, 'discord_avatar_url', 'https://example.com/avatar.png');
    await expect(page.locator('#view-settings [data-action="clear-avatar"]')).toBeEnabled();
    await expect(page.locator('#dp-settings-avatar-preview')).toBeVisible();

    await page.locator('#view-settings [data-action="clear-avatar"]').click();
    await confirmDialog(page, 'Clear Avatar');
    await expect(page.locator('#view-settings [data-action="clear-avatar"]')).toBeDisabled();
    await expect(page.locator('#dp-settings-avatar-preview')).toBeHidden();
    expect((await canonical(page)).discord_avatar_url).toBe('');
  } finally {
    await restore(page, before);
  }
});

test('the destination row keeps its geometry in both disclosure states', async ({page}) => {
  await openSettings(page, 'notifications');
  const panel = page.locator('.dp-settings-panel[data-panel="notifications"]');

  // No horizontal overflow, and each Clear action is centred on its own field's
  // control rather than on the whole title/hint stack.
  const measured = await panel.evaluate(el => {
    const rows = [...el.querySelectorAll('.dp-settings-field-row')];
    return {
      overflow: el.scrollWidth - el.clientWidth,
      offsets: rows.map(row => {
        const control = row.querySelector('.input, .dp-action-field');
        const action = row.querySelector(':scope > button');
        const mid = node => { const b = node.getBoundingClientRect(); return b.y + b.height / 2; };
        return Math.round(mid(action) - mid(control));
      }),
      rows: rows.length,
    };
  });
  expect(measured.overflow).toBeLessThanOrEqual(0);
  expect(measured.rows).toBe(4);   // three webhooks, plus the avatar
  for (const offset of measured.offsets) expect(Math.abs(offset)).toBeLessThanOrEqual(1);
});
