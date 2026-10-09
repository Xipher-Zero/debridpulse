const { test, expect } = require('@playwright/test');

/* DP 1.0.13 canonical visual identity: the locked Settings family, protocol
 * and premium provider identities, and the badge composition of the transfer
 * rows that carry an identity datum today. Presentation only: every row below
 * is an existing /api/torrents shape, served unchanged. */

const rgb = hex => `rgb(${[1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16)).join(', ')})`;
const FAMILY = {
  sources: ['#D657FF', 'zap'], downloads: ['#2563EB', 'download-downloads'], extraction: ['#FF9D00', 'package-open'],
  authentication: ['#1DDB69', 'shield-user'], notifications: ['#04D7FE', 'bell-ring'], maintenance: ['#6366F1', 'database'],
};
const CARDS = {
  'Automatic Extraction': ['extraction', 'archive-restore'],
  'Authentication Status': ['authentication', 'shield-check'], 'Username & Password': ['authentication', 'user-lock'],
  'OpenID Connect': ['authentication', 'id-card'], 'API Access': ['authentication', 'key-round'],
  'Discord Notifications': ['notifications', 'message-square'], 'Statistics Reporting': ['notifications', 'chart-line'],
  'Backups & Retention': ['maintenance', 'archive'], 'Database Reset Controls': ['maintenance', 'database-x'],
};
const PROVIDERS = {alldebrid: '#FFA34E', debridlink: '#418DD5', premiumize: '#A43C8E', realdebrid: '#7EAC56',
                   torbox: '#0FBD88'};

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

async function openSettings(page, tab) {
  await isolateExternalFonts(page);
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await page.locator(`#view-settings [data-tab="${tab}"]`).click();
  await expect(page.locator(`.dp-settings-panel[data-panel="${tab}"]`)).toBeVisible();
}

const prop = (locator, name) => locator.evaluate((node, name) => getComputedStyle(node).getPropertyValue(name).trim(), name);

for (const theme of ['dark', 'light']) {
  test(`Settings families, protocol and provider identities in the ${theme} theme`, async ({page}) => {
    await openSettings(page, 'sources');
    if (theme === 'light') await page.evaluate(() => document.body.classList.add('light'));
    for (const [tab, [colour, glyph]] of Object.entries(FAMILY)) {
      const button = page.locator(`#view-settings .stab[data-tab="${tab}"]`);
      expect((await prop(button, '--dp-settings-tab-color')).toUpperCase()).toBe(colour);
      await expect(button.locator('img')).toHaveAttribute('src', `/icons/lucide/${glyph}.svg`);
    }
    // Services: Premium Services' Crown, Network Sources' upright Network, Multimeta's inverted one.
    const crown = page.locator('.dp-settings-debrid-services > .card-header .dp-settings-protocol-chip[data-section="sources"]');
    expect(await prop(crown, '--dp-protocol-color')).toBe('#D657FF');
    await expect(crown.locator('img')).toHaveAttribute('src', '/icons/lucide/crown.svg');
    const chip = protocol => page.locator(`[data-panel="sources"] .dp-settings-protocol-chip[data-protocol="${protocol}"]`).first();
    expect(await prop(chip('direct_sources'), '--dp-protocol-color')).toBe('#D657FF');
    await expect(chip('direct_sources').locator('img')).toHaveAttribute('src', '/icons/lucide/network-services.svg');
    expect(await prop(chip('multimeta'), '--dp-protocol-color')).toBe('#E879F9');
    await expect(chip('multimeta').locator('img')).toHaveAttribute('src', '/icons/lucide/network.svg');
    for (const [provider, colour] of Object.entries(PROVIDERS)) {
      const block = page.locator(`#view-settings .dp-settings-provider-chip--${provider}`).first();
      expect(await prop(block, '--dp-provider-color')).toBe(colour);
      await expect(block.locator('img.dp-settings-provider-logo')).toHaveCount(1);   // the shipped mark, inside
      expect(await block.evaluate(node => getComputedStyle(node).borderTopStyle)).toBe('solid');
    }
    await page.locator('#view-settings [data-tab="downloads"]').click();
    const transfer = page.locator('.dp-executor-tuning-group > .card-header .dp-settings-protocol-chip[data-section="downloads"]');
    expect(await prop(transfer, '--dp-protocol-color')).toBe('#2563EB');
    await expect(transfer.locator('img')).toHaveAttribute('src', '/icons/lucide/arrow-left-right.svg');
    await expect(page.locator('.dp-executor-tuning-group > .card-header .card-title')).toContainText('Transfer Method Settings');
    const usenet = page.locator('[data-panel="downloads"] .dp-settings-protocol-chip[data-protocol="usenet"]').first();
    expect(await prop(usenet, '--dp-protocol-color')).toBe('#C547FF');
    for (const [title, [section, glyph]] of Object.entries(CARDS)) {
      const tab = section === 'downloads' ? 'downloads' : section;
      await page.locator(`#view-settings [data-tab="${tab}"]`).click();
      const icon = page.locator('#view-settings .card-header .card-title', {hasText: title})
        .locator(`.dp-settings-header-chip[data-section="${section}"]`);
      await expect(icon.locator('img')).toHaveAttribute('src', `/icons/lucide/${glyph}.svg`);
      expect((await prop(icon, '--dp-protocol-color')).toUpperCase()).toBe(FAMILY[section][0]);
    }
  });
}

