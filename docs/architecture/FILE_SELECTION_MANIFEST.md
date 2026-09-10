# Universal Torrent File-Selection / Manifest Overlay (v1.0.12)

DebridPulse can let a user acquire an explicit subset of a multi-file
torrent/magnet resource instead of the whole thing. The capability is
**provider-neutral**: AllDebrid is the first provider to implement it, but no
selection policy lives in any provider or executor. The Universal Transfer Core
owns every decision.

```
PROVIDER                        UNIVERSAL CORE                     EXECUTOR
declares FILE_MANIFEST     ->    owns ALL-vs-subset policy    ->   receives
reports ProviderObservation     owns the 120s user-decision hold   ordinary
reports a neutral FileManifest  owns the 60s post-AVAILABLE grace   canonical
continues acquisition alone     owns durable provenance            candidates
(eager, open-ended, capacity-    owns stale-manifest rejection      only — knows
 independent)                    owns executable reconciliation     nothing about
                                filters the final SourceEntry list selection
```

Default acquisition policy is always **ALL FILES**. Interactive file selection
is an explicit opt-in (`selection_mode=interactive`). If no explicit subset is
confirmed, DebridPulse processes the full torrent exactly as before.

**Provider preparation, user file-selection authorization, and executor dispatch
are three separate lifecycle dimensions.** Provider preparation is eager and
independent of executor capacity; the user's decision time begins only when
there is an actionable multi-file manifest to decide on; executor capacity
matters only when executable work is ready to dispatch. A resource may remain
`PREPARING` for far longer than 60 seconds without losing the interactive
selection opportunity.

## Neutral provider contract

* `Capability.FILE_MANIFEST` — the provider may expose a complete neutral
  file-level manifest for a resource *before* core commits that resource's
  executable manifest. Registration requires the provider to also support
  ordinary resource observation (`ResourceLookup`).
* `FileManifest(entries: tuple[FileManifestEntry, ...])`, where each
  `FileManifestEntry` carries only `name`, `relative_path`, `expected_bytes`.
* `ProviderObservation.file_manifest: FileManifest | None` — `None` until the
  provider has a complete authoritative tree. Partial trees are never exposed as
  selectable manifests.
* The early manifest MUST contain no URL, endpoint, signed token, request
  header, API credential, provider-native decision, selection flag, timeout, or
  executor information. Download/capability links are discarded at the adapter
  boundary.

The existing executable manifest (`Capability.METADATA` /
`Manifest.manifest(resource) -> SourceEntry[]`) is unchanged and remains the
routable/executable contract. The two manifest concepts are never merged.

## Core policy owner — `backend/transfers/file_selection.py`

Canonical neutral policy/normalization owner. Contains no `alldebrid`,
`realdebrid`, `aria2`, `statusCode`, or provider-native error strings, and
imports neither `time` nor `datetime`.

* `POST_AVAILABLE_MANIFEST_GRACE_SECONDS = 60.0` — bounded grace, and **only**
  a grace: it runs solely after an interactive `FILE_MANIFEST`-capable resource
  is observed executable/`AVAILABLE` while a usable manifest is still
  unobtainable. It never runs while the provider is `PREPARING`, and it is never
  submission-relative or resource-creation-relative. (Was
  `AUTO_MANIFEST_WINDOW_SECONDS`; renamed with the Torrent/Magnet File-Selection
  Lifecycle Correction.)
* `IMMEDIATE_DECISION_HOLD_SECONDS = 120.0` — the maximum unanswered
  user-decision time. Anchored exactly once to the first actionable multi-file
  manifest, `PREPARING` or `AVAILABLE`. Never a provider-preparation timeout, a
  manifest-discovery timeout from submission, or a minimum delay before
  execution.
* `SELECTION_MODE_ALL` / `SELECTION_MODE_INTERACTIVE` and
  `normalize_selection_mode()` — the neutral per-submission intent policy. The
  interactive lifecycle is entered only for `selection_mode=interactive`; it is
  never inferred from an SSE connection, a session, a user agent, or `source`.
