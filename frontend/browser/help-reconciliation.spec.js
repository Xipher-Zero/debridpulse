const { test, expect } = require('@playwright/test');

/* 1.0.13 Help reconciliation: every Help tab still navigates and renders, and
 * the License tab offers exactly the three surviving bundled documents --
 * GPL-2.0-or-later, Source offer and Third-party licenses -- each opening its
 * real bundled text, closing with Escape back to its button, and saying so
 * when it cannot load. The Attribution notice and the upstream MIT license are
 * no longer Help documents (they still ship with the image). */

const TABS = ['quickstart', 'using', 'howitworks', 'services', 'engines', 'settings', 'trouble', 'license'];
const EXPECTED_DOCUMENTS = [
  ['gpl', 'Read GPL-2.0-or-later', /GNU GENERAL PUBLIC LICENSE/i],
  ['source-offer', 'Source offer', /source/i],
  ['third-party', 'Third-party licenses', /license/i],
];

async function openHelp(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="help"]').evaluate(element => element.click());
  await expect(page.locator('#view-help .dp-help-tabs')).toBeVisible();
}

test('every Help tab renders its own populated section, by click and by keyboard', async ({page}) => {
  await openHelp(page);
  for (const tab of TABS) {
    await page.locator(`#view-help .dp-help-tab[data-tab="${tab}"]`).click();
    const panel = page.locator(`#dp-help-panel-${tab}`);
    await expect(panel).toBeVisible();
    await expect(panel.locator('h2')).toHaveCount(1);
    expect((await panel.innerText()).length, tab).toBeGreaterThan(400);
  }
  // Arrow keys walk the tab strip in order and wrap.
  await page.locator('#view-help .dp-help-tab[data-tab="license"]').focus();
  await page.keyboard.press('ArrowRight');
  await expect(page.locator('#view-help .dp-help-tab[data-tab="quickstart"]')).toBeFocused();
  await expect(page.locator('#dp-help-panel-quickstart')).toBeVisible();
  await page.keyboard.press('End');
  await expect(page.locator('#view-help .dp-help-tab[data-tab="license"]')).toHaveAttribute('aria-selected', 'true');
});

test('the License tab offers exactly the three surviving documents, each opening its bundled text', async ({page}) => {
  await openHelp(page);
  await page.locator('#view-help .dp-help-tab[data-tab="license"]').click();
  const panel = page.locator('#dp-help-panel-license');
  const buttons = panel.locator('[data-legal-document]');
  expect(await buttons.evaluateAll(nodes => nodes.map(node => [node.dataset.legalDocument, node.textContent.trim()])))
    .toEqual(EXPECTED_DOCUMENTS.map(([id, label]) => [id, label]));
  const text = (await panel.innerText()).toLowerCase();
  expect(text).not.toContain('attribution notice');
  expect(text).not.toContain('upstream mit');

  for (const [id, label, content] of EXPECTED_DOCUMENTS) {
    const opener = panel.locator(`[data-legal-document="${id}"]`);
    await opener.focus();
    await opener.click();
    const dialog = page.locator('.dp-help-legal-dialog');
    await expect(dialog).toBeVisible();
    await expect(dialog.locator('.dp-help-legal-latest')).not.toHaveText('Loading bundled document…');
    await expect(dialog.locator('.dp-help-legal-latest')).not.toHaveClass(/is-error/);
    await expect(dialog.locator('.dp-help-legal-document')).toContainText(content);
    expect(label.length).toBeGreaterThan(0);
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(opener).toBeFocused();
  }
});

test('a bundled document that cannot load says so and still closes', async ({page}) => {
  await openHelp(page);
  await page.route('**/api/legal-documents/source-offer', route =>
    route.fulfill({status: 500, contentType: 'application/json', body: JSON.stringify({detail: 'unavailable'})}));
  await page.locator('#view-help .dp-help-tab[data-tab="license"]').click();
  const opener = page.locator('[data-legal-document="source-offer"]');
  await opener.click();
  const dialog = page.locator('.dp-help-legal-dialog');
  await expect(dialog.locator('.dp-help-legal-latest')).toHaveText('The bundled document could not be loaded.');
  await dialog.locator('.dp-help-legal-close').click();
  await expect(dialog).toHaveCount(0);
  await expect(opener).toBeFocused();
});

