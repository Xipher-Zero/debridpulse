const { test, expect } = require('@playwright/test');

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

function observeRuntime(page) {
  const errors = [];
  page.on('pageerror', error => errors.push(`pageerror: ${error.message}`));
  page.on('console', message => { if (message.type() === 'error') errors.push(`console: ${message.text()}`); });
  return errors;
}

const states = [
  ['downloading', 'Downloading', 'downloading'],
  ['paused', 'Paused', 'paused'],
  ['waiting_for_retry', 'Waiting for retry', 'queued'],
  ['waiting_for_provider', 'Waiting for provider', 'pending'],
  ['waiting_for_storage', 'Waiting for storage', 'pending'],
  ['recovering', 'Recovering', 'processing'],
  ['input_required', 'Input Required', 'input_required'],
  ['requires_attention', 'Requires attention', 'error'],
];

function rawStatus(state) {
  if (state === 'downloading') return 'downloading';
  if (state === 'paused') return 'paused';
  if (state === 'input_required') return 'input_required';
  return 'error';
}

function item(id, state, label, badge) {
  return {
    id, name: `Phase4 ${state}`, status: rawStatus(state),
    presentation_status: state, presentation_label: label, presentation_badge_status: badge,
    attention_required: state === 'requires_attention', progress: 50, retained_bytes: 512,
    size_bytes: 1024, source: 'manual', hash: '', label: '', created_at: '2026-09-08T00:00:00Z',
    current_source_identity: {kind: 'link'}, providers: [], historical_providers: [], delivering_provider_ids: [],
    input_required: state === 'input_required' ? {
      id:'phase4-input', generation:1, reason:'auth_required', origin:'provider',
      methods:[{method:'username_password',fields:[{name:'username',required:true},{name:'password',required:true}]}],
    } : null,
  };
}

test('Phase-4 canonical recovery states render from backend truth with retained progress', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  const items = states.map(([state,label,badge], index) => item(940 + index, state, label, badge));
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({items, total: items.length}),
  }));
  await page.goto('/');
  await page.evaluate(async () => {
    nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]'));
    await loadTorrents();
  });
  for (let index = 0; index < states.length; index += 1) {
    const [state, label] = states[index];
    const row = page.locator(`#t-tbody tr[data-torrent-id="${940 + index}"]`);
    await expect(row).toBeVisible();
    await expect(row).toHaveAttribute('data-presentation-status', state);
    const status = row.locator('[data-role="transfer-status"]');
    await expect(status).toContainText(label);
    await expect(status.locator(`[data-dp-lifecycle-status="${state}"]`)).toHaveCount(1);
    await expect(row.locator('[data-role="transfer-progress"]')).toContainText('50');
    if (state !== 'requires_attention') {
      await expect(status.locator('.badge-error')).toHaveCount(0);
      await expect(row.locator('.dp-terminal-error-progress')).toHaveCount(0);
    }
    if (['recovering','waiting_for_retry','waiting_for_provider','waiting_for_storage'].includes(state)) {
      await expect(row.locator('button[data-default-label="Retry"]')).toHaveCount(0);
    }
  }
  expect(errors).toEqual([]);
});

test('Recent Activity keeps retained progress visible while retry is quiescent', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  const waiting = item(980, 'waiting_for_retry', 'Waiting for retry', 'queued');
  waiting.progress = 61;
  waiting.retained_bytes = 610;
  waiting.size_bytes = 1000;
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({items:[waiting],total:1}),
  }));
  await page.goto('/');
  await page.evaluate(async () => { await loadRecent(); });
  const row = page.locator('#dash-tbody tr[data-torrent-id="980"]');
  await expect(row.locator('[data-role="transfer-status"]')).toContainText('Waiting for retry');
  await expect(row.locator('[data-role="transfer-progress"]')).toContainText('61');
  await expect(row.locator('[data-role="transfer-progress"] .dp-terminal-error-progress')).toHaveCount(0);
  await expect(row.locator('.dash-row-bar')).not.toHaveClass(/\bis-empty\b/);
  await expect(row.locator('.dash-row-bar-fill')).toHaveAttribute('style', /width:61%/);
  expect(errors).toEqual([]);
});

test('Details uses canonical artifact state and does not turn recovery into attention', async ({ page }) => {
  await isolateExternalFonts(page);
  const errors = observeRuntime(page);
  const transfer = item(990, 'recovering', 'Recovering', 'processing');
  transfer.files = [{
    id: 1, filename: 'payload.bin', size_bytes: 1024, status: 'error', progress: 50,
    retained_bytes: 512, presentation_status: 'waiting_for_retry', presentation_label: 'Waiting for retry',
    presentation_badge_status: 'queued', attention_required: false, blocked: false, block_reason: null,
  }];
  transfer.route_attempts = []; transfer.execution_attempts = []; transfer.candidate_bindings = []; transfer.source_outcomes = []; transfer.events = [];
  await page.route(url => url.pathname === '/api/torrents/990', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(transfer),
  }));
  await page.goto('/');
  await page.evaluate(async () => { await showDetail(990); });
  await expect(page.locator('#modal-body')).toContainText('Recovering');
  await expect(page.locator('#modal-body')).toContainText('Waiting for retry');
  await expect(page.locator('#modal-body [data-dp-lifecycle-status="requires_attention"]')).toHaveCount(0);
  expect(errors).toEqual([]);
});
