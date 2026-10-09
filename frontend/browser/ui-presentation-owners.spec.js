const { test, expect } = require('@playwright/test');

const MARKERS=['DPToastContract','DPDownloads','DPProcessingPresentation','DPActivityLog','DPArchivePasswords'];
async function ready(page){await page.goto('/');await page.waitForFunction(markers=>markers.every(marker=>Boolean(window[marker])),MARKERS);}
// The Downloads owner replaces its rows after every list read (navigation, its measured-capacity
// re-checks, SSE/polling refreshes), which detaches any element a probe has already resolved and
// makes its rect read as zero. Refreshes are coalesced and run one fetch+render at a time, so once
// the owner's next list read (the only one carrying `offset`) is parked at the network boundary
// every earlier render has finished and none can run until the returned release() is called.
async function parkDownloadsList(page){
 let markParked,open;const parked=new Promise(resolve=>{markParked=resolve;}),gate=new Promise(resolve=>{open=resolve;});
 const isListRead=url=>url.pathname==='/api/torrents'&&url.searchParams.has('offset');
 const hold=async route=>{markParked();await gate;await route.fallback().catch(()=>{});};
 await page.route(isListRead,hold);
 await page.evaluate(()=>{loadTorrents().catch(()=>{});});
 await parked;
 return async()=>{open();await page.unroute(isListRead,hold).catch(()=>{});};
}

test('bounded presentation graph loads without retired correction requests',async({page})=>{
 const requests=[];page.on('request',request=>requests.push(new URL(request.url()).pathname));await ready(page);
 const state=await page.evaluate(markers=>({loader:typeof window.DPPresentationLoader,retired:[typeof window.DPUICorrectionBatch1,typeof window.DPUICorrectionBatch1Final,typeof window.DPUICorrectionP4Repair],markers:markers.map(marker=>Boolean(window[marker]))}),MARKERS);
 expect(state.markers.every(Boolean)).toBe(true);expect(state.loader).toBe('undefined');expect(state.retired).toEqual(['undefined','undefined','undefined']);expect(requests.some(path=>path.includes('ui-correction-batch1')||path.includes('ui-correction-p4-repair')||path.includes('ui-presentation-loader'))).toBe(false);
 const processing=requests.indexOf('/ui-processing-presentation.js'),dashboard=requests.indexOf('/ui-dashboard-transfer-presentation.js'),downloads=requests.indexOf('/ui-downloads.js');
 expect(processing).toBeGreaterThan(-1);expect(dashboard).toBeGreaterThan(processing);expect(downloads).toBeGreaterThan(processing);
});

test('Dashboard source-domain matching is boundary safe and pause presentation is projected',async({page})=>{
 await page.setViewportSize({width:1440,height:900});await ready(page);
 const assets=await page.evaluate(()=>({exact:DPTransferSourcePresentation.hostAsset('rapidgator.net'),sub:DPTransferSourcePresentation.hostAsset('cdn.rapidgator.net'),boundary:DPTransferSourcePresentation.hostAsset('notrapidgator.net')}));
 expect(assets).toEqual({exact:'/icons/hosts/rapidgator.png',sub:'/icons/hosts/rapidgator.png',boundary:''});
 await page.evaluate(()=>{processingPaused=true;renderTopbarActions();});
 await expect(page.locator('.dp-global-pause-center')).toHaveClass(/is-visible/);await expect(page.locator('.dp-global-pause-center')).toContainText('PROCESSING PAUSED');await expect(page.locator('#btn-import-existing')).toHaveCount(0);
 expect(await page.evaluate(()=>Object.prototype.hasOwnProperty.call(settingsData,'paused'))).toBe(false);
});

