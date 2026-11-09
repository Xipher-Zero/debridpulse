// @ts-check
const {test, expect} = require('@playwright/test');

/* DebridPulse 1.0.14 localization, WS3c: Settings services and engines --
 * the Settings shell (tabs, header), Services (premium providers, Usenet
 * news servers, Network Sources), Downloads (behavior, transfer methods, disk
 * space, engine activity) and Extraction -- say every word through the
 * catalog, and a live language change re-says them in place: a typed draft
 * keeps its value and focus, an opened card stays open, an open dialog is
 * re-worded, a Test in flight stays busy, and nothing is read or written.
 *
 * Shown in the test-only pseudo locale en-XA (every message accented and
 * bracketed ⟦ … ⟧). Synthetic accounts and synthetic answers only
 * (page.route) where a request would reach a provider: nothing is tested
 * against a real service and no setting is saved. */

// Proper names (providers, protocols, codecs, formats), units and what the
// installation or a fixture wrote; and language names, which Intl writes
// (English under a pseudo locale, by design).
const NAMES = ['DebridPulse', 'AllDebrid', 'Debrid-Link', 'Real-Debrid', 'TorBox', 'Premiumize', 'Usenet', 'WebDAV', 'rsync',
  'HTTP(S)', '(S)FTP', 'SCP', 'Metalink (Multimeta)', 'Metalink', 'Multimeta', 'Media Downloads', 'HEVC (H.265)', 'H.264', 'AV1',
  'yt-dlp', 'aria2', 'English', 'news.example.com', 'synthetic-renamed', 'KB/s', 'MB/s', 'GB'];

async function boot(page, width = 1440) {
  await page.setViewportSize({width, height: width < 780 ? 820 : 900});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  await expect(page.locator('#sidebar .nav-item[data-view="settings"]')).toBeAttached();
  await page.evaluate(() => window.DPI18n.ready);
}

async function openSettings(page, tab) {
  await page.evaluate(() => window.nav(document.querySelector('#sidebar .nav-item[data-view="settings"]')));
  await expect(page.locator('#view-settings .dp-settings-master-card')).toBeVisible();
  await page.locator(`#view-settings .dp-settings-tabs [data-tab="${tab}"]:not([inert])`).click();
  await expect(page.locator(`#view-settings [data-panel="${tab}"]`)).toBeVisible();
}

const setLocale = (page, locale) => page.evaluate(locale => window.DPI18n.setLocale(locale), locale);
const settle = page => page.evaluate(() => new Promise(resolve => setTimeout(resolve, 400)));

/* Visible words still in English inside `selector` (text and human
 * attributes), leaving out the names above, figures and field values. */
const englishLeft = (page, selector) => page.evaluate(([selector, names]) => {
  const visible = element => element.getClientRects().length > 0 && getComputedStyle(element).visibility !== 'hidden';
  const english = text => !text.includes('⟦')
    && /[A-Za-z]{3,}/.test(names.reduce((left, name) => left.split(name).join(' '), text));
  const found = [];
  for (const root of document.querySelectorAll(selector)) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const element = node.parentElement;
      const text = node.textContent.trim();
      if (!text || !element || !visible(element) || element.closest('script, style, svg, template, option')) continue;
      if (english(text)) found.push(`text: ${text}`);
    }
    for (const element of [root, ...root.querySelectorAll('*')]) {
      if (!visible(element)) continue;
      for (const attribute of ['aria-label', 'data-tooltip', 'placeholder', 'title']) {
        const value = element.getAttribute(attribute);
        if (value && english(value)) found.push(`[${attribute}]: ${value}`);
      }
    }
  }
  return [...new Set(found)];
}, [selector, NAMES]);

// Every request that is not a read, apart from the sidebar's aria2 health probe
// (a POST that changes nothing, retried on its own clock from boot).
function watchWrites(page) {
  const writes = [];
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith('/api/') && request.method() !== 'GET' && path !== '/api/settings/test-aria2') writes.push(`${request.method()} ${path}`);
  });
  return writes;
}

test('L10-WS3c: in en-XA the Settings shell, Services, Downloads and Extraction are the catalog\'s', async ({page}) => {
  await boot(page);
  await openSettings(page, 'sources');
  expect(await setLocale(page, 'en-XA')).toBe('en-XA');
  await expect(page.locator('#view-settings .dp-settings-tabs [data-tab="sources"]:not([inert])')).toContainText('⟦');
  // Opened, so the cards' bodies are drawn too.
  await page.locator('.dp-settings-provider-card--alldebrid .dp-settings-disclosure').click();
  await page.locator('.dp-settings-provider-card--alldebrid .dp-settings-additional > summary').click();
  expect(await englishLeft(page, '#view-settings .dp-settings-master-header')).toEqual([]);
  expect(await englishLeft(page, '#view-settings [data-panel="sources"]')).toEqual([]);
  for (const tab of ['downloads', 'extraction']) {
    await page.locator(`#view-settings .dp-settings-tabs [data-tab="${tab}"]:not([inert])`).click();
    await expect(page.locator(`#view-settings [data-panel="${tab}"]`)).toBeVisible();
    expect(await englishLeft(page, `#view-settings [data-panel="${tab}"]`), tab).toEqual([]);
  }
  expect(await page.evaluate(() => window.DPI18n.diagnostics())).toEqual([]);
  expect(await setLocale(page, 'en')).toBe('en');
  await expect(page.locator('#view-settings .dp-settings-tabs [data-tab="extraction"]:not([inert])')).toHaveText('Extraction');
});

