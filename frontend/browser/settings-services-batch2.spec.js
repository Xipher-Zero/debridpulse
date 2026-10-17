const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Sierra batch 2, Settings -> Services: the API rate ceiling a
 * refused value is told in plain words, the Maximum Active Torrents hint,
 * centred integer tunables, premium expiry dates in the viewer's date
 * preference, and the two-line Metalink title. A refused value writes
 * nothing, so this file owns no settings key. */

const PREF_KEY = 'debridpulse.downloads.date-presentation.v1';

async function openServices(page, {width = 1440, height = 1000, pref} = {}) {
  await page.setViewportSize({width, height});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  if (pref) await page.addInitScript(([key, value]) => localStorage.setItem(key, JSON.stringify(value)), [PREF_KEY, pref]);
  await page.goto('/');
  await page.waitForFunction(() => typeof nav === 'function');
  await page.evaluate(() => nav(document.querySelector('[data-view="settings"]')));
  await page.locator('#view-settings [data-tab="sources"]').click();
}

async function expand(page, provider) {
  const card = page.locator(`.dp-settings-provider-card--${provider}`);
  const disclosure = card.locator('.dp-settings-disclosure').first();
  // Pressed through the DOM: at 900px some header controls cover this button,
  // as they do on the baseline (reported separately).
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.evaluate(button => button.click());
  // Tunables sit in the card's Additional section.
  await card.locator('details.dp-settings-additional').evaluateAll(sections => sections.forEach(section => { section.open = true; }));
  return card;
}

for (const [provider, key, ceiling, text] of [
  ['torbox', 'torbox_rate_limit_per_minute', 300, 'TorBox API limit must be 300 or fewer requests per minute.'],
  ['realdebrid', 'realdebrid_rate_limit_per_minute', 250, 'Real-Debrid API limit must be 250 or fewer requests per minute.'],
]) {
  test(`a ${provider} API rate above its ceiling is refused in plain words and the field returns`, async ({ page }) => {
    await openServices(page);
    const card = await expand(page, provider);
    const field = card.locator(`[data-setting="${key}"]`);
    const before = await field.inputValue();
    // Above the control's own bound (the server's): refused in the field's
    // words before any request (Settings Batch 1, Item 3)...
    const sent = [];
    page.on('request', request => { if (request.url().includes(`/api/integrations/${provider}/configuration`)
      && request.method() === 'PATCH') sent.push(request.postData()); });
    await field.fill(String(ceiling + 200));
    await field.blur();
    // ...and the real server refuses the same value on its own, naming the
    // rule it broke.
    const response = await page.request.patch(`/api/integrations/${provider}/configuration`,
      {data: {options: {[key.replace(`${provider}_`, '')]: ceiling + 200}}});
    expect(response.status()).toBe(400);
    expect((await response.json()).detail.requirement).toBe(`must be ${ceiling} or less`);
    const toast = page.locator('.toast.error').last();
    await expect(toast).toHaveText(text);
    for (const raw of ['validation error', 'Options', key.replace(`${provider}_`, ''), 'input_value', 'http']) {
      await expect(toast).not.toContainText(raw);
    }
    // The existing rollback: the value the server holds is shown again.
    await expect(field).toHaveValue(before);
    expect(sent).toEqual([]);
  });
}

test('Maximum Active Torrents hints read Max, whole, on all three providers', async ({ page }) => {
  for (const width of [1440, 900]) {
    await openServices(page, {width});
    for (const provider of ['alldebrid', 'realdebrid', 'torbox']) {
      const card = await expand(page, provider);
      const field = card.locator(`[data-setting="${provider}_max_active_torrents"]`);
      await expect(field).toHaveAttribute('placeholder', 'Max');
      const fit = await field.evaluate(input => {
        const style = getComputedStyle(input);
        const context = document.createElement('canvas').getContext('2d');
        context.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
        const room = input.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
        return {text: context.measureText(input.placeholder).width, room};
      });
      expect(fit.text, `${provider} at ${width}`).toBeLessThan(fit.room);
      // The explanation stays beneath the field.
      await expect(field.locator('xpath=..')).toContainText('Leave empty to follow');
    }
  }
});

