# Authentication

DebridPulse 1.0.6 supports three authentication mechanisms while retaining intentional no-auth operation for trusted standalone/LAN deployments:

- **Username & Password** for browser sign-in, with the same credentials also available to REST clients through HTTP Basic authentication;
- **OpenID Connect (OIDC)** for browser sign-in through a standards-compliant identity provider;
- **API bearer token** for automation, Prometheus, scripts, and other machine clients.

Authentication is configured under **Settings → Authentication**. The normal application settings page no longer owns authentication controls.

## Deployment modes

The interactive authentication state is one of:

| Mode | Browser access | Machine access |
|---|---|---|
| No authentication | Open | API is open as well |
| Username & Password | DebridPulse login page | HTTP Basic; bearer token if configured |
| OIDC | OIDC login | Bearer token if configured |
| Username & Password + OIDC | OIDC is presented as the preferred path; local password remains available | HTTP Basic and/or bearer token |

The API token is supplemental. Configuring one does **not** create a separate API-token-only deployment mode, and it does not turn an intentionally open installation into a protected one.

## External access and canonical origin

When DebridPulse is exposed through an HTTPS reverse proxy, configure the exact externally reachable origin even if only Username & Password authentication is enabled. The same canonical origin is used for browser mutation-origin validation, HTTPS-only cookie classification, and OIDC callback construction.

The recommended deployment-level setting is:

```yaml
environment:
  PUBLIC_BASE_URL: https://download.example.com
```

The equivalent persisted setting is available under **Settings → Authentication → External Authentication Origin → Canonical External URL**. `PUBLIC_BASE_URL` takes precedence when both are present; the UI displays the effective environment override as read-only.

Use only an HTTPS origin in `scheme://host[:port]` form, with no path, query, fragment, or credentials. Configuring an external base does not disable direct LAN access: a direct LAN request continues to be evaluated against its own Host/scheme, while a request whose Host matches the configured external authority can be recognized as the canonical HTTPS origin across an internal HTTP reverse-proxy hop.

`CORS_ORIGINS` is not a substitute for the canonical origin. CORS controls intentionally permitted cross-origin API callers; the External Base URL describes DebridPulse's own externally reachable browser origin.

## Accounts

Every sign-in resolves to one durable DebridPulse **account** with a stable ID. An account has one of four fixed roles (**Admin**, **User**, **ReadOnly User**, **Integration**), zero or one local Username & Password credential, and zero or more linked OpenID Connect identities, each identified by the provider-validated `(issuer, subject)` pair. A username or an email address is never the identity: email is display information only and never links, merges or grants anything.

Two default accounts always exist:

- the default **Admin**, which has no password until one is set. With no authentication enabled, DebridPulse runs as this single Admin (it is never an anonymous multi-user installation). It cannot be deleted, disabled, demoted or merged away;
- the default **Automation** Integration account, which has no API token.

Accounts are kept by the database. A backup therefore contains them, **including every local password's Argon2id verifier**, and restoring a backup is a privileged operation that replaces every account and authentication setting with the backup's. A restore is refused when its restored state would leave no enabled Admin who could sign in with the restored authentication settings; every browser session ends with a successful restore. The database wipe keeps all accounts.

> **1.0.13 transition.** Until per-role enforcement arrives in the next stage of the access-control work, only **Admin** accounts may sign in. An OIDC identity that is admitted by the authorization policy but not linked to an Admin account is recorded as a new account with the configured default role (`auth_oidc_default_role`: `user` or `readonly`) and is refused with *This account cannot sign in to DebridPulse*. To sign in with OIDC as an Admin, complete **Verify Sign-In** while signed in as that Admin: the proven identity is linked to your account.

## Username & Password

DebridPulse stores the local password only as an Argon2id verifier, on the account it belongs to. The plaintext password is accepted only as transient input when setting or replacing the credential. Until the Users & Access surface exists, **Settings → Authentication** edits the signed-in account's own username and password (the default Admin's in open mode).

Browser users authenticate through the DebridPulse login page. REST clients may use the same credentials with standard HTTP Basic authentication:

```bash
curl -u operator:'your-password' https://debridpulse.example/api/stats
```

Changing or clearing an account's password ends that account's browser sessions and clears the HTTP Basic verification cache. Disabling Username & Password immediately disables HTTP Basic and invalidates password-derived browser sessions. Disabling the mechanism does not erase the stored hash unless **Clear stored password** is explicitly selected.

## OpenID Connect

