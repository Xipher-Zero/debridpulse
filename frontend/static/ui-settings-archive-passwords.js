/* Automatic Extraction archive-password editor owner.
 * ui-settings-page.js renders the whole field -- label, the form-field textarea that
 * carries the persisted value, the editor container, the destructive Clear action,
 * the reveal button and the hint. This owner only fills the editor's rows, binds its
 * own behavior, hydrates from the dedicated endpoint and mirrors edits into that form
 * field. It rewrites no markup the page rendered.
 *
 * PERSISTENCE IS NOT OWNED HERE. The form field is an ordinary changed-blur control of
 * the canonical `settings-document` scope, declared in the page's COMMIT_FIELDS table
 * and written by ui-settings-persistence.js like any other. What this owner adds is the
 * one thing a generic owner cannot know for a composite control: WHERE its commit
 * boundary is. A list edited across many line inputs has no single blur of its own, so
 * the boundary is focus leaving the editor -- and it is reported by calling the
 * canonical owner's own commit(). Baseline, dispatch, serialization, stale-response
 * protection and rollback all stay there; none of it is reimplemented here.
 *
 * THIS MODULE IS ALSO THE ONE LAYOUT OWNER for the list region. Four decisions --
 * rows per column, visible column count, overflow mode and separator geometry --
 * are made HERE, from measured geometry, and expressed by setting the grid's two
 * templates and the separators' offsets. The stylesheet owns material and the gaps
 * this reads back; it states no count and no capacity, so the two cannot disagree.
 *
 * The model is fill-downward-first: entries fill a column top to bottom, then the
 * next column to the right while another useful column still fits, and only once
 * both are exhausted does the region itself scroll VERTICALLY with the column count
 * held at its width-derived maximum. DOM order stays the logical password order, so
 * the visual reflow never changes tab order. */
