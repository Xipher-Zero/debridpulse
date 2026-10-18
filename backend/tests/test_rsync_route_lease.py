"""1.0.13 WS7 SEC-003: rsync's proxy capability lasts exactly one invocation.

rsync accepts its proxy only from its environment (``RSYNC_PROXY``), so the
guard credential of a daemon route is handed to it there. That credential
was a pure function of the endpoint: the same for every attempt, and valid
for the guard's whole lifetime -- once read from an attempt's environment it
could be replayed after the attempt ended. It is now leased to one
invocation: distinct per invocation, admitting only that invocation's one
authorized host and port (under the guard's unchanged resolution and address
policy), and revoked once the invocation's process has ended. A tunnel the
invocation already opened is its own; aria2 job credentials and in-process
tunnels are unchanged.
"""
from __future__ import annotations

import asyncio
import base64
import uuid

import pytest

from test_v113_rsync_executor import (PAYLOAD, _candidate, _executor, _loopback_destinations, _settle,  # noqa: F401
                                      daemon, file_request, spawned)
from test_v113_transport_evidence_sampling import guard_for
from transfers.models import ExecutionSubject

pytestmark = pytest.mark.asyncio


async def _connect(guard, authority: str, user: str, token: str):
    reader, writer = await asyncio.open_connection("127.0.0.1", guard.bound_port)
    credential = base64.b64encode(f"{user}:{token}".encode()).decode()
    writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n"
                 f"Proxy-Authorization: Basic {credential}\r\n\r\n".encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
    return int(head.split(b" ", 2)[1]), reader, writer


async def _status(guard, authority, user, token) -> int:
    status, _reader, writer = await _connect(guard, authority, user, token)
    writer.close()
    return status


async def test_a_leased_capability_admits_its_endpoint_only_while_its_invocation_holds_it():
    async def echo(reader, writer):
        while data := await reader.read(1024):
            writer.write(data)
            await writer.drain()
        writer.close()

    origin = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = origin.sockets[0].getsockname()[1]
    guard, private = guard_for(), guard_for(public=())
    await guard.ensure_started()
    await private.ensure_started()
    try:
        uri, authority = f"rsync://files.test:{port}/m/f", f"files.test:{port}"
        first, second = uuid.uuid4().hex, uuid.uuid4().hex
        _h, _p, user, token = guard.proxy_credential(uri, connect_timeout_seconds=5.0, budget="rsync", lease=first)
        _h, _p, other_user, other = guard.proxy_credential(uri, connect_timeout_seconds=5.0, budget="rsync",
                                                           lease=second)
        assert (user, token) != (other_user, other)            # one capability per invocation, never shared
        status, reader, writer = await _connect(guard, authority, user, token)
        assert status == 200
        assert await _status(guard, f"files.test:{port + 1}", user, token) != 200      # another port
        assert await _status(guard, authority, user, other) != 200                      # another lease's token
        _h, _p, lan_user, lan_token = private.proxy_credential(uri, lease=uuid.uuid4().hex)
        assert await _status(private, authority, lan_user, lan_token) != 200           # a private address

        guard.revoke_proxy_credential(first)
        writer.write(b"still-mine")                            # the invocation's own tunnel is unaffected
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(10), timeout=5) == b"still-mine"
        writer.close()
        assert await _status(guard, authority, user, token) != 200                      # replay after its end
        assert await _status(guard, authority, other_user, other) == 200                # the other invocation
        # Credentials without a lease (aria2 jobs) are exactly as before.
        _h, _p, plain_user, plain = guard.proxy_credential(uri, connect_timeout_seconds=5.0, budget="rsync")
        assert await _status(guard, authority, plain_user, plain) == 200
        with pytest.raises(ValueError):
            guard.proxy_credential(uri, lease="not-a-lease")
    finally:
        await guard.stop()
        await private.stop()
        origin.close()
        await origin.wait_closed()


