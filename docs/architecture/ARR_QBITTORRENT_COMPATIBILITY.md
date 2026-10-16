# *arr Compatibility: the qBittorrent Dialect (Bearer only)

Sonarr and Radarr can use DebridPulse as a **qBittorrent download client**, with
a DebridPulse Integration token as qBittorrent's **API Key**. The dialect is a
translation over the native external-client contract
(`EXTERNAL_CLIENT_PROTOCOL.md`), sharing the *arr translation with the SABnzbd
dialect (`ARR_SABNZBD_COMPATIBILITY.md`). DebridPulse is not a torrent client:
it does not seed, announce or hold a swarm.

> *arr owns its submissions and its interest in the result.
> DebridPulse owns acquisition, transfers, execution and files.

Owners in the code:

- `backend/api/arr_qbittorrent.py`: qBittorrent wire syntax and nothing else.
- `backend/external/arr.py`: the shared translation (removal, output location,
  upload ceilings).
- `backend/external/service.py`: every decision (jobs, the live identity of a
  torrent, the visible view, withdrawal, acknowledgment, delivery).

## 1. Support status

The status of each application was established separately, from the released
source of each.

| Application | Status | Minimum verified released build | Evidence |
|---|---|---|---|
| **Sonarr** | **SUPPORTED** | **v4.0.18.2971** (stable, 2026-06-24, commit `d41805bb`) | The API-key backport `4c66cc2c` on `v4-develop` (2026-05-04) is in every v4 release from v4.0.17.2967 (prerelease). v4.0.18.2971 is the first stable one, and v4.0.20.3014 (latest stable) is unchanged on the API-key path. Sonarr v5 carries it (`6a663910`) **only in prereleases**, so no Sonarr v5 build is claimed. |
| **Radarr** | **SUPPORTED** | **v6.2.1.10461** (stable, 2026-06-09, commit `4dcc7c78`) | `eec3efdb` ("API key support for qBittorrent", 2026-05-05) is in every release from v6.2.1.10437 (prerelease). v6.2.1.10461 is the first stable one, and v6.4.4.10685 (latest stable) is identical on this path. |

**Older releases are not supported.** They have only username and password and
log in through `/api/v2/auth/login` with a cookie. DebridPulse provides no such
login, so their Test fails at authentication. Upgrade, or use the SABnzbd
dialect for NZBs.

### How the key is used

When **API Key** is filled in, both applications behave as follows
(`QBittorrentProxyV2.cs`):

- They send `Authorization: Bearer <key>` on every request.
- They never call `/api/v2/auth/login` and never use a cookie.
- They require **Username and Password to be empty**.
- They report a `401`/`403` as an API-key failure.

On the DebridPulse side:

- The token is verified by the existing authentication boundary, as every
  Bearer token is.
- No key in the URL is accepted here (unlike the SABnzbd dialect).
- No session cookie, open-mode access or Admin session is an external client:
  each gets `401`/`403`.
- **Separate tokens** make separate clients that never see or remove each
  other's torrents.
- **One token shared** by several instances is one client.
- **Rotation** keeps every item.
- **Revocation** refuses the next call and withdraws nothing.

## 2. Setup

1. **Token.** An Admin issues an Integration token
   (`POST /api/accounts/{id}/tokens`).
2. **Delivery profile.** An Admin adds a profile (`PUT /api/external/profiles`):
   - `categories`: the *arr category, matched exactly. Sonarr's default is
     `tv-sonarr` and Radarr's is `radarr`; either use those or set *arr's
     Category to the profile's.
   - `clients`: the token's client ID.
   - `source_kinds`: must include `magnet` and/or `torrent`.
   - `local_root`: beneath the download folder.
   - `import_root`: where *arr sees `local_root`, if it mounts it elsewhere.
3. ***arr** → Settings → Download Clients → **qBittorrent**:
   - **Host / Port**: DebridPulse.
   - **URL Base**: blank. *arr then calls qBittorrent's own `/api/v2/…`, which
     DebridPulse answers. `/api/qbittorrent` still works as URL Base, for
     installations configured that way; it reaches the same endpoint.
   - **API Key**: the token, which *arr sends as `Authorization: Bearer`.
   - **Username / Password**: empty.
   - **Category**: the profile's category.
   - **Post-Import Category**: empty.
   - **Recent/Older Priority**: Last.
   - **Initial State**: Started.
