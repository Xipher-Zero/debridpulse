const { test, expect } = require('@playwright/test');

/* DP 1.0.13 -- Details' ERROR keeps its message and, when the Details route
 * discloses facts of that very error (error_disclosure), becomes a collapsed
 * native disclosure revealing them as text. The same path serves any origin. */

const detail = (id, extra) => ({
  id, name: `Transfer ${id}`, status: 'error', progress: 0, size_bytes: 0, source: 'direct_link',
  created_at: '2026-01-01T00:00:00Z', completed_at: null, hash: '', files: [], source_outcomes: [],
  events: [], executors: ['aria2'], route_attempts: [], execution_attempts: [], ...extra,
});
const REJECTED = detail(627, {
  error_message: 'Candidate rejected',
  error: {domain: 'provider', category: 'candidate_rejected', stage: 'candidate_preparation', origin: 'provider',
          integration_id: 'realdebrid', message: 'Candidate rejected'},
  error_disclosure: {integration_id: 'realdebrid', integration_name: 'Real-Debrid', origin: 'provider',
                     domain: 'provider', diagnostic: 'infringing_file', native_code: '35',
                     stage: 'candidate_preparation', permanence: 'permanent', retryability: 'never'},
});
const STORAGE = detail(41, {
  error_message: 'Download storage unavailable',
  error_disclosure: {origin: 'local_system', domain: 'local_resource', stage: 'execution',
                     permanence: 'temporary', retryability: 'backoff'},
});
const EXECUTOR = detail(42, {
  error_message: 'Download failed',
  error_disclosure: {integration_id: 'aria2', origin: 'executor', domain: 'executor', diagnostic: 'Resource not found',
                     native_code: '3', stage: 'execution'},
});
// No disclosure (no facts beyond the message, or not bound to it): the message alone.
const PLAIN = detail(43, {error_message: 'Candidate rejected'});
// Facts the renderer must never show, even were a response to carry them.
const CANARY = detail(44, {
  error_message: 'Candidate rejected',
  error: {category: 'candidate_rejected', context: {context_canary: 'CONTEXT-CANARY'},
          diagnostic_evidence: {files: [{name: 'EVIDENCE-CANARY.mkv'}]}},
  error_disclosure: {origin: 'provider', diagnostic: '<img src=x onerror="window.__xss=1">\nline two ٣‮EVIL',
                     native_code: 'x'.repeat(128), context: {context_canary: 'CONTEXT-CANARY'},
                     diagnostic_evidence: {name: 'EVIDENCE-CANARY'}},
});

async function boot(page) {
  const byId = new Map([REJECTED, STORAGE, EXECUTOR, PLAIN, CANARY].map(item => [String(item.id), item]));
  await page.route(url => /^\/api\/torrents\/\d+$/.test(url.pathname), route => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify(byId.get(new URL(route.request().url()).pathname.split('/').pop()))}));
  await page.route(url => url.pathname === '/api/torrents',
    route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items: [], total: 0})}));
  await page.goto('/');
  await page.waitForFunction(() => typeof showDetail === 'function');
}
const open = async (page, id) => {
  await page.evaluate(value => showDetail(value), id);
  await expect(page.locator('#modal-body .detail-grid')).toBeVisible();
};
const error = page => page.locator('.dp-detail-error .dv');
const facts = page => page.locator('.dp-detail-error-disclosure dl').evaluate(list =>
  [...list.querySelectorAll('dt')].map(term => [term.textContent, term.nextElementSibling.textContent]));

test('a provider rejection keeps its message, collapsed, and opens to its provider, reason and native code', async ({ page }) => {
  await boot(page);
  await open(page, 627);
  const summary = page.locator('.dp-detail-error-disclosure > summary');
  await expect(summary).toHaveText('Candidate rejected');
  await expect(page.locator('.dp-detail-error-disclosure')).not.toHaveAttribute('open', '');
  await expect(page.locator('.dp-detail-error-disclosure dl')).toBeHidden();
  const collapsed = await error(page).evaluate(node => node.getBoundingClientRect().height);
  // Keyboard: focus, Enter opens, Space closes, focus stays put.
  await summary.focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('.dp-detail-error-disclosure dl')).toBeVisible();
  expect(await facts(page)).toEqual([
    ['Reported by', 'Real-Debrid (realdebrid)'], ['Origin', 'provider'], ['Domain', 'provider'],
    ['Diagnostic', 'infringing_file'], ['Native code', '35'], ['Stage', 'candidate preparation'],
    ['Permanence', 'permanent'], ['Retry', 'never'],
  ]);
  await expect(summary).toBeFocused();
  await page.keyboard.press('Space');
  await expect(page.locator('.dp-detail-error-disclosure dl')).toBeHidden();
  expect(await error(page).evaluate(node => node.getBoundingClientRect().height)).toBe(collapsed);
  // The message keeps the error colour it always had.
  expect(await error(page).evaluate(node => node.style.color)).toBe('var(--red)');
});

test('the same disclosure serves an executor and a local storage error, naming no provider', async ({ page }) => {
  await boot(page);
  await open(page, 42);
  await page.locator('.dp-detail-error-disclosure > summary').click();
  expect(await facts(page)).toEqual([
    ['Reported by', 'aria2'], ['Origin', 'executor'], ['Domain', 'executor'], ['Diagnostic', 'Resource not found'],
    ['Native code', '3'], ['Stage', 'execution'],
  ]);
  await open(page, 41);
  // Opening another transfer starts collapsed, with only its own facts.
  await expect(page.locator('.dp-detail-error-disclosure')).not.toHaveAttribute('open', '');
  await page.locator('.dp-detail-error-disclosure > summary').click();
  expect(await facts(page)).toEqual([
    ['Origin', 'local system'], ['Domain', 'local resource'], ['Stage', 'execution'],
    ['Permanence', 'temporary'], ['Retry', 'backoff'],
  ]);
  await expect(page.locator('.dp-detail-error-disclosure dl')).not.toContainText('Reported by');
});

test('without disclosed facts the message stands alone, with no inert control', async ({ page }) => {
  await boot(page);
  await open(page, 627);
  await open(page, 43);
  await expect(error(page)).toHaveText('Candidate rejected');
  await expect(page.locator('#modal-body details.dp-detail-error-disclosure, #modal-body .dp-detail-error summary')).toHaveCount(0);
});

test('context and diagnostic evidence never reach the page; hostile text is inert and wraps', async ({ page }) => {
  await page.setViewportSize({width: 390, height: 844});
  await boot(page);
  await open(page, 44);
  await page.locator('.dp-detail-error-disclosure > summary').click();
  const html = await page.locator('#modal').evaluate(node => node.outerHTML);
  for (const canary of ['CONTEXT-CANARY', 'context_canary', 'EVIDENCE-CANARY', 'diagnostic_evidence']) {
    expect(html).not.toContain(canary);
  }
  // Markup in a reason is text: no element, no handler, no execution.
  expect(await page.evaluate(() => window.__xss)).toBeUndefined();
  await expect(page.locator('.dp-detail-error-disclosure img')).toHaveCount(0);
  await expect(page.locator('.dp-detail-error-disclosure dd').nth(1)).toContainText('<img src=x');
  // Long values wrap inside the dialog rather than overflowing it.
  const overflow = await page.locator('.dp-detail-error-disclosure dl').evaluate(list =>
    [...list.querySelectorAll('dd')].some(item => item.getBoundingClientRect().right > list.getBoundingClientRect().right + 1));
  expect(overflow).toBe(false);
});
