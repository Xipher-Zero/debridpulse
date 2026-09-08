const { test, expect } = require('@playwright/test');

const MARKERS=['DPToastContract','DPDashboardTransferPresentation','DPDownloadsPresentation','DPProcessingPresentation','DPActivityLog','DPArchivePasswords'];
async function ready(page){await page.goto('/');await page.waitForFunction(markers=>markers.every(marker=>Boolean(window[marker])),MARKERS);}

test('bounded presentation graph loads without retired correction requests',async({page})=>{
 const requests=[];page.on('request',request=>requests.push(new URL(request.url()).pathname));await ready(page);
 const state=await page.evaluate(markers=>({loader:typeof window.DPPresentationLoader,retired:[typeof window.DPUICorrectionBatch1,typeof window.DPUICorrectionBatch1Final,typeof window.DPUICorrectionP4Repair],markers:markers.map(marker=>Boolean(window[marker]))}),MARKERS);
 expect(state.markers.every(Boolean)).toBe(true);expect(state.loader).toBe('undefined');expect(state.retired).toEqual(['undefined','undefined','undefined']);expect(requests.some(path=>path.includes('ui-correction-batch1')||path.includes('ui-correction-p4-repair')||path.includes('ui-presentation-loader'))).toBe(false);
 const processing=requests.indexOf('/ui-processing-presentation.js'),dashboard=requests.indexOf('/ui-dashboard-transfer-presentation.js'),downloads=requests.indexOf('/ui-downloads-presentation.js');
 expect(processing).toBeGreaterThan(-1);expect(dashboard).toBeGreaterThan(processing);expect(downloads).toBeGreaterThan(processing);
});

test('Dashboard source-domain matching is boundary safe and pause presentation is projected',async({page})=>{
 await page.setViewportSize({width:1440,height:900});await ready(page);
 const assets=await page.evaluate(()=>({exact:DPDashboardTransferPresentation.hostAsset('rapidgator.net'),sub:DPDashboardTransferPresentation.hostAsset('cdn.rapidgator.net'),boundary:DPDashboardTransferPresentation.hostAsset('notrapidgator.net')}));
 expect(assets).toEqual({exact:'/icons/hosts/rapidgator.png',sub:'/icons/hosts/rapidgator.png',boundary:''});
 await page.evaluate(()=>{settingsData=settingsData||{};settingsData.paused=true;renderTopbarActions();});
 await expect(page.locator('.dp-global-pause-center')).toHaveClass(/is-visible/);await expect(page.locator('.dp-global-pause-center')).toContainText('PROCESSING PAUSED');await expect(page.locator('#btn-import-existing')).toHaveCount(0);
});

test('Downloads owner exposes fixed three-slot pager and date options',async({page})=>{
 await page.setViewportSize({width:1440,height:800});await ready(page);await page.evaluate(async()=>{nav(document.querySelector('[data-view="torrents"]'));await loadTorrents();renderTorrentPagination(30,10,10);});
 await expect(page.locator('#torrent-page-btns .dp-pager-btn')).toHaveCount(2);await expect(page.locator('#torrent-page-btns .dp-pager-current')).toHaveCount(1);
 const group=await page.locator('#torrent-page-btns').boundingBox(),current=await page.locator('.dp-pager-current').boundingBox();expect(Math.abs(group.width-116)).toBeLessThanOrEqual(1);expect(Math.abs(current.width-36)).toBeLessThanOrEqual(1);
 const trigger=page.locator('.dp-date-menu-trigger');await trigger.click();for(const name of ['Friendly','US','International','ISO','24-hour','12-hour'])await expect(page.getByRole('menuitemradio',{name})).toBeVisible();
});

test('Activity Log filter interaction reaches server with filter metadata',async({page})=>{
 const requests=[];await page.route('**/api/events*',async route=>{const url=new URL(route.request().url());requests.push(Object.fromEntries(url.searchParams.entries()));await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items:[{level:'warning',message:'Retry delayed',torrent_name:'example.iso',created_at:'2026-09-06 08:30:00'}],truncated:false,limit:500})});});
 await ready(page);await page.evaluate(()=>nav(document.querySelector('[data-view="events"]')));await page.locator('#ev-timeframe').selectOption('72h');await page.locator('#ev-level').selectOption('warning');await page.locator('#ev-search').fill('Retry');await page.waitForTimeout(325);await expect.poll(()=>requests.length).toBeGreaterThan(0);
 const filtered=requests.find(item=>item.search==='Retry');expect(filtered).toEqual({limit:'500',include_meta:'true',timeframe:'72h',level:'warning',search:'Retry'});await expect(page.locator('#ev-reset')).toBeVisible();
});

test('Archive Password owner uses click reveal and line-aware editing',async({page})=>{
 await page.route('**/api/settings/extraction-passwords',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({passwords:'alpha\nbeta'})}));await ready(page);await page.evaluate(async()=>{nav(document.querySelector('[data-view="settings"]'));await loadSettings();});
 const tab=page.locator('#view-settings [data-tab="extraction"]');if(await tab.count())await tab.click();await page.waitForFunction(()=>document.querySelectorAll('.dp-settings-extraction-password-editor .dp-settings-password-line').length>=3);
 const editor=page.locator('.dp-settings-extraction-password-editor'),eye=editor.locator('.dp-settings-password-eye');await expect(eye).toContainText('Show all');await eye.click();await expect(eye).toContainText('Hide all');await expect(editor.locator('.dp-settings-password-line').first()).toHaveAttribute('type','text');await eye.click();const first=editor.locator('.dp-settings-password-line').first();await first.focus();await first.fill('changed');await first.press('Escape');await expect(first).toHaveValue('alpha');
});

test('toast bridge preserves reviewed copy and automatic lifetime',async({page})=>{
 await ready(page);expect(await page.evaluate(()=>DPToastDuration('DebridPulse stared at that for a moment. It is not a link, magnet, or torrent.'))).toBe(3750);await page.evaluate(()=>toast('Line 1: enter an HTTP(S) link or magnet URI','info'));const node=page.locator('#toasts .toast').last();await expect(node).toContainText('DebridPulse stared at that for a moment. It is not a link, magnet, or torrent.');await expect(node.locator('button,.dp-toast-close,.dp-toast-dismiss')).toHaveCount(0);
});