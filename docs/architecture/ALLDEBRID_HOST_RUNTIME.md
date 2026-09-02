# AllDebrid Dynamic Supported-Host Runtime State

Roadmap Item 7 makes AllDebrid's own account host inventory the source of its
HTTP/HTTPS `SPECIALIZED` applicability while preserving the provider-neutral
routing introduced in Item 6.

## Native source and boundary

AllDebrid host maintenance uses the existing authenticated AllDebrid client and
its canonical request pacing/error path against `v4.1/user/hosts`. The native
payload is interpreted only under `providers/alldebrid/`.

The provider-owned model keeps these concepts separate:

- **Structural host support**: a validated host record and its native `domains`.
  This alone produces neutral HTTP/HTTPS specialized host claims.
- **Current host availability**: native optional `status`. This is retained as
  transient host-local information and does not remove structural support.
- **Provider health**: remains the existing registry/provider health concept.
  One unavailable host does not make the AllDebrid integration unhealthy.
- **Account limits**: `quota`, `quotaMax`, `quotaType`, and `limitSimuDl`, when
  returned, remain AllDebrid-local operational facts and do not affect hostname
  applicability.

AllDebrid's native `regexps` (and the documented singular `regexp`
compatibility form) are validated and retained inside the provider snapshot.
They are not emitted to or executed by the neutral classifier. Item 7's
structural routing question is whether the provider claims the parsed
hostname; native URL-format regex semantics remain provider-local.

Every native domain/alias becomes a canonical **exact-host** Item 6 claim for
HTTP and HTTPS. Exact-host claims deliberately reject substring and unrelated
subdomain matches. The classifier retains its existing IDNA, trailing-dot,
port, userinfo, IP, and boundary-safe hostname behavior.

## Neutral persistence and last-known-good

The dataset is stored through Item 2's existing neutral provider runtime-state
facility:

- integration id: `alldebrid`
- state key: `supported-hosts`
- provider schema marker: `alldebrid-supported-hosts-v1`
- payload: deterministic provider-owned JSON bytes
- freshness: neutral `observed_at` / `stale_after` metadata
- replacement: generation-guarded atomic replace

No AllDebrid-specific table or universal database column exists. The runtime
store does not parse the payload.

A fetched snapshot is fully validated before replacement. If network,
authentication/provider, rate-limit, timeout, malformed-payload, validation, or
persistence work fails, the previous durable snapshot is left unchanged.
Stale-but-valid last-known-good data remains usable for structural claims while
maintenance recovery continues. An incompatible/corrupt retained snapshot is
not interpreted and exposes no claims until maintenance obtains a valid
replacement.

The snapshot contains no API key, authorization header, submitted credential,
signed transfer URL, or challenge secret. Diagnostics do not log the raw
native host/account payload.

## Refresh policy

Host inventory refresh is integration maintenance, never transfer submission.

An enabled AllDebrid integration refreshes when:

- no usable snapshot exists;
- the retained snapshot reaches its approximately 24-hour freshness boundary;
- the integration transitions from disabled to enabled.

A fresh snapshot restored at application startup is loaded without an immediate
network refresh. On restart a stale valid snapshot can still reconstruct
claims, while the normal maintenance loop separately refreshes it.

Refreshes are serialized for this dataset. Runtime-state generation checks
prevent a slower/stale writer from overwriting a newer successful generation.
A bounded retry delay prevents the one-minute application maintenance cadence
from becoming a provider-local retry storm after failure.

Disabling AllDebrid removes it through the existing descriptor/registry enabled
filter and stops host maintenance; it does **not** delete runtime state.
Re-enabling can reuse retained state during maintenance and independently
triggers a refresh.

Ordinary URL, magnet, and torrent submission never invokes the host endpoint,
including when the snapshot is stale. Classification consumes only the
already-available applicability snapshot.

## Routing result

For a validated native host domain:

1. AllDebrid emits a neutral `SPECIALIZED` HTTP/HTTPS host claim.
2. General HTTP & HTTPS continues to emit `GENERIC`.
3. Item 6 suppresses generic providers when any specialized provider matches.
4. Existing neutral preference/priority/stable-ID ordering applies within the
   surviving class.

For an unclaimed hostname, AllDebrid emits no specialized match and General
HTTP & HTTPS remains eligible.

There is no AllDebrid-name branch in the classifier and General HTTP & HTTPS
contains no AllDebrid knowledge.

## Magnet and torrent preservation

Magnet/torrent routing is not hard-coded to AllDebrid. AllDebrid declares
`magnet` and `torrent` in its integration descriptor's `request_types`, exactly
like any other provider may do. Item 6 classifies eligible providers declaring
those request types as `STATIC`.

Dynamic AllDebrid URL host state does not gate, create, or interpret those
capabilities. A missing, stale, corrupt, or failed host snapshot therefore has
no effect on AllDebrid's separately declared magnet/torrent eligibility.

## Deferred work

Roadmap Item 7 adds no host-inventory UI, provider/provenance UI, quota UI,
manual refresh endpoint/button, credential persistence, new transport, or
Universal Transfer Core lifecycle policy. Those remain later-roadmap concerns.
