const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Downloads & Extraction handheld corrections (below 780px).
 *
 * Headers: Downloads' titles, and Network Sources' and Media Downloads'
 * title-and-disclosure, wrap inside the room left after Help is reserved;
 * Automatic Extraction reserves Help and Enable together on one row.
 * Islands: every Settings island is content-bounded and centred, its settings
 * inline while they fit, taking back room in the shared steps (fitIslands,
 * data-island-fit) and giving each back as soon as the room holds it.
 * Browse is the small DP button inside its field. The Archive Passwords footer
 * is the guidance, then Clear at the leading edge and Show at the trailing
 * edge, with the short words.
 *
 * The settings document and the stored archive passwords are SERVED, and every
 * write is refused here, so nothing is persisted. */

const PASSWORDS = Array.from({length: 9}, (_, index) => `synthetic-archive-${index + 1}`).join('\n');
const HANDHELD = [320, 360, 390, 430, 480, 560, 640, 720, 779];

const settle = page => page.evaluate(() => Promise.all(document.getAnimations()
  .filter(animation => Number.isFinite(animation.effect?.getComputedTiming().endTime))
  .map(animation => animation.finished.catch(() => null)))
  .then(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))));

async function open(page, tab) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  const live = await page.request.get('/api/settings').then(response => response.json());
  page.__writes = [];
  await page.route(url => url.pathname.startsWith('/api/') && !/^\/api\/(events|stream|health)/.test(url.pathname), route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'GET' && path === '/api/settings') {
      return route.fulfill({json: {...live, extraction_password_configured: true}});
    }
    if (request.method() === 'GET' && path === '/api/settings/extraction-passwords') {
      return route.fulfill({json: {passwords: PASSWORDS}});
    }
    // Reads, and the engine's own status probe (POST .../test-aria2), pass.
    if (request.method() === 'GET' || /\/test-aria2$/.test(path)) return route.fallback();
    page.__writes.push(`${request.method()} ${path}`);
    return route.abort();
  });
  await page.setViewportSize({width: 1280, height: 1000});
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await section(page, tab);
}

async function section(page, tab) {
  await page.locator(`#view-settings [data-tab="${tab}"]`).first().click();
  await expect(page.locator(`#view-settings [data-panel="${tab}"]`)).toBeVisible();
  await settle(page);
}

async function resize(page, width) {
  await page.setViewportSize({width, height: 1000});
  await settle(page);
}

// One card header as drawn.
const headers = page => page.evaluate(() => {
  const r = node => node && node.getClientRects().length ? node.getBoundingClientRect() : null;
  const hit = (a, b) => !!(a && b && a.left < b.right - 0.5 && a.right > b.left + 0.5 && a.top < b.bottom - 0.5 && a.bottom > b.top + 0.5);
  const panel = document.querySelector('#view-settings [data-panel]:not([hidden])');
  return [...panel.querySelectorAll('.card-header.dp-settings-card-header')].filter(header => header.getClientRects().length)
    .map(header => {
      const title = header.querySelector('.dp-settings-card-title-text') || header.querySelector('.card-title');
      const range = document.createRange();
      range.selectNodeContents(title);
      const lines = [...range.getClientRects()].filter(box => box.width > 0 && box.height > 8);
      const group = r(header.querySelector(':scope > .dp-settings-card-title-group'));
      const rail = r(header.querySelector(':scope > .dp-settings-card-header-controls'));
      const disclosure = r(header.querySelector('.dp-settings-disclosure'));
      const help = r(header.querySelector('.dp-settings-help-chip'));
      const toggle = r(header.querySelector('.dp-settings-extraction-enable'));
      return {
        title: title.textContent.trim(),
        fullTitle: title.scrollWidth <= title.clientWidth + 1 && getComputedStyle(title).textOverflow !== 'ellipsis',
        lines: new Set(lines.map(box => Math.round(box.bottom))).size,
        textHits: lines.some(box => hit(box, help) || hit(box, toggle)),
        groupHits: hit(group, rail),
        disclosureHits: hit(disclosure, help) || hit(disclosure, toggle),
        disclosure: disclosure && {width: disclosure.width, height: disclosure.height},
        // One structural row: the rail starts beside the identity, never under it.
        oneRow: !!(rail && group && rail.top < group.bottom && rail.left >= group.right - 0.5),
        railOneRow: !toggle || Math.abs((help.top + help.bottom) - (toggle.top + toggle.bottom)) <= 2,
        railAtEnd: rail && header.getBoundingClientRect().right - rail.right,
        overflow: header.scrollWidth > header.clientWidth + 1,
      };
    });
});

