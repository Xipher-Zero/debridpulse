# DebridPulse 1.0.12 — Generalized Recovery, Candidate Activation, Admission, and Presentation Leveling

## Mission

Perform a comprehensive architectural remediation of DebridPulse `1.0.12` to unify the generalized recovery, candidate-activation, execution-admission, parent-lifecycle, presentation, and recovery-persistence architecture.

This is **not** a collection of localized bug fixes.

The current implementation has accumulated individually reasonable corrective layers, but the combined system now has mismatched ownership boundaries:

- lifecycle aggregation and presentation aggregate different child sets;
- queued autonomous work can incorrectly lose to exhausted sibling attention;
- automatic failover and manual candidate switching do not use the same recovery authority;
- candidate selection position doubles as recovery traversal history;
- failover can surrender an already-admitted execution slot and starve behind unrelated work;
- Phase-3 recovery state mixes current actionable state with historical audit facts;
- current recovery state is reconstructed from an unbounded append-only `application_events` history;
- execution progress can append full recovery snapshots at approximately scheduler cadence;
- parent transfer aggregation is derived from multiple independently-read snapshots;
- operator commands and scheduler recovery do not share one complete per-transfer mutation/fencing model;
- a successful manual candidate switch can currently commit and then return an API failure if post-commit aggregation fails;
- important focused tests instantiate a simplified repository/engine stack rather than the production recovery stack and therefore fail to exercise several of these interactions.

The required result is a **leveled architecture with one coherent ownership model**, suitable for completing the intended `1.0.12` form before moving on to `1.0.13`.

Do not preserve an existing abstraction merely because tests currently depend on it. Preserve externally correct behavior and durable user data; replace internal structure where necessary.

---

# 0. Verified Current Implementation Map

The following paths and symbols have been verified on the current `1.0.12` branch and are the starting implementation surfaces for this work.

**Important:** this is an ownership/reference map, not an instruction to mechanically edit every listed file. If the leveling moves ownership into a new canonical module, update or delete superseded implementations rather than leaving parallel owners.

At the start of the session, re-run repository-wide symbol searches and report any path or symbol that has moved since this prompt was generated.

## 0.1 Production composition / application command surface

### `backend/application/composition.py`
**Role:** production composition root.

Inspect and verify the concrete production classes instantiated for:

- transfer engine;
- recovery repository;
- integration registry;
- application service.

This is the authority for deciding whether a focused test actually uses the production stack.

### `backend/application/service.py`
**Relevant symbols / areas:**

- `ApplicationService`
- `pause()`
- `resume()`
- `pause_all()`
- `resume_all()`
- `retry()`
- `cancel()`
- `delete()`
- `select_artifact()`
- `resolve_pending()`
- `reconcile_executions()`
- `_storage_checked_admission()`
- `application_operation()`
- `_publish()`
- `execution_wakeup`
- `resolution_wakeup`
- dispatch/storage admission updates

**Role:** application-level command admission, scheduler wakeups, lifecycle publishing, storage dispatch gating.

### `backend/application/manual_candidate_failover.py`
**Role:** application command wrapper for manual candidate failover.

Verify the exact current function name and flow. It currently delegates the mutation to the transfer-layer manual failover implementation, then wakes execution/publishes application state.

### `backend/api/operational_downloads.py`
**Known relevant API surface:**

- artifact candidate-selection POST route(s), including current aliases under `/transfers/.../candidate` and `/torrents/.../candidate`;
- candidate/source grouping and operational Downloads/Dashboard projections;
- group/common-source operations.

**Role:** HTTP command surface and bounded operational list/read model.

Do not invent a second candidate-switch API while leveling internals.

---

## 0.2 Engine ownership

### `backend/transfers/convergence_engine.py`
**Role:** production convergence-engine surface.

Inspect its inheritance/MRO and verify which implementation owns production recovery/reconciliation behavior.

### `backend/transfers/engine.py`
**Role:** lower/base public engine surface.

Important because existing focused manual-failover tests instantiate this class directly instead of the production convergence stack.

### `backend/transfers/_engine_base.py`
**Relevant symbols / areas:**

- `reconcile_executions()`
- `_process_executions()`
- `_current_artifact()`
- `_converge_execution()`
- `_dispatch()`
- `_aggregate()`
- execution capacity counting / `live_executions()` consumption
- transfer/execution locks
- native Pause/Resume convergence

**Role:** ordinary queued dispatch, execution observation/convergence, active-capacity admission, base parent aggregation.

### `backend/transfers/_engine_recovery.py`
**Relevant symbols / areas:**

- `_wake_quiescent_recoveries()`
- `reconcile_executions()` recovery override
- `_aggregate()` recovery override
- `_next_alternate_index()`
- `_activate_alternate()`
- `_terminal_recovery()`
- `_quiesce()`
- `_recovery_context()`
- sidecar/partial-state handling around candidate failover

**Role:** universal recovery policy application, alternate traversal/activation, recovery wait/wake semantics, recovery-specific parent aggregation behavior.

---

## 0.3 Persistence / recovery repository stack

### `backend/transfers/_repository_base.py`
**Relevant symbols / areas:**

- `TransferRepository.artifacts()`
- parent `state()` persistence
- execution-attempt persistence and retrieval
- `live_executions()`
- pause intent
- transfer/request persistence

**Current canonical actionable-artifact filter:** `artifacts()` filters at least:

```text
request_id IS NOT NULL
blocked = false
mirror_state != standby
```

Verify exact current SQL before changing it.

### `backend/transfers/repository.py`
**Relevant symbols / areas:**

- `_recovery_event_kind()`
- `_recovery_snapshot()`
- `_save_recovery_snapshot()`
- `recovery_context()`
- `record_recovery_decision()`
- `record_source_failure()`
- `consume_recovery_refresh()`
- `reset_source_recovery()`
- `reset_retry_budget()` base implementation
- `execution()`
- `transition_recovery()`

**Role:** original recovery accounting, append-only recovery snapshot storage, progress-driven recovery epoch handling, execution observation persistence.

### `backend/transfers/presentation_repository.py`
**Relevant symbols / areas:**

- `_AUTONOMOUS_PRESENTATION`
- `_WAIT_PRESENTATION`
- `recovery_presentation()`
- `ARTIFACT_PRESENTATION_SNAPSHOT_KEYS`
- `_aggregate_presentation()`
- `effective_presentation()`
- `TransferRepository.presentation()`

**Role:** transfer/artifact presentation projection and source identity presentation.

**Known mismatch:** `presentation()` currently loads all `download_files` rows for a transfer for status voting, while canonical lifecycle `artifacts()` excludes blocked/standby/noncanonical rows.

### `backend/transfers/manual_repository.py`
**Relevant symbols / areas:**

- manual candidate-failover provenance
- candidate/source presentation extensions
- `_SWITCHABLE_STATES`

**Role:** manual-failover-specific persistence/projection layer currently inserted into the repository inheritance chain.

### `backend/transfers/_recovery_repository_phase3.py`
**Relevant symbols / areas:**

- `_PHASE3_DEFAULTS`
- `_recovery_snapshot()` override
- Phase-3 current state additions such as recovery generation, claim identity, candidate generation, blocked retry state.

### `backend/transfers/_recovery_repository_audit.py`
**Relevant symbols / areas:**

- `_AUDIT_DEFAULTS`
- `_recovery_snapshot()` override
- audit/last-* state fields such as decision recovery epoch, failure classification, last candidate-switch reason, last execution-retirement reason.

### `backend/transfers/_recovery_repository_claim_base.py`
**Relevant symbols / areas:**

- `claim_recovery()`
- claim token/generation/expiry creation
- recovery decision/claim identity

### `backend/transfers/recovery_repository.py`
**Relevant symbols / areas:**

- final production `TransferRepository`
- final `reset_retry_budget()`
- `finish_recovery_claim()`
- final Phase-3 reset/claim completion semantics

**Role:** final production recovery repository surface.

### `backend/transfers/recovery_execution.py`
**Relevant symbols / areas:**

- `RecoveryTrigger`
- `TriggerAuthority`
- `_AUTHORITY`
- `RecoveryClaim`

**Known current triggers:**

```text
AUTO_RETRY
USER_RETRY
RESUME
STARTUP_RECONCILE
PROVIDER_RECOVERY
EXECUTOR_RECOVERY
```

There is currently no first-class manual/operator candidate-activation trigger.

---

## 0.4 Candidate ownership / failover

### `backend/transfers/manual_failover.py`
**Relevant behavior:**

- validates requested candidate;
- retires old writer;
- calls `transition_recovery()`;
- changes selected candidate;
- clears some recovery state;
- records manual failover provenance;
- current branch calls `engine._aggregate()` after durable mutation;
- current branch can return `TransferError` if that post-commit aggregation fails.