test('Dashboard Recent Activity keeps fixed row geometry across host artwork and progress states',async({page})=>{
 await page.setViewportSize({width:1440,height:900});
 const items=Array.from({length:6},(_,index)=>({
  id:index+1,name:`Layout transfer ${index+1}`,status:index===0?'downloading':'completed',progress:index===0?42:100,size_bytes:1048576*(index+1),created_at:'2026-09-08 17:00:00',
  current_source_identity:{kind:'host',host:'rapidgator.net'},current_provider_id:'alldebrid',current_provider_name:'AllDebrid',delivering_provider_id:'alldebrid',delivering_provider_name:'AllDebrid',active_provider_id:'alldebrid',active_provider_name:'AllDebrid',provider_provenance_status:'known'
 }));
 await page.route('**/api/torrents*',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items,total:items.length,page:1,page_size:items.length})}));
 await ready(page);await page.evaluate(async()=>{await loadRecent();});await expect(page.locator('#dash-tbody tr')).toHaveCount(6);await expect(page.locator('#dash-tbody .dp-source-host-logo')).toHaveCount(6);
 await expect.poll(()=>page.locator('#dash-tbody .dp-source-host-logo').evaluateAll(nodes=>nodes.every(node=>node.complete&&node.naturalWidth>0))).toBe(true);
 const geometry=await page.locator('#dash-tbody').evaluate(body=>{
  const rows=[...body.querySelectorAll('tr')];
  const px=value=>Number.parseFloat(value)||0;
  return rows.map(row=>{
   const slot=row.querySelector('.dash-row-bar-slot'),bar=row.querySelector('.dash-row-bar'),meta=row.querySelector('.dp-transfer-provider-meta'),icon=row.querySelector('.dp-source-icon-slot'),host=row.querySelector('.dp-source-host-logo');
   const slotStyle=getComputedStyle(slot),metaStyle=getComputedStyle(meta);
   return {status:row.dataset.status,rowHeight:row.getBoundingClientRect().height,slotHeight:slot.getBoundingClientRect().height,slotMarginTop:px(slotStyle.marginTop),slotMarginBottom:px(slotStyle.marginBottom),slotFootprint:slot.getBoundingClientRect().height+px(slotStyle.marginTop)+px(slotStyle.marginBottom),metaMarginTop:px(metaStyle.marginTop),iconHeight:icon.getBoundingClientRect().height,hostHeight:host.getBoundingClientRect().height,barVisibility:getComputedStyle(bar).visibility};
  });
 });
 const heights=geometry.map(row=>row.rowHeight);expect(Math.max(...heights)-Math.min(...heights)).toBeLessThanOrEqual(0.5);expect(Math.max(...heights)).toBeLessThanOrEqual(58);
 for(const row of geometry){expect(row.slotHeight).toBeCloseTo(3,1);expect(row.slotMarginTop).toBeCloseTo(0,1);expect(row.slotMarginBottom).toBeCloseTo(2,1);expect(row.slotFootprint).toBeCloseTo(5,1);expect(row.metaMarginTop).toBeCloseTo(0,1);expect(row.iconHeight).toBeCloseTo(20,1);expect(row.hostHeight).toBeCloseTo(17,1);}
 expect(geometry.find(row=>row.status==='downloading').barVisibility).toBe('visible');expect(geometry.filter(row=>row.status==='completed').every(row=>row.barVisibility==='hidden')).toBe(true);
});

test('Downloads provider/source block adds host artwork and centers its two lines without moving the block',async({page})=>{
 await page.setViewportSize({width:1440,height:800});
 const item={id:77,name:'Rapidgator layout transfer',status:'paused',presentation_status:'paused',progress:18,size_bytes:7340032,created_at:'2026-09-08 18:00:00',source:'direct_link',hash:'request:layout',current_source_identity:{kind:'host',host:'rapidgator.net'},current_provider_id:'alldebrid',current_provider_name:'AllDebrid',delivering_provider_id:null,delivering_provider_name:null,active_provider_id:'alldebrid',active_provider_name:'AllDebrid',provider_provenance_status:'known'};
 await page.route('**/api/torrents*',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items:[item],total:1,page:1,page_size:1})}));
 await ready(page);await page.evaluate(async()=>{nav(document.querySelector('[data-view="torrents"]'));await loadTorrents();});
 const release=await parkDownloadsList(page);
 const row=page.locator('#t-tbody tr[data-torrent-id="77"]');await expect(row).toHaveCount(1);await expect(row.locator('.dp-downloads-provider-block')).toHaveCount(1);await expect(row.locator('.dp-downloads-provider-line .dp-source-host-logo')).toHaveCount(1);await expect(row.locator('.dp-downloads-provider-line .dp-provider-chip')).toContainText('AllDebrid');await expect(row.locator('.dp-downloads-provider-block > .dp-transfer-source-label')).toHaveText('Direct link');
 await expect.poll(()=>row.locator('.dp-source-host-logo').evaluate(node=>node.complete&&node.naturalWidth>0)).toBe(true);
 const geometry=await row.locator('.dp-downloads-provider-cell').evaluate(cell=>{const block=cell.querySelector('.dp-downloads-provider-block'),line=block.querySelector('.dp-downloads-provider-line'),label=block.querySelector('.dp-transfer-source-label'),icon=line.querySelector('.dp-source-icon-slot'),chip=line.querySelector('.dp-provider-chip'),cellStyle=getComputedStyle(cell),rect=node=>node.getBoundingClientRect(),center=node=>{const r=rect(node);return r.left+r.width/2;};return{cellLeft:rect(cell).left,paddingLeft:Number.parseFloat(cellStyle.paddingLeft)||0,blockLeft:rect(block).left,lineCenter:center(line),labelCenter:center(label),iconHeight:rect(icon).height,chipHeight:rect(chip).height};});
 await release();
 expect(Math.abs(geometry.blockLeft-(geometry.cellLeft+geometry.paddingLeft))).toBeLessThanOrEqual(1.5);expect(Math.abs(geometry.lineCenter-geometry.labelCenter)).toBeLessThanOrEqual(1.5);expect(geometry.iconHeight).toBeCloseTo(20,1);expect(geometry.chipHeight).toBeGreaterThanOrEqual(19.5);
});

