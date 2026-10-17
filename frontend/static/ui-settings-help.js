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
        ]},
        {heading: 'On each card', list: [
          'Enable decides whether the service takes part in new downloads. Turning it off keeps its saved credentials and settings.',
          'The credentials are stored on the server and are never shown again after saving; leave a secret field blank to keep the stored value.',
          'Test checks the saved configuration against the service. The status beside it reports Unconfigured (enabled but nothing usable is saved), Unverified (saved but not yet proven by a test), or Verified (the current saved configuration passed a test).',
          'Additional Settings hold that service’s own limits and options; they apply to that service only.',
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
          'TorBox and Premiumize can also take NZBs. By default an NZB goes to Usenet first; turning on Use TorBox Before Usenet or Use Premiumize Before Usenet in that service’s Additional Settings puts it ahead of Usenet for NZBs.',
          'If the first service in that order cannot resolve the NZB, the next one is tried.',
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
          'Media Downloads: target resolution, quality, codec, subtitle language, and parallel fragment downloads for supported media pages.',
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
          'Download Error Retries and Retry Delay set how often, and how soon, a download is retried after an error. 0 retries turns automatic retries off.',
          'Stalled Download Timeout is how long a download may make no progress before DebridPulse stops it and treats it as failed, so its normal retry rules apply. 0 turns this off.',
        ]},
      ],
    },
    'engine-activity': {
      title: 'Download Engine Activity',
      sections: [
        {text: [
          'A live list of the jobs the download engines are running or holding, refreshed every few seconds while this page is visible. It is a troubleshooting surface; Downloads is where downloads are normally managed.',
        ]},
        {heading: 'Controls', list: [
          'Filters show active, waiting, paused, or stopped jobs.',
          'Pause and Resume act on one engine job only when the engine offers them for it.',
          'Terminate stops a job and releases its execution after you confirm. DebridPulse keeps the transfer and may start it again under its normal recovery rules.',
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
          'Concurrent Extractions limits how many archives are unpacked at the same time.',
          'Delete Archives After Extraction removes an archive’s files only after it was extracted successfully; a failed extraction keeps them.',
          'Archive Passwords: each archive is tried first without a password, then with each stored password in order. Passwords are stored on the server and hidden unless you choose to show them.',
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
          'Each sign-in, by password or OpenID Connect, starts a new browser session that lasts for the Browser Session Lifetime. Using DebridPulse does not extend it; when it ends, you sign in again.',
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
          'Username and Password are your own account’s credential. The stored password is never shown; leave the field blank to keep it, or enter a new one to replace it.',
          'Clear Stored Password removes your stored password after you confirm.',
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
          'Clients: the Integration tokens allowed to use the profile. A profile with no clients admits no one.',
          'Source kinds: which kinds of submission are accepted, such as links, magnets, torrents, or NZBs.',
          'Local Root is the folder in DebridPulse, inside the download folder; Import Root is the same folder as the application sees it, if its path differs.',
          'Retention is how long a completed delivery is offered to the application.',
        ]},
        {heading: 'Jobs and files', list: [
          'Submitting a job makes a download request that belongs to that application. Removing the job in the application withdraws that request, or acknowledges the delivery once it is complete; the transfer itself stops only if no one else needs it.',
          'Completed files are shared in place: nothing is copied, linked, or moved for the application, and acknowledging or expiring a delivery deletes no file.',
          'The download-client interfaces that these applications connect to stay unavailable until a profile has a category, accepts a source kind that interface uses, and lists a client with an active token.',
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
          'Send Report sends one now, even while automatic reports are off; it fails if no destination is configured.',
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
          'Send Report sends one now.',
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
