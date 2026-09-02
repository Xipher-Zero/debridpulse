# General HTTP & HTTPS Provider

`general_http` is DebridPulse's general/non-debrid HTTP(S) provider. It resolves ordinary `http` and `https` requests into canonical transfer candidates; the Universal Transfer Core retains lifecycle ownership and aria2 remains the byte-transfer executor.

## Ownership boundary

`backend/providers/general_http/` owns:

- validation of ordinary HTTP(S) requests;
- `GENERIC` applicability for `http` and `https`;
- canonical direct candidate creation;
- provider-level translation of supported conventional HTTP authentication semantics.

It does not fetch content itself, import AllDebrid knowledge, classify specialized hosts, decide specialized-provider precedence, own provenance presentation or choose cross-provider retry/failover policy.

The production catalog registers General HTTP & HTTPS through the same integration-definition/configuration path as other providers. Its enabled state is the canonical backend provider setting used by routing and by the Sources & Providers UI.

## Routing behavior

General HTTP & HTTPS claims no hosts as `SPECIALIZED`. When no healthy enabled provider contributes a matching specialized claim, its generic HTTP(S) applicability remains eligible. When a matching specialized claim exists, the neutral registry suppresses the generic set before same-class selection.

With current providers this means an AllDebrid-supported URL routes to AllDebrid when both providers are enabled, while an unrelated HTTP(S) URL routes direct. Disabling AllDebrid allows the same structurally supported URL to route direct; disabling HTTP & HTTPS leaves unrelated HTTP(S) URLs unsupported.

AllDebrid's dynamic host inventory is fully provider-owned under `providers/alldebrid/host_runtime.py`. General HTTP & HTTPS has no knowledge of that inventory or its freshness/availability/account semantics.

## HTTP resource authentication

URLs containing embedded user information are rejected before durable request persistence and again at the provider boundary. Ordinary URLs are attempted without pre-prompting for credentials.

Conventional HTTP username/password authentication is discovered only after a real supported aria2 authorization failure. A General HTTP candidate advertises the neutral `username_password` input method. aria2 native authorization failure code 24 is translated into `AUTH_REQUIRED` only for candidates supporting that input; the same native code retains its existing non-auth candidate-expiry meaning where appropriate.

The generic `INPUT_REQUIRED` lifecycle and generic browser authentication modal are reused. There is no General HTTP-specific modal.

Wrong credentials:

- do not create a replacement logical transfer;
- do not terminally fail a transfer merely because authentication is still required;
- leave the challenge pending so corrected credentials can be submitted.

Corrected credentials continue the same logical transfer. The modal closes only after backend challenge resolution; successful credential-submission HTTP status alone is not sufficient. If authentication succeeds while the transfer is paused or queued, the challenge resolves without overriding that independent lifecycle state. Cancel resolves the pending-input state and invalidates stale continuation.

Credential values are transient execution input. They are not added to the URL, provider runtime state, provider resource context, execution handle, durable attempt/provenance records or global aria2 configuration. aria2 uses `no-netrc=true`; submitted values are supplied only to the specific challenged retry.

## Execution and security

General HTTP & HTTPS produces canonical candidates; executor selection remains neutral and production direct HTTP(S) acquisition is executed through aria2.

The established network/filesystem boundary remains in force:

- public-address/DNS egress validation;
- DNS-rebinding protection;
- redirect safety;
- TLS certificate and hostname/SNI verification;
- core-assigned destination ownership;
- aria2 execution authorization;
- signed-source and credential sanitization.

Neither generic nor specialized applicability authorizes bypass of these protections.

## Provenance and presentation

When a transfer actually establishes the General HTTP & HTTPS route, durable provenance records provider ID `general_http` and the corresponding candidate/executor relationships. Completed presentation uses the actual delivering provider; active presentation uses the established current provider where available. Historical identity remains `general_http` even if later runtime-state/configuration changes would route the same URL differently.

Recent Activity, Downloads and Details consume this backend truth. They do not infer a direct route from the URL or from aria2.

## Explicit non-goals

General HTTP & HTTPS does not implement HTML login forms, browser sessions, cookies, OAuth/OIDC/SAML, JavaScript challenges, CAPTCHA flows, saved credentials or site-specific login automation. Ordinary HTTP 403/404/5xx, DNS/TLS failures and security failures are not reclassified as credential requests.

Production cross-provider failure fallback is not introduced here. Item 11 qualifies the current deterministic initial routing and failover-ready provenance without inventing a new AllDebrid→HTTP runtime-failure policy.

See [PROVIDER_APPLICABILITY.md](PROVIDER_APPLICABILITY.md) and [MULTI_PROVIDER_HTTP_SLICE.md](MULTI_PROVIDER_HTTP_SLICE.md) for the neutral classification/routing and converged ownership contracts.
