const { test, expect } = require('@playwright/test');

async function waitForBatch(page) {
  await page.goto('/');
  await page.waitForFunction(() => Boolean(window.DPUICorrectionBatch1));
}

function center(box) {
  return box.x + box.width / 2;
}

async function pagerGeometry(page, args) {
  await page.evaluate(value => renderTorrentPagination(value.total, value.limit, value.offset), args);
  return page.evaluate(() => {
    const footer = document.getElementById('torrent-pagination').getBoundingClientRect();
    const group = document.getElementById('torrent-page-btns').getBoundingClientRect();
    const current = document.querySelector('#torrent-page-btns .dp-pager-current').getBoundingClientRect();
    const slots = [...document.querySelectorAll('#torrent-page-btns .dp-pager-slot')].map(node => {
      const box = node.getBoundingClientRect();
      return { width: box.width, height: box.height };
    });
    const arrows = [...document.querySelectorAll('#torrent-page-btns .dp-pager-btn')].map(node => ({
      classes: [...node.classList],
      width: node.getBoundingClientRect().width,
      height: node.getBoundingClientRect().height,
    }));
    const placeholders = [...document.querySelectorAll('#torrent-page-btns .dp-pager-placeholder')].map(node => ({
      tabIndex: node.tabIndex,
      width: node.getBoundingClientRect().width,
      height: node.getBoundingClientRect().height,
      active: document.activeElement === node,
    }));
    return {
      footer: { x: footer.x, width: footer.width },
      group: { x: group.x, width: group.width, height: group.height },
      current: {
        x: current.x,
        width: current.width,
        height: current.height,
        classes: [...document.querySelector('#torrent-page-btns .dp-pager-current').classList],
      },
      slots,
      arrows,
      placeholders,
    };
  });
}

test('two-stage corrective: Downloads pager restores 1.0.11 controls while current page stays fixed at full-footer center', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await waitForBatch(page);

  const cases = [
    { name: 'single', total: 5, limit: 10, offset: 0, arrows: 0, placeholders: 2 },
    { name: 'first', total: 30, limit: 10, offset: 0, arrows: 1, placeholders: 1 },
    { name: 'middle', total: 30, limit: 10, offset: 10, arrows: 2, placeholders: 0 },
    { name: 'last', total: 30, limit: 10, offset: 20, arrows: 1, placeholders: 1 },
  ];

  const results = [];
  for (const item of cases) {
    const geometry = await pagerGeometry(page, item);
    results.push({ name: item.name, geometry });

    const footerCenter = geometry.footer.x + geometry.footer.width / 2;
    const currentCenter = geometry.current.x + geometry.current.width / 2;
    expect(Math.abs(currentCenter - footerCenter), `${item.name} current-page center`).toBeLessThanOrEqual(1);
    expect(Math.abs(geometry.group.width - 116), `${item.name} compact group`).toBeLessThanOrEqual(1);
    expect(Math.abs(geometry.current.width - 36)).toBeLessThanOrEqual(1);
    expect(Math.abs(geometry.current.height - 34)).toBeLessThanOrEqual(1);
    expect(geometry.current.classes).toEqual(expect.arrayContaining(['btn', 'btn-primary', 'btn-sm', 'dp-pager-current']));
    expect(geometry.arrows).toHaveLength(item.arrows);
    expect(geometry.placeholders).toHaveLength(item.placeholders);

    for (const slot of geometry.slots) {
      expect(Math.abs(slot.width - 36)).toBeLessThanOrEqual(1);
      expect(Math.abs(slot.height - 34)).toBeLessThanOrEqual(1);
    }
    for (const arrow of geometry.arrows) {
      expect(arrow.classes).toEqual(expect.arrayContaining(['btn', 'btn-ghost', 'btn-sm', 'dp-pager-btn']));
      expect(Math.abs(arrow.width - 36)).toBeLessThanOrEqual(1);
      expect(Math.abs(arrow.height - 34)).toBeLessThanOrEqual(1);
    }
    for (const placeholder of geometry.placeholders) {
      expect(placeholder.tabIndex).toBeLessThan(0);
      expect(placeholder.active).toBe(false);
      expect(Math.abs(placeholder.width - 36)).toBeLessThanOrEqual(1);
      expect(Math.abs(placeholder.height - 34)).toBeLessThanOrEqual(1);
    }
  }

  const currentCenters = results.map(({ geometry }) => geometry.current.x + geometry.current.width / 2);
  for (const x of currentCenters.slice(1)) expect(Math.abs(x - currentCenters[0])).toBeLessThanOrEqual(1);

  await page.evaluate(() => {
    document.getElementById('torrent-page-info').textContent = 'Showing 123456789 Added Items With A Deliberately Long Summary';
  });
  const afterSummary = await page.evaluate(() => {
    const footer = document.getElementById('torrent-pagination').getBoundingClientRect();
    const current = document.querySelector('#torrent-page-btns .dp-pager-current').getBoundingClientRect();
    return {
      footerCenter: footer.x + footer.width / 2,
      currentCenter: current.x + current.width / 2,
    };
  });
  expect(Math.abs(afterSummary.currentCenter - afterSummary.footerCenter)).toBeLessThanOrEqual(1);
});

