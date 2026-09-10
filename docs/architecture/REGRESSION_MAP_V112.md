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
Universal Transfer Core owns ALL-vs-subset policy, the 60s/120s windows, durable
per-provider-resource selection generations, SQLite `BEGIN IMMEDIATE`
Confirm-vs-materialization serialization, fail-closed executable reconciliation,
and final `SourceEntry` filtering. The executor remains selection-blind. Items
12–16 remain intentionally deferred and this overlay does not change that.

The decision hold is established in the same durable transaction that queues any
auto-presented multi-file offer, whether the resource was initially `AVAILABLE`
or initially `PREPARING`: an automatically actionable offer never coexists with
immediate ALL materialization. `initially_available` remains a persisted
provenance fact but no longer gates the decision opportunity.

The 120-second hold is a **maximum unanswered-decision window, not a minimum
delay**. Confirm (`→ explicit`) and an active-hold Close (`→ all/closed`) settle
the decision and, in the same `BEGIN IMMEDIATE` transaction, release the
file-selection gate's scheduler `retry_at` on the owning request — the next
resolution cycle materialises immediately, without waiting the remaining deadline
or the last provider poll. `retry_at` is multi-purpose (gate wait + provider
backoff via `request_failure`); only the selection-induced component
(`state='waiting' AND error IS NULL`) is released, never a coexisting legitimate
backoff. `decision_deadline` is exposed only while the decision is pending.

| Current contract | Canonical regression owners |
| --- | --- |
| Neutral capability/identity, pure gate, reconciliation, no wall-clock in policy | `test_file_selection_contract.py`, `test_universal_contracts.py`, `test_universal_boundaries.py` |
| Fake-provider-driven cached & PREPARING-origin lifecycle, 60s/120s windows, PREPARING→AVAILABLE decision-window reproducer, **Confirm/Close release the gate wait without advancing the clock**, unanswered hold still waits the full window, provider-backoff isolation, restart survival, executor-boundary proof | `test_file_selection_lifecycle.py` |
| Additive schema, backup/wipe, FK integrity, per-resource generation across re-resolution, two-phase crash recovery, Confirm-vs-materialization concurrency, **Confirm/Dismiss retry_at release + idempotent self-heal + multi-cause isolation** | `test_file_selection_persistence.py` |
| Dedicated API, `SelectionOutcome` transport codes, fixed public whitelist, A→B re-resolution regression, durable browser event, **settled read model drops the active `decision_deadline`** | `test_file_selection_api.py` |
| AllDebrid adapter capability, `ready`-flag initial availability, nested-tree → neutral `FileManifest` without links, file-list fallback | `test_alldebrid_provider_contract.py` |
| One shared modal shell / coordinator, `ui-detail-candidates.js` no longer wraps the modal globals, one file-selection runtime + style owner | `test_ui_presentation_ownership_contract.py` |
| Browser selector: auto-open rules, tri-state tree, countdown from server deadline, stale-409 refresh, Close/X parity, Cancel Transfer routing | `frontend/browser/file-selection.spec.js`, `frontend/browser/details-candidates.spec.js` |

`backend/tests/two_provider_checkpoint_qualification.txt` now also composes the
four `test_file_selection_*` production-path modules.

## Historical migration census

`LEGACY_TEST_MIGRATION.json` remains a traceability artifact for the earlier Universal Transfer cutover. Its historical stage labels do not define current ownership or current support. Real historical bugs keep their permanent regression owners even when the implementation layer that originally exposed them has been removed.
