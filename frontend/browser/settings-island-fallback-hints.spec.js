const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Settings islands below 780px, the last two corrections.
 *
 * A setting the island steps cannot keep inline (data-island-field="column")
 * is compact: its title, the field gap, its control -- no height left over
 * from a width its control was given for the row (the folder's 380px basis).
 * A hint the steps take out of view is said by its own setting's title
 * through the one tooltip owner: on hover, on keyboard focus, and on a tap
 * that activates nothing; drawn again, the title is a plain title again.
 *
 * The settings document is SERVED, with a long synthetic Download Folder, and
 * every write is refused, so nothing is persisted. */

const LONG_PATH = '/srv/media/very-long-library-folder-name-for-movies-and-series/incoming-transfers/synthetic';

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
    if (request.method() === 'GET' && path === '/api/settings') return route.fulfill({json: {...live, download_folder: LONG_PATH}});
    // Reads, and the engine's own status probe (POST .../test-aria2), pass.
    if (request.method() === 'GET' || /\/test-aria2$/.test(path)) return route.fallback();
    page.__writes.push(`${request.method()} ${path}`);
    return route.abort();
  });
  await page.setViewportSize({width: 1280, height: 900});
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
  await page.setViewportSize({width, height: 900});
  await settle(page);
}

// Each setting of an island: its fallback, geometry, and what its title says.
const settings = (page, selector) => page.locator(selector).evaluate(island => {
  const r = node => node.getBoundingClientRect();
  const box = r(island), parent = r(island.parentElement);
  return {
    fit: island.dataset.islandFit,
    height: box.height,
    centred: Math.abs((box.left - parent.left) - (parent.right - box.right)),
    fields: [...island.querySelectorAll('.dp-settings-inline-field')].map(field => {
      const title = field.querySelector('.dp-settings-inline-field-info > .form-label');
      const hint = field.querySelector('.dp-settings-inline-field-info > .form-hint');
      const control = field.querySelector(':scope > .dp-settings-inline-field-control');
      const parts = [...field.querySelectorAll(':scope > .dp-settings-inline-field-info > .form-label, :scope > .dp-settings-inline-field-control, :scope > .dp-settings-inline-field-action')].map(r);
      const t = r(title), c = r(control), f = r(field);
      return {
        label: title.textContent.trim(),
        column: field.dataset.islandField === 'column',
        hintDrawn: !!hint && r(hint).width > 1,
        hintText: hint?.textContent.trim() || '',
        tooltip: title.getAttribute('data-tooltip'),
        touch: title.hasAttribute('data-tooltip-touch'),
        tabindex: title.getAttribute('tabindex'),
        titleToControl: c.top - t.bottom,
        // The field is no taller than what it holds, stacked, with its gap.
        slackHeight: f.height - parts.reduce((sum, part) => sum + part.height, 0)
          - (parts.length - 1) * parseFloat(getComputedStyle(field).rowGap || 0),
        controlHeight: c.height,
        within: f.left >= box.left - 0.5 && f.right <= box.right + 0.5,
        centre: (f.left + f.right) / 2 - (box.left + box.right) / 2,
        controlSpan: [c.left - f.left, f.right - c.right],
      };
    }),
  };
});

const tooltip = page => page.locator('#dp-tooltip');

