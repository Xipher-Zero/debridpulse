const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Services viewport, Media Downloads identity and acquisition tunables.
 *
 * The one owning spec FILE for the keys it writes: integrations.media
 * target_resolution / video_quality / video_codec / youtube_player_clients and the
 * global preferred_subtitle_language.
 * No other spec writes them; every case that changes one returns it to its
 * documented default through its canonical surface.
 */

const MEDIA = 'rgb(255, 86, 174)';                 // #FF56AE
const MULTIMETA = 'rgb(232, 121, 249)';            // #E879F9, unchanged

async function resetMedia(page) {
  const media = await page.request.patch('/api/integrations/media/configuration',
    {data: {options: {target_resolution: 'best', video_quality: 'high', video_codec: 'auto',
                     concurrent_fragment_downloads: 1, youtube_player_clients: []}}});
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
const subtitle = page => page.locator('#dp-settings-field-preferred-subtitle-language');
const picker = page => mediaCard(page).locator('.dp-settings-field', {has: subtitle(page)}).locator('.dp-dropdown__trigger');
const menu = page => page.locator('#dp-dropdown-layer');
const choices = page => page.locator('#dp-dropdown-list .dp-dropdown__option');

async function openMedia(page) {
  await openSettings(page, 'downloads');
  const disclosure = mediaCard(page).locator('.dp-settings-disclosure').first();
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
  await picker(page).scrollIntoViewIfNeeded();
}

// Network Sources is closed on arrival (DP 1.0.14): open it, and let its
// opening motion settle before its row is measured.
async function openNetwork(page) {
  const chip = page.locator('.dp-settings-general-sources .dp-settings-disclosure');
  if ((await chip.getAttribute('aria-expanded')) !== 'true') await chip.click();
  await expect(network(page)).toBeVisible();
  await page.waitForFunction(() => !document.querySelector(
    '[data-panel="sources"] > .dp-settings-general-sources > .dp-settings-group-body').getAnimations().length);
}

async function saveSubtitle(page, code) {
  const current = await (await page.request.get('/api/settings')).json();
  const saved = await page.request.put('/api/settings', {data: {...current, preferred_subtitle_language: code}});
  expect(saved.ok()).toBe(true);
}

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
    // Built by the one badge identity owner, from the provider id, as rows do.
    const identity = window.DPTransferSourcePresentation.providerBadgeIdentity('media');
    host.innerHTML = `<span class="dp-provider-chip"${identity.attributes}>Media Download</span>`
      + '<span class="dp-provider-chip">Other</span><span class="badge badge-error">Error</span>';
    document.body.appendChild(host);
    // Colours as drawn (sRGB), whatever notation the computed value uses.
    const srgb = colour => {
      const context = document.createElement('canvas').getContext('2d');
      context.fillStyle = colour;
      context.fillRect(0, 0, 1, 1);
      const [r, g, b] = context.getImageData(0, 0, 1, 1).data;
      return `rgb(${r}, ${g}, ${b})`;
    };
    const [media, other, error] = Array.from(host.children, node => srgb(getComputedStyle(node).color));
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
    await openNetwork(page);
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

/* An expanded provider card is content of the Premium list, never part of its
 * minimum: with Usenet open and its last server's Advanced settings expanded,
 * the list's viewport stays inside its card, its end is reachable, and
 * Network Sources stays pinned. Adding a server card and opening a disclosure
 * are page-local; nothing is written. */
test('expanded Usenet servers keep the end of the Premium list reachable, Network Sources pinned',
  async ({page}) => {
    await page.setViewportSize({width: 1600, height: 935});
    await openSettings(page, 'sources');
    await openNetwork(page);
    await expect.poll(async () => (await geometry(page)).bodyBottom).toBeGreaterThan(0);
    const pinned = async () => page.evaluate(() => {
      const card = document.querySelector('[data-panel="sources"] > .dp-settings-general-sources').getBoundingClientRect();
      return [Math.round(card.top), Math.round(card.bottom)];
    });
    const at = await pinned();
    const usenet = page.locator('.dp-settings-provider-card--usenet');
    const disclosure = usenet.locator('.dp-settings-disclosure').first();
    if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
    const servers = usenet.locator('[data-usenet-server-id]');
    const reach = async () => {
      await premium(page).evaluate(node => { node.scrollTop = node.scrollHeight; });
      return premium(page).evaluate(list => {
        const visible = Math.min(list.getBoundingClientRect().bottom, list.parentElement.getBoundingClientRect().bottom);
        const all = list.querySelectorAll('.dp-settings-provider-card--usenet [data-usenet-server-id]');
        const last = all[all.length - 1];
        const fields = last.querySelectorAll('.dp-usenet-advanced-body input, .dp-usenet-advanced-body select');
        return {viewportClipped: list.getBoundingClientRect().bottom > list.parentElement.getBoundingClientRect().bottom + 1,
          atEnd: Math.abs(list.scrollTop + list.clientHeight - list.scrollHeight) <= 1,
          serverReachable: last.getBoundingClientRect().bottom <= visible + 1,
          lastFieldReachable: fields.length > 0 && fields[fields.length - 1].getBoundingClientRect().bottom <= visible + 1};
      });
    };
    for (const count of [1, 4]) {
      while (await servers.count() < count) await usenet.locator('[data-usenet-action="add"]').click();
      const toggle = servers.last().locator('[data-usenet-advanced-toggle]');
      if ((await toggle.getAttribute('aria-expanded')) !== 'true') await toggle.click();
      await expect(servers.last().locator('.dp-usenet-advanced-body')).toBeVisible();
      await expect.poll(reach).toEqual({viewportClipped: false, atEnd: true, serverReachable: true, lastFieldReachable: true});
      expect((await geometry(page)).deckOverflow, 'the deck itself scrolls').toBeLessThanOrEqual(1);
      expect(await pinned()).toEqual(at);
    }
    // Collapsing gives the list back its at-rest geometry.
    await servers.last().locator('[data-usenet-advanced-toggle]').click();
    await disclosure.click();
    await expect.poll(async () => (await reach()).viewportClipped).toBe(false);
    expect((await geometry(page)).deckOverflow).toBeLessThanOrEqual(1);
    expect(await pinned()).toEqual(at);
  });

test('a short viewport scrolls the deck by only its shortfall and keeps every control reachable',
  async ({page}) => {
    await page.setViewportSize({width: 1000, height: 420});
    await openSettings(page, 'sources');
    await openNetwork(page);
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
  await openNetwork(page);
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
  await expect(fields).toHaveCount(6);
  await expect(fields.locator('.form-label')).toHaveText(
    ['Target Resolution', 'Video Quality', 'Preferred Video Codec', 'Preferred Subtitle Language',
     'Concurrent Fragment Downloads', 'YouTube Player Client']);
  // The five keep their one row; YouTube Player Client is its own set beneath.
  await expect(mediaCard(page).locator('.dp-settings-tuning-grid').first()).toHaveAttribute('data-tuning-lanes', '5');
  await expect(mediaCard(page).locator('.dp-settings-tuning-grid').nth(1)).toHaveAttribute('data-tuning-lanes', '1');
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
  await expect(subtitle(page)).toHaveValue('en');
  const fragments = mediaCard(page).locator('#dp-settings-field-media-concurrent-fragment-downloads');
  await expect(fragments).toHaveValue('1');
  await expect(fragments).toHaveAttribute('type', 'number');
  await expect(fragments).toHaveAttribute('min', '1');
  await expect(fragments).toHaveAttribute('max', '8');
  // The same standard cell as the subtitle language beside it: same field
  // grammar and the same ungrouped placement.
  expect(await fragments.evaluate(node => node.closest('.dp-settings-field').className))
    .toBe(await subtitle(page).evaluate(node => node.closest('.dp-settings-field').className));
  expect(await fragments.evaluate(node => !!node.closest('.dp-settings-tuning-group'))).toBe(false);
  await expect(group).toContainText('DebridPulse never re-encodes media');
  await expect(mediaCard(page)).not.toContainText(/Embed Subtitles|Subtitle Acquisition|separate files/i);
  await expect(mediaCard(page).locator('input[type="checkbox"]')).toHaveCount(0);
});

test('every control saves and survives a reload', async ({page}) => {
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
  await commit(subtitle(page), 'de');
  await commit(mediaCard(page).locator('#dp-settings-field-media-concurrent-fragment-downloads'), '8');

  const stored = await (await page.request.get('/api/settings')).json();
  expect(stored.preferred_subtitle_language).toBe('de');
  expect(stored.integrations.media.options).toMatchObject({target_resolution: '720', video_quality: 'low',
                                                            video_codec: 'hevc', concurrent_fragment_downloads: 8});

  await page.reload();
  await openSettings(page, 'downloads');
  await mediaCard(page).locator('.dp-settings-disclosure').first().click();
  await expect(mediaCard(page).locator('#dp-settings-field-media-target-resolution')).toHaveValue('720');
  await expect(mediaCard(page).locator('#dp-settings-field-media-video-quality')).toHaveValue('low');
  await expect(mediaCard(page).locator('#dp-settings-field-media-video-codec')).toHaveValue('hevc');
  await expect(subtitle(page)).toHaveValue('de');
  await expect(picker(page)).toHaveText('German');
  await expect(mediaCard(page).locator('#dp-settings-field-media-concurrent-fragment-downloads')).toHaveValue('8');
  await resetMedia(page);
});

// --- Preferred Subtitle Language: the one control, a searchable language picker -----

test('the subtitle language has one control, on the Media card, and reads as a name', async ({page}) => {
  await resetMedia(page);
  await openMedia(page);
  await expect(page.locator('[data-setting="preferred_subtitle_language"]')).toHaveCount(1);
  await expect(page.locator('#view-settings .dp-settings-download-engine-card')).not.toContainText('Subtitle');
  await expect(mediaCard(page).locator('.dp-settings-field', {has: subtitle(page)}).locator('.form-hint'))
    .not.toContainText(/code|Download Behavior/);
  // Closed: the name, never the bare code, centred beside the chevron.
  await expect(picker(page)).toHaveText('English');
  const closed = await picker(page).evaluate(trigger => {
    const value = trigger.querySelector('.dp-dropdown__value').getBoundingClientRect();
    return {align: getComputedStyle(trigger.querySelector('.dp-dropdown__value')).textAlign,
            clear: value.right <= trigger.querySelector('.dp-dropdown__chevron').getBoundingClientRect().left};
  });
  expect(closed).toEqual({align: 'center', clear: true});
});

test('open, it lists every language by name with a quiet code, searches both, and scrolls', async ({page}) => {
  await resetMedia(page);
  await openMedia(page);
  await picker(page).click();
  const search = menu(page).locator('.dp-dropdown__search');
  await expect(search).toBeFocused();
  await expect(search).toHaveAttribute('aria-label', 'Search languages');
  const all = await choices(page).count();
  expect(all).toBeGreaterThan(200);
  // Unique codes, ordered by name.
  const listed = await choices(page).evaluateAll(nodes => nodes.map(node => ({
    name: node.querySelector('.dp-dropdown__option-label').textContent,
    code: node.querySelector('.dp-dropdown__option-detail').textContent})));
  expect(new Set(listed.map(item => item.code.toLowerCase())).size).toBe(listed.length);
  expect(listed.map(item => item.name)).toEqual([...listed.map(item => item.name)].sort((a, b) => a.localeCompare(b, 'en')));
  // Every listed language is named, whatever the browser itself can name.
  expect(new Set(listed.map(item => item.name)).size).toBe(listed.length);
  expect(listed.filter(item => item.name.startsWith('Language code'))).toEqual([]);
  for (const [name, code] of [['Portuguese (Brazil)', 'pt-BR'], ['Spanish (Latin America)', 'es-419'],
                              ['Chinese (Simplified)', 'zh-Hans'], ['Tibetan', 'bo'], ['Navajo', 'nv']]) {
    expect(listed, name).toContainEqual({name, code});
  }
  // The code is the quieter, smaller second label.
  const look = await choices(page).first().evaluate(node => {
    const [name, code] = [node.querySelector('.dp-dropdown__option-label'), node.querySelector('.dp-dropdown__option-detail')]
      .map(part => getComputedStyle(part));
    return {smaller: parseFloat(code.fontSize) < parseFloat(name.fontSize), quieter: code.color !== name.color};
  });
  expect(look).toEqual({smaller: true, quieter: true});
  // A bounded menu: the list scrolls, the current choice is shown in it.
  const list = page.locator('#dp-dropdown-list');
  expect(await list.evaluate(node => node.scrollHeight > node.clientHeight)).toBe(true);
  expect((await menu(page).boundingBox()).height).toBeLessThanOrEqual(321);
  const current = choices(page).filter({has: page.locator('.dp-dropdown__option-label', {hasText: /^English$/})});
  await expect(current).toHaveAttribute('aria-selected', 'true');
  expect(await current.evaluate(node => {
    const box = node.parentElement.getBoundingClientRect(); const item = node.getBoundingClientRect();
    return item.top >= box.top - 1 && item.bottom <= box.bottom + 1;
  })).toBe(true);

  const first = () => choices(page).first().locator('.dp-dropdown__option-label').textContent();
  for (const [query, expected] of [['english', 'English'], ['EN', 'English'], ['portuguese', 'Portuguese'],
                                   ['pt-BR', 'Portuguese (Brazil)'], ['pt-br', 'Portuguese (Brazil)'],
                                   ['deutsch', 'German'], ['zh-hant', 'Chinese (Traditional)']]) {
    await search.fill(query);
    expect(await first(), query).toBe(expected);
  }
  await search.fill('portuguese');
  await expect(choices(page)).toHaveCount(3);
  await search.fill('zzzz');
  await expect(choices(page)).toHaveCount(0);
  await expect(menu(page).locator('.dp-dropdown__empty')).toHaveText('No matches');
  await search.fill('');
  await expect(choices(page)).toHaveCount(all);
  // Escape closes without a change and gives focus back to the control.
  await page.keyboard.press('Escape');
  await expect(menu(page)).toBeHidden();
  await expect(picker(page)).toBeFocused();
  await expect(picker(page)).toHaveText('English');
  expect((await (await page.request.get('/api/settings')).json()).preferred_subtitle_language).toBe('en');
});

test('a language chosen by keyboard saves its code and survives a reload; a refused one is undone', async ({page}) => {
  await resetMedia(page);
  await openMedia(page);
  const choose = async query => {
    await picker(page).focus();
    await page.keyboard.press('Enter');
    await expect(menu(page).locator('.dp-dropdown__search')).toBeFocused();
    await page.keyboard.type(query);
    await page.keyboard.press('ArrowDown');
    await expect(choices(page).first()).toBeFocused();
    const saved = page.waitForResponse(response => response.request().method() === 'PUT'
      && new URL(response.url()).pathname === '/api/settings');
    await page.keyboard.press('Enter');
    return saved;
  };
  for (const [query, code, name] of [['french', 'fr', 'French'], ['pt-br', 'pt-br', 'Portuguese (Brazil)']]) {
    expect((await choose(query)).ok()).toBe(true);
    await expect(menu(page)).toBeHidden();
    await expect(picker(page)).toBeFocused();
    await expect(picker(page)).toHaveText(name);
    expect((await (await page.request.get('/api/settings')).json()).preferred_subtitle_language).toBe(code);
    await page.reload();
    await openMedia(page);
    await expect(subtitle(page)).toHaveValue(code);
    await expect(picker(page)).toHaveText(name);
  }
  // A save the server refuses puts the accepted language back on show.
  await page.route('**/api/settings', route => route.request().method() === 'PUT'
    ? route.fulfill({status: 400, contentType: 'application/json',
                     body: JSON.stringify({detail: {message: 'Value is not valid.'}})})
    : route.fallback());
  expect((await choose('german')).status()).toBe(400);
  await expect(picker(page)).toHaveText('Portuguese (Brazil)');
  await expect(subtitle(page)).toHaveValue('pt-br');
  await page.unroute('**/api/settings');
  expect((await (await page.request.get('/api/settings')).json()).preferred_subtitle_language).toBe('pt-br');
  await resetMedia(page);
});

test('a saved code outside the list is shown as it is and kept', async ({page}) => {
  for (const [code, name] of [['it-ch', 'Italian (Switzerland)'], ['xx-yy', 'Language code: xx-yy']]) {
    await saveSubtitle(page, code);
    await openMedia(page);
    await expect(subtitle(page)).toHaveValue(code);
    await expect(picker(page)).toHaveText(name);
    await picker(page).click();
    await expect(choices(page).first()).toHaveAttribute('aria-selected', 'true');
    await expect(choices(page).first().locator('.dp-dropdown__option-detail')).toHaveText(code);
    await page.keyboard.press('Escape');
    await page.reload();
    expect((await (await page.request.get('/api/settings')).json()).preferred_subtitle_language).toBe(code);
  }
  await resetMedia(page);
});

for (const viewport of [{name: 'desktop', width: 1440, height: 1000}, {name: 'phone', width: 390, height: 844}]) {
  test(`a long language name is read whole and the menu stays on screen (${viewport.name})`, async ({page}) => {
    await saveSubtitle(page, 'en-gb');
    await openMedia(page);
    await page.setViewportSize({width: viewport.width, height: viewport.height});
    await picker(page).scrollIntoViewIfNeeded();
    await expect(picker(page)).toHaveText('English (United Kingdom)');
    const fit = await picker(page).evaluate(trigger => {
      const value = trigger.querySelector('.dp-dropdown__value');
      const cell = trigger.closest('.dp-settings-field').getBoundingClientRect();
      const box = trigger.getBoundingClientRect();
      return {whole: value.scrollWidth <= value.clientWidth + 1,
              inside: box.left >= cell.left - 1 && box.right <= cell.right + 1,
              page: document.documentElement.scrollWidth - document.documentElement.clientWidth};
    });
    expect(fit).toEqual({whole: true, inside: true, page: 0});
    await picker(page).click();
    const box = await menu(page).boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.y).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(viewport.width);
    expect(box.y + box.height).toBeLessThanOrEqual(viewport.height);
    // Every option's name is read whole inside the menu.
    expect(await page.locator('#dp-dropdown-list').evaluate(list => [...list.querySelectorAll('.dp-dropdown__option')]
      .every(option => option.scrollWidth <= option.clientWidth + 1))).toBe(true);
    await page.keyboard.press('Escape');
    await resetMedia(page);
  });
}

