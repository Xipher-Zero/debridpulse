const { test, expect } = require('@playwright/test');

const MARKERS=['DPActivityLog','DPArchivePasswords','DPDownloads'];
async function ready(page){await page.goto('/');await page.waitForFunction(markers=>markers.every(marker=>Boolean(window[marker])),MARKERS);}

test('Activity Log keeps search, Time Window dropdown, Severity label, and Severity dropdown in order',async({page})=>{
 await page.setViewportSize({width:1600,height:900});
 await page.route('**/api/events*',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items:[],truncated:false,limit:500})}));
 await ready(page);await page.evaluate(()=>nav(document.querySelector('[data-view="events"]')));
 await page.waitForFunction(()=>document.querySelector('#ev-timeframe')?._dpDropdownShell&&document.querySelector('#ev-level')?._dpDropdownShell);
 const geometry=await page.locator('#view-events .dp-activity-search-row').evaluate(row=>{
  const search=row.querySelector('#ev-search');
  const time=row.querySelector('.dp-activity-filter-field--time');
  const severity=row.querySelector('.dp-activity-filter-field--severity');
  const timeLabel=time.querySelector('.dp-activity-filter-label');
  const timeShell=time.querySelector('.dp-dropdown-shell');
  const severityLabel=severity.querySelector('.dp-activity-filter-label');
  const severityShell=severity.querySelector('.dp-dropdown-shell');
  const span=node=>{const r=node.getBoundingClientRect();return{left:r.left,right:r.right,width:r.width,height:r.height};};
  return {
   children:[...row.children].map(node=>node.id||node.className),
   search:span(search),timeLabel:span(timeLabel),timeShell:span(timeShell),severityLabel:span(severityLabel),severityShell:span(severityShell)
  };
 });
 expect(geometry.children[0]).toBe('ev-search');
 expect(geometry.children[1]).toContain('dp-activity-filter-field--time');
 expect(geometry.children[2]).toContain('dp-activity-filter-field--severity');
 expect(geometry.search.right).toBeLessThanOrEqual(geometry.timeLabel.left+1);
 expect(geometry.timeLabel.right).toBeLessThanOrEqual(geometry.timeShell.left+1);
 expect(geometry.timeShell.right).toBeLessThanOrEqual(geometry.severityLabel.left+1);
 expect(geometry.severityLabel.right).toBeLessThanOrEqual(geometry.severityShell.left+1);
});

test('Activity Log search keeps focus across debounced refreshes and other controls still initialize exactly once',async({page})=>{
 await page.setViewportSize({width:1600,height:900});
 let requestCount=0;
 await page.route('**/api/events*',route=>{requestCount+=1;return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items:[],truncated:false,limit:500})});});
 await ready(page);await page.evaluate(()=>nav(document.querySelector('[data-view="events"]')));
 await page.waitForFunction(()=>document.querySelector('#ev-timeframe')?._dpDropdownShell&&document.querySelector('#ev-level')?._dpDropdownShell);
 const search=page.locator('#ev-search');
 await search.click();
 await page.keyboard.type('a');
 await page.waitForTimeout(400);
 await expect.poll(()=>page.evaluate(()=>document.activeElement?.id)).toBe('ev-search');
 await page.keyboard.type('bc',{delay:150});
 await page.waitForTimeout(400);
 await expect.poll(()=>page.evaluate(()=>document.activeElement?.id)).toBe('ev-search');
 await expect(search).toHaveValue('abc');
 await expect.poll(()=>requestCount).toBeGreaterThanOrEqual(3);
 // Severity/timeframe/reset controls initialize exactly once (one dropdown
 // shell each) and remain interactive after the repeated debounced refreshes.
 const shellCount=await page.evaluate(()=>document.querySelectorAll('#view-events .dp-dropdown-shell').length);
 expect(shellCount).toBe(2);
 await page.selectOption('#ev-level','error');
 await expect.poll(()=>requestCount).toBeGreaterThanOrEqual(4);
});

test('Archive Passwords hydrate stored values and Show all/Hide all keep identical geometry',async({page})=>{
 await page.route('**/api/settings/extraction-passwords',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({passwords:'alpha\nbeta'})}));
 await ready(page);await page.evaluate(async()=>{nav(document.querySelector('[data-view="settings"]'));await loadSettings();});
 const tab=page.locator('#view-settings [data-tab="extraction"]');if(await tab.count())await tab.click();
 const source=page.locator('#view-settings [data-panel="extraction"] [data-setting="extraction_password"]');
 await expect.poll(()=>source.inputValue()).toBe('alpha\nbeta');
 const editor=page.locator('.dp-settings-extraction-password-editor');
 const rows=editor.locator('.dp-settings-password-line');await expect(rows).toHaveCount(3);
 await expect(rows.nth(0)).toHaveAttribute('type','text');await expect(rows.nth(1)).toHaveAttribute('type','text');
 const eye=editor.locator('.dp-settings-password-eye');await expect(eye).toContainText('Show all');
 const measure=()=>eye.evaluate(button=>{const label=button.querySelector('.dp-settings-password-eye-label'),bs=getComputedStyle(button),ls=getComputedStyle(label),br=button.getBoundingClientRect(),lr=label.getBoundingClientRect();return{text:label.textContent,width:br.width,height:br.height,labelHeight:lr.height,fontFamily:ls.fontFamily,fontSize:ls.fontSize,fontWeight:ls.fontWeight,lineHeight:ls.lineHeight,whiteSpace:ls.whiteSpace,buttonWhiteSpace:bs.whiteSpace};});
 const show=await measure();await eye.click();await expect(eye).toContainText('Hide all');await expect(rows.nth(0)).toHaveValue('alpha');await expect(rows.nth(1)).toHaveValue('beta');await expect(rows.nth(2)).toHaveValue('');const hide=await measure();
 expect(show.whiteSpace).toBe('nowrap');expect(show.buttonWhiteSpace).toBe('nowrap');expect(show.labelHeight).toBeLessThan(20);
 expect(hide.whiteSpace).toBe('nowrap');expect(hide.buttonWhiteSpace).toBe('nowrap');expect(hide.labelHeight).toBeLessThan(20);
 expect({width:show.width,height:show.height,fontFamily:show.fontFamily,fontSize:show.fontSize,fontWeight:show.fontWeight,lineHeight:show.lineHeight}).toEqual({width:hide.width,height:hide.height,fontFamily:hide.fontFamily,fontSize:hide.fontSize,fontWeight:hide.fontWeight,lineHeight:hide.lineHeight});
});

