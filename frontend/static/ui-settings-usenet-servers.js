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
      port: Number(fieldValue(card, 'port') || defaultPort(card)) || defaultPort(card),
      ssl: !!fieldValue(card, 'ssl'),
      username: String(fieldValue(card, 'username') || ''),
      // A server with no connections cannot acquire; DebridPulse's floor is 1
      // and Enable is the control for switching a server off.
      connections: Math.min(500, Math.max(1, Number(fieldValue(card, 'connections')) || 8)),
      priority: Number(fieldValue(card, 'priority') || 0),
      // Per-server acquisition tuning. The bounds mirror the canonical model,
      // which is itself never wider than what the download service accepts, so
      // a saved value can never be silently rewritten underneath the operator.
      articles_per_request: Math.min(20, Math.max(1, Number(fieldValue(card, 'articles_per_request')) || 2)),
      timeout_seconds: Math.min(240, Math.max(20, Number(fieldValue(card, 'timeout_seconds')) || 60)),
      enabled: true,
      display_name: overrideName(card),
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
      toast(`Could not update ${serverName(card)}: ${error?.message || error}`, 'error');
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
    label.textContent = String(fieldValue(card, 'host') || '').trim() || 'New server';
  }

  /* Inline field validation. It never reports an operation result. */
  function validation(card, message) {
    const node = card.querySelector('[data-usenet-validation]');
    if (!node) return;
    node.textContent = message || '';
    node.hidden = !message;
  }

  /* The operator-facing name of one card, for the action results below. */
  function serverName(card) {
    return String(card.querySelector('[data-usenet-display-name]')?.textContent || '').trim()
      || String(fieldValue(card, 'host') || '').trim()
      || 'server';
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
    for (const node of card.querySelectorAll('[data-usenet-field][data-commit="changed-blur"]')) {
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
    if (key === 'ssl') return raw === true || raw === '1';
    if (!NUMERIC_FIELDS.has(key)) return String(raw ?? '');
    const parsed = parseInt(String(raw), 10);
    return Number.isNaN(parsed) ? 0 : parsed;
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
    if (override) label.textContent = override;
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
    const group = card.querySelector('.dp-usenet-clear-password');
    if (group) group.hidden = !configured;
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
    const advancedId = `dp-usenet-advanced-new-${(blankCard.sequence = (blankCard.sequence || 0) + 1)}`;
    wrapper.innerHTML = `
      <div class="dp-usenet-server" data-usenet-server-id="" data-commit-instance=""
           data-usenet-password-configured="0"
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
            <input class="input" type="text" data-usenet-field="host" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="host" value="" autocomplete="off"
                   placeholder="news.example.com">
          </label>
          <label class="dp-usenet-field dp-usenet-field--port">
            <span class="form-label">Port</span>
            <input class="input" type="number" min="1" max="65535" data-usenet-field="port" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="port" value="563">
          </label>
          <label class="dp-usenet-ssl toggle-row">
            <span class="tl">SSL</span>
            <span class="toggle">
              <input type="checkbox" data-usenet-field="ssl" data-commit="immediate" checked>
              <span class="ttrack"></span>
            </span>
          </label>
        </div>
        <div class="dp-usenet-row">
          <label class="dp-usenet-field dp-usenet-field--wide">
            <span class="form-label">Username</span>
            <input class="input" type="text" data-usenet-field="username" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="username" value="" autocomplete="off">
          </label>
        </div>
        <div class="dp-usenet-row">
          <label class="dp-usenet-field dp-usenet-field--wide">
            <span class="form-label">Password</span>
            <input class="input" type="password" data-usenet-field="password" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="password" value=""
                   autocomplete="off" placeholder="Password">
          </label>
        </div>
        <div class="dp-usenet-clear-password" hidden>
          <button type="button" class="btn btn-danger btn-sm" data-usenet-action="clear-password"
                  aria-label="Clear the stored password for this server">Clear Stored Password</button>
        </div>
        <div class="dp-usenet-advanced" data-usenet-advanced>
          <button type="button" class="dp-usenet-advanced-toggle" data-usenet-advanced-toggle
                  aria-controls="${advancedId}" aria-expanded="false"
                  title="Show advanced acquisition settings"
                  aria-label="Show advanced acquisition settings">
            <span class="dp-usenet-advanced-label">Advanced</span>
            <span class="dp-usenet-advanced-chevron" aria-hidden="true">&rsaquo;</span>
          </button>
          <div class="dp-usenet-advanced-body" id="${advancedId}" hidden>
            <div class="dp-usenet-row dp-usenet-row--tuning">
              <label class="dp-usenet-field">
                <span class="form-label">Connections</span>
                <input class="input" type="number" min="1" max="500" data-usenet-field="connections" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="connections"
                       value="8">
              </label>
              <div class="dp-usenet-field dp-usenet-field--priority">
                <label class="dp-usenet-field-control">
                  <span class="form-label">Priority</span>
                  <input class="input" type="number" min="0" max="99" data-usenet-field="priority" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="priority"
                         value="0">
                </label>
                <span class="form-hint dp-usenet-priority-hint">Lower values have priority.</span>
              </div>
            </div>
            <div class="dp-usenet-row dp-usenet-row--tuning">
              <label class="dp-usenet-field">
                <span class="form-label">Articles per Request</span>
                <input class="input" type="number" min="1" max="20" data-usenet-field="articles_per_request" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="articles_per_request"
                       value="2">
              </label>
              <label class="dp-usenet-field">
                <span class="form-label">Server Timeout (seconds)</span>
                <input class="input" type="number" min="20" max="240" data-usenet-field="timeout_seconds" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="timeout_seconds"
                       value="60">
              </label>
            </div>
            <p class="dp-usenet-advanced-hint">Articles per Request asks this server for several articles without waiting for each reply; Server Timeout is how long to wait for it to answer.</p>
          </div>
          <div class="dp-usenet-actions">
            <button type="button" class="btn btn-ghost btn-sm" data-usenet-action="test">Test</button>
            <button type="button" class="btn btn-ghost btn-sm dp-usenet-remove" data-usenet-action="remove">Remove</button>
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
    const confirmed = await window.DPSettingsModal.confirm({
      tone: 'danger',
      title: subject ? `Clear password for ${subject}?` : 'Clear password for this Usenet server?',
      message: `The stored password for ${subject || 'this Usenet server'} will be removed.`,
      confirmLabel: 'Clear Password',
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
      toast(native || `${name} password cleared`, native ? 'warn' : 'success');
    } catch (error) {
      toast(`Could not clear the password for ${name}: ${error?.message || error}`, 'error');
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
      toast(`Could not update ${serverName(card)}: ${error?.message || error}`, 'error');
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
      toast(`Could not rename ${serverName(card)}: ${error?.message || error}`, 'error');
    }
  }

  /* Record CREATION -- the one place a whole card is written at once, because
   * until the backend mints an id there is no record for a field to belong to.
   * From the moment it returns, the card is an ordinary member of the
   * universal persistence model. */
  async function createServer(card) {
    const server = readCard(card);
    if (!server.host) { validation(card, 'A server host is required.'); return; }
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
      if (card.dataset.usenetRemoving === '1') return;
      await resumeCreation(card, result, server.display_name);
      const native = nativeMessage(result);
      toast(native || `${name} saved`, native ? 'warn' : 'success');
    } catch (error) {
      toast(`Could not save ${name}: ${error?.message || error}`, 'error');
    } finally {
      creations.delete(card);
      refreshClearGate(card);
    }
  }

  /* A save is only truthful if the service actually accepted the configuration. */
  function nativeMessage(result) {
    if (result?.native && result.native.applied === false) {
      return `Saved, but the download service did not accept it: ${result.native.detail || 'unknown reason'}`;
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
    const confirmed = await window.DPSettingsModal.confirm({
      tone: 'danger',
      title: subject ? `Remove ${subject}?` : 'Remove this Usenet server?',
      message: `The configuration for ${subject || 'this Usenet server'} will be removed from DebridPulse.`,
      confirmLabel: 'Remove Server',
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
      toast(`${name} removed`, 'success');
    } catch (error) {
      delete card.dataset.usenetRemoving;
      busy(button, false);
      toast(`Could not remove ${name}: ${error?.message || error}`, 'error');
    }
  }

  /* An explicit action on the card's CURRENT draft, including a password the
   * operator has typed but not yet saved. It commits nothing. */
  async function test(host, card, button) {
    await window.DPSettingsPersistence.settle(card);
    const server = readCard(card);
    if (!server.host) { validation(card, 'A server host is required.'); return; }
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
      toast(result?.message || (result?.ok ? 'Connection successful' : 'Test failed'),
            result?.ok ? 'success' : 'error');
    } catch (error) {
      forgetTestedDrafts(card);
      toast(`Test failed: ${error?.message || error}`, 'error');
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
      title: 'Edit Server Name',
      label: 'Display Name',
      value: current,
      hint: 'Leave blank to use the server host.',
      acceptLabel: 'Save',
      placeholder: derived,
    });
    if (next === null) return;
    const trimmed = next.trim();
    const previous = {override: card.dataset.usenetNameOverride, text: label.textContent};
    card.dataset.usenetNameOverride = trimmed ? '1' : '0';
    label.textContent = trimmed || derived || 'New server';
    if (!serverId(card)) return;
    await window.DPSettingsPersistence.settle(card);
    try {
      const result = await writeServer(card, {display_name: trimmed});
      // Only this action's own control is converged.
      converge(card, result, {});
      convergeName(card, result);
    } catch (error) {
      card.dataset.usenetNameOverride = previous.override;
      label.textContent = previous.text;
      toast(`Could not rename ${serverName(card)}: ${error?.message || error}`, 'error');
    }
  }

  /* The per-server Advanced region keeps the normal card compact. It is a
   * local disclosure inside one server card, not a Settings card header, so it
   * carries its own compact control rather than the canonical card chip. */
  function toggleAdvanced(button) {
    const expanded = button.getAttribute('aria-expanded') === 'true';
    const body = document.getElementById(button.getAttribute('aria-controls'));
    if (body) body.hidden = expanded;
    button.setAttribute('aria-expanded', expanded ? 'false' : 'true');
    const label = `${expanded ? 'Show' : 'Hide'} advanced acquisition settings`;
    button.title = label;
    button.setAttribute('aria-label', label);
  }

  function onClick(event) {
    const host = collection();
    if (!host || !host.contains(event.target)) return;
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
    const host = collection();
    if (!host || !host.contains(event.target)) return;
    const card = event.target.closest('[data-usenet-server-id]');
    if (!card) return;
    if (event.target.dataset?.usenetField === 'host') refreshDerivedName(card);
  }

  /* SSL and the Clear confirmation are the card's two `change` controls: one
   * commits immediately, the other only arms the destructive action. */
  function onChange(event) {
    const host = collection();
    if (!host || !host.contains(event.target)) return;
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