**Role:** current operator-requested candidate activation implementation.

### `backend/transfers/canonical.py`
**Role:** canonical candidate ownership/binding/origin behavior.

Inspect symbols around `CanonicalOwnership`, candidate bindings, candidate origins, and source identity before designing attempted-candidate persistence.

### `backend/transfers/cohorts.py`
**Role:** candidate/canonical artifact coordination and consolidation/cohort behavior.

Inspect any candidate-order and candidate-union assumptions before separating selected candidate from attempt history.

---

## 0.5 Database / scheduling / concurrency

### `backend/db/database.py`
**Relevant schema:**

- `application_events`
- `execution_attempts`
- `execution_attempt_provenance`
- `canonical_candidate_bindings`
- `canonical_candidate_origins`
- `artifact_consolidations`
- `transfer_pause_intents`
- runtime schema migration/bootstrap helpers
- index creation

**Role:** SQLite schema and additive migration owner.

### `backend/core/scheduler.py`
**Relevant symbols / areas:**

- `sync_download_clients_loop()`
- `_wait_for_work()`
- execution reconciliation cadence
- `events_ttl_loop()`

**Known behavior:** execution reconciliation is normally driven on approximately a two-second cadence; TTL cleanup explicitly targets ordinary `events`, not the recovery `application_events` snapshots.

### `backend/services/maintenance_gate.py`
**Relevant symbols / areas:**

- `ApplicationMaintenanceGate`
- `operation()`
- `maintenance()`

**Important:** this is a maintenance admission/drain gate, **not** a normal-operation mutex. Do not mistake it for transfer-command serialization.

### `backend/services/db_maintenance.py`
**Role:** inspect and update if recovery/audit retention or cleanup behavior changes.

Do not assume existing ordinary event cleanup automatically covers new/old recovery audit storage.

---

## 0.6 Frontend

### `frontend/static/ui-group-candidates.js`
**Relevant symbols / areas:**

- artifact candidate chooser
- group/common-source chooser
- `renderProgress()`
- `progressMarkup()`
- switching busy state / candidate POST handling

Preserve the current branch correction that artifact-mode switching uses the shared visible switching-progress owner.

### `frontend/browser/group-candidates.spec.js`
**Role:** focused browser regression suite for candidate switching and group switching.

### `frontend/browser/app.spec.js`
**Role:** general browser/runtime behavior and transfer presentation/status interactions where applicable.

If another existing browser test file is a better exact owner for a newly added status, use it, but document the choice.

---

## 0.7 Existing focused backend tests known to be relevant

### `backend/tests/test_manual_candidate_failover.py`
**Current problem:** imports the lower stack:

```text
transfers.engine.TransferEngine
transfers.manual_repository.TransferRepository
```

and currently builds with high capacity (`max_active_executions=8`).

This cannot be the primary qualification authority for production Phase-3 recovery/capacity semantics.

### `backend/tests/test_transfer_recovery_phase4.py`
**Role:** existing recovery/presentation regression coverage. Preserve and extend appropriate invariants rather than deleting them.

### `backend/tests/test_ws2p1_failover_progress.py`
**Role:** existing failover/progress fixtures referenced by manual candidate tests.

Inspect before replacing shared fixture behavior.

### New focused test files

Create **only if there is not already a better existing test owner**. Suggested paths:

```text
backend/tests/test_recovery_leveling.py
backend/tests/test_candidate_activation.py
backend/tests/test_execution_admission_continuity.py
backend/tests/test_operational_artifact_membership.py
backend/tests/test_recovery_state_store.py
backend/tests/test_recovery_command_concurrency.py
backend/tests/test_transfer_aggregation_snapshot.py
```

These paths are **CREATE IF ABSENT**, not claims that they currently exist.

---

# 1. Continuous Execution / DO NOT STOP

This is a continuous implementation assignment.

**DO NOT STOP** after inspection, root-cause confirmation, design, migrations, an individual phase, a RED test, a GREEN test, a failed test, a CI failure, a discovered regression, a branch comparison, or a partial implementation.

Continue automatically through:

1. repository verification;
2. architecture inventory;
3. targeted reproductions;
4. design correction;
5. implementation;
6. migrations;
7. focused regression tests;
8. concurrency/adversarial tests;
9. browser/runtime tests;
10. full Python qualification;
11. static/security checks;
12. final diff audit;
13. final evidence report.

If an ordinary command, test, build, or qualification step fails:

- diagnose it;
- fix it when in scope;
- rerun it;
- automatically retry transient failures;
- continue.

Do not ask for permission at intermediate checkpoints.

Only stop when **external human input is genuinely required** or a hard session/context/platform boundary makes further execution impossible.

Do not voluntarily split the work merely because a phase completed.

---

# 1.1 Forced Session-Boundary Handoff

If, and only if, a hard context/session/platform boundary makes continued execution impossible, produce a precise handoff containing:

- current branch;
- exact current HEAD SHA and tree;
- original starting SHA and tree;
- files modified;
- migrations added;
- completed architectural phases;
- incomplete phases;
- exact tests already run and results;
- exact current failures;
- next command/action required;
- whether worktree is clean or dirty;
- whether any commit exists;
- whether anything was pushed;
- whether `main` changed.

Mark it:

```text
HANDOFF_REASON=FORCED_SESSION_BOUNDARY
VOLUNTARY_HANDOFF_USED=NO
```

A phase boundary, large diff, failed test, or desire for review is **not** a valid reason to stop.

---

# 2. Repository / GitHub HOWTO

Repository:

```text
Xipher-Zero/debridpulse
```

Target branch:

```text
1.0.12
```

At prompt generation time the verified branch head was:

```text
3876fdd2930becbe9a27d41296da8bebf3d5ea5c
```

with parent:

```text
297c454b9c221fa58feb66c273030faca4e36077
```

and tree:

```text
f07cb7129c57a34c2442997d320fed6c4b4f37bd
```

Do **not** blindly assume this is still current.

Begin by fetching the live remote branch and recording:

```text
START_BRANCH=
START_SHA=
START_TREE=
START_PARENT=
MAIN_SHA=
```

Verify:

- the checked-out branch is `1.0.12`;
- local HEAD matches the live intended starting point;
- no unexpected upstream advancement is being overwritten;
- `main` remains unchanged;
- the working tree contains no unrelated edits.

If there are pre-existing unrelated local modifications, preserve them and do not absorb them into this work.

Use ordinary Git discipline:

```bash
git status --short
git branch --show-current
git fetch origin
git rev-parse HEAD
git rev-parse HEAD^{tree}
git rev-parse origin/1.0.12
git rev-parse origin/main
git log -n 5 --oneline --decorate
```

Then verify every path in Section 0 with repository search before editing. Useful commands include:

```bash
git grep -n "def _aggregate"
git grep -n "def transition_recovery"
git grep -n "def reset_retry_budget"
git grep -n "def claim_recovery"
git grep -n "def finish_recovery_claim"
git grep -n "def _activate_alternate"
git grep -n "def _next_alternate_index"
git grep -n "manual_candidate_failover"
git grep -n "application_events"
git grep -n "selected_candidate"
git grep -n "live_executions"
git grep -n "_AUTONOMOUS_PRESENTATION"
```

If a symbol/path differs from Section 0, report the exact live replacement and continue with the live owner. Do not silently edit a guessed path.

Do not rewrite published history.

Do not merge `main`.

Do not promote or release anything.

Do not create a tag.

Do not push `main`.

Do not modify staging deployments.

If Docker qualification is required locally, remember that the production-style Docker environment requires `sudo`.

---

# 3. Commit / Push Policy

This task is an **implementation-qualified pre-commit leveling pass**.

Make all required changes and qualification runs, but stop before creating or pushing a commit.

Final state must report:

```text
READY_FOR_USER_REVIEW=YES
COMMIT_CREATED=NO
PUSH_PERFORMED=NO
MAIN_CHANGED=NO
```

Do not create the final implementation commit unless explicitly instructed after review.

---

# 4. Architectural Objective

The finished architecture must satisfy this invariant:

> **A transfer has one canonical lifecycle/recovery truth. Candidate activation, whether automatic or operator-requested, is one core-owned operation over that truth. Execution admission, recovery generation, candidate-attempt history, parent aggregation, and presentation all consume the same canonical artifact membership and ownership facts. Historical audit data never masquerades as current actionable state.**

The implementation should reduce layering rather than add another compatibility layer.

Prefer deleting or collapsing obsolete transitional machinery where safe.

A successful remediation should make the system easier to explain after the change than before it.

---

# 5. Mandatory Initial Investigation

## Exact implementation surface