test('Archive Passwords empty Backspace does not delete the row or navigate to the previous one',async({page})=>{
 await page.route('**/api/settings/extraction-passwords',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({passwords:'alpha\nbeta'})}));
 await ready(page);await page.evaluate(async()=>{nav(document.querySelector('[data-view="settings"]'));await loadSettings();});
 const tab=page.locator('#view-settings [data-tab="extraction"]');if(await tab.count())await tab.click();
 const source=page.locator('#view-settings [data-panel="extraction"] [data-setting="extraction_password"]');
 await expect.poll(()=>source.inputValue()).toBe('alpha\nbeta');
 const editor=page.locator('.dp-settings-extraction-password-editor');
 const rows=editor.locator('.dp-settings-password-line');await expect(rows).toHaveCount(3);
 // Row 2 (index 1, "beta") is already non-empty text -- normal Backspace on
 // real text must still edit it normally.
 await rows.nth(1).click();await rows.nth(1).press('End');await rows.nth(1).press('Backspace');
 await expect(rows.nth(1)).toHaveValue('bet');
 await expect(rows).toHaveCount(3);
 await expect.poll(()=>source.inputValue()).toBe('alpha\nbet');
 // Row 3 (index 2) is the trailing empty row. Backspace there used to delete
 // the row and jump focus back to row 2 -- it must now be a no-op.
 await rows.nth(2).click();
 await expect.poll(()=>page.evaluate(()=>document.activeElement?.dataset?.passwordIndex)).toBe('2');
 await rows.nth(2).press('Backspace');
 await expect(rows).toHaveCount(3);
 await expect.poll(()=>page.evaluate(()=>document.activeElement?.dataset?.passwordIndex)).toBe('2');
 expect(await source.inputValue()).toBe('alpha\nbet');
});

test('Theme toggle glyph shows the target theme, not the current one, on load and after toggling',async({page})=>{
 await ready(page);
 const initial=await page.evaluate(()=>({title:document.getElementById('theme-toggle').title,ariaLabel:document.getElementById('theme-toggle').getAttribute('aria-label'),hasCircle:!!document.querySelector('#theme-toggle svg circle'),isLight:document.body.classList.contains('light')}));
 expect(initial.isLight).toBe(false);
 expect(initial.title).toBe('Switch to light mode');
 expect(initial.ariaLabel).toBe('Switch to light mode');
 expect(initial.hasCircle).toBe(true); // sun glyph while dark
 await page.click('#theme-toggle');
 const afterLight=await page.evaluate(()=>({title:document.getElementById('theme-toggle').title,ariaLabel:document.getElementById('theme-toggle').getAttribute('aria-label'),hasCircle:!!document.querySelector('#theme-toggle svg circle'),isLight:document.body.classList.contains('light')}));
 expect(afterLight.isLight).toBe(true);
 expect(afterLight.title).toBe('Switch to dark mode');
 expect(afterLight.ariaLabel).toBe('Switch to dark mode');
 expect(afterLight.hasCircle).toBe(false); // moon glyph while light
 await page.click('#theme-toggle');
 const backToDark=await page.evaluate(()=>({title:document.getElementById('theme-toggle').title,hasCircle:!!document.querySelector('#theme-toggle svg circle'),isLight:document.body.classList.contains('light')}));
 expect(backToDark.isLight).toBe(false);
 expect(backToDark.title).toBe('Switch to light mode');
 expect(backToDark.hasCircle).toBe(true);
});

test('Downloads and Activity Log master-card titles change without touching sidebar, page header, or Statistics',async({page})=>{
 await ready(page);
 await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
 await expect(page.locator('.dp-downloads-heading')).toHaveText('On the Books');
 await expect(page.locator('#torrent-card-title')).toHaveAttribute('aria-label',/^On the Books\. /);
 await expect(page.locator('#sidebar .nav-item[data-view="torrents"] .nav-label')).toHaveText('Downloads');
 await expect(page.locator('#page-title')).toHaveText('Downloads');
 await page.locator('#sidebar .nav-item[data-view="events"]').click();
 await expect(page.locator('.dp-activity-heading')).toHaveText('For the Record');
 await expect(page.locator('#sidebar .nav-item[data-view="events"] .nav-label')).toHaveText('Activity Log');
 await expect(page.locator('#page-title')).toHaveText('Activity Log');
 await page.locator('#sidebar .nav-item[data-view="stats"]').click();
 await expect(page.locator('.dp-stats-heading')).toHaveText('By the Numbers');
});

