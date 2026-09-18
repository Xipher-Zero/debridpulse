/* Dashboard Recent Activity transfer presentation owner.
 *
 * Source/host-icon mapping and markup are owned by the neutral
 * window.DPTransferSourcePresentation (DP 1.0.12 canonical flattening) --
 * Downloads consumes the same owner directly; neither duplicates the
 * host/source mapping.
 */
(function(){
'use strict';
// The one canonical pause-eligible presentation-status set is owned by
// ui-processing-presentation.js, a required explicit dependency that loads
// before this file (see index.html) -- there is no fallback copy here. A
// missing owner is a visible load-order bug, not something to paper over
// with a second definition.
const PAUSEABLE_PRESENTATION=new Set(window.DPProcessingPresentation.PAUSEABLE_PRESENTATION);
function sourceSlot(identity){return window.DPTransferSourcePresentation?.sourceSlot?.(identity)||'';}
/* The transfer-level candidate control is the interactive common-source group
   launcher owned by ui-group-candidates.js (DPGroupCandidates.launcherMarkup),
   rendered only when common_candidate_count > 1. Dashboard Recent Items and
   Downloads invoke the SAME shared owner; neither implements group semantics. */
function groupLauncherMarkup(item){return (window.DPGroupCandidates&&typeof window.DPGroupCandidates.launcherMarkup==='function')?window.DPGroupCandidates.launcherMarkup(item,'compact','dashboard_recent'):'';}
/* Torrent/magnet file-selection glyph-only chip (DP 1.0.12 Workstream B),
   owned by ui-file-selection.js -- never a second implementation. Renders in
   the same provider-meta slot as the Candidates launcher; the two never
   coexist for a given transfer (§6.10), so no arbitration logic exists here. */
function fileSelectionChipMarkup(item){return (window.DPFileSelection&&typeof window.DPFileSelection.chipMarkup==='function')?window.DPFileSelection.chipMarkup(item):'';}
async function renderRecent(){
 try{
  const recentLimit=typeof dashboardRecentLimit==='function'?dashboardRecentLimit():6; if(typeof _dashboardRecentFitLimit!=='undefined')_dashboardRecentFitLimit=recentLimit;
  const response=await api('GET',`/torrents?limit=${recentLimit}&order=activity`),items=Array.isArray(response?.items)?response.items:[],body=document.getElementById('dash-tbody'); if(!body)return response;
  const count=document.getElementById('dash-activity-count');
  if(!items.length){body.innerHTML='<tr><td colspan="6"><div class="empty"><div class="empty-icon" aria-hidden="true"></div>No downloads yet. Add a link, magnet, or torrent file to get started.</div></td></tr>'; if(count)count.textContent='Recent transfer history'; document.dispatchEvent(new CustomEvent('debridpulse:dashboard-recent-rendered')); return response;}
  if(count)count.textContent=items.length+' recent activity item'+(items.length===1?'':'s');
  body.innerHTML=items.map(t=>{const pct=t.progress!=null?Math.round(t.progress):0,presentation=window.DPProcessingPresentation.presentationStatus(t,t.status),show=pct>0&&presentation!=='completed'&&presentation!=='consolidated'&&presentation!=='deleted',dn=t.display_name||t.name||''; return `<tr data-torrent-id="${t.id}" data-status="${esc(t.status)}" data-presentation-status="${esc(presentation)}" onclick="if(!window.dpIsInteractiveRowTarget(event.target))showDetail(${t.id})" style="cursor:pointer"><td><div class="t-name" title="${esc(dn)||''}">${esc(dn)||'(unnamed)'}</div><div class="dash-row-bar-slot" aria-hidden="true"><div class="dash-row-bar${show?'':' is-empty'}"><div class="dash-row-bar-fill" style="width:${Math.max(0,Math.min(100,pct))}%;background:var(--blue)"></div></div></div><div class="dp-transfer-provider-meta">${sourceSlot(t.current_source_identity)}${providerChip(t)}${fileSelectionChipMarkup(t)}${groupLauncherMarkup(t)}</div></td><td data-role="transfer-status">${badge(transferDisplayStatus(t),t)}</td><td data-role="transfer-progress">${progress(t.progress,presentation)}</td><td class="sz">${fmtSize(t.size_bytes)}</td><td class="sz">${fmtDate(t.created_at)}</td><td onclick="event.stopPropagation()"><div class="actions">${PAUSEABLE_PRESENTATION.has(presentation)?`<button class="btn btn-blue btn-sm" data-default-label="Pause" onclick="event.stopPropagation();pauseT(${t.id},this)" title="Pause this download">Pause</button>`:''}${presentation==='paused'?`<button class="btn btn-blue btn-sm" data-default-label="Resume" onclick="event.stopPropagation();resumeT(${t.id},this)" title="Resume this download">Resume</button>`:''}</div></td></tr>`;}).join('');
  requestAnimationFrame(()=>{if(!document.getElementById('view-dashboard')?.classList.contains('active'))return; const fitted=typeof dashboardRecentLimit==='function'?dashboardRecentLimit():recentLimit; if(typeof _dashboardRecentFitLimit!=='undefined'&&fitted!==_dashboardRecentFitLimit){_dashboardRecentFitLimit=fitted; if(typeof loadRecent==='function')loadRecent().catch(()=>{});}});
  document.dispatchEvent(new CustomEvent('debridpulse:dashboard-recent-rendered')); return response;
 }catch(error){console.error(error); document.dispatchEvent(new CustomEvent('debridpulse:dashboard-recent-rendered')); return null;}
}
try{if(typeof window.__dpRegisterRecentRenderer==='function')window.__dpRegisterRecentRenderer(renderRecent);}catch(_){}
})();
