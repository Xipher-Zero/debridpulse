const { test, expect } = require('@playwright/test');

/* DP 1.0.13 work items B, C, D and E -- the canonical dialog, the one
 * disclosure chip, capability copy, and copy centred on the FULL header. */

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

async function openSettings(page, tab) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator(`#view-settings [data-tab="${tab}"]`).click();
  await expect(page.locator(`.dp-settings-panel[data-panel="${tab}"]`)).toBeVisible();
}

/* The dialog and the header are presentation, so the settings document is
 * SERVED to the page rather than persisted: every case is deterministic and no
 * shared backend state is left behind for another spec. */
const USENET_FIXTURE = {
  enabled: true, priority: 0, name: 'Usenet', kind: 'provider_executor', configured: true,
  presentation: {status_name: 'Usenet', premium: true, status_endpoint: null,
    static_status: 'healthy', display_order: 20, status_group: null, status_group_label: null,
    status_tier: 'general_family', status_tier_label: 'General'},
  options: {
    operation_timeout_seconds: 30, article_cache_megabytes: 1024,
    direct_write: true, max_acquisition_retries: 3,
    servers: [{
      id: 'dialog-fixture', host: 'news.example.com', port: 563, ssl: true,
      username: 'u', password: '', password_configured: true,
      connections: 8, priority: 0, articles_per_request: 2, timeout_seconds: 60,
      enabled: true, display_name: '',
    }],
  },
};

async function serveUsenet(page) {
  const live = await page.request.get('/api/settings').then(response => response.json());
  const fixture = JSON.parse(JSON.stringify(USENET_FIXTURE));
  const document_ = {...live, integrations: {...live.integrations, usenet: fixture}};
  await page.route(url => url.pathname === '/api/settings', route =>
    (route.request().method() === 'GET'
      ? route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(document_)})
      : route.fallback()));

  /* The served record needs the canonical per-record write surface it implies:
   * a server card's ordinary fields -- the display name among them -- persist
   * themselves through `PUT /usenet/servers/{id}`, so the fixture answers that
   * write the way the backend does, merging only the supplied fields. */
  await page.route(url => /^\/api\/usenet\/servers\/[^/]+$/.test(url.pathname), route => {
    if (route.request().method() !== 'PUT') return route.fallback();
    const [server] = fixture.options.servers;
    const body = route.request().postDataJSON() || {};
    for (const [key, value] of Object.entries(body)) {
      if (key !== 'clear_password' && key !== 'password') server[key] = value;
    }
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(
      {ok: true, server_id: server.id, servers: [server]})});
  });
}

// --- B: the rename dialog is the application's, never the browser's -------