Inspect all of these before making architectural edits:

```text
backend/application/composition.py
backend/application/service.py
backend/application/manual_candidate_failover.py
backend/api/operational_downloads.py
backend/transfers/convergence_engine.py
backend/transfers/engine.py
backend/transfers/_engine_base.py
backend/transfers/_engine_recovery.py
backend/transfers/_repository_base.py
backend/transfers/repository.py
backend/transfers/presentation_repository.py
backend/transfers/manual_repository.py
backend/transfers/_recovery_repository_phase3.py
backend/transfers/_recovery_repository_audit.py
backend/transfers/_recovery_repository_claim_base.py
backend/transfers/recovery_repository.py
backend/transfers/recovery_execution.py
backend/transfers/manual_failover.py
backend/transfers/canonical.py
backend/transfers/cohorts.py
backend/db/database.py
backend/core/scheduler.py
backend/services/maintenance_gate.py
backend/services/db_maintenance.py
frontend/static/ui-group-candidates.js
backend/tests/test_manual_candidate_failover.py
backend/tests/test_transfer_recovery_phase4.py
backend/tests/test_ws2p1_failover_progress.py
frontend/browser/group-candidates.spec.js
```

Trace and document the complete production ownership chain.

Determine the actual runtime class MRO and composition.

Inventory every writer of:

```text
torrents.status
download_files.status
download_files.selected_candidate
download_files.execution_attempt_id
recovery generation
recovery claim token
quiescence state
candidate-switch counters/history
retry/recovery budgets
application_events transfer_recovery:* rows
```

Inventory every caller of:

```text
_aggregate()
transition_recovery()
reset_retry_budget()
claim_recovery()
finish_recovery_claim()
artifact_state()
repository.state()
manual_candidate_failover()
_activate_alternate()
_dispatch()
```

The report must distinguish:

```text
CANONICAL OWNER
COMPATIBILITY OWNER
HISTORICAL/LEGACY OWNER
READ MODEL ONLY
```

Do not proceed under an assumption that a method name represents the final production implementation; verify the MRO.

---

# 6. Reproduce / Lock Down the Existing Defects First

## Exact test surfaces

Primary existing files:

```text
backend/tests/test_manual_candidate_failover.py
backend/tests/test_transfer_recovery_phase4.py
backend/tests/test_ws2p1_failover_progress.py
frontend/browser/group-candidates.spec.js
```

Create additional focused files from Section 0.7 only where there is no suitable existing owner.

For every material issue, capture a real RED condition against the starting implementation wherever technically possible.

The final report must include the test name and the actual failing assertion/result.

A bare statement that “the test would have failed” is insufficient.

If a defect cannot be reproduced as RED because the starting branch already contains a partial correction, explain exactly why and construct the closest architecture assertion capable of protecting the remaining invariant.

---

# 7. Canonical Artifact Membership

## Problem

Lifecycle aggregation currently operates on canonical actionable artifacts while presentation can include every `download_files` row.

That permits blocked, standby, duplicate, historical, or otherwise non-actionable rows to vote in transfer-level operational presentation even when the lifecycle owner excludes them.

## Exact implementation surface

**Primary owners to change:**

```text
backend/transfers/_repository_base.py
  TransferRepository.artifacts()

backend/transfers/presentation_repository.py
  TransferRepository.presentation()
  _aggregate_presentation()
  effective_presentation()

backend/api/operational_downloads.py
  operational Downloads/Dashboard child/status projection
```

**Related ownership consumers to inspect:**

```text
backend/transfers/_engine_base.py
  _aggregate()

backend/transfers/_engine_recovery.py
  _aggregate()

backend/transfers/canonical.py
backend/transfers/cohorts.py
```

**Tests:**

```text
backend/tests/test_transfer_recovery_phase4.py
backend/tests/test_manual_candidate_failover.py
backend/tests/test_operational_artifact_membership.py   # CREATE IF ABSENT
frontend/browser/app.spec.js                            # if browser surface coverage belongs here
```

## Required correction

Create **one canonical definition** of an artifact that participates in current operational transfer truth.

The current lifecycle semantics strongly indicate this includes at least:

```text
request_id IS NOT NULL
blocked = false
mirror_state != standby
```

Do not blindly copy SQL into multiple modules.

Create one reusable repository/query/predicate owner.

It may live in `_repository_base.py` if that is the cleanest canonical persistence owner, or in a new narrowly-scoped transfer module if needed. If a new module is created, document why and ensure both lifecycle and presentation import/use that same owner.

Use it consistently for:

- parent lifecycle aggregation;
- transfer-level effective presentation;
- recovery eligibility;
- active execution/admission calculations where appropriate;
- current candidate-group operational status.

Historical/inactive artifacts may remain visible in Details and provenance views, but they must be explicitly marked/projected as historical/inactive and must **not** vote in current aggregate lifecycle/presentation truth.

Add regressions for:

- blocked failed child + healthy actionable child;
- standby failed child + healthy actionable child;
- historical/consolidated contributor with stale recovery attention;
- transfer containing only completed actionable children plus historical rows;
- Details visibility without aggregate influence.

The implementation must make it impossible for lifecycle and presentation to silently drift onto different child sets again.

---

# 8. Presentation Truth and Queued Autonomous Work

## Exact implementation surface

**Primary:**

```text
backend/transfers/presentation_repository.py
  _AUTONOMOUS_PRESENTATION
  recovery_presentation()
  _aggregate_presentation()
  effective_presentation()
  TransferRepository.presentation()
```

**Bounded/list projection:**

```text
backend/api/operational_downloads.py
```

**Tests:**

```text
backend/tests/test_transfer_recovery_phase4.py
backend/tests/test_operational_artifact_membership.py   # CREATE IF ABSENT
frontend/browser/app.spec.js
```

## Problem

`_aggregate_presentation()` treats operator attention as aggregate truth when no child has a presentation state in `_AUTONOMOUS_PRESENTATION`.

Current autonomous states omit ordinary `queued`.

A queued artifact that is completely capable of continuing automatically can therefore lose to an exhausted sibling and make the transfer appear red / `Requires attention`.

## Required correction

Define autonomous useful work semantically, not accidentally by a short hand-maintained list.

At minimum, a normal executable `queued` artifact is autonomous.

A transfer must **not** present aggregate `Requires attention` while any canonical actionable child is capable of future progress without operator intervention.

Examples of autonomous work can include:

```text
queued
downloading
recovering
waiting_for_retry
waiting_for_provider
waiting_for_storage
waiting_for_executor
```

provided the wait is itself scheduler-recoverable rather than operator-blocked.

Keep `input_required` and genuinely exhausted `wait_for_operator` semantics distinct.

Add explicit tests:

```text
queued sibling + requires_attention sibling
downloading sibling + requires_attention sibling
waiting_for_retry sibling + requires_attention sibling
all siblings requires_attention
blocked requires_attention sibling + queued actionable child
```

---

# 9. Distinguish Queued From Capacity Waiting

## Exact implementation surface

**Capacity truth / dispatch:**

```text
backend/transfers/_engine_base.py
  _process_executions()
  _dispatch()
  reconcile_executions()

backend/transfers/_repository_base.py
  live_executions()
  artifacts()

backend/application/service.py
  reconcile_executions()
  storage/dispatch gates

backend/core/scheduler.py
  sync_download_clients_loop()
```

**Presentation:**

```text
backend/transfers/presentation_repository.py
backend/api/operational_downloads.py
```

**Frontend/browser:**

```text
frontend/browser/app.spec.js
frontend/browser/group-candidates.spec.js   # if candidate switch capacity state is exercised here
```

**Tests:**

```text
backend/tests/test_execution_admission_continuity.py   # CREATE IF ABSENT
backend/tests/test_recovery_leveling.py                # CREATE IF ABSENT
```

## Required correction

Introduce authoritative reportability for **execution-capacity waiting**.

Do not create a second lifecycle engine merely for UI.

Prefer a derived presentation/reason fact such as:

```text
waiting_for_slot
Waiting for execution slot
```

or an equivalent neutral name.

It must be derived from canonical facts:

- artifact is operationally queued;
- retry timer has expired;
- provider/candidate is usable;
- eligible executor exists;
- transfer is not paused;
- storage permits dispatch;
- no operator/input gate exists;
- dispatch is blocked by execution capacity.

Do not label provider-disabled, executor-unavailable, storage-unavailable, retry-backoff, or paused work as capacity waiting.

Expose the distinction consistently on Downloads, Recent Activity/Dashboard, and Details.

Do not invent optimistic `Downloading` while no writer exists.

---

# 10. One Canonical Candidate-Activation Operation

## Exact implementation surface

**Current manual path:**

