const { test, expect } = require('@playwright/test');

/* 1.0.14: the sign-in page's appearance is the installation's, and the product
 * logo follows the theme in force.
 *
 * Password-auth runtime. The login appearance is what an Admin last published
 * (Login Theme, Set current, at the end of the theme menu); a personal theme
 * choice never changes it. This file publishes with an Admin of its own (a
 * run-unique synthetic account) and leaves it as it found it. The page is
 * rendered in it by the server, so neither the browser's colour-scheme
 * preference, nor its stored sign-in-off theme, nor script takes part. Below
 * 701px the card stays centred against the artwork while it fits and scrolls
 * whole when it does not; from 701px it is placed as before. The sidebar's
 * mark is logo.svg drawn inline in the signed-in account's own theme.
 *
 * Every sign-in page counts against the login challenge limit (20 per client
 * per minute, auth/throttle.py): this file loads few, and resizes one page for
 * its geometry. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const ADMIN = {username: 'browser', password: 'browser-secret'};
const run = Date.now().toString(36);
const adminSession = require('./acl-admin-session');

const fonts = page => page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));

async function signIn(browser, {username, password}) {
  const shared = username === ADMIN.username;
  const reuse = shared && adminSession.get() !== null;
  const context = await browser.newContext({baseURL: AUTH, viewport: {width: 1440, height: 914},
    storageState: reuse ? adminSession.get() : undefined});
  const page = await context.newPage();
  await fonts(page);
  if (reuse) await page.goto('/');
  else {
    await page.goto('/login');
    await page.locator('#username').fill(username);
    await page.locator('#password').fill(password);
    await page.locator('button[type="submit"]').click();
  }
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.locator('#sidebar-auth-row .dp-sidebar-profile')).toBeVisible();
  if (shared && !reuse) adminSession.remember(await context.storageState());
  return {context, page};
}

const api = (page, method, path, body) => page.evaluate(async ([method, path, body]) => {
  const response = await window.debridPulseAuth.fetch(`/api${path}`, {
    method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  return {status: response.status, body: await response.json().catch(() => null)};
}, [method, path, body]);

async function person(admin, name, role) {
  const account = {username: `la-${name}-${run}`, password: `la-${name}-${run}-pass`};
  const created = await api(admin.page, 'POST', '/accounts', {kind: 'local', role, display_name: `${name} ${run}`, ...account});
  expect(created.status).toBe(200);
  return {...account, id: created.body.id};
}

// An account's own theme, then -- for an Admin -- that theme published as the login appearance.
const own = (session, mode, palette) => api(session.page, 'PATCH', '/account/appearance', {mode, [`${mode}_palette`]: palette});
const publish = (session, mode, palette) => api(session.page, 'PUT', '/login-appearance', {mode, palette});

// What a sign-in page draws: its mode and palette, and colours resolved from them.
const drawn = page => page.evaluate(() => {
  const css = (el, prop) => getComputedStyle(el).getPropertyValue(prop).trim();
  const mark = document.querySelector('.brand-mark');
  return {
    mode: document.documentElement.dataset.theme, light: document.body.classList.contains('light'),
    palette: document.body.getAttribute('data-dp-palette') || '', scheme: css(document.documentElement, 'color-scheme'),
    bg: css(document.body, '--bg'), accent: css(document.body, '--accent'),
    square: getComputedStyle(mark.querySelector('.dp-logo-surface')).fill,
    arrow: getComputedStyle(mark.querySelector('.dp-logo-arrow-start')).stopColor,
    error: document.querySelector('.error') ? getComputedStyle(document.querySelector('.error')).color : null,
  };
});

async function publicPage(browser, {colorScheme = 'light', javaScriptEnabled = true, stored = null} = {}) {
  const context = await browser.newContext({baseURL: AUTH, colorScheme, javaScriptEnabled, viewport: {width: 1024, height: 900},
    storageState: stored ? {cookies: [], origins: [{origin: new URL(AUTH).origin, localStorage: [{name: 'theme', value: stored}]}]} : undefined});
  const page = await context.newPage();
  await page.goto('/login');
  return {context, page};
}

test('the sign-in page is drawn in the last published login theme, whatever the browser prefers', async ({browser}) => {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const admin = await signIn(browser, ADMIN);
  const selector = await person(admin, 'Selector', 'admin');
  const reader = await person(admin, 'Reader', 'user');
  const first = await publicPage(browser);
  const found = await drawn(first.page);
  await first.context.close();
  const mine = await signIn(browser, selector);
  const user = await signIn(browser, reader);
  try {
    // The Admin's own choice, and a User's: personal, the sign-in page unchanged.
    expect((await own(mine, 'light', 'catppuccin-latte')).status).toBe(200);
    expect((await own(user, 'dark', 'dracula')).status).toBe(200);
    const still = await publicPage(browser);
    expect(await drawn(still.page)).toEqual(found);
    await still.context.close();
    // Published: Light, Catppuccin Latte.
    expect((await publish(mine, 'light', 'catppuccin-latte')).body).toEqual({mode: 'light', palette: 'catppuccin-latte'});

    const latte = {mode: 'light', light: true, palette: 'catppuccin-latte', scheme: 'light', bg: '#e6e9ef', accent: '#8839ef',
      square: 'rgb(239, 241, 245)', arrow: 'rgb(136, 57, 239)', error: null};
    // A dark-preferring browser, a light one, one holding a sign-in-off "dark" theme, one without script.
    for (const options of [{colorScheme: 'dark'}, {colorScheme: 'light'}, {colorScheme: 'light', stored: 'dark'},
      {colorScheme: 'dark', javaScriptEnabled: false}]) {
      const {context, page} = await publicPage(browser, options);
      expect(await drawn(page), JSON.stringify(options)).toEqual(latte);
      await context.close();
    }

    // A refused sign-in is drawn in it too, its error keeping DebridPulse's own colour.
    const {context, page} = await publicPage(browser, {colorScheme: 'dark'});
    await page.locator('#username').fill(`la-nobody-${run}`);
    await page.locator('#password').fill('not-the-password');
    await page.locator('button[type="submit"]').click();
    await expect(page.locator('.error')).toHaveText('Invalid username or password.');
    expect(await drawn(page)).toEqual({...latte, error: 'rgb(230, 66, 85)'});
    await context.close();

    // The Admin's next publication replaces it.
    expect((await own(mine, 'dark', 'dracula')).status).toBe(200);
    expect((await publish(mine, 'dark', 'dracula')).status).toBe(200);
    const next = await publicPage(browser, {colorScheme: 'light'});
    expect(await drawn(next.page)).toMatchObject({mode: 'dark', light: false, palette: 'dracula', scheme: 'dark', bg: '#191a21',
      square: 'rgb(40, 42, 54)'});
    await next.context.close();
  } finally {
    // Leave the installation's login appearance as this file found it.
    const palette = found.palette || `debridpulse-${found.mode}`;
    await own(mine, found.mode, palette);
    await publish(mine, found.mode, palette);
    for (const session of [mine, user]) await session.context.close();
    for (const account of [selector, reader]) await api(admin.page, 'DELETE', `/accounts/${account.id}`);
    await admin.context.close();
  }
});

test('below 701px the card is centred while it fits and scrolls whole when it does not; from 701px it is where it was', async ({browser}) => {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const {context, page} = await publicPage(browser);
  const place = () => page.evaluate(() => {
    scrollTo(0, 0);
    const top = document.querySelector('.card').getBoundingClientRect();
    const facts = {dx: (top.left + top.right) / 2 - innerWidth / 2, dy: (top.top + top.bottom) / 2 - innerHeight / 2,
      fits: top.height <= innerHeight, top: top.top, wide: document.scrollingElement.scrollWidth - innerWidth};
    scrollTo(0, document.scrollingElement.scrollHeight);
    const card = document.querySelector('.card').getBoundingClientRect();
    facts.bottom = card.bottom - innerHeight;
    // The version follows the card, as the app's follows its master surface: its text (a Range over it, not
    // its box) just below the card's bottom edge, its right on the card's outer right edge, never in the card.
    const label = document.querySelector('.version');
    const range = document.createRange(); range.selectNodeContents(label);
    const glyph = range.getBoundingClientRect();
    facts.version = {inCard: document.querySelector('.card').contains(label), below: glyph.top - card.bottom,
      right: Math.abs(card.right - glyph.right), onScreen: glyph.bottom <= innerHeight + 0.5};
    scrollTo(0, 0);
    return facts;
  });
  for (const width of [320, 360, 390, 430, 460, 560, 699, 700]) {
    await page.setViewportSize({width, height: 844});
    const facts = await place();
    expect(facts.fits, `${width}`).toBe(true);
    expect(Math.abs(facts.dx), `${width}`).toBeLessThanOrEqual(0.5);
    expect(Math.abs(facts.dy), `${width}`).toBeLessThanOrEqual(0.5);
    expect(facts.wide, `${width}`).toBeLessThanOrEqual(0);
    expect(facts.version.inCard, `${width}`).toBe(false);
    expect(facts.version.below, `${width}`).toBeGreaterThan(0);
    expect(facts.version.below, `${width}`).toBeLessThanOrEqual(10);
    expect(facts.version.right, `${width}`).toBeLessThanOrEqual(1);
  }
  for (const [width, height] of [[320, 568], [390, 600], [667, 375]]) {
    await page.setViewportSize({width, height});
    const facts = await place();
    expect(facts.fits, `${width}x${height}`).toBe(false);
    expect(facts.top, `${width}x${height}`).toBeGreaterThanOrEqual(0);          // its top is on screen
    expect(facts.bottom, `${width}x${height}`).toBeLessThanOrEqual(0.5);       // and its end is reached by scrolling
    expect(facts.wide, `${width}x${height}`).toBeLessThanOrEqual(0);
    expect(facts.version.inCard, `${width}x${height}`).toBe(false);
    expect(facts.version.below, `${width}x${height}`).toBeGreaterThan(0);
    expect(facts.version.right, `${width}x${height}`).toBeLessThanOrEqual(1);
    expect(facts.version.onScreen, `${width}x${height}`).toBe(true);        // reached by scrolling, with the card
  }
  // From 701px the page's own padding (58px over 42px) places it, as before.
  for (const width of [701, 780, 1024, 1440]) {
    await page.setViewportSize({width, height: 900});
    const facts = await place();
    expect(facts.dx, `${width}`).toBeCloseTo(0, 2);
    expect(facts.dy, `${width}`).toBeCloseTo(8, 2);
    expect(facts.version.inCard, `${width}`).toBe(false);
    expect(facts.version.below, `${width}`).toBeGreaterThan(0);
    expect(facts.version.below, `${width}`).toBeLessThanOrEqual(10);
    expect(facts.version.right, `${width}`).toBeLessThanOrEqual(1);
  }
  await context.close();
});

test('the sidebar logo is logo.svg drawn inline in the signed-in account\'s own theme, live', async ({browser}) => {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const admin = await signIn(browser, ADMIN);
  const viewer = await person(admin, 'Viewer', 'readonly');
  const own = await signIn(browser, viewer);
  try {
    const logo = () => own.page.evaluate(() => {
      const host = document.querySelector('#sidebar dp-logo.logo-icon');
      const svg = host.querySelector('svg');
      const box = host.getBoundingClientRect();
      return {img: !!document.querySelector('#sidebar img.logo-icon'), label: host.getAttribute('aria-label'),
        hidden: svg.getAttribute('aria-hidden'), size: [box.width, box.height],
        square: getComputedStyle(svg.querySelector('.dp-logo-surface')).fill,
        outline: getComputedStyle(svg.querySelector('.dp-logo-outline-start')).stopColor,
        ekg: ['start', 'mid', 'end'].map(stop => getComputedStyle(svg.querySelector(`.dp-logo-ekg-${stop}`)).stopColor)};
    });
    const native = await logo();
    expect(native).toMatchObject({img: false, label: 'DebridPulse logo', hidden: 'true', square: 'rgb(8, 11, 24)',
      outline: 'rgb(166, 44, 255)', ekg: ['rgb(181, 50, 255)', 'rgb(0, 212, 255)', 'rgb(20, 159, 255)']});
    await own.page.evaluate(() => window.DPTheme.choose('gruvbox-dark'));
    expect(await logo()).toMatchObject({square: 'rgb(40, 40, 40)', outline: 'rgb(211, 134, 155)', ekg: ['rgb(211, 134, 155)', 'rgb(131, 165, 152)', 'rgb(131, 165, 152)'], size: native.size});
    await own.page.evaluate(() => window.DPTheme.setMode(true));
    expect(await logo()).toMatchObject({square: 'rgb(255, 255, 255)', outline: 'rgb(166, 44, 255)', ekg: native.ekg, size: native.size});
    await own.page.evaluate(() => window.DPTheme.choose('flexoki-light'));
    const flexoki = await logo();
    expect(flexoki.square).not.toBe('rgb(255, 255, 255)');
    expect(flexoki.outline).not.toBe('rgb(166, 44, 255)');
    expect(flexoki.ekg).toEqual(['rgb(157, 67, 16)', 'rgb(32, 94, 166)', 'rgb(32, 94, 166)']);
  } finally {
    await own.context.close();
    await api(admin.page, 'DELETE', `/accounts/${viewer.id}`);
    await admin.context.close();
  }
});

test('an Admin publishes the login theme from the end of the theme menu, and only by Set current', async ({browser}) => {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const admin = await signIn(browser, ADMIN);
  const publisher = await person(admin, 'Publisher', 'admin');
  const viewer = await person(admin, 'Onlooker', 'user');
  const first = await publicPage(browser);
  const found = await drawn(first.page);
  await first.context.close();
  const loginTheme = async () => {
    const {context, page} = await publicPage(browser);
    const {mode, palette} = await drawn(page);
    await context.close();
    return {mode, palette: palette || `debridpulse-${mode}`};
  };
  const user = await signIn(browser, viewer);
  const mine = await signIn(browser, publisher);
  const page = mine.page;
  const section = page.locator('#theme-picker .dp-theme-picker-login');
  const set = section.locator('button');
  const value = section.locator('.dp-theme-picker-login-value');
  const open = async () => {
    if (!(await page.locator('#theme-picker').evaluate(menu => menu.matches(':popover-open')))) await page.click('#theme-picker-toggle');
    await expect(value).not.toHaveText('Checking…');
  };
  try {
    // Only an Admin's menu has it.
    await user.page.click('#theme-picker-toggle');
    await expect(user.page.locator('#theme-picker .dp-theme-picker-option').first()).toBeVisible();
    await expect(user.page.locator('#theme-picker .dp-theme-picker-login')).toHaveCount(0);

    // The Admin's menu ends with it: what the sign-in page shows, and Set current, a canonical rounded rectangle.
    const header = () => page.evaluate(() => ['#theme-toggle', '#theme-picker-toggle', '#topbar-account-button']
      .map(s => document.querySelector(s)).filter(Boolean).map(el => JSON.stringify(el.getBoundingClientRect())));
    const before = await header();
    await open();
    expect(await header()).toEqual(before);
    await expect(section.locator('.dp-theme-picker-login-label')).toHaveText('Current login theme');
    const label = (mode, palette) => page.evaluate(([mode, palette]) =>
      `${window.DPTheme.catalog[mode].find(item => item.id === palette).label} · ${mode === 'light' ? 'Light' : 'Dark'}`, [mode, palette]);
    await expect(value).toHaveText(await label(found.mode, found.palette || `debridpulse-${found.mode}`));
    const shape = await set.evaluate(button => ({classes: button.className, radius: parseFloat(getComputedStyle(button).borderRadius),
      height: button.getBoundingClientRect().height, token: parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--dp-radius-sm'))}));
    expect(shape.classes).toContain('dp-btn dp-btn--secondary dp-btn--sm');
    expect(shape.radius).toBe(shape.token);
    expect(shape.radius).toBeLessThan(shape.height / 2);                     // never a pill
    // It is last in the menu, after every row, and the keyboard reaches it from the rows.
    expect(await page.evaluate(() => {
      const panel = document.querySelector('#theme-picker .dp-theme-picker-panel');
      return panel.lastElementChild.classList.contains('dp-theme-picker-login');
    })).toBe(true);

    // A personal choice is personal: the sign-in page is unchanged.
    await page.locator('#theme-picker .dp-theme-picker-option[data-palette="dracula"]').click();
    await expect.poll(() => api(page, 'GET', '/account/appearance').then(r => r.body.dark_palette)).toBe('dracula');
    expect(await loginTheme()).toEqual({mode: found.mode, palette: found.palette || `debridpulse-${found.mode}`});

    // Set current while the personal save is still on its way: what is shown is what is published.
    await open();
    await page.evaluate(() => {
      window.DPTheme.choose('gruvbox-dark');
      document.querySelector('#theme-picker .dp-theme-picker-login button').click();
    });
    await expect(value).toHaveText('Gruvbox Dark · Dark');
    await expect(set).toHaveText('Current');
    await expect(set).toBeDisabled();
    expect(await loginTheme()).toEqual({mode: 'dark', palette: 'gruvbox-dark'});

    // A failed publication says so and leaves the published theme.
    await page.evaluate(() => window.DPTheme.choose('kanagawa-wave'));
    await expect(set).toHaveText('Set current');
    await page.route('**/api/login-appearance', route => route.request().method() === 'PUT'
      ? route.fulfill({status: 500, contentType: 'application/json', body: '{"detail":"boom"}'}) : route.continue());
    await set.click();
    await expect(section.locator('.dp-theme-picker-login-note')).toHaveText('The login theme could not be set; it is unchanged.');
    await expect(value).toHaveText('Gruvbox Dark · Dark');
    await page.unroute('**/api/login-appearance');
    expect(await loginTheme()).toEqual({mode: 'dark', palette: 'gruvbox-dark'});

    // Keyboard: End reaches Set current; Escape closes and returns focus to the chevron.
    await page.locator('#theme-picker .dp-theme-picker-option[aria-checked="true"]').focus();
    await page.keyboard.press('End');
    await expect(set).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(page.locator('#theme-picker-toggle')).toBeFocused();

    // Desktop and handheld: the section is in reach -- in the panel's foot below 780px, at the menu's end above.
    for (const [width, height] of [[1440, 900], [780, 900], [779, 844], [390, 844], [320, 568]]) {
      await page.setViewportSize({width, height});
      await page.waitForTimeout(300);
      const fixed = await header();
      await open();
      await page.waitForTimeout(500);
      expect(await header(), `${width}x${height}`).toEqual(fixed);
      const reach = await page.evaluate(() => {
        const menu = document.getElementById('theme-picker');
        menu.scrollTop = menu.scrollHeight;
        const s = document.querySelector('#theme-picker .dp-theme-picker-login').getBoundingClientRect();
        const m = menu.querySelector('.dp-theme-picker-panel').getBoundingClientRect();
        const box = menu.getBoundingClientRect();
        return {inView: s.top >= 0 && s.bottom <= innerHeight + 0.5, inMenu: s.top >= box.top - 0.5 && s.bottom <= box.bottom + 0.5,
          inPanel: s.bottom <= m.bottom + 0.5};
      });
      expect(reach, `${width}x${height}`).toEqual({inView: true, inMenu: true, inPanel: true});
      await page.keyboard.press('Escape');
      await page.waitForTimeout(500);
    }
  } finally {
    const palette = found.palette || `debridpulse-${found.mode}`;
    await own(mine, found.mode, palette);
    await publish(mine, found.mode, palette);
    for (const session of [mine, user]) await session.context.close();
    for (const account of [publisher, viewer]) await api(admin.page, 'DELETE', `/accounts/${account.id}`);
    await admin.context.close();
  }
});
