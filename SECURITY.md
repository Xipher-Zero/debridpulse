# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.0.x   | ✅ Yes     |
| < 1.0.0 | ❌ No      |

Only the latest release receives security fixes. Please update before reporting.

---

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Report security issues through a private GitHub Security Advisory (preferred), or contact the maintainer through the repository profile. Include the vulnerability, reproduction steps, likely impact, and any suggested mitigation.

---

## Security Considerations

### Secrets and configuration

The AllDebrid API key, local authentication password verifier, OIDC client secret, Discord webhook URLs, aria2 credentials, and extraction passwords are sensitive configuration. The main persistent settings live in `config/config.json`; the API bearer-token verifier is stored separately as `config/api-token.json`.

Keep the configuration directory private. DebridPulse attempts to enforce owner-only permissions (`0700` directory / `0600` credential files) where the host filesystem supports them.

Do not publish the config volume or commit it to version control. API responses intentionally redact configured secret values and capability-bearing provider/download URLs. The plaintext local password, raw API bearer token, OIDC authorization code/tokens, PKCE verifier, session identifier, and CSRF token must not be logged.

### Native web UI and API authentication

DebridPulse authentication is configured under **Settings → Authentication**. Intentional no-auth operation remains supported for trusted standalone/LAN deployments; if both interactive mechanisms are deliberately disabled, the REST API is open as well.

DebridPulse supports optional HTTP Basic Authentication for REST clients whenever Username & Password authentication is enabled. HTTP Basic uses the same username and Argon2id password verifier as the browser login; it remains a first-class machine-access path rather than the browser authentication UX.

When authentication is enabled, DebridPulse supports:

- **Username & Password** browser sign-in using a server-side application session. The local password is stored only as an Argon2id verifier.
- **HTTP Basic** for REST clients whenever Username & Password authentication is enabled, using the same username and Argon2id password verifier.
- **OpenID Connect** browser sign-in using Authorization Code + PKCE with state, nonce, issuer/audience validation, and DebridPulse authorization policy.
- **Bearer API token** for automation and monitoring. The raw `dp_...` token is shown only at generation/rotation and is never persisted; only its SHA-256 verifier is stored.

For protected requests, a valid DebridPulse browser session is authoritative, followed by Bearer authentication, then HTTP Basic.

State-changing requests made through browser application sessions require CSRF validation. Cross-site browser mutations are rejected independently of the configured authentication mechanism, including in intentional open mode.

### Authentication lockout protections

Enabling OIDC does not silently disable the local password path.

Username & Password cannot be disabled in favor of OIDC until a real OIDC login has succeeded. Discovery or a configuration-only connectivity check is not sufficient proof.

When OIDC is the sole interactive mechanism, critical OIDC changes are staged as a pending configuration. The current known-working configuration remains authoritative until the proposed configuration itself completes a successful OIDC login. A failed pending verification does not overwrite the working configuration.

Disabling both Username & Password and OIDC from an authenticated state requires explicit confirmation because it intentionally exposes the application. A configured but broken authentication mechanism must fail closed rather than falling back to open access.

Password replacement/clear invalidates password-derived browser sessions and the HTTP Basic verification cache. Disabling Password immediately rejects HTTP Basic and invalidates password-derived sessions. API-token rotation immediately invalidates the previous token.

See [`docs/authentication.md`](docs/authentication.md) for the complete deployment and recovery model.

### OIDC and reverse proxies

OIDC deployments require a canonical externally reachable HTTPS origin. Configure **Public Base URL** to that origin and register the exact derived callback (`/auth/oidc/callback`) with the identity provider.

The reverse proxy must not rewrite the registered callback path. Treat the proxy as a transport/perimeter component; native DebridPulse authorization and lockout protections remain authoritative when native authentication is configured.

### Network exposure

For any network you do not fully trust, enable native DebridPulse authentication and/or place the application behind an independently authenticated reverse proxy such as Authentik, Authelia, or an equivalent access-control layer.

**Do not expose port 8080 directly to the public internet.** The generic Compose example uses bridge networking with an explicit port mapping so exposure remains visible and can be bound/restricted by the operator. Host networking should be an explicit deployment choice, not the generic default.

### aria2

DebridPulse runs its own aria2 daemon. Its JSON-RPC interface listens on loopback only and is never exposed as a setting or a published port. Every job is routed through the DebridPulse egress guard with per-job proxy, credential, and host-key options, and per-GID actions are permitted only for downloads DebridPulse owns.

### Archive extraction

Archive extraction enforces member-path/type checks plus file-count, expanded-size, and compression-ratio budgets. External 7z/RAR extraction occurs in an isolated staging directory, is monitored while the extractor runs, and is validated again before files are merged into the download tree.

### Backups and database maintenance

Database wipe first closes and drains application mutation/execution admission (including all state-changing HTTP requests), then suspends scheduler activity and drains provider/materialization work before holding an exclusive database-maintenance gate that rejects concurrent non-owner application DB sessions; it also fails closed if a required pre-wipe backup fails. The SQLite online-backup API may hold a separate read-only source connection, but it cannot mutate or repopulate the live database. Backup rotation only recursively removes DebridPulse-owned directories carrying the expected ownership manifest.

### Discord webhook URL

Treat Discord webhook URLs as secrets: possession of the URL permits posting to the configured channel.

---

## Scope

The following are **in scope** for security reports:

- secret, credential-verifier, session, token, or capability-bearing URL exposure;
- password/OIDC/API-token authentication or authorization bypass;
- OIDC state, nonce, PKCE, issuer/audience, callback, or pending-configuration validation failures;
- CSRF or cross-origin state-changing request bypass;
- configured-authentication fail-open behavior or lockout-prevention bypass;
- remote code execution;
- path traversal or unsafe archive extraction;
- aria2 jobs bypassing the egress guard or acting on downloads DebridPulse does not own;
- unsafe destructive database/backup behavior.

The following are **out of scope**:

- issues in AllDebrid's own service/API;
- vulnerabilities that exist solely in an unmodified third-party dependency (report upstream as well);
- resource exhaustion requiring trusted local access with no network exposure, unless it crosses a documented DebridPulse safety boundary.
