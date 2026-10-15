"""A Real-Debrid-style backup prepares with the root's ESTABLISHED selection
and, once ready, is promoted by the one readiness yield (1.0.13).

With Prepare Backup Torrents on, a provider that executes only what is
selected on its own resource (``contracts.UpstreamSelection``) is handed --
on its backup's ordinary observation -- exactly the root's established
decision: every file for a select-all submission, the operator's subset
unaltered, nothing at all while nothing is decided. It then prepares like
any backup, its observation reports it ready, and ``_readiness_promotion``
takes the still-preparing route over to it. The switch menu words each state
(Waiting for selection / Preparing / Ready) without changing what may be
switched to. Every provider is a local fake.
"""
from __future__ import annotations

import pytest
from test_v113_readiness_promotion import history
from test_v113_realdebrid_upstream_selection import (
    FILES_592, MOVIE, FakeRealDebrid, choose, lab, members, preparing_parcel, root, submit, ticks,
)

from transfers.manual_route_switch import route_providers
from providers.realdebrid.provider import RealDebridProvider

pytestmark = pytest.mark.asyncio
POLL = 30.0
FILES = [(path.rsplit("/", 1)[-1], path, size) for _i, path, size in FILES_592]


async def world(tmp_path, monkeypatch, *, ready=True):
    fake = FakeRealDebrid(ready=ready)
    first = preparing_parcel("parcel-a", FILES)
    repository, engine = await lab(tmp_path, monkeypatch, first, RealDebridProvider(fake, prepare_backup_torrents=True))
    return fake, repository, engine


async def menu(engine, transfer):
    view = await route_providers(engine, transfer.id)
    return {entry["provider_id"]: (entry["status"], entry["selectable"]) for entry in view["providers"]}


async def test_a_select_all_root_s_backup_is_selected_whole_and_promoted_once_ready(tmp_path, monkeypatch):
    fake, repository, engine = await world(tmp_path, monkeypatch)
    transfer = await submit(engine, interactive=False)                    # every file: the ALL default
    await ticks(engine, 6, step=POLL)
    assert fake.count("add_magnet") == 1                                  # the one backup torrent, adopted
    assert fake.selects() == [",".join(str(file_id) for file_id, _path, _size in FILES_592)]
    current = await root(repository, transfer)
    assert current.resource.provider_id == "realdebrid"
    assert (await history(current.id))[-1]["operation"] == "readiness_promotion"
    assert len(await members(repository, transfer)) == len(FILES_592)


async def test_an_operator_subset_is_handed_unaltered_and_promoted(tmp_path, monkeypatch):
    fake, repository, engine = await world(tmp_path, monkeypatch)
    transfer = await submit(engine)                                       # interactive
    await ticks(engine, 4, step=POLL)
    assert fake.selects() == []                                           # nothing decided: nothing sent
    assert (await menu(engine, transfer))["realdebrid"] == ("waiting_for_selection", True)
    await choose(repository, engine, transfer, {MOVIE})
    await ticks(engine, 4, step=POLL)
    assert fake.selects() == ["3"]                                        # exactly the movie, never more
    current = await root(repository, transfer)
    assert current.resource.provider_id == "realdebrid"
    assert (await history(current.id))[-1]["operation"] == "readiness_promotion"
    assert [path for path, _payload in await members(repository, transfer)] == [MOVIE]
    assert fake.count("add_magnet") == 1


async def test_an_undecided_root_s_backup_is_never_selected_never_ready_never_promoted(tmp_path, monkeypatch):
    fake, repository, engine = await world(tmp_path, monkeypatch)
    transfer = await submit(engine)
    await ticks(engine, 8, step=POLL)
    assert fake.selects() == []
    assert (await root(repository, transfer)).resource.provider_id == "parcel-a"
    assert (await menu(engine, transfer))["realdebrid"] == ("waiting_for_selection", True)


async def test_a_backup_acquiring_the_established_selection_is_preparing_then_ready(tmp_path, monkeypatch):
    """While Real-Debrid acquires the selection the menu says Preparing --
    still the selectable takeover it always was -- and once it is ready the
    root is promoted onto it."""
    fake, repository, engine = await world(tmp_path, monkeypatch, ready=False)
    transfer = await submit(engine, interactive=False)
    await ticks(engine, 6, step=POLL)
    assert len(fake.selects()) == 1
    assert (await root(repository, transfer)).resource.provider_id == "parcel-a"
    assert (await menu(engine, transfer))["realdebrid"] == ("preparing", True)
    for torrent in fake.torrents.values():
        torrent.ready = True
    await ticks(engine, 4, step=POLL)
    assert (await root(repository, transfer)).resource.provider_id == "realdebrid"
    assert len(fake.selects()) == 1                                       # selected once, never re-sent
