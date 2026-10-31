const { test, expect } = require('@playwright/test');

/* 1.0.14: the sign-in page's appearance is the installation's, and the product
 * logo follows the theme in force.
 *
 * Password-auth runtime. The login appearance is the last effective selection
 * an eligible Admin committed: this file selects it with an Admin of its own
 * (a run-unique synthetic account) and leaves it as it found it. The page is
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

test('the sign-in page is drawn in the last eligible Admin selection, whatever the browser prefers', async ({browser}) => {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const admin = await signIn(browser, ADMIN);
  const selector = await person(admin, 'Selector', 'admin');
  const reader = await person(admin, 'Reader', 'user');
  const first = await publicPage(browser);
  const found = await drawn(first.page);
  await first.context.close();
  const own = await signIn(browser, selector);
  const user = await signIn(browser, reader);
  try {
    // An eligible Admin's selection: Light, Catppuccin Latte.
    expect((await api(own.page, 'PATCH', '/account/appearance', {mode: 'light', light_palette: 'catppuccin-latte'})).status).toBe(200);
    // A User's choice is its own alone.
    expect((await api(user.page, 'PATCH', '/account/appearance', {mode: 'dark', dark_palette: 'dracula'})).status).toBe(200);

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

    // The Admin's next effective selection replaces it.
    expect((await api(own.page, 'PATCH', '/account/appearance', {mode: 'dark', dark_palette: 'dracula'})).status).toBe(200);
    const next = await publicPage(browser, {colorScheme: 'light'});
    expect(await drawn(next.page)).toMatchObject({mode: 'dark', light: false, palette: 'dracula', scheme: 'dark', bg: '#191a21',
      square: 'rgb(40, 42, 54)'});
    await next.context.close();
  } finally {
    // Leave the installation's login appearance as this file found it.
    const pair = {mode: found.mode, [`${found.mode}_palette`]: found.palette || `debridpulse-${found.mode}`};
    await api(own.page, 'PATCH', '/account/appearance', pair);
    for (const session of [own, user]) await session.context.close();
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
    // The version is the card's: at its bottom-right corner, inside it, over nothing it holds.
    const version = document.querySelector('.card > .version').getBoundingClientRect();
    facts.corner = [Math.round(card.right - version.right), Math.round(card.bottom - version.bottom)];
    facts.covers = [...document.querySelectorAll('.card input, .card button, .card a, .card label, .card .foot, .card .error, .card .brand-lockup')]
      .map(el => el.getBoundingClientRect())
      .filter(r => version.left < r.right && version.right > r.left && version.top < r.bottom && version.bottom > r.top).length;
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
    expect([facts.corner, facts.covers], `${width}`).toEqual([[13, 7], 0]);
  }
  for (const [width, height] of [[320, 568], [390, 600], [667, 375]]) {
    await page.setViewportSize({width, height});
    const facts = await place();
    expect(facts.fits, `${width}x${height}`).toBe(false);
    expect(facts.top, `${width}x${height}`).toBeGreaterThanOrEqual(0);          // its top is on screen
    expect(facts.bottom, `${width}x${height}`).toBeLessThanOrEqual(0.5);       // and its end is reached by scrolling
    expect(facts.wide, `${width}x${height}`).toBeLessThanOrEqual(0);
    expect([facts.corner, facts.covers], `${width}x${height}`).toEqual([[13, 7], 0]);
  }
  // From 701px the page's own padding (58px over 42px) places it, as before.
  for (const width of [701, 780, 1024, 1440]) {
    await page.setViewportSize({width, height: 900});
    const facts = await place();
    expect(facts.dx, `${width}`).toBeCloseTo(0, 2);
    expect(facts.dy, `${width}`).toBeCloseTo(8, 2);
    expect([facts.corner, facts.covers], `${width}`).toEqual([[17, 9], 0]);
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
        tray: getComputedStyle(svg.querySelector('.dp-logo-tray-end')).stopColor};
    });
    const native = await logo();
    expect(native).toMatchObject({img: false, label: 'DebridPulse logo', hidden: 'true', square: 'rgb(8, 11, 24)',
      outline: 'rgb(166, 44, 255)', tray: 'rgb(20, 159, 255)'});
    await own.page.evaluate(() => window.DPTheme.choose('gruvbox-dark'));
    expect(await logo()).toMatchObject({square: 'rgb(40, 40, 40)', outline: 'rgb(211, 134, 155)', tray: 'rgb(131, 165, 152)', size: native.size});
    await own.page.evaluate(() => window.DPTheme.setMode(true));
    expect(await logo()).toMatchObject({square: 'rgb(255, 255, 255)', outline: 'rgb(166, 44, 255)', size: native.size});
    await own.page.evaluate(() => window.DPTheme.choose('flexoki-light'));
    const flexoki = await logo();
    expect(flexoki.square).not.toBe('rgb(255, 255, 255)');
    expect(flexoki.outline).not.toBe('rgb(166, 44, 255)');
  } finally {
    await own.context.close();
    await api(admin.page, 'DELETE', `/accounts/${viewer.id}`);
    await admin.context.close();
  }
});
