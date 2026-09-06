const { test, expect } = require('@playwright/test');

async function waitForBatch(page) {
  await page.goto('/');
  await page.waitForFunction(() => Boolean(window.DPUICorrectionBatch1 && window.DPProviderStatus));
}

function center(box) {
  return box.x + box.width / 2;
}

test('repair: Quick Add pause state is centered on the entire header and visually dominant', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await waitForBatch(page);

  const before = await page.evaluate(() => {
    const header = document.querySelector('.dp-dashboard-quick-add > .card-header');
    const title = header.querySelector('.card-title').getBoundingClientRect();
    const actions = header.querySelector('.dp-card-header-actions').getBoundingClientRect();
    return { titleX: title.x, actionX: actions.x };
  });

  await page.evaluate(() => {
    settingsData = settingsData || {};
    settingsData.paused = true;
    renderTopbarActions();
  });

  const headerBox = await page.locator('.dp-dashboard-quick-add > .card-header').boundingBox();
  const pauseBox = await page.locator('.dp-global-pause-center').boundingBox();
  expect(Math.abs(center(headerBox) - center(pauseBox))).toBeLessThanOrEqual(1);

  const visual = await page.locator('.dp-global-pause-center').evaluate(node => {
    const title = node.querySelector('.dp-global-pause-title');
    const copy = node.querySelector('.dp-global-pause-copy');
    return {
      titleSize: parseFloat(getComputedStyle(title).fontSize),
      copySize: parseFloat(getComputedStyle(copy).fontSize),
      glyphs: title.querySelectorAll('svg').length,
    };
  });
  expect(visual.glyphs).toBe(1);
  expect(visual.titleSize).toBeGreaterThanOrEqual(15);
  expect(visual.titleSize).toBeGreaterThan(visual.copySize);

  await page.evaluate(() => {
    settingsData.paused = false;
    renderTopbarActions();
  });
  const after = await page.evaluate(() => {
    const header = document.querySelector('.dp-dashboard-quick-add > .card-header');
    const title = header.querySelector('.card-title').getBoundingClientRect();
    const actions = header.querySelector('.dp-card-header-actions').getBoundingClientRect();
    return { titleX: title.x, actionX: actions.x };
  });
  expect(Math.abs(before.titleX - after.titleX)).toBeLessThanOrEqual(1);
  expect(Math.abs(before.actionX - after.actionX)).toBeLessThanOrEqual(1);
});

test('repair: Provider Status is centered, grouped members are suppressed, and only premium crown is offset', async ({ page }) => {
  await waitForBatch(page);

  await page.evaluate(async () => {
    settingsData = {
      integrations: {
        alldebrid: {
          kind: 'provider', enabled: true, configured: true,
          presentation: {
            status_name: 'AllDebrid', static_status: 'healthy', display_order: 10, premium: true,
          },
        },
        general_http: {
          kind: 'provider', enabled: true, configured: true,
          presentation: {
            status_name: 'HTTP & HTTPS', static_status: 'healthy', display_order: 100,
            status_group: 'direct_sources', status_group_label: 'Direct Sources',
          },
        },
      },
    };
    await DPProviderStatus.refresh();
    const premium = document.getElementById('premium-row');
    premium.style.display = 'flex';
    document.getElementById('lbl-premium').innerHTML = '<span class="dp-provider-premium-until">Premium until Sep 30</span><span class="dp-provider-premium-days">25 days</span>';
  });

  const footerBox = await page.locator('.sidebar-footer').boundingBox();
  const headingBox = await page.locator('.dp-provider-status-heading').boundingBox();
  const allDebridBox = await page.locator('[data-provider-id="alldebrid"]').boundingBox();
  const directBox = await page.locator('[data-provider-group="direct_sources"] .dp-provider-status-group-row').boundingBox();
  expect(Math.abs(center(footerBox) - center(headingBox))).toBeLessThanOrEqual(1);
  expect(Math.abs(center(footerBox) - center(allDebridBox))).toBeLessThanOrEqual(1);
  expect(Math.abs(center(footerBox) - center(directBox))).toBeLessThanOrEqual(1);
  await expect(page.locator('#provider-status-list')).toContainText('Direct Sources');
  await expect(page.locator('#provider-status-list')).not.toContainText('HTTP & HTTPS');

  const crown = await page.evaluate(() => {
    const row = document.getElementById('premium-row');
    const label = document.getElementById('lbl-premium');
    return {
      crownTransform: getComputedStyle(row, '::before').transform,
      labelTransform: getComputedStyle(label).transform,
      labelTop: getComputedStyle(label).top,
    };
  });
  expect(crown.crownTransform).not.toBe('none');
  expect(crown.labelTransform).toBe('none');
  expect(crown.labelTop).toBe('auto');
});

