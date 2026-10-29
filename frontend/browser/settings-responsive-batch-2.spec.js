const { test, expect } = require('@playwright/test');

/* DP 1.0.14 responsive Settings batch 2, read from the rendered page:
 *
 *  1. Download Behavior & Limits: the contracted island keeps each setting's
 *     own geometry -- Maximum Concurrent Downloads a compact group centred
 *     beneath Download Folder, its label, hint and number together.
 *  2/6. Every governing card of Authentication, Users & Access, Notifications
 *     and Data & Maintenance is the hinted Settings header.
 *  3. The session island: Browser Session Lifetime, then Active Browser
 *     Sessions, centred beneath it, the outline hugging both.
 *  4/5. Username & Password: a pair that stays side by side while both are
 *     usable and stacks as two equal groups; Clear Stored Password always
 *     inside the password field; Apply in the field's action slot; nothing is
 *     written until Apply, which sends ONE request carrying both.
 *  7. Its header status is the server's truth: Unconfigured or Configured,
 *     never Verified.
 *
 * The User-role check signs in a REAL non-admin User on the password-auth
 * runtime (DP_AUTH_BASE_URL) with synthetic credentials. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const ADMIN = {username: 'browser', password: 'browser-secret'};
const SENTINEL = 'dp-synthetic-sentinel-7f3a91';

const settle = page => page.evaluate(() => new Promise(resolve => {
  const still = () => !document.querySelector('.dp-motion-surface') && document.getAnimations()
    .every(animation => animation.playState !== 'running' || animation.effect?.getTiming().iterations === Infinity);
  const wait = () => (still() ? requestAnimationFrame(() => requestAnimationFrame(resolve)) : setTimeout(wait, 30));
  wait();
}));

async function resize(page, width) {
  await page.setViewportSize({width, height: 1000});
  await page.waitForFunction(() => document.getElementById('sidebar').getAnimations().every(a => a.playState !== 'running'));
  await settle(page);
}

// A Settings section, chosen through its own tab (even one a narrow navigator shows cut).
async function section(page, name) {
  await page.evaluate(name => document.querySelector(`#view-settings .dp-settings-tabs [data-tab="${name}"]`).click(), name);
  await settle(page);
}

async function open(page, width, tab) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.setViewportSize({width, height: 1000});
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]:not([data-title])').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.evaluate(() => document.fonts.ready);
  await section(page, tab);
}

test('Download Behavior & Limits: the island hugs, and Maximum Concurrent Downloads stays one compact group centred beneath Download Folder', async ({page}) => {
  await open(page, 1440, 'downloads');
  for (const width of [1440, 1180, 1024, 900, 780]) {
    await resize(page, width);
    const g = await page.locator('.dp-settings-download-engine-row').evaluate(island => {
      const r = n => n.getBoundingClientRect();
      const [folder, limit] = [...island.querySelectorAll('.dp-settings-inline-field')];
      const ib = r(island), fb = r(folder), lb = r(limit);
      const info = r(limit.querySelector('.dp-settings-inline-field-info')), control = r(limit.querySelector('.dp-settings-inline-field-control'));
      const pad = parseFloat(getComputedStyle(island).paddingLeft) + parseFloat(getComputedStyle(island).borderLeftWidth);
      return {stacked: lb.top >= fb.bottom - 1, centre: ((lb.left + lb.right) - (fb.left + fb.right)) / 2,
        association: control.left - info.right, limit: lb.width, slack: ib.width - Math.max(fb.width, lb.width) - 2 * pad,
        outer: [ib.left - r(island.parentElement).left, r(island.parentElement).right - ib.right]};
    });
    const at = `${width}px`;
    // The setting keeps its own compact geometry wherever it is.
    expect(g.association, `${at}: the number is far from its label`).toBeLessThanOrEqual(20);
    expect(g.limit, `${at}: Maximum Concurrent Downloads stretched`).toBeLessThanOrEqual(320);
    if (g.stacked) {
      expect(Math.abs(g.centre), `${at}: not centred beneath Download Folder`).toBeLessThanOrEqual(1);
      expect(g.slack, `${at}: the outline is wider than its widest setting`).toBeLessThanOrEqual(1);
    }
    expect(Math.abs(g.outer[0] - g.outer[1]), `${at}: island off centre`).toBeLessThanOrEqual(1);
    if (width === 900) expect(g.stacked).toBe(true);
    if (width === 1440) expect(g.stacked).toBe(false);
  }
});

test('the session island: Lifetime first, Active Browser Sessions centred beneath it once stacked, the outline hugging both', async ({page}) => {
  await open(page, 1440, 'authentication');
  for (const width of [1440, 1024, 900, 780]) {
    await resize(page, width);
    const g = await page.locator('.dp-settings-auth-session-row').evaluate(island => {
      const r = n => n.getBoundingClientRect();
      const fields = [...island.querySelectorAll(':scope > .dp-settings-inline-field')];
      const [first, second] = fields.map(r), ib = r(island);
      const pad = parseFloat(getComputedStyle(island).paddingLeft) + parseFloat(getComputedStyle(island).borderLeftWidth);
      const count = island.querySelector('[data-auth-session-count]'), logout = island.querySelector('[data-action="logout-session"]');
      // A setting's CONTENT: its label and hint as drawn text, its control and
      // its action -- never its box, which may be wider than what it holds.
      const text = node => { const range = document.createRange(); range.selectNodeContents(node);
        const boxes = [...range.getClientRects()]; return {left: Math.min(...boxes.map(b => b.left)), right: Math.max(...boxes.map(b => b.right))}; };
      const content = field => { const parts = [text(field.querySelector('.form-label')), text(field.querySelector('.form-hint')),
        r(field.querySelector('.dp-settings-inline-field-control')), ...[...field.querySelectorAll('.dp-settings-inline-field-action .btn')].map(r)];
        return (Math.min(...parts.map(b => b.left)) + Math.max(...parts.map(b => b.right))) / 2; };
      return {order: fields.map(f => f.querySelector('.form-label').textContent.trim()), stacked: second.top >= first.bottom - 1,
        centre: content(fields[1]) - content(fields[0]),
        slack: ib.width - Math.max(first.width, second.width) - 2 * pad,
        logoutInside: fields[1].contains(logout), logoutNearCount: r(logout).left - r(count).right};
    });
    const at = `${width}px`;
    expect(g.order, at).toEqual(['Browser Session Lifetime', 'Active Browser Sessions']);
    expect(g.logoutInside, `${at}: Log Out left its field`).toBe(true);
    expect(g.logoutNearCount, `${at}: Log Out pushed away from the count`).toBeLessThanOrEqual(40);
    if (g.stacked) {
      expect(Math.abs(g.centre), `${at}: Active Browser Sessions not centred`).toBeLessThanOrEqual(1);
      expect(g.slack, `${at}: the outline is wider than its widest setting`).toBeLessThanOrEqual(1);
    }
    if (width === 900) expect(g.stacked).toBe(true);
  }
});

/* The credential pair as drawn. */
const pair = page => page.evaluate(() => {
  const r = n => n.getBoundingClientRect();
  document.querySelector('[data-action="clear-password"]').scrollIntoView({block: 'center'});
  const row = document.querySelector('.dp-settings-auth-credentials-row');
  const fields = [...row.querySelectorAll(':scope > .dp-settings-inline-field')].map(r);
  const pw = document.querySelector('.dp-settings-auth-password-field');
  const box = pw.querySelector('.dp-action-field'), input = pw.querySelector('input[type="password"]');
  const clear = document.querySelectorAll('[data-action="clear-password"]');
  const apply = pw.querySelector('[data-action="apply-credentials"]');
  const cb = r(clear[0]), bb = r(box), hit = document.elementFromPoint((cb.left + cb.right) / 2, (cb.top + cb.bottom) / 2);
  return {sideBySide: Math.abs(fields[0].top - fields[1].top) < 3, widths: fields.map(f => f.width), overflow: row.scrollWidth - row.clientWidth,
    clears: clear.length, clearInField: box.contains(clear[0]) && cb.left >= r(input).right - 0.5 && cb.right <= bb.right + 0.5
      && Math.abs((cb.top + cb.bottom) / 2 - (bb.top + bb.bottom) / 2) <= 1,
    clearHit: clear[0].contains(hit), input: r(input).width, clearText: clear[0].textContent.trim(),
    clearName: clear[0].getAttribute('aria-label'), clearTip: clear[0].dataset.tooltip, applyOutside: !!apply && !box.contains(apply) && (r(apply).left >= bb.right || r(apply).top >= bb.bottom)};
});

