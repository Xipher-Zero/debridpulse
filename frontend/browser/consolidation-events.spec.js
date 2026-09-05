const { test, expect } = require('@playwright/test');

async function installEventSourceFixture(page) {
  await page.addInitScript(() => {
    class FakeEventSource {
      constructor(url) {
        this.url = url;
        this.listeners = new Map();
        window.__dpFakeEventSource = this;
      }

      addEventListener(type, handler) {
        const handlers = this.listeners.get(type) || [];
        handlers.push(handler);
        this.listeners.set(type, handlers);
      }

      close() {}

      emit(type, payload) {
        for (const handler of this.listeners.get(type) || []) {
          handler({data: JSON.stringify(payload)});
        }
      }
    }
    FakeEventSource.CONNECTING = 0;
    FakeEventSource.OPEN = 1;
    FakeEventSource.CLOSED = 2;
    window.EventSource = FakeEventSource;
  });
}

test('consolidation copy is exact for complete one-target, multi-target, singular, and partial summaries', async ({ page }) => {
  await page.goto('/');
  const copies = await page.evaluate(() => ({
    one: window.DPIcons.consolidationToastCopy({
      source_transfer_id: 8,
      canonical_transfer_ids: [1],
      matched_count: 7,
      unmatched_count: 0,
    }),
    multi: window.DPIcons.consolidationToastCopy({
      source_transfer_id: 8,
      canonical_transfer_ids: [1, 2],
      matched_count: 7,
      unmatched_count: 0,
    }),
    singular: window.DPIcons.consolidationToastCopy({
      source_transfer_id: 8,
      canonical_transfer_ids: [1],
      matched_count: 1,
      unmatched_count: 0,
    }),
    partial: window.DPIcons.consolidationToastCopy({
      source_transfer_id: 8,
      canonical_transfer_ids: [1, 2],
      matched_count: 5,
      unmatched_count: 2,
    }),
  }));

  expect(copies.one).toEqual({
    title: 'Duplicate download consolidated',
    body: '7 matching files were merged into the existing download. The new source links were retained as failover candidates.',
  });
  expect(copies.multi).toEqual({
    title: 'Duplicate downloads consolidated',
    body: '7 matching files were merged into existing downloads. The new source links were retained as failover candidates.',
  });
  expect(copies.multi.body).not.toContain('the existing download');
  expect(copies.singular).toEqual({
    title: 'Duplicate download consolidated',
    body: '1 matching file was merged into the existing download. The new source link was retained as a failover candidate.',
  });
  expect(copies.partial).toEqual({
    title: 'Duplicate files consolidated',
    body: '5 matching files were merged into existing downloads and retained as failover candidates. 2 new files will download normally.',
  });
});

test('semantic SSE event produces one structured toast and reload does not replay it client-side', async ({ page }) => {
  await installEventSourceFixture(page);
  await page.goto('/');

  await page.evaluate(() => window.__dpFakeEventSource.emit('duplicate_consolidated', {
    source_transfer_id: 8,
    canonical_transfer_ids: [1],
    matched_count: 7,
    unmatched_count: 0,
  }));

  await expect(page.locator('.dp-toast-title')).toHaveText('Duplicate download consolidated');
  await expect(page.locator('.dp-toast-body')).toHaveText(
    '7 matching files were merged into the existing download. The new source links were retained as failover candidates.'
  );
  await expect(page.locator('.dp-toast-title')).toHaveCount(1);

  await page.reload();
  await expect(page.locator('.dp-toast-title')).toHaveCount(0);
});

test('structured toast renders content as text rather than HTML', async ({ page }) => {
  await page.goto('/');
  await page.evaluate(() => window.DPIcons.toast({
    title: 'Duplicate download consolidated',
    body: '<img src=x onerror="window.__toastInjected = true">',
  }, 'success'));

  await expect(page.locator('.dp-toast-body')).toHaveText('<img src=x onerror="window.__toastInjected = true">');
  expect(await page.evaluate(() => window.__toastInjected === true)).toBe(false);
  await expect(page.locator('.dp-toast-body img')).toHaveCount(0);
});
