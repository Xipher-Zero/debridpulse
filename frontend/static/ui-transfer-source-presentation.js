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
    const boxes = '<path d="M2.97 12.92A2 2 0 0 0 2 14.63v3.24a2 2 0 0 0 .97 1.71l3 1.8a2 2 0 0 0 2.06 0L12 19v-5.5l-5-3-4.03 2.42Z"/><path d="m7 16.5-4.74-2.85"/><path d="m7 16.5 5-3"/><path d="M7 16.5v5.17"/><path d="M12 13.5V19l3.97 2.38a2 2 0 0 0 2.06 0l3-1.8a2 2 0 0 0 .97-1.71v-3.24a2 2 0 0 0-.97-1.71L17 10.5l-5 3Z"/><path d="m17 16.5-5-3"/><path d="m17 16.5 4.74-2.85"/><path d="M17 16.5v5.17"/><path d="M7.97 4.42A2 2 0 0 0 7 6.13v4.37l5 3 5-3V6.13a2 2 0 0 0-.97-1.71l-3-1.8a2 2 0 0 0-2.06 0l-3 1.8Z"/><path d="M12 8 7.26 5.15"/><path d="m12 8 4.74-2.85"/><path d="M12 13.5V8"/>';
    const paths = {
      link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
      magnet: boxes, torrent_file: boxes,
    };
    const usesBoxes = kind === 'magnet' || kind === 'torrent_file';
    return `<svg class="lucide dp-source-fallback${usesBoxes ? ' lucide-boxes dp-source-boxes' : ''}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[kind] || paths.link}</svg>`;
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

  function sourceSlot(identity) {
    const label = sourceIdentityLabel(identity);
    return `<span class="dp-source-icon-slot" title="${esc(label)}" aria-label="${esc(label)}">${sourceIconMarkup(identity)}</span>`;
  }

  window.DPTransferSourcePresentation = Object.freeze({hostAsset, sourceIconMarkup, sourceSlot});
})();
