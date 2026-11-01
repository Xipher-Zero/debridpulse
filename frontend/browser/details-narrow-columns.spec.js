const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Transfer Details keeps its two-column metadata grid at every
 * width: the two former max-width:779px one-column overrides are gone, and the
 * base `.detail-grid` (two tracks, min-width:0 cells, break-all values) fits
 * long names, URLs and paths from 320px without horizontal overflow. The
 * header (title, Trace, Close), the modal's own geometry and its body scroll
 * are the existing contract and do not change. */

const LONG = 'Rune.Borderlands.4.Deluxe.Edition.MULTi12.Repack.Build.20261031.With.All.DLCs.And.Bonus.Content.iso';
const at = '2026-10-31T12:00:00+00:00';
const TRANSFER = {
  id: 9731, name: LONG, display_name: LONG, status: 'downloading', presentation_status: 'downloading',
  progress: 41.5, size_bytes: 87361421312, source: 'direct_link', created_at: at, completed_at: null,
  hash: 'f'.repeat(40), local_path: `/download/${LONG}/disc-images/${LONG}`,
  original_resource: `https://example.invalid/d/AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/${LONG}`,
  files: [], source_outcomes: [], events: [], executors: ['aria2'], route_attempts: [], execution_attempts: [],
  error_message: 'Source temporarily unavailable: https://example.invalid/averyveryveryverylongunbrokenpathsegment0123456789',
  requested_by: {requests: [{name: 'Sonarr-Integration-With-A-Long-Name', kind: 'integration',
    token_label: 'sonarr-main-token', standing: null, via_transfer_id: null, requested_at: at,
    withdrawn_at: null, reference: null}]},
};

async function open(page, width) {
  await page.setViewportSize({width, height: 800});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route(url => /^\/api\/torrents\/9731$/.test(url.pathname),
    route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(TRANSFER)}));
  await page.goto('/');
  await page.waitForFunction(() => typeof showDetail === 'function' && typeof window.downloadsDateMarkup === 'function');
  await page.evaluate(() => showDetail(9731));
  await expect(page.locator('#modal-body .detail-grid')).toBeVisible();
}

const geometry = page => page.evaluate(() => {
  const box = node => node.getBoundingClientRect();
  const grid = document.querySelector('#modal-body .detail-grid');
  const modal = document.getElementById('modal');
  const header = box(document.querySelector('#modal .modal-hdr'));
  const trace = document.querySelector('#modal .dp-detail-trace');
  const close = box(document.querySelector('#modal .modal-close'));
  const cells = [...grid.children];
  return {
    tracks: getComputedStyle(grid).gridTemplateColumns.split(' ').length,
    pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    modalOverflow: modal.scrollWidth - modal.clientWidth,
    cellOverflow: cells.filter(cell => cell.scrollWidth > cell.clientWidth + 0.5).length,
    clippedLabels: cells.filter(cell => { const label = cell.querySelector(':scope > .dk'); return label && label.scrollWidth > label.clientWidth + 0.5; }).length,
    beside: cells.filter(cell => box(cell).width < box(grid).width * 0.75).length,
    modalInside: box(modal).left >= 0 && box(modal).right <= innerWidth,
    closeInHeader: close.top >= header.top && close.bottom <= header.bottom && close.right <= header.right,
    traceBeforeClose: !trace || box(trace).right <= close.left,
  };
});

for (const width of [320, 390, 520, 779, 780]) {
  test(`Details keeps two metadata columns without overflow at ${width}px`, async ({page}) => {
    await open(page, width);
    const facts = await geometry(page);
    expect(facts).toMatchObject({tracks: 2, pageOverflow: 0, modalOverflow: 0, cellOverflow: 0, clippedLabels: 0,
      modalInside: true, closeInHeader: true, traceBeforeClose: true});
    expect(facts.beside).toBeGreaterThanOrEqual(8);    // the paired facts really sit side by side
    // The body still scrolls to its last field.
    const end = await page.locator('#modal').evaluate(node => {
      node.scrollTop = node.scrollHeight;
      return node.scrollTop + node.clientHeight >= node.scrollHeight - 1;
    });
    expect(end).toBe(true);
  });
}
