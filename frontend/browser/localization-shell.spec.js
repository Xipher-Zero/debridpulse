// @ts-check
const {test, expect} = require('@playwright/test');

/* DebridPulse 1.0.14 localization, WS3a: the application shell and shared
 * UI -- the sidebar and topbar, Dashboard, Activity Log, Statistics, and the
 * shared owners they draw with (status badges, progress, toasts, pager,
 * storage and provider status) -- say every word through the catalog.
 *
 * Shown in the test-only pseudo locale en-XA (every message accented, ~40%
 * longer and bracketed ⟦ … ⟧), so a word that bypasses the catalog stays
 * plain English and is caught. The switch is live (DPI18n.setLocale, the
 * substrate a later language choice will use; nothing here offers one): a
 * language change re-words the page in place and keeps what the person was
 * doing. Synthetic accounts only; the open runtime holds no transfers or
 * events, so the lists show their empty states. */

// Proper names (the product wordmark is two text nodes: Debrid + Pulse).
const NAMES = /^(DebridPulse|Debrid|Pulse|aria2|SQLite|AllDebrid|Debrid-Link|Premiumize|Real-Debrid|TorBox|Usenet|WebDAV|rsync|SCP|SFTP|FTP|HTTP|HTTPS)$/;

async function isolateFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

async function boot(page, width = 1440) {
  await page.setViewportSize({width, height: width < 780 ? 760 : 900});
  await isolateFonts(page);
  await page.goto('/');
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeAttached();
  await page.evaluate(() => window.DPI18n.ready);
}

async function show(page, view) {
  await page.evaluate(view => {
    const entry = document.querySelector(`#sidebar .nav-item[data-view="${view}"]`);
    window.nav(entry);
  }, view);
  await expect(page.locator(`#view-${view}`)).toHaveClass(/\bactive\b/);
}

/* The words still in English inside `scopes`: visible text and the visible
 * elements' human attributes that carry no pseudo message (⟦) but an English
 * word -- leaving out proper names, figures, and what the server or the
 * person wrote (event messages, transfer names). */
const englishLeft = (page, scopes) => page.evaluate(([scopes, names]) => {
  const proper = new RegExp(names);
  const visible = element => element.getClientRects().length > 0 && getComputedStyle(element).visibility !== 'hidden';
  const userData = element => element.closest('.dp-activity-message, .t-name, .dp-activity-detail-text, .dp-provider-status-name, .dp-theme-picker-name, #sidebar-version');
  const english = text => {
    if (text.includes('⟦')) return false;
    const words = (text.match(/[A-Za-z]{3,}/g) || []).filter(word => !proper.test(word));
    return words.length > 0;
  };
  const found = [];
  for (const selector of scopes) {
    for (const root of document.querySelectorAll(selector)) {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const element = node.parentElement;
        const text = node.textContent.trim();
        if (!text || !element || !visible(element) || userData(element) || element.closest('script, style, svg, template')) continue;
        if (english(text)) found.push(`${selector} text: ${text}`);
      }
      for (const element of [root, ...root.querySelectorAll('*')]) {
        if (!visible(element) || userData(element)) continue;
        for (const attribute of ['aria-label', 'data-tooltip', 'placeholder', 'title']) {
          const value = element.getAttribute(attribute);
          if (value && english(value)) found.push(`${selector} [${attribute}]: ${value}`);
        }
      }
    }
  }
  return [...new Set(found)];
}, [scopes, NAMES.source]);

const KEYLIKE = /^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$/;
const unrendered = page => page.evaluate(KEYLIKE => [...document.querySelectorAll('[data-i18n],[data-i18n-attr]')].flatMap(node => {
  const out = [];
  if (node.dataset.i18n && (!node.textContent.trim() || new RegExp(KEYLIKE).test(node.textContent.trim()))) out.push(node.dataset.i18n);
  for (const pair of String(node.dataset.i18nAttr || '').split(';').filter(Boolean)) {
    const [attribute] = pair.split(':');
    const value = node.getAttribute(attribute.trim()) || '';
    if (!value || new RegExp(KEYLIKE).test(value)) out.push(pair);
  }
  return out;
}), KEYLIKE.source);

