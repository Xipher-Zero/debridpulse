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
 *     Run Backup and Backups act on the folder the operator just typed;
 *   - Backups opens the manager dialog from the shared dialog owner, not a
 *     list grown inside the Settings viewport (the manager itself is proven
 *     in settings-backup-manager.spec.js);
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
  await expect(field(page, 'stats_snapshot_keep_days')).toBeVisible();
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
      node.querySelectorAll('[data-action="backups"], [data-action="run-backup"], [data-setting="backup_enabled"]'),
      el => el.dataset.action || el.dataset.setting));
    expect(order).toEqual(['backups', 'run-backup', 'backup_enabled']);

    for (const action of ['backups', 'run-backup']) {
      const button = rail.locator(`[data-action="${action}"]`);
      await expect(button).toBeVisible();
      expect(await button.evaluate(el => el.tagName)).toBe('BUTTON');
    }
    await expect(rail.getByRole('button', {name: 'Backups', exact: true})).toBeVisible();
    await expect(rail.getByRole('button', {name: 'List Backups'})).toHaveCount(0);
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

test('Event Logging is the first card: family chip, file-text glyph, centred hint, empty rail, one island',
  async ({page}) => {
    await openMaintenance(page);
    for (const width of [1440, 1024, 820, 390]) {
      await page.setViewportSize({width, height: 900});
      await page.waitForTimeout(150);
      const facts = await page.evaluate(() => {
        const panel = document.querySelector('.dp-settings-panel[data-panel="maintenance"]');
        const cards = Array.from(panel.querySelectorAll(':scope > .dp-settings-card'),
          card => card.querySelector('.card-title').textContent.trim());
        const card = panel.querySelector('.dp-settings-event-logging-card');
        const chip = card.querySelector('.card-title .dp-settings-protocol-chip.dp-settings-header-chip');
        const style = getComputedStyle(chip);
        const reference = getComputedStyle(
          panel.querySelector('.dp-settings-backups-retention-card .dp-settings-header-chip'));
        const island = card.querySelector('.dp-settings-event-logging-island');
        const body = island.closest('.card-body');
        const bodyStyle = getComputedStyle(body);
        const inner = body.getBoundingClientRect().width - parseFloat(bodyStyle.paddingLeft)
          - parseFloat(bodyStyle.paddingRight);
        const i = island.getBoundingClientRect(), b = body.getBoundingClientRect();
        const info = island.querySelector('.dp-settings-inline-field-info').getBoundingClientRect();
        const control = island.querySelector('.dp-settings-inline-field-control').getBoundingClientRect();
        const select = island.querySelector('.dp-dropdown__trigger') || island.querySelector('select');
        return {
          islandWidth: i.width, inner,
          valueText: (island.querySelector('.dp-dropdown__value') || {}).textContent?.trim(),
          valueClipped: (() => { const v = island.querySelector('.dp-dropdown__value'); return !v || v.scrollWidth > v.clientWidth + 1; })(),
          sideBySide: info.right <= control.left + 1,
          centredRow: Math.abs((info.top + info.height / 2) - (control.top + control.height / 2)),
          controlInside: control.left >= i.left - 1 && control.right <= i.right + 1
            && select.getBoundingClientRect().right <= i.right + 1,
          cards,
          section: chip.dataset.section,
          glyph: chip.querySelector('img').getAttribute('src'),
          color: style.getPropertyValue('--dp-protocol-color').trim(),
          glow: style.filter, referenceGlow: reference.filter,
          background: style.backgroundImage, referenceBackground: reference.backgroundImage,
          hint: card.querySelector('.dp-settings-card-header-center').textContent.trim(),
          rail: card.querySelectorAll('.card-header .dp-settings-card-header-controls, .card-header button, .card-header input').length,
          islands: card.querySelectorAll('.card-body > *').length,
          share: i.width / inner,
          leftGap: i.left - (b.left + parseFloat(bodyStyle.paddingLeft)),
          rightGap: (b.right - parseFloat(bodyStyle.paddingRight)) - i.right,
          bordered: parseFloat(getComputedStyle(island).borderTopWidth) > 0,
          label: island.querySelector('.form-label').textContent.trim(),
          fieldHint: island.querySelector('.form-hint').textContent.trim(),
          options: Array.from(island.querySelectorAll('select option'), o => [o.value, o.textContent.trim()]),
          overflow: document.documentElement.scrollWidth > window.innerWidth,
        };
      });
      expect(facts.cards).toEqual(['Event Logging', 'Backups & Retention', 'Database Reset Controls']);
      expect(facts.section).toBe('maintenance');
      expect(facts.glyph).toBe('/icons/lucide/file-text.svg');
      expect(facts.color.toUpperCase()).toBe('#6366F1');
      // The family's own chip material and glow, not an approximation.
      expect(facts.glow).toBe(facts.referenceGlow);
      expect(facts.background).toBe(facts.referenceBackground);
      expect(facts.hint).toBe('Configure how many activity log entries are displayed per page.');
      expect(facts.rail).toBe(0);
      expect(facts.islands).toBe(1);
      expect(facts.bordered).toBe(true);
      expect(Math.abs(facts.leftGap - facts.rightGap)).toBeLessThan(2);
      // A quarter of the card body, centred; the label and hint stay to the
      // left of the selector, centred against it, and nothing leaves the island.
      if (width > 700) {
        // 25% of the card body, floored at the 360px the row needs.
        expect(Math.abs(facts.islandWidth - Math.max(0.25 * facts.inner, Math.min(facts.inner, 360)))).toBeLessThan(2);
        expect(facts.sideBySide).toBe(true);
        expect(facts.centredRow).toBeLessThan(2);
      }
      expect(facts.controlInside).toBe(true);
      // The selector keeps its size: its value is shown whole.
      expect(facts.valueText).toBe('100 events');
      expect(facts.valueClipped).toBe(false);
      expect(facts.label).toBe('Activity Log Page Size');
      expect(facts.fieldHint).toBe('Number of events displayed per page in the Activity Log.');
      expect(facts.options).toEqual([['50', '50 events'], ['100', '100 events'], ['250', '250 events']]);
      expect(facts.overflow).toBe(false);
    }
    // Not a control of the Activity Log itself.
    expect(await page.locator('#view-events [data-setting="activity_log_page_size"]').count()).toBe(0);
  });

