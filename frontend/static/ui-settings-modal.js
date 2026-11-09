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
 * dialog; an action with `placement: 'leading'` takes the footer's far start
 * instead, and `handle.setActionHidden(id, hidden)` shows or withdraws it. An
 * action with `placement: 'title'` and an `icon` (an image URL) is the compact
 * icon control immediately after the title, in the header -- an edit of what
 * the title names -- whose `label` is its accessible name and tooltip;
 * `handle.focusAction(id)` returns focus to any action.
 * A dialog with more than one view changes its slot labels through
 * `handle.setAcceptLabel` / `setCancelLabel` -- and, for a view that asks a
 * different question, its title and tone through `handle.setTitle(title)` and
 * `handle.setTone(tone, acceptTone)`, the same two choices `open` takes -- and
 * (a title or slot label may be said words, {key, values}: its key is kept with
 * it, so a language change re-says it in place; plain text drops any key) -- and
 * may guard settlement:
 * `onAccept()` and `onCancel()` (also reached by Enter and Escape) keep the
 * dialog open by returning (or resolving to) `false` -- a Back, a refused
 * save -- and while an `onAccept` is pending the accept slot is busy.
 * `titleHint` is one short line of plain text directly beneath the title, in
 * the header: a reading of what the title names, never a control.
 * `headerEnd` (an element its client owns) is the header's far end: a choice
 * about the whole dialog's subject, kept beside the title, never a footer
 * action.
 * `closeControl: true` adds the upper-right close control, which is
 * the cancel slot's settlement under a second, conventional placement.
 * `cancelTone: 'danger'` makes the cancel slot a destructive abandonment: it
 * takes the red treatment and leads the footer, with the actions at the far
 * end. `blocking: true` asks for a decision: Escape is inert, as the backdrop
 * always is, so only a control settles the dialog.
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

  // The initiating control and the identities of everything it sits under, captured at open
  // (a dialog opened while another is still leaving returns where that one would have).
  function captureFocusOrigin() {
    const opener = window.DPMotion ? window.DPMotion.focusOrigin() : document.activeElement;
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

  // The one Escape owner and the one focus-trap owner, for the top-most dialog only
  // (a leaving dialog holds the keyboard itself until it has gone).
  function onKeydown(event) {
    const entry = mounted[mounted.length - 1];
    if (!entry || entry.leaving || event.defaultPrevented) return;
    const overlays = document.querySelectorAll(OVERLAY_SELECTOR);
    if (overlays[overlays.length - 1] !== entry.overlay) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      if (!entry.blocking) void entry.requestCancel();
    } else if (event.key === 'Tab') {
      trapTab(event, entry);
    } else if (event.key === 'Enter' && event.target instanceof HTMLInputElement
      && entry.body.contains(event.target) && entry.accept && !entry.accept.disabled) {
      // Enter in a body text field submits the dialog once its accept action is enabled.
      event.preventDefault();
      void entry.requestAccept();
    }
  }

  /* Words into a node. Said words ({key, values}, Settings' Said) keep their key
   * on the node, so DPI18n.render() re-says them where they are; plain text
   * drops any key the node held, so it is never re-said as earlier words. */
  function sayInto(node, value) {
    if (!node) return;
    node.textContent = String(value ?? '');
    if (value && typeof value === 'object' && typeof value.key === 'string') {
      node.dataset.i18n = value.key;
      if (value.values) node.dataset.i18nValues = JSON.stringify(value.values);
      else delete node.dataset.i18nValues;
    } else {
      delete node.dataset.i18n;
      delete node.dataset.i18nValues;
    }
  }
  const isSaid = value => !!value && typeof value === 'object' && typeof value.key === 'string';
  // A catalog message as said words: its key travels with it, so the node it
  // names is re-said on a language change (a default slot word or title).
  const catalogSaid = key => ({key, values: null, toString: () => window.DPI18n.t(key)});

  function open(spec = {}) {
    const role = spec.role === 'alertdialog' ? 'alertdialog' : 'dialog';
    const toneOf = value => (value === 'danger' || value === 'warning' || value === 'success' ? value : '');
    const tone = toneOf(spec.tone);
    const progress = spec.progress === true;
    const dismissOnly = spec.dismiss === true;
    const declared = Array.isArray(spec.actions) ? spec.actions : [];
    const titled = progress ? [] : declared.filter(action => action.placement === 'title');
    const actions = [...declared.filter(action => action.placement === 'leading'),
      ...declared.filter(action => action.placement !== 'leading' && action.placement !== 'title')];
    const blocking = progress || spec.blocking === true;
    const cancelSlot = spec.cancelTone === 'danger'
      ? '<button class="btn btn-danger dp-modal-cancel-leading" type="button" data-modal-cancel></button>'
      : '';
    // The accept control takes the dialog's danger/success tone; `acceptTone:
    // 'warning'` asks for the caution treatment for an accept that is
    // consequential but not destructive of its own.
    const ACCEPT_CLASSES = ['btn-warn', 'btn-danger', 'btn-success', 'btn-primary'];
    const acceptClassOf = (dialogTone, acceptTone) => (acceptTone === 'warning' ? 'btn-warn'
      : dialogTone === 'danger' ? 'btn-danger' : dialogTone === 'success' ? 'btn-success' : 'btn-primary');
    const acceptClass = acceptClassOf(tone, spec.acceptTone);
    const origin = captureFocusOrigin();
    const dialogId = `dp-modal-${sequence += 1}`;
    const titleHint = progress ? '' : String(spec.titleHint || '');
    const titleSlot = `<div class="dp-modal-title" id="${dialogId}-title"></div>`;

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
          ${titleHint ? `<div class="dp-modal-heading">${titleSlot}<p class="dp-modal-title-hint"></p></div>` : titleSlot}
          ${titled.map(() => '<button class="btn btn-ghost btn-sm dp-modal-title-action" type="button" data-modal-action><img alt="" aria-hidden="true"></button>').join('')}
          ${spec.headerEnd instanceof HTMLElement ? '<div class="dp-modal-header-end"></div>' : ''}
          ${spec.closeControl === true ? '<button class="btn btn-ghost dp-modal-close" type="button" data-modal-close data-i18n-attr="aria-label:dialog.close;data-tooltip:dialog.close"></button>' : ''}
        </header>
        <div class="dp-modal-body"></div>
        <footer class="dp-modal-footer">
          ${cancelSlot}
          ${actions.map(() => '<button class="btn" type="button" data-modal-action></button>').join('')}
          ${cancelSlot ? '' : '<button class="btn btn-ghost" type="button" data-modal-cancel></button>'}
          ${dismissOnly ? '' : `<button class="btn ${acceptClass}" type="button" data-modal-accept></button>`}
        </footer>
      </section>`;
    const dialog = overlay.querySelector('.dp-modal-dialog');
    const body = overlay.querySelector('.dp-modal-body');
    const cancel = overlay.querySelector('[data-modal-cancel]');
    const accept = overlay.querySelector('[data-modal-accept]');
    const close = overlay.querySelector('[data-modal-close]');
    const actionButtons = new Map();
    const placed = [...titled, ...actions];   // document order: the header's, then the footer's
    overlay.querySelectorAll('[data-modal-action]').forEach((button, index) => {
      const action = placed[index] || {};
      const id = String(action.id || index);
      button.dataset.modalAction = id;
      if (action.placement === 'title') {
        button.querySelector('img').src = String(action.icon || '');
        button.setAttribute('aria-label', String(action.label || ''));
        button.dataset.tooltip = String(action.label || '');
        // Said words: the name and tooltip are re-said in place.
        if (isSaid(action.label)) {
          button.dataset.i18nAttr = `aria-label:${action.label.key};data-tooltip:${action.label.key}`;
          if (action.label.values) button.dataset.i18nValues = JSON.stringify(action.label.values);
        }
      } else {
        button.classList.add(action.tone === 'success' ? 'btn-success' : action.tone === 'danger' ? 'btn-danger' : 'btn-ghost');
        if (action.placement === 'leading') button.classList.add('dp-modal-action-leading');
        sayInto(button, action.label || '');
      }
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
    if (titleHint) overlay.querySelector('.dp-modal-title-hint').textContent = titleHint;
    overlay.querySelector('.dp-modal-header-end')?.append(spec.headerEnd);
    // The one control of a dismiss-only dialog IS the cancel slot: closing is
    // the only outcome there is, so it needs no second name and no second
    // settlement path.
    if (cancel) cancel.textContent = String(spec.cancelLabel || window.DPI18n.t(dismissOnly ? 'dialog.close' : 'common.cancel'));
    if (close) window.DPI18n.render(close);
    if (accept) {
      accept.textContent = String(spec.acceptLabel || window.DPI18n.t('dialog.confirm'));
      accept.disabled = spec.acceptDisabled === true;
    }
    // ``spec.keys`` ({title, titleValues, accept, acceptValues, cancel, cancelValues,
    // message, messageValues}):
    // the catalog keys these words were said from, so an open dialog is re-worded
    // in place on a language change (ui-i18n.js render). Optional; words alone stay.
    const keys = spec.keys || {};
    const name = (node, key, values) => {
      if (!node || !key) return;
      node.dataset.i18n = key;
      if (values) node.dataset.i18nValues = JSON.stringify(values);
    };
    name(overlay.querySelector(`#${dialogId}-title`), keys.title, keys.titleValues);
    name(cancel, keys.cancel, keys.cancelValues);
    name(accept, keys.accept, keys.acceptValues);
    // The default slot words are the catalog's too, keyed like any other.
    if (!spec.cancelLabel && !keys.cancel) name(cancel, dismissOnly ? 'dialog.close' : 'common.cancel');
    if (!spec.acceptLabel && !keys.accept) name(accept, 'dialog.confirm');
    // Said words name their own node (a key given in ``keys`` is kept first).
    if (isSaid(spec.title) && !keys.title) sayInto(overlay.querySelector(`#${dialogId}-title`), spec.title);
    if (isSaid(spec.cancelLabel) && !keys.cancel) sayInto(cancel, spec.cancelLabel);
    if (isSaid(spec.acceptLabel) && !keys.accept) sayInto(accept, spec.acceptLabel);

    let settled = false;
    let resolveClosed;
    const closed = new Promise(resolve => { resolveClosed = resolve; });
    const entry = {overlay, dialog, body, accept, blocking, settle, requestAccept, requestCancel};

    function settle(accepted) {
      if (settled) return;
      settled = true;
      // It fades away in place, still mounted, covering the page and holding
      // the keyboard (ui-motion.js leave); its client learns the outcome now.
      // Only once it has gone are it, the page lock and the key handler
      // released and focus returned -- once, and never out of a dialog that
      // has opened meanwhile.
      entry.leaving = true;
      const leaving = window.DPMotion ? window.DPMotion.leave(overlay, dialog, resolveFocusTarget(origin)) : Promise.resolve();
      // ``gone``: once it has left, with wherever its own restoration left focus -- so a client that
      // places focus itself afterwards can tell that from a choice the operator made.
      const gone = leaving.then(() => {
        mounted.splice(mounted.indexOf(entry), 1);
        const target = resolveFocusTarget(origin);   // where it belongs now, after the exit
        if (window.DPMotion) window.DPMotion.restore(overlay, target);
        else { overlay.remove(); if (target) target.focus(); }
        if (!document.querySelector(OVERLAY_SELECTOR)) document.body.classList.remove(BODY_OPEN_CLASS);
        if (!mounted.length) document.removeEventListener('keydown', onKeydown);
        return document.activeElement;
      });
      resolveClosed(Object.freeze({accepted: accepted === true, gone}));
    }

    let accepting = false;
    // Settlement through the client's guards: a guard answering ``false``
    // keeps the dialog open (a Back, a refused save).
    async function requestAccept() {
      if (settled || accepting || (accept && accept.disabled)) return;
      if (typeof spec.onAccept !== 'function') { settle(true); return; }
      // Busy, not disabled: whether the accept slot is enabled stays the
      // client's to say (setAcceptEnabled), during and after the guard.
      accepting = true;
      dialog.setAttribute('aria-busy', 'true');
      let result;
      try {
        result = await spec.onAccept(handle);
      } finally {
        accepting = false;
        dialog.setAttribute('aria-busy', 'false');
      }
      if (result !== false) settle(true);
    }

    async function requestCancel() {
      if (settled || accepting) return;
      if (typeof spec.onCancel === 'function' && (await spec.onCancel(handle)) === false) return;
      settle(false);
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
      setActionHidden(id, hidden) {
        const button = actionButtons.get(String(id));
        if (button) button.hidden = !!hidden;
      },
      focusAction(id) { actionButtons.get(String(id))?.focus(); },
      setAcceptLabel(label) { sayInto(accept, label || ''); },
      setCancelLabel(label) { sayInto(cancel, label || ''); },
      setTitle(title) { if (!progress) sayInto(overlay.querySelector(`#${dialogId}-title`), title || ''); },
      setTone(next, acceptTone) {
        const value = toneOf(next);
        if (value) dialog.dataset.tone = value; else delete dialog.dataset.tone;
        if (accept) {
          accept.classList.remove(...ACCEPT_CLASSES);
          accept.classList.add(acceptClassOf(value, acceptTone));
        }
      },
      setBusy(busy) { dialog.setAttribute('aria-busy', busy ? 'true' : 'false'); },
    });

    const described = typeof spec.mount === 'function' ? spec.mount(body, handle) : null;
    if (described instanceof HTMLElement) {
      if (!described.id) described.id = `${dialogId}-description`;
      dialog.setAttribute('aria-describedby', described.id);
    }

    if (cancel) cancel.addEventListener('click', () => { void requestCancel(); });
    if (close) close.addEventListener('click', () => settle(false));
    if (accept) accept.addEventListener('click', () => { void requestAccept(); });
    actionButtons.forEach((button, id) => button.addEventListener('click', () => {
      if (!settled && !button.disabled && typeof spec.onAction === 'function') spec.onAction(id);
    }));

    if (!mounted.length) document.addEventListener('keydown', onKeydown);
    mounted.push(entry);
    if (window.DPMotion) window.DPMotion.enter(overlay, `${OVERLAY_SELECTOR}:not(.dp-auth-required-overlay)`);
    document.body.appendChild(overlay);
    document.body.classList.add(BODY_OPEN_CLASS);
    (cancel || dialog).focus();
    return handle;
  }

  function confirm({
    title,
    message,
    confirmLabel = '',
    cancelLabel = '',
    tone = 'warning',
    typedPhrase = '',
    withExit = false,
    keys = null,
  } = {}) {
    const dialog = open({
      role: 'alertdialog',
      tone,
      keys: keys || undefined,
      // Defaults are the catalog's keyed words; a caller's own label (plain or
      // said) is used as it is, and a key in ``keys`` still names its node.
      title: title || (keys && keys.title ? window.DPI18n.t(keys.title) : catalogSaid('dialog.confirm_action')),
      acceptLabel: confirmLabel || (keys && keys.accept ? window.DPI18n.t(keys.accept) : catalogSaid('dialog.confirm')),
      cancelLabel: cancelLabel || (keys && keys.cancel ? window.DPI18n.t(keys.cancel) : catalogSaid('common.cancel')),
      acceptDisabled: !!typedPhrase,
      mount(body, handle) {
        const messageEl = document.createElement('p');
        messageEl.className = 'dp-modal-message';
        // Said words keep their key (re-said in place); ``keys.message`` names plain words' key.
        if (isSaid(message)) sayInto(messageEl, message);
        else messageEl.textContent = String(message || '');
        if (keys && keys.message) {
          messageEl.dataset.i18n = keys.message;
          if (keys.messageValues) messageEl.dataset.i18nValues = JSON.stringify(keys.messageValues);
        }
        body.appendChild(messageEl);
        if (typedPhrase) {
          const typed = document.createElement('label');
          typed.className = 'dp-modal-typed';
          typed.innerHTML = '<span class="form-label"></span><input class="input" type="text" autocomplete="off" spellcheck="false">';
          // The phrase itself is typed as it is, in every language.
          sayInto(typed.querySelector('.form-label'), {key: 'dialog.type_to_confirm', values: {phrase: typedPhrase},
            toString: () => window.DPI18n.t('dialog.type_to_confirm', {phrase: typedPhrase})});
          const typedInput = typed.querySelector('input');
          typedInput.placeholder = typedPhrase;
          typedInput.addEventListener('input', () => handle.setAcceptEnabled(typedInput.value === typedPhrase));
          body.appendChild(typed);
        }
        return messageEl;
      },
    });
    // ``withExit``: the whole outcome, ``{accepted, gone}``, for a client that places focus itself.
    return withExit ? dialog.closed : dialog.closed.then(result => result.accepted);
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
    acceptLabel = '',
    cancelLabel = '',
    placeholder = '',
    // ``keys`` ({title, label, hint, placeholder, accept, cancel}): the catalog
    // keys of those words, so an open prompt is re-worded in place (its value
    // and focus untouched). Optional; words alone stay.
    keys = {},
  } = {}) {
    let field = null;
    // The default words are keyed like given ones, so they re-word in place too.
    keys = {...keys, accept: keys.accept || (acceptLabel ? undefined : 'common.save'),
      cancel: keys.cancel || (cancelLabel ? undefined : 'common.cancel')};
    acceptLabel = acceptLabel || window.DPI18n.t('common.save');
    cancelLabel = cancelLabel || window.DPI18n.t('common.cancel');
    const dialog = open({
      role: 'dialog',
      title: title || window.DPI18n.t('dialog.edit_value'),
      acceptLabel,
      cancelLabel,
      className: 'dp-modal-prompt',
      keys: {title: keys.title, accept: keys.accept, cancel: keys.cancel},
      mount(body) {
        const wrapper = document.createElement('label');
        wrapper.className = 'dp-modal-field';
        wrapper.innerHTML = '<span class="form-label"></span><input class="input" type="text" autocomplete="off" spellcheck="false">';
        wrapper.querySelector('.form-label').textContent = String(label || '');
        if (keys.label) wrapper.querySelector('.form-label').dataset.i18n = keys.label;
        field = wrapper.querySelector('input');
        field.value = String(value ?? '');
        if (placeholder) field.placeholder = String(placeholder);
        if (keys.placeholder) field.dataset.i18nAttr = `placeholder:${keys.placeholder}`;
        body.appendChild(wrapper);
        if (!hint) return null;
        const help = document.createElement('p');
        help.className = 'dp-modal-message';
        help.textContent = String(hint);
        if (keys.hint) help.dataset.i18n = keys.hint;
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
          sayInto(text, line);
          detail.appendChild(text);
        }
        body.appendChild(detail);
        return detail;
      },
    });
  }

  window.DPSettingsModal = Object.freeze({open, confirm, prompt, progress});
})();
