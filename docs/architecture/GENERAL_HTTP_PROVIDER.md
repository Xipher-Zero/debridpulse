# General HTTP & HTTPS provider

Roadmap Item 5 introduced `general_http`, DebridPulse's first general/non-debrid
provider. It is intentionally small: the provider resolves ordinary `http` and
`https` requests into canonical transfer candidates, while the universal transfer
core retains lifecycle ownership and aria2 remains the byte-transfer executor.
Roadmap Item 6 now places that provider behind the provider-neutral applicability
classifier documented in [PROVIDER_APPLICABILITY.md](PROVIDER_APPLICABILITY.md).

## Ownership boundary

`backend/providers/general_http/` owns only request validation, its canonical
applicability declaration, and candidate resolution. It does not fetch content,
probe origins, import aria2, depend on AllDebrid, classify arbitrary hosts, or
decide retry/failover policy. The production catalog registers it like every other
integration, with enabled state and priority supplied through the existing backend
integration configuration model.

General HTTP & HTTPS declares `http` and `https` as `GENERIC` applicability. It
claims no hosts as `SPECIALIZED`. When no healthy enabled provider contributes a
matching specialized claim, General HTTP & HTTPS remains eligible and the existing
neutral preferred-provider/priority/stable-ID rules order it against other generic
providers. When at least one matching specialized claim exists, generic candidates
are suppressed for ordinary routing before same-class ordering is applied.

AllDebrid dynamic host-support plumbing remains Roadmap Item 7. Item 6 does not add
an AllDebrid host list, fetch or parse AllDebrid host data, persist host-support
records, or teach the universal classifier any provider-specific host knowledge.

Item 5 deliberately did not add a dedicated Sources & Providers settings surface,
and Item 6 remains backend routing architecture only. No routing-choice or host
claim UI is introduced here.

URLs containing embedded user information are rejected before durable request
persistence and again at the provider boundary. Ordinary URLs are attempted without
pre-prompting for credentials. Applicability parsing uses only the parsed hostname
for host matching and does not persist or log raw userinfo.

## HTTP resource authentication

Conventional HTTP username/password authentication is discovered only from a real
aria2 execution result. A General HTTP candidate advertises that it can accept the
neutral `username_password` input method. aria2 native authorization failure code
24 is translated into `AUTH_REQUIRED` only for such a candidate; the same native
code retains its existing non-auth candidate-expiry meaning for candidates that do
not advertise that input method.

The existing generic `INPUT_REQUIRED` lifecycle and browser modal are reused. No
General HTTP-specific modal or form is introduced. Wrong credentials leave the
same logical transfer waiting for replacement input. Accepted credentials continue
the same transfer identity and execution ownership rather than creating a new
logical download.

Credential values are transient execution input. They are not added to the URL,
provider resource context, execution handle, durable attempt records, provenance,
or global aria2 configuration. aria2 jobs set `no-netrc=true` so saved local netrc
credentials cannot silently participate. Initial requests use challenge-based HTTP
auth with empty user/password options; submitted values are added only to the
specific retry after a definitive authorization challenge.

## Explicit non-goals

General HTTP does not discover or automate HTML login forms, cookies, browser
sessions, OAuth, OIDC, SAML, JavaScript challenges, CAPTCHA flows, or site-specific
login behavior. HTTP 403, 404, 5xx responses and DNS/TLS/security failures are not
reclassified as credential requests.

The executor continues to enforce the existing destination and filesystem safety
boundary: public-address validation, DNS-rebinding protection, redirect suppression,
certificate verification, SNI/hostname identity, core-assigned destination paths,
and aria2 ownership authorization remain in force. A specialized applicability
claim is never treated as authorization to bypass those checks.

## Qualification boundary

Stage 6 preserves deterministic direct HTTP and HTTPS transfers through real
General HTTP -> universal core -> aria2, including TLS hostname/SNI identity and the
existing HTTP authentication challenge/continuation cases. Its new routing proof
uses a deterministic provider-neutral specialized fixture: a matching specialized
claim suppresses General HTTP's generic match, an unrelated host leaves General
HTTP eligible, and disabling the specialized provider restores the generic path.
The runtime-derived fixture proves provider-owned opaque state can be translated
into canonical claims without the classifier reading native state.