```text
backend/transfers/manual_failover.py
backend/application/manual_candidate_failover.py
backend/api/operational_downloads.py
backend/transfers/manual_repository.py
```

**Current automatic path:**

```text
backend/transfers/_engine_recovery.py
  _next_alternate_index()
  _activate_alternate()
```

**Production recovery/convergence:**

```text
backend/transfers/convergence_engine.py
backend/transfers/recovery_execution.py
backend/transfers/_recovery_repository_phase3.py
backend/transfers/_recovery_repository_audit.py
backend/transfers/_recovery_repository_claim_base.py
backend/transfers/recovery_repository.py
```

**Candidate ownership/provenance:**

```text
backend/transfers/canonical.py
backend/transfers/cohorts.py
backend/transfers/_repository_base.py
backend/transfers/repository.py
backend/db/database.py
```

**Tests:**

```text
backend/tests/test_manual_candidate_failover.py
backend/tests/test_candidate_activation.py             # CREATE IF ABSENT
backend/tests/test_recovery_command_concurrency.py     # CREATE IF ABSENT
frontend/browser/group-candidates.spec.js
```

## Required architecture

Create **one canonical candidate-activation operation** in the core.

Both automatic alternate activation and operator-requested candidate switch must call it.

The shared operation owns:

1. transfer/artifact validation;
2. current candidate identity;
3. requested replacement eligibility;
4. recovery-generation/fence validation;
5. old execution writer retirement;
6. partial/resumable-state policy;
7. candidate selection mutation;
8. bounded recovery reset for the new attempt;
9. attempted-candidate history;
10. admission continuity/reservation;
11. durable candidate activation provenance;
12. child lifecycle state;
13. parent lifecycle reconciliation;
14. scheduler wakeup requirements;
15. externally truthful command result.

Automatic and operator paths may supply different **authority/policy reasons**, but may not duplicate state-transition implementations.

If a new transfer module is created for this owner, use a neutral name such as `candidate_activation.py`; update both `manual_failover.py` and `_engine_recovery.py` to delegate to it and remove superseded mutation code.

Do not create a new module and leave the old two implementations intact.

---

# 11. Integrate Candidate Activation With Phase-3 Recovery Ownership

## Exact implementation surface

```text
backend/transfers/recovery_execution.py
backend/transfers/_recovery_repository_phase3.py
backend/transfers/_recovery_repository_audit.py
backend/transfers/_recovery_repository_claim_base.py
backend/transfers/recovery_repository.py
backend/transfers/convergence_engine.py
backend/transfers/_engine_recovery.py
backend/transfers/manual_failover.py
backend/application/manual_candidate_failover.py
```

**Tests:**

```text
backend/tests/test_candidate_activation.py             # CREATE IF ABSENT
backend/tests/test_recovery_command_concurrency.py     # CREATE IF ABSENT
backend/tests/test_transfer_recovery_phase4.py
```

A candidate activation must participate in the same recovery ownership/fencing system as retry/resume/recovery.

Do not leave manual source switching as an out-of-band mutation.

The architecture must answer explicitly:

```text
What recovery generation owns this activation?
What claim/fence prevents overlapping mutations?
What happens to an existing recovery claim?
What authority may an operator action override?
What state is intentionally reset?
What audit history remains?
What constitutes a stale activation request?
```

Operator authority is not permission to corrupt an in-flight productive recovery.

Likewise, an automatic recovery worker must not overwrite a newer operator-selected candidate.

Use generation/fence/CAS semantics rather than timing assumptions.

---

# 12. Separate Selected Candidate From Attempt History

## Exact implementation surface

**Current traversal defect:**

```text
backend/transfers/_engine_recovery.py
  _next_alternate_index()
  _activate_alternate()
```

**Candidate identity/order/provenance:**

```text
backend/transfers/canonical.py
backend/transfers/cohorts.py
backend/transfers/_repository_base.py
backend/transfers/repository.py
```

**Persistence/schema:**

```text
backend/db/database.py
  canonical_candidate_bindings
  canonical_candidate_origins
  execution_attempt_provenance
  recovery-state schema added by this leveling
```

**Phase-3 state:**

```text
backend/transfers/_recovery_repository_phase3.py
backend/transfers/recovery_repository.py
```

**Tests:**

```text
backend/tests/test_candidate_activation.py             # CREATE IF ABSENT
backend/tests/test_recovery_state_store.py             # CREATE IF ABSENT
```

## Required model

Persist separate concepts:

```text
selected_candidate_id
candidate_attempt_history for the current recovery generation
```

Do not infer attempt history from candidate ordering.

The recovery algorithm must be able to:

- select any eligible operator-requested candidate;
- later fail over to an eligible lower-index candidate that has not been attempted in the current generation;
- avoid immediate cycling back to candidates already attempted in that generation;
- incorporate newly refreshed/discovered candidates;
- reset/advance the attempt set at a clearly defined new recovery epoch/generation boundary;
- remain deterministic.

Candidate order may still influence preferred traversal order.

It must not define historical exhaustion.

Required regression:

```text
operator jumps from candidate 0 to candidate 7;
candidate 7 fails;
candidate 1 is still eligible and unattempted;
automatic recovery must find candidate 1.
```

---

# 13. Execution Admission Continuity Across Failover

## Exact implementation surface

**Dispatch/capacity:**

```text
backend/transfers/_engine_base.py
  _dispatch()
  _process_executions()
  reconcile_executions()
  _converge_execution()
```

**Automatic failover:**

```text
backend/transfers/_engine_recovery.py
  _activate_alternate()
```

**Manual failover:**

```text
backend/transfers/manual_failover.py
```

**Persistence for any durable/reconstructible reservation:**

```text
backend/transfers/_repository_base.py
backend/transfers/repository.py
backend/transfers/recovery_repository.py
backend/db/database.py
```

**Application/scheduler gates:**

```text
backend/application/service.py
backend/core/scheduler.py
```

**Tests:**

```text
backend/tests/test_execution_admission_continuity.py   # CREATE IF ABSENT
backend/tests/test_candidate_activation.py             # CREATE IF ABSENT
```

## Required correction

Implement a bounded **continuation admission** mechanism for already-admitted failover.

If an artifact already owned one active execution slot immediately before candidate activation, replacement dispatch should retain that slot entitlement across the short writer-replacement handoff.

Requirements:

- never exceed `max_active_executions`;
- no double-counting the retired writer and replacement;
- unrelated queued work must not steal the reserved continuation between retirement and replacement dispatch;
- artifacts that were not already admitted must not gain priority merely because the operator changed candidate;
- reservations must be artifact/transfer/generation fenced;
- cancellation, deletion, pause, terminal failure, permanent quiescence, or unusable replacement must release the reservation appropriately;
- a crashed/restarted process must not leave capacity permanently consumed;
- stale reservations must be detectable/recoverable;
- replacement dispatch failure must release or correctly retain the reservation according to retry semantics;
- global Pause and storage gates remain authoritative.

Do not implement this as a UI-only priority hack.

Required deterministic scenario:

```text
A is downloading and owns a slot.
B is queued.
A activates another candidate.
A's old writer is retired.
B must not steal A's continuation reservation.
A replacement dispatches using the same admission entitlement.
Total actual active writers never exceeds the configured limit.
```

---

# 14. Canonical Recovery State Must Be State, Not an Event Log

## Exact implementation surface

**Current snapshot/event owner:**

```text
backend/transfers/repository.py
  _recovery_event_kind()
  _recovery_snapshot()
  _save_recovery_snapshot()
  recovery_context()
  execution()
  transition_recovery()
```

**Layered snapshot extensions:**

```text
backend/transfers/_recovery_repository_phase3.py
backend/transfers/_recovery_repository_audit.py
backend/transfers/_recovery_repository_claim_base.py
backend/transfers/recovery_repository.py
```

**Schema/migration:**

```text
backend/db/database.py
  application_events schema
  additive runtime schema/migration bootstrap
```

**Retention/maintenance:**

```text
backend/core/scheduler.py
  events_ttl_loop()

backend/services/db_maintenance.py
```

**Consumers:**

```text
backend/transfers/presentation_repository.py
backend/api/operational_downloads.py
```

**Tests:**

```text
backend/tests/test_recovery_state_store.py             # CREATE IF ABSENT
backend/tests/test_transfer_recovery_phase4.py
```

## Required architecture

Introduce a dedicated canonical current-state store for artifact recovery.

A likely shape is a table conceptually similar to:

```text
artifact_recovery_state
```

with exactly one current row per canonical artifact.

Do not blindly adopt that name if a better existing canonical schema location exists.

Current state should contain only current/actionable facts, including recovery epoch/generation, current claim, current quiescence/wake, current decision when applicable, current candidate-attempt state, bounded counters, durable target, and any admission-continuation identity.

