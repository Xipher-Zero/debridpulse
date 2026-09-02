# Neutral `INPUT_REQUIRED` lifecycle

Roadmap Item 3 adds one universal nonterminal transfer condition for work that
cannot continue until a user supplies transient input. The initial and only
reason defined by this item is `AUTH_REQUIRED`.

`INPUT_REQUIRED` is not a provider state, executor state, retry state, failure,
cancellation, or pause. The transfer remains the same logical DebridPulse
transfer: its top-level ID, accepted requests, artifacts, destination ownership,
and applicable attempt history remain intact while progress is suspended.
Authentication refusal is translated to this neutral contract at an integration
boundary. The universal engine does not branch on native authentication codes.

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

`origin` is either `provider` or `executor` and tells the engine which operation
is eligible to continue. `methods` is descriptor metadata only. Each method
lists the exact allowed fields and whether each is required.

Roadmap Item 3 defines exactly two methods:

| Method | Fields |
| --- | --- |
| `username_password` | `username` required; `password` required |
| `username_private_key` | `username` required; `private_key` required; `passphrase` optional |

A passphrase is **not** an authentication method. It is an optional sensitive
field belonging to `username_private_key`, so an unencrypted private key is valid
without one.

## Sensitive input boundary

The submitted bundle is transient sensitive input. That classification covers
all of `username`, `password`, `private_key`, and `passphrase`; a username is not
made durable merely because it may be less secret than the other fields.

Submitted values are accepted only for the current transfer/challenge and the
selected advertised method. Undeclared fields, missing required fields, stale
challenge identities, duplicate pending submissions, and cross-transfer use are
rejected before an integration receives the bundle.

The transient broker is process-local and bounded. The current implementation
keeps an accepted bundle for at most 120 seconds while waiting to be consumed.
It is consumed once for the authorized continuation, discarded after use or
expiry, and cleared on challenge invalidation. Provider continuation reacquires
normal resolution capacity before taking the bundle; executor continuation checks
normal execution capacity before taking it. Thus a credential response does not
bypass concurrency policy or sit in memory merely because capacity is unavailable.

The application deliberately makes no claim of cryptographic memory erasure in
Python. `discard()` drops retained references and replaces the local value map;
it does not promise that prior interpreter-managed memory has been overwritten.

Credential values must never be written to transfer/request serialization,
resolution or execution attempts, transfer outcomes, controls, application or
activity events, notifications, diagnostics, tracebacks, provenance, settings,
the Item 2 integration runtime-state store, database backups, migration payloads,
URLs/query strings, logs, object representations, or API responses. The challenge
table and Item 2 runtime-state table are separate non-secret persistence domains;
neither is a credential store.

## Restart behavior

Non-secret challenge metadata is durable. After process restart, the same
transfer can still present `INPUT_REQUIRED`, `AUTH_REQUIRED`, the current
challenge identity/generation, its origin, and its accepted methods/field
descriptors.

The submitted response is intentionally not durable. If the process exits after
receiving a bundle but before the authorized continuation consumes it, that bundle
is lost and fresh user input is required. This is expected behavior, not a
recovery failure and not permission to persist credentials.

## Provider and executor continuation

Providers may return an `InputRequirement` from resolution and, if they support
that path, implement `resolve_with_input(request, submitted)`. The engine records
the provider challenge and stops contacting that request until a current response
exists. The wait does not consume the resolution retry budget. Continuation uses
the ordinary resolution-concurrency semaphore.

Executors may return an `InputRequirement` from `prepare` before external mutation
or execution begins and implement `prepare_with_input(request, submitted)`. The
engine stores the executor challenge against the artifact. Waiting for input does
not reserve an execution slot because no external execution has started. A valid
response still has to satisfy the normal execution-capacity limit before it is
consumed.

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
challenge. Cancellation and deletion invalidate the challenge and clear any
pending in-memory response. `cancelled`, `completed`, and `deleted` transfers are
terminal for input submission and are excluded from scheduler admission.

## API and current browser presentation

The existing transfer read model exposes only the public challenge descriptors
under `input_required`. Item 3 adds the neutral command:

`POST /api/torrents/{transfer_id}/input`

The request body contains the public `challenge_id`, selected `method`, and only
the fields allowed for that method. The response acknowledges the accepted
challenge without echoing submitted values. Rejections use generic HTTP errors
that do not include the credential material.

The current browser only needs to tolerate and neutrally display the
`input_required` transfer state. Roadmap Item 4 owns the generic authentication
modal and interactive credential-entry UI; Item 3 intentionally does not add it.

## Explicitly deferred

Roadmap Item 3 does not add saved credentials, credential profiles/discovery,
filesystem key discovery, SSH-agent integration, browser credential storage,
persistent passphrases, a vault, production SSH/SFTP, HTTP(S) provider integration,
classifier/applicability logic, AllDebrid dynamic host-support plumbing, later
routing/provenance work, or any authentication modal. Those remain separate
roadmap work.