test('a setting turned to a column is compact, and returns inline as the room returns', async ({page}) => {
  await open(page, 'downloads');
  const ISLAND = '.dp-settings-download-engine-row';
  const at = {};
  for (const width of [360, 320, 310, 300, 280, 300, 320, 360]) {
    await resize(page, width);
    const island = await settings(page, ISLAND);
    const [folder, limit] = island.fields;
    at[width] = island;
    expect(island.centred, `@${width}: island off centre`).toBeLessThanOrEqual(1);
    expect(limit.column, `@${width}: Maximum Concurrent Downloads fell back`).toBe(false);
    for (const field of island.fields) expect(field.within, `@${width}: ${field.label} escapes`).toBe(true);
    if (folder.column) {
      // Title, the field gap, the control: nothing else.
      expect(folder.titleToControl, `@${width}: title to control`).toBeGreaterThanOrEqual(0);
      expect(folder.titleToControl, `@${width}: title to control`).toBeLessThanOrEqual(12);
      expect(folder.controlHeight, `@${width}: control height`).toBe(42);
      expect(folder.slackHeight, `@${width}: empty height in the field`).toBeLessThanOrEqual(1);
      expect(Math.abs(folder.centre), `@${width}: setting off centre`).toBeLessThanOrEqual(1);
      expect(folder.controlSpan, `@${width}: control narrower than its setting`).toEqual([0, 0]);
      expect(island.height, `@${width}: island height`).toBeLessThan(200);
    }
  }
  // The column is genuinely exercised, and given back.
  expect(at[300].fields[0].column).toBe(true);
  expect(at[280].fields[0].column).toBe(true);
  expect(at[320].fields[0].column).toBe(false);
  expect(at[360].fields[0].column).toBe(false);
  // The path stays one usable, scrollable field with Browse inside it.
  await resize(page, 300);
  const path = page.locator('[data-setting="download_folder"]');
  await expect(path).toHaveValue(LONG_PATH);
  const g = await page.locator('.dp-settings-download-folder-field .dp-action-field').evaluate(field => {
    const input = field.querySelector('.input'), browse = field.querySelector('.dp-settings-directory-field-browse');
    const f = field.getBoundingClientRect(), b = browse.getBoundingClientRect();
    return {scrolls: input.scrollWidth > input.clientWidth, browse: b.height, inside: b.left >= f.left && b.right <= f.right,
      centred: Math.abs((b.top - f.top) - (f.bottom - b.bottom)), inputEnds: input.getBoundingClientRect().right <= b.left};
  });
  expect(g).toEqual({scrolls: true, browse: 30, inside: true, centred: 0, inputEnds: true});
  await path.focus();
  await page.keyboard.press('End');
  expect(await path.evaluate(input => input.scrollLeft)).toBeGreaterThan(0);
  expect(page.__writes).toEqual([]);
});

test('the session island\'s column fallback is compact too', async ({page}) => {
  await open(page, 'authentication');
  await resize(page, 320);
  const island = await settings(page, '.dp-settings-auth-session-row');
  const sessions = island.fields.find(field => field.label === 'Active Browser Sessions');
  expect(sessions.column).toBe(true);
  expect(sessions.titleToControl).toBeLessThanOrEqual(12);
  expect(sessions.slackHeight).toBeLessThanOrEqual(1);
  expect(island.fields.find(field => field.label === 'Browser Session Lifetime').column).toBe(false);
});

test('a hint out of view is said by its own title, by hover and by keyboard, and only while it is out of view', async ({page}) => {
  await open(page, 'downloads');
  const ISLAND = '.dp-settings-download-engine-row';
  // Drawn: no title tooltip, no extra tab stop.
  await resize(page, 430);
  for (const field of (await settings(page, ISLAND)).fields) {
    expect(field.hintDrawn, field.label).toBe(true);
    expect([field.tooltip, field.touch, field.tabindex], field.label).toEqual([null, false, null]);
  }
  // Out of view: each title carries its own hint, whole.
  await resize(page, 320);
  const narrow = await settings(page, ISLAND);
  expect(narrow.fit).toBe('4');
  for (const field of narrow.fields) {
    expect(field.hintDrawn, field.label).toBe(false);
    expect(field.tooltip, field.label).toBe(field.hintText);
    expect(field.touch, field.label).toBe(true);
    expect(field.tabindex, field.label).toBe('0');
  }
  expect(narrow.fields[0].tooltip).toBe('Where DebridPulse saves downloads.');
  expect(narrow.fields[1].tooltip).toBe('Maximum downloads DebridPulse runs at once.');

  // Hover: each title its own text.
  const folderTitle = page.locator(`${ISLAND} .dp-settings-download-folder-field .form-label`);
  const limitTitle = page.locator(`${ISLAND} .dp-settings-download-limit-field .form-label`);
  await folderTitle.hover();
  await expect(tooltip(page)).toBeVisible();
  await expect(tooltip(page)).toHaveText('Where DebridPulse saves downloads.');
  await limitTitle.hover();
  await expect(tooltip(page)).toHaveText('Maximum downloads DebridPulse runs at once.');
  await page.mouse.move(2, 2);
  await expect(tooltip(page)).toBeHidden();

  // Keyboard: the title is a tab stop, its tooltip shows on focus and Escape
  // dismisses it; the control keeps its own name.
  await page.locator('[data-setting="download_folder"]').focus();
  await page.keyboard.press('Shift+Tab');
  await expect(folderTitle).toBeFocused();
  await expect(tooltip(page)).toHaveText('Where DebridPulse saves downloads.');
  await expect(folderTitle).toHaveAttribute('aria-describedby', /dp-tooltip/);
  await page.keyboard.press('Escape');
  await expect(tooltip(page)).toBeHidden();
  await page.keyboard.press('Tab');
  await expect(page.locator('[data-setting="download_folder"]')).toBeFocused();
  await expect(page.locator('[data-setting="download_folder"]')).toHaveAccessibleName('Download Folder');
  await expect(page.locator('[data-setting="aria2_max_active_downloads"]')).toHaveAccessibleName('Maximum Concurrent Downloads');

  // A shown tooltip goes when the width changes; the room restored, the hint
  // is drawn again and the titles are plain titles.
  await limitTitle.hover();
  await expect(tooltip(page)).toBeVisible();
  await resize(page, 430);
  await expect(tooltip(page)).toBeHidden();
  for (const field of (await settings(page, ISLAND)).fields) {
    expect(field.hintDrawn, field.label).toBe(true);
    expect([field.tooltip, field.touch, field.tabindex], field.label).toEqual([null, false, null]);
  }
  // And again on the way back down: the same, never stale.
  await resize(page, 320);
  expect((await settings(page, ISLAND)).fields.map(field => field.tooltip))
    .toEqual(['Where DebridPulse saves downloads.', 'Maximum downloads DebridPulse runs at once.']);
  // Leaving the tab takes a shown tooltip with it.
  await folderTitle.hover();
  await expect(tooltip(page)).toBeVisible();
  await section(page, 'extraction');
  await expect(tooltip(page)).toBeHidden();
  expect(page.__writes).toEqual([]);
});

