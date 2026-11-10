// @ts-check
const {test, expect} = require('@playwright/test');

/* DebridPulse 1.0.14 localization, WS4b: Settings Help (ui-settings-help.js)
 * -- every topic's title, headings, paragraphs and list items and the role
 * comparison table -- is said from the catalog. The reader's role, deployment
 * type and Easy or Advanced decide which parts a topic has; the language only
 * words them. An open topic is re-said in place on a language change: the
 * same dialog, its scroll and focus stay, nothing is read. The role table's
 * answers are facts (data-answer), never its words.
 *
 * Shown in the test-only pseudo locale en-XA (every message ⟦ … ⟧). Needs an
 * open instance (an Admin); its deployment type is the instance's own. */

const dialog = page => page.locator('.dp-modal-dialog.dp-settings-help-dialog').last();
const setLocale = (page, locale) => page.evaluate(locale => window.DPI18n.setLocale(locale), locale);

async function openSettings(page, width = 1440) {
  await page.setViewportSize({width, height: 900});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  await page.evaluate(() => window.DPI18n.ready);
  await page.locator('#sidebar .nav-item[data-view="settings"]').evaluate(element => element.click());
  await expect(page.locator('#view-settings .dp-settings-tabs')).toBeVisible();
}

async function chooseTab(page, name) {
  await page.locator(`#view-settings .dp-settings-tabs [data-tab="${name}"]:not([inert])`).click();
  await expect(page.locator(`.dp-settings-panel[data-panel="${name}"]`)).toBeVisible();
}

/* What a dialog says: its words not said by a keyed node, keyed nodes left
 * without words, keys shown as themselves, and the runtime's notes. */
const unsaid = page => dialog(page).evaluate(node => {
  const left = [];
  const walker = document.createTreeWalker(node.querySelector('.dp-modal-body'), NodeFilter.SHOW_TEXT);
  for (let text = walker.nextNode(); text; text = walker.nextNode()) {
    if (text.textContent.trim() && !text.parentElement.closest('[data-i18n], svg')) left.push(text.textContent.trim());
  }
  const keyed = [...node.querySelectorAll('[data-i18n]')];
  return {left, empty: keyed.filter(each => !each.textContent.trim()).map(each => each.dataset.i18n),
    keys: keyed.filter(each => each.textContent.trim() === each.dataset.i18n).map(each => each.dataset.i18n),
    notes: window.DPI18n.diagnostics()};
});
const parts = page => dialog(page).evaluate(node => [...node.querySelectorAll('.dp-modal-body [data-i18n]')].map(each => each.dataset.i18n));

