# Provider applicability and deterministic initial routing

Roadmap Items 6–8 establish one provider-neutral path from request structure to deterministic initial provider selection. The canonical applicability mechanics live in `backend/transfers/applicability.py`; `backend/transfers/registry.py` owns routing candidate construction and same-class provider ordering; the universal transfer engine remains the orchestrator.

## Ownership boundary

The applicability classifier owns mechanics, not provider knowledge. It understands canonical request structure, URL scheme/hostname/port, static request types, enabled candidate facts, `SPECIALIZED` and `GENERIC` URL applicability, exact-host/domain-scope matching, and opaque provider IDs. It contains no provider-specific host lists, provider-native payload parsing, provider TTL policy, or provider-name branches.

Providers own what they claim. A provider may expose a canonical `ProviderApplicability` snapshot through `ApplicabilitySource` or request-aware canonical applicability through `RequestApplicabilitySource`. Runtime-derived providers interpret and validate their own opaque state first, including native structural support and freshness policy, then expose only canonical applicability. The classifier and registry never read `ProviderRuntimeStateStore` payloads.

Routing performs no network I/O. It does not probe origins, contact provider APIs, perform host refresh, or contact executors. Provider maintenance owns dynamic refresh independently of request submission and initial routing.

## URL applicability view

URL applicability uses `urllib.parse.urlsplit` and derives a routing-only `UrlApplicabilityView`. It does not reconstruct or modify the endpoint that will later be executed.

For classification:

- scheme is canonicalized case-insensitively;
- hostname is taken from the parsed hostname, never from raw-string substring searches;
- a trailing DNS root dot is removed deliberately;
- DNS names are converted through Python's IDNA codec and case-folded, so equivalent Unicode and punycode forms compare consistently;
- IPv4 and IPv6 literals are parsed through `ipaddress` and canonicalized as address literals;
- explicit port is retained separately and never becomes part of hostname matching;
- URL userinfo is not hostname material and is not logged or persisted by classification;
- malformed or hostless URL-shaped requests produce no URL provider match rather than crashing routing.

This normalization is classification-only. Case-sensitive paths, percent encoding, signed query strings, query ordering, capability tokens and the original endpoint remain unchanged for the candidate/executor path. Providers with native path/query applicability may evaluate those details inside their own request-aware applicability source and expose only the neutral result.

## Applicability classes

`GENERIC` means a provider accepts a URL scheme generally without claiming special knowledge of the requested resource. General HTTP & HTTPS declares `http` and `https` as generic schemes.

`SPECIALIZED` means a provider explicitly claims the requested resource relationship. Neutral host claims can be exact-host or domain-scope:

- exact-host matches only the canonical host itself;
- domain-scope matches the canonical domain and its label-bounded subdomains;
- `evil-example.test`, `notexample.test`, and `example.test.evil.test` do not match a domain-scope claim for `example.test`;
- domain suffix logic is never applied to IPv4 or IPv6 literals; an IP can match only an explicit exact-host claim.

A provider may keep additional native structural checks inside its own applicability source. AllDebrid, for example, owns its supported-domain and native regexp interpretation and emits a neutral specialized claim only after its own data says the concrete request is structurally supported.

## Deterministic initial routing

For ordinary routing, `IntegrationRegistry.eligible_providers()` applies the following pipeline:

1. exclude disabled, registered-unhealthy, capability-incompatible, and request-type-incompatible providers;
2. obtain each remaining provider's neutral applicability;
3. run the Item 6 classifier;
4. if the matching `SPECIALIZED` set is non-empty, suppress the `GENERIC` set completely; otherwise retain the matching generic set;
5. pass only the surviving same-class set into the established neutral selection order: explicit preferred provider, descending priority, then stable provider ID.

The class decision is therefore a filter, not a provider choice. If providers A and B are specialized for the same request and provider C is generic, ordinary selection receives A and B only. C does not compete at equal weight. A versus B is still decided by the neutral preference/priority/stable-ID policy; registration, dictionary, import, filesystem, and Python hash order are not selection policy.

