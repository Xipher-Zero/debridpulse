/* Processing pause-state visibility and canonical scheduler-capacity read.
 *
 * Owns two things: toggling the visibility of the pause banner and shim (static
 * markup in index.html) from app.js's processingPaused projection of the backend's durable pause state (never a setting), and configuredMaxConcurrency(),
 * the one read of the universal scheduler capacity
 * (transfer_policy.max_concurrent_executions). It creates, removes or relabels
 * no other owner's markup and assigns no other owner's global.
 */
(function(){
'use strict';
const PAUSEABLE_PRESENTATION=Object.freeze(['downloading','queued','recovering','waiting_for_retry','waiting_for_provider','waiting_for_storage','waiting_for_executor']);
function presentationStatus(detail,fallback){return String(detail?.presentation_status||fallback||'').trim().toLowerCase();}
function syncPauseUi(){let paused=false;try{paused=processingPaused===true;}catch(_){}document.querySelector('.dp-global-pause-center')?.classList.toggle('is-visible',paused);document.querySelector('.dp-downloads-pause-shim')?.classList.toggle('is-visible',paused);window.DPDownloads?.scheduleCapacityCheck?.();}
function configuredMaxConcurrency(){let data=null;try{data=settingsData;}catch(_){}const value=Number(data?.transfer_policy?.max_concurrent_executions);return Number.isFinite(value)&&value>0?Math.trunc(value):null;}
function init(){syncPauseUi();}
window.DPProcessingPresentation=Object.freeze({configuredMaxConcurrency,syncPauseUi,presentationStatus,PAUSEABLE_PRESENTATION});if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init,{once:true});else init();
})();