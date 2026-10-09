const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Services viewport, Media Downloads identity and acquisition tunables.
 *
 * The one owning spec FILE for the keys it writes: integrations.media
 * target_resolution / video_quality / video_codec and the global
 * preferred_subtitle_language.
 * No other spec writes them; every case that changes one returns it to its
 * documented default through its canonical surface.
 */

const MEDIA = 'rgb(255, 86, 174)';                 // #FF56AE
const MULTIMETA = 'rgb(232, 121, 249)';            // #E879F9, unchanged

async function resetMedia(page) {
  const media = await page.request.patch('/api/integrations/media/configuration',
    {data: {options: {target_resolution: 'best', video_quality: 'high', video_codec: 'auto'}}});
  expect(media.ok()).toBe(true);
  const current = await (await page.request.get('/api/settings')).json();
  if (current.preferred_subtitle_language !== 'en') {
    const saved = await page.request.put('/api/settings', {data: {...current, preferred_subtitle_language: 'en'}});
    expect(saved.ok()).toBe(true);
  }
}

async function openSettings(page, tab) {
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator(`#view-settings [data-tab="${tab}"]`).click();
  await expect(page.locator(`.dp-settings-panel[data-panel="${tab}"]`)).toBeVisible();
}

const premium = page => page.locator('[data-panel="sources"] > .dp-settings-debrid-services > .dp-settings-group-body');
const network = page => page.locator('[data-panel="sources"] > .dp-settings-general-sources > .dp-settings-group-body');
const deck = page => page.locator('#view-settings .dp-settings-scroll');
const mediaCard = page => page.locator('#view-settings [data-executor-tuning="media"]');

async function geometry(page) {
  return page.evaluate(() => {
    const q = selector => document.querySelector(selector);
    const deckNode = q('#view-settings .dp-settings-scroll');
    const panel = q('[data-panel="sources"]');
    const body = q('[data-panel="sources"] > .dp-settings-general-sources > .dp-settings-group-body');
    const list = q('[data-panel="sources"] > .dp-settings-debrid-services > .dp-settings-group-body');
    const tiles = Array.from(body.querySelectorAll('.dp-settings-source-box'));
    const box = body.getBoundingClientRect();
    const style = getComputedStyle(body);
    const top = Math.round(tiles[0].getBoundingClientRect().top);
    const firstRow = tiles.filter(tile => Math.round(tile.getBoundingClientRect().top) === top);
    return {
      deckOverflow: deckNode.scrollHeight - deckNode.clientHeight,
      panelBottom: panel.getBoundingClientRect().bottom,
      networkBottom: q('[data-panel="sources"] > .dp-settings-general-sources').getBoundingClientRect().bottom,
      bodyTop: box.top + parseFloat(style.paddingTop), bodyBottom: box.bottom - parseFloat(style.paddingBottom),
      rowBottom: Math.max(...firstRow.map(tile => tile.getBoundingClientRect().bottom)),
      rowTop: top, tiles: tiles.length, perRow: firstRow.length,
      secondRowTop: tiles.length > firstRow.length ? tiles[firstRow.length].getBoundingClientRect().top : null,
      networkScrolls: body.scrollHeight > body.clientHeight + 1,
      premiumScrolls: list.scrollHeight > list.clientHeight + 1,
      premiumOverflowY: getComputedStyle(list).overflowY,
    };
  });
}

test.afterAll(async ({browser}) => {
  const page = await browser.newPage();
  await resetMedia(page);
  await page.close();
});

// --- Scope A: the Media Downloads accent ---------------------------------------------

test('the Media Downloads accent is #FF56AE on every identity surface, Multimeta untouched', async ({page}) => {
  await openSettings(page, 'sources');
  const chip = protocol => page.locator(`[data-panel="sources"] .dp-settings-protocol-chip[data-protocol="${protocol}"]`)
    .first().evaluate(node => getComputedStyle(node).getPropertyValue('--dp-protocol-color').trim().toLowerCase());
  expect(await chip('media')).toBe('#ff56ae');
  expect(await chip('multimeta')).toBe('#e879f9');
  await page.locator('#view-settings [data-tab="downloads"]').click();
  expect(await mediaCard(page).locator('.dp-settings-protocol-chip[data-protocol="media"]')
    .evaluate(node => getComputedStyle(node).getPropertyValue('--dp-protocol-color').trim().toLowerCase()))
    .toBe('#ff56ae');
  // The transfer-row provider badge of a Media Download, and an error badge beside it.
  const badges = await page.evaluate(() => {
    const host = document.createElement('div');
    host.innerHTML = '<span class="dp-provider-chip" data-provider-theme="hot-rose">Media Download</span>'
      + '<span class="dp-provider-chip">Other</span><span class="badge badge-error">Error</span>';
    document.body.appendChild(host);
    const [media, other, error] = Array.from(host.children, node => getComputedStyle(node).color);
    host.remove();
    return {media, other, error};
  });
  expect(badges.media).toBe(MEDIA);
  expect(badges.other).not.toBe(MEDIA);
  expect(badges.error).not.toBe(MEDIA);
  const glyph = await (await page.request.get('/icons/lucide/monitor-down.svg')).text();
  expect(glyph).toContain('stroke="#FF56AE"');
  expect(MEDIA).not.toBe(MULTIMETA);
});