If no provider survives eligibility and applicability, `provider_for()` returns the canonical request-domain `UNSUPPORTED_REQUEST` at resolution with `Retryability.NEVER`. This means the request was understood but no currently eligible provider can service it.

## Current production matrix

With current production providers, AllDebrid supplies provider-owned `SPECIALIZED` URL applicability from its usable host snapshot while General HTTP & HTTPS supplies `GENERIC` HTTP(S) applicability. That produces:

| AllDebrid | General HTTP(S) | AD-supported HTTP(S) URL | Unrelated HTTP(S) URL |
| --- | --- | --- | --- |
| enabled | enabled | AllDebrid | General HTTP(S) |
| disabled | enabled | General HTTP(S) | General HTTP(S) |
| enabled | disabled | AllDebrid | unsupported route |
| disabled | disabled | unsupported route | unsupported route |

These names describe today's production providers, not universal routing rules. The universal rule is specialized-set precedence over the generic set.

## Enablement, health and class ordering

Enablement and registered health remain routing preconditions rather than applicability classes. The registry excludes disabled or explicitly unhealthy providers before applicability-set construction. Consequently an unhealthy specialized provider does not suppress a healthy generic provider: the unhealthy provider never enters the class competition. If no healthy eligible provider remains, routing is unsupported.

This is the established neutral health contract, not a new cross-provider runtime-failure fallback. Once a specialized provider has been selected, a later provider or execution failure does **not** automatically resurrect a generic provider. Cross-provider failure/failover policy remains separate unless explicitly implemented elsewhere.

Disabling a provider affects only routing eligibility. It does not destroy provider-owned retained runtime state. AllDebrid host state may remain persisted while AllDebrid is disabled and may be rebound when it is enabled again.

## Unsupported route lifecycle

`UNSUPPORTED_REQUEST` at initial provider selection is distinct from malformed input and from a selected provider that later fails. Provider selection occurs before `begin_resolution`, so an unsupported route creates no provider resolution attempt and consumes no provider retry budget. No executor is selected or started, no aria2 work is created, no authentication interaction begins, and routing itself performs no host refresh or destination network access.

The transfer lifecycle records the canonical non-retryable request failure rather than inventing an integration error, aria2 error, retry-exhaustion result, or `AUTH_REQUIRED` state.

## Static request types

Magnet and torrent routing remain static request-type/capability declarations. They do not require a hostname view and do not acquire HTTP(S) `SPECIALIZED`/`GENERIC` precedence. General HTTP & HTTPS is not eligible for magnet or torrent requests merely because it is generic for HTTP(S).

## AllDebrid runtime-derived claims

Roadmap Item 7 owns AllDebrid dynamic supported-host plumbing. AllDebrid maintenance retrieves and validates native host data, persists its last-known-good representation through the neutral Item 2 runtime-state store, applies provider-local freshness/usability rules, and exposes neutral specialized applicability. Roadmap Item 8 consumes only those exposed claims.

The routing layer does not inspect host availability, quota, timestamps, native AllDebrid status, native host payloads, or persisted runtime-state bytes. It does not trigger refresh at submission time. If AllDebrid exposes usable stale-LKG claims under its own policy, routing may consume them; if AllDebrid exposes no claim, the router simply sees no specialized match.

## Security boundary

Applicability is not authorization to connect and is not a trust signal. A specialized match does not bypass destination validation, DNS-rebinding controls, egress policy, TLS/SNI verification, redirect controls, credential boundaries, ownership checks or executor filesystem safety. Those protections continue to run at their established boundaries.

## Deferred policy and UI

Roadmap Item 8 adds initial deterministic routing only. It does not add provider pickers, direct-versus-debrid selectors, routing-provenance cards, provider badges, new provider settings UI, or a new automatic generic fallback after a selected specialized provider fails. Richer provenance and cross-provider failure/failover policy remain later roadmap work.
