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
