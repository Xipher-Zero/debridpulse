# Browser client: pairing, browser tokens and downloads (`downloadpulse.browser/1`)

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
  `{protocol: "downloadpulse.browser/1", token: {id, label}, account: {display_name}}`.
  It is how the extension verifies its pairing and the server's protocol, and it
  names what this server offers a browser: `capabilities: {sources, controls,
  withdraw, estimate}`.
- **The person's appearance.** The same answer carries `appearance: {mode,
  dark_palette, light_palette}`: the token's person's own choice, read from its
  one owner (`services.account_appearance.load`; a retired palette reads as its
  mode's native one), as palette IDs. The extension draws its own pages in it
  and carries the palettes' colours itself; an ID it does not know is drawn as
  that mode's native palette. A change in DownloadPulse is the extension's next
  read.

## 2a. A browser's downloads

A browser's submission is that person's own download, admitted exactly as
Quick Add admits it: same core paths, same demand attribution (the person
and this browser's token), into their normal Downloads. Its job belongs to
the one external-client owner (`external/service.py`), as a **browser** job
(`external_jobs.kind`). It has no category, delivery profile, delivery or
acknowledgement. Everything else is the external protocol's, unchanged:
client-scoped idempotency, the change-cursor listing, isolation, demand-only
withdrawal, advisory pause/resume and the per-client limits.

| Route | Does |
|---|---|
| `POST /api/browser/v1/jobs` `{idempotency_key, source, display_name?, client_reference?}` | Submit. `201` new, `200` the same key and intent again; another intent under that key is `409 idempotency_conflict`. |
| `GET /api/browser/v1/jobs?after&limit&phase` | This browser's jobs by change cursor. |
| `GET /api/browser/v1/jobs/{id}` | One of them; another client's is `404`. |
| `POST /api/browser/v1/jobs/{id}/withdraw` | Remove: withdraw this browser's request. The download stops only if nobody else needs it; finished files are kept. |
| `POST /api/browser/v1/jobs/{id}/controls` `{kind: pause\|resume, request_key}` | The protocol's advisory controls. Honoured only while this browser's request is the only one the download serves, otherwise declined with its reason (`shared_interest`, `globally_paused`, `paused_independently`, …). |

- **Sources.** `links` (1–100), `magnet`, `nzb_link`, `nzb`, `torrent`,
  `metalink` (a `.meta4` file, as DownloadPulse's own upload takes it).
  - A media page is a link: core's routing decides, as for Quick Add.
  - A link that needs an item-or-collection answer is refused
    (`acquisition_scope_required`).
- **Private-LAN links** follow the installation's **Local Network Connections**
  and **Skip Local Connection Confirmation** settings, exactly as for any
  submitter that cannot answer the confirmation. Otherwise they are refused
  (`local_network_confirmation_required`). A browser never consents.
- **The job view:** `id`, `revision`, `cursor`, the timestamps, `display_name`,
  `client_reference`, `name` (the transfer's title as DownloadPulse shows it:
  the Downloads list's derived display name, so a media download's resolved
  title replaces its link's placeholder; a change moves the cursor), `source`
  (a summary without query, credentials or payload), `phase`, `reason`, `actionability`, `paused_by`, `error`,
  `progress`, `estimate` and `control`. A completed download is settled.

## 2b. Open DownloadPulse (the desktop application)
`POST /api/browser/v1/application/show` (a browser token; nothing else is
admitted). On the installed desktop application it asks its supervisor to bring
the window forward -- exactly the tray's Open: a window closed to the tray is
shown again, a minimized one asked back, a shown one raised -- and answers
`{"desktop": true}`. The request is a flag on the backend's private control
channel (`application.desktop_control`, op `show`), which the supervisor reads
every half second; asked twice before it looks is shown once. Whether the
window may take the focus is the operating system's decision (Windows and some
Wayland desktops may flash the taskbar instead). A server has no window: it
answers `{"desktop": false}` and changes nothing, and the extension opens the
page in a tab, as it does for an older DownloadPulse (`404`).

## 3. Disconnecting

| Who | Route |
|---|---|
| The person, their own browsers | `GET /api/account/browsers`; `DELETE /api/account/browsers/{token_id}` |
| The browser itself (the extension's Disconnect) | `DELETE /api/browser/v1/session`: the token carrying the request is revoked, journaled as by its owner |
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
