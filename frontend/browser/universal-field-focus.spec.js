const { test, expect } = require('@playwright/test');

/* DP 1.0.14 universal field language (ui-universal-language.css), in dark and
 * light themes: a focused field shows its focus ring over its resting
 * material, and a field holding an action (.dp-action-field) is one surface
 * -- its own border, material and focus ring around a bare control. The light
 * resting inset and control surface add no specificity of their own, so
 * neither outranks the focus ring nor reaches the bare control. Shown on
 * Quick Add (Dashboard), the Activity Log search and Settings' Download Folder
 * (Browse); the speed-cap menu's controls keep their own light material. */

const TRANSPARENT = 'rgba(0, 0, 0, 0)';

async function open(page) {
  await page.route(/fonts\.(googleapis|gstatic)\.com/, route => route.abort());
  await page.route('**/api/events/stream', () => {});
  await page.setViewportSize({width: 1440, height: 900});
  await page.goto('/');
  await page.waitForFunction(() => window.DPTheme && document.getElementById('s-total').textContent !== '—');
}

const show = (page, view) => page.evaluate(view => nav(document.querySelector(`#sidebar .nav-item[data-view="${view}"]:not([data-title])`)), view);
const material = (page, selector) => page.locator(selector).first().evaluate(node => {
  const style = getComputedStyle(node);
  return {background: style.backgroundColor, shadow: style.boxShadow};
});
const tokens = page => page.evaluate(() => {
  // The tokens as the browser resolves them, through a probe that is not a field.
  const probe = document.createElement('div');
  probe.style.cssText = 'position:absolute;visibility:hidden;box-shadow:var(--dp-focus-ring);background:var(--dp-field-surface);color:var(--dp-chrome-input-surface)';
  document.body.append(probe);
  const style = getComputedStyle(probe);
  const out = {ring: style.boxShadow, field: style.backgroundColor, control: style.color};
  probe.remove();
  return out;
});

for (const theme of ['dark', 'light']) {
  test(`${theme}: focused fields show their ring; a field holding an action is one surface around a bare control`, async ({page}) => {
    await open(page);
    await page.evaluate(light => { window.DPTheme.setMode(light); document.dispatchEvent(new CustomEvent('debridpulse:theme-changed', {detail: {light}})); }, theme === 'light');
    const {ring, field} = await tokens(page);

    // Quick Add: the field (its entry) owns material and ring; the textarea is bare.
    await show(page, 'dashboard');
    const quickAdd = '#view-dashboard .direct-link-entry.dp-action-field', textarea = '#q-transfer-input';
    await expect.poll(async () => (await material(page, quickAdd)).background, 'Quick Add field surface (after the theme change settles)').toBe(field);
    expect(await material(page, textarea), 'Quick Add textarea at rest').toEqual({background: TRANSPARENT, shadow: 'none'});
    await page.locator(textarea).focus();
    expect((await material(page, quickAdd)).shadow, 'Quick Add focused').toBe(ring);
    expect(await material(page, textarea), 'Quick Add textarea focused').toEqual({background: TRANSPARENT, shadow: 'none'});
    await page.locator(textarea).blur();

    // Activity Log search: a plain field; focus adds the ring over its own resting surface.
    await show(page, 'events');
    const search = '#view-events input.input';
    const rest = await material(page, search);
    await page.locator(search).first().focus();
    expect(await material(page, search), 'Activity Log search focused').toEqual({background: rest.background, shadow: ring});
    await page.locator(search).first().blur();

    // Settings, Download Folder (Browse): one surface around a bare control.
    await show(page, 'settings');
    await page.waitForFunction(() => [...document.querySelectorAll('#view-settings .dp-action-field .btn')].some(button => button.textContent.trim() === 'Browse'));
    const compound = await page.evaluate(() => {
      const field = [...document.querySelectorAll('#view-settings .dp-action-field')].find(node => node.querySelector('.btn')?.textContent.trim() === 'Browse');
      const panel = field.closest('[role="tabpanel"], [data-panel]');
      (document.querySelector(`[aria-controls="${panel.id}"]`) || document.querySelector(`[data-tab="${panel.dataset.panel}"]`))?.click();
      field.dataset.probeField = '1';
      return Boolean(field);
    });
    expect(compound).toBe(true);
    const browse = '[data-probe-field="1"]', control = '[data-probe-field="1"] > .input';
    await expect(page.locator(control)).toBeVisible();
    await expect.poll(() => material(page, control), 'Browse field control at rest').toEqual({background: TRANSPARENT, shadow: 'none'});
    await page.locator(control).focus();
    expect((await material(page, browse)).shadow, 'Browse field focused').toBe(ring);
    expect(await material(page, control), 'Browse field control focused').toEqual({background: TRANSPARENT, shadow: 'none'});
  });
}

test('light: the speed-cap menu controls keep their own light control surface', async ({page}) => {
  await open(page);
  await page.evaluate(() => { window.DPTheme.setMode(true); document.dispatchEvent(new CustomEvent('debridpulse:theme-changed', {detail: {light: true}})); });
  const {control} = await tokens(page);
  for (const selector of ['.runtime-cap-options button', '.runtime-cap-custom input']) {
    await expect.poll(async () => (await material(page, selector)).background, selector).toBe(control);
  }
});
