const { test, expect } = require('@playwright/test');

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status:200, contentType:'text/css', body:''}));
}

function candidate(id, source, relationship, dispositions = []) {
  return {
    candidate_id:id,
    source_label:source,
    provider_id:'alldebrid',
    relationship,
    dispositions,
    is_selected:dispositions.includes('Active') || dispositions.includes('Selected') || dispositions.includes('Delivering'),
    is_delivering:dispositions.includes('Delivering'),
  };
}

function file(id, name, status, candidates) {
  const value = {
    id,
    filename:name,
    size_bytes:10737418240,
    status,
    blocked:false,
    block_reason:null,
    candidate_count:candidates.length,
  };
  if (candidates.length > 1) value.acquisition_candidates = candidates;
  return value;
}

function detailFixture(files) {
  return {
    id:990,
    name:'Corrective interaction fixture',
    status:'paused',
    progress:42,
    size_bytes:files.reduce((n, f) => n + f.size_bytes, 0),
    source:'direct_link',
    label:'',
    hash:'',
    created_at:'2026-09-05T10:00:00Z',
    original_resource:'https://rapidgator.net/file/example',
    provider_provenance_status:'recorded',
    current_provider_id:'alldebrid',
    current_provider_name:'AllDebrid',
    delivering_provider_id:null,
    delivering_provider_name:null,
    route_attempts:[],
    execution_attempts:[],
    executors:['aria2'],
    source_outcomes:[],
    events:[],
    files,
  };
}

const two = [
  candidate('a', 'rapidgator.net', 'Original', ['Active']),
  candidate('b', 'mirror.example', 'Consolidated', []),
];
const three = [...two, candidate('c', 'second.example', 'Consolidated', [])];

async function installDetailRoute(page, files) {
  const fixture = detailFixture(files);
  await page.route(url => url.pathname === '/api/torrents/990', route => route.fulfill({
    status:200,
    contentType:'application/json',
    body:JSON.stringify(fixture),
  }));
}

async function openDetail(page) {
  await page.evaluate(() => showDetail(990));
  await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);
  await expect(page.locator('.dp-detail-files-card')).toBeVisible();
}

async function dragSelectText(page, locator) {
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  const y = box.y + box.height / 2;
  const start = box.x + Math.min(3, box.width / 4);
  const end = box.x + Math.max(4, Math.min(box.width - 3, 150));
  await page.mouse.move(start, y);
  await page.mouse.down();
  await page.mouse.move(end, y, {steps:12});
  await page.mouse.up();
  return page.evaluate(() => String(window.getSelection() || '').trim());
}

async function clickNav(page, view) {
  await page.locator(`.nav-item[data-view="${view}"]`).click();
  await expect(page.locator(`#view-${view}`)).toHaveClass(/\bactive\b/);
}

async function createToast(page, message = 'Corrective placement probe') {
  await page.evaluate(value => window.DPIcons.toast(value, 'info'), message);
  const toast = page.locator('#toasts .toast').last();
  await expect(toast).toBeVisible();
  return toast;
}

async function toastGeometry(page, toast) {
  return toast.evaluate(element => {
    const rect = element.getBoundingClientRect();
    const host = document.getElementById('toasts');
    const topbar = document.getElementById('topbar');
    const content = document.getElementById('content');
    const topbarRect = topbar.getBoundingClientRect();
    const activeView = content.querySelector(':scope > .view.active');
    const contentStyle = getComputedStyle(content);
    const contentTop = activeView
      ? activeView.getBoundingClientRect().top
      : topbarRect.bottom + (parseFloat(contentStyle.paddingTop) || 0);
    const visibleChildren = Array.from(topbar.children).filter(child => {
      if (!(child instanceof HTMLElement)) return false;
      const style = getComputedStyle(child);
      return style.display !== 'none' && style.visibility !== 'hidden';
    });
    const visibleBottoms = visibleChildren
      .map(child => child.getBoundingClientRect().bottom)
      .filter(Number.isFinite);
    const controlsBottom = Math.min(
      topbarRect.bottom,
      Math.max(topbarRect.top, visibleBottoms.length ? Math.max(...visibleBottoms) : topbarRect.bottom)
    );
    const lower = Math.max(topbarRect.bottom, contentTop);
    return {
      left:rect.left,
      right:rect.right,
      top:rect.top,
      bottom:rect.bottom,
      width:rect.width,
      height:rect.height,
      centerX:(rect.left + rect.right) / 2,
      centerY:(rect.top + rect.bottom) / 2,
      viewportWidth:window.innerWidth,
      viewportHeight:window.innerHeight,
      topbarBottom:topbarRect.bottom,
      controlsBottom,
      contentTop:lower,
      expectedDesktopAnchor:controlsBottom + Math.max(0, lower - controlsBottom) / 2,
      hostPointerEvents:getComputedStyle(host).pointerEvents,
      toastPointerEvents:getComputedStyle(element).pointerEvents,
    };
  });
}