def _route(env: dict) -> tuple[str, str]:
    credential = env["RSYNC_PROXY"].rsplit("@", 1)[0]
    user, token = credential.split(":", 1)
    return user, token


@pytest.mark.real_runtime
async def test_a_real_rsync_attempt_reaches_its_source_and_its_capability_ends_with_it(tmp_path, daemon, spawned):  # noqa: F811
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    candidate = _candidate(origin.url("/pub/payload.bin"), size=len(PAYLOAD))
    target = tmp_path / "downloads" / "payload.bin"
    request = file_request(candidate, target, "attempt-lease", root=tmp_path / "downloads")
    handle = executor.prepare(request)
    assert (await executor.start(request, handle)).state.value == "running"
    done = await _settle(executor, handle)
    assert done.state.value == "succeeded" and target.read_bytes() == PAYLOAD      # it reached its source
    env = next(env for argv, env, _kwargs in spawned if "--append" in argv)
    user, token = _route(env)
    # After the attempt's verified end its capability admits nothing.
    assert guard._admits(user, token, "rsync-origin.test", origin.port) is None
    assert guard._leases == set()

    listed = await executor.discover(ExecutionSubject.of(candidate))
    assert listed.expected_bytes == len(PAYLOAD)               # a listing reaches its source too
    assert guard._leases == set()                              # and its lease ends with its invocation


def _gated_executor(tmp_path, guard, decide):
    """The real executor whose authorization ``decide`` answers once its
    lease exists: the authorization check that follows the transport."""
    from executors.rsync.executor import RsyncConfiguration, RsyncExecutor

    (tmp_path / "downloads").mkdir(exist_ok=True)
    configuration = RsyncConfiguration(str(tmp_path / "downloads"), str(tmp_path / "runtime"),
                                       connection_timeout_seconds=10, transfer_timeout_seconds=30)

    async def authorize(_handle, action):
        return await decide(action) if guard._leases else True

    executor = RsyncExecutor(configuration, authorize, egress=guard)
    spawned_here = []

    async def spawn(*args, **kwargs):
        spawned_here.append(args)
        raise AssertionError("no native process may start")

    executor.processes.spawn = spawn
    return executor, spawned_here


@pytest.mark.parametrize("ending", ["cancelled", "raised", "refused"])
async def test_a_lease_never_outlives_a_start_that_ends_before_its_process(tmp_path, ending):
    """The post-transport authorization check is held with the lease issued,
    then ends -- cancelled, failing, or refusing. No process starts, and the
    lease is revoked before ``start`` returns to its caller."""
    guard = guard_for()
    await guard.ensure_started()
    held, release = asyncio.Event(), asyncio.Event()

    async def decide(action):
        if action != "observe":
            return ending != "refused"                       # the attempt's own authority, refused
        held.set()
        await release.wait()
        if ending == "raised":
            raise RuntimeError("authorization store unavailable")
        return True

    try:
        executor, spawned_here = _gated_executor(tmp_path, guard, decide)
        candidate = _candidate("rsync://rsync-origin.test:873/pub/payload.bin", size=len(PAYLOAD))
        request = file_request(candidate, tmp_path / "downloads" / "payload.bin", "attempt-held",
                               root=tmp_path / "downloads")
        handle = executor.prepare(request)
        started = asyncio.ensure_future(executor.start(request, handle))
        await asyncio.wait_for(held.wait(), timeout=10)
        assert len(guard._leases) == 1                       # issued, and held at the check
        if ending == "cancelled":
            started.cancel()
            with pytest.raises(asyncio.CancelledError):
                await started
        else:
            release.set()
            outcome = await asyncio.wait_for(started, timeout=10)
            assert outcome.state.value in ({"paused"} if ending == "refused" else {"failed", "unknown"})
        assert guard._leases == set()                        # revoked by the time start has returned
        assert spawned_here == []
    finally:
        await guard.stop()