// The four Downloads subsection headers: each glyph inside the one Downloads
// family chip Transfer Method Settings established -- same geometry, surface,
// border and glow -- in both themes, wide and narrow.
const DOWNLOADS_HEADERS = [
  ['.dp-settings-download-engine-card', 'Download Behavior & Limits', 'gauge'],
  ['.dp-executor-tuning-group', 'Transfer Method Settings', 'arrow-left-right'],
  ['.dp-settings-download-recovery-card', 'Disk Space & Recovery', 'shield-alert'],
  ['.dp-executor-work-card', 'Download Engine Activity', 'activity'],
];
for (const [theme, width] of [['dark', 1440], ['light', 1440], ['dark', 390], ['light', 390]]) {
  test(`Downloads subsection headers share the family chip (${theme}, ${width}px)`, async ({page}) => {
    await openSettings(page, 'downloads');
    await page.setViewportSize({width, height: 1000});
    if (theme === 'light') await page.evaluate(() => document.body.classList.add('light'));
    const facts = await page.evaluate(headers => headers.map(([card, title]) => {
      const heading = document.querySelector(`.dp-settings-panel[data-panel="downloads"] ${card} > .card-header .card-title`);
      const chip = heading.querySelector(':scope > .dp-settings-protocol-chip');
      const style = getComputedStyle(chip);
      const box = chip.getBoundingClientRect();
      const text = heading.querySelector('.dp-settings-card-title-text').getBoundingClientRect();
      const image = chip.querySelector('img');
      const glyph = image.getBoundingClientRect();
      return {
        title: heading.textContent.trim(), expected: title, section: chip.dataset.section, src: image.getAttribute('src'),
        naked: heading.querySelectorAll('.dp-settings-inner-card-icon').length,
        colour: style.getPropertyValue('--dp-protocol-color').trim(),
        geometry: [box.width, box.height, style.borderTopWidth, style.borderTopStyle, style.borderTopColor,
          style.borderTopLeftRadius, style.backgroundImage, style.boxShadow, glyph.width, glyph.height,
          getComputedStyle(image).filter].join(' | '),
        glyphInside: glyph.left >= box.left && glyph.right <= box.right && glyph.top >= box.top && glyph.bottom <= box.bottom,
        gap: Math.round(text.left - box.right), centre: Math.abs((box.top + box.height / 2) - (text.top + text.height / 2)),
        overflow: heading.scrollWidth > heading.clientWidth + 1 || box.right > document.documentElement.clientWidth,
      };
    }), DOWNLOADS_HEADERS);
    DOWNLOADS_HEADERS.forEach(([, title, glyph], index) => {
      const fact = facts[index];
      expect(fact.title).toBe(title);
      expect(fact.section, title).toBe('downloads');
      expect(fact.colour, title).toBe('#2563EB');
      expect(fact.src, title).toBe(`/icons/lucide/${glyph}.svg`);
      expect(fact.naked, `${title}: no free-floating glyph`).toBe(0);
      expect(fact.glyphInside, `${title}: glyph inside the chip`).toBe(true);
      expect(fact.geometry, `${title}: the Transfer Method Settings chip`).toBe(facts[1].geometry);
      expect(fact.gap, title).toBe(facts[1].gap);
      expect(fact.centre, `${title}: chip and title share a centre line`).toBeLessThanOrEqual(1);
      expect(fact.overflow, `${title}: no overflow at ${width}px`).toBe(false);
    });
    expect(facts[0].geometry.startsWith('38 | 38 | 1px | solid')).toBe(true);
  });
}

