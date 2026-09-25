/* The ONE canonical Settings field-persistence owner.
 *
 * A Settings control's commit boundary is decided by the semantics and risk of
 * that setting, never by the arbitrary fact that several controls share a page.
 * DebridPulse recognises three classes:
 *
 *   immediate        the mutation IS the intended action -- a participation
 *                    toggle, or an ordinary reversible boolean whose only
 *                    draft state is the state it already shows. A
 *                    participation toggle is committed by its own operational
 *                    owner; an ordinary one declares a scope and key and is
 *                    owned HERE, through exactly the same baseline, scope
 *                    dispatch, serialization, stale-response and rollback
 *                    machinery as changed-blur. Only the BOUNDARY differs.
 *   changed-blur     ordinary, non-destructive values -- INCLUDING entering or
 *                    replacing a credential, which is an ordinary value change
 *                    however sensitive the value is; owned HERE. What makes a
 *                    value a secret is what its SCOPE does with the accepted
 *                    result, never a different commit boundary.
 *   explicit-action  Test / Browse / probes, and every DESTRUCTIVE act -- a
 *                    confirmed Clear. They may READ the current draft; a probe
 *                    commits nothing, and a destructive act carries only its
 *                    own removal.
 *
 * This module owns everything generic that a changed-blur control needs -- the
 * canonical accepted baseline, dirty comparison, scoped mutation dispatch,
 * per-record serialization, stale-response protection, success convergence and
 * failure rollback -- so no Settings page implements any of it a second time.
 * A page DECLARES a control's class in its markup (`data-commit`) and declares
 * how a scope writes (`defineScope`); it never reimplements the machinery.
 *
 * Controls may be per-RECORD: a page may render one card per stored record, so
 * the same logical field exists once per record. A control's identity is
 * therefore scope + key + INSTANCE, taken from the nearest enclosing
 * `[data-commit-instance]`, and writes serialize per record so editing one
 * never queues behind another. A control inside a record that has no canonical
 * id yet cannot be field-committed at all: there is nothing to write to. Only
 * that record's own scope knows how one comes into existence, so this owner
 * merely ASKS it to -- once per record, through the optional `materialize`
 * hook -- and owns no part of the creation itself.
 *
 * This module names no page, no integration and no endpoint.
 */