Historical audit/provenance must be separate sparse append-only history.

Production recovery policy must stop deriving current state from `application_events` latest-snapshot rows.

---

# 15. Separate Current State From Historical Audit Facts

## Exact implementation surface

```text
backend/transfers/repository.py
backend/transfers/_recovery_repository_phase3.py
backend/transfers/_recovery_repository_audit.py
backend/transfers/_recovery_repository_claim_base.py
backend/transfers/recovery_repository.py
backend/transfers/presentation_repository.py
backend/db/database.py
```

**Tests:**

```text
backend/tests/test_recovery_state_store.py             # CREATE IF ABSENT
backend/tests/test_transfer_recovery_phase4.py
```

Historical fields such as:

```text
last_failure_identity
last_applied_action
last_applied_reason
last_execution_attempt
last_terminalization_reason
last_candidate_switch_reason
historical decision IDs
```

must not live in the same flat current-state object unless their historical nature is structurally explicit and policy cannot mistake them for current state.

Prefer:

```text
current recovery state
+
separate recovery audit/provenance events
```

Policy must read only current state.

UI history/details may read audit history.

---

# 16. Recovery Epoch and Generation Semantics

## Exact implementation surface

```text
backend/transfers/repository.py
  execution()
  transition_recovery()

backend/transfers/recovery_execution.py
backend/transfers/_recovery_repository_phase3.py
backend/transfers/_recovery_repository_audit.py
backend/transfers/_recovery_repository_claim_base.py
backend/transfers/recovery_repository.py
backend/transfers/_engine_recovery.py
backend/db/database.py
```

**Tests:**

```text
backend/tests/test_recovery_state_store.py             # CREATE IF ABSENT
backend/tests/test_transfer_recovery_phase4.py
```

Define and document the model for:

- recovery epoch;
- recovery generation;
- candidate-attempt generation/history.

Do not increment epochs on arbitrary scheduler ticks.

Do not carry current decision identity across an epoch where it no longer applies.

Do not erase useful historical provenance merely to clear current state.

A meaningful-progress transition must have one canonical method that updates every current field that semantically belongs to the old epoch.

---

# 17. One Canonical Recovery Reset / New-Attempt Transition

## Exact implementation surface

**Current divergent reset owners:**

```text
backend/transfers/repository.py
  reset_source_recovery()
  reset_retry_budget()
  transition_recovery(reset_budget=...)
  execution() meaningful-progress reset

backend/transfers/recovery_repository.py
  final reset_retry_budget()
```

**Layer extensions whose fields must be accounted for:**

```text
backend/transfers/_recovery_repository_phase3.py
backend/transfers/_recovery_repository_audit.py
backend/transfers/_recovery_repository_claim_base.py
```

**Callers:**

```text
backend/transfers/_engine_recovery.py
backend/transfers/manual_failover.py
backend/transfers/convergence_engine.py
```

**Tests:**

```text
backend/tests/test_recovery_state_store.py             # CREATE IF ABSENT
backend/tests/test_candidate_activation.py             # CREATE IF ABSENT
```

Create one canonical internal transition for beginning a new recovery attempt/generation or advancing a recovery epoch.

Callers supply authority/reason such as:

```text
meaningful progress
operator retry
candidate activation
resume
automatic failover
startup reconciliation
```

The transition must intentionally define which facts are RESET, PRESERVED, ARCHIVED, or ADVANCED.

Do not fix this by manually adding more snapshot assignments to `manual_failover.py`.

---

# 18. Sparse Recovery Audit Persistence

## Exact implementation surface

```text
backend/transfers/repository.py
  _save_recovery_snapshot()
  execution()
  record_recovery_decision()
  record_source_failure()

backend/transfers/_recovery_repository_phase3.py
backend/transfers/_recovery_repository_audit.py
backend/transfers/_recovery_repository_claim_base.py
backend/transfers/recovery_repository.py
backend/db/database.py
backend/services/db_maintenance.py
backend/core/scheduler.py
```

**Tests:**

```text
backend/tests/test_recovery_state_store.py             # CREATE IF ABSENT
```

Preserve durable explainability, but append audit rows only for semantically meaningful transitions, e.g. claim creation, decision, candidate activation, refresh, execution retirement, quiescence entry/exit, generation/epoch advancement, terminal recovery, operator retry, operator source switch.

Ordinary byte progress belongs in execution progress state, not a full recovery-history snapshot.

Add a regression with many monotonically increasing progress observations proving audit row growth is bounded by semantic transitions rather than observation count.

---

# 19. Existing `application_events` Migration

## Exact implementation surface

```text
backend/db/database.py
  schema bootstrap / additive migrations
  application_events schema

backend/transfers/repository.py
  old snapshot decoder/reader

backend/transfers/_recovery_repository_phase3.py
backend/transfers/_recovery_repository_audit.py
backend/transfers/recovery_repository.py

backend/services/db_maintenance.py
```

**Tests:**

```text
backend/tests/test_recovery_state_store.py             # CREATE IF ABSENT
```

Implement an idempotent migration that:

1. creates the new current recovery-state structure;
2. reconstructs the best current state for each artifact from existing durable data;
3. uses the latest applicable historical recovery snapshot only as migration input;
4. preserves existing historical `application_events` rows unless there is an explicit safe retention policy;
5. does not fabricate recovery facts absent from the old DB;
6. is safe to run repeatedly;
7. supports legacy DBs where no recovery snapshot exists;
8. supports partially upgraded DBs.

After migration, production policy must read from the new canonical current-state owner.

Do not retain a permanent dual-read owner where either old or new storage can silently win.

---

# 20. Index / Persistence Audit

## Exact implementation surface

```text
backend/db/database.py
backend/services/db_maintenance.py
backend/transfers/repository.py
backend/transfers/presentation_repository.py
backend/api/operational_downloads.py
```

If migration or history still queries `application_events` by `kind`, verify query plans and add only the indexes justified by real access patterns.

Audit database growth and write amplification.

The final report must include:

```text
old steady-state recovery writes per progress observation
new steady-state recovery writes per progress observation
```

and explain the reduction.

---

# 21. Parent Transfer Aggregation Must Use a Coherent Snapshot

## Exact implementation surface

**Aggregation owners:**

```text
backend/transfers/_engine_base.py
  _aggregate()

backend/transfers/_engine_recovery.py
  _aggregate() override
```

**Persistence/read snapshot:**

```text
backend/transfers/_repository_base.py
  state()
  get()/active()/artifacts()/requests()/execution retrieval as consumed by aggregate

backend/transfers/repository.py
backend/transfers/recovery_repository.py
```

**Application caller:**

```text
backend/application/service.py
  reconcile_executions()
```

**Manual caller currently added by 3876:**

```text
backend/transfers/manual_failover.py
```

**Tests:**

```text
backend/tests/test_transfer_aggregation_snapshot.py    # CREATE IF ABSENT
backend/tests/test_manual_candidate_failover.py
```

Make canonical parent aggregation derive from one coherent durable snapshot.

Acceptable approaches include a repository-owned SQLite transaction/read snapshot that loads all required transfer facts and computes/writes the parent state, or another equally strong generation-fenced design.

Do not solve this with arbitrary scheduler locking alone if DB reads remain torn.

Inventory direct parent-state writes and reduce them to explicitly justified owners.

---

# 22. Parent Aggregation and Mutation Fencing

## Exact implementation surface

```text
backend/transfers/_engine_base.py
backend/transfers/_engine_recovery.py
backend/transfers/convergence_engine.py
backend/transfers/_repository_base.py
backend/transfers/repository.py
backend/transfers/recovery_repository.py
backend/transfers/manual_failover.py
backend/application/service.py
```

**Tests:**

```text
backend/tests/test_transfer_aggregation_snapshot.py    # CREATE IF ABSENT
backend/tests/test_recovery_command_concurrency.py     # CREATE IF ABSENT
```

The parent aggregate must not overwrite a newer mutation based on stale child facts.

Use transfer-level generation/fencing, lock ordering, or compare-and-set semantics as appropriate.

Create adversarial tests where aggregation reads old child truth, a concurrent command changes candidate/recovery state, then aggregation attempts a stale write. The stale write must not win.

---

# 23. Transfer 221 State-Churn Investigation

## Exact implementation surface

```text
backend/transfers/_engine_base.py
  reconcile_executions()
  _process_executions()
  _aggregate()
  _converge_execution()

backend/transfers/_engine_recovery.py
  _aggregate()

backend/transfers/repository.py
  execution()

backend/transfers/_repository_base.py
  state()

backend/application/service.py
  reconcile_executions()

backend/core/scheduler.py
  sync_download_clients_loop()
```

