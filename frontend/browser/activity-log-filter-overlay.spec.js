const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Activity Log filters: one control beside Search. With the three
 * defaults applied (Available history, All, All) it reads Filter Results and
 * opens a panel of three single-choice columns -- Time Window, Severity, Event
 * Type -- whose choices are a draft until Apply commits them together; Cancel,
 * Escape or a press outside discards them. With any other combination applied
 * the same control reads Clear Filters, names what is applied in its DP tooltip
 * and description, and one press restores the defaults, leaving Search alone.
 * The journal is answered here (GET /events); nothing is written to the backend. */

// The contract's options, in order, with their wire values (independent of the page's markup).
const OPTIONS = {
  timeframe: [['all', 'Available history'], ['1h', 'Last hour'], ['12h', 'Last 12 hours'], ['24h', 'Last day'],
    ['72h', 'Last 3 days'], ['7d', 'Last week'], ['30d', 'Last 30 days']],
  level: [['', 'All'], ['info', 'Info'], ['warning', 'Warning'], ['error', 'Error']],
  category: [['', 'All'], ['transfer', 'Transfers'], ['routing', 'Routing'], ['resource', 'Provider Resources'],
    ['selection', 'File Selection'], ['consolidation', 'Consolidation'], ['execution', 'Downloads'], ['recovery', 'Recovery'],
    ['storage', 'Storage'], ['extraction', 'Extraction'], ['input', 'Input Required'], ['integration', 'Integrations'],
    ['configuration', 'Configuration'], ['administration', 'Administration']],
};
const LEGENDS = ['Time Window', 'Severity', 'Event Type'];

// A journal of 260 events, answered page by page within a snapshot; each row
// says which filters answered it, so a stale answer would show.
async function open(page, {width = 1440, height = 900, hold} = {}) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route('**/api/events/stream', route => route.abort());
  const requests = [];
  await page.route('**/api/events?**', async route => {
    const q = Object.fromEntries(new URL(route.request().url()).searchParams);
    requests.push(q);
    if (hold) await hold(q);
    const limit = Number(q.limit) || 100, snapshot = q.snapshot ? Number(q.snapshot) : 260;
    const top = q.before ? Number(q.before) - 1 : snapshot;
    const tag = `${q.timeframe}/${q.level || '-'}/${q.category || '-'}`;
    const items = [];
    for (let id = top; id > 0 && items.length < limit; id--) {
      items.push({id, severity: 'info', category: 'transfer', type: 'transfer.synthetic', message: `Event ${id} [${tag}]`,
        name: `fixture-${id}`, transfer_id: id, occurred_at: '2026-10-09T12:00:00Z'});
    }
    const last = items.at(-1)?.id ?? 0;
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items, limit, snapshot,
      has_more: last > 1, next_before: last > 1 ? last : null, more_after_next: last - 1 > limit, newer_available: false,
      window_start: null, search: {text: 'none', pending: 0, complete: true}})});
  });
  await page.setViewportSize({width, height});
  await page.goto('/');
  await page.evaluate(() => { settingsData.activity_log_page_size = 100; });
  await page.evaluate(() => nav(document.querySelector('#sidebar .nav-item[data-view="events"]')));   // the app's navigation (the sidebar is a drawer on a phone)
  await expect(page.locator('#event-list .dp-activity-row').first()).toHaveText(/Event 260 \[all\/-\/-\]/);
  return requests;
}

const trigger = page => page.locator('#ev-filter');
const panel = page => page.locator('#ev-filter-panel');
const choose = (page, label, column) => panel(page).locator(`fieldset[data-filter="${column}"] label`, {hasText: new RegExp(`^${label}$`)}).click();
const shown = page => page.evaluate(() => Boolean(document.getElementById('ev-filter-panel')?.matches(':popover-open')));
// Each column's checked row: [value, label].
const checked = page => page.evaluate(() => Object.fromEntries([...document.querySelectorAll('#ev-filter-panel fieldset')].map(group => {
  const rows = [...group.querySelectorAll('input[type="radio"]')].filter(radio => radio.checked);
  return [group.dataset.filter, rows.map(radio => [radio.value, radio.closest('label').textContent.trim()])];
})));
// The trigger as assistive technology and the pointer meet it.
const triggerState = page => page.evaluate(() => {
  const button = document.getElementById('ev-filter');
  const described = (button.getAttribute('aria-describedby') || '').split(/\s+/).filter(id => id && id !== 'dp-tooltip')
    .map(id => document.getElementById(id)?.textContent.trim() || '').join(' ');
  const glyph = [...button.querySelectorAll('svg')].filter(svg => getComputedStyle(svg).display !== 'none').map(svg => svg.dataset.dpLucide);
  return {text: button.textContent.trim(), glyph, described, tooltip: button.getAttribute('data-tooltip'), title: button.getAttribute('title')};
});
const DEFAULT_TRIGGER = {text: 'Filter Results', glyph: ['funnel'], described: '', tooltip: null, title: null};
const cleared = described => ({text: 'Clear Filters', glyph: ['funnel-x'], described, tooltip: `Clear filters — ${described}`, title: null});

