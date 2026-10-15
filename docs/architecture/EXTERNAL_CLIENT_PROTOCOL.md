# External Client Protocol v1 (`debridpulse.external/1`)

DebridPulse 1.0.13 lets external programs act as requesters: an *arr installation,
a browser extension, a script. This is the native foundation they use. A
compatibility adapter (for example a qBittorrent- or SABnzbd-shaped API for *arr)
is a later, separate layer that calls only these operations.

> External clients own their logical requests and delivery interests.
> DebridPulse exclusively owns canonical transfers, execution decisions and
> artifact lifecycle.

An external client can submit demand, inspect its own jobs and deliveries,
acknowledge a delivery, withdraw its demand, and request an advisory pause or
resume. It cannot read, operate, reorder, prioritize, route, retry or delete a
transfer. It cannot choose a provider, executor, candidate, queue position or
destination.

Owners in the code:

- `backend/external/service.py`: the contract.
- `external/arr.py`, `api/arr_sabnzbd.py` and `api/arr_qbittorrent.py`: the
  *arr compatibility adapter (`ARR_SABNZBD_COMPATIBILITY.md`,
  `ARR_QBITTORRENT_COMPATIBILITY.md`).
- `external/intake.py`: source normalization.
- `external/profiles.py`: delivery profiles.
- `external/store.py`: schema.
- `api/external_routes.py`: HTTP routes.
- `auth/accounts.py`: client binding and rotation.
- `auth/permissions.py`: the `external.client` capability.

## 1. Authentication and credential scoping

- **Credential.** A generated Integration token (`dp_…`) sent as
  `Authorization: Bearer <token>` through the existing authentication boundary.
  There is no second credential system, no session and no CSRF path. An invalid
  or revoked token gets `401` on its very next call.
- **Capability.** Every client route requires `external.client`. Only the
  Integration role holds it; Admin does not, so an Admin session gets `403`.
  Integration still has no `transfers.read`, `transfers.operate` or
  `transfers.destroy`. Even when a job serves a transfer, `/api/torrents/*`
  answers that token with `403`.
- **Client binding.** Each token has a `client_id` (`account_tokens.client_id`).
  A newly issued token is a new client. Two tokens of one Integration account are
  two clients, and neither sees the other's jobs.
- **Rotation.** An Admin rotates with
  `POST /api/accounts/{id}/tokens {"label", "succeeds": <token id>}`:
  - The new token continues the predecessor's client: its jobs, deliveries and
    controls.
  - The predecessor is revoked in the same transaction.
  - The act is journaled (`administration.integration_token_rotated`).
  - A predecessor must belong to the same account, or to an account merged into it.
  - It may already be revoked. Rotating from a revoked token is the explicit
    administrative recovery for a client whose token was lost.
  - Without a configured successor, the client's jobs persist but no other
    credential can reach them.
- **Revocation and accounts.**
  - Revoking a token stops authentication and withdraws nothing.
  - Disabling or deleting an account withdraws its outstanding demand, under the
    existing account policy. Those jobs then read `withdrawn` with reason
    `withdrawn_by_administrator`.
  - A merged account's jobs remain reachable through an authorized successor
    token on the surviving account.
  - Attribution (account, token label) is kept as the demand ledger keeps it.

## 2. Jobs and idempotency

- **Job ID.** A job is one submission: `job_<hex>`, opaque and immutable. It is
  never the transfer, demand, provider-resource or delivery ID; the transfer ID is
  not exposed.
- **Idempotency key.** `idempotency_key` is required: 1–200 printable characters,
  scoped to the client. There is no content-based deduplication.
  - Same client and key with the same normalized intent: the same job (`200`),
    and never a second demand.
  - Same client and key with a different intent (source, category/profile,
    `display_name`, `client_reference`): `409 idempotency_conflict`. Nothing is
    admitted.
  - Different keys, even with identical content, are distinct jobs with distinct
    demand. The work itself may still converge on one canonical transfer.
  - Different clients never collide.
- **Atomicity.**
  1. The job is reserved with a demand ID chosen before admission.
  2. The existing submission seam admits the transfer with exactly that demand
     (`SubmissionDemand.id`).
  3. The job is accepted exactly when that demand row exists.

  After a crash between steps 1 and 2, the retry admits once. After a crash
  between steps 2 and 3, the retry, or the 15-second maintenance pass, binds the
  job from the ledger. A reservation that never admitted anything (its ID was
  never returned) is forgotten after 24 hours.
- **Limits.**
  - 2000 unsettled jobs per client: `429 client_job_limit`.
  - 120 mutations per client per minute: `429 rate_limited`.
  - 64 control requests per job: `429 control_limit`.
  - Pages of at most 200 jobs.

