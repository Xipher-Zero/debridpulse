/* DebridPulse canonical Help & Documentation page.
 *
 * Help deliberately does not consume the inherited Help tab markup or legacy
 * tab lifecycle. This runtime owns the current user-facing documentation,
 * master-card/tab composition, and performs no API or backend work.
 */
(function () {
  'use strict';

  const TABS = Object.freeze([
    ['quickstart', 'Quick Start', 'rocket'],
    ['using', 'Everyday Use', 'activity'],
    ['howitworks', 'How It Works', 'workflow'],
    ['services', 'Services', 'plug'],
    ['engines', 'Engines', 'download'],
    ['settings', 'Settings', 'settings'],
    ['trouble', 'Troubleshooting', 'wrench'],
    ['license', 'License', 'scale'],
  ]);

  const state = { activeTab: 'quickstart' };
  const root = () => document.getElementById('view-help');

  function step(number, title, copy) {
    return `
      <article class="dp-help-step">
        <div class="dp-help-step-number" aria-hidden="true">${number}</div>
        <div class="dp-help-step-copy">
          <div class="dp-help-step-title">${title}</div>
          <div class="dp-help-copy">${copy}</div>
        </div>
      </article>`;
  }

  function pipeline(label, copy, tone = '') {
    return `
      <div class="dp-help-pipeline-row${tone ? ` is-${tone}` : ''}">
        <div class="dp-help-pipeline-node">${label}</div>
        <div class="dp-help-pipeline-arrow" aria-hidden="true">→</div>
        <div class="dp-help-copy">${copy}</div>
      </div>`;
  }

  function inset(title, copy) {
    return `
      <article class="dp-help-inset">
        <h3>${title}</h3>
        <div class="dp-help-copy dp-help-prose">${copy}</div>
      </article>`;
  }

  function accordion(summary, copy, open = false) {
    return `
      <details class="dp-help-accordion"${open ? ' open' : ''}>
        <summary>${summary}</summary>
        <div class="dp-help-accordion-body dp-help-copy dp-help-prose">${copy}</div>
      </details>`;
  }

  function quickStartPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-quickstart-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-quickstart-heading">Getting started with DebridPulse</h2>
          <p>Four short steps from a fresh installation to your first finished download.</p>
        </div>

        <div class="dp-help-copy dp-help-copy--lead dp-help-prose">
          <p><b>DebridPulse is the application you use to submit, route, track, and manage downloads.</b> You give it a link, a magnet, or a file such as a <code>.torrent</code>, <code>.nzb</code> or Metalink file. DebridPulse works out which of your services can fetch it, downloads the files to your storage with the right download engine, and keeps track of every download until it finishes.</p>
          <p>You do not need a paid account to start. Ordinary web (HTTP and HTTPS), FTP, SFTP, SCP, rsync and WebDAV links, Metalink files and supported media pages work without one. Torrents, magnets, file-hoster links and NZBs need a service you already have an account with, such as a debrid service (a paid online service that fetches torrents and file-hoster downloads for you) or a Usenet news server.</p>
        </div>

        ${inset('Before you begin', `
          <ul>
            <li><b>Storage:</b> DebridPulse needs a folder it is allowed to write to. This can be a folder on the computer running DebridPulse or a mounted network share.</li>
            <li><b>Login protection:</b> If people or devices you do not fully trust can reach DebridPulse, open <b>Settings → Authentication</b> first and set up <b>Username &amp; Password</b>. A new installation does not ask anyone to sign in.</li>
          </ul>`)}

        <div class="dp-help-steps">
          ${step(1, 'Check where downloads are saved', `
            <div class="dp-help-prose">
              <p>Open <b>Settings → Downloads → Download Behavior &amp; Limits</b> and look at <b>Download Folder</b>. The usual value is <code>/download</code>. <b>Browse</b> lets you pick a folder and shows how much free space it has.</p>
              <p>If DebridPulse runs in Docker, it lives in an isolated environment called a <b>container</b>, and <code>/download</code> is the folder as DebridPulse sees it inside that container. Your installation connects it to a real folder on your computer, NAS or server. If files already arrive where you expect, leave this setting alone.</p>
            </div>`)}

          ${step(2, 'Choose the services you want to use', `
            <div class="dp-help-prose">
              <p>Open <b>Settings → Services</b>. The <b>Network Sources</b> group (HTTP(S), (S)FTP, SCP, rsync, WebDAV, Metalink (Multimeta) and Media Downloads) is switched on in a new installation, so ordinary links work straight away.</p>
              <p>To use a premium service, open its card under <b>Premium Services</b>, switch it on with <b>Enable</b>, and connect your account:</p>
              <ul>
                <li><b>AllDebrid, Debrid-Link and Premiumize:</b> paste the API key from your account into <b>API Key</b>. An API key is a private code that lets DebridPulse use your account without your password.</li>
                <li><b>Real-Debrid and TorBox:</b> press <b>Connect Real-Debrid</b> or <b>Connect TorBox</b>, then approve the code on the service's own website.</li>
                <li><b>Usenet:</b> add your news server with <b>Add Server</b>.</li>
              </ul>
              <p>Press <b>Test</b> on the card to check the connection. The <b>Services</b> tab of this manual describes every service.</p>
            </div>`)}

          ${step(3, 'Add your first download', `
            <div class="dp-help-prose">
              <p>On the <b>Dashboard</b>, paste a link into <b>Quick Add</b> and press <b>Add</b>. Put each download on its own line. To add a file instead, leave the box empty and press <b>Add</b> to choose a <code>.torrent</code>, <code>.nzb</code>, <code>.meta4</code> or a text file that lists links.</p>
              <p>If the download contains several files, DebridPulse may ask you which ones you want. If a server needs a password, it asks for one.</p>
            </div>`)}

          ${step(4, 'Follow it to the finish', `
            <div class="dp-help-prose">
              <p>The download appears in <b>Download Activity</b> on the Dashboard and in <b>Downloads</b>. Click it to open its details: the service delivering it, its files, and its events.</p>
              <p>If something does not go as expected, read the download's details and the <b>Activity Log</b> before deleting anything. The <b>Troubleshooting</b> tab starts from common symptoms.</p>
            </div>`)}
        </div>

        ${inset('Where to read next', `
          <ul>
            <li><b>Everyday Use:</b> every page, button and prompt you will meet day to day.</li>
            <li><b>How It Works:</b> how a service is chosen, what happens when something fails, and what pausing, retrying and removing do.</li>
            <li><b>Services</b> and <b>Engines:</b> what each service and engine can and cannot do.</li>
            <li><b>Settings:</b> every Settings tab, card and important option.</li>
          </ul>
          <p>Optional features such as automatic archive extraction, Discord notifications, backups and OpenID Connect sign-in can be added whenever you need them.</p>`)}
      </section>`;
  }

  function usingPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-using-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-using-heading">Using DebridPulse</h2>
          <p>The pages, controls and prompts you use every day, in the order you meet them.</p>
        </div>

        <div class="dp-help-copy dp-help-copy--lead dp-help-prose">
          <p>The sidebar has three groups: <b>Queue</b> (<b>Dashboard</b> and <b>Downloads</b>), <b>Monitor</b> (<b>Activity Log</b> and <b>Statistics</b>) and <b>System</b> (<b>Settings</b> and <b>Help &amp; License</b>). The number beside Downloads counts downloads that are still in progress or waiting; failed and finished ones are not counted.</p>
        </div>

        <div class="dp-help-accordion-list">
          ${accordion('Header controls', `
            <ul>
              <li><b>Pause All:</b> pauses every download and stops DebridPulse from starting new work. You can still add downloads while paused; they wait in the queue. The button then reads <b>Resume All</b>, which resumes everything, including downloads you paused one by one.</li>
              <li><b>Resume Paused (N):</b> appears when processing is running but some downloads are individually paused. It resumes them all.</li>
              <li><b>Activity badge:</b> shows how many downloads are running out of the maximum allowed, the current combined speed, and the speed cap. Open its menu to pick a cap (Unlimited, or 1 to 100 MB/s), or type your own in <b>Custom cap (MB/s)</b> and press <b>Set Custom</b>. The cap covers every download together, whichever engine runs it, and stays in place after a restart.</li>
              <li><b>Update badge:</b> appears when a newer DebridPulse version is available and links to its change log.</li>
              <li><b>Theme button:</b> switches between dark and light mode.</li>
            </ul>
            <p>While downloads are running, the browser tab title shows the overall speed and progress.</p>`, true)}

          ${accordion('Provider Status (sidebar)', `
            <p>The sidebar lists your services with a coloured dot. The colour is the whole message:</p>
            <ul>
              <li><b>Green:</b> working. For a premium service this also means its current settings have passed a Test.</li>
              <li><b>Amber:</b> usable but limited. For example: switched on but not set up yet, not tested since its settings changed, or connected to a free or expired account.</li>
              <li><b>Red:</b> not usable. For example: the sign-in was rejected, the service is unavailable, or it is switched off.</li>
              <li><b>Grey:</b> still checking.</li>
            </ul>
            <p>Network Sources share one row. If a status check briefly fails, DebridPulse keeps showing the last good answer for a few minutes rather than flashing red, as long as that service's settings have not changed. Below the services you can see your premium account's expiry, the <b>aria2</b> engine version (or <b>offline</b>), the database, and the DebridPulse version.</p>`)}

          ${accordion('Dashboard and Quick Add', `
            <p>The summary cards count <b>Total Downloads</b>, <b>Completed</b>, <b>Active Now</b>, <b>Processing</b>, <b>Errors</b> and <b>Total Downloaded</b>. Click <b>Errors</b> to open Downloads filtered to failed downloads.</p>
            <p><b>Quick Add</b> accepts:</p>
            <ul>
              <li><b>Links:</b> <code>http://</code>, <code>https://</code>, <code>ftp://</code>, <code>sftp://</code>, <code>scp://</code>, <code>ssh://</code>, <code>rsync://</code>, <code>rsync+ssh://</code>, <code>webdav://</code>, <code>webdavs://</code>, <code>dav://</code> and <code>davs://</code>. One download per line; up to <b>100 unique links</b> at once. Exact duplicate lines are ignored.</li>
              <li><b>Magnet links</b> (they start with <code>magnet:?</code>), one per line.</li>
              <li><b>Alternate sources:</b> put several links for the same file on one line, separated by a Tab. DebridPulse treats them as one download and keeps the extras as standby sources it can use only after confirming they hold the same file.</li>
            </ul>
            <p>Press <b>Add</b> (or Ctrl+Enter). If any line is not something DebridPulse accepts, nothing is added and the message names the line. Lines that could not be added stay in the box.</p>
            <p>To add a file, empty the box and press <b>Add</b>. The file's name decides what it is:</p>
            <ul>
              <li><code>.torrent</code>: a torrent file, up to 16 MB.</li>
              <li><code>.nzb</code>: a Usenet download, up to 1 GB.</li>
              <li><code>.meta4</code>: a Metalink file, up to 16 MB.</li>
              <li><b>Any other file:</b> read as a list of links, up to 1 MB. Plain text with one link per line works; so do simple JSON, XML or YAML lists. Lines beginning with <code>#</code> are ignored.</li>
            </ul>
            <p><b>Recover All</b>, in the Quick Add header, asks DebridPulse to check all tracked work and pick up anything that can safely continue. See the <b>How It Works</b> tab.</p>
            <p><b>Download Activity</b> shows the downloads at the top of the queue with their status, progress, size and date. You can pause or resume them there; click a row to open its details, or <b>View All</b> to open Downloads.</p>`)}

          ${accordion('Questions DebridPulse may ask while adding', `
            <ul>
              <li><b>Connect to a local network address?</b> appears when a link points at a device on your private network and <b>Local Network Connections</b> is on. <b>Allow</b> applies to that download only. When Local Network Connections is off, such links are refused.</li>
              <li><b>Download the item or its collection?</b> appears when a link points at one item inside a collection, for example a video that is part of a playlist. Choose the item, the whole collection, or <b>Cancel Transfer</b>.</li>
              <li><b>Authentication Required</b> appears when a server needs a username and password, or, for SSH-based servers, a key file and its passphrase. <b>Cancel</b> cancels the download.</li>
              <li><b>Verify Server Identity</b> appears before DebridPulse connects to an SFTP, SCP or SSH server for a download. Compare the fingerprint shown with the one the server's owner gave you, then press <b>Verify &amp; Continue</b>.</li>
            </ul>
            <p>These dialogs do not close when you click outside them; choose one of their buttons.</p>`)}

          ${accordion('Choosing files', `
            <p>When a torrent, magnet, Metalink file or folder link contains more than one file, <b>Select files</b> opens automatically once the file list is known. Every file starts ticked. Untick what you do not want and press <b>Confirm</b>.</p>
            <ul>
              <li>If you do nothing, every file downloads when the countdown ends. <b>Close</b> also keeps all files.</li>
              <li><b>Cancel Transfer</b> cancels the whole download.</li>
              <li>You can reopen the selection later with <b>Choose Files</b> or <b>Change Files</b> on the download, while it can still be changed.</li>
              <li>For a collection of separate items (titled <b>Select entries</b>), nothing downloads until you confirm, and Close cancels the download.</li>
              <li>NZB downloads always fetch the whole posting; there is no file selection.</li>
            </ul>`)}

          ${accordion('Downloads page', `
            <ul>
              <li><b>Filters:</b> All, Downloading, Paused, Processing, Ready, Done, Error. <b>Search</b> matches as you type.</li>
              <li><b>Order:</b> <b>Priority</b> (queue order, the default) or <b>Newest</b>.</li>
              <li><b>Row actions:</b> <b>Pause</b>, <b>Resume</b>, <b>Retry</b> (on a failed download) and <b>Remove</b>. <b>Start Now</b> appears when a download you moved up can only start if lower downloads restart.</li>
              <li><b>Several at once:</b> tick rows to show <b>Pause</b>, <b>Resume</b>, <b>Reset</b> (retry), <b>Move to Top</b> and <b>Delete</b>.</li>
              <li><b>Queue order:</b> in Priority order with no filter or search, drag a download by its name to move it, or use the keyboard (Space or Enter to pick up, arrow keys to move, Space or Enter to drop, Escape to cancel). Right-click offers <b>Move to Top</b>. If a move would interrupt a running download, DebridPulse asks first because that download's progress would be lost.</li>
              <li><b>Provider / Source</b> shows where the download came from, which service is handling it, and how it was submitted. A network icon with a number means several sources are available for its files.</li>
              <li>The <b>Added</b> column's menu changes the date and time format.</li>
            </ul>`)}

          ${accordion('Download details', `
            <p>Click any download to open its details.</p>
            <ul>
              <li><b>Provider</b> is the service handling the download now. <b>Submitted As</b> is what you gave DebridPulse (for example Direct link, Magnet link, Torrent file, NZB file or Metalink file). <b>Original Resource</b> is the source you submitted.</li>
              <li><b>Route History</b> lists every service DebridPulse tried for this download and how each attempt ended.</li>
              <li><b>Advanced acquisition details</b> separates the engine, the service you started with, the current service, and the service that actually delivered the files.</li>
              <li><b>Files</b> lists each file with its size and status. Depending on the download, its header lets you switch service (<b>Provider</b>), switch the remaining files to another common source, or change the file selection. Each file's <b>Candidates</b> list shows the other sources known for it. If a switch would throw away progress, DebridPulse asks <b>Discard downloaded progress?</b> first.</li>
              <li><b>Events</b> is this download's own history. <b>Transfer Trace Log</b> saves a detailed record you can attach to a bug report; DebridPulse removes passwords and private link details from it, but read it before sharing.</li>
            </ul>
            <p>Progress marked <b>not yet verified</b> or <b>estimated</b> is DebridPulse's best estimate until the files are checked.</p>`)}

          ${accordion('Activity Log', `
            <p>The Activity Log is the full event history of DebridPulse: transfers, routing decisions, recovery, storage, extraction, settings changes and more. Filter it by <b>Time Window</b>, <b>Severity</b> (Info, Warning, Error) and <b>Event Type</b>, or search for a name or transfer ID. Expand <b>Details</b> on an event for more information.</p>
            <p>Events are kept until you reset the database. The number shown per page is set in <b>Settings → Data &amp; Maintenance → Event Logging</b>.</p>`)}

          ${accordion('Statistics', `
            <ul>
              <li>The top cards and the <b>Completions</b> chart follow the <b>Period</b> you choose (1 hour to All Time).</li>
              <li><b>Overall &amp; Rolling Metrics</b> always shows the last 24 hours, the last 7 days and lifetime figures.</li>
              <li>The <b>Completions</b> chart splits each bar into Premium Sources and Network Sources by the service that actually delivered the files.</li>
              <li><b>Transfer Methods</b> counts finished files by how they were really obtained (for example Debrid, Usenet, HTTP(S), FTP / SFTP, SCP, rsync, WebDAV or Media Downloads), not by what you submitted. A Metalink download is counted under the method that delivered each file. Files whose method was not recorded appear as <b>Unattributed</b>.</li>
              <li><b>Transfer Disposition</b>, <b>File Status</b> and <b>Events Severity</b> count downloads, files and log events by state.</li>
            </ul>`)}
        </div>
      </section>`;
  }

  function howItWorksPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-howitworks-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-howitworks-heading">How downloads work</h2>
          <p>What DebridPulse does between pressing Add and a finished file, and what it does when something goes wrong.</p>
        </div>

        <div class="dp-help-copy dp-help-copy--lead dp-help-prose">
          <p>Each thing you add becomes one <b>download</b> that DebridPulse tracks from start to finish, even if it changes service, source or engine along the way. A <b>service</b> (also called a provider or source) is what can obtain the files: a debrid account, a Usenet server, or a network protocol such as HTTPS or rsync. A <b>download engine</b> is the program that writes the files to your storage.</p>
        </div>

        <div class="dp-help-pipeline">
          ${pipeline('1. Add', 'DebridPulse records what you submitted before anything else happens, so nothing is lost if processing is paused or the application restarts.')}
          ${pipeline('2. Choose a service', 'It finds the enabled services that can handle this input and picks one, as described below.')}
          ${pipeline('3. Prepare', 'The service makes the files available. A debrid service may need time to fetch a torrent first; a plain web link is ready at once. File selection happens here.')}
          ${pipeline('4. Download', 'The matching engine downloads the files: aria2, rsync, Usenet or Media Downloads.', 'active')}
          ${pipeline('5. Check and finish', 'DebridPulse checks the result, for example the file size or checksum when one is known, and marks the download done only when its files are complete.', 'success')}
          ${pipeline('6. Extract (optional)', 'If Automatic Extraction is on, supported archives are unpacked afterwards.')}
        </div>

        <div class="dp-help-accordion-list">
          ${accordion('How a service is chosen', `
            <ul>
              <li><b>Specialist services come first.</b> A magnet, <code>.torrent</code> or NZB goes only to services that handle that kind of input. A web link goes to a debrid service only if that service lists the link's host as supported. When a specialist service can take a download, the ordinary HTTP(S) source is not used for it, not even if that service later fails.</li>
              <li><b>Links added together are one download.</b> All the links you add at once in Quick Add, or in one link-list file, become a single download; only magnets each become a download of their own. If a debrid service supports any link in that download, the whole download is left to debrid services: each of its links goes to a debrid service that supports that particular link, and a link that none of them supports is not downloaded as an ordinary web file. To download ordinary web links directly, add them separately from links meant for a debrid service. Media Downloads links do not have this effect.</li>
              <li><b>It waits rather than guesses.</b> While a debrid service is still loading its list of supported hosts, DebridPulse waits for it instead of downloading the link as an ordinary web file.</li>
              <li><b>Ready content is preferred.</b> For torrents, a service that reports the content as already available is tried before one that would need to fetch it.</li>
              <li><b>No ranking setting.</b> Settings does not have a general priority order for services; the order of cards in Settings and in the sidebar is only for display. For NZBs, <b>Use TorBox Before Usenet</b> and <b>Use Premiumize Before Usenet</b> let those services try first. For a single torrent, you can switch the service yourself from the download's <b>Provider</b> control.</li>
              <li><b>Enabled is not the same as usable.</b> A service that is switched on but not connected, or whose account does not include a feature (for example a free plan), is skipped for that download.</li>
            </ul>`, true)}

          ${accordion('What the status words mean', `
            <ul>
              <li><b>Pending, Resolving, Processing:</b> a service is preparing the download; no files are arriving yet.</li>
              <li><b>Queued, Waiting for execution slot:</b> ready, but waiting for a free place under Maximum Concurrent Downloads.</li>
              <li><b>Downloading, Verifying:</b> files are arriving or being checked.</li>
              <li><b>Waiting for provider, Waiting for storage, Waiting for executor, Waiting for retry:</b> held until a service, enough free disk space, an engine or the next retry time is available.</li>
              <li><b>Input Required, Awaiting confirmation:</b> DebridPulse needs an answer from you, such as a password or a file selection.</li>
              <li><b>Paused, Recovering:</b> held on purpose, or being picked up again.</li>
              <li><b>Extracting:</b> archives are being unpacked.</li>
              <li><b>Done:</b> finished. <b>Consolidated:</b> merged into another download of the same thing.</li>
              <li><b>Failed, Requires attention, Cancelled:</b> stopped. The details explain why.</li>
            </ul>`)}

          ${accordion('When something fails', `
            <p>DebridPulse sorts failures by how far they reach, and only does what each one allows:</p>
            <ul>
              <li><b>A temporary problem</b> (a timeout, a busy server, a rate limit) is retried automatically using <b>Download Error Retries</b> and <b>Retry Delay</b>. The wait grows with each attempt, up to about five minutes unless the server asks for longer.</li>
              <li><b>A problem with one service</b> (for example, its retries are used up, or it refuses this download) makes DebridPulse try the next service that can handle the download, if there is one. A service that only failed temporarily may be tried again later, but it does not replace a service that is working.</li>
              <li><b>A problem with the download itself</b> (the file no longer exists, the source is invalid) stops it, because no other service can fix that.</li>
              <li><b>Alternate sources</b> are used only when DebridPulse can confirm they hold the same file, for example the same size and checksum. It never assumes two files match because of their names or positions.</li>
              <li><b>Stalled downloads:</b> a download that makes no progress for the <b>Stalled Download Timeout</b> is stopped and retried, and a service that makes no progress preparing a download for that long counts as a failure of that service.</li>
            </ul>
            <p>Switching to another source or service does not always keep the progress already made. Whether a partly finished file can be continued depends on the engine and the source, so a switch may start that file again.</p>`)}

          ${accordion('Backup torrents', `
            <p>Each debrid service has a <b>Prepare Backup Torrents</b> switch. It is on by default for AllDebrid, Debrid-Link, Premiumize and Real-Debrid, and off by default for TorBox, whose torrent limits are tighter. While it is on, that service also adds torrents another service is delivering, so it is ready to take over if needed, and when you switch a download to another service, the torrent it already started is kept as a backup instead of being removed. This uses that account's torrent slots and limits; a new download that needs a slot takes it back first. Turn it off to keep that service's slots for its own downloads: its backups are then removed. A choice you already saved is kept.</p>`)}

          ${accordion('Pause, resume, retry, recover and remove', `
            <ul>
              <li><b>Pause</b> holds one download; it does not occupy a download slot while paused. <b>Resume</b> lets it continue.</li>
              <li><b>Pause All</b> pauses every download and starts no new work, but still accepts new submissions. <b>Resume All</b> resumes everything.</li>
              <li><b>Retry</b> (or <b>Reset</b>) starts a failed download again from its original source, without re-adding it.</li>
              <li><b>Recover All</b> re-checks all tracked work with your services and engines and continues whatever can safely continue. It does not reset retry counts or override your settings.</li>
              <li><b>Remove</b> takes a download off the list. DebridPulse also cleans up the copy it created at the service and any unfinished partial files it was writing. <b>Files that finished downloading stay in your folder.</b></li>
            </ul>`)}

          ${accordion('Adding the same thing twice', `
            <p>If you add something DebridPulse is already working on (including one that failed and has not been removed), it shows <b>Already in queue</b> and keeps the existing download instead of starting a second one. Once a download is finished, cancelled or removed, adding the same source again starts a new download. When two downloads turn out to fetch the same content, DebridPulse may merge them; the merged one shows <b>Consolidated</b>.</p>`)}

          ${accordion('What survives a restart', `
            <p>Downloads, their history, the queue order, Pause All and the speed cap are saved and survive a restart; DebridPulse reconnects to its engines and continues. Browser sign-ins do not survive a restart, so you sign in again. Passwords typed into a prompt or written into a link are never saved and are forgotten on restart.</p>`)}

          ${accordion('Passwords and private links', `
            <p>A username and password written into a link (<code>scheme://user:password@host/...</code>) are used for that download instead of asking, including for every file in a folder, and are removed from the link before it is saved. They are kept only in memory for that download, and are forgotten when it finishes, is cancelled or removed, after at most 24 hours, or when DebridPulse restarts.</p>
            <p>For SFTP, SCP and SSH servers, DebridPulse confirms the server's identity before sending any password. A rejected password is never retried automatically; DebridPulse asks again. Media Downloads never uses passwords.</p>`)}
        </div>
      </section>`;
  }

  function servicesPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-services-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-services-heading">Services and sources</h2>
          <p>What each service in <b>Settings → Services</b> is for, what it needs, and what to expect from it.</p>
        </div>

        <div class="dp-help-copy dp-help-copy--lead dp-help-prose">
          <p>Settings → Services has two groups. <b>Premium Services</b> need your own account with an outside service. <b>Network Sources</b> download directly from a server and need no account with anyone (although the server itself may ask for a password).</p>
        </div>

        ${inset('How a service card works', `
          <ul>
            <li><b>Enable</b> switches the service on or off immediately.</li>
            <li>The status word says whether it is <b>Unconfigured</b> (switched on but not connected), <b>Unverified</b> (connected, but the current settings have not passed a Test) or <b>Verified</b>.</li>
            <li><b>Test</b> checks the saved connection. A passing Test means the account and settings worked at that moment; it does not guarantee that every link or plan feature will work later.</li>
            <li><b>Additional Settings</b> holds optional tuning. The defaults suit most people.</li>
            <li>While a service still has downloads in progress, DebridPulse will not let you change its account; finish or remove those downloads first.</li>
          </ul>`)}

        <h3>Premium Services</h3>
        <div class="dp-help-accordion-list">
          ${accordion('AllDebrid', `
            <p><b>For:</b> magnets, <code>.torrent</code> files and links to file hosters AllDebrid supports.</p>
            <p><b>You need:</b> an AllDebrid account and its API key, from AllDebrid's API key page. Paste it into <b>API Key</b>; it is saved when you leave the field and then shows <b>Key present</b>. <b>Remove API Key</b> deletes it.</p>
            <p><b>What happens:</b> AllDebrid fetches the content on its own servers, then DebridPulse downloads the files from AllDebrid with aria2. Your computer never joins a torrent swarm. For a multi-file torrent you can choose files.</p>
            <p><b>Good to know:</b> AllDebrid is switched on in a new installation, but does nothing until a key is added. With a free account, only hosts AllDebrid offers to free users can be used, and torrents are refused. <b>Maximum Active Torrents</b> left empty follows AllDebrid's own limit; per AllDebrid's documentation that is 30 active magnets. This card also holds four settings that apply to all premium services: <b>Provider Poll Interval</b>, <b>Full Sync Interval</b>, <b>Upload Failure Retries</b> and <b>Retry Delay (minutes)</b>.</p>
            <p><b>If it fails:</b> a Test error usually means a wrong or revoked key. A torrent refused on a free account needs a premium account or another service.</p>`)}

          ${accordion('Debrid-Link', `
            <p><b>For:</b> magnets, <code>.torrent</code> files (through your Debrid-Link seedbox) and links to supported file hosters.</p>
            <p><b>You need:</b> a Debrid-Link account and its API key, pasted into <b>API Key</b>.</p>
            <p><b>What happens:</b> Debrid-Link prepares the files; DebridPulse downloads them with aria2. A hoster link that contains several files offers file selection.</p>
            <p><b>Good to know:</b> off by default. The supported host list is refreshed every 24 hours by default (<b>Supported Host Refresh Interval</b>). What your account may use depends on your Debrid-Link plan: DebridPulse only uses torrents on an account it can confirm is premium, and on a free account only hosts Debrid-Link marks as free.</p>
            <p><b>If it fails:</b> check the API key with Test, and the download's details for an account or quota message.</p>`)}

          ${accordion('Premiumize', `
            <p><b>For:</b> magnets, <code>.torrent</code> files, NZBs and links to supported file hosters.</p>
            <p><b>You need:</b> a Premiumize premium account and its API key, pasted into <b>API Key</b>.</p>
            <p><b>What happens:</b> DebridPulse first asks Premiumize for immediate download links. If that is not possible, it adds the item to your Premiumize cloud storage and downloads it from there once ready. Files are downloaded with aria2.</p>
            <p><b>Good to know:</b> off by default. A free or expired Premiumize account stays connected but cannot start anything. For NZBs, Premiumize is normally tried after your own Usenet servers; switch on <b>Use Premiumize Before Usenet</b> to try it first. Either way, both stay available as a fallback for each other.</p>
            <p><b>Premiumize as a news server:</b> per Premiumize's documentation, a Premiumize account also includes Usenet access that you can add as an ordinary news server under <b>Usenet</b> with <b>Add Server</b>: host <code>usenet.premiumize.me</code>, port <code>563</code> with <b>SSL</b> on, your Premiumize customer ID as the username and your Premiumize API key as the password. Check Premiumize's own instructions if these details have changed. That server is separate from this card.</p>
            <p><b>If it fails:</b> an account limit or fair-use message is treated as temporary and DebridPulse may try another service.</p>`)}

          ${accordion('Real-Debrid', `
            <p><b>For:</b> magnets, <code>.torrent</code> files and links to supported file hosters.</p>
            <p><b>You need:</b> a Real-Debrid account. There is no key to paste. Press <b>Connect Real-Debrid</b>; DebridPulse shows a code. Press <b>Open Real-Debrid</b>, sign in on Real-Debrid's site and enter the code (<b>Copy</b> copies it). The card then shows <b>Connected as</b> your account name, and Real-Debrid is switched on once the connection passes a Test.</p>
            <p><b>What happens:</b> Real-Debrid fetches the content; DebridPulse downloads the files with aria2.</p>
            <p><b>Good to know:</b> the code expires after a while; press Connect again for a new one. <b>Disconnect</b> makes DebridPulse forget the authorization and also asks Real-Debrid to cancel it. With a free account, torrents are refused and only free hosts work. Per Real-Debrid's documentation it allows at most 250 requests a minute; <b>API Calls per Minute</b> defaults to 240.</p>
            <p><b>If it fails:</b> if the card says it is not connected, connect again.</p>`)}

          ${accordion('TorBox', `
            <p><b>For:</b> magnets, <code>.torrent</code> files, links to supported file hosters and, depending on your plan, NZBs.</p>
            <p><b>You need:</b> a TorBox account. Like Real-Debrid, it uses a code instead of a key: press <b>Connect TorBox</b>, then <b>Open TorBox</b> and approve the code there.</p>
            <p><b>What happens:</b> TorBox fetches the content; DebridPulse downloads the files with aria2. TorBox can report whether something is already available on its side, which lets it be tried first.</p>
            <p><b>Your plan matters:</b> per TorBox's documentation, a free plan cannot use the API at all, the paid plans include torrents and web downloads, and only the Pro plan includes NZB downloads. DebridPulse follows what TorBox reports for your account: if it is known not to include Usenet, switching on <b>Use TorBox Before Usenet</b> is refused with <b>Requires TorBox Pro.</b> That switch only changes the order for NZBs; TorBox still handles NZBs as a fallback when your plan allows it. <b>Maximum Active Torrents</b> left empty follows your plan's limit.</p>
            <p><b>Good to know:</b> <b>Disconnect</b> only makes DebridPulse forget the token. TorBox offers no way to cancel it remotely, so reset your token on TorBox's website if you want it revoked there.</p>`)}

          ${accordion('Usenet', `
            <p><b>For:</b> NZB files. An NZB is a small file that lists the parts of a Usenet posting; Usenet is a network of news servers, usually reached through a paid news server subscription.</p>
            <p><b>You need:</b> at least one news server account. Press <b>Add Server</b> and fill in <b>Host</b>, <b>Port</b> (563 by default), <b>SSL</b> (on by default), <b>Username</b> and <b>Password</b>. Under <b>Advanced</b> you can set <b>Connections</b>, <b>Priority</b> (lower numbers are used first), <b>Articles per Request</b> and <b>Server Timeout</b>. Press the server's own <b>Test</b> to check it, and the pencil to give it a display name.</p>
            <p><b>What happens:</b> DebridPulse runs its own private SABnzbd download engine for Usenet; there is nothing to install or sign in to. It downloads the posting, checks it and repairs it with the posting's repair files when needed. Extraction is done afterwards by DebridPulse's own <b>Automatic Extraction</b>, using your <b>Archive Passwords</b>.</p>
            <p><b>Good to know:</b> Usenet is off by default and counts as set up only when at least one enabled server has a host and at least one connection. It shows <b>Verified</b> only when every such server has passed a Test with its current settings. NZB files are added through Quick Add; there is no file selection. Global Usenet settings are under <b>Settings → Downloads → Transfer Method Settings → Usenet</b>.</p>
            <p><b>If it fails:</b> the header <b>Test</b> reports how many servers passed. Missing parts of a posting are usually a server retention or coverage issue; adding another server can help.</p>`)}
        </div>

        <h3>Network Sources</h3>
        <div class="dp-help-copy dp-help-prose">
          <p>All network sources are switched on in a new installation. The group's <b>Enable</b> switches them all; each box also has its own. They have no Test button: whether a link works depends on that server. Their tuning is under <b>Settings → Downloads → Transfer Method Settings</b>.</p>
        </div>
        <div class="dp-help-accordion-list">
          ${accordion('HTTP(S)', `
            <p>Ordinary web links (<code>http://</code> and <code>https://</code>) that no specialist service claims. Each link is one file, downloaded with aria2. A server that needs a password asks for one, or you can write it into the link. Tuning: <b>Transfer Method Settings → Network Sources</b>.</p>`)}

          ${accordion('(S)FTP', `
            <p><code>ftp://</code> and <code>sftp://</code> links to a file or a folder. A folder means the files directly inside it (no subfolders), offered in file selection. Sign-in is by username and password; SFTP asks you to confirm the server's identity first. Downloaded with aria2.</p>`)}

          ${accordion('SCP', `
            <p><code>scp://</code> and <code>ssh://</code> links to one file (<code>scp://host/path/file</code>), the files directly inside a folder (<code>ssh://host:2222/path/folder/</code>), or the files in one folder matching a pattern in the last part of the path (<code>scp://host/path/*.iso</code>; only <code>*</code> and <code>?</code>). Subfolders are never entered. A path starting <code>~/</code> starts in your login folder; another user's <code>~name</code> and patterns in folder names are not accepted.</p>
            <p>Files are downloaded over SFTP with aria2, so the server must offer SFTP. Sign-in is by username and password, after confirming the server's identity.</p>`)}

          ${accordion('rsync', `
            <p><code>rsync://host/path</code> names a file or folder on an rsync server; <code>rsync://host/</code> lists the server's shared areas. DebridPulse asks the rsync service first. If it does not share that path, or does not answer within <b>Connection Timeout</b>, the same path is read over SSH instead. A link with its own port always means that rsync service, and <code>rsync+ssh://host/path</code> always means SSH.</p>
            <p>A folder includes its subfolders, down to the <b>Directory Depth</b> you choose (all levels by default), and opens file selection. Links on the server are never followed and names are taken literally (no wildcards). rsync only reads from the server; it never changes, deletes or uploads anything there.</p>
            <p>Over SSH, the server's identity is confirmed first and you sign in with a password or a key file (and its passphrase). An rsync service's shared areas may ask for their own password. Downloaded by DebridPulse's rsync engine. Tuning: <b>Transfer Method Settings → rsync</b>.</p>`)}

          ${accordion('WebDAV', `
            <p><code>webdavs://</code> or <code>davs://</code> links (or <code>webdav://</code> and <code>dav://</code> for a server without HTTPS) to a file or folder on a WebDAV server, a common way to share files from cloud storage and NAS devices. A plain <code>https://</code> link ending in <code>/</code> is tried as WebDAV first and becomes an ordinary download if the server does not offer WebDAV there.</p>
            <p>A folder opens file selection with the files directly inside it; <b>Directory Depth</b> adds subfolder levels. A folder with more files than <b>Maximum Files</b>, or whose files cannot all be found within <b>Collection Scan Timeout</b> (no limit by default), is not added at all rather than added in part. A protected server asks for its password once per server. Files are downloaded with aria2. Tuning: <b>Transfer Method Settings → WebDAV</b>.</p>`)}

          ${accordion('Metalink (Multimeta)', `
            <p>Reads Metalink files: an uploaded <code>.meta4</code> file, or a web link ending in <code>.meta4</code>. A Metalink file lists one or more files with several download addresses for each. Each address is downloaded through whichever service handles it, in the order the publisher prefers; if one fails, the next listed address is used. A declared size or checksum is checked when the file completes. A Metalink file describing several files opens file selection. Older Metalink 3 files are not supported, and links to other metadata (such as torrents) inside a Metalink file are not followed.</p>`)}

          ${accordion('Media Downloads', `
            <p>Downloads video and audio from web pages on sites the bundled yt-dlp media downloader recognises, for example many popular video sites. A link to a page yt-dlp does not specifically recognise stays an ordinary HTTP(S) download.</p>
            <ul>
              <li><b>What you get:</b> one file per item, named <code>Title [id]</code>, in the site's own format when possible and otherwise MKV. Video and audio are combined without re-encoding, so quality is not reduced. Title, description and date are written into the file, and one subtitle in your <b>Preferred Subtitle Language</b> is embedded when the site offers it.</li>
              <li><b>Choices:</b> <b>Target Resolution</b>, <b>Video Quality</b> and <b>Preferred Video Codec</b> choose among the versions the site offers; DebridPulse never converts media. Settings are under <b>Transfer Method Settings → Media Downloads</b>.</li>
              <li><b>Playlists:</b> up to the first 100 entries. A video link that is also part of a playlist asks whether you want the <b>Current Video</b> or the <b>Playlist</b>.</li>
              <li><b>Not supported:</b> media that needs a sign-in, cookies or a subscription; live or upcoming streams; DRM-protected media; and media on your local network.</li>
              <li><b>Good to know:</b> an interrupted media download starts that item again from the beginning.</li>
            </ul>`)}
        </div>
      </section>`;
  }

  function enginesPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-engines-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-engines-heading">Download engines</h2>
          <p>The programs that write files to your storage. DebridPulse includes and runs all of them; there is nothing to install or connect.</p>
        </div>

        <div class="dp-help-copy dp-help-copy--lead dp-help-prose">
          <p>A service decides <b>where</b> files come from; an engine does the <b>downloading</b>. DebridPulse picks the engine that fits each file, so one download can use different engines over its life, for example after switching to another service.</p>
        </div>

        <div class="dp-help-integration-grid">
          ${inset('aria2', `
            <p>Downloads web (HTTP and HTTPS), FTP and SFTP files. It handles files from debrid services, HTTP(S), (S)FTP, SCP, WebDAV and most Metalink addresses. aria2 in DebridPulse cannot share torrents, so it never uploads to anyone.</p>
            <p>The sidebar shows its version, or <b>offline</b> if it is not responding.</p>`)}
          ${inset('rsync', `
            <p>Downloads rsync links, both from rsync services and over SSH. It can continue a partly downloaded file and only ever reads from the server.</p>`)}
          ${inset('Usenet (SABnzbd)', `
            <p>A private copy of SABnzbd that DebridPulse runs for NZB downloads from your news servers. It downloads and repairs postings but does not unpack them; Automatic Extraction does that.</p>`)}
          ${inset('Media Downloads (yt-dlp)', `
            <p>Downloads media from supported sites in an isolated process and combines video, audio and subtitles without re-encoding. It cannot continue an interrupted item; that item starts again.</p>`)}
        </div>

        ${inset('Limits that apply to every engine', `
          <ul>
            <li><b>Maximum Concurrent Downloads</b> (<b>Settings → Downloads → Download Behavior &amp; Limits</b>, 3 by default) is one limit for all engines together.</li>
            <li>The <b>speed cap</b> in the header is shared equally between the engines that are downloading at that moment, and together they never exceed it.</li>
            <li><b>Disk Space &amp; Recovery</b> settings (free space, retries, stalled downloads) apply to every engine.</li>
          </ul>`)}

        <div class="dp-help-accordion-list">
          ${accordion('Tuning aria2: Transfer Method Settings → Network Sources', `
            <ul>
              <li><b>Connections per Server</b> and <b>Segments per File:</b> how many connections and pieces one file may use at once.</li>
              <li><b>Minimum Split Size:</b> files smaller than about twice this size are not split.</li>
              <li><b>Continue Partial Downloads:</b> continue a partly downloaded file instead of starting again, when the server allows it.</li>
              <li><b>Lowest Speed Limit:</b> drops a connection that stays slower than this; <code>0</code> turns it off.</li>
              <li><b>Disk Cache:</b> memory used to reduce disk writes.</li>
              <li><b>File Allocation:</b> how disk space is reserved for a new file.</li>
            </ul>
            <p>More connections do not guarantee more speed: the server, the service, your network or your disk can each be the limit. Change one setting at a time.</p>`, true)}

          ${accordion('Tuning rsync, Usenet and Media Downloads', `
            <ul>
              <li><b>rsync:</b> <b>Partial Transfers</b>, <b>Compression</b>, <b>Preserve Modification Time</b>, <b>Connection Timeout</b>, <b>Transfer Timeout</b> and <b>Directory Depth</b>.</li>
              <li><b>Usenet:</b> <b>Article Cache Limit</b>, <b>Maximum Retries</b> (for each part of a posting, separate from DebridPulse's own download retries), <b>Request Timeout</b> and <b>Direct Write</b>. Settings for each news server are on its card under Services.</li>
              <li><b>Media Downloads:</b> <b>Target Resolution</b>, <b>Video Quality</b>, <b>Preferred Video Codec</b>, <b>Preferred Subtitle Language</b> and <b>Concurrent Fragment Downloads</b> (pieces of one streamed video fetched at once).</li>
            </ul>`)}

          ${accordion('Download Engine Activity', `
            <p><b>Settings → Downloads → Download Engine Activity</b> lists the jobs the engines are running right now, with <b>Pause</b>, <b>Resume</b> and <b>Terminate</b> for each. It is a recovery tool for a job that is truly stuck. For normal work, use the Downloads page, which keeps DebridPulse's own records in step.</p>`)}
        </div>
      </section>`;
  }

  function settingsPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-settings-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-settings-heading">Settings reference</h2>
          <p>The seven Settings tabs, their cards, and the options most people need.</p>
        </div>

        ${inset('How saving works', `
          <p>There is no Save button. Every setting is saved on its own: a typed value is saved when you leave the field, and a switch or list choice is saved the moment you change it. If a value cannot be saved, a message explains why and the field returns to its saved value.</p>
          <p>Stored secrets such as API keys and passwords are never shown again. When one is stored, the field shows that it is set, and leaving it blank keeps the stored value. To delete a secret, use its <b>Remove</b> or <b>Clear</b> button and confirm.</p>
          <p><b>Test</b> buttons save any field you are still editing, then check the saved settings.</p>`)}

        <div class="dp-help-accordion-list">
          ${accordion('Services', `
            <ul>
              <li><b>Premium Services:</b> one card each for Usenet, AllDebrid, Debrid-Link, Premiumize, Real-Debrid and TorBox, with <b>Enable</b>, <b>Test</b>, the account connection and <b>Additional Settings</b>.</li>
              <li><b>Network Sources:</b> one <b>Enable</b> for the group and one for each of HTTP(S), (S)FTP, SCP, rsync, WebDAV, Metalink (Multimeta) and Media Downloads.</li>
            </ul>
            <p>The <b>Services</b> tab of this manual explains each one.</p>`, true)}

          ${accordion('Downloads', `
            <ul>
              <li><b>Download Behavior &amp; Limits:</b> <b>Download Folder</b> (with <b>Browse</b>) and <b>Maximum Concurrent Downloads</b>. Under <b>Advanced Settings</b>: <b>Material Checkpoint Interval</b> and <b>Graceful Stop Timeout</b> (best left alone), <b>Local Network Connections</b> (off by default) to allow downloads from devices on your private network, and <b>Skip Local Connection Confirmation</b> to stop asking each time. Even when allowed, DebridPulse never connects to the computer it runs on or other non-private local addresses.</li>
              <li><b>Transfer Method Settings:</b> tuning for <b>Network Sources</b> (aria2), <b>rsync</b>, <b>WebDAV</b>, <b>Usenet</b> and <b>Media Downloads</b>. See the <b>Engines</b> and <b>Services</b> tabs of this manual.</li>
              <li><b>Disk Space &amp; Recovery:</b> <b>Minimum Free Disk Space</b> (0 turns the guard off) holds new downloads when free space falls below it; <b>Resume Free Space Buffer</b> is how much extra must be free before they continue, so downloads do not stop and start repeatedly. <b>Download Error Retries</b>, <b>Retry Delay</b> and <b>Stalled Download Timeout</b> (0 turns it off) control automatic recovery.</li>
              <li><b>Download Engine Activity:</b> the engines' current jobs, for troubleshooting only.</li>
            </ul>`)}

          ${accordion('Extraction', `
            <p>The <b>Automatic Extraction</b> card unpacks archives after a download finishes. It is off by default.</p>
            <ul>
              <li><b>Concurrent Extractions:</b> how many archives are unpacked at once.</li>
              <li><b>Delete Archives After Extraction</b> (on by default): removes the archive only after it was unpacked successfully.</li>
              <li><b>Archive Passwords:</b> DebridPulse tries no password first, then each stored password in order. Each row holds one password; it is saved when you leave the row. <b>Show all</b> reveals them; <b>Clear Passwords</b> erases the whole list.</li>
            </ul>
            <p>Supported formats include ZIP, 7z, RAR (including multi-part RAR), and TAR with gzip, bzip2, xz, zstd or lzma compression. Archives that would write outside the download folder, or that are unreasonably large when unpacked, are refused for your safety.</p>`)}

          ${accordion('Authentication', `
            <ul>
              <li><b>Authentication Status:</b> the current mode, the state of each sign-in method, <b>Active Browser Sessions</b> with <b>Log Out Current Session</b>, and <b>Browser Session Lifetime</b> (12 hours by default).</li>
              <li><b>Username &amp; Password:</b> a local account for browser sign-in. The same username and password also work for scripts that use HTTP Basic sign-in.</li>
              <li><b>OpenID Connect</b> (OIDC): sign in through an identity service you already run, such as Authentik or Keycloak. Set <b>Public DebridPulse Base URL</b> to the HTTPS address people use to reach DebridPulse; DebridPulse then shows the exact <b>OIDC Callback URL</b> to register with your identity service. Fill in <b>Issuer URL</b>, <b>Client ID</b> and, if needed, <b>Client Secret</b>, then press <b>Test</b>, which runs a real sign-in in a pop-up window. <b>Access Control</b> limits which identities may sign in, and <b>Default Role for New OIDC Users</b> (User or ReadOnly User) is the role an identity receives the first time it signs in. A new OIDC identity always gets its own account; it is never joined to an existing account just because the email matches. If the base URL is set by the <code>PUBLIC_BASE_URL</code> environment variable, the field is read-only here; change it in your installation instead.</li>
            </ul>
            <p>Turning off the last sign-in method asks for confirmation, because it leaves DebridPulse open to anyone who can reach it. Failed sign-ins are slowed down rather than locked out.</p>
            <p>When sign-in is on, the Prometheus metrics address <code>/api/metrics</code> also needs a credential; give your monitoring tool an Integration token from <b>Users &amp; Access</b>.</p>`)}

          ${accordion('Users &amp; Access', `
            <p>Every person who signs in has an account with one role:</p>
            <ul>
              <li><b>Admin:</b> everything, including Settings, the Activity Log and every transfer.</li>
              <li><b>User:</b> adds and runs downloads, sees every transfer, and can withdraw the requests they made. Settings are not available.</li>
              <li><b>ReadOnly User:</b> sees the Dashboard, Downloads, Details and Statistics, and changes nothing.</li>
            </ul>
            <p><b>User Accounts</b> lists each person with their role, how they sign in and whether the account is enabled. <b>Add Local Account</b> creates a username-and-password account. <b>Change Role</b> applies at once: that person's open pages reload with the new role, without signing them out. Click an account's name for its details and its activity, where an outstanding request can be withdrawn on its own. <b>Disable</b> and <b>Remove</b> first show how many outstanding download requests the account has; those requests are withdrawn, a transfer someone else also asked for keeps going, and finished downloads are never touched. Removing keeps the account's history; a removed OIDC sign-in that comes back gets a new account that stays disabled until you enable it. <b>Merge</b> joins two or more accounts into the one you choose to keep; two accounts that share a verified email are pointed out, but only a Merge joins them.</p>
            <p><b>Integration Accounts</b> are for automation and cannot sign in to these pages. <b>Issue Token</b> shows a new token once, so copy it then; tools send it as <code>Authorization: Bearer &lt;token&gt;</code>. <b>Revoke</b> stops a token at once without touching anything it already submitted.</p>
            <p>Anyone signed in can change their own display name and actual name from their name at the bottom of the sidebar, beside <b>Log Out</b>, and their password when they sign in with one.</p>`)}

          ${accordion('Notifications', `
            <ul>
              <li><b>Discord Notifications:</b> sends messages to a Discord channel through a <b>webhook</b>, a private address Discord creates for a channel. Fill in <b>Discord Webhook</b>; <b>Download Added Webhook</b> optionally sends new-download messages elsewhere. <b>Display Name</b> and <b>Avatar URL</b> (or <b>Upload Avatar</b>) set how messages appear. Under <b>Notification Events &amp; Delivery Options</b>, choose <b>Download Added</b>, <b>Download Completed</b>, <b>Download Error</b>, <b>Extraction Result</b> and <b>Update Available</b>, and the <b>Update Check Interval</b> (0 turns update checks off). <b>Test</b> sends a test message using the saved settings.</li>
              <li><b>Statistics Reporting:</b> sends periodic summaries to Discord. <b>Reporting Webhook</b> is optional and falls back to the Discord Webhook. <b>Automatic Report Interval</b> is 0 (off) by default; <b>Report Window</b> sets how much history each report covers. <b>Test</b> sends one report now.</li>
            </ul>
            <p>An uploaded avatar is served by DebridPulse itself, so Discord must be able to reach it over the internet. If DebridPulse is only reachable on your private network, use an avatar link hosted elsewhere.</p>`)}

          ${accordion('Data &amp; Maintenance', `
            <ul>
              <li><b>Event Logging:</b> <b>Activity Log Page Size</b>, and <b>Recorded Events</b>, the number of events stored. Events are kept until the database is reset.</li>
              <li><b>Backups &amp; Retention:</b> scheduled backups (on by default) to <b>Backup Folder</b>, with interval and retention under <b>Additional Backup &amp; Retention Settings</b>, plus how often statistics snapshots are taken and how long they are kept. <b>Run Backup</b> makes a backup now (only while backups are enabled).</li>
              <li><b>Backups</b> opens the backup list. Select one backup to <b>Save Backup</b> (download a copy), <b>Restore Backup</b> or <b>Remove</b> it; <b>Add Backup</b> uploads a saved copy after checking it. Restoring pauses processing, makes a safety backup of the current state, checks the chosen backup again, replaces the current database and settings, and restarts DebridPulse's services. If the check fails, nothing changes.</li>
              <li><b>Database Reset Controls:</b> switch on <b>Allow Database Reset</b>, press <b>Reset Database</b>, type <code>WIPE</code> and press <b>Wipe Database</b>. This erases all downloads, history and events. DebridPulse always makes a safety backup first and stops if that backup fails. It stops running downloads and pauses processing for the reset, then restores the previous pause state afterwards. Finished files already in your download folder are not deleted.</li>
            </ul>`)}
        </div>
      </section>`;
  }

  function troubleshootingPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-trouble-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-trouble-heading">Troubleshooting</h2>
          <p>Start from what you see. Look before you change anything, and fix the cause before retrying.</p>
        </div>

        ${inset('Before changing anything', `
          <p>Open the download's details and read its <b>Status</b>, <b>Error</b>, <b>Route History</b> and <b>Events</b>. Then check the <b>Activity Log</b> around the same time. Together they usually show whether the problem was the service, the server, the download itself, your storage or extraction.</p>
          <p>Avoid removing a download just to clear an error: DebridPulse keeps its source and history so it can retry or recover. If you need help, <b>Transfer Trace Log</b> in the details saves a record without passwords or private link details. Read it before sharing, and never share API keys, tokens or links that contain passwords.</p>`)}

        <div class="dp-help-accordion-list">
          ${accordion('I added something, but nothing starts', `
            <ul>
              <li>Check for <b>Pause All</b> (the header shows <b>Resume All</b> and the Dashboard shows <b>PROCESSING PAUSED</b>). New downloads are accepted while paused but wait.</li>
              <li><b>Queued</b> or <b>Waiting for execution slot</b> means other downloads are using all <b>Maximum Concurrent Downloads</b>.</li>
              <li><b>Input Required</b> means DebridPulse is waiting for you: a password, a server identity check or a file selection. Open the download if no dialog is showing.</li>
              <li><b>Waiting for provider</b> means no enabled service can take it right now. Check that the service you expect is switched on, connected and showing green or amber in Provider Status.</li>
              <li><b>Waiting for storage</b> means free space is below <b>Minimum Free Disk Space</b>.</li>
            </ul>`, true)}

          ${accordion('Quick Add refuses a link or file', `
            <ul>
              <li><b>Line N: enter an HTTP(S), FTP, SFTP, SCP, SSH, rsync or WebDAV link or a magnet URI</b>: that line is not a supported link. Fix or remove it; nothing else was added.</li>
              <li>A batch over 100 links is refused; split it.</li>
              <li><b>Local network transfer not added</b>: you cancelled the local network question, or <b>Local Network Connections</b> is off.</li>
              <li>A file over its size limit is refused (16 MB for <code>.torrent</code> and <code>.meta4</code>, 1 GB for <code>.nzb</code>, 1 MB for a link list).</li>
              <li>A link list that points at items inside a collection must be added through Quick Add so you can choose what to download.</li>
            </ul>`)}

          ${accordion('A service is amber or red, or its Test fails', `
            <ul>
              <li><b>API key services</b> (AllDebrid, Debrid-Link, Premiumize): check the key on the service's website, paste it again and press <b>Test</b>.</li>
              <li><b>Real-Debrid and TorBox:</b> if the code expired, press <b>Connect</b> again. If the card is connected but the Test fails, <b>Disconnect</b> and connect again.</li>
              <li><b>Usenet:</b> test each server separately to find the one that fails, and check its host, port, SSL setting and sign-in.</li>
              <li>Amber after a plan change usually means a free or expired account: some or all features stop working until the plan is renewed.</li>
              <li><b>Finish or remove existing ... resources before changing its connection</b>: that service still has downloads in progress.</li>
            </ul>`)}

          ${accordion('A download stays in Processing for a long time', `
            <p>A torrent may take time while the debrid service fetches it; no files arrive during that time. Read the Events for a message from the service. If the service is healthy but the download seems out of step, use <b>Recover All</b>. If one service keeps failing, switch the download to another with its <b>Provider</b> control (single torrents) or let DebridPulse move on after its retries.</p>
            <p>How often DebridPulse checks premium services is set by <b>Provider Poll Interval</b> and <b>Full Sync Interval</b> on the AllDebrid card. Very short intervals only add traffic.</p>`)}

          ${accordion('A download is slow, stalls or fails', `
            <ul>
              <li>Check the speed cap in the header first.</li>
              <li>Read the error in the details. Temporary errors are retried automatically; a permanent one (for example a file that no longer exists) will not succeed on retry.</li>
              <li>If other sources are listed, you can switch the remaining files to one of them from the details.</li>
              <li>If the sidebar shows <b>aria2: offline</b>, the engine is not responding; restarting DebridPulse starts it again.</li>
              <li>For a job that is truly stuck, <b>Download Engine Activity</b> under Settings → Downloads can terminate it. Prefer <b>Retry</b> on the download itself.</li>
            </ul>`)}

          ${accordion('A server keeps asking for a password, or rejects its identity', `
            <p>A rejected password is never retried automatically; the prompt returns so you can correct it. A failed identity check (<b>Host Identity Failed</b>) means the server's fingerprint did not match: confirm with the server's owner before continuing, because it can mean you are not talking to the server you expect.</p>`)}

          ${accordion('A Usenet download fails', `
            <ul>
              <li><b>no enabled news server is configured</b>: add a server or switch one on.</li>
              <li>Missing articles usually mean your server no longer has the whole posting. Another server, or TorBox or Premiumize with NZB support, may help.</li>
              <li>Passworded archives inside a posting are unpacked with your <b>Archive Passwords</b>; add the password there.</li>
            </ul>`)}

          ${accordion('A media download fails', `
            <p>Media Downloads cannot sign in, so media that needs an account or subscription fails. Live or upcoming streams, region-blocked media, DRM-protected media and sites yt-dlp does not recognise are also not supported. The download's error names the reason.</p>`)}

          ${accordion('Files are not where I expect, or disk space runs out', `
            <ul>
              <li>Check <b>Download Folder</b> and, with Docker, how that folder is mapped to your real storage. Use <b>Browse</b> to see what DebridPulse can reach.</li>
              <li>DebridPulse must be allowed to create and change files there. Check the folder's permissions for the user DebridPulse runs as.</li>
              <li>When free space falls below <b>Minimum Free Disk Space</b>, new downloads wait until enough space, plus the <b>Resume Free Space Buffer</b>, is free again.</li>
            </ul>`)}

          ${accordion('Automatic extraction did not run or failed', `
            <p>Check that <b>Settings → Extraction → Automatic Extraction</b> is enabled; extraction only starts after the download is complete. Read the download's <b>Extraction</b> and <b>Extraction Error</b> fields. A protected archive needs its password in <b>Archive Passwords</b>, and the destination needs enough space. When extraction fails, the archive is kept so you can try again.</p>`)}

          ${accordion('Discord or Prometheus is not working', `
            <p>For Discord, press <b>Test</b> on the card, check the webhook still exists in Discord, and that the event you expect is switched on. For Prometheus, scrape <code>/api/metrics</code>; when sign-in is enabled, give the scraper an Integration token from <b>Users &amp; Access</b>.</p>`)}

          ${accordion('The page stops updating live', `
            <p>DebridPulse sends live updates over a long-lived connection (Server-Sent Events) and falls back to checking every 15 seconds if that fails. If updates are often late, a reverse proxy in front of DebridPulse is probably buffering or cutting the connection: it must allow long-running streamed responses. Reloading the page reconnects.</p>`)}

          ${accordion('I cannot sign in', `
            <p>If another sign-in method still works, use it and open <b>Settings → Authentication</b>. For OIDC, check that <b>Public DebridPulse Base URL</b> is the real HTTPS address with no extra path, that the identity service has the exact <b>OIDC Callback URL</b>, and press <b>Test</b>.</p>
            <p><b>Last resort, if no sign-in method works:</b> this needs access to the computer running DebridPulse and temporarily removes all protection, so do it only on a trusted network. Stop DebridPulse and back up its configuration file (normally <code>/app/config/config.json</code> inside the container). In that file set <code>auth_password_enabled</code> and <code>auth_oidc_enabled</code> to <code>false</code>, keeping the file valid JSON; DebridPulse refuses to start with a damaged configuration file. Start DebridPulse; it is now open to anyone who can reach it, including its API. Your stored password and OIDC settings are kept, so repair them in Settings → Authentication and switch protection back on straight away.</p>`)}

          ${accordion('Backup, restore or reset will not run', `
            <ul>
              <li><b>Backup is disabled in Settings</b>: switch on backups in <b>Backups &amp; Retention</b>.</li>
              <li>A reset needs <b>Allow Database Reset</b> switched on and <code>WIPE</code> typed in the confirmation.</li>
              <li>If the safety backup fails, the reset or restore stops. Fix the <b>Backup Folder</b> (location, permissions, free space); the safety backup cannot be skipped.</li>
            </ul>`)}
        </div>
      </section>`;
  }

  function licensePanel() {
    return `
      <section class="dp-help-document dp-help-license" aria-labelledby="dp-help-license-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-license-heading">Licensing and source</h2>
          <p>DebridPulse is open-source software distributed under the GNU General Public License v2.0 or later.</p>
        </div>

        <div class="dp-help-copy dp-help-copy--lead dp-help-prose">
          <p>DebridPulse modifications are Copyright &copy; 2026 Chris Moore and are distributed under the <b>GNU General Public License v2.0 or later</b> (<code>GPL-2.0-or-later</code>).</p>
          <p>In practical terms, the GPL allows you to use, study, modify, and redistribute DebridPulse under its license conditions. If you distribute a modified version, the GPL's source-code and licensing requirements apply to that distribution. The complete license text is the authority for the exact terms.</p>
          <p>DebridPulse is provided without warranty as described by the license.</p>
        </div>

        <article class="dp-help-inset">
          <h3>Why there are several legal documents</h3>
          <div class="dp-help-copy dp-help-prose">
            <ul>
              <li><b>GPL-2.0-or-later:</b> the complete license governing the DebridPulse modifications and distribution.</li>
              <li><b>Attribution notice:</b> identifies the project, upstream origin, copyright notices, and licensing relationship.</li>
              <li><b>Upstream MIT license:</b> preserves the license text that accompanied the upstream code DebridPulse is derived from.</li>
              <li><b>Source offer:</b> explains how to obtain the corresponding DebridPulse source associated with a distributed build.</li>
              <li><b>Third-party licenses:</b> records the runtime dependencies and other third-party components shipped with the application.</li>
            </ul>
          </div>
        </article>

        <div class="dp-help-license-actions">
          <button type="button" class="dp-btn dp-btn--primary dp-help-local-document-button" data-legal-document="gpl">Read GPL-2.0-or-later</button>
          <button type="button" class="dp-btn dp-btn--ghost dp-help-local-document-button" data-legal-document="notice">Attribution notice</button>
          <button type="button" class="dp-btn dp-btn--ghost dp-help-local-document-button" data-legal-document="upstream-mit">Upstream MIT license</button>
          <button type="button" class="dp-btn dp-btn--ghost dp-help-local-document-button" data-legal-document="source-offer">Source offer</button>
          <button type="button" class="dp-btn dp-btn--ghost dp-help-local-document-button" data-legal-document="third-party">Third-party licenses</button>
        </div>

        <div class="dp-help-copy dp-help-license-note dp-help-prose">
          <p>Those buttons open the exact legal documents bundled with the running DebridPulse build, so you can read the terms even without GitHub access. The document viewer also provides an explicit link to the latest repository copy when you want to compare the bundled snapshot with current project files.</p>
          <p>The complete license, notices, dependency inventory, and source offer are packaged in the DebridPulse container image.</p>
        </div>
      </section>`;
  }

  function canonicalDocumentMarkup(markup) {
    const template = document.createElement('template');
    template.innerHTML = String(markup || '').trim();
    const section = template.content.firstElementChild;
    if (!section || !section.classList.contains('dp-help-document')) return markup;

    section.classList.add('card', 'dp-help-section-card', 'dp-large-panel-surface');
    const heading = section.querySelector(':scope > .dp-help-section-heading');
    if (!heading) return template.innerHTML;

    const header = document.createElement('div');
    header.className = 'card-header dp-help-section-card-header';
    heading.before(header);
    header.appendChild(heading);

    const body = document.createElement('div');
    body.className = 'card-body dp-help-section-card-body';
    while (header.nextSibling) body.appendChild(header.nextSibling);
    section.appendChild(body);
    return template.innerHTML;
  }

  function panel(name, body) {
    const active = state.activeTab === name;
    return `
      <section class="dp-help-panel" id="dp-help-panel-${name}" data-panel="${name}" role="tabpanel"
               aria-labelledby="dp-help-tab-${name}" ${active ? '' : 'hidden'}>
        ${canonicalDocumentMarkup(body)}
      </section>`;
  }

  function activateTab(name, focus = false) {
    selectTabControl(name, focus);
    showTabPanel();
  }

  function selectTabControl(name, focus = false) {
    if (!TABS.some(([id]) => id === name)) name = 'quickstart';
    state.activeTab = name;

    root()?.querySelectorAll('.dp-help-tab').forEach(tab => {
      const active = tab.dataset.tab === name;
      tab.classList.toggle('is-active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
      tab.tabIndex = active ? 0 : -1;
      if (active && focus) tab.focus();
    });
  }

  // The panel of the tab selected NOW (a later choice made meanwhile wins).
  function showTabPanel() {
    const view = root();
    if (!view) return;
    view.querySelectorAll('.dp-help-panel').forEach(section => {
      section.hidden = section.dataset.panel !== state.activeTab;
    });

    const scroller = view.querySelector('.dp-help-scroll');
    if (scroller) scroller.scrollTop = 0;
  }

  /* The operator chose a section: the strip changes at once and the panel
   * slides across from the one on screen, by the tabs' displayed order. */
  function chooseTab(name, focus = false) {
    const view = root();
    const order = Array.from(view?.querySelectorAll('.dp-help-tab') || []).map(tab => tab.dataset.tab);
    const shown = view?.querySelector('.dp-help-panel:not([hidden])')?.dataset.panel;
    selectTabControl(name, focus);
    const from = order.indexOf(shown), to = order.indexOf(state.activeTab);
    const step = from >= 0 && to >= 0 ? to - from : 0;
    if (window.DPMotion) window.DPMotion.slide({element: view?.querySelector('.dp-help-scroll'), axis: 'x', direction: Math.sign(step), jump: Math.abs(step) > 1}, showTabPanel);
    else showTabPanel();
  }

  function bindEvents(view) {
    if (view.dataset.dpHelpEventsBound === '1') return;
    view.dataset.dpHelpEventsBound = '1';

    view.addEventListener('click', event => {
      const tab = event.target.closest('.dp-help-tab');
      if (!tab) return;
      chooseTab(tab.dataset.tab);
    });

    view.addEventListener('keydown', event => {
      const current = event.target.closest('.dp-help-tab');
      if (!current || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const tabs = Array.from(view.querySelectorAll('.dp-help-tab'));
      let index = tabs.indexOf(current);
      if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = tabs.length - 1;
      else index = (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
      chooseTab(tabs[index].dataset.tab, true);
    });
  }

  function render() {
    const view = root();
    if (!view) return;
    if (view.dataset.dpHelpRendered === '1') {
      activateTab(state.activeTab);
      return;
    }

    view.classList.add('dp-help-clean-view');

    const tabs = TABS.map(([id, label, icon]) => {
      const active = state.activeTab === id;
      return `
        <button class="dp-tab dp-help-tab${active ? ' is-active' : ''}" id="dp-help-tab-${id}"
                type="button" role="tab" data-tab="${id}"
                aria-controls="dp-help-panel-${id}"
                aria-selected="${active ? 'true' : 'false'}"
                tabindex="${active ? '0' : '-1'}">
          <span class="dp-help-tab-chip" aria-hidden="true"><img class="dp-help-tab-glyph" src="/icons/lucide/${icon}.svg" alt=""></span>
          <span class="dp-help-tab-label">${label}</span>
        </button>`;
    }).join('');

    view.innerHTML = `
      <section class="dp-card dp-help-master-card dp-list-workspace-surface" aria-label="Help & Documentation">
        <header class="dp-card__header dp-help-master-header">
          <div class="dp-help-header-copy">
            <img class="dp-help-title-icon" src="/icons/dp/document.svg" alt="" aria-hidden="true">
            <div class="dp-help-header-text">
              <div class="dp-help-header-title">Field Manual</div>
              <div class="dp-help-header-subtitle">When intuition fails.</div>
            </div>
          </div>
          <div class="dp-tabs dp-help-tabs" role="tablist" aria-label="Help sections">${tabs}</div>
        </header>
        <div class="dp-help-master-body">
          <div class="dp-help-scroll">
            <div class="dp-help-panels">
              ${panel('quickstart', quickStartPanel())}
              ${panel('using', usingPanel())}
              ${panel('howitworks', howItWorksPanel())}
              ${panel('services', servicesPanel())}
              ${panel('engines', enginesPanel())}
              ${panel('settings', settingsPanel())}
              ${panel('trouble', troubleshootingPanel())}
              ${panel('license', licensePanel())}
            </div>
          </div>
        </div>
      </section>`;

    view.dataset.dpHelpRendered = '1';
    bindEvents(view);
    activateTab(state.activeTab);
    document.dispatchEvent(new CustomEvent('debridpulse:help-rendered', {detail: {view: 'help'}}));
  }

  // The one Help entry (app.js navigation): every entry opens on Quick Start.
  function load() {
    state.activeTab = 'quickstart';
    render();
  }

  // app.js owns generic navigation and calls this canonical Help entry point.
  window.loadHelp = load;
  try { loadHelp = load; } catch (_) {}
  window.DPHelpPage = Object.freeze({load, activateTab});
})();