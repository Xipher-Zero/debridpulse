const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Items 7 and 8 -- one Settings protocol icon block, per-instance
 * colour, and the operator-facing family name `General Sources` on both
 * Sources & Providers and Downloads. */

const PROTOCOLS = {
  direct_sources: {glyph: 'globe', colour: 'rgb(59, 130, 246)'},
  general_http: {glyph: 'globe', colour: 'rgb(59, 130, 246)'},
  general_ftp: {glyph: 'arrow-up-down', colour: 'rgb(45, 212, 191)'},
  usenet: {glyph: 'newspaper', colour: 'rgb(203, 213, 225)'},
};

const CHIP = '[data-protocol]';

/** The RENDERED chip: its own box, its own surface, and the glyph inside it. */
const chipGeometry = (page, tab) => page.evaluate(tab => {
  // A computed colour is transparent only when it says so. color-mix()
  // serialises as `color(srgb r g b)` (optionally `/ a`), so matching rgb()
  // alone would report every derived colour as transparent.
  const opaque = value => {
    const text = String(value || '').trim();
    if (!text || text === 'transparent' || text === 'none') return false;
    const slashAlpha = /\/\s*([0-9.]+)\s*\)/.exec(text);
    if (slashAlpha) return Number(slashAlpha[1]) > 0.02;
    const rgba = /rgba?\(([^)]+)\)/.exec(text);
    if (rgba) {
      const parts = rgba[1].split(/[\s,/]+/).filter(Boolean).map(Number);
      return parts.length < 4 || parts[3] > 0.02;
    }
    return /^(?:color|rgb|hsl|lab|lch|oklab|oklch|#)/.test(text);
  };
  const panel = document.querySelector(`.dp-settings-panel[data-panel="${tab}"]`);
  return Array.from(panel.querySelectorAll('[data-protocol]')).map(chip => {
    const style = getComputedStyle(chip);
    const glyph = chip.querySelector('img');
    const chipBox = chip.getBoundingClientRect();
    const glyphBox = glyph ? glyph.getBoundingClientRect() : null;
    return {
      protocol: chip.dataset.protocol,
      classes: chip.className,
      // The chip is an element in its own right, not the glyph.
      isOwnElement: !!glyph && glyph !== chip && chip.contains(glyph),
      borderWidth: parseFloat(style.borderTopWidth) || 0,
      borderStyle: style.borderTopStyle,
      borderColour: style.borderTopColor,
      borderOpaque: opaque(style.borderTopColor),
      radius: parseFloat(style.borderTopLeftRadius) || 0,
      background: style.backgroundImage !== 'none' ? style.backgroundImage : style.backgroundColor,
      surfacePainted: style.backgroundImage !== 'none' || opaque(style.backgroundColor),
      boxShadow: style.boxShadow,
      width: Math.round(chipBox.width),
      height: Math.round(chipBox.height),
      // Containment is geometric, not merely a DOM relationship.
      glyphInside: !!glyphBox
        && glyphBox.left >= chipBox.left - 0.5 && glyphBox.right <= chipBox.right + 0.5
        && glyphBox.top >= chipBox.top - 0.5 && glyphBox.bottom <= chipBox.bottom + 0.5,
      glyphWidth: glyphBox ? Math.round(glyphBox.width) : 0,
      glyphFilter: glyph ? getComputedStyle(glyph).filter : '',
    };
  });
}, tab);

async function openSettings(page, tab) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator(`#view-settings [data-tab="${tab}"]`).click();
  await expect(page.locator(`.dp-settings-panel[data-panel="${tab}"]`)).toBeVisible();
}

const blocks = (page, tab) => page.evaluate(tab => {
  const panel = document.querySelector(`.dp-settings-panel[data-panel="${tab}"]`);
  return Array.from(panel.querySelectorAll('[data-protocol]')).map(node => {
    const image = node.querySelector('img');
    const box = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    return {
      protocol: node.dataset.protocol,
      src: image ? new URL(image.getAttribute('src'), location.origin).pathname : null,
      alt: image ? image.getAttribute('alt') : null,
      hidden: node.getAttribute('aria-hidden'),
      colour: style.getPropertyValue('--dp-settings-inner-icon-color').trim(),
      filter: image ? getComputedStyle(image).filter : '',
      width: Math.round(box.width), height: Math.round(box.height),
    };
  });
}, tab);

