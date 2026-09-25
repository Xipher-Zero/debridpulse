const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Settings consolidation -- the surfaces this batch made canonical,
 * proven against the RENDERED page and the REAL backend rather than a selector.
 *
 *   Extraction   one behaviour group, immediate booleans, changed-blur
 *                concurrency, archive passwords persisted at the composite
 *                control's own boundary, one explicit destructive Clear, and
 *                no remaining Apply Settings responsibility of any kind.
 *   AllDebrid    the credential block keeps the canonical label / control /
 *                help rhythm rather than a taller one of its own.
 *   Downloads    Download Location & Limits is a deliberate two-zone primary
 *                form whose zones share three rows.
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

const canonical = page => page.request.get('/api/settings').then(r => r.json());
const storedPasswords = page =>
  page.request.get('/api/settings/extraction-passwords').then(r => r.json())
    .then(payload => String(payload.passwords || ''));

/* Restore the four Extraction values this file mutates. Written through the
 * same whole-settings surface the page uses, so nothing here is a second
 * persistence path either. */
async function restoreExtraction(page, before) {
  const live = await canonical(page);
  const document_ = {...live};
  for (const name of live.compatibility_fields || []) delete document_[name];
  delete document_.compatibility_fields;
  delete document_.integrations;
  delete document_.integration_groups;
  delete document_.transfer_policy;
  delete document_.execution_runtime_limits;
  await page.request.put('/api/settings', {data: {
    ...document_,
    clear_secrets: before.extraction_password_configured ? [] : ['extraction_password'],
    extract_enabled: before.extract_enabled,
    extract_delete_archive: before.extract_delete_archive,
    extract_max_concurrent: before.extract_max_concurrent,
  }});
}

const behaviorGroup = page => page.locator('.dp-settings-extraction-behavior');
const concurrency = page => page.locator('#dp-settings-field-extract-max-concurrent');
const clearPasswords = page => page.locator('[data-action="clear-archive-passwords"]');
const passwordLines = page => page.locator('.dp-settings-password-line');

// --- Extraction layout -----------------------------------------------------

test('the two extraction behaviour controls are ONE group with one shared outline',
  async ({page}) => {
    await isolateExternalFonts(page);
    await page.goto('/');
    await openSettings(page, 'extraction');

    const group = behaviorGroup(page);
    await expect(group).toHaveCount(1);
    // One group, not two cards.
    await expect(group.locator('.card')).toHaveCount(0);
    await expect(group.locator('.dp-settings-field')).toHaveCount(2);

    const geometry = await group.evaluate(el => {
      const style = getComputedStyle(el);
      const host = el.getBoundingClientRect();
      const fields = Array.from(el.querySelectorAll(':scope > .dp-settings-field'));
      const part = (field, selector) => {
        const node = field.querySelector(selector);
        return node ? node.getBoundingClientRect() : null;
      };
      return {
        outline: parseFloat(style.borderTopWidth),
        lanes: style.gridTemplateColumns.split(' ').filter(Boolean).length,
        width: host.width,
        subgrid: fields.map(field => getComputedStyle(field).gridTemplateRows),
        rows: fields.map(field => {
          const control = part(field, '.input') || part(field, '.toggle');
          return {
            label: part(field, '.form-label').top,
            // Controls of different heights sit on the same row; what they
            // share is the row's centre, not their own top edge.
            control: (control.top + control.bottom) / 2,
            help: part(field, '.form-hint').top,
            left: field.getBoundingClientRect().left - host.left,
            controlLeft: control.left - part(field, '.form-label').left,
          };
        }),
      };
    });

    // A single subtle outline around the pair.
    expect(geometry.outline).toBeGreaterThan(0);
    expect(geometry.outline).toBeLessThanOrEqual(2);
    // Two lanes that use the available width, not a centred cluster.
    expect(geometry.lanes).toBe(2);
    expect(geometry.rows[1].left).toBeGreaterThanOrEqual(geometry.width / 3 - 2);
    // The two lanes are subgrids of the SAME three rows, so they cannot drift.
    expect(geometry.subgrid[0]).toBe(geometry.subgrid[1]);
    // Both lanes align on label, control and help.
    for (const key of ['label', 'control', 'help']) {
      expect(Math.abs(geometry.rows[0][key] - geometry.rows[1][key]),
        `the two lanes disagree about the ${key} row`).toBeLessThanOrEqual(TOLERANCE);
    }
    // The boolean's control sits on its own row beneath its label, on the same
    // left datum -- never floating beside the description.
    expect(Math.abs(geometry.rows[1].controlLeft)).toBeLessThanOrEqual(TOLERANCE);

    await expect(group.locator('.form-hint').first())
      .toHaveText('Maximum extraction jobs DebridPulse runs at once.');
    // The concurrency control stays a bounded number field.
    expect(await concurrency(page).evaluate(el => el.getBoundingClientRect().width))
      .toBeLessThanOrEqual(130);
  });

