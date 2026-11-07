const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Users & Access headers below 780px.
 *
 * User Accounts, Integration Accounts and Integration Delivery Profiles take
 * the wrapping one-row header (fitHeaderSteps): the title on at most two
 * lines, the rail on one line beside it, and room taken back in the
 * header's steps -- 2 each action's shorter words, 3 its shortest (User
 * Accounts' Merge and Add Local Account become one Manage menu), then
 * Delivery Profiles' short title (4, before Help), then Help lent to the row
 * above the body. Narrowing and widening choose alike. From 780px nothing
 * changes.
 *
 * A real Admin session on the password-auth backend (DP_AUTH_BASE_URL); the
 * delivery-profile catalog is served here, and every write is recorded and
 * refused, so no account, token or profile is created. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const PROFILES = {revision: 1, profiles: [
  {id: 'synthetic-tv', categories: ['tv'], clients: [], local_root: '/download/tv', import_root: '/download/tv', source_types: ['all'], retention: {}}]};
const CARDS = {
  users: '.dp-settings-users-card:not(.dp-settings-users-integrations-card)',
  integrations: '.dp-settings-users-integrations-card',
  profiles: '.dp-settings-delivery-profiles-card',
};

async function open(browser, viewport = {width: 1280, height: 900}, options = {}) {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const context = await browser.newContext({baseURL: AUTH, viewport, ...options});
  const page = await context.newPage();
  page.__writes = [];
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/login');
  await page.locator('#username').fill('browser');
  await page.locator('#password').fill('browser-secret');
  await page.locator('button[type="submit"]').click();
  await expect(page).not.toHaveURL(/\/login/);
  await page.waitForFunction(() => Boolean(window.debridPulseAuth?.fetch));
  await page.route(url => url.pathname.startsWith('/api/'), route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/external/profiles' && request.method() === 'GET') return route.fulfill({json: PROFILES});
    if (request.method() === 'GET' || path === '/api/settings/test-aria2') return route.continue();
    page.__writes.push(`${request.method()} ${path}`);
    return route.abort();
  });
  await page.evaluate(() => window.nav(document.querySelector('#sidebar .nav-item[data-view="settings"]')));
  await page.locator('#view-settings .dp-settings-tabs [data-tab="users"]:not(.dp-motion-still *)').first().dispatchEvent('click');
  await expect(page.locator(`#view-settings ${CARDS.profiles}`)).toBeVisible();
  return {context, page};
}

const settle = page => page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
async function resize(page, width) {
  await page.setViewportSize({width, height: 900});
  await settle(page);
}

