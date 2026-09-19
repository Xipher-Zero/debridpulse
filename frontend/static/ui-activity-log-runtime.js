/* Activity Log behavior and render owner.
 *
 * The only implementation of loading, server-side filtering, debounce, reset and
 * rendering for #view-events. The control markup is static in index.html; this
 * module binds it once and renders only into #event-list. It exposes its own
 * API (DPActivityLog) and assigns no other owner's global. app.js calls
 * DPActivityLog.load() on navigation.
 */
(function(){
'use strict';
const EVENT_LIMIT=500,SEARCH_DEBOUNCE_MS=250;let generation=0,timer=null,bound=false;
function controls(){const search=document.getElementById('ev-search'),timeframe=document.getElementById('ev-timeframe'),severity=document.getElementById('ev-level'),reset=document.getElementById('ev-reset'),note=document.getElementById('dp-activity-result-note');return search&&timeframe&&severity&&reset?{search,timeframe,severity,reset,note}:null;}
function filters(c){return Boolean(c.search.value.trim()||(c.timeframe.value||'all')!=='all'||(c.severity.value||'')!=='');}
function syncReset(c){c.reset.hidden=!filters(c);}
function bind(){const c=controls();if(!c||bound)return c;bound=true;
c.search.addEventListener('input',()=>{syncReset(c);if(timer)clearTimeout(timer);timer=setTimeout(()=>{timer=null;void load();},SEARCH_DEBOUNCE_MS);});
c.timeframe.addEventListener('change',()=>{syncReset(c);void load();});
c.severity.addEventListener('change',()=>{syncReset(c);void load();});
c.reset.addEventListener('click',()=>{if(timer){clearTimeout(timer);timer=null;}c.search.value='';c.timeframe.value='all';c.severity.value='';syncReset(c);void load();});
syncReset(c);return c;}
function parseTimestamp(value){if(!value)return null;try{if(typeof parseApiDate==='function')return parseApiDate(value);}catch(_){}const raw=String(value),d=new Date(/[zZ]|[+-]\d\d:\d\d$/.test(raw)?raw:`${raw.replace(' ','T')}Z`);return Number.isNaN(d.getTime())?null:d;}
function formatTimestamp(value){const d=parseTimestamp(value);if(!d)return String(value||'—');let zone='UTC';try{zone=String(settingsData?.timezone||'').trim()||'UTC';}catch(_){}try{return new Intl.DateTimeFormat('en-US',{timeZone:zone,year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(d);}catch(_){return d.toLocaleString();}}
function render(items,c){const list=document.getElementById('event-list');if(!list)return;list.replaceChildren();if(!items.length){const e=document.createElement('div');e.className='empty';e.textContent=filters(c)?'No events match your filters.':'No events yet.';list.appendChild(e);return;}for(const event of items){const row=document.createElement('div');row.className='dp-activity-row';const level=document.createElement('div');const sev=String(event.level||'info').toLowerCase()==='warn'?'warning':String(event.level||'info').toLowerCase();level.className=`elevel dp-activity-level ${sev==='warning'?'warn':sev==='error'?'error':'info'}`;level.setAttribute('aria-label',sev==='warning'?'Warning':sev==='error'?'Error':'Info');const copy=document.createElement('div');copy.className='dp-activity-copy';const msg=document.createElement('div');msg.className='emsg dp-activity-message';msg.textContent=String(event.message??'');copy.appendChild(msg);if(event.torrent_name){const name=document.createElement('div');name.className='ename dp-activity-transfer';name.textContent=String(event.torrent_name);copy.appendChild(name);}const time=document.createElement('div');time.className='etime dp-activity-time';time.textContent=formatTimestamp(event.created_at);row.append(level,copy,time);list.appendChild(row);}document.dispatchEvent(new CustomEvent('debridpulse:activity-rendered'));}
async function load(){const c=bind();if(!c||typeof api!=='function')return null;const owned=++generation,filtered=filters(c),params=new URLSearchParams();params.set('limit',String(EVENT_LIMIT));if(filtered){params.set('include_meta','true');params.set('timeframe',c.timeframe.value||'all');if(c.severity.value)params.set('level',c.severity.value);if(c.search.value.trim())params.set('search',c.search.value.trim());}try{const payload=await api('GET',`/events?${params.toString()}`);if(owned!==generation)return null;const items=Array.isArray(payload?.items)?payload.items:Array.isArray(payload)?payload:[];render(items,c);if(c.note){if(payload?.truncated===true){c.note.textContent=`Showing the latest ${EVENT_LIMIT} matching events. Narrow your filters to see older matches.`;c.note.hidden=false;}else{c.note.hidden=true;c.note.textContent='';}}syncReset(c);return payload;}catch(error){try{toast(sanitizeErrorMsg(error?.message||error),'error');}catch(_){}return null;}}
window.DPActivityLog=Object.freeze({load,formatTimestamp});
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',bind,{once:true});else bind();
})();
