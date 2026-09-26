const { test, expect } = require('@playwright/test');

/* DP 1.0.13 terminal Settings migration -- Data & Maintenance, proven against
 * the RENDERED page and the REAL backend.
 *
 * Data & Maintenance was generic Apply's last consumer. It now joins Services,
 * Downloads, Extraction, Notifications and Authentication: every editable
 * control commits at its own boundary through the ONE canonical persistence
 * owner -- a path or a number at its changed-blur boundary, a boolean the
 * moment it is flipped -- and the Settings footer is gone entirely.
 *
 * The surfaces with real risk get real proof:
 *   - an operational action settles pending field writes BEFORE it runs, so
 *     Run Backup and List Backups act on the folder the operator just typed;
 *   - the backup listing is a bounded dialog from the shared dialog owner, not
 *     a list grown inside the Settings viewport;
 *   - `Allow Database Reset` persisting immediately authorizes nothing: the
 *     destructive reset still requires its typed confirmation.
 *
 * Every case restores what it changed, so the shared backend is left as found.
 */

async function openMaintenance(page) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator('#view-settings [data-tab="maintenance"]').click();
  await expect(page.locator('.dp-settings-panel[data-panel="maintenance"]')).toBeVisible();
}

const field = (page, key) => page.locator(`#dp-settings-field-${key.replaceAll('_', '-')}`);
const backupsCard = page => page.locator('#view-settings .dp-settings-backups-retention-card');
const resetCard = page => page.locator('#view-settings .dp-settings-database-wipe-card');
const retention = page =>
  page.locator('#view-settings [data-disclosure-persist="section:backup-retention"]');

async function canonical(page) {
  const response = await page.request.get('/api/settings');
  expect(response.ok()).toBeTruthy();
  return response.json();
}

async function openRetention(page) {
  if ((await retention(page).getAttribute('aria-expanded')) !== 'true') await retention(page).click();
  await expect(field(page, 'events_keep_days')).toBeVisible();
}

/* Put back exactly the values this case changed, against FRESHLY read
 * canonical truth. The suite shares one backend with every other spec file,
 * so restoring a whole snapshot would overwrite whatever a concurrent file
 * committed in the meantime -- which is the same read-modify-write discipline
 * the application's own single-field write follows. */
async function restore(page, values) {
  const current = await canonical(page);
  await page.request.put('/api/settings', {data: {
    ...current,
    integrations: undefined, integration_groups: undefined,
    transfer_policy: undefined, execution_runtime_limits: undefined,
    compatibility_fields: undefined, clear_secrets: [],
    ...values,
  }});
}

/** The Data & Maintenance values one case owns, as they stand now. */
async function keep(page, ...names) {
  const current = await canonical(page);
  return Object.fromEntries(names.map(name => [name, current[name]]));
}

test.beforeEach(async ({page}) => {
  await page.goto('/');
});

// --- the page carries no Apply contract at all -----------------------------

test('Data & Maintenance carries no Apply contract, and neither does Settings', async ({page}) => {
  await openMaintenance(page);
  await expect(page.locator('#view-settings [data-action="save"]')).toHaveCount(0);
  await expect(page.locator('#view-settings .dp-settings-save-hint')).toHaveCount(0);
  // The footer container is DELETED, not hidden: it reserves no space either.
  await expect(page.locator('#view-settings .dp-settings-master-footer')).toHaveCount(0);
  // And no tab reintroduces one.
  for (const tab of ['sources', 'downloads', 'extraction', 'notifications', 'authentication']) {
    await page.locator(`#view-settings [data-tab="${tab}"]`).click();
    await expect(page.locator('#view-settings [data-action="save"]')).toHaveCount(0);
  }
});

// --- the header rail -------------------------------------------------------

test('both backup actions are keyboard-operable buttons in the header rail, in order',
  async ({page}) => {
    await openMaintenance(page);
    const rail = backupsCard(page).locator('.card-header');
    const order = await rail.evaluate(node => Array.from(
      node.querySelectorAll('[data-action="list-backups"], [data-action="run-backup"], [data-setting="backup_enabled"]'),
      el => el.dataset.action || el.dataset.setting));
    expect(order).toEqual(['list-backups', 'run-backup', 'backup_enabled']);

    for (const action of ['list-backups', 'run-backup']) {
      const button = rail.locator(`[data-action="${action}"]`);
      await expect(button).toBeVisible();
      expect(await button.evaluate(el => el.tagName)).toBe('BUTTON');
    }
    await expect(rail.getByRole('button', {name: 'List Backups'})).toBeVisible();
    await expect(rail.getByRole('button', {name: 'Run Backup'})).toBeVisible();
    await expect(rail.getByRole('button', {name: 'Run Backup Now'})).toHaveCount(0);

    // The old body-level action row and its inline result surface are gone.
    await expect(page.locator('#view-settings .dp-settings-backups-actions')).toHaveCount(0);
    await expect(page.locator('#dp-settings-backup-list')).toHaveCount(0);
  });

