# v1.0.12 Two-Provider Canonical Development Architecture

Completed Roadmap Items 0–11 establish the current production slice. The early Stage 17/18 shim pass canonicalizes that slice before deferred Items 12–16 add more providers/protocols. This document describes the **current qualified two-provider development architecture**; it is not a final v1.0.12 release declaration.

## End-to-end flow

```text
submitted HTTP(S) resource
        ↓
canonical request admission / durable logical transfer
        ↓
enabled provider applicability
        ↓
SPECIALIZED vs GENERIC classification
        ↓
deterministic provider routing
        ↓
provider resolution
        ↓
canonical candidate
        ↓
executor selection
        ↓
aria2 acquisition
        ↓
INPUT_REQUIRED / AUTH_REQUIRED only after a genuine challenge
        ↓
same logical transfer continuation
        ↓
durable route/provider/executor provenance
        ↓
verified artifact delivery
        ↓
Recent Activity / Downloads / Details presentation
```

The universal path is provider-neutral. AllDebrid and General HTTP & HTTPS participate through the same provider interfaces and neutral applicability/routing structures.

## Canonical ownership map

### Request admission and logical transfer identity

`backend/transfers/engine.py` admits work and orchestrates the lifecycle. `backend/transfers/repository.py` owns durable transfer/request/artifact/attempt identity and persistence. Provider resolution, execution retries, authentication continuation, pause state and presentation do not create replacement logical transfers unless the normal lifecycle explicitly admits new work.

### Provider definitions, configuration and enablement

`backend/integrations/definition.py` defines the neutral integration contract. `backend/integrations/catalog.py` is the production composition catalog. `backend/integrations/configuration.py` owns backend integration settings and registry rebuilds. `IntegrationDescriptor.enabled` is the single routing enablement fact consumed by the registry; the Settings UI edits this canonical backend configuration rather than maintaining a frontend-only enablement state.

### Provider runtime state

`backend/integrations/runtime_state.py` owns neutral durable persistence mechanics, generations and neutral timestamps. Payload bytes, schema compatibility, validation, freshness/LKG policy and interpretation belong to the provider that wrote the state. Universal routing/classification never decodes provider runtime payloads.

### URL normalization and applicability

`backend/transfers/applicability.py` owns canonical URL applicability parsing and the neutral `SPECIALIZED` / `GENERIC` classification model. It understands URL structure, normalized host/domain matching and neutral claims; it does not contain AllDebrid hosts, native regexps, native availability, API semantics or runtime payload parsing.

### Provider routing

`backend/transfers/registry.py` owns provider eligibility and deterministic initial selection. Matching specialized providers suppress matching generic providers. Same-class selection then uses the existing neutral preferred-provider, priority and stable-ID policy. No eligible provider produces the canonical unsupported route before resolution/execution is begun.

Routing performs no provider refresh, destination probe or executor I/O.

### AllDebrid native host handling

`backend/providers/alldebrid/host_runtime.py` owns AllDebrid's native `v4.1/user/hosts` retrieval cadence, native payload validation, aliases/domains, native regexps, structural support, current availability/account facts, schema marker, LKG/freshness policy and translation into neutral applicability. The neutral runtime-state store persists the provider-owned bytes but never interprets them.

AllDebrid host inventory is maintenance-owned. Startup restores usable persisted state without a synchronous fetch, and ordinary URL/magnet/torrent admission and routing perform zero host-inventory refresh calls.

### General HTTP & HTTPS

`backend/providers/general_http/provider.py` owns generic `http`/`https` applicability and direct candidate creation. It advertises the neutral `username_password` input method on eligible direct candidates but does not interpret aria2 native authentication failures. `backend/executors/aria2/` detects the bounded HTTP authorization challenge and translates it into the neutral `AUTH_REQUIRED` contract. General HTTP contains no AllDebrid knowledge and does not decide specialized-provider precedence.

### Scheduling, capacity, retry, and execution

