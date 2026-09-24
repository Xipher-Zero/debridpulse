const { test, expect } = require('@playwright/test');

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status:200, contentType:'text/css', body:''}));
}
async function openSettings(page) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await expect(page.locator('.dp-settings-panel[data-panel="sources"]')).toBeVisible();
  await revealGeneralSources(page);
}

/* Sources & Providers cards render COLLAPSED: expansion is LOCAL presentation
 * state, never a projection of enabled/configured/verified state. Opening one
 * through the canonical disclosure writes no canonical state, so this spec
 * never depends on another spec's enable/disable timing against the shared
 * backend. The General Sources members live inside that group's body. */
async function revealGeneralSources(page) {
  const group = page.locator('.dp-settings-general-sources');
  const disclosure = group.locator('.dp-settings-disclosure');
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
  await expect(group.locator('.dp-settings-provider-card--general-http')).toBeVisible();
}

async function primaryTextColor(page) {
  return page.evaluate(() => {
    const probe = document.createElement('span');
    probe.style.color = 'var(--dp-text-primary)';
    document.body.appendChild(probe);
    const color = getComputedStyle(probe).color;
    probe.remove();
    return color;
  });
}
function integrationInput(page, identity) {
  return page.locator(`[data-integration-enabled="${identity}"]`);
}
function integrationControl(page, identity) {
  return page.locator(`label[for="dp-settings-integration-${identity}-enabled"]`);
}
async function setIntegrationChecked(page, identity, value) {
  const input = integrationInput(page, identity);
  if ((await input.isChecked()) !== value) await integrationControl(page, identity).click();
  await expect(input).toBeChecked({checked:value});
}
async function saveSettings(page) {
  const responsePromise = page.waitForResponse(
    response => response.url().endsWith('/api/settings') && response.request().method() === 'PUT',
    {timeout:20000},
  );
  await page.locator('#view-settings [data-action="save"]').click();
  const response = await responsePromise;
  expect(response.ok()).toBeTruthy();
  // Apply Settings re-renders the whole Settings view, which returns every
  // expandable card to its collapsed default. Re-open the group this spec
  // operates, through the same canonical disclosure.
  await revealGeneralSources(page);
}
function listFixture(overrides = {}) {
  return {
    id:901, name:'Stage 10 fixture', status:'completed', progress:100, size_bytes:1024,
    source:'direct_link', label:'', hash:'', created_at:'2026-09-02T12:00:00Z',
    provider_provenance_status:'recorded', current_provider_id:'general_http', current_provider_name:'HTTP & HTTPS',
    delivering_provider_id:'general_http', delivering_provider_name:'HTTP & HTTPS', ...overrides,
  };
}

test('Sources & Providers exposes canonical AllDebrid and General HTTP enable controls without HTTP tuning', async ({ page }) => {
  await isolateExternalFonts(page); await page.goto('/'); await openSettings(page);
  await expect(page.locator('.dp-settings-debrid-services')).toContainText('External Providers');
  await expect(page.locator('.dp-settings-provider-card--alldebrid')).toContainText('AllDebrid');
  await expect(integrationControl(page, 'alldebrid')).toBeVisible();
  await expect(page.locator('.dp-settings-general-sources')).toContainText('General Sources');
  const httpCard = page.locator('.dp-settings-provider-card--general-http');
  await expect(httpCard).toContainText('HTTP & HTTPS');
  await expect(httpCard).toContainText('Direct downloads from standard HTTP and HTTPS URLs.');
  await expect(integrationControl(page, 'general_http')).toBeVisible();
  await expect(httpCard.locator('input')).toHaveCount(1);
  for (const text of ['User Agent','Timeout','Retry','Proxy']) await expect(httpCard).not.toContainText(text);
  const headerCopy = await page.locator('.dp-settings-header-copy').boundingBox();
  const tabsBox = await page.locator('.dp-settings-tabs').boundingBox();
  expect(headerCopy.y + headerCopy.height).toBeLessThanOrEqual(tabsBox.y + 1);
  await page.screenshot({path:'test-results/checkpoint-settings-dark-desktop.png', fullPage:true});
  await page.locator('#theme-toggle').click();
  await expect.poll(() => page.evaluate(() => document.body.classList.contains('light'))).toBeTruthy();
  const primaryText = await primaryTextColor(page);
  await expect(page.locator('.dp-settings-tabs .stab[aria-selected="true"]')).toHaveCSS('color', primaryText);
  await expect(page.locator('#sidebar .nav-item[data-view="settings"].active')).toHaveCSS('color', primaryText);
  await page.screenshot({path:'test-results/checkpoint-settings-light-desktop.png', fullPage:true});
});