// --- the Backup Folder island ----------------------------------------------

test('Backup Folder is a centred island at roughly 70% with Browse inside the field',
  async ({page}) => {
    await openMaintenance(page);
    const geometry = await page.evaluate(() => {
      const island = document.querySelector('#view-settings .dp-settings-backup-folder-island');
      const body = island.closest('.card-body');
      const input = island.querySelector('[data-setting="backup_folder"]');
      const browse = island.querySelector('[data-action="browse-backup-folder"]');
      const compound = browse.closest('.dp-action-field');
      const i = island.getBoundingClientRect();
      const b = body.getBoundingClientRect();
      const inp = input.getBoundingClientRect();
      const btn = browse.getBoundingClientRect();
      const bodyStyle = getComputedStyle(body);
      const inner = b.width - parseFloat(bodyStyle.paddingLeft) - parseFloat(bodyStyle.paddingRight);
      return {
        share: i.width / inner,
        leftGap: i.left - (b.left + parseFloat(bodyStyle.paddingLeft)),
        rightGap: (b.right - parseFloat(bodyStyle.paddingRight)) - i.right,
        bordered: parseFloat(getComputedStyle(island).borderTopWidth) > 0,
        embedded: !!compound && compound.contains(input),
        overlap: inp.right - btn.left,
        centred: Math.abs((inp.top + inp.height / 2) - (btn.top + btn.height / 2)),
      };
    });
    expect(geometry.share).toBeGreaterThan(0.6);
    expect(geometry.share).toBeLessThan(0.8);
    expect(Math.abs(geometry.leftGap - geometry.rightGap)).toBeLessThan(2);
    expect(geometry.bordered).toBe(true);
    // Browse is a sibling of the control inside ONE field border, so the path
    // physically ends where the button begins rather than running under it.
    expect(geometry.embedded).toBe(true);
    expect(geometry.overlap).toBeLessThanOrEqual(1);
    expect(geometry.centred).toBeLessThan(2);
    // No second, external Browse survives.
    await expect(page.locator('#view-settings [data-action="browse-backup-folder"]')).toHaveCount(1);

    // The Title/Hint block is on the left, centred against the control.
    const grammar = await page.evaluate(() => {
      const row = document.querySelector('#view-settings .dp-settings-backup-folder-field');
      const info = row.querySelector('.dp-settings-inline-field-info');
      const control = row.querySelector('.dp-settings-inline-field-control');
      const i = info.getBoundingClientRect();
      const c = control.getBoundingClientRect();
      return {
        leftOfControl: i.right <= c.left + 1,
        title: info.querySelector('.form-label').textContent.trim(),
        hint: info.querySelector('.form-hint').textContent.trim(),
        centred: Math.abs((i.top + i.height / 2) - (c.top + c.height / 2)),
      };
    });
    expect(grammar.leftOfControl).toBe(true);
    expect(grammar.title).toBe('Backup Folder');
    expect(grammar.hint)
      .toBe('Choose where DebridPulse stores database and configuration backups.');
    expect(grammar.centred).toBeLessThan(2);
  });

test('opening or closing the disclosure never moves the Backup Folder island',
  async ({page}) => {
    await openMaintenance(page);
    const island = page.locator('#view-settings .dp-settings-backup-folder-island');
    const closed = await island.boundingBox();
    await openRetention(page);
    const opened = await island.boundingBox();
    expect(Math.abs(opened.width - closed.width)).toBeLessThan(1);
    expect(Math.abs(opened.x - closed.x)).toBeLessThan(1);
    expect(Math.abs(opened.y - closed.y)).toBeLessThan(1);
    await retention(page).click();
    await expect(retention(page)).toHaveAttribute('aria-expanded', 'false');
    const reclosed = await island.boundingBox();
    expect(Math.abs(reclosed.width - closed.width)).toBeLessThan(1);
  });

// --- the disclosure and the five policy values -----------------------------