**Tests / reproducer:**

```text
backend/tests/test_transfer_aggregation_snapshot.py    # CREATE IF ABSENT
backend/tests/test_execution_admission_continuity.py   # CREATE IF ABSENT
```

Before declaring leveling complete, create a deterministic multi-artifact reproduction and determine whether rapid `downloading ↔ queued` parent churn is caused by:

- genuine executor observations;
- parent aggregation semantics;
- competing state writers;
- Pause/Resume convergence;
- capacity behavior;
- stale/torn aggregate snapshots;
- event duplication;
- another mechanism.

Do not claim the historical transfer-221 observation solved unless reproduced and explained.

---

# 24. One Per-Transfer Mutation Authority

## Exact implementation surface

**Application admission (not a mutex):**

```text
backend/services/maintenance_gate.py
backend/application/service.py
```

**Execution/transfer locks:**

```text
backend/transfers/_engine_base.py
backend/transfers/convergence_engine.py
```

**Recovery claims/fences:**

```text
backend/transfers/_recovery_repository_claim_base.py
backend/transfers/recovery_repository.py
backend/transfers/recovery_execution.py
```

**Commands:**

```text
backend/transfers/manual_failover.py
backend/application/manual_candidate_failover.py
backend/application/service.py
  pause/resume/retry/cancel/delete/select_artifact
```

**Tests:**

```text
backend/tests/test_recovery_command_concurrency.py     # CREATE IF ABSENT
```

Create one documented lock/fence ordering for transfer mutations.

Not every operation necessarily needs an asyncio mutex if DB generation fencing provides equivalent safety, but there must be one coherent concurrency model.

Document lock ordering and enforce it consistently.

---

# 25. Recovery Claim Concurrency

## Exact implementation surface

```text
backend/transfers/recovery_execution.py
backend/transfers/_recovery_repository_claim_base.py
backend/transfers/recovery_repository.py
backend/transfers/convergence_engine.py
backend/transfers/_engine_recovery.py
backend/transfers/manual_failover.py
backend/application/service.py
```

**Tests:**

```text
backend/tests/test_recovery_command_concurrency.py     # CREATE IF ABSENT
```

Barrier-test:

```text
manual candidate switch vs AUTO_RETRY
manual candidate switch vs USER_RETRY
manual candidate switch vs RESUME
manual candidate switch vs scheduler execution observation
manual candidate switch vs pause
manual candidate switch vs cancel/delete
candidate activation vs stale recovery claim completion
```

A stale claim must not restore an old candidate/quiescence/generation/writer or overwrite newer operator truth.

---

# 26. Eliminate Post-Commit False Failure

## Exact implementation surface

**Current defect:**

```text
backend/transfers/manual_failover.py
  durable switch + success provenance
  followed by engine._aggregate()
  followed by TransferError if aggregate raises
```

**Provenance:**

```text
backend/transfers/manual_repository.py
```

**Application/API result mapping:**

```text
backend/application/manual_candidate_failover.py
backend/api/operational_downloads.py
```

**Future shared owner:**

```text
canonical candidate-activation module/path created in Section 10
```

**Tests:**

```text
backend/tests/test_manual_candidate_failover.py
backend/tests/test_candidate_activation.py             # CREATE IF ABSENT
```

The external result must truthfully represent durable outcome.

**Hard post-commit rule:** once candidate activation is durably committed — including the new selected candidate/generation and its success provenance, with prior-writer retirement truth already recorded — any subsequent parent aggregation or reconciliation failure is a reconciliation failure of the **already-committed transfer state**. It must not change the durable candidate-activation command outcome to failed, must not append contradictory candidate-activation failure provenance, and must not tell the caller that the switch itself failed in a way that invites an unsafe duplicate retry. Preserve the committed activation result and surface/record the reconciliation problem through the canonical reconciliation/error path.

Do not fabricate rollback of an already-retired external writer.

Do not write contradictory success/failure provenance.

Do not return a plain failure that invites unsafe duplicate retry after a committed candidate activation.

---

# 27. External Writer Retirement Semantics

## Exact implementation surface

```text
backend/transfers/manual_failover.py
backend/transfers/_engine_recovery.py
  _activate_alternate()

backend/transfers/_engine_base.py
  _converge_execution()
  execution ownership/dispatch handling

backend/transfers/_repository_base.py
  execution attempts / authorization / live executions

backend/transfers/repository.py
  execution()
  transition_recovery()

backend/transfers/recovery_repository.py
backend/transfers/canonical.py
```

**Tests:**

```text
backend/tests/test_candidate_activation.py             # CREATE IF ABSENT
backend/tests/test_recovery_command_concurrency.py     # CREATE IF ABSENT
```

Candidate activation must clearly distinguish:

```text
writer retirement requested
writer retirement confirmed
writer retirement uncertain
new writer authorized
```

Never authorize two current writers for one artifact.

Late observations from a retired generation must be fenced and incapable of mutating the new artifact generation.

---

# 28. Partial File / Resume Policy

## Exact implementation surface

**Current recovery-side sidecar/partial handling:**

```text
backend/transfers/_engine_recovery.py
  _candidate_sidecars()
  _activate_alternate()
  _terminal_recovery()
```

**Current manual activation:**

```text
backend/transfers/manual_failover.py
```

**Canonical partial-retirement owner:**

```text
backend/transfers/filesystem.py
  retire_partial()
```

**Recovery-side sidecar / activation callers:**

```text
backend/transfers/_engine_recovery.py
  _candidate_sidecars()
  _activate_alternate()
  _terminal_recovery()
```

**Manual candidate activation path:**

```text
backend/transfers/manual_failover.py
  current manual candidate-switch partial/writer handling
```

**Production executor resumability contract:**

```text
backend/executors/aria2/executor.py
  Aria2Executor.resumable_paths()
```

**Neutral test executor / test support:**

```text
backend/tests/fake_integrations.py
  MemoryExecutor.resumable_paths()

backend/tests/test_ws2p1_failover_progress.py
```

These are verified current `1.0.12` paths. Do not replace them with a new executor-specific partial/resume authority. If implementation inspection discovers additional call sites, record them in the implementation report, but the files above are the required starting map and must be reviewed before changing partial/resume semantics.

**Tests:**

```text
backend/tests/test_candidate_activation.py             # CREATE IF ABSENT
backend/tests/test_ws2p1_failover_progress.py
backend/tests/fake_integrations.py
```

Automatic and operator candidate activation must use the same partial-state policy.

No provider-specific recovery rule belongs in the universal core.

---

# 29. Candidate Identity and Provenance

## Exact implementation surface

```text
backend/transfers/canonical.py
backend/transfers/cohorts.py
backend/transfers/manual_repository.py
backend/transfers/manual_failover.py
backend/transfers/_repository_base.py
backend/transfers/repository.py
backend/transfers/recovery_repository.py
backend/db/database.py
  canonical_candidate_bindings
  canonical_candidate_origins
  execution_attempt_provenance
  route_attempt_provenance
```

**Tests:**

```text
backend/tests/test_candidate_activation.py             # CREATE IF ABSENT
backend/tests/test_manual_candidate_failover.py
```

For every candidate activation retain at least transfer, artifact, old/new candidate, provider/source identity, activation reason/authority, recovery epoch/generation, old/new execution identity, partial-state decision, admission-continuation decision, and outcome.

Do not infer active source later from URL alone.

---

# 30. UI / Presentation Consistency

## Exact implementation surface

**Backend presentation:**

```text
backend/transfers/presentation_repository.py
backend/api/operational_downloads.py
```

**Candidate command/API:**

```text
backend/application/manual_candidate_failover.py
backend/api/operational_downloads.py
```

**Frontend:**

```text
frontend/static/ui-group-candidates.js
```

**Browser tests:**

```text
frontend/browser/group-candidates.spec.js
frontend/browser/app.spec.js
```

Preserve current switching-progress UX:

```text
Switching to <safe source label>
0 of 1
```

before accepted artifact-mode POST completion and `1 of 1` only after acceptance.

Do not optimistically label the transfer `Downloading`.

During a continuation-admission handoff, show intentional switching/recovering truth rather than transient red failure.

If replacement is genuinely waiting for capacity, show canonical capacity-wait presentation.

---

# 31. Do Not Create Another Presentation Authority

## Exact implementation surface

```text
backend/transfers/presentation_repository.py
backend/api/operational_downloads.py
frontend/static/ui-group-candidates.js
```

Presentation derives from canonical durable facts.

It must not repair backend state, invent candidate attempt history, infer recovery policy, persist lifecycle truth, or silently override contradictions.

Fix contradictions at the canonical owner.

---

# 32. Production-Stack Test Harness

## Exact implementation surface

