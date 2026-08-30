/* DebridPulse v1.0.11 final cross-page polish.
 *
 * This late, idempotent presentation layer applies the final reviewed master-card
 * copy, Settings/Help surface hierarchy, and Downloads bulk-strip placement.
 * It does not own application data, persistence, or transfer behavior.
 */
(function () {
  'use strict';

  let scheduled = false;
  const observers = [];

  function setText(node, value) {
    if (node && node.textContent !== value) node.textContent = value;
  }

  function applyDownloads() {
    const view = document.getElementById('view-torrents');
    if (!view) return;

    const title = document.getElementById('torrent-card-title');
    const heading = title?.querySelector('.dp-downloads-heading');
    const subtitle = title?.querySelector('.dp-downloads-subtitle');
    setText(heading, 'Download Queue');

    if (subtitle) {
      const match = String(subtitle.textContent || '').match(/^(\d+)\s+downloads?\s+tracked\b/i);
      if (match) {
        const count = Number(match[1]);
        const copy = count === 1
          ? '1 download tracked. It followed instructions.'
          : `${count} downloads tracked. Most of them followed instructions.`;
        setText(subtitle, copy);
        title?.setAttribute('aria-label', `Download Queue. ${copy}`);
      }
    }

    const card = view.querySelector(':scope > .card');
    const bar = document.getElementById('bulk-bar');
    const tableWrap = card?.querySelector('.dp-downloads-table-wrap')
      || card?.querySelector('div[style*="overflow-x:auto"]');
    if (card && bar && tableWrap) {
      bar.classList.add('dp-downloads-bulk-integrated');
      if (bar.parentElement !== card || bar.nextElementSibling !== tableWrap) {
        card.insertBefore(bar, tableWrap);
      }
    }
  }

  function applyActivityLog() {
    const view = document.getElementById('view-events');
    if (!view) return;
    setText(view.querySelector('.dp-activity-heading'), 'Activity Log');
    setText(
      view.querySelector('.dp-activity-subtitle'),
      'Everything DebridPulse thought was worth mentioning.'
    );
  }

  function applyStatistics() {
    const view = document.getElementById('view-stats');
    if (!view) return;
    setText(view.querySelector('.dp-stats-heading'), 'By the Numbers');
    setText(
      view.querySelector('.dp-stats-subtitle'),
      'Because vibes are not a performance metric.'
    );
  }

  function applySettings() {
    const view = document.getElementById('view-settings');
    if (!view) return;

    view.querySelector('.dp-settings-master-card')?.classList.add('dp-list-workspace-surface');
    setText(view.querySelector('.dp-settings-header-title'), 'Tuning Deck');
    setText(view.querySelector('.dp-settings-header-subtitle'), 'Your rules, your defaults.');

    view.querySelectorAll('.dp-settings-card, .dp-settings-group-card').forEach(card => {
      card.classList.add('dp-large-panel-surface');
    });
  }

  function applyHelp() {
    const view = document.getElementById('view-help');
    if (!view) return;

    view.querySelector('.dp-help-master-card')?.classList.add('dp-list-workspace-surface');
    setText(view.querySelector('.dp-help-header-title'), 'Field Manual');
    setText(view.querySelector('.dp-help-header-subtitle'), 'When intuition fails.');

    view.querySelectorAll('.dp-help-section-card').forEach(card => {
      card.classList.add('dp-large-panel-surface');
    });
  }

  function applyAll() {
    applyDownloads();
    applyActivityLog();
    applyStatistics();
    applySettings();
    applyHelp();
  }

  function scheduleApply() {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(() => {
      scheduled = false;
      applyAll();
    });
  }

  function observeView(id) {
    const view = document.getElementById(id);
    if (!view) return;
    const observer = new MutationObserver(scheduleApply);
    observer.observe(view, {childList: true, subtree: true, characterData: true});
    observers.push(observer);
  }

  function bind() {
    ['view-torrents', 'view-events', 'view-stats', 'view-settings', 'view-help'].forEach(observeView);
    applyAll();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind, {once: true});
  } else {
    bind();
  }
})();
