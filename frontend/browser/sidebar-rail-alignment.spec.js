const { test, expect } = require('@playwright/test');

/* DP 1.0.13 sidebar icon rail.
 *
 * Every sidebar navigation icon shares one horizontal centerline. The brand
 * glyph (with its wordmark) and the Log Out glyph (with its label) sit on that
 * same rail wherever the icons form the left rail: the expanded sidebar and the
 * drawer. The brand block is also a silent pointer shortcut to Dashboard
 * through the one navigation owner -- it gains no visible treatment at all.
 */

const RAIL_WIDTHS = [1440, 1300, 1180, 800];

async function open(page, {authenticated = false} = {}) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  if (authenticated) {
    // Only so the session owner (auth.js) mounts its real Log Out row.
    await page.route('**/api/auth/session', route => route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({authenticated: true, mechanism: 'password', subject: 'rail', display_name: 'rail',
        csrf_token: 'rail-csrf', session_expires_in_seconds: 3600}),
    }));
  }
  await page.goto('/');
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeVisible();
  if (authenticated) await expect(page.locator('#sidebar-auth-row')).toBeVisible();
}

function geometry(page) {
  return page.evaluate(() => {
    const box = element => {
      const r = element.getBoundingClientRect();
      return {left: r.left, right: r.right, top: r.top, bottom: r.bottom, cx: r.left + r.width / 2,
              cy: r.top + r.height / 2, width: r.width};
    };
    const icons = [...document.querySelectorAll('#sidebar nav .nav-item .icon svg')].map(box);
    const dashboardIcon = document.querySelector('#sidebar nav .nav-item[data-view="dashboard"] .icon');
    const dashboardLabel = document.querySelector('#sidebar nav .nav-item[data-view="dashboard"] .nav-label');
    const row = document.querySelector('#sidebar-auth-row');
    return {
      icons,
      logo: box(document.querySelector('#sidebar .logo')),
      // The block's content box (its bottom divider is a border): where the glyph is centred.
      logoContentCy: (() => {
        const node = document.querySelector('#sidebar .logo');
        return node.getBoundingClientRect().top + node.clientTop + node.clientHeight / 2;
      })(),
      glyph: box(document.querySelector('#sidebar .logo-icon')),
      word: box(document.querySelector('#sidebar .logo-name')),
      gap: parseFloat(getComputedStyle(document.querySelector('#sidebar .logo')).columnGap),
      navGap: box(dashboardLabel).left - box(dashboardIcon).right,
      logout: row ? {
        icon: box(row.querySelector('.icon svg')),
        iconBox: box(row.querySelector('.icon')),
        label: box(row.querySelector('.nav-label')),
      } : null,
    };
  });
}

for (const width of RAIL_WIDTHS) {
  test(`brand and Log Out glyphs sit on the navigation icon rail at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 1000});
    await open(page, {authenticated: true});
    const g = await geometry(page);
    const rail = g.icons[0].cx;
    expect(g.icons.length).toBe(6);
    for (const icon of g.icons) expect(Math.abs(icon.cx - rail)).toBeLessThan(0.5);

    // 1. The brand glyph is centred on the rail, unchanged in size, and still
    //    vertically centred in its block.
    expect(Math.abs(g.glyph.cx - rail)).toBeLessThan(0.5);
    expect(g.glyph.width).toBe(48);
    expect(Math.abs(g.glyph.cy - g.logoContentCy)).toBeLessThan(0.5);
    // 3. The wordmark moved with it: the glyph-to-wordmark spacing is the block's own gap.
    expect(Math.abs((g.word.left - g.glyph.right) - g.gap)).toBeLessThan(0.5);

    // 2. The Log Out glyph is on the same rail ...
    expect(Math.abs(g.logout.icon.cx - rail)).toBeLessThan(0.5);
    // 4. ... and its label keeps the navigation item spacing.
    expect(Math.abs((g.logout.label.left - g.logout.iconBox.right) - g.navGap)).toBeLessThan(0.5);
  });
}

test('the brand block returns to Dashboard through the one navigation owner', async ({page}) => {
  await open(page);
  for (const target of ['.logo-icon', '.logo-name', '.logo']) {
    await page.locator('#sidebar .nav-item[data-view="settings"]').click();
    await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
    await page.locator(`#sidebar ${target}`).click({position: target === '.logo' ? {x: 4, y: 4} : undefined});
    await expect(page.locator('#view-dashboard')).toHaveClass(/\bactive\b/);
    await expect(page.locator('#view-settings')).not.toHaveClass(/\bactive\b/);
    await expect(page.locator('#sidebar .nav-item.active')).toHaveCount(1);
    await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toHaveClass(/\bactive\b/);
    await expect(page.locator('#page-title')).toHaveText('Dashboard');
    await expect(page.locator('#content')).toHaveClass(/\bdashboard-active\b/);
  }
});

test('the brand block has no hover, active, focus or button treatment', async ({page}) => {
  await open(page);
  const logo = page.locator('#sidebar .logo');
  // Not a control: no role, no tab stop, no tooltip of its own.
  for (const attribute of ['role', 'tabindex', 'title', 'aria-label', 'href']) {
    await expect(logo).not.toHaveAttribute(attribute);
  }
  const look = () => logo.evaluate(node => {
    const read = element => {
      const style = getComputedStyle(element);
      return ['cursor', 'background-color', 'background-image', 'box-shadow', 'outline-style', 'color',
        'opacity', 'transform', 'filter', 'border-bottom-color', 'transition-duration']
        .map(name => `${name}:${style.getPropertyValue(name)}`).join(';');
    };
    return [node, ...node.querySelectorAll('*')].map(read).join('|');
  });
  await page.mouse.move(1000, 600);
  const resting = await look();
  await logo.hover({position: {x: 4, y: 4}});
  expect(await look()).toBe(resting);
  await page.mouse.down();
  expect(await look()).toBe(resting);
  await page.mouse.up();
  // Keyboard focus never lands on it; the Dashboard entry stays the keyboard control.
  await page.locator('body').focus();
  for (let index = 0; index < 4; index += 1) {
    await page.keyboard.press('Tab');
    expect(await page.evaluate(() => Boolean(document.activeElement?.closest('#sidebar .logo')))).toBe(false);
  }
});

test('ordinary Dashboard sidebar navigation is unchanged', async ({page}) => {
  await open(page);
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await expect(page.locator('#view-torrents')).toHaveClass(/\bactive\b/);
  await page.locator('#sidebar .nav-item[data-view="dashboard"]').click();
  await expect(page.locator('#view-dashboard')).toHaveClass(/\bactive\b/);
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toHaveClass(/\bactive\b/);
  await expect(page.locator('#page-title')).toHaveText('Dashboard');
});