test('Dashboard hero sparklines graph cumulative deltas for cumulative metrics and raw samples for instantaneous metrics',async({page})=>{
 await ready(page);
 const result=await page.evaluate(()=>{
  const cumulative=dashboardCumulativeDeltas([10,10,12,15]);
  const reset=dashboardCumulativeDeltas([15,17,3,5]);
  const kinds=Object.fromEntries(Object.values(DASHBOARD_HERO_METRICS).map(m=>[m.key,m.kind]));
  return {cumulative,reset,kinds};
 });
 expect(result.cumulative).toEqual([0,0,2,3]);
 expect(result.reset).toEqual([0,2,0,2]);
 expect(result.reset).not.toContain(-14);
 expect(result.kinds).toEqual({total:'cumulative',completed:'cumulative',active:'instantaneous',processing:'instantaneous',errors:'instantaneous',downloaded:'cumulative'});
});

test('Archive Passwords persist edits across Apply, rerender, navigation, and fresh reload',async({page})=>{
 let passwords='alpha\nbeta',saved=false,postSaveReads=0,releasePostSaveHydrate;
 const postSaveHydrateGate=new Promise(resolve=>{releasePostSaveHydrate=resolve;});
 const puts=[];
 await page.route('**/api/settings/extraction-passwords',async route=>{
  if(saved){postSaveReads+=1;await postSaveHydrateGate;}
  await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({passwords})});
 });
 await page.route(/\/api\/settings(?:\?.*)?$/,async route=>{
  const request=route.request();
  if(request.method()==='GET'){
   const response=await route.fetch();
   const body=await response.json();
   body.extraction_password='';body.extraction_password_configured=Boolean(passwords);
   await route.fulfill({response,json:body});return;
  }
  if(request.method()!=='PUT'){await route.continue();return;}
  const payload=request.postDataJSON();puts.push(payload);
  const clears=new Set(payload.clear_secrets||[]);
  if(clears.has('extraction_password'))passwords='';
  else if(String(payload.extraction_password||'').trim())passwords=String(payload.extraction_password).trim();
  saved=true;
  await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({...payload,extraction_password:'',extraction_password_configured:Boolean(passwords),ok:true})});
 });
 await ready(page);await page.evaluate(async()=>{nav(document.querySelector('[data-view="settings"]'));await loadSettings();});
 await page.locator('#view-settings [data-tab="extraction"]').click();
 const source=page.locator('#view-settings [data-panel="extraction"] [data-setting="extraction_password"]');
 await expect.poll(()=>source.inputValue()).toBe('alpha\nbeta');
 let rows=page.locator('.dp-settings-extraction-password-editor .dp-settings-password-line');await expect(rows).toHaveCount(3);
 await rows.nth(0).fill('gamma');
 await expect.poll(()=>source.inputValue()).toBe('gamma\nbeta');
 await page.locator('#view-settings button[data-action="save"]').click();
 await expect.poll(()=>puts.length).toBe(1);
 expect(puts[0].extraction_password).toBe('gamma\nbeta');
 expect(puts[0].clear_secrets||[]).not.toContain('extraction_password');
 await expect.poll(()=>postSaveReads).toBeGreaterThan(0);
 await expect(source).toHaveValue('gamma\nbeta');
 const clear=page.locator('#view-settings [data-panel="extraction"] [data-clear-secret="extraction_password"]');
 await expect(clear).not.toBeChecked();
 releasePostSaveHydrate();
 await expect.poll(()=>source.inputValue()).toBe('gamma\nbeta');
 await page.locator('#view-settings [data-tab="downloads"]').click();
 await page.locator('#view-settings [data-tab="extraction"]').click();
 await expect(source).toHaveValue('gamma\nbeta');
 await page.reload();await page.waitForFunction(markers=>markers.every(marker=>Boolean(window[marker])),MARKERS);
 await page.evaluate(async()=>{nav(document.querySelector('[data-view="settings"]'));await loadSettings();});
 await page.locator('#view-settings [data-tab="extraction"]').click();
 await expect.poll(()=>source.inputValue()).toBe('gamma\nbeta');
 rows=page.locator('.dp-settings-extraction-password-editor .dp-settings-password-line');
 const eye=page.locator('.dp-settings-extraction-password-editor .dp-settings-password-eye');await eye.click();
 await expect(rows.nth(0)).toHaveValue('gamma');await expect(rows.nth(1)).toHaveValue('beta');
});