const pageOverflow = page => page.evaluate(() => document.scrollingElement.scrollWidth - window.innerWidth);
const SHELL = ['#sidebar', '#topbar'];

test('L10-WS3a: in en-XA every shell, Dashboard, Activity Log and Statistics word is the catalog\'s', async ({page}) => {
  await boot(page);
  // English first: nothing names a key it does not render.
  expect(await unrendered(page)).toEqual([]);
  expect(await page.evaluate(() => window.DPI18n.setLocale('en-XA'))).toBe('en-XA');
  await expect(page.locator('#dash-tbody .empty')).toContainText('⟦');                  // the empty list, said again
  expect(await englishLeft(page, [...SHELL, '#view-dashboard'])).toEqual([]);
  for (const view of ['events', 'stats']) {
    await show(page, view);
    if (view === 'events') await expect(page.locator('#event-list .empty')).toContainText('⟦');
    if (view === 'stats') await expect(page.locator('#detail-file-status')).toContainText('⟦');
    expect(await englishLeft(page, [...SHELL, `#view-${view}`])).toEqual([]);
  }
  expect(await unrendered(page)).toEqual([]);
  // Shared owners: a status badge, a progress line, the storage and pager names.
  const shared = await page.evaluate(() => ({
    badge: window.DPIcons.statusBadge('downloading', ''),
    failed: window.badge('error', {error: {category: 'disk_full'}}),
    projected: window.badge('queued', {presentation_status: 'waiting_for_slot', presentation_badge_status: 'queued', presentation_label: 'Waiting for execution slot'}),
    progress: window.progress(42, 'downloading', null, null),
    size: window.fmtSize(1536),
    speed: window.fmtSpeed(3 * 1048576),
  }));
  for (const [name, html] of Object.entries(shared)) expect(html, name).toContain('⟦');
  expect(shared.failed).not.toContain('Disk Full');
  expect(shared.projected).not.toContain('Waiting for execution slot');
  expect(await page.evaluate(() => window.DPI18n.diagnostics())).toEqual([]);
  // Back to English in place, with nothing left over.
  expect(await page.evaluate(() => window.DPI18n.setLocale('en'))).toBe('en');
  expect(await page.locator('body').innerText()).not.toContain('⟦');
});

test('L12-WS3a: a live language change keeps the view, a Quick Add draft and its focus, an open filter panel and the period', async ({page}) => {
  await boot(page);
  const draft = 'https://example.invalid/a-draft-that-must-survive';
  const field = page.locator('#q-transfer-input');
  await field.fill(draft);
  await field.focus();
  expect(await page.evaluate(() => window.DPI18n.setLocale('en-XA'))).toBe('en-XA');
  await expect(field).toHaveValue(draft);
  await expect(field).toBeFocused();
  await expect(page.locator('#view-dashboard')).toHaveClass(/\bactive\b/);
  await expect(page.locator('#btn-add-transfer')).toContainText('⟦');

  // Activity Log: an open filter panel keeps its unapplied choice.
  await show(page, 'events');
  await page.locator('#ev-filter').click();
  const panel = page.locator('#ev-filter-panel');
  await expect(panel).toBeVisible();
  // The radio is visually hidden behind its words: a person picks the option by its label.
  await panel.locator('label:has(input[name="ev-filter-level"][value="error"])').click();
  await expect(panel.locator('input[name="ev-filter-level"][value="error"]')).toBeChecked();
  expect(await page.evaluate(() => window.DPI18n.setLocale('en'))).toBe('en');
  await expect(panel).toBeVisible();
  await expect(panel.locator('input[name="ev-filter-level"][value="error"]')).toBeChecked();
  await expect(panel.locator('legend').first()).toHaveText('Time Window');
  await expect(page.locator('#ev-filter-label')).toHaveText('Filter Results');          // nothing applied
  await panel.locator('[data-filter-action="cancel"]').click();

  // Statistics: the chosen period stays chosen and its sentences follow.
  await show(page, 'stats');
  await page.locator('#stats-period-tabs .ftab[data-period="30d"]').click();
  await expect(page.locator('#chart-title')).toHaveText('Completed downloads in the last 30 days.');
  expect(await page.evaluate(() => window.DPI18n.setLocale('en-XA'))).toBe('en-XA');
  await expect(page.locator('#stats-period-tabs .ftab.active')).toHaveAttribute('data-period', '30d');
  await expect(page.locator('#chart-title')).toHaveText(/^⟦Çömpléţéd döwñlöàdš îñ ţhé làšţ 30 dàýš\. ·+⟧$/);
  await expect(page.locator('[data-dp-stats-metric="success"] .metric-sub')).toContainText('30');
  expect(await page.evaluate(() => window.DPI18n.setLocale('en'))).toBe('en');
  await expect(page.locator('#chart-title')).toHaveText('Completed downloads in the last 30 days.');
});

