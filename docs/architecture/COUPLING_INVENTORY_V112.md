# v1.0.12 coupling inventory

## Baseline and evidence

Repository: `Xipher-Zero/debridpulse`. Branch `1.0.12` was created directly from
verified `main` commit `61d5eec345c473532c46508f76c69a3d6b36747a` (`VERSION=1.0.11.1`).
The connected GitHub API reported successful Tests, Browser Runtime, CodeQL,
Container Security, Fork Image, and Release Promotion runs for that commit.
All 421 files were retrieved at that immutable revision and checked against
their Git blob hashes. No repository `AGENTS.md` was present in its complete tree.

This document records the starting architecture, not a claim that extraction is
complete. The final architecture and its acceptance evidence are separate deliverables.

Classification: **A** legitimate provider-local behavior; **B** universal behavior;
**C** integration semantics leaked into universal behavior; **D** compatibility or
migration dependency; **E** obsolete ownership or superseded implementation.
An entry can carry multiple classifications because a module mixes responsibilities.

## Behavioral dependency graph

HTTP handlers and scheduler loops import the global `TransferService`. It constructs
a `DirectLinkRetryGuardManager`, which inherits `DirectLinkResultGuardManager`,
`GuardedTransferIntegrityManager`, `TransferIntegrityManager`, and `TorrentManager`.
The root service then binds itself back into that engine. Engine methods select
between bound-service delegation and their own `_engine_*` fallback implementations.

ProviderGateway delegates provider operations back to this same engine, which
constructs AllDebridService. It therefore enumerates operations without removing
provider-specific lifecycle authority from the engine. Aria2Gateway, OwnershipLedger,
DispatchCoordinator, and ReconciliationService similarly call engine internals.

TransferControlService constructs RestartResumableTransferControlCoordinator, which
inherits MirrorAwareTransferControlCoordinator and TransferControlCoordinator.
The coordinator captures engine methods in `_orig_*` attributes. Mirror dispatch
substitutes another captured callable. The state machine replaces the coordinator's
parent-progress callback with its own aggregator. This makes the effective behavior
depend on construction order and inheritance, including distinct bound/unbound paths.

The architecture must be replaced at these ownership boundaries. Renaming classes
or introducing another wrapper would leave the dependency graph substantially intact.

## Responsibility and extraction map

