from base64 import b64decode
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from providers.general_http.provider import GeneralHttpProvider
from transfers.models import TransferRequest
from transfers.presentation_repository import public_source_identity


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
EXPECTED_HOST_ARCHIVE_SHA256 = "2bfb7cadf647f6d4093ce4ad7d13159e137a190925a1b840f8a50a7f579be90f"
EXPECTED_HOST_ARTWORK = {
    "1fichier.png", "4shared.png", "alfafile.png", "fastbit.png", "file-upload.png",
    "fileal.png", "filedot.png", "filefactory.png", "filespace.png", "gigapeta.png",
    "hexupload.png", "hitfile.png", "isra-cloud.png", "katfile.png", "mediafire.png",
    "mega.svg", "modsbase.png", "mp4upload.png", "prefiles.png", "rapidgator.png",
    "scribd.png", "sendit.png", "simfileshare.png", "streamtape.png", "turbobit.png",
    "upload42.png", "uploadhaven.png", "uploadrar.png", "world-files.png",
}


def _host_archive_bytes() -> bytes:
    parts = ROOT / "frontend" / "host-icons.parts"
    encoded = "".join(path.read_text(encoding="ascii") for path in sorted(parts.iterdir()))
    return b64decode(encoded, validate=True)


def test_batch1_runtime_and_styles_are_wired_through_canonical_assets():
    provider_status = (STATIC / "ui-provider-status.js").read_text()
    styles = (STATIC / "style-v11.css").read_text()
    runtime = (STATIC / "ui-correction-batch1.js").read_text()
    capacity = (STATIC / "ui-correction-batch1-capacity.js").read_text()
    batch_css = (STATIC / "ui-correction-batch1.css").read_text()

    assert "ui-correction-batch1.js" in provider_status
    assert "ui-correction-batch1-capacity.js" in provider_status
    assert "ui-correction-batch1.css" in styles
    assert "DebridPulse stared at that for a moment" in runtime
    assert "Checking transfers for recoverable work" in runtime
    assert "File Archive" not in runtime
    assert "torrent_file" in runtime
    assert "ResizeObserver" in runtime
    assert "Math.floor(oldOffset / measured) + 1" in runtime
    assert "Friendly" in runtime and "International" in runtime and "ISO" in runtime
    assert "12-hour" in runtime and "24-hour" in runtime
    assert "dp-pager-placeholder" in runtime
    assert "dp-downloads-pause-shim" in runtime
    assert "dp-global-pause-center" in runtime
    assert "width: 136px" in batch_css
    assert "canonicalLoadTorrents" in runtime
    assert "correctedLoadTorrents" not in runtime

    assert "size >= 15" in capacity
    assert "query.set('limit', String(size))" in capacity
    assert "query.set('offset', String((effectivePage() - 1) * size))" in capacity
    assert "originalPagination.call(this, total, size, (page - 1) * size)" in capacity


def test_supplied_host_artwork_is_losslessly_reconstructable_and_domain_matching_is_safe():
    runtime = (STATIC / "ui-correction-batch1.js").read_text()
    archive = _host_archive_bytes()

    assert sha256(archive).hexdigest() == EXPECTED_HOST_ARCHIVE_SHA256
    assert "host === domain || host.endsWith('.' + domain)" in runtime
    assert "rapidgator.net" in runtime
    assert "mega.nz" in runtime
    with ZipFile(BytesIO(archive)) as package:
        names = {member.filename for member in package.infolist() if not member.is_dir()}
    assert names == EXPECTED_HOST_ARTWORK


def test_quick_add_import_capability_is_not_removed_from_backend():
    routes = (ROOT / "backend" / "api" / "routes.py").read_text()
    runtime = (STATIC / "ui-correction-batch1.js").read_text()

    assert "/torrents/import-existing" in routes
    assert "btn-import-existing" in runtime
    assert 'button[onclick*="importExisting"]' in runtime


def test_direct_sources_group_is_explicit_backend_presentation_metadata():
    definition = (ROOT / "backend" / "providers" / "general_http" / "definition.py").read_text()
    presentation = (ROOT / "backend" / "integrations" / "definition.py").read_text()

    assert 'status_group="direct_sources"' in definition
    assert 'status_group_label="Direct Sources"' in definition
    assert '"status_group": self.status_group' in presentation
    assert '"status_group_label": self.status_group_label' in presentation


@pytest.mark.asyncio
async def test_general_http_candidate_persists_neutral_host_source_identity():
    provider = GeneralHttpProvider()
    result = await provider.resolve(
        TransferRequest("https", "https://sub.rapidgator.net/file/abc?token=secret", name="payload.bin")
    )
    candidate = result.candidates[0]

    assert candidate.source_identity is not None
    assert candidate.source_identity.scope == "host"
    assert candidate.source_identity.key == "sub.rapidgator.net"
    assert "token" not in candidate.source_identity.key
    assert "secret" not in candidate.source_identity.key


def test_public_source_identity_uses_durable_candidate_provenance_and_root_precedence():
    source = {"scope": "host", "key": "cdn.rapidgator.net"}
    http = public_source_identity("https", source)
    magnet = public_source_identity("magnet", source)
    torrent = public_source_identity("torrent_file", source)
    invalid = public_source_identity("https", {"scope": "host", "key": "https://evil.invalid/?token=secret"})

    assert http == {"kind": "host", "host": "cdn.rapidgator.net"}
    assert magnet == {"kind": "magnet"}
    assert torrent == {"kind": "torrent_file"}
    assert invalid == {"kind": "link"}
    serialized = repr((http, magnet, torrent, invalid))
    assert "token" not in serialized
    assert "secret" not in serialized
    assert "magnet:?" not in serialized


def test_presentation_repository_reads_persisted_candidate_provenance_not_request_url_for_http_identity():
    source = (ROOT / "backend" / "transfers" / "presentation_repository.py").read_text()

    assert "execution_attempt_provenance" in source
    assert "route_attempt_provenance" in source
    assert "candidate_source" in source
    assert "candidate_summary" in source
    assert "urlparse" not in source