test('Downloads owner exposes fixed three-slot pager and date options',async({page})=>{
 await page.setViewportSize({width:1440,height:800});
 // Drive pagination entirely through observable application behavior: mock
 // a 60-item collection (three pages at the default page size) and click
 // the real Next control to land on a middle page with both nav buttons
 // visible, rather than calling the internal render function directly.
 await page.route('**/api/torrents*',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items:[],total:60,page:1,page_size:25})}));
 await ready(page);await page.evaluate(async()=>{nav(document.querySelector('[data-view="torrents"]'));await loadTorrents();});
 await page.locator('#torrent-page-btns .dp-pager-btn[aria-label="Next page"]').click();
 await expect(page.locator('#torrent-page-btns .dp-pager-btn')).toHaveCount(2);await expect(page.locator('#torrent-page-btns .dp-pager-current')).toHaveCount(1);
 await expect(page.locator('#torrent-page-btns .dp-pager-current')).toHaveText('2');
 const release=await parkDownloadsList(page);
 const group=await page.locator('#torrent-page-btns').boundingBox(),current=await page.locator('.dp-pager-current').boundingBox();await release();expect(Math.abs(group.width-116)).toBeLessThanOrEqual(1);expect(Math.abs(current.width-36)).toBeLessThanOrEqual(1);
 const trigger=page.locator('#view-torrents .dp-date-menu-trigger');await trigger.click();for(const name of ['Friendly','US','International','ISO','24-hour','12-hour'])await expect(page.locator('#view-torrents').getByRole('menuitemradio',{name})).toBeVisible();
});

/* Dashboard Recent Activity's Date Added heading carries the SAME date menu,
   formatter and persisted preference as the Downloads Date heading: a choice
   in either list redraws both, survives a reload, and changes only how the
   original added timestamp reads -- never which rows show or their order. */