test('Username & Password: side by side while both are usable, then two equal groups; Clear always inside the field; Apply outside it', async ({page}) => {
  await open(page, 1440, 'authentication');
  for (const width of [1440, 1180, 1024, 900, 780, 700]) {
    await resize(page, width);
    const g = await pair(page);
    const at = `${width}px`;
    expect(g.overflow, `${at}: overflow`).toBeLessThanOrEqual(0);
    expect(g.clears, `${at}: a second Clear`).toBe(1);
    expect(g.clearInField, `${at}: Clear is not inside the password field`).toBe(true);
    expect(g.clearHit, `${at}: Clear is not its own target`).toBe(true);
    expect([g.clearText, g.clearName, g.clearTip], at).toEqual(['Clear', 'Clear Stored Password', 'Clear Stored Password']);
    expect(g.applyOutside, `${at}: Apply is not in the field's action slot`).toBe(true);
    expect(g.input, `${at}: the password has no room to type in`).toBeGreaterThanOrEqual(100);
    if (!g.sideBySide) expect(Math.abs(g.widths[0] - g.widths[1]), `${at}: unequal groups ${g.widths}`).toBeLessThanOrEqual(1);
    if (width >= 1180) expect(g.sideBySide, `${at}: stacked while both fit`).toBe(true);
  }
});

