"""The download list says where a switch accepted while paused will go
(transfer 1086).

A source switch made while a download is paused is accepted and durably
recorded -- the file SELECTS another candidate while its parked writer keeps
serving its own (``TransferEngine.pending_source``) -- and Resume completes
it. Until then the list row's provider and source are, truthfully, the
committed ones; its ``deferred_switch`` names the accepted destination: the
selected candidate's provider and, when its source is a host, that host. It
follows the latest accepted selection, disappears when Resume completes it or
the operator selects the writer's own candidate again, and names nothing when
the transfer's paused files disagree.

Driven through the real engine and repository with the byte-moving executor
that parks across Pause (``continuation_fakes``)."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_v113_native_continuation as native
from continuation_fakes import MIB, SPARSE_NATIVE, SpoolProvider, payload
from test_v113_multi_root_provider_switch import listed

from transfers.manual_failover import manual_candidate_failover
from transfers.models import SourceIdentity, TransferRequest

pytestmark = pytest.mark.asyncio

# The fixture registers no integration definitions: names are those of a
# provider without one (``None``; the badge falls back to the id).
HOSTS = {"src-a": "1fichier.com", "src-b": "rapidgator.net", "src-c": "vikingfile.com", "src-z": "pixeldrain.com"}


class HostSpoolProvider(SpoolProvider):
    """The spool provider, naming the upstream host each provider serves."""

    async def resolve(self, request):
        result = await super().resolve(request)
        return replace(result, candidates=tuple(
            replace(item, source_identity=SourceIdentity("host", HOSTS[self.descriptor.id]))
            for item in result.candidates))


async def paused_with_alternates(tmp_path, monkeypatch, *, hosts=True):
    if hosts:
        monkeypatch.setattr(native, "SpoolProvider", HostSpoolProvider)
    ctx = await native.build(tmp_path, monkeypatch, continuation=SPARSE_NATIVE)
    transfer, _first = await native.running_sparse(ctx)
    await native.attach(ctx, transfer, "src-b")
    artifact, _ = await native.attach(ctx, transfer, "src-c")
    ctx.transfer = transfer
    await ctx.engine.pause(transfer.id)
    return ctx, artifact


def on(artifact, provider_id):
    return str(next(item for item in artifact.candidates if item.provider_id == provider_id).id)


async def row(ctx):
    return await listed(SimpleNamespace(engine=ctx.engine, repository=ctx.repository, transfer=ctx.transfer))


async def test_a_paused_host_switch_names_its_destination_beside_the_committed_route(tmp_path, monkeypatch):
    ctx, artifact = await paused_with_alternates(tmp_path, monkeypatch)
    before = await row(ctx)
    assert "deferred_switch" not in before
    await manual_candidate_failover(ctx.engine, ctx.transfer.id, artifact.id, on(artifact, "src-b"))
    item = await row(ctx)
    assert item["deferred_switch"] == {"provider_id": "src-b", "provider_name": None, "host": "rapidgator.net"}
    # The committed route is still what the row names: nothing moved yet.
    assert item["active_provider_id"] == before["active_provider_id"] == "src-a"
    assert item["current_source_identity"] == before["current_source_identity"]


async def test_the_latest_accepted_selection_replaces_the_destination_and_its_own_source_clears_it(
        tmp_path, monkeypatch):
    ctx, artifact = await paused_with_alternates(tmp_path, monkeypatch)
    await manual_candidate_failover(ctx.engine, ctx.transfer.id, artifact.id, on(artifact, "src-b"))
    await manual_candidate_failover(ctx.engine, ctx.transfer.id, artifact.id, on(artifact, "src-c"))
    assert (await row(ctx))["deferred_switch"]["host"] == "vikingfile.com"
    # Selecting the parked writer's own source withdraws the transition.
    await manual_candidate_failover(ctx.engine, ctx.transfer.id, artifact.id, on(artifact, "src-a"))
    assert "deferred_switch" not in await row(ctx)


async def test_resume_completing_the_switch_clears_the_destination_and_moves_the_route(tmp_path, monkeypatch):
    ctx, artifact = await paused_with_alternates(tmp_path, monkeypatch)
    await manual_candidate_failover(ctx.engine, ctx.transfer.id, artifact.id, on(artifact, "src-c"))
    assert "deferred_switch" in await row(ctx)
    await ctx.engine.resume(ctx.transfer.id)
    await ctx.engine.reconcile_executions()
    await ctx.engine.tick()
    item = await row(ctx)
    assert "deferred_switch" not in item
    assert item["active_provider_id"] == "src-c"


async def test_a_switch_while_downloading_is_never_reported_as_deferred(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "SpoolProvider", HostSpoolProvider)
    ctx = await native.build(tmp_path, monkeypatch, continuation=SPARSE_NATIVE)
    transfer, _first = await native.running_sparse(ctx)
    artifact, _ = await native.attach(ctx, transfer, "src-b")
    ctx.transfer = transfer
    await manual_candidate_failover(ctx.engine, transfer.id, artifact.id, on(artifact, "src-b"))
    assert "deferred_switch" not in await row(ctx)


async def test_a_destination_whose_source_is_not_a_host_names_no_host(tmp_path, monkeypatch):
    ctx, artifact = await paused_with_alternates(tmp_path, monkeypatch, hosts=False)
    await manual_candidate_failover(ctx.engine, ctx.transfer.id, artifact.id, on(artifact, "src-b"))
    assert (await row(ctx))["deferred_switch"] == {"provider_id": "src-b", "provider_name": None, "host": None}


async def test_paused_files_switched_to_different_destinations_name_none(tmp_path, monkeypatch):
    """Two files of one transfer, each parked and each switched while paused --
    to different providers: the row names no destination rather than one."""
    monkeypatch.setattr(native, "SpoolProvider", HostSpoolProvider)
    ctx = await native.build(tmp_path, monkeypatch, continuation=SPARSE_NATIVE)
    ctx.native.sources["second"] = payload(3 * MIB + 7, seed="second")
    transfer = await ctx.engine.submit((
        TransferRequest("spool", "movie", name="movie.bin", preferred_provider="src-a"),
        TransferRequest("spool", "second", name="second.bin", preferred_provider="src-a")), deduplicate=False)
    await ctx.engine.tick()
    for name in ("movie", "second"):
        for provider in ("src-b", "src-c"):
            await ctx.engine.submit((TransferRequest("spool", name, name=f"{name}.bin", preferred_provider=provider),),
                                    deduplicate=False)
    await ctx.engine.resolve_pending()
    ctx.transfer = transfer
    files = sorted(await ctx.repository.artifacts(transfer.id), key=lambda item: item.name)
    assert len(files) == 2 and all(item.execution is not None for item in files)
    await ctx.engine.pause(transfer.id)
    await manual_candidate_failover(ctx.engine, transfer.id, files[0].id, on(files[0], "src-b"))
    item = await row(ctx)
    assert item["deferred_switch"]["provider_id"] == "src-b"           # one destination so far
    await manual_candidate_failover(ctx.engine, transfer.id, files[1].id, on(files[1], "src-c"))
    assert "deferred_switch" not in await row(ctx)
    await manual_candidate_failover(ctx.engine, transfer.id, files[1].id, on(files[1], "src-b"))
    assert (await row(ctx))["deferred_switch"]["host"] == "rapidgator.net"   # agreeing again: one destination