test('Recent Activity Date Added shares the Downloads date preference, formatter and menu',async({page})=>{
 await page.setViewportSize({width:1440,height:1000});
 await page.route('https://fonts.googleapis.com/**',route=>route.fulfill({status:200,contentType:'text/css',body:''}));
 const row=(id,created_at)=>({id,name:`Dated ${id}`,status:'completed',presentation_status:'completed',progress:100,size_bytes:1,created_at,current_source_identity:{kind:'link'},providers:[],historical_providers:[],delivering_provider_ids:[]});
 const items=[row(7703,'2026-03-04T05:06:00Z'),row(7701,'2025-12-31T23:59:00Z'),row(7702,'2024-07-08T12:30:00Z')];
 await page.route(url=>url.pathname==='/api/torrents',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items,total:items.length})}));
 await ready(page);await page.evaluate(()=>localStorage.removeItem('debridpulse.downloads.date-presentation.v1'));await ready(page);
 const recent=page.locator('#view-dashboard .dash-activity-table-wrap');
 await expect(recent.locator('#dash-tbody tr[data-torrent-id="7702"]')).toBeVisible();
 const heading=recent.locator('thead th:has(.dp-date-menu-trigger)');
 await expect(heading).toHaveCount(1);await expect(heading.locator('span').first()).toHaveText('Date Added');
 await expect(heading).toHaveCSS('text-transform','uppercase');
 const cells=scope=>page.locator(`${scope} tr[data-torrent-id]`).evaluateAll(rows=>rows.map(tr=>{const v=tr.querySelector('.dp-downloads-date-value');return {id:tr.dataset.torrentId,text:v?.textContent,exact:v?.getAttribute('title')};}));
 const before=await cells('#dash-tbody');
 expect(before.map(c=>c.id)).toEqual(['7703','7701','7702']);
 // Keyboard: the trigger opens the shared menu with the current choices checked.
 const trigger=heading.locator('.dp-date-menu-trigger');
 await expect(trigger).toHaveAttribute('aria-haspopup','menu');
 await trigger.focus();await page.keyboard.press('Enter');
 await expect(trigger).toHaveAttribute('aria-expanded','true');
 const menu=heading.locator('.dp-date-menu[role="menu"]');
 for(const name of ['Friendly','US','International','ISO','24-hour','12-hour'])await expect(menu.getByRole('menuitemradio',{name})).toBeVisible();
 await expect(menu.getByRole('menuitemradio',{name:'Friendly'})).toHaveAttribute('aria-checked','true');
 await expect(menu.getByRole('menuitemradio',{name:'24-hour'})).toHaveAttribute('aria-checked','true');
 await page.keyboard.press('Tab');await expect(menu.getByRole('menuitemradio',{name:'Friendly'})).toBeFocused();
 await page.keyboard.press('Escape');await expect(menu).toBeHidden();await expect(trigger).toBeFocused();
 // Choosing ISO in Recent redraws Recent AND Downloads, and checks it in both menus.
 await trigger.click();await menu.getByRole('menuitemradio',{name:'ISO'}).click();await expect(menu).toBeHidden();
 await expect.poll(async()=>(await cells('#dash-tbody')).every(c=>/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(c.text))).toBe(true);
 await expect.poll(async()=>(await cells('#t-tbody')).every(c=>/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(c.text))).toBe(true);
 const iso=await cells('#dash-tbody');
 expect(iso.map(c=>c.id)).toEqual(['7703','7701','7702']);
 expect(iso.map(c=>c.exact)).toEqual(before.map(c=>c.exact));
 expect(Object.fromEntries((await cells('#t-tbody')).map(c=>[c.id,c.text]))).toEqual(Object.fromEntries(iso.map(c=>[c.id,c.text])));
 await expect(page.locator('#view-torrents .dp-date-menu [data-date-format="iso"]')).toHaveAttribute('aria-checked','true');
 expect(await page.evaluate(()=>JSON.parse(localStorage.getItem('debridpulse.downloads.date-presentation.v1')))).toEqual({format:'iso',hour12:false});
 // It persists across a reload, in both lists.
 await ready(page);await expect(page.locator('#dash-tbody tr[data-torrent-id="7702"]')).toBeVisible();
 expect((await cells('#dash-tbody')).map(c=>c.text)).toEqual(iso.map(c=>c.text));
 // Choosing 12-hour in Downloads redraws Recent too.
 await page.evaluate(async()=>{nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]'));await loadTorrents();});
 const downloadsMenu=page.locator('#view-torrents .dp-date-menu');
 await page.locator('#view-torrents .dp-date-menu-trigger').click();
 await expect(downloadsMenu.getByRole('menuitemradio',{name:'ISO'})).toHaveAttribute('aria-checked','true');
 await downloadsMenu.getByRole('menuitemradio',{name:'12-hour'}).click();
 await expect.poll(async()=>(await cells('#dash-tbody')).every(c=>/^\d{4}-\d{2}-\d{2} \d{2}:\d{2} (AM|PM)$/.test(c.text))).toBe(true);
 await expect(page.locator('#view-dashboard .dp-date-menu [data-hour12="true"]')).toHaveAttribute('aria-checked','true');
 // The Date Added column holds its heading and trigger without spilling into Action.
 await page.evaluate(()=>nav(document.querySelector('#sidebar .nav-item[data-view="dashboard"]')));
 const fit=await heading.evaluate(th=>{const t=th.getBoundingClientRect(),b=th.querySelector('.dp-date-menu-trigger').getBoundingClientRect(),hit=document.elementFromPoint(b.left+b.width/2,b.top+b.height/2);return {inside:b.right<=t.right,hit:th.contains(hit)};});
 expect(fit).toEqual({inside:true,hit:true});
});

