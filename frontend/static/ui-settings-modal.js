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
    if (!controls.length) return;
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
      entry.settle(false);
    } else if (event.key === 'Tab') {
      trapTab(event, entry);
    } else if (event.key === 'Enter' && event.target instanceof HTMLInputElement
      && entry.body.contains(event.target) && !entry.accept.disabled) {
      // Enter in a body text field submits the dialog once its accept action is enabled.
      event.preventDefault();
      entry.settle(true);
    }
  }

  function open(spec = {}) {
    const role = spec.role === 'alertdialog' ? 'alertdialog' : 'dialog';
    const tone = spec.tone === 'danger' || spec.tone === 'warning' ? spec.tone : '';
    const origin = captureFocusOrigin();
    const dialogId = `dp-modal-${sequence += 1}`;

    const overlay = document.createElement('div');
    overlay.className = 'dp-modal-overlay';
    overlay.innerHTML = `
      <section class="dp-modal-dialog" role="${role}" aria-modal="true" aria-labelledby="${dialogId}-title">
        <header class="dp-modal-header">
          <div class="dp-modal-title" id="${dialogId}-title"></div>
        </header>
        <div class="dp-modal-body"></div>
        <footer class="dp-modal-footer">
          <button class="btn btn-ghost" type="button" data-modal-cancel></button>
          <button class="btn ${tone === 'danger' ? 'btn-danger' : 'btn-primary'}" type="button" data-modal-accept></button>
        </footer>
      </section>`;
    const dialog = overlay.querySelector('.dp-modal-dialog');
    const body = overlay.querySelector('.dp-modal-body');
    const cancel = overlay.querySelector('[data-modal-cancel]');
    const accept = overlay.querySelector('[data-modal-accept]');
    if (tone) dialog.dataset.tone = tone;
    if (spec.className) dialog.classList.add(...String(spec.className).split(/\s+/).filter(Boolean));
    if (spec.bodyClassName) body.classList.add(...String(spec.bodyClassName).split(/\s+/).filter(Boolean));
    overlay.querySelector('.dp-modal-title').textContent = String(spec.title || '');
    cancel.textContent = String(spec.cancelLabel || 'Cancel');
    accept.textContent = String(spec.acceptLabel || 'Confirm');
    accept.disabled = spec.acceptDisabled === true;

    let settled = false;
    let resolveClosed;
    const closed = new Promise(resolve => { resolveClosed = resolve; });
    const entry = {overlay, dialog, body, accept, settle};

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
      setAcceptEnabled(enabled) { accept.disabled = !enabled; },
      setBusy(busy) { dialog.setAttribute('aria-busy', busy ? 'true' : 'false'); },
    });

    const described = typeof spec.mount === 'function' ? spec.mount(body, handle) : null;
    if (described instanceof HTMLElement) {
      if (!described.id) described.id = `${dialogId}-description`;
      dialog.setAttribute('aria-describedby', described.id);
    }

    cancel.addEventListener('click', () => settle(false));
    accept.addEventListener('click', () => settle(true));

    if (!mounted.length) document.addEventListener('keydown', onKeydown);
    mounted.push(entry);
    document.body.appendChild(overlay);
    document.body.classList.add(BODY_OPEN_CLASS);
    cancel.focus();
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

  window.DPSettingsModal = Object.freeze({open, confirm, prompt});
})();