test('compact tunables centre their value whatever its type; paths, addresses and credentials do not', async ({ page }) => {
  await openServices(page);
  // A compact READING -- a count shown read-only in the selector footprint
  // beside the selector it accompanies (Event Logging's Recorded Events) -- is
  // a compact value like a tunable, never a path, address or credential.
  const readings = ['event_journal_recorded'];
  const fields = await page.evaluate(readings => [...document.querySelectorAll('#view-settings input.input')].map(input => ({
    key: input.dataset.setting || input.dataset.usenetField || input.id, type: input.type,
    tunable: input.type === 'number' || !!input.closest('.dp-settings-tuning-grid .dp-settings-field')
      || (input.readOnly && readings.includes(input.dataset.setting)),
    align: getComputedStyle(input).textAlign})), readings);
  const tunables = fields.filter(field => field.tunable);
  const others = fields.filter(field => !field.tunable);
  // Integers, fractions and sized strings alike, in Services, Downloads,
  // Maintenance and Usenet servers.
  for (const key of ['torbox_rate_limit_per_minute', 'alldebrid_max_active_torrents', 'aria2_split',
                     'aria2_max_active_downloads', 'backup_keep_days', 'webdav_max_files',
                     'min_free_disk_gb', 'disk_guard_resume_hysteresis_gb', 'aria2_min_split_size', 'aria2_disk_cache']) {
    expect(tunables.map(field => field.key), key).toContain(key);
  }
  readings.forEach(key => expect(tunables.map(field => field.key), key).toContain(key));
  expect(tunables.length).toBeGreaterThan(20);
  tunables.forEach(field => expect(field.align, field.key).toBe('center'));
  // Paths, addresses, names and credentials keep the ordinary alignment.
  for (const key of ['download_folder', 'backup_folder', 'alldebrid_api_key', 'oidc_issuer_url', 'discord_username']) {
    expect(others.map(field => field.key), key).toContain(key);
  }
  others.forEach(field => expect(field.align, field.key).not.toBe('center'));
  // Every selector's chosen value is centred beside its chevron.
  const values = await page.evaluate(() => [...document.querySelectorAll('#view-settings .dp-dropdown__value')]
    .map(value => getComputedStyle(value).textAlign));
  expect(values.length).toBeGreaterThan(5);
  values.forEach(align => expect(align).toBe('center'));
  // Editing is unchanged: a typed value stays a number in a centred field, and
  // a fractional field still steps by its own step.
  const field = page.locator('[data-setting="torbox_rate_limit_per_minute"]');
  await expand(page, 'torbox');
  await field.fill('123');
  await expect(field).toHaveValue('123');
  // Its stored value is the suite's neutral probe (other files write it), so
  // the step is proven from a value this case sets in the control itself and
  // puts back -- nothing is committed.
  const buffer = page.locator('[data-setting="disk_guard_resume_hysteresis_gb"]');
  expect(await buffer.evaluate(input => { const stored = input.value; input.value = '0.5'; const before = input.value;
    input.stepUp(); const after = input.value; input.value = stored; return [before, after]; })).toEqual(['0.5', '0.6']);
});

// 23:30 UTC on the last day of January is already February in Tokyo.
const UNTIL = '2027-01-31T23:30:00Z';
for (const [format, timezone, date] of [
  ['iso', 'UTC', '2027-01-31'], ['iso', 'Asia/Tokyo', '2027-02-01'], ['us', 'UTC', '01/31/2027'],
  ['international', 'Asia/Tokyo', '01/02/2027'], ['friendly', 'UTC', 'Jan 31, 2027'],
]) {
  test(`a premium expiry follows the ${format} preference in ${timezone}`, async ({ page }) => {
    await openServices(page, {pref: {format, hour12: false}});
    const text = await page.evaluate(([until, zone]) => {
      settingsData = {...(settingsData || {}), timezone: zone};
      const wording = window.DPPremiumAccount.describe(new Date(until), 'Pro');
      return wording;
    }, [UNTIL, timezone]);
    expect(text.until).toBe(`Pro until ${date}`);
    // Days remaining come from the instant, never from the formatted date.
    const days = await page.evaluate(until => Math.ceil((new Date(until) - Date.now()) / 86400000), UNTIL);
    expect(text.days).toBe(`(${days} days remaining)`);
  });
}