test('both provider enable controls round-trip through the running backend and survive reload', async ({ page }) => {
  await isolateExternalFonts(page); await page.goto('/');
  const original = await page.request.get('/api/settings').then(response => response.json());
  const originalAd = original.integrations.alldebrid.enabled;
  const originalHttp = original.integrations.general_http.enabled;
  await openSettings(page);

  const firstAd = !originalAd;
  const firstHttp = originalHttp || !firstAd;
  await setIntegrationChecked(page, 'alldebrid', firstAd);
  await setIntegrationChecked(page, 'general_http', firstHttp);
  await saveSettings(page);
  await expect.poll(async () => {
    const s = await page.request.get('/api/settings').then(r => r.json()); return [s.integrations.alldebrid.enabled, s.integrations.general_http.enabled];
  }).toEqual([firstAd, firstHttp]);
  await page.reload(); await openSettings(page);
  await expect(integrationInput(page, 'alldebrid')).toBeChecked({checked:firstAd});
  await expect(integrationInput(page, 'general_http')).toBeChecked({checked:firstHttp});

  const secondAd = true;
  const secondHttp = !originalHttp;
  await setIntegrationChecked(page, 'alldebrid', secondAd);
  await setIntegrationChecked(page, 'general_http', secondHttp);
  await saveSettings(page);
  await expect.poll(async () => {
    const s = await page.request.get('/api/settings').then(r => r.json()); return [s.integrations.alldebrid.enabled, s.integrations.general_http.enabled];
  }).toEqual([secondAd, secondHttp]);

  await setIntegrationChecked(page, 'alldebrid', originalAd);
  await setIntegrationChecked(page, 'general_http', originalHttp);
  await saveSettings(page);
  await expect.poll(async () => {
    const s = await page.request.get('/api/settings').then(r => r.json()); return [s.integrations.alldebrid.enabled, s.integrations.general_http.enabled];
  }).toEqual([originalAd, originalHttp]);
});

test('Recent Activity shows final provider and neutral legacy unknown without URL inference', async ({ page }) => {
  await isolateExternalFonts(page);
  const items = [listFixture(), listFixture({id:902,name:'Legacy unknown',provider_provenance_status:'unknown_legacy',current_provider_id:null,current_provider_name:null,delivering_provider_id:null,delivering_provider_name:null})];
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items,total:items.length})}));
  await page.goto('/');
  await expect(page.locator('#dash-tbody tr[data-torrent-id="901"] .dp-provider-chip')).toHaveText('HTTP & HTTPS');
  await expect(page.locator('#dash-tbody tr[data-torrent-id="902"] .dp-provider-chip')).toHaveText('Unknown');
});

test('Downloads uses current provider for active transfers and delivering provider for completed transfers', async ({ page }) => {
  await isolateExternalFonts(page);
  const items = [
    listFixture({id:903,name:'Active',status:'downloading',progress:42,delivering_provider_id:null,delivering_provider_name:null,current_provider_id:'alldebrid',current_provider_name:'AllDebrid'}),
    listFixture({id:904,name:'Completed'}),
    listFixture({id:905,name:'Pending',status:'pending',progress:0,provider_provenance_status:'pending',current_provider_id:null,current_provider_name:null,delivering_provider_id:null,delivering_provider_name:null}),
  ];
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items,total:items.length})}));
  await page.goto('/'); await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await expect(page.locator('#t-tbody tr[data-torrent-id="903"] .dp-provider-chip')).toHaveText('AllDebrid');
  await expect(page.locator('#t-tbody tr[data-torrent-id="904"] .dp-provider-chip')).toHaveText('HTTP & HTTPS');
  await expect(page.locator('#t-tbody tr[data-torrent-id="905"] .dp-provider-chip')).toHaveText('Pending');
  await expect(page.locator('#view-torrents thead')).toContainText('Provider / Source');
});