async function apply(page, picks) {
  await trigger(page).click();
  await expect.poll(() => shown(page)).toBe(true);
  for (const [column, label] of picks) await choose(page, label, column);
  await panel(page).getByRole('button', {name: 'Apply', exact: true}).click();
  await expect.poll(() => shown(page)).toBe(false);
}

test('Filter Results sits at the far right of a full-width Search; its panel shows every choice in order, the applied defaults selected', async ({page}) => {
  await open(page);
  // The three dropdowns, their projected shells and Reset are gone; one control remains beside Search.
  const band = await page.evaluate(() => {
    const row = document.querySelector('#view-events .dp-activity-search-row'), r = node => node.getBoundingClientRect();
    const search = r(document.getElementById('ev-search')), button = r(document.getElementById('ev-filter')), box = r(row);
    return {old: ['ev-timeframe', 'ev-level', 'ev-category', 'ev-reset'].filter(id => document.getElementById(id)).length,
      shells: document.querySelectorAll('#view-events .dp-dropdown-shell, #view-events select').length,
      shown: [...row.children].filter(node => getComputedStyle(node).display !== 'none').map(node => node.id),
      searchWide: search.width > box.width * 0.7, beside: search.right <= button.left,
      farRight: Math.abs(button.right - box.right) < 1, centred: Math.abs((search.top + search.bottom) / 2 - (button.top + button.bottom) / 2) < 1};
  });
  expect(band).toEqual({old: 0, shells: 0, shown: ['ev-search', 'ev-filter'], searchWide: true, beside: true, farRight: true, centred: true});
  expect(await triggerState(page)).toEqual(DEFAULT_TRIGGER);
  await expect(trigger(page)).toHaveAccessibleName('Filter Results');
  await expect(trigger(page)).toHaveAttribute('aria-expanded', 'false');

  await trigger(page).click();
  await expect.poll(() => shown(page)).toBe(true);
  await expect(trigger(page)).toHaveAttribute('aria-expanded', 'true');
  await expect(panel(page)).toHaveAttribute('role', 'dialog');
  await expect(panel(page)).toHaveAccessibleName('Filter results');
  const columns = await page.evaluate(() => [...document.querySelectorAll('#ev-filter-panel fieldset')].map(group => ({
    legend: group.querySelector('legend').textContent.trim(),
    options: [...group.querySelectorAll('input[type="radio"]')].map(radio => [radio.value, radio.closest('label').textContent.trim()]),
  })));
  expect(columns).toEqual([{legend: LEGENDS[0], options: OPTIONS.timeframe}, {legend: LEGENDS[1], options: OPTIONS.level},
    {legend: LEGENDS[2], options: OPTIONS.category}]);
  expect(await checked(page)).toEqual({timeframe: [OPTIONS.timeframe[0]], level: [OPTIONS.level[0]], category: [OPTIONS.category[0]]});
  // The whole row of the chosen option is marked, not only its text; and focus is on the first column's choice.
  const marks = await page.evaluate(() => [...document.querySelectorAll('#ev-filter-panel fieldset')].map(group => {
    const [first, second] = group.querySelectorAll('label'), style = node => getComputedStyle(node);
    return {differs: style(first).backgroundColor !== style(second).backgroundColor, check: getComputedStyle(first, '::before').content,
      full: Math.abs(first.getBoundingClientRect().width - group.getBoundingClientRect().width) < 1};
  }));
  expect(marks).toEqual(Array(3).fill({differs: true, check: '"✓"', full: true}));
  await expect(panel(page).locator('input[value="all"]')).toBeFocused();
  // The panel lies inside the window with Cancel left of a green Apply.
  const fit = await page.evaluate(() => {
    const p = document.getElementById('ev-filter-panel').getBoundingClientRect(), r = s => document.querySelector(s).getBoundingClientRect();
    const cancel = r('#ev-filter-panel [data-filter-action="cancel"]'), apply = r('#ev-filter-panel [data-filter-action="apply"]');
    return {inside: p.left >= 0 && p.top >= 0 && p.right <= innerWidth && p.bottom <= innerHeight,
      order: cancel.right < apply.left, green: getComputedStyle(document.querySelector('#ev-filter-panel [data-filter-action="apply"]')).color};
  });
  expect(fit.inside && fit.order).toBe(true);
  expect(fit.green).toBe(await page.evaluate(() => getComputedStyle(document.body).getPropertyValue('--dp-state-success').trim() ?
    getComputedStyle(Object.assign(document.body.appendChild(document.createElement('span')), {style: 'color: var(--dp-state-success)'})).color : ''));
});

