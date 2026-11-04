"""Real runtime proof (packaged aria2): saved Network Sources tuning reaches
existing DP-owned jobs in place, keeping identity, bytes and guarded route.

Real owners end to end: the convergence engine and repository, the real
``Aria2Executor`` driving a real ``aria2c`` (the packaged 1.37.0+dp2 in CI's
runtime layer) through the real ``DownloaderEgressGuard`` (only its resolver
maps the fixture hostnames to loopback), and ``Aria2Administration``'s pass. A
Save is modelled exactly as it reaches the executor: the daemon-wide tuning
changes (``changeGlobalOption``) and the executor holds the new values.

* An active job keeps its GID, attempt and committed material, each segment
  reconnecting where it was; its guarded route options are unchanged; it
  completes byte-exact as the one execution. A second pass sends nothing.
* Waiting and paused jobs take the values without starting, moving or
  unpausing.
* A provider-issued link that stopped admitting new connections fails on the
  tuning-forced reconnect; the core's ordinary recovery refreshes it and the
  transfer completes, the provider never exhausted. A link that stays dead
  still ends in the ordinary operator wait -- nothing suppresses it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import db.database as database
from executors.aria2.admin import Aria2Administration
from test_v113_native_continuation_runtime import BODY, MIB, completed, refetched, runtime, start_origin
from transfers import codec
from transfers.applicability import ProviderApplicability
from transfers.models import (
    Capability, Endpoint, IntegrationDescriptor, IntegrityMetadata, ResolutionResult, ResourceState, SourceIdentity,
    TransferCandidate, TransferRequest, TransferState,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.real_runtime]
TUNING = ("max-connection-per-server", "split", "min-split-size")
# Every per-job option that carries the guarded route, the destination or the
# job's transport policy: compared by digest only, never printed.
GUARDED = ("all-proxy", "all-proxy-user", "all-proxy-passwd", "http-proxy", "http-proxy-user", "http-proxy-passwd",
           "https-proxy", "https-proxy-user", "https-proxy-passwd", "ftp-proxy", "ftp-proxy-user",
           "ftp-proxy-passwd", "no-proxy", "proxy-method", "dir", "out", "continue", "allow-overwrite",
           "auto-file-renaming", "header", "max-http-redirection", "max-tries", "check-certificate")
SAVED = {"split": 2, "connections_per_server": 2, "minimum_split_size": "2M"}


async def guarded_digest(service, gid):
    options = await service._call("aria2.getOption", [gid])
    try:
        return hashlib.sha256(json.dumps({name: options.get(name) for name in GUARDED},
                                         sort_keys=True).encode()).hexdigest()
    finally:
        options.clear()


async def save(rt):
    """The Save, as it reaches aria2 and the executor: the daemon-wide tuning
    (``apply_memory_tuning``) and the executor's configuration."""
    await rt.service.change_global_options({"split": "2", "max-connection-per-server": "2", "min-split-size": "2M"})
    rt.executor.configuration = replace(rt.executor.configuration, **SAVED)


def administration(rt):
    return Aria2Administration(rt.executor, rt.repository, None, SimpleNamespace(tuning_lock=asyncio.Lock()))


async def ticking(rt, stop):
    while not stop.is_set():
        await rt.engine.tick()
        await asyncio.sleep(0.2)


async def submitted(rt, kind, payload, *, status, at_least=0):
    transfer = await rt.engine.submit((TransferRequest(kind, payload),), deduplicate=False)

    async def reached():
        attempts = await rt.repository.executions(transfer.id)
        if not attempts or attempts[-1].handle.native is None:
            return None
        try:
            native = await rt.service.tell_status(attempts[-1].handle.native["gid"])
        except Exception:
            return None
        return attempts[-1] if native.status == status and native.completed_length >= at_least else None

    return transfer, await rt.until(reached, label=f"{kind} job {status}")