test('nothing is written until Apply; Apply sends ONE request with both, refuses an incomplete pair, and keeps a typed draft through reflow', async ({page}) => {
  const writes = [];
  let release;
  await page.route('**/api/auth/config', async route => {
    if (route.request().method() !== 'PUT') return route.continue();
    writes.push(route.request().postDataJSON());
    await new Promise(resolve => { release = resolve; });
    const current = await (await page.request.get('/api/auth/config')).json();
    return route.fulfill({status: 200, contentType: 'application/json', headers: {'Cache-Control': 'no-store'},
      body: JSON.stringify({...current, ok: true, username: 'dp-synthetic-operator', password_configured: true})});
  });
  await open(page, 1440, 'authentication');
  const username = page.locator('#dp-settings-field-auth-username'), password = page.locator('#dp-settings-field-auth-password');
  const apply = page.locator('[data-action="apply-credentials"]');
  const status = page.locator('.dp-settings-username-password-card .dp-settings-provider-config-status');
  await expect(status).toHaveText('Unconfigured');
  await expect(apply).toBeDisabled();

  // Typing, leaving, Enter and a reflow write nothing.
  await username.fill('dp-synthetic-operator');
  await username.press('Enter');
  await username.blur();
  await resize(page, 900);
  await resize(page, 1440);
  await page.waitForTimeout(400);
  expect(writes, 'a credential was written without Apply').toEqual([]);
  // A username alone is not a credential: Apply refuses it, and says why.
  await expect(apply).toBeDisabled();
  await expect(apply).toHaveAttribute('data-tooltip', 'Enter a new password to change the username.');
  // A dirty draft never changes the status.
  await expect(status).toHaveText('Unconfigured');

  await password.fill(`${SENTINEL}-pw`);
  await expect(apply).toBeEnabled();
  await resize(page, 780);
  await expect(username).toHaveValue('dp-synthetic-operator');
  await expect(password).toHaveValue(`${SENTINEL}-pw`);
  await resize(page, 1440);
  // Twice, while the first is in flight: one request.
  await apply.click();
  await apply.click({force: true});
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toEqual({auth_username: 'dp-synthetic-operator', auth_password: `${SENTINEL}-pw`});
  await expect(apply).toBeDisabled();
  release();
  await expect(password).toHaveValue('');
  await expect(username).toHaveValue('dp-synthetic-operator');
  expect(writes.length).toBe(1);
  // The secret is nowhere in the page's markup.
  expect(await page.evaluate(() => document.documentElement.outerHTML)).not.toContain(SENTINEL);
});

test('Clear Stored Password: cancelling writes nothing and never applies a typed draft', async ({page}) => {
  const writes = [];
  await page.route('**/api/auth/config', route => {
    if (route.request().method() === 'PUT') writes.push(route.request().postDataJSON());
    return route.request().method() === 'PUT'
      ? route.fulfill({status: 500, contentType: 'application/json', body: '{"detail":"not reached"}'}) : route.continue();
  });
  await page.route('**/api/auth/config', async route => {
    if (route.request().method() !== 'GET') return route.fallback();
    const current = await (await route.fetch()).json();
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({...current, password_configured: true, username: 'dp-synthetic-operator'})});
  });
  await open(page, 1440, 'authentication');
  await page.locator('#dp-settings-field-auth-password').fill(`${SENTINEL}-draft`);
  const clear = page.locator('[data-action="clear-password"]');
  await expect(clear).toBeEnabled();
  await clear.click();
  const dialog = page.locator('[role="alertdialog"]');
  await expect(dialog).toContainText('Clear stored password?');
  await dialog.locator('[data-modal-cancel]').click();
  await expect(dialog).toHaveCount(0);
  await page.waitForTimeout(300);
  expect(writes).toEqual([]);
  await expect(page.locator('#dp-settings-field-auth-password')).toHaveValue(`${SENTINEL}-draft`);
});