test('Activity Log Page Size persists through the canonical owner and sizes the Activity Log page',
  async ({page}) => {
    const before = await keep(page, 'activity_log_page_size');
    try {
      await openMaintenance(page);
      const control = field(page, 'activity_log_page_size');
      const next = Number(before.activity_log_page_size) === 250 ? 50 : 250;
      const writes = await observeWrites(page, async () => {
        await control.selectOption(String(next));
        await page.waitForResponse(r => r.url().includes('/api/settings')
          && r.request().method() === 'PUT', {timeout: 10000});
      });
      expect(writes).toHaveLength(1);
      // The select's value on the wire; the server accepts it as the number.
      expect(Number(writes[0].sent.activity_log_page_size)).toBe(next);
      expect(writes[0].accepted.activity_log_page_size).toBe(next);
      const request = page.waitForRequest(r => r.url().includes('/api/events?'));
      await page.locator('#sidebar .nav-item[data-view="events"]').click();
      expect(new URL((await request).url()).searchParams.get('limit')).toBe(String(next));
    } finally {
      await restore(page, before);
    }
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

test('the disclosure exposes its state and holds exactly the four retention settings',
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
    ]);
    // The #2 compact-card collection, with the two related pairs grouped. The
    // event journal is kept indefinitely, so no event-log retention exists.
    expect(inside.grids).toBe(1);
    expect(inside.groups).toBe(2);
    expect(inside.titles).toEqual([
      'Backup Interval', 'Backup Retention',
      'Statistics Snapshot Interval', 'Statistics Snapshot Retention',
    ]);
    // The unit is carried inside each field, so the title no longer repeats it.
    const units = await page.locator(`#${bodyId} .dp-settings-field-unit`)
      .evaluateAll(nodes => nodes.map(n => n.textContent.trim()));
    expect(units).toEqual(['hours', 'days', 'minutes', 'days']);
  });

