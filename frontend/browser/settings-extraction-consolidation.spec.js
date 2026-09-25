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

test('the two extraction behaviour controls are ONE centred, content-bounded island',
  async ({page}) => {
    await isolateExternalFonts(page);
    await page.goto('/');
    await openSettings(page, 'extraction');

    const group = behaviorGroup(page);
    await expect(group).toHaveCount(1);
    // One group, not two cards.
    await expect(group.locator('.card')).toHaveCount(0);
    // Both settings are the shared inline field: stacked title + hint, control
    // beside it. Neither states a geometry of its own.
    await expect(group.locator('.dp-settings-inline-field')).toHaveCount(2);

    const geometry = await group.evaluate(el => {
      const style = getComputedStyle(el);
      const host = el.getBoundingClientRect();
      const body = el.closest('.card-body');
      const bodyBox = body.getBoundingClientRect();
      const rect = node => node.getBoundingClientRect();
      const midX = box => (box.left + box.right) / 2;
      const midY = box => (box.top + box.bottom) / 2;
      const fields = Array.from(el.querySelectorAll(':scope > .dp-settings-inline-field'));
      return {
        outline: parseFloat(style.borderTopWidth),
        width: host.width,
        bodyWidth: bodyBox.width,
        offCentreOfBody: midX(host) - midX(bodyBox),
        sideBySide: Math.abs(rect(fields[0]).top - rect(fields[1]).top) < 3,
        // Every control sits on its own stack's centre line.
        controlsCentred: fields.map(field => {
          const info = rect(field.querySelector('.dp-settings-inline-field-info'));
          const control = rect(field.querySelector('.dp-settings-inline-field-control'));
          return midY(control) - midY(info);
        }),
        // The title sits above its hint, as one informational block.
        stacked: fields.every(field =>
          rect(field.querySelector('.form-hint')).top >= rect(field.querySelector('.form-label')).bottom - 1),
        numericWidth: rect(el.querySelector('input[type="number"]')).width,
      };
    });

    // A single subtle outline around the pair.
    expect(geometry.outline).toBeGreaterThan(0);
    expect(geometry.outline).toBeLessThanOrEqual(2);
    // Content-bounded and CENTRED -- never viewport-wide, never left-anchored.
    expect(geometry.width).toBeLessThan(geometry.bodyWidth * 0.8);
    expect(Math.abs(geometry.offCentreOfBody), 'the island is not centred')
      .toBeLessThanOrEqual(TOLERANCE);
    // The two settings sit beside one another.
    expect(geometry.sideBySide).toBe(true);
    expect(geometry.stacked).toBe(true);
    for (const off of geometry.controlsCentred) {
      expect(Math.abs(off), 'a control is not centred against its title/hint stack')
        .toBeLessThanOrEqual(TOLERANCE);
    }
    // Restrained: a job count needs room for a number, not for a sentence.
    expect(geometry.numericWidth).toBeLessThanOrEqual(110);

    await expect(group.locator('.form-hint').first())
      .toHaveText('Maximum extraction jobs DebridPulse runs at once.');
  });