test('Sources & Providers renders every protocol identity from one block', async ({page}) => {
  await openSettings(page, 'sources');
  const rendered = await blocks(page, 'sources');
  const byProtocol = Object.fromEntries(rendered.map(item => [item.protocol, item]));
  for (const [protocol, expected] of Object.entries(PROTOCOLS)) {
    const block = byProtocol[protocol];
    expect(block, `${protocol} has no protocol icon block`).toBeTruthy();
    expect(block.src).toBe(`/icons/lucide/${expected.glyph}.svg`);
    expect(block.alt).toBe('');
    expect(block.hidden).toBe('true');
    expect(block.filter).toContain('drop-shadow');
  }
  // One geometry: every block is the same size.
  const sizes = new Set(rendered.map(item => `${item.width}x${item.height}`));
  expect(sizes.size, `protocol blocks do not share one geometry: ${[...sizes]}`).toBe(1);
});

test('each protocol block carries its own frozen colour datum', async ({page}) => {
  await openSettings(page, 'sources');
  const rendered = await blocks(page, 'sources');
  for (const item of rendered) {
    const expected = PROTOCOLS[item.protocol];
    if (!expected) continue;
    const normalised = await page.evaluate(value => {
      const probe = document.createElement('span');
      probe.style.color = value;
      document.body.appendChild(probe);
      const computed = getComputedStyle(probe).color;
      probe.remove();
      return computed;
    }, item.colour);
    expect(normalised, `${item.protocol} colour`).toBe(expected.colour);
  }
});

test('Downloads shows General Sources with the identical identity', async ({page}) => {
  await openSettings(page, 'downloads');
  await expect(page.locator('#view-settings [data-executor-tuning="direct"] .card-title'))
    .toContainText('General Sources');
  const rendered = await blocks(page, 'downloads');
  const general = rendered.find(item => item.protocol === 'direct_sources');
  const usenet = rendered.find(item => item.protocol === 'usenet');
  expect(general, 'Downloads General Sources has no protocol identity').toBeTruthy();
  expect(general.src).toBe('/icons/lucide/globe.svg');
  expect(usenet, 'Downloads Usenet has no protocol identity').toBeTruthy();
  expect(usenet.src).toBe('/icons/lucide/newspaper.svg');
  expect(general.width).toBe(usenet.width);
});

test('no operator-facing Direct Transfers or Direct Sources wording survives', async ({page}) => {
  for (const tab of ['sources', 'downloads']) {
    await openSettings(page, tab);
    const text = await page.locator(`.dp-settings-panel[data-panel="${tab}"]`).innerText();
    expect(text).not.toContain('Direct Transfers');
    expect(text).not.toContain('Direct Sources');
  }
});

test('the identity survives expanding the Downloads tuning card', async ({page}) => {
  await openSettings(page, 'downloads');
  const card = page.locator('#view-settings [data-executor-tuning="direct"]');
  await card.locator('.dp-settings-disclosure').click();
  await expect(card.locator('[data-protocol="direct_sources"]')).toHaveCount(1);
});

test('transfer-row provider badges are untouched by this pass', async ({page}) => {
  await page.goto('/');
  const badge = await page.evaluate(() => !!document.querySelector('[data-protocol]'));
  expect(badge, 'a protocol block leaked outside Settings').toBe(false);
});


/* DP 1.0.13 Revision 3 -- the protocol identity must be a CHIP/BLOCK in the
 * same visual grammar as the AllDebrid identity block, not a free-floating
 * glowing SVG.
 *
 * The previous oracle proved glyph, colour, glow and a 34x34 footprint, all of
 * which a naked <img> in a bare <span> satisfies. These assert the rendered
 * container itself: its border, its radius, its painted surface, its
 * dimensional treatment, and that the glyph is geometrically inside it. */

test('every protocol identity renders a real chip, not a free-floating glyph', async ({page}) => {
  await openSettings(page, 'sources');
  const chips = await chipGeometry(page, 'sources');
  expect(chips.length).toBeGreaterThanOrEqual(4);
  for (const chip of chips) {
    const where = `${chip.protocol}`;
    expect(chip.isOwnElement, `${where}: the chip is not an element containing the glyph`).toBe(true);
    expect(chip.borderWidth, `${where}: no visible border`).toBeGreaterThan(0);
    expect(chip.borderStyle, `${where}: border is not drawn`).not.toBe('none');
    expect(chip.borderOpaque, `${where}: border colour is transparent`).toBe(true);
    expect(chip.radius, `${where}: no rounded corners`).toBeGreaterThan(0);
    expect(chip.surfacePainted, `${where}: chip has no painted surface`).toBe(true);
    expect(chip.boxShadow, `${where}: no dimensional treatment`).not.toBe('none');
    expect(chip.glyphInside, `${where}: glyph is not contained by the chip`).toBe(true);
    // A container, not a frame drawn exactly around the artwork.
    expect(chip.glyphWidth, `${where}: glyph fills the whole chip`).toBeLessThan(chip.width);
  }
});

