const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Recent Activity compact controls. Where the table itself is 987px
 * or narrower (about a 1105px window on the tablet's rail), its Status badges
 * and row actions show their glyph alone, in the same DP chip and button with
 * the glyph where it was; each label leaves the row but stays the control's
 * name, and DP's tooltip says it -- a badge its whole status, an action its own
 * tooltip. Wider, both are exactly as before and a badge has no tooltip. The
 * table's own width decides, not the window's. Mocked rows; no backend state is
 * read or written (the one pause request is answered here). Where compact,
 * Date Added also steps aside and Progress takes the room. */

function row(id, o) {
  return {
    id, name: `Row ${id}`, status: 'downloading', presentation_status: 'downloading',
    presentation_label: 'Downloading', presentation_badge_status: 'downloading', attention_required: false,
    progress: 40, active_execution_progress: null, active_execution_basis: null, size_bytes: 1000, source: 'manual',
    hash: 'a1b2c3d4e5f60718293a4b5c6d7e8f90', label: '', created_at: '2025-12-28T22:54:00Z',
    current_source_identity: {kind: 'link'}, providers: [], historical_providers: [], delivering_provider_ids: [],
    input_required: null, acquisition_eta: null, ...o,
  };
}
// Nonterminal, terminal and exceptional statuses, and both actions.
const BASE = [
  row(9801, {name: 'Downloading.mkv'}),
  row(9802, {name: 'Awaiting.mkv', status: 'input_required', presentation_status: 'input_required',
    presentation_label: 'Awaiting confirmation', presentation_badge_status: 'input_required'}),
  row(9803, {name: 'Failed.zip', status: 'error', presentation_status: 'error', presentation_label: 'Execution missing',
    presentation_badge_status: 'error'}),
  row(9804, {name: 'Paused.mkv', status: 'paused', presentation_status: 'paused', presentation_label: 'Paused',
    presentation_badge_status: 'paused'}),
  row(9805, {name: 'Done.mkv', status: 'completed', presentation_status: 'completed', presentation_label: 'Completed',
    presentation_badge_status: 'completed', progress: 100}),
];

async function open(page, items) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route(url => url.pathname === '/api/torrents', route =>
    route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items: items(), total: items().length})}));
  await page.setViewportSize({width: 1300, height: 1100});
  await page.goto('/');
  await page.evaluate(() => loadRecent());
  await expect(page.locator('#dash-tbody tr[data-torrent-id="9805"]')).toBeVisible();
}

async function resize(page, width) {
  await page.setViewportSize({width, height: 1100});
  await page.waitForFunction(() =>
    document.getElementById('sidebar').getAnimations().every(animation => animation.playState !== 'running'));
}

// The table's width and every control's presentation.
const controls = page => page.evaluate(() => {
  const r = node => node.getBoundingClientRect();
  const shown = node => Number(getComputedStyle(node).opacity) > 0.5 && getComputedStyle(node).position !== 'absolute';
  return {
    wrap: r(document.querySelector('#view-dashboard .dash-activity-table-wrap')).width,
    badges: [...document.querySelectorAll('#dash-tbody .badge')].map(badge => {
      const icon = badge.querySelector('.dp-status-icon'), label = badge.querySelector('.dp-status-label');
      return {id: badge.closest('tr').dataset.torrentId, width: r(badge).width, iconLeft: r(icon).left - r(badge).left,
        iconWidth: r(icon).width, label: label.textContent, labelShown: shown(label), tooltip: badge.getAttribute('data-tooltip'),
        glyph: icon.dataset.dpLucide, background: getComputedStyle(badge).backgroundColor, border: getComputedStyle(badge).borderTopColor};
    }),
    buttons: [...document.querySelectorAll('#dash-tbody .actions .btn')].map(button => {
      const icon = button.querySelector('.dp-row-action-icon'), label = button.querySelector('[data-dp-icon-label]');
      return {id: button.closest('tr').dataset.torrentId, width: r(button).width, height: r(button).height,
        iconShown: getComputedStyle(icon).display !== 'none', glyph: icon.dataset.dpLucide, label: label.textContent,
        labelShown: shown(label), tooltip: button.getAttribute('data-tooltip'), background: getComputedStyle(button).backgroundImage};
    }),
    titles: document.querySelectorAll('#dash-tbody [title]').length,
  };
});

