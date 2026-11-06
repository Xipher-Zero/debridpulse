const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Network Sources handheld header: ONE row at every width, its
 * title in full, with the least compression the room needs.
 *
 * Below 780px the Network Sources group header is identity, full title,
 * disclosure, then Help and the group toggle. The row never wraps and the
 * title never shrinks: the group's own data-header-fit step (declared
 * data-header-fit-self; fitHeaderSteps, in fitHeaderHints) takes back the
 * gaps (1), the header padding (2), the identity mark (3), and lends Help --
 * the SAME element -- to the row above the protocol boxes (4), choosing the
 * first step at which the header fits in the rendered layout and giving
 * each back as soon as the room holds it. While the row holds Help, the
 * one-row viewport includes it, so one full row of boxes stays in view.
 *
 * Nothing is written: the page is only read and resized. */

const GROUP = '#view-settings [data-panel="sources"] > .dp-settings-general-sources';

async function prepare(page, {open = true} = {}) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  await page.waitForFunction(() => typeof window.nav === 'function' && typeof window.loadSettings === 'function');
  await page.evaluate(() => window.nav(document.querySelector('#sidebar .nav-item[data-view="settings"]')));
  await page.locator('#view-settings .dp-settings-tabs [data-tab="sources"]:not(.dp-motion-still *)').first()
    .dispatchEvent('click');
  await expect(page.locator('#view-settings [data-panel="sources"]')).toBeVisible();
  if (open) await setOpen(page, true);
}

async function setOpen(page, open) {
  const disclosure = page.locator(`${GROUP} > .card-header .dp-settings-disclosure`);
  if ((await disclosure.getAttribute('aria-expanded')) !== String(open)) await disclosure.click();
  const body = page.locator(`${GROUP} > .card-body`);
  await (open ? expect(body).toBeVisible() : expect(body).toBeHidden());
  await page.evaluate(group => Promise.all(document.querySelector(group).getAnimations({subtree: true})
    .map(motion => motion.finished)), GROUP);
  await settle(page);
}

const settle = page => page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));

async function width(page, value) {
  await page.setViewportSize({width: value, height: 900});
  // The fit is the ResizeObserver's: settled after two frames.
  await settle(page);
}

