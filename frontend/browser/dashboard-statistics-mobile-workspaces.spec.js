const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Dashboard KPI hierarchy, Statistics recomposition and the four
 * handheld master workspaces.
 * - Dashboard: from 780px every KPI card is its icon at the left and, beside
 *   it, its title over its value (and any secondary line), the icon where it
 *   always was; below 780px its title centred over its icon and value. The
 *   card keeps its 130px height, and the tallest heartbeat on the ECG line
 *   below (R at y=-2.51, with its 3px glow) stays clear of the text wherever
 *   the title is one line.
 * - Statistics: the title and hint each show the longest of their three
 *   wordings that is drawn whole; from 780px the six period filters, below it
 *   one header control (clock, period, chevron) of one width whichever period
 *   is chosen, whose menu chooses through the filters' own state; exactly two
 *   period cards, side by side at every width (label and value scaled down on
 *   a narrow card, each whole on one line), the data card labelled Data
 *   Transferred below 780px only.
 * - Below 780px Statistics, Activity Log, Settings and Help each fill the
 *   shell's content box -- 10px under the header, 10px above the dock -- with
 *   their header fixed and one inner scroller; the page itself does not
 *   scroll.
 * Engine: Chromium (Playwright), CSS viewport sizes; fonts as the runtime
 * serves them (DP_WEB_FONTS=1 for the web faces, else the fallback face).
 * Runtime facts are routed: nothing depends on a live transfer. */

const WEB_FONTS = process.env.DP_WEB_FONTS === '1';
const PERIODS = [['1h', '1Hour'], ['24h', '1Day'], ['7d', '7Days'], ['30d', '30Days'], ['1y', '1Year'], ['all', 'All Time']];

function event(id) {
  return {id, occurred_at: new Date(Date.UTC(2026, 9, 30, 12) - id * 60000).toISOString(), category: 'transfer', type: 'transfer.completed',
    severity: 'info', outcome: null, subject_kind: 'transfer', subject_id: String(id), transfer_id: id, related_transfer_id: null,
    transfer_available: false, integration_id: null, name: `Synthetic.Transfer.${id}.mkv`, message: 'Transfer completed', detail: null,
    error_category: null, error_domain: null};
}

async function boot(page, {width = 1440, height = 900, stats = {}} = {}) {
  if (!WEB_FONTS) await page.route(/fonts\.(googleapis|gstatic)\.com/, route => route.abort());
  await page.setViewportSize({width, height});
  const detail = [];
  await page.route(url => url.pathname === '/api/stats', async route => {
    const response = await route.fetch();
    route.fulfill({response, json: {...(await response.json()), by_status: {completed: 12345, downloading: 3, processing: 2, error: 1},
      completed_count: 12345, error_count: 1, active_operations: 3, total_completed_bytes: 7.9 * 1024 ** 4, total_blocked_files: 3, ...stats}});
  });
  await page.route(url => url.pathname === '/api/stats/detail', route => {
    const period = new URL(route.request().url()).searchParams.get('period');
    detail.push(period);
    route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({period,
      totals: {torrent_total: 412, completed_count: 380, partial_total: 9, success_rate_pct: 96.4, completed_size: 679.9 * 1024 ** 3},
      torrent_status: [{status: 'completed', count: 380}], file_status: [{status: 'completed', count: 2210}],
      event_levels: [{level: 'info', count: 900}], transfer_methods: [{method: 'Debrid', count: 300}],
      daily_completions: [{date: '2026-10-29', count: 4, premium: 3, network: 1}]})});
  });
  await page.route(url => url.pathname === '/api/events', route => {
    const limit = Number(new URL(route.request().url()).searchParams.get('limit')) || 100;
    route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items: Array.from({length: Math.min(limit, 80)}, (_, i) => event(i + 1)),
      limit, snapshot: 80, has_more: false, next_before: null, more_after_next: false, newer_available: false, window_start: null,
      history_started_at: '2026-10-01T00:00:00Z'})});
  });
  await page.goto('/');
  await page.waitForFunction(() => window.DPStatisticsLifecycle && document.getElementById('s-total').textContent !== '—');
  await page.evaluate(() => document.fonts.ready);
  return detail;
}

