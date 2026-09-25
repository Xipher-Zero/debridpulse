"""The generic integration ownership fence.

An integration's connection fields cannot change while something still DEPENDS
on the current credential. The fence used to read "any provider_resources row
for this provider whose state is not absent", which conflated live ownership
with history: a completed transfer keeps its resource row, that row routinely
stays ``available``, and so a credential became permanent the moment the
integration was ever used.

These cases pin the corrected meaning. They are written against the generic
predicate with an invented provider identity, because the rule belongs to the
mechanism rather than to any provider; one AllDebrid case at the end proves the
real acceptance path through the real validator.
"""

import pytest
import pytest_asyncio

from db import database
from db.database import get_db
from transfers._repository_base import _TERMINAL_TRANSFER_STATUSES
from transfers.repository import TransferRepository


PROVIDER = "probe_provider"
EXECUTOR = "probe_executor"


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "ownership.db")
    await database.init_db()
    return TransferRepository()


_SEQUENCE = iter(range(1, 10_000))


async def _transfer(status: str) -> int:
    ordinal = next(_SEQUENCE)
    digest = f"probe-hash-{ordinal}"
    async with get_db() as handle:
        await handle.execute(
            "INSERT INTO torrents(hash, name, status) VALUES(?,?,?)",
            (digest, "ownership-probe", status))
        await handle.commit()
        row = await handle.fetchone("SELECT id FROM torrents WHERE hash=?", (digest,))
    return int(row["id"])


async def _resource(transfer_id: int, state: str, *, authority=None, abandoned=0,
                    provider: str = PROVIDER) -> None:
    async with get_db() as handle:
        await handle.execute(
            "INSERT INTO provider_resources(id, transfer_id, provider_id, payload, state,"
            " cleanup_authority, cleanup_abandoned) VALUES(?,?,?,?,?,?,?)",
            (f"res-{transfer_id}-{state}-{authority or 'none'}", transfer_id, provider,
             "{}", state, authority, abandoned))
        await handle.commit()


async def _execution(transfer_id: int, state: str, *, authorized: int = 1) -> None:
    async with get_db() as handle:
        await handle.execute(
            "INSERT INTO download_files(torrent_id, filename) VALUES(?,?)",
            (transfer_id, f"probe-{state}-{authorized}"))
        await handle.commit()
        artifact = await handle.fetchone(
            "SELECT id FROM download_files WHERE torrent_id=? AND filename=?",
            (transfer_id, f"probe-{state}-{authorized}"))
        await handle.execute(
            "INSERT INTO execution_attempts(id, transfer_id, artifact_id, executor_id, handle,"
            " state, authorized) VALUES(?,?,?,?,?,?,?)",
            (f"exec-{transfer_id}-{state}-{authorized}", transfer_id, int(artifact["id"]),
             EXECUTOR, "{}", state, authorized))
        await handle.commit()


def _blocked():
    return TransferRepository().has_integration_references([PROVIDER, EXECUTOR])


# --- history is not ownership -------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("status", sorted(_TERMINAL_TRANSFER_STATUSES))
@pytest.mark.parametrize("state", ["available", "preparing", "unavailable"])
async def test_terminal_transfer_resource_is_provenance_not_ownership(db, status, state):
    """The production case: 66 historical rows must not freeze a credential."""
    await _resource(await _transfer(status), state)
    assert await _blocked() is False


@pytest.mark.asyncio
async def test_many_historical_rows_still_do_not_block(db):
    for index, status in enumerate(_TERMINAL_TRANSFER_STATUSES * 4):
        transfer = await _transfer(status)
        await _resource(transfer, "available", authority=None)
        assert index >= 0
    assert await _blocked() is False


@pytest.mark.asyncio
async def test_abandoned_cleanup_on_a_terminal_transfer_does_not_block(db):
    """Cleanup that will never be retried is finished, not outstanding."""
    await _resource(await _transfer("deleted"), "available",
                    authority="alldebrid", abandoned=1)
    assert await _blocked() is False


