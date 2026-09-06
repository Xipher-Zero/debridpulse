const { test, expect } = require('@playwright/test');
const fs = require('fs');

async function ready(page) {
  await page.goto('/');
  await page.waitForFunction(() => Boolean(window.DPIcons?.toast && window.DPToastContract));
}

async function settle(page) {
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

async function create(page, message, type = 'info') {
  await page.evaluate(({message, type}) => DPIcons.toast(message, type), {message, type});
  const node = page.locator('#toasts .toast').last();
  await expect(node).toBeVisible();
  await settle(page);
  return node;
}

async function clearToasts(page) {
  await page.evaluate(() => {
    document.querySelectorAll('#toasts .toast').forEach(node => {
      if (typeof node.__dpToastDispose === 'function') node.__dpToastDispose();
      else node.remove();
    });
  });
}

test('canonical toast lifetime is exact word-count clamp for simple and structured copy', async ({ page }) => {
  await ready(page);
  const values = await page.evaluate(() => {
    const words = count => Array.from({length: count}, (_, index) => `w${index + 1}`).join(' ');
    const structured = {
      title: words(4),
      body: words(16),
    };
    return {
      counts: [3, 12, 20, 30].map(count => DPIcons.toastWordCount(words(count))),
      durations: [3, 12, 20, 30].map(count => DPIcons.toastDuration(words(count))),
      structuredCount: DPIcons.toastWordCount(structured),
      structuredDuration: DPIcons.toastDuration(structured),
      bridgeDuration: DPToastDuration(words(30)),
      batchDuration: DPUICorrectionBatch1.toastDuration(words(20)),
    };
  });
  expect(values).toEqual({
    counts: [3, 12, 20, 30],
    durations: [3000, 3000, 5000, 7500],
    structuredCount: 20,
    structuredDuration: 5000,
    bridgeDuration: 7500,
    batchDuration: 5000,
  });
});

test('toast source contract contains no legacy dismissal presentation', async ({ page }) => {
  await ready(page);
  const [legacyResponse, canonicalResponse] = await Promise.all([
    page.request.get('/ui-correction-batch1.css'),
    page.request.get('/ui-toast-contract.css'),
  ]);
  expect(legacyResponse.ok()).toBeTruthy();
  expect(canonicalResponse.ok()).toBeTruthy();
  const source = `${await legacyResponse.text()}\n${await canonicalResponse.text()}`;
  expect(source).not.toContain('.dp-toast-close');
  expect(source).not.toContain('.dp-toast-dismiss');
  expect(source).not.toContain('manual dismissal');
});

test('canonical toast has no dismiss control and independent lifetimes include fade inside total lifetime', async ({ page }) => {
  await ready(page);
  const captured = await page.evaluate(() => {
    const nativeSetTimeout = window.setTimeout;
    const nativeClearTimeout = window.clearTimeout;
    let nextId = -1000000;
    const timers = [];
    const words = count => Array.from({length: count}, (_, index) => `w${index + 1}`).join(' ');

    window.setTimeout = (fn, delay) => {
      const id = nextId--;
      timers.push({id, delay:Number(delay), fn, ran:false});
      return id;
    };
    window.clearTimeout = id => {
      const timer = timers.find(item => item.id === id);
      if (timer) timer.ran = true;
    };
    try {
      const short = DPIcons.toast(words(12), 'warning');
      short.dataset.dpTimingProbe = 'short';
      const long = DPIcons.toast(words(20), 'info');
      long.dataset.dpTimingProbe = 'long';
    } finally {
      window.setTimeout = nativeSetTimeout;
      window.clearTimeout = nativeClearTimeout;
    }

    window.__runToastTimers = cutoff => {
      timers
        .filter(timer => !timer.ran && timer.delay <= cutoff)
        .sort((a, b) => a.delay - b.delay || b.id - a.id)
        .forEach(timer => {
          if (timer.ran) return;
          timer.ran = true;
          timer.fn();
        });
    };
    window.__toastCapturedDelays = timers.map(timer => timer.delay).sort((a, b) => a - b);
    return window.__toastCapturedDelays;
  });
  expect(captured).toEqual([2750, 3000, 4750, 5000]);

  const short = page.locator('#toasts .toast[data-dp-timing-probe="short"]');
  const long = page.locator('#toasts .toast[data-dp-timing-probe="long"]');
  await expect(short).toHaveAttribute('role', 'alert');
  await expect(short).toHaveAttribute('data-dp-toast-duration-ms', '3000');
  await expect(short).toHaveAttribute('data-dp-toast-fade-at-ms', '2750');
  await expect(long).toHaveAttribute('role', 'status');
  await expect(long).toHaveAttribute('data-dp-toast-duration-ms', '5000');
  await expect(long).toHaveAttribute('data-dp-toast-fade-at-ms', '4750');
  await expect(short.locator('button, .dp-toast-dismiss, .dp-toast-close')).toHaveCount(0);
  await expect(long.locator('button, .dp-toast-dismiss, .dp-toast-close')).toHaveCount(0);

  const alignment = await long.evaluate(element => ({
    alignItems:getComputedStyle(element).alignItems,
    justifyContent:getComputedStyle(element).justifyContent,
    textAlign:getComputedStyle(element).textAlign,
  }));
  expect(alignment).toEqual({alignItems:'center', justifyContent:'center', textAlign:'center'});

  await page.evaluate(() => window.__runToastTimers(2750));
  await expect(short).toHaveAttribute('data-dp-toast-fading', '1');
  await expect(short).toHaveCount(1);
  await expect(long).not.toHaveAttribute('data-dp-toast-fading', '1');

  await page.evaluate(() => window.__runToastTimers(3000));
  await expect(short).toHaveCount(0);
  await expect(long).toHaveCount(1);

  await page.evaluate(() => window.__runToastTimers(4750));
  await expect(long).toHaveAttribute('data-dp-toast-fading', '1');
  await expect(long).toHaveCount(1);

  await page.evaluate(() => window.__runToastTimers(5000));
  await expect(long).toHaveCount(0);
  await page.evaluate(() => {
    delete window.__runToastTimers;
    delete window.__toastCapturedDelays;
  });
});

test('desktop structured toast stays in the topbar and resize recomputes a short toast without occupant overlap', async ({ page }) => {
  await page.setViewportSize({width: 1440, height: 900});
  await ready(page);
  let node = await create(page, {
    title:'Transfer route updated',
    body:'The selected provider changed while the original source and acquisition history remain available in Details.',
  }, 'info');

  const inspect = () => node.evaluate(element => {
    const rect = element.getBoundingClientRect();
    const topbarRect = document.getElementById('topbar').getBoundingClientRect();
    const lane = DPIcons.toastSafeLane();
    const heading = document.querySelector('.dp-page-heading');
    const title = heading?.querySelector('#page-title');
    const subtitle = heading?.querySelector('#page-subtitle');
    const candidates = [];
    if (title) candidates.push(title.getBoundingClientRect());
    if (subtitle) {
      const range = document.createRange();
      range.selectNodeContents(subtitle);
      const subtitleRect = range.getBoundingClientRect();
      if (subtitleRect.width > 0 && subtitleRect.height > 0) candidates.push(subtitleRect);
    }
    [
      document.getElementById('update-badge'),
      document.getElementById('topbar-actions'),
      document.getElementById('aria2-speed-badge'),
      document.querySelector('.topbar-theme-control'),
    ].filter(Boolean).forEach(item => {
      const style = getComputedStyle(item);
      const box = item.getBoundingClientRect();
      if (style.display !== 'none' && style.visibility !== 'hidden' && box.width > 0 && box.height > 0) candidates.push(box);
    });
    const overlaps = candidates.some(box => (
      rect.left < box.right && rect.right > box.left && rect.top < box.bottom && rect.bottom > box.top
    ));
    return {rect, topbarRect, lane, overlaps};
  });

  let geometry = await inspect();
  expect(geometry.lane.narrow).toBe(false);
  expect(geometry.rect.left).toBeGreaterThanOrEqual(geometry.lane.left - 1);
  expect(geometry.rect.right).toBeLessThanOrEqual(geometry.lane.right + 1);
  expect(geometry.rect.top).toBeGreaterThanOrEqual(geometry.lane.top - 1);
  expect(geometry.rect.bottom).toBeLessThanOrEqual(geometry.lane.bottom + 1);
  expect(geometry.overlaps).toBe(false);

  await clearToasts(page);
  await page.setViewportSize({width: 1180, height: 900});
  await settle(page);
  node = await create(page, 'aria2 queue resumed', 'info');
  geometry = await inspect();
  expect(geometry.lane.narrow).toBe(false);
  expect(geometry.rect.left).toBeGreaterThanOrEqual(geometry.lane.left - 1);
  expect(geometry.rect.right).toBeLessThanOrEqual(geometry.lane.right + 1);
  expect(geometry.rect.top).toBeGreaterThanOrEqual(geometry.lane.top - 1);
  expect(geometry.rect.bottom).toBeLessThanOrEqual(geometry.lane.bottom + 1);
  expect(geometry.overlaps).toBe(false);
});

test('desktop toast placement settles without rAF oscillation', async ({ page }) => {
  await page.setViewportSize({width: 1440, height: 900});
  await ready(page);
  const node = await create(page, 'Stable placement remains anchored to rendered header geometry across frames.', 'success');
  const samples = await node.evaluate(async element => {
    const values = [];
    for (let index = 0; index < 6; index += 1) {
      await new Promise(resolve => requestAnimationFrame(resolve));
      const rect = element.getBoundingClientRect();
      values.push({x: rect.x, y: rect.y, width: rect.width, height: rect.height});
    }
    return values;
  });
  const tail = samples.slice(2);
  for (const key of ['x', 'y', 'width', 'height']) {
    const values = tail.map(sample => sample[key]);
    expect(Math.max(...values) - Math.min(...values)).toBeLessThanOrEqual(0.5);
  }
});

test('global compatibility producer uses the same noninteractive canonical structure', async ({ page }) => {
  await ready(page);
  await page.evaluate(() => toast('Checking AllDebrid for ready torrents…', 'info'));
  const node = page.locator('#toasts .toast').last();
  await expect(node).toHaveText(/Checking transfers for recoverable work/);
  await expect(node.locator('button, .dp-toast-dismiss, .dp-toast-close')).toHaveCount(0);
  await expect(node).toHaveAttribute('data-dp-toast-duration-ms', '3000');
  const pointer = await node.evaluate(element => ({
    host: getComputedStyle(document.getElementById('toasts')).pointerEvents,
    toast: getComputedStyle(element).pointerEvents,
  }));
  expect(pointer).toEqual({host: 'none', toast: 'none'});
});

test('toast visual checkpoints cover short, medium, multiline, and structured copy in dark and light themes', async ({ page }) => {
  await page.setViewportSize({width: 1440, height: 900});
  await ready(page);
  fs.mkdirSync('test-results', {recursive:true});

  const variants = [
    ['short', 'Saved'],
    ['medium', 'Settings were saved and the active provider configuration is ready for new downloads.'],
    ['multiline', 'This notification intentionally uses enough words to wrap naturally while the complete card remains contained inside the measured topbar lane.'],
    ['structured', {
      title:'Duplicate files consolidated',
      body:'Matching files were merged while alternate acquisition candidates remain available for failover.',
    }],
  ];

  for (const theme of ['dark', 'light']) {
    await page.evaluate(value => document.body.classList.toggle('light', value === 'light'), theme);
    for (const [name, message] of variants) {
      await clearToasts(page);
      const node = await create(page, message, name === 'structured' ? 'success' : 'info');
      const bounds = await node.evaluate(element => {
        const rect = element.getBoundingClientRect();
        const lane = DPIcons.toastSafeLane();
        return {rect, lane};
      });
      expect(bounds.rect.left).toBeGreaterThanOrEqual(bounds.lane.left - 1);
      expect(bounds.rect.right).toBeLessThanOrEqual(bounds.lane.right + 1);
      expect(bounds.rect.top).toBeGreaterThanOrEqual(bounds.lane.top - 1);
      expect(bounds.rect.bottom).toBeLessThanOrEqual(bounds.lane.bottom + 1);
      await page.screenshot({path:`test-results/checkpoint-toast-${theme}-${name}.png`, fullPage:false});
    }
  }
  await clearToasts(page);
});