test('the disclosure exposes its state and holds exactly the five retention settings',
  async ({page}) => {
    await openMaintenance(page);
    const chip = retention(page);
    await expect(chip).toHaveAttribute('aria-expanded', 'false');
    const bodyId = await chip.getAttribute('aria-controls');
    await expect(page.locator(`#${bodyId}`)).toBeHidden();
    await expect(backupsCard(page).getByText('Additional Backup & Retention Settings')).toBeVisible();

    await chip.click();
    await expect(chip).toHaveAttribute('aria-expanded', 'true');
    await expect(page.locator(`#${bodyId}`)).toBeVisible();

    const inside = await page.locator(`#${bodyId}`).evaluate(node => ({
      controls: Array.from(node.querySelectorAll('[data-setting]'), el => el.dataset.setting),
      groups: node.querySelectorAll('.dp-settings-tuning-group').length,
      grids: node.querySelectorAll('.dp-settings-tuning-grid').length,
      titles: Array.from(node.querySelectorAll('.dp-settings-field > .form-label'),
                         el => el.textContent.trim()),
    }));
    expect(inside.controls).toEqual([
      'backup_interval_hours', 'backup_keep_days',
      'stats_snapshot_interval_minutes', 'stats_snapshot_keep_days',
      'events_keep_days',
    ]);
    // The #2 compact-card collection, with the two related pairs grouped and
    // event-log retention standing alone.
    expect(inside.grids).toBe(1);
    expect(inside.groups).toBe(2);
    expect(inside.titles).toEqual([
      'Backup Interval', 'Backup Retention',
      'Statistics Snapshot Interval', 'Statistics Snapshot Retention',
      'Event Log Retention',
    ]);
    // The unit is carried inside each field, so the title no longer repeats it.
    const units = await page.locator(`#${bodyId} .dp-settings-field-unit`)
      .evaluateAll(nodes => nodes.map(n => n.textContent.trim()));
    expect(units).toEqual(['hours', 'days', 'minutes', 'days', 'days']);
  });

// --- persistence -----------------------------------------------------------

const RETENTION_VALUES = [
  'backup_interval_hours', 'backup_keep_days', 'stats_snapshot_interval_minutes',
  'stats_snapshot_keep_days', 'events_keep_days',
];

test('every Data & Maintenance value commits at its own boundary, with no Apply',
  async ({page}) => {
    const before = await keep(page, ...RETENTION_VALUES);
    try {
      await openMaintenance(page);
      await openRetention(page);
      const probes = {
        backup_interval_hours: Number(before.backup_interval_hours) === 13 ? 14 : 13,
        backup_keep_days: Number(before.backup_keep_days) === 11 ? 12 : 11,
        stats_snapshot_interval_minutes:
          Number(before.stats_snapshot_interval_minutes) === 45 ? 50 : 45,
        stats_snapshot_keep_days: Number(before.stats_snapshot_keep_days) === 21 ? 22 : 21,
        events_keep_days: Number(before.events_keep_days) === 31 ? 32 : 31,
      };
      for (const [key, value] of Object.entries(probes)) {
        const control = field(page, key);
        await control.fill(String(value));
        // Typing alone crosses no boundary.
        await page.waitForTimeout(150);
        await control.blur();
        await expect.poll(async () => (await canonical(page))[key], {timeout: 10000}).toBe(value);
      }
      // ...and they survive a reload, which is what "persisted" means.
      await page.reload();
      await openMaintenance(page);
      await openRetention(page);
      for (const [key, value] of Object.entries(probes)) {
        await expect(field(page, key)).toHaveValue(String(value));
      }
    } finally {
      await restore(page, before);
    }
  });

test('every Data & Maintenance toggle commits immediately', async ({page}) => {
  const before = await keep(page, 'backup_enabled', 'db_backup_before_wipe', 'db_wipe_enabled');
  try {
    await openMaintenance(page);
    for (const key of ['backup_enabled', 'db_backup_before_wipe', 'db_wipe_enabled']) {
      const control = field(page, key);
      const was = await control.isChecked();
      await page.locator(`label[for="dp-settings-field-${key.replaceAll('_', '-')}"]`).click();
      await expect.poll(async () => (await canonical(page))[key], {timeout: 10000}).toBe(!was);
      await page.locator(`label[for="dp-settings-field-${key.replaceAll('_', '-')}"]`).click();
      await expect.poll(async () => (await canonical(page))[key], {timeout: 10000}).toBe(was);
    }
  } finally {
    await restore(page, before);
  }
});

