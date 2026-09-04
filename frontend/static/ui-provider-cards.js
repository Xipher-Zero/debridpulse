/* Neutral premium-provider Settings card disclosure/state owner.
 *
 * Enabled state follows the current staged form. Configured state comes only
 * from persisted safe integration metadata. Disclosure is page-local.
 */
(function () {
  'use strict';

  function loadedIntegrations() {
    try {
      return settingsData && settingsData.integrations && typeof settingsData.integrations === 'object'
        ? settingsData.integrations : {};
    } catch (_) {
      return {};
    }
  }

  function valueSignature(element) {
    if (element.type === 'checkbox' || element.type === 'radio') return element.checked ? '1' : '0';
    return String(element.value ?? '');
  }

  function configurationControls(card, enableToggle) {
    return Array.from(card.querySelectorAll('.card-body input, .card-body select, .card-body textarea'))
      .filter(element => element !== enableToggle);
  }

  function snapshot(card, enableToggle) {
    return configurationControls(card, enableToggle).map(element => [element, valueSignature(element)]);
  }

  function dirty(context) {
    return context.initial.some(([element, initial]) => element.isConnected && valueSignature(element) !== initial);
  }

  function statusPresentation(enabled, configured) {
    if (!enabled && configured) return {text: 'Provider configured', tone: 'info'};
    if (enabled && !configured) return {text: 'Configuration required', tone: 'warning'};
    return {text: '', tone: 'none'};
  }

  function setExpanded(context, expanded) {
    context.expanded = !!expanded;
    context.body.hidden = !context.expanded;
    context.card.classList.toggle('dp-settings-provider-card--collapsed', !context.expanded);
    context.disclosure.setAttribute('aria-expanded', context.expanded ? 'true' : 'false');
    context.disclosure.title = context.expanded ? 'Collapse provider configuration' : 'Expand provider configuration';
    context.disclosure.setAttribute('aria-label', context.disclosure.title);
  }

  function updateStatus(context) {
    const presentation = statusPresentation(context.enable.checked, context.configured);
    context.status.textContent = presentation.text;
    context.status.dataset.tone = presentation.tone;
    context.status.hidden = !presentation.text;
  }

  function onEnableChange(context) {
    if (context.enable.checked) {
      setExpanded(context, true);
    } else if (!dirty(context)) {
      setExpanded(context, false);
    }
    updateStatus(context);
  }

  function decorate(enable) {
    const identity = String(enable.dataset.integrationEnabled || '');
    const integration = loadedIntegrations()[identity];
    if (!integration?.presentation?.premium) return;

    const card = enable.closest('.dp-settings-provider-card');
    const header = card?.querySelector(':scope > .card-header');
    const body = card?.querySelector(':scope > .card-body');
    const enableControl = enable.closest('.dp-settings-integration-header-enable');
    if (!card || !header || !body || !enableControl || card.dataset.dpProviderStateOwner === '1') return;

    card.dataset.dpProviderStateOwner = '1';
    card.dataset.providerId = identity;
    card.dataset.providerPremium = 'true';
    card.dataset.providerConfigured = integration.configured ? 'true' : 'false';

    const safeIdentity = identity.replace(/[^a-z0-9_-]/gi, '-');
    body.id = body.id || `dp-settings-provider-body-${safeIdentity}`;

    const status = document.createElement('div');
    status.className = 'dp-settings-provider-config-status';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    header.insertBefore(status, enableControl);

    const disclosure = document.createElement('button');
    disclosure.type = 'button';
    disclosure.className = 'dp-settings-provider-disclosure';
    disclosure.setAttribute('aria-controls', body.id);
    disclosure.innerHTML = '<span aria-hidden="true">›</span>';

    const controls = document.createElement('div');
    controls.className = 'dp-settings-provider-header-controls';
    header.insertBefore(controls, enableControl);
    controls.append(disclosure, enableControl);

    const context = {
      card,
      body,
      enable,
      status,
      disclosure,
      configured: !!integration.configured,
      expanded: false,
      initial: [],
    };
    context.initial = snapshot(card, enable);

    disclosure.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      setExpanded(context, !context.expanded);
    });
    enable.addEventListener('change', () => onEnableChange(context));

    setExpanded(context, enable.checked);
    updateStatus(context);
  }

  function apply() {
    document.querySelectorAll('#view-settings input[data-integration-enabled]').forEach(decorate);
  }

  document.addEventListener('debridpulse:settings-rendered', apply);
  apply();

  window.DPProviderCardState = Object.freeze({statusPresentation});
})();