const tooltip = page => page.evaluate(() => {
  const tip = document.getElementById('dp-tooltip');
  return tip && !tip.hidden ? tip.textContent : null;
});

async function hoverTooltip(page, selector) {
  await page.mouse.move(2, 2);
  await expect.poll(() => tooltip(page)).toBe(null);
  await page.locator(selector).hover();
  await page.waitForTimeout(600);                  // past the tooltip owner's 450 ms hover delay
  return tooltip(page);
}

test('the controls switch to icon only, together, where the table is 987px or narrower', async ({page}) => {
  let items = BASE;
  await open(page, () => items);
  // Find the first window, narrowing, whose table is 987px or narrower: the
  // controls are full one pixel wider and compact from there.
  let compactAt = null;
  for (let width = 1120; width >= 1090; width--) {
    await resize(page, width);
    if ((await controls(page)).wrap <= 987) { compactAt = width; break; }
  }
  expect(compactAt).not.toBeNull();
  await resize(page, compactAt + 1);
  const full = await controls(page);
  expect(full.wrap).toBeGreaterThan(987);
  for (const badge of full.badges) {
    expect(badge.labelShown, badge.id).toBe(true);
    expect(badge.tooltip, badge.id).toBe(badge.label);               // declared, but shown only while the label is not
  }
  for (const button of full.buttons) {
    expect([button.width, button.iconShown, button.labelShown], button.id).toEqual([72, false, true]);
  }
  expect(await hoverTooltip(page, '#dash-tbody tr[data-torrent-id="9802"] .badge')).toBe(null);

  await resize(page, compactAt);
  const compact = await controls(page);
  expect(compact.wrap).toBeLessThanOrEqual(987);
  compact.badges.forEach((badge, index) => {
    const before = full.badges[index];
    expect(badge.labelShown, badge.id).toBe(false);
    expect(badge.width, badge.id).toBeCloseTo(35, 0);                 // 9 + 15 + 9 and its border
    expect(badge.iconLeft, badge.id).toBeCloseTo(before.iconLeft, 1);  // the glyph where it was
    expect([badge.glyph, badge.background, badge.border, badge.label], badge.id)
      .toEqual([before.glyph, before.background, before.border, before.label]);   // the same chip
  });
  compact.buttons.forEach((button, index) => {
    const before = full.buttons[index];
    expect([button.width, button.height, button.iconShown, button.labelShown], button.id).toEqual([36, 36, true, false]);
    expect(button.glyph, button.id).toBe(before.label === 'Pause' ? 'pause' : 'play');
    expect([button.background, button.tooltip, button.label], button.id).toEqual([before.background, before.tooltip, before.label]);
  });
  expect(compact.titles).toBe(0);                                      // never a native title
});

test('compact controls say their status and action in DP tooltips, on hover and keyboard focus, and keep their names', async ({page}) => {
  let items = BASE;
  await open(page, () => items);
  await resize(page, 1000);
  expect((await controls(page)).wrap).toBeLessThanOrEqual(987);
  for (const [id, text] of [['9801', 'Downloading'], ['9802', 'Awaiting confirmation'], ['9803', 'Execution missing'],
    ['9804', 'Paused'], ['9805', 'Completed']]) {
    expect(await hoverTooltip(page, `#dash-tbody tr[data-torrent-id="${id}"] .badge`), id).toBe(text);
  }
  expect(await hoverTooltip(page, '#dash-tbody tr[data-torrent-id="9801"] .actions .btn')).toBe('Pause this download');
  expect(await hoverTooltip(page, '#dash-tbody tr[data-torrent-id="9804"] .actions .btn')).toBe('Resume this download');
  await page.mouse.move(2, 2);
  await page.locator('#dash-tbody tr[data-torrent-id="9804"] .actions .btn').focus();
  await expect.poll(() => tooltip(page)).toBe('Resume this download');
  // The tooltip sits within the viewport, beside its control.
  const placed = await page.evaluate(() => {
    const tip = document.getElementById('dp-tooltip').getBoundingClientRect();
    return tip.left >= 0 && tip.right <= innerWidth && tip.top >= 0 && tip.bottom <= innerHeight;
  });
  expect(placed).toBe(true);
  // Each control keeps its whole name.
  await expect(page.getByRole('button', {name: 'Pause', exact: true})).toHaveCount(1);
  await expect(page.getByRole('button', {name: 'Resume', exact: true})).toHaveCount(1);
  await expect(page.locator('#dash-tbody tr[data-torrent-id="9802"] .badge')).toHaveText('Awaiting confirmation');
});