| Current owner / path | Class | Actual behavior and required destination |
|---|---|---|
| `services/alldebrid.py`: request client | A | Bearer authentication, endpoint/version fallback, multipart upload, rate pacing, response decoding, delayed-link polling. Remain AllDebrid-local. Native failures must become normalized failures before returning to core. |
| `alldebrid.py`: `flatten_files` | A/C | Native `n/e/l/s` tree fields and URL validation. Translate to canonical candidates/manifests inside provider boundary; API and materializer must not flatten native trees. |
| `alldebrid.py`: torrent hash parser | B | BitTorrent metainfo parsing is request identity logic, not an AllDebrid client responsibility. Move to request parsing. |
| `services/rate_limit.py` | A/C | AllDebrid request pacing reads flat global provider settings. Provider implementation/configuration must own this pacing. |
| `manager_v2.py`: `ad`, `aria2`, `reset_services` | C/E | Core constructs concrete integrations and caches them. Replace with composition-root registration and injected contracts. |
| `transfer_service.py`: constructor and `_engine` forwarding | C/E | Service root wraps the inherited manager and binds it back into all services. Remove obsolete ownership after callers use canonical services. |
| `provider_gateway.py` | C/E | Native magnet methods, polling, imports, cleanup, submission, and testing forward into engine. Replace with capability registry and canonical provider operations. |
| `provider_gateway.py`: operation/quiescence/tombstones | B | Maintenance admission/drain and deletion authority belong to core lifecycle/maintenance, not a provider gateway. |
| `manager_v2.py`: normalization constants and `normalize_provider_state` | A/C | Interprets `statusCode`, ready=4, expired=3, failures=5..15 and native status messages. Move to AllDebrid state translation. |
| `manager_v2.py`: `_direct_link_unlock_failure_prefix` | C | Native `LINK_*` and message-text matching select source vs systemic failure. Replace with semantic normalized domain/responsibility/recovery fields. |
| `manager_v2.py`: `_retry_async` and direct-link unlock closures | B/C | Generic retry helper is called with AllDebrid exceptions and `LINK_DOWN` predicates. Core retry must consume explicit normalized retryability; native mapping stays provider-local. |
| Request submission: magnets/torrent files/direct links | B/C | Validation, duplicate gate, durable request creation, background tracking and Pause All deferral mix with provider uploads/native response interpretation. Split request admission from provider resolution. |
| `_persist_deferred_magnet`, `_persist_deferred_torrent_file`, `resume_deferred_provider_submissions` | B/D | Accepted work survives Pause All before provider contact. Preserve payloads and deterministic replay under canonical requests. |
| `_upsert`, `_add_magnet`, `_upload_torrent_file_provider` | B/C | Transfer rows are created/rewritten using native provider IDs/hash/status and upload results. Establish universal identity before attempts; attach provider resources separately. |
| `transfer_runtime_guard.py`: explicit same-hash reacquisition | B/C | Completed history does not prove local possession; explicit resubmission revives intended work. Preserve semantics without constructing provider operations in duplicate/history policy. |
| `services/duplicates.py` | B/C/D | Local duplicate policy uses infohash/title but also global AllDebrid-ID matching. Resource duplicate identity must be provider-scoped; retain title/history policy and explicit resubmit behavior. |
| Direct-link collection preparation | B/C | One logical collection contains multiple source outcomes and physical artifacts. Filename allocation, manifest persistence, accounting and queue admission are core; native unlock belongs to provider. |
| `direct_link_result_guard.py`: physical/source separation | B/E | Missing or pre-dispatch failures and standby mirrors are retained but excluded from physical progress. Move rule into canonical artifact/outcome accounting, not a post-hoc inherited override. |
| `dispatch_coordinator.py`: mirror planning | B/C | Cross-host candidate identity, bounded size tolerance, retained standby selection and one-copy policy. Keep core ownership; provider size/checksum availability are facts, not AllDebrid assumptions. |
| `direct_link_result_guard.py`: mirror failover | B/C | Native numeric aria2 codes and persisted text prefixes choose whether another source may help. Replace parsing with normalized failure/recovery semantics. Preserve partial-state retirement and standby ordering. |
| `direct_link_retry_guard.py` | B/C/E | Manual retry repairs the existing manifest, verifies completed siblings, preserves live executions and canonical paths/sidecars. Collapse into retry/artifact owners; remove inherited whole-collection alternative except legitimate no-manifest resolution path. |
| `transfer_integrity.py` | B/C/E | File possession requires directory visibility, no resumable sidecar, no-follow regular-file open, exact size, readable edges, and delayed revalidation. Keep core integrity policy; executor reports its resumable artifacts. Delete superseded base materializer. |
| `transfer_runtime_guard.py`: manifest destinations | B | Distinct source files must not map to a shared sanitized path. Preserve collision validation before materialization. |
| `transfer_runtime_guard.py`: locks, delete events, task cancellation | B | Delete wins against resolution, materialization, retries and finalization; preserve lock order and cleanup of resources created after cancellation. These are core lifecycle responsibilities. |
| `manager_v2.py`: provider polling/full sync/import | B/C | Core selects rows/transitions and interprets native bulk windows, IDs, statuses, and errors. Adapter must report normalized observations and snapshot completeness; core reconciles them. |
| Bulk snapshot and per-ID fallback | A/B/C | AllDebrid bulk inventory is incomplete; missing from bulk is not proof of deletion. Adapter must expose authoritative lookup/absence, preserving fallback and failed-lookup uncertainty. |
| `_apply_provider_update` | B/C | Native codes 3/5/7/8 control reimport, retries and cleanup; provider-ready 100% must not overwrite local progress. Separate provider observation from core transition/recovery policy. |
| `_handle_expired_reimport`, `_handle_upload_failed` | B/C | Re-resolution/retry, durable budget, Pause/Delete handling, synthesis of provider-compatible requests, and cleanup are entangled. Core owns attempts/budget; provider owns native recovery mechanics. |
| No-peer, stuck, orphan cleanup | B/C | Provider-native codes and message text drive automated deletion/restart. Normalize unavailability and resource facts; require explicit ownership/authority for cleanup. |
| `_delete_magnet_after_completion`, Delete | B/C | Automatic cleanup depends on local-creation provenance; explicit user-authorized provider deletion has different semantics. Model authority explicitly; provider performs native deletion only. |
| `services/aria2.py` | Executor-local | JSON-RPC, multicall, native GIDs/statuses/error codes, path/URI dedupe, timeout and native transport retry. Keep native protocol in executor; expose canonical execution observations/results. |
| Integrity/runtime aria2 subclasses | B/C/E | Stopped results must not satisfy fresh dispatch; guarded egress must apply to every job. Consolidate into one executor submission path with permanent regression coverage. |
| Engine `_aria2_job_options` and `_remote_aria2_path` | C | Native job option dictionaries and remote-daemon path translation must live in executor configuration/submission, not universal policy. |
| `aria2_gateway.py`, `ownership_ledger.py`, engine ownership table/cache | B/C/E | Shared-daemon mutation authorization is split across wrappers and manager. Core records authority; executor enforces it at native mutation boundary. Foreign jobs remain isolated. |
| `transfer_control.py`: strict confirm/pause/resume | B/C | Operator intent belongs to core; GID error-text matching/RPC pause/unpause and native confirmation belong to executor. Preserve fresh confirmation and uncertainty on connectivity loss. |
| `restart_resume_control.py` | B/C/E | Missing paused jobs, source URL retention, completed siblings and redispatch are core recovery concerns; native GID/sidecar checks belong to executor. Merge into canonical control/recovery. |
| `dispatch_coordinator.py` and engine queue methods | B/C/E | Slot accounting, priority, paused work, duplicate suppression and dispatch are shared among competing/captured methods. One scheduler/dispatcher must consume canonical executor capacity and observations. |
| `reconciliation_service.py` | B/C | Single-cycle snapshots and missing-job confirmation are valuable, but it branches on aria2 and interprets native states. Preserve bounded snapshot reuse through executor contracts. |
| `aria2_error_recovery.py` | B/C | Persisted retry budget is claimed before mutation, retry delay runs between cycles, and failed starts consume budget. Keep these universal guarantees; normalize eligibility and remove native reconstruction from policy. |
| `transfer_state_machine.py`, `torrent_state.py` | B/C | One derives parent status from native executor observations; the other is only a warning-based transition vocabulary. Establish one canonical lifecycle authority and artifact aggregation. |
| `transfer_repository.py` | B/C | Queries constrain physical work to `download_client='aria2'`. Persistence policy must operate on canonical artifacts/attempts independent of executor name. |
| `db/database.py`: schema | C/D | `torrents.alldebrid_id`, numeric provider status, native job ownership table and source-kind-overloaded fields couple persistence. Migrate required metadata into provider resources/resolution and execution attempts without losing active state. |
| `db/database.py`: historical compatibility | D | Legacy DB filename, old columns, pause-intent upgrade, and existing rows require deterministic migration. Keep only compatibility with an ongoing external/data obligation. |
| `core/config.py`, `config_validator.py` | C/D | Flat provider credentials/rate limit plus executor-specific defaults/restrictions. Introduce integration namespaces; translate existing settings safely. Secrets remain private. |
| `core/scheduler.py` | B/C | Universal loops call AllDebrid-named lifecycle work; concrete aria2 runtime housekeeping/restart is embedded in scheduler. Universal schedules invoke registered maintenance/capabilities. |
| `main.py`: startup | C | Startup selects AllDebrid IDs, starts concrete runtime, imports provider inventory and invokes aria2 recovery. Composition root may wire integrations; core startup must initialize without them. |
| `api/routes.py`: preview/retry/bulk retry | B/C | Preview parses native file trees; retry clears native IDs and rewrites lifecycle columns in HTTP handlers. Move policy into canonical application operations. |
| `api/routes.py`: provider/admin settings routes | A/D | Existing provider-specific paths can remain outward compatibility endpoints, implemented via registry/provider configuration. They must not carry universal lifecycle policy. |
| `api/routes.py`: aria2 runtime escape hatch | Executor-local/D | Explicit native runtime administration remains legitimate where it is clearly executor-specific and ownership-scoped. Universal API state must be normalized. |
| `api/serializers.py` | B/C | Protects URLs/magnets and separates provider readiness from local progress; directly imports native executor model. Preserve redaction and local progress through canonical presentation models. |
| `settings_validation_routes.py` | A/C/D | Concrete provider client constructed in HTTP validation. Route draft settings validation through the registered integration definition. Preserve draft isolation. |
| `services/notifications.py` | B/C/D | Shared notifications hard-code provider labels, IDs, queued state, retry descriptions. Render normalized events with integration display metadata. Preserve settings/webhook semantics. |
| `services/stats.py`, events and metrics | B/C/D | Aggregation mostly uses persisted universal outcomes but old names/metrics and source distinctions remain. Preserve counts and external metric compatibility while removing provider identity assumptions. |
| `extraction_service.py`, extractor/safety modules | B | Existing extraction service already provides a useful separate boundary. Core controls post-processing stages; extraction reports its own outcome. Do not turn extraction failure into provider failure. |
| Frontend app/settings/error semantics | C/D | Provider status, native failure text and AllDebrid settings are rendered in existing UI. Preserve presentation and settings behavior; consume canonical error/outcome fields at common UI boundary. |
| Tests | B/C/D/E | Many tests patch old manager modules and instantiate base/derived layers separately. Preserve behavioral assertions while migrating fixtures to final owners; obsolete structural expectations cannot freeze wrong ownership. |
| Documentation/build metadata | C/D | README and OCI descriptions frame the product as AllDebrid+aria2. Update final architecture/extension documentation and metadata after behavior converges; retain provenance/license notices. |

