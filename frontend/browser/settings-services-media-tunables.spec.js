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
    {data: {options: {target_resolution: 'best', video_quality: 'high', video_codec: 'auto',
                     concurrent_fragment_downloads: 1}}});
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
  await expect(fields).toHaveCount(5);
  await expect(fields.locator('.form-label')).toHaveText(
    ['Target Resolution', 'Video Quality', 'Preferred Video Codec', 'Preferred Subtitle Language',
     'Concurrent Fragment Downloads']);
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