async function dismissToast(page, toast) {
  const close = toast.locator('.dp-toast-dismiss');
  await expect(close).toBeVisible();
  await close.click();
  await expect(toast).toHaveCount(0);
}

test('Candidates remains an inline real-pointer disclosure without capture ownership or destructive click rerender', async ({ page }) => {
  await isolateExternalFonts(page);
  const files = [file(502, 'GF200826-TMNTSFS-RN.rar', 'paused', two), file(503, 'part-02.rar', 'queued', three)];
  for (let i = 0; i < 10; i++) files.push(file(600 + i, `scroll-${i}.rar`, 'queued', three));
  await installDetailRoute(page, files);
  await page.goto('/');
  await openDetail(page);

  const first = page.locator('tr[data-dp-artifact-id="502"] .dp-detail-candidate-disclosure');
  const second = page.locator('tr[data-dp-artifact-id="503"] .dp-detail-candidate-disclosure');
  await first.evaluate(button => {
    button.dataset.pointerIdentityProbe = 'survives-click';
    window.__candidateBubbleEvents = 0;
    document.getElementById('modal-body').addEventListener('click', event => {
      if (event.target instanceof Element && event.target.closest('.dp-detail-candidate-disclosure')) {
        window.__candidateBubbleEvents += 1;
        window.__candidateDefaultPrevented = event.defaultPrevented;
      }
    });
  });

  await first.click();
  await expect(first).toHaveAttribute('aria-expanded', 'true');
  await expect(first).toHaveAttribute('data-pointer-identity-probe', 'survives-click');
  await expect(page.locator('tr[data-dp-candidate-owner="502"]')).toBeVisible();
  expect(await page.evaluate(() => window.__candidateBubbleEvents)).toBe(1);
  expect(await page.evaluate(() => window.__candidateDefaultPrevented)).toBe(false);

  const ordinary = page.locator('tr[data-dp-artifact-id="502"] .dp-detail-filename-copy');
  await ordinary.evaluate(element => {
    window.__ordinaryDetailClicks = 0;
    element.addEventListener('click', () => { window.__ordinaryDetailClicks += 1; });
  });
  await ordinary.click();
  expect(await page.evaluate(() => window.__ordinaryDetailClicks)).toBe(1);

  const pointerOwner = await ordinary.evaluate(element => {
    const rect = element.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.left + Math.min(8, rect.width / 2), rect.top + rect.height / 2);
    return !!hit && document.getElementById('modal-body').contains(hit);
  });
  expect(pointerOwner).toBe(true);

  const selected = await dragSelectText(page, ordinary);
  expect(selected.length).toBeGreaterThan(0);

  await first.click();
  await expect(first).toHaveAttribute('aria-expanded', 'false');
  await first.click();
  await expect(first).toHaveAttribute('aria-expanded', 'true');
  await second.click();
  await expect(second).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator('tr[data-dp-candidate-owner="502"]')).toBeVisible();
  await expect(page.locator('tr[data-dp-candidate-owner="503"]')).toBeVisible();

  await first.focus();
  await page.keyboard.press('Enter');
  await expect(first).toHaveAttribute('aria-expanded', 'false');
  await page.keyboard.press('Enter');
  await expect(first).toHaveAttribute('aria-expanded', 'true');

  const modal = page.locator('#modal');
  await modal.hover();
  const beforeScroll = await modal.evaluate(element => element.scrollTop);
  await page.mouse.wheel(0, 700);
  await expect.poll(() => modal.evaluate(element => element.scrollTop)).toBeGreaterThan(beforeScroll);
  const last = page.locator('tr[data-dp-artifact-id="609"] .dp-detail-candidate-disclosure');
  await last.scrollIntoViewIfNeeded();
  await last.click();
  await expect(last).toHaveAttribute('aria-expanded', 'true');

  await page.locator('.dp-detail-close').click();
  await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);
  await openDetail(page);
  await expect(page.locator('.dp-detail-candidate-disclosure[aria-expanded="true"]')).toHaveCount(0);
  await expect(page.locator('.dp-detail-candidate-row')).toHaveCount(0);
});

test('global toast lane is viewport-centered in the same shell deadspace across normal pages', async ({ page }) => {
  await isolateExternalFonts(page);
  await page.goto('/');

  const views = ['dashboard', 'torrents', 'events', 'stats', 'settings', 'help'];
  const centers = [];
  for (const view of views) {
    await clickNav(page, view);
    const toast = await createToast(page, `Placement probe: ${view}`);
    const geometry = await toastGeometry(page, toast);
    expect(Math.abs(geometry.centerX - geometry.viewportWidth / 2)).toBeLessThanOrEqual(1.5);
    expect(geometry.centerY).toBeGreaterThanOrEqual(geometry.controlsBottom - 1);
    expect(geometry.centerY).toBeLessThanOrEqual(geometry.contentTop + 1);
    expect(Math.abs(geometry.centerY - geometry.expectedDesktopAnchor)).toBeLessThanOrEqual(2);
    expect(geometry.hostPointerEvents).toBe('none');
    expect(geometry.toastPointerEvents).toBe('auto');
    centers.push(geometry.centerY);
    await dismissToast(page, toast);
  }
  expect(Math.max(...centers) - Math.min(...centers)).toBeLessThanOrEqual(2);
});

