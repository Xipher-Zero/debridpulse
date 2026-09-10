# v1.0.12 two-provider canonical regression and replacement map

The frozen production baseline remains `61d5eec345c473532c46508f76c69a3d6b36747a` (released v1.0.11.1). The current v1.0.12 development checkpoint begins from the qualified Roadmap Item 11 baseline `3ae7be9259f93bfafc686640454d86a4c893f5a3`, tree `2f9fc139b5554b10fdb06df7fab74597c881b0bb`.

This document describes the **implemented and qualified current two-provider development architecture**. It is an early Stage 17/18 shim convergence over completed Items 0–11, not the final v1.0.12 release convergence. Items 12–16 remain intentionally deferred and the eventual full Stage 17/18 consolidation/qualification must be rerun after those providers, protocols, executors, and dependencies are implemented.

## Replacement history

The v1.0.12 Universal Transfer cutover physically removed the manager inheritance chain, captured-method coordinators, and provider/executor forwarding gateways. `LEGACY_TEST_MIGRATION.json` preserves the historical test migration census. Those retired layers are not compatibility owners in the current runtime.

Current permanent architecture coverage proves that the retired owner modules remain absent and that application/core/provider/executor responsibilities terminate at the canonical boundaries documented in `UNIVERSAL_TRANSFER_CORE.md` and `MULTI_PROVIDER_HTTP_SLICE.md`.

## Canonical behavioral evidence

All paths below are under `backend/tests/` unless explicitly stated otherwise.

| Current contract | Canonical regression owners |
| --- | --- |
| Universal identity/lifecycle, pause/cancel/delete, capacity, retry/recovery, verified possession | `test_application_runtime.py`, `test_universal_lifecycle.py`, `test_universal_boundaries.py`, `test_universal_parity.py`, `test_universal_hardening.py`, `test_transfer_integrity.py` |
| Delete permanently retires the active dedupe identity; re-add is a fresh transfer with its own provider-resource *binding generation* (`RA ≠ RB` for identical native `R`, no manifest/selection aliasing); observation/inventory/duplicate lookups on a shared canonical resource resolve to the active binding (incl. the pre-split legacy form) and never cross-target or resurrect the retired one; idempotent additive `source_fingerprint` / `resource_key` backfills; provider cleanup fence blocks pending/claimed-in-flight/scheduled-retry and releases only on completion or terminal abandonment, with startup stale-claim reclaim | `test_deleted_transfer_generation_retirement.py` |
| Canonical architecture/retired-owner absence | `test_canonical_runtime_architecture.py`, `test_two_provider_canonical_architecture.py` |
| Provider-neutral applicability and `SPECIALIZED > GENERIC` initial routing | `test_provider_applicability.py`, `test_initial_provider_routing.py`, `test_item11_multi_provider_slice.py` |
| AllDebrid native contract and dynamic host-state/LKG ownership | `test_alldebrid_provider_contract.py`, `test_alldebrid_pattern_applicability.py`, `test_alldebrid_host_runtime.py`, `test_alldebrid_host_runtime_acceptance.py` |
| General HTTP(S) direct provider, TLS/network boundaries, authentication handoff | `test_general_http_provider.py`, `test_general_http_stage5_architecture.py`, `test_general_http_stage5_auth_boundaries.py`, `test_general_http_stage5_https.py`, `test_general_http_stage5_runtime.py` |
| Neutral `INPUT_REQUIRED` / `AUTH_REQUIRED`, same-transfer continuation, transient secrets | `test_input_required_lifecycle.py`, `test_input_required_acceptance.py`, `test_input_required_architecture.py`, `frontend/browser/input-required.spec.js`, `frontend/browser/auth-required-modal.spec.js` |
| Durable provider route/candidate/executor provenance and no URL reconstruction | `test_route_provider_provenance.py`, `test_route_provider_provenance_audit.py`, `test_stage10_provenance_presentation.py`, `frontend/browser/stage10-provenance.spec.js` |
| Canonical integration settings/enablement and Settings layout | `test_integration_configuration.py`, `test_stage10_settings_admission.py`, `test_settings_architecture_ui.py`, `test_ui_settings_chrome_batch_contract.py` |
| aria2 executor/security boundary and native ownership | `test_v1111_aria2_security_boundary.py`, `test_aria2_executor_contract.py` |
| Current docs/version/license/OCI checkpoint truth | `test_two_provider_checkpoint_documentation.py`, `test_license_policy.py`, `test_v106_final_corrective_pass.py` |
| Browser runtime, theme/responsive/provider/auth presentation | `frontend/browser/*.spec.js` |

## Current focused qualification