# --- real ownership still blocks ----------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "processing", "downloading", "error", "paused"])
async def test_nonterminal_transfer_holding_a_resource_blocks(db, status):
    """FAILED is reopenable, so it is deliberately NOT terminal here."""
    await _resource(await _transfer(status), "available")
    assert await _blocked() is True


@pytest.mark.asyncio
async def test_outstanding_cleanup_blocks_even_on_a_terminal_transfer(db):
    """The credential is exactly what the cleanup owner still needs."""
    await _resource(await _transfer("deleted"), "available", authority="alldebrid")
    assert await _blocked() is True


@pytest.mark.asyncio
async def test_cleared_cleanup_authority_stops_blocking(db):
    transfer = await _transfer("completed")
    await _resource(transfer, "available", authority="alldebrid")
    assert await _blocked() is True
    async with get_db() as handle:
        await handle.execute(
            "UPDATE provider_resources SET cleanup_authority=NULL WHERE transfer_id=?", (transfer,))
        await handle.commit()
    assert await _blocked() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["prepared", "queued", "running", "paused", "unknown"])
async def test_live_authorized_execution_blocks(db, state):
    await _execution(await _transfer("downloading"), state)
    assert await _blocked() is True


@pytest.mark.asyncio
async def test_unauthorized_or_finished_execution_does_not_block(db):
    await _execution(await _transfer("completed"), "running", authorized=0)
    await _execution(await _transfer("completed"), "complete")
    assert await _blocked() is False


# --- the fence is generic ------------------------------------------------

@pytest.mark.asyncio
async def test_another_integration_identity_is_unaffected(db):
    """Ownership is per-identity: one integration's live work is not another's."""
    await _resource(await _transfer("downloading"), "available", provider="other_provider")
    assert await _blocked() is False
    assert await TransferRepository().has_integration_references(["other_provider"]) is True


@pytest.mark.asyncio
async def test_absent_resource_never_blocks_whatever_the_transfer_state(db):
    await _resource(await _transfer("downloading"), "absent")
    assert await _blocked() is False


# --- the real acceptance path, through the real validator ----------------

@pytest.mark.asyncio
async def test_alldebrid_key_change_is_allowed_once_only_history_remains(db, tmp_path, monkeypatch):
    """The reported defect, end to end through ApplicationService.

    AllDebrid disabled, nothing running, nothing to clean up, but a pile of
    completed/deleted transfers still holding their `available` resource rows.
    Both replacing and clearing the key must be accepted -- by the ordinary
    scoped mutation and the ordinary validator, with no AllDebrid exception.
    """
    from types import SimpleNamespace

    from application.service import ApplicationService
    from core.config import AppSettings
    from integrations.configuration import normalize_settings
    from providers.alldebrid.definition import definition as ALLDEBRID

    identity = sorted(ALLDEBRID.owned_identities)[0]
    for status in ("completed", "deleted", "consolidated", "cancelled"):
        for _ in range(3):
            await _resource(await _transfer(status), "available", provider=identity)

    repository = TransferRepository()
    service = ApplicationService(
        SimpleNamespace(repository=repository, dispatch_permitted=True), capacity=None)
    service.definitions = [ALLDEBRID]

    def _settings(key):
        return normalize_settings(
            AppSettings(integrations={ALLDEBRID.id: {"enabled": False, "options": {"api_key": key}}}),
            [ALLDEBRID])

    previous = _settings("old-key")
    # Replace, then clear. Neither raises: nothing depends on the old key.
    await service.validate_configuration(previous, _settings("new-key"))
    await service.validate_configuration(previous, _settings(""))

    # And the fence has not been disarmed -- outstanding cleanup still bites.
    await _resource(await _transfer("deleted"), "available",
                    authority=identity, provider=identity)
    with pytest.raises(ValueError, match="Finish or remove"):
        await service.validate_configuration(previous, _settings("new-key"))