* Manifest identity:
  * `manifest_digest = SHA-256` over the sorted canonical
    `(normalized_path\0size\n)` pairs — order-independent, so a provider
    re-ordering the tree does not create a new manifest version, but a path or
    size change does.
  * `manifest_id = uuid5(NAMESPACE_URL, "file-manifest:<provider-resource-id>:<digest>")`.
  * `entry_id = uuid5(NAMESPACE_URL, "file-manifest-entry:<provider-resource-id>:<normalized-relative-path>")`
    — size is not part of entry identity, so a same-path size change keeps the
    logical entry stable while bumping the manifest version.
* `evaluate_gate(state, now) -> {WAIT_FOR_MANIFEST | WAIT_FOR_DECISION | PROCEED}`
  plus a settled `(decision, reason)` — the pure decision function.
* `reconcile_executable_subset(selected, executable_entries)` — fail-closed
  path/size reconciliation of a confirmed subset against the late executable
  manifest. Raises `SelectionUnprovable` rather than broadening to ALL.

## Timing semantics

Three independent, non-overlapping dimensions, all driven by the injected engine
clock; deadlines are persisted as absolute values and survive restart without
resetting or extending.

| Dimension | Length | Anchored at | On expiry |
| --- | --- | --- | --- |
| Provider preparation | unbounded | — | (no timer; provider work continues on its own cadence) |
| User-decision hold | 120 s | the **first actionable multi-file manifest**, whether the resource is `PREPARING` or `AVAILABLE` at that moment | modal closes, draft discarded, default ALL wins (`decision_reason = decision_timeout`), local materialization proceeds |
| Post-`AVAILABLE` manifest grace | 60 s | the **first `AVAILABLE`-without-usable-manifest observation** (`available_at`) | default ALL wins (`decision_reason = manifest_timeout`), local materialization proceeds |

Key statements (Torrent/Magnet File-Selection Lifecycle Correction §25):

* File selection is an interactive pre-execution authorization dimension.
  Provider preparation is eager and independent of executor capacity.
* The 120-second timeout measures unanswered user decision time beginning at the
  first actionable multi-file manifest. It does not measure provider preparation
  time.
* A provider may remain `PREPARING` for longer than 60 seconds without losing
  the interactive selection opportunity.
* The 60-second bound, when applicable, is only a grace period after an
  interactive `FILE_MANIFEST`-capable resource is `AVAILABLE` but a usable
  manifest is still unavailable.
* Settled `EXPLICIT`/`ALL` decisions cannot have a selection-derived scheduler
  wait recreated by stale gate work.
* Browser UI submissions explicitly opt into interactive selection. Historical /
  headless API submissions default to ALL.

**An automatically actionable multi-file offer and immediate irreversible ALL
materialization must never coexist.** `record_file_manifest` persists
`hold_until = manifest_observed_at + 120 s` in the *same* durable transaction
that binds the first multi-file manifest and queues the auto-offer — there is no
submission-relative cutoff on when that manifest may arrive. `evaluate_gate`
then returns `WAIT_FOR_DECISION` until the hold expires. The production
reproducer (a torrent that stays `PREPARING` for minutes, then becomes
`AVAILABLE` *and* exposes its first complete manifest) gets the full bounded
decision window.

`initially_available` is persisted only as an immutable provenance/diagnostic
fact; it no longer decides any timing. The gate reads the live
`resource_available` fact plus `available_at` (the durable first-`AVAILABLE`
anchor). While `PREPARING` with no manifest, the gate `WAIT_FOR_MANIFEST`s with
**no** countdown of any kind. Once `AVAILABLE` without a manifest, the 60-second
grace runs from `available_at`; a manifest arriving inside it starts the 120-second
hold from that arrival, and grace expiry settles `manifest_timeout`.

