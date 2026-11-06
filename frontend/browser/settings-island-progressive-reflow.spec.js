const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Settings islands below 780px: progressive compression.
 *
 * As the room narrows, a setting's title and hint narrow with it and wrap
 * only where they must; its control keeps its size. The island's steps change
 * composition only where the room requires it, and a step never hands room
 * back to a title that had already narrowed -- so, inside one composition, a
 * title never unwraps while the room shrinks. The same width gives the same
 * layout whichever way it was reached.
 *
 * Swept every 2px from the widest handheld width to 300px and back, for every
 * Settings island. Reads only; every write is refused. */

const ISLANDS = [
  ['extraction', '.dp-settings-extraction-behavior'],
  ['downloads', '.dp-settings-download-engine-row'],
  ['authentication', '.dp-settings-auth-session-row'],
  ['notifications', '.dp-settings-statistics-cadence-group'],
  ['maintenance', '.dp-settings-event-logging-island'],
];

async function open(page, tab) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  page.__writes = [];
  await page.route(url => url.pathname.startsWith('/api/') && !/^\/api\/(events|stream|health)/.test(url.pathname), route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'GET' || /\/test-aria2$/.test(path)) return route.fallback();
    page.__writes.push(`${request.method()} ${path}`);
    return route.abort();
  });
  await page.setViewportSize({width: 1280, height: 900});
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await page.locator(`#view-settings [data-tab="${tab}"]`).first().click();
  await expect(page.locator(`#view-settings [data-panel="${tab}"]`)).toBeVisible();
}

// The island as drawn at one width.
const read = (page, selector) => page.locator(selector).evaluate(island => {
  const r = node => node.getBoundingClientRect();
  const lines = node => {
    if (!node || r(node).width < 2) return 0;
    const range = document.createRange();
    range.selectNodeContents(node);
    return new Set([...range.getClientRects()].filter(box => box.width > 0).map(box => Math.round(box.bottom))).size;
  };
  const box = r(island), parent = r(island.parentElement);
  const style = getComputedStyle(island);
  const inner = [box.left + island.clientLeft + parseFloat(style.paddingLeft),
    box.left + island.clientLeft + island.clientWidth - parseFloat(style.paddingRight)];
  return {
    state: `${island.dataset.islandFit}/${island.dataset.island || 'row'}`,
    centred: Math.round(Math.abs((box.left - parent.left) - (parent.right - box.right)) * 100) / 100,
    inside: box.left >= parent.left - 0.5 && box.right <= parent.right + 0.5,
    fields: [...island.querySelectorAll('.dp-settings-inline-field')].map(field => {
      const f = r(field), c = r(field.querySelector(':scope > .dp-settings-inline-field-control'));
      return {
        column: field.dataset.islandField === 'column',
        info: Math.round(r(field.querySelector(':scope > .dp-settings-inline-field-info')).width * 10) / 10,
        titleLines: lines(field.querySelector('.dp-settings-inline-field-info > .form-label')),
        hintLines: lines(field.querySelector('.dp-settings-inline-field-info > .form-hint')),
        control: Math.round(c.width * 10) / 10,
        within: f.left >= inner[0] - 0.5 && f.right <= inner[1] + 0.5 && field.scrollWidth <= field.clientWidth + 1,
      };
    }),
  };
});

for (const [tab, selector] of ISLANDS) {
  test(`${tab}: every 2px from 779 to 300px and back, the island narrows progressively and the same width gives the same layout`,
    async ({page}) => {
      test.setTimeout(180000);
      await open(page, tab);
      const widths = [];
      for (let width = 779; width >= 300; width -= 2) widths.push(width);
      const down = new Map();
      let previous = null;
      for (const [index, width] of [...widths, ...widths.slice(0, -1).reverse()].entries()) {
        await page.setViewportSize({width, height: 900});
        await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
        const now = await read(page, selector);
        const at = `${tab} @${width}px (${now.state})`;
        expect(now.inside, `${at}: past its card`).toBe(true);
        expect(now.centred, `${at}: off centre`).toBeLessThanOrEqual(1);
        for (const field of now.fields) expect(field.within, `${at}: a setting escapes or overflows`).toBe(true);
        const narrowing = index < widths.length;
        if (narrowing) {
          down.set(width, JSON.stringify(now));
          // One composition, 2px narrower: no title unwraps and no
          // information region widens; a control keeps its size or narrows.
          if (previous && previous.state === now.state) {
            now.fields.forEach((field, k) => {
              const before = previous.fields[k];
              if (field.column !== before.column) return;
              expect(field.titleLines, `${at}: setting ${k} title unwrapped while narrowing`).toBeGreaterThanOrEqual(before.titleLines);
              expect(field.info, `${at}: setting ${k} information widened while narrowing`).toBeLessThanOrEqual(before.info + 0.6);
              expect(field.control, `${at}: setting ${k} control widened while narrowing`).toBeLessThanOrEqual(before.control + 0.6);
            });
          }
          // Tighter spacing hands no room back to a title that had narrowed.
          if (previous && previous.state === '2/stacked' && now.state === '3/stacked') {
            now.fields.forEach((field, k) => {
              expect(Math.abs(field.info - previous.fields[k].info), `${at}: setting ${k} information moved at the spacing step`).toBeLessThanOrEqual(1);
              expect(field.titleLines, `${at}: setting ${k} title re-wrapped at the spacing step`).toBe(previous.fields[k].titleLines);
            });
          }
          // Taking the hints out of view hands no title's room to its control:
          // a title wraps more there only as the room itself narrowed (2px a
          // step, and a pixel of rounding), never because its information
          // region was given to the control.
          if (previous && previous.state === '3/stacked' && now.state === '4/stacked') {
            now.fields.forEach((field, k) => {
              if (field.column || previous.fields[k].column || field.titleLines <= previous.fields[k].titleLines) return;
              expect(previous.fields[k].info - field.info, `${at}: setting ${k} title wrapped as its room went to the control`).toBeLessThanOrEqual(3);
            });
          }
          previous = now;
        } else {
          expect(JSON.stringify(now), `${tab} @${width}px: widening differs from narrowing`).toBe(down.get(width));
        }
      }
      expect(page.__writes).toEqual([]);
    });
}

test('Delete Archives After Extraction keeps its title through the spacing step that used to re-wrap it', async ({page}) => {
  await open(page, 'extraction');
  const states = new Set();
  let lastLines = 0;
  // Narrowing through the whole stacked range one pixel at a time: the title
  // goes from one line to two once, and never back.
  for (let width = 587; width >= 316; width -= 1) {
    await page.setViewportSize({width, height: 900});
    await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
    const now = await read(page, '.dp-settings-extraction-behavior');
    states.add(now.state);
    const lines = now.fields[0].titleLines;
    expect(lines, `@${width}px (${now.state}): Delete Archives title unwrapped`).toBeGreaterThanOrEqual(lastLines);
    lastLines = lines;
  }
  // The stacked range really was crossed, through the spacing step.
  expect([...states]).toEqual(expect.arrayContaining(['2/stacked', '3/stacked']));
});