test('OIDC islands: one row while every control stays useful, else a centred island hugging one field per row, controls one bounded width and edge; never a partial wrap', async ({page}) => {
  await page.route('**/api/auth/config', async route => {
    if (route.request().method() !== 'GET') return route.fallback();
    const current = await (await route.fetch()).json();
    return route.fulfill({json: {...current, public_base_url: 'https://debridpulse.example.net',
      public_base_url_effective: 'https://debridpulse.example.net', oidc_provider_name: 'Authentik',
      oidc_issuer_url: 'https://id.example.net/application/o/debridpulse/',
      // A generated client ID is long: forty characters, as Authentik issues them.
      oidc_client_id: 'Hk3pQ9sXv2LmT8wZr4NbYc6JdF1gUe7AoK5iRt0S', oidc_client_secret_configured: true}});
  });
  await open(page, 1440, 'authentication');
  // Each group stacks below its own threshold of the card body's content width.
  const STACK = {identity: 1176, credentials: 1168, protocol: 846};
  const FIELDS = {identity: ['oidc_provider_name', 'public_base_url', 'oidc_callback_url'],
    credentials: ['oidc_client_id', 'oidc_client_secret'], protocol: ['oidc_scopes', 'oidc_group_claim']};
  const measure = () => page.evaluate(FIELDS => {
    const body = document.querySelector('.dp-settings-oidc-grouped-card > .card-body');
    const bs = getComputedStyle(body), b = body.getBoundingClientRect();
    const left = b.left + parseFloat(bs.borderLeftWidth) + parseFloat(bs.paddingLeft);
    const right = b.right - parseFloat(bs.borderRightWidth) - parseFloat(bs.paddingRight);
    const out = {body: right - left, centre: (left + right) / 2,
      overflow: body.scrollWidth - body.clientWidth, pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      issuer: document.querySelector('.dp-settings-oidc-issuer-band').getBoundingClientRect().width, groups: {}};
    for (const [key, settings] of Object.entries(FIELDS)) {
      const group = document.querySelector(`.dp-settings-oidc-group--${key}`);
      const gs = getComputedStyle(group), g = group.getBoundingClientRect();
      const fields = [...group.querySelectorAll(':scope > .dp-settings-inline-field')].map(field => {
        const f = field.getBoundingClientRect(), info = field.querySelector('.dp-settings-inline-field-info').getBoundingClientRect();
        const control = field.querySelector('.dp-settings-inline-field-control').getBoundingClientRect();
        const input = field.querySelector('input'), is = getComputedStyle(input), ir = input.getBoundingClientRect();
        const action = field.querySelector('.dp-action-field .btn');
        return {setting: input.dataset.setting, top: f.top, height: f.height, spare: f.right - control.right,
          content: Math.max(info.height, control.height), infoLeft: info.left, infoWidth: info.width,
          left: control.left, right: control.right, width: control.width,
          // Typing room: the input's own text box, beside its embedded action.
          room: (action ? Math.min(ir.right, action.getBoundingClientRect().left) : ir.right) - ir.left
            - parseFloat(is.paddingLeft) - parseFloat(is.borderLeftWidth),
          whole: input.scrollWidth <= input.clientWidth,
          inside: f.left >= g.left && f.right <= g.right && f.bottom <= g.bottom};
      });
      out.groups[key] = {fields, width: g.width, offset: (g.left + g.right) / 2 - out.centre,
        border: `${gs.borderTopWidth} ${gs.borderTopStyle} ${gs.borderTopLeftRadius}`, overflow: group.scrollWidth - group.clientWidth};
    }
    return out;
  }, FIELDS);
  // A row only while every control stays useful (font-independent bounds).
  const ROW_USEFUL = {
    identity: fields => fields[1].width >= 199.5 && fields.every(f => f.width >= 144.5),
    credentials: fields => fields[0].whole && fields[1].room >= 279.5,
    protocol: fields => fields.every(f => f.width >= 159.5),
  };
  const states = {};
  for (const width of [1920, 1560, 1440, 1360, 1359, 1352, 1351, 1030, 1029, 780, 1440]) {
    await resize(page, width);
    const m = await measure(), at = `${width}px (body ${m.body}px)`;
    expect(m.overflow, `${at}: the card body overflows`).toBeLessThanOrEqual(0);
    expect(m.pageOverflow, `${at}: the page scrolls sideways`).toBeLessThanOrEqual(0);
    for (const [key, g] of Object.entries(m.groups)) {
      const where = `${at} ${key}`;
      expect(g.fields.map(f => f.setting), where).toEqual(FIELDS[key]);
      expect(g.border, `${where}: border`).toBe('1px solid 12px');
      expect(g.overflow, `${where}: overflow`).toBeLessThanOrEqual(0);
      expect(g.fields.every(f => f.inside), `${where}: a field leaves its island`).toBe(true);
      expect(Math.abs(g.offset), `${where}: the island is off the card's centre`).toBeLessThanOrEqual(0.5);
      const rows = new Set(g.fields.map(f => Math.round(f.top))).size;
      const stacked = m.body < STACK[key];
      states[`${key}@${width}`] = stacked;
      if (stacked) {
        // One field per row, each as tall as its content; one description
        // column; controls one bounded width and edge, the island hugging them.
        expect(rows, `${where}: partial wrap`).toBe(g.fields.length);
        expect(g.fields.every(f => f.height <= f.content + 1), `${where}: a row is taller than its content`).toBe(true);
        for (const edge of ['infoLeft', 'infoWidth', 'left', 'right', 'width']) {
          const values = g.fields.map(f => f[edge]);
          expect(Math.max(...values) - Math.min(...values), `${where}: ${edge}`).toBeLessThanOrEqual(0.5);
        }
        expect(g.fields.every(f => f.spare <= 0.5), `${where}: the island is wider than its rows`).toBe(true);
        const control = g.fields[0].width;
        expect(control, `${where}: stacked control`).toBeGreaterThanOrEqual(300);
        expect(control, `${where}: stacked control grows past its reading room`).toBeLessThanOrEqual(420.5);
        // Below its reading room only where the card itself is the limit.
        if (control < 419.5) expect(g.width, `${where}: controls yield with room to spare`).toBeGreaterThanOrEqual(m.body - 16 - 0.5);
      } else {
        expect(rows, `${where}: not one row`).toBe(1);
        expect(ROW_USEFUL[key](g.fields), `${where}: a row control is no longer useful`).toBe(true);
      }
    }
    if (width === 1440) states.issuer = [...(states.issuer || []), m.issuer];
  }
  // Each threshold is crossed between its two neighbouring widths, and the
  // wide arrangement returns intact.
  expect(states).toMatchObject({'identity@1360': false, 'identity@1359': true, 'credentials@1352': false, 'credentials@1351': true,
    'protocol@1030': false, 'protocol@1029': true, 'identity@780': true, 'credentials@780': true, 'protocol@780': true,
    'identity@1560': false, 'credentials@1560': false, 'protocol@1560': false,
    'identity@1920': false, 'credentials@1920': false, 'protocol@1920': false});
  expect(states.issuer[1]).toBe(states.issuer[0]);

  // Stacked at the floor, the derived Callback URL keeps its whole value and its Copy.
  await resize(page, 780);
  const callback = page.locator('[data-setting="oidc_callback_url"]');
  await expect(callback).toHaveJSProperty('readOnly', true);
  await expect(callback).toHaveValue(/^https:\/\/debridpulse\.example\.net\/.+callback$/);
  const copy = page.locator('[data-action="copy-oidc-callback"]');
  await expect(copy).toBeEnabled();
  await expect(copy).toHaveAttribute('aria-label', 'Copy OIDC Callback URL');
  expect(await copy.evaluate(button => {
    button.scrollIntoView({block: 'center'});
    const r = button.getBoundingClientRect(), field = button.closest('.dp-action-field').getBoundingClientRect();
    return button.contains(document.elementFromPoint((r.left + r.right) / 2, (r.top + r.bottom) / 2))
      && r.left >= field.left && r.right <= field.right + 0.5;
  }), 'Copy is not reachable inside the Callback field').toBe(true);
});