// Every Settings subsection header: its glyph inside the one header chip, in
// its family colour, with the chip's inner glow and the outer glow around it --
// the treatment the Network Sources master established. The glows are proven
// in pixels: each is switched off on the rendered page and the screenshot must
// change in the ring around (outer) or inside (inner) the chip.
const HEADERS = [
  ['sources', 'Premium Services', '.dp-settings-debrid-services', 'sources', 'crown'],
  ['sources', 'Network Sources', '.dp-settings-general-sources', null, 'network-services'],
  ['downloads', 'Download Behavior & Limits', '.dp-settings-download-engine-card', 'downloads', 'gauge'],
  ['downloads', 'Transfer Method Settings', '.dp-executor-tuning-group', 'downloads', 'arrow-left-right'],
  ['downloads', 'Disk Space & Recovery', '.dp-settings-download-recovery-card', 'downloads', 'shield-alert'],
  ['downloads', 'Download Engine Activity', '.dp-executor-work-card', 'downloads', 'activity'],
  ['extraction', 'Automatic Extraction', '.dp-settings-extraction-card', 'extraction', 'archive-restore'],
  ['authentication', 'Authentication Status', '.dp-settings-auth-status-card', 'authentication', 'shield-check'],
  ['authentication', 'Username & Password', '.dp-settings-username-password-card', 'authentication', 'user-lock'],
  ['authentication', 'OpenID Connect', '.dp-settings-oidc-card', 'authentication', 'id-card'],
  ['authentication', 'API Access', '.dp-settings-api-access-card', 'authentication', 'key-round'],
  ['notifications', 'Discord Notifications', '.dp-settings-discord-card', 'notifications', 'message-square'],
  ['notifications', 'Statistics Reporting', '.dp-settings-statistics-reporting-card', 'notifications', 'chart-line'],
  ['maintenance', 'Backups & Retention', '.dp-settings-backups-retention-card', 'maintenance', 'archive'],
  ['maintenance', 'Database Reset Controls', '.dp-settings-database-wipe-card', 'maintenance', 'database-x'],
];

/* Mean per-pixel RGB difference between two PNG screenshots of the same clip,
 * over the pixels selected by ``where(x, y)``. Decoded by the page's own canvas. */
async function pixelDelta(page, before, after, where) {
  return page.evaluate(async ({before, after, where}) => {
    const select = new Function('x', 'y', `return ${where};`);
    const decode = async data => {
      const image = await createImageBitmap(await (await fetch(`data:image/png;base64,${data}`)).blob());
      const canvas = new OffscreenCanvas(image.width, image.height);
      const context = canvas.getContext('2d');
      context.drawImage(image, 0, 0);
      return context.getImageData(0, 0, image.width, image.height);
    };
    const [a, b] = [await decode(before), await decode(after)];
    let total = 0;
    let count = 0;
    for (let y = 0; y < a.height; y += 1) {
      for (let x = 0; x < a.width; x += 1) {
        if (!select(x, y)) continue;
        const i = (y * a.width + x) * 4;
        total += Math.abs(a.data[i] - b.data[i]) + Math.abs(a.data[i + 1] - b.data[i + 1]) + Math.abs(a.data[i + 2] - b.data[i + 2]);
        count += 1;
      }
    }
    return total / count / 3;
  }, {before: before.toString('base64'), after: after.toString('base64'), where});
}

