/* DebridPulse v1.0.11 bundled Help legal-document overlay.
 *
 * License actions open the exact document shipped with the running DebridPulse
 * build. GitHub is never required to read the bundled copy; the only external
 * navigation is the explicit "latest version" link shown inside the overlay.
 */
(function () {
  'use strict';

  function focusableElements(dialog) {
    return Array.from(dialog.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
      'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )).filter(function (element) {
      return !element.hasAttribute('hidden') && element.getAttribute('aria-hidden') !== 'true';
    });
  }

  function closeModal(options) {
    const restoreFocus = !options || options.restoreFocus !== false;
    if (!activeBackdrop) return;

    const opener = activeOpener;
    activeBackdrop.remove();
    activeBackdrop = null;
    activeOpener = null;
    document.body.classList.remove('dp-help-legal-modal-open');

    if (restoreFocus && opener && document.contains(opener)) {
      opener.focus();
    }
  }

  function trapDialogKeydown(event, dialog) {
    if (event.key === 'Escape') {
      event.preventDefault();
      closeModal();
      return;
    }
    if (event.key !== 'Tab') return;

    const focusable = focusableElements(dialog);
    if (!focusable.length) {
      event.preventDefault();
      dialog.focus();
      return;
    }

    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function isMarkdownHeading(line) {
    return /^#{1,6}\s+/.test(line.trim());
  }

  function isListItem(line) {
    return /^(?:[-*+]\s+|\d+[.)]\s+)/.test(line.trim());
  }

  function isTableRow(line) {
    return /^\s*\|.*\|\s*$/.test(line);
  }

  function isCodeFence(line) {
    return /^\s*```/.test(line);
  }

  function appendDocumentBlock(container, text, className) {
    const block = document.createElement('div');
    block.className = 'dp-help-legal-document-block' + (className ? ' ' + className : '');
    block.textContent = text;
    container.appendChild(block);
  }

  function renderFlowingBlock(container, blockText) {
    const lines = blockText.split('\n');

    if (lines.some(isCodeFence) || lines.every(function (line) {
      return !line.trim() || isTableRow(line);
    })) {
      appendDocumentBlock(container, blockText, 'is-structured');
      return;
    }

    if (lines.length === 1 && isMarkdownHeading(lines[0])) {
      appendDocumentBlock(container, lines[0].trim(), 'is-heading');
      return;
    }

    if (lines.some(isListItem)) {
      let current = '';
      let currentIsListItem = false;

      lines.forEach(function (line) {
        const trimmed = line.trim();
        if (!trimmed) return;

        if (isListItem(line) || isMarkdownHeading(line)) {
          if (current) {
            appendDocumentBlock(container, current, currentIsListItem ? 'is-list-item' : '');
          }
          current = trimmed;
          currentIsListItem = isListItem(line);
          return;
        }

        current = current ? current + ' ' + trimmed : trimmed;
      });

      if (current) {
        appendDocumentBlock(container, current, currentIsListItem ? 'is-list-item' : '');
      }
      return;
    }

    appendDocumentBlock(
      container,
      lines.map(function (line) { return line.trim(); }).filter(Boolean).join(' '),
      ''
    );
  }

  function renderBundledDocument(container, content) {
    const normalized = String(content || '').replace(/\r\n?/g, '\n').trim();
    container.replaceChildren();
    if (!normalized) return;

    normalized.split(/\n[\t ]*\n+/).forEach(function (blockText) {
      renderFlowingBlock(container, blockText);
    });
  }

  async function fetchBundledDocument(documentId) {
    const controller = new AbortController();
    const timeout = window.setTimeout(function () { controller.abort(); }, 8000);
    try {
      const response = await fetch('/api/legal-documents/' + encodeURIComponent(documentId), {
        method: 'GET',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {'Accept': 'application/json'},
        signal: controller.signal,
      });
      const payload = await response.json().catch(function () { return {}; });
      if (!response.ok) {
        throw new Error(payload.detail || response.statusText || 'Unable to load bundled document');
      }
      return payload;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function makeModal(opener) {
    const backdrop = document.createElement('div');
    backdrop.className = 'dp-backdrop dp-help-legal-backdrop';

    const dialog = document.createElement('section');
    dialog.className = 'dp-dialog dp-dialog--lg dp-help-legal-dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-labelledby', 'dp-help-legal-dialog-title');
    dialog.tabIndex = -1;

    const header = document.createElement('header');
    header.className = 'dp-dialog__header dp-help-legal-dialog-header';

    const title = document.createElement('div');
    title.className = 'dp-dialog__title';
    title.id = 'dp-help-legal-dialog-title';
    title.textContent = String(opener.textContent || 'Bundled document').trim();

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'dp-icon-btn dp-icon-btn--ghost dp-help-legal-close';
    close.setAttribute('aria-label', 'Close document');
    close.textContent = '×';

    header.append(title, close);

    const body = document.createElement('div');
    body.className = 'dp-dialog__body dp-help-legal-dialog-body';

    const latest = document.createElement('div');
    latest.className = 'dp-help-legal-latest';
    latest.setAttribute('role', 'note');
    latest.textContent = 'Loading bundled document…';

    const documentBody = document.createElement('div');
    documentBody.className = 'dp-help-legal-document';
    documentBody.textContent = 'Loading…';

    body.append(latest, documentBody);
    dialog.append(header, body);
    backdrop.appendChild(dialog);

    close.addEventListener('click', function () { closeModal(); });
    backdrop.addEventListener('click', function (event) {
      if (event.target === backdrop) closeModal();
    });
    dialog.addEventListener('keydown', function (event) {
      trapDialogKeydown(event, dialog);
    });

    return {backdrop, dialog, title, close, latest, documentBody, body};
  }

  async function openDocument(opener, documentId) {
    closeModal({restoreFocus: false});
    activeOpener = opener;

    const modal = makeModal(opener);
    activeBackdrop = modal.backdrop;
    document.body.classList.add('dp-help-legal-modal-open');
    document.body.appendChild(modal.backdrop);
    modal.close.focus();

    try {
      const payload = await fetchBundledDocument(documentId);
      if (activeBackdrop !== modal.backdrop) return;

      modal.title.textContent = payload.title || String(opener.textContent || 'Bundled document').trim();
      modal.latest.replaceChildren();

      const version = String(payload.bundled_version || 'this build').trim();
      const prefix = document.createElement('span');
      prefix.textContent = 'This is the copy bundled with DebridPulse ' + version + '. ';

      const link = document.createElement('a');
      link.href = String(payload.latest_url || '#');
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = 'View the latest version on GitHub.';

      modal.latest.append(prefix, link);
      renderBundledDocument(modal.documentBody, payload.content);
      modal.body.scrollTop = 0;
    } catch (error) {
      if (activeBackdrop !== modal.backdrop) return;
      modal.latest.classList.add('is-error');
      modal.latest.textContent = 'The bundled document could not be loaded.';
      renderBundledDocument(
        modal.documentBody,
        error && error.message ? String(error.message) : 'Unable to load bundled document.'
      );
    }
  }

  function bindEvents(view) {
    if (view.dataset.dpHelpLegalDocumentsBound === '1') return;
    view.dataset.dpHelpLegalDocumentsBound = '1';

    view.addEventListener('click', function (event) {
      const button = event.target.closest('.dp-help-local-document-button[data-legal-document]');
      if (!button || !view.contains(button)) return;
      event.preventDefault();
      void openDocument(button, button.dataset.legalDocument);
    });
  }

  function enhance() {
    const view = helpRoot();
    if (!view || !view.querySelector('.dp-help-license-actions')) return false;
    bindEvents(view);
    view.dataset.dpHelpLegalDocumentsReady = '1';
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
