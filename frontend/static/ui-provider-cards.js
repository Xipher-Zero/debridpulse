/* Neutral provider Settings disclosure and presentation hierarchy owner. */
(function(){
  'use strict';
  function integrations(){try{return settingsData?.integrations&&typeof settingsData.integrations==='object'?settingsData.integrations:{}}catch(_){return{}}}
  function signature(el){return el.type==='checkbox'||el.type==='radio'?(el.checked?'1':'0'):String(el.value??'');}
  function controls(card,enable){return Array.from(card.querySelectorAll('.card-body input,.card-body select,.card-body textarea')).filter(el=>el!==enable);}
  function statusPresentation(enabled,configured){if(!enabled&&configured)return{text:'Provider configured',tone:'info'};if(enabled&&!configured)return{text:'Configuration required',tone:'warning'};return{text:'',tone:'none'};}
  function addPremium(card,integration){if(!integration?.presentation?.premium||card.querySelector('.dp-provider-premium'))return;const title=card.querySelector(':scope > .card-header .dp-settings-card-title-text,:scope > .card-header .card-title');if(!title)return;const crown=document.createElement('span');crown.className='dp-provider-premium';crown.setAttribute('role','img');crown.setAttribute('title','Premium provider');crown.setAttribute('aria-label','Premium provider');title.appendChild(crown);}
  function applyGroupTitle(card,integration){const label=String(integration?.presentation?.status_group_label||'').trim();if(!label)return;const parent=card.closest('.dp-settings-source-group');const title=parent?.querySelector(':scope > .card-header .card-title');if(title)title.textContent=label;}
  function decorate(enable){const id=String(enable.dataset.integrationEnabled||'');const integration=integrations()[id];const card=enable.closest('.dp-settings-provider-card');if(!integration||!card)return;card.dataset.providerConfigured=integration.configured?'true':'false';addPremium(card,integration);applyGroupTitle(card,integration);if(!integration.presentation?.premium||card.dataset.dpProviderStateOwner==='1')return;
    const header=card.querySelector(':scope > .card-header'),body=card.querySelector(':scope > .card-body'),enableControl=enable.closest('.dp-settings-integration-header-enable');if(!header||!body||!enableControl)return;card.dataset.dpProviderStateOwner='1';const safe=id.replace(/[^a-z0-9_-]/gi,'-');body.id=body.id||`dp-settings-provider-body-${safe}`;
    const status=document.createElement('div');status.className='dp-settings-provider-config-status';status.setAttribute('role','status');status.setAttribute('aria-live','polite');header.insertBefore(status,enableControl);
    const disclosure=document.createElement('button');disclosure.type='button';disclosure.className='dp-settings-provider-disclosure';disclosure.setAttribute('aria-controls',body.id);disclosure.innerHTML='<span aria-hidden="true">›</span>';
    const wrapper=document.createElement('div');wrapper.className='dp-settings-provider-header-controls';header.insertBefore(wrapper,enableControl);wrapper.append(disclosure,enableControl);
    const initial=controls(card,enable).map(el=>[el,signature(el)]);const dirty=()=>initial.some(([el,value])=>el.isConnected&&signature(el)!==value);
    const setExpanded=value=>{body.hidden=!value;card.classList.toggle('dp-settings-provider-card--collapsed',!value);disclosure.setAttribute('aria-expanded',value?'true':'false');disclosure.title=value?'Collapse provider configuration':'Expand provider configuration';disclosure.setAttribute('aria-label',disclosure.title);};
    const update=()=>{const p=statusPresentation(enable.checked,!!integration.configured);status.textContent=p.text;status.dataset.tone=p.tone;status.hidden=!p.text;card.dataset.providerConfigured=integration.configured?'true':'false';};
    disclosure.addEventListener('click',event=>{event.preventDefault();event.stopPropagation();setExpanded(disclosure.getAttribute('aria-expanded')!=='true');});enable.addEventListener('change',()=>{if(enable.checked)setExpanded(true);else if(!dirty())setExpanded(false);update();});setExpanded(enable.checked);update();
  }
  function apply(){document.querySelectorAll('#view-settings input[data-integration-enabled]').forEach(decorate);}
  document.addEventListener('debridpulse:settings-rendered',apply);apply();window.DPProviderCardState=Object.freeze({statusPresentation});
})();