const settle = page => page.evaluate(() => new Promise(resolve => {
  const still = () => !document.querySelector('.dp-motion-surface') && document.getAnimations()
    .every(animation => animation.playState !== 'running' || animation.effect?.getTiming().iterations === Infinity);
  const wait = () => (still() ? requestAnimationFrame(() => requestAnimationFrame(resolve)) : setTimeout(wait, 30));
  wait();
}));

async function show(page, view) {
  await page.evaluate(view => nav(document.querySelector(`#sidebar .nav-item[data-view="${view}"]:not([data-title])`)), view);
  await expect(page.locator(`#view-${view}`)).toHaveClass(/\bactive\b/);
  await settle(page);
}

/* Each shown KPI card: its title, group (icon, value as drawn, secondary
 * line) and ECG line box, and where the tallest heartbeat reaches. */
const kpis = page => page.evaluate(() => {
  const drawnBox = node => {
    if (node.dataset.compact) return node.getBoundingClientRect();
    const range = document.createRange(); range.selectNodeContents(node); return range.getBoundingClientRect();
  };
  return [...document.querySelectorAll('#view-dashboard .dash-hero-stat')].filter(card => card.offsetWidth).map(card => {
    const label = card.querySelector('.dhs-label'), sub = card.querySelector('.dhs-sub');
    const range = document.createRange(); range.selectNodeContents(label);
    const title = range.getBoundingClientRect();
    const icon = card.querySelector('.dhs-icon').getBoundingClientRect(), value = drawnBox(card.querySelector('.dhs-val'));
    const parts = [icon, value, ...(sub && sub.textContent.trim() ? [drawnBox(sub)] : [])];
    const group = {left: Math.min(...parts.map(r => r.left)), right: Math.max(...parts.map(r => r.right)),
      top: Math.min(...parts.map(r => r.top)), bottom: Math.max(...parts.map(r => r.bottom))};
    const box = card.getBoundingClientRect(), style = getComputedStyle(card);
    const svg = card.querySelector('.dp-card-spark'), line = svg.getBoundingClientRect(), view = Number(svg.getAttribute('viewBox').split(' ')[3]);
    return {title: label.textContent, lines: new Set([...range.getClientRects()].map(r => Math.round(r.top))).size,
      titleBox: {left: title.left, right: title.right, top: title.top, bottom: title.bottom}, group, height: box.height,
      icon: {left: icon.left - box.left, top: icon.top - box.top, right: icon.right}, value: {left: value.left, top: value.top},
      inner: {left: box.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft), right: box.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight)},
      edge: {left: box.left + parseFloat(style.borderLeftWidth), right: box.right - parseFloat(style.borderRightWidth)},
      peak: line.top - 2.51 * line.height / view, lineBottom: line.bottom, cardBottom: box.bottom, mode: card.dataset.dpKpi ?? null};
  });
});