The settle reason is deterministic: `decision_timeout` **only** when a persisted
`hold_until` actually expired while still pending; `manifest_timeout` **only**
when the post-`AVAILABLE` grace expired. `default_materialization` is no longer
emitted by `evaluate_gate` (it survives only as the `commit_selected_manifest`
fallback reason for a straight ALL commit).

`decision_deadline` (the persisted `hold_until`, surfaced only while
`decision = pending`) is the authoritative user-decision deadline. A browser
that cold-loads or reconnects any time before `hold_until` recovers the active
offer through `GET /api/file-selections/offers`.

### Legacy `manifest_wait_until` / new `available_at`

`available_at REAL` (nullable) is an **additive metadata-only column** — no data
backfill. Pre-correction rows get `NULL`, the correct "resource not yet observed
`AVAILABLE` under the corrected engine" value; their next ordinary `AVAILABLE`
observation anchors a fresh grace, so a stale submission-relative
`manifest_wait_until` can never settle ALL for an uncached `PREPARING` transfer
after upgrade. `manifest_wait_until` is retired as a window: it is now only a
mirror of the post-`AVAILABLE` grace deadline (`0.0` = not started) and the gate
never reads it.

## Hold release — the 120 s window is a maximum, never a minimum

**The 120-second hold exists only while the decision is pending.** It is a
maximum unanswered-decision window, not a mandatory delay after the user has
decided. Confirm (`pending → explicit`) and an active-hold Close/X
(`pending → all/closed`) each resolve the decision immediately and, **in the same
`BEGIN IMMEDIATE` transaction**, release the scheduler retry delay the
file-selection gate created — so the next ordinary resolution cycle materialises
the confirmed subset (or ALL) without waiting out the remaining decision deadline
or the last provider-poll timestamp. `application/service.py` then re-drives the
existing `resolution_wakeup`; no new scheduler, no new lifecycle state, no
frontend-owned release.

**Gate authority and the scheduling of the wait it produces are transactionally
coupled** (Torrent/Magnet File-Selection Lifecycle Correction §7-8).
`repository.file_selection_gate(...)` is one `BEGIN IMMEDIATE` on the
selection-generation row that: anchors the post-`AVAILABLE` grace once; evaluates
the pure gate; settles a still-pending timeout/single-file decision; and — only
for a genuine still-pending selection `WAIT`, and only when the engine passes a
`poll_interval` — persists the next selection-derived `retry_at`. A stale `WAIT`
can therefore never recreate `retry_at` after Confirm/Close/timeout has settled
the decision: a concurrent settle either commits first (this transaction then
sees `EXPLICIT`/`ALL` and schedules nothing) or blocks on the row's write lock
until this transaction commits and then releases the wait itself. Both legal
orders converge to `decision = explicit/all` with `retry_at <= now`. The engine
no longer performs a separate `poll_after` after the gate.

`decision_deadline` in the public read model is `hold_until` **only while
`decision == "pending"`** and `null` for any settled decision. The durable
`hold_until` column is retained as historical evidence of the anchored deadline.

### `retry_at` is multi-purpose — only the selection-induced wait is released

`transfer_requests.retry_at` is set forward by two paths that both leave the
request `state='waiting'`:

* the interactive file-selection gate wait (scheduled atomically inside
  `repository.file_selection_gate(...)` when the gate `WAIT`s for a still-pending
  decision) and the generic `_repository_base.poll_after()` `PREPARING` re-poll
  cadence. Neither records an `error`.
* `_repository_base.request_failure()` (via `_engine_base._request_failure(...,
  waiting=True)`) — a provider observation error / `ABSENT` / `EXPIRED` /
  reconciliation-exception backoff, whose delay is
  `policy.retry_resolution(error).retry_at`. This path **always** writes a
  non-null `error` and increments `attempts`.

(A cross-transfer equivalence proof-retry also writes `retry_at` forward, but
only for `state='materializing'` rows — never `state='waiting'`.)