test('L10-WS3a: at 390px, in en-XA, Dashboard, Activity Log and Statistics never scroll the page sideways', async ({page}) => {
  await boot(page, 390);
  expect(await page.evaluate(() => window.DPI18n.setLocale('en-XA'))).toBe('en-XA');
  for (const view of ['dashboard', 'events', 'stats']) {
    await show(page, view);
    expect(await pageOverflow(page), view).toBeLessThanOrEqual(0);
    expect(await englishLeft(page, [`#view-${view}`]), view).toEqual([]);
  }
  expect(await page.evaluate(() => window.DPI18n.setLocale('en'))).toBe('en');
});

// ── Live changes while something is under way (WS3a revision) ───────────────
// Synthetic answers only (page.route): a held request keeps its control
// pending; nothing reaches a provider and no operation runs.
async function hold(page, pattern, method, body) {
  const state = {calls: 0, release: null};
  const gate = new Promise(resolve => { state.release = resolve; });
  await page.route(pattern, async route => {
    if (route.request().method() !== method) return route.fallback();
    state.calls += 1;
    await gate;
    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(body)});
  });
  return state;
}
const word = (page, key) => page.evaluate(key => window.DPI18n.t(key), key);
const label = button => button.evaluate(node => (node.querySelector('[data-dp-icon-label]') || node).textContent.trim());

async function pendingAcrossLanguages(page, button, pendingKey, idleKey) {
  await expect(button).toHaveAttribute('aria-busy', 'true');
  expect(await label(button)).toBe(await word(page, pendingKey));
  // A render of the page's keyed markup (as auth.js does after composing it)
  // never puts a pending control's idle words back.
  await page.evaluate(() => window.DPI18n.render(document));
  expect(await label(button)).toBe(await word(page, pendingKey));
  expect(await page.evaluate(() => window.DPI18n.setLocale('en-XA'))).toBe('en-XA');
  await expect(button).toHaveAttribute('aria-busy', 'true');
  await expect(button).toBeDisabled();
  expect(await label(button)).toBe(await word(page, pendingKey));
  expect(await label(button)).toContain('⟦');
  expect(await page.evaluate(() => window.DPI18n.setLocale('en'))).toBe('en');
  expect(await label(button)).toBe(await word(page, pendingKey));
  expect(await page.evaluate(() => window.DPI18n.setLocale('en-XA'))).toBe('en-XA');   // completes in en-XA
  return async state => {
    state.release();
    await expect(button).not.toHaveAttribute('aria-busy', 'true');
    expect(await label(button)).toBe(await word(page, idleKey));
    expect(await label(button)).toContain('⟦');                                     // the language now selected
    expect(state.calls).toBe(1);                                                       // never run again
  };
}