test('two-stage corrective: configured concurrency comes from authoritative settings for builtin, external, telemetry refresh, pause, and legacy default paths', async ({ page }) => {
  await waitForBatch(page);

  const scenarios = await page.evaluate(() => {
    const read = () => ({
      active: document.getElementById('aria2-badge-active')?.textContent,
      max: document.getElementById('aria2-badge-max')?.textContent,
      resolved: window.DPUICorrectionBatch1.configuredMaxConcurrency(),
    });

    settingsData = { aria2_mode: 'external', max_concurrent_downloads: 5, aria2_max_active_downloads: 5, paused: false };
    renderTopbarActions();
    updateAria2TopbarBadge({ active: 3, externalControl: true });
    const external = read();

    settingsData = { aria2_mode: 'builtin', max_concurrent_downloads: 4, aria2_max_active_downloads: 4, paused: false };
    renderTopbarActions();
    updateAria2TopbarBadge({ active: 2, externalControl: false });
    const builtin = read();

    settingsData = { aria2_mode: 'builtin', max_concurrent_downloads: 5, aria2_max_active_downloads: 5, paused: false };
    renderTopbarActions();
    updateAria2TopbarBadge({ active: 2 });
    updateAria2TopbarBadge({ active: 3 });
    const telemetry = read();

    settingsData = { aria2_mode: 'builtin', max_concurrent_downloads: 6, aria2_max_active_downloads: 6, paused: true };
    renderTopbarActions();
    updateAria2TopbarBadge({ active: 0 });
    const paused = read();

    settingsData = { aria2_mode: 'builtin', aria2_max_active_downloads: 7, paused: false };
    renderTopbarActions();
    updateAria2TopbarBadge({ active: 1 });
    const legacy = read();

    settingsData = { aria2_mode: 'builtin', paused: false };
    renderTopbarActions();
    updateAria2TopbarBadge({ active: 1 });
    const defaults = read();

    return { external, builtin, telemetry, paused, legacy, defaults };
  });

  expect(scenarios.external).toEqual({ active: '3', max: '5', resolved: 5 });
  expect(scenarios.builtin).toEqual({ active: '2', max: '4', resolved: 4 });
  expect(scenarios.telemetry).toEqual({ active: '3', max: '5', resolved: 5 });
  expect(scenarios.paused).toEqual({ active: '0', max: '6', resolved: 6 });
  expect(scenarios.legacy).toEqual({ active: '1', max: '7', resolved: 7 });
  expect(scenarios.defaults).toEqual({ active: '1', max: '3', resolved: 3 });
});

test('two-stage corrective: settings render updates the denominator immediately and later telemetry preserves it', async ({ page }) => {
  await waitForBatch(page);

  const values = await page.evaluate(() => {
    settingsData = { aria2_mode: 'builtin', max_concurrent_downloads: 3, aria2_max_active_downloads: 3, paused: false };
    renderTopbarActions();
    updateAria2TopbarBadge({ active: 2 });
    const before = document.getElementById('aria2-badge-max').textContent;

    settingsData = { ...settingsData, max_concurrent_downloads: 6, aria2_max_active_downloads: 6 };
    renderSettings();
    const afterSettingsRender = document.getElementById('aria2-badge-max').textContent;

    updateAria2TopbarBadge({ active: 3, liveBps: 1024 });
    const afterTelemetry = {
      active: document.getElementById('aria2-badge-active').textContent,
      max: document.getElementById('aria2-badge-max').textContent,
    };
    return { before, afterSettingsRender, afterTelemetry };
  });

  expect(values.before).toBe('3');
  expect(values.afterSettingsRender).toBe('6');
  expect(values.afterTelemetry).toEqual({ active: '3', max: '6' });
});

test('two-stage corrective: aria2 global-options refresh cannot overwrite configured application concurrency', async ({ page }) => {
  await waitForBatch(page);

  const result = await page.evaluate(async () => {
    settingsData = { aria2_mode: 'builtin', max_concurrent_downloads: 5, aria2_max_active_downloads: 5, paused: false };
    renderTopbarActions();

    const originalApi = api;
    api = async function(method, path) {
      if (method === 'GET' && path === '/aria2/global-options') {
        return {
          max_download_speed: 0,
          max_concurrent_downloads: 2,
          global_options_read_only: false,
        };
      }
      return originalApi.apply(this, arguments);
    };

    try {
      await loadAria2SpeedLimit();
      return {
        canonical: settingsData.max_concurrent_downloads,
        legacy: settingsData.aria2_max_active_downloads,
        badge: document.getElementById('aria2-badge-max').textContent,
      };
    } finally {
      api = originalApi;
    }
  });

  expect(result).toEqual({ canonical: 5, legacy: 5, badge: '5' });
});
