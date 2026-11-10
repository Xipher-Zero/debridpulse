// @ts-check
const {test, expect} = require('@playwright/test');

/* DebridPulse 1.0.14 localization, WS4a: the Field Manual (the Help page), the
 * License viewer's own words and the shared section navigator's tooltip are
 * said from the catalog. The reader's role, deployment type and Easy or
 * Advanced decide which passages exist; the language only words them. A
 * language change re-says the open manual in place -- the section shown, an
 * open accordion, the scroll and focus stay, nothing is composed again and
 * nothing is requested -- and the bundled legal documents stay exactly as
 * bundled.
 *
 * Shown in the test-only pseudo locale en-XA (every message ⟦ … ⟧). Needs an
 * open instance (an Admin); its deployment type is the instance's own. Easy
 * is chosen through the account's own presentation and returned to Advanced. */

const help = '#view-help';
const live = selector => `${help} ${selector}:not(.dp-motion-still *)`;

async function boot(page, width = 1440) {
  await page.setViewportSize({width, height: 900});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  await page.evaluate(() => window.DPI18n.ready);
  await page.locator('#sidebar .nav-item[data-view="help"]').evaluate(element => element.click());
  await expect(page.locator(live('.dp-help-tabs'))).toBeVisible();
}

const setLocale = (page, locale) => page.evaluate(locale => window.DPI18n.setLocale(locale), locale);
const settled = page => page.waitForFunction(() => !document.querySelector('#view-help .dp-motion-surface')
  && document.getAnimations().every(animation => animation.playState !== 'running'));

async function chooseSection(page, id) {
  await page.locator(live(`.dp-help-tab[data-tab="${id}"]`)).click();
  await settled(page);
  await expect(page.locator(`#dp-help-panel-${id}`)).toBeVisible();
}

/* The manual's composition: which passages (keys) each section has, in order. */
const passages = page => page.evaluate(() => Object.fromEntries([...document.querySelectorAll('#view-help .dp-help-panel')]
  .filter(panel => !panel.closest('.dp-motion-still'))
  .map(panel => [panel.dataset.panel, [...panel.querySelectorAll('[data-i18n]')].map(node => node.dataset.i18n)])));

/* Words of the manual not said by a keyed node (the decorative step numbers and
 * pipeline arrows aside), keyed nodes left without words, keys shown as
 * themselves, and the runtime's notes. */
const unsaid = page => page.evaluate(() => {
  const view = document.getElementById('view-help');
  const left = [];
  const walker = document.createTreeWalker(view.querySelector('.dp-help-panels'), NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.textContent.trim();
    if (text && !node.parentElement.closest('[data-i18n], svg, .dp-help-step-number, .dp-help-pipeline-arrow')) left.push(text);
  }
  const keyed = [...view.querySelectorAll('[data-i18n]')];
  const keys = keyed.filter(node => node.textContent.trim() === node.dataset.i18n).map(node => node.dataset.i18n);
  const empty = keyed.filter(node => !node.textContent.trim()).map(node => node.dataset.i18n);
  return {left, empty, keys, notes: window.DPI18n.diagnostics()};
});

