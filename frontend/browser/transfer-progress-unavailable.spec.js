const { test, expect } = require('@playwright/test');

/* DP 1.0.13: a transfer whose total size is not yet known has NO percentage.
 * The one progress renderer shows the established unavailable glyph (and the
 * indeterminate rail while active) -- never a fabricated 0%. Pure rendering:
 * no backend state is read or written. */

async function ready(page) {
  await page.goto('/');
  await page.waitForFunction(() => typeof window.progress === 'function');
}

test('unknown total size renders an unavailable percentage, never 0%', async ({page}) => {
  await ready(page);
  const rendered = await page.evaluate(() => {
    const label = html => {
      const holder = document.createElement('div');
      holder.innerHTML = html;
      return holder.textContent.trim();
    };
    return {
      activeUnknown: label(window.progress(null, 'downloading')),
      queuedUnknown: label(window.progress(undefined, 'queued')),
      knownZero: label(window.progress(0, 'queued')),
      known: label(window.progress(42, 'downloading')),
      completed: label(window.progress(null, 'completed')),
      stripe: window.progress(null, 'downloading').includes('repeating-linear-gradient'),
    };
  });
  expect(rendered.activeUnknown).toBe('—');
  expect(rendered.queuedUnknown).toBe('—');
  expect(rendered.activeUnknown).not.toContain('0%');
  expect(rendered.stripe).toBe(true);
  expect(rendered.knownZero).toBe('0%');
  expect(rendered.known).toBe('42%');
  expect(rendered.completed).toBe('100%');
});
