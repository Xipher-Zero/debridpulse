const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Downloads -> Download Behavior & Limits -> Advanced Settings.
 *
 * The one owning spec FILE for the four transfer_policy keys it touches
 * (material_checkpoint_interval_seconds, graceful_stop_timeout_seconds,
 * private_lan_connections, skip_private_lan_confirmation). Each case sets the
 * state it needs itself, and the file returns every key to its documented
 * default through the canonical scoped surface -- never by replaying a stale
 * snapshot.
 */

const DEFAULTS = {
  material_checkpoint_interval_seconds: 5, graceful_stop_timeout_seconds: 10,
  private_lan_connections: false, skip_private_lan_confirmation: false,
};

async function resetPolicy(page) {
  const response = await page.request.patch('/api/transfer-policy', {data: DEFAULTS});
  expect(response.ok()).toBe(true);
}

async function openDownloads(page) {
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator('#view-settings [data-tab="downloads"]').click();
  await expect(page.locator('.dp-settings-panel[data-panel="downloads"]')).toBeVisible();
}

const card = page => page.locator('.dp-settings-download-engine-card');
const section = page => card(page).locator('.dp-settings-subsection', {hasText: 'Advanced Settings'});

test.afterAll(async ({browser}) => {
  const page = await browser.newPage();
  await resetPolicy(page);
  await page.close();
});

test('Advanced Settings is collapsed, its chevron sits beside the title, and four cards flow in the shared grid',
  async ({page}) => {
    await resetPolicy(page);
    await page.setViewportSize({width: 1440, height: 1000});
    await openDownloads(page);
    await expect(card(page).locator('.card-title').first()).toContainText('Download Behavior & Limits');
    const chevron = section(page).locator('.dp-settings-disclosure');
    await expect(chevron).toHaveAttribute('aria-expanded', 'false');
    await expect(section(page).locator('.dp-settings-subsection-body')).toBeHidden();

    const adjacency = await section(page).locator('.dp-settings-subsection-header').evaluate(header => {
      const title = header.querySelector('.dp-settings-subsection-title').getBoundingClientRect();
      const button = header.querySelector('.dp-settings-disclosure').getBoundingClientRect();
      return {gap: button.left - title.right, headerRight: header.getBoundingClientRect().right,
              buttonRight: button.right};
    });
    expect(adjacency.gap).toBeGreaterThanOrEqual(0);
    expect(adjacency.gap).toBeLessThanOrEqual(16);
    expect(adjacency.headerRight - adjacency.buttonRight).toBeGreaterThan(200);  // not pushed to the far edge

    await chevron.click();
    await expect(chevron).toHaveAttribute('aria-expanded', 'true');
    const grid = section(page).locator('.dp-settings-tuning-grid');
    await expect(grid).toHaveAttribute('data-tuning-lanes', '4');
    // The two local-network cells are one shared tuning relation: outlined
    // while all four lanes hold, withdrawn (no box, no outline) once the grid wraps.
    const group = grid.locator(':scope > .dp-settings-tuning-group');
    await expect(group).toHaveCount(1);
    await expect(group).toHaveAttribute('data-tuning-span', '2');
    await expect(group.locator('[data-setting="private_lan_connections"]')).toHaveCount(1);
    await expect(group.locator('[data-setting="skip_private_lan_confirmation"]')).toHaveCount(1);
    await expect(group.locator('[data-setting="material_checkpoint_interval_seconds"]')).toHaveCount(0);
    const wide = await group.evaluate(el => ({display: getComputedStyle(el).display,
                                             outline: getComputedStyle(el).outlineStyle}));
    expect(wide.display).toBe('grid');
    expect(wide.outline).not.toBe('none');
    const cells = await grid.evaluate(el => Array.from(el.querySelectorAll('.dp-settings-field')).map(child => {
      const box = child.getBoundingClientRect();
      return {top: Math.round(box.top), left: box.left, width: box.width, text: child.textContent};
    }));
    expect(cells).toHaveLength(4);
    for (const label of ['Material Checkpoint Interval', 'Graceful Stop Timeout', 'Local Network Connections',
      'Skip Local Connection Confirmation']) {
      expect(cells.some(cell => cell.text.includes(label)), label).toBe(true);
    }
    // Horizontal flow: at least two cards share the first row, left to right,
    // and none of them is stretched edge-to-edge.
    const firstRow = cells.filter(cell => Math.abs(cell.top - cells[0].top) < 4);
    expect(firstRow.length).toBeGreaterThan(1);
    expect(firstRow[1].left).toBeGreaterThan(firstRow[0].left);
    const gridWidth = await grid.evaluate(el => el.getBoundingClientRect().width);
    for (const cell of cells) expect(cell.width).toBeLessThan(gridWidth * 0.9);

    // Narrow: the grid wraps and the relation is simply not drawn.
    await page.setViewportSize({width: 700, height: 1000});
    await expect.poll(() => group.evaluate(el => getComputedStyle(el).display)).toBe('contents');
    expect(await group.evaluate(el => getComputedStyle(el).outlineStyle)).toBe('none');
  });