## Security boundaries that must survive

- `downloader_egress_guard.py` authorizes destinations at connection time; the
  early DNS check alone is insufficient. Preserve DNS rebinding race protection.
- `network_safety.py` validates public destinations; policy rejection must map
  to explicit non-retryable security semantics.
- aria2 jobs must retain `follow-torrent=false`, `follow-metalink=false`, redirect
  restrictions, original hostname verification/certificate validation/SNI, and
  the safe routing requirement in external mode.
- Shared/external aria2 policy distinguishes existing resources from resources
  DebridPulse may mutate. No foreign-job adoption, mutation, or daemon-global changes.
- Preserve path containment, collision checks, secure file access, archive
  extraction safety, diagnostic redaction, maintenance admission, and verified backups.

## Behavioral regression obligations

The existing tests cover direct links and collections, magnets/torrent metadata,
cached-ready fast paths, missing resources and incomplete inventory, deferred
submission under Pause All, concurrent deletion/materialization, retry identity,
mirror identity and failover, completed-history reacquisition, parent progress,
lost-job restart, durable pause intent, atomic retry budgets, explicit versus
automatic remote cleanup, extraction, notifications, authentication, settings,
and browser presentation. Those assertions must be retained at the correct owners.

Existing structural tests cannot by themselves establish provider independence:
the starting core imports concrete integrations, accepts native response objects,
and has multiple effective base/bound implementations. Required new evidence is
the actual lifecycle driven by deterministic fake provider/executor implementations,
with AllDebrid imports prohibited, plus migration, normalized failures, safe unknown
errors, sanitization, capability routing, attempt history, and dependency tests.

## Design constraints derived from this inventory

1. Requests, provider resources, logical artifacts/candidates and executions are
   different identities. Retained source outcomes are not automatically physical work.
2. Provider readiness and local possession are different facts. Neither cached
   status nor historical completion can authorize local success.
3. Inventory absence needs authoritative confirmation; transport failure must not
   masquerade as absence. Deletion tombstones override subsequent observations.
4. Retry must repair existing artifacts and preserve completed siblings. Alternative
   candidates must not combine partial bytes from unrelated sources.
5. Core policy interprets normalized errors, explicit retryability and recovery
   support. Native codes/messages are diagnostics only; unknown failures fail safely.
6. Cancellation/deletion authority, maintained lock order, admission gates and
   durable attempts are necessary to prevent duplicate or resurrected work.
7. A final contract modeled directly on magnet upload/status/files and aria2 RPC
   methods would reproduce the coupling. Contracts must describe core needs instead.
