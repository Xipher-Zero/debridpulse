# Universal transfer architecture

DebridPulse admits a durable transfer before contacting an integration. Providers
resolve requests into resources, manifests and transfer candidates. Executors
move candidate bytes to core-assigned paths. The transfer core owns lifecycle,
identity, scheduling, retry, reconciliation, local possession and cleanup policy.
Post-processors operate on verified local artifacts and report separate outcomes.

## Ownership and dependencies

| Responsibility | Canonical owner |
| --- | --- |
| Request parsing and fingerprints | `backend/transfers/requests.py` |
| Lifecycle/recovery/control decision authority (sole) | `backend/transfers/convergence_engine.py` (`TransferEngine`) |
| Provider-transition hard-stop enforcement, admitted-resource continuation | `backend/transfers/engine.py` (`TransferEngine`, base of `convergence_engine.TransferEngine`) |
| Neutral recovery/materialization mechanics (no decision authority) | `backend/transfers/_engine_recovery.py` |
| Universal admission, durable attempts, retry-policy application, cleanup orchestration | `backend/transfers/_engine_base.py` |
| Durable identity, attempts, intents, read models | `backend/transfers/repository.py`, `_repository_base.py` |
| Recovery-repository durable claim/lock/state transitions | `backend/transfers/recovery_repository.py` |
| Presentation projection (never a lifecycle decision) | `backend/transfers/presentation_repository.py` |
| Retry decisions and transitions | `backend/transfers/policy.py` |
| Paths, local possession, partial-file retirement | `backend/transfers/filesystem.py` |
| Candidate equivalence | `backend/transfers/mirrors.py` |
| URL parsing and provider-neutral applicability | `backend/transfers/applicability.py` |
| Capability registration and provider/executor routing | `backend/transfers/registry.py` |
| Models, errors and contracts | `backend/transfers/models.py`, `errors.py`, `contracts.py` |
| Integration definitions/configuration | `backend/integrations/definition.py`, `configuration.py` |
| Neutral provider runtime-state persistence | `backend/integrations/runtime_state.py` |
| Application commands and maintenance admission | `backend/application/service.py` |
| Production composition | `backend/application/composition.py`, `backend/integrations/catalog.py` |
| Independent work cadences | `backend/core/scheduler.py` |
| Neutral input-required broker/challenge lifecycle | `backend/transfers/input_required.py`, engine/repository challenge state |
| Browser Authentication Required interaction | `frontend/static/ui-auth-required.js` |
| Durable route/candidate/executor provenance | `backend/transfers/repository.py` |
| Safe transfer/provider presentation | `backend/api/routes.py`, canonical frontend presentation helpers |
| Browser and notification event delivery | `backend/application/observability.py` |
| AllDebrid native protocol, host-state interpretation and translation | `backend/providers/alldebrid/` |
| General HTTP(S) generic resolution | `backend/providers/general_http/` |
| aria2 protocol, execution, ownership checks and runtime administration | `backend/executors/aria2/` |
| Database schema/migrations | `backend/db/database.py`, `backend/db/migrations/` |
| Archive execution | `backend/postprocessors/archive/` |

The core imports contracts, never concrete integrations. Providers do not import
executors or the engine. Executors do not import providers or own transfer state.
HTTP lifecycle handlers invoke application commands; they do not rewrite native
identifiers or decide retry eligibility. Explicit integration administration
routes may expose integration-specific settings and actions, subject to ownership.

`ApplicationService` takes an engine with an injected registry. It contains no
concrete integration constructor. Production registration is a composition choice.
The deterministic parcel provider and memory executor in `backend/tests/fake_integrations.py`
exercise the actual engine, SQLite repository, scheduler calls and HTTP commands.

## Identity and persistence

A `TransferRequest` describes an input kind, opaque payload, optional name,
fingerprint and preferred provider. The stable `Transfer` ID belongs to DebridPulse.
It exists before resolution, survives retries, and is independent of remote IDs.
Manifests produce child request identities from their parent and relative path;
resolved children retain their artifact IDs and allocated paths across refresh.

Admission dedupes on the logical source fingerprint (`torrents.hash`, the active
unique key). `torrents.source_fingerprint` durably records the original logical
fingerprint. **Delete permanently retires the active dedupe identity**: in the
same transaction that tombstones the transfer, `hash` is replaced with a
deterministic, per-transfer, non-recursive tombstone
(`deleted:<transfer_id>:<source_fingerprint>`) while `source_fingerprint` keeps
the original value. A user-deleted transfer therefore never remains an automatic
recovery/dedupe target — re-submitting the same source creates a genuinely fresh
transfer (new transfer ID, root request, provider-resource generation, resolution
attempts, and file-selection generation), never a `retry(..., reacquire=True)` of
the deleted row. The historical row, its provider/resource/execution provenance,
outstanding cleanup responsibility, and its file-selection generations all stay
scoped to that historical transfer. An existing 1.0.12 database is brought to
this model by idempotent additive backfills in `db/database.py`
(`_retire_and_backfill_source_fingerprints`, `_backfill_provider_resource_bindings`);
`db/migrations/v112.py` is untouched. Completed-transfer re-acquisition and all
other non-deleted dedupe behavior are unchanged.

### Provider-resource binding generations

`ProviderResource.id` is the **canonical, transfer-independent** DP resource
identity (`R`), derived by the adapter from the neutral resource identity, not a
provider-native field, and never rewritten by core. The persisted binding row
carries two identities: `provider_resources.resource_key = R`, and
`provider_resources.id` = the **binding-generation id**
`UUIDv5("transfer-provider-resource:<transfer_id>:<R>")`, with
`UNIQUE(transfer_id, resource_key)`. Repository lookups resolve a binding by
`(transfer_id, R)`; pre-split rows whose primary key *is* `R` (`resource_key`
backfilled from `id`, historical primary key untouched) are matched by `id`.