// --- Scope B: Services scroll ownership ------------------------------------------------

test('desktop: the deck is fixed, Network Sources is pinned with one full row, each list scrolls alone',
  async ({page}) => {
    await page.setViewportSize({width: 1000, height: 900});
    await openSettings(page, 'sources');
    await expect.poll(async () => (await geometry(page)).bodyBottom).toBeGreaterThan(0);
    const facts = await geometry(page);
    expect(facts.deckOverflow, 'the deck itself scrolls').toBeLessThanOrEqual(1);
    expect(Math.abs(facts.networkBottom - facts.panelBottom), 'Network Sources is not pinned').toBeLessThanOrEqual(1);
    // Exactly one full row: the first row ends inside the viewport, the next begins below it.
    expect(facts.rowTop).toBeGreaterThanOrEqual(facts.bodyTop - 1);
    expect(facts.rowBottom).toBeLessThanOrEqual(facts.bodyBottom + 1);
    expect(facts.tiles).toBeGreaterThan(facts.perRow);              // 7 protocols wrap at this width
    expect(facts.secondRowTop).toBeGreaterThanOrEqual(facts.bodyBottom - 1);
    expect(facts.networkScrolls && facts.premiumScrolls).toBe(true);
    expect(facts.premiumOverflowY).toBe('auto');

    // A wheel over each list moves only that list.
    const wheel = async (locator) => {
      const box = await locator.boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + Math.min(40, box.height / 2));
      await page.mouse.wheel(0, 300);
    };
    await wheel(premium(page));
    await expect.poll(() => premium(page).evaluate(node => node.scrollTop)).toBeGreaterThan(0);
    expect(await network(page).evaluate(node => node.scrollTop)).toBe(0);
    expect(await deck(page).evaluate(node => node.scrollTop)).toBe(0);
    const premiumTop = await premium(page).evaluate(node => node.scrollTop);
    await wheel(network(page));
    await expect.poll(() => network(page).evaluate(node => node.scrollTop)).toBeGreaterThan(0);
    expect(await premium(page).evaluate(node => node.scrollTop)).toBe(premiumTop);
    expect(await deck(page).evaluate(node => node.scrollTop)).toBe(0);

    // Keyboard: focusing a control in a hidden row brings it into view inside Network only.
    await network(page).evaluate(node => { node.scrollTop = 0; });
    const lastToggle = network(page).locator('.dp-settings-source-box').last().locator('input, button').first();
    await lastToggle.focus();
    await expect.poll(() => network(page).evaluate(node => node.scrollTop)).toBeGreaterThan(0);
    await expect(lastToggle).toBeInViewport();
    expect(await deck(page).evaluate(node => node.scrollTop)).toBe(0);

    // Leaving for another tab and returning keeps both list positions; the
    // other tab keeps the deck as its scroll owner.
    const before = {premium: await premium(page).evaluate(node => node.scrollTop),
                    network: await network(page).evaluate(node => node.scrollTop)};
    await page.locator('#view-settings [data-tab="downloads"]').click();
    const downloads = await page.evaluate(() => ({
      panelsMax: getComputedStyle(document.querySelector('#view-settings .dp-settings-panels')).maxHeight,
      deckScrolls: (node => node.scrollHeight > node.clientHeight + 1)(document.querySelector('#view-settings .dp-settings-scroll')),
    }));
    expect(downloads.panelsMax).toBe('none');
    expect(downloads.deckScrolls).toBe(true);
    await page.locator('#view-settings [data-tab="sources"]').click();
    expect(await premium(page).evaluate(node => node.scrollTop)).toBe(before.premium);
    expect(await network(page).evaluate(node => node.scrollTop)).toBe(before.network);
  });

test('a short viewport scrolls the deck by only its shortfall and keeps every control reachable',
  async ({page}) => {
    await page.setViewportSize({width: 1000, height: 420});
    await openSettings(page, 'sources');
    await expect.poll(async () => (await geometry(page)).bodyBottom).toBeGreaterThan(0);
    const facts = await geometry(page);
    expect(facts.deckOverflow).toBeGreaterThan(0);                  // the bounded fallback engages...
    expect(facts.deckOverflow).toBeLessThan(400);                   // ...by the shortfall, not a whole page
    expect(facts.rowBottom - facts.rowTop).toBeLessThanOrEqual(facts.bodyBottom - facts.bodyTop + 1);
    const lastToggle = network(page).locator('.dp-settings-source-box').last().locator('input, button').first();
    await lastToggle.scrollIntoViewIfNeeded();
    await expect(lastToggle).toBeInViewport();
  });

test('a narrow width keeps one full Network row with its own scroll', async ({page}) => {
  await openSettings(page, 'sources');
  await page.setViewportSize({width: 700, height: 900});
  await expect.poll(async () => (await geometry(page)).bodyBottom).toBeGreaterThan(0);
  const facts = await geometry(page);
  expect(facts.rowBottom).toBeLessThanOrEqual(facts.bodyBottom + 1);
  expect(facts.secondRowTop).toBeGreaterThanOrEqual(facts.bodyBottom - 1);
  expect(facts.networkScrolls).toBe(true);
  expect(await page.evaluate(() =>
    document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)).toBe(true);
});