DebridPulse implements provider-neutral OpenID Connect Authorization Code flow with PKCE, state, nonce, issuer/audience validation, and server-side application sessions.

Configure at minimum:

- provider display name;
- issuer URL;
- client ID;
- client secret when required by the provider;
- scopes (normally `openid profile email`);
- canonical public base URL;
- authorization policy.

The **issuer URL is an exact OpenID Connect identifier**. Copy the issuer exactly as published by the provider, including a trailing `/` when one is present. DebridPulse does not treat an issuer ending in `/` as equivalent to the same text without it. This is particularly relevant to providers such as authentik whose per-provider issuer commonly ends with `/`.

The public base URL must be the externally reachable HTTPS origin used by the identity provider, for example:

```text
https://debridpulse.example
```

The callback is derived from that origin:

```text
https://debridpulse.example/auth/oidc/callback
```

Register that exact callback URL with the identity provider.

### Authorization policy

OIDC authentication and DebridPulse authorization are separate checks. Configure either **Allow all authenticated identities** or one or more explicit allow lists for subject, email, or group claims. Group matching uses the configured group-claim name.

Subject allow-list entries use the issuer-qualified identity form `<issuer>|<sub>`, for example `https://id.example/application/o/debridpulse/|user-1`. This is the same stable identity stored in DebridPulse OIDC sessions.

Email allow-list authorization requires the provider to assert `email_verified: true` for the matching address. If that claim is absent from the ID token, DebridPulse may use the provider's UserInfo endpoint to complete it. The email address and verification status are evaluated as one claim pair from the same source; verification from UserInfo is never attached to a different email from the ID token. If the provider never supplies a verified-email claim, use subject or group authorization instead of an email allow list.

Do not enable OIDC without an authorization policy that matches the intended users.

### Lockout prevention

Enabling OIDC never silently disables a working local password.

A fresh installation that is still in intentional open mode cannot make an **unproven OIDC configuration its first and only interactive authentication mechanism**. There is no authenticated DebridPulse session in that state from which to prove or recover a bad identity-provider configuration. Bootstrap OIDC safely in this order:

1. configure and enable **Username & Password** together with the proposed OIDC settings;
2. save the configuration and sign in with the new local password;
3. use **Verify Sign-In** and complete the real provider flow through the externally reachable HTTPS URL;
4. confirm normal OIDC sign-in works;
5. only then disable Username & Password if OIDC-only operation is desired.

Username & Password cannot be disabled in favor of OIDC until a **real OIDC login** has completed successfully. Discovery or a configuration-only HTTP check is not sufficient.

When OIDC is already the sole interactive mechanism, security-critical OIDC changes are staged as a pending configuration. The current known-working configuration remains authoritative until the proposed settings themselves complete a successful OIDC login. If another authentication change is committed while that verification is in flight, the pending proposal is considered stale and must be verified again rather than overwriting the newer state. The successful proof is bound to the exact staged OIDC configuration before that configuration can be persisted.

Use **Verify Sign-In** in the Authentication settings tab after entering proposed OIDC changes.

## API bearer token

Generate the token under **Settings → Authentication → API Access**. Tokens use a recognizable `dp_...` format and are shown in full only once on generation or rotation.

The raw token is never persisted. DebridPulse stores only a SHA-256 verifier in `api-token.json` beside `config.json`, using restrictive file permissions where supported.

Use it as:

```bash
curl \
  -H 'Authorization: Bearer dp_REPLACE_WITH_TOKEN' \
  https://debridpulse.example/api/stats
```

Rotation immediately invalidates the previous token. Clearing removes the persisted verifier. Disabling bearer authentication retains the verifier so the same token can be re-enabled later; **Clear token** is the destructive operation.

When interactive authentication is enabled, request authentication precedence is:

1. valid DebridPulse application session;
2. explicit Bearer token;
3. explicit HTTP Basic credentials;
4. unauthenticated.

A valid browser application session therefore remains authoritative even if an intermediary unexpectedly adds another authorization header.

## Intentional open mode

Both Username & Password and OIDC may be deliberately disabled. Moving from an authenticated deployment into no-auth mode requires explicit confirmation because the application and REST API become unrestricted.

DebridPulse distinguishes intentional open mode from a broken configured authentication mechanism. A configured authentication failure must not cause the application to fail open. Likewise, an existing `config.json` that cannot be read or parsed is a startup error rather than permission to invent default/open authentication state.

## Browser sessions and CSRF