test('OIDC Client Secret: Clear is inside the field at every width, named Clear Stored Secret, and keeps its confirmation', async ({page}) => {
  const writes = [];
  await page.route('**/api/auth/config', async route => {
    if (route.request().method() === 'PUT') {
      writes.push(route.request().postDataJSON());
      const current = await (await page.request.get('/api/auth/config')).json();
      return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({...current, ok: true, oidc_client_secret_configured: false})});
    }
    const current = await (await route.fetch()).json();
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({...current, oidc_client_secret_configured: true})});
  });
  await open(page, 1440, 'authentication');
  const secret = page.locator('.dp-settings-oidc-client-secret-field');
  for (const width of [1440, 1180, 1024, 900, 780, 700]) {
    await resize(page, width);
    const g = await secret.evaluate(field => {
      const r = n => n.getBoundingClientRect();
      const clears = document.querySelectorAll('[data-action="clear-oidc-secret"]');
      const clear = clears[0]; clear.scrollIntoView({block: 'center'});
      const box = field.querySelector('.dp-action-field'), input = field.querySelector('input[type="password"]');
      const cb = r(clear), bb = r(box), hit = document.elementFromPoint((cb.left + cb.right) / 2, (cb.top + cb.bottom) / 2);
      return {clears: clears.length, inField: !!box && box.contains(clear) && cb.left >= r(input).right - 0.5 && cb.right <= bb.right + 0.5
        && Math.abs((cb.top + cb.bottom) / 2 - (bb.top + bb.bottom) / 2) <= 1, hit: clear.contains(hit), input: r(input).width,
        text: clear.textContent.trim(), name: clear.getAttribute('aria-label'), tip: clear.dataset.tooltip, masked: input.type,
        overflow: field.scrollWidth - field.clientWidth};
    });
    const at = `${width}px`;
    expect(g.clears, at).toBe(1);
    expect(g.inField, `${at}: Clear is not inside the Client Secret field`).toBe(true);
    expect(g.hit, `${at}: Clear is not its own target`).toBe(true);
    expect(g.input, `${at}: no room to type`).toBeGreaterThanOrEqual(100);
    expect([g.text, g.name, g.tip, g.masked], at).toEqual(['Clear', 'Clear Stored Secret', 'Clear Stored Secret', 'password']);
    expect(g.overflow, `${at}: overflow`).toBeLessThanOrEqual(0);
  }
  await resize(page, 1440);
  const clear = page.locator('[data-action="clear-oidc-secret"]');
  await expect(clear).toBeEnabled();
  // Keyboard reaches it and opens the same confirmation; Cancel writes nothing.
  await clear.focus();
  await page.keyboard.press('Enter');
  const dialog = page.locator('[role="alertdialog"]');
  await expect(dialog).toContainText('Clear stored client secret?');
  await dialog.locator('[data-modal-cancel]').click();
  await expect(dialog).toHaveCount(0);
  await page.waitForTimeout(300);
  expect(writes).toEqual([]);
  // Confirming is the one clear path, carrying only its own removal.
  await clear.click();
  await dialog.locator('[data-modal-accept]').click();
  await expect.poll(() => writes).toEqual([{clear_oidc_client_secret: true}]);
  await expect(clear).toBeDisabled();
});

