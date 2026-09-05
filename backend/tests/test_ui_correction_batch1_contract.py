from pathlib import Path
from zipfile import ZipFile

import pytest

from providers.general_http.provider import GeneralHttpProvider
from transfers.models import TransferRequest
from transfers.presentation_repository import public_root_source_identity


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


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

    # app.js remains the renderer/selection/filter owner. The bridge only makes
    # its transport and pager honor a measured desktop page size below the old
    # hard-coded minimum of 15 rows.
    assert "size >= 15" in capacity
    assert "query.set('limit', String(size))" in capacity
    assert "query.set('offset', String((effectivePage() - 1) * size))" in capacity
    assert "originalPagination.call(this, total, size, (page - 1) * size)" in capacity


def test_host_artwork_archive_and_domain_matching_contract():
    runtime = (STATIC / "ui-correction-batch1.js").read_text()
    archive = ROOT / "frontend" / "host-icons.zip"

    assert "host === domain || host.endsWith('.' + domain)" in runtime
    assert "rapidgator.net" in runtime
    assert "mega.nz" in runtime
    assert archive.is_file()
    with ZipFile(archive) as package:
        names = set(package.namelist())
    assert "rapidgator.png" in names
    assert "mega.svg" in names
    assert len(names) == 29


def test_quick_add_import_capability_is_not_removed_from_backend():
    routes = (ROOT / "backend" / "api" / "routes.py").read_text()
    runtime = (STATIC / "ui-correction-batch1.js").read_text()

    assert "/torrents/import-existing" in routes
    assert 'btn-import-existing' in runtime
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


def test_public_root_source_identity_obeys_root_request_precedence_and_never_exposes_urls():
    http = public_root_source_identity(
        TransferRequest("https", "https://rapidgator.net/file/abc?token=secret")
    )
    magnet = public_root_source_identity(
        TransferRequest("magnet", "magnet:?xt=urn:btih:deadbeef")
    )
    torrent = public_root_source_identity(
        TransferRequest("torrent_file", b"d4:infod4:name4:testee")
    )

    assert http == {"kind": "host", "host": "rapidgator.net"}
    assert magnet == {"kind": "magnet"}
    assert torrent == {"kind": "torrent_file"}
    serialized = repr((http, magnet, torrent))
    assert "token" not in serialized
    assert "secret" not in serialized
    assert "magnet:?" not in serialized
