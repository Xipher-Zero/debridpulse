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
