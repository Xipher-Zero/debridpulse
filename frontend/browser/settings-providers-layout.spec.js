const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Settings interaction foundation -- items 2, 4 and 5.
 *
 * Geometry is measured against the RENDERED layout and the element's OWN
 * available viewport, never against guessed pixel constants: the invariants
 * are "centred in the space it has" and "one shared row", both of which must
 * survive a different viewport width and a different number of cards.
 */

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

async function openSources(page) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator('#view-settings [data-tab="sources"]').click();
  await expect(page.locator('.dp-settings-panel[data-panel="sources"]')).toBeVisible();
}

/* DP 1.0.13 final interaction pass -- the provider card's operational header
 * rail, and the tuning-only disclosure beneath it.
 *
 * Test is a PROVIDER-level action, so it lives in the header beside the state it
 * proves and the participation control it is about. Every invariant below is
 * measured against the RENDERED layout, and the point of them is that the rail
 * does not move: not when the body opens, not when the credential state
 * changes, not when the body grows. */
test.describe('the AllDebrid card header rail carries state, Test and Enable', () => {
  const card = page => page.locator('.dp-settings-provider-card--alldebrid');
  const header = page => card(page).locator(':scope > .card-header');
  const rail = page => header(page).locator('.dp-settings-card-header-controls');
  const status = page => rail(page).locator('.dp-settings-provider-config-status');
  const testButton = page => rail(page).locator('[data-action="test-alldebrid"]');
  const enable = page => rail(page).locator('.dp-settings-integration-header-enable');
  const summary = page => card(page).locator('.dp-settings-additional > summary');
  const optionBody = page => card(page).locator('.dp-settings-additional-body');
  // A cell is a cell wherever it sits: directly on the line, or inside a
  // relationship group that draws a light shared outline around it.
  const cells = page => card(page).locator('.dp-settings-tuning-grid .dp-settings-field');

  const boxOf = locator => locator.evaluate(el => {
    const r = el.getBoundingClientRect();
    return {top: r.top, bottom: r.bottom, left: r.left, right: r.right,
            width: r.width, height: r.height,
            centerX: (r.left + r.right) / 2, centerY: (r.top + r.bottom) / 2};
  });

  /* The card body is hidden while the integration is switched off. Expanding
   * the card is LOCAL presentation and writes no canonical state, so this
   * geometry spec never depends on another spec's enable/disable timing. */
  test.beforeEach(async ({page}) => {
    await isolateExternalFonts(page);
    await page.goto('/');
    await openSources(page);
    await expect(card(page)).toBeVisible();
    const disclosure = card(page).locator('.dp-settings-disclosure');
    if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
    await expect(card(page).locator(':scope > .card-body')).toBeVisible();
  });

  test('the rail reads state, then Test, then Enable/toggle', async ({page}) => {
    // Semantic order, read off the rendered DOM.
    const order = await rail(page).evaluate(el => Array.from(el.children).map(child =>
      child.matches('.dp-settings-provider-config-status') ? 'state'
        : child.matches('.dp-settings-header-action') ? 'action'
        : child.matches('.dp-settings-integration-header-enable') ? 'enable' : 'other'));
    expect(order).toEqual(['state', 'action', 'enable']);
    // Visual order, left to right, on one shared centreline.
    const [s, t, e] = [await boxOf(status(page)), await boxOf(testButton(page)),
                       await boxOf(enable(page))];
    expect(s.right).toBeLessThanOrEqual(t.left + 1);
    expect(t.right).toBeLessThanOrEqual(e.left + 1);
    expect(Math.abs(s.centerY - t.centerY)).toBeLessThanOrEqual(2);
    expect(Math.abs(t.centerY - e.centerY)).toBeLessThanOrEqual(2);
    await expect(enable(page)).toContainText('Enable');
  });

  test('Test stays in the header rail collapsed and expanded, and the body never holds it',
    async ({page}) => {
      await expect(page.locator('.dp-settings-additional')).not.toHaveAttribute('open', /.*/);
      const collapsed = await boxOf(testButton(page));
      // Test is the header's, so the body cannot hold a copy of it in either state.
      await expect(card(page).locator(':scope > .card-body [data-action="test-alldebrid"]'))
        .toHaveCount(0);

      await summary(page).click();
      await expect(optionBody(page)).toBeVisible();
      const expanded = await boxOf(testButton(page));
      // The body grew by a whole option grid; the rail did not move at all.
      expect(Math.abs(expanded.top - collapsed.top)).toBeLessThanOrEqual(1);
      expect(Math.abs(expanded.left - collapsed.left)).toBeLessThanOrEqual(1);
      await expect(card(page).locator(':scope > .card-body [data-action="test-alldebrid"]'))
        .toHaveCount(0);
      await expect(testButton(page)).toHaveCount(1);
      // Still exactly one Test on the page.
      await expect(page.locator('[data-action="test-alldebrid"]')).toHaveCount(1);
    });

  test('Test is not in the credential row, and clicking it writes no credential',
    async ({page}) => {
      await expect(card(page).locator('.dp-settings-alldebrid-key-row [data-action="test-alldebrid"]'))
        .toHaveCount(0);
      const writes = [];
      page.on('request', request => {
        if (new URL(request.url()).pathname === '/api/integrations/alldebrid/configuration'
            && request.method() === 'PATCH') writes.push(request.postDataJSON());
      });
      // Nothing typed, so there is no pending commit for Test to settle: a Test
      // on its own must not produce a credential write of its own.
      await testButton(page).click();
      await page.waitForTimeout(1200);
      expect(writes).toEqual([]);
    });

  test('no provider action footer survives beneath the disclosure', async ({page}) => {
    await expect(card(page).locator('[data-action="save-alldebrid"]')).toHaveCount(0);
    await expect(card(page).locator('.dp-settings-provider-actions')).toHaveCount(0);
    await expect(card(page).locator('.dp-settings-provider-advanced')).toHaveCount(0);
    // The disclosure is the whole of the card's secondary row: it is the last
    // thing in the body, so nothing sits to its right and no band follows it.
    expect(await card(page).locator(':scope > .card-body').evaluate(el =>
      el.lastElementChild.classList.contains('dp-settings-additional'))).toBe(true);
    const head = await boxOf(summary(page));
    const details = await boxOf(card(page).locator('.dp-settings-additional'));
    expect(details.bottom - head.bottom).toBeLessThanOrEqual(2);
  });

  test('expanded: exactly five compact tuning cells, each still bound to its setting',
    async ({page}) => {
      await summary(page).click();
      await expect(optionBody(page)).toBeVisible();
      await expect(cells(page)).toHaveCount(5);
      for (const id of ['#dp-settings-field-alldebrid-rate-limit-per-minute',
                        '#dp-settings-field-poll-interval-seconds',
                        '#dp-settings-field-full-sync-interval-minutes',
                        '#dp-settings-field-upload-fail-retry-count',
                        '#dp-settings-field-upload-fail-retry-delay-minutes']) {
        await expect(page.locator(id)).toBeVisible();
        // Each control is inside a cell of the grid, and none lost its binding.
        await expect(page.locator(`.dp-settings-tuning-grid ${id}`)).toHaveCount(1);
        await expect(page.locator(id)).toHaveAttribute('data-setting', /.+/);
      }
      // No Test, no Save, no footer inside the tuning region.
      await expect(optionBody(page).locator('button')).toHaveCount(0);
    });

  test('the tuning cards are bounded, centred in their lanes and never overflow',
    async ({page}) => {
      await summary(page).click();
      await expect(optionBody(page)).toBeVisible();
      const grid = await boxOf(card(page).locator('.dp-settings-tuning-grid'));

      // DP 1.0.13 Settings consolidation: one lane per cell of the fixed set,
      // spanning the usable width, with a bounded card centred in each.
      await expect(card(page).locator('.dp-settings-tuning-grid'))
        .toHaveAttribute('data-tuning-lanes', '5');
      const lanes = await card(page).locator('.dp-settings-tuning-grid').evaluate(el =>
        getComputedStyle(el).gridTemplateColumns.split(' ').filter(Boolean).map(parseFloat));
      expect(lanes.length, 'the set lost a lane').toBe(5);
      expect(Math.max(...lanes) - Math.min(...lanes), 'the lanes are not equal')
        .toBeLessThanOrEqual(1);
      expect(grid.width - (lanes.reduce((a, b) => a + b, 0) + 4 * 16),
        'the lanes do not span the usable width').toBeLessThanOrEqual(2);

      for (let i = 0; i < 5; i += 1) {
        const cell = cells(page).nth(i);
        const cellBox = await boxOf(cell);
        // Bounded: a card never stretches edge to edge in its lane.
        expect(cellBox.width).toBeLessThanOrEqual(240);
        // Label, control and help are each centred AS ELEMENTS on the cell axis.
        for (const part of ['.form-label', '.input', '.form-hint']) {
          const partBox = await boxOf(cell.locator(part));
          expect(Math.abs(partBox.centerX - cellBox.centerX),
            `cell ${i} ${part} is not centred`).toBeLessThanOrEqual(2);
        }
        // The value inside the control keeps the canonical field alignment.
        expect(await cell.locator('.input').evaluate(el =>
          getComputedStyle(el).textAlign)).toBe('left');
      }

      // The lane count drops on its own as the width falls, the rows left-fill
      // what remains, and nothing scrolls.
      let seen = [];
      for (const width of [1440, 900, 700, 420]) {
        await page.setViewportSize({width, height: 1000});
        await expect(optionBody(page)).toBeVisible();
        const measured = await card(page).locator('.dp-settings-tuning-grid').evaluate(el => {
          const host = el.getBoundingClientRect();
          const tracks = getComputedStyle(el).gridTemplateColumns
            .split(' ').filter(Boolean).map(parseFloat);
          const byTop = new Map();
          // The CELLS are the layout unit, whether or not a relationship group
          // is currently drawn around some of them.
          for (const child of el.querySelectorAll('.dp-settings-field')) {
            const r = child.getBoundingClientRect();
            const key = Math.round(r.top);
            if (!byTop.has(key)) byTop.set(key, []);
            byTop.get(key).push(r);
          }
          return {
            lanes: tracks.length,
            lane: tracks[0],
            rows: Array.from(byTop.values()).map(boxes => ({
              leading: Math.min(...boxes.map(b => b.left)) - host.left,
              card: Math.min(...boxes.map(b => b.width)),
            })),
          };
        });
        seen.push(measured.lanes);
        for (const row of measured.rows) {
          // Left-filled: the row occupies the FIRST lane. Its bounded card is
          // centred inside that lane, so the slack is the lane's, not a
          // sparse-row offset.
          expect(row.leading,
            `tuning row does not start at the first lane at ${width}px`)
            .toBeLessThanOrEqual((measured.lane - row.card) / 2 + 2);
        }
        expect(await page.evaluate(() =>
          document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1),
          `horizontal overflow at ${width}px`).toBeTruthy();
      }
      // Full capacity at the wide viewport, never more lanes than the set has
      // cells, and a demonstrable drop at some narrower width. Monotonicity in
      // the VIEWPORT is deliberately not asserted: the Settings chrome has its
      // own breakpoints, so the region's own width is not a monotonic function
      // of the window's -- and the lane rule answers to the region alone.
      expect(seen[0], `the set did not hold all five lanes: ${seen}`).toBe(5);
      for (const lanes of seen) expect(lanes).toBeLessThanOrEqual(5);
      expect(Math.min(...seen), `lane count never dropped: ${seen}`).toBeLessThan(5);
      await page.setViewportSize({width: 1440, height: 1000});
    });

  test('the rail keeps its order when it reflows at a narrow width', async ({page}) => {
    await page.setViewportSize({width: 560, height: 1000});
    await expect(testButton(page)).toBeVisible();
    // Reflow is allowed; losing the order, or overflowing, is not.
    const order = await rail(page).evaluate(el => Array.from(el.children).map(child =>
      child.matches('.dp-settings-provider-config-status') ? 'state'
        : child.matches('.dp-settings-header-action') ? 'action'
        : child.matches('.dp-settings-integration-header-enable') ? 'enable' : 'other'));
    expect(order).toEqual(['state', 'action', 'enable']);
    expect(await page.evaluate(() =>
      document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)).toBeTruthy();
    await page.setViewportSize({width: 1440, height: 1000});
  });
});