for (const theme of ['dark', 'light']) {
  test(`all fifteen Settings subsection headers wear the one header chip with inner and outer glow (${theme}, 1440px)`, async ({page}) => {
    await openSettings(page, 'sources');
    if (theme === 'light') await page.evaluate(() => document.body.classList.add('light'));
    const facts = [];
    for (const [tab, title, card, section, glyph] of HEADERS) {
      await page.locator(`#view-settings [data-tab="${tab}"]`).click();
      const heading = page.locator(`.dp-settings-panel[data-panel="${tab}"] ${card} > .card-header .card-title`).first();
      await heading.scrollIntoViewIfNeeded();
      const chip = heading.locator(':scope > .dp-settings-protocol-chip');
      await expect(chip, title).toHaveCount(1);
      await expect(heading.locator('img'), `${title}: one glyph`).toHaveCount(1);
      await expect(heading.locator('.dp-settings-card-title-text')).toHaveText(title);
      await expect(chip).toHaveClass(/\bdp-settings-header-chip\b/);
      await expect(chip.locator('img')).toHaveAttribute('src', `/icons/lucide/${glyph}.svg`);
      if (section) await expect(chip).toHaveAttribute('data-section', section);
      const fact = await heading.evaluate(node => {
        const chipNode = node.querySelector(':scope > .dp-settings-protocol-chip');
        const style = getComputedStyle(chipNode);
        const box = chipNode.getBoundingClientRect();
        const image = chipNode.querySelector('img');
        const glyphBox = image.getBoundingClientRect();
        const text = node.querySelector('.dp-settings-card-title-text').getBoundingClientRect();
        const neutral = value => value.replace(/color\([^)]*\)|rgba?\([^)]*\)/g, 'C');
        return {
          colour: style.getPropertyValue('--dp-protocol-color').trim(),
          geometry: [box.width, box.height, style.borderTopWidth, style.borderTopStyle, style.borderTopLeftRadius,
            glyphBox.width, glyphBox.height, glyphBox.left - box.left, glyphBox.top - box.top].join(' | '),
          material: [neutral(style.backgroundImage), neutral(style.boxShadow), neutral(style.filter),
            neutral(getComputedStyle(image).filter)].join(' | '),
          gap: Math.round(text.left - box.right),
          centre: Math.abs((box.top + box.height / 2) - (text.top + text.height / 2)),
          clip: {x: box.x - 14, y: box.y - 14, width: box.width + 28, height: box.height + 28},
        };
      });
      // Outer glow, in pixels: the ring 1-9px outside the chip changes when the
      // glow is switched off, so it is painted and not clipped or occluded.
      const shot = () => page.screenshot({clip: fact.clip, animations: 'disabled'});
      const lit = await shot();
      await chip.evaluate(node => { node.style.filter = 'none'; });
      const unlit = await shot();
      await chip.evaluate(node => { node.style.filter = ''; });
      const ring = 'Math.max(14 - x, x - (' + (fact.clip.width - 15) + '), 14 - y, y - (' + (fact.clip.height - 15) + ')) >= 1 && '
        + 'Math.max(14 - x, x - (' + (fact.clip.width - 15) + '), 14 - y, y - (' + (fact.clip.height - 15) + ')) <= 9';
      fact.outer = await pixelDelta(page, lit, unlit, ring);
      // Inner glow, in pixels: the glyph's own glow inside the chip.
      await chip.locator('img').evaluate(node => { node.style.filter = 'none'; });
      const flat = await shot();
      await chip.locator('img').evaluate(node => { node.style.filter = ''; });
      fact.inner = await pixelDelta(page, lit, flat, `x > 15 && x < ${fact.clip.width - 16} && y > 15 && y < ${fact.clip.height - 16}`);
      facts.push({title, ...fact});
    }
    const reference = facts[1];
    const report = facts.map(f => `${f.title}: outer ${f.outer.toFixed(1)} inner ${f.inner.toFixed(1)}`).join('\n');
    for (const fact of facts) {
      const family = HEADERS.find(([, title]) => title === fact.title)[0];
      expect(fact.colour.toUpperCase(), fact.title).toBe(FAMILY[family][0]);
      expect(fact.geometry, `${fact.title}: the Network Sources chip geometry`).toBe(reference.geometry);
      expect(fact.material, `${fact.title}: the Network Sources chip material`).toBe(reference.material);
      expect(fact.gap, `${fact.title}: label spacing`).toBe(reference.gap);
      expect(fact.centre, `${fact.title}: chip and label share a centre line`).toBeLessThanOrEqual(1);
      expect(fact.outer, `${fact.title}: visible outer glow\n${report}`).toBeGreaterThan(3);
      expect(fact.inner, `${fact.title}: visible inner glow\n${report}`).toBeGreaterThan(1);
    }
    test.info().annotations.push({type: 'glow', description: report});
  });
}