test('repair: Direct Sources aggregate honors disabled, mixed, and healthy membership without child rows', async ({ page }) => {
  await waitForBatch(page);
  const states = await page.evaluate(() => {
    const entry = enabled => ({
      id: enabled ? 'one' : 'two', name: enabled ? 'One' : 'Two', enabled,
      state: enabled ? 'healthy' : 'disabled', groupId: 'direct_sources', groupLabel: 'Direct Sources',
    });
    return {
      disabled: DPProviderStatus.aggregateState([entry(false)]),
      mixed: DPProviderStatus.aggregateState([entry(true), entry(false)]),
      healthy: DPProviderStatus.aggregateState([entry(true), {...entry(true), id: 'three'}]),
    };
  });
  expect(states).toEqual({ disabled: 'disabled', mixed: 'mixed', healthy: 'healthy' });
});

test('repair: Downloads pager current slot stays at footer center for one/first/middle/last pages', async ({ page }) => {
  await waitForBatch(page);
  await page.evaluate(async () => {
    nav(document.querySelector('[data-view="torrents"]'));
    await loadTorrents();
  });

  const cases = [
    { total: 5, limit: 10, offset: 0 },
    { total: 30, limit: 10, offset: 0 },
    { total: 30, limit: 10, offset: 10 },
    { total: 30, limit: 10, offset: 20 },
  ];
  for (const item of cases) {
    await page.evaluate(args => renderTorrentPagination(args.total, args.limit, args.offset), item);
    const footer = await page.locator('#torrent-pagination').boundingBox();
    const current = await page.locator('#torrent-page-btns .dp-pager-current').boundingBox();
    const group = await page.locator('#torrent-page-btns').boundingBox();
    expect(Math.abs(center(footer) - center(current))).toBeLessThanOrEqual(1);
    expect(group.width).toBeLessThanOrEqual(116);
    await expect(page.locator('#torrent-page-btns .dp-pager-placeholder')).not.toBeFocused();
  }

  const relation = await page.evaluate(() => {
    const footer = document.getElementById('torrent-pagination').getBoundingClientRect();
    const info = document.getElementById('torrent-page-info').getBoundingClientRect();
    return { footerCenter: footer.x + footer.width / 2, infoCenter: info.x + info.width / 2 };
  });
  expect(relation.infoCenter).toBeGreaterThan(relation.footerCenter);
});

test('repair: Details file status badges have identical outer dimensions across vocabulary', async ({ page }) => {
  await waitForBatch(page);
  await page.evaluate(() => {
    const probe = document.createElement('div');
    probe.id = 'details-badge-probe';
    probe.className = 'dp-detail-files-card';
    const statuses = ['downloading', 'queued', 'completed', 'paused', 'error', 'processing'];
    probe.innerHTML = `<table><tbody>${statuses.map(status => `<tr><td>file</td><td>${badge(status, {status})}</td></tr>`).join('')}</tbody></table>`;
    document.body.appendChild(probe);
  });

  const boxes = await page.locator('#details-badge-probe .badge').evaluateAll(nodes => nodes.map(node => {
    const box = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    return { width: box.width, height: box.height, wrap: style.whiteSpace, overflow: style.overflow };
  }));
  expect(boxes.length).toBeGreaterThanOrEqual(6);
  for (const box of boxes) {
    expect(Math.abs(box.width - boxes[0].width)).toBeLessThanOrEqual(0.5);
    expect(Math.abs(box.height - boxes[0].height)).toBeLessThanOrEqual(0.5);
    expect(box.wrap).toBe('nowrap');
  }
  expect(Math.round(boxes[0].width)).toBe(136);
  expect(Math.round(boxes[0].height)).toBe(28);
});