test('Downloads pager: a stale in-flight refresh cannot overwrite a newer page click (race regression)',async({page})=>{
 await page.setViewportSize({width:1440,height:800});
 // Deterministically reproduce the exact-SHA Browser Runtime race: a plain
 // page-1 refresh is in flight (held unresolved here) when the user clicks
 // Next through the real control. The stale page-1 response must be
 // discarded as non-authoritative rather than reverting the newer page-2
 // intent -- see ui-downloads.js's requestGeneration mechanism. Only the
 // real bounded list GET is intercepted (mirrors downloads-large-history.spec.js);
 // every other /api/torrents* call (candidate refresh, dashboard recent,
 // SSE-driven housekeeping, etc.) falls through to the real local backend so
 // it cannot pollute this test's determinism.
 let page1Count=0,releaseHeld=null;
 const offsets=[];
 await page.route('**/api/torrents**',async route=>{
  const request=route.request();
  const url=new URL(request.url());
  if(url.pathname!=='/api/torrents'||request.method()!=='GET'||!url.searchParams.has('offset')){return route.fallback();}
  const offset=url.searchParams.get('offset');
  offsets.push(offset);
  if(offset==='0'){
   page1Count+=1;
   // Hold the *second* page-1 request unresolved, regardless of whether it
   // was triggered by our own deliberate refresh below or by the owner's
   // own capacity/navigation lifecycle -- either is a legitimate instance
   // of "a page-1 refresh is already running" per the fix-forward task.
   if(page1Count===2){await new Promise(resolve=>{releaseHeld=resolve;});}
  }
  await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items:[],total:60,page:1,page_size:25})});
 });
 await ready(page);
 await page.evaluate(()=>{nav(document.querySelector('[data-view="torrents"]'));});
 await expect(page.locator('#torrent-page-btns .dp-pager-btn[aria-label="Next page"]')).toBeVisible();

 // Start a second page-1 refresh; the route handler above holds it unresolved.
 await page.evaluate(()=>{loadTorrents();});
 await expect.poll(()=>page1Count).toBeGreaterThanOrEqual(2);
 await expect.poll(()=>typeof releaseHeld).toBe('function');

 // While that stale refresh is still in flight, drive the real Next control.
 await page.locator('#torrent-page-btns .dp-pager-btn[aria-label="Next page"]').click();

 // Release the held, now-stale page-1 response.
 releaseHeld();

 // The trailing/current request (coalesced behind the held one) must win and
 // render page 2 -- the stale page-1 payload must never resurface page 1.
 await expect(page.locator('#torrent-page-btns .dp-pager-current')).toHaveText('2');
 await expect(page.locator('#torrent-page-btns .dp-pager-btn')).toHaveCount(2);
 await expect(page.locator('#torrent-page-btns .dp-pager-btn[aria-label="Previous page"]')).toBeVisible();
 await expect(page.locator('#torrent-page-btns .dp-pager-btn[aria-label="Next page"]')).toBeVisible();
 // The winning request must carry the page-2 offset (25 for a 25-item page
 // size) -- proof the fetch itself, not just the render, used the newer
 // intent.
 await expect.poll(()=>offsets[offsets.length-1]).toBe('25');
 // Give any further stale-tail activity a moment, then confirm page 2 held
 // -- the late page-1 response must not revert it.
 await page.waitForTimeout(150);
 await expect(page.locator('#torrent-page-btns .dp-pager-current')).toHaveText('2');
});