test('Details separates safe original resource, final provider, ordered failover history, and advanced executor identity', async ({ page }) => {
  await isolateExternalFonts(page);
  const detail = {...listFixture({id:906,name:'Failover detail'}), original_resource:'https://downloads.example/file.bin?…', executors:['aria2'],
    route_attempts:[
      {ordinal:5,provider_id:'alldebrid',provider_name:'AllDebrid',outcome:'failed',route_origin:null,route_location:null,route_identity:null},
      {ordinal:9,provider_id:'general_http',provider_name:'HTTP & HTTPS',outcome:'completed',route_origin:'https://mirror.example',route_location:'https://mirror.example/file.bin',route_identity:'https://mirror.example'},
    ], files:[], source_outcomes:[], events:[]};
  await page.route(url => url.pathname === '/api/torrents/906', route => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(detail)}));
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items:[detail],total:1})}));
  await page.goto('/'); await page.evaluate(() => showDetail(906));
  await expect(page.locator('.dp-detail-provider .dv')).toHaveText('HTTP & HTTPS');
  await expect(page.locator('.dp-detail-original-resource .dv')).toHaveText('https://downloads.example/file.bin?…');
  const rows = page.locator('.dp-detail-route-row'); await expect(rows).toHaveCount(2);
  // Case B11/backend-truth-only: the durable ordinal (5, 9) renders as given,
  // never index+1, and route_identity is backend-provided, never re-derived.
  await expect(rows.nth(0).locator('.dp-detail-route-order')).toHaveText('5');
  await expect(rows.nth(0)).toContainText('AllDebrid'); await expect(rows.nth(0)).toContainText('Failed');
  await expect(rows.nth(0).locator('.dp-detail-route-identity')).toHaveText('—');
  await expect(rows.nth(1).locator('.dp-detail-route-order')).toHaveText('9');
  await expect(rows.nth(1)).toContainText('HTTP & HTTPS'); await expect(rows.nth(1)).toContainText('Completed');
  await expect(rows.nth(1).locator('.dp-detail-route-identity')).toHaveText('https://mirror.example');
  await expect(rows.nth(1).locator('.dp-detail-route-identity')).toHaveAttribute('title', 'https://mirror.example/file.bin');
  await expect(page.locator('.detail-grid')).not.toContainText('aria2');
  await page.locator('.dp-detail-advanced > summary').click(); await expect(page.locator('.dp-detail-advanced-grid')).toContainText('aria2');
  await page.screenshot({path:'test-results/checkpoint-details-failover-dark-desktop.png', fullPage:true});
});

test('Route History projects backend route_identity verbatim and never reconstructs provider, domain, or cache semantics', async ({ page }) => {
  await isolateExternalFonts(page);
  // Each row carries misleading neighbours (cache fields, a debrid-looking origin, the provider id): the
  // renderer must show exactly what the backend decided in route_identity, nothing derived.
  const row = (ordinal, outcome, extra) => ({ordinal, provider_id:'alldebrid', provider_name:'AllDebrid', outcome,
    route_origin:null, route_location:null, route_identity:null, ...extra});
  const detail = {...listFixture({id:907,name:'Debrid route identity'}), original_resource:'magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567',
    executors:[], files:[], source_outcomes:[], events:[],
    route_attempts:[
      row(1,'resolved',{route_identity:'Torrent cache', cache_presence:'miss'}),
      row(2,'completed',{route_identity:'BitTorrent', cache_presence:'hit'}),
      row(3,'completed',{route_identity:'1fichier.com'}),
      row(4,'failed',{route_origin:'https://f8g9h0.debrid.it'}),
    ]};
  await page.route(url => url.pathname === '/api/torrents/907', route => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(detail)}));
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items:[detail],total:1})}));
  await page.goto('/'); await page.evaluate(() => showDetail(907));
  const identities = page.locator('.dp-detail-route-row .dp-detail-route-identity');
  await expect(identities).toHaveCount(4);
  await expect(identities).toHaveText(['Torrent cache', 'BitTorrent', '1fichier.com', '—']);
  // Hover identity is the backend identity; a logical row exposes no provider capability path.
  await expect(identities.nth(0)).toHaveAttribute('title', 'Torrent cache');
  await expect(identities.nth(1)).toHaveAttribute('title', 'BitTorrent');
  await expect(identities.nth(2)).toHaveAttribute('title', '1fichier.com');
  await expect(identities.nth(3)).toHaveAttribute('title', '');
  await expect(page.locator('.dp-detail-route-list')).not.toContainText('debrid.it');
});

