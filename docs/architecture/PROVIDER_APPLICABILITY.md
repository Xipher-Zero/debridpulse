# Provider Applicability and Deterministic Routing

The current v1.0.12 architecture has one provider-neutral path from HTTP(S) request structure to deterministic provider selection. Canonical applicability mechanics live in `backend/transfers/applicability.py`; `backend/transfers/registry.py` owns routing candidate construction and same-class selection; providers own the facts they expose.

## Ownership boundary

The applicability layer understands only canonical request structure and neutral provider facts:

- normalized URL scheme, hostname and port;
- static request types;
- enabled candidate descriptors;
- `SPECIALIZED` and `GENERIC` applicability;
- exact-host/domain-scope claims;
- opaque provider IDs.

It contains no provider-specific host lists, provider-native payload parsing, TTL/freshness policy, host availability, API endpoint semantics or provider-name routing branches.

Providers may expose a static `ProviderApplicability` or request-aware applicability through the neutral provider contract. Runtime-derived providers validate and interpret their own opaque state first, then emit only canonical applicability. The classifier and registry never read `ProviderRuntimeStateStore` payloads.

Routing performs no network I/O. It does not probe origins, contact provider APIs, refresh inventories or contact executors. Provider maintenance owns dynamic refresh independently of request submission and initial routing.

## URL applicability view

URL classification uses `urllib.parse.urlsplit` to derive a routing-only normalized view. Hostnames are parsed rather than substring-matched, trailing DNS root dots are normalized, DNS names are IDNA/case normalized, IP literals are canonicalized, and explicit ports remain separate from host matching. Userinfo is not hostname material. Malformed or hostless URL-shaped requests produce no URL provider match rather than crashing routing.

This parsing does not rewrite the endpoint that later reaches the provider/executor. Paths, percent encoding, query order and signed capability material remain candidate/execution concerns. A provider with native path/query semantics may evaluate them inside its own request-aware applicability source and expose only the resulting neutral claim.

## Applicability classes

`GENERIC` means a provider accepts a URL scheme generally. General HTTP & HTTPS declares `http` and `https` generic applicability.

`SPECIALIZED` means a provider explicitly claims the concrete resource relationship. AllDebrid derives such claims from its own validated host inventory and native regexp semantics inside `providers/alldebrid/`.

Exact-host/domain matching remains boundary-safe: lookalike suffixes do not match, and IP literals cannot accidentally participate in DNS suffix logic.

## Deterministic routing

`IntegrationRegistry.eligible_providers()`:

1. excludes disabled, explicitly unhealthy, capability-incompatible and request-type-incompatible providers;
2. obtains each surviving provider's neutral applicability;
3. classifies the request;
4. suppresses all matching `GENERIC` providers when at least one matching `SPECIALIZED` provider exists;
5. applies the existing neutral preferred-provider, descending-priority and stable-ID ordering within the surviving class.

The class decision is a filter, not provider-specific priority code. Registration/import/hash order is not policy.

If no provider survives, routing returns the canonical non-retryable `UNSUPPORTED_REQUEST` before provider resolution begins. Unsupported routing therefore creates no fake provider attempt, consumes no provider retry budget, starts no executor/aria2 work, opens no authentication interaction and performs no host refresh.

## Current production matrix

| AllDebrid | HTTP & HTTPS | AD-supported URL | Unrelated HTTP(S) URL |
| --- | --- | --- | --- |
| enabled | enabled | AllDebrid | HTTP & HTTPS |
| disabled | enabled | HTTP & HTTPS | HTTP & HTTPS |
| enabled | disabled | AllDebrid | unsupported |
| disabled | disabled | unsupported | unsupported |

These provider names are today's production participants; the universal rule remains specialized-set precedence over the generic set.

Once a specialized provider is selected, a later provider/execution failure does not automatically resurrect a generic provider. Production cross-provider failure fallback is not introduced by this routing rule.

## Runtime-derived claims

AllDebrid host maintenance retrieves and validates native host data, persists its LKG representation through the neutral runtime-state store and exposes neutral specialized applicability. Missing state produces no invented claim. A valid stale LKG can remain usable according to AllDebrid's provider-local policy while maintenance refreshes it. The router does not inspect timestamps, native availability, quota, native status or runtime-state bytes and never triggers refresh on submission.

Disabling a provider removes it from eligibility without deleting its retained provider runtime state.

Magnet/torrent capabilities remain static request-type declarations and do not acquire HTTP(S) specialized/generic precedence.

## Provenance and presentation

Durable provider/candidate/executor provenance is now part of the production architecture. Historical route identity is persisted at the attempt/execution boundary and is never inferred from current URL applicability. Recent Activity, Downloads and Details consume backend provenance projections; presentation does not classify or route.

Provider enablement is exposed through the Sources & Providers settings UI and round-trips through the canonical backend integration configuration. There is no frontend-only enablement mechanism.

## Security boundary

Applicability is not trust. A specialized match never bypasses DNS/egress protection, redirects policy, TLS/SNI verification, credential secrecy, signed-source sanitization, filesystem ownership or execution authorization.

See [MULTI_PROVIDER_HTTP_SLICE.md](MULTI_PROVIDER_HTTP_SLICE.md) for the complete converged ownership map and Item 11 end-to-end boundary.
