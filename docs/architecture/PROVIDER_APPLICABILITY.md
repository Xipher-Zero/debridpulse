# Provider applicability classification

Roadmap Item 6 adds a single provider-neutral applicability layer for deciding which enabled providers can service a request before the existing registry ordering chooses among equivalent candidates. The canonical implementation is `backend/transfers/applicability.py`; the registry remains the routing owner and the universal transfer engine remains the orchestrator.

## Ownership boundary

The classifier owns mechanics, not provider knowledge. It understands canonical request structure, URL scheme/hostname/port, static request types, enabled candidates, `SPECIALIZED` and `GENERIC` URL applicability, exact-host/domain-scope matching, and opaque provider IDs. It contains no provider-specific host lists, provider-native payload parsing, provider TTL policy, or provider-name branches.

Providers own what they claim. A provider may expose a canonical `ProviderApplicability` snapshot through the `ApplicabilitySource` contract. Static definitions can contribute generic schemes or stable specialized host claims. A provider that learns applicability from runtime data must interpret and validate its own opaque state first, including any freshness policy, then expose only canonical claims. The classifier never reads `ProviderRuntimeStateStore` or provider-native payloads.

Classification performs no network I/O. It does not probe origins, resolve provider APIs, perform health checks, or refresh runtime datasets. Existing registered health remains a separate registry precondition.

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
- query strings, fragments and paths do not participate in Stage 6 host applicability;
- malformed or hostless URL-shaped requests produce no URL provider match rather than crashing routing.

This normalization is classification-only. Case-sensitive paths, percent encoding, signed query strings, query ordering, capability tokens and the original endpoint remain unchanged for the candidate/executor path.

## Applicability classes

`GENERIC` means a provider accepts a URL scheme generally without claiming special knowledge of the requested host. General HTTP & HTTPS declares `http` and `https` as generic schemes.

`SPECIALIZED` means a provider explicitly claims the requested host relationship. Claims can be exact-host or domain-scope:

- exact-host matches only the canonical host itself;
- domain-scope matches the canonical domain and its label-bounded subdomains;
- `evil-example.test`, `notexample.test`, and `example.test.evil.test` do not match a domain-scope claim for `example.test`;
- domain suffix logic is never applied to IPv4 or IPv6 literals; an IP can match only an explicit exact-host claim.

For ordinary URL routing, if at least one healthy enabled candidate has a matching `SPECIALIZED` claim, matching `GENERIC` candidates are suppressed. All specialized matches remain eligible. If no specialized claim matches, generic matches remain eligible. Within the surviving class, the registry preserves the existing neutral order: explicit preferred provider, priority, then stable provider ID.

Health is intentionally separate from applicability. The registry already excludes disabled or registered-unhealthy providers before applicability competition. Consequently an unhealthy specialized provider does not suppress a healthy generic provider. This preserves the established health contract; Item 6 does not create a new failure/failover policy.

Likewise, specialized suppression governs ordinary provider selection only. A later execution failure does not automatically resurrect a suppressed generic provider unless a separate existing or future recovery policy explicitly does so. Cross-provider failover is outside Roadmap Item 6.

## Static request types

Magnet and torrent routing remain static request-type/capability declarations. They do not require a hostname view and do not acquire URL-specific applicability classes. Other non-URL request forms likewise retain static capability semantics unless their provider contract explicitly evolves later.

## Runtime-derived claims

The permanent test provider demonstrates the intended future boundary:

1. opaque provider-owned runtime state is persisted through the existing neutral runtime-state store;
2. the provider validates its own schema, payload, usability and staleness;
3. the provider converts usable state into canonical specialized domain claims;
4. the classifier consumes those claims without importing or parsing runtime state.

Disabling the provider removes its active claims from routing without destroying retained runtime state.

## General HTTP(S) and AllDebrid in Stage 6

General HTTP & HTTPS contributes `GENERIC` applicability for `http` and `https`. It remains the protocol-general fallback whenever no healthy enabled provider contributes a matching specialized claim.

AllDebrid retains its existing HTTP(S), magnet and torrent behavior in Stage 6. Its HTTP(S) applicability remains generic until Roadmap Item 7 supplies real provider-owned dynamic host support through this contract.

Roadmap Item 6 deliberately does **not** fetch, parse, persist, refresh or cache AllDebrid host-support data, does not add an AllDebrid host table/list, and does not add provider-specific classifier logic. Roadmap Item 7 can later translate AllDebrid-owned dynamic host state into canonical `SPECIALIZED` claims; the universal classifier will then suppress generic HTTP(S) handlers automatically without learning anything about AllDebrid itself.

## Security boundary

Applicability is not authorization to connect and is not a trust signal. A specialized match does not bypass destination validation, DNS-rebinding controls, egress policy, TLS/SNI verification, redirect controls, ownership checks or executor filesystem safety. Those protections continue to run at their existing boundaries.
