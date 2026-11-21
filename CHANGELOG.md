# Changelog

## [1.0.0] — First public release

DownloadPulse 1.0.0 is the first public release: a self-hosted download manager for a server or NAS,
and a desktop application for Windows, macOS and Linux, used from your browser. You give it a link, a
magnet, a `.torrent`, an NZB, a Metalink file or a list of links; it records one durable transfer,
works out which of your enabled services can obtain it, downloads the files with the right download
engine, and keeps that transfer and its history whole even when its service, source or engine
changes along the way.

### Included

- **Sources:** HTTP(S), FTP & SFTP, SCP, rsync, WebDAV, Metalink and supported media pages, with no
  premium account needed; magnets, `.torrent` files and NZBs through the services that accept them.
- **Premium services and Usenet, all optional:** AllDebrid, Debrid-Link, Premiumize, Real-Debrid and
  TorBox, and a bundled Usenet service for your own news servers. Each is off until you connect your
  own account.
- **Switching and recovery:** automatic retry and failover, backup torrents on a second service, and
  operator-controlled switching of a transfer's service or source.
- **File selection, extraction and delivery:** choose the files of a multi-file download, optional
  extraction of common archives, and a download folder of your choice.
- **Sonarr and Radarr:** DownloadPulse acts as a SABnzbd or qBittorrent download client through
  Integration Delivery Profiles.
- **The browser extension:** send links, files and supported video pages from Firefox or a
  Chromium-based browser, and follow and control the downloads it sent.
- **Accounts and access:** Standalone and Server deployment types; Admin, User, ReadOnly User and
  Integration accounts; Username & Password sign-in, OpenID Connect, and revocable tokens for
  connected tools.
- **Operating it:** scheduled backups with in-application restore, a searchable event log,
  statistics, Discord notifications and Prometheus metrics.
- **The interface:** a dashboard, downloads list, details and statistics, themes, and 19 languages.

### Installation

- **Server:** the Docker image `ghcr.io/xipher-zero/downloadpulse:v1.0.0` (Linux, amd64 and arm64)
  listens on port 8095. See the README's Installation section for the compose file.
- **Desktop:** installers for Windows (x64), macOS (Apple silicon) and Linux (Flatpak, x86_64).

### Known limitations

These are deliberate boundaries of 1.0.0, not defects. Each one fails safe: nothing is lost or
silently changed, and DownloadPulse tells you why an action is not available.