**Existing insufficient harness:**

```text
backend/tests/test_manual_candidate_failover.py
```

It currently imports the lower engine/repository stack and uses high capacity.

**Production composition references:**

```text
backend/application/composition.py
backend/transfers/convergence_engine.py
backend/transfers/recovery_repository.py
backend/transfers/recovery_execution.py
```

**Existing test fixtures:**

```text
backend/tests/test_ws2p1_failover_progress.py
backend/tests/fake_integrations.py
```

**Suggested new harness/tests:**

```text
backend/tests/test_candidate_activation.py             # CREATE IF ABSENT
backend/tests/test_execution_admission_continuity.py   # CREATE IF ABSENT
backend/tests/test_recovery_command_concurrency.py     # CREATE IF ABSENT
```

Create a reusable test composition that instantiates the **actual production recovery repository/engine ownership chain** without requiring the full web application.

The harness must include final repository MRO, Phase-3 recovery claims, final reset behavior, canonical candidate ownership, execution convergence, parent aggregation, configurable low capacity, deterministic clock, controllable executors/providers, and barrier hooks.

Keep lighter unit tests where useful, but major recovery/candidate invariants need production-stack regressions.

---

# 33. Required Named Regression Set

Place each test in the existing owner file where that is cleanest; otherwise use the CREATE IF ABSENT files identified above.

### Canonical membership

**Target file:** `backend/tests/test_operational_artifact_membership.py` unless existing recovery-presentation test ownership is clearly better.

```text
test_blocked_attention_child_does_not_vote_in_parent_presentation
test_standby_attention_child_does_not_vote_in_parent_presentation
test_details_can_show_historical_child_without_operational_vote
```

### Presentation autonomy

**Target files:**

```text
backend/tests/test_transfer_recovery_phase4.py
backend/tests/test_operational_artifact_membership.py
```

```text
test_queued_child_suppresses_sibling_requires_attention_aggregate
test_all_exhausted_children_require_attention
test_capacity_wait_is_not_operator_attention
```

### Candidate traversal / shared activation

**Target file:** `backend/tests/test_candidate_activation.py`.

```text
test_manual_high_index_switch_does_not_hide_unattempted_lower_candidates
test_candidate_attempt_history_prevents_cycle_within_generation
test_new_recovery_generation_resets_candidate_attempt_scope
test_refreshed_new_candidate_enters_current_traversal
test_manual_and_automatic_activation_use_same_core_transition
test_candidate_activation_advances_or_fences_recovery_generation
test_stale_recovery_claim_cannot_overwrite_new_candidate
```

### Admission continuity

**Target file:** `backend/tests/test_execution_admission_continuity.py`.

```text
test_active_failover_preserves_execution_admission
test_unrelated_queue_cannot_steal_failover_continuation_slot
test_nonactive_manual_switch_does_not_gain_capacity_priority
test_failed_handoff_releases_capacity_reservation
test_restart_reconciles_stale_handoff_reservation
```

### Parent snapshot / concurrency

**Target file:** `backend/tests/test_transfer_aggregation_snapshot.py`.

```text
test_parent_aggregate_uses_coherent_child_snapshot
test_stale_aggregate_cannot_overwrite_newer_generation
test_multi_artifact_parent_does_not_flap_from_noncanonical_rows
```

### Recovery persistence

**Target file:** `backend/tests/test_recovery_state_store.py`.

```text
test_progress_observations_do_not_append_full_recovery_snapshot_each_tick
test_meaningful_progress_advances_current_epoch_and_clears_current_decision
test_historical_failure_remains_audit_only_after_epoch_advance
test_migration_recovers_latest_current_state_without_fabrication
test_migration_is_idempotent
```

### Command races

**Target file:** `backend/tests/test_recovery_command_concurrency.py`.

```text
test_manual_activation_vs_resume_is_generation_safe
test_manual_activation_vs_retry_is_generation_safe
test_manual_activation_vs_pause_is_deterministic
test_manual_activation_vs_cancel_never_reauthorizes_writer
test_late_retired_writer_observation_cannot_mutate_new_generation
```

### Truthful acknowledgement

**Target files:**

```text
backend/tests/test_manual_candidate_failover.py
backend/tests/test_candidate_activation.py
```

```text
test_committed_candidate_activation_never_returns_plain_failure_due_to_postcommit_projection
test_activation_history_never_records_contradictory_success_and_failure
```

Preserve prior stale-attention manual-switch regressions and current parent re-aggregation behavior.

---

# 34. RED / GREEN Evidence Discipline

For each material architectural defect, final evidence must state:

```text
CLAIM:
FILES / SYMBOLS:
TEST:
STARTING IMPLEMENTATION:
RED:
CORRECTION:
GREEN:
```

A bare full-suite count does not replace defect-specific evidence.

---

# 35. Architecture-Level Assertions

## Exact source files whose boundaries must be protected

```text
backend/transfers/_repository_base.py
backend/transfers/presentation_repository.py
backend/transfers/_engine_recovery.py
backend/transfers/manual_failover.py
backend/transfers/recovery_repository.py
backend/transfers/repository.py
backend/db/database.py
backend/application/composition.py
backend/tests/test_manual_candidate_failover.py
```

Add tests/invariant checks that make future drift difficult, including:

- presentation operational membership comes from canonical artifact membership;
- automatic and manual candidate activation reach the same implementation;
- candidate traversal never uses `selected + 1` as historical exhaustion;
- current recovery policy no longer queries `application_events` as its current state store;
- ordinary progress observation does not append a current-recovery snapshot event;
- production recovery tests instantiate final production engine/repository MRO;
- only approved lifecycle owners write parent status.

Prefer behavioral assertions over brittle source-text tests where possible.

---

# 36. Migration / Upgrade Qualification

## Exact migration source

```text
backend/db/database.py
```

## Exact legacy/current recovery readers involved in migration

```text
backend/transfers/repository.py
backend/transfers/_recovery_repository_phase3.py
backend/transfers/_recovery_repository_audit.py
backend/transfers/recovery_repository.py
```

## Tests

```text
backend/tests/test_recovery_state_store.py             # CREATE IF ABSENT
```

Cover representative pre-leveling DB states including no recovery history, one snapshot, many progress snapshots, retry backoff, wait-for-operator, active claim, meaningful progress with stale historical fields, candidate switches, and mixed multi-artifact recovery states.

Migration must not resurrect completed work, fabricate attempts, accidentally clear legitimate retry/operator state, create two writers, or lose provenance.

---

# 37. Performance Qualification

## Exact hot paths

```text
backend/transfers/repository.py
backend/transfers/recovery_repository.py
backend/transfers/presentation_repository.py
backend/api/operational_downloads.py
backend/transfers/_engine_base.py
backend/application/service.py
backend/core/scheduler.py
backend/db/database.py
```

Measure representative before/after behavior for:

- recovery current-state read;
- execution progress persistence;
- Downloads list projection;
- Details presentation;
- scheduler execution cycle with several active artifacts.

Record SQLite acquisition count where material, recovery-history writes, current recovery-state writes, and list projection query count.

Key acceptance criterion: ordinary progress no longer grows recovery state/history linearly with polling frequency.

---

# 38. Static / Security Qualification

Run the normal applicable checks, including:

```text
python compile / compileall
undefined-name checks F821/F822/F823 or project equivalent
JS syntax checks
Bandit
pip-audit
CodeQL if available in normal repository workflow
git diff --check
```

If a check is unavailable locally, report that exact limitation.

---

# 39. Full Test Qualification

Run:

- new focused recovery/candidate/admission tests;
- existing `backend/tests/test_manual_candidate_failover.py`;
- existing `backend/tests/test_transfer_recovery_phase4.py`;
- existing recovery/Phase-3 suites discovered with `git grep`/test listing;
- existing WS3 readiness/routing suites;
- existing post-audit suites;
- existing two-provider/generalized-provider suites;
- full Python test suite;
- `frontend/browser/group-candidates.spec.js`;
- full Browser Runtime.

Before execution, list the exact discovered existing recovery-related test files rather than relying on remembered names.

If Browser Runtime has environment failures, prove the full failing-set baseline equivalence before claiming all failures pre-existing.

---

# 40. Docker / Runtime Qualification

If local environment permits candidate runtime qualification, build/test without replacing production or staging.

Do not touch:

```text
debridpulse-v1012-staging
adc-v199-staging
```

Do not replace the running production container as part of this prompt.

If Docker qualification runs, record exact image digest/revision and architecture results.

---

# 41. Out of Scope

Unless required by a direct architectural dependency, do not drift into:

```text
/api/torrents -> /api/downloads rename
Real-Debrid implementation
SCP/SFTP implementation
rsync implementation
browser extension work
Usenet
general CSS redesign
unrelated Settings work
provider-card UX
download file-selector redesign
unrelated storage-health UX
release/tag/promotion
main branch changes
```

The previously observed whole-application lock remains outside the proven root cause.

If it reproduces during this work and can be causally tied to the architecture being modified, capture evidence and fix it. Do not claim it solved otherwise.

---

# 42. Preserve Existing Correct Semantics

Requalify, do not regress:

- direct HTTP/HTTPS enumeration;
- torrent/magnet lifecycle;
- universal file selection;
- provider readiness/UNRESOLVED semantics;
- route provenance;
- candidate provenance;
- durable execution provenance;
- stale-LKG applicability behavior;
- provider-disabled/storage/executor quiescence;
- input-required lifecycle;
- pause/resume convergence;
- prior stale manual-switch recovery-context correction;
- current `3876fdd...` visible switching progress and parent re-aggregation intent;
- completed-transfer presentation;
- storage-domain isolation;
- provider-neutral core boundaries.

Use repository search to discover the exact existing test files for each preserved behavior before modifying shared owners.

No provider-specific recovery policy is acceptable in the universal core.

---

# 43. Architectural Simplification Requirement

At the end, explicitly report whether these concepts have exactly one owner and name the **exact final file + symbol** for each:

```text
canonical actionable artifact membership
current recovery state
recovery generation
candidate attempt history
candidate activation
execution admission / continuation reservation
parent lifecycle aggregation
effective transfer presentation
historical recovery audit
```

Format the report as:

```text
CONCEPT=
OWNER_FILE=
OWNER_SYMBOL=
SECONDARY_WRITERS=NONE | <exact list>
```

If any still have multiple semantic owners, explain why and whether the architecture is actually complete.

Do not declare leveling complete merely because multiple layers now agree.

---

# 44. Code Review Pass

After all tests pass, repository-wide search for these patterns/concepts:

```bash
git grep -n "selected + 1"
git grep -n "application_events"
git grep -n "reset_budget"
git grep -n "clear_quiescence"
git grep -n "manual_candidate_failover"
git grep -n "_activate_alternate"
git grep -n "UPDATE torrents SET status"
git grep -n "download_files WHERE torrent_id"
git grep -n "recovery_generation"
git grep -n "candidate_switches"
git grep -n "blocked_retry_at"
```

Specifically detect:

- selected-index traversal still acting as history;
- current-state reads from recovery event history;
- duplicate reset dictionaries;
- duplicate quiescence clearing;
- manual-only candidate mutation;
- automatic-only candidate mutation;
- direct parent-state writes outside approved owners;
- presentation queries using all `download_files` indiscriminately;
- new unbounded event writes;
- new lock-order inversions;
- post-commit exceptions returning false failure;
- capacity reservations that can leak;
- recovery generation writes outside canonical owner.

---

# 45. Final Diff Hygiene

Before final report:

```bash
git status --short
git diff --check
git diff --stat
git diff
```

Ensure:

- no debug prints;
- no temporary fixtures;
- no accidental binary files;
- no generated caches;
- no unrelated formatting churn;
- no secrets;
- no production credentials;
- no staging modifications;
- no accidental `main` changes.

---

# 46. Final Evidence Report

Produce a complete report with these sections.

## A. Starting state

```text
START_BRANCH=
START_SHA=
START_TREE=
START_PARENT=
MAIN_SHA=
```

## B. Root causes confirmed

For every issue classify:

```text
PROVEN
PROVEN ARCHITECTURAL RACE SURFACE
DISPROVEN
NOT REPRODUCED
```

Include exact source file/symbol references for each claim.

## C. Final architecture / owner map

For every canonical concept give exact final file and symbol.

## D. Files changed

For each changed file explain:

```text
FILE=
PREVIOUS_ROLE=
NEW_ROLE=
WHY_CHANGED=
SUPERSEDED_OWNER_REMOVED=YES/NO
```

## E. Migration

State new schema objects, migration behavior, idempotence result, and backward/rollback considerations.

## F. RED/GREEN ledger

For every material defect include claim, exact source files/symbols, named test, actual RED, correction, actual GREEN.

## G. Qualification

Give exact counts/results for focused backend, recovery/Phase-3, manual failover, routing/readiness, two-provider, full pytest, focused Browser Runtime, full Browser Runtime, compile/static, Bandit, pip-audit, CodeQL, and Docker/runtime if run.

## H. Performance / persistence evidence

Include before/after recovery write behavior and material query changes.

## I. Remaining risks

Anything not fully proven must be explicitly marked:

```text
UNPROVEN
```

## J. Git state

```text
FINAL_HEAD_SHA=
FINAL_TREE=
FILES_CHANGED=
COMMIT_CREATED=NO
PUSH_PERFORMED=NO
MAIN_CHANGED=NO
VOLUNTARY_HANDOFF_USED=NO
READY_FOR_USER_REVIEW=YES
```

---

# 47. Acceptance Criteria

This leveling pass is complete only when all of the following are true:

1. lifecycle and transfer-level presentation use the same canonical actionable artifact membership;
2. queued autonomous work cannot incorrectly lose to exhausted sibling attention;
3. capacity waiting is distinguishable from broken recovery;
4. manual and automatic candidate changes use one canonical candidate-activation path;
5. candidate activation participates in recovery generation/fencing;
6. current selected candidate is no longer used as candidate-attempt history;
7. automatic traversal can find unattempted candidates regardless of index relative to a manual selection;
8. already-admitted failover can preserve a bounded continuation admission without exceeding execution capacity;
9. current recovery state no longer depends on an append-only per-progress snapshot log;
10. historical audit facts cannot act as current policy state;
11. meaningful progress advances/clears recovery state through one canonical owner;
12. parent aggregation is based on a coherent durable snapshot and cannot stale-write over a newer generation;
13. recovery-affecting commands have one explicit concurrency/fencing model;
14. a committed candidate activation cannot be reported as an ordinary failed command solely because post-commit reconciliation failed;
15. production-stack tests exercise Phase-3 + candidate activation + low-capacity behavior;
16. transfer-221 churn is either reproduced/explained or explicitly left evidence-bounded `UNPROVEN`;
17. prior correct `1.0.12` behavior remains green;
18. no release, tag, promotion, or `main` modification occurs;
19. the final report names exact final file/symbol owners for every core concept.

---

# 48. Design Principle to Keep in View

> A user submitted a durable transfer. The core owns its lifecycle. Providers expose candidate resources. Executors perform work. Recovery decides how the same durable transfer continues. Candidate switching—manual or automatic—is simply one recovery-controlled way of continuing that same work. Capacity determines when work may execute, not whether recovery truth is valid. Presentation explains that canonical truth; it does not invent another one.

If the resulting implementation still requires explaining separate manual-failover truth, automatic-failover truth, presentation truth, and Phase-3 truth, the leveling is not finished.

Continue until those concepts converge on one architecture.

---

# 49. Final Stop Condition

Do not stop simply because the full suite is green.

Before stopping, explicitly answer:

```text
ONE_CURRENT_RECOVERY_OWNER=YES/NO
CURRENT_RECOVERY_OWNER_FILE=
CURRENT_RECOVERY_OWNER_SYMBOL=

ONE_CANDIDATE_ACTIVATION_OWNER=YES/NO
CANDIDATE_ACTIVATION_OWNER_FILE=
CANDIDATE_ACTIVATION_OWNER_SYMBOL=

ONE_ARTIFACT_MEMBERSHIP_OWNER=YES/NO
ARTIFACT_MEMBERSHIP_OWNER_FILE=
ARTIFACT_MEMBERSHIP_OWNER_SYMBOL=

ONE_PARENT_AGGREGATION_OWNER=YES/NO
PARENT_AGGREGATION_OWNER_FILE=
PARENT_AGGREGATION_OWNER_SYMBOL=

ONE_PRESENTATION_OWNER=YES/NO
PRESENTATION_OWNER_FILE=
PRESENTATION_OWNER_SYMBOL=

CANDIDATE_SELECTION_SEPARATE_FROM_ATTEMPT_HISTORY=YES/NO
FAILOVER_ADMISSION_CONTINUITY_PROVEN=YES/NO
POSTCOMMIT_ACK_AMBIGUITY_REMOVED=YES/NO
HIGH_FREQUENCY_RECOVERY_EVENT_APPEND_REMOVED=YES/NO
PRODUCTION_STACK_RECOVERY_TESTS_ADDED=YES/NO
TRANSFER_221_CHURN_EXPLAINED=YES/NO/UNPROVEN
```

Any `NO` must be explained and prevents claiming architectural leveling complete unless the missing item is demonstrably outside scope.

Then stop for user review without committing or pushing.