test('choices are a draft: one per column, no request, and Cancel, Escape or a press outside leave everything as applied', async ({page}) => {
  const requests = await open(page);
  await page.locator('#ev-search').fill('needle');
  await expect.poll(() => requests.at(-1)?.search).toBe('needle');
  const before = requests.length, list = await page.locator('#event-list').innerText();
  for (const close of ['cancel', 'escape', 'outside']) {
    await trigger(page).click();
    await choose(page, 'Last hour', 'timeframe');
    await choose(page, 'Last day', 'timeframe');               // replaces Last hour in its column
    await choose(page, 'Error', 'level');
    await choose(page, 'Storage', 'category');
    expect(await checked(page)).toEqual({timeframe: [['24h', 'Last day']], level: [['error', 'Error']], category: [['storage', 'Storage']]});
    expect(await triggerState(page)).toEqual(DEFAULT_TRIGGER);  // a draft is never described as applied
    if (close === 'cancel') await panel(page).getByRole('button', {name: 'Cancel', exact: true}).click();
    else if (close === 'escape') await page.keyboard.press('Escape');
    else await page.mouse.click(400, 140);
    await expect.poll(() => shown(page)).toBe(false);
    if (close !== 'outside') await expect(trigger(page)).toBeFocused();
    expect(requests.length, close).toBe(before);
    expect(await page.locator('#event-list').innerText()).toBe(list);
    await expect(page.locator('#ev-search')).toHaveValue('needle');
    expect(await triggerState(page)).toEqual(DEFAULT_TRIGGER);
    await trigger(page).click();                                  // reopening shows the applied defaults, not the draft
    expect(await checked(page)).toEqual({timeframe: [OPTIONS.timeframe[0]], level: [OPTIONS.level[0]], category: [OPTIONS.category[0]]});
    await page.keyboard.press('Escape');
  }
  // Apply with the applied values chosen again closes without a request.
  await trigger(page).click();
  await panel(page).getByRole('button', {name: 'Apply', exact: true}).click();
  await expect.poll(() => shown(page)).toBe(false);
  expect(requests.length).toBe(before);
});

