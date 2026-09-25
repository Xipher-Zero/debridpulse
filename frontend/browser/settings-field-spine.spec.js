const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Item 3 -- ONE Settings-wide field alignment spine, and Item 4 --
 * the Usenet clear-password row centred as one unit.
 *
 * The frozen invariant is the field's left x-origin:
 *
 *     label.left == control.left
 *     hint.left  == control.left
 *
 * measured against the VISIBLE control's OUTER left edge. Two controls make
 * that distinction load-bearing:
 *
 *   - a native <select> is clipped to 1px and visually replaced by
 *     .dp-dropdown__trigger, so the trigger is the control the operator sees;
 *   - a directory field wraps its input and Browse button in a composite,
 *     which is the control's outer box.
 *
 * Both the label's box and its rendered text are asserted, because a padded
 * label would satisfy one and not the other. */

const TOLERANCE = 0.75;
const TABS = ['sources', 'downloads', 'extraction', 'authentication', 'notifications', 'maintenance'];

const USENET_FIXTURE = {
  enabled: true, priority: 0, name: 'Usenet', kind: 'provider_executor', configured: true,
  presentation: {status_name: 'Usenet', premium: true, status_endpoint: null,
    static_status: 'healthy', display_order: 20, status_group: null, status_group_label: null,
    status_tier: 'general_family', status_tier_label: 'General'},
  options: {
    operation_timeout_seconds: 30, article_cache_megabytes: 1024,
    direct_write: true, max_acquisition_retries: 3,
    servers: [{
      id: 'spine-fixture', host: 'news.example.com', port: 563, ssl: true,
      username: 'operator', password: '', password_configured: true,
      connections: 8, priority: 0, articles_per_request: 2, timeout_seconds: 60,
      enabled: true, display_name: '',
    }],
  },
};

async function openSettings(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  const live = await page.request.get('/api/settings').then(response => response.json());
  const document_ = {...live, integrations: {...live.integrations, usenet: USENET_FIXTURE}};
  await page.route(url => url.pathname === '/api/settings', route =>
    (route.request().method() === 'GET'
      ? route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(document_)})
      : route.fallback()));
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
}

/** Expand every collapsed region of one tab so nested fields really render. */
async function expandAll(page, tab) {
  await page.locator(`#view-settings [data-tab="${tab}"]`).click();
  await expect(page.locator(`.dp-settings-panel[data-panel="${tab}"]`)).toBeVisible();
  for (const selector of ['.dp-settings-disclosure[aria-expanded="false"]',
                          '[data-usenet-advanced-toggle]', 'details:not([open]) > summary']) {
    for (const node of await page.$$(`.dp-settings-panel[data-panel="${tab}"] ${selector}`)) {
      await node.click().catch(() => {});
    }
  }
  await page.waitForTimeout(150);
}

/** Every STACKED field of one tab, measured against the visible control's
 *  OUTER left edge. */
const measure = (page, tab) => page.evaluate(tab => {
  const textRect = node => {
    const range = document.createRange();
    range.selectNodeContents(node);
    const rect = range.getBoundingClientRect();
    return rect.width ? rect : node.getBoundingClientRect();
  };
  const panel = document.querySelector(`.dp-settings-panel[data-panel="${tab}"]`);
  const rows = [];
  for (const field of panel.querySelectorAll('.dp-settings-field, .dp-usenet-field')) {
    const control = field.querySelector('.dp-dropdown__trigger')
      || field.querySelector('.dp-settings-directory-field-control')
      || field.querySelector('input.input, textarea.input, select.input, .dp-field');
    if (!control) continue;
    const box = control.getBoundingClientRect();
    // A clipped native control is not the visible one.
    if (box.width < 2) continue;
    // A field whose control sits BESIDE its label is a different pattern: there
    // is no control above the label for it to share an origin with.
    const style = getComputedStyle(field);
    if (style.display.includes('grid') && style.gridTemplateColumns.split(' ').length > 1) continue;
    // A deliberately CENTRED tuning cell is a different pattern as well: its
    // label, control and help text are each centred on the cell's own axis, so
    // they have no shared left origin to share. Read off the rendered style,
    // never a list of selectors.
    if (style.justifyItems === 'center') continue;
    const label = field.querySelector(':scope > .form-label');
    const hint = field.querySelector(':scope > .form-hint');
    rows.push({
      key: (control.dataset && (control.dataset.setting || control.dataset.usenetField))
        || field.querySelector('[data-setting]')?.dataset?.setting
        || field.querySelector('[data-usenet-field]')?.dataset?.usenetField
        || control.className.slice(0, 40),
      label: label ? label.getBoundingClientRect().left - box.left : null,
      labelText: label ? textRect(label).left - box.left : null,
      hint: hint ? hint.getBoundingClientRect().left - box.left : null,
      hintText: hint ? textRect(hint).left - box.left : null,
    });
  }
  return rows;
}, tab);