- **A source that was split into several files cannot have its provider switched.** When one
  submitted source resolves to several files that DownloadPulse downloads as separate members (for
  example a folder on a server, a Metalink description, or a provider's folder link), Switch
  provider is not offered for that source, and a switch requested through the API is refused as
  *Unsupported Request*. A single torrent, including one with many files, and a single link
  (including one link of a download made of several links) can still be switched. Switching an
  individual file to another of its own sources is unaffected.
- **A link shared with another unfinished download cannot be switched.** When DownloadPulse has
  recognised that a link of one download is the same source as another unfinished download's and
  serves both from one, that link's provider cannot be switched; the request is refused as
  *Unsupported Request*, also when the sharing is recognised while the switch is being made. A
  download that is a single torrent is switched as a whole and is not affected by this rule.
- **Automatic promotion of a prepared backup is narrow.** DownloadPulse moves a torrent to a backup
  another provider has already prepared only for a download made of a single torrent, only to the
  first backup that is ready, and never while the download is paused, after you have chosen its
  files, after it has been split into files, after its download has started, or when it was routed
  by the provider you prefer anyway. Backups are prepared only by providers that support it for
  torrents: AllDebrid, Debrid-Link, Real-Debrid and Premiumize (Prepare Backup Torrents on by default,
  once the provider itself is enabled); TorBox only when you turn
  it on, because TorBox's limits on new and active torrents are restrictive. Usenet, the direct
  sources (HTTP(S), FTP/SFTP, SCP, rsync, WebDAV), Metalink and Media Downloads never prepare
  backups.
- **A question waiting for your answer holds up the rest of that download.** While one part of a
  download is asking for input (a password, a sign-in or another answer), no other part of the same
  download starts anything new until you answer. This includes switching another part's provider:
  the switch is accepted and that part's earlier download stops, but it starts again on the
  provider you chose only after the question is answered. Other work of that download that was
  already running keeps running. Other downloads are not affected.
- **Cancelling one volume of a multi-part archive can make its extraction fail.** If you cancel one
  file of a download and that file is a volume of a split archive (for example `part02.rar`), the
  download still finishes once its remaining files are complete, but extraction of that archive
  fails because a volume is missing. The download is shown as completed with *Extraction failed*,
  the delivered volumes are kept, and nothing is overwritten or deleted. Retry downloads it again
  and extracts again.
- **Not every switch can keep what was already downloaded.** Only aria2 and rsync can continue a
  partly downloaded file from data another source delivered. Switching a file to a source that Media
  Downloads or the Usenet service (SABnzbd) would download starts it again from the beginning, and
  DownloadPulse asks you to confirm before it discards progress; if the progress changes after you
  confirmed, nothing is switched and you are asked again. After a provider has given up, automatic
  failover to another provider happens only before the source's files have been created.
- **Sonarr and Radarr see DownloadPulse as a qBittorrent or SABnzbd client, with these limits:**
  - Through the qBittorrent connection, a failed download is shown as *error*, which Sonarr and
    Radarr treat as a warning and never blocklist. Only API-token sign-in is supported (not the
    qBittorrent login/cookie sign-in), a torrent is added as one magnet link or one `.torrent` file
    (an http(s) link to a torrent is refused),
    torrents that are BitTorrent v2-only are refused, and options DownloadPulse does not support (such
    as changing a category, share limits, priority or forced start) are refused rather than ignored.
  - Through the SABnzbd connection, a failed download is reported as *Failed* (so the release is
    blocklisted and another is searched for) unless the cause is one you can fix without a new
    release: a provider's or download engine's credentials, configuration, availability or account
    limits, DownloadPulse's own storage, or a Delivery Profile problem. Those are reported as *Paused*.
    A completed download that cannot be imported yet is reported as *Verifying*.
  - A submitted `.torrent` can be at most 16 MiB and an NZB at most 8 MiB. Each submission is one
    item; the SABnzbd connection accepts uploaded NZBs only (adding by URL is refused). The native
    external-client API (`/api/external/v1`) takes at most 100 links per submission.
  - A Delivery Profile whose DP Download Path is outside the Download Folder keeps offering its
    categories, but new downloads to it are refused (`profile_root_outside_downloads`) until its path
    is moved back inside.
- **Very large downloads are bounded.** A file manifest can list at most 20,000 entries (paths up to
  1,024 characters), a Metalink description at most 10,000 files and 20,000 sources (16 MiB), a
  WebDAV scan at most 10,000 files by default, and an rsync listing 16 MiB. A source beyond these
  limits is refused rather than downloaded in part. Switching the provider of a torrent with
  hundreds of files takes noticeably longer than an ordinary switch.
- **Work grows with the number of files in one transfer.** DownloadPulse opens a fresh database
  connection for each step it takes, and many steps are taken file by file, so the engine's work in
  each cycle grows with how many files a transfer has. In one measured case, a transfer of 133
  files took about 3,000 database connections per engine cycle. That is an observation of that
  transfer, not a rate DownloadPulse guarantees or bounds.
- **Two mirror links of one file can each use the provider.** When a file is submitted through two
  mirror links (two separate links, not one item with alternative sources), each link can create its
  own resource at a debrid provider before DownloadPulse recognises them as the same file. This can
  use more of the account's quota and leave a duplicate at the provider; only one copy is
  downloaded and kept locally.
- **Some provider refusals end the download instead of trying another provider.** Real-Debrid's
  "file not allowed" (code 28) and Debrid-Link's `infringingFile` are treated as a permanent
  failure of the content, so the download fails instead of being handed to another provider.
  (Real-Debrid's code 35, by contrast, does hand over to the next provider.)
- **Live updates after a lost connection.** If the browser's live connection fails after it has
  worked once, the page marks itself as not live and tries to reconnect every 10 seconds, re-reading
  what it shows when it reconnects; it does not switch to periodic refreshing in the meantime. Only a
  page whose very first connection fails refreshes on a timer instead. Reloading the page restores
  live updates as soon as the server is reachable.
- **A browser that stops reading live updates.** When a browser stops reading its live updates (for
  example a suspended tab), DownloadPulse stops sending to it once it is 100 updates behind and keeps
  at most two pending messages for it, so it never holds up other browsers or grows memory. Its
  network connection itself can stay open until the browser or the network closes it.
- **aria2 downloads write each file at its final location.** aria2, and rsync with Partial
  Transfers on, write the file at its final path while it downloads; the Usenet service, Media
  Downloads and rsync with Partial Transfers off (or rebuilding from other material) already
  download privately and move the finished file into place. A general staged, engine-owned download with an atomic move into place on verified
  completion (`STAGED-MATERIALIZATION-001`) is not implemented in 1.0.0. A file
  whose download cannot be proven to have produced its content (for example an unproven empty
  file) is rejected and removed rather than reported complete, so where a file is written in
  place it can exist briefly at its final path before DownloadPulse retires it.
