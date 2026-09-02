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
| Capability registration and routing | `backend/transfers/registry.py` |
| Models, errors and contracts | `backend/transfers/models.py`, `errors.py`, `contracts.py` |
| Application commands and maintenance admission | `backend/application/service.py` |
| Production composition | `backend/application/composition.py`, `backend/integrations/catalog.py` |
| Independent work cadences | `backend/core/scheduler.py` |
| Browser and notification event delivery | `backend/application/observability.py` |
| AllDebrid protocol and translation | `backend/providers/alldebrid/` |
| aria2 protocol, ownership checks and runtime administration | `backend/executors/aria2/` |
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
Provider selection considers enabled state, registered health, supported request
kind, capability, optional preference, priority, then stable ID ordering. Health
affects routing when observations are registered with `mark_health`; selection
itself does not make an implicit network call.

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