test('every island\'s hidden hints, and only those, are on their titles', async ({page}) => {
  for (const [tab, selector] of [['downloads', '.dp-settings-download-engine-row'], ['extraction', '.dp-settings-extraction-behavior'],
    ['authentication', '.dp-settings-auth-session-row'], ['notifications', '.dp-settings-statistics-cadence-group'],
    ['maintenance', '.dp-settings-event-logging-island']]) {
    await open(page, tab);
    for (const width of [300, 320, 430, 779]) {
      await resize(page, width);
      const island = await settings(page, selector);
      for (const field of island.fields) {
        const at = `${tab} ${field.label} @${width} (step ${island.fit})`;
        if (field.hintDrawn) expect([field.tooltip, field.touch, field.tabindex], at).toEqual([null, false, null]);
        else expect([field.tooltip, field.touch, field.tabindex], at).toEqual([field.hintText, true, '0']);
      }
    }
    // From 780px: no island title carries a tooltip.
    await resize(page, 1280);
    expect(await page.locator(`${selector} .form-label[data-tooltip]`).count(), tab).toBe(0);
    await page.unrouteAll({behavior: 'ignoreErrors'});
  }
});

test.describe('on a touch screen', () => {
  test.use({hasTouch: true});

  test('a tap on a title shows its hint and activates nothing; a tap elsewhere dismisses it', async ({page}) => {
    await open(page, 'extraction');
    // Narrow enough that the Extraction island takes its hints out of view.
    await resize(page, 280);
    const island = await settings(page, '.dp-settings-extraction-behavior');
    expect(island.fit).toBe('4');
    const toggle = page.locator('[data-setting="extract_delete_archive"]');
    const before = await toggle.isChecked();
    const title = page.locator('.dp-settings-extraction-delete-field .form-label');
    await title.tap();
    await expect(tooltip(page)).toBeVisible();
    await expect(tooltip(page)).toHaveText('Remove original archive files only after extraction completes successfully.');
    // The label activated nothing: the toggle is as it was, nothing written.
    await expect(toggle).toBeChecked({checked: before});
    expect(page.__writes).toEqual([]);
    // Inside the viewport, clear of the edges, no horizontal scroll.
    const box = await tooltip(page).boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(8);
    expect(box.x + box.width).toBeLessThanOrEqual(280 - 8);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(280);
    // Readable: still there a moment later.
    await page.waitForTimeout(1500);
    await expect(tooltip(page)).toBeVisible();
    // A tap on the other title shows its own.
    await page.locator('.dp-settings-extraction-concurrency-field .form-label').tap();
    await expect(tooltip(page)).toHaveText('Maximum extraction jobs DebridPulse runs at once.');
    // A tap elsewhere dismisses it.
    await page.locator('#view-settings [data-panel="extraction"] .dp-settings-extraction-password-field > .form-label').tap();
    await expect(tooltip(page)).toBeHidden();
    await expect(toggle).toBeChecked({checked: before});

    // While shown, a tap on the title is an ordinary use of the label: here,
    // the number field it labels takes focus.
    await resize(page, 280);
    const count = page.locator('.dp-settings-extraction-concurrency-field .form-label');
    await count.tap();
    await expect(tooltip(page)).toBeVisible();
    await count.tap();
    await expect(tooltip(page)).toBeHidden();
    await expect(page.locator('[data-setting="extract_max_concurrent"]')).toBeFocused();
    await expect(toggle).toBeChecked({checked: before});
    expect(page.__writes).toEqual([]);
  });
});