// --- persistence -----------------------------------------------------------

/* What the server ACCEPTED for one act, observed on the wire.
 *
 * "Persisted" is proven by the write the page sent and the document the server
 * answered with -- not by reading the document back afterwards. The suite
 * shares one backend and its files run concurrently, and the whole-settings
 * surface has no partial write, so every file's restore is a read-modify-write
 * of the WHOLE document: one that began its read a millisecond before this
 * commit will faithfully put the older value back. That is an artefact of the
 * shared fixture, not a persistence defect, and a later readback cannot tell
 * the two apart. The acceptance can. */
async function observeWrites(page, act) {
  const writes = [];
  await page.route('**/api/settings', async route => {
    if (route.request().method() !== 'PUT') { await route.continue(); return; }
    const sent = route.request().postDataJSON();
    const response = await route.fetch();
    writes.push({sent, accepted: await response.json()});
    await route.fulfill({response});
  });
  try {
    await act();
  } finally {
    await page.unroute('**/api/settings');
  }
  return writes;
}

const RETENTION_VALUES = [
  'backup_interval_hours', 'backup_keep_days', 'stats_snapshot_interval_minutes',
  'stats_snapshot_keep_days',
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
      };
      for (const [key, value] of Object.entries(probes)) {
        const control = field(page, key);
        const writes = await observeWrites(page, async () => {
          await control.fill(String(value));
          // Typing alone crosses no boundary.
          await page.waitForTimeout(250);
          await control.blur();
          await page.waitForResponse(r => r.url().includes('/api/settings')
            && r.request().method() === 'PUT', {timeout: 10000});
        });
        // Exactly one write, carrying exactly this field, and the server
        // answered with it -- which is what the control then converges on.
        expect(writes, `${key}: one boundary, one write`).toHaveLength(1);
        expect(writes[0].sent[key], `${key} is not what the page sent`).toBe(value);
        expect(writes[0].accepted[key], `${key} is not what the server accepted`).toBe(value);
        await expect(control).toHaveValue(String(value));
      }
    } finally {
      await restore(page, before);
    }
  });

test('every Data & Maintenance toggle commits immediately', async ({page}) => {
  const before = await keep(page, 'backup_enabled', 'db_wipe_enabled');
  try {
    await openMaintenance(page);
    for (const key of ['backup_enabled', 'db_wipe_enabled']) {
      const control = field(page, key);
      const track = page.locator(`label[for="dp-settings-field-${key.replaceAll('_', '-')}"]`);
      const was = await control.isChecked();
      // The flip IS the boundary: one click, one write, accepted at once.
      for (const expected of [!was, was]) {
        const writes = await observeWrites(page, async () => {
          await track.click();
          await page.waitForResponse(r => r.url().includes('/api/settings')
            && r.request().method() === 'PUT', {timeout: 10000});
        });
        expect(writes, `${key}: one flip, one write`).toHaveLength(1);
        expect(writes[0].sent[key], `${key} is not what the page sent`).toBe(expected);
        expect(writes[0].accepted[key], `${key} is not what the server accepted`).toBe(expected);
        await expect(control).toBeChecked({checked: expected});
      }
    }
  } finally {
    await restore(page, before);
  }
});

// --- operational actions settle pending writes first -----------------------

/* An action settles pending writes BEFORE it runs.
 *
 * The invariant is an ORDER -- the field's commit is sent and accepted, and
 * only then does the action go out -- so that is what this observes, on the
 * wire. Reading the shared document back instead would make the case depend
 * on no other spec file writing it in the same instant, which is not what is
 * being tested and is not something this file owns. */