(function(){
'use strict';
let rows=null,editorNode=null,sourceNode=null,keySerial=0,revealAll=false,activeKey=null,scheduled=false;
let hydratedSource=null,hydratedEditor=null,hydrationPromise=null,hydrationGeneration=0,dirty=false,hydrated=false;
/* Re-rendering the rows replaces the focused line input, which fires focusout
 * exactly as leaving the editor does. This says which of the two it is: the
 * owner sets it when it is about to re-render AND hand focus back, and clears
 * it once it has. Nothing else distinguishes them, because nothing else can. */
let refocusing=false;
// ONE observer for the whole page lifetime, and one coalescing frame. Re-applying
// re-points the observer instead of adding another, so no rerender can accumulate them.
let regionObserver=null,layoutFrame=0;
const source=()=>document.querySelector('#view-settings [data-panel="extraction"] [data-setting="extraction_password"]');
const editor=()=>document.querySelector('#view-settings [data-panel="extraction"] .dp-settings-extraction-password-editor');
const nextKey=()=>`row:${keySerial++}`;
function normalize(items){while(items.length>1&&items.at(-1).value===''&&items.at(-2).value==='')items.pop();if(!items.length||items.at(-1).value!=='')items.push({key:nextKey(),value:''});return items;}
function canonical(raw){return String(raw||'').replace(/\r\n?/g,'\n').split('\n').map(v=>v.trim()).filter(Boolean).join('\n');}
function reset(s,e,raw=String(s?.value||'')){const values=canonical(raw).split('\n').filter(Boolean);rows=normalize(values.map(value=>({key:nextKey(),value})));sourceNode=s;editorNode=e;revealAll=false;activeKey=null;}
function serialized(){return(rows||[]).map(r=>String(r.value||'').trim()).filter(Boolean).join('\n');}
function syncSource(s){if(!s||!rows)return;s.value=serialized();}
/* The composite control's commit boundary: focus has left the editor. Reported to the
 * ONE canonical persistence owner, which decides whether anything changed at all.
 * Never before hydration: until the stored list has been read, an empty field means
 * "not known yet", and writing it would replace the stored list with nothing. */
function commitSource(){if(refocusing||!hydrated||!sourceNode)return;try{window.DPSettingsPersistence?.commit(sourceNode);}catch(_){}}
function acceptSource(value){if(!sourceNode)return;try{window.DPSettingsPersistence?.accept(sourceNode,value);}catch(_){}}
function mask(value){return'•'.repeat(String(value||'').length);}
function eyeSvg(hidden){const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('fill','none');svg.setAttribute('stroke','currentColor');svg.setAttribute('stroke-width','2');svg.setAttribute('aria-hidden','true');const add=d=>{const p=document.createElementNS(ns,'path');p.setAttribute('d',d);svg.appendChild(p);};if(hidden){add('m2 2 20 20');add('M6.7 6.7C4.6 8.1 3 10.3 3 12c0 0 3 7 9 7 1.9 0 3.5-.7 4.8-1.7');add('M9.9 4.2A8.4 8.4 0 0 1 12 4c6 0 9 7 9 8a10.2 10.2 0 0 1-1.7 2.7');}else{add('M2.1 12.3a1 1 0 0 1 0-.7C3.5 7.7 7.2 5 12 5s8.5 2.7 9.9 6.7a1 1 0 0 1 0 .7C20.5 16.3 16.8 19 12 19s-8.5-2.7-9.9-6.7');const c=document.createElementNS(ns,'circle');c.setAttribute('cx','12');c.setAttribute('cy','12');c.setAttribute('r','3');svg.appendChild(c);}return svg;}
function setEye(button){if(!button)return;const action=revealAll?'Hide all passwords':'Show all passwords',label=document.createElement('span');label.className='dp-settings-password-eye-label';label.textContent=revealAll?'Hide all':'Show all';button.classList.add('dp-settings-password-eye--ghost');button.setAttribute('aria-pressed',revealAll?'true':'false');button.setAttribute('aria-label',action);button.title=action;button.classList.toggle('is-open',revealAll);button.replaceChildren(eyeSvg(revealAll),label);}
function focusKey(e,key){if(!e||!key){refocusing=false;return;}requestAnimationFrame(()=>{const input=Array.from(e.querySelectorAll('.dp-settings-password-line')).find(n=>n.dataset.passwordKey===key);if(input){input.focus();try{input.setSelectionRange(input.value.length,input.value.length);}catch(_){}}refocusing=false;});}
function present(input,row,raw){if(!input||!row)return;input.type='text';input.dataset.passwordDisplay=raw?'raw':'masked';const next=raw?String(row.value||''):mask(row.value);if(input.value!==next)input.value=next;}
function refreshPresentation(e){if(!e||!rows)return;e.querySelectorAll('.dp-settings-password-line').forEach(input=>{const row=rows.find(item=>item.key===input.dataset.passwordKey);if(row)present(input,row,revealAll||activeKey===row.key);});}
function markDirty(){dirty=true;}

/* ── The list layout owner ────────────────────────────────────────────────
 *
 * Everything here is derived from what is actually rendered: the canonical entry
 * row's own height, the grid's own gaps, the region's own usable box and the
 * minimum useful column width the stylesheet declares. No row count, column count
 * or breakpoint is written down anywhere. */
const region=e=>e?.querySelector('.dp-settings-password-region');
const canvasOf=e=>e?.querySelector('.dp-settings-password-canvas');

function geometry(host,grid){
  const line=grid.querySelector('.dp-settings-password-line');
  if(!line)return null;
  const style=getComputedStyle(grid);
  const g={
    rowHeight:line.getBoundingClientRect().height,
    rowGap:parseFloat(style.rowGap)||0,
    columnGap:parseFloat(style.columnGap)||0,
    minColumn:parseFloat(style.getPropertyValue('--dp-password-column-min'))||0,
    available:host.clientHeight,
    width:grid.clientWidth,
  };
  // A hidden panel measures zero. Nothing is laid out from that; the region's own
  // observer brings us back the moment it has a box.
  if(g.rowHeight<1||g.available<1||g.width<1||g.minColumn<1)return null;
  return g;
}

/* One separator per visible inter-column gap, centred in the gap it belongs to.
 * They are children of the canvas rather than of either column, so they are
 * attached to neither, and they are rebuilt to the exact count every pass -- a
 * narrower viewport leaves none behind. */
function paintSeparators(canvas,columns,g){
  const want=Math.max(0,columns-1);
  let rules=Array.from(canvas.querySelectorAll('.dp-settings-password-separator'));
  for(let i=rules.length;i>want;i-=1)rules[i-1].remove();
  for(let i=rules.length;i<want;i+=1){
    const rule=document.createElement('span');
    rule.className='dp-settings-password-separator';
    rule.setAttribute('aria-hidden','true');
    canvas.appendChild(rule);
  }
  if(!want)return;
  const columnWidth=(g.width-(columns-1)*g.columnGap)/columns;
  canvas.querySelectorAll('.dp-settings-password-separator').forEach((rule,index)=>{
    rule.style.left=`${(index+1)*columnWidth+(index+0.5)*g.columnGap}px`;
  });
}

function layout(){
  const e=editorNode;if(!e)return;
  const host=region(e),canvas=canvasOf(e),grid=e.querySelector('.dp-settings-password-rows');
  if(!host||!canvas||!grid)return;
  const count=grid.childElementCount;
  const g=count?geometry(host,grid):null;
  if(!g){paintSeparators(canvas,1,{width:0,columnGap:0});return;}
  // How many entries fit in one visible column, and how many useful columns fit
  // across. A column is only added while a WHOLE further column and its gap fit.
  const perColumn=Math.max(1,Math.floor((g.available+g.rowGap)/(g.rowHeight+g.rowGap)));
  const maxColumns=Math.max(1,Math.floor((g.width+g.columnGap)/(g.minColumn+g.columnGap)));
  const columns=Math.min(maxColumns,Math.max(1,Math.ceil(count/perColumn)));
  // Fill downward FIRST: a column takes its full visible capacity before the next
  // one starts. Only when every visible column is full do the columns grow taller
  // than the region -- which is what makes the region, and nothing else, scroll.
  const rows=Math.max(perColumn,Math.ceil(count/columns));
  grid.style.gridTemplateRows=`repeat(${rows}, min-content)`;
  grid.style.gridTemplateColumns=`repeat(${columns}, minmax(0, 1fr))`;
  paintSeparators(canvas,columns,g);
  centreGuidance(e);
}

/* The footer's leading spacer takes the action group's own measured width, so
 * the guidance is centred on the editor rather than on whatever the flex
 * algorithm left over. The stylesheet cannot know that width; this owner can,
 * and it is the same kind of decision as the rest of this function. */
function centreGuidance(e){
  const footer=e.querySelector('.dp-settings-password-footer');
  const actions=footer?.querySelector('.dp-settings-password-actions');
  if(!footer||!actions)return;
  const style=getComputedStyle(footer);
  const controls=actions.getBoundingClientRect().width;
  const gaps=(parseFloat(style.columnGap)||0)*2;
  const floor=parseFloat(getComputedStyle(e).getPropertyValue('--dp-password-guidance-min'))||0;
  if(controls<=0)return;
  // Centring is worth having only while the hint still has room to read. Where
  // it does not, the spacer gives way entirely rather than wrapping the hint
  // into a column tall enough to consume the list region below it.
  const affordable=footer.clientWidth-controls-gaps-floor;
  footer.style.setProperty('--dp-password-lead',`${Math.max(0,Math.min(controls,affordable))}px`);
}

function scheduleLayout(){
  if(layoutFrame)return;
  layoutFrame=requestAnimationFrame(()=>{layoutFrame=0;layout();});
}

/* The region's own size is the only input that changes without a render, so it is
 * the only thing observed -- not a timer, and not the document. */
function observeRegion(e){
  const host=region(e);if(!host)return;
  if(!regionObserver)regionObserver=new ResizeObserver(()=>scheduleLayout());
  regionObserver.disconnect();
  regionObserver.observe(host);
}
function render(e,s,focus=null){if(!e||!s||!rows)return;refocusing=!!focus;normalize(rows);let host=e.querySelector('.dp-settings-password-rows');if(!host){host=document.createElement('div');host.className='dp-settings-password-rows';(canvasOf(e)||e).prepend(host);}host.replaceChildren();rows.forEach((row,index)=>{const input=document.createElement('input');input.className='dp-settings-password-line';input.type='text';input.dataset.passwordKey=row.key;input.dataset.passwordIndex=String(index);input.autocomplete='off';input.autocapitalize='none';input.spellcheck=false;input.setAttribute('aria-label',`Archive password ${index+1}`);if(index===rows.length-1&&!row.value)input.placeholder='Add an archive password';present(input,row,revealAll||activeKey===row.key);input.addEventListener('focus',()=>{input.dataset.passwordEditStart=row.value;activeKey=row.key;present(input,row,true);try{input.select();}catch(_){}});input.addEventListener('input',()=>{if(input.dataset.passwordDisplay!=='raw')return;markDirty();row.value=input.value;syncSource(s);if(index===rows.length-1&&row.value!==''){normalize(rows);activeKey=row.key;render(e,s,row.key);}});input.addEventListener('blur',()=>{if(input.dataset.passwordDisplay==='raw'){row.value=input.value;syncSource(s);}queueMicrotask(()=>{const i=rows.indexOf(row);if(i>=0&&i<rows.length-1&&String(row.value||'').trim()===''){markDirty();rows.splice(i,1);normalize(rows);activeKey=null;render(e,s);return;}if(document.activeElement?.closest('.dp-settings-extraction-password-editor')!==e&&!revealAll){activeKey=null;refreshPresentation(e);}});});input.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();row.value=input.dataset.passwordEditStart??row.value;input.value=row.value;syncSource(s);input.blur();return;}if(event.key==='Enter'){event.preventDefault();if(input.dataset.passwordDisplay==='raw'){row.value=input.value;syncSource(s);}markDirty();const inserted={key:nextKey(),value:''};rows.splice(index+1,0,inserted);normalize(rows);activeKey=inserted.key;render(e,s,inserted.key);return;}if(event.altKey&&(event.key==='ArrowUp'||event.key==='ArrowDown')){const to=event.key==='ArrowUp'?index-1:index+1;if(to<0||to>=rows.length)return;event.preventDefault();if(input.dataset.passwordDisplay==='raw')row.value=input.value;markDirty();[rows[index],rows[to]]=[rows[to],rows[index]];activeKey=row.key;syncSource(s);render(e,s,row.key);}});input.addEventListener('paste',event=>{const pasted=event.clipboardData?.getData('text')||'';if(!/[\r\n]/.test(pasted))return;event.preventDefault();markDirty();const incoming=pasted.replace(/\r\n?/g,'\n').split('\n').map(value=>({key:nextKey(),value}));rows.splice(index,1,...incoming);normalize(rows);const target=incoming.at(-1);activeKey=target.key;syncSource(s);render(e,s,target.key);});host.appendChild(input);});const eye=e.querySelector('.dp-settings-password-eye');if(eye&&eye.dataset.dpArchiveOwner!=='1'){eye.dataset.dpArchiveOwner='1';eye.addEventListener('click',()=>{revealAll=!revealAll;if(!revealAll)activeKey=null;setEye(eye);refreshPresentation(e);});}setEye(eye);const clearAction=e.querySelector('.dp-settings-password-clear');if(clearAction)clearAction.disabled=serialized().length===0;if(e.dataset.dpArchiveOwner!=='1'){e.addEventListener('focusout',event=>{if(!event.relatedTarget||!e.contains(event.relatedTarget))commitSource();});}e.dataset.dpArchiveOwner='1';syncSource(s);observeRegion(e);layout();if(focus)focusKey(e,focus);}
function hydrate(s,e){if(!s||!e||typeof api!=='function')return null;if(hydratedSource===s&&hydratedEditor===e)return hydrationPromise;hydratedSource=s;hydratedEditor=e;hydrated=false;const generation=++hydrationGeneration,localAtStart=serialized(),dirtyAtStart=dirty;hydrationPromise=(async()=>{try{const payload=await api('GET','/settings/extraction-passwords');if(generation!==hydrationGeneration||source()!==s||editor()!==e)return;hydrated=true;const remote=canonical(payload?.passwords||'');acceptSource(remote);if(dirtyAtStart){if(dirty&&serialized()===localAtStart&&remote===localAtStart)dirty=false;return;}if(dirty)return;reset(s,e,remote);dirty=false;render(e,s);}catch(_){/* Existing source state remains the fallback if the narrow editor read is unavailable. */}finally{if(generation===hydrationGeneration)hydrationPromise=null;}})();return hydrationPromise;}
function apply(){const s=source();if(!s)return;const e=editor();if(!e)return;const changed=e!==editorNode||s!==sourceNode||!rows;if(changed){const carried=rows?serialized():null,carriedDirty=dirty;reset(s,e,carried!==null?carried:String(s?.value||''));dirty=carriedDirty;render(e,s);}if(e.dataset.dpArchiveOwner!=='1')render(e,s);void hydrate(s,e);}
function scheduleApply(){if(scheduled)return;scheduled=true;queueMicrotask(()=>{scheduled=false;apply();});}
function init(){document.addEventListener('debridpulse:settings-rendered',scheduleApply);scheduleApply();}
/* Converge on the accepted canonical state after the page's destructive clear.
 * The removal itself is the page's explicit action; this owner only makes the editor
 * show what the server now holds, and records it as the accepted baseline so the empty
 * list is not mistaken for an unsaved edit. */
function clear(){const s=source(),e=editor();if(!s||!e)return;reset(s,e,'');dirty=false;render(e,s);acceptSource('');}
window.DPArchivePasswords=Object.freeze({apply,clear,get hydrated(){return hydrated;}});if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init,{once:true});else init();
})();
