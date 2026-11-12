/* Usenet news-server collection owner.
 *
 * ui-settings-page.js renders the whole collection -- every server card, its
 * fields, its actions and the Add Server tile. This owner only binds behavior:
 * adding and removing cards, the derived/overridden display name, and the
 * Clear / Test actions. It rewrites no markup another owner rendered, and it
 * persists exclusively through the one canonical namespace
 * (`integrations.usenet`); there is no second settings store.
 *
 * Each card addresses ONE canonical server by its stable id, so a write to one
 * record never touches another and removing a card never disturbs a survivor.
 * A blank password is simply omitted from the request, which the backend reads
 * as "keep this server's stored credential"; erasing one requires the explicit
 * clear control. No secret is ever held here.
 *
 * Usenet owns NO notification system. The RESULT of any action is reported by
 * the application's one canonical toast owner, exactly like every other
 * operator-visible action. What survives here is inline FIELD
 * VALIDATION -- a different thing, which explains a missing or malformed value
 * on this card and never reports the outcome of an operation.
 *
 * A card is NOT a credential transaction. Every control on it is classified by
 * ITS OWN semantics and risk, exactly like every other Settings control -- a
 * field does not become gated merely because a password shares its card:
 *
 *   host / port / username / connections / priority /
 *   articles per request / timeout          changed-blur
 *   password (entry / replacement)          changed-blur -- entering a value is
 *                                           an ordinary value change, however
 *                                           sensitive the value is
 *   SSL                                     immediate (the mutation IS the act)
 *   display name                            committed when its dialog accepts
 *   Test / Remove / Clear / Add Server      explicit-action
 *
 * The generic behaviour -- baseline, dirty comparison, per-record
 * serialization, stale-response protection, convergence and rollback -- belongs
 * to ui-settings-persistence.js; this file only DECLARES the classes and says
 * how one record is written.
 *
 * A card with no canonical id yet is the one deliberate exception. There is no
 * record for a field to be written to, so the canonical persistence owner asks
 * THIS scope to materialize one the first time an ordinary commit boundary is
 * crossed on a draft that has a Host; from that moment the card joins the
 * universal model. There is no Save.
 *
 * The setup wizard draws the same cards on a surface that stores its fields
 * with its own Save (`data-commit-explicit`; ui-settings-persistence.js): there
 * a field crosses no boundary until that Save, which writes an existing record
 * field by field and creates a new card's record through the same `materialize`
 * creation -- whose outcome it waits for. Every action keeps its own semantics.
 */