// --- YouTube Player Client: Automatic, or exactly one client the packaged yt-dlp offers -----

const EVIDENCE = process.env.DP_EVIDENCE_DIR || '';
const clients = page => mediaCard(page).locator('#dp-settings-field-media-youtube-player-clients');
const clientsField = page => mediaCard(page).locator('.dp-settings-field',
  {has: page.locator('#dp-settings-field-media-youtube-player-clients')});
const storedClients = async page =>
  (await (await page.request.get('/api/settings')).json()).integrations.media.options.youtube_player_clients;
const supportedClients = async page => (await (await page.request.get('/api/settings')).json())
  .integrations.media.options.youtube_player_client_support.supported;
const mediaPatch = page => page.waitForResponse(r => r.request().method() === 'PATCH'
  && new URL(r.url()).pathname === '/api/integrations/media/configuration');
const optionValues = page => clients(page).locator('option').evaluateAll(nodes => nodes.map(node => [node.value, node.textContent]));
// The selection the closed control shows, and whether it reads whole.
const shown = page => clientsField(page).locator('.dp-dropdown__trigger .dp-dropdown__value').evaluate(node =>
  ({text: node.textContent.trim(), whole: node.scrollWidth <= node.clientWidth + 1}));
const storeClients = async (page, list) => expect((await page.request.patch('/api/integrations/media/configuration',
  {data: {options: {youtube_player_clients: list}}})).ok()).toBe(true);

