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
          <h2 id="dp-help-quickstart-heading">Five steps to your first download</h2>
          <p>Complete these steps once and everything runs automatically from then on.</p>
        </div>
        <div class="dp-help-steps">
          ${step(1, 'Enter your AllDebrid API key', 'Go to <b>Settings → AllDebrid</b> and paste your API key. Get it at <a href="https://alldebrid.com/apikeys" target="_blank" rel="noopener">alldebrid.com/apikeys</a>. Click <b>Save</b>.')}
          ${step(2, 'Set the download folder', 'Go to <b>Settings → Download</b> and set the <b>Built-in aria2 Download Folder</b> to an existing path accessible by the container (the documented container mount is <code>/download</code>).')}
          ${step(3, 'Configure aria2', 'By default aria2 runs <b>built-in</b> inside the container — no extra setup. For an external aria2 instance, set <b>aria2 Mode</b> to <b>External aria2</b> in <b>Settings → Download</b> and provide the RPC URL.')}
          ${step(4, 'Add a torrent', 'Submit a direct link, paste a magnet link, or upload a <code>.torrent</code> file from the Dashboard.')}
          ${step(5, 'Watch it download', 'The <b>Dashboard</b> shows live progress. The client polls AllDebrid every 30 s, fetches unlocked links, and hands them to aria2. Completed torrents are removed from AllDebrid automatically.')}
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
          <p>DebridPulse sits between your download sources and your disk. It never downloads directly from peers — your debrid provider's cloud does. You get the finished file via an unlocked HTTPS link.</p>
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
          <h2 id="dp-help-aria2-heading">aria2 — the download engine</h2>
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
            <li><b>Connections per file</b> (<code>split</code>): more connections = faster single-file downloads. 8–16 is typically optimal.</li>
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
              <p><b>AllDebrid API Key</b> — Required. Get at alldebrid.com/apikeys.</p>
              <p><b>Access Control</b> — Optional HTTP Basic Auth. Set both username and password to enable. Leave either empty to disable. Exempt paths: /api/health, /api/version, /api/avatar.</p>
            </div>
          </details>
          <details class="dp-help-accordion">
            <summary>Download Client</summary>
            <div class="dp-help-accordion-body dp-help-copy">
              <p><b>Max Concurrent Downloads</b> — How many torrents are processed (unlocked + dispatched) simultaneously. Default: 3.</p>
              <p><b>Built-in aria2 Download Folder</b> — Local path used by DebridPulse and built-in aria2.</p>
              <p><b>External aria2 Download Path</b> — Path to that download location as seen by an external aria2 daemon.</p>
              <p><b>Min Free Disk Space (GB)</b> — If less than this is available, active transfers are allowed to finish while new dispatches are deferred until space recovers. 0 = disabled.</p>
              <p><b>Stalled download timeout (hours)</b> — Downloads left queued or downloading without an update longer than this are reset for retry. 0 = disabled.</p>
            </div>
          </details>
          <details class="dp-help-accordion">
            <summary>AllDebrid API</summary>
            <div class="dp-help-accordion-body dp-help-copy">
              <p><b>API calls per minute</b> — Rate limit for AllDebrid API calls (token-bucket). Default: 60. 0 = unlimited.</p>
              <p><b>Upload fail retries</b> — How many times to retry a "Upload failed" (code 5) magnet. Default: 3.</p>
              <p><b>Retry delay (minutes)</b> — Wait between upload-failed retry attempts. Default: 5.</p>
            </div>
          </details>
          <details class="dp-help-accordion">
            <summary>Notifications</summary>
            <div class="dp-help-accordion-body dp-help-copy">
              <p><b>Discord Webhook URL</b> — Main webhook for all notifications.</p>
              <p>Per-event toggles control which events trigger a notification independently.</p>
            </div>
          </details>
          <details class="dp-help-accordion">
            <summary>Advanced / Maintenance</summary>
            <div class="dp-help-accordion-body dp-help-copy">
              <p><b>Event Log Retention (days)</b> — Events older than this are deleted daily. Torrent rows are never deleted — duplicate prevention is unaffected. 0 = keep forever.</p>
              <p><b>Backup</b> — Automatic JSON backups of the database. Configurable interval and retention.</p>
              <p><b>Database</b> — DebridPulse uses an internal SQLite database with WAL mode.</p>
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
              <p>2. Click <b>⟳ Recover All</b> in the Downloads view — this resets stalled transfers and re-dispatches ready AllDebrid downloads.</p>
              <p>3. Open <code>/api/torrents/diagnose</code> to see exact status counts and a sample of non-terminal torrents with file counts.</p>
              <p>4. Verify the AllDebrid API key is valid: Settings → AllDebrid → Test.</p>
            </div>
          </details>
          <details class="dp-help-accordion">
            <summary>Downloads start but files are never written to disk</summary>
            <div class="dp-help-accordion-body dp-help-copy">
              <p>1. Check the <b>Built-in aria2 Download Folder</b> path — it must be accessible inside the container.</p>
              <p>2. In Unraid: ensure the path mapping in the Docker template is correct (host path → container path).</p>
              <p>3. If using an external aria2 instance, set the <b>External aria2 Download Path</b> to the path seen by that daemon (it may differ from the DebridPulse container path).</p>
              <p>4. Check the <code>min_free_disk_gb</code> setting — when the guard is active, new downloads are deferred until free space recovers.</p>
            </div>
          </details>
          <details class="dp-help-accordion">
            <summary>Upload Failed / No peers errors</summary>
            <div class="dp-help-accordion-body dp-help-copy">
              <p><b>Upload Failed (code 5):</b> AllDebrid rejected the upload. The client retries automatically (up to <code>upload_fail_retry_count</code> times). If retries are exhausted the torrent is marked error — use the <b>↻</b> retry button on the row or <b>⟳ Recover All</b>.</p>
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