test('multiline toast stays centered around the deadspace anchor, wraps, dismisses, and leaves transparent page interaction', async ({ page }) => {
  await isolateExternalFonts(page);
  await page.goto('/');

  const shortToast = await createToast(page, 'Short notification');
  const shortGeometry = await toastGeometry(page, shortToast);
  await dismissToast(page, shortToast);

  const longToast = await createToast(page, {
    title:'Duplicate files consolidated',
    body:'This deliberately long notification verifies that multiline content wraps inside the bounded application-wide notification lane without moving the card away from the shell deadspace center or covering lower page controls.',
  });
  const longGeometry = await toastGeometry(page, longToast);
  expect(longGeometry.height).toBeGreaterThan(shortGeometry.height);
  expect(longGeometry.width).toBeLessThanOrEqual(480.5);
  expect(longGeometry.left).toBeGreaterThanOrEqual(15);
  expect(longGeometry.right).toBeLessThanOrEqual(longGeometry.viewportWidth - 15);
  expect(Math.abs(longGeometry.centerX - longGeometry.viewportWidth / 2)).toBeLessThanOrEqual(1.5);
  expect(Math.abs(longGeometry.centerY - longGeometry.expectedDesktopAnchor)).toBeLessThanOrEqual(2);

  const downloadsNav = page.locator('.nav-item[data-view="torrents"]');
  const hitIsNavigation = await downloadsNav.evaluate(element => {
    const rect = element.getBoundingClientRect();
    const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    return !!hit && element.contains(hit);
  });
  expect(hitIsNavigation).toBe(true);
  await downloadsNav.click();
  await expect(page.locator('#page-title')).toHaveText('Downloads');

  const afterNavigation = await toastGeometry(page, longToast);
  expect(Math.abs(afterNavigation.centerX - longGeometry.centerX)).toBeLessThanOrEqual(1.5);
  expect(Math.abs(afterNavigation.centerY - longGeometry.centerY)).toBeLessThanOrEqual(2);

  await dismissToast(page, longToast);
  await page.locator('.nav-item[data-view="dashboard"]').click();
  await expect(page.locator('#page-title')).toHaveText('Dashboard');
});

test('centered toast and expanded Candidates remain mutually non-blocking in Details', async ({ page }) => {
  await isolateExternalFonts(page);
  await installDetailRoute(page, [file(502, 'GF200826-TMNTSFS-RN.rar', 'paused', three)]);
  await page.goto('/');
  await openDetail(page);

  const disclosure = page.locator('.dp-detail-candidate-disclosure');
  await disclosure.click();
  await expect(disclosure).toHaveAttribute('aria-expanded', 'true');
  const toast = await createToast(page, {
    title:'Duplicate download consolidated',
    body:'Equivalent acquisition candidates were retained on the canonical artifact.',
  });

  const filename = page.locator('.dp-detail-filename-copy');
  await filename.click();
  expect((await dragSelectText(page, filename)).length).toBeGreaterThan(0);
  await disclosure.click();
  await expect(disclosure).toHaveAttribute('aria-expanded', 'false');
  await dismissToast(page, toast);
  await disclosure.click();
  await expect(disclosure).toHaveAttribute('aria-expanded', 'true');
  await page.locator('.dp-detail-close').click();
  await expect(page.locator('#overlay')).not.toHaveClass(/\bopen\b/);
});

test('mobile toast lane remains centered, bounded, and below responsive header controls', async ({ page }) => {
  await isolateExternalFonts(page);
  await page.setViewportSize({width:390, height:844});
  await page.goto('/');

  const toast = await createToast(page, {
    title:'Responsive notification',
    body:'A narrow viewport still keeps the notification readable and clear of the mobile header controls.',
  });
  const geometry = await toastGeometry(page, toast);
  expect(Math.abs(geometry.centerX - geometry.viewportWidth / 2)).toBeLessThanOrEqual(1.5);
  expect(geometry.left).toBeGreaterThanOrEqual(15);
  expect(geometry.right).toBeLessThanOrEqual(geometry.viewportWidth - 15);
  expect(geometry.top).toBeGreaterThanOrEqual(geometry.topbarBottom + 6);
  expect(geometry.bottom).toBeLessThanOrEqual(geometry.viewportHeight - 8);
  await dismissToast(page, toast);

  await page.locator('#mobile-menu-btn').click();
  await expect(page.locator('#sidebar')).toHaveClass(/\bopen\b/);
});