(function () {
  'use strict';

  /* Words (1.0.14 localization): the catalog's, keyed so DPI18n.render() says
   * them again in place on a language change -- the card's fields, focus and
   * drafts untouched. The same keys as the page's own server card
   * (ui-settings-page.js usenetServerCard). */
  const t = (key, values) => window.DPI18n.t(key, values);
  const NEW_SERVER = 'settings.usenet.new_server';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[ch]);
  // An argument that is itself a message ({$t, $v}), said first.
  const plain = values => values && Object.fromEntries(Object.entries(values).map(([name, value]) =>
    [name, value && typeof value === 'object' && value.$t ? t(value.$t, plain(value.$v || {})) : value]));
  // The keying of one element: its text key, its attributes' keys, one argument object.
  function keyed(textKey, attrs = {}, values = null) {
    const parts = textKey ? [`data-i18n="${textKey}"`] : [];
    const pairs = [];
    for (const [name, key] of Object.entries(attrs)) {
      parts.push(`${name}="${esc(t(key, plain(values) || undefined))}"`);
      pairs.push(`${name}:${key}`);
    }
    if (pairs.length) parts.push(`data-i18n-attr="${pairs.join(';')}"`);
    if (values) parts.push(`data-i18n-values="${esc(JSON.stringify(values))}"`);
    return parts.join(' ');
  }
  const said = (tag, key, attrs = '') => `<${tag}${attrs ? ` ${attrs}` : ''} data-i18n="${key}">${esc(t(key))}</${tag}>`;
  // One attribute of `node` said from `key` with `values`; its other keyed attributes stay.
  function sayAttr(node, name, key, values) {
    if (!node) return;
    const pairs = String(node.dataset.i18nAttr || '').split(';').filter(pair => pair && !pair.startsWith(`${name}:`));
    pairs.push(`${name}:${key}`);
    node.dataset.i18nAttr = pairs.join(';');
    node.dataset.i18nValues = JSON.stringify({...JSON.parse(node.dataset.i18nValues || '{}'), ...values});
    node.setAttribute(name, t(key, plain(values)));
  }

  const PANEL = '#view-settings [data-panel="sources"]';
  const collection = () => document.querySelector(`${PANEL} [data-usenet-collection]`);
  // The collection an event happened in: Services' or the setup wizard's copy
  // (ui-settings-page.js mountProvider), each its own cards and its own records.
  const collectionOf = event => event.target?.closest?.('[data-usenet-collection]') || null;

  function cards(host) {
    return Array.from(host.querySelectorAll('[data-usenet-server-id]'));
  }

  function fieldValue(card, name) {
    const node = card.querySelector(`[data-usenet-field="${name}"]`);
    if (!node) return '';
    return node.type === 'checkbox' ? node.checked : node.value;
  }

  /* The display name the operator has explicitly CHOSEN right now -- '' while
   * the name is still derived from Host. */
  function overrideName(card) {
    return card.dataset.usenetNameOverride === '1'
      ? String(card.querySelector('[data-usenet-display-name]')?.textContent || '').trim()
      : '';
  }

  /* Cards whose creation write is in flight. A card has no canonical identity
   * in that window, so anything that must reach the record -- above all its
   * REMOVAL -- has to be able to wait for the id rather than act without one. */
  const creations = new WeakMap();

  function readCard(card) {
    const password = String(fieldValue(card, 'password') || '');
    const payload = {
      host: String(fieldValue(card, 'host') || '').trim(),
      // An empty port follows the SSL convention; any other value is exactly
      // what the card shows.
      port: String(fieldValue(card, 'port') ?? '') === '' ? defaultPort(card) : numberField(card, 'port'),
      ssl: !!fieldValue(card, 'ssl'),
      username: String(fieldValue(card, 'username') || ''),
      // Exactly what the card shows: a value outside the canonical model's
      // bounds (the control's own min/max) is refused by the server and the
      // card says why -- never clamped or replaced underneath the operator.
      connections: numberField(card, 'connections'),
      priority: numberField(card, 'priority'),
      articles_per_request: numberField(card, 'articles_per_request'),
      timeout_seconds: numberField(card, 'timeout_seconds'),
      // Whatever the card's own Enable control currently says. A draft created
      // with it switched off is created disabled; nothing here asserts
      // participation the operator did not choose.
      enabled: !!fieldValue(card, 'enabled'),
      display_name: overrideName(card),
    };
    // A blank field is an absent field: the stored credential is preserved.
    if (password) payload.password = password;
    return payload;
  }

  // A numeric card value read exactly: the plain decimal the card shows is
  // sent as that number (the server states its bounds); anything else is sent
  // as the text it is, for the server to refuse -- never guessed into a number.
  function numberField(card, key) {
    const raw = String(fieldValue(card, key) ?? '');
    return /^-?\d+(?:\.\d+)?$/.test(raw) ? Number(raw) : raw;
  }

  const serverId = card => String(card.dataset.usenetServerId || '');

  // Conventional NNTP ports; used only to follow an SSL change while the port
  // still holds the other convention's default (finding 7).
  const SSL_PORT = 563;
  const PLAIN_PORT = 119;
  const defaultPort = card => (fieldValue(card, 'ssl') ? SSL_PORT : PLAIN_PORT);

  function followSslPort(card) {
    const port = card.querySelector('[data-usenet-field="port"]');
    if (!port) return false;
    const current = Number(port.value);
    const secure = !!fieldValue(card, 'ssl');
    // Never overwrite a deliberate, non-conventional port.
    if (current === SSL_PORT || current === PLAIN_PORT || !current) {
      const next = String(secure ? SSL_PORT : PLAIN_PORT);
      const changed = port.value !== next;
      port.value = next;
      return changed;
    }
    return false;
  }

  /* The act performed on a card that is not a record yet.
   *
   * Nothing can be written, so what is remembered is the EXACT payload this
   * act would have written -- never the form state the card happens to hold
   * when the record finally exists. A value typed afterwards is a different
   * intent and is not folded into this one. Performing the act again simply
   * replaces it: the latest act is the operator's intent.
   */
  const pendingSsl = new WeakMap();

  function deferSsl(card, control) {
    const moved = followSslPort(card);
    const node = card.querySelector('[data-usenet-field="port"]');
    const sent = {ssl: !!control.checked};
    if (moved && node) {
      sent.port = committedValue('port', node.value);
      // The port this act moved is part of the act, and the act is later than
      // anything the port crossed before it.
      window.DPSettingsPersistence.supersede(node, node.value);
    }
    pendingSsl.set(card, sent);
  }

  /* SSL is an ordinary reversible toggle: switching transport IS the intended
   * action, so it commits immediately, on the same discipline as every other
   * immediate control -- the visible state can never report something the
   * server has not accepted. The conventional port it just followed travels
   * with it, because that is one operator action, not two. */
  async function sslChanged(card, control) {
    if (!serverId(card)) { deferSsl(card, control); return; }
    await window.DPSettingsPersistence.settle(card);
    const secure = !!control.checked;
    const node = card.querySelector('[data-usenet-field="port"]');
    const previousPort = node ? node.value : '';
    const sent = {ssl: secure};
    if (followSslPort(card) && node) sent.port = committedValue('port', node.value);
    control.disabled = true;
    try {
      converge(card, await writeServer(card, sent), sent);
    } catch (error) {
      // Nothing optimistic is left lying -- but only where the operator has
      // not moved on: a control they have changed since keeps their value.
      if (control.checked === secure) control.checked = !secure;
      if (node && 'port' in sent && String(node.value) === String(sent.port)) {
        node.value = previousPort;
      }
      toast(t('settings.usenet.update_failed', {name: serverName(card), reason: failure(card, error)}), 'error');
    } finally {
      control.disabled = false;
    }
  }

  /* The display name is derived from Host until the operator overrides it;
   * an override survives later Host edits, and clearing it returns the name
   * to derived behavior. */
  function refreshDerivedName(card) {
    if (card.dataset.usenetNameOverride === '1') return;
    const label = card.querySelector('[data-usenet-display-name]');
    if (!label) return;
    showName(card, String(fieldValue(card, 'host') || '').trim());
  }

  /* The one writer of a card's shown name. A name too long for its room is
   * drawn with an ellipsis, so the whole name is also the label's tooltip and
   * part of the Edit and Enable controls' accessible names. */
  // ``text`` empty: the card has no name yet, said as "New server" in the
  // page's language (and keyed, so a language change says it again).
  function showName(card, text) {
    const label = card.querySelector('[data-usenet-display-name]');
    if (!label) return;
    const shown = text || t(NEW_SERVER);
    if (text) {
      delete label.dataset.i18n;
      delete label.dataset.i18nAttr;
    } else {
      label.dataset.i18n = NEW_SERVER;
      label.dataset.i18nAttr = `data-tooltip:${NEW_SERVER}`;
    }
    label.textContent = shown;
    label.dataset.tooltip = shown;
    const name = {name: text || {$t: NEW_SERVER}};
    sayAttr(card.querySelector('[data-usenet-action="rename"]'), 'aria-label', 'settings.usenet.edit_name_for', name);
    sayAttr(card.querySelector('[data-usenet-field="enabled"]'), 'aria-label', 'settings.enable_named', name);
  }
  // The card's own name ('' while it is "New server").
  const ownName = label => (label?.dataset.i18n === NEW_SERVER ? '' : String(label?.textContent || ''));

  /* Inline field validation. It never reports an operation result. */
  // ``key``: the catalog key of the problem shown, kept on the node so a
  // language change says it again while it stands.
  function validation(card, key) {
    const node = card.querySelector('[data-usenet-validation]');
    if (!node) return;
    if (key) node.dataset.i18n = key;
    else delete node.dataset.i18n;
    node.textContent = key ? t(key) : '';
    node.hidden = !key;
  }

  /* The operator-facing name of one card, for the action results below. */
  /* A refused server write, named by the card's own field when the server
   * says which field it was; otherwise the server's message. */
  function failure(card, error) {
    const detail = Array.isArray(error?.detail) ? error.detail[0] : error?.detail;
    // A refused setting names its key; a refused request body, its location.
    const key = detail?.field || [...(detail?.loc || [])].reverse().find(part => typeof part === 'string');
    const field = key ? card.querySelector(`[data-usenet-field="${CSS.escape(key)}"]`) : null;
    return (field && window.DPSettingsPersistence.refusalMessage(error, field)) || error?.message || error;
  }

  function serverName(card) {
    return String(card.querySelector('[data-usenet-display-name]')?.textContent || '').trim()
      || String(fieldValue(card, 'host') || '').trim()
      || t('settings.usenet.server_fallback');
  }

  /* An action in flight simply cannot be started again. There is no progress
   * message: a second notification surface is exactly what this file no
   * longer owns. */
  function busy(button, running) {
    if (button) button.disabled = !!running;
  }

  const SERVER_SCOPE = 'usenet-server';
  const NUMERIC_FIELDS = new Set(['port', 'connections', 'priority',
                                  'articles_per_request', 'timeout_seconds']);
  // Booleans of this record. The canonical namespace holds real booleans, so a
  // control's '1'/'0' signature is translated once, here, for every one of
  // them -- never per field.
  const BOOLEAN_FIELDS = new Set(['ssl', 'enabled']);
  // A credential is never projected back into the browser, so the ACCEPTED
  // presentation of one is always blank -- which is what stops the canonical
  // persistence owner from ever holding a secret as a baseline.
  const SECRET_FIELDS = new Set(['password']);

  /* The canonical id lives in BOTH vocabularies: this file's, and the
   * persistence owner's record identity. One assignment site keeps them
   * inseparable. */
  function adoptServerId(card, id) {
    card.dataset.usenetServerId = String(id || '');
    card.dataset.commitInstance = String(id || '');
  }

  /* A control's identity is scope + key + INSTANCE, so the moment a card
   * acquires its canonical id EVERY control on it is a different control as
   * far as the canonical persistence owner is concerned -- and one with no
   * accepted baseline, which can therefore never commit again.
   *
   * Each baseline is re-established under the new identity from what the
   * creation ACTUALLY ACCEPTED, never from what the card currently shows: a
   * draft the operator typed while the record was being minted must stay
   * dirty and commit on its own blur, exactly as it would have done had the
   * record existed all along. A credential's accepted presentation is blank,
   * so the browser still retains no secret. */
  function adoptCreatedBaselines(card, server) {
    if (!server) return;
    for (const node of card.querySelectorAll('[data-usenet-field][data-commit][data-commit-key]')) {
      const key = String(node.dataset.usenetField || '');
      if (SECRET_FIELDS.has(key)) window.DPSettingsPersistence.accept(node, '');
      else if (key in server) window.DPSettingsPersistence.accept(node, server[key]);
    }
  }

  /* Proof of what a successful Test of THIS card actually exercised.
   *
   * Opaque, server-minted and page-lifetime only -- see the same carriage in
   * ui-settings-page.js. The card never claims its server is verified; it only
   * hands the proof back with the write that saves what was tested, and the
   * backend accepts it solely for the configuration it actually saved. */
  const tested = new WeakMap();

  function rememberTestedDraft(card, proof) {
    if (!proof) return;
    tested.set(card, [...(tested.get(card) || []).slice(-3), String(proof)]);
  }

  const testedDraftProofs = card => tested.get(card) || [];

  /* Hygiene only, never authority: the server supersedes the proofs it minted
   * for material a Test has just failed, so keeping them would only present
   * tokens it will refuse. */
  const forgetTestedDrafts = card => tested.delete(card);

  /* The ONE neutral publication of an accepted canonical mutation.
   *
   * A server write can change the provider's derived `configured` / `verified`
   * state, which this file does not present. So every accepted response is
   * published once, carrying the identity and public projection the backend
   * stated, and whoever else renders that provider converges from it -- with no
   * per-integration callback, no second settings cache, no page rerender that
   * would destroy a pending draft, and no polling. */
  function publishAccepted(result) {
    const integration = result?.integration;
    const identity = String(result?.integration_id || '');
    if (integration && identity) {
      document.dispatchEvent(new CustomEvent('debridpulse:integration-accepted',
        {detail: {integration_id: identity, integration}}));
    }
    return result;
  }

  /* The ONE per-record request. Every class of control on a card reaches the
   * canonical namespace through this, so a record has exactly one writer and a
   * request carries only what that control changed. */
  function requestServerWrite(card, values) {
    return api('PUT', `/usenet/servers/${encodeURIComponent(serverId(card))}`,
               withTestedDrafts(card, values), 30000)
      .then(publishAccepted);
  }

  /* A request carries only what its control changed. Proof of a successful
   * Test is added ONLY when there is one to present, so an ordinary field
   * commit stays byte-for-byte the request it always was. */
  function withTestedDrafts(card, values) {
    const proofs = testedDraftProofs(card);
    return proofs.length ? {...values, verification: proofs} : values;
  }

  /* An owner-driven write -- the immediate SSL toggle, the gated credential,
   * the renamed display name -- queued on the SAME per-record lane the field
   * commits use, so two writes to one record can never overlap. A field commit
   * is already running ON that lane, so it issues the request directly;
   * queueing it behind itself would deadlock the record. */
  function writeServer(card, values) {
    return window.DPSettingsPersistence.perform(SERVER_SCOPE, serverId(card),
      () => requestServerWrite(card, values));
  }

  function committedValue(key, raw) {
    if (BOOLEAN_FIELDS.has(key)) return raw === true || raw === '1';
    if (!NUMERIC_FIELDS.has(key)) return String(raw ?? '');
    // Read exactly, never truncated ("1e3" is not 1): the persistence owner
    // refused anything that is not a whole number in range before this write.
    return Number(String(raw));
  }

  const recordFrom = (result, id) =>
    (result?.servers || []).find(item => String(item.id) === String(id));

  /* Converge the controls THIS action wrote on what the server accepted.
   *
   * Settling before dispatch orders everything that existed before the
   * request; it says nothing about an edit made while the request was in
   * flight. So two rules apply, and only to the keys in `sent`:
   *
   *   - the BASELINE always becomes the accepted value, so what the server
   *     already holds is never mistaken for a pending change;
   *   - the visible value is replaced only while the control still holds
   *     exactly what this action sent. A newer draft stays, is now dirty
   *     against that baseline, and commits on its own blur.
   *
   * A control this action did not write is never touched at all, so an older
   * response can neither overwrite nor silently swallow a newer edit. */
  function converge(card, result, sent) {
    const server = recordFrom(result, serverId(card));
    if (!server) return;
    for (const [key, dispatched] of Object.entries(sent || {})) {
      const node = card.querySelector(`[data-usenet-field="${key}"]`);
      if (!node) continue;
      if (SECRET_FIELDS.has(key)) {
        // A credential is never projected back into the browser -- but its
        // ACCEPTED PRESENTATION still has to become the baseline, and for a
        // secret that is blank. Skipping the control entirely would leave it
        // with no accepted value at all.
        window.DPSettingsPersistence.accept(node, '');
        continue;
      }
      if (!(key in server)) continue;
      const accepted = server[key];
      const shown = node.type === 'checkbox' ? node.checked : node.value;
      if (String(shown) === String(dispatched)) {
        if (node.type === 'checkbox') node.checked = !!accepted;
        else node.value = String(accepted ?? '');
      }
      window.DPSettingsPersistence.accept(node, accepted);
    }
    convergeCredentialPresence(card, server);
  }

  /* What the card must SHOW about the stored credential: whether there is one
   * to clear at all. Never the credential itself. */
  function convergeCredentialPresence(card, server) {
    if (!server) return;
    card.dataset.usenetPasswordConfigured = server.password_configured ? '1' : '0';
    refreshClearGate(card);
  }

  /* The display name is the rename dialog's own control, so only that action
   * converges it. A derived name follows the CURRENT host, never the host this
   * response happens to carry. */
  function convergeName(card, result) {
    const server = recordFrom(result, serverId(card));
    const label = card.querySelector('[data-usenet-display-name]');
    if (!server || !label) return;
    const override = String(server.display_name || '').trim();
    card.dataset.usenetNameOverride = override ? '1' : '0';
    if (override) showName(card, override);
    else refreshDerivedName(card);
  }

  /* One ordinary field of one record. The canonical persistence owner decides
   * WHEN this runs; this only says what the write is. */
  function registerServerScope() {
    window.DPSettingsPersistence.defineScope(SERVER_SCOPE, {
      commit: async ({key, draft, control, instance}) => {
        const card = control.closest('[data-usenet-server-id]');
        const result = await requestServerWrite(card, {[key]: committedValue(key, draft)});
        const server = recordFrom(result, instance);
        if (SECRET_FIELDS.has(key)) {
          // Only the PRESENCE of a credential is converged -- whether the card
          // has something to clear. The accepted presentation of the value
          // itself is blank, so nothing here can become a baseline holding it.
          convergeCredentialPresence(card, server);
          return '';
        }
        const value = server ? server[key] : undefined;
        if (value === undefined || value === null) return draft;
        return typeof value === 'boolean' ? (value ? '1' : '0') : String(value);
      },
      /* The record does not exist yet. The generic owner only ASKS; creating it
       * and adopting the minted identity are entirely this owner's business,
       * through the same one creation path Add Server has always used. */
      materialize: ({record}) => createServer(record),
    });
  }

  /* A completed creation consumes only the credential it DISPATCHED.
   *
   * The same rule scoped convergence applies to ordinary controls: compare the
   * control against what was actually sent, and reset it only while it still
   * represents that. A credential typed after dispatch is NEWER intent -- it
   * stays on screen, dirty against the blank accepted baseline, and commits on
   * its own blur, so an older response can never erase it. */
  function consumeCarriedCredential(card, dispatched) {
    const field = card.querySelector('[data-usenet-field="password"]');
    if (field && field.value === dispatched) field.value = '';
  }

  /* The whole clear group is hidden while this server has nothing stored to
   * clear. That is STATE, converged from the accepted record -- never markup
   * this owner rewrites. Whether the operator MEANS it is the canonical
   * confirmation's question, asked at the moment they act. */
  function refreshClearGate(card) {
    const configured = card.dataset.usenetPasswordConfigured === '1';
    // The action keeps its slot whatever the card holds; only whether it can
    // act converges. Hiding the group made the password row change width the
    // moment a credential was saved or cleared.
    const clear = card.querySelector('[data-usenet-action="clear-password"]');
    if (clear) clear.disabled = !configured;
  }

  /* The operator-facing subject of a destructive confirmation about one card.
   *
   * The name the operator CHOSE, else the host they typed, else nothing -- a
   * host-derived display name must not masquerade as a chosen one, and a blank
   * local draft has no identity to state. This reads the card's own accepted,
   * visible identity; it never asks the backend a second time. */
  function confirmSubject(card) {
    const chosen = card.dataset.usenetNameOverride === '1'
      ? String(card.querySelector('[data-usenet-display-name]')?.textContent || '').trim() : '';
    return chosen || String(fieldValue(card, 'host') || '').trim();
  }

  /* A brand-new card carries an EMPTY canonical id: the backend mints one when
   * the card is first saved. It never invents an id locally. */
  function blankCard() {
    const wrapper = document.createElement('div');
    // A brand-new card has no canonical id yet, so its Advanced region gets a
    // locally unique one; the backend mints the record id when the card's
    // first ordinary commit boundary asks its scope to materialize it.
    const sequence = (blankCard.sequence = (blankCard.sequence || 0) + 1);
    const advancedId = `dp-usenet-advanced-new-${sequence}`;
    const passwordId = `dp-usenet-password-new-${sequence}`;
    // The same card the page renders (ui-settings-page.js usenetServerCard), its words keyed.
    const newName = {name: {$t: NEW_SERVER}};
    const label = (key, attrs = '') => said('span', key, `class="form-label"${attrs ? ` ${attrs}` : ''}`);
    wrapper.innerHTML = `
      <div class="dp-usenet-server" data-usenet-server-id="" data-commit-instance=""
           data-usenet-password-configured="0"
           data-usenet-name-override="0">
        <div class="dp-usenet-server-head">
          <span class="dp-usenet-server-title">
            <span class="dp-usenet-server-name" data-usenet-display-name ${keyed(NEW_SERVER, {'data-tooltip': NEW_SERVER})}>${esc(t(NEW_SERVER))}</span>
            <button type="button" class="btn btn-ghost btn-sm dp-usenet-name-edit" data-usenet-action="rename"
                    ${keyed('', {'data-tooltip': 'settings.usenet.edit_name', 'aria-label': 'settings.usenet.edit_name_for'}, newName)}>
              <img src="/icons/lucide/pencil.svg" alt="" aria-hidden="true">
            </button>
          </span>
          <div class="dp-usenet-server-controls">
            <button class="btn btn-ghost btn-sm dp-settings-provider-test" type="button" data-usenet-action="test">
              ${said('span', 'settings.test')}
            </button>
            <label class="dp-usenet-enable toggle-row">
              <span class="toggle">
                <input type="checkbox" data-usenet-field="enabled" ${keyed('', {'aria-label': 'settings.enable_named'}, newName)}
                       data-commit="immediate" data-commit-scope="usenet-server" data-commit-key="enabled" checked>
                <span class="ttrack"></span>
              </span>
            </label>
          </div>
        </div>
        <div class="dp-usenet-row dp-usenet-row--host">
          <label class="dp-usenet-field dp-usenet-field--host">
            ${label('settings.usenet.host')}
            <input class="input" type="text" data-usenet-field="host" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="host" value="" autocomplete="off"
                   placeholder="news.example.com">
          </label>
          <div class="dp-usenet-port-ssl">
            <label class="dp-usenet-field dp-usenet-field--port">
              ${label('settings.usenet.port')}
              <input class="input" type="number" min="1" max="65535" data-usenet-field="port" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="port" value="563">
            </label>
            <label class="dp-usenet-field dp-usenet-field--ssl">
              ${label('settings.usenet.ssl')}
              <span class="toggle dp-usenet-ssl">
                <input type="checkbox" data-usenet-field="ssl" data-commit="immediate" checked>
                <span class="ttrack"></span>
              </span>
            </label>
          </div>
        </div>
        <div class="dp-usenet-row">
          <label class="dp-usenet-field dp-usenet-field--wide">
            ${label('settings.usenet.username')}
            <input class="input" type="text" data-usenet-field="username" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="username" value="" autocomplete="off">
          </label>
        </div>
        <div class="dp-usenet-row">
          <div class="dp-usenet-field dp-usenet-field--wide dp-usenet-field--password">
            ${said('label', 'settings.usenet.password', `class="form-label" for="${passwordId}"`)}
            <div class="dp-action-field">
              <input class="input" id="${passwordId}" type="password" data-usenet-field="password" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="password" value=""
                     autocomplete="off" ${keyed('', {placeholder: 'settings.usenet.password'})}>
              <button type="button" class="btn btn-danger btn-sm" data-usenet-action="clear-password"
                      ${keyed('settings.usenet.clear', {'aria-label': 'settings.usenet.clear_password', 'data-tooltip': 'settings.usenet.clear_password'})} disabled>${esc(t('settings.usenet.clear'))}</button>
            </div>
          </div>
        </div>
        <div class="dp-usenet-advanced" data-usenet-advanced>
          <button type="button" class="dp-usenet-advanced-toggle" data-usenet-advanced-toggle
                  aria-controls="${advancedId}" aria-expanded="false"
                  ${keyed('', {'data-tooltip': 'settings.usenet.advanced_show', 'aria-label': 'settings.usenet.advanced_show'})}>
            ${said('span', 'settings.usenet.advanced', 'class="dp-usenet-advanced-label"')}
            <span class="dp-usenet-advanced-chevron" aria-hidden="true">&rsaquo;</span>
          </button>
          <div class="dp-usenet-advanced-body" id="${advancedId}" hidden>
            <div class="dp-usenet-row dp-usenet-row--tuning">
              <label class="dp-usenet-field">
                ${label('settings.usenet.connections')}
                <input class="input" type="number" min="1" max="500" data-usenet-field="connections" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="connections"
                       value="8">
              </label>
              <div class="dp-usenet-field dp-usenet-field--priority">
                <label class="dp-usenet-field-control">
                  ${label('settings.usenet.priority')}
                  <input class="input" type="number" min="0" max="99" data-usenet-field="priority" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="priority"
                         value="0">
                </label>
                ${said('span', 'settings.usenet.priority_hint', 'class="form-hint dp-usenet-priority-hint"')}
              </div>
            </div>
            <div class="dp-usenet-row dp-usenet-row--tuning">
              <label class="dp-usenet-field">
                ${label('settings.usenet.articles_per_request')}
                <input class="input" type="number" min="1" max="20" data-usenet-field="articles_per_request" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="articles_per_request"
                       value="2">
              </label>
              <label class="dp-usenet-field">
                ${label('settings.usenet.timeout_seconds')}
                <input class="input" type="number" min="20" max="240" data-usenet-field="timeout_seconds" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="timeout_seconds"
                       value="60">
              </label>
            </div>
            ${said('p', 'settings.usenet.advanced_hint', 'class="dp-usenet-advanced-hint"')}
          </div>
          <div class="dp-usenet-actions">
            ${said('button', 'settings.usenet.remove', 'type="button" class="btn btn-danger btn-sm dp-usenet-remove" data-usenet-action="remove"')}
          </div>
        </div>
        <p class="dp-usenet-field-validation" role="alert" data-usenet-validation hidden></p>
      </div>`;
    return wrapper.firstElementChild;
  }

  /* Cards always pack left in DOM order and the Add tile stays last, so removing
   * a middle card can never leave a visual hole. */
  function reindex(host) {
    const tile = host.querySelector('[data-usenet-action="add"]');
    if (tile) host.appendChild(tile);
  }

  /* Erasing this server's stored credential.
   *
   * Destructive, so it is an explicit confirmed action and never a commit
   * boundary. It carries ONLY the removal: a replacement the operator typed
   * belongs to its own changed-blur boundary, which the settle below orders
   * before this on the record's own lane, so the stored credential ends up
   * removed either way and this request never saves one.
   *
   * The ONE canonical Settings confirmation is the GATE in front of this
   * owner: declining it performs no mutation at all. */
  async function clearPassword(card, button) {
    if (!serverId(card)) return;
    const subject = confirmSubject(card);
    const values = {name: subject || {$t: 'settings.usenet.this_server'}};
    const title = subject ? 'settings.usenet.clear_password_title' : 'settings.usenet.clear_password_title_unnamed';
    const confirmed = await window.DPSettingsModal.confirm({
      tone: 'danger',
      title: t(title, plain(values)),
      message: t('settings.usenet.clear_password_message', plain(values)),
      confirmLabel: t('settings.usenet.clear_password_accept'),
      keys: {title, titleValues: values, message: 'settings.usenet.clear_password_message', messageValues: values,
        accept: 'settings.usenet.clear_password_accept'},
    });
    if (!confirmed) return;
    await window.DPSettingsPersistence.settle(card);
    const name = serverName(card);
    busy(button, true);
    try {
      const result = await writeServer(card, {clear_password: true});
      // This action wrote no ordinary control, so it converges none -- only
      // what the card must now show about the stored credential.
      converge(card, result, {});
      const native = nativeMessage(result);
      toast(native || t('settings.usenet.password_cleared', {name}), native ? 'warn' : 'success');
    } catch (error) {
      toast(t('settings.usenet.clear_password_failed', {name, reason: error?.message || error}), 'error');
    } finally {
      busy(button, false);
      refreshClearGate(card);
    }
  }

  /* The handoff out of "pending creation" into the canonical record.
   *
   * A card stays interactive while its record is being minted, so the operator
   * can express intent AFTER the creation write is dispatched and BEFORE the
   * id arrives. In that window nothing can be written: there is no record. The
   * moment there is one, everything they did is carried onto it in its own
   * semantic order -- ordinary fields by the canonical persistence owner,
   * because their commit boundary was already crossed; SSL immediately,
   * because performing it IS the act; the display name because its dialog
   * already committed it. Each is carried only where the card still differs
   * from what the creation actually established, so nothing untouched is
   * rewritten. (The credential is the one exception, and it is already
   * correct: a password typed in that window stays pending for its own Save.)
   */
  async function resumeCreation(card, result, dispatchedName) {
    const accepted = recordFrom(result, serverId(card));
    if (!accepted) return;
    // In the order the operator performed them: the immediate act first, then
    // the boundaries their ordinary fields crossed -- a port boundary crossed
    // AFTER the act must win over the port that act carried.
    await resumeSsl(card, accepted);
    window.DPSettingsPersistence.resume(card);
    await resumeName(card, result, accepted, dispatchedName);
  }

  /* The SSL act performed while the record was being minted. It is an
   * immediate control: it cannot wait for a blur the operator has no reason to
   * make. What replays is the payload the act ITSELF carried -- reconstructing
   * it from the card's current values would fold in a later, uncommitted draft
   * and persist something that crossed no boundary. */
  async function resumeSsl(card, accepted) {
    const sent = pendingSsl.get(card);
    pendingSsl.delete(card);
    if (!sent) return;
    // The creation already established exactly this: there is nothing to write.
    if (sent.ssl === !!accepted.ssl
        && (!('port' in sent) || String(sent.port) === String(accepted.port))) return;
    const control = card.querySelector('[data-usenet-field="ssl"]');
    const node = card.querySelector('[data-usenet-field="port"]');
    try {
      converge(card, await writeServer(card, sent), sent);
    } catch (error) {
      if (control && control.checked === sent.ssl) control.checked = !!accepted.ssl;
      if (node && 'port' in sent && String(node.value) === String(sent.port)) {
        node.value = String(accepted.port ?? '');
      }
      toast(t('settings.usenet.update_failed', {name: serverName(card), reason: failure(card, error)}), 'error');
    }
  }

  /* A name chosen while the record was being minted is newer than the one the
   * creation carried, so the creation response must not project over it. */
  async function resumeName(card, result, accepted, dispatchedName) {
    if (!card.querySelector('[data-usenet-display-name]')) return;
    const chosen = overrideName(card);
    // Nothing newer: the creation response is this control's truth.
    if (chosen === String(dispatchedName || '')) { convergeName(card, result); return; }
    try {
      convergeName(card, await writeServer(card, {display_name: chosen}));
    } catch (error) {
      convergeName(card, {servers: [accepted]});
      toast(t('settings.usenet.rename_failed', {name: serverName(card), reason: failure(card, error)}), 'error');
    }
  }

  /* Record CREATION -- the one place a whole card is written at once, because
   * until the backend mints an id there is no record for a field to belong to.
   * From the moment it returns, the card is an ordinary member of the
   * universal persistence model. */
  async function createServer(card) {
    const server = readCard(card);
    if (!server.host) { validation(card, 'settings.usenet.host_required'); return false; }
    validation(card, '');
    const name = serverName(card);
    // Published BEFORE the request is awaited, so a removal raised in this
    // window can wait for the id instead of acting without one.
    const minted = (async () => {
      const result = publishAccepted(await api(
        'POST', '/usenet/servers', withTestedDrafts(card, server), 30000));
      // The record now exists, so the card's controls acquire their canonical
      // identity BEFORE their accepted baselines are recorded under it.
      if (result?.server_id) adoptServerId(card, result.server_id);
      return result;
    })();
    creations.set(card, minted.catch(() => null));
    try {
      const result = await minted;
      // The card now has a canonical identity, so every control acquires its
      // accepted baseline UNDER it before anything converges or replays.
      adoptCreatedBaselines(card, recordFrom(result, serverId(card)));
      // A credential typed while the record was being minted is newer intent:
      // it stays on the card, dirty against the blank accepted baseline, and
      // commits on its own blur.
      consumeCarriedCredential(card, server.password || '');
      converge(card, result, server);
      // The operator asked for this card to go while it was being minted.
      // Removal owns the outcome from here; carrying intent onto a record that
      // is about to be deleted would be noise, and claiming it was saved would
      // be untrue.
      if (card.dataset.usenetRemoving === '1') return false;
      await resumeCreation(card, result, server.display_name);
      const native = nativeMessage(result);
      toast(native || t('settings.usenet.saved', {name}), native ? 'warn' : 'success');
      // Created -- what a surface that creates it with its own Save waits for.
      return true;
    } catch (error) {
      toast(t('settings.usenet.save_failed', {name, reason: failure(card, error)}), 'error');
      return false;
    } finally {
      creations.delete(card);
      refreshClearGate(card);
    }
  }

  /* A save is only truthful if the service actually accepted the configuration. */
  function nativeMessage(result) {
    if (result?.native && result.native.applied === false) {
      return t('settings.usenet.native_refused', {reason: result.native.detail || t('settings.usenet.unknown_reason')});
    }
    return '';
  }

  /* Removal is serialized behind creation. A card removed while its record is
   * being minted cannot simply vanish: the creation write is already on its
   * way, so removal waits for the id and deletes the record it mints. A
   * backend record with no card is never an acceptable outcome.
   *
   * The ONE canonical Settings confirmation gates that owner and does not
   * replace any part of it: declining removes nothing, locally or durably, and
   * accepting runs exactly the sequence below. A card that is still a local
   * draft has no record to delete, so confirming it removes only the draft --
   * the DELETE below is reached only when an id actually exists. */
  async function removeCard(host, card, button) {
    const subject = confirmSubject(card);
    const values = {name: subject || {$t: 'settings.usenet.this_server'}};
    const title = subject ? 'settings.usenet.remove_title' : 'settings.usenet.remove_title_unnamed';
    const confirmed = await window.DPSettingsModal.confirm({
      tone: 'danger',
      title: t(title, plain(values)),
      message: t('settings.usenet.remove_message', plain(values)),
      confirmLabel: t('settings.usenet.remove_accept'),
      keys: {title, titleValues: values, message: 'settings.usenet.remove_message', messageValues: values,
        accept: 'settings.usenet.remove_accept'},
    });
    if (!confirmed) return;
    const name = serverName(card);
    busy(button, true);
    card.dataset.usenetRemoving = '1';
    const minting = creations.get(card);
    if (minting) await minting;
    const id = serverId(card);
    try {
      if (id) publishAccepted(await api('DELETE', `/usenet/servers/${encodeURIComponent(id)}`, null, 30000));
      card.remove();
      reindex(host);
      toast(t('settings.usenet.removed', {name}), 'success');
    } catch (error) {
      delete card.dataset.usenetRemoving;
      busy(button, false);
      toast(t('settings.usenet.remove_failed', {name, reason: error?.message || error}), 'error');
    }
  }

  /* An explicit action on the card's CURRENT draft, including a password the
   * operator has typed but not yet saved. It commits nothing. */
  async function test(host, card, button) {
    await window.DPSettingsPersistence.settle(card);
    const server = readCard(card);
    if (!server.host) { validation(card, 'settings.usenet.host_required'); return; }
    validation(card, '');
    busy(button, true);
    try {
      const result = await api('POST', '/usenet/servers/test', {
        host: server.host, port: server.port, ssl: server.ssl,
        username: server.username, password: server.password || '',
        connections: server.connections, server_id: serverId(card) || null,
      }, 60000);
      if (result?.ok) rememberTestedDraft(card, result.verification);
      else forgetTestedDrafts(card);
      // A Test of exactly the SAVED server configuration settles this
      // provider's durable verification, in either direction; whatever the
      // backend accepted is published through the same one seam.
      publishAccepted(result);
      toast(result?.message || t(result?.ok ? 'settings.usenet.test_ok' : 'settings.usenet.test_failed'),
            result?.ok ? 'success' : 'error');
    } catch (error) {
      forgetTestedDrafts(card);
      toast(t('settings.usenet.test_failed_reason', {reason: error?.message || error}), 'error');
    } finally {
      busy(button, false);
    }
  }

  /* The rename dialog is the application's, never the browser's: a native
   * prompt bypasses the visual, focus and accessibility contract the rest of
   * Settings honours. Accepting the dialog IS this field's commit boundary:
   * the display name is an ordinary, non-secret value, so it persists as soon
   * as the operator chooses it -- unless the card is not yet a record, in which
   * case it travels with the creation write. */
  async function rename(card) {
    const label = card.querySelector('[data-usenet-display-name]');
    if (!label) return;
    const derived = String(fieldValue(card, 'host') || '').trim();
    // Prefill the explicit override ONLY: a host-derived name must never
    // masquerade as one the operator chose.
    const current = card.dataset.usenetNameOverride === '1' ? label.textContent.trim() : '';
    const next = await window.DPSettingsModal.prompt({
      title: t('settings.usenet.rename_title'),
      label: t('settings.usenet.rename_label'),
      value: current,
      hint: t('settings.usenet.rename_hint'),
      acceptLabel: t('common.save'),
      placeholder: derived,
      keys: {title: 'settings.usenet.rename_title', label: 'settings.usenet.rename_label', hint: 'settings.usenet.rename_hint', accept: 'common.save'},
    });
    if (next === null) return;
    const trimmed = next.trim();
    const previous = {override: card.dataset.usenetNameOverride, text: ownName(label)};
    card.dataset.usenetNameOverride = trimmed ? '1' : '0';
    showName(card, trimmed || derived);
    if (!serverId(card)) return;
    await window.DPSettingsPersistence.settle(card);
    try {
      const result = await writeServer(card, {display_name: trimmed});
      // Only this action's own control is converged.
      converge(card, result, {});
      convergeName(card, result);
    } catch (error) {
      card.dataset.usenetNameOverride = previous.override;
      showName(card, previous.text);
      toast(t('settings.usenet.rename_failed', {name: serverName(card), reason: failure(card, error)}), 'error');
    }
  }

  /* The per-server Advanced region keeps the normal card compact. It is a
   * local disclosure inside one server card, not a Settings card header, so it
   * carries its own compact control rather than the canonical card chip. */
  function toggleAdvanced(button) {
    const expanded = button.getAttribute('aria-expanded') === 'true';
    const body = document.getElementById(button.getAttribute('aria-controls'));
    // The operator's own toggle: the panel rolls down or up (ui-motion.js).
    if (body && window.DPMotion) window.DPMotion.disclose(body, !expanded, {animate: true, focusReturn: button});
    else if (body) body.hidden = expanded;
    button.setAttribute('aria-expanded', expanded ? 'false' : 'true');
    const label = expanded ? 'settings.usenet.advanced_show' : 'settings.usenet.advanced_hide';
    sayAttr(button, 'data-tooltip', label, {});
    sayAttr(button, 'aria-label', label, {});
  }

  function onClick(event) {
    const host = collectionOf(event);
    if (!host) return;
    const advanced = event.target.closest('[data-usenet-advanced-toggle]');
    if (advanced) {
      event.preventDefault();
      toggleAdvanced(advanced);
      return;
    }
    const action = event.target.closest('[data-usenet-action]');
    if (!action) return;
    const kind = action.dataset.usenetAction;
    const card = action.closest('[data-usenet-server-id]');
    if (kind === 'add') {
      event.preventDefault();
      const created = host.insertBefore(blankCard(), action);
      window.DPSettingsPersistence.adopt(created);
      refreshClearGate(created);
      reindex(host);
      host.querySelector('[data-usenet-server-id]:last-of-type [data-usenet-field="host"]')?.focus();
      return;
    }
    if (!card) return;
    event.preventDefault();
    if (kind === 'remove') {
      void removeCard(host, card, action);
    } else if (kind === 'clear-password') {
      void clearPassword(card, action);
    } else if (kind === 'test') {
      void test(host, card, action);
    } else if (kind === 'rename') {
      void rename(card);
    }
  }

  function onInput(event) {
    const host = collectionOf(event);
    if (!host) return;
    const card = event.target.closest('[data-usenet-server-id]');
    if (!card) return;
    if (event.target.dataset?.usenetField === 'host') refreshDerivedName(card);
  }

  /* SSL is this owner's own `change` control: switching it may also carry the
   * conventional port, which is why it is not an ordinary scoped commit. Every
   * other immediate control of a card -- Enable -- is an ordinary one and is
   * committed by the canonical persistence owner, not here. */
  function onChange(event) {
    const host = collectionOf(event);
    if (!host) return;
    const card = event.target.closest('[data-usenet-server-id]');
    if (!card) return;
    if (event.target.dataset?.usenetField === 'ssl') void sslChanged(card, event.target);
  }

  /* Every render re-establishes the canonical baseline for this collection's
   * record-scoped controls and the state of each card's gate. */
  function bind() {
    const host = collection();
    if (!host) return;
    window.DPSettingsPersistence.adopt(host);
    for (const card of cards(host)) refreshClearGate(card);
    if (host.dataset.dpUsenetOwner === '1') return;
    host.dataset.dpUsenetOwner = '1';
    reindex(host);
  }

  registerServerScope();
  document.addEventListener('click', onClick);
  document.addEventListener('input', onInput);
  document.addEventListener('change', onChange);
  document.addEventListener('debridpulse:settings-rendered', bind);
  document.addEventListener('DOMContentLoaded', bind, {once: true});
  bind();

  window.DPUsenetServers = Object.freeze({readCard, refreshDerivedName, followSslPort});
})();