// A Settings island and its settings as drawn.
const islandAt = (page, selector) => page.locator(selector).evaluate(island => {
  const r = node => node.getBoundingClientRect();
  const box = r(island), parent = r(island.parentElement);
  const style = getComputedStyle(island);
  const inner = [box.left + island.clientLeft + parseFloat(style.paddingLeft),
    box.left + island.clientLeft + island.clientWidth - parseFloat(style.paddingRight)];
  const fields = [...island.querySelectorAll('.dp-settings-inline-field')].filter(field => field.getClientRects().length);
  const boxes = fields.map(r);
  return {
    fit: island.dataset.islandFit,
    stacked: island.dataset.island === 'stacked',
    oneRow: boxes.every((b, index) => !index || b.left >= boxes[index - 1].right - 0.5),
    inside: box.left >= parent.left - 0.5 && box.right <= parent.right + 0.5,
    outer: [box.left - parent.left, parent.right - box.right],
    slack: box.width - Math.max(...boxes.map(b => b.width)) - (box.width - (inner[1] - inner[0])),
    centres: boxes.map(b => (b.left + b.right) / 2 - (box.left + box.right) / 2),
    labels: fields.map(field => field.querySelector('.form-label').textContent.trim()),
    fields: fields.map((field, index) => {
      const info = r(field.querySelector('.dp-settings-inline-field-info'));
      const control = r(field.querySelector('.dp-settings-inline-field-control'));
      const hint = field.querySelector('.dp-settings-inline-field-info > .form-hint');
      return {
        within: boxes[index].left >= inner[0] - 0.5 && boxes[index].right <= inner[1] + 0.5,
        overflow: field.scrollWidth > field.clientWidth + 1,
        inline: control.left >= info.right - 0.5 && control.top < info.bottom && control.bottom > info.top,
        // A setting the fit turned to a column -- or every setting of an island
        // that declares its settings take columns together at the last step
        // (data-island-columns: Statistics Reporting's cadence and window).
        column: field.dataset.islandField === 'column'
          || (island.hasAttribute('data-island-columns') && island.dataset.islandFit === '4'),
        hintShown: !!hint && hint.getBoundingClientRect().width > 2,
        hintText: hint?.textContent.trim() || '',
      };
    }),
  };
});

const ISLANDS = {
  downloads: {selector: '.dp-settings-download-engine-row',
    labels: ['Download Folder', 'Maximum Concurrent Downloads']},
  extraction: {selector: '.dp-settings-extraction-behavior',
    labels: ['Delete Archives After Extraction', 'Concurrent Extractions']},
};

test('Downloads and Automatic Extraction headers: the title, and its disclosure, wrap in the room left beside Help (and Enable)',
  async ({page}) => {
    for (const tab of ['downloads', 'extraction']) {
      await open(page, tab);
      for (const width of HANDHELD) {
        await resize(page, width);
        const all = await headers(page);
        expect(all.length, `${tab} @${width}`).toBeGreaterThan(0);
        for (const header of all) {
          const at = `${tab} ${header.title} @${width}`;
          expect(header.textHits, `${at}: title under Help/Enable`).toBe(false);
          expect(header.groupHits, `${at}: identity reaches the rail`).toBe(false);
          expect(header.disclosureHits, `${at}: disclosure under Help`).toBe(false);
          expect(header.oneRow, `${at}: the rail left the header row`).toBe(true);
          expect(header.railOneRow, `${at}: Help and Enable not on one row`).toBe(true);
          expect(header.overflow, `${at}: overflow`).toBe(false);
          expect(header.fullTitle, `${at}: title cut`).toBe(true);
          expect(header.lines, `${at}: title lines`).toBeLessThanOrEqual(2);
          if (header.disclosure) expect(header.disclosure, at).toEqual({width: 32, height: 32});
        }
        if (width === 320) {
          const byTitle = Object.fromEntries(all.map(header => [header.title, header]));
          // The long titles do wrap here, rather than run under Help.
          for (const title of tab === 'downloads'
            ? ['Download Behavior & Limits', 'Transfer Method Settings', 'Download Engine Activity']
            : ['Automatic Extraction']) {
            expect(byTitle[title]?.lines, `${title} @320`).toBe(2);
          }
        }
      }
    }
    // The disclosure is still the card's own control, beside its title.
    const media = page.locator('[data-executor-tuning="media"] .dp-settings-disclosure').first();
    await section(page, 'downloads');
    await resize(page, 320);
    await expect(media).toHaveAttribute('aria-expanded', 'false');
    await media.focus();
    await page.keyboard.press('Enter');
    await expect(media).toHaveAttribute('aria-expanded', 'true');
    await page.keyboard.press('Enter');
    await expect(media).toHaveAttribute('aria-expanded', 'false');
    const enable = page.locator('#view-settings [data-panel="extraction"] [data-setting="extract_enabled"]');
    await section(page, 'extraction');
    await expect(enable).toHaveAccessibleName('Enable Automatic Extraction');
    expect(page.__writes).toEqual([]);
  });