test('Downloads pager: a shrink-triggered clamp refetches the clamped page instead of rendering the out-of-range payload',async({page})=>{
 await page.setViewportSize({width:1440,height:800});
 // Real rows (unlike the empty-item fixtures used elsewhere in this file)
 // participate in measuredSize()/applySize() capacity auto-fit, so the
 // effective page size is whatever the owner settles on for this viewport --
 // never assumed. 300 rows guarantees several pages regardless of that size.
 // Every row carries an identifiable id so assertions can prove *which*
 // page's data actually rendered, not just that the pager label changed.
 function transfer(id){return{id,name:`Historical Transfer ${id}`,hash:`hash-${id}-0123456789abcdef`,status:'completed',progress:100,size_bytes:1024*1024*id,created_at:'2026-08-01T12:00:00Z',source:'direct_link',label:null,current_provider_id:'general_http',current_provider_name:'HTTP & HTTPS',delivering_provider_id:'general_http',delivering_provider_name:'HTTP & HTTPS',active_provider_id:'general_http',active_provider_name:'HTTP & HTTPS',provider_provenance_status:'known'};}
 let downloads=Array.from({length:300},(_,i)=>transfer(i+1));
 const requests=[];
 await page.route('**/api/torrents**',async route=>{
  const request=route.request();const url=new URL(request.url());
  if(url.pathname!=='/api/torrents'||request.method()!=='GET'||!url.searchParams.has('offset')){return route.fallback();}
  const limit=Math.max(1,Number(url.searchParams.get('limit'))||25),offset=Math.max(0,Number(url.searchParams.get('offset'))||0);
  requests.push({limit,offset});
  await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items:downloads.slice(offset,offset+limit),total:downloads.length})});
 });
 async function settle(){
  let last=-1;
  while(last!==requests.length){last=requests.length;await page.waitForTimeout(300);}
 }
 await ready(page);
 await page.evaluate(()=>{nav(document.querySelector('[data-view="torrents"]'));});
 await expect(page.locator('.dp-downloads-detail-row').first()).toBeVisible();
 // Let any capacity-driven auto-fit corrective fetch settle before treating
 // the effective page size as fixed.
 await settle();
 const effectiveLimit=requests[requests.length-1].limit;
 expect(effectiveLimit).toBeGreaterThan(0);

 // Navigate to page 3 through the real UI (two real Next clicks).
 await page.locator('#torrent-page-btns .dp-pager-btn[aria-label="Next page"]').click();
 await expect(page.locator('#torrent-page-btns .dp-pager-current')).toHaveText('2');
 await page.locator('#torrent-page-btns .dp-pager-btn[aria-label="Next page"]').click();
 await expect(page.locator('#torrent-page-btns .dp-pager-current')).toHaveText('3');
 const page3FirstId=await page.locator('.dp-downloads-detail-row').first().getAttribute('data-torrent-id');
 expect(page3FirstId).toBeTruthy();

 // Shrink the collection to fewer than one page's worth -- the requested
 // page-3 offset becomes out of range regardless of the effective limit.
 downloads=downloads.slice(0,Math.max(1,Math.min(3,effectiveLimit-1)));
 const beforeRefresh=requests.length;

 // Trigger a normal refresh through the real Refresh control -- not a
 // test-only setter -- while the application's authoritative state is still
 // "page 3" (now out of range for the shrunk collection).
 await page.locator('.dp-downloads-refresh').click();

 // The application must issue a fresh request for the clamped page (offset
 // 0), not merely relabel the out-of-range response.
 await expect.poll(()=>requests.length).toBeGreaterThan(beforeRefresh);
 await expect.poll(()=>requests[requests.length-1]?.offset).toBe(0);

 await expect(page.locator('#torrent-page-btns .dp-pager-current')).toHaveText('1');
 // Only one slot: the shrunk total fits in one page, so both nav buttons are absent.
 await expect(page.locator('#torrent-page-btns .dp-pager-btn')).toHaveCount(0);
 // Real, correct page-1 rows -- not the empty/out-of-range page-3 payload.
 await expect(page.locator('.dp-downloads-detail-row')).toHaveCount(downloads.length);
 await expect(page.locator(`.dp-downloads-detail-row[data-torrent-id="${downloads[0].id}"]`)).toBeVisible();
 await expect(page.locator(`.dp-downloads-detail-row[data-torrent-id="${page3FirstId}"]`)).toHaveCount(0);
});

test('Activity Log filter interaction reaches server with filter metadata',async({page})=>{
 const requests=[];await page.route('**/api/events*',async route=>{const url=new URL(route.request().url());requests.push(Object.fromEntries(url.searchParams.entries()));await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items:[{id:1,severity:'warning',category:'routing',type:'routing.request_failed',message:'Retry delayed',name:'example.iso',transfer_id:4,occurred_at:'2026-09-06T08:30:00Z'}],limit:100,snapshot:1,has_more:false,next_before:null,newer_available:false,search:{text:'indexed',pending:0,complete:true}})});});
 await ready(page);await page.evaluate(()=>nav(document.querySelector('[data-view="events"]')));await page.locator('#ev-timeframe').selectOption('72h');await page.locator('#ev-level').selectOption('warning');await page.locator('#ev-search').fill('Retry');await page.waitForTimeout(325);await expect.poll(()=>requests.length).toBeGreaterThan(0);
 const filtered=requests.find(item=>item.search==='Retry');expect(filtered).toEqual({limit:'100',timeframe:'72h',level:'warning',search:'Retry'});await expect(page.locator('#ev-reset')).toBeVisible();
});