// --- Extraction persistence ------------------------------------------------

test('Extraction carries no Apply contract at all', async ({page}) => {
  await page.goto('/');
  await openSettings(page, 'extraction');
  await expect(page.locator('#view-settings [data-action="save"]')).toBeHidden();
  await expect(page.locator('#view-settings .dp-settings-save-hint')).toBeHidden();
  // And the deferred clear-on-save mechanism is gone entirely, not hidden.
  await expect(page.locator('#view-settings [data-clear-secret="extraction_password"]'))
    .toHaveCount(0);
});

test('both Extraction booleans commit immediately and the count commits on blur',
  async ({page}) => {
    await page.goto('/');
    const before = await canonical(page);
    try {
      await openSettings(page, 'extraction');

      // Enable Automatic Extraction -- immediate.
      await page.locator('.dp-settings-extraction-enable .ttrack').click();
      await expect.poll(async () => (await canonical(page)).extract_enabled)
        .toBe(!before.extract_enabled);

      // Delete Archives After Extraction -- immediate.
      await behaviorGroup(page).locator('.ttrack').click();
      await expect.poll(async () => (await canonical(page)).extract_delete_archive)
        .toBe(!before.extract_delete_archive);

      // Concurrent Extractions -- changed blur, and nothing before it.
      const target = before.extract_max_concurrent === 4 ? 5 : 4;
      await concurrency(page).fill(String(target));
      expect((await canonical(page)).extract_max_concurrent,
        'the count was written before its commit boundary').toBe(before.extract_max_concurrent);
      await concurrency(page).blur();
      await expect.poll(async () => (await canonical(page)).extract_max_concurrent).toBe(target);

      // All of it survives a reload with no Apply anywhere.
      await page.reload();
      await openSettings(page, 'extraction');
      await expect(concurrency(page)).toHaveValue(String(target));
    } finally {
      await restoreExtraction(page, before);
    }
  });

test('archive passwords persist when focus leaves the editor, and keep their specialized behaviour',
  async ({page}) => {
    await page.goto('/');
    const before = await canonical(page);
    try {
      await openSettings(page, 'extraction');
      await expect.poll(() => page.evaluate(() =>
        !!(window.DPArchivePasswords && window.DPArchivePasswords.hydrated))).toBe(true);

      await passwordLines(page).last().click();
      await page.keyboard.type('hunter2', {delay: 30});
      await page.keyboard.press('Enter');
      await page.waitForTimeout(100);
      await page.keyboard.type('swordfish', {delay: 30});

      // Still masked while it is typed into, and the value never reaches the
      // whole-settings projection in the clear.
      await concurrency(page).click();                 // focus leaves the editor
      await expect.poll(() => storedPasswords(page)).toBe('hunter2\nswordfish');
      expect((await canonical(page)).extraction_password).toBe('');
      expect((await canonical(page)).extraction_password_configured).toBe(true);

      // Masking and Show all / Hide all survive.
      const masked = await passwordLines(page).first().inputValue();
      expect(masked).toMatch(/^•+$/);
      await page.locator('.dp-settings-password-eye').click();
      await expect(passwordLines(page).first()).toHaveValue('hunter2');
      await page.locator('.dp-settings-password-eye').click();
      await expect(passwordLines(page).first()).toHaveValue(masked);

      // Leaving again without changing anything writes nothing at all.
      const writes = [];
      page.on('request', request => {
        if (request.method() === 'PUT' && request.url().includes('/api/settings')) writes.push(1);
      });
      await passwordLines(page).first().click();
      await concurrency(page).click();
      await page.waitForTimeout(600);
      expect(writes, 'an unchanged editor wrote anyway').toEqual([]);

      // And the list survives a reload as separate lines.
      await page.reload();
      await openSettings(page, 'extraction');
      await expect.poll(() => passwordLines(page).count()).toBe(3);  // two + the empty tail
    } finally {
      await restoreExtraction(page, before);
    }
  });