test('every KPI card from 780px is its icon beside its title over its value, below it its title over its group; 130px tall, the tallest heartbeat clear of the text', async ({page}) => {
  await boot(page, {stats: {paused: true, by_status: {completed: 12345, downloading: 3, processing: 2, error: 1, paused: 2}}});
  for (const [width, titles] of [
    [1440, ['Total Downloads', 'Completed', 'Active Now', 'Processing', 'Errors', 'Total Downloaded']],
    [1300, ['Total Downloads', 'Completed', 'Total Downloaded']],
    [780, ['Total Downloads', 'Completed', 'Total Downloaded']],
    [390, ['Total Downloads', 'Completed']],
    [320, ['Total Downloads', 'Completed']]]) {
    await page.setViewportSize({width, height: 900});
    await show(page, 'dashboard');
    const cards = await kpis(page);
    expect(cards.map(card => card.title), `${width}px`).toEqual(titles);
    for (const card of cards) {
      const at = `${width}px ${card.title}`;
      expect(card.height, `${at}: the card keeps its height`).toBeCloseTo(130, 1);
      expect(card.mode, `${at}: one composition`).toBe(null);
      expect(card.titleBox.bottom, `${at}: the title above the value`).toBeLessThanOrEqual(card.value.top);
      if (width >= 780) {
        // The icon where it always was (the card's padding and its 1px), the text beside it, title and value aligned.
        expect([card.icon.left, card.icon.top], `${at}: the icon at the left`).toEqual([18, 22]);
        expect(card.titleBox.left, `${at}: the title beside the icon`).toBeGreaterThanOrEqual(card.icon.right);
        expect(Math.abs(card.titleBox.left - card.value.left), `${at}: title and value aligned`).toBeLessThanOrEqual(1);
      } else {
        expect(card.titleBox.bottom, `${at}: the title above the group`).toBeLessThanOrEqual(card.group.top);
        expect(Math.abs((card.titleBox.left + card.titleBox.right) / 2 - (card.group.left + card.group.right) / 2), `${at}: the title centred on the group`).toBeLessThanOrEqual(1);
      }
      expect(card.group.left >= card.inner.left - 0.5 && card.group.right <= card.inner.right + 0.5, `${at}: the group inside the card`).toBe(true);
      // Inside the card, never clipped (from 780px the widest title, Total Downloaded, may reach into the
      // card's right padding, as it always has: measured unchanged from the original composition).
      expect(card.titleBox.left >= card.inner.left - 0.5 && card.titleBox.right <= card.edge.right + 0.5, `${at}: the title inside the card`).toBe(true);
      expect(Math.abs(card.lineBottom - card.cardBottom), `${at}: the ECG line box at the card's floor`).toBeLessThanOrEqual(0.5);
      if (WEB_FONTS) expect(card.lines, `${at}: the title on one line`).toBe(1);
      // The tallest heartbeat and its 3px glow pass beneath the group, as beneath the former composition.
      if (card.lines === 1) expect(card.peak - Math.max(card.group.bottom, card.titleBox.bottom), `${at}: clearance`).toBeGreaterThan(3 + 3);
    }
    if (width === 1440) {
      // Text-heavy samples: the paused Processing card and the blocked files line, whole and inside.
      await expect(page.locator('#s-processing')).toHaveText('Paused');
      await expect(page.locator('#s-blocked')).toHaveText('3 blocked files');
    }
    await page.screenshot({path: `test-results/checkpoint-kpi-hierarchy-${width}.png`, clip: await page.locator('#dash-hero').boundingBox()});
  }
});

/* The Statistics header: which wording each line shows, whether each is drawn
 * whole, and whether the next longer wording would have been. */