test('Local Network Connections persists immediately and gates Skip Confirmation without erasing it',
  async ({page}) => {
    // Skip was chosen earlier while LAN access is off: stored, but inactive.
    const seeded = await page.request.patch('/api/transfer-policy', {data: {
      private_lan_connections: false, skip_private_lan_confirmation: true}});
    expect(seeded.ok()).toBe(true);
    await openDownloads(page);
    await section(page).locator('.dp-settings-disclosure').click();
    const lan = page.locator('[data-setting="private_lan_connections"]');
    const skip = page.locator('[data-setting="skip_private_lan_confirmation"]');
    await expect(lan).not.toBeChecked();
    await expect(skip).toBeDisabled();
    await expect(skip).toBeChecked();  // the stored preference is shown, not erased
    await expect(skip.locator('xpath=ancestor::div[contains(@class,"dp-settings-field")][1]'))
      .toHaveClass(/\bis-disabled\b/);

    const accepted = page.waitForResponse(response => response.url().endsWith('/api/transfer-policy')
      && response.request().method() === 'PATCH');
    await lan.locator('xpath=ancestor::div[contains(@class,"dp-settings-field")][1]//label[contains(@class,"dp-settings-engine-tuning-toggle-control")]').click();
    const response = await accepted;
    expect(response.ok()).toBe(true);
    const body = await response.json();
    expect(body.private_lan_connections).toBe(true);
    expect(body.skip_private_lan_confirmation).toBe(true);  // re-enabling restores the old preference
    await expect(skip).toBeEnabled();
    await expect(skip).toBeChecked();
    expect(JSON.parse(response.request().postData())).toEqual({private_lan_connections: true});
  });

test('the numeric cards commit at the changed-blur boundary through the canonical scope', async ({page}) => {
  await resetPolicy(page);
  await openDownloads(page);
  await section(page).locator('.dp-settings-disclosure').click();
  const field = page.locator('[data-setting="material_checkpoint_interval_seconds"]');
  await expect(field).toHaveValue('5');
  const accepted = page.waitForResponse(response => response.url().endsWith('/api/transfer-policy')
    && response.request().method() === 'PATCH');
  await field.fill('12');
  await field.blur();
  const response = await accepted;
  expect(JSON.parse(response.request().postData())).toEqual({material_checkpoint_interval_seconds: 12});
  expect((await response.json()).material_checkpoint_interval_seconds).toBe(12);
});

test('an explicit private-LAN link waits for the per-transfer confirmation; Cancel admits nothing and writes no setting',
  async ({page}) => {
    const policy = await page.request.patch('/api/transfer-policy', {data: {
      private_lan_connections: true, skip_private_lan_confirmation: false}});
    expect(policy.ok()).toBe(true);
    const link = 'http://192.168.77.5/dp-lan-confirmation.bin';
    const transferCount = async () => {
      const response = await page.request.get('/api/torrents?limit=200');
      const listed = await response.json();
      return (Array.isArray(listed) ? listed : listed.items || listed.torrents || [])
        .filter(item => String(item.name || item.display_name || '').includes('dp-lan-confirmation')).length;
    };
    const before = await transferCount();
    await page.goto('/');
    const input = page.locator('#q-transfer-input');
    await input.fill(link);
    await page.locator('#btn-add-transfer').click();

    const dialog = page.locator('.dp-modal-dialog');
    await expect(dialog.locator('.dp-modal-title')).toHaveText('Connect to a local network address?');
    await expect(dialog).toContainText('private network');
    await expect(dialog).toContainText('this transfer only');
    await expect(dialog.locator('input[type="checkbox"]')).toHaveCount(0);  // no "remember" in the modal
    await dialog.locator('[data-modal-cancel]').click();
    await expect(dialog).toHaveCount(0);
    await expect(input).toHaveValue(link);
    expect(await transferCount()).toBe(before);
    const unchanged = await (await page.request.get('/api/transfer-policy')).json();
    expect(unchanged.skip_private_lan_confirmation).toBe(false);
    expect(unchanged.private_lan_connections).toBe(true);

    const submitted = page.waitForRequest(request => request.url().endsWith('/api/links/add')
      && JSON.parse(request.postData() || '{}').allow_local_network === true);
    await page.locator('#btn-add-transfer').click();
    await dialog.locator('[data-modal-accept]').click();
    const allowed = await submitted;
    expect(JSON.parse(allowed.postData()).links).toEqual([link]);
    const admitted = await (await allowed.response()).json();
    const id = admitted.torrent_id || admitted.id;
    expect(id).toBeTruthy();
    expect((await (await page.request.get('/api/transfer-policy')).json()).skip_private_lan_confirmation).toBe(false);
    await page.request.delete(`/api/torrents/${id}`);
  });