`backend/tests/two_provider_checkpoint_qualification.txt` is the permanent focused manifest for this checkpoint. It preserves every canonical test path from the qualified Item 11 manifest and adds the canonical runtime architecture, neutral input/auth architecture, current checkpoint documentation, and license-policy owners. The manifest composes production-path tests; it does not create a parallel mock implementation. The 1.0.12 corrective slices deliberately extend it with the four `test_file_selection_*` modules and `test_deleted_transfer_generation_retirement.py`.

The full pytest suite remains authoritative beyond the focused slice. Browser Runtime, static/compile, dependency/security, CodeQL, container runtime/security, OCI identity, SBOM/provenance, and immutable image publication remain separate required gates on the same exact checkpoint SHA.

## Current architecture invariants

- One Universal Transfer lifecycle owner; providers do not own lifecycle, scheduling, capacity, final possession, or global reconciliation.
- Universal retry/failover policy remains in the core; executors translate native execution conditions but do not decide provider routing or logical lifecycle.
- `backend/transfers/applicability.py` understands URL structure and neutral claims, not AllDebrid/General HTTP native semantics or runtime payloads.
- `backend/transfers/registry.py` performs neutral eligibility/classification/selection and contains no concrete provider or executor policy branch.
- `backend/integrations/runtime_state.py` persists opaque provider-owned bytes and neutral timing/generation metadata without interpreting payload meaning.
- AllDebrid owns its native host inventory, regex/domain semantics, freshness/LKG policy, native availability facts, and translation to neutral claims.
- General HTTP & HTTPS remains the generic `http`/`https` provider with intentionally minimal configuration.
- The Authentication Required browser component consumes canonical challenge descriptors and contains no provider/protocol/executor routing policy.
- Durable provider/executor provenance is historical truth and is never reconstructed from current URL/applicability state.

## Current support and deferred work

The current qualified development slice is AllDebrid + General HTTP & HTTPS over the Universal Transfer architecture, with aria2 as the current HTTP(S) executor. General HTTP(S) supports qualified conventional HTTP resource username/password authentication. The neutral auth component can represent private-key input, but SSH/SFTP/SCP production transports are not implemented by this checkpoint.

Deferred Items 12–16 remain future work, including FTP, SCP, SFTP/SSH, rsync, additional providers/executors/dependencies, and richer routing/failover behavior where the roadmap later requires it. Saved credential discovery/persistence and protocol-specific authentication UI are not introduced here.

## Universal file-selection / manifest overlay

The v1.0.12 universal torrent file-selection capability is provider-neutral: a
provider declares `Capability.FILE_MANIFEST` and reports facts only, while the
Universal Transfer Core owns ALL-vs-subset policy, the three independent timing
dimensions, durable per-provider-resource selection generations, SQLite
`BEGIN IMMEDIATE` Confirm-vs-materialization serialization, fail-closed
executable reconciliation, and final `SourceEntry` filtering. The executor
remains selection-blind. Items 12–16 remain intentionally deferred and this
overlay does not change that.

**Torrent/Magnet File-Selection Lifecycle Correction.** Provider preparation,
user file-selection authorization, and executor dispatch are three separate
dimensions:

* Provider preparation is eager and **independent of executor capacity** — an
  interactive torrent added while every execution slot is full still resolves,
  observes, records its manifest, opens its hold, and queues its offer.
* The 120-second **user-decision hold** is anchored exactly once to the first
  actionable multi-file manifest, `PREPARING` or `AVAILABLE`, with no
  submission-relative cutoff on when that manifest may arrive. A provider may
  stay `PREPARING` far longer than 60 seconds without losing the interactive
  selection opportunity. `decision_timeout` on expiry.
* The 60-second bound is **only** a post-`AVAILABLE` manifest-acquisition grace,
  anchored to `available_at` (the first `AVAILABLE`-without-manifest
  observation); it never runs while `PREPARING`. `manifest_timeout` on expiry.
* `initially_available` is a persisted provenance fact only and gates no timing;
  `available_at` (additive nullable column, no backfill) is the durable anchor.
* Gate authority and the wait it schedules are **one `BEGIN IMMEDIATE`
  transaction** (`repository.file_selection_gate`) — a settled `EXPLICIT`/`ALL`
  can never have a selection-derived `retry_at` recreated by stale gate work;
  both legal orders converge. The engine no longer performs a separate
  `poll_after` after the gate.
