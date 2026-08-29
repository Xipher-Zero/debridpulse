/* DebridPulse v1.0.11 Settings / Downloads completion runtime.
 *
 * The clean Settings renderer remains authoritative for values and persistence.
 * This small, idempotent presentation layer applies the final Downloads copy
 * and card classifications after that renderer paints the persistent Settings
 * root. It deliberately does not add per-render action listeners or own saves.
 */
(function () {
  'use strict';

  const CONFIGURED_SECRET_MASK = '••••••••••••••••••••••••••••••••••••••••••••••••';

  const RECOVERY_COPY = Object.freeze([
    [
      'min_free_disk_gb',
      'Minimum Free Disk Space (GB)',
      'Stops new downloads from starting when free disk space falls below this amount. Set to 0 to disable the disk-space guard.',
    ],
    [
      'disk_guard_resume_hysteresis_gb',
      'Resume Free Space Buffer (GB)',
      'Extra free space required above the minimum before DebridPulse starts downloads again. Helps prevent repeated stop/start behavior near the limit.',
    ],
    [
      'stuck_download_timeout_hours',
      'Stalled Download Timeout (hours)',
      'How long a download can remain stalled before DebridPulse attempts automatic recovery. Set to 0 to disable stalled-download recovery.',
    ],
    [
      'aria2_error_retry_count',
      'Download Error Retries',
      'How many times DebridPulse retries a download after aria2 reports an error. Set to 0 to disable automatic retries.',
    ],
    [
      'aria2_error_retry_delay_seconds',
      'Retry Delay (seconds)',
      'How long DebridPulse waits before retrying a download after an aria2 error. Set to 0 to retry immediately.',
    ],
  ]);

  const root = () => document.getElementById('view-settings');

  function directChild(element, selector) {
    if (!element) return null;
    return Array.from(element.children).find(child => child.matches(selector)) || null;
  }

  function cardByTitle(panel, title) {
    if (!panel) return null;
    return Array.from(panel.children).find(card => {
      if (!card.classList.contains('dp-settings-card')) return false;
      const header = directChild(card, '.card-header');
      const titleNode = header?.querySelector('.card-title');
      return String(titleNode?.textContent || '').trim() === title;
    }) || null;
  }

  function setFieldCopy(panel, key, label, hint) {
    const control = panel?.querySelector(`[data-setting="${key}"]`);
    const field = control?.closest('.dp-settings-field');
    if (!field) return;

    const labelNode = directChild(field, '.form-label');
    if (labelNode && labelNode.textContent !== label) labelNode.textContent = label;

    let hintNode = directChild(field, '.form-hint');
    if (!hintNode) {
      hintNode = document.createElement('span');
      hintNode.className = 'form-hint';
      field.appendChild(hintNode);
    }
    if (hintNode.textContent !== hint) hintNode.textContent = hint;
  }

  function expandConfiguredSecretMasks(view) {
    view.querySelectorAll(
      '[data-panel="sources"] input[type="password"], [data-panel="downloads"] input[type="password"]'
    ).forEach(input => {
      const placeholder = String(input.getAttribute('placeholder') || '');
      if (placeholder && /^•+$/u.test(placeholder) && placeholder !== CONFIGURED_SECRET_MASK) {
        input.setAttribute('placeholder', CONFIGURED_SECRET_MASK);
      }
    });
  }

  function applyDownloads(view) {
    const panel = view.querySelector('[data-panel="downloads"]');
    if (!panel) return;

    const recovery = cardByTitle(panel, 'Download Safety & Recovery');
    recovery?.classList.add('dp-settings-download-recovery-card');

    for (const [key, label, hint] of RECOVERY_COPY) {
      setFieldCopy(panel, key, label, hint);
    }

    /* UI-only retirement: keep the loaded controls intact so Apply Settings
       preserves legacy values until their backend/config pruning pass. */
    const fileFilters = cardByTitle(panel, 'File Filters');
    if (fileFilters) {
      fileFilters.classList.add('dp-settings-file-filters-retired');
      fileFilters.setAttribute('aria-hidden', 'true');
      fileFilters.inert = true;
    }
  }

  function apply() {
    const view = root();
    if (!view) return;
    applyDownloads(view);
    expandConfiguredSecretMasks(view);
  }

  let scheduled = false;
  function scheduleApply() {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(() => {
      scheduled = false;
      apply();
    });
  }

  function attach() {
    const view = root();
    if (!view) return;
    if (view.dataset.dpSettingsDownloadsCompletionBound === '1') {
      apply();
      return;
    }

    view.dataset.dpSettingsDownloadsCompletionBound = '1';
    const observer = new MutationObserver(scheduleApply);
    observer.observe(view, {childList: true, subtree: true});
    apply();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', attach, {once: true});
  } else {
    attach();
  }
})();