4. **Filesystem.** *arr must read `import_root`. Remote Path Mapping is *arr's
   own setting. DebridPulse copies, links and moves nothing.

*arr's **Test** calls these, and DebridPulse answers each:

| Call | Answer |
|---|---|
| `app/webapiVersion` | `2.15.1`: the qBittorrent 5.2 WebAPI revision whose shapes this dialect answers |
| `app/preferences` | no ratio or seeding-time removal, `queueing_enabled: false`, `dht: true` |
| `torrents/categories` | the client's torrent categories, each `savePath` = its import root |
| `torrents/createCategory` | an existing category: no-op `200`; any other: `409` |
| `torrents/info` | the client's torrents |

- **`dht: true`** is the signal *arr uses to decide whether a magnet without
  trackers can be resolved. DebridPulse's providers resolve such magnets.
- **`queueing_enabled: false`** is truthful: DebridPulse owns queue order. As a
  result, a **First** priority fails *arr's Test.

### Not Found until it is usable

Both paths of this dialect answer `404 Not Found` — to every caller, with or
without a key — until DebridPulse has at least one usable qBittorrent integration:
a delivery profile with a category, permitting `magnet` or `torrent` (`links` and `nzb` do not count), that lists a client
holding an active Integration token (not revoked, of an enabled Integration
account). There is no switch: an Admin configures Integration Accounts and
Integration Delivery Profiles in Settings → Users & Access, and the dialect
appears or disappears with them on the next request — issuing, rotating or
revoking a token, disabling an account, or editing or removing a profile.

- A `404` here does not mean DebridPulse is down. *arr's Test reports it as a
  failed connection; check the profile and token first.
- The SABnzbd dialect is exposed or hidden on its own, by its own source kinds.
- Exposure authenticates nobody: every request still needs its own token, and
  sees only its own client's categories and jobs.
- Revoking the last qualifying token withdraws nothing; the jobs stay.

## 3. Calls answered

All are under `/api/v2/` (and, identically, `/api/qbittorrent/api/v2/`).