test('the Downloads and Extraction islands keep their settings inline, take back room in order and give it back as the width returns',
  async ({page}) => {
    for (const [tab, {selector, labels}] of Object.entries(ISLANDS)) {
      await open(page, tab);
      let previous = Infinity;
      const seen = {};
      for (const width of HANDHELD) {
        await resize(page, width);
        const island = await islandAt(page, selector);
        const at = `${tab} @${width} (step ${island.fit})`;
        const step = Number(island.fit);
        seen[width] = step;
        // Least compression that fits: never more as the room grows.
        expect(step, at).toBeLessThanOrEqual(previous);
        previous = step;
        expect(island.labels, at).toEqual(labels);
        expect(island.inside, `${at}: past its card`).toBe(true);
        expect(Math.abs(island.outer[0] - island.outer[1]), `${at}: not centred ${island.outer}`).toBeLessThanOrEqual(1);
        for (const [index, field] of island.fields.entries()) {
          expect(field.within, `${at}: ${labels[index]} escapes the island`).toBe(true);
          expect(field.overflow, `${at}: ${labels[index]} overflows`).toBe(false);
          expect(field.column, `${at}: ${labels[index]} fell back to a column`).toBe(false);
          expect(field.inline, `${at}: ${labels[index]} is not title-beside-control`).toBe(true);
          expect(field.hintShown, `${at}: ${labels[index]} hint`).toBe(step < 4);
        }
        if (island.stacked) {
          // One centred stack whose outline hugs its widest setting.
          for (const centre of island.centres) expect(Math.abs(centre), `${at}: off centre`).toBeLessThanOrEqual(1);
          expect(island.slack, `${at}: outline wider than its widest setting`).toBeLessThanOrEqual(1);
        } else {
          expect(island.oneRow, `${at}: not one row`).toBe(true);
        }
      }
      // Side by side at the widest handheld width, both hints shown; stacked at
      // the narrowest.
      expect(seen[779], `${tab} @779`).toBeLessThanOrEqual(1);
      expect(seen[320], `${tab} @320`).toBeGreaterThanOrEqual(2);
      // Restored on the way back up, to the same steps.
      for (const width of [...HANDHELD].reverse()) {
        await resize(page, width);
        expect(Number((await islandAt(page, selector)).fit), `${tab} back @${width}`).toBe(seen[width]);
      }
    }
    expect(page.__writes).toEqual([]);
  });

test('a fitted island is stable: no mark changes once laid out, and its controls still work', async ({page}) => {
  await open(page, 'extraction');
  for (const width of [320, 430, 700]) {
    await resize(page, width);
    const changes = await page.evaluate(() => new Promise(done => {
      let count = 0;
      const observer = new MutationObserver(records => { count += records.length; });
      document.querySelectorAll('#view-settings .dp-settings-island, #view-settings .dp-settings-island .dp-settings-inline-field')
        .forEach(node => observer.observe(node, {attributes: true, attributeFilter: ['data-island', 'data-island-fit', 'data-island-field']}));
      setTimeout(() => { observer.disconnect(); done(count); }, 600);
    }));
    expect(changes, `marks changed while idle @${width}`).toBe(0);
  }
  await resize(page, 320);
  // The toggle's own track is still its hit target, and the number takes input.
  const remove = page.locator('[data-setting="extract_delete_archive"]');
  const before = await remove.isChecked();
  await page.locator('.dp-settings-extraction-behavior .dp-settings-inline-toggle .ttrack').click();
  await expect(remove).toBeChecked({checked: !before});
  const count = page.locator('[data-setting="extract_max_concurrent"]');
  await count.fill('3');
  await expect(count).toHaveValue('3');
  await expect(count).toHaveAccessibleName('Concurrent Extractions');
});

