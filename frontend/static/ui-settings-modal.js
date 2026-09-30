/* DebridPulse canonical application-dialog owner.
 *
 * This module is the ONE physical owner of the shared dialog shell used by
 * Settings confirmations, Settings directory browsing, and the Downloads
 * removal confirmation. It owns: mount/unmount, overlay, dialog shell, ARIA
 * base wiring, header/body/footer slots, the focus trap, Escape, initial
 * focus, exactly-once settlement, focus restoration, body scroll locking, and
 * the neutral light/dark/narrow presentation hooks (ui-modal-contract.css).
 *
 * It owns no Settings persistence, no directory semantics and no list
 * semantics. A client supplies semantic content through `mount(body, dialog)`
 * at creation time and receives a narrow handle; nothing outside this module
 * may inspect or rewrite the shell DOM after it is created.
 *
 * A dialog that asks nothing has nothing to accept. `dismiss: true` is that
 * declared shape -- a read-only presentation whose footer is the single
 * control that closes it -- and the footer is composed HERE, by the one owner,
 * rather than by a client hiding a button it does not own. Everything else
 * about such a dialog is identical: same shell, same trap, same Escape, same
 * focus restoration, same settlement.
 *
 * A dialog whose content is operated on declares that too, and the footer is
 * still composed here: `actions` ([{id, label, tone}]) are client buttons placed
 * ahead of the cancel slot, reported through `onAction(id)` and enabled
 * through `handle.setActionEnabled(id, enabled)`; they never settle the
 * dialog. `closeControl: true` adds the upper-right close control, which is
 * the cancel slot's settlement under a second, conventional placement.
 *
 * A dialog that reports work the operator cannot stop declares `progress:
 * true` (built by `progress()`): its title is the centred status line in the
 * body, and it has no header, no footer and no control of any kind. Escape
 * and the backdrop are inert, focus is held on the dialog itself, and only
 * its client ends it -- `handle.close()` once the work has returned control,
 * or never, when the page itself is replaced.
 *
 * Focus contract (explicit lifecycle boundary: settlement of the dialog):
 *   1. The initiating control is restored when it is still focusable.
 *   2. If a refresh replaced it while the dialog was open, its equivalent
 *      replacement is restored (same tag and identity attributes, chosen by
 *      the identity of the ancestors it sits under) inside the nearest
 *      ancestor that survived.
 *   3. Otherwise the first focusable control of the nearest surviving region.
 *   Focus is never parked on <body>, and a detached control is never focused.
 * Accepting does not guess the consequence of the accepted operation: a client
 * whose operation removes the initiating control chooses its own successor
 * after its own re-render.
 */
