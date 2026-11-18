# Browser client: pairing and browser tokens (`debridpulse.browser/1`)

A person connects a browser extension to their own account: the extension
asks, the person approves on this server, and the extension receives its own
**browser token**. That token can do what a browser client needs and nothing
else; its owner, or an Admin, can disconnect it at any time.

Owners in the code:

- `backend/auth/browser_pairing.py`: pending pairings (in memory only).
- `backend/auth/accounts.py`: browser tokens (`kind = browser` in
  `account_tokens`), their issuance, resolution and revocation.
- `backend/auth/permissions.py`: the `browsers.own` and `browser.client`
  capabilities, and the fence (`capabilities`).
- `backend/auth/middleware.py`: extension origins under `/api/browser/v1/`.
- `backend/api/browser_routes.py`: the HTTP routes.
- `frontend/static/ui-browser-pairing.js`: the "Connect a browser" dialog.
- `frontend/static/auth.js`: My Account › Connected browsers.
- `frontend/static/ui-settings-page.js`: Account Details › Connected browsers
  (an Admin's view).

## 1. Pairing

A device-authorization flow, after RFC 8628.

| Step | Who | Route | Answer |
|---|---|---|---|
| Start | the extension, no credential | `POST /api/browser/v1/pairings` `{client_name}` | `{protocol, device_code, user_code, verification_path, expires_in, interval}` |
| Ask | the person, signed in | opens `verification_path` (`/?connect-browser=<user_code>`); `GET /api/account/browser-pairings/{user_code}` | `{user_code, client_name, requested_at, expires_in}` |
| Answer | the person (session + CSRF) | `POST …/{user_code}/approve` `{label}` or `POST …/{user_code}/deny` | `{ok, browser: {id, label}}` / `{ok}` |
| Collect | the extension, holding `device_code` | `POST /api/browser/v1/pairings/poll` `{device_code}` | `{status}`: `pending`, `slow_down`, `denied`, `expired`, or `approved` with `{token, token_id, label}` |

- **Lifetime.** A pairing lives 10 minutes in this process's memory. A restart
  cancels it; the extension connects again. At most 64 are pending.
- **Secrets.** The `device_code` (256 bits) is the only proof that collects the
  token. It is held as a SHA-256 key, never raw, and never shown to the person.
  The `user_code` (`XXXX-XXXX`, from an unambiguous alphabet) only identifies
  the request to the approving person, who sees it beside the code the
  extension shows.
- **Exactly once.** The token is issued at approval, in the same transaction
  as its journal event, and handed to the first poll that follows. The pairing
  is then deleted, and a later poll answers `expired`. A token approved but
  never collected is revoked when its pairing expires. A code that never
  existed and one that expired answer alike.
- **Pace.** Polls closer than `interval` (2 s) answer `slow_down`.
- **Limits.**
  - Starting: 10 per peer and 64 installation-wide per 10 minutes.
  - Viewing and answering: 30 per person per 10 minutes.
  - Over a limit: `429`, `browser.pairing.rate_limited`.

## 2. Browser tokens and their fence

- **Who.** Only an active Admin or User account holds a browser token
  (`BROWSER_ROLES`). ReadOnly and Integration accounts may not approve a pairing.
- **Authority.** A browser token's principal holds exactly one capability,
  `browser.client`, **whatever its account's role**: a browser on an Admin
  account is still only a browser client. No role holds `browser.client`, so no
  session reaches the browser-client API, and no browser token reaches
  anything else (`403`). The download-client dialects refuse a browser token
  as an unknown key.
- **Re-checked on every request.** `resolve_token` re-reads the account, so a
  person demoted to ReadOnly, deactivated, merged or removed stops every browser
  of theirs at once.
- **Provenance.** A browser token is its own client (`client_id` = its own id),
  attributed to its person and its name.
- **The first route.** `GET /api/browser/v1/session` →
  `{protocol: "debridpulse.browser/1", token: {id, label}, account: {display_name}}`.
  It is how the extension verifies its pairing and the server's protocol. The
  browser adapter extends this API.

## 3. Disconnecting

| Who | Route |
|---|---|
| The person, their own browsers | `GET /api/account/browsers`; `DELETE /api/account/browsers/{token_id}` |
| An Admin, anyone's | `GET /api/accounts/{id}/tokens` (each row names its `kind`); `DELETE /api/accounts/{id}/tokens/{token_id}` |

Revocation is immediate (`401` on the next call). The work the browser sent is
untouched. A person's route finds only their own browser tokens: another
account's token, or an Integration token, is `404`. Nobody creates a browser
token for someone else; only the person's own approval does.

The journal records `administration.browser_connected` (account, token id and
name, requesting address) and `administration.browser_disconnected` (by its
owner or by an Admin). No event holds a secret.

## 4. Origins

An extension's background sends its own origin (`chrome-extension://…`,
`moz-extension://…`). The general web-security boundary admits such an origin
**only under `/api/browser/v1/`**, where no route accepts an ambient
credential. Pairing start and poll take no credential, and every other route
there admits only a browser token sent as `Authorization: Bearer`. A web
page cannot claim an extension origin, and outside this prefix an extension
origin is refused as any foreign origin is. The person-facing routes live
under `/api/account/` and keep every ordinary protection: session, CSRF and
same origin.

An extension must call this server with `credentials: "omit"`. If a session
cookie rides along, the session is what authenticates the request, and it is
refused here (`403`).