// Each header as drawn.
const headers = page => page.evaluate(cards => Object.fromEntries(Object.entries(cards).map(([name, selector]) => {
  const card = document.querySelector(`#view-settings ${selector}`);
  const head = card.querySelector(':scope > .card-header');
  const R = node => node && node.getClientRects().length ? node.getBoundingClientRect() : null;
  const help = card.querySelector('.dp-settings-help-chip');
  const buttons = [...head.querySelectorAll('.dp-settings-card-header-controls button:not(.dp-settings-help-chip)')]
    .filter(button => button.getClientRects().length);
  const parts = [R(head.querySelector('.dp-settings-card-title-group')), head.contains(help) ? R(help) : null, ...buttons.map(R)].filter(Boolean);
  const overlap = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0.5 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5;
  let collisions = 0;
  for (let i = 0; i < parts.length; i += 1) for (let j = i + 1; j < parts.length; j += 1) if (overlap(parts[i], parts[j])) collisions += 1;
  const text = head.querySelector('.dp-settings-card-title-text');
  const range = document.createRange();
  range.selectNodeContents(text);
  const lines = new Set([...range.getClientRects()].filter(box => box.width > 1 && box.height > 8).map(box => Math.round(box.bottom))).size;
  const mid = box => (box.top + box.bottom) / 2;
  const controls = parts.slice(1);
  const words = button => {
    const short = button.querySelector('.dp-settings-fit-label-short');
    return short && short.getClientRects().length ? getComputedStyle(short, '::before').content.replace(/"/g, '') : button.innerText.trim();
  };
  const shortTitle = head.querySelector('.dp-settings-card-title-short');
  return [name, {
    fit: Number(card.dataset.headerFit), lines, collisions,
    oneRow: controls.every(box => Math.abs(mid(box) - mid(controls[0])) <= 2),
    lent: !head.contains(help),
    title: shortTitle && shortTitle.getClientRects().length ? 'short' : 'full',
    labels: buttons.map(words),
    names: buttons.map(button => button.getAttribute('aria-label') || button.textContent.replace(/\s+/g, ' ').trim()),
    overflow: head.scrollWidth > head.clientWidth + 1 || document.documentElement.scrollWidth > window.innerWidth,
  }];
})), CARDS);

const LADDER = {
  users: [['Merge', 'Add Local Account'], ['Merge', 'Add Account'], ['Manage']],
  integrations: [['Add Integration Account'], ['Add Account'], ['Add']],
  profiles: [['Create Delivery Profile'], ['Create Profile'], ['Create']],
};
// The drawn labels at a step: the full words before 2, the shorter at 2, the shortest from 3.
const expected = (name, fit) => LADDER[name][fit >= 3 ? 2 : fit === 2 ? 1 : 0];

test('every header is one row, its title on at most two lines, its labels the step\'s, and narrowing and widening agree', async ({browser}) => {
  const {context, page} = await open(browser);
  const widths = [];
  for (let width = 779; width >= 320; width -= 3) widths.push(width);
  const narrowing = {};
  for (const width of widths) {
    await resize(page, width);
    const drawn = await headers(page);
    for (const [name, h] of Object.entries(drawn)) {
      const at = `${name} @${width} (step ${h.fit})`;
      expect([h.lines <= 2, h.collisions, h.oneRow, h.overflow], at).toEqual([true, 0, true, false]);
      expect(h.labels, at).toEqual(expected(name, h.fit));
      // Delivery Profiles' short title comes before its Help is lent; the full one is used wherever it fits.
      if (name === 'profiles') expect(h.title, at).toBe(h.fit >= 4 ? 'short' : 'full');
      else expect(h.title, at).toBe('full');
      expect(h.lent, at).toBe(h.fit >= (name === 'profiles' ? 5 : 4));
      // Whatever the words, each action keeps its full name.
      expect(h.names, at).toEqual(h.fit >= 3 && name === 'users' ? ['Manage'] : LADDER[name][0]);
    }
    narrowing[width] = JSON.stringify(Object.values(drawn).map(h => [h.fit, h.labels, h.title, h.lent]));
  }
  // The steps only ever take more back as the room narrows.
  const fits = widths.map(width => JSON.parse(narrowing[width]).map(([fit]) => fit));
  for (let index = 1; index < fits.length; index += 1) {
    fits[index].forEach((fit, card) => expect(fit, `card ${card} @${widths[index]}`).toBeGreaterThanOrEqual(fits[index - 1][card]));
  }
  // At the extremes: the full composition at 779px, and the narrowest states reached by 320px.
  expect(fits[0]).toEqual([0, 0, 0]);
  const narrowest = await headers(page);
  expect(narrowest.users.labels).toEqual(['Manage']);
  expect(narrowest.integrations.labels).toEqual(['Add']);
  expect([narrowest.profiles.title, narrowest.profiles.labels]).toEqual(['short', ['Create']]);
  // Widening through the same widths chooses the same composition.
  for (const width of [...widths].reverse()) {
    await resize(page, width);
    const drawn = await headers(page);
    expect(JSON.stringify(Object.values(drawn).map(h => [h.fit, h.labels, h.title, h.lent])), `@${width}`).toBe(narrowing[width]);
  }
  expect(page.__writes).toEqual([]);
  await context.close();
});

test('User Accounts: Manage opens a menu of exactly Merge and Add Local Account, each its own existing action', async ({browser}) => {
  const {context, page} = await open(browser);
  await resize(page, 320);
  const card = page.locator(`#view-settings ${CARDS.users}`);
  const trigger = card.locator('.dp-settings-header-test-trigger');
  const menu = card.locator('.dp-settings-header-test-menu');
  await expect(trigger).toHaveText('Manage');
  await expect(card.locator('.dp-settings-header-action')).toBeHidden();
  // Keyboard: Enter opens it on Merge; Escape closes it to the trigger.
  await trigger.focus();
  await page.keyboard.press('Enter');
  await expect(menu).toBeVisible();
  await expect(menu.locator('button')).toHaveText(['Merge', 'Add Local Account']);
  await expect(menu.getByRole('button', {name: 'Add Local Account', exact: true})).toHaveCount(1);
  await expect(menu.locator('button').first()).toBeFocused();
  const inside = await menu.evaluate(node => {
    const box = node.getBoundingClientRect();
    return box.left >= 0 && box.right <= window.innerWidth && box.top >= 0 && box.bottom <= window.innerHeight;
  });
  expect(inside).toBe(true);
  await page.keyboard.press('Escape');
  await expect(menu).toBeHidden();
  await expect(trigger).toBeFocused();
  // Merge: the menu closes and the existing merge flow opens.
  await trigger.click();
  await menu.getByRole('button', {name: 'Merge'}).click();
  await expect(menu).toBeHidden();
  const merge = page.locator('.dp-modal-overlay .dp-modal-dialog').last();
  await expect(merge).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(merge).toBeHidden();
  // Add Local Account: the existing account dialog.
  await trigger.click();
  await menu.getByRole('button', {name: 'Add Local Account'}).click();
  await expect(menu).toBeHidden();
  const add = page.locator('.dp-modal-overlay .dp-modal-dialog').last();
  await expect(add).toContainText('Add Local Account');
  await page.keyboard.press('Escape');
  await expect(add).toBeHidden();
  // From 780px: the two actions, no Manage.
  await resize(page, 780);
  await expect(trigger).toBeHidden();
  await expect(card.locator('.dp-settings-header-action > button')).toHaveText(['Merge', 'Add Local Account']);
  expect(page.__writes).toEqual([]);
  await context.close();
});

test('Add Integration Account and Create Delivery Profile keep their names and open their own dialogs at every label', async ({browser}) => {
  const {context, page} = await open(browser);
  for (const width of [779, 400, 320]) {
    await resize(page, width);
    for (const [selector, name, title] of [[CARDS.integrations, 'Add Integration Account', 'Add Integration Account'],
      [CARDS.profiles, 'Create Delivery Profile', 'Create Delivery Profile']]) {
      const button = page.locator(`#view-settings ${selector} > .card-header .dp-settings-header-action > button`);
      await expect(button, `${name} @${width}`).toHaveAccessibleName(name);
      await expect(page.locator(`#view-settings ${selector} > .card-header`).getByRole('button', {name})).toHaveCount(1);
      await button.click();
      const dialog = page.locator('.dp-modal-overlay .dp-modal-dialog').last();
      await expect(dialog, `${name} @${width}`).toContainText(title);
      await page.keyboard.press('Escape');
      await expect(dialog).toBeHidden();
    }
  }
  // Add Local Account keeps its name while it reads "Add Account".
  for (const width of [779, 470, 440, 420, 410]) {
    await resize(page, width);
    const add = page.locator(`#view-settings ${CARDS.users} > .card-header .dp-settings-header-action > [data-user-action="create-local"]`);
    if (await add.isVisible()) await expect(add, `@${width}`).toHaveAccessibleName('Add Local Account');
  }
  expect(page.__writes).toEqual([]);
  await context.close();
});

test('the short Delivery Profiles title says the full one as its tooltip, by hover and keyboard', async ({browser}) => {
  const {context, page} = await open(browser);
  await resize(page, 320);
  const card = page.locator(`#view-settings ${CARDS.profiles}`);
  const text = card.locator('.dp-settings-card-title-text');
  await expect(card.locator('.card-title')).toHaveText('Integration Delivery Profiles');
  await expect(text).toHaveAttribute('data-tooltip', 'Integration Delivery Profiles');
  await expect(text).toHaveAttribute('tabindex', '0');
  await text.hover();
  await expect(page.locator('#dp-tooltip')).toHaveText('Integration Delivery Profiles');
  await page.mouse.move(2, 2);
  await expect(page.locator('#dp-tooltip')).toBeHidden();
  await text.focus();
  await expect(page.locator('#dp-tooltip')).toHaveText('Integration Delivery Profiles');
  await page.keyboard.press('Escape');
  await expect(page.locator('#dp-tooltip')).toBeHidden();
  await resize(page, 779);
  await expect(text).not.toHaveAttribute('data-tooltip', /.*/);
  await expect(card.locator('.dp-settings-card-title-short')).toBeHidden();
  await context.close();
});

test('Help, lent where the header cannot hold it, sits above the body clear of its content, opens its topic, and comes back', async ({browser}) => {
  const {context, page} = await open(browser);
  // Below the supported minimum, User Accounts and Integration Accounts lend it.
  await resize(page, 300);
  for (const name of ['users', 'integrations']) {
    const card = page.locator(`#view-settings ${CARDS[name]}`);
    const chip = card.locator('.dp-settings-help-chip');
    await expect(card.locator(':scope > .card-body > .dp-settings-help-slot--row > .dp-settings-help-chip')).toHaveCount(1);
    const clear = await card.evaluate(el => {
      const chip = el.querySelector('.dp-settings-help-chip').getBoundingClientRect();
      const next = el.querySelector(':scope > .card-body > .dp-settings-help-slot--row').nextElementSibling.getBoundingClientRect();
      return next.top - chip.bottom;
    });
    expect(clear, name).toBeGreaterThanOrEqual(6);
    await chip.click();
    const dialog = page.locator('.dp-modal-overlay .dp-modal-dialog').last();
    await expect(dialog).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(dialog).toBeHidden();
  }
  await resize(page, 779);
  for (const name of Object.keys(CARDS)) {
    await expect(page.locator(`#view-settings ${CARDS[name]} > .card-header .dp-settings-help-chip`), name).toHaveCount(1);
  }
  await context.close();
});

test.describe('on a touch screen', () => {
  test('a tap opens Manage, and a tap on the short title shows the full one', async ({browser}) => {
    const {context, page} = await open(browser, {width: 320, height: 900}, {hasTouch: true});
    const users = page.locator(`#view-settings ${CARDS.users}`);
    await users.locator('.dp-settings-header-test-trigger').tap();
    await expect(users.locator('.dp-settings-header-test-menu button')).toHaveText(['Merge', 'Add Local Account']);
    await page.locator(`#view-settings ${CARDS.profiles}`).scrollIntoViewIfNeeded();
    await page.locator(`#view-settings ${CARDS.profiles} .dp-settings-card-title-text`).tap();
    await expect(page.locator('#dp-tooltip')).toHaveText('Integration Delivery Profiles');
    expect(page.__writes).toEqual([]);
    await context.close();
  });
});
