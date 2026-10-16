/* Activity Log behavior and render owner.
 *
 * The only implementation of loading, server-side filtering, page navigation,
 * reset and rendering for #view-events. The search box and its request
 * identity belong to the shared search lifecycle (DPSearch, ui-search.js), as
 * on Downloads. The control markup is
 * static in index.html; this module binds it once and renders only into
 * #event-list and #dp-activity-pagination. It exposes
 * its own API (DPActivityLog) and assigns no other owner's global. app.js
 * calls DPActivityLog.load() on navigation.
 *
 * The browser holds exactly one page of the durable event journal. Pages are
 * navigated by the server's cursors within one frozen snapshot (the first
 * page's): Next walks older (before), Previous to the newer neighbour (after),
 * First back to that snapshot's newest page and Last straight to its final page
 * (the server's one bounded count says which page that is, or why it cannot).
 * The shared DP pager (DPPager) presents them; this owner keeps the snapshot,
 * cursors and page number -- and the time window's cutoff the first page
 * reported (window_start), so every page filters by the same window as the
 * snapshot it shares. Any filter change, Refresh, "Show newest events" or
 * navigation back to the page starts a new investigation at the newest event.
 * The page size is the Settings value activity_log_page_size; it is not a
 * control on this page.
 *
 * Auto Refresh (#ev-live) is view-local: off whenever the page is entered or
 * reloaded, never saved. On, it follows the journal through the application's
 * one live stream (app.js forwards activity_journal_changed as
 * debridpulse:activity-journal-changed, a hint carrying only the newest
 * committed id): while the newest page is shown from its top, a burst of hints
 * is coalesced into ONE ordinary new investigation at the newest event -- the
 * same request Refresh makes, through the same search identity, filters and
 * page size -- so a row only ever comes from the journal. An older page, or a
 * newest page read below its top, is never replaced: "Show newest events" says
 * newer events exist. A reconnected stream is caught up from the journal, and
 * while the stream is down the switch says so instead of claiming to be live.
 */