`backend/core/scheduler.py` owns application cadence/wake-up scheduling and invokes application commands; it does not decide provider/executor-native policy. `backend/transfers/engine.py` owns resolution/execution admission and durable capacity claims, while `backend/transfers/storage.py` supplies the disk-capacity guard. `backend/transfers/policy.py` owns universal retry/recovery decisions and budgets.

The registry selects an executor through neutral candidate/executor capability matching. `backend/executors/aria2/` owns aria2 invocation, observation, native execution-local options, and native error translation. The universal engine/repository retain logical execution authority, universal retry/lifecycle ownership, destination/artifact possession, and provider routing. aria2 does not decide cross-provider routing or the universal retry budget.

### Authentication lifecycle

The neutral input-required lifecycle is owned by `backend/transfers/input_required.py`, `backend/transfers/engine.py` and durable non-secret repository state. A General HTTP candidate is attempted unauthenticated first by aria2; only a genuine supported HTTP authorization failure on a candidate that explicitly advertises username/password input produces `INPUT_REQUIRED` / `AUTH_REQUIRED`. Submitted username/password values are transient executor input. Wrong credentials leave the same transfer waiting; corrected credentials continue that same transfer. Cancellation resolves the pending challenge and invalidates stale continuation.

The browser presentation is owned by `frontend/static/ui-auth-required.js` and its surrounding canonical UI state. The modal closes only after backend challenge resolution, not merely because credential submission returned HTTP success.

### Provenance

`backend/transfers/repository.py` owns durable route, candidate and executor provenance. Provider attempts and route transitions are append-only historical facts. The final delivering provider comes from the successful delivering execution relationship. Historical provider identity is never reconstructed from the current URL, current provider registry, or current applicability state.

### Filesystem / possession

`backend/transfers/filesystem.py` owns destination allocation, path containment, collision handling, partial retirement, and verified local possession. Providers and executors may supply or observe native facts, but neither can declare final DebridPulse possession merely from provider readiness, URL identity, or native success without the core's filesystem verification.

### Presentation

`backend/api/routes.py` projects safe provider/source/provenance facts for the frontend. The shared frontend provider presentation helper is consumed by Recent Activity and Downloads; Details consumes the same backend provider/provenance facts plus ordered route history. Provider labels come from integration definitions/catalog metadata; presentation does not classify, route, refresh provider state, or infer provider identity from URL/executor.

### Observability

`backend/application/observability.py` and the durable application-event paths own event delivery/projection. Notification/statistics services consume canonical lifecycle facts; they do not mutate transfer state or reinterpret provider/executor failures into new lifecycle authority.

### Database and migrations

`backend/db/database.py` owns canonical SQLite schema initialization. `backend/db/migrations/` owns supported durable upgrade translation, with integration-native migration decoding kept in the appropriate integration migration module. Runtime provider state and transfer provenance are distinct provider-neutral persistence domains.

## Legacy/generic service census

The remaining `backend/services/` package is retained only for legitimate cross-cutting application services; no hidden Universal Transfer/provider/executor lifecycle owner remains there.

| Module | Classification / retained responsibility |
| --- | --- |
| `backup.py`, `db_maintenance.py` | cross-cutting database backup/maintenance boundaries |
| `downloader_egress_guard.py`, `network_safety.py` | cross-cutting network/egress security enforcement |
| `duplicates.py` | shared duplicate/resource comparison support; not lifecycle ownership |
| `event_bus.py`, `notification_service.py`, `notifications.py` | observability/notification delivery |
| `extraction_safety.py` | cross-cutting archive/extraction safety support |
| `maintenance_gate.py` | application maintenance admission/synchronization support |
| `page_cache.py` | presentation/cache support |
| `stats.py` | statistics projection/aggregation |

The prior manager/gateway/coordinator lifecycle owners enumerated by `test_canonical_runtime_architecture.py` are physically absent. No module is moved merely to satisfy package aesthetics.

