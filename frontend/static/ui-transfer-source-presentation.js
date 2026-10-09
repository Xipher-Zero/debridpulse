/* Neutral transfer source/host-icon presentation owner (DP 1.0.12 canonical
 * flattening). Single owner of the host->icon-asset mapping and the
 * source-identity glyph markup shared across every surface that renders a
 * transfer row -- Dashboard Recent, Downloads, and any future surface. No
 * other file may define its own HOST_ASSETS table or re-derive source-icon
 * markup from a transfer's current_source_identity.
 */
(function () {
  'use strict';

  const HOST_ASSETS = Object.freeze([
    ['1fichier.com', '1fichier.png'], ['4shared.com', '4shared.png'], ['alfafile.net', 'alfafile.png'],
    ['fastbit.cc', 'fastbit.png'], ['file-upload.com', 'file-upload.png'], ['fileal.com', 'fileal.png'],
    ['filedot.to', 'filedot.png'], ['filefactory.com', 'filefactory.png'], ['filespace.com', 'filespace.png'],
    ['gigapeta.com', 'gigapeta.png'], ['hexupload.net', 'hexupload.png'], ['hitfile.net', 'hitfile.png'],
    ['isra.cloud', 'isra-cloud.png'], ['katfile.com', 'katfile.png'], ['mediafire.com', 'mediafire.png'],
    ['mega.nz', 'mega.svg'], ['megaup.net', 'mega.svg'], ['modsbase.com', 'modsbase.png'],
    ['mp4upload.com', 'mp4upload.png'], ['prefiles.com', 'prefiles.png'], ['rapidgator.net', 'rapidgator.png'],
    ['scribd.com', 'scribd.png'], ['sendit.cloud', 'sendit.png'], ['simfileshare.net', 'simfileshare.png'],
    ['streamtape.com', 'streamtape.png'], ['turbobit.net', 'turbobit.png'], ['upload42.com', 'upload42.png'],
    ['uploadhaven.com', 'uploadhaven.png'], ['uploadrar.com', 'uploadrar.png'], ['world-files.com', 'world-files.png'],
  ]);

  /* The ONE protocol identity glyph table, keyed by integration id: Settings
   * draws it in its identity chip (ui-settings-page.js), and a transfer row
   * draws it inside that provider's badge, left of the label. Each glyph file
   * carries its exact identity colour (and Multimeta's inversion). A provider
   * absent here -- a premium service -- has no glyph: its badge is text. */
  const PROTOCOL_GLYPHS = Object.freeze({
    direct_sources: 'network-services',
    general_http: 'globe',
    general_ftp: 'arrow-up-down',
    general_scp: 'file-down',
    general_rsync: 'folder-sync',
    general_webdav: 'cloud-sync',
    multimeta: 'network',
    media: 'monitor-down',
    usenet: 'newspaper',
  });

  // A safe integration id, or ''.
  function identityKey(providerId) {
    const key = String(providerId || '');
    return /^[a-z0-9_]+$/.test(key) ? key : '';
  }

  /* A provider badge's identity, from its integration id alone: its colour
   * token (design-tokens.css, --dp-identity-<id>; an id without one keeps
   * the shared badge accent) and, for a protocol identity, its glyph inside
   * the badge on the left. */
  function providerBadgeIdentity(providerId) {
    const key = identityKey(providerId);
    if (!key) return {attributes: '', glyph: ''};
    const glyph = PROTOCOL_GLYPHS[key];
    return {
      attributes: ` data-provider-id="${key}" style="--dp-provider-identity: var(--dp-identity-${key})"`,
      glyph: glyph ? `<span class="dp-provider-glyph" style="--dp-provider-glyph: url(/icons/lucide/${glyph}.svg)" aria-hidden="true"></span>` : '',
    };
  }

  function normalizeHost(value) {
    return String(value || '').trim().toLowerCase().replace(/^www\./, '').replace(/\.$/, '');
  }

  function hostAsset(host) {
    const normalized = normalizeHost(host);
    const found = HOST_ASSETS.find(([domain]) => normalized === domain || normalized.endsWith('.' + domain));
    return found ? `/icons/hosts/${found[1]}` : '';
  }

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[c]);
  }

  function sourceSvg(kind) {
    // Torrent / Magnet: the Lucide Magnet glyph, its identity colour and its
    // clockwise 315° turn on .dp-source-magnet
    // (ui-dashboard-transfer-presentation.css).
    const magnet = '<path d="m12 15 4 4"/><path d="M2.352 10.648a1.205 1.205 0 0 0 0 1.704l2.296 2.296a1.205 1.205 0 0 0 1.704 0l6.029-6.029a1 1 0 1 1 3 3l-6.029 6.029a1.205 1.205 0 0 0 0 1.704l2.296 2.296a1.205 1.205 0 0 0 1.704 0l6.365-6.367A1 1 0 0 0 8.716 4.282z"/><path d="m5 8 4 4"/>';
    const paths = {
      link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
      magnet, torrent_file: magnet,
    };
    const isMagnet = kind === 'magnet' || kind === 'torrent_file';
    return `<svg class="lucide dp-source-fallback${isMagnet ? ' lucide-magnet dp-source-magnet' : ''}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[kind] || paths.link}</svg>`;
  }

  function sourceIconMarkup(identity) {
    const kind = String(identity?.kind || 'link').toLowerCase();
    if (kind === 'magnet' || kind === 'torrent_file') return sourceSvg(kind);
    if (kind === 'host') {
      const asset = hostAsset(identity?.host);
      if (asset) return `<img class="dp-source-host-logo" src="${esc(asset)}" alt="" aria-hidden="true">`;
    }
    return sourceSvg('link');
  }

  function sourceIdentityLabel(identity) {
    const kind = String(identity?.kind || 'link').toLowerCase();
    if (kind === 'host' && identity?.host) return String(identity.host);
    if (kind === 'magnet') return 'Magnet source';
    if (kind === 'torrent_file') return 'Torrent file source';
    return 'Link source';
  }

  /* ``providerId``: the provider the row's badge names. When that badge is a
   * protocol identity carrying its own glyph, the generic link glyph would be
   * a second, meaningless identity and is not drawn. A real hoster logo or the
   * magnet glyph still is. */
  function sourceSlot(identity, {providerId = ''} = {}) {
    if (PROTOCOL_GLYPHS[identityKey(providerId)] && sourceIconMarkup(identity) === sourceSvg('link')) return '';
    const label = sourceIdentityLabel(identity);
    return `<span class="dp-source-icon-slot" title="${esc(label)}" aria-label="${esc(label)}">${sourceIconMarkup(identity)}</span>`;
  }

  window.DPTransferSourcePresentation = Object.freeze({
    PROTOCOL_GLYPHS, hostAsset, providerBadgeIdentity, sourceIconMarkup, sourceSlot,
  });
})();
