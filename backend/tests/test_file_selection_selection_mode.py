"""Phase B — explicit submission intent (``selection_mode``).

Torrent/Magnet File-Selection Lifecycle Correction §6, §17.

Interactive file-selection is entered ONLY when the submitter explicitly opts
in with ``selection_mode=interactive``. It is never inferred from an SSE
connection, a browser session, a user agent, or the ``source`` string. A
historical/headless caller that sends the unchanged request shape always
defaults to ALL and never becomes dependent on a browser/modal.

``selection_mode`` is a per-submission policy only: it must not change the
torrent's dedupe / source-fingerprint identity.
"""
from __future__ import annotations

import pytest

from fake_integrations import ParcelProvider
from transfers import file_selection as fs
from transfers.models import ResourceState, TransferRequest

from test_file_selection_api import api  # noqa: F401  (shared fixture)

FILES = [("a.mkv", "S/a.mkv", 10), ("b.mkv", "S/b.mkv", 20), ("c.mkv", "S/c.mkv", 30)]


async def _selection_row(api, transfer_id):
    import db.database as database
    async with database.get_db() as db:
        return await db.fetchone(
            "SELECT * FROM transfer_file_selections WHERE transfer_id=?", (transfer_id,))


async def _submit(api, *, selection_mode, payload="box", available=True):
    api.provider.responses.append(api.provider.parcel(
        payload, state=ResourceState.AVAILABLE if available else ResourceState.PREPARING, files=FILES))
    transfer = await api.engine.submit(
        (TransferRequest("parcel", payload, name="show", fingerprint="hash-" + payload,
                         selection_mode=selection_mode),),
        deduplicate=False)
    await api.engine.resolve_pending()
    return transfer.id


# --------------------------------------------------------------------------- #
# §6 — normalize_selection_mode
# --------------------------------------------------------------------------- #

def test_normalize_selection_mode_defaults_and_validation():
    assert fs.normalize_selection_mode(None) == "all"
    assert fs.normalize_selection_mode("") == "all"
    assert fs.normalize_selection_mode("  ") == "all"
    assert fs.normalize_selection_mode("all") == "all"
    assert fs.normalize_selection_mode("INTERACTIVE") == "interactive"
    for bad in ("auto", "explicit", "ask", "1", "true", "selective"):
        with pytest.raises(ValueError):
            fs.normalize_selection_mode(bad)


# --------------------------------------------------------------------------- #
# §17 — the headless/API matrix, exercised through the real engine
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_default_all_never_creates_a_selection_generation_or_offer(api):
    transfer_id = await _submit(api, selection_mode="all")
    assert await _selection_row(api, transfer_id) is None
    assert await api.repository.file_selection_presentation(transfer_id, now=api.clock()) is None
    assert await api.repository.active_file_selection_offers(now=api.clock()) == []
    import db.database as database
    async with database.get_db() as db:
        events = await db.fetchall(
            "SELECT 1 FROM application_events WHERE transfer_id=? AND kind='file_selection_available'",
            (transfer_id,))
    assert events == []
    # ALL materialises normally: every file becomes a child request.
    members = sorted(r.entry.relative_path for r in await api.repository.requests(transfer_id) if r.parent_id)
    assert members == ["S/a.mkv", "S/b.mkv", "S/c.mkv"]


@pytest.mark.asyncio
async def test_explicit_interactive_enters_the_selection_lifecycle(api):
    transfer_id = await _submit(api, selection_mode="interactive")
    row = await _selection_row(api, transfer_id)
    assert row is not None and row["decision"] == "pending"
    assert row["hold_until"] is not None
    view = await api.repository.file_selection_presentation(transfer_id, now=api.clock())
    assert view["eligible"] is True and view["auto_offer"] is True
    # Nothing materialises until the decision settles.
    assert [r for r in await api.repository.requests(transfer_id) if r.parent_id] == []


