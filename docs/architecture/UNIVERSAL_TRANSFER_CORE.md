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
| Lifecycle, dispatch, retry, control, recovery | `backend/transfers/engine.py` |
| Durable identity, attempts, intents, read models | `backend/transfers/repository.py` |
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

## Universal file-selection / manifest overlay (v1.0.12)

A capable provider may declare `Capability.FILE_MANIFEST` and report a neutral
`FileManifest` on `ProviderObservation` before the core commits that resource's
executable manifest. The core — not the provider or executor — owns every
selection decision: ALL-vs-explicit-subset policy, the 60-second automatic
presentation window, the 120-second decision hold (established in the same
durable transaction that queues any auto-presented multi-file offer, whether the
resource's initial observation was `AVAILABLE` or `PREPARING`, so an actionable
offer never coexists with immediate ALL materialization), durable
per-provider-resource selection generations, stale manifest rejection,
fail-closed executable-manifest reconciliation, and the
final `SourceEntry` filtering before child fan-out. Default policy remains ALL;
provider-side acquisition never waits on the browser. The Confirm-vs-
materialization race is serialized by durable SQLite (`BEGIN IMMEDIATE` on the
selection-generation row), never an in-memory lock. All timing derives from the
injected core clock with persisted absolute deadlines that survive restart.

The 120-second hold is a **maximum unanswered-decision window, not a minimum
delay**: it applies only while `decision == "pending"`. Confirm (`→ explicit`)
and an active-hold Close (`→ all/closed`) each settle the decision and, in the
*same* `BEGIN IMMEDIATE` transaction, release the scheduler `retry_at` the
file-selection gate scheduled on the owning request — so the next ordinary
resolution cycle materialises immediately rather than after the decision deadline
or the last provider poll. `retry_at` is multi-purpose (the gate wait, and also
provider-failure backoff via `request_failure`); the release targets only the
selection-induced component (`state='waiting' AND error IS NULL`), never a
coexisting legitimate backoff. `decision_deadline` is exposed only while the
decision is pending; the durable `hold_until` is retained as historical evidence.
No new scheduler or lifecycle state is introduced.

The executor still receives ordinary canonical candidates only and knows nothing
about file selection. See [FILE_SELECTION_MANIFEST.md](FILE_SELECTION_MANIFEST.md).