test('YouTube Player Client is one dropdown: Automatic by default, and every offered client saves alone and reloads',
  async ({page}) => {
    await resetMedia(page);
    await openMedia(page);
    await clients(page).scrollIntoViewIfNeeded();
    const supported = await supportedClients(page);
    expect(supported.length).toBeGreaterThan(3);
    // A single-choice select (no free text, no multiple selection): Automatic, then each offered client by name.
    expect(await clients(page).evaluate(node => [node.tagName, node.multiple])).toEqual(['SELECT', false]);
    expect(await optionValues(page)).toEqual([['', 'Automatic (yt-dlp default)'], ...supported.map(name => [name, name])]);
    await expect(clients(page)).toHaveValue('');
    expect(await shown(page)).toEqual({text: 'Automatic (yt-dlp default)', whole: true});
    await expect(clientsField(page).locator('.form-label')).toHaveText('YouTube Player Client');
    await expect(clientsField(page).locator('.form-hint')).toHaveText(
      'Automatic is recommended. Select another player client only when troubleshooting YouTube downloads.');
    await expect(clientsField(page)).not.toContainText('PO-token');
    await expect(clientsField(page)).not.toContainText('Available:');
    await expect(clientsField(page).locator('[data-player-client-notice]')).toBeEmpty();

    // Each offered client: exactly that one, as a list of one; reloaded, it is the selection shown.
    for (const name of supported) {
      const saved = mediaPatch(page);
      await clients(page).selectOption(name);
      const written = await saved;
      expect(written.ok()).toBe(true);
      expect(written.request().postDataJSON()).toEqual({options: {youtube_player_clients: [name]}});
      expect(await storedClients(page)).toEqual([name]);
      await expect(clients(page)).toHaveValue(name);
      expect(await shown(page)).toEqual({text: name, whole: true});
    }
    await page.reload();
    await openMedia(page);
    await expect(clients(page)).toHaveValue(supported[supported.length - 1]);

    // Automatic: the existing no-override form, the empty list.
    const saved = mediaPatch(page);
    await clients(page).selectOption('');
    expect((await saved).request().postDataJSON()).toEqual({options: {youtube_player_clients: []}});
    expect(await storedClients(page)).toEqual([]);
    await page.reload();
    await openMedia(page);
    await expect(clients(page)).toHaveValue('');
    await resetMedia(page);
  });

