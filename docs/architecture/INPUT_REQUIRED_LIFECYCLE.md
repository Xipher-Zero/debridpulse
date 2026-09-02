# Neutral `INPUT_REQUIRED` lifecycle

Roadmap Item 3 defines one universal nonterminal transfer condition for work that
cannot continue until a user supplies transient input. The initial reason is
`AUTH_REQUIRED`. Roadmap Item 4 adds the generic browser interaction that consumes
that existing contract; it does not change the transfer-state model.

`INPUT_REQUIRED` is not a provider state, executor state, retry state, failure,
cancellation, or pause. The transfer remains the same logical DebridPulse
transfer: its top-level ID, accepted requests, artifacts, destination ownership,
and applicable attempt history remain intact while progress is suspended.
Authentication refusal is translated to this neutral contract at an integration
boundary. The universal engine and browser do not branch on native authentication
codes.

## Challenge contract

A current challenge identifies what input is needed without containing the input
itself. The durable record contains only:

- `transfer_id`
- `challenge_id`
- `generation`
- `reason`
- `origin`
- `integration_id`
- `operation_id`
- `request_id`
- `artifact_id`
- `methods`
- `created_at`
- `updated_at`

`challenge_id` plus `generation` identifies the currently authorized response.
Replacing a rejected challenge creates a new identity/generation; a delayed
response to an older challenge is rejected. Native provider or executor IDs are
not used as the authorization token.

`origin` tells the engine which operation is eligible to continue. `methods` is
descriptor metadata only. Each method lists the exact allowed fields and whether
each field is required.

The current contract defines exactly two methods:

| Method | Fields |
| --- | --- |
| `username_password` | `username` required; `password` required |
| `username_private_key` | `username` required; `private_key` required; `passphrase` optional |

A passphrase is **not** an authentication method. It is an optional sensitive
field belonging to `username_private_key`, so an unencrypted private key is valid
without one.

## Generic browser interaction

The browser observes canonical transfer presentation. When a transfer presents
`INPUT_REQUIRED` with reason `AUTH_REQUIRED`, it opens the shared generic
**Authentication Required** interaction. The browser does not infer authentication
requirements from URL schemes, hostnames, transfer sources, provider IDs,
executor IDs, filenames, native errors, or protocol parsing.

The backend challenge is authoritative:

- `username_password` supplies Username + Password.
- `username_private_key` supplies Username + private-key material and an optional
  Passphrase.
- `Select Keyfile` exists only when the current challenge advertises
  `username_private_key`.
- If both methods are advertised, the interaction begins in password mode.
- Selecting a locally accepted key switches Password to Passphrase; the key
  control itself is the method switch, so no protocol-specific tabs or selectors
  are required.

`Select Keyfile` invokes the browser-native file picker. The browser reads the
chosen file and keeps the resulting key material only in the current modal
session. Basic local ingestion rejects unreadable, empty, oversized, or
structurally unrecognized private-key material before any remote authentication
submission. Local acceptance does not claim that the remote side will accept the
key, that it belongs to the username, or that a passphrase is correct.

A locally accepted key gives the control a green, textual **Key supplied** state.
Green means only that private-key material was supplied to DebridPulse for this
attempt. It does **not** mean authenticated, authorized, verified, or remotely
accepted. Clicking the selected-key control reopens the native picker so the key
can be replaced. A failed replacement does not silently replace a previously
accepted key.

## Submission and success semantics

`Continue` submits the selected canonical method against the same logical
transfer and the current challenge identity. The modal remains visible and busy
while the authentication attempt is pending; duplicate Continue submissions and
key replacement are disabled during that interval. An HTTP success response only
acknowledges submission and does not close the modal.

The browser closes the modal when backend presentation confirms that the current
`AUTH_REQUIRED` challenge has been resolved and the same logical transfer has
left `INPUT_REQUIRED`. Active downloading is **not** required. The transfer may
legitimately remain queued, paused, globally paused, ready, or waiting for
capacity after authentication succeeds.

If the backend regenerates the challenge after rejected credentials, the modal
stays open, adopts the new challenge identity, re-renders the advertised methods,
and never resubmits against the stale identity. Username/password/key/passphrase
values that remain valid for the new challenge stay available only in the current
browser session so the operator can correct the rejected input. If key auth is
removed by the new challenge, selected-key and passphrase state is cleared.

Local key-ingestion failure and remote authentication rejection are deliberately
different classes. A local key failure creates no remote request. A remote
rejection produces a sanitized inline authentication failure and permits another
Continue without closing/reopening the dialog or creating a replacement transfer.

## Cancellation and multiple challenges

Cancel resolves the canonical pending interaction through the existing transfer
cancel lifecycle; it is not a frontend-only hide operation. Escape and overlay
dismissal use that same canonical cancellation path when dismissal is allowed.
During an in-flight submission those dismissal paths are disabled, preventing a
hidden unresolved challenge.

Only one secret-bearing authentication dialog is shown at a time. Simultaneous
`AUTH_REQUIRED` transfers are presented through a deterministic queue. Challenge
identity and transient values remain bound to their transfer, and closing or
cancelling one interaction cannot resolve or populate another.