// The header as drawn: its one Help's place, one row, the full title, its
// step, and the parts' collisions; the body's help row and viewport.
const state = page => page.evaluate(group => {
  const section = document.querySelector(group);
  const header = section.querySelector(':scope > .card-header');
  header.scrollIntoView({block: 'center'});
  const style = getComputedStyle(header);
  const titleGroup = header.querySelector(':scope > .dp-settings-card-title-group');
  const title = titleGroup.getBoundingClientRect();
  const text = titleGroup.querySelector('.dp-settings-card-title-text');
  const rail = header.querySelector(':scope > .dp-settings-card-header-controls');
  const railBox = rail.getBoundingClientRect();
  const chips = document.querySelectorAll('#view-settings [data-settings-help="network-sources"]');
  const chip = chips[0];
  const right = header.getBoundingClientRect().left + header.clientLeft + header.clientWidth - parseFloat(style.paddingRight);
  const range = document.createRange(); range.selectNodeContents(text);
  const parts = {
    mark: header.querySelector('.dp-settings-protocol-chip'), title: text,
    disclosure: header.querySelector('.dp-settings-disclosure'), help: header.querySelector('.dp-settings-help-chip'),
    toggle: header.querySelector('.toggle'),
  };
  const box = header.getBoundingClientRect();
  const boxes = Object.entries(parts).filter(([, n]) => n && n.getClientRects().length)
    .map(([name, n]) => [name, n, n.getBoundingClientRect()]);
  const found = [];
  const sizes = {disclosure: [32, 32], help: [34, 28]};
  for (const [name, n, b] of boxes) {
    if (b.left < box.left - 0.5 || b.right > box.right + 0.5) found.push(`${name} outside the header`);
    const [w, h] = sizes[name] || [];
    if ((w && Math.abs(b.width - w) > 0.5) || (h && Math.abs(b.height - h) > 0.5)) found.push(`${name} ${b.width}x${b.height}`);
    if (['disclosure', 'help', 'toggle'].includes(name)) {
      const hit = document.elementFromPoint((b.left + b.right) / 2, (b.top + b.bottom) / 2);
      if (!n.contains(hit) && !hit?.contains(n)) found.push(`${name} not reachable at its centre`);
    }
  }
  for (let i = 0; i < boxes.length; i += 1) for (let j = i + 1; j < boxes.length; j += 1) {
    const [an, a, ab] = boxes[i]; const [bn, b, bb] = boxes[j];
    if (a.contains(b) || b.contains(a)) continue;
    const w = Math.min(ab.right, bb.right) - Math.max(ab.left, bb.left);
    const h = Math.min(ab.bottom, bb.bottom) - Math.max(ab.top, bb.top);
    if (w > 0.5 && h > 0.5) found.push(`${an} ~ ${bn} ${w.toFixed(1)}x${h.toFixed(1)}`);
  }
  if (document.documentElement.scrollWidth > document.documentElement.clientWidth) found.push('page overflows horizontally');
  const body = section.querySelector(':scope > .card-body');
  const slot = body.querySelector(':scope > .dp-settings-help-slot');
  const tile = body.querySelector('.dp-settings-source-box-grid > *');
  const shown = !body.hidden && body.getClientRects().length;
  const bodyBox = body.getBoundingClientRect();
  const slotBox = slot.getBoundingClientRect();
  const tileBox = tile.getBoundingClientRect();
  const bodyStyle = getComputedStyle(body);
  return {count: chips.length, step: section.dataset.headerFit,
    place: !chip ? 'missing' : chip.parentElement === rail ? 'header' : chip.parentElement === slot ? 'body' : 'elsewhere',
    // One row: the rail beside the title, and every control in the rail on
    // that same line (a rail can wrap within itself while still centred).
    oneRow: Math.abs((railBox.top + railBox.bottom) / 2 - (title.top + title.bottom) / 2) < 2
      && [...rail.children].filter(n => n.getClientRects().length).every(n => {
        const b = n.getBoundingClientRect();
        return Math.abs((b.top + b.bottom) / 2 - (title.top + title.bottom) / 2) < 2;
      }),
    text: text.textContent, fullTitle: range.getBoundingClientRect().width <= text.getBoundingClientRect().width + 0.5,
    withinRow: railBox.right <= right + 0.5, overflow: header.scrollWidth - header.clientWidth,
    height: box.height, display: style.display, padding: style.paddingLeft, collisions: found,
    slotDrawn: getComputedStyle(slot).display !== 'none' && slotBox.height > 0,
    // One full row of boxes in view, from the top of the body's scroll.
    rowInView: shown ? tileBox.top >= bodyBox.top - 0.5
      && tileBox.bottom <= bodyBox.bottom - parseFloat(bodyStyle.paddingBottom) + 0.5 : null,
    // The viewport ends at the first row's bottom edge (plus padding), no more.
    viewportSlack: shown ? bodyBox.bottom - parseFloat(bodyStyle.paddingBottom) - tileBox.bottom : null,
    helpClearOfTiles: shown && chip?.parentElement === slot
      ? chip.getBoundingClientRect().bottom <= tileBox.top + 0.5 : null};
}, GROUP);

test('Network Sources header is one row with its full title at every required width, with the least step', async ({page}) => {
  await prepare(page);
  const ledger = {};
  for (const value of [320, 360, 375, 390, 440, 779, 780]) {
    await width(page, value);
    const s = await state(page);
    const at = `${value}px`;
    expect(s.count, at).toBe(1);
    expect(s.oneRow, at).toBe(true);
    expect(s.withinRow, at).toBe(true);
    expect(s.overflow, at).toBeLessThanOrEqual(0);
    expect(s.text, at).toBe('Network Sources');
    expect(s.fullTitle, at).toBe(true);
    expect(s.collisions, at).toEqual([]);
    expect(s.rowInView, at).toBe(true);
    expect(s.viewportSlack, at).toBeLessThan(1);
    ledger[value] = {step: s.step, place: s.place, height: Math.round(s.height)};
  }
  expect(ledger[320]).toMatchObject({step: '4', place: 'body'});
  expect(ledger[360]).toMatchObject({step: '1', place: 'header'});
  expect(ledger[375]).toMatchObject({step: '1', place: 'header'});
  expect(ledger[390]).toMatchObject({step: '0', place: 'header'});
  expect(ledger[440]).toMatchObject({step: '0', place: 'header'});
  expect(ledger[779]).toMatchObject({step: '0', place: 'header'});
  // 780px+ is the desktop header: grid, its own padding, Help in the rail.
  expect(ledger[780]).toMatchObject({step: '0', place: 'header', height: 50});
  const desktop = await state(page);
  expect(desktop.display).toBe('grid');
  expect(desktop.slotDrawn).toBe(false);
});