test('L12-WS3c: a language change keeps a typed draft and its focus, an open card and disclosure, and asks nothing', async ({page}) => {
  const writes = watchWrites(page);
  let settingsReads = 0;
  page.on('request', request => { if (new URL(request.url()).pathname === '/api/settings' && request.method() === 'GET') settingsReads += 1; });
  await boot(page);
  await openSettings(page, 'sources');
  const card = page.locator('.dp-settings-provider-card--alldebrid');
  await card.locator('.dp-settings-disclosure').click();
  await card.locator('.dp-settings-additional > summary').click();
  const rate = page.locator('#view-settings [data-setting="alldebrid_rate_limit_per_minute"]');
  await expect(rate).toBeVisible();
  await rate.fill('59');                                                          // typed, not committed
  await rate.focus();
  await settle(page);
  const reads = settingsReads;
  writes.length = 0;
  expect(await setLocale(page, 'en-XA')).toBe('en-XA');
  await settle(page);
  await expect(rate).toBeFocused();
  await expect(rate).toHaveValue('59');
  await expect(card).not.toHaveClass(/dp-settings-provider-card--collapsed/);
  await expect(card.locator('.dp-settings-additional')).toHaveAttribute('open', '');
  await expect(card.locator('.dp-settings-disclosure')).toHaveAttribute('aria-label', /⟦/);
  await expect(page.locator('#view-settings .dp-settings-tabs [data-tab="sources"]:not([inert])')).toHaveAttribute('aria-selected', 'true');
  expect(await setLocale(page, 'en')).toBe('en');
  await settle(page);
  await expect(rate).toBeFocused();
  await expect(rate).toHaveValue('59');
  await expect(card.locator('.dp-settings-disclosure')).toHaveAttribute('aria-label', 'Collapse provider configuration');
  expect(settingsReads).toBe(reads);
  expect(writes).toEqual([]);
});

test('L12-WS3c: an open news-server rename is re-worded while its typed name stays', async ({page}) => {
  await boot(page);
  await openSettings(page, 'sources');
  const usenet = page.locator('.dp-settings-provider-card--usenet');
  if ((await usenet.locator('.dp-settings-disclosure').getAttribute('aria-expanded')) !== 'true') await usenet.locator('.dp-settings-disclosure').click();
  await usenet.locator('[data-usenet-action="add"]').click();
  const server = usenet.locator('[data-usenet-server-id=""]').last();
  await expect(server.locator('[data-usenet-display-name]')).toHaveText('New server');
  await server.locator('[data-usenet-action="rename"]').click();
  const dialog = page.locator('.dp-modal-overlay .dp-modal-dialog');
  const value = dialog.locator('.dp-modal-field .input');
  await value.fill('synthetic-renamed');
  expect(await setLocale(page, 'en-XA')).toBe('en-XA');
  await expect(dialog.locator('.dp-modal-field .form-label')).toContainText('⟦');
  expect(await englishLeft(page, '.dp-modal-overlay .dp-modal-dialog')).toEqual([]);
  await expect(value).toHaveValue('synthetic-renamed');
  await expect(value).toBeFocused();
  // The card behind it: its default name, said again.
  await expect(server.locator('[data-usenet-display-name]')).toContainText('⟦');
  await dialog.locator('[data-modal-cancel]').click();
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
  expect(await setLocale(page, 'en')).toBe('en');
  await expect(server.locator('[data-usenet-display-name]')).toHaveText('New server');
});

test('L12-WS3c: a Test in flight keeps its busy state across a language change and ends in the new language', async ({page}) => {
  let release;
  const held = new Promise(resolve => { release = resolve; });
  let tests = 0;
  await page.route('**/api/settings/validate-alldebrid', async route => {
    tests += 1;
    await held;
    await route.fulfill({status: 400, contentType: 'application/json', body: JSON.stringify({detail: 'synthetic refusal'})});
  });
  await boot(page);
  await openSettings(page, 'sources');
  const button = page.locator('.dp-settings-provider-card--alldebrid [data-action="test-alldebrid"]').first();
  await button.click();
  await expect(button).toBeDisabled();
  await expect(button).toHaveText('Testing…');
  expect(await setLocale(page, 'en-XA')).toBe('en-XA');
  await expect(button).toBeDisabled();
  await expect(button).toContainText('⟦');
  release();
  await expect(button).toBeEnabled();
  await expect(button).toContainText('⟦');                                         // its idle words, in en-XA
  await expect(page.locator('#toasts .toast').last()).toContainText('⟦');
  expect(tests).toBe(1);
  expect(await setLocale(page, 'en')).toBe('en');
  await expect(button).toContainText('Test');
});