test('the License introduction reads as approved, with only the warranty centred and bold', async ({page}) => {
  await openHelp(page);
  await page.locator('#view-help .dp-help-tab[data-tab="license"]').click();
  const paragraphs = page.locator('#dp-help-panel-license .dp-help-copy--lead > p');
  await expect(paragraphs).toHaveText([
    'DebridPulse is Copyright © 2026 Chris Moore and is distributed under the GNU General Public License v2.0 or later (GPL-2.0-or-later).',
    'The GPL permits you to use, study, modify, and redistribute DebridPulse under its terms. If you redistribute DebridPulse '
      + 'or a modified version, applicable licensing and source-code requirements must be followed. The complete license text governs the exact terms.',
    'DebridPulse is provided without warranty, as specified in the license.',
  ]);
  const alignment = await paragraphs.evaluateAll(nodes => nodes.map(node => getComputedStyle(node).textAlign));
  expect(alignment.slice(0, 2).every(value => value === 'start' || value === 'left')).toBe(true);
  expect(alignment[2]).toBe('center');
  await expect(paragraphs.nth(2).locator('b')).toHaveText('DebridPulse is provided without warranty, as specified in the license.');
  await expect(paragraphs.nth(0).locator('b')).toHaveText('GNU General Public License v2.0 or later');
  await expect(paragraphs.nth(0).locator('code')).toHaveText('GPL-2.0-or-later');
});

/* A Help accordion's body rolls down as it opens and up before it closes
 * (the height is sampled each frame), the native `open` state follows, and
 * fast repeated toggles settle with nothing left behind. */
async function rollSamples(page, details, act) {
  // Sampling starts from the click itself, so the whole motion is recorded
  // however long the pointer action takes to land.
  await details.evaluate(element => {
    const body = element.querySelector(':scope > .dp-help-accordion-body');
    window.__roll = [];
    window.__rollDone = false;
    element.querySelector(':scope > summary').addEventListener('click', () => {
      const start = performance.now();
      const tick = () => {
        window.__roll.push({height: body.getBoundingClientRect().height, open: element.open});
        if (performance.now() - start < 450) requestAnimationFrame(tick);
        else window.__rollDone = true;
      };
      requestAnimationFrame(tick);
    }, {once: true, capture: true});
  });
  await act();
  await page.waitForFunction(() => window.__rollDone === true);
  return page.evaluate(() => window.__roll);
}

test('Help accordions roll down and up, by mouse and keyboard, and settle after fast toggles', async ({page}) => {
  await openHelp(page);
  await page.locator('#view-help .dp-help-tab[data-tab="howitworks"]').click();
  const details = page.locator('#dp-help-panel-howitworks details.dp-help-accordion').nth(1);
  const summary = details.locator(':scope > summary');
  const body = details.locator(':scope > .dp-help-accordion-body');
  await expect(details).not.toHaveAttribute('open', '');

  const opening = await rollSamples(page, details, () => summary.click());
  await expect(details).toHaveAttribute('open', '');
  await expect.poll(() => body.evaluate(element => element.getAnimations().length)).toBe(0);
  const full = await body.evaluate(element => element.getBoundingClientRect().height);
  expect(opening.filter(frame => frame.open && frame.height > 1 && frame.height < full - 1).length).toBeGreaterThan(0);

  const closing = await rollSamples(page, details, () => summary.click());
  await expect(details).not.toHaveAttribute('open', '');
  // It was still open, partly rolled up, before it closed.
  expect(closing.filter(frame => frame.open && frame.height > 1 && frame.height < full - 1).length).toBeGreaterThan(0);

  // Keyboard: Enter opens, Space closes, focus stays on the summary.
  await summary.focus();
  await page.keyboard.press('Enter');
  await expect(details).toHaveAttribute('open', '');
  await expect.poll(() => body.evaluate(element => element.getAnimations().length)).toBe(0);
  await page.keyboard.press(' ');
  await expect(details).not.toHaveAttribute('open', '');
  await expect(summary).toBeFocused();

  // Five fast clicks end open; nothing is left on the body.
  for (let index = 0; index < 5; index += 1) await summary.click();
  await expect(details).toHaveAttribute('open', '');
  // Settled is the motion owner's finish handler having run, which the browser
  // delivers only after the animation has left getAnimations(): the whole
  // settled state is observed together, never one before the other.
  await expect.poll(() => body.evaluate(element => ({animations: element.getAnimations().length,
    style: element.getAttribute('style') || '', hidden: element.hidden, inert: element.inert,
    fits: Math.abs(element.getBoundingClientRect().height - element.scrollHeight) < 1})))
    .toEqual({animations: 0, style: '', hidden: false, inert: false, fits: true});
});