@pytest.mark.asyncio
async def test_invalid_selection_mode_is_rejected_at_the_engine_boundary(api):
    with pytest.raises(ValueError):
        await api.application.submit_magnet(
            "magnet:?xt=urn:btih:" + "a" * 40, selection_mode="sometimes")
    with pytest.raises(ValueError):
        await api.application.submit_torrent(b"d4:junke", "x.torrent", selection_mode="maybe")


@pytest.mark.asyncio
async def test_selection_mode_does_not_change_dedupe_or_fingerprint_identity(api):
    from transfers.repository import TransferRepository

    repo = TransferRepository()
    a, created_a = await repo.admit(
        (TransferRequest("magnet", "magnet:?xt=urn:btih:" + "b" * 40, fingerprint="b" * 40,
                         selection_mode="all"),),
        name="x", source="manual")
    b, created_b = await repo.admit(
        (TransferRequest("magnet", "magnet:?xt=urn:btih:" + "b" * 40, fingerprint="b" * 40,
                         selection_mode="interactive"),),
        name="x", source="manual")
    assert created_a is True and created_b is False        # same logical source
    assert a.id == b.id
    import db.database as database
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT hash, source_fingerprint FROM torrents WHERE id=?", (a.id,))
    assert rows[0]["hash"] == "b" * 40 and rows[0]["source_fingerprint"] == "b" * 40


@pytest.mark.asyncio
async def test_selection_mode_survives_serialization_round_trip(api):
    from transfers import codec
    original = TransferRequest("torrent", b"data", name="x", fingerprint="f", selection_mode="interactive")
    restored = codec.request(codec.load(codec.dump(original)))
    assert restored.selection_mode == "interactive"
    # A legacy payload with no selection_mode decodes to the ALL default.
    legacy = codec.load(codec.dump(original))
    legacy.pop("selection_mode")
    assert codec.request(legacy).selection_mode == "all"


# --------------------------------------------------------------------------- #
# §6 / §17 — the built-in API routes carry the field
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_add_magnet_route_passes_selection_mode_through(api, monkeypatch):
    captured = {}

    async def _fake_submit_magnet(magnet, *, source="manual", selection_mode="all"):
        captured["magnet"] = magnet
        captured["selection_mode"] = selection_mode
        return {"id": 1, "status": "pending"}

    monkeypatch.setattr(api.application, "submit_magnet", _fake_submit_magnet)

    r1 = await api.client.post("/api/torrents/add-magnet", json={"magnet": "magnet:?xt=urn:btih:x"})
    assert r1.status_code == 200
    assert captured["selection_mode"] is None                # omitted -> service default ALL

    r2 = await api.client.post("/api/torrents/add-magnet",
                               json={"magnet": "magnet:?xt=urn:btih:x", "selection_mode": "interactive"})
    assert r2.status_code == 200
    assert captured["selection_mode"] == "interactive"


@pytest.mark.asyncio
async def test_add_file_route_accepts_selection_mode_form_field(api, monkeypatch):
    captured = {}

    async def _fake_submit_torrent(data, filename, *, source="manual_file", selection_mode="all"):
        captured["selection_mode"] = selection_mode
        return {"id": 2, "status": "pending"}

    monkeypatch.setattr(api.application, "submit_torrent", _fake_submit_torrent)

    files = {"file": ("t.torrent", b"d4:testi1ee", "application/x-bittorrent")}
    r1 = await api.client.post("/api/torrents/add-file", files=files)
    assert r1.status_code == 200
    assert captured["selection_mode"] is None                # historical upload -> ALL

    r2 = await api.client.post("/api/torrents/add-file", files=files,
                               data={"selection_mode": "interactive"})
    assert r2.status_code == 200
    assert captured["selection_mode"] == "interactive"


@pytest.mark.asyncio
async def test_add_magnet_route_rejects_an_invalid_selection_mode(api):
    r = await api.client.post("/api/torrents/add-magnet",
                              json={"magnet": "magnet:?xt=urn:btih:" + "a" * 40,
                                    "selection_mode": "bogus"})
    assert r.status_code == 400