Successful local-password and OIDC browser logins both produce the same server-side DebridPulse application-session model. Sessions have an absolute lifetime configured in the Authentication tab.

State-changing requests made through an application session require the session CSRF token. Logout is a state-changing POST and revokes the server-side session.

Same-origin EventSource connections use the normal browser session cookie. REST requests that are not browser navigation receive ordinary HTTP authentication failures rather than identity-provider HTML.

Authentication request bodies are bounded before authentication/configuration middleware can buffer them. Public login/OIDC-start state allocation and expensive password verification also have bounded admission/rate limits so unauthenticated request bursts cannot create unbounded in-process authentication work.

## Reverse proxies

For externally proxied deployments, terminate HTTPS at the public ingress and set **Canonical External URL** in the **External Authentication Origin** card to the canonical external origin. Ensure the proxy preserves the normal Host/protocol information expected by the deployment and does not rewrite the registered callback path.

DebridPulse does not independently trust an arbitrary client-supplied `X-Forwarded-Proto` header when deciding whether Password-session/login-CSRF cookies should use the HTTPS-only `__Host-` form. HTTPS is established from the ASGI request scheme after the server's trusted-proxy handling, or from the operator-configured HTTPS canonical external URL when its authority matches the request Host. This keeps secure-cookie classification tied to trusted deployment state rather than a spoofable request header.

If a non-loopback reverse proxy is expected to supply forwarded client/scheme information to Uvicorn, configure Uvicorn's `FORWARDED_ALLOW_IPS` allowlist for the actual proxy peer or proxy network. Do not use a blanket `*` trust setting on an interface reachable by untrusted clients. If forwarded client addresses are not trusted/configured, DebridPulse safely treats the transport proxy itself as the peer for authentication throttling; this can make multiple users behind the same proxy share the per-peer budget but does not create an authentication bypass.

DebridPulse does not require an external authentication proxy when its native Password/OIDC mechanisms are enabled. An external proxy may still be used as an additional perimeter control, but it should not be relied on to repair an intentionally misconfigured native authentication state.

## Release validation boundary

The automated test suite exercises OIDC protocol validation, state/nonce/PKCE handling, authorization policy, pending-configuration lockout protection, session behavior, HTTP Basic coexistence, bearer-token behavior, browser security boundaries, authentication admission limits, and pre-persistence proof/configuration binding with controlled test providers and responses.

Those tests do not prove a particular external identity-provider, public DNS, TLS termination, or reverse-proxy deployment. Before relying on OIDC as the sole interactive mechanism in production, complete **Verify Sign-In** through the actual externally reachable HTTPS URL and configured identity provider while the known-working Password fallback is still available.

## Recovering access

If no Admin can sign in any more (a forgotten password, or an identity provider that is down or no longer maps to the Admin), recovery needs access to the computer running DebridPulse. Stop DebridPulse, back up `config.json`, and either:

- add `"auth_username": "<name>"` and a plaintext `"auth_password": "<new password>"`, and set `"auth_password_enabled": true`. At the next start the plaintext is converted to a verifier, installed as the **default Admin's** local credential, and removed from the file; or
- set `auth_password_enabled` and `auth_oidc_enabled` to `false`. DebridPulse then starts open, as the default Admin, so repair authentication in **Settings → Authentication** and switch protection back on straight away.

DebridPulse logs an error at startup whenever no enabled Admin can sign in with the enabled methods.

## Configuration-file examples

The web UI is the supported configuration surface. The following JSON fragments document the persistent representation for recovery and automation; they are not a substitute for the lockout protections in the Authentication UI/API. Local credentials are not configuration: they belong to accounts in the database.

Password only:

```json
{
  "auth_password_enabled": true,
  "auth_oidc_enabled": false,
  "auth_session_lifetime_hours": 12
}
```

OIDC plus Password fallback:

```json
{
  "auth_password_enabled": true,
  "auth_oidc_enabled": true,
  "auth_oidc_default_role": "user",
  "oidc_provider_name": "Authentik",
  "oidc_issuer_url": "https://id.example/application/o/debridpulse/",
  "oidc_client_id": "debridpulse",
  "oidc_client_secret": "REDACTED",
  "oidc_scopes": ["openid", "profile", "email"],
  "oidc_allow_all": false,
  "oidc_allowed_emails": ["operator@example.com"],
  "public_base_url": "https://debridpulse.example"
}
```

Do not copy a password hash or client secret into documentation, logs, bug reports, or screenshots. The API token verifier is stored separately in `api-token.json`; never place the raw bearer token in `config.json`.