## 3. Submission kinds (support matrix)

| `source.kind` | Fields | Existing owner |
|---|---|---|
| `links` | `links`: 1–100 direct links (any scheme the direct-link owner accepts: http(s), ftp/sftp/scp/rsync/WebDAV-style) | `ApplicationService.submit_links` |
| `magnet` | `magnet` | `submit_magnet` |
| `nzb_link` | `url` (http/https) | `submit_nzb_link`: guarded fetch, staged, NZB-validated |
| `nzb` | `nzb_base64` (≤ 8 MB decoded), `filename` (`*.nzb`) | `submit_nzb(validate=True)`: staged, NZB-validated before admission |
| `torrent` | `torrent_base64` (≤ 16 MB decoded, DebridPulse's own torrent upload ceiling), `filename` (`*.torrent`) | `submit_torrent` |

Not supported in v1:

- Metalink uploads, link files, Media Downloads as a distinct kind, and
  alternate-source rows.
- Each is refused with `unsupported_source`. No other kind is substituted.

Every external submission:

- uses `selection_mode=all`, with no interactive file selection;
- never answers an operator-only question. If one is needed, the submission is
  refused and nothing is admitted:
  - private-LAN consent: `local_network_confirmation_required`;
  - acquisition scope: `acquisition_scope_required`.

Network safety, SSRF and egress rules, entitlement and routing are exactly those
of an interactive submission. An input that needs operator credentials after
admission (input required) appears as `processing` with
`reason=input_required, actionability=operator`. One client is never given
another client's input.

## 4. Categories and delivery profiles

An Admin owns the catalog through `GET`/`PUT /api/external/profiles`, and edits
it in Settings → Users & Access → **Integration Delivery Profiles**.

- **Lost updates.** The PUT takes `{profiles, expected_revision}` and returns
  `409 stale_revision` on a lost update.
- **Settings document.** The whole-settings `PUT /api/settings` carries the
  catalog forward unchanged.
- **Profile in use.** A PUT that would take a profile away from jobs that still
  depend on it is refused with `409 profile_in_use`, giving per profile `id`,
  `change` and `affected_jobs`, plus the total. Nothing is changed. A dependent
  job is a reservation still being admitted, or an accepted job that is not
  settled: not withdrawn, delivery neither acknowledged nor expired, work not
  cancelled or deleted. The refused changes are:
  - deleting the profile;
  - moving its `local_root` or effective `import_root`;
  - removing a client that such a job belongs to.
- **Always editable.** Categories, source kinds, retention and added clients
  govern only what is admitted or committed next.
- **No admission race.** The count and the catalog replacement happen inside one
  database write transaction. An external reservation re-selects its profile
  inside its own, so a submission is either counted or refused, never admitted
  under a profile that has just been removed or narrowed.

| Field | Meaning |
|---|---|
| `id` | stable profile ID |
| `categories` | external categories selecting it (case-insensitive, unique across profiles) |
| `clients` | client IDs allowed to use it (explicit; empty admits nobody) |
| `source_kinds` | permitted kinds |
| `representation` | `shared_path` (the only one in v1) |
| `local_root` | DebridPulse's view of the shared directory: the download folder or beneath it |
| `import_root` | the same directory as the client sees it (default: `local_root`) |
| `retention_hours` | how long a committed delivery is advertised (1–8760, default 168) |

A category is a selector, never a path.

- An unknown category, or one the client is not authorized for, is
  `unknown_category`; the two cases are indistinguishable.
- A profile cannot set a provider, executor, priority, target path, internal
  directory or post-processing.
- DebridPulse writes downloads where it always does. A profile only confines and
  maps delivery: nothing is copied, linked or moved.

## 5. Status

Each job has its own status, never a blind copy of the shared transfer.

| `phase` | Meaning |
|---|---|
| `accepted` | durable; awaiting resolution |
| `queued` | waiting for capacity |
| `processing` | resolving, downloading, verifying, finalizing, paused (`paused_by`: `client` / `operator` / `administrator`), input required, or served by another transfer still in progress |
| `completed` | a delivery generation was committed for **every** fulfilling artifact |
| `failed` | the existing core's outcome is failed (`acquisition_failed`, operator may retry), the work was `cancelled`/`deleted`, or the completed material cannot be delivered truthfully (see below) |
| `withdrawn` | this job's demand was withdrawn (`withdrawn_by_client`, `withdrawn_by_administrator`, `withdrawn`) |

Each job also carries:

- `reason`;
- `actionability`: `none`, `wait`, `operator` or `resubmit`;
- `progress`: `basis=members`, `completed_members`, `total_members`, and
  `percent` (or `null` when no own figure exists);
- `error`: category and retryability only;
- `revision`: increments on every public change;
- `cursor`: a global change sequence.

A provider refusal, failover, recovery or executor restart is not a terminal
external failure. The phase follows the core.

**Fulfilling artifacts:**

- the transfer's own completed canonical members;
- every canonical artifact of another transfer that one of its requests was
  consolidated into, or is terminally associated with.

A consolidated job completes only when the owning transfer has completed,
including its finalization. One missing member is never `completed`. While some
members are done, the delivery state is `partial`, with counts and no paths.

**Finalization.** When DebridPulse ran post-processing (archive extraction),
the job is ready only when it finishes. What the job then delivers is the final
file set that post-processing recorded (`PostProcessOutcome.outputs`): the files
it extracted plus the delivered inputs it kept. That set is taken from exactly
what each extraction wrote, never from a directory walk, so other transfers'
files in the same folder are never included. Each file is re-verified on disk at
every inspection. Its modification time is part of the material key, so a
re-extraction over the same paths is a new generation.

**Not deliverable.** These leave the phase `failed` with
`actionability=operator`, and the job is re-evaluated on every refresh:

- `finalization_failed`;
- `extraction_outputs_unrecorded`: a finalization recorded before output sets
  were kept;
- `extraction_outputs_unattributable`: another transfer's extracted set is
  delivered only when this job's requests cover all of that transfer's members;
- `outside_export_root`;
- `profile_unavailable`;
- `profile_root_outside_downloads`;
- `material_missing` (before any commit);
- `shared_transfer_failed`.

While executors are still starting after a restart, the job reads
`processing / verifying_material`, never "missing".

## 6. Delivery

`GET /api/external/v1/jobs/{id}/delivery` returns:

- `delivery_id`: stable per job;
- `generation`;
- `state`: `unavailable`, `partial`, `available`, `acknowledged` or `expired`;
- `reason`, `committed_at`, `expires_at`, `acknowledged_at`;
- when `available` or `acknowledged`: `representation=shared_path`,
  `import_root`, `complete=true`, `total_bytes`, and `files[]` with `name`,
  `relative_path`, `import_path` and `size`.

DebridPulse's own local root is never returned unless the Admin set no separate
`import_root`.

**Commitment:**

1. Every fulfilling artifact is re-verified through the engine's delivery-time
   verification (`engine.delivered_material`).
2. Every path must lie beneath the profile's `local_root`, with every component a
   real directory and the leaf a regular file. Symlinks are refused.
3. A generation is committed against the material key: artifact IDs, material
   generations, relative paths, sizes and both roots.

A different key commits a **new** generation. The old row is kept, marked
superseded and never mutated. Each inspection re-verifies. A list poll
re-verifies an available delivery at most every 60 seconds.

- **Material loss:** `unavailable / material_missing`, with no paths. The phase
  stays `completed`; the delivery shows the loss.
- **Retention:** after `retention_hours` the delivery is `expired` and its paths
  are no longer advertised. Nothing is deleted. Storage is reclaimed only by
  DebridPulse's existing lifecycle, and Delete never removes completed material.
  Reacquisition happens only through the existing demand and recovery policy.

## 7. Withdrawal vs acknowledgment

| | Withdraw `POST …/withdraw` | Acknowledge `POST …/delivery/ack {delivery_id, generation}` |
|---|---|---|
| When | before completion (allowed any time) | after a generation is committed |
| Effect | retires **this job's** demand through the existing ledger; the core cancels the work only if nobody else still needs it | records that this job's interest in **that exact generation** is satisfied |
| Never | cancels shared work, touches another requester's demand, deletes files | deletes or releases material, affects another job's delivery |
| Retry | idempotent (`already_withdrawn`) | idempotent (`already_acknowledged`) |
| Conflicts | — | `delivery_mismatch`, `unknown_generation`, `stale_generation` (`current_generation` given) |

An external "remove" before completion is a withdrawal; after completion it is an
acknowledgment. A `deleteFiles` flag is never deletion authority.

## 8. Advisory pause and resume

`POST /api/external/v1/jobs/{id}/controls {kind: pause|resume, request_key}` is
idempotent per `request_key`. Reusing a key for the other kind returns
`409 control_conflict`.

DebridPulse decides and applies the decision on the same serialized
operator-control boundary as Pause and Resume, through the core transition
(`ApplicationService.adjudicated_control`). The response carries:

- `disposition`: `honored`, `declined`, `revoked`, `superseded`, or `pending`
  (an interrupted request not yet settled);
- `reason`;
- `effective`: the state observed now at the pause owner:
  - `pausing` or `paused`: this job's own hold is still in force;
  - `held_by_operator`: the transfer is paused by an operator or global control;
  - `not_paused`.

`honored` with `effective=pausing` means the pause was applied but the transfer
has not yet been observed paused.

**Hold ownership.** An honored pause is applied by the existing pause owner
(`TransferRepository.set_pause_and_fence`) as a named hold: `holder` =
`external:<control id>`, plus the intent's `generation`. Every write to
`transfer_pause_intents` advances `generation` (operator or global pause, Resume
All, settlement, a route switch's fence), whatever its timestamp, and an
operator's write carries no holder.

A client resume, and a reconciliation release, use the owner's fenced release
(`engine.resume(held_by=(holder, generation))`). In one transaction it lifts the
intent only while it is still exactly that hold and processing is not globally
paused. Otherwise it writes nothing, and the request is declined
(`ownership_unproven` / `paused_independently`) or retired
(`operator_changed_pause`). Both values are durable, so this holds across
restart and recovery.

| Request | Decision |
|---|---|
| pause, this job is the transfer's **only** outstanding demand | `honored / paused` |
| pause, others need the work | `declined / shared_interest` |
| pause while globally paused / already paused by an operator | `declined / globally_paused` / `paused_independently` |
| pause or resume of a completed, consolidated, cancelled or deleted transfer | `declined / already_settled` |
| pause or resume of a withdrawn job | `declined / job_withdrawn` |
| resume of this client's standing pause | `honored / resumed` |
| resume when not paused | `honored / not_paused` (no side effect) |
| resume while an operator or global pause holds | `declined / paused_independently` / `globally_paused` (a client never lifts an operator or global hold) |
| maintenance or unsafe storage | `declined / maintenance_active` (`retryable`) |

**Interruption.** A decision that applies a transition is first recorded
`pending`, together with the hold it applies or releases and the pause-intent
generation observed before it. That record is committed before the transition.
After a crash, cancellation or restart, a retry with the same `request_key`, or
the maintenance pass, settles it from what the pause owner shows, without
re-applying anything:

- pause, the intent is still exactly this control's hold: `honored / paused`
  (the client's own hold, released as usual);
- pause, the generation is unchanged since the record: `declined / interrupted`
  (nothing was written);
- resume, the client's hold still stands: `declined / interrupted`;
- otherwise: `superseded / interrupted_unconfirmed`. Something wrote the intent
  since, possibly an operator, so nothing is touched, and a resume's pause
  control is retired.

A retry never creates a second hold.

**Re-adjudication.** A standing client pause is re-adjudicated on withdrawal, on
every client control, and by the 15-second maintenance pass:

- If another requester's demand now shares the work, DebridPulse releases the
  pause (`revoked / shared_interest_joined`), unless a global pause holds the
  transfer anyway.
- If the operator replaced it, or Resume All cleared it, it is retired
  (`operator_changed_pause`).
- A pause never becomes permanent client ownership.

## 9. Polling and reconciliation

`GET /api/external/v1/jobs?after=<cursor>&limit=<≤200>&phase=` returns the
client's jobs whose `cursor` exceeds `after`, in cursor order, with
`next_cursor`, `more` and `high_water`.

- Starting from `0` lists everything.
- Store `next_cursor` and poll again to receive exactly the jobs that changed.
- Each poll first refreshes up to 200 of the client's unsettled jobs, least
  recently refreshed first.
- Polling is authoritative. v1 has no event stream (`capabilities.events.supported
  = false`), and the live browser stream is never exposed to clients.

**After a restart:**

- Projections rebuild from the demand ledger, the transfer and the filesystem.
- Job IDs, delivery IDs and generations, revisions and cursors are durable.
- No demand is manufactured.

**Retention of jobs.** Jobs are kept as long as the demand ledger they name, so a
withdrawn or acknowledged job stays queryable. Only the explicit Admin database
reset (Data & Maintenance) removes them. It removes them child-first, together
with the transfer history, and keeps tokens and their client bindings. After a
reset, a missing job is `404`: the client must treat it as gone, not as an
invitation to resubmit silently.

## 10. Errors and privacy

| Status | Meaning |
|---|---|
| `401` | missing, invalid or revoked token |
| `403` | not an external client credential (`client_credential_required`), or a route outside `external.client` |
| `404` | `job_not_found`: unknown or another client's job, indistinguishable |
| `409` | `idempotency_conflict`, `control_conflict`, `delivery_mismatch`, `unknown_generation`, `stale_generation`, `stale_revision`, `duplicate_torrent` (live submission only) |
| `413` / `422` | an invalid, unsupported or refused submission |
| `429` | `rate_limited`, `client_job_limit`, `control_limit` |
| `503` | `source_unavailable` or `submission_outcome_unknown` (retry with the same key) |

Refusals are `{"detail": {"code", "message", "retryable", …}}`.

What clients see:

- A job's `source` is a sanitized summary only: kind, count, hostnames,
  info-hash and declared name.
- Never a full link, query, credential or payload.

What the audit trail records, in the event journal (category `transfer`, subject
`external_job`):

- accept, withdraw, control decision, delivery commit, acknowledgment,
  unavailability and expiry;
- job and client IDs only, never sources, tokens or paths.

Idempotent replays and denials are logged, not journaled.

## 11. Example client workflow

```
GET  /api/external/v1/capabilities                      → profiles, kinds, limits
POST /api/external/v1/jobs {"idempotency_key":"sonarr-4711","category":"tv",
     "source":{"kind":"magnet","magnet":"magnet:?xt=…"},"client_reference":"4711"}
                                                        → 201 {"id":"job_…","phase":"accepted",…}
GET  /api/external/v1/jobs?after=0                      → store next_cursor
… poll ?after=<next_cursor> …                           → phase completed, delivery available
GET  /api/external/v1/jobs/job_…/delivery              → files[].import_path
(import from the shared path)
POST /api/external/v1/jobs/job_…/delivery/ack {"delivery_id":"dlv_…","generation":1}
```

### Live submission (in-process, for a protocol without request IDs)

`ExternalClients.submit_live` serves a client protocol whose caller sends no
request identity: the SABnzbd dialect's `addfile` and the qBittorrent
dialect's `torrents/add`.

- **Source kind.** It takes an `nzb`, `magnet` or `torrent` source and no
  idempotency key.
- **Torrent identity.** For a magnet or `.torrent`, the identity is the v1
  info-hash the intake computed. One client holds at most one visible job per
  hash: the same hash in the same category answers that job while it is live;
  while it is visible but failed, or under another category, the submission is
  refused (`409 duplicate_torrent`); once it has left the view, it is a new job.
- **Derived key.** This owner derives the key from the client and the exact NZB
  bytes, category and filename, plus a sequence number.
- **Reuse.** An identical submission answers the latest such job only while that
  job is still live: not withdrawn, failed, acknowledged or expired. Otherwise it
  is a new job. Concurrent identical submissions serialize on that identity.
- **Guarantee.** This is best-effort, not exactly-once.
- **Reach.** It is not reachable over this HTTP API, which still requires a caller
  key.

`ExternalClients.visible` lists the client's jobs still in its view: unfinished
oldest first, finished newest first, with completed deliveries re-verified,
optionally limited to some source kinds. An adapter needs it to show a whole
queue, history or torrent list.

## 12. Adapter boundary and the next phase

An adapter translates a client's established protocol onto these operations and
nothing else: category to profile, client job ID to `client_reference` plus
`idempotency_key`, queue and history to polling, paused state to controls, and
completed path to `import_path`. It owns no queue or executor semantics and calls
no internal or administrative transfer endpoint. Capability negotiation is
`GET /capabilities`.

The SABnzbd dialect answers these for SABnzbd (`ARR_SABNZBD_COMPATIBILITY.md`).
They remain open for any other dialect:

1. Which download-client protocol to emulate (qBittorrent, SABnzbd, NZBGet,
   Transmission, …), and its exact endpoint and credential format (username and
   password vs API key, and how that maps onto one Integration token).
2. Category semantics: *arr's per-category save path, and category creation by
   the client.
3. Download identity: the client-visible ID (an info-hash for torrent clients,
   `nzo_id` for SABnzbd) and how it maps to `job_…` and `client_reference`.
4. Queue and history shape: history retention, failed-item semantics and
   `removeCompleted`.
5. Completed-path expectations, Remote Path Mapping, and the per-file vs
   per-folder import contract.
6. `deleteData`/`deleteFiles` on remove. Here it is mapped to withdraw or
   acknowledge and never to deletion; how *arr reacts to files that remain.
7. Pause, resume and error semantics: how *arr treats a declined pause and
   stalled or failed states.
8. Uploads that v1 does not support (NZB file upload, `.torrent` larger than
   8 MB), and whether they need the staged multipart path.
9. Polling cadence and the load of many *arr instances.

Out of scope for v1: any emulation, remote streaming or range download, a
general webhook subscription, a file browser, writes to external filesystems,
and a browser extension.
