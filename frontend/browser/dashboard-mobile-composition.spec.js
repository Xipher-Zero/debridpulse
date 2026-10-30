const { test, expect } = require('@playwright/test');

/* DP 1.0.14 handheld Dashboard composition (779px and below):
 * - Exactly two KPI cards, Total Downloads then Completed, side by side at
 *   equal widths at every width. Each card is its icon and value, side by side
 *   and centred together, over its label centred across the card's width.
 * - Quick Add's header is one row: the icon and title over a one-line
 *   subtitle on the left, Recover All (a 44px target) at the right inset. It
 *   gives way in one order where it is short: Recover All keeps only its glyph
 *   (a 44px square), then the subtitle shows its shorter wording.
 * - Quick Add's entry is one field with Add inside it at its right (44px tall
 *   here), centred on the field however tall it grows.
 * - The empty field rests at two lines: its authored placeholder, else its
 *   concise one, else its minimum one -- the first whose lines each fit.
 * From 780px the Dashboard is the desktop's (Add inside the field, 40px).
 * Runtime facts are routed, so nothing here depends on a live transfer; every
 * account on the password-auth runtime is a synthetic fixture created here. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const ADMIN = {username: 'browser', password: 'browser-secret'};
const run = Date.now().toString(36);
const adminSession = require('./acl-admin-session');

const FULL = 'Submit links, files, or other supported resources for download.';
const SHORT = 'Links, files & more.';
const FITS = ['', 'icon', 'icon short'];
const PLACEHOLDERS = ['Paste a link, magnet, or other supported resource…\nAdd multiple entries, one per line.',
  'Paste links or magnets…\nOne per line.', 'Paste links…\nOne per line.'];
const WIDTHS = [779, 680, 600, 560, 520, 480, 440, 430, 414, 390, 375, 360, 320];

const noFonts = page => page.route(/fonts\.(googleapis|gstatic)\.com/, route => route.abort());
const noPulse = page => page.route('**/api/events/stream', () => {});

async function open(page, {width = 390, paused = false, totals = null} = {}) {
  await noFonts(page);
  await noPulse(page);
  if (paused || totals) await page.route('**/api/stats', async route => {
    const response = await route.fetch();
    const stats = await response.json();
    route.fulfill({response, json: {...stats, ...(paused ? {paused: true} : {}),
      ...(totals ? {completed_count: totals, by_status: {...stats.by_status, completed: totals}} : {})}});
  });
  await page.setViewportSize({width, height: 844});
  await page.goto('/');
  await expect(page.locator('#s-total')).not.toHaveText('—');
  await page.evaluate(() => document.fonts.ready);
}

// Settled: the header's fit and the field's sizing have run for this width (observers, before paint).
const settle = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));