| Call | Behavior |
|---|---|
| `POST torrents/add` | One magnet (`urls`) or one `.torrent` (multipart `torrents`, ≤ 16 MB, DebridPulse's own torrent upload ceiling), with `category`. Answer: `{"success_count":1,"failure_count":0,"pending_count":0,"added_torrent_ids":["<hash>"]}`, given only once the job and its one demand are durable. |
| `GET torrents/info?category=&hashes=` | One item per info-hash (§5) |
| `GET torrents/properties?hash=` | `{hash, save_path, seeding_time: 0}`; unknown: `404` |
| `POST torrents/delete hashes=…&deleteFiles=…` | §6 |
| `POST torrents/createCategory` | as above |
| `POST torrents/setCategory`, `setShareLimits`, `topPrio`, `setForceStart` | `409`, declined. *arr only logs these failures (`QBittorrent.cs:52-180`). |
| anything else | `404` |

### Refused submissions

Refusals are qBittorrent-style status codes with a text body; every one admits
nothing:

- **415** (Unsupported Media Type):
  - an `http(s)` URL (DebridPulse fetches no URL here);
  - an invalid or non-canonical `.torrent`;
  - a v2-only torrent.
- **400** (Bad Request): more than one source in one add.
- **409** (Conflict):
  - `savepath` or `downloadPath`;
  - `stopped`, `paused`, `useDownloadPath` or `autoTMM` set to `true` (DebridPulse
    holds no client-paused or client-placed work);
  - an unknown category, or one this client may not use (indistinguishable).

*arr reports a refused add as a failed grab.

### Accepted settings that do nothing

`sequentialDownload`, `firstLastPiecePrio`, `contentLayout`, `ratioLimit` and
`seedingTimeLimit` are accepted and have **no effect**: DebridPulse owns file
layout and never seeds. Nothing is granted.

## 4. Identity

- **The wire ID is the v1 info-hash, in lowercase hex.** The native intake owner
  computes it from the authenticated request itself; the client's claim is never
  used:
  - **Magnet:** the `btih`, with base32 converted to hex.
  - **`.torrent`:** SHA-1 of the original `info` bytes.
  - **Hybrid:** keeps its v1 hash.
  - **v2-only:** refused.
  - **Why this matches *arr:**
    - Sonarr (MonoTorrent 2.0.7) uses `InfoHash`.
    - Radarr (MonoTorrent 3.0.2) uses `InfoHashes.V1OrV2`, which is v1 whenever
      v1 exists.
- **Where it lives.** The hash is the job's recorded source summary. It is not
  the transfer, job, provider or executor ID, and no hash registry exists.
- **What it survives.** It is unchanged by provider and transport switches,
  consolidation and restarts.
- **One item per (client, info-hash), as in a torrent client:**
  - **Same client, hash and category, while the item is live:** the same item. A
    retried or concurrent add creates no second demand.
  - **Item failed but still visible, or held under another category:** the add
    is refused (`409 duplicate_torrent`). Nothing is replaced or merged; remove
    the old item first.
  - **Item has left the client's view** (removed, acknowledged or expired): a new
    job.
  - **Another client:** its own item. The work may converge on one canonical
    transfer, with each client's demand its own.
  - This is a best-effort accommodation of a protocol with no request ID, not
    exactly-once.

## 5. State

States come from the native job, not from an executor.

| Native job | qBittorrent state | *arr reads |
|---|---|---|
| accepted, queued, resolving, input required | `queuedDL` | Queued |
| downloading, served by shared work | `downloading` | Downloading |
| verifying, material not yet verifiable after a restart | `checkingDL` | Queued |
| finalizing (extraction) | `moving` | Downloading |
| paused by DebridPulse | `stoppedDL` | Paused |
| completed, with a delivery available now | **`stoppedUP`**: `progress` 1, `content_path` = its location, `save_path` = the import root, `ratio` 0, `ratio_limit` 0 | Completed, importable |
| completed, not importable now | `checkingUP` | Queued, never Completed |
| failed | `error` | Warning |

Notes on the values:

- **`stoppedUP` is a presentation.** It means "downloaded, and not transferring".
  DebridPulse does not seed: it reports no upload, swarm or tracker figure. Its
  share limit of 0 is therefore already met, which is what *arr needs before it
  may remove a completed torrent after import.
- **Failures are a Warning, by *arr's design.** *arr maps no qBittorrent state to
  Failed, and `error` is deliberately a Warning (`QBittorrent.cs:249-252`). A
  terminally failed job therefore stays as a warning until removed.
- **Progress.** *arr reads a torrent as `TotalSize = size` and
  `RemainingSize = size × (1 − progress)`, and draws its queue bar from those two
  (`QBittorrent.cs` `GetItems`). It never reads `amount_left` or `completed`. A
  bar therefore exists only when `size` is known. The figure is DebridPulse's own
  primary progress, the external job's `progress.effective`: the in-flight
  execution while nothing is verified yet, otherwise verified material. It is the
  same figure the SABnzbd dialect reports. Each kind of figure carries only its
  own byte counters, so `size`, `progress`, `amount_left` and `completed` always
  describe one scope (`progress = round(completed / size, 4)`,
  `amount_left = size − completed`):
  - **Verified material, nonzero.** A writer that exports material ranges, such
    as aria2, has its completely written ranges checkpointed into DP-valid
    material while it downloads, so its progress is verified material, not an
    in-flight figure. `size` is the transfer's canonical artifacts' total size
    and `completed` their DP-valid material: a completed artifact counts its
    whole size, any other its valid material up to its size. This is the
    measurement DebridPulse's own verified percentage is computed from
    (`verified_material_measure`, read at the poll). It is never combined with an
    executor's counter: downloaded but not yet checkpointed bytes are not in it,
    so it can trail the executor by up to one checkpoint interval. It holds while
    paused, finalizing, or failed, since the material is what the transfer has.
  - **Exact in-flight bytes**, present only when the canonical projection attaches
    one exact byte scope while nothing is verified yet: `size` is that total,
    `progress` is `completed / total`, and `amount_left` and `completed` are the
    executor's counters. *arr's bar and remaining size equal DebridPulse's.
  - **A percentage without exact bytes** (a best-effort estimate or a unit
    count): `progress` carries it, and `size` stays `0`. No total is ever derived
    from a percentage, so *arr shows no bar: the most it can truthfully be given.
  - **Nothing verified and no in-flight figure** (before a material writer's
    first checkpoint, a paused or post-acquisition writer whose work is not DP
    material), **or a member whose size is unknown:** `progress` is the figure,
    or `0`, and `size` `0`. Nothing verified is not reported as `0` of a total: a
    writer whose work is not DP material may have acquired much of it.
  - **Completed:** `size` is the committed delivery's, `progress` `1`,
    `amount_left` `0`. No in-flight counter outlives acquisition.
- **Unknown values.** `eta` is qBittorrent's unknown value `8640000`, which *arr
  reads as no time left known. No speed, ETA, peer or tracker figure is invented.
- **Import location.** `content_path` is the single delivered file, or the one
  folder holding every delivered file beneath the import root. It always differs
  from `save_path`, as *arr requires. A multi-file delivery loose in the import
  root reads `checkingUP`.

## 6. Removal and `deleteFiles`

Removal is decided by the item's condition, through `external.arr.remove`, for
every visible job of the hash. `deleteFiles=true` never authorizes DebridPulse to
delete anything.

| Item | Effect |
|---|---|
| Unfinished, only this client needs the work | Its demand is withdrawn. The core cancels the transfer and, needed by nobody, removes it from the ordinary Downloads and Dashboard lists (its soft delete: history and provenance stay), each step atomically against demand that arrives meanwhile. Only DebridPulse's own incomplete material is reclaimed. At a provider, every resource DebridPulse created or adopted for the transfer -- its route's and each prepared backup's -- is removed by a durable, retryable cleanup; a resource it only observed, or one another transfer still holds, is kept. A provider inventory that still lists a removed resource never brings the work back. |
| Unfinished, others need the work | Only this client's demand is withdrawn. |
| Completed | The delivery is acknowledged. The transfer and its history stay; once every claim on the transfer is acknowledged, an empty folder DebridPulse created for it is removed, never its contents (`ARR_SABNZBD_COMPATIBILITY.md`, "After the import"). |
| Failed | Withdrawn; the core's failure policy applies. |
| Unknown, or another client's | Skipped silently: the answer is the same either way. |

- *arr deletes a completed download's files itself when asked to, and may move
  them on import. These are ordinary files.
- An imported file that was moved or deleted is never a failure and never
  reacquired.

## 7. Limits

- Not supported: login or cookie authentication; any URL fetch.
- `.torrent` over 16 MB (DebridPulse's canonical torrent upload ceiling); v2-only
  torrents (DebridPulse's canonical torrent identity is v1). Both are current
  DebridPulse-wide limits, not adapter ones.
- Client-chosen save path, start state, priority or category changes.
- Seeding, ratio and seeding time.
- No qBittorrent state maps to Failed in *arr (see §5).

## 8. Live verification (after deployment only)

The deterministic suite (`backend/tests/test_arr_qbittorrent_adapter.py`)
checks every response against *arr's parsing rules, restated from the pinned
source above. It does not run Sonarr or Radarr. After deployment:

1. Sonarr ≥ v4.0.18.2971: add DebridPulse as a qBittorrent client per §2, then
   run **Test**.
   - It must pass.
   - A wrong key must report an API-key failure.
   - The browser network panel must show no `/auth/login` call.
2. Grab one magnet release and one `.torrent` release:
   - each appears in Activity → Queue under its info-hash;
   - each is imported from `import_root` on completion;
   - with Remove Completed on, each disappears.
   DebridPulse's transfer stays completed.
3. Remove a queued item. DebridPulse cancels it only when no other client needs
   it.
4. Repeat with Radarr ≥ v6.2.1.10461.
