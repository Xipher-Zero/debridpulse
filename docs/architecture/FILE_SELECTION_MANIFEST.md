# Universal Torrent File-Selection / Manifest Overlay (v1.0.12)

DebridPulse can let a user acquire an explicit subset of a multi-file
torrent/magnet resource instead of the whole thing. The capability is
**provider-neutral**: AllDebrid is the first provider to implement it, but no
selection policy lives in any provider or executor. The Universal Transfer Core
owns every decision.

```
PROVIDER                        UNIVERSAL CORE                     EXECUTOR
declares FILE_MANIFEST     ->    owns ALL-vs-subset policy    ->   receives
reports ProviderObservation     owns the 60s auto-offer window     ordinary
reports a neutral FileManifest  owns the 120s decision hold        canonical
continues acquisition alone     owns durable provenance            candidates
                                owns stale-manifest rejection      only — knows
                                owns executable reconciliation     nothing about
                                filters the final SourceEntry list selection
```

Default acquisition policy is always **ALL FILES**. File selection is an
optional override. If no explicit subset is confirmed, DebridPulse processes the
full torrent exactly as before. Provider-side acquisition/resolution starts
immediately and never waits on the browser.

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

* `AUTO_MANIFEST_WINDOW_SECONDS = 60.0` — automatic presentation window.
* `IMMEDIATE_DECISION_HOLD_SECONDS = 120.0` — decision hold (any auto-presented
  multi-file offer, cached or PREPARING-origin).
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

Two distinct windows, both driven by the injected engine clock; deadlines are
persisted as absolute values and survive restart without resetting.

| Window | Length | Applies when | On expiry |
| --- | --- | --- | --- |
| Automatic manifest presentation | 60 s | a `FILE_MANIFEST`-capable provider resource is durably bound | no more auto-popup; Details entry stays available while mutable |
| Decision hold | 120 s | provider declares `FILE_MANIFEST` **and** a complete multi-file manifest becomes populated and usable **while the 60 s auto-presentation window is still open** — whatever the resource's initial availability | modal closes, draft discarded, default ALL wins (`decision_reason = decision_timeout`), local materialization proceeds |

**An automatically actionable multi-file offer and immediate irreversible ALL
materialization must never coexist** (specification sections 5/7). Whenever core
queues an auto-offer, the *same* durable transaction persists
`hold_until = now + 120 s`, and `evaluate_gate` then returns
`WAIT_FOR_DECISION` — regardless of whether the provider resource was initially
`AVAILABLE` or initially `PREPARING`. The production reproducer (a torrent that
begins `PREPARING` and, on one later observation, becomes `AVAILABLE` *and*
exposes its first complete manifest) therefore gets a real bounded decision
window instead of a millisecond race to `default_materialization`.

`initially_available` is still persisted as an immutable provenance/diagnostic
fact, but it no longer decides whether an auto-presented manifest receives a
decision opportunity. It still governs one narrower thing: whether a resource
with *no manifest yet* keeps `WAIT_FOR_MANIFEST` until the 60 s discovery window
elapses (initially `AVAILABLE`) or lets provider-side work continue with default
ALL settling later (initially `PREPARING`).

A manifest first observed *after* the 60 s window has closed gets **no**
automatic hold and **no** auto-popup; manual Details selection stays available
while the selection is still mutable. It settles `decision_reason =
manifest_timeout` for an initially-`AVAILABLE` origin (the 60 s manifest
opportunity genuinely expired) and `decision_reason = default_materialization`
for a `PREPARING` origin. Only local materialization / executor dispatch is ever
held; provider-side acquisition and resolution are never blocked.

The settle reason is deterministic: `decision_timeout` is emitted **only** when a
persisted `hold_until` actually expired while still pending; `manifest_timeout`
**only** when the 60 s manifest opportunity expired. `record_file_manifest`
establishes the decision hold in the same durable transaction that binds an
in-window multi-file manifest, so an in-window multi-file manifest can never lack
a hold — if that state is somehow observed the gate keeps waiting rather than
settling a mislabelled timeout.

`decision_deadline` (the persisted `hold_until`) is the authoritative
user-decision deadline and is **not** the same as the 60 s manifest-discovery
cutoff. A browser that cold-loads or reconnects after the discovery cutoff but
before `hold_until` still recovers the active offer through
`GET /api/file-selections/offers`.

## Persistence — `backend/db/database.py` (additive current schema)

Four additive tables (no new migration number; `db/migrations/v112.py` is
untouched):

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
neutral seam (`return None`). The public engine overrides it: for a
`FILE_MANIFEST` provider with a bound resource it opens the selection window,
persists `initially_available`, and records an inline manifest if present.

During observation, a `FILE_MANIFEST` provider's `AVAILABLE` resource is gated by
`repository.file_selection_gate(...)` before the executable manifest is fetched:

* `WAIT_FOR_MANIFEST` / `WAIT_FOR_DECISION` → `poll_after` a deadline wake and
  return; **no** `provider.manifest()` call, so the executor gets nothing.
* `PROCEED` → `provider.manifest()` → validate →
  `repository.commit_selected_manifest(record, full_entries)` (which filters to
  the authorized subset) → `repository.manifest(record, authorized)` (ordinary
  idempotent child fan-out).

No new scheduler loop: waits reuse `poll_after` + the ordinary
`resolve_pending()` cadence, and `confirm`/`dismiss` on an active hold set the
canonical `resolution_wakeup`.

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

A newly auto-presentable multi-file offer inside the 60 s window inserts a
durable `application_event` (`kind = file_selection_available`), claimed once so
repeated provider polls do not duplicate it. `application/observability.py`
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
  and a manifest that arrives after the 60 s window never auto-open.

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