/* The Dashboard as drawn: the shown KPI cards, Quick Add's header and entry. */
const composition = page => page.evaluate(() => {
  const box = node => { const r = node.getBoundingClientRect(); return {left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height}; };
  const card = document.querySelector('#view-dashboard .dp-dashboard-quick-add');
  const header = card.querySelector(':scope > .card-header');
  const subtitle = header.querySelector('.dp-card-subtitle');
  const shown = getComputedStyle(subtitle, '::before').content;
  const recover = document.getElementById('btn-recover-all');
  const pause = header.querySelector('.dp-global-pause-center');
  const textBox = node => { const range = document.createRange(); range.selectNodeContents(node); return box(range); };
  const lines = node => { const range = document.createRange(); range.selectNodeContents(node); return new Set([...range.getClientRects()].map(rect => Math.round(rect.top))).size; };
  return {
    cards: [...document.querySelectorAll('#view-dashboard .dash-hero-stat')].filter(node => node.getBoundingClientRect().width > 0).map(node => {
      const label = node.querySelector('.dhs-label'), value = node.querySelector('.dhs-val');
      // Each word of the label drawn whole on one line (a wrap only between words).
      const words = [...label.textContent.matchAll(/\S+/g)].map(match => {
        const range = document.createRange();
        range.setStart(label.firstChild, match.index);
        range.setEnd(label.firstChild, match.index + match[0].length);
        return new Set([...range.getClientRects()].map(rect => Math.round(rect.top))).size;
      });
      const style = getComputedStyle(node), own = node.getBoundingClientRect();
      return {label: label.textContent, value: value.textContent, box: box(node), icon: box(node.querySelector('.dhs-icon')),
        valueText: textBox(value), labelText: textBox(label), labelLines: lines(label), wordsWhole: words.every(count => count === 1),
        inner: {left: own.left + parseFloat(style.paddingLeft), right: own.right - parseFloat(style.paddingRight)},
        spark: box(node.querySelector('.dp-card-spark'))};
    }),
    fit: header.dataset.dpFit ?? null,
    subtitle: {text: shown !== 'none' ? JSON.parse(shown.split(' / ')[0]) : subtitle.textContent, box: box(subtitle),
      // The drawn text's own width, in the font it is drawn in (the alias or the span).
      drawn: (() => { const font = getComputedStyle(subtitle, shown !== 'none' ? '::before' : null); const context = document.createElement('canvas').getContext('2d');
        context.font = `${font.fontWeight} ${font.fontSize} ${font.fontFamily}`; context.letterSpacing = font.letterSpacing;
        return context.measureText(shown !== 'none' ? JSON.parse(shown.split(' / ')[0]) : subtitle.textContent).width; })(),
      within: subtitle.scrollWidth <= subtitle.clientWidth, line: parseFloat(getComputedStyle(subtitle, shown !== 'none' ? '::before' : null).lineHeight)},
    title: box(header.querySelector('.card-title')), copy: box(header.querySelector('.dp-card-heading-copy')),
    icon: box(header.querySelector('.card-title > .dp-icon')), header: box(header),
    headerInset: parseFloat(getComputedStyle(header).paddingRight),
    recover: recover && box(recover), recoverLabelShown: recover ? recover.querySelector('[data-dp-icon-label]').getBoundingClientRect().width > 1 : null,
    pause: {display: getComputedStyle(pause).display, box: box(pause)},
    field: box(document.getElementById('q-transfer-input')), frame: box(document.querySelector('#view-dashboard .direct-link-entry')),
    add: box(document.getElementById('btn-add-transfer')), quickAdd: box(card),
    pageWidth: document.documentElement.scrollWidth, contentOverflow: document.getElementById('content').scrollWidth > document.getElementById('content').clientWidth,
  };
});

// Would the header state one step earlier in the order have kept the subtitle within its column?
const earlierFits = page => page.evaluate(FITS => {
  const header = document.querySelector('#view-dashboard .dp-dashboard-quick-add > .card-header');
  const subtitle = header.querySelector('.dp-card-subtitle');
  const at = FITS.indexOf(header.dataset.dpFit);
  if (at <= 0) return false;
  const kept = header.dataset.dpFit;
  header.dataset.dpFit = FITS[at - 1];
  const fits = subtitle.scrollWidth <= subtitle.clientWidth;
  header.dataset.dpFit = kept;
  return fits;
}, FITS);

/* The empty field: which placeholder it shows, whether that one and each
 * earlier one rest within two lines (measured as the field's own text, as the
 * sizing owner measures it), and its height against its two-line rest. */
const placeholder = page => page.evaluate(PLACEHOLDERS => {
  const field = document.getElementById('q-transfer-input');
  const style = getComputedStyle(field);
  const border = parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
  const twoLines = Math.ceil(parseFloat(style.lineHeight) * 2 + parseFloat(style.paddingTop) + parseFloat(style.paddingBottom) + border);
  const resting = field.getBoundingClientRect().height, height = field.style.height, value = field.value;
  const fits = text => { field.value = text; field.style.height = `${twoLines}px`; const fit = field.scrollHeight + border <= twoLines; field.value = ''; return fit; };
  const index = PLACEHOLDERS.indexOf(field.placeholder);
  const result = {index, value, restsAtTwo: Math.abs(resting - twoLines) <= 0.5, twoLines,
    fitsItself: index >= 0 && fits(PLACEHOLDERS[index]), earlierFits: PLACEHOLDERS.slice(0, Math.max(index, 0)).some(fits)};
  field.style.height = height;
  return result;
}, PLACEHOLDERS);

