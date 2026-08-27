<div align="center">
  <img src="docs/logo.svg" width="96" alt="DebridPulse Logo"/>
  <h1>DebridPulse</h1>
  <p><strong>A self-hosted AllDebrid download client for direct links, magnets, and torrent files.</strong><br/>AllDebrid processing · aria2 downloads · unified transfer tracking · recovery · observability</p>

  [![License](https://img.shields.io/github/license/Xipher-Zero/debridpulse?style=flat-square)](LICENSE)
  [![Tests](https://img.shields.io/github/actions/workflow/status/Xipher-Zero/debridpulse/tests.yml?style=flat-square&label=tests)](https://github.com/Xipher-Zero/debridpulse/actions/workflows/tests.yml)
  [![Image](https://img.shields.io/github/actions/workflow/status/Xipher-Zero/debridpulse/fork-image.yml?style=flat-square&label=image)](https://github.com/Xipher-Zero/debridpulse/actions/workflows/fork-image.yml)
</div>

---

## What is DebridPulse?

**DebridPulse** is a self-hosted debrid download manager for direct links, magnet links, and `.torrent` files. V1 submits work through AllDebrid and manages the resulting transfers through aria2.

The normal workflow is intentionally simple:

1. Submit an ordinary HTTP/HTTPS hoster link, magnet link, or `.torrent` file.
2. DebridPulse sends it to AllDebrid for unlocking or torrent processing.
3. AllDebrid produces downloadable HTTP(S) file URLs.
4. DebridPulse dispatches those files to aria2.
5. The resulting transfer is tracked through the Dashboard, Downloads view, statistics, and event log.
6. Failed or expired transfers can be retried or recovered without rebuilding the download manually.

DebridPulse can manage its own built-in aria2 instance or safely use a shared external aria2 daemon.

---

## Core features

| Feature | Description |
|---|---|
| **Unified Dashboard submission** | Paste HTTP/HTTPS direct links and magnet URIs into one mixed-input control, or use the same Add action with an empty field to choose a `.torrent` file |
| **Batch link submission** | Submit up to 100 unique direct links in one tracked transaction |
| **Magnet links** | Submit one or more magnets through AllDebrid |
| **Torrent files** | Upload `.torrent` files directly to AllDebrid |
| **Pause-safe intake** | Pause All stops processing, not intake: new links, magnets, and `.torrent` files are recorded locally and begin provider work after Resume All |
| **Delayed link generation** | Automatically handles AllDebrid links that require asynchronous generation |
| **Built-in aria2** | Run DebridPulse with its bundled aria2 instance for a self-contained deployment |
| **External aria2** | Connect to an existing aria2 JSON-RPC daemon |
| **Shared aria2 safety** | Tracks DebridPulse-owned downloads and avoids modifying global settings, result history, or unrelated transfers on external aria2 instances |
| **Unified Downloads view** | Direct links, magnets, torrent files, and imported transfers share one lifecycle and history |
| **Recent Activity** | Dashboard view of active and recently processed downloads |
| **Retry and recovery** | Retry failed transfers and regenerate expired AllDebrid download URLs from the original source |
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

Direct hoster links are first-class DebridPulse transfers rather than untracked aria2 jobs.

Paste one or more HTTP/HTTPS links into the unified Dashboard submission field (direct links and magnets may be mixed, one item per line). DebridPulse then:

1. validates and records the original URL;
2. asks AllDebrid to unlock the link;
3. waits for delayed generation when required;
4. records the generated file information;
5. submits the generated download URL to aria2;
6. tracks progress with the rest of the download queue.

The original source URL is retained. If an AllDebrid-generated URL expires, DebridPulse can generate a new one during retry or recovery.

A single submission can contain up to **100 unique links**.

---

## Torrent downloads

DebridPulse supports both magnet links and `.torrent` files.

For a torrent submission:

1. the magnet or torrent metadata is sent to AllDebrid;
2. DebridPulse monitors the AllDebrid torrent state;
3. once files are available, their unlocked HTTP(S) links are retrieved;
4. those files are dispatched to aria2;
5. DebridPulse tracks the complete transfer lifecycle locally.

The local aria2 daemon does **not** need to participate in BitTorrent swarms. AllDebrid performs the torrent-side work and aria2 downloads the resulting files.

---

## aria2 modes

### Built-in aria2

The default configuration can run a bundled aria2 instance controlled by DebridPulse.

In this mode DebridPulse owns the daemon and can manage its runtime configuration.

### External aria2

DebridPulse can instead use an existing aria2 JSON-RPC endpoint.

External mode is designed to be safe for a **shared aria2 daemon**. DebridPulse maintains ownership information for downloads that it creates and does not assume that every transfer in aria2 belongs to DebridPulse.

In external mode DebridPulse intentionally avoids operations such as:

- changing daemon-wide bandwidth limits;
- rewriting global aria2 configuration;
- purging global download-result history;
- controlling unrelated aria2 GIDs.

Application-level concurrency for DebridPulse-owned jobs remains independently configurable.

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

Go to **Settings → General** and configure your AllDebrid API key. If the installation is reachable by users or networks you do not fully trust, configure native access control under **Settings → Authentication** before exposing it beyond that trusted boundary.

### Docker image

Fork-owned images are published to GHCR.

Versioned V1 images use the release tag:

```text
ghcr.io/xipher-zero/debridpulse:v1.0.10
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
  ghcr.io/xipher-zero/debridpulse:v1.0.10
```

Adjust the paths and UID/GID for your system.

---

## Configuration

The primary supported configuration is available through **Settings**.

### General

Configure:

- AllDebrid API key;
- AllDebrid agent name;
- disk-space guard and provider retry/rate-limit behavior.

### Authentication

Authentication has its own Settings tab and is not configured in General.

DebridPulse supports four effective interactive states:

- **No authentication** — supported for trusted standalone/LAN deployments;
- **Username & Password** — browser sign-in through the DebridPulse login page, with HTTP Basic available to REST clients using the same credentials;
- **OIDC** — provider-neutral OpenID Connect Authorization Code + PKCE browser sign-in;
- **Username & Password + OIDC** — OIDC-preferred browser UX with the known-working local password path retained.

An independent `dp_...` bearer token can be generated for automation, Prometheus, scripts, and other machine clients. The raw token is shown only once and is never persisted.

DebridPulse includes lockout protections: Password cannot be disabled in favor of OIDC until a real OIDC login succeeds, and critical OIDC changes in OIDC-only mode stay pending until the proposed configuration itself completes a successful sign-in. Deliberately disabling all interactive authentication requires explicit confirmation.

See **[docs/authentication.md](docs/authentication.md)** for configuration examples, reverse-proxy/OIDC callback guidance, API authentication, credential lifecycle, and recovery behavior.

### Download

Configure:

- download directory;
- built-in or external aria2 mode;
- external aria2 URL and authentication when applicable;
- DebridPulse download concurrency;
- download filtering and limits.

### Extract

Configure optional archive extraction. DebridPulse enforces per-archive file-count, expanded-size, and compression-ratio limits. Every supported archive format is extracted into an isolated staging directory, validated, and committed to the download tree with no-clobber semantics.

### Notifications

Configure optional Discord lifecycle notifications.

### Database

DebridPulse uses a single authoritative SQLite/WAL database. Configure its persistent path through `DB_PATH` or the container data mount.

### Advanced

Additional application and operational settings are available here.

---

## Dashboard

The Dashboard is intended for current activity and common download submission.

It provides:

- one unified direct-link/magnet submission field;
- `.torrent` file selection from the same Add control when the field is empty;
- import and recovery controls;
- current queue state;
- completion and error counts;
- recent download activity.

The Dashboard intentionally shows only a small Recent Activity window. Use **Downloads** for full transfer history and management.

**Pause All stops processing, not intake.** Submissions made while globally paused are durably recorded with a paused state; no new AllDebrid or aria2 work is started until Resume All (or an explicit per-transfer resume) releases them.

---

## Downloads

The **Downloads** view is the unified transfer history.

It includes transfers originating from:

- direct debrid links;
- magnets;
- `.torrent` files;
- imported AllDebrid entries;
- supported API submissions.

Transfers can be searched and filtered by state, with retry, reset, pause, resume, and delete controls available where applicable.

---

## REST API

DebridPulse exposes a REST API used by the web interface and available for external automation.

When Username & Password authentication is enabled, REST clients may use standard HTTP Basic with the same credentials as the browser login. When an API token is enabled, clients may instead send `Authorization: Bearer dp_...`. In an intentional no-auth deployment the API remains open. See [docs/authentication.md](docs/authentication.md) for precedence and security details.

### Download submission and management

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/links/add` | Submit one or more direct HTTP/HTTPS links |
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
| `PUT` | `/api/settings` | Update application settings; legacy auth-compatible path remains subject to the same lockout policy |
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
| `GET/PUT/POST/DELETE` | `/api/auth/api-token` | Inspect, enable/disable, generate/rotate, or clear the machine bearer credential |

---

## V1 scope boundary

V1 intentionally excludes the media-automation and indexer surface inherited from the original codebase, including:

- qBittorrent API emulation;
- Sonarr/Radarr integration;
- Jackett/Prowlarr search;
- FlexGet;
- saved-search and automation systems.

Their routes, services, scheduler jobs, configuration, UI, database tables, and dependencies are not part of the V1 application. DebridPulse is a **debrid download manager**, not an all-in-one media automation suite.

---

## Development

Backend requirements are under `backend/`.

```bash
cd backend
pip install -r requirements.txt
python -m pytest tests -v
```

Run the development server with:

```bash
uvicorn main:app --reload --port 8080
```

The primary implementation areas are:

```text
backend/
  api/
    routes.py
    auth_routes.py
    auth_config_routes.py
    serializers.py
  auth/
    manager.py
    middleware.py
    passwords.py
    sessions.py
    oidc.py
    pending_oidc.py
    api_tokens.py
    transitions.py
  core/
    scheduler.py
  services/
    transfer_service.py
    transfer_repository.py
    transfer_state_machine.py
    transfer_control_service.py
    dispatch_coordinator.py
    reconciliation_service.py
    provider_gateway.py
    aria2_gateway.py
    ownership_ledger.py
    extraction_safety.py
    manager_v2.py        # V1 provider/materialization implementation
  db/
    database.py

frontend/static/
  index.html
  app.js
  auth.js
  auth-settings.js
  auth-help.js
  style.css
```

Runtime dependencies are exactly pinned through `backend/requirements.in` → `backend/requirements.txt`. Python runtime license metadata is checked against `licenses/python-runtime.json`; published images also carry BuildKit provenance and SBOM attestations. See [`docs/DEPENDENCY_LICENSES.md`](docs/DEPENDENCY_LICENSES.md).

---

## Project direction

DebridPulse favors a focused responsibility:

> **Submit work to AllDebrid, retrieve the resulting files, download them reliably, and make that lifecycle observable and recoverable.**

Features that improve that workflow belong naturally in DebridPulse. Provider-specific integrations belong behind the DebridPulse provider layer; V1 uses AllDebrid as its provider backend.

Recreating an entire media-management or indexer ecosystem inside the download client does not.

---

## Upstream provenance

DebridPulse originated as a fork of [`kroeberd/alldebrid-client`](https://github.com/kroeberd/alldebrid-client) release **v1.9.9**, commit:

```text
c0f7a5bfeba4f259fb2acc62ac6eed27e8ac4d5c
```

Since that fork point, DebridPulse has substantially diverged in application architecture, transfer lifecycle and control, safety and recovery semantics, authentication, persistence behavior, and user interface. Portions of the original codebase remain and retain their original MIT attribution.

Copyright © 2026 kroeberd applies to the retained upstream work. The original MIT copyright and permission notice are preserved in [`NOTICE`](NOTICE) and [`LICENSES/MIT.txt`](LICENSES/MIT.txt).

See [`INTERNAL_FORK.md`](INTERNAL_FORK.md) for historical notes about the original fork point and [`CHANGELOG.md`](CHANGELOG.md) for current DebridPulse changes.

---

## License

DebridPulse modifications are copyright © 2026 Chris Moore and are distributed under
[`GPL-2.0-or-later`](LICENSE). The upstream MIT copyright and permission notice
are preserved in [`NOTICE`](NOTICE) and [`LICENSES/MIT.txt`](LICENSES/MIT.txt).
Runtime dependency licensing is inventoried in
[`docs/DEPENDENCY_LICENSES.md`](docs/DEPENDENCY_LICENSES.md). The container source
offer is documented in [`SOURCE_OFFER.md`](SOURCE_OFFER.md).