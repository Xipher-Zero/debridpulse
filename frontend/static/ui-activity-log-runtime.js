/* Activity Log behavior and render owner.
 *
 * The only implementation of loading, server-side filtering (the applied
 * filters, their panel and Clear Filters), page navigation and rendering for
 * #view-events. The search box and its request
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
 * Auto Refresh (#ev-live) is on unless the operator switched it off: the choice
 * lasts for the browser tab's session (sessionStorage) -- through other views,
 * reloads and any time the tab is in the background -- and a new tab session
 * starts on again; it is nothing the server keeps. On, it follows the journal
 * through the application's
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
// The operator's Auto Refresh choice for this tab session: on unless switched off (storage unavailable: on).
const LIVE_CHOICE='dp-activity-auto-refresh';
function chosen(){try{return sessionStorage.getItem(LIVE_CHOICE)!=='off';}catch(_){return true;}}
function choose(on){try{sessionStorage.setItem(LIVE_CHOICE,on?'on':'off');}catch(_){}}
function controls(){const search=document.getElementById('ev-search'),filter=document.getElementById('ev-filter'),label=document.getElementById('ev-filter-label'),applied=document.getElementById('ev-filter-applied'),panel=document.getElementById('ev-filter-panel'),pagination=document.getElementById('dp-activity-pagination'),info=document.getElementById('dp-activity-page-info'),pager=document.getElementById('dp-activity-page-btns'),newest=document.getElementById('ev-newest');return search&&filter&&label&&applied&&panel?{search,filter,label,applied,panel,pagination,info,pager,newest}:null;}
// ── Filters ──
// The applied filters are the only values a request reads: Available history,
// All and All until the operator applies another combination. The panel's
// radios (index.html, the one list of options, labels and values) are a draft
// of them while it is open -- set from the applied filters whenever it opens or
// closes, read once by Apply, never by a request, Auto Refresh or the pager.
const FILTERS=Object.freeze(['timeframe','level','category']),DEFAULT_FILTERS=Object.freeze({timeframe:'all',level:'',category:''});
let applied=DEFAULT_FILTERS;
const restricted=()=>FILTERS.some(key=>applied[key]!==DEFAULT_FILTERS[key]);
function filters(text){return Boolean(text||restricted());}
const group=(c,key)=>c.panel.querySelector(`fieldset[data-filter="${key}"]`);
const radios=(c,key)=>Array.from(group(c,key)?.querySelectorAll('input[type="radio"]')||[]);
function setDraft(c,values){for(const key of FILTERS)for(const radio of radios(c,key))radio.checked=radio.value===values[key];}
function draft(c){return Object.freeze(Object.fromEntries(FILTERS.map(key=>[key,radios(c,key).find(radio=>radio.checked)?.value??DEFAULT_FILTERS[key]])));}
// The applied restrictions in the panel's own words, in its column order:
// "Time Window: Last hour · Severity: Error". Columns at their defaults restrict nothing.
function restrictions(c){return FILTERS.filter(key=>applied[key]!==DEFAULT_FILTERS[key]).map(key=>{const option=radios(c,key).find(radio=>radio.value===applied[key]);return `${group(c,key).querySelector('legend').textContent.trim()}: ${option?.closest('label')?.textContent.trim()||applied[key]}`;}).join(' · ');}
// One control, two functions: with the defaults applied it opens the panel
// (Filter Results); with any other combination applied it clears it (Clear
// Filters), and its DP tooltip and description name what is applied.
function syncFilter(c){const t=c.filter,clear=restricted(),own=(t.getAttribute('aria-describedby')||'').split(/\s+/).filter(id=>id&&id!==c.applied.id);
t.dataset.state=clear?'clear':'filter';c.label.textContent=clear?'Clear Filters':'Filter Results';
if(clear){const text=restrictions(c);c.applied.textContent=text;t.dataset.tooltip=`Clear filters — ${text}`;t.setAttribute('aria-describedby',[c.applied.id,...own].join(' '));t.removeAttribute('popovertarget');t.removeAttribute('aria-haspopup');t.removeAttribute('aria-expanded');}
else{c.applied.textContent='';t.removeAttribute('data-tooltip');if(own.length)t.setAttribute('aria-describedby',own.join(' '));else t.removeAttribute('aria-describedby');t.setAttribute('popovertarget',c.panel.id);t.setAttribute('aria-haspopup','dialog');t.setAttribute('aria-expanded',String(c.panel.matches(':popover-open')));}}
// The applied filters become ``next`` (one new investigation, if they changed).
function applyFilters(c,next){if(FILTERS.every(key=>next[key]===applied[key]))return;applied=next;syncFilter(c);changed(c);}
// The panel opens below the control, its right edge on the control's (above it
// where there is more room), inside the window; its columns scroll, its actions stay.
const EDGE=8,GAP=6;
function place(c){const p=c.panel;if(!p.matches(':popover-open'))return;const box=c.filter.getBoundingClientRect(),vw=document.documentElement.clientWidth,vh=document.documentElement.clientHeight;
p.style.maxHeight='';p.style.left=`${EDGE}px`;p.style.top=`${EDGE}px`;const size=p.getBoundingClientRect();
const below=vh-box.bottom-GAP-EDGE,above=box.top-GAP-EDGE,up=size.height>below&&above>below,room=Math.max(0,up?above:below);
p.style.maxHeight=`${room}px`;const height=Math.min(size.height,room);
p.style.left=`${Math.max(EDGE,Math.min(box.right-size.width,vw-EDGE-size.width))}px`;p.style.top=`${up?box.top-GAP-height:box.bottom+GAP}px`;}
function pageSize(){let value=DEFAULT_PAGE_SIZE;try{value=Number(settingsData?.activity_log_page_size);}catch(_){}return PAGE_SIZES.includes(value)?value:DEFAULT_PAGE_SIZE;}
// nav: the accepted page -- {page, first, oldest, following, pages, lastUnavailable}.
function restart(){cursor={snapshot:null,windowStart:null};request={};nav=null;investigation++;}
// One page request within the snapshot; ``page`` is resolved from the accepted answer.
// A page turn answers at once: it slides to its destination before that page is
// fetched, drawn as a placeholder (or, already answered, as itself), and the
// answer crossfades into it (ui-motion.js). Nothing else slides.
let turn=null;// {request, source, motion, shown, paint, ended}
function go(target){if(pending||!nav)return;request=target;const from=nav.page;
// Where it lands, as far as is known now. Last is the server's own page when the count is known; otherwise the next page
// when it is the final one, or -- several pages on, how many is not yet known -- the far end (the longest travel).
const to=target.kind==='next'?from+1:target.kind==='previous'?from-1:target.kind==='first'?1:nav.pages||(nav.following>1?from+4:from+1);
// Any page but the last is a full page; the last one's rows are not known until it is read.
const full=target.kind==='previous'||target.kind==='first'||(target.kind==='next'&&nav.following>1);
endTurn(turn);if(to!==from&&window.DPMotion)beginTurn(target,to-from,full?pageSize():null);void fetchPage();}
function beginTurn(target,distance,rows){const list=document.getElementById('event-list');if(!list)return;const t={request:target,source:Array.from(list.childNodes),motion:null,shown:false,paint:null,ended:false};turn=t;
t.motion=window.DPMotion.slide({element:list,axis:'x',direction:Math.sign(distance),distance:Math.abs(distance)},()=>{if(t.ended&&!t.paint)return;if(t.paint)t.paint();else{placeholder(list,rows);t.shown=true;}});}
// The turn is over without its page being shown here; a slide not yet begun is skipped.
function endTurn(t){if(!t||t.ended)return;t.ended=true;if(turn===t)turn=null;if(!t.shown)t.motion?.skip();}
// Rows shaped like events, nothing in them: as many as the destination holds
// when that is known (never more than the list shows at once), else a modest
// share of the list -- each the height of a row on screen.
function placeholder(list,rows){const height=list.querySelector('.dp-activity-row')?.getBoundingClientRect().height||52;const fits=Math.max(1,Math.ceil(list.clientHeight/height));
const block=document.createElement('div');block.className='dp-activity-placeholder';block.dataset.dpPlaceholder=rows?String(rows):'';block.setAttribute('aria-hidden','true');
block.innerHTML=`<div class="dp-activity-placeholder-row dp-page-placeholder-row" style="height:${Math.round(height)}px"><span class="dp-ph dp-ph--dot"></span><div class="dp-activity-copy"><span class="dp-ph dp-ph--line"></span><span class="dp-ph dp-ph--sub"></span></div><span class="dp-ph dp-ph--time"></span></div>`.repeat(rows?Math.min(rows,fits):Math.ceil(fits/2));
list.scrollTop=0;list.replaceChildren(block);}
// Whatever replaces a placeholder on screen crossfades into it.
function replace(commit){const list=document.getElementById('event-list'),block=list?.querySelector(':scope > .dp-activity-placeholder');
if(block&&window.DPMotion)window.DPMotion.resolve({region:block,host:list,content:()=>Array.from(list.children).filter(node=>!node.classList.contains('dp-motion-still'))},commit);else commit();}
const ACTIONS=Object.freeze({first:()=>go({kind:'first'}),previous:()=>go({kind:'previous',after:nav.first}),next:()=>go({kind:'next',before:nav.oldest}),last:()=>go({kind:'last'})});
function changed(c){restart();void fetchPage();}
function bind(){const c=controls();if(!c||bound)return c;bound=true;
search=window.DPSearch.attach({input:c.search,noun:'events',onCommit:()=>changed(c)});
// Clear Filters: the defaults at once, Search untouched. (Filter Results opens the panel through popovertarget.)
c.filter.addEventListener('click',event=>{if(!restricted())return;event.preventDefault();applyFilters(c,DEFAULT_FILTERS);});
// Every open starts from the applied filters; every close -- Apply, Cancel, Escape, a press outside -- leaves no draft behind.
c.panel.addEventListener('beforetoggle',event=>{if(event.newState==='open')setDraft(c,applied);});
c.panel.addEventListener('toggle',event=>{const open=event.newState==='open';if(!restricted())c.filter.setAttribute('aria-expanded',String(open));
if(open){place(c);(radios(c,FILTERS[0]).find(radio=>radio.checked)||radios(c,FILTERS[0])[0])?.focus();}else setDraft(c,applied);});
c.panel.addEventListener('keydown',event=>{if(event.key==='Enter'&&event.target.matches?.('input[type="radio"]')){event.preventDefault();event.target.checked=true;}});
c.panel.querySelector('[data-filter-action="cancel"]')?.addEventListener('click',()=>c.panel.hidePopover());
c.panel.querySelector('[data-filter-action="apply"]')?.addEventListener('click',()=>{const next=draft(c);c.panel.hidePopover();applyFilters(c,next);});
window.addEventListener('resize',()=>place(c));
document.addEventListener('scroll',()=>place(c),{capture:true,passive:true});
c.newest?.addEventListener('click',()=>{restart();void fetchPage();});
const toggle=document.getElementById('ev-live');toggle?.addEventListener('change',()=>{live=toggle.checked;choose(live);stopFollowing();syncLive();if(live)catchUp();});
document.getElementById('event-list')?.addEventListener('scroll',()=>{if(live&&c.newest&&!c.newest.hidden&&following())schedule();},{passive:true});
syncFilter(c);syncLive();return c;}
function parseTimestamp(value){if(!value)return null;try{if(typeof parseApiDate==='function')return parseApiDate(value);}catch(_){}const raw=String(value),d=new Date(/[zZ]|[+-]\d\d:\d\d$/.test(raw)?raw:`${raw.replace(' ','T')}Z`);return Number.isNaN(d.getTime())?null:d;}
function formatTimestamp(value){const d=parseTimestamp(value);if(!d)return String(value||'—');let zone='UTC';try{zone=String(settingsData?.timezone||'').trim()||'UTC';}catch(_){}try{return new Intl.DateTimeFormat('en-US',{timeZone:zone,year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(d);}catch(_){return d.toLocaleString();}}
function empty(list,text){const e=document.createElement('div');e.className='empty';e.textContent=text;list.appendChild(e);}
function subjectLine(event){const parts=[CATEGORY_LABELS[event.category]||'Event'];const name=event.name?String(event.name):'';if(name)parts.push(name);if(event.transfer_id!==null&&event.transfer_id!==undefined)parts.push(`#${event.transfer_id}`);return parts.join(' · ');}
function render(payload,c){const list=document.getElementById('event-list');if(!list)return;list.replaceChildren();const items=Array.isArray(payload?.items)?payload.items:[];if(!items.length){if(search.completeness()!=='complete')empty(list,'No matching events among those searched so far.');else if(filters(search.accepted()))empty(list,'No events match your filters.');else empty(list,payload?.history_started_at?`No events recorded yet. Event history begins ${formatTimestamp(payload.history_started_at)}.`:'No events recorded yet.');}
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
if(island){island.dataset.state=state;if(state==='unavailable')island.dataset.tooltip=text;else island.removeAttribute('data-tooltip');}if(status)status.textContent=text;}
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
// Every entry to the page restores the session's choice; a departure stops following.
document.addEventListener('debridpulse:navigation',event=>{live=event.detail?.view==='events'&&chosen();stopFollowing();syncLive();const panel=document.getElementById('ev-filter-panel');if(event.detail?.view!=='events'&&panel?.matches(':popover-open'))panel.hidePopover();});
async function fetchPage({quiet=false}={}){const c=bind();if(!c||typeof api!=='function')return null;const ticket=search.request();const params=new URLSearchParams();params.set('limit',String(pageSize()));params.set('timeframe',applied.timeframe);if(applied.level)params.set('level',applied.level);if(applied.category)params.set('category',applied.category);if(ticket.query)params.set('search',ticket.query);if(cursor.snapshot!==null)params.set('snapshot',String(cursor.snapshot));if(cursor.windowStart!==null)params.set('window_start',String(cursor.windowStart));if(request.before!=null)params.set('before',String(request.before));if(request.after!=null)params.set('after',String(request.after));if(request.kind==='last')params.set('last','true');const list=document.getElementById('event-list');list?.setAttribute('aria-busy','true');setPending(c,true);
try{const payload=await api('GET',`/events?${params.toString()}`,null,0,{signal:ticket.signal});if(!search.settle(ticket,payload?.search))return null;if(cursor.snapshot===null&&Number.isInteger(payload?.snapshot)){cursor.snapshot=payload.snapshot;cursor.windowStart=Number.isFinite(payload?.window_start)?payload.window_start:null;}const commit=()=>{accept(payload);pending=false;render(payload,c);renderPagination(payload,c);};
// The page turn this request makes is already sliding here: its placeholder crossfades into the page, or -- not yet drawn -- the slide draws it. Every other answer is shown in place.
const t=turn;if(t&&t.request===request){turn=null;t.ended=true;if(t.shown)replace(commit);else{t.paint=()=>{if(search.current(ticket))replace(commit);};await t.motion.done;}}
else{endTurn(t);replace(commit);}
if(dirty){dirty=false;schedule();}return payload;}
catch(error){if(!search.current(ticket))return null;const message=(()=>{try{return sanitizeErrorMsg(error?.message||error);}catch(_){return String(error?.message||error);}})();
// The turn did not happen: the page it left comes back.
const t=turn;endTurn(t);const back=()=>{if(t?.shown)replace(()=>list?.replaceChildren(...t.source));};
// A Last the server cannot prove leaves the page as it was and says why.
if(request.kind==='last'&&error?.status===409&&nav){back();nav={...nav,lastUnavailable:String(error.detail?.message||message)};setPending(c,false);renderPagination({newer_available:!c.newest.hidden},c);try{toast(nav.lastUnavailable,'warn');}catch(_){}return null;}
setPending(c,false);
// An Auto Refresh that fails keeps the page -- and its snapshot, cursors and
// pager -- as they were and says nothing; the next hint or reconnect tries again.
if(quiet){cursor=quiet.cursor;request=quiet.request;nav=quiet.nav;dirty=false;return null;}
if(search.fail(ticket,error)==='retried'){back();return null;}replace(()=>renderFailure(c,message));try{toast(message,'error');}catch(_){}return null;}
finally{if(search.current(ticket))list?.removeAttribute('aria-busy');}}
function load(){restart();return fetchPage();}
window.DPActivityLog=Object.freeze({load,formatTimestamp});
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',bind,{once:true});else bind();
})();