// Add inside the field: at its right inset (10px and the edge), centred on it
// however tall it is, `height` tall, and the text area ends where Add begins.
function expectAddInField(c, at, height) {
  expect(c.add.left >= c.frame.left && c.add.top >= c.frame.top && c.add.bottom <= c.frame.bottom, `${at}: Add inside the field`).toBe(true);
  expect(c.add.left, `${at}: the text area ends before Add`).toBeGreaterThanOrEqual(c.field.right);
  expect(Math.abs((c.add.top + c.add.bottom) / 2 - (c.frame.top + c.frame.bottom) / 2), `${at}: Add centred on the field`).toBeLessThanOrEqual(0.5);
  expect(Math.abs(c.frame.right - c.add.right - 11), `${at}: Add at the field's right inset`).toBeLessThanOrEqual(0.5);
  expect(Math.round(c.add.height), at).toBe(height);
}

// The two KPI cards: one row, equal widths, each its icon and value centred
// together over its centred label, nothing clipped, its ECG at its bottom.
function expectKpis(c, at) {
  expect(c.cards.map(card => card.label), at).toEqual(['Total Downloads', 'Completed']);
  const [first, second] = c.cards;
  expect(Math.abs(first.box.top - second.box.top), `${at}: one row`).toBeLessThanOrEqual(0.5);
  expect(first.box.right, `${at}: in order`).toBeLessThanOrEqual(second.box.left);
  expect(Math.abs(first.box.width - second.box.width), `${at}: equal widths`).toBeLessThanOrEqual(0.5);
  for (const card of c.cards) {
    const where = `${at} ${card.label}`;
    const middle = rect => (rect.top + rect.bottom) / 2;
    expect(Math.abs(middle(card.icon) - middle(card.valueText)), `${where}: icon and value centred together`).toBeLessThanOrEqual(1);
    const group = {left: card.icon.left, right: card.valueText.right};
    expect(Math.abs((group.left + group.right) / 2 - (card.box.left + card.box.right) / 2), `${where}: the group centred in the card`).toBeLessThanOrEqual(1);
    expect(group.left >= card.inner.left - 0.5 && group.right <= card.inner.right + 0.5, `${where}: icon and value within the card`).toBe(true);
    expect(card.labelText.top, `${where}: the label below the group`).toBeGreaterThanOrEqual(Math.max(card.icon.bottom, card.valueText.bottom));
    expect(Math.abs((card.labelText.left + card.labelText.right) / 2 - (card.box.left + card.box.right) / 2), `${where}: the label centred`).toBeLessThanOrEqual(1);
    expect(card.labelText.left >= card.inner.left - 0.5 && card.labelText.right <= card.inner.right + 0.5, `${where}: the label within the card`).toBe(true);
    expect(card.wordsWhole, `${where}: a word of the label split`).toBe(true);
    expect(card.labelLines, `${where}: label lines`).toBeLessThanOrEqual(card.label === 'Completed' ? 1 : 2);
    expect(card.labelText.bottom, `${where}: the label inside the card`).toBeLessThanOrEqual(card.box.bottom);
    expect(Math.abs(card.box.bottom - card.spark.bottom), `${where}: the ECG at the card's bottom edge`).toBeLessThanOrEqual(1);
  }
}

