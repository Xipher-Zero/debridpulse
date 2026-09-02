# General HTTP & HTTPS provider

Roadmap Item 5 introduces `general_http`, DebridPulse's first general/non-debrid
provider. It is intentionally small: the provider resolves ordinary `http` and
`https` requests into canonical transfer candidates, while the universal transfer
core retains lifecycle ownership and aria2 remains the byte-transfer executor.

## Stage 5 ownership boundary

`backend/providers/general_http/` owns only request validation and candidate
resolution. It does not fetch content, probe origins, import aria2, depend on
AllDebrid, classify hosts, or decide retry/failover policy. The production catalog
registers it like every other integration, with enabled state and priority supplied
through the existing backend integration configuration model.

Item 5 deliberately does not add a dedicated Sources & Providers settings surface.
It also does not implement specialized-versus-generic routing. AllDebrid and General
HTTP may both advertise HTTP(S); when both are eligible, the neutral registry's
existing preferred-provider, enabled-state, priority, and stable-ID rules decide
which provider is selected. A later roadmap stage owns host-aware classification.

URLs containing embedded user information are rejected before durable request
persistence and again at the provider boundary. Ordinary URLs are attempted without
pre-prompting for credentials.

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

Item 5 does not discover or automate HTML login forms, cookies, browser sessions,
OAuth, OIDC, SAML, JavaScript challenges, CAPTCHA flows, or site-specific login
behavior. HTTP 403, 404, 5xx responses and DNS/TLS/security failures are not
reclassified as credential requests.

The executor continues to enforce the existing destination and filesystem safety
boundary: public-address validation, DNS-rebinding protection, redirect suppression,
certificate verification, SNI/hostname identity, core-assigned destination paths,
and aria2 ownership authorization remain in force.

## Qualification boundary

Stage 5 qualification exercises deterministic direct HTTP and HTTPS transfers via
General HTTP -> universal core -> real aria2, including TLS hostname/SNI identity.
It also exercises a real HTTP authentication origin through initial challenge,
wrong credentials, replacement input, and successful continuation; negative
403/404/5xx and HTML-form cases; neutral AllDebrid/General-HTTP overlap routing;
secret-persistence checks; generic browser input handling; and the normal security,
static-analysis, container, and multi-architecture image gates.
