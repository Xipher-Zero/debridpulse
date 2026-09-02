# Multi-Provider HTTP(S) Slice — Canonical Architecture

Roadmap Item 11 qualifies the converged Items 1–10 architecture as one production slice. This document describes the current ownership boundaries; it is not a roadmap-transition document.

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

`backend/providers/general_http/provider.py` owns generic `http`/`https` applicability, direct candidate creation and provider-level HTTP authentication semantics. It contains no AllDebrid knowledge and does not decide specialized-provider precedence.

### Execution

The registry selects an executor through neutral candidate/executor capability matching. `backend/executors/aria2/` owns aria2 invocation and observation. The universal engine/repository retain execution authority, retry/lifecycle ownership and artifact possession.

### Authentication lifecycle

The neutral input-required lifecycle is owned by `backend/transfers/input_required.py`, `backend/transfers/engine.py` and durable non-secret repository state. A General HTTP transfer is attempted unauthenticated first; only a genuine supported HTTP authorization challenge produces `INPUT_REQUIRED` / `AUTH_REQUIRED`. Submitted username/password values are transient execution input. Wrong credentials leave the same transfer waiting; corrected credentials continue that same transfer. Cancellation resolves the pending challenge and invalidates stale continuation.

The browser presentation is owned by `frontend/static/ui-auth-required.js` and its surrounding canonical UI state. The modal closes only after backend challenge resolution, not merely because credential submission returned HTTP success.

### Provenance

`backend/transfers/repository.py` owns durable route, candidate and executor provenance. Provider attempts and route transitions are append-only historical facts. The final delivering provider comes from the successful delivering execution relationship. Historical provider identity is never reconstructed from the current URL, current provider registry, or current applicability state.

### Presentation

`backend/api/routes.py` projects safe provider/source/provenance facts for the frontend. Recent Activity, Downloads and Details consume those durable facts. Provider labels come from integration definitions/catalog metadata; presentation does not classify, route, refresh provider state, or infer provider identity from URL/executor.

## Current production routing matrix

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

## Consolidation result

The Item 11 ownership audit found the production layers above to be distinct canonical owners rather than staged duplicate implementations. No qualified Universal Transfer Core lifecycle layer is collapsed merely for stylistic simplification. Superseded roadmap-transition wording is removed from architecture documentation, and the permanent Item 11 qualification manifest composes the canonical lower-level regression owners with cross-slice tests instead of duplicating their implementation logic.

Item 11 does not introduce production AllDebrid→HTTP runtime-failure fallback, provider priority UI, manual routing override, saved credentials or another transport/provider.