const header = page => page.evaluate(() => {
  const h = document.querySelector('#view-stats > .dp-stats-master-header');
  const box = node => node.getBoundingClientRect();
  const lines = [['dpTitle', '.dp-stats-heading'], ['dpHint', '.dp-stats-subtitle']].map(([key, selector]) => {
    const line = h.querySelector(selector), kept = h.dataset[key];
    const state = kept ?? '';
    const order = ['', 'short', 'shortest'];
    const whole = line.scrollWidth <= line.clientWidth;
    let longerFits = false;
    if (order.indexOf(state) > 0) {
      const longer = order[order.indexOf(state) - 1];
      if (longer) h.dataset[key] = longer; else delete h.dataset[key];
      longerFits = line.scrollWidth <= line.clientWidth;
      if (kept === undefined) delete h.dataset[key]; else h.dataset[key] = kept;
    }
    return {state, whole, longerFits, text: line.textContent, shown: getComputedStyle(line, '::before').content};
  });
  const trigger = document.getElementById('stats-period'), tabs = document.getElementById('stats-period-tabs');
  const style = getComputedStyle(h);
  return {lines, tip: h.querySelector('.dp-stats-master-title').dataset.tooltip ?? null,
    trigger: trigger.offsetWidth ? box(trigger).toJSON() : null, tabs: tabs.offsetWidth ? box(tabs).toJSON() : null, header: box(h).toJSON(),
    contentRight: box(h).right - parseFloat(style.paddingRight) - parseFloat(style.borderRightWidth),
    rows: new Set([...h.children].filter(n => n.offsetWidth && !n.matches('[popover]')).map(n => Math.round((box(n).top + box(n).bottom) / 2))).size,
    cards: [...document.querySelectorAll('#detail-stat-cards > .metric-card')].map(card => ({metric: card.dataset.dpStatsMetric,
      label: card.querySelector('.metric-label').innerText, value: card.querySelector('.metric-value').textContent, top: Math.round(box(card).top),
      whole: ['.metric-label', '.metric-value'].every(selector => {
        const node = card.querySelector(selector), shown = [...node.querySelectorAll('span')].find(span => span.offsetWidth) || node;
        const range = document.createRange(); range.selectNodeContents(shown);
        const rects = range.getClientRects(), text = range.getBoundingClientRect(), c = box(card), cs = getComputedStyle(card);
        return new Set([...rects].map(rect => Math.round(rect.top))).size === 1
          && text.left >= c.left + parseFloat(cs.paddingLeft) - 0.5 && text.right <= c.right - parseFloat(cs.paddingRight) + 0.5;
      })})),
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth};
});

test('the Statistics header shows the longest wordings drawn whole, the filters from 780px and one handheld control below it; two period cards', async ({page}) => {
  await boot(page);
  for (const width of [320, 390, 779, 780, 1440]) {
    await page.setViewportSize({width, height: 900});
    await show(page, 'stats');
    const h = await header(page);
    for (const line of h.lines) {
      expect(line.whole, `${width}px ${line.text}: drawn whole`).toBe(true);
      expect(line.longerFits, `${width}px ${line.text}: the longest wording that fits`).toBe(false);
    }
    // The full wordings stay the text; a shortened header says them in its tooltip.
    expect(h.lines.map(line => line.text)).toEqual(['By The Numbers', 'Vibes are not a performance metric.']);
    expect(h.tip, `${width}px tooltip`).toBe(h.lines.some(line => line.state) ? 'By The Numbers. Vibes are not a performance metric.' : null);
    expect(h.overflow, `${width}px`).toBe(0);
    const handheld = width < 780;
    expect([Boolean(h.trigger), Boolean(h.tabs)], `${width}px: the control below 780px, the filters from it`).toEqual([handheld, !handheld]);
    expect(h.rows, `${width}px: one header row`).toBe(1);
    const control = h.trigger || h.tabs;
    expect(Math.abs(control.right - h.contentRight), `${width}px: right-aligned`).toBeLessThanOrEqual(0.5);
    expect(Math.abs((control.top + control.bottom) / 2 - (h.header.top + h.header.bottom) / 2), `${width}px: centred on the row`).toBeLessThanOrEqual(1);
    expect(h.cards.map(card => card.metric)).toEqual(['downloads', 'data']);
    expect(h.cards.map(card => card.label)).toEqual(['Downloads Added', handheld ? 'Data Transferred' : 'Total Data Downloaded']);
    expect(h.cards[1].value).toBe('679.9 GB');
    expect(h.cards[0].top === h.cards[1].top, `${width}px: the two cards side by side`).toBe(true);
    // Label and value each drawn whole on one line inside their card (scaled down on a narrow card).
    for (const card of h.cards) expect(card.whole, `${width}px ${card.metric}: label and value whole`).toBe(true);
  }
  // From 780px the header is the desktop's: the full wordings at 1440px.
  expect((await header(page)).lines.map(line => line.state)).toEqual(['', '']);
});