test('Clear Passwords is the canonical destructive action, and declining mutates nothing',
  async ({page}) => {
    await page.goto('/');
    const before = await canonical(page);
    try {
      await openSettings(page, 'extraction');
      await expect.poll(() => page.evaluate(() =>
        !!(window.DPArchivePasswords && window.DPArchivePasswords.hydrated))).toBe(true);

      // Seed a stored list through the ordinary boundary.
      await passwordLines(page).last().click();
      await page.keyboard.type('to-be-erased', {delay: 30});
      await concurrency(page).click();
      await expect.poll(() => storedPasswords(page)).toBe('to-be-erased');

      const action = clearPasswords(page);
      await expect(action).toHaveClass(/btn-danger/);
      await expect(action).toBeEnabled();
      // Immediately to the LEFT of Show all / Hide all, in one action row.
      const order = await page.locator('.dp-settings-password-actions').evaluate(el =>
        Array.from(el.children).map(child => child.className));
      expect(order.length).toBe(2);
      expect(order[0]).toContain('dp-settings-password-clear');
      expect(order[1]).toContain('dp-settings-password-eye');

      // The canonical confirmation gates it, and declining performs no mutation.
      await action.click();
      await expect(page.locator('.dp-modal-overlay')).toHaveCount(1);
      await page.keyboard.press('Escape');
      await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
      await page.waitForTimeout(400);
      expect(await storedPasswords(page)).toBe('to-be-erased');

      // Confirming erases the stored list and converges the editor.
      await action.click();
      await page.locator('.dp-modal-overlay [data-modal-accept]').click();
      await expect.poll(() => storedPasswords(page)).toBe('');
      await expect.poll(() => passwordLines(page).count()).toBe(1);
      await expect(action).toBeDisabled();
      expect((await canonical(page)).extraction_password_configured).toBe(false);
    } finally {
      await restoreExtraction(page, before);
    }
  });

test('an Apply on another tab cannot replay Extraction state', async ({page}) => {
  await page.goto('/');
  const before = await canonical(page);
  try {
    await openSettings(page, 'extraction');
    const target = before.extract_max_concurrent === 6 ? 7 : 6;
    await concurrency(page).fill(String(target));
    await concurrency(page).blur();
    await expect.poll(async () => (await canonical(page)).extract_max_concurrent).toBe(target);

    // A deferred write on a tab that still has an Apply contract.
    await page.locator('#view-settings [data-tab="notifications"]').click();
    await page.locator('#dp-settings-field-discord-username').fill('ExtractionReplayProbe');
    await page.locator('#view-settings [data-action="save"]').click();
    await expect.poll(async () => (await canonical(page)).discord_username)
      .toBe('ExtractionReplayProbe');

    const after = await canonical(page);
    expect(after.extract_max_concurrent, 'Apply replayed a stale Extraction value').toBe(target);
    expect(after.extraction_password_configured,
      'Apply erased the stored archive passwords').toBe(before.extraction_password_configured);
  } finally {
    await page.request.put('/api/settings', {data: {
      ...(await canonical(page)),
      integrations: undefined, integration_groups: undefined,
      transfer_policy: undefined, execution_runtime_limits: undefined,
      compatibility_fields: undefined,
      clear_secrets: [],
      discord_username: before.discord_username,
    }});
    await restoreExtraction(page, before);
  }
});

// --- AllDebrid credential rhythm -------------------------------------------