test('Apply commits the three at once as one new investigation; Clear Filters names what is applied and restores the defaults, keeping Search', async ({page}) => {
  const requests = await open(page);
  const pager = page.locator('#dp-activity-page-btns');
  await page.locator('#ev-search').fill('needle');
  await expect.poll(() => requests.at(-1)?.search).toBe('needle');
  await pager.locator('[aria-label="Next page"]').click();             // an older page of the frozen snapshot
  await expect(pager.locator('.dp-pager-current')).toHaveText('2');
  expect(requests.at(-1)).toMatchObject({snapshot: '260', before: '161'});

  let before = requests.length;
  await apply(page, [['timeframe', 'Last hour'], ['level', 'Error']]);
  await expect.poll(() => requests.length).toBe(before + 1);
  expect(requests.at(-1)).toEqual({limit: '100', timeframe: '1h', level: 'error', search: 'needle'});   // new investigation: no snapshot or cursor
  await expect(pager.locator('.dp-pager-current')).toHaveText('1');
  await expect(page.locator('#event-list .dp-activity-row').first()).toHaveText(/Event 260 \[1h\/error\/-\]/);
  expect(await triggerState(page)).toEqual(cleared('Time Window: Last hour · Severity: Error'));
  await expect(trigger(page)).toHaveAccessibleName('Clear Filters');
  await expect(trigger(page)).toHaveAccessibleDescription('Time Window: Last hour · Severity: Error');
  await trigger(page).hover();
  await expect(page.locator('#dp-tooltip')).toHaveText('Clear filters — Time Window: Last hour · Severity: Error');
  await page.mouse.move(5, 5);
  await page.waitForTimeout(500);                                     // nothing re-renders the description away
  expect(await triggerState(page)).toEqual(cleared('Time Window: Last hour · Severity: Error'));

  // Clear: the defaults at once, one request, Search and its accepted query kept; the control opens the panel again.
  before = requests.length;
  await trigger(page).click();
  await expect.poll(() => requests.length).toBe(before + 1);
  expect(requests.at(-1)).toEqual({limit: '100', timeframe: 'all', search: 'needle'});
  await expect(page.locator('#ev-search')).toHaveValue('needle');
  expect(await shown(page)).toBe(false);
  expect(await triggerState(page)).toEqual(DEFAULT_TRIGGER);
  await expect(trigger(page)).toHaveAccessibleDescription('');
  await expect(trigger(page)).toHaveAttribute('aria-expanded', 'false');

  // One column, then all three: only what differs from the defaults is named, in column order.
  await apply(page, [['category', 'Downloads']]);
  expect(requests.at(-1)).toEqual({limit: '100', timeframe: 'all', category: 'execution', search: 'needle'});
  expect(await triggerState(page)).toEqual(cleared('Event Type: Downloads'));
  await trigger(page).click();
  await apply(page, [['category', 'Provider Resources'], ['level', 'Warning'], ['timeframe', 'Last week']]);
  expect(requests.at(-1)).toEqual({limit: '100', timeframe: '7d', level: 'warning', category: 'resource', search: 'needle'});
  expect(await triggerState(page)).toEqual(cleared('Time Window: Last week · Severity: Warning · Event Type: Provider Resources'));
  await page.locator('.dp-activity-refresh').click();                 // a refresh keeps both the filters and their description
  await expect.poll(() => requests.at(-1)).toEqual({limit: '100', timeframe: '7d', level: 'warning', category: 'resource', search: 'needle'});
  expect(await triggerState(page)).toEqual(cleared('Time Window: Last week · Severity: Warning · Event Type: Provider Resources'));

  // Search alone never turns the control into Clear Filters.
  await trigger(page).click();
  await page.locator('#ev-search').fill('other');
  await expect.poll(() => requests.at(-1)?.search).toBe('other');
  expect(await triggerState(page)).toEqual(DEFAULT_TRIGGER);
});

test('Auto Refresh, Refresh and a newer choice use only the applied filters; a stale answer never replaces a newer one', async ({page}) => {
  let release = null;
  const requests = await open(page, {hold: q => q.level === 'warning' ? new Promise(resolve => { release = resolve; }) : null});
  // The live stream connected: a journal hint is followed with an ordinary request (Auto Refresh is on by default).
  await page.evaluate(() => { document.documentElement.dataset.dpPulse = 'connected'; document.dispatchEvent(new CustomEvent('debridpulse:pulse-connected')); });
  const hint = latest => page.evaluate(latest => document.dispatchEvent(new CustomEvent('debridpulse:activity-journal-changed', {detail: {latest}})), latest);

  // Applied Error: the hint and Refresh ask with it.
  await apply(page, [['level', 'Error']]);
  let from = requests.length;
  await hint(900);
  await page.evaluate(() => DPActivityLog.load());
  await expect.poll(() => requests.length).toBeGreaterThanOrEqual(from + 2);
  expect(requests.slice(from).every(q => q.level === 'error')).toBe(true);

  // Defaults applied and a draft open (Info, Storage): the hint and Refresh still ask with the applied defaults.
  await trigger(page).click();                                         // Clear Filters
  await expect.poll(() => requests.at(-1)?.level).toBeUndefined();
  await trigger(page).click();                                         // Filter Results: the panel
  await choose(page, 'Info', 'level');
  await choose(page, 'Storage', 'category');
  from = requests.length;
  await hint(901);
  await page.evaluate(() => DPActivityLog.load());
  await expect.poll(() => requests.length).toBeGreaterThanOrEqual(from + 2);
  expect(requests.slice(from).every(q => q.level === undefined && q.category === undefined)).toBe(true);
  expect(await shown(page)).toBe(true);                                // the draft is untouched, still open
  expect(await checked(page)).toEqual({timeframe: [OPTIONS.timeframe[0]], level: [['info', 'Info']], category: [['storage', 'Storage']]});
  await page.keyboard.press('Escape');

  // A slower answer for Warning arrives after Clear's newer answer: it is never shown.
  await apply(page, [['level', 'Warning']]);
  await expect.poll(() => typeof release).toBe('function');
  await trigger(page).click();                                         // Clear while Warning is still in flight
  await expect(page.locator('#event-list .dp-activity-row').first()).toHaveText(/\[all\/-\/-\]/);
  release();
  await page.waitForTimeout(400);
  await expect(page.locator('#event-list .dp-activity-row').first()).toHaveText(/\[all\/-\/-\]/);
  expect(await triggerState(page)).toEqual(DEFAULT_TRIGGER);
});

