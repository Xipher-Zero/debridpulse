# Durable Route & Provider Provenance

Roadmap Item 9 makes provider/acquisition history a canonical durable fact without changing logical transfer identity or adding automatic cross-provider failover policy.

## Identity and ownership

- `torrents.id` remains the logical transfer identity. Provider identity is never embedded in it.
- `resolution_attempts.id` remains the provider/resolution attempt identity. `route_attempt_provenance` adds deterministic per-request ordering, transition linkage, normalized transition reason, safe candidate identities, and acquisition-route outcome.
- `execution_attempts.id` remains executor-attempt identity. `execution_attempt_provenance` links it to the provider route and selected candidate without persisting endpoint capability data in provenance.
- A verified artifact delivery is recorded only when canonical artifact verification marks a file `completed` while an execution attempt owns it. Partial bytes do not establish delivery.

## Historical truth

Provider identity is persisted when routing/acquisition occurs and is never reconstructed later from the submitted URL, current provider enablement, current applicability, or current AllDebrid host state. Pre-Item-9 rows are backfilled only from durable resolution/candidate/execution facts. If a provider cannot be proven, provenance remains unknown rather than being guessed.

A provider transition is represented as:

```text
Logical Transfer
  -> Provider A route attempt -- failed/superseded
  -> Provider B route attempt -- completed
```

Both attempts retain the same logical transfer identity. Candidate changes within one route and executor retries beneath one candidate remain distinct from provider changes.

## Safe candidate provenance

Provenance stores candidate IDs, stable provider IDs, candidate ordering, and safe `SourceIdentity` metadata when supplied. It does not copy endpoint URLs, headers, signed query strings, credentials, API keys, authentication challenges, or provider-native payloads into the provenance tables/API history.

## Delivering provider

The completed-provider projection is derived from the execution attempt that passed canonical artifact verification. It is not the first provider, latest enabled provider, current classifier winner, or a hostname guess. Historical route attempts remain available through transfer detail even when the summary projects only the delivering provider.

## Scope

Item 9 does not add general automatic cross-provider failover policy and does not add the later provenance timeline/dashboard/badge/filter UI. It makes those later capabilities safe because their data source is durable history rather than reconstruction.

## Item 10 presentation contract

Roadmap Item 10 projects this durable history into the normal UI without reconstructing it. Recent Activity and Downloads use the current durable route for active transfers and the verified delivering provider for completed transfers. Details presents the safe original resource separately from the provider route and keeps route attempts in the Item 9 durable order.

Provider labels come from integration definitions. If a historical provider is no longer registered, the stable provider ID remains in the API while the normal UI falls back to a neutral unknown label. Current enablement, current applicability, current AllDebrid host data, executor identity, and the submitted URL never rewrite historical provenance.

The Settings Sources & Providers controls update the canonical integration `enabled` state. AllDebrid and General HTTP(S) do not have parallel frontend enablement flags.

## Post-audit retry isolation (v1.0.12)

Initial routing and ordinary retry are separate decisions. A new logical route uses the neutral provider-selection policy: enabled SPECIALIZED applicability wins over GENERIC applicability, then the normal same-class selection policy applies. Once that route has selected a provider, ordinary resolution retry and re-resolution remain bound to that selected provider. Provider enablement, health, priority, or dynamic host-applicability changes do not silently reopen the global provider set for an existing route.

Automatic cross-provider production failover is deferred. A future explicit failover mechanism may create a new provider route attempt and append truthful provenance such as Provider A failed -> Provider B completed, but ordinary retry is not that mechanism. Provider identity recorded on route, candidate, artifact, and execution provenance is durable historical truth and is never reconstructed later from the submitted URL or current applicability state.

## Torrent cache fact and Route History identity (v1.0.12)

Route History's middle value is the **logical source route**, not whichever URL an executor was handed. Two neutral durable facts make that possible without the browser (or generic core presentation) knowing any provider, hostname, or cache semantics.