test('a real Apply against this runtime configures the credential, and the status says Configured -- never Verified', async ({page}) => {
  await open(page, 1440, 'authentication');
  const before = await (await page.request.get('/api/auth/config')).json();
  test.skip(before.password_configured, 'this runtime already holds a local credential');
  const status = page.locator('.dp-settings-username-password-card .dp-settings-provider-config-status');
  await expect(status).toHaveText('Unconfigured');
  await page.locator('#dp-settings-field-auth-username').fill('dp-synthetic-operator');
  await page.locator('#dp-settings-field-auth-password').fill(`${SENTINEL}-real`);
  await page.locator('[data-action="apply-credentials"]').click();
  await expect(status).toHaveText('Configured');
  const after = await (await page.request.get('/api/auth/config')).json();
  expect([after.username, after.password_configured, after.password_enabled]).toEqual(['dp-synthetic-operator', true, before.password_enabled]);
  await expect(page.locator('#dp-settings-field-auth-password')).toHaveValue('');
  // A rename without a password is refused by the server too.
  const refused = await page.request.put('/api/auth/config', {data: {auth_username: 'dp-synthetic-renamed'}});
  expect(refused.status()).toBe(400);
  expect((await (await page.request.get('/api/auth/config')).json()).username).toBe('dp-synthetic-operator');
});