test('Archive Passwords never arm explicit clear while hydration is pending',async({page})=>{
 let passwords='alpha\nbeta',releaseHydrate,putPayload=null;
 const hydrateGate=new Promise(resolve=>{releaseHydrate=resolve;});
 await page.route('**/api/settings/extraction-passwords',async route=>{await hydrateGate;await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({passwords})});});
 await page.route(/\/api\/settings(?:\?.*)?$/,async route=>{
  const request=route.request();
  if(request.method()==='GET'){
   const response=await route.fetch();const body=await response.json();body.extraction_password='';body.extraction_password_configured=true;await route.fulfill({response,json:body});return;
  }
  if(request.method()!=='PUT'){await route.continue();return;}
  const payload=request.postDataJSON();putPayload=payload;
  const clears=new Set(payload.clear_secrets||[]);if(clears.has('extraction_password'))passwords='';else if(String(payload.extraction_password||'').trim())passwords=String(payload.extraction_password).trim();
  await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({...payload,extraction_password:'',extraction_password_configured:Boolean(passwords),ok:true})});
 });
 await ready(page);await page.evaluate(async()=>{nav(document.querySelector('[data-view="settings"]'));await loadSettings();});
 await page.locator('#view-settings [data-tab="extraction"]').click();
 const clear=page.locator('#view-settings [data-panel="extraction"] [data-clear-secret="extraction_password"]');await expect(clear).toBeVisible();await expect(clear).not.toBeChecked();
 await page.locator('#view-settings button[data-action="save"]').click();await expect.poll(()=>putPayload!==null).toBe(true);
 expect(putPayload.clear_secrets||[]).not.toContain('extraction_password');expect(passwords).toBe('alpha\nbeta');
 releaseHydrate();
 const source=page.locator('#view-settings [data-panel="extraction"] [data-setting="extraction_password"]');await expect.poll(()=>source.inputValue()).toBe('alpha\nbeta');
});

test('Archive Passwords Apply submits the full edited multi-row list with no clear request',async({page})=>{
 let passwords='alpha\nbeta\ngamma',putBody=null;
 await page.route('**/api/settings/extraction-passwords',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({passwords})}));
 await page.route(/\/api\/settings(?:\?.*)?$/,async route=>{
  const request=route.request();
  if(request.method()==='GET'){
   const response=await route.fetch();const body=await response.json();
   body.extraction_password='';body.extraction_password_configured=true;
   await route.fulfill({response,json:body});return;
  }
  if(request.method()!=='PUT'){await route.continue();return;}
  putBody=request.postDataJSON();
  const clears=new Set(putBody.clear_secrets||[]);
  if(clears.has('extraction_password'))passwords='';
  else if(String(putBody.extraction_password||'').trim())passwords=String(putBody.extraction_password).trim();
  await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({...putBody,extraction_password:'',extraction_password_configured:Boolean(passwords),ok:true})});
 });
 await ready(page);await page.evaluate(async()=>{nav(document.querySelector('[data-view="settings"]'));await loadSettings();});
 await page.locator('#view-settings [data-tab="extraction"]').click();
 const source=page.locator('#view-settings [data-panel="extraction"] [data-setting="extraction_password"]');
 await expect.poll(()=>source.inputValue()).toBe('alpha\nbeta\ngamma');
 const rows=page.locator('.dp-settings-extraction-password-editor .dp-settings-password-line');
 await expect(rows).toHaveCount(4);
 await rows.nth(0).fill('ALPHA');
 await expect.poll(()=>rows.count()).toBe(4);
 await rows.nth(3).fill('delta');
 await expect.poll(()=>source.inputValue()).toBe('ALPHA\nbeta\ngamma\ndelta');
 await page.locator('#view-settings button[data-action="save"]').click();
 await expect.poll(()=>putBody!==null).toBe(true);
 expect(putBody.extraction_password).toBe('ALPHA\nbeta\ngamma\ndelta');
 expect(putBody.clear_secrets||[]).not.toContain('extraction_password');
 expect(passwords).toBe('ALPHA\nbeta\ngamma\ndelta');
});

test('Torrent and magnet source identities use teal Lucide Boxes while MegaUp reuses the Mega host asset',async({page})=>{
 await ready(page);
 const result=await page.evaluate(()=>{
  const presentation=window.DPTransferSourcePresentation;
  const probe=identity=>{const slot=document.createElement('span');slot.className='dp-source-icon-slot';slot.innerHTML=presentation.sourceIconMarkup(identity);document.body.appendChild(slot);const svg=slot.querySelector('svg'),img=slot.querySelector('img'),value={slotColor:getComputedStyle(slot).color,glyphColor:svg?getComputedStyle(svg).color:null,classes:svg?[...svg.classList]:[],pathCount:svg?.querySelectorAll('path').length||0,firstPath:svg?.querySelector('path')?.getAttribute('d')||'',src:img?.getAttribute('src')||''};slot.remove();return value;};
  return {magnet:probe({kind:'magnet'}),torrent:probe({kind:'torrent_file'}),megaup:probe({kind:'host',host:'megaup.net'}),megaupAsset:presentation.hostAsset('cdn.megaup.net')};
 });
 for(const item of [result.magnet,result.torrent]){expect(item.classes).toContain('lucide-boxes');expect(item.classes).toContain('dp-source-boxes');expect(item.glyphColor).toBe('rgb(15, 189, 136)');expect(item.slotColor).not.toBe(item.glyphColor);expect(item.pathCount).toBe(12);expect(item.firstPath).toBe('M2.97 12.92A2 2 0 0 0 2 14.63v3.24a2 2 0 0 0 .97 1.71l3 1.8a2 2 0 0 0 2.06 0L12 19v-5.5l-5-3-4.03 2.42Z');}
 expect(result.megaup.src).toBe('/icons/hosts/mega.svg');expect(result.megaupAsset).toBe('/icons/hosts/mega.svg');
});