(function () {
  'use strict';

  const OVERLAY_SELECTOR = '.dp-modal-overlay';
  const BODY_OPEN_CLASS = 'dp-modal-open';
  const FOCUSABLE = 'button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])';
  // Attributes that describe presentation or transient state, not identity.
  const NON_IDENTITY_ATTRIBUTE = /^(class|style|tabindex|disabled|hidden|autofocus)$|^on|^aria-(?!label$)/;

  const mounted = [];
  let sequence = 0;

  function isFocusable(element) {
    return !!element
      && element.isConnected
      && !element.disabled
      && !element.closest('[hidden], [inert]')
      && element.getClientRects().length > 0;
  }

  function identityOf(element) {
    const attributes = {};
    for (const attribute of element.attributes) {
      if (!NON_IDENTITY_ATTRIBUTE.test(attribute.name)) attributes[attribute.name] = attribute.value;
    }
    return {tag: element.localName, attributes};
  }

  function sharedIdentity(element, identity) {
    if (element.localName !== identity.tag) return 0;
    return Object.entries(identity.attributes)
      .filter(([name, value]) => element.getAttribute(name) === value).length;
  }

  function sameIdentity(element, identity) {
    return sharedIdentity(element, identity) === Object.keys(identity.attributes).length;
  }

  // The initiating control and the identities of everything it sits under, captured at open.
  function captureFocusOrigin() {
    const opener = document.activeElement;
    if (!(opener instanceof HTMLElement) || opener === document.body) return null;
    const chain = [];
    for (let node = opener; node && node !== document.body && node !== document.documentElement; node = node.parentElement) {
      chain.push({node, ...identityOf(node)});
    }
    return {opener, chain};
  }

  function equivalentControl(origin, survivorIndex) {
    const [own] = origin.chain;
    const candidates = Array.from(origin.chain[survivorIndex].node.querySelectorAll(own.tag))
      .filter(element => sameIdentity(element, own) && isFocusable(element));
    if (candidates.length === 1) return candidates[0];
    let best = null;
    let bestScore = 0;
    let tied = false;
    for (const element of candidates) {
      let score = 0;
      let ancestor = element;
      for (let level = 1; level < survivorIndex && ancestor; level += 1) {
        ancestor = ancestor.parentElement;
        if (ancestor) score += sharedIdentity(ancestor, origin.chain[level]);
      }
      if (score > bestScore) {
        best = element;
        bestScore = score;
        tied = false;
      } else if (score === bestScore && score > 0) {
        tied = true;
      }
    }
    return best && !tied ? best : null;
  }

  function resolveFocusTarget(origin) {
    if (!origin) return null;
    if (isFocusable(origin.opener)) return origin.opener;
    const survivorIndex = origin.chain.findIndex(link => link.node.isConnected);
    if (survivorIndex === -1) return null;
    if (survivorIndex > 0) {
      const equivalent = equivalentControl(origin, survivorIndex);
      if (equivalent) return equivalent;
    }
    for (let index = Math.max(survivorIndex, 1); index < origin.chain.length; index += 1) {
      const region = origin.chain[index].node;
      if (!region.isConnected) continue;
      const first = Array.from(region.querySelectorAll(FOCUSABLE)).find(isFocusable);
      if (first) return first;
    }
    return null;
  }

  function trapTab(event, entry) {
    const controls = Array.from(entry.dialog.querySelectorAll(FOCUSABLE)).filter(isFocusable);
    if (!controls.length) {
      // Nothing to move between: focus stays on (or returns to) the dialog itself.
      event.preventDefault();
      entry.dialog.focus();
      return;
    }
    const first = controls[0];
    const last = controls[controls.length - 1];
    const active = document.activeElement;
    if (!entry.dialog.contains(active)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    } else if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  // The one Escape owner and the one focus-trap owner, for the top-most dialog only.
  function onKeydown(event) {
    const entry = mounted[mounted.length - 1];
    if (!entry || event.defaultPrevented) return;
    const overlays = document.querySelectorAll(OVERLAY_SELECTOR);
    if (overlays[overlays.length - 1] !== entry.overlay) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      if (!entry.progress) entry.settle(false);
    } else if (event.key === 'Tab') {
      trapTab(event, entry);
    } else if (event.key === 'Enter' && event.target instanceof HTMLInputElement
      && entry.body.contains(event.target) && entry.accept && !entry.accept.disabled) {
      // Enter in a body text field submits the dialog once its accept action is enabled.
      event.preventDefault();
      entry.settle(true);
    }
  }

  function open(spec = {}) {
    const role = spec.role === 'alertdialog' ? 'alertdialog' : 'dialog';
    const tone = spec.tone === 'danger' || spec.tone === 'warning' || spec.tone === 'success' ? spec.tone : '';
    const progress = spec.progress === true;
    const dismissOnly = spec.dismiss === true;
    const actions = Array.isArray(spec.actions) ? spec.actions : [];
    const acceptClass = tone === 'danger' ? 'btn-danger' : tone === 'success' ? 'btn-success' : 'btn-primary';
    const origin = captureFocusOrigin();
    const dialogId = `dp-modal-${sequence += 1}`;

    const overlay = document.createElement('div');
    overlay.className = 'dp-modal-overlay';
    overlay.innerHTML = progress ? `
      <section class="dp-modal-dialog dp-modal-progress" role="${role}" aria-modal="true" aria-busy="true"
               aria-labelledby="${dialogId}-title" tabindex="-1">
        <div class="dp-modal-body">
          <p class="dp-modal-progress-status" id="${dialogId}-title"></p>
        </div>
      </section>` : `
      <section class="dp-modal-dialog" role="${role}" aria-modal="true" aria-labelledby="${dialogId}-title">
        <header class="dp-modal-header">
          <div class="dp-modal-title" id="${dialogId}-title"></div>
          ${spec.closeControl === true ? '<button class="btn btn-ghost dp-modal-close" type="button" data-modal-close aria-label="Close" title="Close"></button>' : ''}
        </header>
        <div class="dp-modal-body"></div>
        <footer class="dp-modal-footer">
          ${actions.map(() => '<button class="btn" type="button" data-modal-action></button>').join('')}
          <button class="btn btn-ghost" type="button" data-modal-cancel></button>
          ${dismissOnly ? '' : `<button class="btn ${acceptClass}" type="button" data-modal-accept></button>`}
        </footer>
      </section>`;
    const dialog = overlay.querySelector('.dp-modal-dialog');
    const body = overlay.querySelector('.dp-modal-body');
    const cancel = overlay.querySelector('[data-modal-cancel]');
    const accept = overlay.querySelector('[data-modal-accept]');
    const close = overlay.querySelector('[data-modal-close]');
    const actionButtons = new Map();
    overlay.querySelectorAll('[data-modal-action]').forEach((button, index) => {
      const action = actions[index] || {};
      const id = String(action.id || index);
      button.dataset.modalAction = id;
      button.classList.add(action.tone === 'success' ? 'btn-success' : action.tone === 'danger' ? 'btn-danger' : 'btn-ghost');
      button.textContent = String(action.label || '');
      button.disabled = action.disabled === true;
      actionButtons.set(id, button);
    });
    if (close) {
      close.innerHTML = window.DPIcons && typeof window.DPIcons.svg === 'function' ? window.DPIcons.svg('x') : '&times;';
    }
    if (tone) dialog.dataset.tone = tone;
    if (spec.className) dialog.classList.add(...String(spec.className).split(/\s+/).filter(Boolean));
    if (spec.bodyClassName) body.classList.add(...String(spec.bodyClassName).split(/\s+/).filter(Boolean));
    overlay.querySelector(`#${dialogId}-title`).textContent = String(spec.title || '');
    // The one control of a dismiss-only dialog IS the cancel slot: closing is
    // the only outcome there is, so it needs no second name and no second
    // settlement path.
    if (cancel) cancel.textContent = String(spec.cancelLabel || (dismissOnly ? 'Close' : 'Cancel'));
    if (accept) {
      accept.textContent = String(spec.acceptLabel || 'Confirm');
      accept.disabled = spec.acceptDisabled === true;
    }

    let settled = false;
    let resolveClosed;
    const closed = new Promise(resolve => { resolveClosed = resolve; });
    const entry = {overlay, dialog, body, accept, progress, settle};

    function settle(accepted) {
      if (settled) return;
      settled = true;
      mounted.splice(mounted.indexOf(entry), 1);
      overlay.remove();
      if (!document.querySelector(OVERLAY_SELECTOR)) document.body.classList.remove(BODY_OPEN_CLASS);
      if (!mounted.length) document.removeEventListener('keydown', onKeydown);
      const target = resolveFocusTarget(origin);
      if (target) target.focus();
      resolveClosed(Object.freeze({accepted: accepted === true}));
    }

    const handle = Object.freeze({
      closed,
      get isOpen() { return !settled; },
      close() { settle(false); },
      setAcceptEnabled(enabled) { if (accept) accept.disabled = !enabled; },
      setActionEnabled(id, enabled) {
        const button = actionButtons.get(String(id));
        if (button) button.disabled = !enabled;
      },
      setBusy(busy) { dialog.setAttribute('aria-busy', busy ? 'true' : 'false'); },
    });

    const described = typeof spec.mount === 'function' ? spec.mount(body, handle) : null;
    if (described instanceof HTMLElement) {
      if (!described.id) described.id = `${dialogId}-description`;
      dialog.setAttribute('aria-describedby', described.id);
    }

    if (cancel) cancel.addEventListener('click', () => settle(false));
    if (close) close.addEventListener('click', () => settle(false));
    if (accept) accept.addEventListener('click', () => settle(true));
    actionButtons.forEach((button, id) => button.addEventListener('click', () => {
      if (!settled && !button.disabled && typeof spec.onAction === 'function') spec.onAction(id);
    }));

    if (!mounted.length) document.addEventListener('keydown', onKeydown);
    mounted.push(entry);
    document.body.appendChild(overlay);
    document.body.classList.add(BODY_OPEN_CLASS);
    (cancel || dialog).focus();
    return handle;
  }

  function confirm({
    title,
    message,
    confirmLabel = 'Confirm',
    cancelLabel = 'Cancel',
    tone = 'warning',
    typedPhrase = '',
  } = {}) {
    const dialog = open({
      role: 'alertdialog',
      tone,
      title: title || 'Confirm action',
      acceptLabel: confirmLabel || 'Confirm',
      cancelLabel: cancelLabel || 'Cancel',
      acceptDisabled: !!typedPhrase,
      mount(body, handle) {
        const messageEl = document.createElement('p');
        messageEl.className = 'dp-modal-message';
        messageEl.textContent = String(message || '');
        body.appendChild(messageEl);
        if (typedPhrase) {
          const typed = document.createElement('label');
          typed.className = 'dp-modal-typed';
          typed.innerHTML = '<span class="form-label"></span><input class="input" type="text" autocomplete="off" spellcheck="false">';
          typed.querySelector('.form-label').textContent = `Type ${typedPhrase} to confirm.`;
          const typedInput = typed.querySelector('input');
          typedInput.placeholder = typedPhrase;
          typedInput.addEventListener('input', () => handle.setAcceptEnabled(typedInput.value === typedPhrase));
          body.appendChild(typed);
        }
        return messageEl;
      },
    });
    return dialog.closed.then(result => result.accepted);
  }

  /* The single-text-field dialog shape.
   *
   * It exists here, beside confirm(), because this module is the ONE physical
   * owner of the application dialog shell -- a browser-native prompt() bypasses
   * the visual, focus and accessibility contract entirely, and a second modal
   * implementation would duplicate the owner. Resolves to the entered string,
   * or null when the operator cancels (Escape, Cancel, or backdrop dismissal),
   * exactly like the primitive it replaces. A blank value is a legal answer.
   */
  function prompt({
    title,
    label,
    value = '',
    hint = '',
    acceptLabel = 'Save',
    cancelLabel = 'Cancel',
    placeholder = '',
  } = {}) {
    let field = null;
    const dialog = open({
      role: 'dialog',
      title: title || 'Edit value',
      acceptLabel,
      cancelLabel,
      className: 'dp-modal-prompt',
      mount(body) {
        const wrapper = document.createElement('label');
        wrapper.className = 'dp-modal-field';
        wrapper.innerHTML = '<span class="form-label"></span><input class="input" type="text" autocomplete="off" spellcheck="false">';
        wrapper.querySelector('.form-label').textContent = String(label || '');
        field = wrapper.querySelector('input');
        field.value = String(value ?? '');
        if (placeholder) field.placeholder = String(placeholder);
        body.appendChild(wrapper);
        if (!hint) return null;
        const help = document.createElement('p');
        help.className = 'dp-modal-message';
        help.textContent = String(hint);
        body.appendChild(help);
        return help;
      },
    });
    // The shell focuses Cancel by default; a text dialog starts in its field.
    if (field) {
      field.focus();
      field.select();
    }
    return dialog.closed.then(result => (result.accepted && field ? field.value : null));
  }

  /* The non-dismissible progress shape: one centred status line (`title`)
   * and, beneath it, static centred explanatory `lines`. It resolves nothing
   * by itself; the returned handle's close() is the only way it ends. */
  function progress({title, lines = [], className = ''} = {}) {
    return open({
      role: 'alertdialog',
      progress: true,
      title,
      className,
      mount(body) {
        const detail = document.createElement('div');
        detail.className = 'dp-modal-progress-detail';
        for (const line of lines) {
          const text = document.createElement('p');
          text.textContent = String(line);
          detail.appendChild(text);
        }
        body.appendChild(detail);
        return detail;
      },
    });
  }

  window.DPSettingsModal = Object.freeze({open, confirm, prompt, progress});
})();