test('every other Settings island is bounded, centred and inline at handheld widths', async ({page}) => {
  for (const [tab, selector] of [['authentication', '.dp-settings-auth-session-row'],
    ['notifications', '.dp-settings-statistics-cadence-group'], ['maintenance', '.dp-settings-event-logging-island']]) {
    await open(page, tab);
    for (const width of [320, 430, 640, 779]) {
      await resize(page, width);
      const island = await islandAt(page, selector);
      const at = `${tab} @${width} (step ${island.fit})`;
      expect(island.inside, `${at}: past its card`).toBe(true);
      expect(Math.abs(island.outer[0] - island.outer[1]), `${at}: not centred`).toBeLessThanOrEqual(1);
      for (const field of island.fields) {
        expect(field.within, `${at}: escapes`).toBe(true);
        expect(field.overflow, `${at}: overflows`).toBe(false);
        // Inline wherever it fits; only a setting that cannot fit even at the
        // last step takes its title above its control.
        if (!field.column) expect(field.inline, `${at}: not inline`).toBe(true);
        else expect(island.fit, at).toBe('4');
      }
      if (width >= 430) expect(island.fields.every(field => !field.column), `${at}: column`).toBe(true);
    }
  }
  expect(page.__writes).toEqual([]);
});

test('Browse is the small DP button, centred inside the Download Folder field, and still opens the directory browser',
  async ({page}) => {
    await open(page, 'downloads');
    const measure = () => page.locator('.dp-settings-download-folder-field').evaluate(field => {
      const button = field.querySelector('.dp-settings-directory-field-browse').getBoundingClientRect();
      const box = field.querySelector('.dp-action-field').getBoundingClientRect();
      return {height: button.height, field: box.height, top: button.top - box.top, bottom: box.bottom - button.bottom,
        inside: button.left >= box.left && button.right <= box.right};
    });
    const wide = await measure();
    // From 780px: the compact chip, as before.
    expect(wide.height).toBe(22);
    for (const width of [320, 560, 779]) {
      await resize(page, width);
      const g = await measure();
      expect(g.height, `@${width}`).toBe(30);
      expect(g.field, `@${width}`).toBe(42);
      expect(Math.abs(g.top - g.bottom), `@${width}: not centred`).toBeLessThanOrEqual(1);
      expect(g.inside, `@${width}`).toBe(true);
    }
    await resize(page, 320);
    const browse = page.locator('[data-action="browse-download-folder"]');
    await expect(browse).toHaveAccessibleName('Browse server directories for Download Folder');
    await browse.click();
    const dialog = page.locator('.dp-settings-directory-dialog');
    await expect(dialog).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    expect(page.__writes).toEqual([]);
  });

