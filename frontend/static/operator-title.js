/* DebridPulse v1.0.11 canonical Lucide presentation integration.
 *
 * Locally vendored Lucide subset; no runtime CDN. Geometry is sourced from
 * lucide-icons/lucide commit 23f9abc4ed0146cffededd3d7f94c1018bfdf693.
 * License notices are bundled in licenses/Lucide-ISC-MIT.txt.
 */
(function () {
  'use strict';

  const LUCIDE = {
    dashboard: '<rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/>',
    download: '<path d="M12 15V3"/><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/>',
    logs: '<path d="M3 5h1"/><path d="M3 12h1"/><path d="M3 19h1"/><path d="M8 5h1"/><path d="M8 12h1"/><path d="M8 19h1"/><path d="M13 5h8"/><path d="M13 12h8"/><path d="M13 19h8"/>',
    statistics: '<path d="M5 21v-6"/><path d="M12 21V9"/><path d="M19 21V3"/>',
    settings: '<path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915"/><circle cx="12" cy="12" r="3"/>',
    help: '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
    menu: '<path d="M4 5h16"/><path d="M4 12h16"/><path d="M4 19h16"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>',
    moon: '<path d="M20.985 12.486a9 9 0 1 1-9.473-9.472c.405-.022.617.46.402.803a6 6 0 0 0 8.268 8.268c.344-.215.825-.004.803.401"/>',
    pause: '<rect x="14" y="3" width="5" height="18" rx="1"/><rect x="5" y="3" width="5" height="18" rx="1"/>',
    play: '<path d="M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z"/>',
    chevronDown: '<path d="m6 9 6 6 6-6"/>',
    chevronLeft: '<path d="m15 18-6-6 6-6"/>',
    chevronRight: '<path d="m9 18 6-6-6-6"/>',
    arrowRight: '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>',
    refresh: '<path d="M20 11a8.1 8.1 0 0 0-15.5-2M4 4v5h5"/><path d="M4 13a8.1 8.1 0 0 0 15.5 2M20 20v-5h-5"/>',
    upload: '<path d="M12 16V4"/><path d="m7 9 5-5 5 5"/><path d="M5 20h14"/>',
    x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    circleCheck: '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    circleX: '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/>',
    clock3: '<path d="M12 6v6h4"/><circle cx="12" cy="12" r="10"/>',
    loaderCircle: '<path d="M21 12a9 9 0 1 1-6.219-8.56"/>',
    packageOpen: '<path d="M12 22v-9"/><path d="M15.5 8.5 12 12 8.5 8.5"/><path d="m3.27 6.96 8.73 5.04 8.73-5.04"/><path d="M12 22 3.5 17V7L12 2l8.5 5v10Z"/>',
    triangleAlert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.46 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    trash2: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="m19 6-1 14H6L5 6"/><path d="M10 11v5"/><path d="M14 11v5"/>',
    info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    fileInput: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v6h6"/><path d="M8 13h2"/><path d="M8 17h2"/><path d="m15 12-3 3 3 3"/>'
  };

  const STATUS = Object.freeze({
    pending: {icon: 'clock3', label: 'Pending', className: 'pending'},
    uploading: {icon: 'upload', label: 'Uploading', className: 'uploading'},
    processing: {icon: 'loaderCircle', label: 'Processing', className: 'processing'},
    input_required: {icon: 'triangleAlert', label: 'Input Required', className: 'input_required'},
    extracting: {icon: 'packageOpen', label: 'Extracting', className: 'extracting'},
    queued: {icon: 'clock3', label: 'Queued', className: 'queued'},
    paused: {icon: 'pause', label: 'Paused', className: 'paused'},
    downloading: {icon: 'download', label: 'Downloading', className: 'downloading'},
    ready: {icon: 'play', label: 'Ready', className: 'ready'},
    completed: {icon: 'circleCheck', label: 'Done', className: 'completed'},
    consolidated: {icon: 'circleCheck', label: 'Consolidated', className: 'consolidated'},
    downloading_with_errors: {icon: 'triangleAlert', label: 'Downloading', className: 'partial'},
    completed_with_errors: {icon: 'triangleAlert', label: 'Completed with errors', className: 'partial'},
    error: {icon: 'x', label: 'Error', className: 'error'},
    missing: {icon: 'x', label: 'Missing file', className: 'error'},
    failed: {icon: 'x', label: 'Failed', className: 'error'},
    verifying: {icon: 'clock3', label: 'Verifying', className: 'queued'},
    cancelled: {icon: 'x', label: 'Cancelled', className: 'deleted'},
    unknown: {icon: 'triangleAlert', label: 'Awaiting confirmation', className: 'partial'},
    unresolved: {icon: 'clock3', label: 'Resolving', className: 'processing'},
    refresh_pending: {icon: 'clock3', label: 'Refreshing source', className: 'queued'},
    lost: {icon: 'triangleAlert', label: 'Execution missing', className: 'error'},
    deleted: {icon: 'trash2', label: 'Deleted', className: 'deleted'},
    imported: {icon: 'fileInput', label: 'Imported', className: 'imported'},
    partial: {icon: 'triangleAlert', label: 'Partial', className: 'partial'}
  });

  const TOAST_ICON = Object.freeze({
    success: 'circleCheck',
    warning: 'triangleAlert',
    warn: 'triangleAlert',
    error: 'circleX',
    info: 'info'
  });

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function lucideSvg(name, extraClass) {
    const geometry = LUCIDE[name];
    if (!geometry) return '';
    const cls = ['lucide', 'dp-utility-icon', extraClass || ''].filter(Boolean).join(' ');
    return '<svg class="' + cls + '" data-dp-lucide="' + name + '" xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' + geometry + '</svg>';
  }

  function statusBadge(status, label, category) {
    const key = String(status == null ? '' : status).trim().toLowerCase();
    const descriptor = STATUS[key] || {icon: 'info', label: key || 'Unknown', className: key || 'pending'};
    return '<span class="badge badge-' + escapeHtml(descriptor.className) + '" data-dp-status="' + escapeHtml(key) + '"' + (category ? ' data-dp-failure-code="' + escapeHtml(category) + '"' : '') + '>' +
      lucideSvg(descriptor.icon, 'dp-status-icon') +
      '<span class="dp-status-label">' + escapeHtml(label || descriptor.label) + '</span></span>';
  }

  function decorateButton(button, iconName, label, labelMarker) {
    if (!button || button.dataset.pending === '1' || button.getAttribute('aria-busy') === 'true') return;
    const copy = label == null ? (button.dataset.defaultLabel || button.textContent || '').trim() : String(label);
    const marker = labelMarker || 'dpIconLabel';
    const existingIcon = button.querySelector('[data-dp-lucide="' + iconName + '"]');
    const existingLabel = button.querySelector('[data-' + marker.replace(/[A-Z]/g, function (letter) { return '-' + letter.toLowerCase(); }) + ']');
    if (existingIcon && existingLabel && existingLabel.textContent === copy) return;
    button.innerHTML = lucideSvg(iconName);
    const span = document.createElement('span');
    span.dataset[marker] = '1';
    span.textContent = copy;
    button.appendChild(span);
    button.dataset.defaultLabel = copy;
  }

  function visibleTopbarContentBottom(topbar) {
    const children = Array.from(topbar.children).filter(function (element) {
      if (!(element instanceof HTMLElement)) return false;
      const style = window.getComputedStyle(element);
      return style.display !== 'none' && style.visibility !== 'hidden';
    });
    const bottoms = children.map(function (element) {
      return element.getBoundingClientRect().bottom;
    }).filter(Number.isFinite);
    return bottoms.length ? Math.max.apply(null, bottoms) : topbar.getBoundingClientRect().bottom;
  }

  function toastDeadspaceAnchor() {
    const topbar = document.getElementById('topbar');
    const content = document.getElementById('content');
    if (!topbar || !content) return 64;

    const topbarRect = topbar.getBoundingClientRect();
    const activeView = content.querySelector(':scope > .view.active');
    const contentStyle = window.getComputedStyle(content);
    const contentTop = activeView
      ? activeView.getBoundingClientRect().top
      : topbarRect.bottom + (parseFloat(contentStyle.paddingTop) || 0);

    if (window.matchMedia('(max-width: 700px)').matches) {
      // Narrow layouts have little inter-surface deadspace. Keep notifications
      // below the header controls rather than centering them over those controls.
      return topbarRect.bottom + 8;
    }

    const controlsBottom = Math.min(
      topbarRect.bottom,
      Math.max(topbarRect.top, visibleTopbarContentBottom(topbar))
    );
    const lower = Math.max(topbarRect.bottom, contentTop);
    return controlsBottom + Math.max(0, lower - controlsBottom) / 2;
  }

  function updateToastHostPosition() {
    const host = document.getElementById('toasts');
    if (!host) return;
    host.style.top = toastDeadspaceAnchor() + 'px';

    if (window.matchMedia('(max-width: 700px)').matches) {
      host.style.transform = 'translateX(-50%)';
    } else {
      host.style.transform = 'translate(-50%, -50%)';
    }
  }

  function normalizeToastNode(node) {
    if (!(node instanceof HTMLElement) || !node.classList.contains('toast')) return;
    // The host itself is intentionally pointer-transparent so empty lane space
    // never blocks the application. Individual notifications remain fully
    // interactive for hover/focus pause and explicit dismissal.
    node.style.pointerEvents = 'auto';
    node.style.position = 'relative';
    // The legacy corner entrance animation translates each card on X. That
    // makes a centered host measurably off-center during the entrance interval.
    // Placement now owns the transition, so suppress only that obsolete motion.
    node.style.animation = 'none';
  }

  function ensureToastHost() {
    const host = document.getElementById('toasts');
    if (!host) return null;
    host.style.position = 'fixed';
    host.style.left = '50vw';
    host.style.right = 'auto';
    host.style.bottom = 'auto';
    host.style.zIndex = '1000';
    host.style.display = 'flex';
    host.style.flexDirection = 'column';
    host.style.alignItems = 'center';
    host.style.gap = '8px';
    host.style.width = 'max-content';
    host.style.maxWidth = 'calc(100vw - 32px)';
    host.style.pointerEvents = 'none';

    Array.from(host.children).forEach(normalizeToastNode);

    if (host.dataset.dpToastLaneBound !== '1') {
      const update = function () {
        window.requestAnimationFrame(updateToastHostPosition);
      };
      window.addEventListener('resize', update);
      document.addEventListener('debridpulse:navigation', update);
      host.addEventListener('animationstart', function (event) {
        normalizeToastNode(event.target);
        update();
      });
      host.dataset.dpToastLaneBound = '1';
    }
    updateToastHostPosition();
    return host;
  }

  function canonicalToast(message, type) {
    const host = ensureToastHost();
    if (!host) return;
    const requested = String(type || 'info').toLowerCase();
    const tone = requested === 'warning' ? 'warn' : (TOAST_ICON[requested] ? requested : 'info');
    const toast = document.createElement('div');
    toast.className = 'toast ' + tone;
    toast.style.width = 'max-content';
    toast.style.maxWidth = 'min(480px, calc(100vw - 32px))';
    toast.style.whiteSpace = 'normal';
    toast.style.pointerEvents = 'auto';
    toast.style.position = 'relative';
    toast.style.animation = 'none';
    toast.setAttribute('role', tone === 'error' ? 'alert' : 'status');
    toast.innerHTML = lucideSvg(TOAST_ICON[tone] || 'info', 'dp-toast-icon');
    if (message && typeof message === 'object' && message.title && message.body) {
      const copy = document.createElement('div');
      copy.className = 'dp-toast-copy';
      const title = document.createElement('div');
      title.className = 'dp-toast-title';
      title.textContent = String(message.title);
      const body = document.createElement('div');
      body.className = 'dp-toast-body';
      body.textContent = String(message.body);
      copy.appendChild(title);
      copy.appendChild(body);
      toast.appendChild(copy);
    } else {
      const copy = document.createElement('span');
      copy.className = 'dp-toast-copy';
      copy.textContent = String(message == null ? '' : message);
      toast.appendChild(copy);
    }

    const dismiss = document.createElement('button');
    dismiss.type = 'button';
    dismiss.className = 'dp-toast-dismiss';
    dismiss.setAttribute('aria-label', 'Dismiss notification');
    dismiss.title = 'Dismiss notification';
    dismiss.innerHTML = lucideSvg('x');
    Object.assign(dismiss.style, {
      border: '0',
      background: 'transparent',
      color: 'inherit',
      cursor: 'pointer',
      display: 'inline-flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: '2px',
      marginLeft: '2px',
      flex: '0 0 auto',
      pointerEvents: 'auto'
    });
    toast.appendChild(dismiss);

    let fadeTimer = null;
    let removeTimer = null;
    const remove = function () {
      if (fadeTimer) window.clearTimeout(fadeTimer);
      if (removeTimer) window.clearTimeout(removeTimer);
      toast.remove();
      updateToastHostPosition();
    };
    dismiss.addEventListener('click', remove);

    host.appendChild(toast);
    updateToastHostPosition();
    fadeTimer = window.setTimeout(function () { toast.style.opacity = '0'; }, 3000);
    removeTimer = window.setTimeout(remove, 3400);
    return toast;
  }

  function positiveInteger(value) {
    return Number.isInteger(value) && value > 0 ? value : null;
  }

  function nonnegativeInteger(value) {
    return Number.isInteger(value) && value >= 0 ? value : null;
  }

  function consolidationToastCopy(payload) {
    if (!payload || typeof payload !== 'object') return null;
    const sourceTransferId = positiveInteger(payload.source_transfer_id);
    const matched = positiveInteger(payload.matched_count);
    const unmatched = nonnegativeInteger(payload.unmatched_count);
    if (sourceTransferId == null || matched == null || unmatched == null || !Array.isArray(payload.canonical_transfer_ids)) return null;
    const targets = new Set();
    for (const value of payload.canonical_transfer_ids) {
      const target = positiveInteger(value);
      if (target == null) return null;
      targets.add(target);
    }
    if (!targets.size || targets.size > matched) return null;

    const matchingNoun = matched === 1 ? 'file' : 'files';
    const matchingVerb = matched === 1 ? 'was' : 'were';
    const candidateNoun = matched === 1 ? 'a failover candidate' : 'failover candidates';
    const sourceLinkNoun = matched === 1 ? 'link' : 'links';
    const sourceLinkVerb = matched === 1 ? 'was' : 'were';
    const destination = targets.size === 1 ? 'the existing download' : 'existing downloads';

    if (unmatched > 0) {
      const newNoun = unmatched === 1 ? 'file' : 'files';
      return {
        title: 'Duplicate files consolidated',
        body: matched + ' matching ' + matchingNoun + ' ' + matchingVerb + ' merged into ' + destination + ' and retained as ' + candidateNoun + '. ' + unmatched + ' new ' + newNoun + ' will download normally.'
      };
    }

    return {
      title: targets.size === 1 ? 'Duplicate download consolidated' : 'Duplicate downloads consolidated',
      body: matched + ' matching ' + matchingNoun + ' ' + matchingVerb + ' merged into ' + destination + '. The new source ' + sourceLinkNoun + ' ' + sourceLinkVerb + ' retained as ' + candidateNoun + '.'
    };
  }

  function installConsolidationEventConsumer() {
    const NativeEventSource = window.EventSource;
    if (typeof NativeEventSource !== 'function' || NativeEventSource.__dpConsolidationConsumer === true) return;

    function DPEventSource(url, init) {
      const source = arguments.length > 1 ? new NativeEventSource(url, init) : new NativeEventSource(url);
      const endpoint = String(url == null ? '' : url);
      if (endpoint === '/api/events/stream' || endpoint.endsWith('/api/events/stream')) {
        source.addEventListener('duplicate_consolidated', function (event) {
          try {
            const copy = consolidationToastCopy(JSON.parse(event.data));
            if (copy) canonicalToast(copy, 'success');
          } catch (_error) {
            // Invalid public event data is ignored rather than rendered.
          }
        });
      }
      return source;
    }

    DPEventSource.prototype = NativeEventSource.prototype;
    try { Object.setPrototypeOf(DPEventSource, NativeEventSource); } catch (_error) { /* older browsers */ }
    Object.defineProperty(DPEventSource, '__dpConsolidationConsumer', {value: true});
    window.EventSource = DPEventSource;
  }

  window.DPIcons = Object.freeze({
    svg: lucideSvg,
    statusBadge: statusBadge,
    statusMap: STATUS,
    toastMap: TOAST_ICON,
    decorateButton: decorateButton,
    toast: canonicalToast,
    ensureToastHost: ensureToastHost,
    consolidationToastCopy: consolidationToastCopy,
    renderThemeGlyph: renderThemeGlyph
  });

  ensureToastHost();
  installConsolidationEventConsumer();

  function renderThemeGlyph(isLight) {
    const button = document.getElementById('theme-toggle');
    if (!button) return;
    button.innerHTML = lucideSvg(isLight ? 'sun' : 'moon');
  }
})();