test('L12-WS3a: Quick Add, Recover All and Pause All stay pending across language changes and end in the current language', async ({page}) => {
  await boot(page);
  const add = await hold(page, '**/api/links/add', 'POST', {});
  await page.locator('#q-transfer-input').fill('https://example.invalid/held');
  await page.locator('#btn-add-transfer').click();
  await (await pendingAcrossLanguages(page, page.locator('#btn-add-transfer'), 'quick_add.adding', 'quick_add.add'))(add);
  expect(await page.evaluate(() => window.DPI18n.setLocale('en'))).toBe('en');

  const recover = await hold(page, '**/api/torrents/recover-all', 'POST', {recovered: 0, imported: 0, errors: []});
  await page.locator('#btn-recover-all').click();
  await (await pendingAcrossLanguages(page, page.locator('#btn-recover-all'), 'recovery.recovering', 'recovery.recover_all'))(recover);
  expect(await page.evaluate(() => window.DPI18n.setLocale('en'))).toBe('en');

  const pause = await hold(page, '**/api/processing/pause', 'POST', {});
  await page.locator('#btn-pause-all').click();
  await (await pendingAcrossLanguages(page, page.locator('#btn-pause-all'), 'processing.pausing', 'processing.pause_all'))(pause);
  expect(await page.evaluate(() => window.DPI18n.setLocale('en'))).toBe('en');
});

test('L12-WS3a: an open Statistics period menu and an open Full List are re-worded in place', async ({page}) => {
  const statuses = ['completed', 'deleted', 'error', 'missing', 'duplicate', 'pending', 'uploading', 'processing', 'ready', 'queued', 'downloading', 'paused'];
  await page.route('**/api/stats/detail*', route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
    totals: {torrent_total: 12, completed_count: 1, partial_total: 0, success_rate_pct: 50, completed_size: 1024},
    torrent_status: {completed: 1}, file_status: Object.fromEntries(statuses.map((name, index) => [name, index + 1])),
    event_levels: [{level: 'info', count: 2}], transfer_methods: [], daily_completions: []})}));
  await boot(page, 390);
  await show(page, 'stats');
  await page.locator('#stats-period').click();
  const menu = page.locator('#stats-period-menu');
  await expect(menu).toBeVisible();
  await page.keyboard.press('ArrowDown');
  const focused = await page.evaluate(() => { const node = document.activeElement; node.dataset.mark = 'focused'; return node.dataset.period; });
  expect(focused).toBeTruthy();
  expect(await page.evaluate(() => window.DPI18n.setLocale('en-XA'))).toBe('en-XA');
  await expect(menu).toBeVisible();
  expect(await page.evaluate(() => ({mark: document.activeElement.dataset.mark, period: document.activeElement.dataset.period})))
    .toEqual({mark: 'focused', period: focused});
  for (const text of await menu.locator('[role="option"]').allTextContents()) expect(text).toContain('⟦');
  await page.keyboard.press('ArrowDown');                                            // keyboard still drives the menu
  expect(await page.evaluate(() => document.activeElement.closest('#stats-period-menu') !== null)).toBe(true);
  await page.keyboard.press('Escape');
  expect(await page.evaluate(() => window.DPI18n.setLocale('en'))).toBe('en');

  await page.setViewportSize({width: 1440, height: 900});
  const card = page.locator('#detail-file-status').locator('xpath=ancestor::div[contains(@class,"list-card")]');
  await expect(card).toHaveClass(/dp-stats-overflow-card/);
  await card.focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#overlay')).toHaveClass(/open/);
  await expect(page.locator('#modal-title')).toHaveText('File Status — Full List');
  const before = await page.evaluate(() => { const list = document.querySelector('#modal-body > .dp-stats-overflow-list'); list.dataset.mark = 'list'; return list.querySelectorAll('.kv-row').length; });
  expect(await page.evaluate(() => window.DPI18n.setLocale('en-XA'))).toBe('en-XA');
  await expect(page.locator('#overlay')).toHaveClass(/open/);
  await expect(page.locator('#modal-title')).toContainText('⟦');
  expect(await page.evaluate(() => {
    const list = document.querySelector('#modal-body > .dp-stats-overflow-list');
    return {same: list?.dataset.mark === 'list', rows: list?.querySelectorAll('.kv-row').length,
            words: [...list.querySelectorAll('.kv-key')].every(key => key.textContent.includes('⟦'))};
  })).toEqual({same: true, rows: before, words: true});
  expect(await page.evaluate(() => window.DPI18n.setLocale('en'))).toBe('en');
});

