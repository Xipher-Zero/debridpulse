# *arr Compatibility: the SABnzbd Dialect

Sonarr and Radarr can use DebridPulse as a **SABnzbd download client**. The
dialect is a translation over the native external-client contract
(`EXTERNAL_CLIENT_PROTOCOL.md`); it is not a second downloader.

> *arr owns its submissions and its interest in the result.
> DebridPulse owns acquisition, transfers, execution and files.

Owners in the code:

- `backend/api/arr_sabnzbd.py`: SABnzbd wire syntax and nothing else.
- `backend/external/arr.py`: the translation any download-client dialect shares
  (removal, output location, the query-key paths).
- `backend/external/service.py`: every decision (jobs, live submission, the
  visible view, withdrawal, acknowledgment, delivery).
- `backend/auth/middleware.py`: verification of the API key.

## 1. Setup

1. **Token.** An Admin issues an Integration token for an Integration account
   (`POST /api/accounts/{id}/tokens`).
   - One token per *arr instance gives each its own client and its own items.
   - One token shared by several instances is **one client**: they see each
     other's items. Tell them apart by category.
   - Rotating a token (`succeeds`) keeps every item and `nzo_id`.
   - Revoking it refuses the next call and withdraws nothing.
2. **Delivery profile.** An Admin adds a profile (`PUT /api/external/profiles`):
   - `categories`: the *arr category (`tv`, `movies`). The match is exact, as *arr
     compares it.
   - `clients`: the token's client ID.
   - `source_kinds`: must include `nzb`.
   - `local_root`: where DebridPulse's completed downloads lie (the download
     folder or beneath it).
   - `import_root`: the same directory as *arr sees it, if *arr mounts it
     elsewhere.
3. **Download client in *arr.** Settings → Download Clients → **SABnzbd**:
   - **Host / Port**: DebridPulse.
   - **URL Base**: blank. *arr then calls SABnzbd's own `/api?mode=…`, which
     DebridPulse answers. `/api/sabnzbd` still works as URL Base, for
     installations configured that way; it reaches the same endpoint.
   - **API Key**: the token.
   - **Category**: the profile's category.
   - Leave **Use SSL** to match your listener.
4. **Filesystem.** *arr must be able to read the completed files at the path
   DebridPulse reports (next section). DebridPulse copies, links and moves
   nothing.

### Paths: one set of files, two points of view

DebridPulse and *arr usually run in different containers or hosts and see the
**same** completed files under **different** paths. Four settings are
involved; they are not interchangeable:

| Setting | Where | What it is |
|---|---|---|
| **Download Folder** | DebridPulse → Settings → Downloads | Where DebridPulse writes every download, in DebridPulse's own filesystem view (for example `/download`). |
| **DP Download Path** | Integration Delivery Profile (Settings → Users & Access) | The same directory, or one beneath the Download Folder, as DebridPulse sees it. |
| **External Import Path** | the same Integration Delivery Profile | That **same directory as *arr sees it**. DebridPulse replaces the DP Download Path prefix with it and keeps the rest of each path (the release folder and file names). Empty means *arr sees the same path DebridPulse does. |
| **Root Folder** | *arr → Settings → Media Management | *arr's **final library** (for example `/movies`). It is **not** a download path; never point it at the Download Folder. |

Each completed job is reported to *arr as one folder named after the
submitted NZB file, without `.nzb` (for example `Release.Name.2026.1080p-GRP/`),
never after a file inside it (an `.nfo`, `.par2` or `info.txt`), beneath the
External Import Path. The files in it keep their own names. A second job
with the same name gets its own folder. *arr's Completed Download Handling then imports from
there into its Root Folder, moving or copying (hardlinking) the files as its
own settings say.

A path is only a name: both sides must actually **mount the same storage**,
and *arr's user must be able to **traverse** every directory down to the files
and **read** them (and write, for a move-style import). Typing a path creates
no mount and grants no permission.

**Recommended: translate once, in DebridPulse.** An illustrative example --
these host paths are examples, not defaults:

