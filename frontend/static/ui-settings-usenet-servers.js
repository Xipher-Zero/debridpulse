/* Usenet news-server collection owner.
 *
 * ui-settings-page.js renders the whole collection -- every server card, its
 * fields, its actions and the Add Server tile. This owner only binds behavior:
 * adding and removing cards, the derived/overridden display name, and the
 * Save / Test actions. It rewrites no markup another owner rendered, and it
 * persists exclusively through the one canonical namespace
 * (`integrations.usenet`); there is no second settings store.
 *
 * Each card addresses ONE canonical server by its stable id, so saving a card
 * never persists another card's unsaved edits and removing a card never
 * disturbs a survivor. A blank password is simply omitted from the request,
 * which the backend reads as "keep this server's stored credential"; erasing
 * one requires the explicit clear control. No secret is ever held here.
 */
(function () {
  'use strict';

  const PANEL = '#view-settings [data-panel="sources"]';
  const collection = () => document.querySelector(`${PANEL} [data-usenet-collection]`);

  function cards(host) {
    return Array.from(host.querySelectorAll('[data-usenet-server-id]'));
  }

  function fieldValue(card, name) {
    const node = card.querySelector(`[data-usenet-field="${name}"]`);
    if (!node) return '';
    return node.type === 'checkbox' ? node.checked : node.value;
  }

  function readCard(card) {
    const password = String(fieldValue(card, 'password') || '');
    const payload = {
      host: String(fieldValue(card, 'host') || '').trim(),
      port: Number(fieldValue(card, 'port') || defaultPort(card)) || defaultPort(card),
      ssl: !!fieldValue(card, 'ssl'),
      username: String(fieldValue(card, 'username') || ''),
      // A server with no connections cannot acquire; DebridPulse's floor is 1
      // and Enable is the control for switching a server off.
      connections: Math.min(500, Math.max(1, Number(fieldValue(card, 'connections')) || 8)),
      priority: Number(fieldValue(card, 'priority') || 0),
      enabled: true,
      display_name: card.dataset.usenetNameOverride === '1'
        ? String(card.querySelector('[data-usenet-display-name]')?.textContent || '').trim()
        : '',
      clear_password: !!card.querySelector('[data-usenet-clear-password]')?.checked,
    };
    // A blank field is an absent field: the stored credential is preserved.
    if (password) payload.password = password;
    return payload;
  }

  const serverId = card => String(card.dataset.usenetServerId || '');

  // Conventional NNTP ports; used only to follow an SSL change while the port
  // still holds the other convention's default (finding 7).
  const SSL_PORT = 563;
  const PLAIN_PORT = 119;
  const defaultPort = card => (fieldValue(card, 'ssl') ? SSL_PORT : PLAIN_PORT);

  function followSslPort(card) {
    const port = card.querySelector('[data-usenet-field="port"]');
    if (!port) return;
    const current = Number(port.value);
    const secure = !!fieldValue(card, 'ssl');
    // Never overwrite a deliberate, non-conventional port.
    if (current === SSL_PORT || current === PLAIN_PORT || !current) {
      port.value = String(secure ? SSL_PORT : PLAIN_PORT);
    }
  }

  /* The display name is derived from Host until the operator overrides it;
   * an override survives later Host edits, and clearing it returns the name
   * to derived behavior. */
  function refreshDerivedName(card) {
    if (card.dataset.usenetNameOverride === '1') return;
    const label = card.querySelector('[data-usenet-display-name]');
    if (!label) return;
    label.textContent = String(fieldValue(card, 'host') || '').trim() || 'New server';
  }

  function status(card, message, tone) {
    const node = card.querySelector('[data-usenet-status]');
    if (!node) return;
    node.textContent = message || '';
    node.hidden = !message;
    node.dataset.tone = tone || '';
  }

  /* A brand-new card carries an EMPTY canonical id: the backend mints one when
   * the card is first saved. It never invents an id locally. */
  function blankCard() {
    const wrapper = document.createElement('div');
    wrapper.innerHTML = `
      <div class="dp-usenet-server" data-usenet-server-id="" data-usenet-password-configured="0"
           data-usenet-name-override="0">
        <div class="dp-usenet-server-head">
          <span class="dp-usenet-server-name" data-usenet-display-name>New server</span>
          <button type="button" class="btn btn-ghost btn-sm dp-usenet-name-edit" data-usenet-action="rename"
                  title="Edit display name" aria-label="Edit display name">
            <img src="/icons/lucide/pencil.svg" alt="" aria-hidden="true">
          </button>
        </div>
        <div class="dp-usenet-row dp-usenet-row--host">
          <label class="dp-usenet-field dp-usenet-field--host">
            <span class="form-label">Host</span>
            <input class="input" type="text" data-usenet-field="host" value="" autocomplete="off"
                   placeholder="news.example.com">
          </label>
          <label class="dp-usenet-field dp-usenet-field--port">
            <span class="form-label">Port</span>
            <input class="input" type="number" min="1" max="65535" data-usenet-field="port" value="563">
          </label>
          <label class="dp-usenet-ssl toggle-row">
            <span class="tl">SSL</span>
            <span class="toggle">
              <input type="checkbox" data-usenet-field="ssl" checked>
              <span class="ttrack"></span>
            </span>
          </label>
        </div>
        <div class="dp-usenet-row">
          <label class="dp-usenet-field dp-usenet-field--wide">
            <span class="form-label">Username</span>
            <input class="input" type="text" data-usenet-field="username" value="" autocomplete="off">
          </label>
        </div>
        <div class="dp-usenet-row">
          <label class="dp-usenet-field dp-usenet-field--wide">
            <span class="form-label">Password</span>
            <input class="input" type="password" data-usenet-field="password" value=""
                   autocomplete="off" placeholder="Password">
          </label>
        </div>
        <div class="dp-usenet-row dp-usenet-row--tuning">
          <label class="dp-usenet-field">
            <span class="form-label">Connections</span>
            <input class="input" type="number" min="1" max="500" data-usenet-field="connections" value="8">
          </label>
          <label class="dp-usenet-field">
            <span class="form-label">Priority</span>
            <input class="input" type="number" min="0" max="99" data-usenet-field="priority" value="0">
          </label>
        </div>
        <div class="dp-usenet-actions">
          <button type="button" class="btn btn-sm" data-usenet-action="save">Save</button>
          <button type="button" class="btn btn-ghost btn-sm" data-usenet-action="test">Test</button>
          <button type="button" class="btn btn-ghost btn-sm dp-usenet-remove" data-usenet-action="remove">Remove</button>
        </div>
        <p class="dp-usenet-priority-hint">Lower values have priority.</p>
        <p class="dp-usenet-server-status" role="status" aria-live="polite" data-usenet-status hidden></p>
      </div>`;
    return wrapper.firstElementChild;
  }

  /* Cards always pack left in DOM order and the Add tile stays last, so removing
   * a middle card can never leave a visual hole. */
  function reindex(host) {
    const tile = host.querySelector('[data-usenet-action="add"]');
    if (tile) host.appendChild(tile);
  }

  /* Exactly one card is written, addressed by its canonical id. */
  async function save(host, card) {
    const server = readCard(card);
    if (!server.host) { status(card, 'A server host is required.', 'error'); return; }
    status(card, 'Saving…', 'info');
    try {
      const id = serverId(card);
      const result = id
        ? await api('PUT', `/usenet/servers/${encodeURIComponent(id)}`, server, 30000)
        : await api('POST', '/usenet/servers', server, 30000);
      if (!id && result?.server_id) card.dataset.usenetServerId = String(result.server_id);
      // The credential is now stored, so the card stops holding it.
      const field = card.querySelector('[data-usenet-field="password"]');
      if (field) field.value = '';
      const clear = card.querySelector('[data-usenet-clear-password]');
      if (clear) clear.checked = false;
      status(card, nativeMessage(result) || 'Saved.', result?.native?.applied === false ? 'error' : 'ok');
    } catch (error) {
      status(card, `Could not save: ${error?.message || error}`, 'error');
    }
  }

  /* A save is only truthful if the service actually accepted the configuration. */
  function nativeMessage(result) {
    if (result?.native && result.native.applied === false) {
      return `Saved, but the download service did not accept it: ${result.native.detail || 'unknown reason'}`;
    }
    return '';
  }

  async function removeCard(host, card) {
    const id = serverId(card);
    status(card, 'Removing…', 'info');
    try {
      if (id) await api('DELETE', `/usenet/servers/${encodeURIComponent(id)}`, null, 30000);
      card.remove();
      reindex(host);
    } catch (error) {
      status(card, `Could not remove: ${error?.message || error}`, 'error');
    }
  }

  async function test(host, card) {
    const server = readCard(card);
    if (!server.host) { status(card, 'A server host is required.', 'error'); return; }
    status(card, 'Testing…', 'info');
    try {
      const result = await api('POST', '/usenet/servers/test', {
        host: server.host, port: server.port, ssl: server.ssl,
        username: server.username, password: server.password || '',
        connections: server.connections, server_id: serverId(card) || null,
      }, 60000);
      status(card, result?.message || (result?.ok ? 'Connection successful.' : 'Test failed.'),
             result?.ok ? 'ok' : 'error');
    } catch (error) {
      status(card, `Test failed: ${error?.message || error}`, 'error');
    }
  }

  function rename(card) {
    const label = card.querySelector('[data-usenet-display-name]');
    if (!label) return;
    const derived = String(fieldValue(card, 'host') || '').trim();
    const current = card.dataset.usenetNameOverride === '1' ? label.textContent.trim() : '';
    const next = window.prompt('Display name (leave blank to use the host)', current);
    if (next === null) return;
    const trimmed = next.trim();
    card.dataset.usenetNameOverride = trimmed ? '1' : '0';
    label.textContent = trimmed || derived || 'New server';
  }

  function onClick(event) {
    const host = collection();
    if (!host || !host.contains(event.target)) return;
    const action = event.target.closest('[data-usenet-action]');
    if (!action) return;
    const kind = action.dataset.usenetAction;
    const card = action.closest('[data-usenet-server-id]');
    if (kind === 'add') {
      event.preventDefault();
      host.insertBefore(blankCard(), action);
      reindex(host);
      host.querySelector('[data-usenet-server-id]:last-of-type [data-usenet-field="host"]')?.focus();
      return;
    }
    if (!card) return;
    event.preventDefault();
    if (kind === 'remove') {
      void removeCard(host, card);
    } else if (kind === 'save') {
      void save(host, card);
    } else if (kind === 'test') {
      void test(host, card);
    } else if (kind === 'rename') {
      rename(card);
    }
  }

  function onInput(event) {
    const host = collection();
    if (!host || !host.contains(event.target)) return;
    const field = event.target.dataset?.usenetField;
    if (field !== 'host' && field !== 'ssl') return;
    const card = event.target.closest('[data-usenet-server-id]');
    if (!card) return;
    if (field === 'host') refreshDerivedName(card);
    else followSslPort(card);
  }

  function bind() {
    const host = collection();
    if (!host || host.dataset.dpUsenetOwner === '1') return;
    host.dataset.dpUsenetOwner = '1';
    reindex(host);
  }

  document.addEventListener('click', onClick);
  document.addEventListener('input', onInput);
  document.addEventListener('debridpulse:settings-rendered', bind);
  document.addEventListener('DOMContentLoaded', bind, {once: true});
  bind();

  window.DPUsenetServers = Object.freeze({readCard, refreshDerivedName, followSslPort});
})();
