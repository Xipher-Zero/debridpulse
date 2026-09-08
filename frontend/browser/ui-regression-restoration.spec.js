const { test, expect } = require('@playwright/test');

const MARKERS=['DPActivityLog','DPArchivePasswords','DPDownloadsPresentation'];
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

test('Archive Password Show all and Hide all stay single-line with identical typography and geometry',async({page})=>{
 await page.route('**/api/settings/extraction-passwords',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({passwords:'alpha\nbeta'})}));
 await ready(page);await page.evaluate(async()=>{nav(document.querySelector('[data-view="settings"]'));await loadSettings();});
 const tab=page.locator('#view-settings [data-tab="extraction"]');if(await tab.count())await tab.click();
 const eye=page.locator('.dp-settings-extraction-password-editor .dp-settings-password-eye');await expect(eye).toContainText('Show all');
 const measure=()=>eye.evaluate(button=>{const label=button.querySelector('.dp-settings-password-eye-label'),bs=getComputedStyle(button),ls=getComputedStyle(label),br=button.getBoundingClientRect(),lr=label.getBoundingClientRect();return{text:label.textContent,width:br.width,height:br.height,labelHeight:lr.height,fontFamily:ls.fontFamily,fontSize:ls.fontSize,fontWeight:ls.fontWeight,lineHeight:ls.lineHeight,whiteSpace:ls.whiteSpace,buttonWhiteSpace:bs.whiteSpace};});
 const show=await measure();await eye.click();await expect(eye).toContainText('Hide all');const hide=await measure();
 expect(show.whiteSpace).toBe('nowrap');expect(show.buttonWhiteSpace).toBe('nowrap');expect(show.labelHeight).toBeLessThan(20);
 expect(hide.whiteSpace).toBe('nowrap');expect(hide.buttonWhiteSpace).toBe('nowrap');expect(hide.labelHeight).toBeLessThan(20);
 expect({width:show.width,height:show.height,fontFamily:show.fontFamily,fontSize:show.fontSize,fontWeight:show.fontWeight,lineHeight:show.lineHeight}).toEqual({width:hide.width,height:hide.height,fontFamily:hide.fontFamily,fontSize:hide.fontSize,fontWeight:hide.fontWeight,lineHeight:hide.lineHeight});
});

test('Downloads Provider Inventory source label shares the same left edge as other source labels',async({page})=>{
 await page.setViewportSize({width:1600,height:900});
 const common={status:'completed',presentation_status:'completed',progress:100,size_bytes:1048576,created_at:'2026-09-08 12:00:00',current_source_identity:{kind:'link'},current_provider_id:'alldebrid',current_provider_name:'AllDebrid',delivering_provider_id:'alldebrid',delivering_provider_name:'AllDebrid',provider_provenance_status:'recorded'};
 const items=[{...common,id:71,name:'Direct',hash:'direct:71',source:'direct_link'},{...common,id:72,name:'Inventory',hash:'inventory:72',source:'alldebrid_existing'}];
 await page.route('**/api/torrents*',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items,total:2,page:1,page_size:2})}));
 await ready(page);await page.evaluate(async()=>{nav(document.querySelector('[data-view="torrents"]'));await loadTorrents();});
 const labels=page.locator('#t-tbody .dp-downloads-provider-block > .dp-transfer-source-label');await expect(labels).toHaveCount(2);
 await expect(labels.nth(0)).toHaveText('Direct link');await expect(labels.nth(1)).toHaveText('Provider inventory');
 const lefts=await labels.evaluateAll(nodes=>nodes.map(node=>node.getBoundingClientRect().left));expect(Math.abs(lefts[0]-lefts[1])).toBeLessThanOrEqual(0.5);
});

test('Details candidate switch remains available after comprehensive presentation refresh',async({page})=>{
 const candidate=(id,source,active)=>({candidate_id:id,source_label:source,provider_id:'alldebrid',relationship:'Original',dispositions:active?['Active']:[],is_selected:active,is_active:active,is_delivering:false,switch_eligible:!active});
 const detail={id:990,name:'Candidate review fixture',status:'downloading',progress:42,size_bytes:1024,source:'direct_link',label:'',hash:'',created_at:'2026-09-06T10:00:00Z',current_provider_id:'alldebrid',current_provider_name:'AllDebrid',route_attempts:[],execution_attempts:[],executors:['aria2'],source_outcomes:[],events:[],files:[{id:502,filename:'fixture.rar',size_bytes:1024,status:'downloading',blocked:false,block_reason:null,candidate_count:2,acquisition_candidates:[candidate('a','rapidgator.net',true),candidate('b','megaup.net',false)]}]};
 let detailReads=0;await page.route(url=>url.pathname==='/api/torrents/990',async route=>{detailReads+=1;if(detailReads>1)await new Promise(resolve=>setTimeout(resolve,150));await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(detail)});});
 await ready(page);await page.evaluate(()=>showDetail(990));await page.locator('tr[data-dp-artifact-id="502"] .dp-detail-candidate-disclosure').click();
 const panel=page.locator('tr[data-dp-candidate-owner="502"]');await expect(panel).toBeVisible();await expect(panel.locator('.dp-detail-candidate-switch')).toHaveCount(1);await expect(panel.locator('.dp-detail-candidate-switch')).toHaveText('Switch to this source');expect(detailReads).toBeGreaterThanOrEqual(2);
});