| | DebridPulse container | Radarr (another host) |
|---|---|---|
| Storage | `/download` (bind mount) | `/Volumes/SABnzbdDATA/AriaNG Downloads` (network share of the same disk) |
| Setting | Download Folder `/download`; profile DP Download Path `/download`; External Import Path `/Volumes/SABnzbdDATA/AriaNG Downloads` | no Remote Path Mapping |
| A finished file | `/download/Release.Name.2026.1080p-GRP/Release.Name.2026.1080p-GRP.mkv` | reported and imported as `/Volumes/SABnzbdDATA/AriaNG Downloads/Release.Name.2026.1080p-GRP/Release.Name.2026.1080p-GRP.mkv` |

**Alternative: an *arr Remote Path Mapping.** Use it only when the profile does
not translate (External Import Path empty) and *arr cannot see DebridPulse's
path. In *arr → Settings → Download Clients → Remote Path Mappings:

- **Host**: exactly the Host configured for the DebridPulse download client;
- **Remote Path**: DebridPulse's path (the DP Download Path, e.g. `/download/`);
- **Local Path**: where *arr sees that directory.

Configure **one** of the two, not both: a mapping applied on top of an
already-translated path no longer matches anything. Changing either setting
needs no redownload: DebridPulse reports the new path on *arr's next poll.

### After the import

*arr removes a completed item from its download client once it has imported
it (Completed Download Handling with "Remove Completed"). That removal is
DebridPulse's **acknowledgement** of the delivery. Its effect:

- The transfer and its history stay in DebridPulse.
- If *arr moved the files out, DebridPulse removes the **empty** folder it had
  created for them -- only once every claim on that transfer is a delivery
  its client has acknowledged, and only if the folder is empty at that
  moment. It deletes no file and never empties anything: a copy-style import,
  a hardlink import or any leftover file keeps the folder and everything in
  it. Another client's or an operator's own request for the same transfer
  keeps it too.
- An item *arr imported but never removed is not acknowledged: its folder
  stays, and the item leaves *arr's history at the profile's Retention.
- Folders from before this behaviour, and folders of transfers acknowledged
  before it existed, are left as they are.

### Troubleshooting

| Symptom | Check |
|---|---|
| Queue shows **Downloaded — Waiting to Import**, or *arr reports `No files found are eligible for import in /download/...` | *arr is looking at DebridPulse's own path. Set the profile's External Import Path to the directory as *arr sees it (or add a Remote Path Mapping), then let *arr poll again. No resubmission is needed. |
| The reported path is right but *arr still cannot import | From *arr's host, list the reported folder: the share or mount must be present, and *arr's user must be able to enter every directory and read the files (write, for a move). |
| A folder exists but holds no video, or an unexpected file type | Look at the files in the folder: *arr imports only media it recognises. This is the posting's content, not a path problem. |
| The download client test fails with `404` | No usable integration yet: the profile needs a category, `nzb` among its Source Types, and an active token of an authorized client (above). |
| Jobs never appear in *arr's queue | The *arr category must match a category of a profile that authorizes this token's client. |
| Completed downloads stay in *arr's queue | Enable Completed Download Handling and "Remove Completed" in *arr; until it removes them, the deliveries are not acknowledged. |
| An empty folder remains after import | It is kept if another client or an operator's own request still needs the transfer, if anything is left in it, or if the delivery was not acknowledged. DebridPulse never deletes the folder's contents. |

*arr's **Test** checks:

- the version: DebridPulse reports its own, never a SABnzbd version;
- the key;
- that the category exists, with no sorting and no pre-check.

### Not Found until it is usable

Both paths of this dialect answer `404 Not Found` — to every caller, with or
without a key — until DebridPulse has at least one usable SABnzbd integration:
a delivery profile with a category, permitting `nzb` (an uploaded NZB; `nzb_link` and `links` do not count), that lists a client
holding an active Integration token (not revoked, of an enabled Integration
account). There is no switch: an Admin configures Integration Accounts and
Integration Delivery Profiles in Settings → Users & Access, and the dialect
appears or disappears with them on the next request — issuing, rotating or
revoking a token, disabling an account, or editing or removing a profile.