test('the Archive Passwords guidance lives inside the editor and intercepts nothing',
  async ({page}) => {
    await isolateExternalFonts(page);
    await page.goto('/');
    await openSettings(page, 'extraction');

    const editor = page.locator('.dp-settings-extraction-password-editor');
    const guidance = editor.locator('.dp-settings-password-guidance');
    await expect(guidance).toHaveCount(1);
    // It consumes no external row.
    await expect(page.locator('.dp-settings-extraction-password-field > .form-hint')).toHaveCount(0);

    const placement = await editor.evaluate(el => {
      const rect = node => node.getBoundingClientRect();
      const guide = el.querySelector('.dp-settings-password-guidance');
      const actions = el.querySelector('.dp-settings-password-actions');
      const box = rect(el);
      const g = rect(guide);
      const style = getComputedStyle(guide);
      const mid = r => (r.left + r.right) / 2;
      return {
        inside: g.top >= box.top && g.bottom <= box.bottom,
        centredOnEditor: mid(g) - mid(box),
        // A true box intersection, not a left/right ordering: below the width
        // where a centred line and the action row can share the bottom band,
        // the guidance takes the band ABOVE them instead of overlapping.
        overlapsActions: (a => !(g.right <= a.left || g.left >= a.right
          || g.bottom <= a.top || g.top >= a.bottom))(rect(actions)),
        pointerEvents: style.pointerEvents,
        userSelect: style.userSelect,
        reservedBottom: getComputedStyle(el).paddingBottom,
      };
    });
    expect(placement.inside).toBe(true);
    expect(Math.abs(placement.centredOnEditor)).toBeLessThanOrEqual(TOLERANCE);
    expect(placement.overlapsActions, 'the guidance overlaps the editor actions').toBe(false);
    expect(placement.pointerEvents).toBe('none');
    expect(placement.userSelect).toBe('none');

    // Hit-testing proves it: the point at its centre resolves to the EDITOR,
    // so a click, drag or caret there reaches the field, never the guidance.
    const hit = await page.evaluate(() => {
      const guide = document.querySelector('.dp-settings-password-guidance').getBoundingClientRect();
      const node = document.elementFromPoint((guide.left + guide.right) / 2, (guide.top + guide.bottom) / 2);
      return node ? node.className : null;
    });
    expect(String(hit)).not.toContain('dp-settings-password-guidance');

    // And editing still works: typing, caret movement and row insertion.
    const line = editor.locator('.dp-settings-password-line').first();
    await line.click();
    await page.keyboard.press('End');
    // The editor re-renders its rows and hands focus back on a frame, so a
    // keystroke burst with no delay can outrun it. Every password-line case in
    // this suite types at a human cadence for the same reason.
    await page.keyboard.type('ZZ', {delay: 40});
    expect(await line.inputValue()).toContain('ZZ');
    await page.keyboard.press('Home');
    expect(await page.evaluate(() => document.activeElement.selectionStart)).toBe(0);
    // Editable content never collides with the guidance or the action row.
    const collision = await editor.evaluate(el => {
      const rect = node => node.getBoundingClientRect();
      const lines = [...el.querySelectorAll('.dp-settings-password-line')];
      const last = rect(lines[lines.length - 1]);
      return {
        clearOfGuidance: last.bottom <= rect(el.querySelector('.dp-settings-password-guidance')).top + 1,
        clearOfActions: last.bottom <= rect(el.querySelector('.dp-settings-password-actions')).top + 1,
      };
    });
    expect(collision.clearOfGuidance).toBe(true);
    expect(collision.clearOfActions).toBe(true);
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

test('the AllDebrid credential row is the inline grammar, with its status inside the field',
  async ({page}) => {
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
      const rect = node => node.getBoundingClientRect();
      const midY = box => (box.top + box.bottom) / 2;
      const info = rect(row.querySelector('.dp-settings-inline-field-info'));
      const input = row.querySelector('.dp-settings-inline-field-control > .input');
      const badge = row.querySelector('.dp-settings-key-present');
      const clear = row.querySelector('[data-action="clear-alldebrid-key"]');
      const style = getComputedStyle(badge);
      const contentRight = rect(input).right - parseFloat(getComputedStyle(input).paddingRight);
      return {
        height: rect(row).height,
        stacked: rect(row.querySelector('.form-hint')).top
          >= rect(row.querySelector('.form-label')).bottom - 1,
        inputCentred: midY(rect(input)) - midY(info),
        clearCentred: midY(rect(clear)) - midY(info),
        badgeInsideField: rect(badge).right <= rect(input).right && rect(badge).left > rect(input).left,
        // Reserved trailing room: entered or masked content stops clear of it.
        contentToBadge: rect(badge).left - contentRight,
        pointerEvents: style.pointerEvents,
        userSelect: style.userSelect,
        externalStatusRow: !!document.querySelector('.dp-settings-alldebrid-key-meta'),
      };
    });

    expect(measured.stacked).toBe(true);
    // One compact row: no extra help/status band beneath it.
    expect(measured.height).toBeLessThanOrEqual(64);
    expect(Math.abs(measured.inputCentred)).toBeLessThanOrEqual(TOLERANCE);
    expect(Math.abs(measured.clearCentred)).toBeLessThanOrEqual(TOLERANCE);
    // The status lives inside the field's trailing edge, reserved for, and
    // incapable of catching a pointer or a selection.
    expect(measured.badgeInsideField).toBe(true);
    expect(measured.contentToBadge).toBeGreaterThan(0);
    expect(measured.pointerEvents).toBe('none');
    expect(measured.userSelect).toBe('none');
    expect(measured.externalStatusRow).toBe(false);

    // Hit-testing: the badge's own centre resolves to the input beneath it.
    const hit = await page.evaluate(() => {
      const b = document.querySelector('.dp-settings-key-present').getBoundingClientRect();
      const node = document.elementFromPoint((b.left + b.right) / 2, (b.top + b.bottom) / 2);
      return node ? node.id || node.className : null;
    });
    expect(String(hit)).not.toContain('dp-settings-key-present');
  });