// --- operational actions settle pending writes first -----------------------

test('Run Backup settles the Backup Folder before it runs', async ({page}) => {
  const before = await keep(page, 'backup_folder');
  const runs = [];
  await page.route('**/api/admin/backup', async route => {
    runs.push((await canonical(page)).backup_folder);
    await route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({ok: true, skipped: false})});
  });
  try {
    await openMaintenance(page);
    const folder = `${String(before.backup_folder || '/app/data/backups')}/settle-probe`;
    // Typed, NOT blurred: the action itself is what must flush it.
    await field(page, 'backup_folder').fill(folder);
    await backupsCard(page).locator('[data-action="run-backup"]').click();
    await expect.poll(() => runs.length, {timeout: 15000}).toBe(1);
    expect(runs[0], 'Run Backup ran against a stale Backup Folder').toBe(folder);
  } finally {
    await page.unroute('**/api/admin/backup');
    await restore(page, before);
  }
});

test('List Backups settles the Backup Folder and opens a bounded shared dialog',
  async ({page}) => {
    const before = await keep(page, 'backup_folder');
    const listed = [];
    await page.route('**/api/admin/backups', async route => {
      listed.push((await canonical(page)).backup_folder);
      await route.fulfill({status: 200, contentType: 'application/json',
        body: JSON.stringify({backups: Array.from({length: 40}, (_, index) => ({
          name: `dp-backup-${String(index).padStart(3, '0')}`,
          files: ['debridpulse.db', 'config.json'],
        }))})});
    });
    try {
      await openMaintenance(page);
      const folder = `${String(before.backup_folder || '/app/data/backups')}/list-probe`;
      await field(page, 'backup_folder').fill(folder);
      await backupsCard(page).locator('[data-action="list-backups"]').click();
      await expect.poll(() => listed.length, {timeout: 15000}).toBe(1);
      expect(listed[0], 'List Backups listed a stale Backup Folder').toBe(folder);

      // The SHARED dialog shell, with nothing to accept.
      const dialog = page.locator('.dp-modal-overlay .dp-modal-dialog');
      await expect(dialog).toBeVisible();
      await expect(dialog).toHaveAttribute('aria-modal', 'true');
      await expect(dialog.locator('.dp-modal-title')).toHaveText('Backups');
      await expect(dialog.locator('[data-modal-accept]')).toHaveCount(0);
      await expect(dialog.locator('[data-modal-cancel]')).toHaveText('Close');

      // Every entry the backend listed, in the backend's own order, with its
      // own files -- and bounded, scrolling inside the dialog rather than
      // growing the Settings viewport.
      const rows = dialog.locator('.dp-settings-backup-list-row');
      await expect(rows).toHaveCount(40);
      await expect(rows.first().locator('.dp-settings-backup-list-name'))
        .toHaveText('dp-backup-000');
      await expect(rows.first().locator('.dp-settings-backup-list-files'))
        .toHaveText('debridpulse.db, config.json');
      const bounded = await dialog.locator('.dp-settings-backup-list').evaluate(node => ({
        scrollable: node.scrollHeight > node.clientHeight + 1,
        withinViewport: node.getBoundingClientRect().bottom <= window.innerHeight + 1,
      }));
      expect(bounded.scrollable).toBe(true);
      expect(bounded.withinViewport).toBe(true);
      // No management action was invented for the listing.
      await expect(dialog.getByRole('button')).toHaveCount(1);

      // Escape closes it through the shared owner and returns focus.
      await page.keyboard.press('Escape');
      await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
      await expect(backupsCard(page).locator('[data-action="list-backups"]')).toBeFocused();
      // Nothing of it is left behind in the page.
      await expect(page.locator('#view-settings .dp-settings-backup-list')).toHaveCount(0);
    } finally {
      await page.unroute('**/api/admin/backups');
      await restore(page, before);
    }
  });

// --- database reset safety -------------------------------------------------

