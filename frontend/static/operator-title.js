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
  const TOAST_MIN_MS = 3000;
  const TOAST_MAX_MS = 10000;
  const TOAST_WORD_MS = 250;
  const TOAST_FADE_MS = 250;
  const TOAST_MAX_WIDTH = 480;
  const TOAST_GUTTER = 10;
  let toastPositionFrame = 0;
  let toastTopbarResizeObserver = null;

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

  function toastWordCount(message) {
    const parts = [];
    if (message && typeof message === 'object') {
      if (message.title != null) parts.push(String(message.title));
      if (message.body != null) parts.push(String(message.body));
    } else {
      parts.push(String(message == null ? '' : message));
    }
    const text = parts.join(' ').trim();
    return text ? text.split(/\s+/u).filter(Boolean).length : 0;
  }

  function toastDuration(message) {
    return Math.max(TOAST_MIN_MS, Math.min(TOAST_MAX_MS, toastWordCount(message) * TOAST_WORD_MS));
  }

  function visibleElement(element) {
    if (!(element instanceof HTMLElement)) return false;
    const style = window.getComputedStyle(element);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function unionRect(elements) {
    const rects = elements.filter(visibleElement).map(function (element) {
      return element.getBoundingClientRect();
    });
    if (!rects.length) return null;
    return {
      left: Math.min.apply(null, rects.map(function (rect) { return rect.left; })),
      right: Math.max.apply(null, rects.map(function (rect) { return rect.right; })),
      top: Math.min.apply(null, rects.map(function (rect) { return rect.top; })),
      bottom: Math.max.apply(null, rects.map(function (rect) { return rect.bottom; }))
    };
  }

  function topbarOccupantRect(element) {
    if (!visibleElement(element)) return null;
    const style = window.getComputedStyle(element);
    if (element.classList.contains('dp-page-heading') || parseFloat(style.flexGrow) > 0) {
      const contentRect = unionRect(Array.from(element.children));
      if (contentRect) return contentRect;
    }
    return element.getBoundingClientRect();
  }

  function toastSafeLane() {
    const topbar = document.getElementById('topbar');
    if (!topbar) {
      return {narrow: false, left: 16, right: Math.max(17, window.innerWidth - 16), top: 0, bottom: 80};
    }
    const topbarRect = topbar.getBoundingClientRect();
    if (window.matchMedia('(max-width: 700px)').matches) {
      return {
        narrow: true,
        left: 16,
        right: Math.max(17, window.innerWidth - 16),
        top: topbarRect.bottom + 8,
        bottom: window.innerHeight - 8
      };
    }

    const interiorLeft = topbarRect.left + TOAST_GUTTER;
    const interiorRight = topbarRect.right - TOAST_GUTTER;
    const occupied = Array.from(topbar.children)
      .map(topbarOccupantRect)
      .filter(Boolean)
      .map(function (rect) {
        return {
          left: Math.max(interiorLeft, rect.left - TOAST_GUTTER),
          right: Math.min(interiorRight, rect.right + TOAST_GUTTER)
        };
      })
      .filter(function (rect) { return rect.right > rect.left; })
      .sort(function (a, b) { return a.left - b.left; });

    const merged = [];
    occupied.forEach(function (rect) {
      const last = merged[merged.length - 1];
      if (last && rect.left <= last.right) last.right = Math.max(last.right, rect.right);
      else merged.push({left: rect.left, right: rect.right});
    });

    const free = [];
    let cursor = interiorLeft;
    merged.forEach(function (rect) {
      if (rect.left > cursor) free.push({left: cursor, right: rect.left});
      cursor = Math.max(cursor, rect.right);
    });
    if (cursor < interiorRight) free.push({left: cursor, right: interiorRight});
    if (!free.length) free.push({left: interiorLeft, right: Math.max(interiorLeft + 1, interiorRight)});

    const preference = window.innerWidth / 2;
    free.sort(function (a, b) {
      const aDistance = preference < a.left ? a.left - preference : (preference > a.right ? preference - a.right : 0);
      const bDistance = preference < b.left ? b.left - preference : (preference > b.right ? preference - b.right : 0);
      if (aDistance !== bDistance) return aDistance - bDistance;
      return (b.right - b.left) - (a.right - a.left);
    });
    return {
      narrow: false,
      left: free[0].left,
      right: free[0].right,
      top: topbarRect.top,
      bottom: topbarRect.bottom
    };
  }

  function updateToastHostPosition() {
    toastPositionFrame = 0;
    const host = document.getElementById('toasts');
    if (!host) return;
    const lane = toastSafeLane();
    const laneWidth = Math.max(1, lane.right - lane.left);
    const maxToastWidth = Math.max(1, Math.min(TOAST_MAX_WIDTH, laneWidth));
    Array.from(host.children).forEach(function (node) {
      normalizeToastNode(node);
      if (node instanceof HTMLElement) node.style.maxWidth = maxToastWidth + 'px';
    });

    host.style.right = 'auto';
    host.style.bottom = 'auto';
    host.style.maxWidth = laneWidth + 'px';
    host.style.height = 'auto';
    host.style.maxHeight = 'none';
    host.style.justifyContent = 'flex-start';

    if (lane.narrow) {
      host.style.left = lane.left + 'px';
      host.style.top = lane.top + 'px';
      host.style.width = laneWidth + 'px';
      host.style.transform = 'none';
      return;
    }

    host.style.width = 'max-content';
    host.style.top = ((lane.top + lane.bottom) / 2) + 'px';
    const hostWidth = Math.min(laneWidth, host.getBoundingClientRect().width || maxToastWidth);
    const half = hostWidth / 2;
    const desired = window.innerWidth / 2;
    const center = Math.max(lane.left + half, Math.min(lane.right - half, desired));
    host.style.left = center + 'px';
    host.style.transform = 'translate(-50%, -50%)';
  }

  function scheduleToastHostPosition() {
    if (toastPositionFrame) return;
    toastPositionFrame = window.requestAnimationFrame(updateToastHostPosition);
  }

  function normalizeToastNode(node) {
    if (!(node instanceof HTMLElement) || !node.classList.contains('toast')) return;
    node.style.pointerEvents = 'none';
    node.style.position = 'relative';
    node.style.animation = 'none';
    node.style.whiteSpace = 'normal';
    node.style.overflowWrap = 'anywhere';
  }

  function ensureToastHost() {
    const host = document.getElementById('toasts');
    if (!host) return null;
    host.style.position = 'fixed';
    host.style.zIndex = '1000';
    host.style.display = 'flex';
    host.style.flexDirection = 'column';
    host.style.alignItems = 'center';
    host.style.gap = '8px';
    host.style.pointerEvents = 'none';
    Array.from(host.children).forEach(normalizeToastNode);

    if (host.dataset.dpToastLaneBound !== '1') {
      window.addEventListener('resize', scheduleToastHostPosition, {passive: true});
      if (window.visualViewport) window.visualViewport.addEventListener('resize', scheduleToastHostPosition, {passive: true});
      document.addEventListener('debridpulse:navigation', scheduleToastHostPosition);
      const topbar = document.getElementById('topbar');
      if (topbar && 'ResizeObserver' in window) {
        toastTopbarResizeObserver = new ResizeObserver(scheduleToastHostPosition);
        toastTopbarResizeObserver.observe(topbar);
      }
      host.dataset.dpToastLaneBound = '1';
    }
    scheduleToastHostPosition();
    return host;
  }

  function canonicalToast(message, type) {
    const host = ensureToastHost();
    if (!host) return null;
    const requested = String(type || 'info').toLowerCase();
    const tone = requested === 'warning' ? 'warn' : (TOAST_ICON[requested] ? requested : 'info');
    const toast = document.createElement('div');
    toast.className = 'toast ' + tone;
    toast.style.width = 'max-content';
    toast.style.whiteSpace = 'normal';
    toast.style.pointerEvents = 'none';
    toast.style.position = 'relative';
    toast.style.animation = 'none';
    toast.style.opacity = '1';
    toast.style.transition = 'opacity ' + TOAST_FADE_MS + 'ms ease';
    toast.setAttribute('role', tone === 'error' || tone === 'warn' ? 'alert' : 'status');
    toast.innerHTML = lucideSvg(TOAST_ICON[tone] || 'info', 'dp-toast-icon');
    if (message && typeof message === 'object' && message.title != null && message.body != null) {
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

    const duration = toastDuration(message);
    const fadeAt = Math.max(0, duration - TOAST_FADE_MS);
    toast.dataset.dpToastDurationMs = String(duration);
    toast.dataset.dpToastFadeAtMs = String(fadeAt);
    let fadeTimer = null;
    let removeTimer = null;
    const remove = function () {
      if (fadeTimer !== null) window.clearTimeout(fadeTimer);
      if (removeTimer !== null) window.clearTimeout(removeTimer);
      fadeTimer = null;
      removeTimer = null;
      if (toast.isConnected) toast.remove();
      scheduleToastHostPosition();
    };

    host.appendChild(toast);
    scheduleToastHostPosition();
    fadeTimer = window.setTimeout(function () {
      toast.dataset.dpToastFading = '1';
      toast.style.opacity = '0';
    }, fadeAt);
    removeTimer = window.setTimeout(remove, duration);
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
    toastDuration: toastDuration,
    toastWordCount: toastWordCount,
    ensureToastHost: ensureToastHost,
    toastSafeLane: toastSafeLane,
    repositionToasts: scheduleToastHostPosition,
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