test('L15-WS4b: an open Settings Help topic is re-said in place; the dialog, its scroll, focus and the role answers stay', async ({page}) => {
  const server = await page.request.get('/api/health');
  expect(server.ok()).toBe(true);
  await openSettings(page);
  const context = await page.evaluate(() => window.debridPulseAuth.presentation.context());
  // User Accounts on a Server, its Standalone counterpart (Access & Integrations) otherwise.
  const topic = context.standalone ? 'integration-accounts' : 'user-accounts';
  await chooseTab(page, 'users');
  await page.locator(`#view-settings [data-settings-help="${topic}"]`).first().click();
  const help = dialog(page);
  await expect(help).toBeVisible();
  const english = {title: await help.locator('.dp-modal-title').innerText(), body: await help.locator('.dp-modal-body').innerText()};
  const answers = await help.evaluate(node => [...node.querySelectorAll('[data-answer]')].map(each => each.dataset.answer));
  await help.evaluate(node => {
    node.dataset.testMark = 'kept';
    const body = node.querySelector('.dp-modal-body');
    body.scrollTop = Math.min(400, body.scrollHeight - body.clientHeight);
  });
  await help.locator('[data-modal-close]').focus();
  const before = await help.evaluate(node => node.querySelector('.dp-modal-body').scrollTop);
  const asked = [];
  page.on('request', request => asked.push(request.url()));

  await setLocale(page, 'en-XA');
  await expect(help.locator('.dp-modal-title')).toContainText('⟦');
  const body = await help.locator('.dp-modal-body').innerText();
  expect(body).toContain('⟦');
  expect(body).not.toContain(english.body.split('\n')[0]);
  const after = await help.evaluate(node => ({mark: node.dataset.testMark, scroll: node.querySelector('.dp-modal-body').scrollTop,
    answers: [...node.querySelectorAll('[data-answer]')].map(each => each.dataset.answer), dialogs: document.querySelectorAll('.dp-settings-help-dialog').length}));
  expect(after.mark).toBe('kept');                       // the same dialog, not opened again
  expect(after.dialogs).toBe(1);
  expect(after.answers).toEqual(answers);                // the facts, whatever the words
  if (before > 0) expect(after.scroll).toBeGreaterThan(0);
  await expect(help.locator('[data-modal-close]')).toBeFocused();
  expect(await help.locator('[data-modal-close]').getAttribute('aria-label')).toContain('⟦');
  expect(await unsaid(page)).toEqual({left: [], empty: [], keys: [], notes: []});
  expect(asked.filter(url => /\/i18n\/|settings|help|account|integrations|auth/.test(url))).toEqual([]);

  await setLocale(page, 'en');
  expect(await help.locator('.dp-modal-title').innerText()).toBe(english.title);
  expect(await help.locator('.dp-modal-body').innerText()).toBe(english.body);
  await page.keyboard.press('Escape');
  await expect(help).toHaveCount(0);
});

test('L14/L16-WS4b: every topic, for every reader, is said whole in en-XA with the same parts as in English', async ({page}) => {
  await openSettings(page);
  const EASY = ['activity-log', 'provider-additional-settings', 'download-behavior-advanced', 'transfer-method-settings',
    'disk-space-recovery', 'download-engine-activity', 'username-password', 'openid-connect', 'deployment-type',
    'statistics-reporting', 'event-logging', 'backup-additional-settings', 'database-reset'];
  const roles = {admin: [true, false, true, true], user: [false, true, true, true], readonly: [false, false, false, false]};
  /* The reader's context is the test's (auth.js presentation.context's facts);
   * which parts each topic has, and their words, are the page's own. */
  const read = (given, locale) => page.evaluate(async ({given, EASY, locale}) => {
    await window.DPI18n.setLocale(locale);
    const real = window.__realAuth || (window.__realAuth = window.debridPulseAuth);
    const shows = part => !(given.mode === 'easy' && EASY.includes(part));
    window.debridPulseAuth = Object.freeze({...real, presentation: Object.freeze({...real.presentation, context: () => Object.freeze({
      ...given, standalone: given.deployment === 'standalone', server: given.deployment !== 'standalone',
      easy: given.mode === 'easy', advanced: given.mode !== 'easy', shows, omits: part => !shows(part)})})});
    const found = {};
    for (const topic of window.DPSettingsHelp.topics) {
      const handle = window.DPSettingsHelp.open(topic, topic === 'my-account' ? {account: {presentation: true}} : {});
      const node = [...document.querySelectorAll('.dp-settings-help-dialog')].pop();
      const left = [];
      const walker = document.createTreeWalker(node.querySelector('.dp-modal-body'), NodeFilter.SHOW_TEXT);
      for (let text = walker.nextNode(); text; text = walker.nextNode()) {
        if (text.textContent.trim() && !text.parentElement.closest('[data-i18n], svg')) left.push(text.textContent.trim());
      }
      const keyed = [...node.querySelectorAll('[data-i18n]')];
      found[topic] = {parts: [...node.querySelectorAll('.dp-modal-body [data-i18n]')].map(each => each.dataset.i18n),
        answers: [...node.querySelectorAll('[data-answer]')].map(each => each.dataset.answer).join(),
        title: node.querySelector('.dp-modal-title').textContent, left,
        bad: keyed.filter(each => !each.textContent.trim() || each.textContent.trim() === each.dataset.i18n).map(each => each.dataset.i18n)};
      handle.close();
      await new Promise(resolve => setTimeout(resolve, 0));
    }
    window.debridPulseAuth = real;
    return found;
  }, {given, EASY, locale});
  let topics = 0;
  for (const [role, [admin, personal, submit, operate]] of Object.entries(roles)) {
    for (const deployment of ['server', 'standalone']) {
      for (const mode of ['advanced', 'easy']) {
        const given = {role, deployment, mode, admin, personal, submit, operate};
        const english = await read(given, 'en');
        const pseudo = await read(given, 'en-XA');
        for (const [topic, said] of Object.entries(pseudo)) {
          const at = `${role}/${deployment}/${mode} ${topic}`;
          expect(said.parts, at).toEqual(english[topic].parts);       // the language never adds or drops a part
          expect(said.answers, at).toEqual(english[topic].answers);
          expect(said.left, at).toEqual([]);
          expect(said.bad, at).toEqual([]);
          expect(said.title, at).toContain('⟦');
          topics += 1;
        }
      }
    }
  }
  expect(topics).toBe(12 * 33);
  expect(await page.evaluate(() => window.DPI18n.diagnostics())).toEqual([]);
  await setLocale(page, 'en');
});

