const { test, expect } = require('@playwright/test');

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status:200, contentType:'text/css', body:''}));
}

function candidate(id, source, relationship, dispositions = []) {
  return {candidate_id:id, source_label:source, provider_id:'alldebrid', relationship, dispositions,
    is_selected:dispositions.includes('Active') || dispositions.includes('Selected') || dispositions.includes('Delivering'),
    is_delivering:dispositions.includes('Delivering')};
}

function file(id, name, status, candidates) {
  const value = {id, filename:name, size_bytes:10737418240, status, blocked:false, block_reason:null,
    candidate_count:candidates.length};
  if (candidates.length > 1) value.acquisition_candidates = candidates;
  return value;
}

function detailFixture(files) {
  return {
    id:990, name:'Candidate presentation fixture', status:'paused', progress:42, size_bytes:files.reduce((n,f)=>n+f.size_bytes,0),
    source:'direct_link', label:'', hash:'', created_at:'2026-09-05T10:00:00Z', original_resource:'https://rapidgator.net/file/example',
    provider_provenance_status:'recorded', current_provider_id:'alldebrid', current_provider_name:'AllDebrid',
    delivering_provider_id:null, delivering_provider_name:null, route_attempts:[], execution_attempts:[], executors:['aria2'],
    source_outcomes:[], events:[], files,
  };
}

async function installDetailRoute(page, holder) {
  await page.route(url => url.pathname === '/api/torrents/990', route => route.fulfill({
    status:200, contentType:'application/json', body:JSON.stringify(holder.detail),
  }));
}

async function openDetail(page) {
  await page.evaluate(() => showDetail(990));
  await expect(page.locator('#overlay')).toHaveClass(/\bopen\b/);
  await expect(page.locator('.dp-detail-files-card')).toBeVisible();
}

const two = [
  candidate('a','rapidgator.net','Original',['Active']),
  candidate('b','1fichier.com','Consolidated',[]),
];
const three = [...two, candidate('c','mirror.example','Consolidated',[])];

test('Details Candidates disclosure is multiplicity-only, correctly counted, and preserves lifecycle status', async ({ page }) => {
  await isolateExternalFonts(page);
  const holder = {detail:detailFixture([
    file(501,'single.rar','completed',[candidate('s','rapidgator.net','Original',['Delivering'])]),
    file(502,'two.rar','paused',two),
    file(503,'three.rar','queued',three),
  ])};
  await installDetailRoute(page, holder);
  await page.goto('/');
  await openDetail(page);

  const single = page.locator('tr[data-dp-artifact-id="501"]');
  const double = page.locator('tr[data-dp-artifact-id="502"]');
  const triple = page.locator('tr[data-dp-artifact-id="503"]');
  await expect(single.locator('.dp-detail-candidate-disclosure')).toHaveCount(0);
  await expect(double.locator('.dp-detail-candidate-disclosure')).toHaveText(/2\s*Candidates/);
  await expect(triple.locator('.dp-detail-candidate-disclosure')).toHaveText(/3\s*Candidates/);
  await expect(double.locator('.dp-detail-candidate-disclosure')).toHaveAttribute('aria-expanded','false');
  await expect(single).toContainText('Done');
  await expect(double).toContainText('Paused');
  await expect(triple).toContainText('Queued');
  await expect(page.locator('.dp-detail-files-card')).not.toContainText('Duplicate');
  await expect(page.locator('.dp-detail-files-card thead th')).toHaveCount(3);
  await expect(page.locator('.dp-detail-files-card thead')).toHaveText(/Filename.*Size.*Status/);
});

test('candidate expansion is accessible, independent per artifact, and renders authoritative provider/source metadata', async ({ page }) => {
  await isolateExternalFonts(page);
  const holder = {detail:detailFixture([file(502,'two.rar','paused',two), file(503,'three.rar','queued',three)])};
  await installDetailRoute(page, holder);
  await page.goto('/');
  await openDetail(page);

  const twoButton = page.locator('tr[data-dp-artifact-id="502"] .dp-detail-candidate-disclosure');
  const threeButton = page.locator('tr[data-dp-artifact-id="503"] .dp-detail-candidate-disclosure');
  await twoButton.click();
  await expect(twoButton).toHaveAttribute('aria-expanded','true');
  const twoPanel = page.locator('tr[data-dp-candidate-owner="502"]');
  await expect(twoPanel).toBeVisible();
  await expect(twoPanel.locator('.dp-detail-candidate-item')).toHaveCount(2);
  await expect(twoPanel).toContainText('rapidgator.net');
  await expect(twoPanel).toContainText('1fichier.com');
  await expect(twoPanel).toContainText('AllDebrid');
  await expect(twoPanel).toContainText('Original · Active');
  await expect(twoPanel).toContainText('Consolidated');
  await expect(page.locator('tr[data-dp-candidate-owner="503"]')).toHaveCount(0);

  await threeButton.click();
  await expect(page.locator('tr[data-dp-candidate-owner="502"]')).toHaveCount(1);
  await expect(page.locator('tr[data-dp-candidate-owner="503"] .dp-detail-candidate-item')).toHaveCount(3);
  await twoButton.click();
  await expect(page.locator('tr[data-dp-candidate-owner="502"]')).toHaveCount(0);
  await expect(page.locator('tr[data-dp-candidate-owner="503"]')).toHaveCount(1);
});