(function () {
  'use strict';

  // scope id -> {commit(key, draft, control) -> Promise<accepted value>}
  const scopes = new Map();
  // control identity -> the canonical accepted value it was rendered from
  const baselines = new Map();
  // control identity -> monotonic edit token; a write or a response carrying an
  // older token has been superseded and is discarded
  const tokens = new Map();
  // control identity -> the draft its latest queued write carries, so flushing
  // never re-issues a write that is already on its way
  const dispatched = new Map();
  // scope id + record instance -> tail of that record's serialized write chain
  const chains = new Map();
  // control -> the exact draft that crossed its commit boundary while its
  // record had no canonical identity yet; replayed by `resume` once it has one
  const deferred = new WeakMap();
  // record element -> the creation its own scope is performing for it, so a
  // second boundary crossed while that runs can never create a second record
  const materializing = new WeakMap();
  // every creation still running, so `settle` waits for one exactly as it
  // waits for a scoped write
  const materializations = new Set();
  let outstanding = 0;

  /* Every control this owner persists, whatever its boundary. A control that
   * declares a commit CLASS but no key is not this owner's at all (a
   * participation toggle, whose own operational owner commits it), so the key
   * is what opts a control in. */
  const COMMITTED = '[data-commit][data-commit-key]';
  const CHANGED_BLUR = '[data-commit="changed-blur"][data-commit-key]';
  const IMMEDIATE = '[data-commit="immediate"][data-commit-key]';

  function signature(control) {
    return control.type === 'checkbox' || control.type === 'radio'
      ? (control.checked ? '1' : '0')
      : String(control.value ?? '');
  }

  function present(control, value) {
    if (control.type === 'checkbox' || control.type === 'radio') {
      control.checked = value === true || value === '1';
    } else {
      control.value = String(value ?? '');
    }
  }

  function controls(host) {
    return host && typeof host.querySelectorAll === 'function'
      ? Array.from(host.querySelectorAll(COMMITTED))
      : [];
  }

  // The record a control belongs to, if it belongs to one at all.
  function record(control) {
    return control && typeof control.closest === 'function'
      ? control.closest('[data-commit-instance]')
      : null;
  }

  function instanceOf(control) {
    const owner = record(control);
    return owner ? String(owner.dataset.commitInstance || '') : '';
  }

  /* A page-level control always has something to write to. A control that
   * belongs to a RECORD does not until that record exists. */
  function committable(control) {
    const owner = record(control);
    return !owner || !!String(owner.dataset.commitInstance || '');
  }

  function identity(control) {
    const scope = String(control.dataset.commitScope || '');
    const key = String(control.dataset.commitKey || '');
    return `${scope}|${key}|${instanceOf(control)}`;
  }

  /* A page declares how ONE canonical namespace is written. The handler
   * receives the control's key and current draft, issues exactly that scoped
   * mutation, adopts what the server accepted and returns it. */
  function defineScope(id, handler) {
    scopes.set(String(id), handler);
  }

  /* Ask a scope to give an uncommittable record its canonical identity.
   *
   * A record's controls cannot be written until the record exists, and only
   * its own scope knows how one is created -- so this ASKS, and owns nothing
   * else: not the request, not the identity, not the adoption. A scope that
   * declares no hook behaves exactly as it did, and nothing here names a page,
   * an integration or a kind of record.
   *
   * Exactly ONE creation per draft record: a second boundary crossed while the
   * first is still running must never mint a second record. It is counted as
   * outstanding work, so `settle` waits for it like any scoped write.
   *
   * The boundary that asked is remembered exactly as before, and replayed by
   * `resume` once identity exists. A creation is made from the record's
   * CURRENT values, so it ordinarily carries that same draft -- and where the
   * scope records what the creation accepted as the new baseline, the replay
   * then writes nothing. Where the accepted value cannot describe the draft --
   * a credential, whose accepted presentation is always blank -- the replay
   * writes the same value a second time. That is idempotent and ordered on the
   * record's own lane, and it is the ordinary deferred-draft semantics rather
   * than a second writer.
   */
  function materialize(control) {
    const owner = record(control);
    const scope = scopes.get(String(control.dataset.commitScope || ''));
    if (!owner || !scope || typeof scope.materialize !== 'function') return;
    if (materializing.has(owner)) return;
    outstanding += 1;
    const run = Promise.resolve()
      .then(() => scope.materialize({record: owner, control}))
      .catch(() => {})
      .finally(() => {
        outstanding -= 1;
        materializing.delete(owner);
        materializations.delete(run);
      });
    materializing.set(owner, run);
    materializations.add(run);
  }

  /* The canonical accepted baseline is whatever the page just rendered FROM
   * canonical state; call this after every render. Without it a re-rendered
   * control would look dirty and a later blur would write a value nobody
   * changed. */
  function adopt(host) {
    for (const control of controls(host)) baselines.set(identity(control), signature(control));
  }

  function baseline(control) {
    return baselines.get(typeof control === 'string' ? control : identity(control));
  }

  function dirty(control) {
    if (!control || !control.dataset || !control.dataset.commitKey) return false;
    const key = identity(control);
    return baselines.has(key) && signature(control) !== baselines.get(key);
  }

  function report(error) {
    const message = String((error && error.message) || error || 'That setting could not be saved.');
    // Failure is reported by the canonical toast owner; there is no second
    // notification system. Ordinary success is deliberately silent.
    if (typeof window.toast === 'function') window.toast(message, 'error');
  }

  /* Persist ONE control through its declared scope.
   *
   * Unchanged means no write at all -- merely focusing and leaving a control
   * mutates nothing. A write is serialized behind its scope's previous write
   * and abandoned if a newer edit superseded it before it was sent.
   *
   * Supersession suppresses stale PRESENTATION, never canonical knowledge. A
   * response that arrives after a newer edit still tells us what the server
   * now holds, so its value always becomes the accepted baseline; only the
   * repainting of the control is skipped. Otherwise a later write's rollback
   * would land on a value the server abandoned two writes ago.
   *
   * Failure therefore rolls back to the baseline AS IT STANDS AT THAT MOMENT,
   * not to a snapshot taken when the write was queued: earlier writes in the
   * same lane may have been accepted in between. No false optimistic value,
   * and no false pessimistic one either.
   */
  function commit(control, replayed) {
    if (!control || !control.dataset || !control.dataset.commitKey) return null;
    const key = identity(control);
    // A commit boundary belongs to a DRAFT. Ordinarily that is what the
    // control shows; a replay carries the draft that crossed the boundary
    // earlier, which is not necessarily what it shows now.
    const draft = replayed === undefined ? signature(control) : String(replayed);
    // Not a write at all: this draft is what the server already holds.
    if (!baselines.has(key) || draft === baselines.get(key)) return null;
    // The record this control belongs to does not exist yet. The operator has
    // crossed the commit boundary all the same, so THAT DRAFT is remembered
    // and replayed the moment the record acquires its canonical identity --
    // never silently dropped, and never requiring a second blur. A draft typed
    // afterwards crossed no boundary of its own and is never promoted with it.
    if (!committable(control)) {
      deferred.set(control, draft);
      // The record can be brought into existence only by its own scope.
      materialize(control);
      return null;
    }
    const scopeId = String(control.dataset.commitScope || '');
    const scope = scopes.get(scopeId);
    if (!scope) return null;

    const instance = instanceOf(control);
    const field = String(control.dataset.commitKey);
    // This exact draft is already on its way: a blur and the flush an explicit
    // action performs are the SAME commit, never two.
    if (dispatched.get(key) === draft) return null;
    // Only a fallback, for the impossible case of a control with no baseline;
    // the rollback below always prefers what the server has accepted SINCE.
    const fallback = baselines.get(key);
    const token = (tokens.get(key) || 0) + 1;
    tokens.set(key, token);
    dispatched.set(key, draft);
    outstanding += 1;

    // One chain per RECORD: two servers are independent, one server's writes
    // are not.
    const lane = `${scopeId}|${instance}`;
    const run = (chains.get(lane) || Promise.resolve()).then(async () => {
      // A newer edit superseded this one before it was sent: that edit carries
      // the operator's intent, so this write is abandoned rather than raced.
      if (tokens.get(key) !== token) return;
      try {
        const result = await scope.commit({key: field, draft, control, instance});
        const value = result === undefined || result === null ? draft : result;
        // Canonical knowledge, recorded unconditionally: the server holds this
        // now, whatever the operator has typed since.
        baselines.set(key, String(value));
        // Presentation only while this is still the newest write AND the
        // control still shows what it sent.
        if (tokens.get(key) === token && control.isConnected && signature(control) === draft) {
          present(control, value);
        }
      } catch (error) {
        // Superseded: the newer write owns the outcome, including what the
        // control ends up showing.
        if (tokens.get(key) !== token) return;
        // Nothing was accepted, so no canonical state moves here.
        //
        // Roll the VISIBLE control back only while it still shows exactly what
        // this write sent. A draft typed while the request was in flight has
        // not reached a commit boundary yet, so no token protects it -- its
        // value is the only evidence that newer intent exists, and an older
        // failure must not paint over it. It simply stays dirty against the
        // unchanged baseline and commits on its own blur.
        //
        // The rollback target is the baseline AS IT STANDS NOW -- which may be
        // an earlier write in this very lane that succeeded while this one was
        // queued -- never a stale queue-time snapshot.
        if (control.isConnected && signature(control) === draft) {
          present(control, baselines.has(key) ? baselines.get(key) : fallback);
        }
        report(error);
      }
    }).finally(() => {
      outstanding -= 1;
      if (dispatched.get(key) === draft) dispatched.delete(key);
    });

    chains.set(lane, run.catch(() => {}));
    return run;
  }

  /* Run an owner's own write on a record's lane.
   *
   * A record has controls that are not changed-blur -- an immediate toggle, a
   * gated credential, a value committed by a dialog -- and they mutate the
   * SAME record. Sharing the per-record chain that field commits already use
   * is what makes "the last write wins" mean the operator's last action rather
   * than whichever request happened to return last. The action is counted as
   * outstanding, so `settle` waits for it exactly like a field commit.
   */
  function perform(scopeId, instance, run) {
    const lane = `${String(scopeId || '')}|${String(instance ?? '')}`;
    outstanding += 1;
    const chain = (chains.get(lane) || Promise.resolve())
      .then(run)
      .finally(() => { outstanding -= 1; });
    chains.set(lane, chain.catch(() => {}));
    return chain;
  }

  /* Record what the server accepted for one control as its new baseline.
   *
   * The control's VALUE stays the owner's business. An owner that converged it
   * leaves it clean; an owner that deliberately kept a newer draft leaves that
   * draft DIRTY against this baseline, so it commits on the next blur instead
   * of being silently swallowed by an older response.
   */
  function accept(control, value) {
    if (!control || !control.dataset || !control.dataset.commitKey) return;
    const next = control.type === 'checkbox' || control.type === 'radio'
      ? ((value === true || value === '1') ? '1' : '0')
      : String(value ?? '');
    baselines.set(identity(control), next);
  }

  /* The handoff out of "this record does not exist yet".
   *
   * A record's own owner calls this the moment the record acquires its
   * canonical identity and its accepted baselines have been recorded under it.
   * Every commit boundary the operator crossed while the record was being
   * created is then replayed on the record's own lane -- and only where the
   * control still differs from what the creation actually established, so
   * nothing the operator did not change is ever written.
   */
  function resume(host) {
    for (const control of controls(host)) {
      if (!deferred.has(control)) continue;
      const draft = deferred.get(control);
      deferred.delete(control);
      // The draft that crossed the boundary -- not whatever the control shows
      // now. A newer draft is preserved by exactly the ordinary convergence
      // rules: it is not repainted over, and it stays dirty until its own blur.
      commit(control, draft);
    }
  }

  /* One operator action may necessarily move ANOTHER control's value -- an
   * immediate toggle that carries a conventional companion value. That moved
   * value is part of the same act, so it SUPERSEDES whatever boundary that
   * control crossed before it: the act, being later, is the operator's
   * current intent. This records the superseding draft as the control's
   * pending boundary; it changes nothing about how a boundary crossed by the
   * operator themselves is remembered or replayed.
   */
  function supersede(control, draft) {
    if (!control || !control.dataset || !control.dataset.commitKey) return;
    if (committable(control)) return;
    deferred.set(control, control.type === 'checkbox' || control.type === 'radio'
      ? ((draft === true || draft === '1') ? '1' : '0')
      : String(draft ?? ''));
  }

  /* The ONE deterministic path an explicit action takes before it reads form
   * state. Clicking Test or Save inherently removes focus from whatever was
   * being edited: this flushes every control that is still dirty -- whether or
   * not its blur has already fired -- and then waits for every outstanding
   * scoped write, so an action can neither read a stale value nor be overtaken
   * by a commit that started underneath it. */
  async function settle(host) {
    for (const control of controls(host)) commit(control);
    for (let guard = 0; outstanding > 0 && guard < 50; guard += 1) {
      // A record still being created is outstanding work too: an action must
      // not read form state, or dispatch, underneath one.
      await Promise.allSettled([...chains.values(), ...materializations]);
    }
  }

  const matches = (control, selector) =>
    !!control && typeof control.matches === 'function' && control.matches(selector);

  // The single commit boundary for every changed-blur control in the
  // application. No other module listens for it.
  document.addEventListener('focusout', event => {
    if (matches(event.target, CHANGED_BLUR)) commit(event.target);
  });

  /* The other boundary this owner recognises, for exactly two kinds of
   * control:
   *
   *   an ordinary IMMEDIATE control -- a reversible boolean whose mutation is
   *   the intended action, so the change IS the boundary;
   *
   *   a changed-blur SELECT -- a control with no intermediate draft at all.
   *   Choosing an option is the whole edit, and the application projects
   *   selects into a listbox whose native element is never focused, so a blur
   *   the operator can perform does not exist for one.
   *
   * Both run the SAME commit(): same baseline, same per-record lane, same
   * stale-response rule, same rollback. Nothing else about them is special. */
  document.addEventListener('change', event => {
    const control = event.target;
    if (matches(control, IMMEDIATE)
        || (matches(control, CHANGED_BLUR) && control.tagName === 'SELECT')) {
      commit(control);
    }
  });

  window.DPSettingsPersistence = Object.freeze({
    defineScope, adopt, baseline, dirty, commit, settle, perform, accept, resume, supersede,
    pending: () => outstanding,
  });
})();