test('the chip surface and border derive from the protocol colour', async ({page}) => {
  await openSettings(page, 'sources');
  const chips = await chipGeometry(page, 'sources');
  const by = Object.fromEntries(chips.map(chip => [chip.protocol, chip]));

  // Two protocols that share a colour render the same surface...
  expect(by.general_http.borderColour).toBe(by.direct_sources.borderColour);
  expect(by.general_http.background).toBe(by.direct_sources.background);
  // ...and protocols with different colours do not.
  for (const [a, b] of [['general_http', 'general_ftp'], ['general_ftp', 'usenet'],
                        ['general_http', 'usenet']]) {
    expect(by[a].borderColour, `${a} vs ${b} share a border colour`).not.toBe(by[b].borderColour);
    expect(by[a].background, `${a} vs ${b} share a surface`).not.toBe(by[b].background);
  }
  // The glyph's glow still comes from the same canonical colour.
  for (const chip of chips) expect(chip.glyphFilter).toContain('drop-shadow');
});

test('Sources and Downloads consume one shared chip primitive', async ({page}) => {
  await openSettings(page, 'sources');
  const sources = Object.fromEntries((await chipGeometry(page, 'sources')).map(c => [c.protocol, c]));
  await openSettings(page, 'downloads');
  const downloads = Object.fromEntries((await chipGeometry(page, 'downloads')).map(c => [c.protocol, c]));

  for (const protocol of ['direct_sources', 'usenet']) {
    const a = sources[protocol], b = downloads[protocol];
    expect(a, `Sources ${protocol} chip missing`).toBeTruthy();
    expect(b, `Downloads ${protocol} chip missing`).toBeTruthy();
    expect(b.classes, `${protocol}: Downloads uses different classes`).toBe(a.classes);
    for (const property of ['borderWidth', 'borderColour', 'radius', 'background',
                            'boxShadow', 'width', 'height', 'glyphWidth']) {
      expect(b[property], `${protocol}: ${property} differs between Sources and Downloads`)
        .toEqual(a[property]);
    }
  }
});

test('the chip keeps its grammar in the light theme', async ({page}) => {
  await openSettings(page, 'sources');
  const dark = await chipGeometry(page, 'sources');
  await page.evaluate(() => document.body.classList.add('light'));
  const light = await chipGeometry(page, 'sources');
  await page.evaluate(() => document.body.classList.remove('light'));

  expect(light.length).toBe(dark.length);
  for (const chip of light) {
    expect(chip.borderWidth, `${chip.protocol}: light theme lost its border`).toBeGreaterThan(0);
    expect(chip.borderOpaque, `${chip.protocol}: light theme border is transparent`).toBe(true);
    expect(chip.radius).toBeGreaterThan(0);
    expect(chip.surfacePainted, `${chip.protocol}: light theme lost its surface`).toBe(true);
    expect(chip.glyphInside).toBe(true);
  }
  // The theme actually changes the treatment rather than reusing the dark ramp.
  const darkBy = Object.fromEntries(dark.map(c => [c.protocol, c]));
  expect(light.some(chip => chip.background !== darkBy[chip.protocol].background)).toBe(true);
});

test('the chip is one primitive, distinct from the naked inner-card icon', async ({page}) => {
  await openSettings(page, 'sources');
  const shared = await page.evaluate(() => {
    const chip = document.querySelector('.dp-settings-panel[data-panel="sources"] [data-protocol]');
    const inner = document.querySelector('#view-settings .dp-settings-inner-card-icon');
    return {
      chipClasses: chip ? chip.className.split(/\s+/).filter(Boolean) : [],
      innerBorder: inner ? parseFloat(getComputedStyle(inner).borderTopWidth) || 0 : null,
    };
  });
  // The chip does not borrow the naked icon's class: one owner, not two.
  expect(shared.chipClasses).not.toContain('dp-settings-inner-card-icon');
  expect(shared.chipClasses.length, 'the chip carries more than one presentation class').toBe(1);
});