// --- Scope C: the three Media Downloads preferences -------------------------------------

test('Target Resolution, one bordered Video Preferences group, and the subtitle language', async ({page}) => {
  await resetMedia(page);
  await page.setViewportSize({width: 1440, height: 1000});
  await openSettings(page, 'downloads');
  await mediaCard(page).locator('.dp-settings-disclosure').first().click();
  const fields = mediaCard(page).locator('.dp-settings-field');
  await expect(fields).toHaveCount(4);
  await expect(fields.locator('.form-label')).toHaveText(
    ['Target Resolution', 'Video Quality', 'Preferred Video Codec', 'Preferred Subtitle Language']);
  // The two video preferences: one related group, drawn with the shared outline.
  const group = mediaCard(page).locator('.dp-settings-tuning-group');
  await expect(group).toHaveCount(1);
  await expect(group.locator('.dp-settings-field .form-label')).toHaveText(['Video Quality', 'Preferred Video Codec']);
  expect(await group.evaluate(node => getComputedStyle(node).outlineStyle !== 'none'
    && parseFloat(getComputedStyle(node).outlineWidth) > 0)).toBe(true);
  await expect(mediaCard(page).locator('#dp-settings-field-media-video-quality option')).toHaveText(['High', 'Normal', 'Low']);
  await expect(mediaCard(page).locator('#dp-settings-field-media-video-codec option'))
    .toHaveText(['Auto', 'AV1', 'HEVC (H.265)', 'H.264']);
  // Defaults: Auto + High, the native selection.
  await expect(mediaCard(page).locator('#dp-settings-field-media-video-quality')).toHaveValue('high');
  await expect(mediaCard(page).locator('#dp-settings-field-media-video-codec')).toHaveValue('auto');
  await expect(mediaCard(page).locator('#dp-settings-field-media-target-resolution')).toHaveValue('best');
  await expect(mediaCard(page).locator('#dp-settings-field-media-preferred-subtitle-language')).toHaveValue('en');
  await expect(group).toContainText('DebridPulse never re-encodes media');
  await expect(mediaCard(page)).not.toContainText(/Embed Subtitles|Subtitle Acquisition|separate files/i);
  await expect(mediaCard(page).locator('input[type="checkbox"]')).toHaveCount(0);
});

test('every control saves, survives a reload, and the one subtitle key agrees in both places', async ({page}) => {
  await resetMedia(page);
  await openSettings(page, 'downloads');
  await mediaCard(page).locator('.dp-settings-disclosure').first().click();
  const commit = async (locator, value) => {
    const saved = page.waitForResponse(response => ['PATCH', 'PUT'].includes(response.request().method())
      && /\/api\/(integrations\/media\/configuration|settings)$/.test(new URL(response.url()).pathname));
    if (await locator.evaluate(node => node.tagName) === 'SELECT') await locator.selectOption(value);
    else await locator.fill(value);
    await locator.blur();
    expect((await saved).ok()).toBe(true);
  };
  await commit(mediaCard(page).locator('#dp-settings-field-media-target-resolution'), '720');
  await commit(mediaCard(page).locator('#dp-settings-field-media-video-quality'), 'low');
  await commit(mediaCard(page).locator('#dp-settings-field-media-video-codec'), 'hevc');
  await commit(mediaCard(page).locator('#dp-settings-field-media-preferred-subtitle-language'), 'de');
  // The same key, shown under Download Behavior & Limits, follows at once.
  const global = page.locator('#dp-settings-field-preferred-subtitle-language');
  await expect(global).toHaveValue('de');

  const stored = await (await page.request.get('/api/settings')).json();
  expect(stored.preferred_subtitle_language).toBe('de');
  expect(stored.integrations.media.options).toMatchObject({target_resolution: '720', video_quality: 'low',
                                                            video_codec: 'hevc'});

  await page.reload();
  await openSettings(page, 'downloads');
  await mediaCard(page).locator('.dp-settings-disclosure').first().click();
  await expect(mediaCard(page).locator('#dp-settings-field-media-target-resolution')).toHaveValue('720');
  await expect(mediaCard(page).locator('#dp-settings-field-media-video-quality')).toHaveValue('low');
  await expect(mediaCard(page).locator('#dp-settings-field-media-video-codec')).toHaveValue('hevc');
  await expect(mediaCard(page).locator('#dp-settings-field-media-preferred-subtitle-language')).toHaveValue('de');

  // Changed from the other entry point, the Media card follows too.
  await page.locator('.dp-settings-subsection', {hasText: 'Advanced Settings'}).locator('.dp-settings-disclosure').click();
  await commit(global, 'pt-br');
  await expect(mediaCard(page).locator('#dp-settings-field-media-preferred-subtitle-language')).toHaveValue('pt-br');
  expect((await (await page.request.get('/api/settings')).json()).preferred_subtitle_language).toBe('pt-br');
  await resetMedia(page);
});
