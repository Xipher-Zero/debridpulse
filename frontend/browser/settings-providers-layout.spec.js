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

  test('the rail reads state, then Help, then Test, then Enable/toggle', async ({page}) => {
    // Semantic order, read off the rendered DOM.
    const order = await rail(page).evaluate(el => Array.from(el.children).map(child =>
      child.matches('.dp-settings-provider-config-status') ? 'state'
        : child.matches('.dp-settings-help-chip') ? 'help'
        : child.matches('.dp-settings-header-action') ? 'action'
        : child.matches('.dp-settings-integration-header-enable') ? 'enable' : 'other'));
    expect(order).toEqual(['state', 'help', 'action', 'enable']);
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
      await expect(card(page).locator('.dp-settings-additional')).not.toHaveAttribute('open', /.*/);
      // Measured against the card's own box: opening the disclosure may scroll
      // the Services container to bring it into view, which moves the whole
      // card and is not the rail moving.
      const railIn = async () => {
        const [button, own] = [await boxOf(testButton(page)), await boxOf(card(page))];
        return {top: button.top - own.top, left: button.left - own.left};
      };
      const collapsed = await railIn();
      // Test is the header's, so the body cannot hold a copy of it in either state.
      await expect(card(page).locator(':scope > .card-body [data-action="test-alldebrid"]'))
        .toHaveCount(0);

      await summary(page).click();
      await expect(optionBody(page)).toBeVisible();
      const expanded = await railIn();
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
    // Key island -> Additional Settings directly, the summary row's own height
    // being the separation: no spacer band and no separator above the disclosure.
    const island = await boxOf(card(page).locator('.dp-settings-alldebrid-key-row'));
    expect(Math.abs(details.top - island.bottom)).toBeLessThanOrEqual(1);
    expect(await card(page).locator('.dp-settings-additional').evaluate(el =>
      parseFloat(getComputedStyle(el).borderTopWidth))).toBe(0);
  });

  test('expanded: exactly three compact tuning cells, each still bound to its setting',
    async ({page}) => {
      await summary(page).click();
      await expect(optionBody(page)).toBeVisible();
      await expect(cells(page)).toHaveCount(3);
      // The backup-torrent toggle is a cell of the same set (its checkbox is
      // the visually hidden control behind the track).
      await expect(page.locator('.dp-settings-tuning-grid [data-setting="alldebrid_prepare_backup_torrents"]'))
        .toHaveCount(1);
      for (const id of ['#dp-settings-field-alldebrid-rate-limit-per-minute',
                        '#dp-settings-field-alldebrid-max-active-torrents']) {
        await expect(page.locator(id)).toBeVisible();
        // Each control is inside a cell of the grid, and none lost its binding.
        await expect(page.locator(`.dp-settings-tuning-grid ${id}`)).toHaveCount(1);
        await expect(page.locator(id)).toHaveAttribute('data-setting', /.+/);
      }
      // The global provider intervals and resolution retries live under
      // Downloads -> Download Behavior & Limits, not on this card.
      for (const key of ['poll_interval_seconds', 'full_sync_interval_minutes',
                         'upload_fail_retry_count', 'upload_fail_retry_delay_minutes']) {
        await expect(card(page).locator(`[data-setting="${key}"]`)).toHaveCount(0);
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
      // with a bounded card centred in each. A lane is its cell plus the slack
      // centring the card in it. The set of three fills one row of three,
      // spanning the usable width (the five-lane row ceiling is proven by the
      // larger Downloads sets below).
      await expect(card(page).locator('.dp-settings-tuning-grid'))
        .toHaveAttribute('data-tuning-lanes', '3');
      const lanes = await cells(page).evaluateAll(nodes => nodes.map(node => {
        const style = getComputedStyle(node);
        return node.getBoundingClientRect().width + parseFloat(style.marginLeft) + parseFloat(style.marginRight);
      }));
      expect(lanes.length, 'the set lost a lane').toBe(3);
      expect(Math.max(...lanes) - Math.min(...lanes), 'the lanes are not equal')
        .toBeLessThanOrEqual(1);
      expect(grid.width - (lanes.reduce((a, b) => a + b, 0) + 2 * 16),
        'three lanes do not span the usable width').toBeLessThanOrEqual(2);

      for (let i = 0; i < 3; i += 1) {
        const cell = cells(page).nth(i);
        const cellBox = await boxOf(cell);
        // Bounded: a card never stretches edge to edge in its lane.
        expect(cellBox.width).toBeLessThanOrEqual(240);
        // A toggle cell has no number control: its own parts are checked.
        if (await cell.locator('.input').count() === 0) continue;
        // Label, control and help are each centred AS ELEMENTS on the cell axis.
        for (const part of ['.form-label', '.input', '.form-hint']) {
          const partBox = await boxOf(cell.locator(part));
          expect(Math.abs(partBox.centerX - cellBox.centerX),
            `cell ${i} ${part} is not centred`).toBeLessThanOrEqual(2);
        }
        // Each of these is an integer tunable, so its value is centred.
        expect(await cell.locator('.input').evaluate(el =>
          getComputedStyle(el).textAlign)).toBe('center');
      }

      // The lane count drops on its own as the width falls, every row --
      // a partial one included -- is centred, and nothing scrolls.
      let seen = [];
      for (const width of [1440, 900, 700, 420]) {
        await page.setViewportSize({width, height: 1000});
        await expect(optionBody(page)).toBeVisible();
        const measured = await card(page).locator('.dp-settings-tuning-grid').evaluate(el => {
          const host = el.getBoundingClientRect();
          const byTop = new Map();
          // The CELLS are the layout unit, whether or not a relationship group
          // is currently drawn around some of them; a cell's lane begins where
          // its centring slack begins.
          for (const child of el.querySelectorAll('.dp-settings-field')) {
            const r = child.getBoundingClientRect();
            const key = Math.round(r.top);
            if (!byTop.has(key)) byTop.set(key, []);
            const style = getComputedStyle(child);
            byTop.get(key).push([r.left - parseFloat(style.marginLeft), r.right + parseFloat(style.marginRight)]);
          }
          const rows = Array.from(byTop.values());
          return {lanes: Math.max(...rows.map(row => row.length)), rows: rows.map(spans => ({
            leading: Math.min(...spans.map(span => span[0])) - host.left,
            trailing: host.right - Math.max(...spans.map(span => span[1]))}))};
        });
        seen.push(measured.lanes);
        for (const row of measured.rows) {
          // Centred: the same slack on both sides of every row.
          expect(Math.abs(row.leading - row.trailing),
            `tuning row is not centred at ${width}px`).toBeLessThanOrEqual(2);
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
      expect(seen[0], `the set did not hold its three lanes: ${seen}`).toBe(3);
      for (const lanes of seen) expect(lanes).toBeLessThanOrEqual(3);
      expect(Math.min(...seen), `lane count never dropped: ${seen}`).toBeLessThan(3);
      await page.setViewportSize({width: 1440, height: 1000});
    });

  test('the rail keeps its order when it reflows at a narrow width', async ({page}) => {
    await page.setViewportSize({width: 560, height: 1000});
    await expect(testButton(page)).toBeVisible();
    // Reflow is allowed; losing the order, or overflowing, is not.
    const order = await rail(page).evaluate(el => Array.from(el.children).map(child =>
      child.matches('.dp-settings-provider-config-status') ? 'state'
        : child.matches('.dp-settings-help-chip') ? 'help'
        : child.matches('.dp-settings-header-action') ? 'action'
        : child.matches('.dp-settings-integration-header-enable') ? 'enable' : 'other'));
    expect(order).toEqual(['state', 'help', 'action', 'enable']);
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
    // The 1200px content area these cases were written against (1440px with the
    // full sidebar before 1.0.14; the same content beside the compact rail).
    await page.setViewportSize({width: 1272, height: 1000});
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

  /* Every cell of a region, with its lane: the cell plus the slack that
   * centres its bounded card. */
  const lanesOf = grid => grid.evaluate(el => {
    const host = el.getBoundingClientRect();
    return Array.from(el.querySelectorAll('.dp-settings-field')).map(node => {
      const r = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      return {top: Math.round(r.top), left: r.left - parseFloat(style.marginLeft) - host.left,
              right: r.right + parseFloat(style.marginRight) - host.left, host: host.width,
              lane: r.width + parseFloat(style.marginLeft) + parseFloat(style.marginRight),
              width: r.width, selector: !!node.querySelector('select.input')};
    });
  });

  test('every set declares its own cardinality, and one lane grammar serves all three',
    async ({page}) => {
      // DP 1.0.13 Settings consolidation: the ONLY thing a set contributes is
      // how many cells it has. A cell holding a selector has a lane of at
      // least one selector cell (the selector bound, 214px, and the cell's own
      // 22px), so every value it offers reads whole -- and that requirement is
      // the selector cell's ALONE: its siblings keep the set's own lanes.
      const SELECTOR_LANE = 236;
      for (const [selector, label, count] of REGIONS) {
        const grid = page.locator(`${selector} .dp-settings-tuning-grid`);
        await expect(grid, label).toHaveAttribute('data-tuning-lanes', String(count));
        const cells = await lanesOf(grid);
        expect(cells.length, label).toBe(count);
        const plain = cells.filter(cell => !cell.selector);
        expect(Math.max(...plain.map(c => c.lane)) - Math.min(...plain.map(c => c.lane)), `${label} lanes unequal`)
          .toBeLessThanOrEqual(1);
        for (const cell of cells.filter(c => c.selector)) {
          expect(cell.width, `${label} selector cell narrower than a selector cell`)
            .toBeGreaterThanOrEqual(SELECTOR_LANE - 0.5);
        }
        if (!cells.some(cell => cell.selector)) {
          // A plain set holds every lane on one row at this width.
          expect(new Set(cells.map(c => c.top)).size, `${label} does not hold one lane per cell`).toBe(1);
        }
      }
      // Network Sources at this width: File Allocation's lane is a selector
      // lane while its six siblings stay at the set's own, narrower, lanes.
      const direct = await lanesOf(page.locator('[data-executor-tuning="direct"] .dp-settings-tuning-grid'));
      const siblings = direct.filter(cell => !cell.selector);
      expect(Math.max(...siblings.map(c => c.width)), 'a sibling inherited the selector lane')
        .toBeLessThan(SELECTOR_LANE - 20);
      // Among plain sets the widest cards belong to the smallest cardinality,
      // from one rule.
      const widthOf = async selector => (await page.locator(`${selector} .dp-settings-field`)
        .first().evaluate(el => el.getBoundingClientRect().width));
      expect(await widthOf('[data-executor-tuning="usenet"]'))
        .toBeGreaterThan(await widthOf('.dp-settings-download-recovery-card'));
    });

  test('cards stay bounded, rows hold at most five and are centred, and nothing scrolls at any width',
    async ({page}) => {
      for (const width of [1600, 1280, 1024, 860, 700, 520, 400]) {
        await page.setViewportSize({width, height: 1100});
        for (const [selector, label] of REGIONS) {
          const cells = await lanesOf(page.locator(`${selector} .dp-settings-tuning-grid`));
          for (const cell of cells) {
            // Bounded: a card fits its lane, it never stretches edge to edge.
            expect(cell.width, `${label} card stretched at ${width}px`).toBeLessThanOrEqual(240);
          }
          // Settings Batch 1, Item 4: every row -- the first, a partial last
          // one and any between -- holds at most five cells and is centred by
          // the same rule: the same slack before its first lane and after its
          // last.
          const byRow = new Map();
          for (const cell of cells) {
            if (!byRow.has(cell.top)) byRow.set(cell.top, []);
            byRow.get(cell.top).push(cell);
          }
          for (const [, row] of byRow) {
            expect(row.length, `${label} row holds more than five at ${width}px`).toBeLessThanOrEqual(5);
            const leading = Math.min(...row.map(c => c.left));
            const trailing = row[0].host - Math.max(...row.map(c => c.right));
            expect(Math.abs(leading - trailing), `${label} row is not centred at ${width}px`).toBeLessThanOrEqual(2);
          }
        }
        expect(await page.evaluate(() =>
          document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1),
          `horizontal overflow at ${width}px`).toBeTruthy();
      }
      await page.setViewportSize({width: 1440, height: 1000});
    });

  const groupFacts = page => page.locator('#view-settings [data-panel="downloads"] .dp-settings-tuning-group')
    .evaluateAll(nodes => nodes.map(node => {
      const cells = Array.from(node.querySelectorAll('.dp-settings-field'))
        .map(c => Math.round(c.getBoundingClientRect().top));
      return {
        region: node.closest('[data-executor-tuning]')?.dataset.executorTuning || 'other',
        // A group inside a collapsed card has no layout at all.
        visible: node.getBoundingClientRect().width > 0,
        outlined: getComputedStyle(node).outlineStyle !== 'none' && parseFloat(getComputedStyle(node).outlineWidth) > 0,
        rows: new Set(cells).size,
        span: Number(node.dataset.tuningSpan),
        members: cells.length,
        keys: Array.from(node.querySelectorAll('[data-setting]'), el => el.dataset.setting),
      };
    }));

  test('a relationship outline is drawn exactly while its own group is whole on one row',
    async ({page}) => {
      let sawDrawn = false;
      let sawWithdrawn = false;

      for (const width of [1600, 1280, 1024, 860, 700, 520, 400]) {
        await page.setViewportSize({width, height: 1100});
        const groups = (await groupFacts(page)).filter(group => group.visible && group.region !== 'other');
        expect(groups.length, 'no relationship groups rendered').toBeGreaterThan(0);
        for (const group of groups) {
          expect(group.members, 'a group lost a member').toBe(group.span);
          // Drawn means one unbroken row with a real outline on it; a group
          // whose members share a row is never left undrawn.
          expect(group.outlined, `${group.keys[0]} outline vs ${group.rows} row(s) at ${width}px`)
            .toBe(group.rows === 1);
          if (group.outlined) sawDrawn = true;
          else sawWithdrawn = true;
        }
      }
      // The rule is meaningful only if both states actually occur.
      expect(sawDrawn, 'no outline was ever drawn').toBe(true);
      expect(sawWithdrawn, 'no outline was ever withdrawn at a narrow width').toBe(true);
      await page.setViewportSize({width: 1440, height: 1000});
    });

  /* DP 1.0.13 Network Sources relational groups. The relation is a fact of
   * each GROUP: A (connection/splitting, 3), B (transfer behaviour, 2) and C
   * (local handling, 2 -- File Allocation's selector among them). */
  const NETWORK = {
    A: ['aria2_max_connection_per_server', 'aria2_split', 'aria2_min_split_size'],
    B: ['aria2_continue_downloads', 'aria2_lowest_speed_limit'],
    C: ['aria2_disk_cache', 'aria2_file_allocation'],
  };
  const networkGroups = async page => {
    const facts = (await groupFacts(page)).filter(group => group.region === 'direct');
    return Object.fromEntries(Object.entries(NETWORK).map(([name, keys]) =>
      [name, facts.find(group => JSON.stringify(group.keys) === JSON.stringify(keys))]));
  };

  test('Network Sources keeps all three relationship outlines across a multi-row layout', async ({page}) => {
    for (const width of [1440, 1280, 1024]) {
      await page.setViewportSize({width, height: 1100});
      const grid = page.locator('[data-executor-tuning="direct"] .dp-settings-tuning-grid');
      const groups = await networkGroups(page);
      for (const [name, group] of Object.entries(groups)) {
        expect(group, `group ${name} not rendered`).toBeTruthy();
        expect(group.members).toBe(NETWORK[name].length);
        expect(group.rows, `group ${name} split at ${width}px`).toBe(1);
        expect(group.outlined, `group ${name} lost its outline at ${width}px`).toBe(true);
      }
      // The seven-control set itself spans more than one row here: that alone
      // withdraws nothing.
      const rows = new Set((await lanesOf(grid)).map(cell => cell.top)).size;
      expect(rows, `the set fits one row at ${width}px; the case is not exercised`).toBeGreaterThan(1);
    }
    await page.setViewportSize({width: 1440, height: 1000});
  });

  test('only the Network Sources group that must split withdraws its outline', async ({page}) => {
    const grid = page.locator('[data-executor-tuning="direct"] .dp-settings-tuning-grid');
    // The group minimums (Settings Batch 1, Item 4: a set of seven keeps the
    // ordinary 160px lane minimum): A 3 x 160 + 2 x 16 = 512px,
    // C 160 + 236 + 16 = 412px, B 2 x 160 + 16 = 336px. The region is set to a
    // width; each group answers for itself.
    for (const [width, whole] of [[520, 'ABC'], [480, 'BC'], [400, 'B'], [320, '']]) {
      await grid.evaluate((node, value) => { node.style.width = `${value}px`; }, width);
      const groups = await networkGroups(page);
      for (const [name, group] of Object.entries(groups)) {
        const expected = whole.includes(name);
        expect(group.rows === 1, `group ${name} whole at ${width}px`).toBe(expected);
        expect(group.outlined, `group ${name} outline at ${width}px`).toBe(expected);
      }
      // File Allocation still reads every value whole: its selector keeps the
      // one selector bound inside a lane at least one selector cell wide.
      const fileAllocation = await grid.evaluate(node => {
        const field = node.querySelector('[data-setting="aria2_file_allocation"]').closest('.dp-settings-field');
        const shell = field.querySelector('.dp-dropdown-shell') || field.querySelector('select');
        return {cell: field.getBoundingClientRect().width, control: shell.getBoundingClientRect().width};
      });
      if (width >= 236) {
        expect(fileAllocation.cell, `File Allocation lane at ${width}px`).toBeGreaterThanOrEqual(235.5);
        expect(fileAllocation.control, `File Allocation control at ${width}px`).toBeGreaterThanOrEqual(213.5);
      }
    }
    await grid.evaluate(node => { node.style.width = ''; });
  });

  test('no relationship or lane rule is keyed to a selector anywhere but its own cell', async ({page}) => {
    // Read the live cascade: the only selector-presence condition is the one
    // that sizes the selector cell itself, and no rule draws or withdraws a
    // relationship from the region's width threshold.
    const rules = await page.evaluate(() => {
      const found = [];
      const walk = list => {
        for (const rule of list) {
          if (rule.styleSheet) {
            walk(rule.styleSheet.cssRules);  // the @import graph
          } else if (rule.cssRules && !rule.selectorText) {
            if (rule.conditionText !== undefined) found.push({at: rule.cssText.split('{')[0].trim()});
            walk(rule.cssRules);
          } else if (rule.selectorText) {
            found.push({selector: rule.selectorText, text: rule.style.cssText});
          }
        }
      };
      for (const sheet of document.styleSheets) {
        try { walk(sheet.cssRules); } catch (_) { /* cross-origin sheet */ }
      }
      return found;
    });
    const selectorKeyed = rules.filter(rule => rule.selector && /:has\(select/.test(rule.selector))
      .map(rule => rule.selector);
    expect(selectorKeyed).toEqual(['#view-settings .dp-settings-tuning-grid .dp-settings-field:has(select.input)']);
    const keyedRule = rules.find(rule => rule.selector === selectorKeyed[0]);
    expect(keyedRule.text).toBe('--dp-tuning-cell-min: var(--dp-tuning-select-lane);');
    expect(rules.filter(rule => rule.at && rule.at.includes('dp-tuning'))).toEqual([]);
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

/* DP 1.0.13 provider-card corrections -- the AllDebrid credential is ONE
 * compact, centred, bordered control island (title/hint, field and Remove side
 * by side, about 65% of the body), and neither AllDebrid nor Usenet keeps a
 * blank status row above its content: AllDebrid's account region (the shared
 * account line, alldebrid-account-summary.spec.js) renders nothing while there
 * is no account to state, which is the case on this backend. */
test('the AllDebrid credential is one compact centred island and no blank row sits above it', async ({page}) => {
  await isolateExternalFonts(page);
  await page.goto('/');
  await openSources(page);
  for (const id of ['alldebrid', 'usenet']) {
    const card = page.locator(`.dp-settings-provider-card--${id}`);
    const disclosure = card.locator('.dp-settings-disclosure');
    if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
    await expect(card.locator('.dp-settings-provider-status-line')).toHaveCount(0);
  }
  const row = page.locator('.dp-settings-provider-card--alldebrid .dp-settings-alldebrid-key-row');
  // The stored AllDebrid key belongs to settings-providers-persistence.spec.js,
  // which runs concurrently on this one backend, so this file never assumes
  // whether a key is stored: the island's geometry is the same in either state,
  // and its hint must be the contextual copy for the state the island itself
  // shows (read in one snapshot, so a concurrent re-render cannot split them).
  await expect(row.locator('[data-action="clear-alldebrid-key"]')).toHaveText('Remove API Key');
  const keyState = await row.evaluate(island => ({
    hint: island.querySelector('.form-hint').textContent.trim(),
    configured: !island.querySelector('[data-action="clear-alldebrid-key"]').disabled,
  }));
  expect(keyState.hint).toBe(keyState.configured
    ? 'API key configured for your AllDebrid account.'
    : 'Enter an API key to connect your AllDebrid account.');
  const geometry = await row.evaluate(island => {
    const box = island.getBoundingClientRect();
    const body = island.parentElement.getBoundingClientRect();
    const parts = ['.dp-settings-inline-field-info', '.dp-settings-inline-field-control', '.dp-settings-inline-field-action']
      .map(selector => island.querySelector(selector).getBoundingClientRect());
    return {
      border: getComputedStyle(island).borderTopStyle,
      share: box.width / body.width,
      centred: Math.abs((box.left - body.left) - (body.right - box.right)),
      // Side by side, in order, inside the island, on one centre line.
      ordered: parts.every((part, index) => index === 0 || part.left >= parts[index - 1].right),
      inside: parts.every(part => part.left >= box.left && part.right <= box.right),
      centreLine: Math.max(...parts.map(part => (part.top + part.bottom) / 2))
        - Math.min(...parts.map(part => (part.top + part.bottom) / 2)),
      // Only the account region precedes the island, and with nothing to
      // state it is empty and takes no height.
      above: [...island.parentElement.children].slice(0, [...island.parentElement.children].indexOf(island))
        .map(node => ({account: node.matches('[data-alldebrid-account]'), children: node.childElementCount,
                       height: node.getBoundingClientRect().height})),
    };
  });
  expect(geometry.border).toBe('solid');
  expect(geometry.share).toBeGreaterThan(0.6);
  expect(geometry.share).toBeLessThan(0.7);
  expect(geometry.centred).toBeLessThan(2);
  expect(geometry.ordered).toBe(true);
  expect(geometry.inside).toBe(true);
  expect(geometry.centreLine).toBeLessThan(2);
  expect(geometry.above).toEqual([{account: true, children: 0, height: 0}]);
});

/* DP 1.0.13 -- a provider card whose body ENDS in its closed Additional Settings
 * disclosure ends there: no generic card-body padding band beneath the summary
 * row. Open, the tuning content keeps the ordinary bottom padding. Above it, the
 * disclosure follows the card's primary island directly, its own 42px summary
 * row being the separation: no spacer band. One shared structural rule; the
 * cards below are only the current members of the grammar. */
for (const viewport of [{name: 'desktop', width: 1280, height: 900}, {name: 'phone', width: 390, height: 844}]) {
test(`no blank band follows a closed terminal Additional Settings disclosure (${viewport.name})`, async ({page}) => {
  await isolateExternalFonts(page);
  await page.goto('/');
  await openSources(page);
  const ids = ['alldebrid', 'debridlink', 'realdebrid', 'torbox'];
  // Reach Services and expand the cards through the desktop layout, then
  // measure at the case's viewport (the phone-width card sizing rule is in
  // force for the phone case).
  for (const id of ids) {
    const disclosure = page.locator(`.dp-settings-provider-card--${id} .dp-settings-disclosure`);
    if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
  }
  await page.setViewportSize({width: viewport.width, height: viewport.height});
  expect(await page.evaluate(() => matchMedia('(max-width: 700px)').matches)).toBe(viewport.width <= 700);
  for (const id of ids) {
    const card = page.locator(`.dp-settings-provider-card--${id}`);
    const body = card.locator(':scope > .card-body');
    await expect(body).toBeVisible();
    const gap = () => body.evaluate(el => {
      const details = el.querySelector(':scope > .dp-settings-additional');
      const top = details.getBoundingClientRect().top;
      return {last: el.lastElementChild === details,
              band: el.getBoundingClientRect().bottom - details.getBoundingClientRect().bottom,
              // The primary island (or the region it ends) directly above.
              follows: top - details.previousElementSibling.getBoundingClientRect().bottom,
              row: details.querySelector(':scope > summary').getBoundingClientRect().height,
              left: parseFloat(getComputedStyle(el).paddingLeft), top: parseFloat(getComputedStyle(el).paddingTop)};
    });
    const closed = await gap();
    expect(closed.last).toBe(true);
    expect(closed.band).toBeLessThanOrEqual(1);
    expect(Math.abs(closed.follows), `${id}: a spacer band precedes Additional Settings`).toBeLessThanOrEqual(1);
    expect(closed.row).toBeGreaterThanOrEqual(42);           // the disclosure target is the separation
    expect(closed.left).toBeGreaterThan(0);                  // horizontal/top geometry kept
    expect(closed.top).toBeGreaterThan(0);
    const summary = card.locator('.dp-settings-additional > summary');
    await summary.scrollIntoViewIfNeeded();
    await summary.click();
    await expect(card.locator('.dp-settings-additional-body')).toBeVisible();
    const open = await gap();
    expect(open.band).toBeGreaterThan(4);                    // open content keeps its breathing room
    expect(Math.abs(open.follows)).toBeLessThanOrEqual(1);
    await summary.click();
  }
});
}

/* Settings Batch 1, Item 4 -- every tuning collection on every tab: at most
 * five cards to a row, a relationship group never split while a row of the
 * region can hold it (it moves to the next row whole), every row centred by
 * the same rule, and a relationship outline that is present but quieter than
 * before (the strong border tone at 58%, was 78%) in both themes. */
for (const theme of ['dark', 'light']) for (const width of [1920, 1440, 1180]) {
  test(`${theme} ${width}px: no tuning row holds more than five cards, groups move whole, every row is centred`, async ({page}) => {
    await isolateExternalFonts(page);
    await page.addInitScript(value => { try { localStorage.setItem('theme', value); } catch (_) {} }, theme);
    {
      await page.setViewportSize({width, height: 1100});
      for (const tab of ['sources', 'downloads', 'notifications', 'maintenance']) {
        await page.goto('/');
        await page.locator('#sidebar .nav-item[data-view="settings"]').click();
        await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
        // The tab may still be sliding in; choose it once it is there.
        await expect.poll(async () => {
          await page.locator(`#view-settings [data-tab="${tab}"]`).evaluate(button => button.click());
          return page.locator(`.dp-settings-panel[data-panel="${tab}"]`).isVisible();
        }).toBe(true);
        await page.evaluate(() => {
          document.querySelectorAll('.view.active .dp-settings-disclosure[aria-expanded="false"]').forEach(b => b.click());
          document.querySelectorAll('.view.active details').forEach(d => { d.open = true; });
        });
        await page.waitForFunction(() => ![...document.querySelectorAll('.view.active *')].some(n => n.getAnimations().length));
        const grids = await page.locator('.view.active .dp-settings-tuning-grid').evaluateAll(nodes => nodes
          .filter(grid => grid.getBoundingClientRect().width > 0)
          .map(grid => {
            const host = grid.getBoundingClientRect();
            const rows = new Map();
            for (const cell of grid.querySelectorAll('.dp-settings-field')) {
              const r = cell.getBoundingClientRect(), style = getComputedStyle(cell);
              const key = Math.round(r.top);
              if (!rows.has(key)) rows.set(key, []);
              rows.get(key).push([r.left - parseFloat(style.marginLeft) - host.left,
                                  r.right + parseFloat(style.marginRight) - host.left]);
            }
            const groups = [...grid.querySelectorAll(':scope > .dp-settings-tuning-group')].map(group => ({
              span: Number(group.dataset.tuningSpan),
              rows: new Set([...group.querySelectorAll('.dp-settings-field')].map(c => Math.round(c.getBoundingClientRect().top))).size,
              outline: getComputedStyle(group).outlineColor,
            }));
            return {lanes: Number(grid.dataset.tuningLanes), width: host.width,
                    rows: [...rows.values()].map(row => ({count: row.length,
                      leading: Math.min(...row.map(s => s[0])), trailing: host.width - Math.max(...row.map(s => s[1]))})),
                    groups};
          }));
        for (const grid of grids) {
          const where = `${theme} ${width}px ${tab} (${grid.lanes} cells)`;
          for (const row of grid.rows) {
            expect(row.count, `${where}: a row holds ${row.count}`).toBeLessThanOrEqual(5);
            expect(Math.abs(row.leading - row.trailing), `${where}: a row is not centred`).toBeLessThanOrEqual(2);
          }
          // A set of more than five continues on the next row; five or fewer
          // plain cells fill one row at these widths.
          if (grid.lanes > 5) expect(grid.rows.length, where).toBeGreaterThan(1);
          for (const group of grid.groups) {
            expect(group.rows, `${where}: a group of ${group.span} was split`).toBe(1);
            // color-mix(var(--dp-border-strong) 58%, transparent)
            expect(group.outline, where).toMatch(/\/ 0\.58\)$/);
          }
        }
      }
    }
    await expect(page.locator('body')).toHaveClass(theme === 'light' ? /\blight\b/ : /^(?!.*\blight\b).*$/);
  });
}