test.describe('handheld Dashboard composition', () => {
  test('two KPI cards on one row, one Quick Add header row and one field with Add inside at every handheld width; the header gives way in one order and nothing given up returns', async ({page}) => {
    await open(page, {width: 779});
    let order = 0;
    for (const width of WIDTHS) {
      await page.setViewportSize({width, height: 844});
      await settle(page);
      const at = `${width}px`;
      const c = await composition(page);
      expectKpis(c, at);
      // The header: one row, Recover All 44px tall at the right inset beside the title; no paused row while not paused.
      expect(c.pause.display, at).toBe('none');
      expect(c.header.height, `${at}: one header row`).toBeLessThanOrEqual(78.5);
      expect(Math.abs((c.recover.top + c.recover.bottom) / 2 - (c.title.top + c.title.bottom) / 2), `${at}: Recover All beside the title`).toBeLessThanOrEqual(1);
      expect(Math.abs(c.recover.right - (c.header.right - c.headerInset)), `${at}: Recover All at the right inset`).toBeLessThanOrEqual(1);
      expect(Math.round(c.recover.height), `${at}: Recover All's target`).toBe(44);
      expect(c.title.right, `${at}: the title clear of Recover All`).toBeLessThanOrEqual(c.recover.left);
      expect(c.icon.width, `${at}: the title icon kept`).toBeGreaterThan(30);
      // The subtitle: one line, within its column, the full sentence unless the shorter wording is needed.
      expect(FITS, at).toContain(c.fit);
      expect(c.subtitle.text, at).toBe(c.fit.includes('short') ? SHORT : FULL);
      expect(c.subtitle.within, `${at}: the subtitle overflows its column`).toBe(true);
      expect(c.subtitle.box.height, `${at}: the subtitle wraps`).toBeLessThanOrEqual(c.subtitle.line + 0.5);
      expect(c.subtitle.box.right, at).toBeLessThanOrEqual(c.copy.right + 0.5);
      expect(c.recoverLabelShown, at).toBe(c.fit === '');
      if (c.fit !== '') expect([Math.round(c.recover.width), Math.round(c.recover.height)], `${at}: a glyph-only 44px square`).toEqual([44, 44]);
      expect(await earlierFits(page), `${at}: an earlier state would have fitted`).toBe(false);
      expect(FITS.indexOf(c.fit), `${at}: something given up returned`).toBeGreaterThanOrEqual(order);
      order = FITS.indexOf(c.fit);
      if (width === 320) {
        expect(c.subtitle.text).toBe(SHORT);
        // The room the header leaves the subtitle: from the copy's start to the title region's end (it contracts; Recover All follows).
        expect(c.title.right - c.copy.left - c.subtitle.drawn, '320px: the short subtitle has room to spare').toBeGreaterThanOrEqual(24);
      }
      // The entry: one field spanning the body, Add inside it.
      expectAddInField(c, at, 44);
      expect(Math.abs((c.quickAdd.right - c.frame.right) - (c.frame.left - c.quickAdd.left)), `${at}: the field spans the body`).toBeLessThanOrEqual(1);
      // The empty field: the first placeholder that rests at two lines.
      const p = await placeholder(page);
      expect(p.index, `${at}: one of the authored placeholders`).toBeGreaterThanOrEqual(0);
      expect([p.fitsItself, p.earlierFits, p.restsAtTwo], `${at}: placeholder ${p.index}`).toEqual([true, false, true]);
      expect([c.pageWidth, c.contentOverflow], `${at}: horizontal overflow`).toEqual([width, false]);
    }
    expect(order, 'the shorter wording is reached at 320px').toBe(2);
    await page.screenshot({path: 'test-results/checkpoint-dashboard-mobile-320.png', clip: {x: 0, y: 0, width: 320, height: 560}});
    // From 780px: the desktop's three cards, its header, the full sentence and placeholder, Add inside at 40px.
    await page.setViewportSize({width: 780, height: 844});
    await settle(page);
    const wide = await composition(page);
    expect(wide.cards.map(card => card.label)).toEqual(['Total Downloads', 'Completed', 'Total Downloaded']);
    expect([wide.fit, wide.subtitle.text, wide.recoverLabelShown, Math.round(wide.recover.height)]).toEqual([null, FULL, true, 36]);
    expectAddInField(wide, '780px', 40);
    expect((await placeholder(page)).index).toBe(0);
  });

  // A count wider than the room beside its icon is drawn in compact notation,
  // the most precise form that fits (3, then 2 significant digits); the
  // element's text -- what assistive technology reads -- stays exact, and
  // from 780px every count is drawn exactly. Counts arrive through the real
  // stats path (loadStats), changed live while the page stays open.
  test('KPI counts: whole where they fit, compact (most precise that fits) where not, exact text kept, exact again from 780px; live changes and resizing re-fit both cards', async ({page}) => {
    let count = 636;
    await noFonts(page);
    await noPulse(page);
    await page.route('**/api/stats', async route => {
      const response = await route.fetch();
      const stats = await response.json();
      route.fulfill({response, json: {...stats, completed_count: count, by_status: {completed: count}}});
    });
    await page.setViewportSize({width: 320, height: 844});
    await page.goto('/');
    await expect(page.locator('#s-total')).not.toHaveText('—');
    await page.evaluate(() => document.fonts.ready);
    const tooltips = await page.locator('#view-dashboard .dash-hero-stat').evaluateAll(cards => cards.map(card => card.dataset.tooltip));
    const drawn = () => page.evaluate(() => ['s-total', 's-completed'].map(id => {
      const value = document.getElementById(id), card = value.closest('.dash-hero-stat'), style = getComputedStyle(card);
      const own = card.getBoundingClientRect(), box = value.getBoundingClientRect();
      const inner = own.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight);
      const room = own.width - parseFloat(style.borderLeftWidth) - parseFloat(style.borderRightWidth) - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight)
        - card.querySelector('.dhs-icon').getBoundingClientRect().width - parseFloat(getComputedStyle(value).marginLeft);
      // Each form's drawn width, tried on the value itself and put back.
      const kept = value.dataset.compact;
      const width = form => { if (form === null) delete value.dataset.compact; else value.dataset.compact = form; return value.getBoundingClientRect().width; };
      const forms = [null, ...[3, 2].map(digits => new Intl.NumberFormat('en', {notation: 'compact', maximumSignificantDigits: digits}).format(Number(value.textContent)))];
      const widths = forms.map(width);
      if (kept === undefined) delete value.dataset.compact; else value.dataset.compact = kept;
      return {text: value.textContent, shown: kept ?? null, forms, tooltip: card.dataset.tooltip, fits: widths.map(w => w <= room), inside: box.right <= own.right - parseFloat(style.borderRightWidth),
        within: box.right <= inner + 0.5, alias: getComputedStyle(value, '::before').content};
    }));
    const expectFitted = async (at, exact) => {
      const cards = await drawn();
      for (const [index, card] of cards.entries()) {
        const where = `${at} card ${index + 1} (${exact})`;
        expect(card.text, `${where}: the exact count is the text`).toBe(String(exact));
        // The card's own tooltip: its explanation verbatim, plus the exact count only while a compact form is drawn.
        expect(card.tooltip, `${where}: tooltip`).toBe(card.shown === null ? tooltips[index]
          : `${tooltips[index]} Exact count: ${new Intl.NumberFormat('en').format(exact)}.`);
        expect(card.inside, `${where}: nothing drawn past the card`).toBe(true);
        if (at.startsWith('780') || at.startsWith('1440')) { expect([card.shown, card.alias], where).toEqual([null, 'none']); continue; }
        const chosen = card.forms.indexOf(card.shown);
        const first = card.fits.indexOf(true);
        // The most precise form that fits; where none does, the shortest compact one.
        expect(chosen, `${where}: drew ${card.shown ?? card.text}`).toBe(first === -1 ? card.forms.length - 1 : first);
        if (first !== -1) expect(card.within, `${where}: within the card beside its icon`).toBe(true);
      }
      expect(cards[0].shown, `${at}: both cards agree`).toBe(cards[1].shown);
      return cards[0].shown ?? cards[0].text;
    };
    const seen = {};
    for (const exact of [636, 1234, 12345, 123456, 1234567]) {
      count = exact;
      await page.evaluate(() => loadStats());
      await expect(page.locator('#s-completed')).toHaveText(String(exact));
      for (const width of [320, 375, 390, 414, 440, 779, 780]) {
        await page.setViewportSize({width, height: 844});
        await settle(page);
        seen[`${exact}@${width}`] = await expectFitted(`${width}px`, exact);
      }
    }
    // Where it fits, the count is whole; compact only where the whole count would not fit.
    expect(seen['636@320']).toBe('636');
    expect(seen['12345@440']).toBe('12345');
    expect(seen['123456@779']).toBe('123456');
    expect(seen['1234567@375']).toBe('1.23M');
    // The tooltip owner shows it: hovering the card at 320px while it is compact.
    await page.setViewportSize({width: 320, height: 844});
    await settle(page);
    await page.locator('#view-dashboard .dash-hero-stat').nth(1).hover();
    await expect(page.locator('#dp-tooltip')).toHaveText(`${tooltips[1]} Exact count: 1,234,567.`);
    await page.mouse.move(0, 0);
    // Drawn compact, the card still reads its exact count to assistive technology.
    await page.setViewportSize({width: 375, height: 844});
    await settle(page);
    expect(await page.locator('#s-completed').getAttribute('data-compact')).toBe('1.23M');
    expect(await page.locator('#view-dashboard .dash-hero-stat').nth(1).ariaSnapshot()).toContain('1234567 Completed');
    // Live: a count that crosses the room re-fits at once, both ways, at one width.
    await page.setViewportSize({width: 375, height: 844});
    await settle(page);
    for (const exact of [99999, 123456, 636]) {
      count = exact;
      await page.evaluate(() => loadStats());
      await expect(page.locator('#s-completed')).toHaveText(String(exact));
      await expectFitted('375px live', exact);
    }
    // Whole again, the cards' tooltips are exactly their explanations.
    expect(await page.locator('#view-dashboard .dash-hero-stat').evaluateAll(cards => cards.map(card => card.dataset.tooltip))).toEqual(tooltips);
  });

  test('the empty field keeps its two-line placeholder through an entry, a clear and the 779/780 crossing, and grows with what is typed', async ({page}) => {
    await open(page, {width: 320});
    await settle(page);
    const field = page.locator('#q-transfer-input');
    const rest = await placeholder(page);
    expect(rest).toMatchObject({index: 2, restsAtTwo: true});
    await field.click();
    for (const [i, line] of ['https://example.invalid/a', 'https://example.invalid/b', 'https://example.invalid/c'].entries()) {
      if (i) await page.keyboard.press('Enter');
      await page.keyboard.type(line);
    }
    const typed = await composition(page);
    expect(typed.field.height, 'the field grows with its lines').toBeGreaterThan(rest.twoLines);
    expectAddInField(typed, 'typed', 44);
    await expect(field).toBeFocused();
    await expect(field).toHaveValue('https://example.invalid/a\nhttps://example.invalid/b\nhttps://example.invalid/c');
    await field.fill('');
    expect(await placeholder(page)).toMatchObject({index: 2, restsAtTwo: true, value: ''});
    for (const [width, index] of [[780, 0], [779, null], [1440, 0], [390, null]]) {
      await page.setViewportSize({width, height: 844});
      await settle(page);
      const p = await placeholder(page);
      if (index === null) expect([p.fitsItself, p.earlierFits, p.restsAtTwo], `${width}px`).toEqual([true, false, true]);
      else expect([p.index, p.restsAtTwo], `${width}px: the authored placeholder`).toEqual([index, true]);
    }
  });

  test('Recover All keeps its name, tooltip, busy feedback and glyph as a 44px glyph-only square; Add opens the file chooser from inside the field', async ({page}) => {
    await open(page, {width: 320});
    await settle(page);
    expect((await composition(page)).fit).toBe('icon short');
    const recover = page.getByRole('button', {name: 'Recover All', exact: true});
    await expect(recover).toBeVisible();
    await expect(recover).toHaveAttribute('data-tooltip', 'Check transfers for recoverable work');
    const material = () => recover.evaluate(node => ['color', 'backgroundImage', 'borderTopColor', 'borderRadius']
      .map(name => getComputedStyle(node)[name]).join('|'));
    const glyphOnly = await material();
    let release;
    await page.route('**/api/torrents/recover-all', async route => {
      await new Promise(resolve => { release = resolve; });
      route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({recovered: 0, errors: []})});
    });
    await recover.click();
    const busy = page.getByRole('button', {name: 'Recovering…', exact: true});
    await expect(busy).toBeDisabled();
    await expect(busy).toHaveAttribute('aria-busy', 'true');
    const during = await composition(page);
    expect([Math.round(during.recover.width), Math.round(during.recover.height), during.recoverLabelShown]).toEqual([44, 44, false]);
    expect(await page.locator('#btn-recover-all svg').count()).toBe(1);
    release();
    await expect(recover).toBeEnabled();
    expect(await page.locator('#btn-recover-all svg').count()).toBe(1);
    await page.mouse.move(0, 0);                                    // its material at rest, not hovered
    await expect.poll(material).toBe(glyphOnly);
    // Wider, with its label: the same material, still 44px tall.
    await page.setViewportSize({width: 779, height: 844});
    await settle(page);
    const labelled = await composition(page);
    expect([labelled.recoverLabelShown, Math.round(labelled.recover.height)]).toEqual([true, 44]);
    expect(await material()).toBe(glyphOnly);
    // Add, from inside the field: empty, it opens the file chooser.
    await page.setViewportSize({width: 320, height: 844});
    await settle(page);
    const chooser = page.waitForEvent('filechooser');
    await page.locator('#btn-add-transfer').click();
    await chooser;
  });

  test('processing paused: the notice is shown below the header row, which keeps its title and Recover All', async ({page}) => {
    await open(page, {width: 390, paused: true});
    await expect(page.locator('.dp-global-pause-center')).toHaveClass(/\bis-visible\b/);
    await settle(page);
    const c = await composition(page);
    expect(c.pause.display).toBe('block');
    await expect(page.locator('.dp-global-pause-center')).toContainText('PROCESSING PAUSED');
    expect(c.pause.box.top, 'the notice is below the title row').toBeGreaterThanOrEqual(Math.max(c.title.bottom, c.recover.bottom));
    expect(Math.abs((c.recover.top + c.recover.bottom) / 2 - (c.title.top + c.title.bottom) / 2)).toBeLessThanOrEqual(1);
    expect(c.subtitle.within).toBe(true);
  });
});