async def test_real_active_job_takes_saved_tuning_in_place_keeping_identity_bytes_and_guard(tmp_path, monkeypatch):
    server, port, requests = await start_origin(rate=2 * MIB)
    rt = await runtime(tmp_path, monkeypatch)
    stop = asyncio.Event()
    try:
        transfer, attempt = await submitted(rt, "http", f"http://mirror-a.test:{port}/movie.bin",
                                            status="active", at_least=4 * MIB)
        gid = attempt.handle.native["gid"]
        artifact = (await rt.repository.artifacts(transfer.id))[0]

        async def material():
            state = await rt.repository.material_state(artifact.id)
            return state.valid if state is not None and state.valid else None

        committed = await rt.until(material, label="DP-committed material")
        guard = await guarded_digest(rt.service, gid)
        background = asyncio.create_task(ticking(rt, stop))
        await save(rt)
        # Baseline: the daemon-wide change alone never reaches the existing job.
        assert await rt.service.get_options(gid, TUNING) == {
            "max-connection-per-server": "4", "split": "4", "min-split-size": str(1 * MIB)}
        before = len(requests)

        result = await administration(rt).converge_job_tuning()
        assert (result["selected"], result["applied"]) == (1, 1)
        assert await rt.service.get_options(gid, TUNING) == {
            "max-connection-per-server": "2", "split": "2", "min-split-size": str(2 * MIB)}
        assert await guarded_digest(rt.service, gid) == guard
        native = await rt.service.tell_status(gid)
        assert native.status == "active"

        # A second pass finds nothing to change and sends nothing, so aria2 has
        # nothing to restart.
        changes = []
        change_option = rt.service.change_option

        async def counted(*args):
            changes.append(args)
            return await change_option(*args)

        rt.service.change_option = counted
        again = await administration(rt).converge_job_tuning()
        assert (again["selected"], again["unchanged"], again["applied"]) == (1, 1, 0) and changes == []
        stop.set()
        await background

        await completed(rt, transfer)
        attempts = await rt.repository.executions(transfer.id)
        assert [item.handle.native["gid"] for item in attempts] == [gid]
        assert attempts[0].handle.attempt_id == attempt.handle.attempt_id
        # The reconnected segments continued where they were: nothing DP had
        # committed was fetched again, and the file is byte-exact.
        resumed = requests[before:]
        assert any(item.ranged for item in resumed)
        assert not refetched(resumed, committed)
        assert Path(artifact.target).read_bytes() == BODY
    finally:
        stop.set()
        await rt.close()
        server.close()


async def test_real_waiting_and_paused_jobs_take_the_tuning_without_starting_or_moving(tmp_path, monkeypatch):
    server, port, _requests = await start_origin(rate=1 * MIB)
    rt = await runtime(tmp_path, monkeypatch)
    try:
        await rt.service.change_global_options({"max-concurrent-downloads": "1"})
        await submitted(rt, "http", f"http://mirror-a.test:{port}/first.bin", status="active")
        _second, waiting = await submitted(rt, "http", f"http://mirror-b.test:{port}/second.bin", status="waiting")
        third, paused = await submitted(rt, "http", f"http://mirror-c.test:{port}/third.bin", status="waiting")
        await rt.engine.pause(third.id)
        paused_gid, waiting_gid = paused.handle.native["gid"], waiting.handle.native["gid"]

        async def parked():
            return (await rt.service.tell_status(paused_gid)).status == "paused"

        await rt.until(parked, label="third job paused")
        queue = [item["gid"] for item in await rt.service._call("aria2.tellWaiting", [0, 10, ["gid"]])]
        progress = {gid: (await rt.service.tell_status(gid)).completed_length for gid in (waiting_gid, paused_gid)}

        await save(rt)
        result = await administration(rt).converge_job_tuning()
        assert (result["selected"], result["applied"]) == (3, 3)
        for gid, status in ((waiting_gid, "waiting"), (paused_gid, "paused")):
            assert await rt.service.get_options(gid, TUNING) == {
                "max-connection-per-server": "2", "split": "2", "min-split-size": str(2 * MIB)}
            native = await rt.service.tell_status(gid)
            assert native.status == status and native.completed_length == progress[gid]
        assert [item["gid"] for item in await rt.service._call("aria2.tellWaiting", [0, 10, ["gid"]])] == queue
        assert (await rt.repository.get(third.id)).state == TransferState.PAUSED
    finally:
        await rt.close()
        server.close()


# ── a provider-issued link and a tuning-forced reconnect ────────────────────

class SigningOrigin:
    """BODY at ``/sig/<token>/movie.bin`` while ``<token>`` admits NEW
    requests; one already streaming continues after its token is revoked."""

    def __init__(self, rate):
        self.rate, self.valid, self.log = rate, set(), []

    def issue(self):
        token = secrets.token_hex(8)
        self.valid.add(token)
        return token

    async def start(self):
        async def handle(reader, writer):
            try:
                raw = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)).decode("iso-8859-1")
                path = raw.split(" ", 2)[1]
                token = path.split("/")[2] if path.startswith("/sig/") else ""
                start, end = 0, len(BODY)
                for line in raw.split("\r\n"):
                    name, _, value = line.partition(":")
                    if name.casefold() == "range":
                        first, _, last = value.split("=", 1)[1].strip().partition("-")
                        start, end = int(first), (int(last) + 1 if last else len(BODY))
                admitted = token in self.valid
                self.log.append((token, admitted))
                if not admitted:
                    writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    return
                partial = (start, end) != (0, len(BODY))
                head = b"HTTP/1.1 206 Partial Content\r\n" if partial else b"HTTP/1.1 200 OK\r\n"
                head += f"Content-Length: {end - start}\r\nAccept-Ranges: bytes\r\n".encode()
                if partial:
                    head += f"Content-Range: bytes {start}-{end - 1}/{len(BODY)}\r\n".encode()
                writer.write(head + b"Connection: close\r\n\r\n")
                for offset in range(start, end, 65536):
                    writer.write(BODY[offset:min(end, offset + 65536)])
                    await writer.drain()
                    await asyncio.sleep(65536 / self.rate)
            except (ConnectionError, asyncio.IncompleteReadError, TimeoutError):
                pass
            finally:
                writer.close()

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = int(self.server.sockets[0].getsockname()[1])
        return self


