/* Processing pause-state, scheduler-capacity, and canonical transfer-presentation bridge.
 *
 * DP 1.0.12 canonical flattening: this module previously reassigned the
 * shared globals `badge`, `transferDisplayStatus`, `loadAria2SpeedLimit`,
 * `renderSettings` and `renderTopbarActions` at load time to graft its
 * behavior onto them. Those five owners now implement this behavior
 * directly (badge()/transferDisplayStatus() in app.js consume
 * `presentation_status`/`presentation_label`/`presentation_badge_status`
 * natively; loadAria2SpeedLimit()/renderTopbarActions() in app.js call the
 * bridge functions exposed below directly). This module owns no other
 * owner's globals.
 */
(function(){
'use strict';
const PAUSEABLE_PRESENTATION=Object.freeze(['downloading','queued','recovering','waiting_for_retry','waiting_for_provider','waiting_for_storage','waiting_for_executor']);
function pauseGlyph(){return '<svg class="lucide" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>';}
function presentationStatus(detail,fallback){return String(detail?.presentation_status||fallback||'').trim().toLowerCase();}
function ensurePauseUi(){const header=document.querySelector('#view-dashboard .dp-dashboard-quick-add > .card-header');if(header&&!header.querySelector('.dp-global-pause-center')){const center=document.createElement('div');center.className='dp-global-pause-center';center.innerHTML=`<div class="dp-global-pause-title">${pauseGlyph()}<span>PROCESSING PAUSED</span></div><div class="dp-global-pause-copy">New downloads can still be added. They will remain queued until processing is resumed.</div>`;header.insertBefore(center,header.querySelector('.dp-card-header-actions')||null);}const card=document.querySelector('#view-torrents > .card');if(card&&!card.querySelector('.dp-downloads-pause-shim')){const shim=document.createElement('div');shim.className='dp-downloads-pause-shim';shim.textContent='Processing paused. Queued and newly added downloads will not start until processing is resumed.';card.querySelector(':scope > .card-header')?.insertAdjacentElement('afterend',shim);}}
function removeImportAction(){document.getElementById('btn-import-existing')?.remove();document.querySelectorAll('#view-dashboard button[onclick*="importExisting"]').forEach(n=>n.remove());const recover=document.getElementById('btn-recover-all');if(recover)recover.title='Check transfers for recoverable work';const add=document.getElementById('btn-add-transfer');if(add)add.title='Submit links and magnets for provider routing; when empty, choose a .torrent file';}
function syncPauseUi(){ensurePauseUi();removeImportAction();let paused=false;try{paused=!!settingsData?.paused;}catch(_){}document.querySelector('.dp-global-pause-center')?.classList.toggle('is-visible',paused);document.querySelector('.dp-downloads-pause-shim')?.classList.toggle('is-visible',paused);window.DPDownloads?.scheduleCapacityCheck?.();}
function configuredMaxConcurrency(){let data=null;try{data=settingsData;}catch(_){}const primary=Number(data?.max_concurrent_downloads);if(Number.isFinite(primary)&&primary>0)return Math.trunc(primary);const legacy=Number(data?.aria2_max_active_downloads);if(Number.isFinite(legacy)&&legacy>0)return Math.trunc(legacy);return data&&Object.keys(data).length?3:null;}
function syncConfiguredConcurrency(){if(typeof updateAria2TopbarBadge==='function')updateAria2TopbarBadge({maxDl:configuredMaxConcurrency()});}
function patchTerminology(){document.querySelectorAll('#view-settings .card-title').forEach(title=>{if(['General Sources','General Downloads'].includes(title.textContent.trim()))title.textContent='Direct Sources';});}
function init(){ensurePauseUi();removeImportAction();patchTerminology();syncConfiguredConcurrency();syncPauseUi();document.addEventListener('debridpulse:settings-rendered',()=>{patchTerminology();syncConfiguredConcurrency();});document.addEventListener('debridpulse:dashboard-recent-rendered',removeImportAction);}
window.DPProcessingPresentation=Object.freeze({configuredMaxConcurrency,syncPauseUi,syncConfiguredConcurrency,presentationStatus,PAUSEABLE_PRESENTATION});if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init,{once:true});else init();
})();