test('Dashboard Recent Items renders host artwork on cold load without navigation and emits one canonical event per render',async({page})=>{
 await page.setViewportSize({width:1440,height:900});
 const items=Array.from({length:6},(_,i)=>({
  id:i+1,name:`Cold load transfer ${i+1}`,status:'completed',progress:100,size_bytes:1048576*(i+1),created_at:'2026-09-08 17:00:00',
  current_source_identity:{kind:'host',host:'rapidgator.net'},current_provider_id:'alldebrid',current_provider_name:'AllDebrid',delivering_provider_id:'alldebrid',delivering_provider_name:'AllDebrid',provider_provenance_status:'known'
 }));
 await page.route('**/api/torrents*',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items,total:items.length,page:1,page_size:items.length})}));
 await page.addInitScript(()=>{window.__recentEvents=0;document.addEventListener('debridpulse:dashboard-recent-rendered',()=>{window.__recentEvents+=1;});});
 // Dashboard is the default surface; no nav() runs at startup. The bootstrap
 // loadRecent() fires before the lazy-loaded canonical owner registers, yet the
 // Recent Items table must still reconcile to host artwork once the owner is ready.
 await page.goto('/');
 await expect(page.locator('#dash-tbody .dp-source-host-logo')).toHaveCount(6);
 await expect.poll(()=>page.locator('#dash-tbody .dp-source-host-logo').evaluateAll(nodes=>nodes.every(node=>node.complete&&node.naturalWidth>0))).toBe(true);
 await page.waitForLoadState('networkidle');
 // The canonical owner is the sole producer of the event and the app.js
 // entrypoint never becomes a second renderer: one explicit refresh => one event.
 const state=await page.evaluate(async()=>{
  await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  const before=window.__recentEvents;
  await window.loadRecent();
  await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  return {registered:typeof window.__dpRegisterRecentRenderer,delta:window.__recentEvents-before};
 });
 expect(state.registered).toBe('function');
 expect(state.delta).toBe(1);
});

test('Dashboard Recent Activity requests priority ordering and reports a truthful, non-recency subtitle',async({page})=>{
 await page.setViewportSize({width:1440,height:900});
 const capturedUrls=[];
 const items=Array.from({length:3},(_,i)=>({
  id:i+1,name:`Item ${i+1}`,status:'downloading',progress:10,size_bytes:1024,created_at:'2026-09-08 17:00:00',
  current_source_identity:{kind:'link'},current_provider_id:'alldebrid',provider_provenance_status:'known'
 }));
 await page.route('**/api/torrents*',route=>{capturedUrls.push(route.request().url());return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items,total:items.length})});});
 await page.goto('/');
 await expect(page.locator('#dash-activity-count')).toHaveText('3 recent activity items');
 const activityRequest=capturedUrls.find(u=>new URL(u).searchParams.get('order')==='activity');
 expect(activityRequest).toBeTruthy();
 expect(await page.locator('#dash-activity-count').textContent()).not.toContain('most recent');
});

const DASH_WIDE_WIDTHS=[1440,1600,1920,2200];
const DASH_ROW_HTML='<tr data-torrent-id="1" data-status="downloading"><td>Name</td><td>Status</td><td>Progress</td><td>1MB</td><td>today</td><td><div class="actions"><button class="btn btn-blue btn-sm">Pause</button></div></td></tr>';

test('Dashboard shares one action centerline across Recover All, Add, and the row action button',async({page})=>{
 for(const theme of ['dark','light']){
  for(const width of DASH_WIDE_WIDTHS){
   await page.setViewportSize({width,height:900});
   await ready(page);
   if(theme==='light')await page.evaluate(()=>document.body.classList.add('light'));
   await page.evaluate(html=>{document.getElementById('dash-tbody').innerHTML=html;},DASH_ROW_HTML);
   const centers=await page.evaluate(()=>{
    const centerOf=el=>{const r=el.getBoundingClientRect();return (r.left+r.right)/2;};
    return {
     recover:centerOf(document.getElementById('btn-recover-all')),
     add:centerOf(document.getElementById('btn-add-transfer')),
     action:centerOf(document.querySelector('#dash-tbody .actions .btn')),
    };
   });
   expect(Math.abs(centers.recover-centers.add)).toBeLessThanOrEqual(1);
   expect(Math.abs(centers.recover-centers.action)).toBeLessThanOrEqual(1);
  }
 }
});

