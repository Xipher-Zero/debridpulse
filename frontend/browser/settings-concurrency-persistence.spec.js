const { test, expect } = require('@playwright/test');

async function ready(page){await page.goto('/');await page.waitForFunction(()=>Boolean(window.DPProcessingPresentation&&window.DPDownloads));}

// The configured scheduler capacity is transfer_policy.max_concurrent_executions.
// A later telemetry refresh from the aria2 daemon must never displace it, and
// nothing mirrors it into a flat alias.
test('configured scheduler denominator wins over later telemetry refresh',async({page})=>{
 await ready(page);
 const result=await page.evaluate(async()=>{
   settingsData={integrations:{aria2:{options:{}}},transfer_policy:{max_concurrent_executions:6},execution_runtime_limits:{max_download_bytes_per_second:0},paused:false};
   renderTopbarActions();updateRuntimeStatusBadge({active:2});
   const before={active:document.getElementById('runtime-badge-active')?.textContent,max:document.getElementById('runtime-badge-max')?.textContent,resolved:DPProcessingPresentation.configuredMaxConcurrency()};
   const originalApi=api;api=async function(method,path){if(method==='GET'&&path==='/aria2/global-options')return{max_download_speed:0,max_concurrent_downloads:2};return originalApi.apply(this,arguments);};
   try{await loadRuntimeStatus();updateRuntimeStatusBadge({active:3,liveBps:1024});return{before,after:{canonical:settingsData.transfer_policy.max_concurrent_executions,flat:['max_concurrent_downloads','aria2_max_active_downloads'].filter(key=>key in settingsData),active:document.getElementById('runtime-badge-active')?.textContent,max:document.getElementById('runtime-badge-max')?.textContent}};}finally{api=originalApi;}
 });
 expect(result.before).toEqual({active:'2',max:'6',resolved:6});expect(result.after).toEqual({canonical:6,flat:[],active:'3',max:'6'});
});

test('configuredMaxConcurrency reads only the canonical transfer policy',async({page})=>{
 await ready(page);
 const values=await page.evaluate(()=>{
   const read=data=>{settingsData=data;return DPProcessingPresentation.configuredMaxConcurrency();};
   return{
     canonical:read({transfer_policy:{max_concurrent_executions:9}}),
     flatAliasesIgnored:read({max_concurrent_downloads:8,aria2_max_active_downloads:8,transfer_policy:{}}),
     empty:read({}),
   };
 });
 expect(values).toEqual({canonical:9,flatAliasesIgnored:null,empty:null});
});