test('editing a server display name opens the DP modal and never a browser prompt', async ({page}) => {
  await isolateExternalFonts(page);
  await serveUsenet(page);
  await page.goto('/');
  await openSettings(page, 'sources');
  await revealUsenet(page);
  await expect(page.locator('[data-usenet-collection] [data-usenet-server-id]')).toHaveCount(1);

  const nativeCalls = [];
  await page.exposeFunction('__dpNative', name => nativeCalls.push(name));
  await page.evaluate(() => {
    for (const name of ['prompt', 'alert', 'confirm']) {
      window[name] = (...args) => { window.__dpNative(name); return null; };
    }
  });

  const card = page.locator('[data-usenet-collection] [data-usenet-server-id]').first();
  await card.locator('[data-usenet-action="rename"]').click();

  const dialog = page.locator('.dp-modal-overlay .dp-modal-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('.dp-modal-title')).toHaveText('Edit Server Name');
  await expect(dialog.locator('.dp-modal-field .form-label')).toHaveText('Display Name');
  await expect(dialog.locator('.dp-modal-field .input')).toBeFocused();
  // The derived host never masquerades as an explicit override.
  await expect(dialog.locator('.dp-modal-field .input')).toHaveValue('');
  expect(nativeCalls).toEqual([]);

  // Cancel changes nothing.
  await dialog.locator('[data-modal-cancel]').click();
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
  await expect(card.locator('[data-usenet-display-name]')).toHaveText('news.example.com');

  // Enter accepts once.
  await card.locator('[data-usenet-action="rename"]').click();
  await page.locator('.dp-modal-field .input').fill('Primary feed');
  await page.keyboard.press('Enter');
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
  await expect(card.locator('[data-usenet-display-name]')).toHaveText('Primary feed');
  // Focus returns to the initiating control.
  await expect(card.locator('[data-usenet-action="rename"]')).toBeFocused();

  // Reopening prefills the explicit override only.
  await card.locator('[data-usenet-action="rename"]').click();
  await expect(page.locator('.dp-modal-field .input')).toHaveValue('Primary feed');
  // Escape cancels.
  await page.keyboard.press('Escape');
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
  await expect(card.locator('[data-usenet-display-name]')).toHaveText('Primary feed');

  // Blank Save clears the override and the name returns to the host.
  await card.locator('[data-usenet-action="rename"]').click();
  await page.locator('.dp-modal-field .input').fill('');
  await page.locator('[data-modal-accept]').click();
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
  await expect(card.locator('[data-usenet-display-name]')).toHaveText('news.example.com');

  expect(nativeCalls).toEqual([]);
});

// --- C: one canonical disclosure chip, immediately after the title -------

test('provider and executor-tuning cards share one disclosure chip after the title', async ({page}) => {
  await isolateExternalFonts(page);
  await serveUsenet(page);
  await page.goto('/');
  await openSettings(page, 'sources');

  const usenetHeader = page.locator('.dp-settings-provider-card--usenet > .card-header');
  const sourcesChip = usenetHeader.locator('.dp-settings-disclosure');
  await expect(sourcesChip).toHaveCount(1);

  const sourcesBox = await sourcesChip.boundingBox();
  const sourcesTitle = await usenetHeader.locator('.card-title').boundingBox();
  const sourcesEnable = await usenetHeader.locator('.dp-settings-integration-header-enable').boundingBox();
  expect(sourcesBox.x).toBeGreaterThan(sourcesTitle.x + sourcesTitle.width - 1);
  expect(sourcesBox.x).toBeLessThan(sourcesEnable.x - 100);   // beside the title, not at the far edge

  await openSettings(page, 'downloads');
  const tuningHeader = page.locator('.dp-executor-tuning-card[data-executor-tuning="usenet"] > .card-header');
  const tuningChip = tuningHeader.locator('.dp-settings-disclosure');
  await expect(tuningChip).toHaveCount(1);
  const tuningBox = await tuningChip.boundingBox();

  // The exact same visual component in both places.
  expect(Math.round(tuningBox.width)).toBe(Math.round(sourcesBox.width));
  expect(Math.round(tuningBox.height)).toBe(Math.round(sourcesBox.height));

  // Collapsed -> right, expanded -> down, with accurate ARIA.
  await expect(tuningChip).toHaveAttribute('aria-expanded', 'false');
  const controls = await tuningChip.getAttribute('aria-controls');
  await expect(page.locator(`#${controls}`)).toBeHidden();
  const glyph = tuningChip.locator('span');
  const rotation = () => glyph.evaluate(el => getComputedStyle(el).transform);
  // Collapsed points right (no rotation); expanded points down. The chip has a
  // transition, so the settled value is polled rather than sampled mid-flight.
  expect(await rotation()).toBe('matrix(1, 0, 0, 1, 0, 0)');
  await tuningChip.click();
  await expect(tuningChip).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator(`#${controls}`)).toBeVisible();
  await expect.poll(rotation).toBe('matrix(0, 1, -1, 0, 0, 0)');

  // Keyboard accessible.
  await tuningChip.focus();
  await page.keyboard.press('Enter');
  await expect(tuningChip).toHaveAttribute('aria-expanded', 'false');
});