test('Route History shows the canonical object source story: original, consolidated and unverified, from backend truth only', async ({ page }) => {
  await isolateExternalFonts(page);
  // Production 298/299/300 shape. Everything about relationship comes from the backend's relation /
  // verification_state / contributing_transfer_id / presentation_ordinal. The fixture is deliberately
  // adversarial toward client-side inference: durable ordinals restart per contributing transfer, a
  // "consolidated" row shares its host family with an original, and the candidate list names none of these hosts.
  const route = (presentation_ordinal, ordinal, host, relation, contributing_transfer_id, extra = {}) => ({
    ordinal, presentation_ordinal, provider_id:'general_http', provider_name:'HTTP & HTTPS', outcome:'resolved',
    route_origin:`https://${host}`, route_location:`https://${host}/releases/ubuntu.iso`, route_identity:`https://${host}`,
    relation, verification_state: relation === 'unverified' ? 'unverified' : 'verified', unverified_reason:null,
    contributing_transfer_id, ...extra});
  const verifiedCandidates = Array.from({length:8}, (_, index) => ({candidate_id:`c${index}`, source_label:`candidate-${index}.example`,
    provider_id:'general_http', relationship:index < 3 ? 'Original' : 'Consolidated', dispositions:index === 0 ? ['Active'] : [],
    is_selected:index === 0, is_active:index === 0, is_delivering:false, switch_eligible:index !== 0}));
  const detail = {...listFixture({id:298, name:'ubuntu-24.04.3-desktop-amd64.iso', status:'downloading', progress:40}),
    original_resource:'https://releases.ubuntu.com/…', executors:['aria2'], source_outcomes:[], events:[], execution_attempts:[],
    files:[{id:17419, filename:'ubuntu-24.04.3-desktop-amd64.iso', size_bytes:1024, status:'downloading', blocked:false,
      block_reason:null, candidate_count:8, acquisition_candidates:verifiedCandidates}],
    route_attempts:[
      route(1, 1, 'releases.ubuntu.com', 'original', 298),
      route(2, 2, 'mirrors.mit.edu', 'original', 298),
      route(3, 3, 'mirror.pilotfiber.com', 'original', 298),
      route(4, 1, 'mirrors.tuna.tsinghua.edu.cn', 'consolidated', 299),
      route(5, 2, 'ubuntu-releases.mirrorservice.org', 'consolidated', 299),
      route(6, 3, 'mirror.sg.gs', 'consolidated', 299),
      route(7, 1, 'mirror.serversaustralia.com.au', 'consolidated', 300),
      route(8, 2, 'mirrors.163.com', 'consolidated', 300),
      route(9, 3, 'mirrors.ustc.edu.cn', 'unverified', 300, {unverified_reason:'range_ignored'}),
      route(10, 4, 'mirrors.aliyun.com', 'unverified', 300, {unverified_reason:'range_unsupported'}),
    ]};
  await page.route(url => url.pathname === '/api/torrents/298', r => r.fulfill({status:200,contentType:'application/json',body:JSON.stringify(detail)}));
  await page.route(url => url.pathname === '/api/torrents', r => r.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items:[detail],total:1})}));
  await page.goto('/'); await page.evaluate(() => showDetail(298));

  const rows = page.locator('.dp-detail-route-row');
  await expect(rows).toHaveCount(10);  // every backend row exactly once: nothing duplicated, nothing synthesized.
  await expect(page.locator('.dp-detail-route-row .dp-detail-route-order')).toHaveText(['1','2','3','4','5','6','7','8','9','10']);
  await expect(page.locator('.dp-detail-route-row .dp-detail-route-relation')).toHaveText([
    '(Original)', '(Original)', '(Original)',
    '(Consolidated from #299)', '(Consolidated from #299)', '(Consolidated from #299)',
    '(Consolidated from #300)', '(Consolidated from #300)',
    '(From #300)', '(From #300)',
  ]);
  await expect(page.locator('.dp-detail-route-row .dp-detail-route-outcome')).toHaveText([
    ...Array(8).fill('Resolved'), 'Unverified', 'Unverified',
  ]);
  // Where a source came from is read before what happened to that route:
  // # | Provider | URL | Origin | Status, as DOM order, not as a visual hack.
  const columnOrder = await rows.nth(3).evaluate(row => Array.from(row.children)
    .map(cell => cell.className.replace('dp-detail-route-', '')));
  expect(columnOrder).toEqual(['order', 'provider', 'identity', 'relation', 'outcome']);
  const placement = await rows.nth(3).evaluate(row => {
    const box = selector => row.querySelector(selector).getBoundingClientRect();
    const [identity, relation, outcome] = ['.dp-detail-route-identity', '.dp-detail-route-relation',
      '.dp-detail-route-outcome'].map(box);
    return {identityRight: identity.right, relationLeft: relation.left, relationRight: relation.right,
            outcomeLeft: outcome.left};
  });
  expect(placement.relationLeft).toBeGreaterThanOrEqual(placement.identityRight - 1);
  expect(placement.outcomeLeft).toBeGreaterThanOrEqual(placement.relationRight - 1);
  // Provider + safe route identity are still the backend's own projection.
  await expect(rows.nth(3)).toContainText('HTTP & HTTPS');
  await expect(rows.nth(3).locator('.dp-detail-route-identity')).toHaveText('https://mirrors.tuna.tsinghua.edu.cn');
  await expect(rows.nth(3).locator('.dp-detail-route-identity')).toHaveAttribute('title', 'https://mirrors.tuna.tsinghua.edu.cn/releases/ubuntu.iso');

  // Unverified sources are visibly distinct from verified ones, and say why.
  const unverified = page.locator('.dp-detail-route-row[data-route-relation="unverified"]');
  await expect(unverified).toHaveCount(2);
  await expect(unverified.nth(0).locator('.dp-detail-route-outcome')).toHaveAttribute('data-route-outcome', 'unverified');
  await expect(unverified.nth(0).locator('.dp-detail-route-outcome')).toHaveAttribute('title', 'Equivalence unproven: range_ignored');
  const colors = await page.evaluate(() => {
    const color = selector => getComputedStyle(document.querySelector(selector)).color;
    return {verified: color('.dp-detail-route-row[data-route-relation="consolidated"] .dp-detail-route-outcome'),
            unverified: color('.dp-detail-route-row[data-route-relation="unverified"] .dp-detail-route-outcome')};
  });
  expect(colors.unverified).not.toBe(colors.verified);

  // "8 Candidates" still means eight VERIFIED candidates: ten history rows never inflate it.
  const disclosure = page.locator('tr[data-dp-artifact-id="17419"] .dp-detail-candidate-disclosure');
  await expect(disclosure.locator('.dp-candidate-chip-count')).toHaveText('8');
  await expect(disclosure).toHaveAttribute('aria-label', /^Show 8 Candidates for/);
  await page.screenshot({path:'test-results/checkpoint-details-canonical-history-dark-desktop.png', fullPage:true});

  // Light theme + narrow layout: the relation stays readable and the row never overflows the card.
  // (The open Details modal covers the toolbar, so the page's own toggle is invoked directly.)
  await page.evaluate(() => toggleTheme());
  await expect.poll(() => page.evaluate(() => document.body.classList.contains('light'))).toBeTruthy();
  await page.setViewportSize({width:680, height:900});
  const relation = rows.nth(8).locator('.dp-detail-route-relation');
  await expect(relation).toBeVisible();
  const [rowBox, relationBox] = [await rows.nth(8).boundingBox(), await relation.boundingBox()];
  expect(relationBox.x).toBeGreaterThanOrEqual(rowBox.x);
  expect(relationBox.x + relationBox.width).toBeLessThanOrEqual(rowBox.x + rowBox.width + 1);
  await page.screenshot({path:'test-results/checkpoint-details-canonical-history-light-narrow.png', fullPage:true});
});