test('Dashboard Recent Activity table/rows reach the card\'s full right content edge without moving any column',async({page})=>{
 for(const theme of ['dark','light']){
  for(const width of DASH_WIDE_WIDTHS){
   await page.setViewportSize({width,height:900});
   await ready(page);
   if(theme==='light')await page.evaluate(()=>document.body.classList.add('light'));
   await page.evaluate(html=>{document.getElementById('dash-tbody').innerHTML=html;},DASH_ROW_HTML);
   const geometry=await page.evaluate(()=>{
    const rect=el=>{const r=el.getBoundingClientRect();return {left:r.left,right:r.right};};
    const wrap=document.querySelector('.dash-activity-table-wrap');
    const table=document.querySelector('.dp-dashboard-activity .t-table');
    const theadRow=document.querySelector('.dp-dashboard-activity .t-table thead tr');
    const tbodyRow=document.querySelector('#dash-tbody tr');
    const nameCell=tbodyRow.children[0];
    const actionCell=tbodyRow.children[5];
    const track=parseFloat(getComputedStyle(document.getElementById('view-dashboard')).getPropertyValue('--dp-dashboard-action-track'));
    const inset=parseFloat(getComputedStyle(document.getElementById('view-dashboard')).getPropertyValue('--dp-dashboard-action-inset'));
    return {
     wrap:rect(wrap),table:rect(table),theadRow:rect(theadRow),tbodyRow:rect(tbodyRow),
     nameCell:rect(nameCell),actionCell:rect(actionCell),track,inset,
    };
   });
   // The dead strip this test guards against: table/row painting must reach
   // the wrapper's own right content edge, not stop `inset` short of it.
   expect(Math.abs(geometry.table.right-geometry.wrap.right)).toBeLessThanOrEqual(1);
   expect(Math.abs(geometry.theadRow.right-geometry.wrap.right)).toBeLessThanOrEqual(1);
   expect(Math.abs(geometry.tbodyRow.right-geometry.wrap.right)).toBeLessThanOrEqual(1);
   // Name still starts flush with the wrapper's own left edge (unmoved).
   expect(Math.abs(geometry.nameCell.left-geometry.wrap.left)).toBeLessThanOrEqual(1);
   // The Action cell -- not a compensating layer -- is the one column
   // whose own right edge legitimately extends into the recovered inset,
   // and its width equals the shared track plus that inset, relationally.
   expect(Math.abs(geometry.actionCell.right-geometry.wrap.right)).toBeLessThanOrEqual(1);
   expect(Math.abs((geometry.actionCell.right-geometry.actionCell.left)-(geometry.track+geometry.inset))).toBeLessThanOrEqual(1);
  }
 }
});

test('Below the 1440px Dashboard breakpoint, Recent Activity keeps auto column layout and no wrapper inset',async({page})=>{
 for(const width of [1439,1200]){
  await page.setViewportSize({width,height:900});
  await ready(page);
  await page.evaluate(html=>{document.getElementById('dash-tbody').innerHTML=html;},DASH_ROW_HTML);
  const info=await page.evaluate(()=>{
   const table=document.querySelector('.dp-dashboard-activity .t-table');
   const wrap=document.querySelector('.dash-activity-table-wrap');
   return {
    tableLayout:getComputedStyle(table).tableLayout,
    wrapPaddingRight:getComputedStyle(wrap).paddingRight,
    wrapRight:wrap.getBoundingClientRect().right,
    tableRight:table.getBoundingClientRect().right,
   };
  });
  expect(info.tableLayout).toBe('auto');
  expect(parseFloat(info.wrapPaddingRight)).toBe(0);
  expect(Math.abs(info.tableRight-info.wrapRight)).toBeLessThanOrEqual(1);
 }
});

test('Download speed custom cap reads Set Custom and still applies on Enter and click',async({page})=>{
 await page.setViewportSize({width:1440,height:900});
 const bodies=[];
 // DP 1.0.12 canonical architecture correction: the neutral runtime-limit
 // surface, not the aria2-specific route, is the write authority for live
 // bandwidth (specification section 9.6) -- see app.js `_setAria2Speed`.
 await page.route('**/api/execution/runtime-limits',route=>{
  if(route.request().method()!=='PATCH')return route.continue();
  bodies.push(route.request().postDataJSON());
  return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({ok:true,configured:{max_download_bytes_per_second:route.request().postDataJSON().max_download_bytes_per_second},effective:{max_download_bytes_per_second:route.request().postDataJSON().max_download_bytes_per_second},last_apply_error:null})});
 });
 await ready(page);
 await page.evaluate(()=>{document.getElementById('aria2-cap-menu').hidden=false;});
 const button=page.locator('.aria2-cap-custom button');
 await expect(button).toHaveText('Set Custom');
 const input=page.locator('#aria2-cap-custom-mbps');
 await input.fill('7');
 await input.press('Enter');
 await expect.poll(()=>bodies.length).toBe(1);
 expect(bodies[0].max_download_bytes_per_second).toBe(Math.round(7*1048576));
 // A successful apply closes the menu (existing behavior) -- reopen it.
 await page.evaluate(()=>{document.getElementById('aria2-cap-menu').hidden=false;});
 await input.fill('12');
 await button.click();
 await expect.poll(()=>bodies.length).toBe(2);
 expect(bodies[1].max_download_bytes_per_second).toBe(Math.round(12*1048576));
 // Presets still apply immediately.
 await page.evaluate(()=>{document.getElementById('aria2-cap-menu').hidden=false;});
 await page.locator('.aria2-cap-options button', {hasText:'1 MB/s'}).click();
 await expect.poll(()=>bodies.length).toBe(3);
 expect(bodies[2].max_download_bytes_per_second).toBe(1048576);
});