test('each Network step is the least that fits, and Help returns from the row as the room holds it', async ({page}) => {
  await prepare(page);
  await page.evaluate(() => { window.__netHelp = document.querySelector('[data-settings-help="network-sources"]'); });
  for (const value of [440, 390, 375, 360, 340, 320, 340, 360, 375, 390, 440]) {
    await width(page, value);
    const s = await state(page);
    const at = `${value}px`;
    expect(s.oneRow, at).toBe(true);
    expect(s.count, at).toBe(1);
    expect(await page.evaluate(() => document.querySelector('[data-settings-help="network-sources"]') === window.__netHelp), at).toBe(true);
    // Lent only at step 4, and then drawn above the boxes, covering none, the
    // viewport grown by its row so the first row of boxes stays whole.
    expect(s.place, at).toBe(Number(s.step) >= 4 ? 'body' : 'header');
    expect(s.slotDrawn, at).toBe(s.place === 'body');
    if (s.place === 'body') expect(s.helpClearOfTiles, at).toBe(true);
    expect(s.rowInView, at).toBe(true);
    expect(s.viewportSlack, at).toBeLessThan(1);
    // One step less does not fit (drawn with the lighter step, then put back).
    const chosen = Number(s.step);
    if (chosen > 0) {
      const lighterFits = await page.evaluate(([group, lighter]) => {
        const section = document.querySelector(group);
        const header = section.querySelector(':scope > .card-header');
        const kept = section.dataset.headerFit;
        section.dataset.headerFit = String(lighter);
        const style = getComputedStyle(header);
        const rail = header.querySelector(':scope > .dp-settings-card-header-controls');
        const chip = document.querySelector('[data-settings-help="network-sources"]');
        const help = 34 + parseFloat(getComputedStyle(rail).columnGap);
        const without = rail.getBoundingClientRect().width - (chip.parentElement === rail ? help : 0);
        const room = header.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight)
          - header.querySelector(':scope > .dp-settings-card-title-group').getBoundingClientRect().width - parseFloat(style.columnGap);
        section.dataset.headerFit = kept;
        return without + help <= room || (lighter >= 4 && without <= room);
      }, [GROUP, chosen - 1]);
      expect(lighterFits, `step ${chosen - 1} at ${value}px`).toBe(false);
    }
  }
  // Collapsed and re-opened at 320px: the row and the viewport follow.
  await width(page, 320);
  await setOpen(page, false);
  await setOpen(page, true);
  const s = await state(page);
  expect(s).toMatchObject({place: 'body', slotDrawn: true, rowInView: true, helpClearOfTiles: true});
  expect(s.viewportSlack).toBeLessThan(1);
  // Help in the row is operable and opens its topic.
  await page.locator('[data-settings-help="network-sources"]').click();
  await expect(page.locator('.dp-modal-dialog.dp-settings-help-dialog')).toHaveCount(1);
  await page.keyboard.press('Escape');
  await expect(page.locator('[data-settings-help="network-sources"]')).toBeFocused();
});

test('a focused Help in the collapsed Network Sources header moves out at once; focus goes to its disclosure', async ({page}) => {
  await prepare(page, {open: false});
  const help = page.locator('[data-settings-help="network-sources"]');
  const disclosure = page.locator(`${GROUP} > .card-header .dp-settings-disclosure`);
  const body = page.locator(`${GROUP} > .card-body`);
  await width(page, 440);
  await expect(body).toBeHidden();
  await help.focus();
  await expect(help).toBeFocused();
  expect((await state(page)).place).toBe('header');

  await width(page, 320);
  const s = await state(page);
  expect(s).toMatchObject({count: 1, place: 'body', oneRow: true, withinRow: true, fullTitle: true, collisions: []});
  expect(s.overflow).toBeLessThanOrEqual(0);
  await expect(disclosure).toBeFocused();
  await expect(body).toBeHidden();
  await page.keyboard.press('Enter');
  await expect(help).toBeVisible();
  await page.keyboard.press('Enter');
  await expect(body).toBeHidden();

  await width(page, 440);
  expect(await state(page)).toMatchObject({count: 1, place: 'header', oneRow: true});
  await help.focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('.dp-modal-dialog.dp-settings-help-dialog')).toHaveCount(1);
  await page.keyboard.press('Escape');
  await expect(help).toBeFocused();
});