test('the Metalink box is titled Metalink over (Multimeta), like Media Downloads, at a narrow width too', async ({ page }) => {
  for (const width of [1440, 900]) {
    await openServices(page, {width});
    const group = page.locator('.dp-settings-general-sources');
    const disclosure = group.locator('.dp-settings-disclosure').first();
    if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
    for (const [selector, lines] of [['.dp-settings-provider-card--multimeta', ['Metalink', '(Multimeta)']],
                                     ['.dp-settings-provider-card--media', ['Media', 'Downloads']]]) {
      const box = page.locator(selector);
      const layout = await box.evaluate(node => {
        const title = node.querySelector('.card-title');
        const mark = node.querySelector('.dp-settings-protocol-chip').getBoundingClientRect();
        const block = title.getBoundingClientRect();
        const spans = [...title.querySelectorAll('.dp-settings-source-box-title-line')].map(span => {
          const rect = span.getBoundingClientRect();
          return {text: span.textContent, top: Math.round(rect.top), centre: rect.left + rect.width / 2};
        });
        return {spans, blockCentre: block.left + block.width / 2, blockMiddle: block.top + block.height / 2,
                markMiddle: mark.top + mark.height / 2, clear: block.left >= mark.right,
                overflow: node.scrollWidth - node.clientWidth, enable: node.querySelector('[data-integration-enabled]').getAttribute('aria-label')};
      });
      expect(layout.spans.map(span => span.text), `${selector} at ${width}`).toEqual(lines);
      expect(layout.spans[0].top).toBeLessThan(layout.spans[1].top);
      layout.spans.forEach(span => expect(Math.abs(span.centre - layout.blockCentre)).toBeLessThanOrEqual(1));
      expect(Math.abs(layout.blockMiddle - layout.markMiddle)).toBeLessThanOrEqual(2);
      expect(layout.clear).toBe(true);
      expect(layout.overflow).toBe(0);
    }
    await expect(page.locator('.dp-settings-provider-card--general-scp .card-title')).toHaveText('SCP');
    await expect(page.locator('.dp-settings-provider-card--general-scp .dp-settings-source-box-copy')).not.toContainText('SSH');
  }
});

test('every Request Timeout hint states what it is and its default', async ({ page }) => {
  await openServices(page);
  for (const provider of ['realdebrid', 'torbox', 'debridlink', 'premiumize']) {
    const card = await expand(page, provider);
    const field = card.locator(`[data-setting="${provider}_request_timeout_seconds"]`);
    await expect(field.locator('xpath=..').locator('.form-hint'))
      .toHaveText('Time allowed for this provider to respond to a request. Defaults to 30 seconds.');
    await expect(field).toHaveValue('30');
  }
});

/* A refused value, worded by its own field. Settings Batch 1, Item 3: a value
 * outside the control's own bound (the server's) is refused before any
 * request, in the server's own words; `serverRefuses` proves the server
 * refuses the same value directly. */
const NEVER = ['validation error', 'Options', 'Settings', 'input_value', 'type=', 'http', '[object Object]', '_seconds'];
async function refuse(page, card, key, value, serverRefuses) {
  const field = card.locator(`[data-setting="${key}"]`);
  const before = await field.inputValue();
  const sent = [];
  const watch = request => { if (/\/api\/(integrations\/\w+\/configuration|transfer-policy)$/.test(new URL(request.url()).pathname)
    && request.method() === 'PATCH') sent.push(request.postData()); };
  page.on('request', watch);
  await field.fill(String(value));
  await field.blur();
  const direct = await serverRefuses();
  expect(direct.status()).toBe(400);
  page.off('request', watch);
  expect(sent).toEqual([]);
  const toast = page.locator('.toast.error').last();
  await expect(toast).toBeVisible();
  for (const raw of NEVER) await expect(toast).not.toContainText(raw);
  await expect(field).toHaveValue(before);
  return toast;
}

test('a refused provider option and a refused transfer-policy value name their field and its bound', async ({ page }) => {
  await openServices(page);
  const torbox = await expand(page, 'torbox');
  await expect(await refuse(page, torbox, 'torbox_request_timeout_seconds', 1, () =>
    page.request.patch('/api/integrations/torbox/configuration', {data: {options: {request_timeout_seconds: 1}}})))
    .toHaveText('Request Timeout (seconds) must be 5 or more.');
  const alldebrid = await expand(page, 'alldebrid');
  await expect(await refuse(page, alldebrid, 'poll_interval_seconds', 99999, () =>
    page.request.patch('/api/transfer-policy', {data: {provider_poll_interval_seconds: 99999}})))
    .toHaveText('Provider Poll Interval (seconds) must be 3600 or less.');
});