test('Route History never infers a relationship the backend did not project', async ({ page }) => {
  await isolateExternalFonts(page);
  // No relation fields at all (a legacy-shaped payload), yet hosts and candidate summaries that LOOK like another
  // transfer's mirrors: the renderer must label nothing, and must keep showing the durable ordinal as given.
  const bare = (ordinal, host) => ({ordinal, provider_id:'general_http', provider_name:'HTTP & HTTPS', outcome:'resolved',
    route_origin:`https://${host}`, route_location:`https://${host}/ubuntu.iso`, route_identity:`https://${host}`,
    candidates:[{candidate_id:'x', source:{scope:'host', key:'mirrors.aliyun.com'}}]});
  const detail = {...listFixture({id:908, name:'No projected relation'}), executors:[], files:[], source_outcomes:[], events:[],
    route_attempts:[bare(5, 'mirrors.aliyun.com'), bare(9, 'mirrors.ustc.edu.cn'),
      // An unverified own-transfer source: backend says unverified, contributed by this same transfer.
      {...bare(11, 'mirror.example'), relation:'unverified', verification_state:'unverified', unverified_reason:'range_ignored',
       contributing_transfer_id:908, presentation_ordinal:11}]};
  await page.route(url => url.pathname === '/api/torrents/908', r => r.fulfill({status:200,contentType:'application/json',body:JSON.stringify(detail)}));
  await page.route(url => url.pathname === '/api/torrents', r => r.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items:[detail],total:1})}));
  await page.goto('/'); await page.evaluate(() => showDetail(908));
  const rows = page.locator('.dp-detail-route-row'); await expect(rows).toHaveCount(3);
  await expect(page.locator('.dp-detail-route-row .dp-detail-route-order')).toHaveText(['5', '9', '11']);
  await expect(rows.nth(0).locator('.dp-detail-route-relation')).toHaveText('');
  await expect(rows.nth(0).locator('.dp-detail-route-relation')).toBeHidden();
  await expect(rows.nth(0).locator('.dp-detail-route-outcome')).toHaveText('Resolved');
  await expect(rows.nth(0)).toHaveAttribute('data-route-relation', '');
  await expect(rows.nth(2).locator('.dp-detail-route-outcome')).toHaveText('Unverified');
  await expect(rows.nth(2).locator('.dp-detail-route-relation')).toHaveText('(Original)');  // its own source: no "From #".
  await expect(page.locator('.dp-detail-route-list')).not.toContainText('From #');
  await expect(page.locator('.dp-detail-route-list')).not.toContainText('Consolidated');
});