// --- D: capability copy ---------------------------------------------------

test('every provider/source card explains what enabling it allows', async ({page}) => {
  await isolateExternalFonts(page);
  await serveUsenet(page);
  await page.goto('/');
  await openSettings(page, 'sources');
  // The two expandable Premium Services cards say it in the canonical card
  // header. The Network Sources members are compact protocol BOXES with no card
  // header, so they keep the same promise as their two centred lines.
  const headerCopy = {
    'usenet': 'Download NZB content from configured Usenet news servers.',
    'alldebrid': 'Resolve supported links and torrents through your AllDebrid account.',
  };
  for (const [slug, text] of Object.entries(headerCopy)) {
    await expect(page.locator(`.dp-settings-provider-card--${slug} > .card-header .dp-settings-card-header-center`))
      .toHaveText(text);
  }
  const boxCopy = {
    'general-http': 'Direct downloads from HTTP and HTTPS URLs.',
    'general-ftp': 'Direct downloads from FTP and SFTP URLs.',
  };
  for (const [slug, text] of Object.entries(boxCopy)) {
    const lines = page.locator(`.dp-settings-provider-card--${slug} .dp-settings-source-box-copy > span`);
    await expect(lines).toHaveCount(2);
    expect((await lines.allTextContents()).map(line => line.trim()).join(' ')).toBe(text);
  }
});

// --- E: centred against the FULL header, not the flex remainder ----------

test('header flavour copy is geometrically centred on the full card header', async ({page}) => {
  await isolateExternalFonts(page);
  await serveUsenet(page);
  await page.goto('/');
  await openSettings(page, 'downloads');

  const offsets = await page.evaluate(() => {
    const centres = [];
    for (const card of document.querySelectorAll('#view-settings .dp-executor-tuning-card')) {
      const header = card.querySelector('.card-header');
      const copy = header.querySelector('.dp-settings-card-header-center');
      const h = header.getBoundingClientRect(), c = copy.getBoundingClientRect();
      centres.push({id: card.dataset.executorTuning,
        offset: ((c.left + c.right) / 2) - ((h.left + h.right) / 2),
        titleWidth: header.querySelector('.card-title').getBoundingClientRect().width});
    }
    return centres;
  });
  expect(offsets.length).toBeGreaterThan(1);
  // Different title widths, identical centring.
  expect(new Set(offsets.map(o => Math.round(o.titleWidth))).size).toBeGreaterThan(1);
  for (const entry of offsets) expect(Math.abs(entry.offset)).toBeLessThan(1);

  // The same invariant holds for Services cards.
  await openSettings(page, 'sources');
  const sources = await page.evaluate(() => {
    const out = [];
    for (const card of document.querySelectorAll('#view-settings .dp-settings-provider-card')) {
      const header = card.querySelector(':scope > .card-header');
      if (!header) continue;
      const copy = header.querySelector('.dp-settings-card-header-center');
      if (!copy) continue;
      const h = header.getBoundingClientRect(), c = copy.getBoundingClientRect();
      out.push(((c.left + c.right) / 2) - ((h.left + h.right) / 2));
    }
    return out;
  });
  // The two expandable Premium Services cards. The Network Sources members are
  // protocol boxes and have no card header to centre anything on.
  expect(sources.length).toBe(2);
  for (const offset of sources) expect(Math.abs(offset)).toBeLessThan(1);
});

/* The Usenet card renders COLLAPSED: expansion is LOCAL presentation state,
 * never a projection of enabled/configured/verified state. The server
 * collection lives inside its body, so opening it through the canonical
 * disclosure is how this spec reaches the collection -- exactly as an
 * operator does, and writing no canonical state. */
async function revealUsenet(page) {
  const card = page.locator('.dp-settings-provider-card--usenet');
  const disclosure = card.locator('.dp-settings-disclosure');
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
  await expect(card.locator(':scope > .card-body')).toBeVisible();
}
