const { test, expect } = require('@playwright/test');

async function ready(page){await page.goto('/');await page.waitForFunction(()=>Boolean(window.DPProcessingPresentation&&window.DPDownloadsPresentation));}

test('configured scheduler denominator wins over later telemetry refresh',async({page})=>{
 await ready(page);
 const result=await page.evaluate(async()=>{
   settingsData={aria2_mode:'builtin',max_concurrent_downloads:6,aria2_max_active_downloads:6,paused:false};
   renderTopbarActions();updateAria2TopbarBadge({active:2});
   const before={active:document.getElementById('aria2-badge-active')?.textContent,max:document.getElementById('aria2-badge-max')?.textContent,resolved:DPProcessingPresentation.configuredMaxConcurrency()};
   const originalApi=api;api=async function(method,path){if(method==='GET'&&path==='/aria2/global-options')return{max_download_speed:0,max_concurrent_downloads:2,global_options_read_only:false};return originalApi.apply(this,arguments);};
   try{await loadAria2SpeedLimit();updateAria2TopbarBadge({active:3,liveBps:1024});return{before,after:{canonical:settingsData.max_concurrent_downloads,legacy:settingsData.aria2_max_active_downloads,active:document.getElementById('aria2-badge-active')?.textContent,max:document.getElementById('aria2-badge-max')?.textContent}};}finally{api=originalApi;}
 });
 expect(result.before).toEqual({active:'2',max:'6',resolved:6});expect(result.after).toEqual({canonical:6,legacy:6,active:'3',max:'6'});
});