test('provider controls remain readable in light theme and narrow layout', async ({ page }) => {
  await isolateExternalFonts(page); await page.goto('/'); await openSettings(page);
  await page.locator('#theme-toggle').click();
  await expect.poll(() => page.evaluate(() => document.body.classList.contains('light'))).toBeTruthy();
  await page.setViewportSize({width:680,height:900});
  const httpCard = page.locator('.dp-settings-provider-card--general-http'); await expect(httpCard).toBeVisible();
  await expect(integrationControl(page, 'general_http')).toBeVisible();
  const box = await httpCard.boundingBox(); expect(box.width).toBeLessThanOrEqual(680);
  await page.screenshot({path:'test-results/checkpoint-settings-light-narrow.png', fullPage:true});
});

// --------------------------------------------------------------------------- //
// DP 1.0.12 Details Files canonical-object presentation leveling.
// Production 303/304/305 shape: 3 physical transfer-local artifacts, 8 VERIFIED
// canonical candidates, 10 canonical source/file relationships (8 verified +
// 2 terminal UNVERIFIED). Everything below is backend-projected; the renderer
// derives no relationship from a URL, host, filename or transfer adjacency.
// --------------------------------------------------------------------------- //

const CANONICAL_ISO = 'ubuntu-26.04.1-desktop-amd64.iso';
const CANONICAL_SIZE = 6442450944;

function canonicalDetail() {
  const candidates = Array.from({length:8}, (_, index) => ({
    candidate_id:`c${index}`, source_label:`mirror-${index}.example`, provider_id:'general_http',
    relationship:index < 3 ? 'Original' : 'Consolidated', dispositions:index === 0 ? ['Active'] : [],
    is_selected:index === 0, is_active:index === 0, is_delivering:false, switch_eligible:index !== 0}));
  const physical = (id, status, label, extra = {}) => ({
    id, artifact_id:id, presentation_id:`artifact:${id}`, request_id:`req-${id}`, filename:CANONICAL_ISO,
    size_bytes:CANONICAL_SIZE, status, presentation_status:status, presentation_label:label,
    presentation_badge_status:status, blocked:false, block_reason:null,
    relationship:'original', verification_state:'verified', contributing_transfer_id:303, ...extra});
  const contributed = (id, transfer) => ({
    id, artifact_id:id, presentation_id:`artifact:${id}`, request_id:`req-${id}`, filename:CANONICAL_ISO,
    size_bytes:CANONICAL_SIZE, status:'duplicate', presentation_status:'duplicate',
    presentation_label:'Duplicate', presentation_badge_status:'duplicate', blocked:false, block_reason:null,
    relationship:'consolidated', verification_state:'verified', contributing_transfer_id:transfer});
  const associated = (request_id, reason) => ({
    id:null, artifact_id:null, presentation_id:`request:${request_id}`, request_id, filename:CANONICAL_ISO,
    size_bytes:null, status:'unverified', presentation_status:'unverified', presentation_label:'Unverified',
    presentation_badge_status:'unverified', blocked:false, block_reason:null, unverified_reason:reason,
    relationship:'unverified', verification_state:'unverified', contributing_transfer_id:305});

  const canonical = physical(17432, 'downloading', 'Downloading',
    {candidate_count:8, acquisition_candidates:candidates});
  const files = [canonical, physical(17433, 'duplicate', 'Duplicate', {candidate_count:0}),
                 physical(17434, 'duplicate', 'Duplicate', {candidate_count:0})];
  return {...listFixture({id:303, name:CANONICAL_ISO, status:'downloading', progress:40, size_bytes:CANONICAL_SIZE}),
    original_resource:'https://releases.ubuntu.com/…', executors:['aria2'], source_outcomes:[], events:[],
    execution_attempts:[], route_attempts:[], file_count:3, files,
    file_presentations:[...files,
      contributed(17440, 304), contributed(17441, 304), contributed(17442, 304),
      contributed(17450, 305), contributed(17451, 305),
      associated('req-unverified-a', 'range_ignored'), associated('req-unverified-b', 'range_unsupported')]};
}

