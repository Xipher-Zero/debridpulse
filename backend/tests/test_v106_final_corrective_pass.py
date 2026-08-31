import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_exact_capability_is_removed_from_persisted_errors():
    from services.manager_v2 import _safe_persisted_error

    capability = "https://locked.invalid/cap"
    rendered = _safe_persisted_error(
        RuntimeError(f"provider echoed {capability} in failure"), capability
    )
    assert capability not in rendered
    assert "<capability-url>" in rendered


def test_aria2_success_logging_never_formats_request_uri():
    source = (Path(__file__).parents[1] / "services" / "aria2.py").read_text()
    ensure = source.split("async def ensure_download", 1)[1].split("def _find_all_matches", 1)[0]
    assert "queued download accepted as GID %s" in ensure
    assert "sanitize_log_value(normalized_uri" not in ensure
    assert "queued download %s (%s)" not in ensure


@pytest.mark.asyncio
async def test_provider_gateway_tracks_read_only_file_preview_and_test_calls():
    from services.provider_gateway import ProviderGateway

    class Client:
        async def get_magnet_files(self, ids):
            return [{"id": ids[0]}]

        async def get_user(self):
            return {"user": {"username": "ok"}}

    client = Client()
    gateway = ProviderGateway(SimpleNamespace(ad=lambda: client))
    assert await gateway.get_magnet_files(["7"]) == [{"id": "7"}]
    assert (await gateway.test())["user"]["username"] == "ok"

    await gateway.begin_quiescence()
    try:
        with pytest.raises(RuntimeError, match="quiesced"):
            await gateway.get_magnet_files(["7"])
        with pytest.raises(RuntimeError, match="quiesced"):
            await gateway.test()
    finally:
        await gateway.end_quiescence()


def test_routes_do_not_bypass_provider_gateway():
    root = Path(__file__).parents[1]
    routes = (root / "api" / "routes.py").read_text()
    service = (root / "services" / "transfer_service.py").read_text()
    assert "provider.client()" not in routes
    test_block = routes.split("async def test_alldebrid():", 1)[1].split(
        '@router.post("/settings/test-aria2")', 1
    )[0]
    assert "transfer_service.provider.test()" in test_block
    assert "AllDebridService" not in test_block
    assert "def ad(self)" not in service


def test_dockerfile_uses_current_v1_oci_identity():
    root = Path(__file__).parents[2]
    dockerfile = (root / "Dockerfile").read_text()
    workflow = (root / ".github" / "workflows" / "fork-image.yml").read_text()
    expected_title = "DebridPulse: AllDebrid + aria2 Download Manager"

    assert f'org.opencontainers.image.title="{expected_title}"' in dockerfile
    assert "org.opencontainers.image.title=" + expected_title in workflow
    assert f'expected_title="{expected_title}"' in workflow
    assert "DebridPulse \u2014 AllDebrid + aria2 Download Manager" not in dockerfile
    assert "DebridPulse \u2014 AllDebrid + aria2 Download Manager" not in workflow
    assert 'org.opencontainers.image.description="AllDebrid-backed download manager for direct links, magnets, and torrent files via aria2"' in dockerfile
    assert "Multi-provider Debrid Download Manager" not in dockerfile
    assert "Multi-provider debrid download manager" not in dockerfile


def test_lifespan_shutdown_is_finally_guarded():
    source = (Path(__file__).parents[1] / "main.py").read_text()
    block = source.split("async def lifespan", 1)[1].split("class _RequestBodyTooLarge", 1)[0]
    started = block.index("await start_scheduler()")
    guarded = block.index("try:", started)
    yielded = block.index("yield", guarded)
    final = block.index("finally:", yielded)
    stopped = block.index("await stop_scheduler()", final)
    aria2 = block.index("await aria2_runtime.stop()", stopped)
    assert started < guarded < yielded < final < stopped < aria2


def test_event_and_snapshot_routes_use_public_timestamp_serialization():
    source = (Path(__file__).parents[1] / "api" / "routes.py").read_text()
    detail = source.split("async def get_torrent(torrent_id: int):", 1)[1].split(
        '@router.delete("/torrents/{torrent_id}")', 1
    )[0]
    events = source.split("async def get_events(", 1)[1].split(
        '@router.get("/admin/performance")', 1
    )[0]
    snapshots = source.split("async def list_stats_snapshots", 1)[1].split("@router", 1)[0]
    assert 'public_payload(dict(event))' in detail
    assert "return public_payload(rows)" in events
    assert 'return {"snapshots": public_payload(rows)}' in snapshots


def test_public_payload_normalizes_event_timestamp():
    from api.serializers import public_payload

    payload = public_payload({"message": "x", "created_at": "2026-08-21 03:00:00"})
    assert payload["created_at"] == "2026-08-21T03:00:00Z"


def test_missing_gid_recovery_log_omits_capability_url():
    source = (Path(__file__).parents[1] / "services" / "manager_v2.py").read_text()
    sync = source.split("async def sync_aria2_downloads", 1)[1].split(
        "async def _engine_reset_torrent_for_redownload", 1
    )[0]
    assert "(path=%s, url=%s) -> scheduling reset" not in sync
    assert "(path=%s) -> scheduling reset" in sync
