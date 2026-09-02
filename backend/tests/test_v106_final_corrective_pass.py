import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_aria2_success_logging_never_formats_request_uri():
    import ast
    root = Path(__file__).parents[1] / "executors" / "aria2"
    for name in ("client.py", "executor.py"):
        source = (root / name).read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "logger":
                call = ast.get_source_segment(source, node)
                assert "normalized_uri" not in call
                assert "candidate.endpoints" not in call


def test_dockerfile_uses_current_v1_oci_identity():
    root = Path(__file__).parents[2]
    dockerfile = (root / "Dockerfile").read_text()
    workflow = (root / ".github" / "workflows" / "fork-image.yml").read_text()
    expected_title = "DebridPulse: Universal Transfer Manager"

    assert f'org.opencontainers.image.title="{expected_title}"' in dockerfile
    assert "org.opencontainers.image.title=" + expected_title in workflow
    assert f'expected_title="{expected_title}"' in workflow
    assert "DebridPulse \u2014 AllDebrid + aria2 Download Manager" not in dockerfile
    assert "DebridPulse \u2014 AllDebrid + aria2 Download Manager" not in workflow
    assert 'org.opencontainers.image.description="Provider-independent transfer orchestration with AllDebrid resolution and aria2 execution"' in dockerfile
    assert "Multi-provider Debrid Download Manager" not in dockerfile
    assert "Multi-provider debrid download manager" not in dockerfile


def test_lifespan_shutdown_is_finally_guarded():
    source = (Path(__file__).parents[1] / "main.py").read_text()
    block = source.split("async def lifespan", 1)[1].split("class _RequestBodyTooLarge", 1)[0]
    started = block.index("await start_scheduler(application)")
    guarded = block.index("try:", started)
    yielded = block.index("yield", guarded)
    final = block.index("finally:", yielded)
    stopped = block.index("await stop_scheduler()", final)
    aria2 = block.index("await application.stop_integrations()", stopped)
    assert started < guarded < yielded < final < stopped < aria2


def test_event_and_snapshot_routes_use_public_timestamp_serialization():
    source = (Path(__file__).parents[1] / "api" / "routes.py").read_text()
    detail = source.split("async def get_torrent(torrent_id: int,", 1)[1].split(
        '@router.delete("/torrents/{torrent_id}")', 1
    )[0]
    events = source.split("async def get_events(", 1)[1].split(
        '@router.get("/admin/performance")', 1
    )[0]
    snapshots = source.split("async def list_stats_snapshots", 1)[1].split("@router", 1)[0]
    assert 'return public_payload(item)' in detail
    assert "return public_payload(rows)" in events
    assert 'return {"snapshots": public_payload(rows)}' in snapshots


def test_public_payload_normalizes_event_timestamp():
    from api.serializers import public_payload

    payload = public_payload({"message": "x", "created_at": "2026-08-21 03:00:00"})
    assert payload["created_at"] == "2026-08-21T03:00:00Z"