test('Reset Database keeps its warning, its spacer, its island and its confirmation',
  async ({page}) => {
    const before = await keep(page, 'db_wipe_enabled');
    try {
      await openMaintenance(page);
      const card = resetCard(page);
      await expect(card.getByText('Database Reset is Destructive')).toBeVisible();
      await expect(card.getByText(/Processing must be paused before the database can be reset/))
        .toBeVisible();

      const layout = await card.evaluate(node => {
        const caution = node.querySelector('.dp-settings-caution');
        const spacer = node.querySelector('.dp-settings-database-reset-spacer');
        const island = node.querySelector('.dp-settings-database-wipe-row');
        const body = node.querySelector('.card-body');
        const b = body.getBoundingClientRect();
        const i = island.getBoundingClientRect();
        const style = getComputedStyle(body);
        return {
          ordered: caution.compareDocumentPosition(spacer) & Node.DOCUMENT_POSITION_FOLLOWING
                   && spacer.compareDocumentPosition(island) & Node.DOCUMENT_POSITION_FOLLOWING,
          spacing: spacer.getBoundingClientRect().height,
          silent: spacer.getAttribute('aria-hidden') === 'true' && !spacer.textContent.trim(),
          bordered: parseFloat(getComputedStyle(island).borderTopWidth) > 0,
          leftGap: i.left - (b.left + parseFloat(style.paddingLeft)),
          rightGap: (b.right - parseFloat(style.paddingRight)) - i.right,
          share: i.width / (b.width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight)),
          oneRow: Array.from(island.children)
            .every(child => Math.abs(child.getBoundingClientRect().top - i.top) < i.height),
          order: Array.from(island.querySelectorAll('[data-setting], [data-action]'),
                            el => el.dataset.setting || el.dataset.action),
        };
      });
      expect(Boolean(layout.ordered)).toBe(true);
      expect(layout.spacing).toBeGreaterThan(12);
      expect(layout.silent).toBe(true);
      expect(layout.bordered).toBe(true);
      expect(Math.abs(layout.leftGap - layout.rightGap)).toBeLessThan(2);
      expect(layout.share).toBeLessThan(0.98);   // never a full-width band
      expect(layout.oneRow).toBe(true);
      expect(layout.order)
        .toEqual(['db_backup_before_wipe', 'db_wipe_enabled', 'wipe-database']);

      // The destructive flow is untouched: a typed confirmation, and nothing
      // happens when it is declined.
      await restore(page, {db_wipe_enabled: true});
      await page.reload();
      await openMaintenance(page);
      const wiped = [];
      await page.route('**/api/admin/database/wipe', route => {
        wiped.push(route.request().postDataJSON());
        return route.fulfill({status: 409, contentType: 'application/json',
          body: JSON.stringify({detail: 'Pause processing before wiping the database'})});
      });
      await card.locator('[data-action="wipe-database"]').click();
      const dialog = page.locator('.dp-modal-overlay .dp-modal-dialog[data-tone="danger"]');
      await expect(dialog).toBeVisible();
      await expect(dialog.locator('[data-modal-accept]')).toBeDisabled();
      await dialog.locator('.dp-modal-typed input').fill('WIPE');
      await expect(dialog.locator('[data-modal-accept]')).toBeEnabled();
      await dialog.locator('[data-modal-cancel]').click();
      await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
      expect(wiped, 'a declined confirmation still reached the wipe endpoint').toHaveLength(0);
      await page.unroute('**/api/admin/database/wipe');
    } finally {
      await restore(page, before);
    }
  });

test('Allow Database Reset persists immediately but authorizes nothing on its own',
  async ({page}) => {
    const before = await keep(page, 'db_wipe_enabled');
    try {
      await restore(page, {db_wipe_enabled: false});
      await page.reload();
      await openMaintenance(page);

      // Flipping it writes at once -- no Apply, and no stale copy referring to one.
      await page.locator('label[for="dp-settings-field-db-wipe-enabled"]').click();
      await expect.poll(async () => (await canonical(page)).db_wipe_enabled, {timeout: 10000})
        .toBe(true);
      await expect(page.locator('#toasts')).not.toContainText('Apply');

      // ...and the reset STILL asks, because the toggle is a gate, not consent.
      const wiped = [];
      await page.route('**/api/admin/database/wipe', route => {
        wiped.push(route.request().postDataJSON());
        return route.fulfill({status: 409, contentType: 'application/json',
          body: JSON.stringify({detail: 'Pause processing before wiping the database'})});
      });
      await resetCard(page).locator('[data-action="wipe-database"]').click();
      await expect(page.locator('.dp-modal-overlay .dp-modal-dialog[data-tone="danger"]'))
        .toBeVisible();
      expect(wiped).toHaveLength(0);
      await page.keyboard.press('Escape');
      await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
      await page.unroute('**/api/admin/database/wipe');
    } finally {
      await restore(page, before);
    }
  });