## Current qualified development routing matrix

| AllDebrid | HTTP & HTTPS | Structurally AD-supported URL | Unrelated HTTP(S) URL |
| --- | --- | --- | --- |
| enabled | enabled | AllDebrid | HTTP & HTTPS |
| disabled | enabled | HTTP & HTTPS | HTTP & HTTPS |
| enabled | disabled | AllDebrid | unsupported |
| disabled | disabled | unsupported | unsupported |

This table is an instance of the neutral rule, not a provider-specific branch: matching `SPECIALIZED` providers beat matching `GENERIC` providers after ordinary enablement/health/capability filtering.

## Runtime host-state flow

```text
AllDebrid native host state
        ↓
AllDebrid validation/normalization
        ↓
neutral runtime-state persistence of opaque provider bytes
        ↓
AllDebrid freshness/LKG interpretation
        ↓
AllDebrid request-aware structural applicability
        ↓
neutral SPECIALIZED claim
```

General HTTP & HTTPS independently contributes:

```text
http / https
        ↓
GENERIC applicability
```

Missing or unusable AllDebrid host state therefore does not create a fake specialized claim and does not prevent generic HTTP routing. Refresh failure with valid LKG keeps the previous valid provider state. Staleness and native availability remain provider-local facts; the classifier/router do not inspect them.

## Historical truth and settings changes

Provider enablement and current applicability affect new route selection only. Completed transfer history remains the route actually used even if:

- a provider is later disabled or enabled;
- an AllDebrid host snapshot later starts or stops claiming the same hostname;
- current provider priority/configuration changes;
- an integration implementation is no longer present and only its stable historical ID remains.

Legacy records without durable provider facts remain unknown rather than being guessed from the URL.

## Security boundaries

Applicability is not trust or authorization. Specialized routing never bypasses DNS/egress validation, redirect controls, TLS/SNI verification, signed-source sanitization, filesystem ownership, execution authorization or credential secrecy.

HTTP authentication secrets are transient. They must not enter ordinary transfer persistence, provider runtime state, provenance, candidate durable source fields, activity/events, diagnostics/logs, API responses or browser persistent storage. Safe presentation redacts URL userinfo, query/fragment capabilities and other sensitive source material according to the established sanitizer.

## Current support matrix

| Integration | Implemented current capability | Not implied |
| --- | --- | --- |
| AllDebrid | magnets, `.torrent` files, and HTTP(S) URLs structurally claimed by the provider's validated dynamic supported-host state | generic handling of every HTTP(S) URL; ownership of universal retry/lifecycle |
| General HTTP & HTTPS | generic direct `http`/`https` candidates; conventional HTTP resource username/password challenge continuation | FTP, SSH/SFTP/SCP, saved credentials, provider-specific retry policy |
| aria2 | current HTTP(S) execution/observation boundary for built-in or external mode | provider routing, universal retry budgets, logical transfer lifecycle |
| Generic Authentication Required UI | username/password and challenge-advertised username/private-key with optional passphrase | production SSH/SFTP/SCP support merely because key input can be represented |

FTP, SCP, SFTP/SSH, rsync, additional providers/executors/dependencies, and richer provider-routing/failover work remain deferred to Items 12–16/later roadmap decisions.

## Consolidation result

The current ownership audit found these production layers to be distinct canonical owners rather than staged duplicate implementations. No correct Universal Transfer lifecycle layer is rewritten merely for stylistic simplification. Superseded roadmap-transition wording is removed from current-state documentation, and the permanent `two_provider_checkpoint_qualification.txt` manifest composes canonical lower-level regression owners with cross-slice architecture/documentation tests instead of duplicating implementation logic.

This checkpoint does not introduce production AllDebrid→HTTP runtime-failure fallback, provider priority UI, manual routing override, saved credentials, or another transport/provider. The eventual full Stage 17/18 consolidation, dependency/license audit, and release qualification remain required after deferred provider/protocol work is implemented.