test('candidate rerender updates count without duplicate rows or ownership migration', async ({ page }) => {
  await isolateExternalFonts(page);
  const holder = {detail:detailFixture([file(502,'two.rar','paused',two), file(503,'three.rar','queued',three)])};
  await installDetailRoute(page, holder);
  await page.goto('/');
  await openDetail(page);
  await page.locator('tr[data-dp-artifact-id="502"] .dp-detail-candidate-disclosure').click();
  await expect(page.locator('tr[data-dp-candidate-owner="502"] .dp-detail-candidate-item')).toHaveCount(2);

  holder.detail = detailFixture([file(502,'two.rar','paused',three), file(503,'three.rar','queued',two)]);
  await page.evaluate(() => document.dispatchEvent(new CustomEvent('debridpulse:downloads-rendered')));
  await expect(page.locator('tr[data-dp-artifact-id="502"] .dp-detail-candidate-disclosure')).toHaveText(/3\s*Candidates/);
  await expect(page.locator('tr[data-dp-candidate-owner="502"]')).toHaveCount(1);
  await expect(page.locator('tr[data-dp-candidate-owner="502"] .dp-detail-candidate-item')).toHaveCount(3);
  await expect(page.locator('tr[data-dp-candidate-owner="503"] .dp-detail-candidate-disclosure')).toHaveText(/2\s*Candidates/);
  await expect(page.locator('tr[data-dp-candidate-owner="503"]')).toHaveCount(0);
});

test('expanded candidate rows preserve modal width, scrolling, long filenames, and dark/light readability', async ({ page }) => {
  await isolateExternalFonts(page);
  await page.setViewportSize({width:900,height:650});
  const longName = 'GF030926-M2SP-RN-' + 'very-long-release-name-'.repeat(5) + '.rar';
  const files = [file(600,longName,'paused',three)];
  for (let i=0;i<8;i++) files.push(file(610+i,'part'+i+'.rar','queued',three));
  const holder = {detail:detailFixture(files)};
  await installDetailRoute(page, holder);
  await page.goto('/');
  await openDetail(page);

  const modal = page.locator('#modal');
  const collapsed = await modal.boundingBox();
  await page.locator('tr[data-dp-artifact-id="600"] .dp-detail-candidate-disclosure').click();
  const expanded = await modal.boundingBox();
  expect(Math.abs(expanded.width - collapsed.width)).toBeLessThanOrEqual(1);
  const wrap = page.locator('.dp-detail-files-card .dp-detail-table-wrap');
  expect(await wrap.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBeTruthy();
  await expect(page.locator('tr[data-dp-artifact-id="600"] .dp-detail-filename-copy')).toContainText('GF030926-M2SP-RN');
  await page.screenshot({path:'test-results/checkpoint-details-candidates-dark.png', fullPage:true});

  await page.locator('#theme-toggle').click();
  await expect.poll(() => page.evaluate(() => document.body.classList.contains('light'))).toBeTruthy();
  await expect(page.locator('tr[data-dp-candidate-owner="600"] .dp-detail-candidate-item').first()).toBeVisible();
  await page.screenshot({path:'test-results/checkpoint-details-candidates-light.png', fullPage:true});

  const lastButton = page.locator('tr[data-dp-artifact-id="617"] .dp-detail-candidate-disclosure');
  await lastButton.scrollIntoViewIfNeeded();
  await lastButton.click();
  await expect(page.locator('tr[data-dp-candidate-owner="617"]')).toBeVisible();
  expect(await modal.evaluate(el => el.scrollHeight >= el.clientHeight)).toBeTruthy();
});