Because a provider may hand back the *same* native resource for a re-upload,
`R` can repeat across transfers, but the binding-generation id cannot: transfer A
and transfer B binding the same `R` get `RA ≠ RB`, coexisting rows. Manifest,
entry and selection identities and `transfer_file_manifests/selections.provider_resource_id`
all key on the binding-generation id, so an identical native resource and an
identical file tree can never alias A's manifest/selection generation into B.
Inventory and observation still match on the stable canonical `R`, and then
resolve to the authoritative binding: `reconcile_inventory` maps `R` only through
*active* transfers, and every repository write path (`resource_observation`,
`cleanup_intent`, `pending_cleanup`, …) is transfer-scoped or resolves
`(transfer_id, R)` to the binding id. A historical deleted binding sharing `R`
is never chosen, updated, resurrected, or handed cleanup authority through the
canonical id. Two *live* (non-retired) transfers may never share one native
resource — that remains an `OWNERSHIP_CONFLICT` — but a retired predecessor
(deleted/cancelled/consolidated) sharing it is the ordinary delete/re-add case
and is allowed to coexist. `services/duplicates.py::find_resource_id_duplicate`
(the advisory duplicate-preview API) resolves the supplied identity against
`resource_key` and targets the current non-deleted binding.

### Provider cleanup fence for a re-add

A fresh re-add is admitted immediately even while the predecessor's provider
cleanup is still outstanding. Before the fresh generation's first
`provider.resolve()` (which could create/return the shared native resource), a
**provider cleanup fence** holds it as ordinary waiting/retry state — never
`Recovery failed`, never `INPUT_REQUIRED`, no new scheduler — while any provider
resource of a retired same-fingerprint predecessor still has
`cleanup_authority` set and is not `cleanup_abandoned`. That predicate blocks
pending, claimed-in-flight *and* scheduled-retry cleanup alike; it never infers
"finished" from the transient `cleanup_blocked` claim flag. `cleanup_abandoned`
is set only *after* a `provider.cleanup()` call has returned and retry policy has
permanently given up — so no operation is in flight at that moment — and it
releases the fence, so a fresh transfer is never deadlocked. A cleanup claim that
a restart interrupted (`cleanup_blocked=1`, not abandoned) cannot have an
operation still running; `engine.initialize()` releases it for the ordinary
cadence to re-drive to completion or terminal abandonment. Old executor cleanup
likewise stays bound to the predecessor's execution attempts, and the fresh
generation's provider-resource row is always its own — never re-homed from the
predecessor.

A provider returns a `ProviderResource` with a provider ID, core resource ID,
ownership and opaque context. Only that provider interprets its native context.
A `TransferCandidate` describes alternatives for one artifact: endpoints, expected
size, integrity metadata, expiration, provider identity and an optional refresh
request. A manifest describes multiple artifacts; alternatives describe different
ways to obtain the same artifact. These concepts are not interchangeable.

An executor's `prepare` operation allocates an opaque `ExecutionHandle` without
remote contact. Core stores it in an `ExecutionAttempt` before `start`. A lost
acknowledgement is reconciled using the same handle, not a fresh submission.
Resolution attempts are also recorded before contact. An interrupted submission
whose result is unknown requires evidence or operator action before resubmission.

The SQLite schema contains `transfer_requests`, `provider_resources`,
`resolution_attempts`, `execution_attempts`, `transfer_outcomes`, `transfer_controls`,
`postprocess_attempts`, `application_events` and the non-secret
`transfer_input_challenges` metadata table. Submitted authentication values are
process-local and are never part of that schema or integration runtime state.
Existing parent/artifact table
names (`torrents`, `download_files`) and numeric IDs remain part of the persisted
format. Native legacy columns are decoded by the v1 upgrade, not by runtime policy.

`db/migrations/v112.py` makes and verifies a pre-upgrade SQLite backup, adds the
canonical schema, translates legacy state in a transaction, checks foreign keys,
and records the migration marker only after success. Native decoding belongs to
each integration's migration module. External jobs without the historical durable
ownership record do not gain mutation authority during migration.

## Lifecycle and work scheduling

The `TransferState` enum is the authoritative lifecycle vocabulary. Its serialized
values preserve the supported UI/API states: accepted (`pending`), resolving
(`processing`), ready, queued, transferring (`downloading`), paused, input required
(`input_required`), verifying, post-processing (`extracting`), completed, failed
(`error`), cancelled and deleted. Provider resource states and executor states are
separate observations. `INPUT_REQUIRED` is nonterminal: it preserves the logical
transfer identity while a durable non-secret challenge waits for transient input.
See [INPUT_REQUIRED_LIFECYCLE.md](INPUT_REQUIRED_LIFECYCLE.md).

Provider preparation or cache readiness never means local download completion.
The engine derives local progress from selected physical artifacts. Failed source
requests and standby mirrors remain inspectable without entering that denominator.
Completed physical payloads can therefore finish with source warnings. A failed
physical artifact remains a transfer failure.

Resolution, execution observation, post-processing, event delivery, inventory and
integration maintenance run on independent cadences. New intake and resume wake
the appropriate loops. Core claims dispatch capacity durably before submission;
queued and unknown executions reserve capacity, while confirmed paused executions
do not. Resume uses the same capacity limit. Reducing the limit does not cancel
existing work. The local disk guard blocks new dispatch while active transfers
may finish.

Pause is durable intent. Intake during global pause is stored without provider
contact. A selected transfer can be resumed while other transfers retain their
pause intents. Pause arriving during an external start is reapplied to the accepted
execution. Delete records its tombstone before remote work; late resolution and
execution acknowledgements cannot resurrect it.

Retry repairs existing requests and artifacts. It retains confirmed live executions,
rechecks completed local payloads, keeps canonical paths, and opens an explicit
operator retry budget without deleting attempt history. Expired source resources
are confirmed and owned cleanup is performed before replacement. A refreshed
manifest that omits an expected child records a visible source failure.

## Contracts and routing

Providers implement `Provider.resolve` plus only the capabilities they advertise:
`ResourceLookup`, `Manifest`, `CandidateRefresh`, `Inventory`, `Cleanup` and `Health`.
A provider that can suspend for external input returns the neutral `InputRequirement`
and implements `resolve_with_input`; submitted values are delivered only to that
provider for the current challenge. The registry validates advertised protocol
implementations at registration.
Provider selection first filters by enabled state, registered health, supported request kind, and capability. URL-shaped requests then pass through the provider-neutral applicability classifier. The currently implemented initial rule is `SPECIALIZED > GENERIC`: when one or more specialized claims match, generic handlers are suppressed; when no specialized match survives, matching generic handlers remain eligible. Magnet/torrent request-type routes remain static. Only the surviving same-class provider set reaches the neutral optional preference, priority, then stable-ID ordering. Health affects routing when observations are registered with `mark_health`; selection itself does not make an implicit network call or refresh provider state.

