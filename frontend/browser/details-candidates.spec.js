const { test, expect } = require('@playwright/test');

function candidate(id, source, active) {
  return {candidate_id:id,source_label:source,provider_id:'alldebrid',relationship:'Original',
    dispositions:active?['Active']:[],is_selected:active,is_active:active,is_delivering:false,switch_eligible:!active};
}
function detail(active) {
  return {id:990,name:'Candidate switch fixture',status:'downloading',progress:42,size_bytes:1024,
    source:'direct_link',label:'',hash:'',created_at:'2026-09-06T10:00:00Z',current_provider_id:'alldebrid',
    current_provider_name:'AllDebrid',route_attempts:[],execution_attempts:[],executors:['aria2'],source_outcomes:[],events:[],
    files:[{id:502,filename:'GF030926-M2SP-RN.rar',size_bytes:1024,status:'downloading',blocked:false,block_reason:null,
      candidate_count:2,acquisition_candidates:[candidate('a','rapidgator.net',active==='a'),candidate('b','megaup.net',active==='b')]}]};
}
async function openCandidates(page, holder, handler) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status:200,contentType:'text/css',body:''}));
  await page.route(url => url.pathname === '/api/torrents/990', route => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(holder.detail)}));
  await page.route(url => url.pathname === '/api/torrents/990/artifacts/502/candidate', route => handler(route));
  await page.goto('/');
  await page.evaluate(() => showDetail(990));
  await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);
  await page.locator('tr[data-dp-artifact-id="502"] .dp-detail-candidate-disclosure').click();
  await expect(page.locator('tr[data-dp-candidate-owner="502"]')).toBeVisible();
}

test('manual switch is backend authoritative and keeps Details usable', async ({page}) => {
  const holder={detail:detail('a')};
  await openCandidates(page,holder,async route=>{
    expect(route.request().postDataJSON()).toEqual({candidate_id:'b'});
    await new Promise(resolve=>setTimeout(resolve,250));
    holder.detail=detail('b');
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({ok:true,transfer_id:990,artifact_id:502,filename:'GF030926-M2SP-RN.rar',candidate_id:'b',source_host:'megaup.net',provider_id:'alldebrid'})});
  });
  const panel=page.locator('tr[data-dp-candidate-owner="502"]');
  await expect(panel.locator('.dp-detail-candidate-item').first().locator('.dp-detail-candidate-active')).toHaveText('ACTIVE');
  const button=panel.locator('.dp-detail-candidate-switch');
  await button.click();
  await expect(button).toBeDisabled();
  await expect(panel.locator('.dp-detail-candidate-item').first().locator('.dp-detail-candidate-active')).toHaveText('ACTIVE');
  await expect(page.locator('tr[data-dp-candidate-owner="502"] .dp-detail-candidate-item').nth(1).locator('.dp-detail-candidate-active')).toHaveText('ACTIVE');
  await expect(page.locator('.toast')).toContainText('GF030926-M2SP-RN.rar file source switched to megaup.net');
  const disclosure=page.locator('tr[data-dp-artifact-id="502"] .dp-detail-candidate-disclosure');
  await expect(disclosure).toBeFocused();
  await disclosure.click();
  await expect(page.locator('tr[data-dp-candidate-owner="502"]')).toHaveCount(0);
  await disclosure.click();
  await expect(page.locator('tr[data-dp-candidate-owner="502"]')).toBeVisible();
});

test('switch button is keyboard reachable and ACTIVE is noninteractive', async ({page}) => {
  const holder={detail:detail('a')};
  await openCandidates(page,holder,async route=>{holder.detail=detail('b');await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({ok:true,filename:'GF030926-M2SP-RN.rar',candidate_id:'b',source_host:'megaup.net'})});});
  const panel=page.locator('tr[data-dp-candidate-owner="502"]');
  await expect(panel.locator('.dp-detail-candidate-active')).not.toHaveAttribute('role','button');
  await expect(panel.locator('.dp-detail-candidate-active')).not.toHaveAttribute('tabindex');
  const button=panel.locator('.dp-detail-candidate-switch');
  await button.focus();
  await expect(button).toBeFocused();
  await button.press('Enter');
  await expect(page.locator('tr[data-dp-candidate-owner="502"] .dp-detail-candidate-item').nth(1).locator('.dp-detail-candidate-active')).toHaveText('ACTIVE');
});

test('structured server rejection stays truthful and does not move ACTIVE', async ({page}) => {
  const holder={detail:detail('a')};
  await openCandidates(page,holder,async route=>route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:{category:'provider_unavailable',message:'Selected provider is unavailable'}})}));
  await page.locator('tr[data-dp-candidate-owner="502"] .dp-detail-candidate-switch').click();
  await expect(page.locator('.toast')).toContainText('Unable to switch source for GF030926-M2SP-RN.rar');
  await expect(page.locator('.toast')).toContainText('Selected provider is unavailable');
  const panel=page.locator('tr[data-dp-candidate-owner="502"]');
  await expect(panel.locator('.dp-detail-candidate-item').first().locator('.dp-detail-candidate-active')).toHaveText('ACTIVE');
  await expect(panel.locator('.dp-detail-candidate-item').nth(1).locator('.dp-detail-candidate-active')).toHaveCount(0);
  await expect(panel.locator('.dp-detail-candidate-switch')).toBeEnabled();
});

test('in-flight suppression prevents duplicate candidate POSTs', async ({page}) => {
  const holder={detail:detail('a')}; let posts=0;
  await openCandidates(page,holder,async route=>{posts+=1;await new Promise(resolve=>setTimeout(resolve,180));holder.detail=detail('b');await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({ok:true,filename:'GF030926-M2SP-RN.rar',candidate_id:'b',source_host:'megaup.net'})});});
  const button=page.locator('tr[data-dp-candidate-owner="502"] .dp-detail-candidate-switch');
  await button.evaluate(node=>{node.click();node.click();});
  await expect(page.locator('tr[data-dp-candidate-owner="502"] .dp-detail-candidate-item').nth(1).locator('.dp-detail-candidate-active')).toHaveText('ACTIVE');
  expect(posts).toBe(1);
});