test('L12-WS3a: the Activity Log says why Last is unavailable in the language now shown, without asking again', async ({page}) => {
  let reads = 0;
  await page.route('**/api/events?*', route => { reads += 1; return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
    items: [{id: 9, type: 'transfer.added', category: 'transfer', severity: 'info', message: 'Synthetic event', occurred_at: '2026-11-09T10:00:00Z'}],
    has_more: true, next_before: 9, more_after_next: true, newer_available: false, snapshot: 9, window_start: null,
    search: {mode: 'none', finality: 'unproven'}})}); });
  await boot(page);
  await show(page, 'events');
  const last = page.locator('#dp-activity-page-btns [data-dp-pager="last"]');
  await expect(last).toHaveAttribute('aria-label', /The last page cannot be found until every event has been searched\./);
  const asked = reads;
  expect(await page.evaluate(() => window.DPI18n.setLocale('en-XA'))).toBe('en-XA');
  await expect(last).toHaveAttribute('aria-label', /⟦.*⟧.*⟦.*⟧/);
  await expect(last).not.toHaveAttribute('aria-label', /every event has been searched/);
  await expect(last).toBeDisabled();
  expect(reads).toBe(asked);
  expect(await page.evaluate(() => window.DPI18n.setLocale('en'))).toBe('en');
  await expect(last).toHaveAttribute('aria-label', /The last page cannot be found/);
});

test('L12-WS3a: a focused Download Activity row action keeps focus across a language change', async ({page}) => {
  const row = {id: 77, name: 'synthetic.bin', display_name: 'synthetic.bin', status: 'downloading', presentation_status: 'downloading',
    presentation_badge_status: 'downloading', progress: 10, size_bytes: 1048576, created_at: '2026-11-09T10:00:00Z',
    current_source_identity: {kind: 'link'}};
  await page.route('**/api/torrents?*', route => route.fulfill({status: 200, contentType: 'application/json',
    body: JSON.stringify({items: [row], total: 1})}));
  await boot(page);
  const pause = page.locator('#dash-tbody tr[data-torrent-id="77"] button[onclick*="pauseT(77"]');
  await pause.focus();
  expect(await page.evaluate(() => window.DPI18n.setLocale('en-XA'))).toBe('en-XA');
  expect(await page.evaluate(() => document.activeElement.getAttribute('onclick'))).toContain('pauseT(77');
  await expect(pause).toBeFocused();
  await expect(pause).toHaveAttribute('data-tooltip', /⟦/);
  await page.keyboard.press('Tab');                                                   // still a keyboard-usable row
  expect(await page.evaluate(() => window.DPI18n.setLocale('en'))).toBe('en');
});

test('L10-WS3a: a consolidation is unverified by its association count, never by the words the server sent', async ({page}) => {
  await boot(page);
  const shown = await page.evaluate(() => ({
    counted: window.badge('consolidated', {presentation_status: 'consolidated', presentation_badge_status: 'partial',
      presentation_label: 'Any words at all', unverified_association_count: 2}),
    worded: window.badge('consolidated', {presentation_status: 'consolidated', presentation_badge_status: 'consolidated',
      presentation_label: 'Consolidated · Unverified', unverified_association_count: 0}),
    unknown: window.badge('queued', {presentation_status: 'a_future_status', presentation_badge_status: 'queued',
      presentation_label: 'A Future Status'}),
  }));
  expect(shown.counted).toContain('Consolidated · Unverified');
  expect(shown.worded).toContain('>Consolidated<');
  expect(shown.worded).not.toContain('Unverified');
  expect(shown.unknown).toContain('A Future Status');                                // the server's words, for a status the catalog does not know
});