test('one footprint for both functions: Filter Results and Clear Filters have the same size, and Search never moves', async ({page}) => {
  await open(page);
  const read = () => page.evaluate(() => {
    const r = id => { const b = document.getElementById(id).getBoundingClientRect(); return {left: b.left, top: b.top, width: b.width, height: b.height}; };
    const row = document.querySelector('#view-events .dp-activity-search-row').getBoundingClientRect(), button = document.getElementById('ev-filter');
    return {search: r('ev-search'), button: r('ev-filter'), farRight: Math.abs(row.right - button.getBoundingClientRect().right) < 1,
      fits: button.scrollWidth <= button.clientWidth, text: button.textContent.trim()};
  });
  for (const width of [1440, 780]) {
    await page.setViewportSize({width, height: 900});
    await page.waitForFunction(() => document.getElementById('sidebar').getAnimations().every(animation => animation.playState !== 'running'));
    const filter = await read();
    await apply(page, [['level', 'Error']]);
    const clear = await read();
    await trigger(page).click();
    const again = await read();
    expect(filter.text).toBe('Filter Results');
    expect(clear.text).toBe('Clear Filters');
    expect(clear.button, `${width}px`).toEqual(filter.button);
    expect(clear.search, `${width}px`).toEqual(filter.search);
    expect(again).toEqual(filter);
    for (const state of [filter, clear]) {
      expect(state.farRight && state.fits).toBe(true);
      expect(Math.abs((state.search.top + state.search.height / 2) - (state.button.top + state.button.height / 2))).toBeLessThan(1);
    }
  }
});

test('keyboard: the panel opens on its choice, each column is one group, Enter or Space chooses, Escape returns to the control', async ({page}) => {
  const requests = await open(page);
  await page.locator('#ev-search').focus();
  await page.keyboard.press('Tab');
  await expect(trigger(page)).toBeFocused();                           // no hidden select or projected shell in between
  await page.keyboard.press('Enter');
  await expect.poll(() => shown(page)).toBe(true);
  await expect(panel(page).locator('input[value="all"]')).toBeFocused();
  await expect(panel(page).getByRole('radiogroup')).toHaveCount(0);   // native fieldset groups (role group), named by their legends
  for (const name of LEGENDS) await expect(panel(page).getByRole('group', {name})).toHaveCount(1);
  await page.keyboard.press('ArrowDown');                              // Time Window: Last hour
  await page.keyboard.press('Tab');                                    // Severity: its chosen row
  await expect(panel(page).locator('input[name="ev-filter-level"][value=""]')).toBeFocused();
  await page.keyboard.press('ArrowDown');                              // Info
  await page.keyboard.press('ArrowDown');                              // Warning
  await page.keyboard.press('Tab');                                    // Event Type
  await page.keyboard.press('ArrowDown');                              // Transfers
  await page.keyboard.press('ArrowDown');                              // Routing
  // Enter and Space choose the focused row too.
  await panel(page).locator('input[name="ev-filter-category"][value="selection"]').focus();
  await page.keyboard.press('Enter');
  await panel(page).locator('input[name="ev-filter-level"][value="error"]').focus();
  await page.keyboard.press('Space');
  expect(await checked(page)).toEqual({timeframe: [['1h', 'Last hour']], level: [['error', 'Error']], category: [['selection', 'File Selection']]});
  await panel(page).locator('input[name="ev-filter-category"]:checked').focus();
  await page.keyboard.press('Tab');
  await expect(panel(page).getByRole('button', {name: 'Cancel', exact: true})).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(panel(page).getByRole('button', {name: 'Apply', exact: true})).toBeFocused();
  const before = requests.length;
  await page.keyboard.press('Enter');
  await expect.poll(() => shown(page)).toBe(false);
  await expect(trigger(page)).toBeFocused();
  await expect.poll(() => requests.length).toBe(before + 1);
  expect(requests.at(-1)).toEqual({limit: '100', timeframe: '1h', level: 'error', category: 'selection'});
  await expect(trigger(page)).toHaveAccessibleName('Clear Filters');
  await page.keyboard.press('Enter');                                  // Clear Filters, by keyboard
  await expect.poll(() => requests.at(-1)).toEqual({limit: '100', timeframe: 'all'});
  await expect(trigger(page)).toBeFocused();
  await page.keyboard.press('Space');                                  // Filter Results again: opens
  await expect.poll(() => shown(page)).toBe(true);
  await page.keyboard.press('Escape');
  await expect.poll(() => shown(page)).toBe(false);
  await expect(trigger(page)).toBeFocused();
});