test('the AllDebrid credential block keeps the canonical field rhythm', async ({page}) => {
  await isolateExternalFonts(page);
  const live = await page.request.get('/api/settings').then(r => r.json());
  const document_ = {...live, integrations: {...live.integrations, alldebrid: {
    ...live.integrations.alldebrid,
    options: {...(live.integrations.alldebrid?.options || {}), api_key_configured: true},
  }}};
  await page.route(url => url.pathname === '/api/settings', route =>
    (route.request().method() === 'GET'
      ? route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(document_)})
      : route.fallback()));
  await page.goto('/');
  await openSettings(page, 'sources');
  await page.locator('.dp-settings-provider-card--alldebrid .dp-settings-disclosure').click();
  await expect(page.locator('.dp-settings-alldebrid-key-row')).toBeVisible();

  const measured = await page.evaluate(() => {
    const row = document.querySelector('.dp-settings-alldebrid-key-row');
    const rect = el => el.getBoundingClientRect();
    const label = rect(row.querySelector('.dp-settings-alldebrid-key-label'));
    const input = rect(row.querySelector('.dp-settings-alldebrid-key-input'));
    const meta = rect(row.querySelector('.dp-settings-alldebrid-key-meta'));
    const help = rect(row.querySelector('.dp-settings-alldebrid-key-meta .form-hint'));
    const present = rect(row.querySelector('.dp-settings-key-present'));
    const clear = rect(row.querySelector('.dp-settings-alldebrid-key-clear .btn'));

    return {
      labelToInput: input.top - label.bottom,
      inputToHelp: help.top - input.bottom,
      clearCentre: ((clear.top + clear.bottom) / 2) - ((input.top + input.bottom) / 2),
      presentOnHelpRow: present.top - help.top,
      height: rect(row).height,
    };
  });

  // The canonical unadorned Settings field stacks label / control / help with
  // no gap of its own beyond the help text's own margin: ~0px and ~3px. The
  // credential block must not be taller than that rhythm allows.
  expect(measured.labelToInput, 'extra space between the label and the input')
    .toBeLessThanOrEqual(4);
  expect(measured.inputToHelp, 'extra space between the input and the help row')
    .toBeLessThanOrEqual(6);
  // Clear Stored API Key stays centred on the INPUT row.
  expect(Math.abs(measured.clearCentre)).toBeLessThanOrEqual(TOLERANCE);
  // Key present stays on the help row.
  expect(Math.abs(measured.presentOnHelpRow)).toBeLessThanOrEqual(TOLERANCE);
  // Whole block: label + control + help and nothing else.
  expect(measured.height).toBeLessThanOrEqual(90);
});

// --- Download Location & Limits --------------------------------------------

test('Download Location & Limits is a two-zone primary form sharing three rows',
  async ({page}) => {
    await isolateExternalFonts(page);
    await page.goto('/');
    await openSettings(page, 'downloads');

    const measured = await page.locator('.dp-settings-download-engine-row').evaluate(el => {
      const host = el.getBoundingClientRect();
      const rect = node => node.getBoundingClientRect();
      const folder = el.querySelector('.dp-settings-download-path-stack .dp-settings-field');
      const limit = el.querySelector('.dp-settings-download-limit .dp-settings-field');
      const input = el.querySelector('.dp-settings-directory-field-control > .input');
      const browse = el.querySelector('.dp-settings-directory-field-browse');
      const row = node => ({
        label: rect(node.querySelector('.form-label')).top,
        control: rect(node.querySelector('.input, .dp-settings-directory-field-control')).top,
        help: rect(node.querySelector('.form-hint')).top,
      });
      return {
        width: host.width,
        left: rect(folder).width,
        right: rect(limit).width,
        rightEdge: host.right - rect(limit).right,
        folderRows: row(folder),
        limitRows: row(limit),
        seam: rect(browse).left - rect(input).right,
        browseHeight: rect(browse).height,
        inputHeight: rect(input).height,
      };
    });

    // A dominant folder lane and a bounded concurrency lane -- not a compact
    // field stranded at the far right.
    const share = measured.left / (measured.left + measured.right);
    expect(share).toBeGreaterThan(0.66);
    expect(share).toBeLessThan(0.78);
    expect(measured.rightEdge, 'the concurrency lane does not fill its own share')
      .toBeLessThanOrEqual(TOLERANCE);
    // Both zones on the same label / control / help rows.
    for (const key of ['label', 'control', 'help']) {
      expect(Math.abs(measured.folderRows[key] - measured.limitRows[key]),
        `the two zones disagree about the ${key} row`).toBeLessThanOrEqual(TOLERANCE);
    }
    // Download Folder + Browse read as one compound control.
    expect(measured.seam).toBeGreaterThanOrEqual(0);
    expect(measured.seam).toBeLessThanOrEqual(8);
    expect(Math.abs(measured.browseHeight - measured.inputHeight)).toBeLessThanOrEqual(1);

    await expect(page.locator('.dp-settings-download-limit .form-hint'))
      .toHaveText('Maximum downloads DebridPulse runs at once.');
  });