class SigningProvider:
    """Debrid-like: each resolve or refresh issues a NEW signed link to the
    same bytes (same strong integrity), stored as the candidate's endpoint."""

    def __init__(self, origin):
        self.descriptor = IntegrationDescriptor("signer", "signer", frozenset({Capability.RESOLVE}),
                                                request_types=frozenset({"signed"}))
        self.origin, self.issues, self.calls = origin, True, []

    @property
    def applicability(self):
        return ProviderApplicability()

    def _candidate(self, request, candidate_id=None):
        token = self.origin.issue() if self.issues else "revoked"
        extra = {} if candidate_id is None else {"id": candidate_id}
        return TransferCandidate(
            "movie.bin", (Endpoint("http", f"http://cdn.test:{self.origin.port}/sig/{token}/movie.bin"),),
            expected_bytes=len(BODY), provider_id=self.descriptor.id,
            integrity=(IntegrityMetadata("sha256", hashlib.sha256(BODY).hexdigest()),),
            source_identity=SourceIdentity("signed", f"signer:{request.payload}"), **extra)

    async def resolve(self, request):
        self.calls.append("resolve")
        return ResolutionResult(ResourceState.AVAILABLE, (self._candidate(request),))

    async def refresh(self, candidate):
        self.calls.append("refresh")
        return ResolutionResult(ResourceState.AVAILABLE, (self._candidate(candidate.refresh_request, candidate.id),))


async def recovery(transfer_id):
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT detail FROM application_events WHERE kind='recovery_audit' AND transfer_id=?"
                                 " ORDER BY id", (transfer_id,))
        routes = await db.fetchall("SELECT provider_id, state FROM resolution_attempts")
    actions = []
    for row in rows:
        try:
            detail = codec.load(row["detail"]) if row["detail"] else {}
        except Exception:
            continue
        if isinstance(detail, dict) and detail.get("action"):
            actions.append((detail["action"], detail.get("reason")))
    return actions, [(row["provider_id"], row["state"]) for row in routes]


@pytest.mark.parametrize("dead", [False, True], ids=["link_expired_during_transfer", "control_link_stays_dead"])
async def test_real_tuning_reconnect_on_an_expired_link_follows_ordinary_recovery(tmp_path, monkeypatch, dead):
    origin = await SigningOrigin(2 * MIB).start()
    provider = SigningProvider(origin)
    rt = await runtime(tmp_path, monkeypatch)
    rt.registry.register_provider(provider)
    try:
        transfer, attempt = await submitted(rt, "signed", "movie", status="active", at_least=4 * MIB)
        # The link stops admitting new connections; the running ones continue.
        origin.valid.clear()
        if dead:
            provider.issues = False  # independent evidence: no fresh link works either
        await save(rt)
        result = await administration(rt).converge_job_tuning()
        assert result["selected"] == 1 and result["applied"] + result["interrupted"] == 1

        async def settled():
            current = await rt.repository.get(transfer.id)
            if current.state in {TransferState.COMPLETED, TransferState.FAILED}:
                return current
            actions, _routes = await recovery(transfer.id)
            return current if dead and ("wait_for_operator", "recovery_budget_exhausted") in actions else None

        final = await rt.until(settled, label="recovery settled", timeout=120)
        actions, routes = await recovery(transfer.id)
        failed = await rt.service.tell_status(attempt.handle.native["gid"])
        # What aria2 reports for the forced reconnect: its HTTP failure (code 22,
        # worded "status=403" or "No URI available." once every URI refused).
        assert (failed.status, failed.error_code) == ("error", "22")
        assert any(admitted is False for _token, admitted in origin.log)
        # The provider's route was never exhausted, and no other provider was tried.
        assert routes and all(state != "exhausted" for _provider, state in routes)
        assert set(provider.calls) == {"resolve", "refresh"} and provider.calls.count("resolve") == 1
        assert any(action == "refresh_candidate" for action, _reason in actions)
        assert not any(action == "try_alternate_candidate" for action, _reason in actions)
        artifact = (await rt.repository.artifacts(transfer.id))[0]
        if dead:
            assert final.state != TransferState.COMPLETED
            assert ("wait_for_operator", "recovery_budget_exhausted") in actions
        else:
            assert final.state == TransferState.COMPLETED
            assert Path(artifact.target).read_bytes() == BODY
            plans = [await rt.repository.execution_continuation(item.handle.attempt_id)
                     for item in await rt.repository.executions(transfer.id)]
            # The replacement writer kept what DP had committed.
            assert plans[-1] is not None and plans[-1].retained_bytes > 0 and plans[-1].discarded == ()
    finally:
        await rt.close()
        origin.server.close()
