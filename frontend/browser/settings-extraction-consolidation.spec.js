const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Settings consolidation -- the surfaces this batch made canonical,
 * proven against the RENDERED page and the REAL backend rather than a selector.
 *
 *   Extraction   one behaviour group, immediate booleans, changed-blur
 *                concurrency, archive passwords persisted at the composite
 *                control's own boundary, one explicit destructive Clear, and
 *                no remaining page-level save responsibility of any kind.
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
  await expect(page.locator('#view-settings [data-action="save"]')).toHaveCount(0);
  await expect(page.locator('#view-settings .dp-settings-save-hint')).toHaveCount(0);
  await expect(page.locator('#view-settings .dp-settings-master-footer')).toHaveCount(0);
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

/* The suite's designated NEUTRAL whole-settings probe.
 *
 * Every spec file shares one backend and files run concurrently, so a case may
 * only read back keys its own file owns. `disk_guard_resume_hysteresis_gb` is
 * owned by no case at all: nothing asserts its value, which is exactly why it
 * can be written from anywhere to make the ONE remaining whole-settings write
 * happen. Each file writes its own disjoint pair of values, so the draft it
 * types always differs from what is stored and the commit always occurs; and
 * because nobody reads it, it is deliberately not restored.
 *
 * What this proves is that a whole-settings write HAPPENED while this page
 * held a stale draft -- the assertions that follow are about this page's own
 * canonical values, which this file owns exclusively. */
async function writeTheWholeSettingsDocument(page) {
  const current = await page.request.get('/api/settings').then(r => r.json());
  const draft = Number(current.disk_guard_resume_hysteresis_gb) === 12.2 ? 12.3 : 12.2;
  const written = page.waitForResponse(
    r => r.url().includes('/api/settings') && r.request().method() === 'PUT', {timeout: 15000});
  await page.locator('#view-settings [data-tab="downloads"]').click();
  const probe = page.locator('#dp-settings-field-disk-guard-resume-hysteresis-gb');
  await probe.fill(String(draft));
  await probe.blur();
  await written;
}