async function openCanonicalDetail(page, detail) {
  await page.route(url => url.pathname === '/api/torrents/303',
    r => r.fulfill({status:200, contentType:'application/json', body:JSON.stringify(detail)}));
  await page.route(url => url.pathname === '/api/torrents',
    r => r.fulfill({status:200, contentType:'application/json', body:JSON.stringify({items:[detail], total:1})}));
  await page.goto('/');
  await page.evaluate(() => showDetail(303));
}

test('Details Files presents the whole canonical object: native, contributed and unverified rows', async ({ page }) => {
  await isolateExternalFonts(page);
  await openCanonicalDetail(page, canonicalDetail());

  // Ten canonical source/file relationships, not the three physical artifacts.
  await expect(page.locator('.dp-detail-files-card .card-title')).toHaveText(`Files (10)`);
  const rows = page.locator('.dp-detail-files-card tr.dp-detail-file-row');
  await expect(rows).toHaveCount(10);

  // The three-column contract is retained: filename (+ provenance) / size / status.
  await expect(page.locator('.dp-detail-files-card thead th')).toHaveText(['Filename', 'Size', 'Status']);
  await expect(rows.nth(0).locator('td')).toHaveCount(3);

  // Native rows carry no redundant origin subtitle; contributed and unverified do.
  await expect(page.locator('.dp-detail-files-card tr.dp-detail-file-row .dp-detail-file-origin'))
    .toHaveText(['From #304', 'From #304', 'From #304', 'From #305', 'From #305', 'From #305', 'From #305']);
  for (const index of [0, 1, 2]) {
    await expect(rows.nth(index).locator('.dp-detail-file-origin')).toHaveCount(0);
  }

  // An unverified association has no independently known size and says so factually.
  const unverified = rows.nth(8);
  await expect(unverified.locator('.dp-detail-filename-copy')).toHaveText(CANONICAL_ISO);
  await expect(unverified.locator('td').nth(1)).toHaveText('—');
  await expect(unverified.locator('td').nth(2)).toContainText('Unverified');
  await expect(unverified.locator('.dp-detail-file-origin')).toHaveText('From #305');

  // Status stays in the right-hand column for every row kind: it is never moved
  // under the filename, and the provenance subtitle never becomes a status.
  const geometry = await page.evaluate(() => {
    const row = document.querySelectorAll('.dp-detail-files-card tr.dp-detail-file-row')[8];
    const cells = Array.from(row.querySelectorAll('td')).map(cell => cell.getBoundingClientRect().left);
    const origin = row.querySelector('.dp-detail-file-origin').getBoundingClientRect();
    const filename = row.querySelector('.dp-detail-filename-copy').getBoundingClientRect();
    return {cells, originBelowFilename: origin.top >= filename.bottom - 1, originLeft: origin.left,
            statusLeft: cells[2], filenameCellLeft: cells[0]};
  });
  expect(geometry.cells[0]).toBeLessThan(geometry.cells[1]);
  expect(geometry.cells[1]).toBeLessThan(geometry.cells[2]);
  expect(geometry.originBelowFilename).toBeTruthy();
  expect(geometry.originLeft).toBeLessThan(geometry.statusLeft);
});