test('L10-WS3c: at 390px, in en-XA, Services, Downloads and Extraction never scroll the page sideways', async ({page}) => {
  await boot(page, 390);
  await openSettings(page, 'sources');
  expect(await setLocale(page, 'en-XA')).toBe('en-XA');
  for (const tab of ['sources', 'downloads', 'extraction']) {
    await page.evaluate(tab => document.querySelector(`#view-settings .dp-settings-tabs [data-tab="${tab}"]:not([inert])`)?.click(), tab);
    await expect(page.locator(`#view-settings [data-panel="${tab}"]`)).toBeVisible();
    await settle(page);
    expect(await page.evaluate(() => document.scrollingElement.scrollWidth - window.innerWidth), tab).toBeLessThanOrEqual(0);
  }
  expect(await setLocale(page, 'en')).toBe('en');
});

test('L12-WS3c: an open subtitle-language dropdown is re-said in place: its search name, its options, the typed query and focus', async ({page}) => {
  const writes = watchWrites(page);
  let settingsReads = 0;
  page.on('request', request => { if (new URL(request.url()).pathname === '/api/settings' && request.method() === 'GET') settingsReads += 1; });
  await boot(page);
  await openSettings(page, 'downloads');
  const media = page.locator('#view-settings [data-executor-tuning="media"]');
  await media.locator('.dp-settings-disclosure').click();
  await expect(media.locator('[data-setting="preferred_subtitle_language"]')).toBeAttached();
  expect(await setLocale(page, 'en-XA')).toBe('en-XA');
  const openMenu = setting => page.evaluate(setting =>
    document.querySelector(`#view-settings [data-setting="${setting}"]`)._dpDropdownTrigger.click(), setting);
  // The projection as drawn, and the select's own options it must mirror.
  const shown = () => page.evaluate(() => Array.from(document.querySelectorAll('.dp-dropdown__option')).map(button => ({
    index: button.dataset.dpOptionIndex,
    label: (button.querySelector('.dp-dropdown__option-label') || button).textContent,
  })));
  const mirrored = setting => page.evaluate(setting => {
    const select = document.querySelector(`#view-settings [data-setting="${setting}"]`);
    return Array.from(document.querySelectorAll('.dp-dropdown__option')).every(button =>
      ((button.querySelector('.dp-dropdown__option-label') || button).textContent) === select.options[Number(button.dataset.dpOptionIndex)].textContent);
  }, setting);
  const subtitle = '#view-settings [data-setting="preferred_subtitle_language"]';
  const selected = () => page.evaluate(selector => document.querySelector(selector).value, subtitle);

  // Opened after the change: its search says the catalog's words in en-XA.
  await openMenu('preferred_subtitle_language');
  const search = page.locator('.dp-dropdown__search');
  await expect(search).toBeFocused();
  await expect(search).toHaveAttribute('placeholder', /⟦/);
  await expect(search).toHaveAttribute('aria-label', /⟦/);
  await search.fill('fr');
  const value = await selected();
  const options = await shown();
  expect(options.length).toBeGreaterThan(0);
  await settle(page);
  const reads = settingsReads;
  writes.length = 0;

  for (const [locale, words] of [['en', 'Search languages'], ['en-XA', /⟦/]]) {
    expect(await setLocale(page, locale)).toBe(locale);
    await settle(page);
    await expect(page.locator('.dp-dropdown-menu--search')).toBeVisible();           // still open
    await expect(search).toBeFocused();
    await expect(search).toHaveValue('fr');
    await expect(search).toHaveAttribute('placeholder', words);
    await expect(search).toHaveAttribute('aria-label', words);
    expect((await shown()).map(entry => entry.index)).toEqual(options.map(entry => entry.index));   // the same matches
    expect(await mirrored('preferred_subtitle_language')).toBe(true);                 // the names the select now has
    expect(await selected()).toBe(value);
  }
  await page.keyboard.press('Escape');

  // A list of translated words, open while the language changes: re-worded in place.
  await openMenu('media_video_quality');
  const second = page.locator('.dp-dropdown__option').nth(1);
  await second.focus();
  const index = await second.getAttribute('data-dp-option-index');
  await expect(second).toContainText('⟦');
  expect(await setLocale(page, 'en')).toBe('en');
  await settle(page);
  const focused = page.locator(`.dp-dropdown__option[data-dp-option-index="${index}"]`);
  await expect(focused).toBeFocused();
  await expect(focused).toHaveText('Normal');
  expect(await mirrored('media_video_quality')).toBe(true);
  await page.keyboard.press('ArrowDown');                                            // keyboard still runs the list
  await expect(page.locator('.dp-dropdown__option').nth(2)).toBeFocused();
  await page.keyboard.press('Escape');
  expect(settingsReads).toBe(reads);
  expect(writes).toEqual([]);
});