test('Archive Password owner uses click reveal and line-aware editing',async({page})=>{
 await page.route('**/api/settings/extraction-passwords',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({passwords:'alpha\nbeta'})}));await ready(page);await page.evaluate(async()=>{nav(document.querySelector('[data-view="settings"]'));await loadSettings();});
 const tab=page.locator('#view-settings [data-tab="extraction"]');if(await tab.count())await tab.click();await page.waitForFunction(()=>document.querySelectorAll('.dp-settings-extraction-password-editor .dp-settings-password-line').length>=3);
 const editor=page.locator('.dp-settings-extraction-password-editor'),eye=editor.locator('.dp-settings-password-eye'),source=page.locator('#view-settings [data-panel="extraction"] [data-setting="extraction_password"]');await expect(eye).toContainText('Show all');await eye.click();await expect(eye).toContainText('Hide all');await expect(editor.locator('.dp-settings-password-line').first()).toHaveAttribute('type','text');await eye.click();const first=editor.locator('.dp-settings-password-line').first();await first.focus();await first.fill('changed');await first.press('Escape');await expect(first).toHaveValue('•••••');await expect(source).toHaveValue('alpha\nbeta');await first.focus();await expect(first).toHaveValue('alpha');
});

test('toast bridge preserves reviewed copy and automatic lifetime',async({page})=>{
 await ready(page);expect(await page.evaluate(()=>DPToastDuration('DebridPulse stared at that for a moment. It is not a link, magnet, or torrent.'))).toBe(3750);await page.evaluate(()=>toast('Line 1: enter an HTTP(S) link or magnet URI','info'));const node=page.locator('#toasts .toast').last();await expect(node).toContainText('DebridPulse stared at that for a moment. It is not a link, magnet, or torrent.');await expect(node.locator('button,.dp-toast-close,.dp-toast-dismiss')).toHaveCount(0);
});

test('Activity Log has one owner: explicit API, no compatibility globals, controls bound once',async({page})=>{
 await ready(page);
 const shape=await page.evaluate(()=>({
  loadEvents:typeof window.loadEvents,filterEvents:typeof window.filterEvents,
  api:Object.keys(window.DPActivityLog).sort(),frozen:Object.isFrozen(window.DPActivityLog),
  inline:['ev-search','ev-level','ev-timeframe'].map(id=>document.getElementById(id)?.getAttribute('oninput')||document.getElementById(id)?.getAttribute('onchange')||null),
  fields:document.querySelectorAll('#view-events .dp-activity-search-row > *').length,
 }));
 expect(shape).toEqual({loadEvents:'undefined',filterEvents:'undefined',api:['formatTimestamp','load'],frozen:true,inline:[null,null,null],fields:5});
});

test('Activity Log refresh, reset, empty state and single render',async({page})=>{
 await ready(page);
 const requests=[];
 await page.route('**/api/events*',route=>{const url=new URL(route.request().url());requests.push(Object.fromEntries(url.searchParams));
  const items=url.searchParams.get('search')==='nothing'?[]:[{id:2,severity:'info',category:'transfer',type:'transfer.accepted',message:'Started',name:'Alpha',transfer_id:1,occurred_at:'2026-09-08T17:00:00Z'},{id:1,severity:'warning',category:'execution',type:'execution.attempt_failed',message:'Slow',occurred_at:'2026-09-08T17:01:00Z'}];
  return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items,limit:100,snapshot:2,has_more:false,next_before:null,newer_available:false,search:{text:'none',pending:0,complete:true}})});});
 await page.evaluate(()=>nav(document.querySelector('[data-view="events"]')));
 await expect(page.locator('#event-list .dp-activity-row')).toHaveCount(2);
 expect(requests.at(-1)).toEqual({limit:'100',timeframe:'all'});
 await page.evaluate(()=>{window.__renders=0;document.addEventListener('debridpulse:activity-rendered',()=>window.__renders++);});
 await page.locator('.dp-activity-refresh').click();
 await expect.poll(()=>page.evaluate(()=>window.__renders)).toBe(1);
 await expect(page.locator('#event-list .dp-activity-row')).toHaveCount(2);
 await expect(page.locator('#ev-reset')).toBeHidden();
 await page.locator('#ev-search').fill('nothing');
 await expect(page.locator('#event-list .empty')).toHaveText('No events match your filters.');
 await expect(page.locator('#ev-reset')).toBeVisible();
 await page.locator('#ev-reset').click();
 await expect(page.locator('#event-list .dp-activity-row')).toHaveCount(2);
 await expect(page.locator('#ev-search')).toHaveValue('');
 await expect(page.locator('#ev-reset')).toBeHidden();
 expect(await page.evaluate(()=>DPActivityLog.formatTimestamp('2026-09-08 17:00:00'))).toMatch(/Sep 8, 2026/);
});