for (const tab of TABS) {
  test(`every stacked ${tab} field shares one left x-origin`, async ({page}) => {
    await openSettings(page);
    await expandAll(page, tab);
    const rows = await measure(page, tab);
    // A tab may legitimately have no STACKED field left: Downloads and
    // Extraction converged on the inline grammar, where the control sits
    // beside its title/hint rather than beneath it, so there is no shared left
    // origin to measure. What must never happen is a tab rendering no field of
    // EITHER grammar -- that would mean the panel stopped rendering, not that
    // it changed shape.
    const present = await page.locator(
      `.dp-settings-panel[data-panel="${tab}"] .dp-settings-field, ` +
      `.dp-settings-panel[data-panel="${tab}"] .dp-usenet-field, ` +
      `.dp-settings-panel[data-panel="${tab}"] .dp-settings-inline-field`).count();
    expect(present, `${tab} renders no Settings field at all`).toBeGreaterThan(0);
    if (!rows.length) return;
    const off = value => value !== null && Math.abs(value) > TOLERANCE;
    const broken = rows.filter(row =>
      off(row.label) || off(row.labelText) || off(row.hint) || off(row.hintText));
    expect(broken.map(row => `${row.key}: label ${row.label} / text ${row.labelText}` +
                             ` hint ${row.hint} / text ${row.hintText}`)).toEqual([]);
  });
}

test('the Usenet server fields obey the same spine as every other tab', async ({page}) => {
  await openSettings(page);
  await expandAll(page, 'sources');
  const rows = await page.evaluate(() => {
    const card = document.querySelector('[data-usenet-collection] [data-usenet-server-id]');
    const out = [];
    for (const field of card.querySelectorAll('.dp-usenet-field')) {
      const control = field.querySelector('.input');
      const label = field.querySelector('.form-label');
      if (!control || !label || control.getBoundingClientRect().width === 0) continue;
      out.push({field: control.dataset.usenetField,
                delta: label.getBoundingClientRect().left - control.getBoundingClientRect().left});
    }
    return out;
  });
  for (const name of ['host', 'port', 'username', 'password', 'connections', 'priority']) {
    const row = rows.find(item => item.field === name);
    expect(row, `Usenet ${name} field is missing`).toBeTruthy();
    expect(Math.abs(row.delta)).toBeLessThanOrEqual(TOLERANCE);
  }
});

test('a themed select is measured as the control the operator can see', async ({page}) => {
  await openSettings(page);
  await expandAll(page, 'notifications');
  const geometry = await page.evaluate(() => {
    const trigger = document.querySelector(
      '.dp-settings-panel[data-panel="notifications"] .dp-dropdown__trigger');
    if (!trigger) return null;
    const field = trigger.closest('.dp-settings-field');
    const native = field.querySelector('select.input');
    return {
      nativeWidth: native ? native.getBoundingClientRect().width : null,
      delta: field.querySelector(':scope > .form-label').getBoundingClientRect().left
        - trigger.getBoundingClientRect().left,
    };
  });
  expect(geometry, 'no themed select rendered on this tab').toBeTruthy();
  // The native control really is the clipped one, so the trigger really is
  // what the spine has to align to.
  expect(geometry.nativeWidth).toBeLessThan(2);
  expect(Math.abs(geometry.delta)).toBeLessThanOrEqual(TOLERANCE);
});

/* DP 1.0.13: erasing a stored credential stopped being a gated checkbox that a
 * Save committed and became an explicit destructive ACTION, asked by the ONE
 * canonical Settings confirmation -- and the batch that followed put it on the
 * Password INPUT's own row.
 *
 * There is ONE state again. The collection's track minimum is now the width
 * the card genuinely needs, so the card is never starved into reflowing this
 * row onto a second line, and the two-state oracle that briefly described that
 * reflow described a layout that can no longer occur.
 *
 * What this spine spec owns is therefore the relationship between the two: the
 * button is beside the input, vertically centred against the CONTROL rather
 * than against the taller label+input stack, and it takes horizontal room from
 * the field instead of adding an action band beneath it. */
test('Clear Password sits beside the Password input on its own row', async ({page}) => {
  await openSettings(page);
  await expandAll(page, 'sources');
  const geometry = await page.evaluate(() => {
    const row = document.querySelector('.dp-usenet-clear-password');
    if (!row) return null;
    const card = row.closest('[data-usenet-server-id]');
    const input = card.querySelector('[data-usenet-field="password"]').getBoundingClientRect();
    const label = card.querySelector('[data-usenet-field="password"]')
      .closest('.dp-usenet-field').querySelector('.form-label').getBoundingClientRect();
    const button = row.querySelector('[data-usenet-action="clear-password"]').getBoundingClientRect();
    const host = row.closest('.dp-usenet-row').getBoundingClientRect();
    return {
      inputRight: input.right,
      inputCentreY: (input.top + input.bottom) / 2,
      labelCentreY: (label.top + label.bottom) / 2,
      buttonLeft: button.left,
      buttonRight: button.right,
      buttonCentreY: (button.top + button.bottom) / 2,
      rowRight: host.right,
      controls: row.querySelectorAll('input, label').length,
    };
  });
  expect(geometry, 'the clear-password row did not render').toBeTruthy();
  // Beside the input, on the same row -- never beneath it.
  expect(geometry.buttonLeft).toBeGreaterThanOrEqual(geometry.inputRight - 1);
  // Centred against the CONTROL, not against the label+input stack.
  expect(Math.abs(geometry.buttonCentreY - geometry.inputCentreY)).toBeLessThanOrEqual(2);
  expect(Math.abs(geometry.buttonCentreY - geometry.labelCentreY)).toBeGreaterThan(2);
  // The group is the button: nothing follows it, and it is not stretched.
  expect(geometry.controls).toBe(0);
  expect(geometry.buttonRight).toBeLessThanOrEqual(geometry.rowRight + 1);
});