test('Archive Passwords: the guidance centred above Clear (leading) and Show (trailing), with the short words, outside the list',
  async ({page}) => {
    await open(page, 'extraction');
    const editor = page.locator('.dp-settings-extraction-password-editor');
    const clear = editor.locator('.dp-settings-password-clear');
    const eye = editor.locator('.dp-settings-password-eye');
    await expect(editor.locator('.dp-settings-password-line')).toHaveCount(10);
    const footer = () => editor.evaluate(node => {
      const r = el => el.getBoundingClientRect();
      const foot = node.querySelector('.dp-settings-password-footer');
      const guidance = node.querySelector('.dp-settings-password-guidance');
      const range = document.createRange();
      range.selectNodeContents(guidance);
      const lines = [...range.getClientRects()].filter(box => box.width > 0);
      const style = getComputedStyle(foot);
      const inner = [r(foot).left + parseFloat(style.paddingLeft), r(foot).right - parseFloat(style.paddingRight)];
      const c = r(node.querySelector('.dp-settings-password-clear')), e = r(node.querySelector('.dp-settings-password-eye'));
      const g = r(guidance);
      return {
        region: r(node.querySelector('.dp-settings-password-region')).bottom, footTop: r(foot).top,
        guidance: {width: g.width, bottom: g.bottom,
          centre: (Math.min(...lines.map(b => b.left)) + Math.max(...lines.map(b => b.right))) / 2 - (r(node).left + r(node).right) / 2,
          lines: new Set(lines.map(b => Math.round(b.bottom))).size},
        clear: {left: c.left - inner[0], top: c.top, height: c.height},
        eye: {right: inner[1] - e.right, top: e.top, height: e.height, icon: !!node.querySelector('.dp-settings-password-eye svg')},
        editorRight: r(node).right, pageWidth: document.documentElement.scrollWidth, viewport: innerWidth,
      };
    });
    for (const width of [320, 390, 560, 779]) {
      await resize(page, width);
      const f = await footer();
      const at = `@${width}`;
      expect(f.region, `${at}: footer inside the list`).toBeLessThanOrEqual(f.footTop + 0.5);
      expect(f.guidance.bottom, `${at}: guidance not above the actions`).toBeLessThanOrEqual(Math.min(f.clear.top, f.eye.top));
      expect(Math.abs(f.guidance.centre), `${at}: guidance not centred`).toBeLessThanOrEqual(1);
      expect(f.guidance.width, `${at}: guidance squeezed`).toBeGreaterThanOrEqual(180);
      expect(f.guidance.lines, `${at}`).toBeLessThanOrEqual(3);
      expect(f.clear.left, `${at}: Clear not at the leading edge`).toBeLessThanOrEqual(1);
      expect(f.eye.right, `${at}: Show not at the trailing edge`).toBeLessThanOrEqual(1);
      expect(Math.abs(f.clear.top - f.eye.top), `${at}: not one row`).toBeLessThanOrEqual(1);
      expect([f.clear.height, f.eye.height], at).toEqual([32, 32]);
      expect(f.eye.icon, at).toBe(true);
      expect(f.pageWidth, `${at}: horizontal overflow`).toBeLessThanOrEqual(f.viewport);
      await expect(clear).toHaveText('Clear', {useInnerText: true});
      await expect(eye).toHaveText('Show', {useInnerText: true});
      await expect(editor.locator('.dp-settings-password-guidance')).toHaveText(
        'Passwords are saved as you finish editing. Use Show to reveal them, and Clear to erase the stored list.',
        {useInnerText: true});
    }
    // The meaning, the names and the state are unchanged.
    await resize(page, 320);
    await expect(clear).toHaveAccessibleName('Clear the stored archive passwords');
    await expect(eye).toHaveAccessibleName('Show all passwords');
    await eye.click();
    await expect(eye).toHaveText('Hide', {useInnerText: true});
    await expect(eye).toHaveAttribute('aria-pressed', 'true');
    await expect(eye).toHaveAccessibleName('Hide all passwords');
    await expect(editor.locator('.dp-settings-password-line').first()).toHaveValue('synthetic-archive-1');
    await eye.click();
    await expect(eye).toHaveText('Show', {useInnerText: true});
    // Clear still asks first; Cancel writes nothing.
    await clear.click();
    const dialog = page.locator('.dp-modal-overlay .dp-modal-dialog[data-tone="danger"]');
    await expect(dialog).toBeVisible();
    await dialog.locator('[data-modal-cancel]').click();
    await expect(dialog).toHaveCount(0);
    await expect(editor.locator('.dp-settings-password-line')).toHaveCount(10);
    expect(page.__writes).toEqual([]);
    // From 780px the labels are the full ones, beside the guidance.
    await resize(page, 1280);
    await expect(clear).toHaveText('Clear Passwords', {useInnerText: true});
    await expect(eye).toHaveText('Show all', {useInnerText: true});
    const wide = await footer();
    expect(Math.abs(wide.clear.top - wide.eye.top)).toBeLessThanOrEqual(1);
    expect(wide.guidance.lines).toBe(1);
  });

test('in the light theme the narrow Downloads and Extraction surfaces keep the same composition', async ({page}) => {
  await open(page, 'downloads');
  await page.evaluate(() => window.DPTheme.setMode(true));
  await resize(page, 320);
  for (const header of await headers(page)) {
    expect(header.textHits || header.disclosureHits || header.overflow, header.title).toBe(false);
  }
  const downloads = await islandAt(page, ISLANDS.downloads.selector);
  expect(downloads.fields.every(field => field.inline && field.within && !field.overflow)).toBe(true);
  await section(page, 'extraction');
  const extraction = await islandAt(page, ISLANDS.extraction.selector);
  expect(extraction.fields.every(field => field.inline && field.within && !field.overflow)).toBe(true);
  for (const header of await headers(page)) expect(header.textHits || header.overflow, header.title).toBe(false);
  expect(page.__writes).toEqual([]);
});