### Canonical torrent cache fact

- Provider-native cache evidence is translated **once, at the provider boundary**, into the neutral `transfers.models.CachePresence` — `HIT`, `MISS` or `UNKNOWN` — carried as `ProviderObservation.cache_presence`. It is deliberately not a boolean. `HIT`/`MISS` mean the provider authoritatively said, at that observation, that the torrent was / was not already available in its cache; `UNKNOWN` means it gave no trustworthy fact.
- AllDebrid documents the `ready` boolean returned by `POST /v4/magnet/upload` and `/v4/magnet/upload/file` as "already available". `providers.alldebrid.translation.cache_presence_from_upload` maps exactly `True` → `HIT`, exactly `False` → `MISS`, anything else → `UNKNOWN`, and only for upload responses. It is never inferred from a later `statusCode == 4`, `ResourceState.AVAILABLE`, speed, completion time, files/links, or an endpoint hostname.
- Cache presence is **orthogonal to provider resource readiness**. `ResourceState` keeps its existing meaning (`AVAILABLE`: usable now; `PREPARING`: not yet) and is translated independently from the same native field. A `MISS` that later becomes `AVAILABLE` stays a `MISS`.
- The fact is persisted in the existing `resolution_attempts.result` JSON (the durable `ResolutionResult`, `observation.cache_presence`). There is no side store and no schema migration. A row written before this fact existed has no field and decodes as `UNKNOWN` (`transfers.codec.cache_presence`); it is never migrated into a guess.
- **Current 1.0.12 routing does not use cache presence.** Provider selection is unchanged (`IntegrationRegistry.provider_for` performs no I/O, and a provider that reports a `HIT` does not prefer itself). A future multi-provider availability/preflight policy may consume the same neutral fact through one core routing-policy owner; providers only ever emit facts.

### Route History identity rules

`transfers._repository_base._project_route_history` is the single presentation owner. It reads only the exact historical `resolution_attempts.result` of each route attempt plus the transfer's durable request lineage.

| Route | `route_identity` |
| --- | --- |
| Ordinary native/direct route (for example General HTTP(S), or future FTP/SFTP/SCP) | Safe endpoint origin, or the safe path-bearing location when several routes share one origin — unchanged |
| Provider-mediated hoster link (candidate `delivery == PROVIDER_ISSUED`) | The upstream host durably attested by the candidate's own `SourceIdentity("host", …)`, normalized by `core.presentation_safety.safe_public_host`; unknown (`—`) when no safe host is provable |
| Any route descended from a magnet/torrent root | `Torrent cache` when the root acquisition's first authoritative cache observation on that provider is a `HIT`, otherwise `BitTorrent` |

- A provider-issued delivery endpoint (`TransferCandidate.delivery == DeliveryKind.PROVIDER_ISSUED`) is an execution capability, not the logical source. It is never the row's identity or hover title (`route_origin` and `route_location` are empty for these rows). Candidates persisted before this fact existed decode as `DIRECT`, so their presentation is left as it was rather than guessed from the endpoint or provider.
- Lineage, not child request kind, decides BitTorrent: a provider-generated HTTP(S) descendant belongs to its magnet/torrent root by durable `parent_id` lineage. Source class and cache fact are computed per root lineage, so a multi-root transfer never reads "the first request".
- The cache label is the **first authoritative (`HIT`/`MISS`) observation** recorded on the root request itself for that provider, in durable route order — never the latest state. `UNKNOWN` never establishes a fact, and a `HIT` reported by another provider does not relabel this provider's route.
- Ambiguity fails closed: an unprovable lineage, a missing/malformed source host, or several candidates never falls back to filenames, URLs, provider names, or endpoint domains.
- `frontend/static/app.js::renderRouteHistory` stays a thin projector of `route_identity` / `route_location`; it holds no provider, domain, cache, or request-kind logic.