test('a whole-settings write from another tab cannot replay Extraction state', async ({page}) => {
  await page.goto('/');
  const before = await canonical(page);
  try {
    await openSettings(page, 'extraction');
    const target = before.extract_max_concurrent === 6 ? 7 : 6;
    await concurrency(page).fill(String(target));
    await concurrency(page).blur();
    await expect.poll(async () => (await canonical(page)).extract_max_concurrent).toBe(target);

    // The one remaining whole-settings write: an ordinary field committing at
    // its own boundary. It is a read-modify-write against freshly read
    // canonical truth, so it carries nothing of this page.
    await writeTheWholeSettingsDocument(page);

    const after = await canonical(page);
    expect(after.extract_max_concurrent, 'a later write replayed a stale Extraction value').toBe(target);
    expect(after.extraction_password_configured,
      'a later write erased the stored archive passwords').toBe(before.extraction_password_configured);
  } finally {
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
      const compound = el.querySelector('.dp-settings-download-folder-field .dp-action-field');
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
        // The compound FIELD is what carries the canonical field height now;
        // the bare control inside it is drawn without material of its own.
        fieldHeight: rect(compound).height,
        browseInsideField: rect(browse).right <= rect(compound).right + 0.5
          && rect(browse).top >= rect(compound).top - 0.5
          && rect(browse).bottom <= rect(compound).bottom + 0.5,
        browseCentred: Math.abs(midY(rect(browse)) - midY(rect(compound))),
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
    // DP 1.0.13: Browse lives INSIDE the field's own border, as a sibling of
    // the path control, so path text physically ends where the button begins.
    // It is a chip within the field, not a second control matching its height.
    expect(measured.browseInsideField, 'Browse is not inside the field border').toBe(true);
    expect(measured.seam, 'path text can reach under Browse').toBeGreaterThanOrEqual(0);
    expect(measured.seam).toBeLessThanOrEqual(8);
    expect(measured.browseHeight).toBeLessThan(measured.fieldHeight);
    expect(measured.browseCentred, 'Browse is not centred in the field').toBeLessThanOrEqual(TOLERANCE);
    // Readable, but not absurdly wide merely because room exists.
    expect(measured.pathWidth).toBeGreaterThan(200);
    expect(measured.pathWidth).toBeLessThanOrEqual(460);
    expect(measured.numericWidth).toBeLessThanOrEqual(130);
    expect(measured.danglingHelp).toBe(0);

    await expect(page.locator('.dp-settings-download-limit-field .form-hint'))
      .toHaveText('Maximum downloads DebridPulse runs at once.');
  });

/* The one-line promise has to survive TEXT, not just this machine's text.
 *
 * The two informational stacks are bounded, so they wrap to different line
 * counts at different font metrics. While the island centred items of unequal
 * height that pulled the taller one's top upward, and the two settings stopped
 * sharing a baseline while still sitting on one row -- which is what the
 * hosted container reproduced at metrics ~1.08x this machine's, with only 2px
 * of local slack against a 3px tolerance to hide it. Stressing the font size
 * is what makes the promise provable rather than lucky. */
test('Download Location & Limits keeps one shared baseline at any text size',
  async ({page}) => {
    await isolateExternalFonts(page);
    await page.goto('/');
    await openSettings(page, 'downloads');

    for (const scale of [1, 1.15, 1.3]) {
      if (scale !== 1) {
        await page.addStyleTag({content:
          `#view-settings .form-hint, #view-settings .form-label { font-size: ${11 * scale}px !important; }`});
      }
      const measured = await page.locator('.dp-settings-download-engine-row').evaluate(el => {
        const rect = node => node.getBoundingClientRect();
        // The zone wrappers are `display: contents`, so the fields are the
        // island's flex items without being its element children.
        const fields = Array.from(el.querySelectorAll('.dp-settings-inline-field'));
        const tops = fields.map(field => rect(field).top);
        return {
          count: fields.length,
          topSpread: Math.max(...tops) - Math.min(...tops),
          equalHeights: new Set(fields.map(field => Math.round(rect(field).height))).size === 1,
          // Each control still centres against its OWN stack inside the field.
          centred: fields.map(field => {
            const mid = box => (box.top + box.bottom) / 2;
            return Math.abs(mid(rect(field.querySelector('.dp-settings-inline-field-control')))
              - mid(rect(field.querySelector('.dp-settings-inline-field-info'))));
          }),
        };
      });

      expect(measured.count).toBe(2);
      expect(measured.topSpread, `the two settings lost their shared baseline at ${scale}x text`)
        .toBeLessThanOrEqual(TOLERANCE);
      expect(measured.equalHeights, `the island stopped equalising item height at ${scale}x text`)
        .toBe(true);
      for (const off of measured.centred) {
        expect(Math.abs(off), `a control lost its own centring at ${scale}x text`)
          .toBeLessThanOrEqual(TOLERANCE);
      }
    }
  });


/* ── Archive Passwords: the responsive column flow ────────────────────────
 *
 * DP 1.0.13. The list is a bounded, vertical-first packing surface: entries
 * fill a column top to bottom, then the next column to the right while another
 * useful column still fits, and only once BOTH are exhausted does the region
 * itself scroll -- vertically, with the column count held at its width-derived
 * maximum. The hint and the collection controls sit in a reserved footer row
 * outside that scroll surface, and a subtle separator sits centred in each
 * inter-column gap.
 *
 * Every number below is derived from what the page actually rendered -- the
 * entry row's own height, the grid's own gaps -- so nothing here encodes a row
 * count, a column count or a viewport.
 *
 * These cases live in THIS file, beside the rest of Extraction, because the
 * suite shares one backend and its files run concurrently: `extraction_password`
 * has exactly one owning spec file, and a second file seeding it would make
 * both report failures against innocent code. */

const EDITOR = '#view-settings .dp-settings-extraction-password-editor';

async function seedPasswords(page, count) {
  const list = Array.from({length: count},
    (_, index) => `archive-password-${String(index + 1).padStart(3, '0')}`).join('\n');
  const current = await canonical(page);
  await page.request.put('/api/settings', {data: {
    ...current,
    integrations: undefined, integration_groups: undefined,
    transfer_policy: undefined, execution_runtime_limits: undefined,
    compatibility_fields: undefined, clear_secrets: [],
    extraction_password: list,
  }});
}

async function openExtraction(page) {
  await page.goto('/');
  await openSettings(page, 'extraction');
  await expect(page.locator(`${EDITOR} .dp-settings-password-region`)).toBeVisible();
  await expect.poll(() => page.evaluate(() => !!window.DPArchivePasswords?.hydrated)).toBe(true);
  await settled(page);
}

/** Let the layout owner's next animation frame land. */
const settled = page => page.evaluate(() => new Promise(resolve =>
  requestAnimationFrame(() => requestAnimationFrame(resolve))));

/** Everything this contract is about, measured from the rendered page. */
const measure = page => page.evaluate(() => {
  const editor = document.querySelector('#view-settings .dp-settings-extraction-password-editor');
  const region = editor.querySelector('.dp-settings-password-region');
  const canvas = editor.querySelector('.dp-settings-password-canvas');
  const grid = editor.querySelector('.dp-settings-password-rows');
  const footer = editor.querySelector('.dp-settings-password-footer');
  const style = getComputedStyle(grid);
  const lines = [...grid.querySelectorAll('.dp-settings-password-line')];
  const separators = [...canvas.querySelectorAll('.dp-settings-password-separator')];
  const canvasBox = canvas.getBoundingClientRect();
  const box = node => node.getBoundingClientRect();
  const columnGap = parseFloat(style.columnGap);
  const rowGap = parseFloat(style.rowGap);
  const minColumn = parseFloat(style.getPropertyValue('--dp-password-column-min'));
  const xs = [...new Set(lines.map(line => Math.round(box(line).left)))].sort((a, b) => a - b);
  const rowHeight = lines.length ? box(lines[0]).height : 0;
  return {
    entries: lines.length,
    order: lines.map(line => line.getAttribute('aria-label')),
    columnX: xs,
    columns: xs.length,
    // index of the first entry rendered in each column, in DOM order
    columnStarts: xs.map(x => lines.findIndex(line => Math.round(box(line).left) === x)),
    perColumn: xs.length > 1 ? lines.filter(l => Math.round(box(l).left) === xs[0]).length : lines.length,
    rowHeight, rowGap, columnGap, minColumn,
    columnWidth: lines.length ? Math.round(box(lines[0]).width) : 0,
    regionHeight: region.clientHeight,
    regionWidth: grid.clientWidth,
    scrollsVertically: region.scrollHeight > region.clientHeight + 1,
    scrollsHorizontally: region.scrollWidth > region.clientWidth + 1,
    editorOverflowsX: editor.scrollWidth > editor.clientWidth + 1,
    documentOverflowsX: document.scrollingElement.scrollWidth > window.innerWidth + 1,
    separators: separators.length,
    separatorCentres: separators.map(s => Math.round(box(s).left + box(s).width / 2 - canvasBox.left)),
    separatorHeightRatio: separators.length
      ? +(box(separators[0]).height / canvasBox.height).toFixed(2) : null,
    separatorTopRatio: separators.length
      ? +((box(separators[0]).top - canvasBox.top) / canvasBox.height).toFixed(2) : null,
    separatorsAriaHidden: separators.every(s => s.getAttribute('aria-hidden') === 'true'),
    footerInsideScrollRegion: region.contains(footer),
    footerBelowRegion: box(footer).top >= box(region).bottom - 1,
    footerWithinEditor: box(footer).bottom <= box(editor).bottom + 1,
    /* An entry can only reach the footer if it is actually PAINTED there. The
       region clips its own content, so what matters is each entry's visible
       rect -- its box intersected with the region's -- against the footer. */
    footerOverlapsAnyEntry: lines.some(line => {
      const entry = box(line);
      const clip = box(region);
      const top = Math.max(entry.top, clip.top);
      const bottom = Math.min(entry.bottom, clip.bottom);
      if (bottom <= top) return false;               // entirely clipped away
      const bar = box(footer);
      return bottom > bar.top + 1 && top < bar.bottom - 1
        && entry.right > bar.left + 1 && entry.left < bar.right - 1;
    }),
    /* ...which holds because the region CLIPS. An entry scrolled out of view
       still has a box below the fold; what matters is that it is not painted
       there, and that is a property of the region, not of the entry. */
    regionClips: getComputedStyle(region).overflowY,
    regionClipsX: getComputedStyle(region).overflowX,
    // the footer's own members, and whether any of them is overlapped
    guidanceRight: Math.round(box(footer.querySelector('.dp-settings-password-guidance')).right),
    actionsLeft: Math.round(box(footer.querySelector('.dp-settings-password-actions')).left),
    clearLeft: Math.round(box(footer.querySelector('.dp-settings-password-clear')).left),
    settingsScrollHeight: document.querySelector('#view-settings .dp-settings-scroll').scrollHeight,
  };
});


test.afterAll(async ({browser}) => {
  const page = await browser.newPage();
  const current = await page.request.get('/api/settings').then(r => r.json());
  await page.request.put('/api/settings', {data: {
    ...current,
    integrations: undefined, integration_groups: undefined,
    transfer_policy: undefined, execution_runtime_limits: undefined,
    compatibility_fields: undefined,
    clear_secrets: ['extraction_password'], extraction_password: '',
  }});
  await page.close();
});

// --- flow ------------------------------------------------------------------

test('a short list is one column that fills downward, with no separator',
  async ({page}) => {
    await seedPasswords(page, 4);
    await openExtraction(page);
    const m = await measure(page);
    expect(m.columns).toBe(1);
    expect(m.separators).toBe(0);
    expect(m.scrollsVertically).toBe(false);
    expect(m.scrollsHorizontally).toBe(false);
  });

test('a column takes its full visible capacity before the next one starts',
  async ({page}) => {
    // Enough to need a second column at any sane height, not enough to scroll.
    await seedPasswords(page, 24);
    await openExtraction(page);
    const m = await measure(page);
    expect(m.columns).toBeGreaterThan(1);

    // Visible vertical capacity, derived the same way the owner derives it.
    const capacity = Math.floor((m.regionHeight + m.rowGap) / (m.rowHeight + m.rowGap));
    // Column one holds exactly that, and column two begins at that DOM index --
    // which is what "fills downward first" means, as opposed to balancing.
    expect(m.perColumn).toBe(capacity);
    expect(m.columnStarts[0]).toBe(0);
    expect(m.columnStarts[1]).toBe(capacity);
    expect(m.scrollsVertically).toBe(false);
  });

test('columns are added only while another useful column still fits',
  async ({page}) => {
    await seedPasswords(page, 400);
    await openExtraction(page);
    const m = await measure(page);
    // The width-derived ceiling, computed from the same two numbers the owner uses.
    const ceiling = Math.floor((m.regionWidth + m.columnGap) / (m.minColumn + m.columnGap));
    expect(m.columns).toBe(ceiling);
    expect(m.columnWidth).toBeGreaterThanOrEqual(m.minColumn - 1);
    // One more column plus its gap demonstrably does not fit.
    expect((m.columns + 1) * m.minColumn + m.columns * m.columnGap)
      .toBeGreaterThan(m.regionWidth);
  });

// --- width -----------------------------------------------------------------

test('no horizontal scrollbar and no off-screen column, at any list size',
  async ({page}) => {
    for (const count of [4, 24, 400]) {
      await seedPasswords(page, count);
      await openExtraction(page);
      const m = await measure(page);
      expect(m.scrollsHorizontally, `${count}`).toBe(false);
      expect(m.editorOverflowsX, `${count}`).toBe(false);
      expect(m.documentOverflowsX, `${count}`).toBe(false);
      // Every rendered column starts inside the grid's own width.
      for (const x of m.columnX) {
        expect(x - m.columnX[0], `${count}`).toBeLessThan(m.regionWidth);
      }
    }
  });

// --- footer ----------------------------------------------------------------

test('the footer is structurally reserved and never covered, however long the list',
  async ({page}) => {
    for (const count of [4, 400]) {
      await seedPasswords(page, count);
      await openExtraction(page);
      const m = await measure(page);
      expect(m.footerInsideScrollRegion, `${count}`).toBe(false);
      expect(m.footerBelowRegion, `${count}`).toBe(true);
      expect(m.footerWithinEditor, `${count}`).toBe(true);
      expect(m.footerOverlapsAnyEntry, `${count}`).toBe(false);
      expect(['auto', 'scroll'], `${count}`).toContain(m.regionClips);
      expect(m.regionClipsX, `${count}`).toBe('hidden');
      // The hint and the controls do not overlap each other either.
      expect(m.actionsLeft, `${count}`).toBeGreaterThanOrEqual(m.guidanceRight);
      expect(m.clearLeft, `${count}`).toBeGreaterThanOrEqual(m.actionsLeft);
      // All three stay operable while the region scrolls.
      await expect(page.locator(`${EDITOR} .dp-settings-password-clear`)).toBeVisible();
      await expect(page.locator(`${EDITOR} .dp-settings-password-eye`)).toBeVisible();
      await expect(page.locator(`${EDITOR} .dp-settings-password-guidance`)).toBeVisible();
    }
  });

test('scrolling the region to the bottom leaves the footer exactly where it was',
  async ({page}) => {
    await seedPasswords(page, 400);
    await openExtraction(page);
    const before = await measure(page);
    expect(before.scrollsVertically).toBe(true);
    await page.locator(`${EDITOR} .dp-settings-password-region`)
      .evaluate(node => { node.scrollTop = node.scrollHeight; });
    await settled(page);
    const after = await measure(page);
    expect(after.footerBelowRegion).toBe(true);
    expect(after.footerOverlapsAnyEntry).toBe(false);
    expect(after.guidanceRight).toBe(before.guidanceRight);
  });

// --- overflow --------------------------------------------------------------

test('internal scrolling begins only after vertical AND horizontal capacity are full',
  async ({page}) => {
    await seedPasswords(page, 24);
    await openExtraction(page);
    const few = await measure(page);
    expect(few.scrollsVertically).toBe(false);   // a spare column still exists

    await seedPasswords(page, 400);
    await openExtraction(page);
    const many = await measure(page);
    const ceiling = Math.floor((many.regionWidth + many.columnGap) / (many.minColumn + many.columnGap));
    const capacity = Math.floor((many.regionHeight + many.rowGap) / (many.rowHeight + many.rowGap));
    expect(many.columns).toBe(ceiling);
    expect(many.entries).toBeGreaterThan(ceiling * capacity);   // both are exhausted
    expect(many.scrollsVertically).toBe(true);
    expect(many.scrollsHorizontally).toBe(false);
  });

test('the Settings surface stops growing once the region is full', async ({page}) => {
  await seedPasswords(page, 24);
  await openExtraction(page);
  const modest = await measure(page);
  await seedPasswords(page, 400);
  await openExtraction(page);
  const huge = await measure(page);
  // Sixteen times the passwords, and the page is no taller.
  expect(huge.settingsScrollHeight).toBe(modest.settingsScrollHeight);
});

// --- separators ------------------------------------------------------------

test('one separator per visible gap, centred in it, shortened and centred vertically',
  async ({page}) => {
    await seedPasswords(page, 400);
    await openExtraction(page);
    const m = await measure(page);
    expect(m.separators).toBe(m.columns - 1);
    expect(m.separatorsAriaHidden).toBe(true);
    // Deliberately not edge to edge: about four fifths of the list, centred.
    expect(m.separatorHeightRatio).toBeGreaterThan(0.75);
    expect(m.separatorHeightRatio).toBeLessThan(0.85);
    expect(m.separatorTopRatio).toBeGreaterThan(0.05);
    expect(m.separatorTopRatio).toBeLessThan(0.15);
    // Each one sits on the centre line of the gap it belongs to, which is
    // derived from the column width and the gap rather than asserted as pixels.
    const columnWidth = (m.regionWidth - (m.columns - 1) * m.columnGap) / m.columns;
    m.separatorCentres.forEach((centre, index) => {
      const expected = (index + 1) * columnWidth + (index + 0.5) * m.columnGap;
      expect(Math.abs(centre - expected), `gap ${index + 1}`).toBeLessThanOrEqual(1.5);
    });
  });

// --- responsive reflow -----------------------------------------------------

test('narrowing the viewport drops columns and separators together, with no overflow',
  async ({page}) => {
    await seedPasswords(page, 400);
    await openExtraction(page);
    const wide = await measure(page);
    expect(wide.columns).toBeGreaterThan(1);

    await page.setViewportSize({width: 900, height: 1000});
    await settled(page);
    await settled(page);
    const narrow = await measure(page);
    expect(narrow.columns).toBeLessThan(wide.columns);
    expect(narrow.separators).toBe(narrow.columns - 1);
    expect(narrow.columnWidth).toBeGreaterThanOrEqual(narrow.minColumn - 1);
    expect(narrow.scrollsHorizontally).toBe(false);
    expect(narrow.documentOverflowsX).toBe(false);

    // Widening again restores them, and leaves no stale separator behind.
    await page.setViewportSize({width: 1440, height: 1000});
    await settled(page);
    await settled(page);
    const restored = await measure(page);
    expect(restored.columns).toBe(wide.columns);
    expect(restored.separators).toBe(wide.columns - 1);
  });

test('a taller viewport raises row capacity and can retire the scrollbar',
  async ({page}) => {
    await page.setViewportSize({width: 1440, height: 760});
    await seedPasswords(page, 24);
    await openExtraction(page);
    const short = await measure(page);

    await page.setViewportSize({width: 1440, height: 1400});
    await settled(page);
    await settled(page);
    const tall = await measure(page);
    // More height, more rows per column -- derived, never a fixed number.
    expect(tall.perColumn).toBeGreaterThan(short.perColumn);
    expect(tall.scrollsVertically).toBe(false);
  });

test('logical password order is stable across every reflow', async ({page}) => {
  await seedPasswords(page, 400);
  await openExtraction(page);
  const wide = await measure(page);
  await page.setViewportSize({width: 900, height: 1000});
  await settled(page);
  const narrow = await measure(page);
  await page.setViewportSize({width: 1440, height: 1400});
  await settled(page);
  const tall = await measure(page);
  // DOM order IS the logical order, so tab order follows it too.
  expect(narrow.order).toEqual(wide.order);
  expect(tall.order).toEqual(wide.order);
  expect(wide.order.slice(0, 3)).toEqual(
    ['Archive password 1', 'Archive password 2', 'Archive password 3']);
});

test('reflow creates no duplicate entry controls and no duplicate observers',
  async ({page}) => {
    await seedPasswords(page, 24);
    await openExtraction(page);
    const before = await measure(page);
    for (const [width, height] of [[1100, 900], [900, 1200], [1440, 1000]]) {
      await page.setViewportSize({width, height});
      await settled(page);
    }
    await settled(page);
    const after = await measure(page);
    // One control per password, still, and exactly one of each footer control.
    expect(after.entries).toBe(before.entries);
    expect(after.order).toEqual(before.order);
    for (const selector of ['.dp-settings-password-clear', '.dp-settings-password-eye',
                            '.dp-settings-password-guidance', '.dp-settings-password-region',
                            '.dp-settings-password-canvas', '.dp-settings-password-rows']) {
      await expect(page.locator(`${EDITOR} ${selector}`)).toHaveCount(1);
    }
    // Separators are rebuilt to the exact count, never accumulated.
    expect(after.separators).toBe(after.columns - 1);
  });

// --- the add slot and existing behaviour -----------------------------------

test('Add Archive Password is the next slot in the same flow, not a pinned row',
  async ({page}) => {
    await seedPasswords(page, 24);
    await openExtraction(page);
    const m = await measure(page);
    const placeholder = await page.locator(`${EDITOR} .dp-settings-password-line[placeholder]`)
      .evaluate(node => ({
        placeholder: node.placeholder,
        index: [...node.parentElement.children].indexOf(node),
        left: Math.round(node.getBoundingClientRect().left),
      }));
    expect(placeholder.placeholder).toBe('Add an archive password');
    // It is the LAST slot in the packing flow, in whichever column that lands.
    expect(placeholder.index).toBe(m.entries - 1);
    expect(m.columnX).toContain(placeholder.left);
    // ...and it is a real, keyboard-reachable control.
    await page.locator(`${EDITOR} .dp-settings-password-line[placeholder]`).focus();
    await expect(page.locator(`${EDITOR} .dp-settings-password-line[placeholder]`)).toBeFocused();
  });
