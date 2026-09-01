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
    extracting: {icon: 'packageOpen', label: 'Extracting', className: 'extracting'},
    queued: {icon: 'clock3', label: 'Queued', className: 'queued'},
    paused: {icon: 'pause', label: 'Paused', className: 'paused'},
    downloading: {icon: 'download', label: 'Downloading', className: 'downloading'},
    ready: {icon: 'play', label: 'Ready', className: 'ready'},
    completed: {icon: 'circleCheck', label: 'Done', className: 'completed'},
    downloading_with_errors: {icon: 'triangleAlert', label: 'Downloading', className: 'partial'},
    completed_with_errors: {icon: 'triangleAlert', label: 'Completed with errors', className: 'partial'},
    error: {icon: 'x', label: 'Error', className: 'error'},
    missing: {icon: 'x', label: 'Missing file', className: 'error'},
    provider_failed: {icon: 'x', label: 'Provider download failed', className: 'error'},
    provider_missing: {icon: 'x', label: 'Removed from provider', className: 'error'},
    failed: {icon: 'x', label: 'Provider download failed', className: 'error'},
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

  function statusBadge(status) {
    const key = String(status == null ? '' : status).trim().toLowerCase();
    const descriptor = STATUS[key] || {icon: 'info', label: key || 'Unknown', className: key || 'pending'};
    return '<span class="badge badge-' + escapeHtml(descriptor.className) + '" data-dp-status="' + escapeHtml(key) + '">' +
      lucideSvg(descriptor.icon, 'dp-status-icon') +
      '<span class="dp-status-label">' + escapeHtml(descriptor.label) + '</span></span>';
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

  function canonicalToast(message, type) {
    const host = document.getElementById('toasts');
    if (!host) return;
    const requested = String(type || 'info').toLowerCase();
    const tone = requested === 'warning' ? 'warn' : (TOAST_ICON[requested] ? requested : 'info');
    const toast = document.createElement('div');
    toast.className = 'toast ' + tone;
    toast.innerHTML = lucideSvg(TOAST_ICON[tone] || 'info', 'dp-toast-icon');
    const copy = document.createElement('span');
    copy.className = 'dp-toast-copy';
    copy.textContent = String(message == null ? '' : message);
    toast.appendChild(copy);
    host.appendChild(toast);
    window.setTimeout(function () { toast.style.opacity = '0'; }, 3000);
    window.setTimeout(function () { toast.remove(); }, 3400);
  }

  window.DPIcons = Object.freeze({
    svg: lucideSvg,
    statusBadge: statusBadge,
    statusMap: STATUS,
    toastMap: TOAST_ICON,
    decorateButton: decorateButton,
    toast: canonicalToast,
    renderThemeGlyph: renderThemeGlyph
  });


  function decorateNavigation() {
    const iconByView = {
      dashboard: 'dashboard', torrents: 'download', events: 'logs',
      stats: 'statistics', settings: 'settings', help: 'help'
    };
    document.querySelectorAll('#sidebar .nav-item[data-view]').forEach(function (item) {
      const holder = item.querySelector('.icon');
      const iconName = iconByView[item.dataset.view];
      if (holder && iconName) holder.innerHTML = lucideSvg(iconName);
    });
  }

  function decorateMobileMenu() {
    const button = document.getElementById('mobile-menu-btn');
    if (button) button.innerHTML = lucideSvg('menu');
  }

  function renderThemeGlyph(isLight) {
    const button = document.getElementById('theme-toggle');
    if (!button) return;
    button.innerHTML = lucideSvg(isLight ? 'sun' : 'moon');
  }

  function decorateAria2CapChevron() {
    const arrow = document.querySelector('#aria2-cap-toggle span[aria-hidden="true"]');
    if (arrow && !arrow.querySelector('[data-dp-lucide="chevronDown"]')) {
      arrow.innerHTML = lucideSvg('chevronDown');
    }
  }

  function decorateActionButton(id, iconName) {
    const button = document.getElementById(id);
    if (!button || button.dataset.pending === '1') return;
    decorateButton(button, iconName, button.dataset.defaultLabel || button.textContent.trim(), 'dpShellLabel');
  }

  function decorateTopbarActions() {
    decorateActionButton('btn-pause-all', 'pause');
    decorateActionButton('btn-resume-all', 'play');
    decorateActionButton('btn-resume-paused', 'play');
    decorateAria2CapChevron();
  }

  function bindThemeToggle() {
    const button = document.getElementById('theme-toggle');
    const control = button && button.closest('.sidebar-theme-control');
    const topbar = document.getElementById('topbar');
    if (!button || !control || !topbar) return;
    control.classList.add('topbar-theme-control');
    if (control.parentElement !== topbar) topbar.appendChild(control);
    renderThemeGlyph(document.body.classList.contains('light'));
  }

  function initializeShellPresentation() {
    decorateNavigation();
    decorateMobileMenu();
    renderThemeGlyph(document.body.classList.contains('light'));
    decorateTopbarActions();
    bindThemeToggle();

    const actionHost = document.getElementById('topbar-actions');
    if (actionHost && !actionHost.dataset.dpShellObserved) {
      actionHost.dataset.dpShellObserved = '1';
      new MutationObserver(function () { decorateTopbarActions(); })
        .observe(actionHost, {childList: true, subtree: true, characterData: true});
    }
  }

  initializeShellPresentation();
  document.addEventListener('DOMContentLoaded', initializeShellPresentation, {once: true});
})();

(function () {
  'use strict';
  if (document.querySelector('script[data-dp-ui-runtime]')) return;
  const script = document.createElement('script');
  script.src = '/ui-runtime.js?v=24';
  script.defer = true;
  script.dataset.dpUiRuntime = '1';
  document.head.appendChild(script);
})();