* `selection_mode` (a neutral per-request policy on the `TransferRequest`
  payload, excluded from the dedupe fingerprint) gates only whether a *new*
  selection generation is created. The browser opts in on every magnet /
  bulk-magnet / torrent-file path; historical / headless API callers default to
  ALL. **Upgrade-boundary invariant:** once a durable selection generation
  exists for a `(request, binding)`, that generation — not the request's
  current/defaulted `selection_mode` — governs manifest recording, gating,
  Confirm/Close/timeout and executable-manifest filtering. A pre-`selection_mode`
  database keeps every existing PENDING hold, EXPLICIT subset and PREPARING
  selection opportunity; every engine step past creation checks
  `repository.selection_generation_exists`, never the policy field.

The 120-second hold is a **maximum unanswered-decision window, not a minimum
delay**. Confirm (`→ explicit`) and an active-hold Close (`→ all/closed`) settle
the decision and, in the same transaction, release the file-selection gate's
scheduler `retry_at` on the owning request. `retry_at` is multi-purpose (gate
wait + provider backoff via `request_failure`); only the selection-induced
component (`state='waiting' AND error IS NULL`) is released or rescheduled, never
a coexisting legitimate backoff. `decision_deadline` is exposed only while the
decision is pending.

| Current contract | Canonical regression owners |
| --- | --- |
| Neutral capability/identity, pure gate, reconciliation, no wall-clock in policy | `test_file_selection_contract.py`, `test_universal_contracts.py`, `test_universal_boundaries.py` |
| Fake-provider-driven cached & PREPARING-origin lifecycle, 120s hold anchored to first manifest, uncached long-PREPARING keeps the decision window, post-`AVAILABLE` 60s grace (starts only at first `AVAILABLE`, expires to `manifest_timeout`), PREPARING→AVAILABLE reproducer, **Confirm/Close release the gate wait without advancing the clock**, unanswered hold still waits the full window, provider-backoff isolation, restart survival (incl. PREPARING > 60s never converts to ALL) | `test_file_selection_lifecycle.py` |
| Additive schema (incl. `available_at`), backup/wipe, FK integrity, per-resource generation across re-resolution, two-phase crash recovery, Confirm-vs-materialization concurrency, **atomic gate + Confirm has no stale-WAIT resurrection window (both orders)**, **Confirm/Dismiss retry_at release + idempotent self-heal + multi-cause isolation** | `test_file_selection_persistence.py` |
| Provider preparation eager & independent of executor capacity: cached & uncached interactive torrent fully prepares / records manifest / opens hold / queues offer while every slot is full; only the confirmed subset dispatches once a slot frees | `test_file_selection_executor_independence.py` |
| Explicit `selection_mode` intent: `normalize_selection_mode` validation, default-ALL never creates a generation/offer, explicit `interactive` enters the lifecycle, dedupe/fingerprint identity unchanged, serialization round-trip + legacy-payload default, add-magnet body / add-file form field / invalid rejection | `test_file_selection_selection_mode.py` |
| Upgrade boundary: a genuinely pre-existing generation (legacy payload with no `selection_mode`, no `available_at` anchor) — PENDING hold survives restart and still governs/times-out, EXPLICIT subset materialises subset-only after PREPARING→AVAILABLE, PREPARING/no-manifest generation keeps the 120s opportunity (stale `manifest_wait_until` never settles ALL), genuinely-new default-ALL submission unchanged, legacy re-resolution opens a fresh generation for the new binding | `test_file_selection_upgrade_boundary.py` |
| Dedicated API, `SelectionOutcome` transport codes, fixed public whitelist, A→B re-resolution regression, durable browser event, **settled read model drops the active `decision_deadline`** | `test_file_selection_api.py` |
| AllDebrid adapter capability, `ready`-flag initial availability, nested-tree → neutral `FileManifest` without links, file-list fallback | `test_alldebrid_provider_contract.py` |
| One shared modal shell / coordinator, `ui-detail-candidates.js` no longer wraps the modal globals, one file-selection runtime + style owner | `test_ui_presentation_ownership_contract.py` |
| Browser selector: auto-open obeys `auto_offer` (no browser-derived window), tri-state tree, countdown from server deadline, stale-409 refresh, Close/X parity, Cancel Transfer routing; **browser sends `selection_mode=interactive` on magnet / bulk-magnet / torrent-file, not on direct-link** | `frontend/browser/file-selection.spec.js`, `frontend/browser/file-selection-submission-intent.spec.js`, `frontend/browser/details-candidates.spec.js` |

`backend/tests/two_provider_checkpoint_qualification.txt` now also composes the
seven `test_file_selection_*` production-path modules.

## Historical migration census

`LEGACY_TEST_MIGRATION.json` remains a traceability artifact for the earlier Universal Transfer cutover. Its historical stage labels do not define current ownership or current support. Real historical bugs keep their permanent regression owners even when the implementation layer that originally exposed them has been removed.