test('a status change updates the compact badge, its tooltip and its action', async ({page}) => {
  let items = BASE;
  await open(page, () => items);
  await resize(page, 1000);
  items = BASE.map(item => item.id === 9801 ? {...item, status: 'paused', presentation_status: 'paused',
    presentation_label: 'Paused', presentation_badge_status: 'paused'} : item);
  await page.evaluate(() => loadRecent());
  const rowSelector = '#dash-tbody tr[data-torrent-id="9801"]';
  await expect(page.locator(`${rowSelector} .badge`)).toHaveAttribute('data-dp-status', 'paused');
  const facts = (await controls(page));
  const badge = facts.badges.find(item => item.id === '9801'), button = facts.buttons.find(item => item.id === '9801');
  expect([badge.glyph, badge.tooltip, badge.labelShown]).toEqual(['pause', 'Paused', false]);
  expect([button.glyph, button.label, button.tooltip, button.width]).toEqual(['play', 'Resume', 'Resume this download', 36]);
  expect(await hoverTooltip(page, `${rowSelector} .badge`)).toBe('Paused');
});

test('a compact action keeps its glyph and square while pending, and its label says so', async ({page}) => {
  let items = BASE;
  await open(page, () => items);
  await resize(page, 1000);
  let release;
  const held = new Promise(resolve => { release = resolve; });
  await page.route(url => /\/api\/torrents\/9801\/pause$/.test(url.pathname), async route => {
    await held;
    route.fulfill({status: 200, contentType: 'application/json', body: '{"ok":true}'});
  });
  const button = page.locator('#dash-tbody tr[data-torrent-id="9801"] .actions .btn');
  await button.click();
  await expect(button).toHaveAttribute('aria-busy', 'true');
  const pending = await button.evaluate(node => ({width: node.getBoundingClientRect().width,
    glyph: !!node.querySelector('.dp-row-action-icon'), label: node.querySelector('[data-dp-icon-label]')?.textContent}));
  expect(pending).toEqual({width: 36, glyph: true, label: 'Pausing…'});
  release();
  await expect.poll(() => page.locator('#dash-tbody tr[data-torrent-id="9801"] .actions .btn [data-dp-icon-label]').first().textContent())
    .toBe('Pause');
});

/* Status's heading and each badge share the column's centre line, in full and
 * compact; where the controls are compact, Date Added steps aside entirely
 * (heading, cells and track) and Progress takes the room, Name resting at its
 * 280px floor. Wider, Date Added and its date menu are as before. */