In the current two-provider tree, AllDebrid translates its own validated runtime host facts into request-aware `SPECIALIZED` HTTP(S) claims while General HTTP & HTTPS contributes `GENERIC` `http`/`https` applicability. These are integration facts, not concrete provider-name branches in the classifier or core. This rule is the implemented initial routing policy for the current architecture; it does not pre-decide every later failover/selection policy for deferred providers.

Executors implement `prepare`, `start`, `observe`, `cancel` and `resumable_paths`.
An executor may return the same neutral `InputRequirement` from `prepare` before
external mutation and continue through `prepare_with_input`; executor credentials
remain independent of provider credentials. Optional protocols include `PauseResume`,
`BatchObservation`, `CandidateSampling` and `Health`. Selection uses supported
endpoint schemes, enabled state, registered
health and priority. A batch observation must account for every requested handle;
a failed or incomplete snapshot never proves absence. The aria2 boundary confirms
missing handles individually and does not adopt jobs by matching URL or path.

Mirrors require equal normalized names, positive known sizes and different source
keys in the same declared scope. Equal sizes are eligible by metadata. Different
sizes must fall within both 0.1% and 512 MiB, and matching bounded content samples
must establish the actual size. Unknown or unprovable relationships remain separate.
Failover retires the old execution and its partial state before another source
can write the same target. Local, security and unknown failures do not justify
cycling through alternate sources.

## Normalized failures and outcomes

`NormalizedError` separates domain, category, stage, retryability, recovery,
origin, permanence, operator action, retry delay, integration identity, native code
and sanitized diagnostic. The enum definitions in `transfers/errors.py` are the
canonical vocabulary; `message` is derived from the category.

Domains cover request, provider, resolution, executor, network, security, local
resource, integrity, lifecycle, reconciliation, cleanup, post-processing and
internal failures. Categories express concrete conditions such as authentication,
source absence, throttling, expired candidates, disk exhaustion, ownership conflict,
TLS identity failure and malformed adapter responses. Native codes terminate at
the integration translation boundary and never drive core policy or UI parsing.

`TransferPolicy` combines explicit retryability and recovery with attempt budgets
and deadlines. Unknown retryability and security failures never automatically
retry. Reauthentication or resource-change requirements need those external
conditions to change. Re-resolution and alternate selection require capabilities
and candidates that support the proposed action. Retry sleeps do not hold core locks.
Stall recovery uses durable progress activity and confirms execution cancellation
before authorizing a successor.

Unknown native codes produce an unmapped provider/executor category with useful
sanitized diagnostics and conservative recovery. Malformed responses produce an
adapter/protocol failure. Diagnostic sanitization removes supplied secrets,
capability URLs and credential patterns and bounds the retained text. Add new
native mappings in an integration translator, not in the engine or browser.

`TransferOutcome` distinguishes success, failure, cancellation, skip and observation.
Cancellation includes its initiator and is not automatically a failure. Paused,
queued, preparing and already-absent resources are observations. Cleanup failure
and post-processing failure do not become provider upload errors.

The canonical presentation model supplies errors and source outcomes to the API.
Durable activity events and notifications use the same messages. Browser failure
labels map canonical categories during normal rendering; they do not refetch
details or parse native messages to reconstruct failure meaning.

A transfer's effective processing presentation has ONE owner,
`transfers.presentation_repository.effective_presentation`. Both projections —
the comprehensive Details projection (`TransferRepository.presentation`) and the
bounded Downloads/Dashboard list read
(`api.operational_downloads.list_operational_torrents`) — call it with the same
logical inputs: the durable transfer status, the per-artifact child
presentations (each already produced by the shared `recovery_presentation`), the
pause / input-required signals, and the state of the *current* authoritative
root provider-resource binding. The bounded list projects only the raw
page-scoped facts it needs for those inputs (each artifact's status plus the
fields of its latest durable recovery snapshot) as a JSON array per transfer,
folded into the one bounded read — no per-row query, no comprehensive
per-transfer presentation call, constant DB round-trips. Because the same owner
sees the same facts, Dashboard, Downloads and Details cannot present a
contradictory processing truth: every early-return state (`completed`, `paused`,
`input_required`), every artifact-aggregated state (`downloading`, `recovering`,
each `_WAIT_PRESENTATION` `waiting_for_*`, `requires_attention`) and the raw
fallback surface identically.

Within that owner, presentation may narrow a generic pre-provider-work status to
a more specific processing truth without touching durable lifecycle state. When
a transfer's current authoritative root provider-resource binding reports
`PREPARING`, `transfers.presentation_repository.waiting_for_provider_override`
presents `waiting_for_provider` / "Waiting for provider" — the same presentation
status already used for provider-quiescence recovery, so no new contract or
badge is introduced. It is resolved from the current transfer-scoped binding
only, never a historical, tombstoned, or predecessor resource; it is applied
last and only against a generic `pending`/`processing` aggregate, so it *refines*
the generic presentation and never overwrites a more-specific one (`paused`,
`input_required`, `waiting_for_storage`, `requires_attention`, failure/recovery,
`completed`, …); and it clears as soon as that resource no longer reports
`PREPARING`.

## Ownership, local files and post-processing

Resources distinguish created, adopted and observed ownership. Automatic cleanup
requires positive ownership; explicit user cleanup carries its own authority.
Observed inventory never grants authority by itself. Cleanup intents and attempts
are durable, and unknown cleanup acknowledgement is not blindly repeated.

An execution handle must match the repository's authorization record. aria2 also
binds it to the daemon and filesystem mapping, checks the observed native target,
and refuses a colliding or foreign job. External/shared daemon global settings
remain read-only. Existing metadata, redirect, egress, DNS rebinding, certificate
and SNI controls live at the executor's network boundary.

