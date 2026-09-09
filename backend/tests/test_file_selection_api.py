"""Gate B — the dedicated file-selection HTTP API and its status-code contract.

Drives the real routes through httpx/ASGI against a real ApplicationService wired
to an unrelated fake FILE_MANIFEST provider. Timing is an injected fake clock.
"""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse

import db.database as database
from api.file_selection_routes import (
    _PUBLIC_OFFER_FIELDS, _PUBLIC_SELECTION_FIELDS, router as file_selection_router,
)
from api.routes import router as generic_router
from api.operational_downloads import router as operational_downloads_router
from application.observability import Observability
from application.service import ApplicationService
from fake_integrations import MemoryExecutor, ParcelProvider
from file_selection_support import Clock
from transfers.engine import TransferEngine
from transfers.errors import TransferError
from transfers.models import ResourceState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository

FILES6 = [
    ("e01.mkv", "Season 1/e01.mkv", 100), ("e02.mkv", "Season 1/e02.mkv", 200),
    ("e03.mkv", "Season 1/e03.mkv", 300), ("e04.mkv", "Season 1/e04.mkv", 400),
    ("e05.mkv", "Season 1/e05.mkv", 500), ("readme.txt", "readme.txt", 0),
]


@pytest_asyncio.fixture
async def api(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fs-api.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider(file_manifest=True)
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    clock = Clock(1000.0)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(adoption_stability_seconds=0, resource_poll_interval=1,
                              retry_delay=0, resolution_retry_delay=0),
        clock=clock,
    )
    await engine.initialize()
    application = ApplicationService(engine)
    application.observability = Observability(repository)

    app = FastAPI()
    app.state.application = application
    app.include_router(file_selection_router, prefix="/api")
    app.include_router(operational_downloads_router, prefix="/api")
    app.include_router(generic_router, prefix="/api")

    @app.exception_handler(TransferError)
    async def _failure(_request, exc):
        return JSONResponse(status_code=409, content={"error": exc.error.as_dict()})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        yield type("Api", (), dict(
            client=client, application=application, engine=engine, repository=repository,
            provider=provider, executor=executor, clock=clock,
        ))


async def _submit_available_multifile(api, *, payload="showA", files=FILES6):
    api.provider.responses.append(
        api.provider.parcel(payload, state=ResourceState.AVAILABLE, files=files)
    )
    transfer = await api.engine.submit((TransferRequest("parcel", "box", name="show"),), deduplicate=False)
    await api.engine.resolve_pending()
    return transfer.id


# --------------------------------------------------------------------------- #
# Read model
# --------------------------------------------------------------------------- #

# Internal provenance identifiers that must never cross the browser boundary.
_INTERNAL_KEYS = ("selection_id", "provider_resource_id", "provider_id", "request_id",
                  "resource_id", "generation_id")
_INTERNAL_SUBSTRINGS = (
    "memory:", "endpoint", "handle", "box_ticket", "signed", "http://", "https://",
    "alldebrid", "aria2", "bearer", "token", "cookie", "secret", "apikey", "api_key",
    "parcel-lab:",
)