// --- Transfer rows: every existing badge family, from existing data alone ---------------

function row(id, overrides) {
  return {
    id, name: `Transfer ${id}`, status: 'downloading', presentation_status: 'downloading',
    presentation_label: 'Downloading', presentation_badge_status: 'downloading', attention_required: false,
    progress: 10, active_execution_progress: null, retained_bytes: 0, size_bytes: 1000, source: 'manual', hash: '',
    label: '', created_at: '2026-10-09T00:00:00Z', current_source_identity: {kind: 'link'}, providers: [],
    historical_providers: [], delivering_provider_ids: [], input_required: null, ...overrides,
  };
}

// The badge each row ALREADY shows: its provider id and display name, as projected.
const NETWORK = [
  ['general_http', 'HTTP(S)', '#3B82F6', 'globe'], ['general_ftp', '(S)FTP', '#2DD4BF', 'arrow-up-down'],
  ['general_scp', 'SCP', '#A78BFA', 'file-down'], ['general_rsync', 'rsync', '#7C3AED', 'folder-sync'],
  ['general_webdav', 'WebDAV', '#38BDF8', 'cloud-sync'], ['multimeta', 'Multimeta', '#E879F9', 'network'],
  ['usenet', 'Usenet', '#C547FF', 'newspaper'], ['media', 'Media Download', '#FF56AE', 'monitor-down'],
];
const PREMIUM = [
  ['alldebrid', 'AllDebrid', '#FFA34E', {kind: 'host', host: 'rapidgator.net'}],
  ['debridlink', 'Debrid-Link', '#418DD5', {kind: 'link'}],
  ['premiumize', 'Premiumize', '#A43C8E', {kind: 'torrent_file'}],
  ['realdebrid', 'Real-Debrid', '#7EAC56', {kind: 'host', host: 'mega.nz'}],
  ['torbox', 'TorBox', '#0FBD88', {kind: 'magnet'}],
];
const ROWS = [
  ...NETWORK.map(([id, name], index) => row(9810 + index, {origin_provider_id: id, origin_provider_name: name})),
  ...PREMIUM.map(([id, name, _colour, source], index) =>
    row(9830 + index, {origin_provider_id: id, origin_provider_name: name, current_source_identity: source})),
  // A torrent root's committed route (the root-provider badge), premium-served.
  row(9840, {route_provider_id: 'torbox', route_provider_name: 'TorBox', current_source_identity: {kind: 'magnet'}}),
  // A provider not yet known keeps the existing neutral pending badge.
  row(9841, {}),
];

async function serve(page) {
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({items: ROWS, total: ROWS.length}),
  }));
}

