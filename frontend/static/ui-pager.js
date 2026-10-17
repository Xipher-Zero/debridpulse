/* The one DP pagination presentation: First, Previous, the current page,
 * Next and Last in five fixed positions, so the current page stays centred
 * whatever is shown around it.
 *
 * Presentational only. Each page's owner keeps its own authority -- what the
 * pages are, which page is current, how a page is fetched -- and tells this
 * owner just the current page, how many pages follow it (0, 1, or 2 and more)
 * and what to do for each action. First shows from page 3 (Previous already
 * reaches page 1 from page 2) and Last only while two or more pages follow
 * (Next already reaches the last from the one before it); an absent control
 * leaves its slot empty and inert. A Last that cannot be proven is shown
 * disabled with its reason, never guessed. While the owner's request is
 * unresolved every action is inert.
 *
 * The slots and buttons are created once per host and only updated, so a
 * focused control that is still shown keeps its element and focus.
 */
(function () {
  'use strict';

  const ACTIONS = Object.freeze([
    ['first', 'First page', 'chevronFirst'],
    ['previous', 'Previous page', 'chevronLeft'],
    ['next', 'Next page', 'chevronRight'],
    ['last', 'Last page', 'chevronLast'],
  ]);
  const hosts = new WeakMap();

  function mount(host) {
    let parts = hosts.get(host);
    if (parts) return parts;
    const slot = () => {
      const node = document.createElement('span');
      node.className = 'dp-pager-slot';
      return node;
    };
    parts = {slots: {}, buttons: {}, placeholders: {}, model: null};
    for (const [name, label, icon] of ACTIONS) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn btn-ghost btn-sm dp-pager-btn';
      button.dataset.dpPager = name;
      button.setAttribute('aria-label', label);
      button.innerHTML = window.DPIcons?.svg?.(icon) || '';
      button.addEventListener('click', () => {
        const action = parts.model?.actions?.[name];
        if (!button.disabled && typeof action === 'function') action();
      });
      const placeholder = document.createElement('span');
      placeholder.className = 'dp-pager-placeholder';
      placeholder.setAttribute('aria-hidden', 'true');
      parts.buttons[name] = button;
      parts.placeholders[name] = placeholder;
      parts.slots[name] = slot();
    }
    parts.current = document.createElement('span');
    parts.current.className = 'btn btn-primary btn-sm dp-pager-current';
    parts.current.setAttribute('aria-current', 'page');
    const centre = slot();
    centre.appendChild(parts.current);
    host.classList.add('dp-pager');
    host.replaceChildren(parts.slots.first, parts.slots.previous, centre, parts.slots.next, parts.slots.last);
    hosts.set(host, parts);
    return parts;
  }

  function sync(parts) {
    const {page, following, lastUnavailable, pending} = parts.model;
    const shown = {first: page >= 3, previous: page > 1, next: following >= 1, last: following >= 2};
    for (const [name, label] of ACTIONS) {
      const button = parts.buttons[name];
      const unavailable = name === 'last' && Boolean(lastUnavailable);
      button.disabled = Boolean(pending) || unavailable;
      button.setAttribute('aria-label', unavailable ? `${label} unavailable: ${lastUnavailable}` : label);
      if (unavailable) button.dataset.tooltip = lastUnavailable;
      else button.removeAttribute('data-tooltip');
      const child = shown[name] ? button : parts.placeholders[name];
      if (parts.slots[name].firstChild !== child) parts.slots[name].replaceChildren(child);
    }
    parts.current.textContent = String(page);
  }

  /* ``model``: {page, following, actions: {first, previous, next, last},
   * lastUnavailable?: reason, pending?: boolean}. */
  function render(host, model) {
    if (!host) return;
    const parts = mount(host);
    parts.model = {page: Math.max(1, Number(model?.page) || 1),
                   following: Math.max(0, Number(model?.following) || 0),
                   actions: model?.actions || {}, lastUnavailable: String(model?.lastUnavailable || ''),
                   pending: Boolean(model?.pending)};
    sync(parts);
  }

  // The owner's authoritative request started (true) or ended (false).
  function pending(host, value) {
    const parts = host && hosts.get(host);
    if (!parts?.model) return;
    parts.model.pending = Boolean(value);
    sync(parts);
  }

  window.DPPager = Object.freeze({render, pending});
})();