Existing local data is adopted only with positive size, directory visibility,
absence of resumable sidecars, a no-follow regular-file open, exact size, readable
edges and delayed revalidation. Supplied integrity metadata must also match.
An empty file requires a fresh successful execution observation; an unknown-size
historical file does not establish possession.

Post-processors receive only verified artifact paths. Archive extraction retains
path containment, resource budgets and staged publication. Cleanup removes only
known successful archive inputs according to settings. Durable post-processing
claims prevent an interrupted non-idempotent operation from being repeated without
evidence. Extraction outcome is reported separately from transport completion.

## Configuration and extension

Each `IntegrationDefinition` supplies a stable ID, kind, name, options model,
factory, secret fields, legacy-field translations and fields that affect resource
ownership. `integrations.<id>` stores enabled state, priority and private options.
Existing flat settings are translated by definitions. Blank secret drafts preserve
saved secrets; explicit clears remove them. Public settings return configuration
flags, never credentials. Unknown plugin options remain private.

`transfer_policy` holds universal execution and resolution retry budgets, their
delays, independent concurrency limits, observation cadences and the stall timeout.
The previous flat retry and polling fields remain translated API/configuration
inputs; the production engine reads the universal policy. Credentials can be
rotated to restore authentication without discarding durable resource identity.

Configuration updates drain in-flight application operations before replacing
integration instances. Connection or path changes that would abandon live
execution/resource references are rejected before settings are saved. Ordinary
limits can be changed while preserving existing transfer identity and attempts.

To add a provider:

1. Create a package with an options model, provider implementation and native
   translation module. Return canonical requests/resources/candidates and errors.
2. Implement and advertise the supported capability protocols. Keep native IDs,
   cache codes, ticket formats and authentication inside opaque provider context.
3. Define an `IntegrationDefinition` and register it in the production catalog.
4. Test native translation, unknown failures, secret sanitization, capabilities,
   routing and the actual lifecycle with the provider injected into a registry.

To add an executor, implement the execution contract with prepared durable identity,
confirmed observation, scoped cancellation, safe destination handling and explicit
resumable paths. Register its schemes and options independently of providers.
Implement optional pause, batch observation or sampling only when supported.
Tests must cover ambiguous acknowledgements, ownership, cancellation and security.

Neither addition requires new provider-name branches in the lifecycle, scheduler,
retry/recovery policy, persistence read model or browser failure classifier. The
parcel and memory implementations provide small executable examples. Real-Debrid,
Premiumize, TorBox and other transports remain future integrations, not placeholder
implementations in the core.

The behavioral replacement census and qualification procedure are documented in
[REGRESSION_MAP_V112.md](REGRESSION_MAP_V112.md).

## Roadmap Item 9: durable route/provider provenance

Provider/resolution attempts and executor attempts now have durable provider-neutral provenance links. Historical provider identity is captured at route time, candidate identity is recorded without endpoint secrets, verified artifact delivery identifies the actual delivering execution/provider, and current routing/applicability state is never used to rewrite history. See `ROUTE_PROVIDER_PROVENANCE.md`.

## Post-audit ownership invariants (v1.0.12)

### Provider selection and retry

The universal core owns provider identity for a route attempt. Initial routing may select among eligible providers, but once selected, ordinary resolution retry and re-resolution stay bound to that provider. Adapter output may omit provider identity and be stamped by the core; contradictory provider identity is rejected before persistence. Ordinary retry never silently becomes cross-provider failover. Broad automatic production failover remains deferred to an explicit future route-transition policy.

### Cancellation serialization

Logical cancellation authority is committed on the parent transfer before remote executor cancellation is attempted. Once the parent is cancelled, later executor observations, reconciliation, completion, or materialization activity cannot revive it. Remote cancellation or cleanup errors are recorded as control-plane/cleanup outcomes and do not revoke the already-authoritative logical cancellation.

### Database startup and migration

Current-schema startup and historical migration are distinct owners. Normal repository initialization ensures the current schema required by runtime code; it does not reconstruct historical migration state. Supported predecessor upgrades are prepared and applied by the explicit v1.0.12 migration owner, including historical provenance backfill, with backup-before-current-mutation semantics. Migration helpers may live beside runtime repositories, but production migration invocation remains in `db/migrations/v112.py`.

Additive current-schema evolution — new columns and idempotent backfills for behavior that must work against an already-running 1.0.12 database — is owned by `db/database.py`, not `v112.py`. The deleted-transfer generation correction adds `torrents.source_fingerprint`, `provider_resources.resource_key`, and `provider_resources.cleanup_abandoned`. `_retire_and_backfill_source_fingerprints`: non-deleted rows gain `source_fingerprint = hash`, already-deleted legacy rows preserve the original fingerprint and have their `hash` retired to the tombstone form. `_backfill_provider_resource_bindings`: `resource_key = id` for existing rows (historical primary key untouched), plus the `UNIQUE(transfer_id, resource_key)` index. Repeated initialization is a no-op. Restoring an untouched pre-migration copy returns fully to the pre-migration state; the live production backup owned by `services/db_maintenance.py` is a separate mandatory deployment prerequisite taken immediately before the corrected image first starts against the real database.

## Canonical equivalence / lifecycle correction (v1.0.12)

Production transfer `263` (five equivalent Ubuntu ISO mirrors, two of which
transiently could not prove identity) exposed two coupled defects, corrected
together so a future protocol/provider cannot reintroduce either half alone.

**Proof-attempt budget is not identity truth.** `transfers/mirrors.py`
classifies equivalence-proof outcomes into `EvidenceKind` (a proof-strength
taxonomy: `unavailable` / `prefix_content_sample` / `full_content_sample` /
`resolver_attested` / `strong_integrity`) and `EvidenceFailureClass`
(`transient` / `contradictory` / `structural`, derived from the *reason* an
`UNAVAILABLE` proof failed). Transient reasons like `dns_failure`, `timeout`,
`sampler_unavailable`, `range_unsupported`, `incomplete_representation` never
establish independent payload identity, only that identity is currently
unresolved. `transfers/cohorts.py` (the equivalence-disposition owner) keeps
that evidence classification and the *decision state*
(`transfer_requests.equivalence_disposition`) strictly separate:
`EvidenceKind`/`EvidenceFailureClass` answer
"how strong is this proof," while `equivalence_disposition` answers "what has
this request's identity durably been decided to be." The disposition owner
consumes the evidence classification (via `EquivalenceEvidence.retryable`/
`.failure_class`) without duplicating it — evidence taxonomy and disposition
state are separate concerns held by separate modules.