async function rowFacts(page, scope) {
  return page.evaluate(scope => {
    // WCAG contrast of two computed colours, the second composited over the
    // nearest opaque surface behind `node` (the colours a viewer actually sees).
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 1;
    const paint = canvas.getContext('2d', {willReadFrequently: true});
    const behind = node => {
      for (let el = node.parentElement; el; el = el.parentElement) {
        const colour = getComputedStyle(el).backgroundColor;
        if (colour.startsWith('rgb(')) return colour;
      }
      return getComputedStyle(document.body).backgroundColor;
    };
    const pixel = (...layers) => {
      paint.clearRect(0, 0, 1, 1);
      for (const layer of layers) { paint.fillStyle = layer; paint.fillRect(0, 0, 1, 1); }
      return [...paint.getImageData(0, 0, 1, 1).data].slice(0, 3);
    };
    const srgb = colour => `rgb(${pixel(colour).join(', ')})`;
    const luminance = rgb => {
      const [r, g, b] = rgb.map(v => { v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; });
      return .2126 * r + .7152 * g + .0722 * b;
    };
    const contrast = (ink, node, face) => {
      const base = behind(node);
      const [a, b] = [luminance(pixel(base, ink)), luminance(pixel(base, face))];
      return Math.round((Math.max(a, b) + .05) / (Math.min(a, b) + .05) * 100) / 100;
    };
    return Object.fromEntries(Array.from(
    document.querySelectorAll(`${scope} tr[data-torrent-id]`), tr => {
      const chip = tr.querySelector('.dp-provider-chip');
      const chipBox = chip.getBoundingClientRect();
      const glyph = chip.querySelector('.dp-provider-glyph');
      const glyphBox = glyph?.getBoundingClientRect();
      const label = chip.querySelector('.dp-root-provider-name') || chip;
      const slot = tr.querySelector('.dp-source-icon-slot');
      return [tr.dataset.torrentId, {
        text: label.textContent.trim(), colour: srgb(getComputedStyle(chip).color),
        identity: getComputedStyle(chip).getPropertyValue('--dp-provider-accent').trim().toUpperCase(),
        contrast: contrast(getComputedStyle(chip).color, chip, getComputedStyle(chip).backgroundColor),
        identityContrast: contrast(getComputedStyle(chip).getPropertyValue('--dp-provider-accent').trim(), chip,
          getComputedStyle(chip).backgroundColor),
        glyphInk: glyph ? srgb(getComputedStyle(glyph).backgroundColor) : '',
        border: getComputedStyle(chip).borderTopColor, pseudo: getComputedStyle(chip, '::before').content,
        glyph: glyph ? getComputedStyle(glyph).maskImage.replace(location.origin, '') : '',
        images: chip.querySelectorAll('.dp-provider-glyph, img, svg').length,
        glyphInsideLeft: glyph ? glyphBox.left >= chipBox.left && glyphBox.right <= chipBox.right
          && glyphBox.top >= chipBox.top - 1 && glyphBox.bottom <= chipBox.bottom + 1
          && glyphBox.left - chipBox.left < chipBox.width / 3 : null,
        slot: slot ? (slot.querySelector('img')?.getAttribute('src') || [...(slot.querySelector('svg')?.classList || [])].join(' ')) : '',
        magnet: (magnet => magnet ? {colour: srgb(getComputedStyle(magnet).color),
          contrast: contrast(getComputedStyle(magnet).color, magnet, getComputedStyle(slot).backgroundColor),
          // Where the magnet's two pole ticks are DRAWN, from the glyph's centre
          // (screen degrees, y down: 270 = poles up).
          facing: (() => {
            const centre = node => { const box = node.getBoundingClientRect(); return [box.left + box.width / 2, box.top + box.height / 2]; };
            const [poleA, , poleB] = magnet.querySelectorAll('path');
            const [gx, gy] = centre(magnet), [ax, ay] = centre(poleA), [bx, by] = centre(poleB);
            return Math.round((Math.atan2((ay + by) / 2 - gy, (ax + bx) / 2 - gx) * 180 / Math.PI + 360) % 360);
          })()} : null)(
          slot?.querySelector('.dp-source-magnet')),
        slotLeftOfChip: slot ? slot.getBoundingClientRect().right <= chipBox.left + 1 : null,
        rowHeight: Math.round(tr.getBoundingClientRect().height),
      }];
    }));
  }, scope);
}

// The identity hex is the badge's datum in both themes. The label is drawn in
// the identity ink: the identity itself wherever that already reads at
// >= 4.5:1 on dark, otherwise the same hue with only its lightness moved (every
// identity on light) -- and the ink always reads at >= 4.5:1 on its badge face.
function expectIdentityInk(fact, id, colour, theme) {
  expect(fact.identity, `${id}: identity datum`).toBe(colour);
  if (theme === 'dark' && fact.identityContrast >= 4.5) expect(fact.colour, id).toBe(rgb(colour));
  else expect(fact.colour, `${id}: ink derived from the identity`).not.toBe(rgb(colour));
  expect(fact.contrast, `${id}: label contrast ${fact.contrast}`).toBeGreaterThanOrEqual(4.5);
}