test('repair: Date chevron exposes all selectable presentation choices inside its panel', async ({ page }) => {
  await waitForBatch(page);
  await page.evaluate(async () => {
    nav(document.querySelector('[data-view="torrents"]'));
    await loadTorrents();
  });
  const trigger = page.locator('.dp-date-menu-trigger');
  await trigger.click();

  const names = ['Friendly', 'US', 'International', 'ISO', '24-hour', '12-hour'];
  for (const name of names) await expect(page.getByRole('menuitemradio', { name })).toBeVisible();
  await expect(page.getByRole('menuitemradio', { name: 'Friendly' })).toHaveAttribute('aria-checked', 'true');
  await expect(page.getByRole('menuitemradio', { name: '24-hour' })).toHaveAttribute('aria-checked', 'true');

  const bounds = await page.evaluate(() => {
    const menu = document.querySelector('.dp-date-menu').getBoundingClientRect();
    const rows = [...document.querySelectorAll('.dp-date-menu button')].map(node => node.getBoundingClientRect());
    return { menu, rows };
  });
  for (const row of bounds.rows) {
    expect(row.left).toBeGreaterThanOrEqual(bounds.menu.left - 0.5);
    expect(row.right).toBeLessThanOrEqual(bounds.menu.right + 0.5);
  }

  await page.getByRole('menuitemradio', { name: 'US' }).click();
  const persisted = await page.evaluate(() => JSON.parse(localStorage.getItem('debridpulse.downloads.date-presentation.v1')));
  expect(persisted.format).toBe('us');
});

test('repair: Recent Activity progress visibility changes do not alter row or action geometry', async ({ page }) => {
  await waitForBatch(page);
  await page.evaluate(() => {
    const body = document.getElementById('dash-tbody');
    body.innerHTML = `<tr data-torrent-id="991" data-status="downloading">
      <td><div class="t-name">geometry.bin</div><div class="dash-row-bar-slot"><div class="dash-row-bar"><div class="dash-row-bar-fill" style="width:45%"></div></div></div><div class="dp-transfer-provider-meta"><span class="dp-source-icon-slot"></span><span class="dp-provider-chip">AllDebrid</span></div></td>
      <td>${badge('downloading', {status:'downloading'})}</td><td>${progress(45, 'downloading')}</td><td>1 GB</td><td>Today</td>
      <td><div class="actions"><button class="btn btn-blue btn-sm">Pause</button></div></td>
    </tr>`;
  });

  const before = await page.evaluate(() => {
    const row = document.querySelector('#dash-tbody tr');
    const action = row.querySelector('.actions .btn');
    const rb = row.getBoundingClientRect();
    const ab = action.getBoundingClientRect();
    return { row: {x:rb.x,y:rb.y,width:rb.width,height:rb.height}, action: {x:ab.x,y:ab.y,width:ab.width,height:ab.height} };
  });

  await page.evaluate(() => {
    const row = document.querySelector('#dash-tbody tr');
    row.dataset.status = 'paused';
    row.querySelector('.dash-row-bar').classList.add('is-empty');
    row.querySelector('.actions .btn').textContent = 'Resume';
  });

  const after = await page.evaluate(() => {
    const row = document.querySelector('#dash-tbody tr');
    const action = row.querySelector('.actions .btn');
    const rb = row.getBoundingClientRect();
    const ab = action.getBoundingClientRect();
    return { row: {x:rb.x,y:rb.y,width:rb.width,height:rb.height}, action: {x:ab.x,y:ab.y,width:ab.width,height:ab.height} };
  });
  expect(Math.abs(before.row.height - after.row.height)).toBeLessThanOrEqual(0.5);
  expect(Math.abs(before.action.x - after.action.x)).toBeLessThanOrEqual(0.5);
  expect(Math.abs(before.action.y - after.action.y)).toBeLessThanOrEqual(0.5);
});

test('repair: malformed Quick Add copy uses adaptive lifetime and hover pauses remaining time', async ({ page }) => {
  await waitForBatch(page);
  const duration = await page.evaluate(() => DPToastDuration('DebridPulse stared at that for a moment. It is not a link, magnet, or torrent.', 'info'));
  expect(duration).toBeGreaterThan(3500);
  expect(duration).toBeLessThanOrEqual(12000);

  await page.evaluate(() => toast('Line 1: enter an HTTP(S) link or magnet URI', 'info'));
  const node = page.locator('#toasts .toast').last();
  await expect(node).toContainText('DebridPulse stared at that for a moment. It is not a link, magnet, or torrent.');
  await node.hover();
  await page.waitForTimeout(3800);
  await expect(node).toBeVisible();
  await node.locator('.dp-toast-close').click();
  await expect(node).toHaveCount(0);
});