test('the handheld period control: one width for every period; its menu chooses through the filters, by touch and keyboard, and closes on Escape, outside, Tab and 780px', async ({page}) => {
  const detail = await boot(page, {width: 390, height: 844});
  await show(page, 'stats');
  await expect.poll(() => detail.length).toBeGreaterThan(0);
  const trigger = page.locator('#stats-period'), menu = page.locator('#stats-period-menu');
  await expect(trigger).toHaveAttribute('aria-label', 'Statistics period: 7Days');
  const widths = new Set();
  const choose = async (period, label) => {
    const before = detail.length;
    await trigger.click();
    await expect(menu).toBeVisible();
    await expect(menu.locator('[role="option"]')).toHaveText(PERIODS.map(([, text]) => text));
    await menu.locator(`[role="option"][data-period="${period}"]`).click();
    await expect(menu).toBeHidden();
    await expect(trigger).toBeFocused();
    await expect(trigger).toHaveAttribute('aria-label', `Statistics period: ${label}`);
    await expect(page.locator(`#stats-period-tabs .ftab[data-period="${period}"]`)).toHaveClass(/\bactive\b/);
    await expect(page.locator(`#stats-period-tabs .ftab[data-period="${period}"]`)).toHaveAttribute('aria-pressed', 'true');
    // One choice is one load of that period.
    await expect.poll(() => detail.slice(before)).toEqual([period]);
    await expect(page.locator('#detail-stat-cards [data-dp-stats-metric="downloads"] .metric-sub')).toContainText('Downloads added');
    widths.add((await trigger.boundingBox()).width);
  };
  for (const [period, label] of PERIODS) await choose(period, label);
  expect(widths.size, 'one width whichever period is chosen').toBe(1);
  // The menu is clear of the header and dock, inside the window, over the card's scroller.
  await trigger.click();
  const [m, dock] = await Promise.all([menu.boundingBox(), page.locator('#shell-dock').boundingBox()]);
  expect(m.y + m.height).toBeLessThanOrEqual(dock.y);
  expect(m.x >= 0 && m.x + m.width <= 390).toBe(true);
  await expect(menu.locator('[aria-selected="true"]')).toHaveText('All Time');
  await expect(menu.locator('[aria-selected="true"]')).toBeFocused();
  // Keyboard: Home, ArrowDown, Enter.
  await page.keyboard.press('Home');
  await page.keyboard.press('ArrowDown');
  await expect(menu.locator('[data-period="24h"]')).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(menu).toBeHidden();
  await expect(trigger).toHaveAttribute('aria-label', 'Statistics period: 1Day');
  // Escape, a press outside, and Tab each close it without choosing.
  for (const [how, close] of [['Escape', () => page.keyboard.press('Escape')], ['outside', () => page.mouse.click(30, 400)], ['Tab', () => page.keyboard.press('Tab')]]) {
    await trigger.click();
    await expect(menu, how).toBeVisible();
    await expect(menu.locator('[aria-selected="true"]'), how).toBeFocused();
    const before = detail.length;
    await close();
    await expect(menu, `${how} closes it`).toBeHidden();
    expect(detail.length, `${how} chooses nothing`).toBe(before);
  }
  await expect(page.locator('#stats-period-tabs .ftab.active')).toHaveAttribute('data-period', '24h');
  // Crossing to 780px closes it; the filters show the same choice, and nothing is read again.
  await trigger.click();
  const before = detail.length;
  await page.setViewportSize({width: 780, height: 844});
  await expect(menu).toBeHidden();
  await expect(page.locator('#stats-period-tabs .ftab[data-period="24h"]')).toHaveAttribute('aria-pressed', 'true');
  await page.setViewportSize({width: 390, height: 844});
  await expect(trigger).toHaveAttribute('aria-label', 'Statistics period: 1Day');
  expect(detail.length).toBe(before);
});

