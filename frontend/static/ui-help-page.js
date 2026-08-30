/* DebridPulse v1.0.11 clean-room Help & Documentation page.
 *
 * Help deliberately does not consume the inherited Help tab markup or legacy
 * tab lifecycle. The inherited Help surface is retained only as the
 * source for the visible documentation copy reproduced below. This runtime
 * owns a new master-card/tab composition and performs no API or backend work.
 */
(function () {
  'use strict';

  const TABS = Object.freeze([
    ['quickstart', 'Quick Start'],
    ['howitworks', 'How it works'],
    ['aria2', 'aria2'],
    ['integrations', 'Integrations'],
    ['settings', 'Settings'],
    ['trouble', 'Troubleshooting'],
    ['license', 'License'],
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

  function quickStartPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-quickstart-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-quickstart-heading">Getting started with DebridPulse</h2>
          <p>A guided first setup for connecting AllDebrid, choosing where files are stored, and adding your first download.</p>
        </div>

        <div class="dp-help-copy dp-help-copy--lead dp-help-prose">
          <p><b>DebridPulse is the application you use to submit, track, and manage downloads.</b> AllDebrid is the online service that prepares the links, magnets, and torrent files you give to DebridPulse. A component called <b>aria2</b> then performs the actual file transfer and writes the files to your storage.</p>
          <p>DebridPulse includes its own built-in copy of aria2, so most users do not need to install or configure a separate download engine. For a basic setup, you normally only need an AllDebrid API key and a download location.</p>
        </div>

        <article class="dp-help-inset">
          <h3>Before you begin</h3>
          <div class="dp-help-copy dp-help-prose">
            <p>You will need an <b>AllDebrid account</b> and somewhere DebridPulse is allowed to save downloaded files. If DebridPulse was installed for you, those pieces may already be prepared.</p>
            <ul>
              <li><b>AllDebrid account:</b> DebridPulse currently uses AllDebrid as its debrid provider. A debrid provider is an online service that prepares supported downloads for you instead of making your DebridPulse system perform that work directly.</li>
              <li><b>Download storage:</b> This can be a folder on the computer running DebridPulse, a mounted NAS share, or another location made available to the application.</li>
              <li><b>Login protection:</b> If DebridPulse can be reached by people or devices you do not fully trust, configure <b>Settings → Authentication</b> before exposing it. Username &amp; Password is the simplest protection for most users.</li>
            </ul>
            <p>You do not need an external aria2 server, Discord notifications, automatic extraction, or OIDC just to perform a basic download. Those are optional features you can configure later.</p>
          </div>
        </article>

        <div class="dp-help-steps">
          ${step(1, 'Connect your AllDebrid account', `
            <div class="dp-help-prose">
              <p>Open <b>Settings → Sources &amp; Providers</b>, then find <b>Debrid Services → AllDebrid</b>.</p>
              <p>DebridPulse connects to AllDebrid using an <b>API key</b>. An API key is a private credential that lets DebridPulse use your AllDebrid account without storing your AllDebrid username and password. You can get your key from <a href="https://alldebrid.com/apikeys" target="_blank" rel="noopener">AllDebrid's API key page</a>.</p>
              <p>Paste the key into <b>API Key</b>, then click <b>Apply Settings</b>. Once a key is stored, leaving the field blank during a later settings change keeps the existing key unless you explicitly choose to clear it.</p>
              <p>The <b>Additional Settings</b> area contains provider polling, synchronization, rate-limit, and retry controls. The defaults are appropriate for a normal installation, so you do not need to change them just to get started.</p>
            </div>`) }

          ${step(2, 'Confirm where downloads will be stored', `
            <div class="dp-help-prose">
              <p>Open <b>Settings → Downloads → Download Engine</b>. The default mode is <b>Built-in aria2</b>, which is the recommended choice unless you already operate a separate aria2 server.</p>
              <p><b>aria2 is the component that performs the actual file transfer.</b> DebridPulse decides what should be downloaded, gets a usable file link from AllDebrid, and gives that link to aria2. Built-in mode means DebridPulse runs and manages aria2 for you.</p>
              <p>The <b>Built-in Download Folder</b> tells DebridPulse where files should be written. The normal container path is <code>/download</code>.</p>
              <p>If you installed DebridPulse with Docker, DebridPulse runs inside an isolated environment called a <b>container</b>. A path such as <code>/download</code> is the folder name as DebridPulse sees it inside that container. During installation, that folder is normally connected to a real folder on your computer, NAS, or server. For example, a folder named <code>/mnt/downloads</code> on the host system might be made available to DebridPulse as <code>/download</code>.</p>
              <p>If downloads are already appearing in the correct place, you do not need to change this path.</p>
              <p><b>Using an external aria2 server?</b> Choose <b>External aria2</b> only if you already have one. The <b>External RPC URL</b> is the network address DebridPulse uses to communicate with that aria2 server. The <b>External aria2 Download Path</b> is the download folder as that external aria2 system sees it. These values can differ from the paths visible inside the DebridPulse container.</p>
            </div>`) }

          ${step(3, 'Add your first download', `
            <div class="dp-help-prose">
              <p>Return to the <b>Dashboard</b>. The main Add field accepts the common source types DebridPulse supports.</p>
              <ul>
                <li><b>HTTP or HTTPS link:</b> A normal web link to content supported by AllDebrid. DebridPulse asks AllDebrid to turn it into a usable download link.</li>
                <li><b>Magnet link:</b> A special link that usually begins with <code>magnet:?</code> and describes torrent content without requiring a separate torrent file.</li>
                <li><b>.torrent file:</b> A small metadata file that describes torrent content. To choose one, leave the Add text field empty and use the same Add control to select the file.</li>
              </ul>
              <p>You can paste more than one direct link or magnet into the text field, one item per line. Direct-link batches can contain up to <b>100 unique links</b>.</p>
              <p>You do not need to manually unlock links on AllDebrid first. Submit the original source to DebridPulse and let DebridPulse manage the provider and download steps.</p>
            </div>`) }

          ${step(4, 'Understand what happens after you click Add', `
            <div class="dp-help-prose">
              <p>For a normal HTTP or HTTPS source, DebridPulse sends the source to AllDebrid. AllDebrid prepares or <b>unlocks</b> it, which means it produces a downloadable file link that DebridPulse can use. Some sources are ready immediately, while others need time to be generated.</p>
              <p>For a magnet or <code>.torrent</code> file, AllDebrid handles the torrent activity on its own service. When the files are ready, AllDebrid provides normal HTTP or HTTPS download links.</p>
              <p>DebridPulse then gives those downloadable links to aria2, which transfers the files to your configured storage. Your DebridPulse aria2 process does <b>not</b> need to join the torrent swarm.</p>
              <p>DebridPulse keeps the original source and the transfer history so it can retry or recover many problems later without making you enter the download again.</p>
            </div>`) }

          ${step(5, 'Follow and manage the transfer', `
            <div class="dp-help-prose">
              <p>DebridPulse separates current activity, full history, detailed events, and long-term statistics so each page stays focused.</p>
              <ul>
                <li><b>Dashboard:</b> Your at-a-glance view. Add new downloads here and watch current or recent activity.</li>
                <li><b>Downloads:</b> The complete transfer history and the main place to manage individual downloads. Use it when you need to inspect, pause, resume, retry, or review a specific transfer.</li>
                <li><b>Activity Log:</b> A chronological record of important application and transfer events. This is useful when you want to understand what DebridPulse did or why something changed state.</li>
                <li><b>Statistics:</b> Longer-term totals, success information, transfer volume, timing, and other historical summaries. It is intended for trends rather than active download control.</li>
              </ul>
              <p>If a download does not behave as expected, start with its details in <b>Downloads</b> and the <b>Activity Log</b>. The <b>Troubleshooting</b> tab in Help provides guided recovery steps for common problems.</p>
            </div>`) }
        </div>

        <article class="dp-help-inset">
          <h3>Useful settings to explore next</h3>
          <div class="dp-help-copy dp-help-prose">
            <p>Once your first download works, the remaining Settings sections can be added as you need them:</p>
            <ul>
              <li><b>Extraction:</b> Automatically unpack supported archives after a download finishes. This is optional and can also use stored archive passwords when needed.</li>
              <li><b>Notifications:</b> Send selected download events and statistics reports to Discord. Discord provides a special URL called a <b>webhook</b>; DebridPulse uses that URL to send messages to the channel you choose.</li>
              <li><b>Authentication:</b> Add Username &amp; Password, OpenID Connect, or both. OpenID Connect, usually shortened to OIDC, lets DebridPulse use a separate login service such as Authentik or Keycloak. If you do not already use an identity provider, Username &amp; Password is the simpler option.</li>
              <li><b>Data &amp; Maintenance:</b> Manage backups, retained application data, and maintenance operations. These controls matter more once you have been using DebridPulse for a while.</li>
            </ul>
          </div>
        </article>

        <div class="dp-help-integration-grid">
          <article class="dp-help-inset">
            <h3>Pause All stops processing, not intake</h3>
            <div class="dp-help-copy dp-help-prose">
              <p>You can still add links, magnets, and torrent files while <b>Pause All</b> is active. DebridPulse records the submissions locally but does not begin new AllDebrid or aria2 work until processing is resumed.</p>
              <p>This lets you build a queue without losing what you submitted.</p>
            </div>
          </article>
          <article class="dp-help-inset">
            <h3>Most failed transfers can be investigated or retried</h3>
            <div class="dp-help-copy dp-help-prose">
              <p>DebridPulse keeps source and transfer information so many failures can be retried without starting over. Individual controls are available in <b>Downloads</b>, while <b>Recover All</b> asks DebridPulse to check its tracked work and recover transfers that can safely continue.</p>
              <p>If recovery does not solve the problem, the transfer details and Activity Log usually provide the next clue.</p>
            </div>
          </article>
        </div>
      </section>`;
  }

  function howItWorksPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-howitworks-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-howitworks-heading">The download pipeline</h2>
        </div>
        <div class="dp-help-copy dp-help-copy--lead">
          <p>DebridPulse sits between your download sources and your disk. It never downloads directly from peers; your debrid provider's cloud does. You get the finished file via an unlocked HTTPS link.</p>
        </div>
        <div class="dp-help-pipeline">
          ${pipeline('Magnet / .torrent', 'Uploaded to AllDebrid. <b>status: uploading</b>')}
          ${pipeline('AllDebrid processes', 'Polls every 30 s. <b>status: processing</b>')}
          ${pipeline('Ready on AllDebrid', 'Links unlocked, handed to aria2. <b>status: downloading</b>', 'active')}
          ${pipeline('aria2 complete', 'Magnet cleaned up on AllDebrid and the local transfer is marked <b>completed</b>.', 'success')}
        </div>
        <div class="dp-help-copy dp-help-copy--after">
          <p><b>Error auto-recovery:</b> Upload failures (code 5) and "no peers" (code 8) are retried automatically up to <code>upload_fail_retry_count</code> times. Stalled downloads are reset after <code>stuck_download_timeout_hours</code> so they can be dispatched again. Use <b>⟳ Recover All</b> to manually trigger a full recovery pass.</p>
        </div>
      </section>`;
  }

  function aria2Panel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-aria2-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-aria2-heading">aria2: the download engine</h2>
        </div>
        <div class="dp-help-copy dp-help-prose">
          <p>DebridPulse uses <b>aria2</b> as the actual HTTP download engine. Two modes are supported:</p>
          <h3>Built-in (default)</h3>
          <p>aria2 runs inside the container. No external setup needed. Configure options in <b>Settings → Download Client → aria2 Config</b>. The process is managed by the client and restarts automatically.</p>
          <h3>External RPC</h3>
          <p>Point to an existing aria2 instance by providing the JSON-RPC URL (e.g. <code>http://aria2:6800/jsonrpc</code>) and an optional secret token. Useful for shared aria2 instances or custom setups.</p>
          <h3>Performance tips</h3>
          <ul>
            <li><b>Max concurrent downloads</b> controls how many torrents are processed in parallel (default: 3). Higher values are faster but use more RAM and bandwidth.</li>
            <li><b>Connections per file</b> (<code>split</code>): more connections = faster single-file downloads. 8-16 is typically optimal.</li>
            <li><b>Speed limits</b>: set in <b>Downloads → ↓ Limit</b> in the header badge, or via Settings.</li>
            <li>Enable <b>Auto-memory tuning</b> to let the client adjust aria2's cache based on available RAM.</li>
          </ul>
        </div>
      </section>`;
  }

  function integrationsPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-integrations-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-integrations-heading">Integrations</h2>
        </div>
        <div class="dp-help-integration-grid">
          <article class="dp-help-inset">
            <h3>🔔 Discord Notifications</h3>
            <div class="dp-help-copy">Enter a Discord webhook URL in <b>Settings → Notifications</b>. Fine-grained toggles let you choose exactly which events trigger a notification: completed, error, upload failed, no peers, and periodic statistics reports.</div>
          </article>
          <article class="dp-help-inset">
            <h3>📊 Prometheus Metrics</h3>
            <div class="dp-help-copy">A Prometheus-compatible scrape endpoint is available at <code>GET /api/metrics</code>. Metrics include torrent counts by status, active downloads, errors, SSE subscriber count, and total bytes downloaded. Add to your Prometheus config with <code>metrics_path: /api/metrics</code>.</div>
          </article>
        </div>
      </section>`;
  }

  function settingsPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-settings-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-settings-heading">Settings reference</h2>
        </div>
        <div class="dp-help-accordion-list">
          <details class="dp-help-accordion" open>
            <summary>General</summary>
            <div class="dp-help-accordion-body dp-help-copy">
              <p><b>AllDebrid API Key</b> - Required. Get at alldebrid.com/apikeys.</p>
              <p><b>Access Control</b> - Optional HTTP Basic Auth. Set both username and password to enable. Leave either empty to disable. Exempt paths: /api/health, /api/version, /api/avatar.</p>
            </div>
          </details>
          <details class="dp-help-accordion">
            <summary>Download Client</summary>
            <div class="dp-help-accordion-body dp-help-copy">
              <p><b>Max Concurrent Downloads</b> - How many torrents are processed (unlocked + dispatched) simultaneously. Default: 3.</p>
              <p><b>Built-in aria2 Download Folder</b> - Local path used by DebridPulse and built-in aria2.</p>
              <p><b>External aria2 Download Path</b> - Path to that download location as seen by an external aria2 daemon.</p>
              <p><b>Min Free Disk Space (GB)</b> - If less than this is available, active transfers are allowed to finish while new dispatches are deferred until space recovers. 0 = disabled.</p>
              <p><b>Stalled download timeout (hours)</b> - Downloads left queued or downloading without an update longer than this are reset for retry. 0 = disabled.</p>
            </div>
          </details>
          <details class="dp-help-accordion">
            <summary>AllDebrid API</summary>
            <div class="dp-help-accordion-body dp-help-copy">
              <p><b>API calls per minute</b> - Rate limit for AllDebrid API calls (token-bucket). Default: 60. 0 = unlimited.</p>
              <p><b>Upload fail retries</b> - How many times to retry a "Upload failed" (code 5) magnet. Default: 3.</p>
              <p><b>Retry delay (minutes)</b> - Wait between upload-failed retry attempts. Default: 5.</p>
            </div>
          </details>
          <details class="dp-help-accordion">
            <summary>Notifications</summary>
            <div class="dp-help-accordion-body dp-help-copy">
              <p><b>Discord Webhook URL</b> - Main webhook for all notifications.</p>
              <p>Per-event toggles control which events trigger a notification independently.</p>
            </div>
          </details>
          <details class="dp-help-accordion">
            <summary>Advanced / Maintenance</summary>
            <div class="dp-help-accordion-body dp-help-copy">
              <p><b>Event Log Retention (days)</b> - Events older than this are deleted daily. Torrent rows are never deleted; duplicate prevention is unaffected. 0 = keep forever.</p>
              <p><b>Backup</b> - Automatic JSON backups of the database. Configurable interval and retention.</p>
              <p><b>Database</b> - DebridPulse uses an internal SQLite database with WAL mode.</p>
            </div>
          </details>
        </div>
      </section>`;
  }

  function troubleshootingPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-trouble-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-trouble-heading">Troubleshooting</h2>
        </div>
        <div class="dp-help-accordion-list">
          <details class="dp-help-accordion" open>
            <summary>Torrents are not downloading / stuck at "processing"</summary>
            <div class="dp-help-accordion-body dp-help-copy">
              <p>1. Check the event log for the torrent (click the row → Events).</p>
              <p>2. Click <b>⟳ Recover All</b> in the Downloads view; this resets stalled transfers and re-dispatches ready AllDebrid downloads.</p>
              <p>3. Open <code>/api/torrents/diagnose</code> to see exact status counts and a sample of non-terminal torrents with file counts.</p>
              <p>4. Verify the AllDebrid API key is valid: Settings → AllDebrid → Test.</p>
            </div>
          </details>
          <details class="dp-help-accordion">
            <summary>Downloads start but files are never written to disk</summary>
            <div class="dp-help-accordion-body dp-help-copy">
              <p>1. Check the <b>Built-in aria2 Download Folder</b> path; it must be accessible inside the container.</p>
              <p>2. In Unraid: ensure the path mapping in the Docker template is correct (host path → container path).</p>
              <p>3. If using an external aria2 instance, set the <b>External aria2 Download Path</b> to the path seen by that daemon (it may differ from the DebridPulse container path).</p>
              <p>4. Check the <code>min_free_disk_gb</code> setting; when the guard is active, new downloads are deferred until free space recovers.</p>
            </div>
          </details>
          <details class="dp-help-accordion">
            <summary>Upload Failed / No peers errors</summary>
            <div class="dp-help-accordion-body dp-help-copy">
              <p><b>Upload Failed (code 5):</b> AllDebrid rejected the upload. The client retries automatically (up to <code>upload_fail_retry_count</code> times). If retries are exhausted the torrent is marked error; use the <b>↻</b> retry button on the row or <b>⟳ Recover All</b>.</p>
              <p><b>No peers (code 8):</b> The torrent has no seeders. The client deletes the magnet from AllDebrid and re-uploads automatically if a magnet link is stored. If the torrent was added via .torrent file (no magnet stored), you must re-add it manually.</p>
            </div>
          </details>
          <details class="dp-help-accordion">
            <summary>Live updates not working (no SSE)</summary>
            <div class="dp-help-accordion-body dp-help-copy">
              <p>The UI uses Server-Sent Events (SSE) for live updates instead of polling. If you see stale data:</p>
              <p>1. Check the browser console for <code>EventSource</code> errors.</p>
              <p>2. If you use a reverse proxy (nginx, Traefik), ensure it does not buffer responses. For nginx add <code>proxy_buffering off;</code> and <code>proxy_read_timeout 3600s;</code> to the DebridPulse location block.</p>
              <p>3. The client falls back to 15-second polling automatically if SSE fails.</p>
            </div>
          </details>
          <details class="dp-help-accordion">
            <summary>Auth is enabled but I'm locked out</summary>
            <div class="dp-help-accordion-body dp-help-copy">
              <p>Edit the configured settings file (documented container default: <code>/app/config/config.json</code>) and set <code>"auth_username": ""</code> and <code>"auth_password": ""</code>, then restart the container. Auth is disabled when either field is empty.</p>
            </div>
          </details>
        </div>
      </section>`;
  }

  function licensePanel() {
    return `
      <section class="dp-help-document dp-help-license" aria-labelledby="dp-help-license-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-license-heading">DebridPulse licensing</h2>
        </div>
        <div class="dp-help-copy dp-help-prose">
          <p>DebridPulse modifications are Copyright &copy; 2026 Chris Moore and are distributed under the <b>GNU General Public License v2.0 or later</b> (<code>GPL-2.0-or-later</code>).</p>
          <p>You may redistribute and modify DebridPulse under those terms. It is provided without warranty; see the complete license for the governing conditions and disclaimer.</p>
        </div>
        <div class="dp-help-inset dp-help-attribution dp-help-copy">
          This application is derived from
          <a href="https://github.com/kroeberd/alldebrid-client/tree/c0f7a5bfeba4f259fb2acc62ac6eed27e8ac4d5c" target="_blank" rel="noopener">kroeberd/alldebrid-client v1.9.9</a>
          (commit <code>c0f7a5bfeba4f259fb2acc62ac6eed27e8ac4d5c</code>),
          Copyright &copy; 2026 kroeberd, originally distributed under the MIT License.
          The upstream copyright and permission notice are retained with DebridPulse.
        </div>
        <div class="dp-help-license-actions">
          <a class="dp-btn dp-btn--primary" href="https://github.com/Xipher-Zero/debridpulse/blob/main/LICENSE" target="_blank" rel="noopener">Read GPL-2.0-or-later</a>
          <a class="dp-btn dp-btn--ghost" href="https://github.com/Xipher-Zero/debridpulse/blob/main/NOTICE" target="_blank" rel="noopener">Attribution notice</a>
          <a class="dp-btn dp-btn--ghost" href="https://github.com/Xipher-Zero/debridpulse/blob/main/LICENSES/MIT.txt" target="_blank" rel="noopener">Upstream MIT license</a>
          <a class="dp-btn dp-btn--ghost" href="https://github.com/Xipher-Zero/debridpulse/blob/main/SOURCE_OFFER.md" target="_blank" rel="noopener">Source offer</a>
          <a class="dp-btn dp-btn--ghost" href="https://github.com/Xipher-Zero/debridpulse/blob/main/docs/DEPENDENCY_LICENSES.md" target="_blank" rel="noopener">Third-party licenses</a>
        </div>
        <p class="dp-help-license-note">The complete license, notices, dependency inventory, and source offer are also packaged in the DebridPulse container image.</p>
      </section>`;
  }

  function panel(name, body) {
    const active = state.activeTab === name;
    return `
      <section class="dp-help-panel" id="dp-help-panel-${name}" data-panel="${name}" role="tabpanel"
               aria-labelledby="dp-help-tab-${name}" ${active ? '' : 'hidden'}>
        ${body}
      </section>`;
  }

  function activateTab(name, focus = false) {
    if (!TABS.some(([id]) => id === name)) name = 'quickstart';
    state.activeTab = name;

    const view = root();
    if (!view) return;

    view.querySelectorAll('.dp-help-tab').forEach(tab => {
      const active = tab.dataset.tab === name;
      tab.classList.toggle('is-active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
      tab.tabIndex = active ? 0 : -1;
      if (active && focus) tab.focus();
    });

    view.querySelectorAll('.dp-help-panel').forEach(section => {
      section.hidden = section.dataset.panel !== name;
    });

    const scroller = view.querySelector('.dp-help-scroll');
    if (scroller) scroller.scrollTop = 0;
  }

  function bindEvents(view) {
    if (view.dataset.dpHelpEventsBound === '1') return;
    view.dataset.dpHelpEventsBound = '1';

    view.addEventListener('click', event => {
      const tab = event.target.closest('.dp-help-tab');
      if (!tab) return;
      activateTab(tab.dataset.tab);
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
      activateTab(tabs[index].dataset.tab, true);
    });
  }

  function render() {
    const view = root();
    if (!view) return;

    view.classList.add('dp-help-clean-view');

    const tabs = TABS.map(([id, label]) => {
      const active = state.activeTab === id;
      return `
        <button class="dp-tab dp-help-tab${active ? ' is-active' : ''}" id="dp-help-tab-${id}"
                type="button" role="tab" data-tab="${id}"
                aria-controls="dp-help-panel-${id}"
                aria-selected="${active ? 'true' : 'false'}"
                tabindex="${active ? '0' : '-1'}">${label}</button>`;
    }).join('');

    view.innerHTML = `
      <section class="dp-card dp-help-master-card" aria-label="Help & Documentation">
        <header class="dp-card__header dp-help-master-header">
          <div class="dp-help-header-copy">
            <img class="dp-help-title-icon" src="/icons/dp/document.svg" alt="" aria-hidden="true">
            <div class="dp-help-header-title">Help &amp; Documentation</div>
          </div>
          <div class="dp-tabs dp-help-tabs" role="tablist" aria-label="Help sections">${tabs}</div>
        </header>
        <div class="dp-help-master-body">
          <div class="dp-help-scroll">
            <div class="dp-help-panels">
              ${panel('quickstart', quickStartPanel())}
              ${panel('howitworks', howItWorksPanel())}
              ${panel('aria2', aria2Panel())}
              ${panel('integrations', integrationsPanel())}
              ${panel('settings', settingsPanel())}
              ${panel('trouble', troubleshootingPanel())}
              ${panel('license', licensePanel())}
            </div>
          </div>
        </div>
      </section>`;

    bindEvents(view);
    activateTab(state.activeTab);
  }

  function init() {
    if (!root()) return;
    render();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
