/* Canonical toast public bridge; operator-title.js owns rendering. */
(function(){
'use strict';
function correctedToastMessage(message){
  if(message&&typeof message==='object')return message;
  const text=String(message??'');
  if(/^Line \d+: enter an HTTP\(S\) link or magnet URI$/i.test(text))return 'DebridPulse stared at that for a moment. It is not a link, magnet, or torrent.';
  if(text==='Checking AllDebrid for ready torrents…'||text==='Checking AllDebrid for ready torrents...')return 'Checking transfers for recoverable work…';
  return text;
}
function canonicalDuration(message){
  const corrected=correctedToastMessage(message);
  if(window.DPIcons&&typeof window.DPIcons.toastDuration==='function')return window.DPIcons.toastDuration(corrected);
  const parts=corrected&&typeof corrected==='object'?[corrected.title,corrected.body]:[corrected];
  const text=parts.filter(v=>v!=null).map(String).join(' ').trim();
  const words=text?text.split(/\s+/u).filter(Boolean).length:0;
  return Math.max(3000,Math.min(10000,words*250));
}
function publicToast(message,type){const presenter=window.DPIcons&&typeof window.DPIcons.toast==='function'?window.DPIcons.toast:null; return presenter?presenter(correctedToastMessage(message),type):null;}
window.toast=publicToast; window.DPToastDuration=canonicalDuration; window.DPToastContract=Object.freeze({toast:publicToast,duration:canonicalDuration,correctedMessage:correctedToastMessage});
})();