(function(){
'use strict';
const PAGE_SIZES=[50,100,250],DEFAULT_PAGE_SIZE=100;
const CATEGORY_LABELS=Object.freeze({transfer:'Transfer',routing:'Routing',resource:'Provider Resource',selection:'File Selection',consolidation:'Consolidation',execution:'Download',recovery:'Recovery',storage:'Storage',extraction:'Extraction',input:'Input Required',integration:'Integration',configuration:'Configuration',administration:'Administration'});
let search=null,bound=false;
let cursor={snapshot:null,windowStart:null},request={},nav=null,pending=false;
// Auto Refresh: on/off, a coalescing timer, a hint that arrived while a request
// was in flight (dirty), the newest id last hinted, and the investigation a
// newer-events probe belongs to.
const COALESCE_MS=750,TOP_SLACK=4;
let live=false,timer=null,dirty=false,hinted=null,investigation=0;
function controls(){const search=document.getElementById('ev-search'),timeframe=document.getElementById('ev-timeframe'),severity=document.getElementById('ev-level'),category=document.getElementById('ev-category'),reset=document.getElementById('ev-reset'),pagination=document.getElementById('dp-activity-pagination'),info=document.getElementById('dp-activity-page-info'),pager=document.getElementById('dp-activity-page-btns'),newest=document.getElementById('ev-newest');return search&&timeframe&&severity&&category&&reset?{search,timeframe,severity,category,reset,pagination,info,pager,newest}:null;}
function filters(c,text){return Boolean(text||(c.timeframe.value||'all')!=='all'||(c.severity.value||'')!==''||(c.category.value||'')!=='');}
function syncReset(c){c.reset.hidden=!filters(c,c.search.value.trim());}
function pageSize(){let value=DEFAULT_PAGE_SIZE;try{value=Number(settingsData?.activity_log_page_size);}catch(_){}return PAGE_SIZES.includes(value)?value:DEFAULT_PAGE_SIZE;}
// nav: the accepted page -- {page, first, oldest, following, pages, lastUnavailable}.
function restart(){cursor={snapshot:null,windowStart:null};request={};nav=null;investigation++;}
// One page request within the snapshot; ``page`` is resolved from the accepted answer.
function go(target){if(pending||!nav)return;request=target;void fetchPage();}
const ACTIONS=Object.freeze({first:()=>go({kind:'first'}),previous:()=>go({kind:'previous',after:nav.first}),next:()=>go({kind:'next',before:nav.oldest}),last:()=>go({kind:'last'})});
function changed(c){syncReset(c);restart();void fetchPage();}
function bind(){const c=controls();if(!c||bound)return c;bound=true;
search=window.DPSearch.attach({input:c.search,noun:'events',onCommit:()=>changed(c)});
c.search.addEventListener('input',()=>syncReset(c));
c.timeframe.addEventListener('change',()=>changed(c));
c.severity.addEventListener('change',()=>changed(c));
c.category.addEventListener('change',()=>changed(c));
c.reset.addEventListener('click',()=>{search.clear();c.timeframe.value='all';c.severity.value='';c.category.value='';for(const select of [c.timeframe,c.severity,c.category]){try{window.DPDropdowns?.sync(select);}catch(_){}}changed(c);});
c.newest?.addEventListener('click',()=>{restart();void fetchPage();});
const toggle=document.getElementById('ev-live');toggle?.addEventListener('change',()=>{live=toggle.checked;stopFollowing();syncLive();if(live)catchUp();});
document.getElementById('event-list')?.addEventListener('scroll',()=>{if(live&&c.newest&&!c.newest.hidden&&following())schedule();},{passive:true});
syncReset(c);syncLive();return c;}
function parseTimestamp(value){if(!value)return null;try{if(typeof parseApiDate==='function')return parseApiDate(value);}catch(_){}const raw=String(value),d=new Date(/[zZ]|[+-]\d\d:\d\d$/.test(raw)?raw:`${raw.replace(' ','T')}Z`);return Number.isNaN(d.getTime())?null:d;}
function formatTimestamp(value){const d=parseTimestamp(value);if(!d)return String(value||'—');let zone='UTC';try{zone=String(settingsData?.timezone||'').trim()||'UTC';}catch(_){}try{return new Intl.DateTimeFormat('en-US',{timeZone:zone,year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(d);}catch(_){return d.toLocaleString();}}
function empty(list,text){const e=document.createElement('div');e.className='empty';e.textContent=text;list.appendChild(e);}
function subjectLine(event){const parts=[CATEGORY_LABELS[event.category]||'Event'];const name=event.name?String(event.name):'';if(name)parts.push(name);if(event.transfer_id!==null&&event.transfer_id!==undefined)parts.push(`#${event.transfer_id}`);return parts.join(' · ');}
function render(payload,c){const list=document.getElementById('event-list');if(!list)return;list.replaceChildren();const items=Array.isArray(payload?.items)?payload.items:[];if(!items.length){if(search.completeness()!=='complete')empty(list,'No matching events among those searched so far.');else if(filters(c,search.accepted()))empty(list,'No events match your filters.');else empty(list,payload?.history_started_at?`No events recorded yet. Event history begins ${formatTimestamp(payload.history_started_at)}.`:'No events recorded yet.');}
for(const event of items){const row=document.createElement('div');row.className='dp-activity-row';row.dataset.eventType=String(event.type||'');const sev=String(event.severity||'info').toLowerCase();const level=document.createElement('div');level.className=`elevel dp-activity-level ${sev==='warning'?'warn':sev==='error'?'error':'info'}`;level.setAttribute('role','img');level.setAttribute('aria-label',sev==='warning'?'Warning':sev==='error'?'Error':'Info');const copy=document.createElement('div');copy.className='dp-activity-copy';const msg=document.createElement('div');msg.className='emsg dp-activity-message';msg.textContent=String(event.message??'');copy.appendChild(msg);const subject=document.createElement('div');subject.className='ename dp-activity-transfer';subject.textContent=subjectLine(event);copy.appendChild(subject);if(event.detail){const detail=document.createElement('details');detail.className='dp-activity-detail';const summary=document.createElement('summary');summary.textContent='Details';const body=document.createElement('div');body.className='dp-activity-detail-text';body.textContent=String(event.detail);detail.append(summary,body);copy.appendChild(detail);}const time=document.createElement('div');time.className='etime dp-activity-time';time.textContent=formatTimestamp(event.occurred_at);row.append(level,copy,time);list.appendChild(row);}
document.dispatchEvent(new CustomEvent('debridpulse:activity-rendered'));}
// The page an answer to a `kind` request lands on, from the page it left.
function pageOf(kind,from,payload){let page=kind==='next'?from+1:kind==='previous'?from-1:kind==='last'?Number(payload?.page)||1:1;if(kind==='previous'&&payload?.has_newer===false)page=1;return Math.max(1,page);}
function accept(payload){const items=Array.isArray(payload?.items)?payload.items:[];const previous=nav||{page:1,pages:null,lastUnavailable:''};const kind=request.kind;
const page=pageOf(kind,previous.page,payload);
// A page count is only ever the server's (from Last); an answer whose final page is unproven cannot offer Last.
const pages=kind==='last'?page:previous.pages;
const lastUnavailable=payload?.search?.finality==='unproven'?'The last page cannot be found until every event has been searched.':previous.lastUnavailable||'';
nav={page,pages,lastUnavailable,first:items.length?items[0].id:null,oldest:payload?.has_more?payload.next_before:null,following:payload?.has_more?(payload.more_after_next?2:1):0};}
// An answer that could not search every event says why here, with its pages.
const INCOMPLETE=Object.freeze({index_incomplete:'Recent events are still being indexed; matches among them may be missing.',unavailable:'Text search is unavailable on this installation; only the transfer ID was matched.'});
function renderPagination(payload,c){if(!c.pagination||!nav)return;c.newest.hidden=!payload?.newer_available;const incomplete=INCOMPLETE[search.completeness()]||'';c.info.textContent=[nav.pages?`Page ${nav.page} of ${nav.pages}`:'',incomplete].filter(Boolean).join(' · ');
window.DPPager.render(c.pager,{page:nav.page,following:nav.following,actions:ACTIONS,lastUnavailable:nav.lastUnavailable,pending});
c.pagination.hidden=!(nav.page>1||nav.following>0||payload?.newer_available||incomplete);}
function renderFailure(c,message){const list=document.getElementById('event-list');if(list){list.replaceChildren();empty(list,`Events could not be loaded: ${message}`);}if(c.pagination)c.pagination.hidden=true;}
function setPending(c,value){pending=value;window.DPPager.pending(c.pager,value);}
// ── Auto Refresh ──
const active=()=>Boolean(document.getElementById('view-events')?.classList.contains('active'));
const connected=()=>document.documentElement.dataset.dpPulse==='connected';
// The newest page, read from its top: the only view a new investigation may replace.
function following(){const list=document.getElementById('event-list');return Boolean(nav&&nav.page===1&&(!list||list.scrollTop<=TOP_SLACK));}
function stopFollowing(){if(timer){clearTimeout(timer);timer=null;}dirty=false;}
function syncLive(){const input=document.getElementById('ev-live');if(!input)return;input.checked=live;const island=input.closest('.dp-activity-live'),status=document.getElementById('ev-live-status');const state=!live?'off':connected()?'live':'unavailable';
const text=state==='off'?'Off':state==='live'?'On: new events appear as they are recorded.':'On, but live updates are not connected. Reconnecting; Refresh still loads the newest events.';
if(island){island.dataset.state=state;if(state==='unavailable')island.title=text;else island.removeAttribute('title');}if(status)status.textContent=text;}
function schedule(){if(!timer)timer=setTimeout(()=>{timer=null;catchUp();},COALESCE_MS);}
// Bring the view up to the journal: a new investigation at the newest event
// when following, else only say whether newer events exist.
function catchUp(){if(!live||!active())return;const c=bind();if(!c)return;if(pending){dirty=true;return;}
if(!nav||following()){const kept={cursor,request,nav};restart();void fetchPage({quiet:kept});return;}
if(hinted!==null&&cursor.snapshot!==null){if(hinted!==cursor.snapshot)renderPagination({newer_available:true},c);return;}
void probeNewer(c);}
// Whether events newer than this investigation's snapshot exist: the page's
// own newer_available, from one bounded one-row request in the same snapshot.
async function probeNewer(c){const snapshot=cursor.snapshot,mine=investigation;if(snapshot===null)return;
try{const payload=await api('GET',`/events?limit=1&snapshot=${encodeURIComponent(String(snapshot))}`);if(mine===investigation&&live&&payload?.newer_available)renderPagination({newer_available:true},c);}catch(_){}}
function hint(event){const latest=Number(event?.detail?.latest);if(Number.isInteger(latest)&&latest>=0)hinted=latest;if(!live||!active())return;if(hinted!==null&&hinted===cursor.snapshot)return;schedule();}
document.addEventListener('debridpulse:activity-journal-changed',hint);
document.addEventListener('debridpulse:pulse-connected',()=>{hinted=null;syncLive();if(live)catchUp();});
document.addEventListener('debridpulse:pulse-lost',()=>{hinted=null;syncLive();});
// Every entry to the page, and every departure, starts with Auto Refresh off.
document.addEventListener('debridpulse:navigation',()=>{live=false;stopFollowing();syncLive();});
async function fetchPage({quiet=false}={}){const c=bind();if(!c||typeof api!=='function')return null;const ticket=search.request();const params=new URLSearchParams();params.set('limit',String(pageSize()));params.set('timeframe',c.timeframe.value||'all');if(c.severity.value)params.set('level',c.severity.value);if(c.category.value)params.set('category',c.category.value);if(ticket.query)params.set('search',ticket.query);if(cursor.snapshot!==null)params.set('snapshot',String(cursor.snapshot));if(cursor.windowStart!==null)params.set('window_start',String(cursor.windowStart));if(request.before!=null)params.set('before',String(request.before));if(request.after!=null)params.set('after',String(request.after));if(request.kind==='last')params.set('last','true');const list=document.getElementById('event-list');list?.setAttribute('aria-busy','true');setPending(c,true);
try{const payload=await api('GET',`/events?${params.toString()}`,null,0,{signal:ticket.signal});if(!search.settle(ticket,payload?.search))return null;if(cursor.snapshot===null&&Number.isInteger(payload?.snapshot)){cursor.snapshot=payload.snapshot;cursor.windowStart=Number.isFinite(payload?.window_start)?payload.window_start:null;}const commit=()=>{accept(payload);pending=false;render(payload,c);renderPagination(payload,c);syncReset(c);};
// A page turn within the snapshot (the pager's First, Previous, Next, Last) slides from the page on screen; every other answer is shown in place.
const from=nav?.page||1,to=nav&&['first','previous','next','last'].includes(request.kind)?pageOf(request.kind,from,payload):from;
if(to!==from&&window.DPMotion)await window.DPMotion.slide({element:document.getElementById('event-list'),axis:'x',direction:Math.sign(to-from),distance:Math.abs(to-from)},()=>{if(search.current(ticket))commit();}).done;else commit();
if(dirty){dirty=false;schedule();}return payload;}
catch(error){if(!search.current(ticket))return null;const message=(()=>{try{return sanitizeErrorMsg(error?.message||error);}catch(_){return String(error?.message||error);}})();
// A Last the server cannot prove leaves the page as it was and says why.
if(request.kind==='last'&&error?.status===409&&nav){nav={...nav,lastUnavailable:String(error.detail?.message||message)};setPending(c,false);renderPagination({newer_available:!c.newest.hidden},c);try{toast(nav.lastUnavailable,'warn');}catch(_){}return null;}
setPending(c,false);
// An Auto Refresh that fails keeps the page -- and its snapshot, cursors and
// pager -- as they were and says nothing; the next hint or reconnect tries again.
if(quiet){cursor=quiet.cursor;request=quiet.request;nav=quiet.nav;dirty=false;return null;}
if(search.fail(ticket,error)==='retried')return null;renderFailure(c,message);try{toast(message,'error');}catch(_){}return null;}
finally{if(search.current(ticket))list?.removeAttribute('aria-busy');}}
function load(){restart();return fetchPage();}
window.DPActivityLog=Object.freeze({load,formatTimestamp});
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',bind,{once:true});else bind();
})();