## Sensitive input boundary

The submitted bundle is transient sensitive input. That classification covers
all of `username`, `password`, `private_key`, and `passphrase`; a username is not
made durable merely because it may be less secret than the other fields.

Submitted values are accepted only for the current transfer/challenge and the
selected advertised method. Undeclared fields, missing required fields, stale
challenge identities, duplicate pending submissions, and cross-transfer use are
rejected before an integration receives the bundle.

The transient backend broker is process-local and bounded. An accepted bundle is
consumed once for the authorized continuation, discarded after use or expiry, and
cleared on challenge invalidation. Provider continuation reacquires normal
resolution capacity before taking the bundle; executor continuation checks normal
execution capacity before taking it. A credential response therefore does not
bypass ordinary concurrency policy.

The browser likewise implements no credential cache, `remember me`, saved
credential profile, or persistent key state. Username/password/key/passphrase are
not written to localStorage, sessionStorage, IndexedDB, cookies, settings,
telemetry, URLs/history, or browser console output. Reloading the page discards
all browser-side secret material.

Credential values must never be written to transfer/request serialization,
resolution or execution attempts, transfer outcomes, controls, application or
activity events, notifications, diagnostics, tracebacks, provenance, settings,
the integration runtime-state store, database backups, migration payloads,
URLs/query strings, logs, object representations, or API responses. The challenge
table and runtime-state table are separate non-secret persistence domains; neither
is a credential store.

The application deliberately makes no claim of cryptographic memory erasure in
Python or JavaScript-managed memory. Cleanup drops retained references and clears
owned value containers; it does not promise that interpreter/browser-managed
memory has been physically overwritten.

## Restart behavior

Non-secret challenge metadata is durable. After process or browser restart, the
same transfer can still present `INPUT_REQUIRED`, `AUTH_REQUIRED`, the current
challenge identity/generation, its origin, and its accepted methods/field
descriptors.

Submitted responses are intentionally not durable. After browser reload, the
modal is rediscovered from backend presentation but its prior username, password,
private key, and passphrase are gone. If the backend process exits after receiving
a bundle but before authorized continuation consumes it, that bundle is also lost
and fresh user input is required. These are expected security properties, not
permission to persist credentials.

## Provider and executor continuation

Providers may return an `InputRequirement` from resolution and, if they support
that path, implement `resolve_with_input(request, submitted)`. Executors may
return an `InputRequirement` from `prepare` before external mutation or execution
begins and implement `prepare_with_input(request, submitted)`.

Provider credentials are delivered only to the provider that issued the provider
challenge. Executor credentials are delivered only to the executor that issued
the executor challenge. There is no shared credential repository and neither
integration family reaches into the other's settings or submitted values.

A rejected credential may cause the integration to return another
`AUTH_REQUIRED` requirement. The engine replaces the old challenge, discards the
old bundle, and leaves the transfer nonterminal. It does not automatically retry
credentials, consume the ordinary retry budget merely because input was missing,
or cycle mirrors/candidates as a substitute for authentication.

An internal bug while processing a challenge remains an ordinary normalized
failure. `INPUT_REQUIRED` is not used to hide adapter/programming failures.

## Scheduling and controls

A current challenge blocks **new** provider/executor work for the affected
transfer. It does not pretend already-started external work stopped: existing
sibling execution handles continue to be observed and reconciled. A safely
suspended pre-execution auth wait therefore releases ordinary capacity while
preserving durable ownership and path reservations.

Submitting input resolves only the missing-input condition. It does not override:

- global pause;
- per-transfer pause intent;
- cancellation or deletion;
- execution/resolution capacity;
- integration enablement or availability;
- destination ownership and security checks;
- normal scheduler admission.

Pause intent can coexist with `INPUT_REQUIRED`; pause/resume does not overwrite the
challenge. Cancellation and deletion invalidate the challenge and clear pending
in-memory input. `cancelled`, `completed`, and `deleted` transfers are terminal
for input submission and excluded from scheduler admission.

## API

The transfer read model exposes only public challenge descriptors under
`input_required`. The neutral input command is:

`POST /api/torrents/{transfer_id}/input`

The request body contains the public `challenge_id`, selected `method`, and only
the fields allowed for that method. The response acknowledges the accepted
challenge without echoing submitted values. Rejections use generic HTTP errors
that do not include credential material.

The existing canonical cancellation command resolves an abandoned pending auth
interaction rather than leaving an invisible challenge:

`POST /api/torrents/{transfer_id}/cancel`

## Explicitly deferred

Roadmap Item 4 does not add saved credentials, credential profiles/discovery,
filesystem key discovery, SSH-agent integration, browser credential storage,
persistent passphrases, a vault, production HTTP(S)/FTP/SCP/SFTP/SSH/rsync
integration, classifier/applicability logic, AllDebrid dynamic host-support
plumbing, or later routing/provenance work. Future integrations gain this browser
interaction by emitting the same neutral challenge contract rather than adding a
protocol-specific authentication modal.
