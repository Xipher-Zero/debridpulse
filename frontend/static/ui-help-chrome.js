/* DebridPulse v1.0.11 Help presentation structure.
 *
 * The clean Help runtime owns content and tab behavior. This layer only applies
 * the approved Settings-derived presentation language: master-header copy,
 * topical Lucide chips, user-facing section labels, and one full-width content
 * card per Help section. It deliberately uses no DOM observer; Help paints once
 * and tab changes only toggle existing panels.
 */
(function () {
  'use strict';

  const ICONS = Object.freeze({
    quickstart: 'rocket',
    howitworks: 'workflow',
    aria2: 'download',
    integrations: 'plug',
    settings: 'settings',
    trouble: 'wrench',
    license: 'scroll-text',
  });

  const LABELS = Object.freeze({
    aria2: 'Download Engine',
  });

  function root() {
    return document.getElementById('view-help');
  }

  function normalizeMasterHeader(view) {
    const copy = view.querySelector('.dp-help-header-copy');
    const title = copy?.querySelector('.dp-help-header-title');
    if (!copy || !title || copy.querySelector('.dp-help-header-subtitle')) return;

    const textBlock = document.createElement('div');
    textBlock.className = 'dp-help-header-text';
    title.before(textBlock);
    textBlock.appendChild(title);

    const subtitle = document.createElement('div');
    subtitle.className = 'dp-help-header-subtitle';
    subtitle.textContent = 'To Be Determined';
    textBlock.appendChild(subtitle);
  }

  function decorateTabs(view) {
    view.querySelectorAll('.dp-help-tab[data-tab]').forEach(function (tab) {
      if (tab.dataset.dpHelpChrome === '1') return;
      const icon = ICONS[tab.dataset.tab];
      if (!icon) return;

      const labelText = LABELS[tab.dataset.tab] || String(tab.textContent || '').trim();
      const chip = document.createElement('span');
      chip.className = 'dp-help-tab-chip';
      chip.setAttribute('aria-hidden', 'true');

      const glyph = document.createElement('img');
      glyph.className = 'dp-help-tab-glyph';
      glyph.src = '/icons/lucide/' + icon + '.svg';
      glyph.alt = '';
      chip.appendChild(glyph);

      const label = document.createElement('span');
      label.className = 'dp-help-tab-label';
      label.textContent = labelText;

      tab.replaceChildren(chip, label);
      tab.dataset.dpHelpChrome = '1';
    });
  }

  function structureSectionCards(view) {
    view.querySelectorAll('.dp-help-panel > .dp-help-document').forEach(function (section) {
      if (section.dataset.dpHelpCard === '1') return;

      const heading = section.querySelector(':scope > .dp-help-section-heading');
      if (!heading) return;

      const header = document.createElement('div');
      header.className = 'card-header dp-help-section-card-header';
      heading.before(header);
      header.appendChild(heading);

      const body = document.createElement('div');
      body.className = 'card-body dp-help-section-card-body';
      Array.from(section.children).forEach(function (child) {
        if (child !== header) body.appendChild(child);
      });
      section.appendChild(body);

      section.classList.add('card', 'dp-help-section-card');
      section.dataset.dpHelpCard = '1';
    });
  }

  function enhance() {
    const view = root();
    if (!view || !view.querySelector('.dp-help-master-card')) return false;
    normalizeMasterHeader(view);
    decorateTabs(view);
    structureSectionCards(view);
    view.dataset.dpHelpChromeReady = '1';
    return true;
  }

  function init() {
    if (enhance()) return;
    if (typeof window.requestAnimationFrame === 'function') {
      window.requestAnimationFrame(enhance);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, {once: true});
  } else {
    init();
  }
})();
