const { test, expect } = require('@playwright/test');

/* DP 1.0.13 emerald semantic green: the success family's computed colours on
 * the surfaces that show it -- Downloading and Done badges and their glyphs,
 * progress fill and percentage on Dashboard and Downloads, and the healthy
 * Provider Status dot -- in both themes, and provider identity chips beside
 * them unchanged. On a light theme the backed operational components (badges,
 * chips, dots) draw the light operational material: the vivid emerald mark,
 * and a label ink of the same hue and chroma deepened only in lightness; bare
 * percentages keep the light label green. The Downloads collection is
 * answered by the page itself. */

const EMERALD = 'rgb(0, 230, 118)';            // #00E676: primary (dark labels and marks; fills in both themes)
const LABEL_LIGHT = 'rgb(1, 120, 59)';         // #01783B: light-theme bare percentages
const INK_LIGHT = 'oklch(0.3 0.214114 151.762)'; // emerald's hue and chroma at lightness 0.30: light badge labels
const IDENTITY_INK = {                          // the locked identity colours, as the chip draws them
  dark: {TorBox: 'oklch(0.707893 0.148228 164.215)', 'Real-Debrid': 'oklch(0.691582 0.126192 132.063)'},
  light: {TorBox: 'oklch(0.3 0.148228 164.215)', 'Real-Debrid': 'oklch(0.3 0.126192 132.063)'},
};

const DEEP = 'rgb(0, 200, 83)';                 // #00C853: the progress gradient's leading stop

const row = (id, provider, name, status, progress) => ({id, name: `${name} sample`, hash: `h${id}`, status, progress,
  size_bytes: 1 << 30, created_at: '2026-10-11T12:00:00Z', source: 'magnet', label: null,
  current_provider_id: provider, current_provider_name: name, route_provider_id: provider, route_provider_name: name,
  provider_provenance_status: 'known', extraction_status: null, source_failure_count: 0, queue_member: status !== 'completed'});
const ROWS = [row(1, 'torbox', 'TorBox', 'downloading', 42), row(2, 'torbox', 'TorBox', 'completed', 100),
              row(3, 'realdebrid', 'Real-Debrid', 'downloading', 63), row(4, 'realdebrid', 'Real-Debrid', 'completed', 100)];

async function computed(page, body) {
  return page.evaluate(sel => {
    const style = (el, pseudo) => el ? getComputedStyle(el, pseudo || null) : null;
    const rows = {};
    for (const tr of document.querySelectorAll(`${sel} tr[data-status]`)) {
      const chip = tr.querySelector('.dp-provider-chip'), badge = tr.querySelector('.badge');
      rows[`${chip?.textContent.trim()}:${tr.dataset.status}`] = {
        chip: style(chip)?.color, badge: style(badge)?.color, glyph: style(badge, '::before')?.color,
        fill: style(tr.querySelector('.prog-fill'))?.backgroundImage, pct: style(tr.querySelector('.prog-pct'))?.color};
    }
    return rows;
  }, body);
}

test('success reads emerald in both themes, with locked identity chips beside it unchanged', async ({page}) => {
  await page.setViewportSize({width: 1600, height: 1000});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route('**/api/events/stream', route => route.abort());
  await page.route('**/api/torrents**', route => {
    if (new URL(route.request().url()).pathname !== '/api/torrents') return route.fallback();
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items: ROWS, total: ROWS.length})});
  });
  await page.goto('/');
  const dot = page.locator('#provider-status-list .dot.ok').first();
  await expect(dot).toBeVisible();
  for (const theme of ['dark', 'light']) {
    await page.evaluate(light => document.body.classList.toggle('light', light), theme === 'light');
    const label = theme === 'dark' ? EMERALD : INK_LIGHT, mark = EMERALD, pct = theme === 'dark' ? EMERALD : LABEL_LIGHT;
    expect(await dot.evaluate(node => getComputedStyle(node).backgroundColor), `${theme} healthy dot`).toBe(mark);
    for (const [view, body] of [['dashboard', '#dash-tbody'], ['torrents', '#t-tbody']]) {
      await page.locator(`#sidebar .nav-item[data-view="${view}"]`).click();
      await expect(page.locator(`${body} tr[data-status="completed"] .badge`)).toHaveCount(2);
      const rows = await computed(page, body);
      for (const provider of ['TorBox', 'Real-Debrid']) {
        for (const status of ['downloading', 'completed']) {
          const at = `${theme} ${view} ${provider} ${status}`, r = rows[`${provider}:${status}`];
          expect(r.badge, `${at} badge label`).toBe(label);
          if (view === 'dashboard') expect(r.glyph, `${at} badge glyph`).toBe(mark);
          expect(r.fill, `${at} progress fill`).toBe(`linear-gradient(90deg, ${DEEP}, ${EMERALD})`);
          if (status === 'downloading') expect(r.pct, `${at} percentage`).toBe(pct);
          expect(r.chip, `${at} identity chip`).toBe(IDENTITY_INK[theme][provider]);
        }
      }
    }
  }
});