for (const [label, open, scope] of [
  ['Downloads', async page => {
    await page.evaluate(async () => { nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]')); await loadTorrents(); });
  }, '#t-tbody'],
  ['Recent Activity', async page => { await page.evaluate(() => loadRecent()); }, '#dash-tbody'],
]) {
  for (const [theme, width] of [['dark', 1440], ['light', 1440], ['dark', 760], ['light', 760]]) {
    test(`${label} badges (${theme}, ${width}px): every network source and premium provider`, async ({page}) => {
      await isolateExternalFonts(page);
      await serve(page);
      await page.setViewportSize({width, height: 1400});
      await page.goto('/');
      if (theme === 'light') await page.evaluate(() => document.body.classList.add('light'));
      await open(page);
      await expect(page.locator(`${scope} tr[data-torrent-id="9841"]`)).toBeVisible();
      const facts = await rowFacts(page, scope);
      // Network sources: their colour, their glyph inside on the left, existing label, no generic link.
      NETWORK.forEach(([id, name, colour, glyph], index) => {
        const fact = facts[String(9810 + index)];
        expect(fact.text, id).toBe(name);
        expectIdentityInk(fact, id, colour, theme);
        expect(fact.glyph, id).toBe(`url("/icons/lucide/${glyph}.svg")`);
        expect(fact.glyphInk, `${id}: the glyph is drawn in the label's ink`).toBe(fact.colour);
        expect(fact.images, `${id}: exactly one glyph`).toBe(1);
        expect(fact.glyphInsideLeft, `${id}: glyph inside, on the left`).toBe(true);
        expect(fact.slot, `${id}: the generic link icon is gone`).toBe('');
      });
      // Premium providers: their colour, text only, external source artwork kept to the left.
      PREMIUM.forEach(([id, name, colour, source], index) => {
        const fact = facts[String(9830 + index)];
        expect(fact.text, id).toBe(name);
        expectIdentityInk(fact, id, colour, theme);
        expect(fact.glyph, id).toBe('');
        expect(fact.images, `${id}: text only`).toBe(0);
        expect(fact.pseudo, `${id}: no pseudo-element glyph`).toBe('none');
        expect(fact.slotLeftOfChip, `${id}: external artwork left of the badge`).toBe(true);
        expect(fact.slot, id).toBe(source.kind === 'host' ? `/icons/hosts/${source.host === 'mega.nz' ? 'mega.svg' : 'rapidgator.png'}`
          : (source.kind === 'link' ? 'lucide dp-source-fallback' : 'lucide dp-source-fallback lucide-magnet dp-source-magnet'));
      });
      expect(facts['9840'].text).toBe('TorBox');
      expectIdentityInk(facts['9840'], 'torbox root', '#0FBD88', theme);
      // The torrent/magnet source glyph: its identity on dark, readable (non-text, >= 3:1) on light.
      for (const id of ['9834', '9840']) {
        const magnet = facts[id].magnet;
        if (theme === 'dark') expect(magnet.colour, id).toBe(rgb('#14FF8C'));
        // Upright: poles up. The retired 315deg turn drew them down (90).
        expect(magnet.facing, `${id}: magnet poles as drawn`).toBe(270);
        expect(magnet.contrast, `${id}: magnet glyph contrast ${magnet.contrast}`).toBeGreaterThanOrEqual(3);
      }
      expect(facts['9840'].images).toBe(0);
      expect(facts['9841'].text).toBe('Pending');
      expect(facts['9841'].glyph).toBe('');
      // Row geometry: no badge family changes the row's height.
      expect(new Set(Object.values(facts).map(fact => fact.rowHeight)).size).toBe(1);
      expect(await page.evaluate(() =>
        document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)).toBe(true);
    });
  }
}