`equivalence_disposition` values and what each authorizes:

| Disposition | Meaning | Authorizes a writer? |
| --- | --- | --- |
| `pending` | A bounded automatic proof retry is scheduled | No — held |
| `exhausted` | Automatic proof attempts stopped; identity still unresolved | No — held (never re-interpreted as independence) |
| `recovered` | Later evidence proved equivalence; attached | N/A — attached, not materialized |
| `released` | An earlier cohort-wide release (a genuinely independent/failed sibling disproved the weak-evidence collection hypothesis) | Yes |
| `independent` | Affirmatively structural/non-pairing evidence | Yes |
| `contradictory` | Affirmatively proven distinct (size/sample/integrity mismatch) | Yes |

Only the last three — each an *affirmative* decision, never mere absence of
proof — authorize `transfers._engine_recovery.TransferEngine._materialize`
to allocate an ordinary independent writer. A held (`exhausted`) request
remains in durable `MATERIALIZING` state; `coordinate_collection()` exits
immediately for it without repeating proof sampling (no automatic-proof hot
loop), while still being re-evaluated cheaply on every scheduler tick so a
later wake (new durable evidence, an explicit operator retry, or another
canonical event) can move it forward. The weak-evidence, multi-sibling
corroboration path (`PREFIX_CONTENT_SAMPLE`-only matches within one
submission cohort) never releases the whole cohort to independence merely
because one member's proof retries exhausted — only a genuinely
independent/contradictory sibling does that.

**Same-transfer canonical membership is durable through candidate binding/
origin provenance, not `artifact_consolidations`.** Same-transfer
convergence intentionally never writes an `artifact_consolidations` row
(that table is cross-transfer provenance only — see Roadmap Item 9 above).
`transfers/canonical.py`'s `CanonicalOwnership.durable_owner_for_request()`
is the one owner of "what canonical artifact, if any, does this request's
own candidate provenance durably attach to" — it derives the answer from
`canonical_candidate_origins.request_id` → `canonical_candidate_bindings` →
`canonical_artifact_id` (populated by `attach()` for both same- and
cross-transfer contributors), unioned with `artifact_consolidations` as an
*additional* valid cross-transfer mapping source. `transfers/cohorts.py`'s
own cohort coordination and `transfers/_repository_base.py`'s lifecycle
voting (below) both call this one helper rather than duplicating the SQL.

