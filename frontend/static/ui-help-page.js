/* DebridPulse v1.0.11 clean-room Help & Documentation page.
 *
 * Help deliberately does not consume the inherited Help tab markup or legacy
 * tab lifecycle. This runtime owns the current user-facing documentation,
 * master-card/tab composition, and performs no API or backend work.
 */
(function () {
  'use strict';

  const TABS = Object.freeze([
    ['quickstart', 'Quick Start', 'rocket'],
    ['howitworks', 'How it works', 'workflow'],
    ['aria2', 'Download Engine', 'download'],
    ['integrations', 'Integrations', 'plug'],
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
          <h2 id="dp-help-howitworks-heading">How DebridPulse moves a download</h2>
          <p>From the source you submit to the file written on disk, DebridPulse keeps each stage separate so it can pause, retry, recover, and explain what happened.</p>
        </div>

        <div class="dp-help-copy dp-help-copy--lead dp-help-prose">
          <p><b>A source is the link, magnet, or torrent file you give to DebridPulse.</b> DebridPulse records that source, asks AllDebrid to prepare downloadable files, then hands the resulting file links to aria2 for the physical transfer to your storage.</p>
          <p>This separation matters because a provider can still be preparing content even though no local bytes are moving yet, and a local download can fail even after AllDebrid has finished its part.</p>
        </div>

        <div class="dp-help-pipeline">
          ${pipeline('1. Intake', 'DebridPulse accepts the HTTP/HTTPS link, magnet, or .torrent file and creates tracked work before provider or download-engine activity begins.')}
          ${pipeline('2. Provider preparation', 'AllDebrid unlocks a direct source or processes torrent content until downloadable file links are available.')}
          ${pipeline('3. Transfer planning', 'DebridPulse turns the provider result into the files it should deliver, reconciles duplicates, and can keep verified alternate mirror links available as standby sources.')}
          ${pipeline('4. aria2 delivery', 'Built-in or external aria2 downloads the prepared HTTP/HTTPS file links to the configured storage.', 'active')}
          ${pipeline('5. Verification and finish', 'DebridPulse reconciles the physical result, preserves useful history, and marks the logical download complete only when its required local work is satisfied.', 'success')}
          ${pipeline('6. Optional extraction', 'If Automatic Extraction is enabled and the completed files include supported archives, extraction runs after the physical download stage.')}
        </div>

        <div class="dp-help-integration-grid">
          <article class="dp-help-inset">
            <h3>What the common states mean</h3>
            <div class="dp-help-copy dp-help-prose">
              <ul>
                <li><b>Queued:</b> DebridPulse knows about the work, but it is waiting for an available stage or slot.</li>
                <li><b>Processing:</b> Provider-side work is still happening or being reconciled.</li>
                <li><b>Downloading:</b> aria2 is transferring the physical file data.</li>
                <li><b>Paused:</b> The tracked work is intentionally held and should not advance until resumed.</li>
                <li><b>Extracting:</b> Downloading has finished and optional archive post-processing is running.</li>
                <li><b>Completed:</b> The required local delivery work finished successfully.</li>
                <li><b>Error:</b> A provider, transfer, extraction, or recovery step needs attention or exhausted its allowed retries.</li>
              </ul>
              <p>The exact path through those states depends on the source type. A direct link may be ready almost immediately, while torrent content can spend more time in provider-side processing first.</p>
            </div>
          </article>

          <article class="dp-help-inset">
            <h3>Mirrors, retries, and recovery</h3>
            <div class="dp-help-copy dp-help-prose">
              <p>If you submit multiple direct links that DebridPulse can verify are mirrors of the same file, it can treat them as one logical download instead of downloading the same file repeatedly. Unused verified mirrors may remain available as standby sources.</p>
              <p>When a source-specific failure occurs, DebridPulse can promote an eligible standby mirror. Transfer errors and stalled work can also be retried according to the settings under <b>Download Safety &amp; Recovery</b>.</p>
              <p><b>Recover All</b> performs a broader reconciliation of tracked work. It is useful when you want DebridPulse to re-check provider and aria2 state without deleting and re-adding everything.</p>
            </div>
          </article>
        </div>

        <article class="dp-help-inset">
          <h3>Pause All is a processing gate</h3>
          <div class="dp-help-copy dp-help-prose">
            <p>Pause All does not reject new submissions. DebridPulse still records links, magnets, and torrent files so they are not lost, but it defers new provider and aria2 work until processing resumes.</p>
            <p>Individual pause and resume controls operate on specific tracked downloads. A paused item does not need to occupy an active download slot while it is being held.</p>
          </div>
        </article>
      </section>`;
  }

  function aria2Panel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-aria2-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-aria2-heading">aria2 and download delivery</h2>
          <p>aria2 is the transfer engine that writes the prepared files to your storage. DebridPulse can manage its own built-in engine or connect to an aria2 server you already operate.</p>
        </div>

        <div class="dp-help-copy dp-help-copy--lead dp-help-prose">
          <p>Configure the engine under <b>Settings → Downloads → Download Engine</b>. Most installations should leave <b>Built-in aria2</b> selected. External mode is intended for users who already have a reason to operate a separate aria2 service.</p>
        </div>

        <div class="dp-help-integration-grid">
          <article class="dp-help-inset">
            <h3>Built-in aria2</h3>
            <div class="dp-help-copy dp-help-prose">
              <p>Built-in mode runs aria2 with DebridPulse. DebridPulse starts it, applies the built-in engine settings, and reconnects to its own tracked downloads during normal reconciliation and recovery.</p>
              <p><b>Built-in Download Folder</b> is the path visible inside the DebridPulse container. The normal path is <code>/download</code>, which should be mapped to the host, NAS, or server folder where you actually want files stored.</p>
              <p>If built-in downloads already land in the correct place, there is usually no reason to change the path or the advanced tuning values.</p>
            </div>
          </article>

          <article class="dp-help-inset">
            <h3>External aria2</h3>
            <div class="dp-help-copy dp-help-prose">
              <p>External mode connects over aria2's JSON-RPC interface. Enter the <b>External RPC URL</b>, normally ending in <code>/jsonrpc</code>, and the <b>aria2 RPC Secret</b> if your server requires one.</p>
              <p><b>External aria2 Download Path</b> is the destination path as the external aria2 server sees it. DebridPulse and the external daemon must agree on the underlying storage even when their path names are different.</p>
              <p>DebridPulse limits external control to jobs it owns. It does not use external mode as a general administration interface for unrelated aria2 jobs or daemon-wide policy.</p>
            </div>
          </article>
        </div>

        <article class="dp-help-inset">
          <h3>Engine tuning in plain language</h3>
          <div class="dp-help-copy dp-help-prose">
            <p>The built-in engine exposes additional tuning. The defaults are a sensible starting point, and changing every value is not a requirement for good performance.</p>
            <ul>
              <li><b>Maximum Concurrent Downloads:</b> the maximum number of physical downloads DebridPulse can run at the same time.</li>
              <li><b>Continue Partial Downloads:</b> lets aria2 resume usable partial files instead of starting from zero when possible.</li>
              <li><b>Segments per File:</b> how many pieces aria2 may divide one file into for parallel downloading.</li>
              <li><b>Connections per Server:</b> caps how many simultaneous connections one download may open to the same server.</li>
              <li><b>Minimum Split Size:</b> prevents aria2 from dividing a file into excessively small pieces.</li>
              <li><b>Disk Cache:</b> allows aria2 to buffer download data in memory to reduce disk I/O.</li>
              <li><b>File Allocation:</b> controls how disk space is prepared for a new file before and during the transfer.</li>
              <li><b>Lowest Speed Limit:</b> can stop a connection that stays at or below a configured low-speed threshold. A value of 0 disables that threshold.</li>
            </ul>
            <p>If you are troubleshooting performance, change one setting at a time. More segments or connections do not guarantee more speed because the remote host, network, storage, and provider can each become the limiting factor.</p>
          </div>
        </article>

        <div class="dp-help-integration-grid">
          <article class="dp-help-inset">
            <h3>Speed cap</h3>
            <div class="dp-help-copy dp-help-prose">
              <p>The speed-cap control in the application header is the normal way to place an operational limit on active downloads. It is separate from the deeper per-file engine tuning.</p>
              <p>Use the cap when you want to temporarily reserve bandwidth for something else without redesigning your aria2 settings.</p>
            </div>
          </article>

          <article class="dp-help-inset">
            <h3>Download Safety &amp; Recovery</h3>
            <div class="dp-help-copy dp-help-prose">
              <p>The Downloads settings also contain protections around the engine. The free-space guard can defer new downloads before storage fills, the resume buffer prevents rapid stop/start behavior near that threshold, and stalled-download recovery can re-check work that has stopped making progress.</p>
              <p>Download Error Retries and Retry Delay control how DebridPulse responds when aria2 reports a transfer error.</p>
            </div>
          </article>
        </div>

        <article class="dp-help-inset">
          <h3>Testing an aria2 configuration</h3>
          <div class="dp-help-copy dp-help-prose">
            <p>While the Downloads tab is open, use <b>Test aria2</b> in the Settings footer. Connection tests use the values currently entered in the form and do not require you to save a bad configuration first.</p>
            <p>After the test succeeds, use <b>Apply Settings</b> to make the new engine configuration persistent.</p>
          </div>
        </article>
      </section>`;
  }

  function integrationsPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-integrations-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-integrations-heading">Connect DebridPulse to other services</h2>
          <p>These integrations are optional. Add only the ones that fit how you operate DebridPulse.</p>
        </div>

        <div class="dp-help-integration-grid">
          <article class="dp-help-inset">
            <h3>Discord Notifications</h3>
            <div class="dp-help-copy dp-help-prose">
              <p>Open <b>Settings → Notifications → Discord Notifications</b> and provide a Discord webhook. A webhook is a private URL Discord creates for a channel so another application can post messages there.</p>
              <p>You can choose notifications for downloads being added, completed downloads, errors, extraction results, and DebridPulse update availability. Display Name and Avatar settings control how the sender appears in Discord.</p>
              <p>An optional <b>Added-event Webhook</b> can route new-download messages separately. If you do not need that split, leave it blank and use the primary webhook.</p>
              <p>Use <b>Test Discord</b> before applying changes. The test uses the current draft values without silently saving them.</p>
            </div>
          </article>

          <article class="dp-help-inset">
            <h3>Statistics Reports</h3>
            <div class="dp-help-copy dp-help-prose">
              <p>The <b>Statistics Reports</b> card can send periodic summary reports to Discord. You can provide a dedicated Reporting Webhook or use the supported fallback to the primary Discord destination.</p>
              <p><b>Automatic Report Interval</b> controls how often reports are sent. Set it to 0 when you do not want scheduled reports. <b>Report Window</b> controls how much history each report summarizes.</p>
              <p><b>Send Test Report</b> lets you verify the report format and destination before relying on scheduled delivery.</p>
            </div>
          </article>

          <article class="dp-help-inset">
            <h3>Prometheus Metrics</h3>
            <div class="dp-help-copy dp-help-prose">
              <p>DebridPulse exposes Prometheus-compatible metrics at <code>GET /api/metrics</code>. Configure your Prometheus job with <code>metrics_path: /api/metrics</code> and the DebridPulse host and port as the scrape target.</p>
              <p>The endpoint reports operational information such as download states, active work, error counts, transferred bytes, and other application metrics useful for dashboards and alerting.</p>
              <p>If DebridPulse authentication is enabled, <code>/api/metrics</code> is protected too. Use a machine credential such as the bearer token from <b>Settings → Authentication → API Access</b> instead of weakening browser authentication for the scraper.</p>
            </div>
          </article>

          <article class="dp-help-inset">
            <h3>API Access</h3>
            <div class="dp-help-copy dp-help-prose">
              <p><b>Settings → Authentication → API Access</b> can create a dedicated bearer token for automation and API clients. The generated token is displayed once, so copy it when DebridPulse shows it.</p>
              <p>Clients send the token in an HTTP header in the form <code>Authorization: Bearer &lt;token&gt;</code>. Rotating the token invalidates the previous token; clearing it removes that machine credential.</p>
              <p>API Access does not replace browser authentication and does not make an otherwise open DebridPulse installation private. Configure Username &amp; Password, OpenID Connect, or both when the web application itself needs protection.</p>
            </div>
          </article>

          <article class="dp-help-inset">
            <h3>OpenID Connect identity providers</h3>
            <div class="dp-help-copy dp-help-prose">
              <p>OIDC lets DebridPulse use an identity provider you already operate, such as Authentik, Keycloak, or another standards-compatible provider.</p>
              <p>Set the <b>Public DebridPulse Base URL</b> to the externally reachable HTTPS origin. DebridPulse derives its fixed callback route, <code>/auth/oidc/callback</code>, from that origin and shows the complete <b>OIDC Callback URL</b> immediately while you type.</p>
              <p>Copy that exact callback URL into the identity provider, then use <b>Test OIDC Sign-In</b>. DebridPulse will not allow an unverified OIDC configuration to become the only login protection.</p>
            </div>
          </article>

          <article class="dp-help-inset">
            <h3>Public URLs and reverse proxies</h3>
            <div class="dp-help-copy dp-help-prose">
              <p>A reverse proxy is commonly used to give DebridPulse a stable HTTPS address. That public origin matters for secure browser sessions, OIDC callbacks, and locally uploaded Discord avatars that Discord itself must be able to fetch.</p>
              <p>If the Public DebridPulse Base URL is managed by the <code>PUBLIC_BASE_URL</code> environment variable, the corresponding field is read-only in Settings. Change the deployment value instead of trying to override it in the browser.</p>
            </div>
          </article>
        </div>
      </section>`;
  }

  function settingsPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-settings-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-settings-heading">Settings reference</h2>
          <p>A practical guide to the six current Settings sections and when you are likely to use them.</p>
        </div>

        <article class="dp-help-inset">
          <h3>How saving works</h3>
          <div class="dp-help-copy dp-help-prose">
            <p>Most form changes do not become persistent until you click <b>Apply Settings</b>. Connection-test buttons use the current draft values so you can validate a provider, aria2, or Discord configuration before saving it.</p>
            <p>Stored secrets are intentionally not shown back to the browser. When a secret is already configured, leaving its replacement field blank keeps the stored value. Use the explicit clear control when you actually want the saved secret removed.</p>
            <p>Some action buttons perform an immediate operation by design, such as generating or rotating an API token, logging out the current session, running a backup, or starting a confirmed destructive maintenance action.</p>
          </div>
        </article>

        <div class="dp-help-accordion-list">
          <details class="dp-help-accordion" open>
            <summary>Sources &amp; Providers</summary>
            <div class="dp-help-accordion-body dp-help-copy dp-help-prose">
              <p>This is where DebridPulse connects to services that prepare your sources. In the current release, the visible debrid provider is <b>AllDebrid</b>.</p>
              <ul>
                <li><b>API Key:</b> required for DebridPulse to use your AllDebrid account. Replace or clear it only when you intend to change the stored credential.</li>
                <li><b>Test AllDebrid:</b> verifies the current draft credential from the Settings footer.</li>
                <li><b>Additional Settings:</b> controls local API-rate limiting, how often active provider state is checked, periodic full reconciliation, provider upload retries, and the delay between those retries.</li>
              </ul>
              <p>The default Additional Settings are appropriate for a normal installation. Shorter provider intervals increase API traffic and should be changed only for a specific reason.</p>
            </div>
          </details>

          <details class="dp-help-accordion">
            <summary>Downloads</summary>
            <div class="dp-help-accordion-body dp-help-copy dp-help-prose">
              <p>The Downloads section controls the physical transfer engine and the safeguards around it.</p>
              <ul>
                <li><b>Download Engine:</b> choose Built-in aria2 or External aria2, configure the destination path, and set Maximum Concurrent Downloads.</li>
                <li><b>External aria2:</b> additionally requires an External RPC URL and optionally an aria2 RPC Secret. The external download path is written from the external server's point of view.</li>
                <li><b>Additional Engine Tuning:</b> available for built-in mode and includes partial-download continuation, segmentation, connection limits, split size, disk cache, file allocation, and the low-speed threshold.</li>
                <li><b>Download Safety &amp; Recovery:</b> contains the minimum-free-space guard, resume buffer, stalled-download recovery, download-error retry count, and retry delay.</li>
                <li><b>Test aria2:</b> verifies the current draft connection before Apply Settings.</li>
              </ul>
              <p>The header speed cap is an operational bandwidth control and is separate from the deeper engine settings on this tab.</p>
            </div>
          </details>

          <details class="dp-help-accordion">
            <summary>Extraction</summary>
            <div class="dp-help-accordion-body dp-help-copy dp-help-prose">
              <p>Automatic Extraction runs only after the physical download has completed.</p>
              <ul>
                <li><b>Enable:</b> turns automatic archive extraction on or off.</li>
                <li><b>Concurrent Extractions:</b> limits how many extraction jobs can run at once.</li>
                <li><b>Delete Archive After Extraction:</b> removes the downloaded archive only after extraction succeeds.</li>
                <li><b>Archive Passwords:</b> stores passwords DebridPulse can try against protected archives. Each password is a separate line.</li>
              </ul>
              <p>The password editor masks stored entries when they are not being edited. Use its reveal control only when you need to inspect the actual values.</p>
            </div>
          </details>

          <details class="dp-help-accordion">
            <summary>Notifications</summary>
            <div class="dp-help-accordion-body dp-help-copy dp-help-prose">
              <p>The Notifications section contains two related Discord features.</p>
              <ul>
                <li><b>Discord Notifications:</b> configure the sender name, avatar, primary webhook, optional added-event webhook, event toggles, and update-check interval.</li>
                <li><b>Statistics Reports:</b> configure an optional reporting webhook, automatic report interval, report window, and Send Test Report action.</li>
              </ul>
              <p>Uploaded avatars are served by DebridPulse. Discord must be able to reach the generated avatar URL, so a private or loopback-only application address may work for your browser but not for Discord's servers.</p>
            </div>
          </details>

          <details class="dp-help-accordion">
            <summary>Authentication</summary>
            <div class="dp-help-accordion-body dp-help-copy dp-help-prose">
              <p>Authentication protects the DebridPulse application and provides machine credentials for integrations.</p>
              <ul>
                <li><b>Authentication Status:</b> summarizes the current mode, Username &amp; Password state, OIDC state, API Access state, and browser-session information.</li>
                <li><b>Username &amp; Password:</b> configure a local username and password, then enable or disable that login method independently.</li>
                <li><b>OpenID Connect:</b> configure an external identity provider, the Public DebridPulse Base URL, provider identity and credentials, scopes, group claim, and optional subject/email/group allowlists.</li>
                <li><b>Test OIDC Sign-In:</b> verifies the proposed identity-provider configuration before you rely on it as the only authentication mechanism.</li>
                <li><b>API Access:</b> generates a bearer token for automation. The raw token is shown only at creation or rotation time.</li>
                <li><b>Browser Session Lifetime:</b> controls how long application sessions remain valid. You can also log out the current session from this section.</li>
              </ul>
              <p>The OIDC callback is not an arbitrary setting. DebridPulse derives the fixed <code>/auth/oidc/callback</code> route from the Public DebridPulse Base URL and gives you the exact URL to register with the identity provider.</p>
            </div>
          </details>

          <details class="dp-help-accordion">
            <summary>Data &amp; Maintenance</summary>
            <div class="dp-help-accordion-body dp-help-copy dp-help-prose">
              <p>This section controls backups, retained historical data, and intentionally destructive database maintenance.</p>
              <ul>
                <li><b>Backups &amp; Retention:</b> enable scheduled backups, choose the backup folder and interval, and set how long backups, statistics snapshots, and event-log entries are retained.</li>
                <li><b>Run Backup Now:</b> creates a backup immediately. <b>List Backups</b> shows the retained backup set.</li>
                <li><b>Allow Database Wipe:</b> is a safety gate that must be enabled before a wipe can run.</li>
                <li><b>Backup Before Wipe:</b> makes the destructive operation depend on a successful pre-wipe backup.</li>
              </ul>
              <p>Database wipe also requires processing to be paused and uses an explicit confirmation flow. Treat it as a reset operation, not routine cleanup.</p>
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
          <p>Start with the transfer details and Activity Log, then verify the provider, download engine, storage, or integration involved in the failed stage.</p>
        </div>

        <article class="dp-help-inset">
          <h3>Before changing anything</h3>
          <div class="dp-help-copy dp-help-prose">
            <p>Open <b>Downloads</b>, select the affected item, and read its file and event details. Then check the <b>Activity Log</b> for messages from the same time. Those two views usually tell you whether the problem happened during provider preparation, local transfer, extraction, or another application action.</p>
            <p>Avoid deleting the tracked download just to make the error disappear. DebridPulse keeps source and transfer state specifically so retries and recovery can use it.</p>
          </div>
        </article>

        <div class="dp-help-accordion-list">
          <details class="dp-help-accordion" open>
            <summary>I added something, but no work starts</summary>
            <div class="dp-help-accordion-body dp-help-copy dp-help-prose">
              <p>First check <b>Pause All</b>. DebridPulse intentionally accepts and records new submissions while processing is paused, so an item can appear normally without contacting AllDebrid or aria2 yet.</p>
              <p>If processing is active, open <b>Settings → Sources &amp; Providers</b> and use <b>Test AllDebrid</b>. A bad or expired API key prevents new provider work from progressing.</p>
              <p>If the provider test succeeds, check whether the item is simply queued behind the configured concurrency limit and inspect its Events for the current stage.</p>
            </div>
          </details>

          <details class="dp-help-accordion">
            <summary>Provider processing appears stuck</summary>
            <div class="dp-help-accordion-body dp-help-copy dp-help-prose">
              <p>Some sources are ready quickly and others require provider-side processing. A Processing state does not necessarily mean local downloading has started.</p>
              <p>Review the item's Events for provider errors. If AllDebrid is reachable and the item remains inconsistent, use <b>Recover All</b> to ask DebridPulse to reconcile its tracked provider and download state.</p>
              <p>Provider polling and full-reconciliation behavior are configurable under <b>Sources &amp; Providers → AllDebrid → Additional Settings</b>. Very long intervals make state changes appear slower; excessively short intervals create unnecessary API traffic.</p>
            </div>
          </details>

          <details class="dp-help-accordion">
            <summary>A download is stalled or aria2 reports an error</summary>
            <div class="dp-help-accordion-body dp-help-copy dp-help-prose">
              <p>Open <b>Settings → Downloads</b> and use <b>Test aria2</b>. If the connection test fails, fix the engine connection before repeatedly retrying the download.</p>
              <p>Check <b>Download Safety &amp; Recovery</b>. The free-space guard can deliberately defer new transfers, while stalled-download recovery and Download Error Retries control automatic recovery behavior.</p>
              <p>If the source has a verified standby mirror, DebridPulse may fail over automatically when the failure is specific to that source. Otherwise use the individual retry control or <b>Recover All</b> after you have corrected the underlying issue.</p>
            </div>
          </details>

          <details class="dp-help-accordion">
            <summary>Files are not appearing in the expected folder</summary>
            <div class="dp-help-accordion-body dp-help-copy dp-help-prose">
              <p>For built-in aria2, confirm the <b>Built-in Download Folder</b> and the Docker or container volume mapping behind it. The default <code>/download</code> path only works as intended when it is mapped to the host or NAS location you want.</p>
              <p>Confirm the container user has permission to create and modify files in the mapped destination.</p>
              <p>For external aria2, <b>External aria2 Download Path</b> must be correct from the external daemon's point of view, and both systems must have access to the same underlying storage.</p>
              <p>If new transfers are not starting at all, also check whether the minimum-free-space guard is active.</p>
            </div>
          </details>

          <details class="dp-help-accordion">
            <summary>External aria2 will not connect</summary>
            <div class="dp-help-accordion-body dp-help-copy dp-help-prose">
              <p>Verify that <b>External RPC URL</b> is reachable from the DebridPulse container or host network, not just from your desktop browser. A typical aria2 RPC endpoint ends in <code>/jsonrpc</code>.</p>
              <p>If the daemon uses an RPC secret, enter the same value in <b>aria2 RPC Secret</b>. Check firewall rules, container networks, DNS, TLS termination, and the listening address on the external aria2 server.</p>
              <p>Use <b>Test aria2</b> while the draft values are still in the form. A successful RPC test confirms connectivity; path mapping must still be correct for downloaded files to appear where DebridPulse expects them.</p>
            </div>
          </details>

          <details class="dp-help-accordion">
            <summary>Automatic extraction did not run or failed</summary>
            <div class="dp-help-accordion-body dp-help-copy dp-help-prose">
              <p>Confirm <b>Settings → Extraction → Enable</b> is on. Extraction begins after the physical download completes, so it does not run while the transfer itself is still incomplete.</p>
              <p>Check the transfer Events and Activity Log for an extraction result. Protected archives may require one of the configured Archive Passwords, and the destination must have enough space and permissions for extracted files.</p>
              <p><b>Delete Archive After Extraction</b> removes archives only after successful extraction. If extraction fails, keeping the source archive available makes troubleshooting and manual recovery easier.</p>
            </div>
          </details>

          <details class="dp-help-accordion">
            <summary>Discord or Prometheus integration is not working</summary>
            <div class="dp-help-accordion-body dp-help-copy dp-help-prose">
              <p>For Discord, use <b>Settings → Notifications → Test Discord</b>, verify the webhook is still valid, and confirm the event you expect is enabled. If you uploaded an avatar, Discord must be able to reach the DebridPulse-generated avatar URL from the public internet.</p>
              <p>For Prometheus, confirm the scraper uses <code>/api/metrics</code>. When DebridPulse authentication is enabled, that endpoint also requires an accepted machine credential. A bearer token from <b>Authentication → API Access</b> is the intended option for automation.</p>
            </div>
          </details>

          <details class="dp-help-accordion">
            <summary>The web interface stops receiving live updates</summary>
            <div class="dp-help-accordion-body dp-help-copy dp-help-prose">
              <p>DebridPulse uses Server-Sent Events for live browser updates and can fall back to polling when the stream is unavailable. If updates are consistently delayed, check the browser console for <code>EventSource</code> errors.</p>
              <p>Reverse proxies must allow long-lived streaming responses and should not buffer the event stream. Proxy timeouts that are too short can repeatedly disconnect an otherwise healthy DebridPulse session.</p>
              <p>A normal page reload can restore the browser connection, but recurring failures usually mean the reverse-proxy or network path needs correction.</p>
            </div>
          </details>

          <details class="dp-help-accordion">
            <summary>Username, password, or OIDC sign-in is failing</summary>
            <div class="dp-help-accordion-body dp-help-copy dp-help-prose">
              <p>If another configured login method still works, use it and open <b>Settings → Authentication</b>. The Authentication Status indicators show whether Username &amp; Password, OIDC, and API Access are configured and usable.</p>
              <p>For OIDC, verify that <b>Public DebridPulse Base URL</b> is the real externally reachable HTTPS origin with no extra path. Copy the exact derived <b>OIDC Callback URL</b> into the identity provider, then use <b>Test OIDC Sign-In</b>.</p>
              <p>DebridPulse prevents an unverified OIDC configuration from becoming the only login protection and requires explicit confirmation before intentionally opening an authenticated installation.</p>
              <p><b>Emergency local recovery:</b> if no browser login method works, stop DebridPulse and make a backup of the configuration file first. In the normal container layout the file is <code>/app/config/config.json</code>. Set <code>auth_password_enabled</code> and <code>auth_oidc_enabled</code> to <code>false</code>, then restart only on a trusted network. This intentionally opens the application without erasing the stored password hash or OIDC configuration, so repair Authentication and re-enable protection immediately.</p>
              <p>If the Public DebridPulse Base URL is controlled by <code>PUBLIC_BASE_URL</code>, fix the deployment environment value rather than the read-only browser field.</p>
            </div>
          </details>

          <details class="dp-help-accordion">
            <summary>Database maintenance will not run</summary>
            <div class="dp-help-accordion-body dp-help-copy dp-help-prose">
              <p>Database wipe is intentionally difficult to trigger accidentally. Processing must be paused, <b>Allow Database Wipe</b> must be enabled, and the confirmation flow must be completed.</p>
              <p>If <b>Backup Before Wipe</b> is enabled, a failed required backup aborts the wipe. Fix the backup folder, permissions, or storage problem instead of bypassing the protection.</p>
              <p>Use <b>Run Backup Now</b> and <b>List Backups</b> before destructive maintenance when you want to verify that recovery material exists.</p>
            </div>
          </details>
        </div>
      </section>`;
  }

  function licensePanel() {
    return `
      <section class="dp-help-document dp-help-license" aria-labelledby="dp-help-license-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-license-heading">Licensing and source</h2>
          <p>DebridPulse is open-source software built on an MIT-licensed upstream project and distributed under GPL-2.0-or-later for the DebridPulse work.</p>
        </div>

        <div class="dp-help-copy dp-help-copy--lead dp-help-prose">
          <p>DebridPulse modifications are Copyright &copy; 2026 Chris Moore and are distributed under the <b>GNU General Public License v2.0 or later</b> (<code>GPL-2.0-or-later</code>).</p>
          <p>In practical terms, the GPL allows you to use, study, modify, and redistribute DebridPulse under its license conditions. If you distribute a modified version, the GPL's source-code and licensing requirements apply to that distribution. The complete license text is the authority for the exact terms.</p>
          <p>DebridPulse is provided without warranty as described by the license.</p>
        </div>

        <article class="dp-help-inset dp-help-attribution">
          <h3>Upstream attribution</h3>
          <div class="dp-help-copy dp-help-prose">
            <p>This application is derived from <a href="https://github.com/kroeberd/alldebrid-client/tree/c0f7a5bfeba4f259fb2acc62ac6eed27e8ac4d5c" target="_blank" rel="noopener">kroeberd/alldebrid-client v1.9.9</a>, commit <code>c0f7a5bfeba4f259fb2acc62ac6eed27e8ac4d5c</code>, Copyright &copy; 2026 kroeberd.</p>
            <p>The upstream project was distributed under the MIT License. Its copyright and permission notice remain included with DebridPulse as required.</p>
          </div>
        </article>

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

    view.dataset.dpHelpRendered = '1';
    bindEvents(view);
    activateTab(state.activeTab);
    document.dispatchEvent(new CustomEvent('debridpulse:help-rendered', {detail: {view: 'help'}}));
  }

  function load() {
    render();
  }

  // app.js owns generic navigation and calls this canonical Help entry point.
  window.loadHelp = load;
  try { loadHelp = load; } catch (_) {}
  window.DPHelpPage = Object.freeze({load, activateTab});
})();