def _assert_no_internal_leak(payload) -> None:
    """Recursively assert no internal/native identifier key appears anywhere."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert key not in _INTERNAL_KEYS, key
            _assert_no_internal_leak(value)
    elif isinstance(payload, list):
        for item in payload:
            _assert_no_internal_leak(item)


@pytest.mark.asyncio
async def test_get_file_selection_returns_safe_core_facts_only(api):
    transfer_id = await _submit_available_multifile(api)
    response = await api.client.get(f"/api/torrents/{transfer_id}/file-selection")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == set(_PUBLIC_SELECTION_FIELDS)          # exact §38 whitelist
    assert body["eligible"] is True and body["mutable"] is True
    assert body["decision"] == "pending" and body["file_count"] == 6
    assert body["total_size_bytes"] == 1500
    assert len(body["entries"]) == 6
    assert set(body["entries"][0]) == {"entry_id", "name", "relative_path", "size_bytes"}
    assert body["auto_offer"] is True
    assert body["decision_deadline"] == 1000.0 + 120.0
    assert body["server_now"] == 1000.0

    _assert_no_internal_leak(body)
    blob = response.text.casefold()
    for forbidden in _INTERNAL_SUBSTRINGS:
        assert forbidden not in blob


@pytest.mark.asyncio
async def test_get_file_selection_404_for_unknown_transfer(api):
    assert (await api.client.get("/api/torrents/999999/file-selection")).status_code == 404


@pytest.mark.asyncio
async def test_offers_endpoint_lists_live_offer_and_recovers_after_reconnect(api):
    transfer_id = await _submit_available_multifile(api)
    response = await api.client.get("/api/file-selections/offers")
    offers = response.json()["offers"]
    assert [o["transfer_id"] for o in offers] == [transfer_id]
    assert offers[0]["file_count"] == 6
    assert set(offers[0]) <= set(_PUBLIC_OFFER_FIELDS)
    _assert_no_internal_leak(response.json())
    blob = response.text.casefold()
    for forbidden in _INTERNAL_SUBSTRINGS:
        assert forbidden not in blob


# --------------------------------------------------------------------------- #
# Confirm / dismiss contract
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_confirm_happy_path_then_only_subset_materializes(api):
    transfer_id = await _submit_available_multifile(api)
    view = (await api.client.get(f"/api/torrents/{transfer_id}/file-selection")).json()
    keep = [view["entries"][0]["entry_id"], view["entries"][2]["entry_id"]]

    ok = await api.client.post(
        f"/api/torrents/{transfer_id}/file-selection/confirm",
        json={"manifest_id": view["manifest_id"], "entry_ids": keep},
    )
    assert ok.status_code == 200 and ok.json()["decision"] == "explicit"

    api.clock.advance(5)
    await api.engine.resolve_pending()
    children = await api.repository.requests(transfer_id)
    members = [r for r in children if r.parent_id is not None]
    assert sorted(r.entry.relative_path for r in members) == ["Season 1/e01.mkv", "Season 1/e03.mkv"]

    after = (await api.client.get(f"/api/torrents/{transfer_id}/file-selection")).json()
    assert after["mutable"] is False


@pytest.mark.asyncio
async def test_confirm_422_on_empty_selection(api):
    transfer_id = await _submit_available_multifile(api)
    view = (await api.client.get(f"/api/torrents/{transfer_id}/file-selection")).json()
    r = await api.client.post(
        f"/api/torrents/{transfer_id}/file-selection/confirm",
        json={"manifest_id": view["manifest_id"], "entry_ids": []},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_confirm_409_on_stale_manifest_id(api):
    transfer_id = await _submit_available_multifile(api)
    view = (await api.client.get(f"/api/torrents/{transfer_id}/file-selection")).json()
    r = await api.client.post(
        f"/api/torrents/{transfer_id}/file-selection/confirm",
        json={"manifest_id": "0" * 32, "entry_ids": [view["entries"][0]["entry_id"]]},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_confirm_409_after_materialization_committed(api):
    transfer_id = await _submit_available_multifile(api)
    view = (await api.client.get(f"/api/torrents/{transfer_id}/file-selection")).json()
    api.clock.advance(200)                      # blow past the 120s hold
    await api.engine.resolve_pending()          # materializes ALL
    r = await api.client.post(
        f"/api/torrents/{transfer_id}/file-selection/confirm",
        json={"manifest_id": view["manifest_id"], "entry_ids": [view["entries"][0]["entry_id"]]},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_dismiss_releases_cached_hold_and_all_materializes(api):
    transfer_id = await _submit_available_multifile(api)
    view = (await api.client.get(f"/api/torrents/{transfer_id}/file-selection")).json()
    r = await api.client.post(
        f"/api/torrents/{transfer_id}/file-selection/dismiss",
        json={"manifest_id": view["manifest_id"]},
    )
    assert r.status_code == 200 and r.json()["decision"] == "all"
    api.clock.advance(1)
    await api.engine.resolve_pending()
    members = [r for r in await api.repository.requests(transfer_id) if r.parent_id is not None]
    assert len(members) == 6


# --------------------------------------------------------------------------- #
# Per-resource provenance across re-resolution (operator-required API regression)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_offers_and_read_model_expose_only_the_current_generation_after_re_resolution(api):
    # request R -> resource A -> explicit selection
    transfer_id = await _submit_available_multifile(api, payload="A", files=FILES6)
    view_a = (await api.client.get(f"/api/torrents/{transfer_id}/file-selection")).json()
    manifest_a = view_a["manifest_id"]
    await api.client.post(
        f"/api/torrents/{transfer_id}/file-selection/confirm",
        json={"manifest_id": manifest_a, "entry_ids": [view_a["entries"][0]["entry_id"]]},
    )

    # resource A expires; re-resolve R onto resource B (different file set / size)
    resource_a_id = api.provider.descriptor.id + ":A"
    api.provider.resources.pop(resource_a_id, None)          # observe(A) -> ABSENT
    files_b = [(f"b{i}.bin", f"B/b{i}.bin", i * 11) for i in range(4)]
    api.provider.responses.append(
        api.provider.parcel("B", state=ResourceState.AVAILABLE, files=files_b)
    )
    api.clock.advance(5000)
    for _ in range(4):                                       # fail A -> re-resolve -> observe B
        await api.engine.resolve_pending()
        api.clock.advance(1)

    # B is identified purely by transfer context (URL) + the canonical manifest_id,
    # never by an exposed provider-resource or generation identifier.
    view_response = await api.client.get(f"/api/torrents/{transfer_id}/file-selection")
    view_b = view_response.json()
    assert view_b["manifest_id"] not in ("", None) and view_b["manifest_id"] != manifest_a
    assert view_b["file_count"] == 4                         # B's tree, not A's 6
    assert view_b["decision"] == "pending"                   # fresh generation, no inheritance
    assert {e["relative_path"] for e in view_b["entries"]} == {f"B/b{i}.bin" for i in range(4)}
    assert set(view_b) == set(_PUBLIC_SELECTION_FIELDS)
    _assert_no_internal_leak(view_b)
    assert manifest_a not in view_response.text              # A never appears as current
    for forbidden in _INTERNAL_SUBSTRINGS:
        assert forbidden not in view_response.text.casefold()

    offers_response = await api.client.get("/api/file-selections/offers")
    offers = offers_response.json()["offers"]
    assert [o["manifest_id"] for o in offers] == [view_b["manifest_id"]]
    assert manifest_a not in offers_response.text            # A's manifest id absent
    _assert_no_internal_leak(offers_response.json())

    # A's stale manifest id can no longer be confirmed or dismissed.
    assert (await api.client.post(
        f"/api/torrents/{transfer_id}/file-selection/confirm",
        json={"manifest_id": manifest_a, "entry_ids": [view_a["entries"][0]["entry_id"]]},
    )).status_code == 409
    assert (await api.client.post(
        f"/api/torrents/{transfer_id}/file-selection/dismiss",
        json={"manifest_id": manifest_a},
    )).status_code == 409


# --------------------------------------------------------------------------- #
# Public-response security boundary (specification sections 38, 64)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_no_public_file_selection_response_carries_internal_identifiers(api):
    transfer_id = await _submit_available_multifile(api)
    view = (await api.client.get(f"/api/torrents/{transfer_id}/file-selection")).json()
    keep = [view["entries"][0]["entry_id"]]

    bodies = [
        await api.client.get("/api/file-selections/offers"),
        await api.client.get(f"/api/torrents/{transfer_id}/file-selection"),
        await api.client.post(
            f"/api/torrents/{transfer_id}/file-selection/confirm",
            json={"manifest_id": view["manifest_id"], "entry_ids": keep}),
        await api.client.post(
            f"/api/torrents/{transfer_id}/file-selection/dismiss",
            json={"manifest_id": view["manifest_id"]}),
    ]
    for response in bodies:
        text = response.text
        for key in ("selection_id", "provider_resource_id", "provider_id"):
            assert f'"{key}"' not in text, (response.request.url, key)
        _assert_no_internal_leak(response.json())
        for forbidden in _INTERNAL_SUBSTRINGS:
            assert forbidden not in text.casefold()
        # No provider-native resource id (the fake provider's resource id shape).
        assert "parcel-lab:" not in text


# --------------------------------------------------------------------------- #
# Browser event emission (specification section 39)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_multi_file_offer_within_window_emits_one_durable_event(api):
    transfer_id = await _submit_available_multifile(api)
    async with database.get_db() as db:
        events = await db.fetchall(
            "SELECT transfer_id, kind, claimed FROM application_events WHERE kind='file_selection_available'")
    assert [(e["transfer_id"], e["claimed"]) for e in events] == [(transfer_id, 0)]

    published = []
    import application.observability as obs
    original = obs.publish

    async def capture(kind, payload):
        published.append((kind, payload))
        return await original(kind, payload)

    obs.publish = capture
    try:
        await api.application.observability.deliver()
        await api.application.observability.deliver()          # idempotent: already claimed
    finally:
        obs.publish = original

    fs_events = [p for p in published if p[0] == "file_selection_available"]
    assert fs_events == [("file_selection_available", {"transfer_id": transfer_id})]

    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT claimed FROM application_events WHERE kind='file_selection_available'")
    assert row["claimed"] == 1


@pytest.mark.asyncio
async def test_single_file_resource_emits_no_offer_event(api):
    await _submit_available_multifile(api, files=[("only.bin", "only.bin", 42)])
    async with database.get_db() as db:
        events = await db.fetchall(
            "SELECT 1 FROM application_events WHERE kind='file_selection_available'")
    assert events == []