- A `404` here does not mean DebridPulse is down. *arr's Test reports it as a
  failed connection; check the profile and token first.
- The qBittorrent dialect is exposed or hidden on its own, by its own source kinds.
- Exposure authenticates nobody: every request still needs its own token, and
  sees only its own client's categories and jobs.
- Revoking the last qualifying token withdraws nothing; the jobs stay.

### The key travels in the URL

*arr sends the API key only as the `apikey` query parameter (never as a header).

- DebridPulse verifies it exactly as a Bearer token, and **only** on the two
  exact paths of this endpoint: `/api` and `/api/sabnzbd/api`. Nothing beneath
  `/api/` (DebridPulse's own API) accepts it, and neither does `/api/`.
- No session cookie, password or open-mode access authenticates there.
- DebridPulse does not log request lines (`uvicorn.access` is held at WARNING),
  and its log sanitizer redacts `apikey=`.
- A reverse proxy in front of DebridPulse logs URLs by default, exactly as it
  would for a real SABnzbd. Exclude the query string from its access log, or
  connect *arr to DebridPulse directly on the private network.

## 2. Calls answered

These are what Sonarr and Radarr call (Sonarr `57816c14`, Radarr `df2faa64`,
`src/NzbDrone.Core/Download/Clients/Sabnzbd/`):

| `mode` | Answer |
|---|---|
| `version` | `{"version": "<DebridPulse version>"}` |
| `get_config` | Categories this client may submit an NZB under. Each category's `dir` is its profile's `import_root`, and `complete_dir` is their common root. No sorters, no `pre_check`. History retention is `days-delete` with the shortest profile retention, in days. |
| `addfile` (POST, multipart part `name`) | `{"status": true, "nzo_ids": ["job_…"]}`, given only once the job and its one demand are durable |
| `queue` | The client's unfinished items |
| `history` | The client's completed and failed items |
| `queue&name=delete`, `history&name=delete` | See §5 |

Refusals are SABnzbd's own `{"status": false, "error": "…"}` with HTTP 200.
That is the form *arr reports: for example "API Key Incorrect", "API Key
Required", or a refused submission with its native code.

Declined explicitly, with nothing changed:

- `addurl` (*arr never sends it; it fetches the NZB itself);
- pause, resume, priority, retry, `change_cat`, `fullstatus`;
- `priority=-2` (add paused);
- `pp`, `script`, `nzbname`, `password`;
- a file in any multipart field other than `name`.

Other `priority` values are accepted and do not order anything: DebridPulse
owns its queue.

## 3. Submission

1. The upload is received whole.
2. It is bounded by the native external upload ceiling, **8 MB**. A larger NZB is
   refused with `source_too_large`; the request body is refused at the boundary
   beyond that.
3. It is submitted as the native `nzb` source kind (`ExternalClients.submit_live`).
4. The existing staged-input owner stores it.
5. The existing NZB reader validates it **before admission**.
6. The canonical NZB request admits it.

A refused or malformed posting is discarded and admits nothing.

**Retry identity (best-effort, not exactly-once).** SABnzbd's `addfile` carries
no request ID, and *arr resubmits a release later when a reply is lost.

- DebridPulse therefore treats an identical submission as the same job: the same
  client, the exact same NZB bytes, the same category and the same filename.
  This holds **only while that job is still live**: not withdrawn, failed,
  acknowledged or expired.
- It then answers the same `nzo_id` and creates no second demand. Concurrent
  identical submissions resolve to one job.
- Once the job has left the client's view, the same NZB is a new job.
- An indexer that serves different bytes for the same release produces a
  different identity, and so a second job.
- Nothing is normalized and no time window applies.

This accommodates the missing request ID. It is not a promise of exactly-once
submission.

## 4. Queue and history

The item is the native **job**; `nzo_id` is its ID. It is stable from queue to
history, across restarts, provider switches and consolidation. A transfer ID is
never exposed.

| Native job | SABnzbd status |
|---|---|
| accepted / resolving | `Grabbing` |
| queued, input required | `Queued` |
| downloading, or served by shared work | `Downloading` |
| verifying, material not yet verifiable after a restart | `Verifying` |
| finalizing (extraction) | `Extracting` |
| paused (by DebridPulse) | `Paused` |
| completed, with a delivery available now | history `Completed`, `storage` = its location |
| completed, not importable now | history `Verifying` (not a failure) |
| failed | history `Failed`, `fail_message` = `DebridPulse: <reason>` |
| withdrawn, acknowledged, expired | absent |

Further rules:

- **Global pause.** When processing is globally paused, `queue.paused` is true.
- **Progress is the bar DebridPulse shows.** Sonarr and Radarr draw their queue
  bar from `mb` and `mbleft` alone (`100 - mbleft / mb * 100`); they parse
  `percentage` but never read it. While a job is downloading and nothing is
  verified yet, DebridPulse's own primary bar is the current execution's
  figure, so:
  - with the executor's exact byte counters (native Usenet: SABnzbd's own `mb`
    / `mbleft` for the running job), `mb` and `mbleft` carry them and *arr's
    bar moves with DebridPulse's;
  - with only an estimate, or no in-flight figure, `mb` and `mbleft` read
    `0.00`: *arr draws no bar rather than an invented one;
  - once anything is verified, or past acquisition (repair, unpack),
    `percentage` is verified material and `mb`/`mbleft` read `0.00`.

  `percentage` always mirrors the same figure. A NZB's declared size is never
  used, and no total is derived from a percentage.