test('a User (no Recover All): Quick Add keeps one header row whose subtitle fits, and the field with Add inside', async ({browser}) => {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const signIn = async ({username, password}, reuse) => {
    const context = await browser.newContext({baseURL: AUTH, viewport: {width: 320, height: 844}, ...(reuse ? {storageState: adminSession.get()} : {})});
    const page = await context.newPage();
    await noFonts(page);
    if (reuse) await page.goto('/');
    else {
      await page.goto('/login');
      await page.locator('#username').fill(username);
      await page.locator('#password').fill(password);
      await page.locator('button[type="submit"]').click();
    }
    await expect(page).not.toHaveURL(/\/login/);
    await page.waitForFunction(() => window.debridPulseAuth && document.getElementById('q-transfer-input'));
    if (username === ADMIN.username && !reuse) adminSession.remember(await context.storageState());
    return {context, page};
  };
  const admin = await signIn(ADMIN, adminSession.get() !== null);
  const account = {username: `dmc-user-${run}`, password: `dmc-user-${run}-pass`};
  const created = await admin.page.evaluate(async body => (await window.debridPulseAuth.fetch('/api/accounts', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})).status,
  {kind: 'local', role: 'user', display_name: `DMC ${run}`, ...account});
  expect(created).toBe(200);
  const user = await signIn(account, false);
  await expect(user.page.locator('#q-transfer-input')).toBeVisible();
  await user.page.evaluate(() => document.fonts.ready);
  await settle(user.page);
  expect(await user.page.locator('#btn-recover-all').count()).toBe(0);
  const c = await composition(user.page);
  expect(c.header.height).toBeLessThanOrEqual(78.5);
  expect([c.subtitle.within, c.subtitle.box.height <= c.subtitle.line + 0.5]).toEqual([true, true]);
  expectAddInField(c, 'User 320px', 44);
  expect((await placeholder(user.page)).restsAtTwo).toBe(true);
  await user.context.close();
  await admin.context.close();
});