test('narrow (390px): Search and the control stay one row; the panel stacks its columns inside the window, Apply in reach; leaving the page discards a draft', async ({page}) => {
  const requests = await open(page, {width: 390, height: 760});
  const row = await page.evaluate(() => {
    const r = id => document.getElementById(id).getBoundingClientRect(), search = r('ev-search'), button = r('ev-filter');
    return {oneRow: Math.abs((search.top + search.bottom) / 2 - (button.top + button.bottom) / 2) < 1 && search.right <= button.left, overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth};
  });
  expect(row).toEqual({oneRow: true, overflow: 0});
  await trigger(page).click();
  const fit = await page.evaluate(() => {
    const p = document.getElementById('ev-filter-panel'), box = p.getBoundingClientRect(), groups = [...p.querySelectorAll('fieldset')].map(g => g.getBoundingClientRect());
    const apply = p.querySelector('[data-filter-action="apply"]').getBoundingClientRect();
    return {inside: box.left >= 0 && box.right <= innerWidth && box.top >= 0 && box.bottom <= innerHeight,
      stacked: groups[1].top >= groups[0].bottom && groups[2].top >= groups[1].bottom,
      whole: [...p.querySelectorAll('label')].every(label => label.scrollWidth <= label.clientWidth + 1),
      apply: apply.bottom <= innerHeight && apply.top >= box.top};
  });
  expect(fit).toEqual({inside: true, stacked: true, whole: true, apply: true});
  await choose(page, 'Error', 'level');
  const before = requests.length;
  await page.evaluate(() => nav(document.querySelector('[data-view="dashboard"]')));
  await expect.poll(() => shown(page)).toBe(false);
  await page.evaluate(() => nav(document.querySelector('[data-view="events"]')));
  await expect.poll(() => requests.length).toBeGreaterThan(before);
  expect(requests.at(-1).level).toBeUndefined();
  expect(await triggerState(page)).toEqual(DEFAULT_TRIGGER);
});

test('a User without administration has no Activity Log, filter control or panel', async ({page}) => {
  // The server's own authority for this browser, without administration, in the bundle and the session.
  const user = authority => ({...authority, role: 'user', capabilities: authority.capabilities.filter(capability => capability !== 'administer')});
  await page.route(url => url.pathname === '/app.js', async route => {
    const body = await (await route.fetch()).text();
    const marker = 'window.debridPulseAuthority=Object.freeze(';
    const [head, rest] = body.split(marker);
    const end = rest.indexOf(');');
    return route.fulfill({status: 200, contentType: 'application/javascript', body: head + marker + JSON.stringify(user(JSON.parse(rest.slice(0, end)))) + rest.slice(end)});
  });
  await page.route('**/api/auth/session', async route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(user(await (await route.fetch()).json()))}));
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  await page.waitForFunction(() => Boolean(window.DPActivityLog));
  expect(await page.evaluate(() => ({view: document.querySelectorAll('#view-events').length, filter: document.querySelectorAll('#ev-filter').length,
    panel: document.querySelectorAll('#ev-filter-panel').length, nav: document.querySelectorAll('#sidebar .nav-item[data-view="events"]').length}))).toEqual({view: 0, filter: 0, panel: 0, nav: 0});
});