/* Every governing header of a section, as drawn. */
const headers = (page, panel) => page.evaluate(panel => [...document.querySelectorAll(`#view-settings [data-panel="${panel}"] .card > .card-header`)].map(h => {
  const st = getComputedStyle(h), hb = h.getBoundingClientRect();
  const left = hb.left + h.clientLeft + parseFloat(st.paddingLeft), right = hb.left + h.clientLeft + h.clientWidth - parseFloat(st.paddingRight);
  const identity = h.querySelector(':scope > .dp-settings-card-title-group').getBoundingClientRect();
  const rail = h.querySelector(':scope > .dp-settings-card-header-controls'), rb = rail.getBoundingClientRect();
  const hint = h.querySelector(':scope > .dp-settings-card-header-center > :only-child');
  const shown = hint.getClientRects().length > 0, c = shown ? hint.getBoundingClientRect() : null;
  const chip = rail.querySelector('.dp-settings-help-chip');
  return {name: h.querySelector('.dp-settings-card-title-text').textContent.trim(), hinted: h.classList.contains('dp-settings-card-header--hinted'),
    height: hb.height, minHeight: parseFloat(st.minHeight), hint: hint.textContent.trim(), shown,
    lines: shown ? Math.round(c.height / parseFloat(getComputedStyle(hint).lineHeight)) : 0,
    offset: shown ? (c.left + c.right) / 2 - (left + right) / 2 : null, clear: shown ? Math.min(c.left - identity.right, rb.left - c.right) : null,
    gap: parseFloat(st.columnGap), titleInset: identity.left - left, railInset: right - rb.right,
    chips: h.querySelectorAll('.dp-settings-help-chip').length, topic: chip.dataset.settingsHelp, label: chip.getAttribute('aria-label'), tooltip: chip.dataset.tooltip,
    rail: [...rail.children].map(n => n.matches('.dp-settings-help-chip') ? 'help' : n.matches('.dp-settings-provider-config-status') ? 'status' : 'control')};
}), panel);

function problems(list, width) {
  const out = [];
  for (const h of list) {
    const at = `${h.name} at ${width}px`;
    if (!h.hinted) out.push(`${at}: not hinted`);
    if (h.chips !== 1 || h.label !== `Help: ${h.name}`) out.push(`${at}: help ${h.chips} ${h.label}`);
    if (Math.abs(h.titleInset) > 0.5 || Math.abs(h.railInset) > 0.5) out.push(`${at}: insets ${h.titleInset} ${h.railInset}`);
    if (h.rail.indexOf('help') !== h.rail.indexOf('status') + 1 && h.rail.includes('status')) out.push(`${at}: rail ${h.rail}`);
    if (width >= 780 && Math.abs(h.height - h.minHeight) > 0.5) out.push(`${at}: not one row (${h.height})`);
    if (h.shown) {
      if (width < 780) out.push(`${at}: hint shown on the handheld header`);
      if (h.lines !== 1) out.push(`${at}: hint ${h.lines} lines`);
      if (Math.abs(h.offset) > 0.5) out.push(`${at}: hint off the header's midpoint (${h.offset.toFixed(1)})`);
      if (h.clear < h.gap - 0.5) out.push(`${at}: hint not clear of identity and rail`);
      if (h.tooltip !== h.label) out.push(`${at}: tooltip ${h.tooltip}`);
    } else if (h.tooltip !== h.hint) {
      out.push(`${at}: the hidden hint is not Help's tooltip (${h.tooltip})`);
    }
  }
  return out;
}

const INVENTORY = {
  authentication: ['Authentication Status', 'Username & Password', 'OpenID Connect'],
  users: ['User Accounts', 'Integration Accounts', 'Integration Delivery Profiles'],
  notifications: ['Discord Notifications', 'Statistics Reporting'],
  maintenance: ['Event Logging', 'Backups & Retention', 'Database Reset Controls'],
};

test('every governing header of Authentication, Users & Access, Notifications and Data & Maintenance keeps the four header rules', async ({page}) => {
  await open(page, 1440, 'authentication');
  const seen = {};
  for (const width of [1440, 1180, 1024, 900, 780, 700]) {
    await resize(page, width);
    for (const [panel, names] of Object.entries(INVENTORY)) {
      await section(page, panel);
      if (panel === 'users') await expect(page.locator('[data-users-mount] .card')).toHaveCount(3);
      const list = await headers(page, panel);
      expect(list.map(h => h.name), `${panel} inventory`).toEqual(names);
      expect(problems(list, width)).toEqual([]);
      for (const h of list) (seen[h.name] ||= new Set()).add(h.shown);
    }
  }
  // Every hint was both shown and handed off somewhere in the sweep.
  for (const [name, states] of Object.entries(seen)) expect([...states].sort(), name).toEqual([false, true]);
  // Username & Password's rail: Status, Help, Enable.
  await section(page, 'authentication');
  expect((await headers(page, 'authentication'))[1].rail).toEqual(['status', 'help', 'control']);
  // Help still opens the card's own read-only topic, by keyboard.
  for (const [panel, card, title] of [['authentication', 'username-password', 'Username & Password'], ['users', 'user-accounts', 'User Accounts'],
    ['maintenance', 'database-reset', 'Database Reset Controls']]) {
    await section(page, panel);
    await page.locator(`[data-settings-help="${card}"]`).focus();
    await page.keyboard.press('Enter');
    const dialog = page.locator('.dp-modal-dialog.dp-settings-help-dialog');
    await expect(dialog.locator('.dp-modal-title')).toHaveText(title);
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
  }
});

