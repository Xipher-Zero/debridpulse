# AllDebrid Dynamic Supported-Host Runtime State

AllDebrid's account host inventory is the provider-owned source of its HTTP(S) `SPECIALIZED` applicability. Native AllDebrid semantics terminate under `backend/providers/alldebrid/`; the universal runtime-state, classifier and routing layers remain neutral.

## Native source and provider boundary

`backend/providers/alldebrid/host_runtime.py` owns the authenticated maintenance path for `v4.1/user/hosts` and the complete interpretation of its native payload.

The provider keeps distinct:

- **Structural support** — validated domains plus native URL regexp semantics; this determines whether the concrete request may become a neutral specialized claim.
- **Current host availability** — optional native `status`; temporary unavailability does not erase structural support.
- **Provider health** — the neutral registry health concept; one unavailable host does not make AllDebrid globally unhealthy.
- **Account facts** — quota/limit fields remain provider-local and do not become universal routing policy.

Native `regexps` (plus the supported singular compatibility form) are validated, compiled and evaluated only inside AllDebrid. A domain-boundary check occurs before a native regexp match can become a canonical exact-host/scheme applicability result. The classifier/router never receive or execute native regex syntax.

## Neutral persistence and last-known-good

AllDebrid persists its validated snapshot through the neutral provider runtime-state facility:

- integration id: `alldebrid`
- state key: `supported-hosts`
- schema marker: `alldebrid-supported-hosts-v1`
- payload: deterministic provider-owned bytes
- freshness metadata: neutral timestamps
- replacement: generation-guarded atomic replace

`backend/integrations/runtime_state.py` owns only storage mechanics and neutral metadata. It never parses AllDebrid payloads.

A new snapshot replaces the previous generation only after successful fetch, validation and persistence. Initial refresh failure with no snapshot leaves no fake usable state. A later refresh failure leaves the previous valid LKG generation intact. Corrupt/incompatible retained payloads expose no claims until maintenance obtains a valid replacement.

The provider snapshot contains no API key, authorization header, submitted HTTP credential, signed transfer URL or auth challenge secret.

## Refresh policy

Host inventory refresh is maintenance work, never transfer submission.

An enabled AllDebrid provider refreshes when:

- no usable snapshot exists;
- the retained snapshot reaches its provider-defined freshness boundary (currently approximately 24 hours);
- AllDebrid transitions from disabled to enabled.

A fresh snapshot is restored on startup without an immediate host-inventory fetch. A stale but structurally valid LKG snapshot may reconstruct claims according to the established provider policy while normal maintenance independently refreshes it.

Refreshes are serialized. Neutral generation checks prevent a slower writer from overwriting a newer successful generation, and provider-local retry cadence prevents the application maintenance loop from creating a refresh storm after failure.

Disabling AllDebrid removes it from routing eligibility and stops maintenance activity without deleting retained runtime state. Re-enabling can restore retained claims and independently schedules refresh according to provider policy.

Ordinary supported/unsupported URL admission, magnet admission, torrent admission, provider selection and resolution perform zero host-inventory calls. Routing consumes only already-exposed neutral applicability.

## Routing relationship

For a concrete URL structurally supported by the current usable AllDebrid snapshot:

1. AllDebrid's request-aware applicability evaluates its provider-local domains/regexps.
2. AllDebrid exposes a neutral `SPECIALIZED` exact-host/scheme result.
3. General HTTP & HTTPS independently exposes `GENERIC` HTTP(S).
4. The neutral registry suppresses generic providers because a matching specialized provider exists.
5. Existing neutral same-class selection policy applies.

For an unclaimed URL, AllDebrid exposes no specialized result and General HTTP & HTTPS remains eligible when enabled.

Missing state or an initial refresh failure does not make the classifier understand AllDebrid failure state; it simply means AllDebrid has no specialized claim to expose. A failed refresh with valid LKG leaves the previously exposed provider claims intact. The classifier/router do not inspect timestamps, current host availability, quota/account fields or refresh errors.

Magnet/torrent eligibility remains an independent static provider capability and is not gated by dynamic URL host state.

## Provenance, settings and presentation

The AllDebrid Enable control in Sources & Providers updates the canonical backend provider enablement used by the registry. There is no frontend-only enablement path and disabling the provider does not destroy its runtime snapshot.

When AllDebrid is actually selected, route/candidate/executor provenance is persisted by the neutral repository. Historical AllDebrid delivery remains historical AllDebrid delivery after restart or later changes to host inventory/configuration. Recent Activity, Downloads and Details consume that durable history; they do not classify the URL again.

AllDebrid host inventory itself is not exposed as a user-facing host list/quota surface by this slice.

## Security boundary

A specialized AllDebrid claim is a routing fact, not destination trust. DNS/egress controls, redirect policy, TLS/SNI validation, signed-source sanitization, credential secrecy, filesystem ownership and executor authorization remain mandatory after provider resolution.

No AllDebrid hostname list, native alias, native regexp, native availability/status, native payload schema or endpoint semantics belongs in the universal classifier/core.

See [PROVIDER_APPLICABILITY.md](PROVIDER_APPLICABILITY.md) and [MULTI_PROVIDER_HTTP_SLICE.md](MULTI_PROVIDER_HTTP_SLICE.md) for the neutral routing and complete converged ownership map.