/* DP 1.0.13 -- the Downloads tuning collections.
 *
 * Network Sources, Usenet and Download Safety & Recovery share ONE tuning-cell
 * grammar with the AllDebrid region above. What is measured here is the part
 * only a real layout can answer: that a cell never stretches, that every row
 * (including a partial one) is centred, that nothing scrolls horizontally at
 * any width, and that a relationship outline is drawn ONLY while the group it
 * describes is contiguous on one row -- disappearing entirely rather than
 * splitting across a wrap.
 */
test.describe('the Downloads tuning collections share one cell grammar', () => {
  const REGIONS = [
    ['[data-executor-tuning="direct"]', 'Network Sources', 7],
    ['[data-executor-tuning="usenet"]', 'Usenet', 4],
    ['.dp-settings-download-recovery-card', 'Disk Space & Recovery', 5],
  ];

  const geom = locator => locator.evaluate(el => {
    const r = el.getBoundingClientRect();
    return {top: r.top, bottom: r.bottom, left: r.left, right: r.right,
            width: r.width, height: r.height};
  });

  test.beforeEach(async ({page}) => {
    await isolateExternalFonts(page);
    await page.setViewportSize({width: 1440, height: 1000});
    await page.goto('/');
    await page.locator('#sidebar .nav-item[data-view="settings"]').click();
    await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
    await page.locator('#view-settings [data-tab="downloads"]').click();
    await expect(page.locator('#view-settings [data-panel="downloads"]')).toBeVisible();
    // The two executor tuning cards render collapsed; open them.
    for (const id of ['direct', 'usenet']) {
      const disclosure = page.locator(`[data-executor-tuning="${id}"] .dp-settings-disclosure`);
      if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
    }
  });

  test('every region renders its controls as cells of the one collection', async ({page}) => {
    for (const [selector, label, count] of REGIONS) {
      const region = page.locator(selector);
      await expect(region.locator('.dp-settings-tuning-grid'), `${label} has no tuning collection`)
        .toHaveCount(1);
      await expect(region.locator('.dp-settings-tuning-grid .dp-settings-field'), label)
        .toHaveCount(count);
      // File Allocation is an ordinary cell: no band of its own survives.
      await expect(region.locator('.dp-settings-engine-file-allocation')).toHaveCount(0);
      await expect(region.locator('.dp-settings-engine-tuning-grid')).toHaveCount(0);
    }
  });

  test('every set declares its own cardinality, and one lane grammar serves all three',
    async ({page}) => {
      // DP 1.0.13 Settings consolidation: the ONLY thing a set contributes is
      // how many cells it has -- plus, for a set holding a selector, that no
      // lane is narrower than one selector cell (the selector bound, 214px, and
      // the cell's own 22px), so every value it offers reads whole.
      const SELECTOR_LANE = 236;
      const GAP = 16;
      for (const [selector, label, count] of REGIONS) {
        const grid = page.locator(`${selector} .dp-settings-tuning-grid`);
        await expect(grid, label).toHaveAttribute('data-tuning-lanes', String(count));
        const {lanes, width, holdsSelector} = await grid.evaluate(el => ({
          lanes: getComputedStyle(el).gridTemplateColumns.split(' ').filter(Boolean).map(parseFloat),
          width: el.getBoundingClientRect().width,
          holdsSelector: !!el.querySelector('select.input'),
        }));
        if (holdsSelector) {
          const fits = Math.floor((width + GAP) / (SELECTOR_LANE + GAP));
          expect(lanes.length, `${label} does not hold its selector lanes`).toBe(Math.max(1, Math.min(count, fits)));
          expect(Math.min(...lanes), `${label} has a lane narrower than a selector cell`)
            .toBeGreaterThanOrEqual(SELECTOR_LANE - 0.5);
        } else {
          expect(lanes.length, `${label} does not hold one lane per cell`).toBe(count);
        }
        expect(Math.max(...lanes) - Math.min(...lanes), `${label} lanes unequal`)
          .toBeLessThanOrEqual(1);
      }
      // Among plain sets the widest cards belong to the smallest cardinality,
      // from one rule; a selector set's cards are at least one selector cell.
      const widthOf = async selector => (await page.locator(`${selector} .dp-settings-field`)
        .first().evaluate(el => el.getBoundingClientRect().width));
      expect(await widthOf('[data-executor-tuning="usenet"]'))
        .toBeGreaterThan(await widthOf('.dp-settings-download-recovery-card'));
      expect(await widthOf('[data-executor-tuning="direct"]')).toBeGreaterThanOrEqual(SELECTOR_LANE - 0.5);
    });

  test('cards stay bounded, rows left-fill their lanes, and nothing scrolls at any width',
    async ({page}) => {
      for (const width of [1600, 1280, 1024, 860, 700, 520, 400]) {
        await page.setViewportSize({width, height: 1100});
        for (const [selector, label] of REGIONS) {
          const grid = page.locator(`${selector} .dp-settings-tuning-grid`);
          const host = await geom(grid);
          const lane = await grid.evaluate(el => parseFloat(
            getComputedStyle(el).gridTemplateColumns.split(' ').filter(Boolean)[0]));
          const cells = await grid.locator('.dp-settings-field').evaluateAll(nodes =>
            nodes.map(n => {
              const r = n.getBoundingClientRect();
              return {top: Math.round(r.top), left: r.left, right: r.right, width: r.width};
            }));
          for (const cell of cells) {
            // Bounded: a card fits its lane, it never stretches edge to edge.
            expect(cell.width, `${label} card stretched at ${width}px`).toBeLessThanOrEqual(240);
          }
          // Left-filled: every row, including a partial one, occupies the FIRST
          // lane -- there is no sparse-row centring anywhere. A bounded card
          // centred inside its own lane is the lane's slack, not an offset.
          const byRow = new Map();
          for (const cell of cells) {
            if (!byRow.has(cell.top)) byRow.set(cell.top, []);
            byRow.get(cell.top).push(cell);
          }
          for (const [, row] of byRow) {
            const leading = Math.min(...row.map(c => c.left)) - host.left;
            const card_ = Math.min(...row.map(c => c.width));
            expect(leading,
              `${label} row does not start at the first lane at ${width}px`)
              .toBeLessThanOrEqual((lane - card_) / 2 + 2);
          }
        }
        expect(await page.evaluate(() =>
          document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1),
          `horizontal overflow at ${width}px`).toBeTruthy();
      }
      await page.setViewportSize({width: 1440, height: 1000});
    });

  test('a relationship outline is drawn only while its group is contiguous on one row',
    async ({page}) => {
      let sawDrawn = false;
      let sawWithdrawn = false;

      for (const width of [1600, 1280, 1024, 860, 700, 520, 400]) {
        await page.setViewportSize({width, height: 1100});
        const groups = await page.locator('#view-settings [data-panel="downloads"] .dp-settings-tuning-group')
          .evaluateAll(nodes => nodes.map(node => {
            const style = getComputedStyle(node);
            const cells = Array.from(node.querySelectorAll('.dp-settings-field'))
              .map(c => Math.round(c.getBoundingClientRect().top));
            return {
              // `display: contents` means the group is not a layout box at all.
              drawn: style.display !== 'contents',
              outlined: style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0,
              rows: new Set(cells).size,
              span: Number(node.dataset.tuningSpan),
              members: cells.length,
            };
          }));
        expect(groups.length, 'no relationship groups rendered').toBeGreaterThan(0);

        for (const group of groups) {
          expect(group.members, 'a group lost a member').toBe(group.span);
          if (group.drawn) {
            sawDrawn = true;
            // Drawn means one unbroken row, with a real outline on it.
            expect(group.rows, `outline split across ${group.rows} rows at ${width}px`).toBe(1);
            expect(group.outlined, `a drawn group carries no outline at ${width}px`).toBe(true);
          } else {
            sawWithdrawn = true;
            // Withdrawn entirely: no box, so no outline can be drawn at all.
            expect(group.outlined).toBe(false);
          }
        }
      }
      // The rule is meaningful only if both states actually occur.
      expect(sawDrawn, 'no outline was ever drawn').toBe(true);
      expect(sawWithdrawn, 'no outline was ever withdrawn at a narrow width').toBe(true);
      await page.setViewportSize({width: 1440, height: 1000});
    });

  test('the Usenet footer is a full-width centred row beneath the cells', async ({page}) => {
    const region = page.locator('[data-executor-tuning="usenet"]');
    const footer = region.locator('.dp-settings-tuning-footer');
    await expect(footer).toHaveText(/Per-server acquisition tuning belongs to each news server under\s+Services\./);
    for (const width of [1440, 900, 600]) {
      await page.setViewportSize({width, height: 1100});
      const body = await geom(region.locator('.card-body'));
      const grid = await geom(region.locator('.dp-settings-tuning-grid'));
      const line = await geom(footer);
      // Beneath the cells...
      expect(line.top).toBeGreaterThanOrEqual(grid.bottom - 1);
      // ...spanning the card, and centred on the CARD rather than on whatever
      // width the cell collection happened to occupy.
      expect(Math.abs((line.left + line.right) / 2 - (body.left + body.right) / 2))
        .toBeLessThanOrEqual(2);
    }
    await page.setViewportSize({width: 1440, height: 1000});
  });
});