Both the release (`confirm`/`dismiss`) and the atomic gate reschedule target
`state='waiting' AND error IS NULL`. A genuine provider backoff coexisting with
an active hold keeps its longer, legitimate `retry_at` because it recorded an
`error`: it is never shortened to a selection cadence and never stripped of its
failure evidence. The confirmed selection still materialises, just after that
backoff. `poll_after()` no longer takes a `clear_error` flag (the atomic gate is
the only remaining file-selection scheduler and it never touches an
`error IS NOT NULL` request).

## Persistence — `backend/db/database.py` (additive current schema)

Four additive tables plus one additive nullable column
(`transfer_file_selections.available_at REAL`) — no new migration number;
`db/migrations/v112.py` is untouched, and `available_at` is added by
`_ensure_column` with **no data backfill** (see "Legacy `manifest_wait_until` /
new `available_at`" above):

* `transfer_file_manifests` — `UNIQUE(provider_resource_id, manifest_digest)`.
* `transfer_file_manifest_entries` — `PRIMARY KEY(manifest_id, entry_id)`,
  `UNIQUE(manifest_id, relative_path)`; `ordinal` preserves provider order for
  presentation only.
* `transfer_file_selections` — one **selection generation** per
  `(request_id, provider_resource_id)` (`id TEXT PRIMARY KEY` =
  `uuid5(... "file-selection:<request>:<resource>")`,
  `UNIQUE(request_id, provider_resource_id)`). A re-resolution onto a new
  provider resource creates a fresh generation; the prior generation is retained
  as historical truth and never inherited.
* `transfer_file_selection_entries` — keyed by the selection generation, with a
  compound FK to `transfer_file_manifest_entries(manifest_id, entry_id)` so an
  explicit selection can never reference an entry outside its manifest.

`backend/services/db_maintenance.py` backs up all four tables and deletes them
child-first on a whole-database wipe.

## Concurrency authority

The race between **user confirms explicit subset** and **core commits the
executable manifest** is resolved entirely by durable SQLite state — a
`BEGIN IMMEDIATE` transaction on the selection-generation row plus a conditional
`UPDATE`. An `asyncio.Lock` is never the correctness authority. The first
transaction to durably establish `decision='explicit'` or
`manifest_committed_at` wins; the loser reads the committed fact. Confirm returns
`409` if materialization already committed, and the browser then refreshes
authoritative state instead of claiming success. Proven over a 60-round
concurrent gather loop plus deterministic single-order tests and a two-stale-tab
test.

## Engine integration — `_engine_base.py` + `engine.py`

`_engine_base._after_resolution_persisted(record, provider, result)` is an inert
neutral seam (`return None`). The public engine overrides it — this is the
**only** place a selection generation is created. It opens a generation for the
`(request, provider-resource binding)` when the request is a root request routed
to a `FILE_MANIFEST` provider (`_file_manifest_root`) **and** either the durable
submission intent is `selection_mode == "interactive"` **or** the transfer
already owns a selection generation
(`repository.transfer_has_selection_generation`). The second clause keeps a
transfer interactive across a re-resolution onto a new provider resource and
across a database that predates `selection_mode`. It persists
`initially_available` + `available_at` and records an inline manifest if
present.

**`selection_mode` gates generation creation only.** Once a durable generation
exists for a `(request, binding)`, that generation — never the request's
current/defaulted policy field — governs manifest recording, selection gating,
Confirm/Close/timeout, and executable-manifest filtering until it is terminal
(upgrade-boundary invariant). Every engine step past creation checks
`repository.selection_generation_exists(request_id, binding_id)`: in
`_observe_resource`, `record_file_manifest` / `file_selection_gate` /
`commit_selected_manifest` are engaged iff a generation exists, so a
pre-`selection_mode` database whose root request now deserializes as
`selection_mode="all"` still has its durable PENDING hold, EXPLICIT subset, and
PREPARING selection opportunity honored. A genuinely new `selection_mode=all`
submission has no generation, so all three are skipped and it goes straight to
the executable manifest as plain ALL — unchanged.

During observation, a `FILE_MANIFEST` provider's `AVAILABLE` resource **that
owns a selection generation** is gated by `repository.file_selection_gate(...)`
before the executable manifest is fetched:

* `WAIT_FOR_MANIFEST` / `WAIT_FOR_DECISION` → the same transaction has already
  persisted the next selection-derived `retry_at` (when the engine passed a
  `poll_interval`); the engine just returns. **No** `provider.manifest()` call,
  so the executor gets nothing.
* `PROCEED` → `provider.manifest()` → validate →
  `repository.commit_selected_manifest(record, full_entries)` (which filters to
  the authorized subset) → `repository.manifest(record, authorized)` (ordinary
  idempotent child fan-out).

No new scheduler loop and no file-selection-specific worker: the gate wait reuses
the ordinary `resolve_pending()` cadence, and `confirm`/`dismiss` on an active
hold set the canonical `resolution_wakeup`. Provider resolve/observe/manifest
never inspects `max_active_executions`, `live_executions`, or aria2 occupancy, so
an interactive torrent added while every execution slot is full still resolves,
records its manifest, opens its hold, and queues its offer; only executor
dispatch waits for capacity.

## Submission intent — `selection_mode`

`selection_mode` is a neutral per-submission policy carried on the durable
`TransferRequest` payload (alongside the existing `preferred_provider` routing
hint), values `all` (default) and `interactive`, validated by
`file_selection.normalize_selection_mode()`. It is **excluded from the dedupe
fingerprint** (which keys on `TransferRequest.fingerprint`, the BitTorrent
infohash), so the same torrent is the same logical source regardless of intent.
`application.submit_magnet` / `submit_torrent` take `selection_mode=`;
`POST /api/torrents/add-magnet` reads an optional body field and
`POST /api/torrents/add-file` an optional multipart form field, both defaulting
to `all`. The built-in browser (`app.js`) sends `interactive` on every
magnet / bulk-magnet / torrent-file path; direct-link submission
(`POST /api/links/add`) is unchanged and sends nothing.

A pre-`selection_mode` `TransferRequest` payload deserializes with the default
`selection_mode="all"`. This is safe: it is consulted only at generation
creation (`_after_resolution_persisted`), and a transfer that already owns a
generation is engaged regardless (`transfer_has_selection_generation`). An
already-existing durable generation is authoritative — the new gate can never
invalidate or bypass it (upgrade-boundary invariant;
`test_file_selection_upgrade_boundary.py`).

## Application service + API

`backend/application/service.py` adds neutral commands `file_selection`,
`file_selection_offers`, `confirm_file_selection(transfer_id, manifest_id,
entry_ids)`, `dismiss_file_selection(transfer_id, manifest_id)`. Mutations wrap
`application_operation()` and set the resolution wakeup only when a hold is
actually released. No provider or registry access.

`backend/api/file_selection_routes.py` (registered in `main.py` before the
generic router) owns:

```
GET  /api/file-selections/offers
GET  /api/torrents/{transfer_id}/file-selection
POST /api/torrents/{transfer_id}/file-selection/confirm
POST /api/torrents/{transfer_id}/file-selection/dismiss
```

Transport codes: `409` stale manifest / already committed / no longer mutable,
`422` invalid shape / empty selection. Confirm and Dismiss return `404` for a
missing transfer. The `GET` read model is queried on every Details open, so a
transfer with no file-selection generation — and an unknown transfer id alike —
is reported as `{"eligible": false}` with `200` (identical for both, so transfer
existence is not disclosed), never `404`.

### Public read model — fixed whitelist

The route layer projects a fixed public whitelist so a new internal field cannot
leak to the browser:

* selection view: `eligible, mutable, manifest_id, decision, decision_reason,
  file_count, total_size_bytes, entries, selected_entry_ids, auto_offer,
  auto_offer_until, decision_deadline, initially_available, server_now`; each
  entry exposes only `entry_id, name, relative_path, size_bytes`.
* offer: `transfer_id, manifest_id, file_count, decision_deadline,
  auto_offer_until`.

The durable read model keeps `selection_id` / `provider_resource_id` /
`request_id` for internal use, but they never cross the HTTP boundary. The
browser identifies mutable selection state by transfer context (the URL) plus
`manifest_id`; Confirm and Dismiss use `manifest_id` as the stale-generation
authority. Provider-native resource ids, endpoints, download/signed URLs,
headers, tokens, request payloads, and executor state are never serialized.

### Browser event

The first actionable multi-file offer (multi-file manifest bound, decision
pending, not dismissed, decision hold active) inserts a durable
`application_event` (`kind = file_selection_available`), claimed once so repeated
provider polls do not duplicate it. `application/observability.py`
re-publishes it through the existing event bus as
`("file_selection_available", {"transfer_id": ...})` — no provider identity,
`event_bus.py` untouched. SSE is not authoritative state; the browser then
queries `GET /api/torrents/{id}/file-selection`.

## Frontend — one bounded owner

`frontend/static/app.js` is the sole modal coordinator (`DPModal`: `open`,
`requestModalClose`, `finishClose`; modes `details` | `file-selection`) and the
sole owner of `window.showDetail` / `window.closeModal`. It emits
`debridpulse:detail-rendered` / `debridpulse:detail-closed`.

`frontend/static/ui-detail-candidates.js` no longer wraps the modal globals; it
listens to those lifecycle events.

`frontend/static/ui-file-selection.js` (`window.DPFileSelection`, lazy-loaded via
`ui-provider-status.js` `bootPresentationOwners()`, styled by
`ui-file-selection.css` imported once in `style-v11.css`) owns the file tree,
tri-state folders reconstructed from paths, Select all / Deselect all, selected
count + byte total, Confirm / Close / Cancel Transfer, the countdown, automatic
presentation, and the Details `Select files` / `Change file selection` entry
point. It owns no policy:

* the countdown is derived from `decision_deadline` + `server_now` and updated
  locally for display only; reaching zero refreshes authoritative state and
  never authorizes ALL from the browser timer;
* checkbox changes stay browser-local until Confirm succeeds; Confirm POSTs
  `manifest_id` + `entry_ids` only; a `409` refreshes authoritative state and
  never shows success;
* Close / X share identical semantics (dismiss `manifest_id`, default ALL
  stays); Cancel Transfer routes to the existing `POST /api/torrents/{id}/cancel`
  endpoint;
* auto-open only when authoritative state says
  `eligible && mutable && auto_offer && file_count > 1`; a single-file resource
  never auto-opens, and the browser never re-derives a presentation window of
  its own — it obeys `auto_offer` (which the core reports true for exactly the
  life of the decision hold).

Browser loss (SSE disconnect, tab suspension, crash) is never treated as a user
action; the durable deadline continues server-side and ALL proceeds at expiry.

## Executor boundary

The executor keeps receiving ordinary `ExecutionRequest` objects containing
ordinary selected `TransferCandidate`s. It never receives a manifest id, a
provider file index, a selection bitmask, a provider-native file tree, or any
provider data. Proven end-to-end: provider reports 6 files → user confirms 2 →
core commits exactly 2 child requests → exactly those 2 produce candidates →
`executor.start` is invoked only for those 2, with candidate context ⊆
`{copy_ticket, destination}`.

## Regression owners

`backend/tests/test_file_selection_contract.py`,
`test_file_selection_lifecycle.py`, `test_file_selection_persistence.py`,
`test_file_selection_api.py` (added to
`two_provider_checkpoint_qualification.txt`); `test_alldebrid_provider_contract.py`
(+file-manifest cases); `test_universal_contracts.py` /
`test_universal_boundaries.py`; `test_ui_presentation_ownership_contract.py`
(§62 modal-ownership assertions); `frontend/browser/file-selection.spec.js` and
`frontend/browser/details-candidates.spec.js`.
