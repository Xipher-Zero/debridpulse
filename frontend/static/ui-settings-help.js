/* DebridPulse Settings contextual help.
 *
 * The ONE owner of what each Settings card's help says. Every Settings card
 * (ui-settings-page.js, the sole Settings markup owner) renders one help chip
 * naming its topic here; activating it calls `DPSettingsHelp.open(topic)`,
 * which presents that topic read-only in the canonical dialog shell
 * (ui-settings-modal.js, `dismiss: true`): its own title, a body, and one
 * Close control, with that shell's focus trap, Escape, focus return, and
 * stacking above an open dialog.
 *
 * Content only. Nothing here reads or writes a setting, issues a request,
 * or names a stored value, and every sentence states behaviour the backend
 * implements today. Text is assigned through textContent; no topic carries
 * markup.
 */
(function () {
  'use strict';

  const YES = 'Yes';
  const NO = 'No';

  /* Shared by User Accounts (the card) and the Role selectors of Add Local
   * Account and Change Role. Derived from backend/auth/permissions.py
   * (ROLE_CAPABILITIES, ACTIONS, destruction_precondition). */
  const ROLE_TABLE = Object.freeze({
    caption: 'What each role can do',
    head: ['Capability', 'Admin', 'User', 'ReadOnly User', 'Integration'],
    rows: [
      ['Sign in to this browser interface', YES, YES, YES, NO],
      ['View the Dashboard, Downloads, and transfer details', YES, YES, YES, NO],
      ['View Statistics', YES, YES, YES, 'Summary counts through the API only'],
      ['Add downloads', YES, YES, NO, 'Through the API only'],
      ['Pause, resume, retry, and reorder transfers', YES, YES, NO,
        'Asks to pause or resume its own jobs; DebridPulse may decline'],
      ['Delete, cancel, or redirect transfers', YES,
        'Only when no other account still needs the transfer', NO, NO],
      ['Withdraw download requests', 'Any account’s', 'Its own', NO, 'Its own jobs'],
      ['Discord notifications and statistics reports', 'The global ones, in Settings',
        'Its own, under Notifications', NO, NO],
      ['Settings, accounts, maintenance, the Activity Log, and global pause', YES, NO, NO, NO],
      ['Hold API tokens', NO, NO, NO, 'Yes, issued by an Admin'],
    ],
  });

  const TOPICS = Object.freeze({
    /* ── Services ─────────────────────────────────────────────────────── */
    'premium-services': {
      title: 'Premium Services',
      sections: [
        {text: [
          'Premium Services are the accounts DebridPulse can use to obtain downloads on your behalf: debrid services such as AllDebrid, Real-Debrid, TorBox, Debrid-Link, and Premiumize, and your own Usenet news servers. Each service has its own card here.',
          'Every premium service is switched off in a new installation, and none is required: Network Sources work without one. A choice you have saved, on or off, is always kept.',
        ]},
        {heading: 'On each card', list: [
          'Enable decides whether the service takes part in new downloads. Turning it off keeps its saved credentials and settings.',
          'Help on each card explains that service: how it connects, what your account with it can do, and its own settings.',
          'The credentials are stored on the server and are never shown again after saving; leave a secret field blank to keep the stored value.',
          'Test checks the saved configuration against the service. The status beside it reports Unconfigured (enabled but nothing usable is saved), Unverified (saved but not yet proven by a test), or Verified (the current saved configuration passed a test).',
          'Additional Settings hold that service’s own limits and options; they apply to that service only. Provider Poll Interval, Full Sync Interval, Resolution Failure Retries, and Retry Delay are configured under Downloads → Download Behavior & Limits → Advanced Settings. Full Sync applies to eligible premium services; resolution retries also cover Network Sources.',
        ]},
        {heading: 'Usenet', text: [
          'Usenet uses your configured news servers rather than a debrid account. Open the Usenet card’s help for server configuration and download behavior.',
        ]},
        {heading: 'Related', text: [
          'Direct sources that need no account, such as HTTP, FTP, or rsync links, are configured under Network Sources instead.',
        ]},
      ],
    },
    usenet: {
      title: 'Usenet',
      sections: [
        {text: [
          'Usenet downloads NZB content directly from news servers you have an account with. Unlike a debrid service, there is no provider account doing the work: DebridPulse runs its own Usenet download service inside the container, connects to your servers, and assembles the files itself. It is off until you enable it.',
        ]},
        {heading: 'News servers', list: [
          'Each server has a host, a port, and an SSL setting (switching SSL moves an unchanged default port between 563 and 119), plus the username and password for that server. The stored password is never shown; leave the field blank to keep it.',
          'Connections is how many simultaneous connections DebridPulse may open to that server, from 1 upward; set it no higher than your account allows.',
          'Priority orders the servers: a lower value is preferred, and 0 is the highest.',
          'Articles per Request and Server Timeout tune how DebridPulse talks to that server. Each server also has its own Enable; a disabled server is never contacted.',
        ]},
        {heading: 'Status and Test', list: [
          'Usenet is Configured once at least one enabled server has a host and at least one connection, and Verified only when every enabled server has passed a test of its current connection settings.',
          'A server’s own Test makes a real connection with what its card currently shows, including an unsaved password, and saves nothing. Test in this card’s header tests every saved, enabled server.',
        ]},
        {heading: 'Downloads and recovery', list: [
          'A Usenet download is an ordinary DebridPulse transfer: it counts toward Maximum Concurrent Downloads, can be paused and resumed, and follows the Stalled Download Timeout and download error retries. After a restart DebridPulse finds its Usenet jobs again rather than submitting them twice.',
          'DebridPulse verifies and repairs the downloaded files, but never unpacks them in the Usenet service; Automatic Extraction handles archives afterwards, as for any download.',
          'Behavior shared by every server is under Downloads, Transfer Method Settings, Usenet: the article cache, Direct Write, how many times one article is retried on a server before that server is given up for it (not the download retry count), and how long DebridPulse waits for its Usenet service to answer.',
        ]},
        {heading: 'NZBs and premium services', list: [
          'TorBox and Premiumize can also take NZBs where your plan with them includes it, per each provider’s documentation. By default an NZB goes to Usenet first; turning on Use TorBox Before Usenet or Use Premiumize Before Usenet in that service’s Additional Settings puts it ahead of Usenet for NZBs.',
          'If the first service in that order cannot resolve the NZB, the next one is tried.',
        ]},
      ],
    },
    alldebrid: {
      title: 'AllDebrid',
      sections: [
        {text: [
          'AllDebrid is a debrid service: it fetches hoster links, magnets, and .torrent files on its own servers, and DebridPulse then downloads the result to your storage. DebridPulse uses your own AllDebrid account through its API key. AllDebrid is off until you enable it.',
        ]},
        {heading: 'Connecting', list: [
          'Paste the API key from your AllDebrid account into API Key. It is saved when you leave the field, is stored on the server, and is never shown again. Remove API Key deletes the stored key.',
          'Test checks the key in the field, or the saved key when the field is empty, against AllDebrid. It saves nothing and changes nothing on AllDebrid; the status reads Verified only once the saved key has passed.',
        ]},
        {heading: 'What your account can do', list: [
          'Per AllDebrid’s documentation, magnets and .torrent files need a premium account. When AllDebrid reports that your account is not premium, or its premium time has ended, DebridPulse stops sending it torrents.',
          'Hoster links work for the hosts AllDebrid lists for your account; per its documentation, a free account can use only the hosts it marks as free.',
        ]},
        {heading: 'Additional Settings', list: [
          'Prepare Backup Torrents (on by default): AllDebrid may also add a torrent as a backup source while another service delivers it. Each backup uses one of AllDebrid’s active magnet slots, and new work takes a slot back when it needs one.',
          'Maximum Active Torrents: left empty, DebridPulse follows AllDebrid’s documented maximum of 30 active magnets. A lower number keeps fewer active; it never goes above 30.',
          'API Calls per Minute (60 by default): how many requests DebridPulse sends to AllDebrid each minute. 0 means no local limit.',
        ]},
        {heading: 'Shared settings', text: [
          'Behavior shared by every premium service, such as Provider Poll Interval, Full Sync Interval, and resolution retries, is under Downloads → Download Behavior & Limits → Advanced Settings; download limits such as Maximum Concurrent Downloads are under Downloads too and are not set per service.',
        ]},
      ],
    },
    debridlink: {
      title: 'Debrid-Link',
      sections: [
        {text: [
          'Debrid-Link is a debrid service: it fetches hoster links, and magnets and .torrent files through your Debrid-Link seedbox, and DebridPulse then downloads the result to your storage. DebridPulse uses your own Debrid-Link account through its API key. Debrid-Link is off until you enable it.',
        ]},
        {heading: 'Connecting', list: [
          'Paste the API key from your Debrid-Link account into API Key. It is saved when you leave the field, is stored on the server, and is never shown again. Remove API Key deletes the stored key.',
          'Test checks the key in the field, or the saved key when the field is empty, against Debrid-Link. It saves nothing and changes nothing on Debrid-Link; the status reads Verified only once the saved key has passed.',
        ]},
        {heading: 'What your account can do', list: [
          'With a premium Debrid-Link account, DebridPulse sends it hoster links, magnets, and .torrent files.',
          'A free account can use hoster links only for the hosts Debrid-Link marks as usable without premium. Debrid-Link does not document seedbox use for free accounts, so DebridPulse sends them no torrents. Other paid account types are given hoster links; what else they include is not documented.',
          'Limits Debrid-Link applies to your account, such as its link, data, or torrent allowances, are respected as Debrid-Link reports them; DebridPulse retries later or uses another service.',
        ]},
        {heading: 'Additional Settings', list: [
          'Prepare Backup Torrents (on by default): Debrid-Link may also add a torrent to your seedbox as a backup source while another service delivers it.',
          'Request Timeout (30 seconds by default), Torrent Upload Timeout (120 seconds), and Supported Host Refresh Interval (24 hours) tune how DebridPulse talks to Debrid-Link. Debrid-Link documents no request-rate limit, so there is no API Calls per Minute setting.',
        ]},
        {heading: 'Shared settings', text: [
          'Behavior shared by every premium service, such as Provider Poll Interval, Full Sync Interval, and resolution retries, is under Downloads → Download Behavior & Limits → Advanced Settings; download limits such as Maximum Concurrent Downloads are under Downloads too and are not set per service.',
        ]},
      ],
    },
    premiumize: {
      title: 'Premiumize',
      sections: [
        {text: [
          'Premiumize is a debrid service: it fetches hoster links, magnets, .torrent files, and NZBs into your Premiumize cloud, and DebridPulse then downloads the result to your storage. DebridPulse uses your own Premiumize account through its API key. Premiumize is off until you enable it.',
        ]},
        {heading: 'Connecting', list: [
          'Paste the API key from your Premiumize account into API Key. It is saved when you leave the field, is stored on the server, and is never shown again. Remove API Key deletes the stored key.',
          'Test checks the key in the field, or the saved key when the field is empty, against Premiumize. It saves nothing and changes nothing on Premiumize; the status reads Verified only once the saved key has passed.',
        ]},
        {heading: 'What your account can do', list: [
          'While your Premiumize premium time is current, DebridPulse may send it hoster links, magnets, .torrent files, and NZBs, as Premiumize documents them for premium accounts.',
          'What a free or expired account can do through Premiumize’s API is not documented, so DebridPulse sends it no new work; the account stays connected.',
          'Premiumize refuses work it cannot take now (its fair-use limit, booster points, or active jobs). DebridPulse treats that as temporary: it retries later or uses another service.',
        ]},
        {heading: 'Additional Settings', list: [
          'Use Premiumize Before Usenet (off by default): tries Premiumize before your Usenet service for NZBs. It needs an active Premiumize premium account; your Usenet service stays available either way.',
          'Prepare Backup Torrents (on by default): Premiumize may also add a torrent to your cloud as a backup source while another service delivers it. A backup uses your account’s resources.',
          'Request Timeout (30 seconds by default), Upload Timeout (120 seconds), and Supported Host Refresh Interval (24 hours) tune how DebridPulse talks to Premiumize. Premiumize documents no request-rate limit, so there is no API Calls per Minute setting.',
        ]},
        {heading: 'Shared settings', text: [
          'Behavior shared by every premium service, such as Provider Poll Interval, Full Sync Interval, and resolution retries, is under Downloads → Download Behavior & Limits → Advanced Settings; download limits such as Maximum Concurrent Downloads are under Downloads too and are not set per service.',
        ]},
      ],
    },
    realdebrid: {
      title: 'Real-Debrid',
      sections: [
        {text: [
          'Real-Debrid is a debrid service: it fetches hoster links, magnets, and .torrent files on its own servers, and DebridPulse then downloads the result to your storage. DebridPulse connects to your own Real-Debrid account through Real-Debrid’s device authorization; there is no key to type. Real-Debrid is off until you enable it.',
        ]},
        {heading: 'Connecting', list: [
          'Connect Real-Debrid shows a code. Enter it on Real-Debrid’s own page in your browser; DebridPulse finishes the connection by itself once you approve it. The authorization is stored on the server.',
          'Test checks the stored authorization against Real-Debrid and saves nothing.',
          'Disconnect forgets the stored authorization at once and asks Real-Debrid to retire it.',
        ]},
        {heading: 'What your account can do', list: [
          'Per Real-Debrid’s documentation, torrents need a premium account. When Real-Debrid reports that your account is free, or its premium time has ended, DebridPulse stops sending it torrents.',
          'A free account may still be able to use some hosters; Real-Debrid decides that per hoster.',
        ]},
        {heading: 'Additional Settings', list: [
          'Prepare Backup Torrents (on by default): Real-Debrid may also add a torrent as a backup source while another service delivers it. Each backup uses one of your account’s active torrent slots, and new work takes a slot back when it needs one.',
          'Maximum Active Torrents: left empty, DebridPulse follows your account’s current Real-Debrid limit. A lower number keeps fewer active; it never goes above your limit.',
          'API Calls per Minute (240 by default): how many requests DebridPulse sends to Real-Debrid each minute; Real-Debrid documents a maximum of 250.',
          'Request Timeout (30 seconds by default), Torrent Upload Timeout (120 seconds), and Supported Host Refresh Interval (24 hours) tune how DebridPulse talks to Real-Debrid.',
        ]},
        {heading: 'Shared settings', text: [
          'Behavior shared by every premium service, such as Provider Poll Interval, Full Sync Interval, and resolution retries, is under Downloads → Download Behavior & Limits → Advanced Settings; download limits such as Maximum Concurrent Downloads are under Downloads too and are not set per service.',
        ]},
      ],
    },
    torbox: {
      title: 'TorBox',
      sections: [
        {text: [
          'TorBox is a debrid service: it fetches torrents and web downloads on its own servers, and DebridPulse then downloads the result to your storage. DebridPulse connects to your own TorBox account through TorBox’s device authorization; there is no key to type. TorBox is off until you enable it.',
        ]},
        {heading: 'Connecting', list: [
          'Connect TorBox shows a code. Enter it on TorBox’s own page in your browser; DebridPulse finishes the connection by itself once you approve it. The token is stored on the server.',
          'Test checks the stored token against TorBox and saves nothing.',
          'Disconnect forgets the stored token at once. TorBox offers no way for DebridPulse to revoke it; you can reset it on TorBox’s own site.',
        ]},
        {heading: 'What your plan can do', list: [
          'DebridPulse follows your TorBox plan as TorBox documents it: Essential and Standard plans for torrents and web downloads.',
          'NZBs only where your plan includes Usenet; per TorBox’s documentation that is the Pro plan. Whether a particular NZB succeeds is TorBox’s decision for your account.',
          'DebridPulse sends no new work to a Free account: TorBox refused the torrent request DebridPulse makes on one. If TorBox refuses a feature as not part of your plan, DebridPulse stops offering that feature to it.',
          'TorBox is separate from your Usenet service: turning TorBox on or off never changes your news servers.',
        ]},
        {heading: 'Additional Settings', list: [
          'Prepare Backup Torrents (off by default, unlike the other debrid services, because TorBox’s create limits and active slots are tighter): when on, TorBox may also add a torrent as a backup source while another service delivers it, and new work takes a slot back when it needs one.',
          'Maximum Active Torrents: left empty, DebridPulse follows your plan’s active-slot count as TorBox documents it. A lower number keeps fewer active.',
          'Use TorBox Before Usenet (off by default): tries TorBox before your Usenet service for NZBs. It needs a plan that includes Usenet; your Usenet service stays available either way.',
          'API Calls per Minute (240 by default): TorBox documents a maximum of 300. Request Timeout (30 seconds by default), Upload Timeout (120 seconds), and Supported Host Refresh Interval (24 hours) tune how DebridPulse talks to TorBox.',
        ]},
        {heading: 'Shared settings', text: [
          'Behavior shared by every premium service, such as Provider Poll Interval, Full Sync Interval, and resolution retries, is under Downloads → Download Behavior & Limits → Advanced Settings; download limits such as Maximum Concurrent Downloads are under Downloads too and are not set per service.',
        ]},
      ],
    },
    'network-sources': {
      title: 'Network Sources',
      sections: [
        {text: [
          'Network Sources are the protocols DebridPulse can download from directly, without a premium account: for example HTTP and HTTPS, FTP and SFTP, SCP, rsync, WebDAV, Metalink (Multimeta), and supported media pages.',
        ]},
        {heading: 'Enable', list: [
          'The Enable switch in this card’s header turns the whole group on or off. A source takes part only while both the group and the source itself are enabled.',
          'Turning the group off does not change any source’s own setting, so turning it back on restores each source exactly as it was.',
        ]},
        {heading: 'Related', text: [
          'How each protocol transfers files, such as timeouts, folder depth, and file limits, is set under Downloads, Transfer Method Settings.',
        ]},
      ],
    },

    /* ── Downloads ────────────────────────────────────────────────────── */
    'download-behavior': {
      title: 'Download Behavior & Limits',
      sections: [
        {text: [
          'These controls apply to every download, whichever service or protocol it comes from.',
        ]},
        {heading: 'Principal controls', list: [
          'Download Folder is where completed downloads are saved on the server.',
          'Maximum Concurrent Downloads is one limit for all download engines together, not a limit per engine. Further downloads wait their turn.',
        ]},
        {heading: 'Advanced Settings', list: [
          'Material Checkpoint Interval sets how often reusable download progress is recorded, so less work is repeated after an unexpected interruption.',
          'Graceful Stop Timeout is how long DebridPulse waits for a download engine to stop cleanly before forcing it.',
          'Local Network Connections allows downloads you enter explicitly to reach private LAN addresses. Skip Local Connection Confirmation can be used only while that is on; it stops DebridPulse asking each time.',
          'Provider Poll Interval is how often DebridPulse checks a service for progress on work it is preparing, such as a torrent or cloud copy. Full Sync Interval is how often it reconciles its downloads with every enabled premium service account that can list its transfers.',
          'Resolution Failure Retries and Retry Delay set how often, and how soon, DebridPulse tries again when a service fails to resolve a download, for any service including Network Sources, before trying another service or giving up.',
        ]},
      ],
    },
    'transfer-methods': {
      title: 'Transfer Method Settings',
      sections: [
        {text: [
          'Each card in this group tunes one way DebridPulse transfers files. A setting here affects only downloads that use that method.',
        ]},
        {heading: 'Methods', list: [
          'Network Sources: the direct download engine’s connections per server, segments per file, slow-connection limit, disk cache, and file allocation.',
          'rsync: compression, modification times, timeouts, and how many folder levels below an added folder are searched.',
          'WebDAV: folder depth, the maximum number of files accepted from one folder (a folder with more is rejected whole, never added in part), and the total time allowed to scan a folder.',
          'Usenet: article cache, retries, and request timeout shared by every news server. Each server’s own settings are on its card under Services.',
          'Media Downloads: target resolution, quality, codec, subtitle language, parallel fragment downloads, and YouTube player clients for supported media pages.',
        ]},
        {heading: 'Related', text: [
          'Whether a method is used at all is decided under Services; how many downloads run at once is set in Download Behavior & Limits.',
        ]},
      ],
    },
    'disk-recovery': {
      title: 'Disk Space & Recovery',
      sections: [
        {heading: 'Disk space guard', list: [
          'When free space falls to Minimum Free Disk Space or below, new downloads are held back; downloads already running are allowed to finish. Set it to 0 to turn the guard off.',
          'New downloads start again only once free space is back above the minimum plus the Resume Free Space Buffer, so they do not stop and start repeatedly near the limit.',
        ]},
        {heading: 'Recovery', list: [
          'Download Error Retries and Retry Delay set how often, and how soon, a download is retried after an error. A plain retry waits Retry Delay, doubling after each failure but never more than 5 minutes; checking or re-resolving a download, and trying a provider again after a temporary failure, wait the full Retry Delay. A provider that asks for a longer wait is always given it. 0 retries turns automatic retries off.',
          'Stalled Download Timeout is how long a download may make no progress before DebridPulse stops it and treats it as failed, so its normal retry rules apply. 0 turns this off.',
        ]},
      ],
    },
    'engine-activity': {
      title: 'Download Engine Activity',
      sections: [
        {text: [
          'A live list of the jobs the download engines are running or holding. While it is on screen it updates by itself as those jobs start, progress, pause or end. It is a troubleshooting surface; Downloads is where downloads are normally managed.',
        ]},
        {heading: 'Controls', list: [
          'Filters show active, waiting, paused, or stopped jobs.',
          'Pause and Resume act on one engine job only when the engine offers them for it.',
          'Terminate stops a job and releases its execution after you confirm. DebridPulse keeps the transfer and may start it again under its normal recovery rules.',
        ]},
      ],
    },

    /* ── Downloads -> Transfer Method Settings: one topic per method card.
     * Defaults and limits are the backend's own option models
     * (executors/aria2/definition.py Aria2Options, executors/rsync/definition.py
     * RsyncOptions, providers/general_rsync|general_webdav/definition.py,
     * integrations/usenet/definition.py UsenetOptions,
     * integrations/media/definition.py MediaOptions, core/config.py). */
    'tuning-direct': {
      title: 'Network Sources Tuning',
      sections: [
        {text: [
          'These settings tune the direct download engine. It downloads HTTP, HTTPS, FTP and SFTP links: direct links you add, and the download links a premium service resolves for you. rsync, Usenet and Media Downloads use their own engines, so these settings do not affect them.',
        ]},
        {heading: 'Connections and segments', list: [
          'Connections per Server is how many connections one download may open to the same server, from 1 to 16 (default 16).',
          'Segments per File is how many parts of one file may download in parallel, from 1 to 64 (default 16). The server, Connections per Server and Minimum Split Size can allow fewer.',
          'Minimum Split Size is the smallest part a file is split into, from 1M to 1024M (default 10M); a larger value gives fewer parallel parts.',
        ]},
        {heading: 'Interrupted and slow downloads', list: [
          'Continue Partial Downloads (on by default) resumes a partly downloaded file where possible instead of starting it again.',
          'Lowest Speed Limit stops a connection whose speed falls to this value or below, in bytes per second with an optional K or M suffix. 0 (the default) turns the limit off.',
        ]},
        {heading: 'Disk', list: [
          'Disk Cache is memory the engine may use as a shared cache to reduce disk writes (default 64M; 0 turns it off). A change takes effect the next time the download engine starts.',
          'File Allocation is how disk space is prepared for a new file: Truncate, Fallocate (the default), Preallocate or None.',
        ]},
        {heading: 'Related', text: [
          'Which network protocols DebridPulse may use at all is chosen under Services, Network Sources; these settings tune the engine those downloads share. How many downloads run at once is Maximum Concurrent Downloads in Download Behavior & Limits.',
        ]},
      ],
    },
    'tuning-rsync': {
      title: 'rsync Tuning',
      sections: [
        {text: [
          'These settings apply only to downloads from rsync sources, whether reached through an rsync server or over SSH.',
        ]},
        {heading: 'Transfers', list: [
          'Partial Transfers (on by default) continues an interrupted file from what was already downloaded. Off, an interrupted file starts again from the beginning.',
          'Compression (off by default) compresses data in transit. It rarely helps with archives, video or other already-compressed files.',
          'Preserve Modification Time (on by default) gives each completed file the modification time it has on the source.',
        ]},
        {heading: 'Timeouts', list: [
          'Connection Timeout is how long reaching the source and starting a transfer may take, including an SSH connection and sign-in: 5 to 300 seconds (default 30).',
          'Transfer Timeout is how long a running transfer may receive no data before it stops: 30 to 3600 seconds (default 300). It is separate from the Stalled Download Timeout under Disk Space & Recovery.',
        ]},
        {heading: 'Directory Depth', list: [
          'How many folder levels below an rsync folder you add are searched for files: the current folder only, 1, 2 or 3 levels, or all subfolders (the default). Files in deeper folders are not included.',
        ]},
        {heading: 'Related', text: [
          'Whether rsync sources are used at all is chosen under Services, Network Sources.',
        ]},
      ],
    },
    'tuning-webdav': {
      title: 'WebDAV Tuning',
      sections: [
        {text: [
          'These settings decide which files DebridPulse collects when you add a WebDAV folder. Every file collected is offered in the usual file selection and downloads as an ordinary file.',
        ]},
        {heading: 'Collecting a folder', list: [
          'Directory Depth is how many folder levels below the folder you add are searched: the current folder only (the default), 1, 2 or 3 levels, or all subfolders. Files in deeper folders are not included.',
          'Maximum Files is the most files one folder may contain, from 1 to 10000 (default 10000). A folder with more files is rejected whole; it is never added in part.',
          'Collection Scan Timeout is how long listing the folder and its subfolders may take in total before the folder is reported unavailable: empty for no limit (the default), or 10 to 3600 seconds. Each listing request keeps its own time limit either way. It applies to collecting the folder only, not to downloading its files.',
        ]},
        {heading: 'Related', text: [
          'Whether WebDAV sources are used at all is chosen under Services, Network Sources.',
        ]},
      ],
    },
    'tuning-usenet': {
      title: 'Usenet Tuning',
      sections: [
        {text: [
          'These settings apply to every Usenet download, whichever news server provides it. Each server’s host, credentials, connections, priority, Server Timeout and Test are on its own card under Services, Usenet.',
        ]},
        {heading: 'Settings', list: [
          'Article Cache Limit is memory DebridPulse may use to hold downloaded article data before writing it to disk, from 0 to 4096 MB (default 1024). 0 turns the cache off.',
          'Maximum Retries is how many times one article is retried on a news server before that server is given up for it, from 2 to 25 (default 3). It is not the download retry count under Disk Space & Recovery.',
          'Request Timeout is how long DebridPulse waits for its own Usenet download service to answer a request, from 5 to 300 seconds (default 30). It is not a news-server timeout.',
          'Direct Write (on by default) writes article data straight to the destination file instead of holding it in memory first, which reduces disk work when articles arrive in order.',
        ]},
      ],
    },
    'tuning-media': {
      title: 'Media Downloads Tuning',
      sections: [
        {text: [
          'These settings apply to Media Downloads: media from supported web pages, downloaded in a form the site itself offers. DebridPulse never re-encodes or transcodes media. The downloaded streams and the chosen subtitle are combined, unchanged, into one file: in the site’s own container when it can hold them all, otherwise in Matroska (MKV). There is no setting for the output container.',
        ]},
        {heading: 'Choosing what is downloaded', list: [
          'Target Resolution is the resolution DebridPulse looks for. When the site does not offer it, the largest lower resolution is used, or the smallest higher one when there is no lower one. Best Available (the default) takes the highest resolution offered.',
          'Video Quality chooses, within the preferred codec, among the bitrates the site offers for the video at that resolution: High (the default) the highest, Low the lowest, Normal one in between. It cannot make a video better than the site offers, and where there is nothing to choose between, the site’s usual choice is kept.',
          'Preferred Video Codec (Auto, AV1, HEVC or H.264) prefers that codec when the site offers it at that resolution; Auto (the default) keeps the site’s usual choice. It is a preference among what the site offers, never a conversion.',
          'Preferred Subtitle Language is the language of the one subtitle embedded in the file, when the site offers one (default English). A subtitle written for the media is preferred; one the site generated automatically in that language is used otherwise. A subtitle that cannot be embedded without converting it makes the download fail rather than being dropped or converted.',
        ]},
        {heading: 'Downloading', list: [
          'Concurrent Fragment Downloads is how many fragments of one segmented stream download at once, from 1 to 8 (default 1). A download that is a single file is unaffected, and it is not the number of downloads that run at once.',
          'YouTube Player Client is Automatic (recommended): the installed yt-dlp’s own default. Choose one of the clients that yt-dlp offers only when troubleshooting YouTube downloads; no choice guarantees that a particular video can be downloaded. A saved client the installed yt-dlp no longer offers is kept, and Automatic is used until you choose again.',
        ]},
        {heading: 'Related', text: [
          'Whether Media Downloads are used at all is chosen under Services, Network Sources.',
        ]},
      ],
    },

    /* ── Extraction ───────────────────────────────────────────────────── */
    'automatic-extraction': {
      title: 'Automatic Extraction',
      sections: [
        {text: [
          'When Enable is on, DebridPulse unpacks supported archives once their download completes: ZIP, TAR (including gzip, bzip2, xz, Zstandard, and LZMA variants), GZ, BZ2, XZ, 7z, and RAR, including multi-part RAR sets. Other files are left as they are.',
        ]},
        {heading: 'Principal controls', list: [
          'Delete Archives After Extraction removes an archive’s files only after it was extracted successfully; a failed extraction keeps them.',
          'Concurrent Extractions limits how many archives are unpacked at the same time.',
          'Archive Passwords: each 7z or RAR archive is tried first without a password, then with each stored password in order. ZIP archives are extracted without a password only; a password-protected ZIP is not unlocked with these passwords. Passwords are stored on the server and hidden unless you choose to show them.',
        ]},
        {heading: 'Safety', text: [
          'Archives are checked before anything is written: entries with absolute paths or paths that climb out of the extraction folder, links, and archives that would expand beyond safe size or file-count limits are refused.',
        ]},
      ],
    },

    /* ── Authentication ───────────────────────────────────────────────── */
    'auth-status': {
      title: 'Authentication Status',
      sections: [
        {text: [
          'A summary of how people sign in: which sign-in methods are enabled, whether each is ready, and how many browser sessions are currently valid.',
        ]},
        {heading: 'Browser sessions', list: [
          'Browser Session Lifetime: each sign-in, by password or OpenID Connect, starts a new browser session that lasts this many hours. Using DebridPulse does not extend it; when it ends, you sign in again.',
          'Active Browser Sessions counts the browser sign-ins that are currently valid.',
          'Log Out Current Session ends only this browser’s session.',
          'Sessions concern the browser interface only. Downloads continue whether or not anyone is signed in.',
        ]},
        {heading: 'Related', text: [
          'Integration accounts never use browser sessions; they authenticate with API tokens under Users & Access.',
        ]},
      ],
    },
    'username-password': {
      title: 'Username & Password',
      sections: [
        {text: [
          'Local sign-in with a username and password, for the browser and for HTTP Basic API access.',
        ]},
        {heading: 'Principal controls', list: [
          'Enable turns local password sign-in on or off for the whole instance. It can be turned on only when an active Admin has a username and password.',
          'Username and Password are your own account’s credential. Typing in them changes nothing until you choose Apply, which saves both together or, if anything is refused, changes nothing.',
          'The stored password is never shown. Leave Password blank to keep it; enter a new one to replace it. Changing the username needs a new password with it, and a first credential needs both.',
          'Applying a new credential ends your account’s browser sessions, including this one; sign in again with the new username and password.',
          'Clear Stored Password, inside the Password field, removes your stored password after you confirm. It never applies what you have typed.',
          'The status beside Help says Configured when a username and password are stored, and Unconfigured otherwise. It does not test the password.',
        ]},
        {heading: 'Related', text: [
          'Other people’s local accounts and passwords are managed under Users & Access, User Accounts.',
        ]},
      ],
    },
    oidc: {
      title: 'OpenID Connect',
      sections: [
        {text: [
          'Lets people sign in to the browser interface through your own identity provider. The provider proves who someone is; the Access Control rules here decide whether that person may use DebridPulse.',
        ]},
        {heading: 'Connection', list: [
          'Public DebridPulse Base URL must be the HTTPS address people use to reach DebridPulse; the OIDC Callback URL is derived from it. Copy that callback into your identity provider exactly. When the PUBLIC_BASE_URL environment variable is set, it is used instead and cannot be changed here.',
          'Issuer URL, Client ID, and Client Secret come from your identity provider. The stored secret is never shown again.',
          'Scopes are requested at every sign-in; openid is always included.',
          'Test signs in through your identity provider in a separate window to prove the saved configuration works.',
        ]},
        {heading: 'Access Control', list: [
          'With Allow Any Authenticated OIDC Identity on, anyone your identity provider signs in may use DebridPulse, and the lists are ignored.',
          'With it off, a user must match every list that is filled in: a listed subject (written as issuer|subject-id), a listed email address that the provider reports as verified (email_verified=true), and membership of a listed group. Empty lists are skipped. If every list is empty, no one can sign in through OpenID Connect.',
          'Email addresses match regardless of letter case. Subjects and group names must match exactly. Groups are read from the Group Claim; a user without that claim does not match a group list.',
        ]},
        {heading: 'Accounts', list: [
          'Someone admitted for the first time gets a new account with the Default Role for New OIDC Users: User or ReadOnly User. OpenID Connect never creates an Admin; change roles under Users & Access.',
          'An identity is matched by its issuer and subject, never by email, so it is never linked to an existing account automatically.',
        ]},
      ],
    },

    /* ── Users & Access ───────────────────────────────────────────────── */
    'user-accounts': {
      title: 'User Accounts',
      sections: [
        {text: [
          'The people who sign in to DebridPulse. Each account has one role, and signs in with a local username and password, through OpenID Connect, or both.',
        ]},
        {heading: 'Roles', list: [
          'Admin: full control of DebridPulse, including Settings, accounts, maintenance, the Activity Log, and every transfer.',
          'User: adds and operates downloads. Deleting, cancelling, or redirecting a transfer is allowed only when no other account still needs it, including transfers combined with a duplicate request from someone else; otherwise the User can withdraw only their own request.',
          'ReadOnly User: views downloads and Statistics, but cannot add, change, or remove anything.',
          'Integration: an external application, not a person. It cannot sign in to this interface and uses API tokens instead (see Integration Accounts).',
        ]},
        {table: ROLE_TABLE},
        {heading: 'Account actions', list: [
          'Add Local Account creates a person with a username and password. Change Role takes effect on the account’s next request; its open browsers reload with the new role.',
          'Disabling an account ends its sessions and withdraws its outstanding download requests. Removing it does the same and deletes its own notification settings.',
          'Merge combines accounts that belong to the same person into one. Accounts that share an email address are only suggested; nothing is merged automatically.',
          'DebridPulse always keeps at least one usable Admin, and you cannot change your own role or disable your own account here.',
        ]},
      ],
    },
    'integration-accounts': {
      title: 'Integration Accounts',
      sections: [
        {text: [
          'An Integration account represents an external application, such as Sonarr or Radarr, not a person. It cannot sign in to this interface; it authenticates with API tokens. It can add downloads through the API, and through the external client API and the download-client interfaces that applications such as Sonarr and Radarr connect to, it submits and follows its own jobs.',
        ]},
        {heading: 'Tokens', list: [
          'Issue Token creates a new token. Its secret is shown once, when it is issued; copy it into the application then. Only a fingerprint is stored, so it cannot be shown again.',
          'Each token is its own client: it sees only the jobs submitted with it.',
          'Revoke stops a token from authenticating at once. It does not withdraw any job.',
          'Only Integration accounts hold API tokens; Admin and User accounts do not.',
        ]},
        {heading: 'What an integration can do', list: [
          'Submit jobs into a Delivery Profile category it is allowed to use, follow them, and withdraw them.',
          'Ask to pause or resume its own jobs. DebridPulse may decline, for example when another account also needs the transfer or processing is paused globally.',
          'It cannot pause, delete, or redirect transfers directly, change Settings, or see other clients’ jobs.',
        ]},
        {heading: 'Disable and Delete', list: [
          'Disabling the account stops all of its tokens from working and withdraws its outstanding requests.',
          'Deleting it also revokes its tokens permanently.',
        ]},
      ],
    },
    'delivery-profiles': {
      title: 'Integration Delivery Profiles',
      sections: [
        {text: [
          'A Delivery Profile tells an external application, such as Sonarr or Radarr, which categories it may submit to and where it will find the completed files. It never changes where DebridPulse downloads, which service is used, or queue order.',
        ]},
        {heading: 'What a profile sets', list: [
          'Categories: the names an application chooses when it submits a job. Each category belongs to one profile only.',
          'Authorized API Tokens: the Integration tokens allowed to use the profile. A profile with no authorized token admits no one.',
          'Source Types: which kinds of submission are accepted, such as links, magnets, torrents, or NZBs.',
          'DP Download Path is the folder in DebridPulse, inside the download folder; External Import Path is the same folder as the application sees it, if its path differs.',
          'Retention is how long a completed delivery is offered to the application.',
        ]},
        {heading: 'Jobs and files', list: [
          'Submitting a job makes a download request that belongs to that application. Removing the job in the application withdraws that request, or acknowledges the delivery once it is complete; the transfer itself stops only if no one else needs it.',
          'Completed files are shared in place: nothing is copied, linked, or moved for the application, and acknowledging or expiring a delivery deletes no file.',
          'The download-client interfaces that these applications connect to stay unavailable until a profile has a category, accepts a Source Type that interface uses, and authorizes an API token that is active.',
        ]},
      ],
    },

    /* ── Notifications (global) ───────────────────────────────────────── */
    discord: {
      title: 'Discord Notifications',
      sections: [
        {text: [
          'Instance-wide Discord alerts about download events. They describe every download, whoever added it, and go only to the webhooks configured here.',
        ]},
        {heading: 'Principal controls', list: [
          'Enable turns these alerts on or off; it does not affect statistics reports.',
          'The Discord Webhook receives every alert. The optional Download Added Webhook receives only Download Added alerts; without it, those go to the Discord Webhook too.',
          'Display Name and Avatar URL set how the sender appears in Discord.',
          'Notification Events choose which alerts are sent, including Update Available and how often DebridPulse checks for a new release.',
          'Test sends a test message to the saved webhooks.',
        ]},
        {heading: 'Related', text: [
          'Each User can also set up their own notifications about their own downloads; those are separate and never receive these alerts. Statistics Reporting sends periodic summaries rather than event alerts.',
        ]},
      ],
    },
    'statistics-reporting': {
      title: 'Statistics Reporting',
      sections: [
        {text: [
          'An outbound summary of this DebridPulse instance’s activity, sent to a Discord webhook. It is not a Statistics Snapshot; snapshots are kept internally under Data & Maintenance and are never sent.',
        ]},
        {heading: 'Who receives it', list: [
          'Only the configured destination: the Reporting Webhook, or the Discord Webhook from Discord Notifications when no Reporting Webhook is set.',
          'It is not sent to DebridPulse users. Users who set up their own reports receive only reports about their own activity, at their own destination.',
        ]},
        {heading: 'What and when', list: [
          'Report Window chooses how much recent activity each report summarizes: 24 hours, 7 days, 30 days, or 1 year.',
          'Automatic Report Interval sends a report that often while Enable is on and a destination exists. 0 sends no automatic reports.',
          'Test sends one report now, even while automatic reports are off; it fails if no destination is configured.',
          'With no destination, nothing is sent and no error is shown for automatic reports.',
        ]},
      ],
    },

    /* ── Notifications (a User's own) ─────────────────────────────────── */
    'discord-own': {
      title: 'Discord Notifications',
      sections: [
        {text: [
          'Your own Discord alerts, about downloads you requested. They go only to the webhooks you configure here, and nothing here changes the instance-wide notifications an Admin manages.',
        ]},
        {heading: 'Principal controls', list: [
          'Enable turns your alerts on or off.',
          'Your Discord Webhook receives your alerts; an optional Download Added Webhook receives only Download Added alerts. Nothing falls back to an Admin’s webhook.',
          'Choose which events you are told about: Download Added, Download Completed, Download Error, and Extraction Result.',
          'An alert is sent only while your own request is part of that download; withdrawn requests send nothing.',
        ]},
      ],
    },
    'statistics-reporting-own': {
      title: 'Statistics Reporting',
      sections: [
        {text: [
          'Your own statistics report: a summary of the download activity attributed to you, sent to a Discord webhook you choose. It never includes other accounts’ activity.',
        ]},
        {heading: 'Who receives it', list: [
          'Only your destination: your Reporting Webhook, or your own Discord Webhook when no Reporting Webhook is set. An Admin’s destinations are never used.',
        ]},
        {heading: 'What and when', list: [
          'Report Window chooses how much recent activity each report covers.',
          'Automatic Report Interval sends a report that often while Enable is on and a destination exists. 0 sends no automatic reports.',
          'Test sends one report now.',
        ]},
      ],
    },

    /* ── Data & Maintenance ───────────────────────────────────────────── */
    'event-logging': {
      title: 'Event Logging',
      sections: [
        {text: [
          'DebridPulse records an event for what happens to downloads and to its configuration, shown in the Activity Log.',
        ]},
        {heading: 'Principal controls', list: [
          'Activity Log Page Size sets how many events one Activity Log page shows. It changes nothing that is recorded.',
          'Recorded Events is the number of events currently kept. Events are kept indefinitely; only a Database Reset clears them.',
        ]},
      ],
    },
    backups: {
      title: 'Backups & Retention',
      sections: [
        {heading: 'Backups', list: [
          'A backup holds the database, the configuration, and an uploaded avatar, stored in the Backup Folder.',
          'While Enable is on, a backup is made every Backup Interval, and Run Backup makes one now. Backups lists the stored backups, and lets you add, save, restore, or remove one.',
          'A backup is deleted once it is older than Backup Retention or is no longer among the newest Maximum Retained Backups, whichever comes first.',
          'Restoring and resetting the database each make a safety backup first.',
        ]},
        {heading: 'Statistics Snapshots', list: [
          'Separately, DebridPulse saves a record of the previous 24 hours of activity every Statistics Snapshot Interval, inside its own database, and deletes snapshots older than Statistics Snapshot Retention. 0 stops new snapshots.',
          'Snapshots are internal: they are not reports, are not sent anywhere, and are not currently viewable in the interface.',
        ]},
      ],
    },
    'database-reset': {
      title: 'Database Reset Controls',
      sections: [
        {text: [
          'Erases DebridPulse’s download history so it starts empty. Use it only when you mean to start over.',
        ]},
        {heading: 'What is removed and kept', list: [
          'Removed: transfers and their files’ records, download requests, external jobs, events and the Activity Log, and statistics snapshots.',
          'Kept: accounts, passwords, OpenID Connect links, API tokens, personal notification settings, and every setting. Downloaded files on disk are not deleted.',
        ]},
        {heading: 'Safeguards', list: [
          'Reset Database works only while Allow Database Reset is on, and asks you to confirm.',
          'Processing pauses for the reset and returns to its previous state afterwards. A safety backup is made first; if it fails, nothing is erased.',
        ]},
      ],
    },
  });

  function paragraph(text, className) {
    const element = document.createElement('p');
    element.className = className;
    element.textContent = text;
    return element;
  }

  function table(spec) {
    const wrapper = document.createElement('div');
    wrapper.className = 'dp-settings-help-table-scroll';
    wrapper.tabIndex = 0;
    wrapper.setAttribute('role', 'region');
    wrapper.setAttribute('aria-label', spec.caption);
    const grid = document.createElement('table');
    grid.className = 'dp-settings-help-table';
    const caption = grid.createCaption();
    caption.textContent = spec.caption;
    const headRow = grid.createTHead().insertRow();
    spec.head.forEach(label => {
      const cell = document.createElement('th');
      cell.scope = 'col';
      cell.textContent = label;
      headRow.appendChild(cell);
    });
    const body = grid.createTBody();
    spec.rows.forEach(([label, ...values]) => {
      const row = body.insertRow();
      const header = document.createElement('th');
      header.scope = 'row';
      header.textContent = label;
      row.appendChild(header);
      values.forEach(value => {
        const cell = row.insertCell();
        cell.textContent = value;
        if (value === YES || value === NO) cell.dataset.answer = value.toLowerCase();
      });
    });
    wrapper.appendChild(grid);
    return wrapper;
  }

  function render(body, topic) {
    const content = document.createElement('div');
    content.className = 'dp-settings-help-content';
    topic.sections.forEach(section => {
      const block = document.createElement('section');
      block.className = 'dp-settings-help-section';
      if (section.heading) {
        const heading = document.createElement('h3');
        heading.className = 'dp-settings-help-heading';
        heading.textContent = section.heading;
        block.appendChild(heading);
      }
      (section.text || []).forEach(text => block.appendChild(paragraph(text, 'dp-settings-help-text')));
      if (section.list) {
        const list = document.createElement('ul');
        list.className = 'dp-settings-help-list';
        section.list.forEach(text => {
          const item = document.createElement('li');
          item.textContent = text;
          list.appendChild(item);
        });
        block.appendChild(list);
      }
      if (section.table) block.appendChild(table(section.table));
      content.appendChild(block);
    });
    body.appendChild(content);
    return content.querySelector('.dp-settings-help-text, .dp-settings-help-list');
  }

  function open(name) {
    const topic = Object.prototype.hasOwnProperty.call(TOPICS, name) ? TOPICS[name] : null;
    if (!topic || !window.DPSettingsModal) return null;
    return window.DPSettingsModal.open({
      title: topic.title,
      dismiss: true,
      closeControl: true,
      className: 'dp-settings-help-dialog',
      mount: body => render(body, topic),
    });
  }

  window.DPSettingsHelp = Object.freeze({open, topics: Object.freeze(Object.keys(TOPICS))});
})();