test('L14-WS4a: a language change re-says the open manual in place; section, accordion, scroll and focus stay', async ({page}) => {
  await boot(page);
  await chooseSection(page, 'howitworks');
  const panel = page.locator('#dp-help-panel-howitworks');
  // An accordion opened by the reader, the manual scrolled, focus on its title.
  const failing = panel.locator('details.dp-help-accordion', {has: page.locator('summary[data-i18n="help.howitworks.when_something_fails.title"]')});
  await failing.locator('summary').click();
  await expect(failing).toHaveAttribute('open', '');
  await settled(page);
  const english = await panel.innerText();
  await page.evaluate(() => {
    const scroller = document.querySelector('#view-help .dp-help-scroll');
    scroller.scrollTop = Math.min(600, scroller.scrollHeight - scroller.clientHeight);
    // The same nodes must survive a language change: mark them.
    document.querySelector('#dp-help-panel-howitworks').dataset.testMark = 'kept';
  });
  await failing.locator('summary').focus();
  // The reading position: the manual stays scrolled (never back to its top, as a
  // new composition would put it) with the focused title in view. Longer words
  // move what follows them; the browser's scroll anchoring keeps the place.
  const position = () => page.evaluate(() => {
    const scroller = document.querySelector('#view-help .dp-help-scroll').getBoundingClientRect();
    const summary = document.activeElement.getBoundingClientRect();
    return {scroll: document.querySelector('#view-help .dp-help-scroll').scrollTop,
      visible: summary.top >= scroller.top && summary.bottom <= scroller.bottom};
  });
  const before = await position();
  expect(before.scroll).toBeGreaterThan(0);
  expect(before.visible).toBe(true);
  let requests = 0;
  const asked = [];
  page.on('request', request => asked.push(request.url()));
  let rendered = 0;
  await page.evaluate(() => document.addEventListener('debridpulse:help-rendered', () => { window.__helpRendered = (window.__helpRendered || 0) + 1; }));

  await setLocale(page, 'en-XA');
  const pseudo = await panel.innerText();
  expect(pseudo).toContain('⟦');
  expect(pseudo).not.toContain('Specialist services come first.');
  await expect(page.locator(live('.dp-help-tab[data-tab="howitworks"]'))).toHaveAttribute('aria-selected', 'true');
  await expect(failing).toHaveAttribute('open', '');
  await expect(failing.locator('summary')).toBeFocused();
  await expect(failing.locator('summary')).toContainText('⟦');
  const state = await page.evaluate(() => ({
    mark: document.querySelector('#dp-help-panel-howitworks').dataset.testMark,
    rendered: window.__helpRendered || 0,
    tab: document.querySelector('#view-help .dp-help-tab[aria-selected="true"]:not(.dp-motion-still *) .dp-help-tab-label').textContent,
    title: document.querySelector('#view-help .dp-help-header-title').textContent,
    tablist: document.querySelector('#view-help .dp-help-tabs:not(.dp-motion-still)').getAttribute('aria-label'),
  }));
  expect(state.mark).toBe('kept');                       // not composed again
  expect(state.rendered).toBe(rendered);
  const after = await position();
  expect(after.scroll).toBeGreaterThan(0);
  expect(after.visible).toBe(true);
  expect(state.tab).toContain('⟦');
  expect(state.title).toContain('⟦');
  expect(state.tablist).toContain('⟦');
  // Nothing read to say it: no catalog, Help or document request (the page's own
  // live updates keep running beside it).
  expect(asked.filter(url => /\/i18n\/|help|legal-documents|presentation|locale/.test(url))).toEqual([]);

  // And back: the server's English, exactly as before.
  await setLocale(page, 'en');
  expect(await panel.innerText()).toBe(english);
  await expect(failing).toHaveAttribute('open', '');
  await expect(failing.locator('summary')).toBeFocused();
});

test('L14-WS4a: every reader\'s manual is said whole -- each role, deployment and presentation, in en-XA', async ({page}) => {
  await boot(page);
  await setLocale(page, 'en-XA');
  /* Each reader context the manual composes for (auth.js presentation.context's
   * facts), composed by the page's own Help owner. The context is the test's;
   * what the manual says for it, and in which words, is the page's. */
  const EASY = ['activity-log', 'provider-additional-settings', 'download-behavior-advanced', 'transfer-method-settings',
    'disk-space-recovery', 'download-engine-activity', 'username-password', 'openid-connect', 'deployment-type',
    'statistics-reporting', 'event-logging', 'backup-additional-settings', 'database-reset'];
  const roles = {admin: [true, false, true, true], user: [false, true, true, true], readonly: [false, false, false, false]};
  const seen = [];
  for (const [role, [admin, personal, submit, operate]] of Object.entries(roles)) {
    for (const deployment of ['server', 'standalone']) {
      for (const mode of ['advanced', 'easy']) {
        const result = await page.evaluate(({given, EASY}) => {
          const real = window.__realAuth || (window.__realAuth = window.debridPulseAuth);
          const shows = part => !(given.mode === 'easy' && EASY.includes(part));
          window.debridPulseAuth = Object.freeze({...real, presentation: Object.freeze({...real.presentation, context: () => Object.freeze({
            ...given, standalone: given.deployment === 'standalone', server: given.deployment !== 'standalone',
            easy: given.mode === 'easy', advanced: given.mode !== 'easy', shows, omits: part => !shows(part)})})});
          window.DPHelpPage.load();
          return [...document.querySelectorAll('#view-help .dp-help-tab-label:not(.dp-motion-still *)')].map(label => label.dataset.i18n).join(',');
        }, {given: {role, deployment, mode, admin, personal, submit, operate}, EASY});
        const found = await unsaid(page);
        expect(found, `${role}/${deployment}/${mode}`).toEqual({left: [], empty: [], keys: [], notes: []});
        seen.push(result);
      }
    }
  }
  // The section strips differ by reader (an Admin's Settings, a User's Notifications, none for ReadOnly).
  expect(new Set(seen).size).toBe(3);
  await page.evaluate(() => { window.debridPulseAuth = window.__realAuth; window.DPHelpPage.load(); });
});

