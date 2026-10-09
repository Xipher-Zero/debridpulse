/* Activity Log behavior and render owner.
 *
 * The only implementation of loading, server-side filtering, search debounce,
 * page navigation, reset and rendering for #view-events. The control markup is
 * static in index.html; this module binds it once and renders only into
 * #event-list, #dp-activity-result-note and #dp-activity-pagination. It exposes
 * its own API (DPActivityLog) and assigns no other owner's global. app.js
 * calls DPActivityLog.load() on navigation.
 *
 * The browser holds exactly one page of the durable event journal. Pages are
 * navigated by the server's cursor: the first page's snapshot freezes the
 * investigation's newest edge, Next walks older (next_before) and Previous
 * returns through the cursors already walked. Any filter change, Refresh or
 * navigation back to the page starts a new investigation at the newest event.
 * The page size is the Settings value activity_log_page_size; it is not a
 * control on this page.
 */
(function(){
'use strict';
const SEARCH_DEBOUNCE_MS=250,PAGE_SIZES=[50,100,250],DEFAULT_PAGE_SIZE=100;
const CATEGORY_LABELS=Object.freeze({transfer:'Transfer',routing:'Routing',resource:'Provider Resource',selection:'File Selection',consolidation:'Consolidation',execution:'Download',recovery:'Recovery',storage:'Storage',extraction:'Extraction',input:'Input Required',integration:'Integration',configuration:'Configuration',administration:'Administration'});
let generation=0,timer=null,bound=false;
let cursor={snapshot:null,before:null,stack:[]},next=null;
function controls(){const search=document.getElementById('ev-search'),timeframe=document.getElementById('ev-timeframe'),severity=document.getElementById('ev-level'),category=document.getElementById('ev-category'),reset=document.getElementById('ev-reset'),note=document.getElementById('dp-activity-result-note'),pagination=document.getElementById('dp-activity-pagination'),info=document.getElementById('dp-activity-page-info'),older=document.getElementById('ev-older'),newer=document.getElementById('ev-newer'),newest=document.getElementById('ev-newest');return search&&timeframe&&severity&&category&&reset?{search,timeframe,severity,category,reset,note,pagination,info,older,newer,newest}:null;}
function filters(c){return Boolean(c.search.value.trim()||(c.timeframe.value||'all')!=='all'||(c.severity.value||'')!==''||(c.category.value||'')!=='');}
function syncReset(c){c.reset.hidden=!filters(c);}
function pageSize(){let value=DEFAULT_PAGE_SIZE;try{value=Number(settingsData?.activity_log_page_size);}catch(_){}return PAGE_SIZES.includes(value)?value:DEFAULT_PAGE_SIZE;}
function restart(){cursor={snapshot:null,before:null,stack:[]};next=null;}
function changed(c){syncReset(c);restart();void fetchPage();}
function bind(){const c=controls();if(!c||bound)return c;bound=true;
c.search.addEventListener('input',()=>{syncReset(c);if(timer)clearTimeout(timer);timer=setTimeout(()=>{timer=null;restart();void fetchPage();},SEARCH_DEBOUNCE_MS);});
c.timeframe.addEventListener('change',()=>changed(c));
c.severity.addEventListener('change',()=>changed(c));
c.category.addEventListener('change',()=>changed(c));
c.reset.addEventListener('click',()=>{if(timer){clearTimeout(timer);timer=null;}c.search.value='';c.timeframe.value='all';c.severity.value='';c.category.value='';for(const select of [c.timeframe,c.severity,c.category]){try{window.DPDropdowns?.sync(select);}catch(_){}}changed(c);});
c.older?.addEventListener('click',()=>{if(next===null)return;cursor.stack.push(cursor.before);cursor.before=next;void fetchPage();});
c.newer?.addEventListener('click',()=>{if(!cursor.stack.length)return;cursor.before=cursor.stack.pop();void fetchPage();});
c.newest?.addEventListener('click',()=>{restart();void fetchPage();});
syncReset(c);return c;}
function parseTimestamp(value){if(!value)return null;try{if(typeof parseApiDate==='function')return parseApiDate(value);}catch(_){}const raw=String(value),d=new Date(/[zZ]|[+-]\d\d:\d\d$/.test(raw)?raw:`${raw.replace(' ','T')}Z`);return Number.isNaN(d.getTime())?null:d;}
function formatTimestamp(value){const d=parseTimestamp(value);if(!d)return String(value||'—');let zone='UTC';try{zone=String(settingsData?.timezone||'').trim()||'UTC';}catch(_){}try{return new Intl.DateTimeFormat('en-US',{timeZone:zone,year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(d);}catch(_){return d.toLocaleString();}}
function empty(list,text){const e=document.createElement('div');e.className='empty';e.textContent=text;list.appendChild(e);}
function subjectLine(event){const parts=[CATEGORY_LABELS[event.category]||'Event'];const name=event.name?String(event.name):'';if(name)parts.push(name);if(event.transfer_id!==null&&event.transfer_id!==undefined)parts.push(`#${event.transfer_id}`);return parts.join(' · ');}
function render(payload,c){const list=document.getElementById('event-list');if(!list)return;list.replaceChildren();const items=Array.isArray(payload?.items)?payload.items:[];if(!items.length){const search=payload?.search||{};if(search.text==='unavailable')empty(list,'No events for this transfer ID. Text search is unavailable on this installation, so text matches could not be checked.');else if(search.complete===false)empty(list,'No matches in the searchable history yet. Recent events are still being indexed and may match; try again shortly.');else if(filters(c))empty(list,'No events match your filters.');else empty(list,payload?.history_started_at?`No events recorded yet. Event history begins ${formatTimestamp(payload.history_started_at)}.`:'No events recorded yet.');}
for(const event of items){const row=document.createElement('div');row.className='dp-activity-row';row.dataset.eventType=String(event.type||'');const sev=String(event.severity||'info').toLowerCase();const level=document.createElement('div');level.className=`elevel dp-activity-level ${sev==='warning'?'warn':sev==='error'?'error':'info'}`;level.setAttribute('role','img');level.setAttribute('aria-label',sev==='warning'?'Warning':sev==='error'?'Error':'Info');const copy=document.createElement('div');copy.className='dp-activity-copy';const msg=document.createElement('div');msg.className='emsg dp-activity-message';msg.textContent=String(event.message??'');copy.appendChild(msg);const subject=document.createElement('div');subject.className='ename dp-activity-transfer';subject.textContent=subjectLine(event);copy.appendChild(subject);if(event.detail){const detail=document.createElement('details');detail.className='dp-activity-detail';const summary=document.createElement('summary');summary.textContent='Details';const body=document.createElement('div');body.className='dp-activity-detail-text';body.textContent=String(event.detail);detail.append(summary,body);copy.appendChild(detail);}const time=document.createElement('div');time.className='etime dp-activity-time';time.textContent=formatTimestamp(event.occurred_at);row.append(level,copy,time);list.appendChild(row);}
document.dispatchEvent(new CustomEvent('debridpulse:activity-rendered'));}
function renderNote(payload,c){if(!c.note)return;const search=payload?.search||{};let text='';if(search.text==='unavailable')text='Text search is unavailable on this installation; showing events for the transfer ID only.';else if(search.complete===false&&Number(search.pending)>0)text=`The search index is still catching up with ${Number(search.pending)>2000?'more than 2000':Number(search.pending)} recent event(s); they may be missing from these results.`;c.note.textContent=text;c.note.hidden=!text;}
function renderPagination(payload,c){if(!c.pagination)return;next=payload?.has_more?payload.next_before:null;const page=cursor.stack.length+1;c.newer.disabled=!cursor.stack.length;c.older.disabled=next===null;c.newest.hidden=!payload?.newer_available;c.info.textContent=`Page ${page}`;c.pagination.hidden=!(cursor.stack.length||next!==null||payload?.newer_available);}
function renderFailure(c,message){const list=document.getElementById('event-list');if(list){list.replaceChildren();empty(list,`Events could not be loaded: ${message}`);}if(c.note){c.note.hidden=true;c.note.textContent='';}if(c.pagination)c.pagination.hidden=true;}
async function fetchPage(){const c=bind();if(!c||typeof api!=='function')return null;const owned=++generation;const params=new URLSearchParams();params.set('limit',String(pageSize()));params.set('timeframe',c.timeframe.value||'all');if(c.severity.value)params.set('level',c.severity.value);if(c.category.value)params.set('category',c.category.value);if(c.search.value.trim())params.set('search',c.search.value.trim());if(cursor.snapshot!==null)params.set('snapshot',String(cursor.snapshot));if(cursor.before!==null)params.set('before',String(cursor.before));const list=document.getElementById('event-list');list?.setAttribute('aria-busy','true');
try{const payload=await api('GET',`/events?${params.toString()}`);if(owned!==generation)return null;if(cursor.snapshot===null&&Number.isInteger(payload?.snapshot))cursor.snapshot=payload.snapshot;render(payload,c);renderNote(payload,c);renderPagination(payload,c);syncReset(c);return payload;}
catch(error){if(owned!==generation)return null;const message=(()=>{try{return sanitizeErrorMsg(error?.message||error);}catch(_){return String(error?.message||error);}})();renderFailure(c,message);try{toast(message,'error');}catch(_){}return null;}
finally{if(owned===generation)list?.removeAttribute('aria-busy');}}
function load(){restart();return fetchPage();}
window.DPActivityLog=Object.freeze({load,formatTimestamp});
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',bind,{once:true});else bind();
})();