test('Status is centred, and the compact table sets Date Added aside and gives Progress the room', async ({page}) => {
  await open(page, () => BASE);
  const layout = () => page.evaluate(() => {
    const r = node => node.getBoundingClientRect();
    const table = document.querySelector('#view-dashboard .dash-activity-table-wrap .t-table');
    const heads = [...table.querySelectorAll('thead th')];
    const status = heads[1], centre = r(status).left + r(status).width / 2;
    const range = document.createRange();
    range.selectNodeContents(status);
    const text = range.getBoundingClientRect();
    const box = r(status).width - parseFloat(getComputedStyle(status).paddingLeft) - parseFloat(getComputedStyle(status).paddingRight);
    return {
      wrap: r(document.querySelector('#view-dashboard .dash-activity-table-wrap')).width,
      columns: heads.map(th => getComputedStyle(th).display === 'none' ? null : r(th).width),
      dateCells: [...new Set([...table.querySelectorAll('tbody tr[data-torrent-id] td:nth-child(5)')].map(td => getComputedStyle(td).display))],
      // The heading's offset from the centre line where its text fits the
      // column's content box; wider (a fallback font's STATUS on the compact
      // 35px box) text-align cannot centre it, so it is only kept in the cell.
      heading: text.width <= box ? text.left + text.width / 2 - centre : null,
      headingInCell: text.left >= r(status).left && text.right <= r(status).right,
      // Each badge that fits its cell's content box (in this file's fallback
      // font "Awaiting confirmation" is wider than Status's 152px and overflows).
      badges: [...table.querySelectorAll('tbody .badge')].filter(badge => {
        const cell = badge.closest('td'), style = getComputedStyle(cell);
        return r(badge).width <= r(cell).width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      }).map(badge => r(badge).left + r(badge).width / 2 - centre),
    };
  });
  await resize(page, 1300);
  const full = await layout();
  expect(full.wrap).toBeGreaterThan(987);
  expect(full.columns.every(width => width !== null)).toBe(true);
  expect(full.dateCells).toEqual(['table-cell']);
  expect(Math.abs(full.heading)).toBeLessThanOrEqual(1.5);
  expect(full.headingInCell).toBe(true);
  expect(full.badges.length).toBeGreaterThanOrEqual(4);
  full.badges.forEach(offset => expect(Math.abs(offset)).toBeLessThanOrEqual(1));
  // The date menu in its heading still opens.
  await page.locator('#view-dashboard .dash-activity-table-wrap th .dp-date-menu-trigger').click();
  await expect(page.locator('#view-dashboard .dash-activity-table-wrap th .dp-date-menu')).toBeVisible();
  await page.keyboard.press('Escape');

  await resize(page, 1000);
  const compact = await layout();
  expect(compact.wrap).toBeLessThanOrEqual(987);
  expect(compact.columns[4]).toBeNull();                              // Date Added: no heading, no track
  expect(compact.dateCells).toEqual(['none']);
  const [name, status, progress, size, , action] = compact.columns;
  expect(name + status + progress + size + action).toBeCloseTo(compact.wrap, 0);   // nothing reserved for it
  expect(name).toBeCloseTo(280, 0);
  expect(progress).toBeGreaterThan(full.columns[2]);                  // Progress, not Name, takes the room
  if (compact.heading !== null) expect(Math.abs(compact.heading)).toBeLessThanOrEqual(1.5);
  expect(compact.headingInCell).toBe(true);
  expect(compact.badges.length).toBe(5);
  compact.badges.forEach(offset => expect(Math.abs(offset)).toBeLessThanOrEqual(1));
});

/* Where the table is 756px or narrower (about an 874px window) Size leaves its
 * column -- heading, cells and track -- and the same formatted value closes the
 * name's metadata row, at its right end, in the name's own type, the row's
 * controls exactly where they were; Progress takes the room. Wider, the value
 * is not shown and Size keeps its column. */