test('Downloads Provider Inventory icon, provider badge, and source label share canonical alignment',async({page})=>{
 await page.setViewportSize({width:1600,height:900});
 const common={status:'completed',presentation_status:'completed',progress:100,size_bytes:1048576,created_at:'2026-09-08 12:00:00',current_source_identity:{kind:'link'},current_provider_id:'alldebrid',current_provider_name:'AllDebrid',delivering_provider_id:'alldebrid',delivering_provider_name:'AllDebrid',provider_provenance_status:'recorded'};
 const items=[
  {...common,id:71,name:'Direct',hash:'direct:71',source:'direct_link'},
  {...common,id:72,name:'Inventory',hash:'inventory:72',source:'alldebrid_existing'},
  {...common,id:73,name:'General HTTP',hash:'direct:73',source:'direct_link',current_provider_id:'general_http',current_provider_name:'HTTP & HTTPS',delivering_provider_id:'general_http',delivering_provider_name:'HTTP & HTTPS'}
 ];
 await page.route('**/api/torrents*',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items,total:3,page:1,page_size:3})}));
 await ready(page);await page.evaluate(async()=>{nav(document.querySelector('[data-view="torrents"]'));await loadTorrents();});
 const blocks=page.locator('#t-tbody .dp-downloads-provider-block');await expect(blocks).toHaveCount(3);
 const labels=blocks.locator(':scope > .dp-transfer-source-label');await expect(labels.nth(0)).toHaveText('Direct link');await expect(labels.nth(1)).toHaveText('Provider inventory');await expect(labels.nth(2)).toHaveText('Direct link');
 const geometry=await blocks.evaluateAll(nodes=>nodes.map(block=>{const cell=block.closest('.dp-downloads-provider-cell'),line=block.querySelector('.dp-downloads-provider-line'),label=block.querySelector(':scope > .dp-transfer-source-label'),icon=line.querySelector('.dp-source-icon-slot'),chip=line.querySelector('.dp-provider-chip');const box=node=>{const r=node.getBoundingClientRect();return{left:r.left,right:r.right,top:r.top,width:r.width,height:r.height,centerY:r.top+r.height/2,scrollWidth:node.scrollWidth,clientWidth:node.clientWidth};};return{cell:box(cell),block:box(block),line:box(line),label:box(label),icon:box(icon),chip:box(chip)};}));
 for(const row of geometry){expect(Math.abs(row.line.left-row.label.left)).toBeLessThanOrEqual(0.75);expect(Math.abs(row.icon.left-row.line.left)).toBeLessThanOrEqual(0.75);expect(Math.abs(row.icon.centerY-row.chip.centerY)).toBeLessThanOrEqual(0.75);expect(row.chip.right).toBeLessThanOrEqual(row.cell.right+0.5);expect(row.chip.scrollWidth).toBeLessThanOrEqual(row.chip.clientWidth);}
 expect(Math.abs(geometry[0].label.left-geometry[1].label.left)).toBeLessThanOrEqual(0.75);
 expect(Math.abs(geometry[0].icon.left-geometry[1].icon.left)).toBeLessThanOrEqual(0.75);
 expect(Math.abs(geometry[0].chip.left-geometry[1].chip.left)).toBeLessThanOrEqual(0.75);
 expect(Math.abs(geometry[0].icon.left-geometry[2].icon.left)).toBeLessThanOrEqual(0.75);
});

test('Dashboard common-source group launcher renders only for 2+ common hosts, in [source][provider][network N] order',async({page})=>{
 await page.setViewportSize({width:1600,height:900});
 const base={status:'completed',progress:100,size_bytes:1048576,created_at:'2026-09-08 17:00:00',current_provider_id:'alldebrid',current_provider_name:'AllDebrid',delivering_provider_id:'alldebrid',delivering_provider_name:'AllDebrid',provider_provenance_status:'known'};
 const items=[
  {...base,id:1,name:'One common host',current_source_identity:{kind:'host',host:'rapidgator.net'},common_candidate_count:1},
  {...base,id:2,name:'Two common hosts',current_source_identity:{kind:'host',host:'rapidgator.net'},common_candidate_count:3},
 ];
 await page.route('**/api/torrents*',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items,total:items.length})}));
 await ready(page);
 await expect(page.locator('#dash-tbody tr[data-torrent-id]')).toHaveCount(2);
 await expect(page.locator('#dash-tbody tr[data-torrent-id="1"] .dp-candidate-chip')).toHaveCount(0);
 const launcher=page.locator('#dash-tbody tr[data-torrent-id="2"] .dp-group-candidate-launcher');
 await expect(launcher).toHaveCount(1);
 const order=await page.locator('#dash-tbody tr[data-torrent-id="2"] .dp-transfer-provider-meta').evaluate(meta=>[...meta.children].map(node=>node.className.split(' ')[0]));
 expect(order).toEqual(['dp-source-icon-slot','dp-provider-chip','dp-candidate-chip']);
 await expect(launcher.locator('svg[data-dp-lucide="network"]')).toHaveCount(1);
 await expect(launcher.locator('.dp-candidate-chip-count')).toHaveText('3');
 // DP 1.0.12 Defect 1: the dense list launcher is glyph+count only now: the
 // word "Candidates" moved to the Details Files-header labeled variant
 // (see group-candidates.spec.js Case A/B). Descriptive text remains in the
 // accessible name/title instead.
 await expect(launcher).not.toContainText('Candidates');
 await expect(launcher).toHaveAttribute('aria-label',/sources are common to every file/);
 await expect(launcher).toHaveJSProperty('tagName','BUTTON');
 await expect(launcher).toHaveAttribute('aria-haspopup','dialog');
 await expect(launcher).toHaveAttribute('aria-expanded','false');
 await expect(launcher).toHaveAttribute('data-dp-transfer-id','2');
 // Light theme keeps the launcher legible.
 await page.locator('#theme-toggle').click();
 expect(await page.evaluate(()=>document.body.classList.contains('light'))).toBe(true);
 await expect(launcher).toBeVisible();
 const lightBorder=await launcher.evaluate(node=>getComputedStyle(node).borderTopWidth);
 expect(parseFloat(lightBorder)).toBeGreaterThan(0);
});

