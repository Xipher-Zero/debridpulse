<div align="center">
  <img src="docs/logo.svg" width="96" alt="DebridPulse Logo"/>
  <h1>DebridPulse</h1>
  <p><strong>Self-hosted transfer orchestration for direct links, magnets, torrents, NZBs and media pages.</strong><br/>Universal lifecycle · debrid, Usenet and network-source providers · aria2, rsync, SABnzbd and yt-dlp execution · recovery · observability</p>

  [![License](https://img.shields.io/github/license/Xipher-Zero/debridpulse?style=flat-square)](LICENSE)
  [![Tests](https://img.shields.io/github/actions/workflow/status/Xipher-Zero/debridpulse/tests.yml?style=flat-square&label=tests)](https://github.com/Xipher-Zero/debridpulse/actions/workflows/tests.yml)
  [![Image](https://img.shields.io/github/actions/workflow/status/Xipher-Zero/debridpulse/fork-image.yml?style=flat-square&label=image)](https://github.com/Xipher-Zero/debridpulse/actions/workflows/fork-image.yml)
</div>

---

## What is DebridPulse?

**DebridPulse** is a self-hosted transfer manager with a provider-independent orchestration core. `1.0.13` builds on the 1.0.12 **Universal Transfer Core**: identity, lifecycle, retry, ownership, recovery, canonicalization, provenance, authentication continuation and post-processing belong to that core rather than to any provider or download engine, and 1.0.13 adds premium debrid services, Usenet, network sources, media downloads, Sonarr/Radarr download-client compatibility, user accounts and in-application backup and restore on top of it.

The [architecture guide](docs/architecture/UNIVERSAL_TRANSFER_CORE.md) explains the contracts, normalized failures, persistence, routing, and extension boundaries; [`docs/architecture/MULTI_PROVIDER_HTTP_SLICE.md`](docs/architecture/MULTI_PROVIDER_HTTP_SLICE.md) is the design record of the original 1.0.12 two-provider slice (AllDebrid + General HTTP(S) providers) that this release generalizes.

The current release is `v1.0.13`. Check [GHCR](https://github.com/Xipher-Zero/debridpulse/pkgs/container/debridpulse) for the published image tags, and [`CHANGELOG.md`](CHANGELOG.md) for what changed, the **upgrade notes** and the **known limitations** of this release.

The workflow is deterministic:

1. Submit a link, magnet, `.torrent` file, NZB (file or link), Metalink description or a file of links.
2. DebridPulse creates one durable logical transfer before any provider or download-engine work.
3. Enabled provider applicability is classified neutrally: a matching `SPECIALIZED` claim (a debrid service's supported host, or a media page an installed yt-dlp extractor recognizes) takes precedence over `GENERIC` handlers such as **HTTP & HTTPS**; magnets, torrents and NZBs go to the providers that accept them.
4. The selected provider resolves the source into canonical candidates (for example unlocked links, a validated direct resource, or a Usenet job).
5. DebridPulse selects a download engine through the executor boundary -- aria2, rsync, the bundled SABnzbd for Usenet, or yt-dlp for Media Downloads -- and performs the physical transfer.
6. Provider route, candidate, engine and final-delivery provenance remain durable, so the interface reports what actually happened without inferring it from a URL.
7. Failed or expired work is retried, recovered or moved to another provider according to universal policy, without rebuilding the logical download manually.

DebridPulse runs and manages its own download engines (aria2, rsync, the bundled SABnzbd and yt-dlp); there is no separate download engine to install or connect.

---

## Core features

| Feature | Description |
|---|---|
| **Unified Dashboard submission** | Paste links (HTTP(S), FTP, SFTP, SCP, rsync, WebDAV, media pages) and magnet URIs into one mixed-input control, or use the same Add action with an empty field to choose a `.torrent`, NZB, Metalink or link file |
| **Batch link submission** | Submit up to 100 unique direct links in one tracked transaction |
| **Provider routing** | Premium debrid services and Media Downloads claim what they support as `SPECIALIZED`; General HTTP(S) supplies the `GENERIC` direct-download path when no specialized claim wins; a provider that gives up hands over to another that can take the request |
| **Premium services** | AllDebrid, Debrid-Link, Premiumize, Real-Debrid and TorBox for hoster links, magnets and `.torrent` files (and NZBs where the service offers them), with backup torrents prepared on a second provider |
| **Usenet** | A bundled, private SABnzbd downloads NZBs from your own news servers |
| **Network sources** | FTP & SFTP, SCP, rsync, WebDAV and Metalink sources, with sign-in prompts when a server asks |
| **Media Downloads** | yt-dlp downloads from media pages it explicitly recognizes, without re-encoding |
| **Provider switching** | Switch the provider of a torrent, of one link, or of every link of a download at once (best effort, link by link) while it downloads |
| **Sonarr and Radarr** | DebridPulse acts as a qBittorrent or SABnzbd download client through Integration Delivery Profiles |
| **User accounts and roles** | Admin, User, ReadOnly User and Integration accounts enforced by the server |
| **Backups and restore** | Scheduled backups and in-application restore |
| **Durable provider provenance** | Recent Activity, Downloads, and Details show current/final provider and ordered route history from persisted facts rather than URL inference |
| **Magnet links** | Submit one or more magnets to the premium services that accept them |
| **Torrent files** | Upload `.torrent` files to the premium services that accept them |
| **Pause-safe intake** | Pause All stops processing, not intake: new links, magnets, and `.torrent` files are recorded locally and begin provider work after Resume All |
| **Delayed link generation** | Automatically handles AllDebrid links that require asynchronous generation |
| **Managed aria2** | DebridPulse starts, configures, and supervises its bundled aria2 daemon for a self-contained deployment |
| **Unified Downloads view** | Direct links, magnets, torrent files, and imported transfers share one lifecycle and history |
| **Recent Activity** | Dashboard view of active and recently processed downloads |
| **Retry and recovery** | Retry failed transfers and regenerate expired provider download links from the original source |
| **Import existing magnets** | Import AllDebrid magnets not yet represented in the local database |
| **Live status updates** | Server-Sent Events carry the application's pulse without requiring full-page polling |
| **Event log** | Searchable transfer and application event history |
| **Statistics** | Built-in operational and download statistics |
| **Auto-extraction** | Optional post-download extraction of common archive formats |
| **Notifications** | Optional Discord notifications for download lifecycle events |
| **Prometheus metrics** | Application and transfer metrics through `/api/metrics` |
| **SQLite persistence** | SQLite/WAL is the authoritative application datastore |
| **Native authentication** | Intentional no-auth mode, Username & Password browser sessions + HTTP Basic API access, provider-neutral OIDC, and optional bearer tokens for machine clients |

---

## Direct-link downloads

Direct HTTP(S) resources are first-class DebridPulse transfers rather than untracked aria2 jobs.

Paste one or more HTTP/HTTPS links into the unified Dashboard submission field (direct links and magnets may be mixed, one item per line). DebridPulse then:

1. validates and durably records the original resource;
2. evaluates enabled provider applicability without contacting a provider;
3. routes a host a premium service supports to that service as `SPECIALIZED`, otherwise routes an eligible ordinary HTTP(S) resource to the `GENERIC` **HTTP & HTTPS** provider;
4. records the selected provider route and canonical candidate;
5. dispatches the candidate to a download engine (aria2 for HTTP(S));
6. tracks progress and durable provider/executor provenance with the rest of the download queue.

When a premium service is the selected provider, delayed generation and later candidate refresh remain provider-owned behavior. When General HTTP(S) is selected, aria2 receives the validated direct resource; conventional HTTP authentication is requested only after a genuine supported authentication challenge. The same logical transfer continues after corrected credentials.

A single submission can contain up to **100 unique links**.

---

## Torrent downloads

DebridPulse supports both magnet links and `.torrent` files.

For a torrent submission:

1. the magnet or torrent metadata is sent to a premium service that accepts it (AllDebrid, Debrid-Link, Premiumize, Real-Debrid or TorBox);
2. DebridPulse monitors that service's torrent state, and may prepare a backup on a second service;
3. once files are available, their unlocked HTTP(S) links are retrieved;
4. those files are dispatched to aria2;
5. DebridPulse tracks the complete transfer lifecycle locally.

DebridPulse's aria2 is built without BitTorrent and never joins a swarm. The premium service performs the torrent-side work and aria2 downloads the resulting files.

---

## v1.0.13 provider and executor support matrix

This matrix is the **implemented and qualified v1.0.13 provider/executor set**. Anything not listed here is not supported.

| Integration | Kind | Enabled on a new installation |
|---|---|---|
| AllDebrid | Premium provider | Yes (needs an API key to resolve anything) |
| Debrid-Link, Premiumize, Real-Debrid, TorBox | Premium providers | No — enable under **Settings → Services** |
| Usenet (bundled SABnzbd) | Provider with its own download engine | No — enable under **Settings → Services** |
| HTTP & HTTPS, FTP & SFTP, SCP, rsync, WebDAV | Network-source providers | Yes |
| Multimeta (Metalink) | Provider | Yes |
| Media Downloads (yt-dlp) | Provider with its own download engine | Yes |
| aria2, rsync | Download engines | Yes |

The [`CHANGELOG.md`](CHANGELOG.md) `[1.0.13]` entry lists the **known limitations** of this release (what cannot be switched, when a backup is promoted automatically, how a question waiting for input holds up the rest of a download, what cancelling one volume of a split archive does to extraction, what Sonarr and Radarr see, and how large sources are bounded). `STAGED-MATERIALIZATION-001` (staged, engine-owned downloading with atomic promotion) is not implemented; it is recorded there as a known limitation.

---

## aria2

DebridPulse starts its bundled aria2 daemon with the application and stops it on shutdown. The daemon
listens on loopback only; DebridPulse constructs its RPC endpoint and credentials itself, applies the
native engine settings, and writes every download into the Download Folder. On restart DebridPulse
reconciles its tracked downloads against the daemon.

Every aria2 job is still routed through the DebridPulse egress guard with per-job proxy, credential,
and host-key settings, so a job never inherits daemon-wide network configuration.

---

## Installation

### Docker Compose

Clone the repository:

```bash
git clone https://github.com/Xipher-Zero/debridpulse.git
cd debridpulse
```

Review `docker-compose.yml` before starting it. Adapt host paths, UID/GID, timezone, networking, and persistent storage to your environment. The generic example uses bridge networking and an explicit `8080:8080` port mapping; use host networking only when your platform specifically requires it.

Then start DebridPulse:

```bash
docker compose up -d
```

Open:

```text
http://your-server:8080
```

Go to **Settings → Services**. Enable the premium services and network sources you intend to use and connect each premium service's account (an API key, or the service's own device sign-in). **HTTP & HTTPS** intentionally has only its canonical enable control. If the installation is reachable by users or networks you do not fully trust, configure native access control under **Settings → Authentication** before exposing it beyond that trusted boundary.

### Docker image

Images are published to GHCR. Every image is built once immutably and tagged by its full source SHA, then scanned and runtime-qualified on `linux/amd64` and `linux/arm64` with signed in-registry attestations; a versioned tag is only ever re-pointed at a digest that already passed every required gate.

Versioned images use the release tag:

```text
ghcr.io/xipher-zero/debridpulse:v1.0.13
```

Example:

```bash
docker run -d \
  --name debridpulse \
  --restart unless-stopped \
  -p 8080:8080 \
  -e PUID=1000 \
  -e PGID=1000 \
  -e TZ=America/Phoenix \
  -e CONFIG_PATH=/app/config/config.json \
  -e DB_PATH=/app/data/debridpulse.db \
  -v /path/to/debridpulse/config:/app/config \
  -v /path/to/debridpulse/data:/app/data \
  -v /path/to/downloads:/download \
  ghcr.io/xipher-zero/debridpulse:v1.0.13
```

Adjust the paths and UID/GID for your system. Before upgrading an existing installation, read the **Upgrade notes** of the `[1.0.13]` entry in [`CHANGELOG.md`](CHANGELOG.md): among them, the first start deletes backups beyond the newest seven without asking, and `CORS_ORIGINS=*` now prevents startup.

---

## Configuration

The primary supported configuration is available through **Settings**.

### Services

**Settings → Services** lists the Premium Services (AllDebrid, Debrid-Link, Premiumize, Real-Debrid, TorBox and Usenet) and the Network Sources (HTTP & HTTPS, FTP & SFTP, SCP, rsync, WebDAV, Multimeta and Media Downloads), each with its own enable control, account connection and Additional Settings.

Provider enablement is backend-owned configuration. `enabled`, `configured`, provider health, and current host availability are distinct facts.

### Authentication

Authentication has its own Settings tab.

DebridPulse supports four effective interactive states:

- **No authentication** — supported for trusted standalone/LAN deployments;
- **Username & Password** — browser sign-in through the DebridPulse login page, with HTTP Basic available to REST clients using the same credentials;
- **OIDC** — provider-neutral OpenID Connect Authorization Code + PKCE browser sign-in;
- **Username & Password + OIDC** — OIDC-preferred browser UX with the known-working local password path retained.

Accounts and roles (Admin, User, ReadOnly User and Integration) are managed under **Settings → Users & Access**. Integration accounts carry the `dp_...` bearer tokens used by automation, Prometheus, Sonarr and Radarr; a token is shown only once and only its verifier is stored.

DebridPulse includes lockout protections: Password cannot be disabled in favor of OIDC until a real OIDC login succeeds, and critical OIDC changes in OIDC-only mode stay pending until the proposed configuration itself completes a successful sign-in. Deliberately disabling all interactive authentication requires explicit confirmation.

See **[docs/authentication.md](docs/authentication.md)** for configuration examples, reverse-proxy/OIDC callback guidance, API authentication, credential lifecycle, and recovery behavior.

### Downloads

Configure:

- download directory;
- DebridPulse download concurrency;
- download concurrency, limits, and recovery behavior.

### Extraction

Configure optional archive extraction. DebridPulse enforces per-archive file-count, expanded-size, and compression-ratio limits. Every supported archive format is extracted into an isolated staging directory, validated, and committed to the download tree with no-clobber semantics.

### Notifications

Configure optional Discord lifecycle notifications.

### Data & Maintenance

DebridPulse uses a single authoritative SQLite/WAL database. Configure its persistent path through `DB_PATH` or the container data mount.

The **Data & Maintenance** tab configures scheduled backups (backup folder, interval, retention and the number kept), lists and restores backups in the application, holds the statistics snapshot settings and Event Logging, and the guarded **Reset Database** action. Reset stays locked until **Allow Database Reset** is enabled; it always creates a safety backup first and is aborted if that backup fails.

---

## Dashboard

The Dashboard is intended for current activity and common download submission.

It provides:

- one unified submission field for links (HTTP(S), FTP/SFTP, SCP, rsync, WebDAV, media pages, NZB links) and magnets;
- `.torrent`, NZB, Metalink and link-file selection from the same Add control when the field is empty;
- import and recovery controls;
- current queue state;
- completion and error counts;
- recent download activity.

The Dashboard intentionally shows only a small Recent Activity window. Use **Downloads** for full transfer history and management.

**Pause All stops processing, not intake.** Submissions made while globally paused are durably recorded with a paused state; no new provider or download-engine work is started until Resume All (or an explicit per-transfer resume) releases them.

---

## Downloads

The **Downloads** view is the unified transfer history.

It includes transfers originating from:

- direct debrid links;
- magnets;
- `.torrent` files;
- NZBs, network sources and media pages;
- imported AllDebrid entries;
- supported API submissions, including Sonarr and Radarr through their download-client connections.

Transfers can be searched and filtered by state, with retry, reset, pause, resume, and delete controls available where applicable.

---

## REST API

DebridPulse exposes a REST API used by the web interface and available for external automation.

When Username & Password authentication is enabled, REST clients may use standard HTTP Basic with the same credentials as the browser login. Automation clients use an Integration account's token, `Authorization: Bearer dp_...`, which grants only submission, its own requests and `/api/metrics`. In an intentional no-auth deployment the API remains open. See [docs/authentication.md](docs/authentication.md) for precedence and security details.

### Download submission and management

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/links/add` | Submit one or more links (HTTP(S), network sources, media pages) |
| `POST` | `/api/torrents/add-magnet` | Submit a magnet link |
| `POST` | `/api/torrents/add-file` | Upload a `.torrent` file |
| `GET` | `/api/torrents` | List tracked downloads |
| `GET` | `/api/torrents/{id}` | Retrieve a tracked download |
| `DELETE` | `/api/torrents/{id}` | Delete a tracked download |
| `POST` | `/api/torrents/{id}/retry` | Retry a failed download |
| `POST` | `/api/torrents/import-existing` | Import existing AllDebrid magnets |
| `POST` | `/api/torrents/recover-all` | Recover eligible stuck or failed transfers |
| `GET` | `/api/torrents/diagnose` | Return transfer-state diagnostics |

### Application state

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/stats` | Application and transfer statistics |
| `GET` | `/api/settings` | Current redacted application settings |
| `PUT` | `/api/settings` | Update application settings (account credentials are managed under Users & Access) |
| `GET` | `/api/events/stream` | Server-Sent Events status stream |
| `GET` | `/api/metrics` | Prometheus-compatible metrics |
| `GET` | `/api/version` | DebridPulse version |
| `GET` | `/api/health` | Lightweight application health endpoint |

### Authentication API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/auth/status` | Minimal public login/bootstrap authentication state |
| `GET` | `/api/auth/session` | Current application-session state |
| `POST` | `/api/auth/logout` | Revoke the current browser application session |
| `GET` | `/api/auth/config` | Redacted authentication configuration/status |
| `PUT` | `/api/auth/config` | Update native authentication configuration through the lockout state machine |
| `POST` | `/api/auth/oidc/verify-config` | Stage proposed OIDC settings and start full verification sign-in |
| `GET/POST/DELETE` | `/api/accounts/{id}/tokens` | List, issue (shown once) or revoke an Integration account's bearer tokens (Admin) |
| `GET` / `POST` | `/api/requests`, `/api/requests/{id}/withdraw` | List and withdraw your own submitted requests (Admin: anyone's) |

---

## Scope

DebridPulse acquires what it is given; it does not look for it. Sonarr and Radarr can use DebridPulse as a download client (through its qBittorrent- and SABnzbd-compatible connections), but DebridPulse itself deliberately excludes:

- Jackett/Prowlarr search and indexer management;
- FlexGet and RSS automation;
- saved-search and media-library automation.

DebridPulse is a **download and transfer manager**, not an all-in-one media automation suite.

---

## Development

Backend requirements are under `backend/`.

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest tests -v
```

Run the development server with:

```bash
uvicorn main:app --reload --port 8080
```

The primary implementation areas are:

| Area | Responsibility |
| --- | --- |
| `backend/transfers/` | Universal contracts, lifecycle, policy and persistence |
| `backend/application/` | Commands, composition and observability |
| `backend/integrations/` | Modular definitions and configuration |
| `backend/providers/` | One package per provider (premium services, Usenet, network sources, Multimeta, Media Downloads) |
| `backend/executors/` | aria2, rsync, SABnzbd and yt-dlp execution, ownership and administration |
| `backend/external/` | External clients: Delivery Profiles and the Sonarr/Radarr connections |
| `backend/postprocessors/archive/` | Secure archive extraction |
| `backend/api/`, `backend/auth/` | HTTP boundaries and authentication |
| `backend/db/migrations/` | Transactional database upgrades |
| `frontend/static/`, `frontend/browser/` | Browser application and runtime tests |


Runtime dependencies are exactly pinned through `backend/requirements.in` → `backend/requirements.txt`. Python runtime license metadata is checked against `licenses/python-runtime.json`; published images also carry BuildKit provenance and SBOM attestations. See [`docs/DEPENDENCY_LICENSES.md`](docs/DEPENDENCY_LICENSES.md).

Release notes for every version are in [`CHANGELOG.md`](CHANGELOG.md); the release operations runbook is [`docs/RELEASE_PROCESS.md`](docs/RELEASE_PROCESS.md).

---

## Project direction

DebridPulse favors a focused responsibility:

> **Resolve sources, transfer files reliably, and make delivery observable and recoverable.**

Features that improve that workflow belong naturally in DebridPulse. Providers and download engines are independent implementations of canonical contracts; the ones included in 1.0.13 are listed in the support matrix above.

Recreating an entire media-management or indexer ecosystem inside the download client does not.

---

## License

DebridPulse is copyright © 2026 Chris Moore and is distributed under
[`GPL-2.0-or-later`](LICENSE). Third-party and inherited copyright and permission
notices are preserved in [`NOTICE`](NOTICE) and [`LICENSES/`](LICENSES).
Runtime dependency licensing is inventoried in
[`docs/DEPENDENCY_LICENSES.md`](docs/DEPENDENCY_LICENSES.md). The container source
offer is documented in [`SOURCE_OFFER.md`](SOURCE_OFFER.md).