test('a saved selection of several clients is kept as Custom through unrelated saves until another choice replaces it',
  async ({page}) => {
    await resetMedia(page);
    const [first, second, third] = await supportedClients(page);
    await storeClients(page, [second, first]);
    await openMedia(page);
    await clients(page).scrollIntoViewIfNeeded();
    // Shown exactly as saved, as one explicit Custom entry (nothing to edit: no multiple selection).
    await expect(clients(page)).toHaveValue(`${second}, ${first}`);
    expect((await optionValues(page)).at(-1)).toEqual([`${second}, ${first}`, 'Custom (multiple clients)']);
    await expect(clients(page).locator('option:checked')).toHaveAttribute('data-dp-detail', `${second}, ${first}`);
    expect(await shown(page)).toEqual({text: 'Custom (multiple clients)', whole: true});
    for (const theme of ['dark', 'light']) {
      await page.evaluate(light => document.body.classList.toggle('light', light), theme === 'light');
      if (EVIDENCE) await clientsField(page).screenshot({path: `${EVIDENCE}/player-client-custom-${theme}.png`});
    }
    await page.evaluate(() => document.body.classList.remove('light'));

    // Unrelated saves -- a Media Downloads option and the whole settings document -- leave it untouched.
    let saved = mediaPatch(page);
    await mediaCard(page).locator('#dp-settings-field-media-video-quality').selectOption('low');
    const written = await saved;
    expect(written.ok()).toBe(true);
    expect(written.request().postDataJSON()).toEqual({options: {video_quality: 'low'}});
    await saveSubtitle(page, 'de');
    expect(await storedClients(page)).toEqual([second, first]);
    await page.reload();
    await openMedia(page);
    await expect(clients(page)).toHaveValue(`${second}, ${first}`);

    // Choosing one client replaces it, and Custom is no longer offered.
    saved = mediaPatch(page);
    await clients(page).selectOption(third);
    expect((await saved).request().postDataJSON()).toEqual({options: {youtube_player_clients: [third]}});
    expect(await storedClients(page)).toEqual([third]);
    await expect(clients(page).locator('option', {hasText: 'Custom'})).toHaveCount(0);

    // ...and Automatic replaces a Custom selection the same way.
    await storeClients(page, [first, second]);
    await page.reload();
    await openMedia(page);
    await expect(clients(page)).toHaveValue(`${first}, ${second}`);
    saved = mediaPatch(page);
    await clients(page).selectOption('');
    expect((await saved).request().postDataJSON()).toEqual({options: {youtube_player_clients: []}});
    expect(await storedClients(page)).toEqual([]);
    await expect(clients(page).locator('option', {hasText: 'Custom'})).toHaveCount(0);
    await resetMedia(page);
  });