- **Unknown values.** Speed and time left are not part of the contract:
  `timeleft` reads `0:00:00`, SABnzbd's unknown form.
- **Completed means verified.** A job is Completed only when its whole delivery
  is committed and re-verified at that poll.
- **`storage`** is the single delivered file, or the one folder holding every
  delivered file beneath the import root. Extraction outputs are the recorded
  final set.
- **Shared folder.** A multi-file delivery lying loose in the import root has no
  folder of its own. It reads `Verifying` rather than offering other jobs' files.

## 5. Removal and `del_files`

Removal is decided by the item's condition. `del_files` and *arr's "delete data"
never authorize DebridPulse to delete anything.

| Item | Effect |
|---|---|
| Unfinished, only this client needs the work | The job's demand is withdrawn. The core cancels the transfer and, needed by nobody, removes it from the ordinary Downloads and Dashboard lists (its soft delete: history and provenance stay), each step atomically against demand that arrives meanwhile. Only DebridPulse's own incomplete material is reclaimed; nothing is removed at a provider. |
| Unfinished, others need the work | Only this client's demand is withdrawn; the work continues. |
| Completed | The delivery is acknowledged. DebridPulse keeps the transfer and its history; once every claim on the transfer is acknowledged, an empty folder DebridPulse created for it is removed (see "After the import"). |
| Failed | The demand is withdrawn. The core's failure policy applies. |
| Another client's or an unknown ID | Skipped silently: the answer is the same either way. |

Two consequences:

- When *arr removes a completed download with "delete data", **it deletes the
  files itself**. They are ordinary files in a directory it can write.
- An imported file that was moved or deleted is downstream consumption. It is
  never treated as a failure and never reacquired.

## 6. Not supported

- `addurl` and any URL submission.
- NZBs over 8 MB.
- Client-held pause, priority or queue order.
- Per-category paths chosen by *arr.
- Any qBittorrent or torrent dialect (a later phase).

## 7. Live verification (after deployment only)

The deterministic suite (`backend/tests/test_arr_sabnzbd_adapter.py`) replays
the recorded upstream requests and checks every response against *arr's parsing
rules. It does not run Sonarr or Radarr. After deployment:

1. Issue a token, add a profile with `source_kinds` including `nzb`, and add
   DebridPulse as a SABnzbd client in Sonarr.
2. **Test** must pass. A wrong key must report "API Key Incorrect".
3. Grab one release:
   - the item appears in Activity → Queue;
   - it is imported from `import_root` on completion;
   - with Remove Completed on, it disappears from history.
   DebridPulse's transfer stays completed.
4. Remove a queued item in *arr. DebridPulse cancels it only when no other client
   needs it.
5. Repeat with Radarr (`movies`).