test('a narrow table moves Size beside the name, in its type, and gives Progress the room', async ({page}) => {
  const items = [{...BASE[0], size_bytes: 999.9 * 1024 ** 2, route_provider_id: 'debridlink', route_provider_name: 'Debrid-Link',
    route_switch_available: true, access: {destroy: true}}, ...BASE.slice(1)];
  await open(page, () => items);
  const state = () => page.evaluate(() => {
    const r = node => node.getBoundingClientRect();
    const wrap = document.querySelector('#view-dashboard .dash-activity-table-wrap');
    const heads = [...wrap.querySelectorAll('thead th')];
    return {
      wrap: r(wrap).width,
      sizeColumn: getComputedStyle(heads[3]).display !== 'none',
      columns: heads.filter(th => getComputedStyle(th).display !== 'none').map(th => r(th).width),
      rows: [...document.querySelectorAll('#dash-tbody tr[data-torrent-id]')].map(tr => {
        const cell = tr.children[0], meta = cell.querySelector('.dp-transfer-provider-meta'), size = meta.querySelector('.dp-recent-size');
        const title = getComputedStyle(cell.querySelector('.t-name')), type = getComputedStyle(size);
        const shown = node => getComputedStyle(node).display !== 'none' && r(node).width > 0;
        const controls = [...meta.children].filter(node => node !== size && shown(node));
        return {
          id: tr.dataset.torrentId, value: size.textContent, cellValue: tr.children[3].textContent.trim(),
          sizes: [shown(size), shown(tr.children[3])].filter(Boolean).length,
          relocated: shown(size),
          rightGap: r(cell).right - parseFloat(getComputedStyle(cell).paddingRight) - r(size).right,
          clear: shown(size) ? r(size).left - Math.max(...controls.map(node => r(node).right)) : null,
          sameType: ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'color', 'letterSpacing'].every(key => title[key] === type[key]),
          controls: controls.map(node => [Math.round(r(node).left - r(cell).left), Math.round(r(node).width)]),
          metaHeight: r(meta).height,
        };
      }),
    };
  });
  let wide = null;
  for (let width = 890; width >= 860; width--) {
    await resize(page, width);
    const now = await state();
    if (now.wrap <= 756) {
      expect(wide && wide.wrap).toBeGreaterThan(756);
      // Wider: Size in its column, the relocated value not shown.
      expect(wide.sizeColumn).toBe(true);
      wide.rows.forEach(row => expect([row.sizes, row.relocated], row.id).toEqual([1, false]));
      // Narrower: no Size column, the value once, at the row's right end, in the name's type.
      expect(now.sizeColumn).toBe(false);
      expect(now.columns.length).toBe(4);
      expect(now.columns.reduce((sum, width) => sum + width, 0)).toBeCloseTo(now.wrap, 0);
      now.rows.forEach((row, index) => {
        expect([row.sizes, row.relocated, row.sameType], row.id).toEqual([1, true, true]);
        expect(row.value, row.id).toBe(row.cellValue);                       // the same formatted value
        expect(Math.abs(row.rightGap), row.id).toBeLessThanOrEqual(0.5);
        expect(row.clear, row.id).toBeGreaterThan(0);                        // clear of every control
        expect(row.controls, row.id).toEqual(wide.rows[index].controls);    // the controls where they were
        expect(row.metaHeight, row.id).toBeCloseTo(wide.rows[index].metaHeight, 1);
      });
      expect(now.columns[2]).toBeGreaterThan(wide.columns[2]);               // Progress takes the room
      // The Debrid-Link route selector is still there and still opens its picker (which reads its providers).
      await page.route(url => url.pathname === '/api/torrents/9801/route', route => route.fulfill({status: 200,
        contentType: 'application/json', body: JSON.stringify({transfer_id: 9801, current_provider_id: 'debridlink',
          current_provider_name: 'Debrid-Link', switchable: true, providers: [
            {provider_id: 'debridlink', provider_name: 'Debrid-Link', status: 'current', selectable: false, reason: null, readiness: null},
            {provider_id: 'alldebrid', provider_name: 'AllDebrid', status: 'available', selectable: true, reason: null, readiness: null}]})}));
      const launcher = page.locator('#dash-tbody tr[data-torrent-id="9801"] .dp-root-provider-launcher');
      await expect(launcher).toContainText('Debrid-Link');
      await launcher.click();
      await expect(page.locator('.dp-root-provider-menu')).toBeVisible();
      await expect(launcher).toHaveAttribute('aria-expanded', 'true');
      return;
    }
    wide = now;
  }
  throw new Error('no window between 890 and 860px gave a 756px table');
});

test('a badge whose label is shown passes the hover to a described element around it', async ({page}) => {
  await open(page, () => BASE);
  await resize(page, 1300);
  await page.evaluate(() => {
    const host = document.createElement('div');
    host.id = 'described-host';
    host.setAttribute('data-tooltip', 'The enclosing description');
    host.style.cssText = 'position:fixed;left:40px;top:40px;z-index:9999;padding:8px;background:#222';
    host.innerHTML = window.DPIcons.statusBadge('completed', 'Completed', '');
    document.body.appendChild(host);
  });
  expect(await hoverTooltip(page, '#described-host .badge')).toBe('The enclosing description');
});