test('Downloads common-source group launcher joins the first line for 2+ common hosts only, source label kept beneath',async({page})=>{
 await page.setViewportSize({width:1600,height:900});
 const common={status:'completed',progress:100,size_bytes:1048576,created_at:'2026-09-08 12:00:00',current_source_identity:{kind:'host',host:'rapidgator.net'},current_provider_id:'alldebrid',current_provider_name:'AllDebrid',delivering_provider_id:'alldebrid',delivering_provider_name:'AllDebrid',provider_provenance_status:'recorded',source:'direct_link'};
 const items=[
  {...common,id:81,name:'One common',hash:'direct:81',common_candidate_count:1},
  {...common,id:82,name:'Two common',hash:'direct:82',common_candidate_count:4},
 ];
 await page.route('**/api/torrents*',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items,total:2,page:1,page_size:2})}));
 await ready(page);await page.evaluate(async()=>{nav(document.querySelector('[data-view="torrents"]'));await loadTorrents();});
 await expect(page.locator('#t-tbody .dp-downloads-provider-block')).toHaveCount(2);
 const singleCells=await page.locator('#t-tbody tr[data-torrent-id="81"] > td').count();
 const multiCells=await page.locator('#t-tbody tr[data-torrent-id="82"] > td').count();
 expect(multiCells).toBe(singleCells);
 await expect(page.locator('#t-tbody tr[data-torrent-id="81"] .dp-candidate-chip')).toHaveCount(0);
 const line=page.locator('#t-tbody tr[data-torrent-id="82"] .dp-downloads-provider-line');
 const launcher=line.locator('.dp-group-candidate-launcher');
 await expect(launcher).toHaveCount(1);
 const order=await line.evaluate(node=>[...node.children].map(child=>child.className.split(' ')[0]));
 expect(order).toEqual(['dp-source-icon-slot','dp-provider-chip','dp-candidate-chip']);
 await expect(page.locator('#t-tbody tr[data-torrent-id="82"] .dp-downloads-provider-block > .dp-transfer-source-label')).toHaveText('Direct link');
 await expect(launcher.locator('.dp-candidate-chip-count')).toHaveText('4');
 await expect(launcher).toHaveJSProperty('tagName','BUTTON');
 const geometry=await page.locator('#t-tbody tr[data-torrent-id="82"]').evaluate(row=>{
  const cell=row.querySelector('.dp-downloads-provider-cell');
  const chip=row.querySelector('.dp-candidate-chip');
  const chipRect=chip.getBoundingClientRect(),cellRect=cell.getBoundingClientRect();
  return {withinCell:chipRect.right<=cellRect.right+0.5,noClip:chip.scrollWidth<=chip.clientWidth,rowHeight:row.getBoundingClientRect().height};
 });
 expect(geometry.withinCell).toBe(true);
 expect(geometry.noClip).toBe(true);
});

test('Details candidate switch remains available after comprehensive presentation refresh',async({page})=>{
 const candidate=(id,source,active)=>({candidate_id:id,source_label:source,provider_id:'alldebrid',relationship:'Original',dispositions:active?['Active']:[],is_selected:active,is_active:active,is_delivering:false,switch_eligible:!active});
 const detail={id:990,name:'Candidate review fixture',status:'downloading',progress:42,size_bytes:1024,source:'direct_link',label:'',hash:'',created_at:'2026-09-06T10:00:00Z',current_provider_id:'alldebrid',current_provider_name:'AllDebrid',route_attempts:[],execution_attempts:[],executors:['aria2'],source_outcomes:[],events:[],files:[{id:502,filename:'fixture.rar',size_bytes:1024,status:'downloading',blocked:false,block_reason:null,candidate_count:2,acquisition_candidates:[candidate('a','rapidgator.net',true),candidate('b','megaup.net',false)]}]};
 let detailReads=0;await page.route(url=>url.pathname==='/api/torrents/990',async route=>{detailReads+=1;if(detailReads>1)await new Promise(resolve=>setTimeout(resolve,150));await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(detail)});});
 await ready(page);await page.evaluate(()=>showDetail(990));await page.locator('tr[data-dp-artifact-id="502"] .dp-detail-candidate-disclosure').click();
 // The rows are rendered once from the payload showDetail loaded (one read). A comprehensive
 // list refresh then makes the owner re-read and re-render its own rows; the open source panel
 // and its Switch action must survive that refresh.
 const panel=page.locator('tr[data-dp-candidate-owner="502"]');await expect(panel).toBeVisible();await expect(panel.locator('.dp-detail-candidate-switch')).toHaveCount(1);
 expect(detailReads).toBe(1);
 await page.evaluate(()=>document.dispatchEvent(new CustomEvent('debridpulse:downloads-rendered')));
 await expect.poll(()=>detailReads).toBeGreaterThanOrEqual(2);
 await expect(panel).toBeVisible();await expect(panel.locator('.dp-detail-candidate-switch')).toHaveCount(1);await expect(panel.locator('.dp-detail-candidate-switch')).toHaveText('Switch to this source');
});