/* A handheld master page: its card against the content box, the header and
 * the dock; which elements scroll; and its scroller driven to its end. */
const MASTER = {
  stats: {card: '#view-stats', head: '#view-stats > .dp-stats-master-header', scroller: '#view-stats > .dp-stats-master-body'},
  events: {card: '#view-events > .dp-activity-card', head: '#view-events > .dp-activity-card > .card-header', scroller: '#event-list'},
  settings: {card: '#view-settings > .dp-settings-master-card', head: '#view-settings .dp-settings-master-header', scroller: '#view-settings .dp-settings-scroll'},
  help: {card: '#view-help > .dp-help-master-card', head: '#view-help .dp-help-master-header', scroller: '#view-help .dp-help-scroll'},
};
const master = (page, view) => page.evaluate(({card, head, scroller}) => {
  const box = node => node.getBoundingClientRect();
  const content = document.getElementById('content'), c = box(document.querySelector(card)), s = document.querySelector(scroller);
  const headTop = box(document.querySelector(head)).top;
  const scrolling = [content, ...content.querySelectorAll('*')].filter(node => node === content || node.offsetParent !== null)
    .filter(node => /auto|scroll/.test(getComputedStyle(node).overflowY) && node.scrollHeight > node.clientHeight + 1);
  s.scrollTop = s.scrollHeight;
  const last = [...s.querySelectorAll('*')].filter(node => node.offsetParent !== null && node.getBoundingClientRect().height > 0).pop();
  return {top: c.top - box(document.getElementById('topbar')).bottom, bottom: box(document.getElementById('shell-dock')).top - c.bottom,
    contentScrolls: content.scrollHeight > content.clientHeight + 1, scrollers: scrolling.map(node => node === s),
    atEnd: Math.abs(s.scrollTop + s.clientHeight - s.scrollHeight) <= 1, headMoved: box(document.querySelector(head)).top - headTop,
    lastInside: !last || box(last).bottom <= box(s).bottom + 1};
}, MASTER[view]);

test('below 780px Statistics, Activity Log, Settings and Help fill the content box 10px from the header and the dock, header fixed, one inner scroller reaching its end', async ({page}) => {
  await boot(page, {width: 320, height: 700});
  for (const [width, height] of [[320, 700], [390, 844], [390, 560], [779, 900]]) {
    await page.setViewportSize({width, height});
    for (const view of Object.keys(MASTER)) {
      await show(page, view);
      const g = await master(page, view);
      const at = `${width}x${height} ${view}`;
      expect(Math.abs(g.top - 10), `${at}: 10px under the header`).toBeLessThanOrEqual(0.5);
      expect(Math.abs(g.bottom - 10), `${at}: 10px above the dock`).toBeLessThanOrEqual(0.5);
      expect(g.contentScrolls, `${at}: the page does not scroll`).toBe(false);
      // Settings' General deck may fit without scrolling at 779x900; any scroller is the page's own one.
      expect(g.scrollers.every(Boolean), `${at}: only the page's own scroller`).toBe(true);
      expect(g.atEnd && g.lastInside, `${at}: its end reachable`).toBe(true);
      expect(g.headMoved, `${at}: the header stays`).toBe(0);
    }
    await page.screenshot({path: `test-results/checkpoint-master-${width}x${height}.png`});
  }
  // From 780px the desktop workspace: the card to the content box's 24px lower datum.
  await page.setViewportSize({width: 780, height: 900});
  for (const view of Object.keys(MASTER)) {
    await show(page, view);
    const g = await page.evaluate(card => {
      const content = document.getElementById('content').getBoundingClientRect(), c = document.querySelector(card).getBoundingClientRect();
      return {lower: Math.round(content.bottom - c.bottom), scrolls: document.getElementById('content').scrollHeight > document.getElementById('content').clientHeight + 1};
    }, MASTER[view].card);
    expect(g, `780px ${view}`).toEqual({lower: 24, scrolls: false});
  }
});