**Parent lifecycle reflects logical delivery obligations, not raw historical
row count.** `transfers/_repository_base.py`'s `canonical_artifact_membership_
sql()` predicate (Section 7's existing membership definition — blocked/
standby/non-request-bound rows never vote) is joined by a second, narrower
exclusion scoped to `aggregate_lifecycle()`'s FAILED/completion decision
only: a canonical-membership artifact in `error` state whose own request is
durably mapped (via the helper above) to a *different*, already-`completed`
canonical artifact does not cast a FAILED vote, and does not block the
parent from reaching `COMPLETED`. It remains fully visible in `presentation()`
for provenance and history — nothing is deleted or hidden — and a
genuinely independent failed artifact (no durable mapping to anything) still
votes exactly as before. This is why a superseded/historical equivalent
child can no longer poison a parent whose logical payload already delivered
through its canonical sibling — the exact production-`263` symptom
(`progress=100%, status=error` after the real canonical artifact had already
completed).

Regression coverage: `backend/tests/test_equivalence_retry_remediation.py`,
`test_multi_mirror_general_http_convergence.py` (`test_five_mirror_
production_263_regression` is the direct five-mirror analogue of transfer
`263`), `test_workspace4_cohort_exit_gate.py`, and
`test_operational_artifact_membership.py`.

## Canonical lifecycle / recovery / completion rework (v1.0.12, transfer 265)

Production transfer `265` proved three coupled defects, corrected together.
While one real artifact was actively transferring, its parent durably
alternated `downloading -> queued -> downloading -> queued`; a bad HTTP
mirror was persisted `completed, size_bytes=0, delivered=1`; and a durably
unresolved (proof-exhausted) sibling request left the parent stuck showing
`processing` after the real payload had already delivered.

**One parent-lifecycle semantic owner.** `transfers._repository_base
.TransferRepository.aggregate_lifecycle` is the sole owner of the durable
`torrents.status` decision. Two post-aggregate overrides used to run after
it committed, each independently free to overwrite that same column moments
later — both are now deleted, and their underlying facts folded into
`aggregate_lifecycle`'s own atomic read-decide-write instead:

- an artifact autonomously waiting on recovery (`recovery_wait`) is now one
  more state at the same precedence tier as `queued`/`paused`/
  `refresh_pending` — checked *after* the `downloading`/`verifying` branch,
  so a genuinely active sibling always still wins. This replaces the deleted
  `TransferRepository.force_queued_for_autonomous_wait()`, previously called
  unconditionally from `transfers._engine_recovery.TransferEngine._aggregate`
  regardless of what the base decision had just computed — the direct cause
  of the `downloading <-> queued` churn.
- durable paused truth (previously a separate crash/restart repair in
  `transfers.engine.TransferEngine._aggregate`, itself a second read-decide-
  write over the same column) is now decided inside the same transaction,
  from execution-attempt rows already read for the ordinary decision.

Neither `transfers._engine_recovery.TransferEngine` nor `transfers.engine
.TransferEngine` define an `_aggregate` override any longer; there is
exactly one `_aggregate` in the production MRO
(`transfers._engine_base.TransferEngine`), and it does nothing but call
`aggregate_lifecycle` and run the completion sequence when it says to.

**Quiescent equivalence hold is orthogonal to autonomous work, not a
special case of `materializing`.** `aggregate_lifecycle`'s "is there genuine
autonomous work pending" fact now excludes a `materializing` request whose
`equivalence_disposition` is durably held (`transfers.cohorts
._HELD_DISPOSITIONS`, i.e. `exhausted`) — imported by object identity, not
redefined, so the two call sites cannot silently drift into different
disposition sets. This is not a special case invented for this rework: it is
the SAME contract `transfers.cohorts.coordinate_collection` already
established and documents as the sole durable state that stops autonomous
materialization for a held request. That existing contract is provably
consistent everywhere a held request is touched:
- `_process_request` (`transfers._engine_base.TransferEngine`) routes every
  `materializing` row through `_materialize` -> `coordinate_collection` on
  every scheduler tick; `coordinate_collection` reads the identical
  disposition fact and returns immediately for a held row (no proof work, no
  writer) — `aggregate_lifecycle` reads the same fact, never a second
  interpretation of it.
- restart: both readers re-derive disposition fresh from the durable column
  on every pass; there is no in-memory state to lose, so a held row cannot
  silently resume autonomous work nor lose its hold across a restart
  (`test_restart_after_exhaustion_stays_quiescent_and_can_still_recover`).
- wake: the only writer of a held disposition is the bounded proof-retry
  budget in `cohorts._schedule_proof_retry`; the only path back out is an
  explicit operator retry or new proof evidence re-running the mapping —
  never an automatic scheduler tick.
- presentation: a held request never materializes an artifact
  (`coordinate_collection` returns before `super()._materialize` runs), so
  there is no artifact-level row for presentation to misrepresent as active;
  only the parent's own truthful `QUEUED` state surfaces the hold.

Excluding it from "pending" stops it from falsely sticking the parent
`RESOLVING` ("processing") while genuinely no autonomous work is scheduled.
It must NOT, however, be treated as license to complete: a durably held
request means identity remains UNRESOLVED, not proven equivalent, and
Section 6.4 requires completion only once every logical delivery obligation
is satisfied. A held request has no artifact and so can never appear in
`voting_artifacts`, which meant an earlier revision's completion check —
`artifacts and not pending and all(... for item in voting_artifacts)` — could
be satisfied purely by the artifacts that exist, silently completing the
parent around an unresolved identity claim exactly as if it had been proven
non-equivalent. `aggregate_lifecycle`'s completion condition now also
requires `not quiescent_hold`, so a real artifact finishing while a sibling
sits held settles the parent to the quiescent, nonterminal `QUEUED` wait
(Section 6.4's "quiescent unresolved hold") instead — truthful, not stuck
`RESOLVING` and not falsely `COMPLETED` — until the hold is later resolved
(new proof evidence) or released (e.g. an explicit operator action retiring
the ambiguous claim), at which point the ordinary completion check runs
again and can now succeed. The request's own `state` never changes; the
disposition column remains the sole durable "is this hold real" fact, read
directly by `aggregate_lifecycle` in the same transaction as everything
else.

The hold must equally never launder a genuine, INDEPENDENT terminal failure
belonging to a different voting artifact. A prior revision placed
`elif quiescent_hold: QUEUED` before the genuine-failure check in the
decision chain, so a real artifact's terminal `ERROR` sat masked forever
behind an unrelated sibling's unresolved hold, reporting truthless perpetual
`QUEUED` instead of `FAILED`. The failure/cancellation checks (`any(item
.state == "error" for item in voting_artifacts) or any(item.state ==
"failed" for item in requests)`, and the all-`cancelled` check) now run
BEFORE `elif quiescent_hold`, so a genuine failure or cancellation always
wins; the hold is the LOWEST-precedence fallback, applying only once every
other real fact (active, queued-ish, pending, failed, cancelled) has already
been ruled out.
`test_quiescent_hold_does_not_mask_an_independent_terminal_failure` proves
the FAILED outcome for that topology; the existing completed-artifact-plus-
hold test proves the QUEUED-not-COMPLETED outcome remains correct for its
own topology, confirming the fix distinguishes the two cases rather than
collapsing to one answer.

**Unknown size is distinct from known-zero — a real three-state model, not
a boolean.** `transfers.models.SizeKnowledge` (`UNKNOWN` / `KNOWN_ZERO` /
`KNOWN_POSITIVE`) is the canonical size-knowledge fact type.
`transfers.filesystem.size_knowledge(expected_bytes, observed_total, *,
affirmative_zero=False)` is the one resolver that produces it: a positive
expected size or executor-reported total resolves `KNOWN_POSITIVE`; `0`
from either source alone is never affirmative evidence and resolves
`UNKNOWN`; `KNOWN_ZERO` is reachable only through the explicit
`affirmative_zero` parameter, which a caller may set only from a genuine,
positively-confirmed zero-length signal (e.g. an HTTP response that itself
carried `Content-Length: 0`), never from a default or an omitted field. No
provider or executor currently wired into this codebase (General HTTP +
aria2, or AllDebrid) has that evidence — every one of them resolves a
reported size through a `value or 0`-shaped fallback that cannot
distinguish an explicit zero from a missing field — so every real call site
today passes `affirmative_zero=False` and can only ever observe `UNKNOWN` or
`KNOWN_POSITIVE`. This is a factual limitation of the current evidence
sources, proven by a static source-scan regression
(`test_general_http_and_aria2_never_pass_affirmative_zero`), not a policy
choice to forbid zero-byte payloads: the parameter exists so a future
provider/executor with a genuine affirmative-zero signal has one canonical
place to report it. `transfers._engine_base.TransferEngine
._execution_result`'s `SUCCEEDED` handling, and `transfers._engine_recovery
.TransferEngine._execution_result`'s mirror-size-refinement override, both
consume `size_knowledge`; when it resolves `UNKNOWN` the observation is
routed through the same verification-failure/recovery path an ordinary
payload mismatch already uses — never silently marked `completed`.
`known_positive_size` (the narrower known-positive-or-`None` resolver
`size_knowledge` is built on) remains available and unchanged.
`transfers.repository.TransferRepository.refine_execution_total` (a durable
size-truth sink) rejects a non-positive total for the same reason, matching
the pre-existing `accept_execution_total`'s stricter guard.

**One recovery/control-decision semantic owner — no alternate implementation,
refusal stub, or alias below the canonical owner, of ANY responsibility,
capable of mutating execution/recovery lifecycle.** The pre-Phase-3
`transfers.engine.TransferEngine` + `transfers.repository.TransferRepository`
composition (test-only; production via `application.composition.compose()`
always builds `convergence_engine.TransferEngine` + `recovery_repository
.TransferRepository`) previously retained its own full policy-driven
recovery-decision chain in `transfers._engine_recovery.py`, then its own
complete `_dispatch` readiness gating, `_wake_quiescent_recoveries`, and bulk
`pause_all`/`resume_all` — all deleted in earlier passes. A further Gate 9
revision found that a still-later pass had converted the remaining
duplicates (`pause`, `resume`, `pause_all`, `resume_all`, `retry`'s operator
path, `_refresh`, `_schedule_refresh`, `_recover_artifact` on
`_engine_base.TransferEngine`) into `raise NotImplementedError` stubs instead
of deleting them, and classified that as sufficient. It was rejected: "a dead
historical method is still architectural residue. It can be accidentally
filled back in, delegated to, or revived by a future refactor." The actual
requirement ("lower layers may contain neutral primitives only") is
satisfied only by absence, never by a stub, an alias, or a "safe because
shadowed" argument.

Every one of `pause`, `resume`, `pause_all`, `resume_all`, `retry`,
`_reacquire_transfer`, `_renew_source_parent`, `_refresh`,
`_schedule_refresh`, and `_recover_artifact` is now DELETED entirely from
`_engine_base.TransferEngine`, `_engine_recovery.TransferEngine`, and
`engine.TransferEngine` — none of the three defines any of these names —
and each exists exactly once, on `convergence_engine.TransferEngine`, the
sole canonical owner. This is checked directly against each class's own
`__dict__` (`name not in vars(cls)`, per class, never a global/merged
classification that could let a reintroduced method on one lower class hide
behind another lower class's clean state) by
`test_canonical_parent_lifecycle.py
::test_no_alternate_recovery_decision_implementation_below_canonical_owner`,
which also confirms `convergence_engine.TransferEngine` implements every one
of them. This removed the previously-load-bearing base implementation for
`engine.TransferEngine`-only tests (~23 tests across 9 files); each was
migrated to build the real production stack instead, via `canonical_core`/
`canonical_pair`/`canonical_p2`/`canonical_runtime`/`build_canonical_engine`-
style fixtures scoped to just those tests, or (for `test_universal_parity.py
::test_expired_resource_re_resolution_preserves_completed_sibling_and_paths`)
rewritten once migration exposed that its scenario depended on a
parent-resource-renewal mechanism that turns out to have NEVER been
reachable in production at all — see below.

`retry` now owns BOTH operator-initiated retry and terminal-transfer
reacquisition as two internal branches of the one canonical owner:
`reacquire=True` (the "resume tracking a transfer a duplicate submission
found already durably COMPLETED/DELETED" case — `submit()` reaches it only
for that specific dedupe outcome, never unconditionally, correcting an
earlier mischaracterization) dispatches to `_reacquire_transfer`, defined on
`convergence_engine.TransferEngine` itself, not inherited from below. Its
own helper, `_renew_source_parent` (manifest-member parent re-observation on
`RESOURCE_EXPIRED`), moved with it for the same reason: it mutates durable
transfer/artifact/execution state and is therefore semantic lifecycle
machinery, not a neutral primitive a lower class may own.

`_dispatch`, `_process_executions`, `initialize`, and `reconcile_executions`
remain genuine `super().<name>(` extensions (verified by source inspection,
not assumed) — ordinary OOP refinement, never a second authority.
`_recovery_context`, `_next_alternate_index`, `_candidate_provider_enabled`,
`_execution_result`'s mirror-size-refinement, and collection-affinity/
cohort-locked materialization mechanics remain in `_engine_recovery.py`,
absent from `convergence_engine.TransferEngine`'s own `vars()` and so
legitimately shared by inheritance, never duplicated by it. The structural
test audits the WHOLE MRO below the canonical owner (`_engine_recovery
.TransferEngine`, `engine.TransferEngine`, AND `_engine_base.TransferEngine`
itself) and fails on any shadowed name landing in neither the delegates set
nor the proven-absent semantic-name set — there is no third, silently-omitted
category, and there is no longer a concept of "verified pure refusal": there
is nothing left below the canonical owner to refuse.

Two real bugs surfaced by this closure, fixed rather than left as residual
risk once the (masking) base fallback was removed:
- **A genuine `pause_all`/`resume_all` concurrency race, found and fixed
  before the method was ultimately deleted.** While the base implementation
  still existed (an intermediate revision of this rework), replacing its
  prior `asyncio.gather`-based concurrency with a serial loop (never
  exercised by production, which always ran `convergence_engine
  .TransferEngine`'s own serial version instead) surfaced that concurrent
  per-transfer `_control` calls could claim the same `max_active_executions`
  slot count inconsistently (`test_resume_all_obeys_capacity_and_releases_
  parked_successors`, flaky ~35% of runs once exercised). The base method no
  longer exists at all — see above — but the underlying capacity-slot race
  this discovered is recorded here as history; the canonical owner's own
  `pause_all`/`resume_all` were already serial and are unaffected.
- **`convergence_engine.TransferEngine._refresh_claimed`'s hard-failure
  branches never triggered re-resolution**, leaving an artifact stuck in
  `refresh_pending` forever with no further progress or wake mechanism once
  the resource genuinely could not be refreshed. This was a pre-existing gap
  in the canonical owner itself — never reachable through the deleted base
  fallback either, since `convergence_engine.TransferEngine._refresh` has
  always fully replaced (never delegated to) the base implementation, so it
  predates this whole rework and was simply never exercised by any test
  built on the canonical stack. Fixed narrowly: `_refresh_claimed` now
  returns the real `NormalizedError` for its `refresh_failed` reason, and
  `_plan_after_reconcile` routes that error through the ordinary
  `_decision_step`/`policy.recover` cycle instead of leaving the artifact
  inert — letting it retry, switch candidates, or (once exhausted) fail
  cleanly, exactly like any other execution failure. This does NOT restore
  the old base-only mechanism's specific "silently re-observe the parent
  resource for fresh per-member candidates" behavior (manifest-member
  parent-renewal-on-refresh-failure was, on inspection, never reachable in
  production either — the retired test now documents this and asserts the
  real, current, honest outcome instead: a genuinely expired resource with
  no alternate candidate fails the artifact cleanly without corrupting an
  already-completed sibling).

Roughly four dozen test files that build the pre-Phase-3 composition for
unrelated concerns (file selection, applicability, provider routing,
HTTP-stage coverage) needed no change at all, since none of them exercise
recovery-decision, dispatch-readiness, or bulk pause/resume/retry/refresh
logic.

Final owner map for this rework:

| Responsibility | Owner |
| --- | --- |
| Parent lifecycle decision (sole) | `_repository_base.TransferRepository.aggregate_lifecycle` |
| Recovery decision/application (executor-affecting, sole) | `convergence_engine.TransferEngine` (claim-fenced); no lower class retains an alternate implementation |
| Size-knowledge resolution | `transfers.filesystem.size_knowledge` (`transfers.models.SizeKnowledge`) |
| Equivalence-hold production | `transfers.cohorts` (`_HELD_DISPOSITIONS`, unchanged) |
| Equivalence-hold consumption (parent truth) | `_repository_base.TransferRepository.aggregate_lifecycle` (same `_HELD_DISPOSITIONS` object) |

Regression coverage: `backend/tests/test_canonical_parent_lifecycle.py`
(including the full-MRO, auto-derived shadowed-method structural proof),
`test_canonical_completion_truth.py`,
`test_equivalence_retry_remediation.py`
(`test_quiescent_equivalence_hold_blocks_completion_until_resolved_or_
released` — inverted from an earlier revision that wrongly asserted
completion won regardless of the hold — `test_quiescent_hold_does_not_mask_
an_independent_terminal_failure`, `test_quiescent_equivalence_hold_
with_no_artifacts_is_queued_not_perpetually_resolving`,
`test_restart_after_exhaustion_stays_quiescent_and_can_still_recover`),
`test_universal_hardening.py`
(`test_unknown_size_zero_byte_success_never_completes`),
`test_multi_mirror_general_http_convergence.py`
(`test_real_aria2_zero_byte_success_never_completes_or_delivers`, a real
GeneralHttpProvider + real Aria2Executor reproduction), and the migrated
canonical-stack tests in `test_universal_lifecycle.py`,
`test_universal_parity.py`, `test_universal_hardening.py`,
`test_pause_resume_recovery.py`, `test_ws2p1_failover_depth.py`,
`test_ws2p1_failover_progress.py`, `test_ws2p1_completion_isolation.py`,
`test_cross_transfer_equivalence.py`, `test_candidate_provenance_
consolidation.py`, `test_details_candidate_presentation.py`,
`test_transfer_recovery_phase2.py` (dispatch-readiness/quiescent-wake
coverage migrated to a `canonical_runtime` fixture), `test_application_
runtime.py`, `test_input_required_lifecycle.py`, `test_pause_lifecycle_
convergence.py`, and `test_transfer_preparing_presentation.py` (bulk pause/
resume/retry/refresh coverage migrated to the canonical stack once
`_engine_base.TransferEngine` stopped providing a working alternative).

## Universal file-selection / manifest overlay (v1.0.12)

A capable provider may declare `Capability.FILE_MANIFEST` and report a neutral
`FileManifest` on `ProviderObservation` before the core commits that resource's
executable manifest. The core — not the provider or executor — owns every
selection decision. **Provider preparation, user file-selection authorization,
and executor dispatch are three separate lifecycle dimensions**: provider
preparation is eager and independent of executor capacity; user decision time
begins only when there is an actionable multi-file manifest to decide on;
executor capacity matters only when executable work is ready to dispatch.

Core owns: ALL-vs-explicit-subset policy; the 120-second **user-decision hold**,
anchored exactly once to the first actionable multi-file manifest (whether the
resource is `PREPARING` or `AVAILABLE` at that moment, and however long provider
preparation has taken — there is no submission-relative cutoff), so an actionable
offer never coexists with immediate ALL materialization; the bounded 60-second
**post-`AVAILABLE` manifest-acquisition grace** (only when an `AVAILABLE`
resource still cannot supply a usable manifest — never while `PREPARING`);
durable per-provider-resource selection generations; stale manifest rejection;
fail-closed executable-manifest reconciliation; and the final `SourceEntry`
filtering before child fan-out. Default policy remains ALL. `selection_mode`
gates only whether a *new* selection generation is created (never inferred from
browser presence); once a durable generation exists for a `(request, binding)`
it is authoritative for the rest of that generation's life regardless of the
request's current/defaulted `selection_mode` — a database that predates
`selection_mode` keeps every existing PENDING hold, EXPLICIT subset, and
PREPARING selection opportunity. The Confirm-vs-materialization race is
serialized by durable SQLite (`BEGIN IMMEDIATE` on the selection-generation row),
never an in-memory lock. All timing derives from the injected core clock with
persisted absolute deadlines that survive restart without resetting or
extending.

The 120-second hold is a **maximum unanswered-decision window, not a minimum
delay**: it applies only while `decision == "pending"`. Gate authority and the
scheduling of the wait it produces are one `BEGIN IMMEDIATE` transaction
(`repository.file_selection_gate`), so a stale `WAIT` can never recreate
`retry_at` after Confirm/Close/timeout has settled the decision — a concurrent
settle either commits first (the gate then sees `EXPLICIT`/`ALL` and schedules
nothing) or blocks on the row lock and then releases the wait itself. Confirm
(`→ explicit`) and an active-hold Close (`→ all/closed`) each settle the decision
and, in the *same* transaction, release the selection `retry_at` the gate
scheduled. `retry_at` is multi-purpose (the gate wait, and also provider-failure
backoff via `request_failure`); both the release and the atomic gate reschedule
target only the selection-induced component (`state='waiting' AND error IS
NULL`), never a coexisting legitimate backoff. `decision_deadline` is exposed
only while the decision is pending; the durable `hold_until` is retained as
historical evidence. No new scheduler, lifecycle state, or file-selection worker
is introduced.

The executor still receives ordinary canonical candidates only and knows nothing
about file selection. See [FILE_SELECTION_MANIFEST.md](FILE_SELECTION_MANIFEST.md).