test('only the canonical actionable row exposes candidate disclosure and switching', async ({ page }) => {
  await isolateExternalFonts(page);
  const posted = [];
  await page.route(url => /\/artifacts\/.+\/candidate$/.test(url.pathname), route => {
    posted.push(route.request().url());
    return route.fulfill({status:200, contentType:'application/json',
      body:JSON.stringify({filename:CANONICAL_ISO, source_host:'mirror-1.example'})});
  });
  await openCanonicalDetail(page, canonicalDetail());

  // Exactly one candidate control exists in the whole Files table, on the canonical row.
  const disclosures = page.locator('.dp-detail-files-card .dp-detail-candidate-disclosure');
  await expect(disclosures).toHaveCount(1);
  await expect(disclosures.locator('.dp-candidate-chip-count')).toHaveText('8');
  const rows = page.locator('.dp-detail-files-card tr.dp-detail-file-row');
  await expect(rows.nth(0).locator('.dp-detail-candidate-disclosure')).toHaveCount(1);
  for (const index of [1, 2, 3, 4, 5, 6, 7, 8, 9]) {
    await expect(rows.nth(index).locator('.dp-detail-candidate-disclosure')).toHaveCount(0);
  }

  // A terminal UNVERIFIED row is a presentation row only: it carries NO artifact
  // identity at all, so no code path can address it as an artifact.
  await expect(page.locator('.dp-detail-files-card tr[data-dp-row-id="request:req-unverified-a"]')).toHaveCount(1);
  await expect(page.locator('.dp-detail-files-card tr[data-dp-row-id="request:req-unverified-a"][data-dp-artifact-id]')).toHaveCount(0);
  // A contributed row names a real foreign artifact, and is still not mutable here.
  await expect(page.locator('.dp-detail-files-card tr[data-dp-row-id="artifact:17440"][data-dp-artifact-id]')).toHaveCount(0);

  // Switching from the canonical row addresses the REAL artifact id.
  await disclosures.click();
  const panel = page.locator('tr[data-dp-candidate-owner="artifact:17432"]');
  await expect(panel).toBeVisible();
  await expect(panel.locator('.dp-detail-candidate-item')).toHaveCount(8);
  await panel.locator('.dp-detail-candidate-switch').first().click();
  await expect.poll(() => posted.length).toBeGreaterThan(0);
  expect(posted.every(url => /\/artifacts\/17432\/candidate$/.test(new URL(url).pathname))).toBeTruthy();
  expect(posted.some(url => url.includes('request:'))).toBeFalsy();
  expect(posted.some(url => /\/artifacts\/(null|undefined|17440|17450)\//.test(url))).toBeFalsy();
});

test('row identity survives an authoritative refresh: expansion and focus are keyed to the presentation row', async ({ page }) => {
  await isolateExternalFonts(page);
  await openCanonicalDetail(page, canonicalDetail());

  const disclosure = page.locator('.dp-detail-files-card .dp-detail-candidate-disclosure');
  await disclosure.click();
  await expect(page.locator('tr[data-dp-candidate-owner="artifact:17432"]')).toBeVisible();
  await disclosure.focus();

  // The same background refresh the Downloads/Dashboard surfaces trigger: the
  // Files rows are re-rendered wholesale from a fresh payload.
  await page.evaluate(() => document.dispatchEvent(new CustomEvent('debridpulse:downloads-rendered')));
  await expect(page.locator('.dp-detail-files-card tr.dp-detail-file-row')).toHaveCount(10);
  // Expanded state is keyed by presentation_id, so it survives the re-render
  // instead of collapsing or reopening against the wrong row.
  await expect(page.locator('tr[data-dp-candidate-owner="artifact:17432"]')).toBeVisible();
  await expect(page.locator('tr[data-dp-candidate-owner="artifact:17432"]')).toHaveCount(1);
  await expect(page.locator('.dp-detail-files-card .dp-detail-candidate-disclosure')).toBeFocused();
  await expect(page.locator('.dp-detail-files-card .dp-detail-candidate-disclosure')).toHaveAttribute('aria-expanded', 'true');
});

test('canonical Files rows stay legible in light theme and at narrow width', async ({ page }) => {
  await isolateExternalFonts(page);
  await openCanonicalDetail(page, canonicalDetail());
  await page.evaluate(() => toggleTheme());
  await expect.poll(() => page.evaluate(() => document.body.classList.contains('light'))).toBeTruthy();
  await page.setViewportSize({width:680, height:900});

  const rows = page.locator('.dp-detail-files-card tr.dp-detail-file-row');
  await expect(rows).toHaveCount(10);
  const contributed = rows.nth(3);
  await expect(contributed.locator('.dp-detail-file-origin')).toBeVisible();
  // The subtitle stays inside its own row and never collides with the status column.
  const bounds = await page.evaluate(() => {
    const row = document.querySelectorAll('.dp-detail-files-card tr.dp-detail-file-row')[3];
    const rowBox = row.getBoundingClientRect();
    const origin = row.querySelector('.dp-detail-file-origin').getBoundingClientRect();
    const status = row.querySelectorAll('td')[2].getBoundingClientRect();
    const card = document.querySelector('.dp-detail-files-card').getBoundingClientRect();
    return {rowLeft:rowBox.left, rowRight:rowBox.right, originLeft:origin.left, originRight:origin.right,
            statusLeft:status.left, cardWidth:card.width};
  });
  expect(bounds.originLeft).toBeGreaterThanOrEqual(bounds.rowLeft - 1);
  expect(bounds.originRight).toBeLessThanOrEqual(bounds.statusLeft + 1);
  expect(bounds.cardWidth).toBeLessThanOrEqual(680);
  await page.screenshot({path:'test-results/checkpoint-details-canonical-files-light-narrow.png', fullPage:true});
});