test('a refused choice is reported and undone; nothing is saved', async ({page}) => {
  await resetMedia(page);
  const [first, second] = await supportedClients(page);
  await storeClients(page, [first]);
  await openMedia(page);
  await clients(page).scrollIntoViewIfNeeded();
  await expect(clients(page)).toHaveValue(first);
  // The server's own validation answer, as for any Settings field.
  await page.route('**/api/integrations/media/configuration', route => route.request().method() === 'PATCH'
    ? route.fulfill({status: 400, contentType: 'application/json',
      body: JSON.stringify({detail: 'YouTube Player Clients must name each client at most once.'})})
    : route.continue());
  const refused = mediaPatch(page);
  await clients(page).selectOption(second);
  expect((await refused).status()).toBe(400);
  await expect(page.locator('#toasts .toast').last()).toContainText('YouTube Player Clients must name each client');
  await expect(clients(page)).toHaveValue(first);
  await page.unroute('**/api/integrations/media/configuration');
  expect(await storedClients(page)).toEqual([first]);
  await resetMedia(page);
});

for (const [label, stored] of [['one client', ['retired_client']], ['several clients', ['retired_client', null]]]) {
  test(`a saved selection of ${label} the installed yt-dlp no longer offers is kept, with Automatic in use and one way back`,
    async ({page}) => {
      await resetMedia(page);
      const supported = await supportedClients(page);
      const list = stored.map(name => name ?? supported[0]);
      // What the server reports for a selection saved under an earlier yt-dlp
      // (that backend contract is proven in test_v113_media_downloads.py).
      await page.route('**/api/settings', async route => {
        if (route.request().method() !== 'GET') return route.continue();
        const response = await route.fetch();
        const body = await response.json();
        const options = body.integrations.media.options;
        options.youtube_player_clients = list;
        options.youtube_player_client_support = {...options.youtube_player_client_support,
                                                 unsupported: ['retired_client'], effective: []};
        await route.fulfill({response, json: body});
      });
      await openMedia(page);
      await clients(page).scrollIntoViewIfNeeded();
      const notice = clientsField(page).locator('[data-player-client-notice] .dp-settings-caution');
      await expect(notice).toHaveCount(1);
      await expect(notice).toHaveAttribute('role', 'status');
      await expect(notice).toContainText('Automatic in use');
      await expect(notice).toContainText('does not offer retired_client');
      // Shown exactly as saved, never silently replaced.
      await expect(clients(page)).toHaveValue(list.join(', '));
      const label = list.length > 1 ? 'Custom (multiple clients)' : 'retired_client';
      await expect(clients(page).locator('option:checked')).toHaveText(label);
      await expect(clients(page).locator('option:checked')).toHaveAttribute('data-dp-detail',
        list.length > 1 ? list.join(', ') : 'No longer offered');
      expect(await shown(page)).toEqual({text: label, whole: true});
      await expect(page.locator('#toasts .toast')).toHaveCount(0);    // a notice, never a toast
      for (const theme of ['dark', 'light']) {
        await page.evaluate(light => document.body.classList.toggle('light', light), theme === 'light');
        if (EVIDENCE) await clientsField(page).screenshot({path: `${EVIDENCE}/player-client-stale-${list.length}-${theme}.png`});
      }
      await page.evaluate(() => document.body.classList.remove('light'));

      // An unrelated save is unaffected by the stale choice.
      let saved = mediaPatch(page);
      await mediaCard(page).locator('#dp-settings-field-media-video-quality').selectOption('low');
      const unrelated = await saved;
      expect(unrelated.ok()).toBe(true);
      expect(unrelated.request().postDataJSON()).toEqual({options: {video_quality: 'low'}});
      await expect(notice).toHaveCount(1);

      // Use Automatic: one explicit write of the empty selection, and the notice goes.
      saved = mediaPatch(page);
      await notice.getByRole('button', {name: 'Use Automatic'}).click();
      const written = await saved;
      expect(written.ok()).toBe(true);
      expect(written.request().postDataJSON()).toEqual({options: {youtube_player_clients: []}});
      await expect(clientsField(page).locator('[data-player-client-notice]')).toBeEmpty();
      await expect(clients(page)).toHaveValue('');
      await page.unroute('**/api/settings');
      await resetMedia(page);
    });
}

test('YouTube Player Client reads whole at a narrow width in both themes', async ({page}) => {
  await resetMedia(page);
  await openMedia(page);
  for (const [width, theme] of [[1440, 'dark'], [1440, 'light'], [700, 'dark'], [700, 'light']]) {
    await page.setViewportSize({width, height: 1000});
    await page.evaluate(value => document.documentElement.setAttribute('data-theme', value), theme);
    await clients(page).scrollIntoViewIfNeeded();
    await expect(clients(page)).toBeVisible();
    const fits = await clientsField(page).evaluate(node => {
      const card = node.closest('[data-executor-tuning="media"]').getBoundingClientRect();
      const box = node.getBoundingClientRect();
      return box.left >= card.left - 1 && box.right <= card.right + 1;
    });
    expect(fits, `${width} ${theme}`).toBe(true);
    expect(await page.evaluate(() =>
      document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)).toBe(true);
    if (EVIDENCE) await mediaCard(page).screenshot({path: `${EVIDENCE}/media-card-${width}-${theme}.png`});
  }
});