test('L16-WS4a: the language never changes which sections or passages a reader has (Advanced and Easy)', async ({page}) => {
  await boot(page);
  const put = mode => page.evaluate(mode => window.debridPulseAuth.fetch('/api/account/presentation', {
    method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({presentation_mode: mode})})
    .then(response => response.status), mode);
  try {
    for (const mode of ['advanced', 'easy']) {
      expect(await put(mode)).toBe(200);
      await page.reload();
      await page.evaluate(() => window.DPI18n.ready);
      await page.locator('#sidebar .nav-item[data-view="help"]').evaluate(element => element.click());
      await expect(page.locator(live('.dp-help-tabs'))).toBeVisible();
      expect(await page.evaluate(() => window.debridPulseAuth.presentation.context().mode)).toBe(mode);
      const english = await passages(page);
      const tabs = await page.locator(live('.dp-help-tab')).evaluateAll(nodes => nodes.map(node => node.dataset.tab));
      await setLocale(page, 'en-XA');
      expect(await passages(page), mode).toEqual(english);
      expect(await page.locator(live('.dp-help-tab')).evaluateAll(nodes => nodes.map(node => node.dataset.tab))).toEqual(tabs);
      expect(await unsaid(page)).toEqual({left: [], empty: [], keys: [], notes: []});
      // Easy hides the Advanced-only Activity Log topic, in every language.
      const activity = 'help.using.activity_log_admin.title';
      const context = await page.evaluate(() => window.debridPulseAuth.presentation.context());
      expect(english.using.includes(activity)).toBe(context.admin && mode === 'advanced');
      await setLocale(page, 'en');
    }
  } finally {
    await put('advanced');
  }
});

test('L17-WS4a: the License viewer\'s words follow the language; the bundled document stays exactly as bundled', async ({page}) => {
  await boot(page);
  await chooseSection(page, 'license');
  const opener = page.locator('#dp-help-panel-license [data-legal-document="gpl"]');
  await opener.click();
  const dialog = page.locator('.dp-help-legal-dialog');
  await expect(dialog.locator('.dp-help-legal-latest a')).toBeVisible();
  const documentText = await dialog.locator('.dp-help-legal-document').innerText();
  const title = await dialog.locator('#dp-help-legal-dialog-title').innerText();
  expect(documentText).toMatch(/GNU GENERAL PUBLIC LICENSE/);
  await expect(dialog.locator('.dp-help-legal-latest')).toContainText('This is the copy bundled with DebridPulse');
  const asked = [];
  page.on('request', request => asked.push(request.url()));

  await setLocale(page, 'en-XA');
  await expect(dialog.locator('.dp-help-legal-latest')).toContainText('⟦');
  await expect(dialog.locator('.dp-help-legal-latest a')).toContainText('⟦');
  expect(await dialog.locator('.dp-help-legal-close').getAttribute('aria-label')).toContain('⟦');
  // The document, and its own title, are the legally authoritative text: unchanged.
  expect(await dialog.locator('.dp-help-legal-document').innerText()).toBe(documentText);
  expect(await dialog.locator('#dp-help-legal-dialog-title').innerText()).toBe(title);
  // The page around it is said too, and nothing was read to say it.
  await expect(page.locator('#dp-help-panel-license [data-legal-document="gpl"]')).toContainText('⟦');
  expect(asked.filter(url => /\/i18n\/|help|legal-documents|presentation|locale/.test(url))).toEqual([]);
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(opener).toBeFocused();
});

test('L24-WS4a: at 320 px the manual\'s header stays one row in en-XA and the navigator says its section', async ({page}) => {
  await boot(page, 320);
  await setLocale(page, 'en-XA');
  await settled(page);
  const geometry = await page.evaluate(() => {
    const header = document.querySelector('#view-help .dp-help-master-header');
    const nav = header.querySelector('.dp-help-nav');
    const rows = [...header.children].filter(child => child.getClientRects().length)
      .map(child => Math.round(child.getBoundingClientRect().top));
    return {rows: new Set(rows).size, overflow: document.scrollingElement.scrollWidth - innerWidth,
      navRight: nav.getBoundingClientRect().right, headerRight: header.getBoundingClientRect().right,
      tooltip: header.querySelector('.dp-section-nav-control').dataset.tooltip || ''};
  });
  expect(geometry.rows).toBe(1);
  expect(geometry.overflow).toBeLessThanOrEqual(0);
  expect(geometry.navRight).toBeLessThanOrEqual(geometry.headerRight + 0.5);
  // The compact navigator says which section is shown, in the shown language.
  expect(geometry.tooltip).toContain('⟦');
  expect(await unsaid(page)).toEqual({left: [], empty: [], keys: [], notes: []});
});