async function recordSettleOrder(page, actionUrl, actionBody) {
  const timeline = [];
  let committed = null;
  await page.route('**/api/settings', async route => {
    if (route.request().method() !== 'PUT') { await route.continue(); return; }
    committed = route.request().postDataJSON();
    timeline.push('commit');
    await route.continue();
  });
  await page.route(actionUrl, async route => {
    timeline.push('action');
    await route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify(actionBody)});
  });
  return {
    timeline,
    get committed() { return committed; },
    async release() {
      await page.unroute('**/api/settings');
      await page.unroute(actionUrl);
    },
  };
}

test('Run Backup settles the Backup Folder before it runs', async ({page}) => {
  const before = await keep(page, 'backup_folder');
  const observed = await recordSettleOrder(page, '**/api/admin/backup', {ok: true, skipped: false});
  try {
    await openMaintenance(page);
    const folder = `${String(before.backup_folder || '/app/data/backups')}/settle-probe`;
    // Typed, NOT blurred: the action itself is what must flush it.
    await field(page, 'backup_folder').fill(folder);
    await backupsCard(page).locator('[data-action="run-backup"]').click();
    await expect.poll(() => observed.timeline.join(','), {timeout: 15000}).toBe('commit,action');
    expect(observed.committed.backup_folder,
      'Run Backup dispatched before the folder it depends on was committed').toBe(folder);
  } finally {
    await observed.release();
    await restore(page, before);
  }
});

test('Backups settles the Backup Folder and opens the manager in the shared dialog',
  async ({page}) => {
    const before = await keep(page, 'backup_folder');
    const observed = await recordSettleOrder(page, '**/api/admin/backups', {
      backups: Array.from({length: 40}, (_, index) => ({
        id: `20260901_0000${String(index).padStart(2, '0')}_${'a'.repeat(32)}`,
        created_at: '2026-09-01T00:00:00+00:00',
        size_bytes: 1024 * (index + 1),
        contents: 'DP State',
      })),
    });
    try {
      await openMaintenance(page);
      const folder = `${String(before.backup_folder || '/app/data/backups')}/list-probe`;
      await field(page, 'backup_folder').fill(folder);
      await backupsCard(page).locator('[data-action="backups"]').click();
      await expect.poll(() => observed.timeline.join(','), {timeout: 15000}).toBe('commit,action');
      expect(observed.committed.backup_folder,
        'Backups dispatched before the folder it depends on was committed').toBe(folder);

      // The SHARED dialog shell, closed by its own Close slot.
      const dialog = page.locator('.dp-modal-overlay .dp-modal-dialog');
      await expect(dialog).toBeVisible();
      await expect(dialog).toHaveAttribute('aria-modal', 'true');
      await expect(dialog.locator('.dp-modal-title')).toHaveText('Backups');
      await expect(dialog.locator('[data-modal-accept]')).toHaveCount(0);
      await expect(dialog.locator('[data-modal-cancel]')).toHaveText('Close');

      // Every restore point, in the backend's own order, bounded: the table
      // scrolls inside the dialog rather than growing the Settings viewport.
      await expect(dialog.locator('tbody tr')).toHaveCount(40);
      const bounded = await dialog.locator('.dp-backup-table-wrap').evaluate(node => ({
        scrollable: node.scrollHeight > node.clientHeight + 1,
        withinViewport: node.getBoundingClientRect().bottom <= window.innerHeight + 1,
      }));
      expect(bounded.scrollable).toBe(true);
      expect(bounded.withinViewport).toBe(true);

      // Escape closes it through the shared owner and returns focus.
      await page.keyboard.press('Escape');
      await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
      await expect(backupsCard(page).locator('[data-action="backups"]')).toBeFocused();
    } finally {
      await observed.release();
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
      await expect(card.getByText(/Processing is paused automatically for the reset/))
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
        .toEqual(['db_wipe_enabled', 'wipe-database']);

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
      const writes = await observeWrites(page, async () => {
        await page.locator('label[for="dp-settings-field-db-wipe-enabled"]').click();
        await page.waitForResponse(r => r.url().includes('/api/settings')
          && r.request().method() === 'PUT', {timeout: 10000});
      });
      expect(writes).toHaveLength(1);
      expect(writes[0].accepted.db_wipe_enabled).toBe(true);
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