// --- Download Location & Limits --------------------------------------------

test('Download Location & Limits puts both settings on one line in the inline grammar',
  async ({page}) => {
    await isolateExternalFonts(page);
    await page.goto('/');
    await openSettings(page, 'downloads');

    const measured = await page.locator('.dp-settings-download-engine-row').evaluate(el => {
      const rect = node => node.getBoundingClientRect();
      const midY = box => (box.top + box.bottom) / 2;
      const fields = Array.from(el.querySelectorAll('.dp-settings-inline-field'));
      const info = field => rect(field.querySelector('.dp-settings-inline-field-info'));
      const input = el.querySelector('.dp-settings-directory-field-control > .input');
      const browse = el.querySelector('.dp-settings-directory-field-browse');
      const numeric = el.querySelector('[data-setting="aria2_max_active_downloads"]');
      return {
        count: fields.length,
        oneLine: Math.abs(rect(fields[0]).top - rect(fields[1]).top) < 3,
        stacked: fields.every(field =>
          rect(field.querySelector('.form-hint')).top >= rect(field.querySelector('.form-label')).bottom - 1),
        centred: [midY(rect(input)) - midY(info(fields[0])),
                  midY(rect(numeric)) - midY(info(fields[1]))],
        seam: rect(browse).left - rect(input).right,
        browseHeight: rect(browse).height,
        inputHeight: rect(input).height,
        pathWidth: rect(input).width,
        numericWidth: rect(numeric).width,
        // Nothing stacks a help row beneath a control any more.
        danglingHelp: el.querySelectorAll('.dp-settings-field > .form-hint').length,
      };
    });

    expect(measured.count).toBe(2);
    expect(measured.oneLine, 'the two settings are not on one line').toBe(true);
    expect(measured.stacked).toBe(true);
    for (const off of measured.centred) {
      expect(Math.abs(off), 'a control is not centred against its title/hint stack')
        .toBeLessThanOrEqual(TOLERANCE);
    }
    // Browse stays part of the same compound control.
    expect(measured.seam).toBeGreaterThanOrEqual(0);
    expect(measured.seam).toBeLessThanOrEqual(8);
    expect(Math.abs(measured.browseHeight - measured.inputHeight)).toBeLessThanOrEqual(1);
    // Readable, but not absurdly wide merely because room exists.
    expect(measured.pathWidth).toBeGreaterThan(200);
    expect(measured.pathWidth).toBeLessThanOrEqual(460);
    expect(measured.numericWidth).toBeLessThanOrEqual(130);
    expect(measured.danglingHelp).toBe(0);

    await expect(page.locator('.dp-settings-download-limit-field .form-hint'))
      .toHaveText('Maximum downloads DebridPulse runs at once.');
  });