test('L11-WS4b: the deployment switch warning keeps its whole consequence in every language', async ({page}) => {
  await openSettings(page);
  const open = () => page.evaluate(() => { window.DPSettingsHelp.open('deployment-type'); });
  await open();
  const warning = 'help.topic.deployment_type.what_switching_changes.afterwards_no_sign';
  const english = await dialog(page).locator(`[data-i18n="${warning}"]`).innerText();
  expect(english).toContain('anyone who can reach DebridPulse');
  expect(english).toContain('it does not expire');
  const englishParts = await parts(page);
  await setLocale(page, 'en-XA');
  expect(await parts(page)).toEqual(englishParts);
  const pseudo = await dialog(page).locator(`[data-i18n="${warning}"]`).innerText();
  // en-XA accents every letter and lengthens: the same sentence, whole, in the new words.
  expect(pseudo).toContain('⟦');
  expect(pseudo.length).toBeGreaterThan(english.length);
  expect(await unsaid(page)).toEqual({left: [], empty: [], keys: [], notes: []});
  await setLocale(page, 'en');
});

test('L24-WS4b: at 320 px the role answers read capability by capability, in en-XA, inside the dialog', async ({page}) => {
  await openSettings(page, 320);
  await setLocale(page, 'en-XA');
  await page.evaluate(() => { window.DPSettingsHelp.open('user-accounts'); });
  const help = dialog(page);
  await expect(help).toBeVisible();
  const fit = await help.evaluate(node => {
    const box = node.getBoundingClientRect();
    const list = node.querySelector('.dp-settings-help-roles');
    const items = [...list.querySelectorAll('.dp-settings-help-role-item')];
    return {fits: box.left >= -0.5 && box.right <= innerWidth + 0.5, overflow: document.documentElement.scrollWidth - innerWidth,
      listShown: getComputedStyle(list).display !== 'none', items: items.length,
      region: list.getAttribute('aria-label'), said: items.every(item => item.querySelector('h4').textContent.includes('⟦')),
      answers: [...list.querySelectorAll('dd[data-answer]')].map(each => each.dataset.answer).join()};
  });
  expect(fit.fits).toBe(true);
  expect(fit.overflow).toBeLessThanOrEqual(0);
  expect(fit.listShown).toBe(true);
  expect(fit.items).toBe(10);
  expect(fit.region).toContain('⟦');
  expect(fit.said).toBe(true);
  expect(fit.answers.split(',').every(answer => answer === 'yes' || answer === 'no')).toBe(true);
  expect(await unsaid(page)).toEqual({left: [], empty: [], keys: [], notes: []});
});