test('with reduced motion a Help accordion opens and closes at once', async ({browser}) => {
  const context = await browser.newContext({reducedMotion: 'reduce'});
  const page = await context.newPage();
  await openHelp(page);
  await page.locator('#view-help .dp-help-tab[data-tab="trouble"]').click();
  const details = page.locator('#dp-help-panel-trouble details.dp-help-accordion').nth(2);
  await details.locator(':scope > summary').click();
  await expect(details).toHaveAttribute('open', '');
  expect(await details.locator(':scope > .dp-help-accordion-body').evaluate(element => element.getAnimations().length)).toBe(0);
  await details.locator(':scope > summary').click();
  await expect(details).not.toHaveAttribute('open', '');
  await context.close();
});

test('without the Web Animations API a Help accordion is plain native details', async ({browser}) => {
  const context = await browser.newContext();
  await context.addInitScript(() => { delete Element.prototype.animate; });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await openHelp(page);
  await page.locator('#view-help .dp-help-tab[data-tab="howitworks"]').click();
  const details = page.locator('#dp-help-panel-howitworks details.dp-help-accordion').nth(1);
  await details.locator(':scope > summary').click();
  await expect(details).toHaveAttribute('open', '');
  await expect(details.locator(':scope > .dp-help-accordion-body')).toBeVisible();
  await details.locator(':scope > summary').click();
  await expect(details).not.toHaveAttribute('open', '');
  expect(errors).toEqual([]);
  await context.close();
});

test('the six-step flow is one group centred in its card, with aligned columns', async ({page}) => {
  for (const width of [1440, 390]) {
    await page.setViewportSize({width, height: 900});
    await openHelp(page);
    await page.locator('#view-help .dp-help-tab[data-tab="howitworks"]').click();
    // The tab's slide commits the panel a frame or two after the click; the
    // live layout is final from that commit on, never transformed by the slide.
    await expect(page.locator('#dp-help-panel-howitworks')).toBeVisible();
    const geometry = await page.locator('#dp-help-panel-howitworks .dp-help-pipeline').evaluate(pipe => {
      const parent = pipe.parentElement.getBoundingClientRect();
      const style = getComputedStyle(pipe.parentElement);
      const box = pipe.getBoundingClientRect();
      const rows = [...pipe.querySelectorAll('.dp-help-pipeline-row')];
      const lefts = selector => new Set(rows.map(row => Math.round(row.querySelector(selector).getBoundingClientRect().left))).size;
      return {
        left: box.left - (parent.left + parseFloat(style.paddingLeft)),
        right: (parent.right - parseFloat(style.paddingRight)) - box.right,
        rows: rows.length, nodeColumns: lefts('.dp-help-pipeline-node'), textColumns: lefts('.dp-help-copy'),
        overflow: document.documentElement.scrollWidth > innerWidth + 1,
      };
    });
    expect(geometry.rows).toBe(6);
    expect(geometry.overflow).toBe(false);
    expect(geometry.nodeColumns).toBe(1);
    expect(geometry.textColumns).toBe(1);
    if (width === 1440) {
      expect(geometry.left).toBeGreaterThan(40);
      expect(Math.abs(geometry.left - geometry.right)).toBeLessThanOrEqual(2);
    }
  }
});