test('Activity Log pages through history by cursor and never jumps off a historical page',async({page})=>{
 await ready(page);
 const requests=[];let total=260;
 await page.route('**/api/events*',route=>{const url=new URL(route.request().url());const q=Object.fromEntries(url.searchParams);requests.push(q);
  if(q.category==='storage')return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'The Activity Log search index is being rebuilt. Try the search again shortly.'})});
  const snapshot=q.snapshot?Number(q.snapshot):total,limit=Number(q.limit),top=q.before?Number(q.before)-1:snapshot;
  const items=[];for(let id=top;id>0&&items.length<limit;id--)items.push({id,severity:id%7?'info':'error',category:'transfer',type:'transfer.synthetic',message:`Event ${id}`,name:`transfer-${id}`,transfer_id:id,detail:id===snapshot?'Safe diagnostic detail':null,occurred_at:'2026-10-09T12:00:00Z'});
  const last=items.at(-1)?.id??0;
  return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items,limit,snapshot,has_more:last>1,next_before:last>1?last:null,newer_available:total>snapshot,search:{text:'none',pending:0,complete:true}})});});
 await page.evaluate(()=>{settingsData.activity_log_page_size=100;nav(document.querySelector('[data-view="events"]'));});
 const rows=page.locator('#event-list .dp-activity-row');
 await expect(rows).toHaveCount(100);await expect(rows.first().locator('.dp-activity-message')).toHaveText('Event 260');
 await expect(page.locator('#dp-activity-page-info')).toHaveText('Page 1');await expect(page.locator('#ev-newer')).toBeDisabled();
 // Safe detail is disclosed on demand, never inline.
 const detail=rows.first().locator('details.dp-activity-detail');await expect(detail).not.toHaveAttribute('open','');await detail.locator('summary').click();await expect(detail).toContainText('Safe diagnostic detail');
 total=300; // events recorded during the investigation
 await page.locator('#ev-older').focus();await page.keyboard.press('Enter');
 await expect(rows.first().locator('.dp-activity-message')).toHaveText('Event 160');await expect(page.locator('#dp-activity-page-info')).toHaveText('Page 2');
 expect(requests.at(-1)).toMatchObject({limit:'100',snapshot:'260',before:'161'});
 await expect(page.locator('#ev-newest')).toBeVisible(); // newer events are announced, not forced on the operator
 await page.locator('#ev-older').click();await expect(rows).toHaveCount(60);await expect(page.locator('#ev-older')).toBeDisabled();
 await page.locator('#ev-newer').click();await page.locator('#ev-newer').click();
 await expect(rows.first().locator('.dp-activity-message')).toHaveText('Event 260');await expect(page.locator('#ev-newer')).toBeDisabled();
 await page.locator('#ev-newest').click();await expect(rows.first().locator('.dp-activity-message')).toHaveText('Event 300');
 // A filter change starts a new investigation; a failure is shown as a failure, not as an empty history.
 await page.locator('#ev-category').selectOption('storage');
 await expect(page.locator('#event-list .empty')).toContainText('Events could not be loaded');await expect(page.locator('#dp-activity-pagination')).toBeHidden();
 expect(requests.at(-1).snapshot).toBeUndefined();expect(requests.at(-1).before).toBeUndefined();
});

test('Activity Log never presents an incomplete search as "no matches"',async({page})=>{
 await ready(page);
 let search={text:'indexed',pending:12,complete:false};
 await page.route('**/api/events*',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items:[],limit:100,snapshot:40,has_more:false,next_before:null,newer_available:false,search})}));
 await page.evaluate(()=>nav(document.querySelector('[data-view="events"]')));
 await page.locator('#ev-search').fill('needle');
 await expect(page.locator('#event-list .empty')).toHaveText('No matches in the searchable history yet. Recent events are still being indexed and may match; try again shortly.');
 await expect(page.locator('#dp-activity-result-note')).toContainText('still catching up with 12 recent event(s)');
 search={text:'unavailable',pending:0,complete:false};
 await page.locator('#ev-search').fill('#4242');
 await expect(page.locator('#event-list .empty')).toHaveText('No events for this transfer ID. Text search is unavailable on this installation, so text matches could not be checked.');
 search={text:'indexed',pending:0,complete:true};
 await page.locator('#ev-search').fill('nothing');
 await expect(page.locator('#event-list .empty')).toHaveText('No events match your filters.');
});