/* ── The account-scoped User Notifications page, in a REAL User session ── */
const authApi = (page, method, path, body) => page.evaluate(async ([method, path, body]) => {
  const response = await window.debridPulseAuth.fetch(`/api${path}`, {
    method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  return {status: response.status, body: await response.json().catch(() => null)};
}, [method, path, body]);

async function signIn(browser, {username, password}, width = 1440) {
  const context = await browser.newContext({baseURL: AUTH, viewport: {width, height: 1000}});
  const page = await context.newPage();
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/login');
  await page.locator('#username').fill(username);
  await page.locator('#password').fill(password);
  await page.locator('button[type="submit"]').click();
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.locator('#sidebar-auth-row .dp-sidebar-profile')).toBeVisible();
  return {context, page};
}

test('a real non-admin User keeps their own Notifications: the same two cards, hinted headers, only their own Help and controls', async ({browser}) => {
  test.skip(!AUTH, 'DP_AUTH_BASE_URL (the password-auth runtime) is required for a real User session');
  const admin = await signIn(browser, ADMIN);
  const run = Date.now().toString(36);
  const user = {username: `batch2-user-${run}`, password: `${SENTINEL}-${run}`};
  const created = await authApi(admin.page, 'POST', '/accounts', {kind: 'local', role: 'user', display_name: `Batch 2 ${run}`, ...user});
  expect(created.status).toBe(200);
  await admin.context.close();

  const {context, page} = await signIn(browser, user);
  try {
    expect(await page.evaluate(() => window.debridPulseAuthority?.role)).toBe('user');
    await page.locator('#sidebar .nav-item', {hasText: 'Notifications'}).click();
    await expect(page.locator('#page-title')).toHaveText('Notifications');
    const view = page.locator('#view-settings');
    await expect(view.locator('.dp-settings-discord-card')).toBeVisible();
    await expect(view.locator('.dp-settings-statistics-reporting-card')).toBeVisible();
    // Only the User's own controls: no Admin-only control reaches this page.
    for (const adminOnly of ['[data-setting="discord_notify_update"]', '[data-setting="update_check_interval_hours"]',
      '[data-action="upload-avatar"]', '.dp-settings-tabs']) {
      await expect(view.locator(adminOnly)).toHaveCount(0);
    }
    for (const width of [1440, 1024, 900, 780]) {
      await page.setViewportSize({width, height: 1000});
      await page.waitForFunction(() => document.getElementById('sidebar').getAnimations().every(a => a.playState !== 'running'));
      await settle(page);
      const list = await headers(page, 'notifications');
      expect(list.map(h => [h.name, h.topic])).toEqual([['Discord Notifications', 'discord-own'], ['Statistics Reporting', 'statistics-reporting-own']]);
      expect(problems(list, width)).toEqual([]);
    }
    // The User's Help is their own scope's, and says nothing of Admin-only controls.
    for (const [topic, title, own] of [['discord-own', 'Discord Notifications', 'Your own Discord alerts'],
      ['statistics-reporting-own', 'Statistics Reporting', 'activity attributed to you']]) {
      await page.locator(`[data-settings-help="${topic}"]`).click();
      const dialog = page.locator('.dp-modal-dialog.dp-settings-help-dialog');
      await expect(dialog.locator('.dp-modal-title')).toHaveText(title);
      await expect(dialog).toContainText(own);
      const text = (await dialog.innerText()).toLowerCase();
      for (const adminOnly of ['avatar', 'update check', 'new version', 'every account']) expect(text, topic).not.toContain(adminOnly);
      await page.keyboard.press('Escape');
      await expect(dialog).toHaveCount(0);
    }
    // An authorized control still operates: the User's Enable commits for the User.
    const toggle = view.locator('.dp-settings-discord-card > .card-header [data-setting="discord_notifications_enabled"]');
    const was = await toggle.isChecked();
    await view.locator('.dp-settings-discord-card > .card-header .ttrack').click();
    await expect.poll(async () => (await authApi(page, 'GET', '/account/notifications')).body?.discord_notifications_enabled).toBe(!was);
    await view.locator('.dp-settings-discord-card > .card-header .ttrack').click();
    await expect.poll(async () => (await authApi(page, 'GET', '/account/notifications')).body?.discord_notifications_enabled).toBe(was);
    expect(await page.evaluate(() => document.documentElement.outerHTML)).not.toContain(SENTINEL);
  } finally {
    await context.close();
  }
});