test('a refused request body is read from its first value, not as Unprocessable Entity', async ({ page }) => {
  await openServices(page);
  const error = await page.evaluate(async () => {
    try { await api('PATCH', '/transfer-policy', {execution_retry_count: 'three'}); return null; }
    catch (caught) { return {message: caught.message, status: caught.status}; }
  });
  expect(error).toEqual({message: 'Value must be a whole number.', status: 422});
});

test('a refused Usenet server field is named by the card', async ({ page }) => {
  await openServices(page);
  // The real server's refusal of a new server's port above 65535 (nothing is created).
  const refusal = await page.evaluate(async () => {
    try { await api('POST', '/usenet/servers', {host: 'news.example', port: 70000}); return null; }
    catch (error) { return error.detail[0]; }
  });
  expect(refusal).toEqual({type: 'less_than_equal', loc: ['body', 'port'], msg: 'Value must be 65535 or less.',
                           requirement: 'must be 65535 or less'});
  const message = await page.evaluate(detail => {
    const card = document.createElement('div');
    card.innerHTML = '<label for="probe-port">Port</label><input id="probe-port" data-usenet-field="port">';
    document.body.append(card);
    const error = Object.assign(new Error(detail.msg), {detail: [detail], status: 422});
    const text = window.DPSettingsPersistence.refusalMessage(error, card.querySelector('[data-usenet-field="port"]'));
    card.remove();
    return text;
  }, refusal);
  expect(message).toBe('Port must be 65535 or less.');
});

for (const [name, reply, expected, saved] of [
  ['saved but not yet applied', {status: 200, body: {ok: false, configured: {max_download_bytes_per_second: 1048576},
    effective: {max_download_bytes_per_second: null}, last_apply_error: 'aria2 RPC changeGlobalOption failed: ECONNREFUSED'}},
   'Speed limit saved, but running downloads could not take it yet.', false],
  ['refused', {status: 400, body: {detail: 'Speed limit must be a whole number.'}}, 'Speed limit must be a whole number.', false],
  ['accepted', {status: 200, body: {ok: true, configured: {max_download_bytes_per_second: 1048576},
    effective: {max_download_bytes_per_second: 1048576}, last_apply_error: null}}, null, true],
]) {
  test(`a speed limit that is ${name} says so plainly`, async ({ page }) => {
    await page.route(url => url.pathname === '/api/execution/runtime-limits', route => route.fulfill({
      status: reply.status, contentType: 'application/json', body: JSON.stringify(reply.body)}));
    await page.goto('/');
    await page.waitForFunction(() => typeof _setDownloadSpeedCap === 'function');
    expect(await page.evaluate(() => _setDownloadSpeedCap(1048576))).toBe(saved);
    if (expected) {
      const toast = page.locator('.toast.error').last();
      await expect(toast).toHaveText(expected);
      await expect(toast).not.toContainText('ECONNREFUSED');
    } else {
      await expect(page.locator('.toast.error')).toHaveCount(0);
    }
  });
}

for (const [name, reply, expected] of [
  ['an object detail', {status: 404, contentType: 'application/json', body: JSON.stringify({detail: {message: 'Transfer not found'}})}, 'Transfer not found'],
  ['a string detail', {status: 404, contentType: 'application/json', body: JSON.stringify({detail: 'Transfer not found'})}, 'Transfer not found'],
  ['no readable body', {status: 500, contentType: 'text/plain', body: 'Internal Server Error'}, 'The trace log could not be downloaded.'],
]) {
  test(`a trace download refused with ${name} reads as a sentence`, async ({ page }) => {
    await page.route(url => /\/api\/torrents\/\d+\/trace$/.test(url.pathname), route => route.fulfill(reply));
    await page.goto('/');
    await page.waitForFunction(() => typeof window.downloadTransferTrace === 'function');
    await page.evaluate(() => {
      const button = document.createElement('button');
      button.dataset.dpTransferId = '41';
      document.body.append(button);
      return window.downloadTransferTrace(button);
    });
    await expect(page.locator('.toast.error').last()).toHaveText(expected);
  });
}
