"""DP 1.0.13 rsync executor: the native boundary, proven against real rsync.

Every case here runs the installed rsync binary against a real rsync daemon
(``RsyncDaemon``) or a real ``rsync --server`` behind an SSH origin
(``RsyncSshOrigin``), through the real ``DownloaderEgressGuard`` (only its
resolver maps fixture hostnames to loopback). The executor is exercised
through the neutral contract only; nothing native crosses it.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import asyncssh
import pytest
import pytest_asyncio

import executors.process_ownership as ownership_module
import executors.rsync.executor as executor_module
from execution_requests import file_request
from executors.rsync.executor import RsyncConfiguration, RsyncExecutor
from rsync_origins import RsyncDaemon, RsyncSshOrigin, free_port, require_rsync, write_tree
from services.artifact_sampling import SAMPLE_BYTES, digest_full, digest_prefix
from test_v113_transport_evidence_sampling import guard_for
from transfers.errors import Category, Domain, Retryability, TransferError
from transfers.input_required import SubmittedInput
from transfers.models import (
    ContinuationCapability, ContinuationPlan, ContinuationStrategy, DiscoveryDepth, Endpoint, ExecutionState, ExecutionSubject,
    FingerprintKind, InputFact, InputFactName, InputField, InputMethod, InputReason, RemoteObjectKind,
    TransferCandidate,
)
from transfers.policy import RecoveryAction, RecoveryContext, TransferPolicy, interpretation_absent

pytestmark = pytest.mark.asyncio

PAYLOAD = os.urandom(3 * 1024 * 1024 + 17)
USER, PASSWORD = "rsync-user-sentinel", "rsync-password-sentinel"
FORBIDDEN_NATIVE = ("--delete", "--remove-source-files", "--recursive", "-r", "-a", "--archive", "-l", "--links",
                    "-L", "--copy-links", "-p", "--perms", "-o", "--owner", "-g", "--group", "-A", "--acls",
                    "-X", "--xattrs", "-D", "--devices", "--specials", "--checksum", "-c", "--inplace", "--sender")


@pytest.fixture(autouse=True)
def _loopback_destinations(monkeypatch):
    async def validated(uri, **_kwargs):
        return uri
    monkeypatch.setattr(executor_module, "validate_resolved_public_destination", validated)


@pytest.fixture
def spawned(monkeypatch):
    """Every native argv and environment the executor hands the OS."""
    calls = []
    real = ownership_module.asyncio.create_subprocess_exec

    async def record(*argv, **kwargs):
        calls.append((list(argv), dict(kwargs.get("env") or {}), kwargs))
        return await real(*argv, **kwargs)

    monkeypatch.setattr(ownership_module.asyncio, "create_subprocess_exec", record)
    return calls


def _executor(tmp_path, guard, **options) -> RsyncExecutor:
    (tmp_path / "downloads").mkdir(exist_ok=True)
    configuration = RsyncConfiguration(str(tmp_path / "downloads"), str(tmp_path / "runtime"),
                                       connection_timeout_seconds=options.pop("connection_timeout_seconds", 10),
                                       transfer_timeout_seconds=options.pop("transfer_timeout_seconds", 30), **options)

    async def authorize(_handle, _action):
        return True

    return RsyncExecutor(configuration, authorize, egress=guard)


def _candidate(url: str, *, size: int = 0, methods=(InputMethod.USERNAME_PASSWORD,)) -> TransferCandidate:
    scheme = url.split("://", 1)[0]
    return TransferCandidate("payload.bin", (Endpoint(scheme, url),), expected_bytes=size,
                             accepted_input_methods=tuple(methods), request_kind=scheme, provider_id="general_rsync")


def _password(username=USER, password=PASSWORD, facts=()) -> SubmittedInput:
    return SubmittedInput("challenge", 1, InputMethod.USERNAME_PASSWORD,
                          {InputField.USERNAME: username, InputField.PASSWORD: password}, tuple(facts))


def _identity(host: str, fingerprint: str) -> tuple:
    return (InputFact(InputFactName.SERVER_HOST, host), InputFact(InputFactName.SERVER_IDENTITY_ALGORITHM, "sha-1"),
            InputFact(InputFactName.SERVER_IDENTITY_FINGERPRINT, fingerprint))


async def _settle(executor, handle, *, timeout=30.0):
    deadline = time.monotonic() + timeout
    while True:
        observed = await executor.observe(handle)
        if observed.state not in {ExecutionState.RUNNING, ExecutionState.QUEUED}:
            return observed
        assert time.monotonic() < deadline, "rsync execution never settled"
        await asyncio.sleep(0.05)


@pytest_asyncio.fixture
async def daemon(tmp_path):
    source = tmp_path / "srv" / "pub"
    write_tree(source, {"payload.bin": PAYLOAD, "tree/a.txt": b"alpha", "tree/sub/b [1].txt": b"beta",
                        "tree/sub/deeper/c d.txt": b"gamma", "st*r.txt": b"star", "str.txt": b"not-star",
                        "x[1].txt": b"bracket", "x1.txt": b"wrong-file"})
    outside = tmp_path / "outside"
    write_tree(outside, {"secret.txt": b"outside-secret"})
    os.symlink("../a.txt", source / "tree" / "sub" / "link-in")
    os.symlink(str(outside / "secret.txt"), source / "tree" / "link-out")
    os.symlink(str(outside), source / "tree" / "dirlink-out")
    os.mkfifo(source / "tree" / "fifo")
    write_tree(tmp_path / "srv" / "priv", {"p.bin": PAYLOAD[:4096]})
    write_tree(tmp_path / "srv" / "one", {"slow.bin": PAYLOAD})
    origin = RsyncDaemon(tmp_path / "daemon", {
        "pub": {"path": source},
        "priv": {"path": tmp_path / "srv" / "priv", "auth": (USER, PASSWORD)},
        "one": {"path": tmp_path / "srv" / "one", "max_connections": 1},
    }, motd="Welcome\tto the fixture\n").start()
    guard = guard_for()
    yield origin, guard
    origin.stop()
    await guard.stop()


# ── 1. identity and claim ────────────────────────────────────────────────────

async def test_executor_identity_and_positive_claim(tmp_path):
    executor = _executor(tmp_path, guard_for())
    assert executor.descriptor.id == "rsync" and executor.descriptor.name == "rsync"
    for url in ("rsync://h/m/f", "rsync+ssh://h/p/f"):
        assert executor.claim(ExecutionSubject.of(_candidate(url))).supported is True
    for url in ("https://h/f", "sftp://h/f", "ftp://h/f", "scp://h/f", "ssh://h/f", "rsync-ish://h/f"):
        assert executor.claim(ExecutionSubject.of(_candidate(url))).supported is False
    # Claimed by transport, never by provider: another provider's rsync candidate is still claimed.
    foreign = replace(_candidate("rsync://h/m/f"), provider_id="some_future_provider")
    assert executor.claim(ExecutionSubject.of(foreign)).supported is True


async def test_declared_capabilities_are_exactly_the_characterized_ones(tmp_path):
    executor = _executor(tmp_path, guard_for())
    caps = executor.capabilities
    assert caps.continuation == frozenset({
        ContinuationCapability.FULL_RESTART, ContinuationCapability.CONTIGUOUS_FROM_OFFSET,
        ContinuationCapability.IMPORT_EXISTING_MATERIAL, ContinuationCapability.EXPORT_MATERIAL_RANGES,
        ContinuationCapability.DESTINATION_AWARE_CONTINUATION})
    assert not caps.per_execution_pause and not caps.acquisition_gate
    # The DP-assigned aggregate ceiling is enforced on every rsync connection.
    assert caps.aggregate_bandwidth_ceiling
    assert caps.remote_discovery and caps.candidate_sampling and caps.transient_input
    off = _executor(tmp_path, guard_for(), partial_transfers=False)
    assert off.capabilities.continuation == frozenset({ContinuationCapability.FULL_RESTART})


async def test_health_reports_a_missing_binary_and_an_unsupported_version(tmp_path):
    missing = _executor(tmp_path, guard_for(), binary="dp-no-such-rsync-binary")
    health = await missing.health()
    assert health.ready is False and health.error.category == Category.EXECUTOR_UNAVAILABLE
    old = tmp_path / "old-rsync"
    old.write_text("#!/bin/sh\necho 'rsync  version 3.1.3  protocol version 31'\n")
    old.chmod(0o755)
    health = await _executor(tmp_path, guard_for(), binary=str(old)).health()
    assert health.ready is False and health.error.category == Category.INVALID_CONFIGURATION
    require_rsync()
    assert (await _executor(tmp_path, guard_for()).health()).ready is True


# ── 2. discovery ─────────────────────────────────────────────────────────────

async def test_discovery_classifies_files_trees_links_and_missing_paths(tmp_path, daemon):
    origin, guard = daemon
    executor = _executor(tmp_path, guard)

    def subject(path):
        return ExecutionSubject.of(_candidate(origin.url(path)))

    file = await executor.discover(subject("/pub/payload.bin"), depth=DiscoveryDepth.UNLIMITED)
    assert (file.kind, file.expected_bytes) == (RemoteObjectKind.FILE, len(PAYLOAD))
    tree = await executor.discover(subject("/pub/tree"), depth=DiscoveryDepth.UNLIMITED)
    assert tree.kind == RemoteObjectKind.DIRECTORY
    assert {(entry.relative_path, entry.expected_bytes) for entry in tree.entries} == {
        ("a.txt", 5), ("sub/b [1].txt", 4), ("sub/deeper/c d.txt", 5)}
    # Links (inside and outside the tree, to files and directories) and special
    # files are never members and never followed.
    assert not any("link" in entry.relative_path or "fifo" in entry.relative_path or "secret" in entry.relative_path
                   for entry in tree.entries)
    flat = await executor.discover(subject("/pub/tree/"))
    assert [entry.relative_path for entry in flat.entries] == ["a.txt"]
    for path, category in (("/pub/tree/link-out", Category.UNSUPPORTED_REQUEST),
                           ("/pub/nope", Category.SOURCE_NOT_FOUND), ("/nomodule/x", Category.SOURCE_NOT_FOUND)):
        with pytest.raises(Exception) as raised:
            await executor.discover(subject(path), depth=DiscoveryDepth.UNLIMITED)
        assert raised.value.error.category == category


async def test_a_daemon_root_is_the_tree_of_every_advertised_named_root(tmp_path, daemon):
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    # "priv" needs authentication: the root is only a result when complete.
    challenge = await executor.discover(ExecutionSubject.of(_candidate(origin.url("/"))), depth=DiscoveryDepth.UNLIMITED)
    assert challenge.reason == InputReason.AUTH_REQUIRED
    root = await executor.discover(ExecutionSubject.of(_candidate(origin.url("/"))), _password(), depth=DiscoveryDepth.UNLIMITED)
    paths = {entry.relative_path for entry in root.entries}
    assert {"pub/payload.bin", "pub/tree/sub/b [1].txt", "priv/p.bin", "one/slow.bin"} <= paths
    assert not any("link" in path for path in paths)


async def test_listed_paths_are_literal_never_patterns(tmp_path, daemon):
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    for name, data in (("x%5B1%5D.txt", b"bracket"), ("st%2Ar.txt", b"star")):
        candidate = _candidate(origin.url(f"/pub/{name}"), size=len(data))
        found = await executor.discover(ExecutionSubject.of(candidate), depth=DiscoveryDepth.UNLIMITED)
        assert found.kind == RemoteObjectKind.FILE and found.expected_bytes == len(data)
        target = tmp_path / "downloads" / f"literal-{len(data)}"
        request = file_request(candidate, target, f"literal-{name}", root=tmp_path / "downloads")
        handle = executor.prepare(request)
        await executor.start(request, handle)
        assert (await _settle(executor, handle)).state == ExecutionState.SUCCEEDED
        # A daemon would otherwise expand x[1].txt to x1.txt and st*r.txt to three files.
        assert target.read_bytes() == data


# ── 3. FILE execution through the neutral contract ───────────────────────────

async def test_a_daemon_file_completes_with_direct_argv_and_no_destructive_option(tmp_path, daemon, spawned):
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    candidate = _candidate(origin.url("/pub/payload.bin"), size=len(PAYLOAD))
    target = tmp_path / "downloads" / "payload.bin"
    request = file_request(candidate, target, "attempt-file", root=tmp_path / "downloads")
    handle = executor.prepare(request)
    assert set(handle.correlation) == {"target", "binding"}
    started = await executor.start(request, handle)
    assert started.state == ExecutionState.RUNNING
    done = await _settle(executor, handle)
    assert done.state == ExecutionState.SUCCEEDED and done.material == ((0, len(PAYLOAD)),)
    assert target.read_bytes() == PAYLOAD
    assert stat.S_IMODE(target.stat().st_mode) & 0o111 == 0
    argv, env, kwargs = next(call for call in spawned if "--append" in call[0])
    assert argv[0] == "rsync" and kwargs["start_new_session"] is True and "shell" not in kwargs
    assert argv[argv.index("--") + 1:] == [f"rsync://rsync-origin.test:{origin.port}/pub/payload.bin", str(target)]
    assert "-I" in argv and "--chmod=F666" in argv
    assert not set(FORBIDDEN_NATIVE) & set(argv[:argv.index("--")])
    assert set(env) == {"PATH", "LC_ALL", "LANG", "HOME", "RSYNC_PROXY"}
    assert env["RSYNC_PROXY"].endswith(f"@127.0.0.1:{guard.bound_port}")


def _core_authority(executor, *, paused=False, started=()):
    """Core's own rule (``authorize_execution``): ``start`` only for an attempt
    still ``prepared`` -- core records it started once its first observation
    lands (``started``) -- continuing a started one is ``resume`` authority,
    and a pause intent withholds both."""
    started, asked = set(started), []

    async def authorize(handle, action):
        asked.append(action)
        if action in {"start", "resume"} and paused:
            return False
        return action != "start" or handle.attempt_id not in started

    executor.authorize = authorize
    return asked


async def test_input_continues_the_same_challenged_attempt_under_resume_authority(tmp_path, daemon):
    """The executor-challenge loop (rsync+ssh after a switch): the answered
    attempt is no longer unstarted, so its continuation must use ``resume``
    authority -- and a refused continuation must keep the attempt's truth
    instead of losing it (observed ABSENT, restarted, asked again)."""
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    _core_authority(executor)
    candidate = _candidate(origin.url("/priv/p.bin"), size=4096)
    target = tmp_path / "downloads" / "continued-input.bin"
    request = file_request(candidate, target, "attempt-continue-input", root=tmp_path / "downloads")
    handle = executor.prepare(request)
    await executor.start(request, handle)
    failed = await _settle(executor, handle)
    assert executor.input_requirement(candidate, failed).reason == InputReason.AUTH_REQUIRED
    # A pause intent refuses the continuation: nothing is started, and the
    # attempt's own terminal truth is kept (never ABSENT).
    _core_authority(executor, paused=True, started={handle.attempt_id})
    held = await executor.start_with_input(request, handle, _password())
    assert held.state == ExecutionState.PAUSED
    assert (await executor.observe(handle)).state == ExecutionState.FAILED
    # Core recorded the attempt as started: it refuses ``start`` for it now.
    asked = _core_authority(executor, started={handle.attempt_id})
    await executor.start_with_input(request, handle, _password())
    done = await _settle(executor, handle)
    assert done.state == ExecutionState.SUCCEEDED and target.read_bytes() == PAYLOAD[:4096]
    assert "resume" in asked


async def test_input_continues_a_challenged_attempt_after_a_restart(tmp_path, daemon):
    origin, guard = daemon
    first = _executor(tmp_path, guard)
    _core_authority(first)
    candidate = _candidate(origin.url("/priv/p.bin"), size=4096)
    target = tmp_path / "downloads" / "restart-input.bin"
    request = file_request(candidate, target, "attempt-restart-input", root=tmp_path / "downloads")
    handle = first.prepare(request)
    await first.start(request, handle)
    await _settle(first, handle)
    # DebridPulse restarted: a new executor holds no memory of the attempt,
    # which core already recorded as started.
    restarted = _executor(tmp_path, guard)
    asked = _core_authority(restarted, started={handle.attempt_id})
    await restarted.start_with_input(request, handle, _password())
    done = await _settle(restarted, handle)
    assert done.state == ExecutionState.SUCCEEDED and target.read_bytes() == PAYLOAD[:4096]
    assert "resume" in asked


async def test_daemon_credentials_never_reach_argv_environment_or_diagnostics(tmp_path, daemon, spawned):
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    candidate = _candidate(origin.url("/priv/p.bin"), size=4096)
    target = tmp_path / "downloads" / "p.bin"
    request = file_request(candidate, target, "attempt-auth", root=tmp_path / "downloads")
    handle = executor.prepare(request)
    await executor.start(request, handle)
    failed = await _settle(executor, handle)
    assert failed.state == ExecutionState.FAILED and failed.error.category == Category.CREDENTIAL_MISSING
    assert executor.input_requirement(candidate, failed).reason == InputReason.AUTH_REQUIRED
    wrong = await executor.start_with_input(request, handle, _password(password="wrong-sentinel"))
    wrong = await _settle(executor, handle)
    assert executor.input_requirement(candidate, wrong).reason == InputReason.AUTH_REQUIRED
    await executor.start_with_input(request, handle, _password())
    done = await _settle(executor, handle)
    assert done.state == ExecutionState.SUCCEEDED and target.read_bytes() == PAYLOAD[:4096]
    for argv, env, _kwargs in spawned:
        blob = json.dumps([argv, env])
        assert PASSWORD not in blob and "wrong-sentinel" not in blob
        assert env.get("USER") in {None, USER}
    assert PASSWORD not in json.dumps([str(failed.error), str(wrong.error)])


async def test_exit_zero_is_never_completion_without_a_regular_target_of_the_source_length(tmp_path, daemon):
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    # Discovered as a regular file, replaced by a link before the writer runs:
    # rsync skips it and exits 0.
    source = tmp_path / "srv" / "pub" / "swap.bin"
    source.write_bytes(b"x" * 100)
    candidate = _candidate(origin.url("/pub/swap.bin"), size=100)
    source.unlink()
    os.symlink(str(tmp_path / "outside" / "secret.txt"), source)
    target = tmp_path / "downloads" / "swap.bin"
    request = file_request(candidate, target, "attempt-swap", root=tmp_path / "downloads")
    await executor.start(request, executor.prepare(request))
    failed = await _settle(executor, executor.prepare(request))
    assert failed.state == ExecutionState.FAILED and failed.error.category == Category.SOURCE_NOT_FOUND
    assert not target.exists() or target.stat().st_size == 0


async def test_local_destination_containment(tmp_path, daemon):
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    candidate = _candidate(origin.url("/pub/payload.bin"), size=len(PAYLOAD))
    with pytest.raises(Exception) as raised:
        executor.prepare(file_request(candidate, tmp_path / "elsewhere.bin", "attempt-outside", root=tmp_path))
    assert raised.value.error.category == Category.PATH_POLICY_VIOLATION
    blocked = tmp_path / "downloads" / "is-a-directory"
    blocked.mkdir()
    request = file_request(candidate, blocked, "attempt-dir", root=tmp_path / "downloads")
    observed = await executor.start(request, executor.prepare(request))
    assert observed.state == ExecutionState.FAILED and observed.error.category == Category.LOCAL_PATH_CONFLICT


# ── 4. tunables ──────────────────────────────────────────────────────────────

async def test_modification_time_and_compression_follow_their_settings(tmp_path, daemon, spawned):
    origin, guard = daemon
    source = tmp_path / "srv" / "pub" / "payload.bin"
    os.utime(source, (1_577_836_800, 1_577_836_800))
    for preserve, compress, name in ((True, True, "on.bin"), (False, False, "off.bin")):
        executor = _executor(tmp_path, guard, preserve_modification_time=preserve, compression=compress)
        candidate = _candidate(origin.url("/pub/payload.bin"), size=len(PAYLOAD))
        target = tmp_path / "downloads" / name
        request = file_request(candidate, target, f"attempt-{name}", root=tmp_path / "downloads")
        handle = executor.prepare(request)
        await executor.start(request, handle)
        assert (await _settle(executor, handle)).state == ExecutionState.SUCCEEDED
        assert (int(target.stat().st_mtime) == 1_577_836_800) is preserve
        argv = spawned[-1][0]
        assert ("--times" in argv) is preserve and ("--compress" in argv) is compress


async def test_partial_transfers_off_keeps_the_target_empty_until_completion(tmp_path, daemon):
    origin, guard = daemon
    slow = RsyncDaemon(tmp_path / "slow-daemon", {"pub": {"path": tmp_path / "srv" / "pub"}}, bwlimit=512).start()
    try:
        executor = _executor(tmp_path, guard, partial_transfers=False)
        candidate = _candidate(slow.url("/pub/payload.bin"), size=len(PAYLOAD))
        target = tmp_path / "downloads" / "atomic.bin"
        request = file_request(candidate, target, "attempt-atomic", root=tmp_path / "downloads")
        handle = executor.prepare(request)
        footprint = executor.footprint(request.work)
        await executor.start(request, handle)
        running = await executor.observe(handle)
        deadline = time.monotonic() + 20
        while running.progress.completed_bytes == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            running = await executor.observe(handle)
        assert running.state == ExecutionState.RUNNING and running.material is None
        assert not target.exists()  # nothing partial ever reaches the destination
        assert Path(footprint.transient_trees[0]).is_dir()
        cancelled = await executor.cancel(handle)
        assert cancelled.state == ExecutionState.CANCELLED and not target.exists()
    finally:
        slow.stop()


async def test_the_connection_timeout_bounds_reaching_the_source_not_the_transfer(tmp_path, daemon):
    _origin, guard = daemon
    # A server that accepts the connection and never speaks the protocol.
    silent = await asyncio.start_server(lambda reader, writer: None, "127.0.0.1", 0)
    port = silent.sockets[0].getsockname()[1]
    try:
        executor = _executor(tmp_path, guard, connection_timeout_seconds=2, transfer_timeout_seconds=600)
        candidate = _candidate(f"rsync://silent.test:{port}/pub/payload.bin", size=len(PAYLOAD))
        request = file_request(candidate, tmp_path / "downloads" / "silent.bin", "attempt-silent",
                               root=tmp_path / "downloads")
        handle = executor.prepare(request)
        started = time.monotonic()
        await executor.start(request, handle)
        observed = await executor.observe(handle)
        assert observed.activity.progress_expected is False  # never a stall candidate before it begins
        failed = await _settle(executor, handle, timeout=20)
        assert failed.state == ExecutionState.FAILED and failed.error.category == Category.CONNECTION_TIMEOUT
        assert failed.error.retryability == Retryability.BACKOFF and time.monotonic() - started < 15
    finally:
        silent.close()


# ── 5. remote capacity ───────────────────────────────────────────────────────

async def test_a_full_module_is_an_immediate_remote_capacity_fact_never_a_stall(tmp_path, daemon):
    origin, guard = daemon
    # Another client holds the module's only slot.
    holder = subprocess.Popen(["rsync", "--bwlimit=64", f"rsync://127.0.0.1:{origin.port}/one/slow.bin",
                               str(tmp_path / "holder.bin")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await asyncio.sleep(1.0)
        executor = _executor(tmp_path, guard)
        candidate = _candidate(origin.url("/one/slow.bin"), size=len(PAYLOAD))
        request = file_request(candidate, tmp_path / "downloads" / "slow.bin", "attempt-full",
                               root=tmp_path / "downloads")
        handle = executor.prepare(request)
        started = time.monotonic()
        await executor.start(request, handle)
        observed = await _settle(executor, handle, timeout=10)
        # The daemon refuses at once; the executor never reports a waiting,
        # byte-producing or stall-expected state for it.
        assert time.monotonic() - started < 5
        assert observed.state == ExecutionState.FAILED
        error = observed.error
        assert (error.domain, error.category, error.retryability) == (
            Domain.NETWORK, Category.CONCURRENCY_LIMITED, Retryability.BACKOFF)
        assert not interpretation_absent(error)  # a full module is the daemon's answer, not absence
        assert observed.progress.completed_bytes == 0
        # Core policy waits for capacity: no budget is spent, no second scheduler.
        policy = TransferPolicy(retry_delay=5)
        for failures in (0, 2, 50):
            decision = policy.recover(error, RecoveryContext(consecutive_no_progress_failures=failures), 0.0)
            assert (decision.action, decision.reason) == (RecoveryAction.BACKOFF, "remote_capacity_wait")
        # Discovery against a full module is the same neutral fact.
        with pytest.raises(Exception) as raised:
            await executor.discover(ExecutionSubject.of(_candidate(origin.url("/one/slow.bin"))), depth=DiscoveryDepth.UNLIMITED)
        assert raised.value.error.category == Category.CONCURRENCY_LIMITED
    finally:
        holder.kill()
        holder.wait()


async def test_evidence_waits_out_the_release_of_the_session_before_it_and_still_reports_a_full_server(
        tmp_path, daemon):
    """Each evidence window is its own daemon session, and a one-connection
    module frees a finished session's slot only once the daemon's side has
    exited. A window refused while the slot is being released waits a bounded
    moment and is acquired; a module that stays full is still capacity."""
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    candidate = _candidate(origin.url("/one/slow.bin"), size=len(PAYLOAD))

    async def hold():
        # Its own process group: an rsync client forks, and the session ends
        # only when every process holding the socket is gone. A holder refused
        # while the previous session is still being released exits at once, so
        # it is started again until it is the one holding the slot.
        for _ in range(20):
            (tmp_path / "holder.bin").unlink(missing_ok=True)
            process = subprocess.Popen(
                ["rsync", "--bwlimit=64", f"rsync://127.0.0.1:{origin.port}/one/slow.bin", str(tmp_path / "holder.bin")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            await asyncio.sleep(1.0)
            if process.poll() is None:
                return process
        raise AssertionError("the holder never obtained the module's slot")

    def end(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()

    holder = await hold()
    try:
        async def release():
            await asyncio.sleep(0.3)
            end(holder)

        released = asyncio.create_task(release())
        sample = await executor.fingerprint(ExecutionSubject.of(candidate))
        await released
        assert sample.kind == FingerprintKind.FULL_CONTENT_SAMPLE and sample.total_bytes == len(PAYLOAD)
    finally:
        end(holder)

    holder = await hold()
    try:
        started = time.monotonic()
        full = await executor.fingerprint(ExecutionSubject.of(candidate))
        assert (full.kind, full.reason) == (FingerprintKind.UNAVAILABLE, "remote_capacity")
        assert time.monotonic() - started < 5
    finally:
        end(holder)


async def test_each_daemon_answer_is_classified_for_the_interpretation_owner(tmp_path, daemon):
    """What the server said decides whether another reading of the same source
    may be tried: only its own positive absence (an unknown module, a missing
    path, nothing listening) -- never a login, a full module or a policy block."""
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    methods = (InputMethod.USERNAME_PASSWORD, InputMethod.USERNAME_PRIVATE_KEY)

    async def failure(url, via=None):
        with pytest.raises(TransferError) as raised:
            await (via or executor).discover(ExecutionSubject.of(_candidate(url, methods=methods)), depth=DiscoveryDepth.UNLIMITED)
        return raised.value.error

    unknown = await failure(origin.url("/home/user/file.iso"))
    missing = await failure(origin.url("/pub/no-such-file.iso"))
    closed = await failure(f"rsync://rsync-origin.test:{free_port()}/pub/file.iso")
    closed_ssh = await failure(f"rsync+ssh://rsync-origin.test:{free_port()}/srv/file.iso")
    assert (unknown.domain, unknown.category) == (Domain.RESOLUTION, Category.SOURCE_NOT_FOUND)
    assert (missing.domain, missing.category) == (Domain.RESOLUTION, Category.SOURCE_NOT_FOUND)
    assert (closed.domain, closed.category) == (Domain.NETWORK, Category.CONNECTION_REFUSED)
    assert (closed_ssh.domain, closed_ssh.category) == (Domain.NETWORK, Category.CONNECTION_REFUSED)
    assert all(interpretation_absent(item) for item in (unknown, missing, closed, closed_ssh))
    # A module that wants its own login asks for it -- a password only: a
    # daemon account is never a key login.
    login = await executor.discover(ExecutionSubject.of(_candidate(origin.url("/priv/p.bin"), methods=methods)),
                                    depth=DiscoveryDepth.UNLIMITED)
    assert login.reason == InputReason.AUTH_REQUIRED
    assert [item.method for item in login.methods] == [InputMethod.USERNAME_PASSWORD]
    # A daemon port that never answers (a firewall that drops it) is a timeout
    # within rsync's own Connection Timeout: uncertainty, never absence.
    from test_v113_egress_guard_route_scope import BlackHole
    with BlackHole() as hole:
        started = time.monotonic()
        silent = await failure(f"rsync://rsync-origin.test:{hole.port}/pub/file.iso",
                               via=_executor(tmp_path, guard, connection_timeout_seconds=5))
        waited = time.monotonic() - started
    assert (silent.domain, silent.category) == (Domain.NETWORK, Category.CONNECTION_TIMEOUT)
    assert not interpretation_absent(silent) and waited < 15
    # A destination the guard's own policy refuses is no statement about the
    # server at all.
    blocked = await failure(origin.url("/home/user/file.iso"), via=_executor(tmp_path, guard_for(public=())))
    assert blocked.category != Category.CONNECTION_REFUSED and not interpretation_absent(blocked)


# ── 6. continuation boundary ─────────────────────────────────────────────────

def _plan(candidate, boundary: int, size: int, strategy=ContinuationStrategy.CONTIGUOUS_FROM_OFFSET):
    retained = ((0, boundary),) if boundary else ()
    return ContinuationPlan(1, 1, 1, str(candidate.id), "rsync", strategy, boundary, retained, (),
                            ((boundary, size),), size, "test")


async def test_rsync_continues_exactly_at_the_authorized_boundary_and_never_rewrites_it(tmp_path, daemon):
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    candidate = _candidate(origin.url("/pub/payload.bin"), size=len(PAYLOAD))
    target = tmp_path / "downloads" / "continued.bin"
    boundary = 1024 * 1024
    # The retained prefix carries a marker rsync would overwrite if it resent
    # it; beyond the boundary sit bytes DebridPulse never authorized.
    retained = bytearray(PAYLOAD[:boundary])
    retained[:6] = b"MARKER"
    target.write_bytes(bytes(retained) + b"unauthorized-tail" * 1000)
    request = replace(file_request(candidate, target, "attempt-continue", root=tmp_path / "downloads"),
                      continuation=_plan(candidate, boundary, len(PAYLOAD)))
    handle = executor.prepare(request)
    await executor.start(request, handle)
    done = await _settle(executor, handle)
    assert done.state == ExecutionState.SUCCEEDED
    assert target.read_bytes() == bytes(retained) + PAYLOAD[boundary:]


async def test_unknown_destination_bytes_are_never_promoted_by_rsync_quick_check(tmp_path, daemon):
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    source = tmp_path / "srv" / "pub" / "payload.bin"
    candidate = _candidate(origin.url("/pub/payload.bin"), size=len(PAYLOAD))
    target = tmp_path / "downloads" / "quick.bin"
    # Same size and same modification time as the source, different content.
    target.write_bytes(bytes(len(PAYLOAD)))
    stamp = source.stat().st_mtime
    os.utime(target, (stamp, stamp))
    request = file_request(candidate, target, "attempt-quick", root=tmp_path / "downloads")  # no plan: nothing kept
    handle = executor.prepare(request)
    await executor.start(request, handle)
    done = await _settle(executor, handle)
    assert done.state == ExecutionState.SUCCEEDED and target.read_bytes() == PAYLOAD


async def test_a_plan_whose_retained_prefix_is_missing_fails_closed(tmp_path, daemon):
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    candidate = _candidate(origin.url("/pub/payload.bin"), size=len(PAYLOAD))
    target = tmp_path / "downloads" / "short.bin"
    target.write_bytes(PAYLOAD[:1000])
    request = replace(file_request(candidate, target, "attempt-short", root=tmp_path / "downloads"),
                      continuation=_plan(candidate, 1024 * 1024, len(PAYLOAD)))
    observed = await executor.start(request, executor.prepare(request))
    assert observed.state == ExecutionState.FAILED and observed.error.category == Category.RESOURCE_STATE_CONFLICT
    assert target.read_bytes() == PAYLOAD[:1000]


MIB = 1024 * 1024
# DP-valid material that is not one prefix (an aria2 partial, say).
SPARSE = ((0, MIB // 2), (MIB, MIB + MIB // 2), (2 * MIB + 4096, 3 * MIB))


def _sparse_target(target):
    """The canonical destination as a previous writer left it: every retained
    range holds the source's bytes, every gap holds bytes nobody vouched for."""
    data = bytearray(b"\xa5" * len(PAYLOAD))
    for start, end in SPARSE:
        data[start:end] = PAYLOAD[start:end]
    target.write_bytes(bytes(data))
    return bytes(data)


def _destination_aware(candidate, retained=SPARSE):
    return ContinuationPlan(1, 1, 1, str(candidate.id), "rsync", ContinuationStrategy.DESTINATION_AWARE,
                            retained[0][1], retained, (), ((0, len(PAYLOAD)),), len(PAYLOAD), "test")


def _argv(monkeypatch):
    seen = []
    real = asyncio.create_subprocess_exec

    async def record(*argv, **kwargs):
        seen.append([str(item) for item in argv])
        return await real(*argv, **kwargs)

    monkeypatch.setattr("executors.process_ownership.asyncio.create_subprocess_exec", record)
    return seen


async def test_a_destination_aware_plan_reads_the_untouched_target_as_its_basis(tmp_path, daemon, monkeypatch):
    origin, guard = daemon
    slow = RsyncDaemon(tmp_path / "slow-daemon", {"pub": {"path": tmp_path / "srv" / "pub"}}, bwlimit=512).start()
    try:
        seen = _argv(monkeypatch)
        executor = _executor(tmp_path, guard)
        candidate = _candidate(slow.url("/pub/payload.bin"), size=len(PAYLOAD))
        target = tmp_path / "downloads" / "basis.bin"
        before = _sparse_target(target)
        request = replace(file_request(candidate, target, "attempt-basis", root=tmp_path / "downloads"),
                          continuation=_destination_aware(candidate))
        handle = executor.prepare(request)
        running = await executor.start(request, handle)
        # Nothing reported while reconstructing is material; the target is the
        # basis, untouched -- never cut to the prefix, never rewritten in place.
        assert running.state == ExecutionState.RUNNING and running.material is None
        (argv,) = [item for item in seen if item and item[0] == "rsync"]
        assert "--no-whole-file" in argv and any(item.startswith("--temp-dir=") for item in argv)
        assert not {"--append", "--inplace"} & set(argv) and not any(item.startswith("--partial") for item in argv)
        progressed = 0
        while progressed == 0:
            observed = await executor.observe(handle)
            assert observed.material is None and target.read_bytes() == before
            progressed = observed.progress.completed_bytes
            await asyncio.sleep(0.1)
        done = await _settle(executor, handle, timeout=60)
        assert done.state == ExecutionState.SUCCEEDED and target.read_bytes() == PAYLOAD
        assert done.material == ((0, len(PAYLOAD)),)
        # The private temporary tree never outlives the writer.
        assert not executor._staging(target.resolve()).exists()
    finally:
        slow.stop()


async def test_an_interrupted_destination_aware_writer_leaves_the_target_exactly_as_it_was(tmp_path, daemon):
    origin, guard = daemon
    slow = RsyncDaemon(tmp_path / "slow-daemon", {"pub": {"path": tmp_path / "srv" / "pub"}}, bwlimit=256).start()
    try:
        executor = _executor(tmp_path, guard)
        candidate = _candidate(slow.url("/pub/payload.bin"), size=len(PAYLOAD))
        target = tmp_path / "downloads" / "interrupted.bin"
        before = _sparse_target(target)
        request = replace(file_request(candidate, target, "attempt-interrupted", root=tmp_path / "downloads"),
                          continuation=_destination_aware(candidate))
        handle = executor.prepare(request)
        await executor.start(request, handle)
        while (await executor.observe(handle)).progress.completed_bytes == 0:
            await asyncio.sleep(0.1)
        cancelled = await executor.cancel(handle)
        assert cancelled.state == ExecutionState.CANCELLED and cancelled.material is None
        assert target.read_bytes() == before
    finally:
        slow.stop()


async def test_a_destination_aware_plan_whose_retained_ranges_are_missing_fails_closed(tmp_path, daemon):
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    candidate = _candidate(origin.url("/pub/payload.bin"), size=len(PAYLOAD))
    target = tmp_path / "downloads" / "short-basis.bin"
    target.write_bytes(PAYLOAD[:MIB])
    request = replace(file_request(candidate, target, "attempt-short-basis", root=tmp_path / "downloads"),
                      continuation=_destination_aware(candidate))
    observed = await executor.start(request, executor.prepare(request))
    assert observed.state == ExecutionState.FAILED and observed.error.category == Category.RESOURCE_STATE_CONFLICT
    assert target.read_bytes() == PAYLOAD[:MIB]


# ── 7. process ownership ─────────────────────────────────────────────────────

async def test_one_attempt_owns_one_group_across_restart_and_cancel_is_observed_truth(tmp_path, daemon):
    origin, guard = daemon
    slow = RsyncDaemon(tmp_path / "slow-daemon", {"pub": {"path": tmp_path / "srv" / "pub"}}, bwlimit=256).start()
    try:
        executor = _executor(tmp_path, guard)
        candidate = _candidate(slow.url("/pub/payload.bin"), size=len(PAYLOAD))
        request = file_request(candidate, tmp_path / "downloads" / "owned.bin", "attempt-owned",
                               root=tmp_path / "downloads")
        handle = executor.prepare(request)
        assert (await executor.start(request, handle)).state == ExecutionState.RUNNING
        group = executor._runs["attempt-owned"].owned.group
        # A repeated (e.g. after a lost acknowledgement) start never creates a second group.
        again = await executor.start(request, handle)
        assert again.state == ExecutionState.RUNNING and executor._runs["attempt-owned"].owned.group == group
        # A new executor (DebridPulse restarted) sees the still-alive group as owned
        # and running, refuses a second start, and stops it only by observed truth.
        restarted = _executor(tmp_path, guard)
        assert (await restarted.observe(handle)).state == ExecutionState.RUNNING
        assert (await restarted.start(request, handle)).state == ExecutionState.RUNNING
        assert "attempt-owned" not in restarted._runs
        cancelled = await restarted.cancel(handle)
        assert cancelled.state == ExecutionState.CANCELLED
        assert restarted.processes.alive("attempt-owned") in {None, False}
        assert (await restarted.observe(handle)).state == ExecutionState.ABSENT
        with pytest.raises(ProcessLookupError):
            os.killpg(group, 0)
    finally:
        slow.stop()


async def test_cancel_of_a_finished_writer_reports_what_it_observed(tmp_path, daemon):
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    candidate = _candidate(origin.url("/pub/tree/a.txt"), size=5)
    request = file_request(candidate, tmp_path / "downloads" / "a.txt", "attempt-finished",
                           root=tmp_path / "downloads")
    handle = executor.prepare(request)
    await executor.start(request, handle)
    assert (await _settle(executor, handle)).state == ExecutionState.SUCCEEDED
    assert (await executor.cancel(handle)).state == ExecutionState.SUCCEEDED
    never = file_request(candidate, tmp_path / "downloads" / "never.txt", "attempt-never",
                         root=tmp_path / "downloads")
    assert (await executor.cancel(executor.prepare(never))).state == ExecutionState.ABSENT


# ── 8. evidence ──────────────────────────────────────────────────────────────

async def test_rsync_evidence_is_the_same_neutral_sample_every_transport_produces(tmp_path, daemon):
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    sample = await executor.fingerprint(ExecutionSubject.of(_candidate(origin.url("/pub/payload.bin"))))
    tail = len(PAYLOAD) - SAMPLE_BYTES
    assert sample.kind == FingerprintKind.FULL_CONTENT_SAMPLE and sample.total_bytes == len(PAYLOAD)
    assert sample.signature == digest_full(len(PAYLOAD), PAYLOAD[:SAMPLE_BYTES], PAYLOAD[tail:])
    assert sample.prefix_signature == digest_prefix(len(PAYLOAD), PAYLOAD[:SAMPLE_BYTES])
    locked = await executor.fingerprint(ExecutionSubject.of(_candidate(origin.url("/priv/p.bin"))))
    assert locked.reason == InputReason.AUTH_REQUIRED
    proven = await executor.fingerprint_with_input(ExecutionSubject.of(_candidate(origin.url("/priv/p.bin"))),
                                                   _password())
    assert proven.signature == digest_full(4096, PAYLOAD[:4096])
    assert not list((tmp_path / "runtime" / "evidence").iterdir())


# ── 9. rsync over SSH ────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def ssh_origin(tmp_path):
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    origin = await RsyncSshOrigin(tmp_path / "ssh", credentials=(USER, PASSWORD), authorized_key=client_key,
                                  key_user=USER).start()
    write_tree(origin.root / "files", {"payload.bin": PAYLOAD, "dir/one.bin": b"one", "dir/two/2.bin": b"two",
                                       "odd [x] $(id) 'q'.bin": b"odd"})
    write_tree(origin.home, {"mine.bin": b"home-file"})
    os.symlink(str(origin.root / "files" / "payload.bin"), origin.root / "files" / "dir" / "link.bin")
    guard = guard_for()
    yield origin, guard, client_key
    await origin.close()
    await guard.stop()


async def test_ssh_identity_is_confirmed_before_any_credential_and_then_enforced(tmp_path, ssh_origin, spawned):
    origin, guard, _key = ssh_origin
    executor = _executor(tmp_path, guard)
    methods = (InputMethod.USERNAME_PASSWORD, InputMethod.USERNAME_PRIVATE_KEY)
    url = origin.url(f"{origin.root}/files/payload.bin")
    subject = ExecutionSubject.of(_candidate(url, methods=methods))
    first = await executor.discover(subject, depth=DiscoveryDepth.UNLIMITED)
    assert first.reason == InputReason.SERVER_IDENTITY_REQUIRED
    facts = {fact.name: fact.value for fact in first.facts}
    # The scope-wide host-key preference: the same key SFTP/SCP would confirm.
    assert facts[InputFactName.SERVER_IDENTITY_FINGERPRINT] == origin.fingerprint("ecdsa-sha2-nistp256")
    assert {item.method for item in first.methods} == set(methods)
    assert origin.auth_attempts == []  # nothing was sent before the identity was confirmed
    confirmed = _identity("rsync-ssh-origin.test", origin.fingerprint())
    found = await executor.discover(subject, _password(facts=confirmed), depth=DiscoveryDepth.UNLIMITED)
    assert (found.kind, found.expected_bytes) == (RemoteObjectKind.FILE, len(PAYLOAD))
    wrong = await executor.discover(subject, _password(password="wrong-sentinel", facts=confirmed), depth=DiscoveryDepth.UNLIMITED)
    assert wrong.reason == InputReason.AUTH_REQUIRED
    # A key other than the confirmed one fails closed as a changed identity.
    with pytest.raises(Exception) as raised:
        await executor.discover(subject, _password(facts=_identity("rsync-ssh-origin.test", "0" * 39 + "1")),
                                depth=DiscoveryDepth.UNLIMITED)
    assert raised.value.error.category == Category.HOST_KEY_FAILURE
    for argv, env, _kwargs in spawned:
        blob = json.dumps([argv, env])
        assert PASSWORD not in blob and "wrong-sentinel" not in blob and "RSYNC_PROXY" not in env


async def test_ssh_file_tree_and_key_login_through_the_one_channel(tmp_path, ssh_origin, spawned):
    origin, guard, key = ssh_origin
    executor = _executor(tmp_path, guard)
    methods = (InputMethod.USERNAME_PASSWORD, InputMethod.USERNAME_PRIVATE_KEY)
    confirmed = _identity("rsync-ssh-origin.test", origin.fingerprint())
    tree = await executor.discover(ExecutionSubject.of(_candidate(origin.url(f"{origin.root}/files/dir"),
                                                                  methods=methods)),
                                   _password(facts=confirmed), depth=DiscoveryDepth.UNLIMITED)
    assert {(entry.relative_path, entry.expected_bytes) for entry in tree.entries} == {("one.bin", 3), ("two/2.bin", 3)}
    passphrase = "key-passphrase-sentinel"
    for fmt, secret in (("openssh", ""), ("openssh", passphrase), ("pkcs8-pem", ""), ("pkcs8-pem", passphrase)):
        exported = key.export_private_key(fmt, secret or None).decode()
        keyed = SubmittedInput("challenge", 1, InputMethod.USERNAME_PRIVATE_KEY,
                               {InputField.USERNAME: USER, InputField.PRIVATE_KEY: exported,
                                **({InputField.PASSPHRASE: secret} if secret else {})}, confirmed)
        for name, data in (("odd%20%5Bx%5D%20%24(id)%20'q'.bin", b"odd"), ("payload.bin", PAYLOAD)):
            candidate = _candidate(origin.url(f"{origin.root}/files/{name}"), size=len(data), methods=methods)
            target = tmp_path / "downloads" / f"ssh-{fmt}-{bool(secret)}-{len(data)}"
            request = file_request(candidate, target, f"ssh-{fmt}-{bool(secret)}-{len(data)}",
                                   root=tmp_path / "downloads")
            handle = executor.prepare(request)
            await executor.start_with_input(request, handle, keyed)
            done = await _settle(executor, handle)
            assert done.state == ExecutionState.SUCCEEDED, (fmt, done.error)
            assert target.read_bytes() == data
        if secret:
            bad = SubmittedInput("challenge", 1, InputMethod.USERNAME_PRIVATE_KEY,
                                 {InputField.USERNAME: USER, InputField.PRIVATE_KEY: exported,
                                  InputField.PASSPHRASE: "wrong-passphrase-sentinel"}, confirmed)
            refused = await executor.discover(ExecutionSubject.of(_candidate(
                origin.url(f"{origin.root}/files/payload.bin"), methods=methods)), bad, depth=DiscoveryDepth.UNLIMITED)
            # A key that cannot be unlocked is asked again, never used or skipped.
            assert refused.reason == InputReason.AUTH_REQUIRED
    malformed = SubmittedInput("challenge", 1, InputMethod.USERNAME_PRIVATE_KEY,
                               {InputField.USERNAME: USER, InputField.PRIVATE_KEY: "-----BEGIN OPENSSH PRIVATE KEY-----\n"
                                "bm90IGEga2V5\n-----END OPENSSH PRIVATE KEY-----\n"}, confirmed)
    attempted = len(origin.auth_attempts)
    unusable = await executor.discover(ExecutionSubject.of(_candidate(
        origin.url(f"{origin.root}/files/payload.bin"), methods=methods)), malformed, depth=DiscoveryDepth.UNLIMITED)
    # A malformed key is asked again: it never reaches the server, and never
    # silently becomes a password login.
    assert unusable.reason == InputReason.AUTH_REQUIRED
    assert not [method for method, _user in origin.auth_attempts[attempted:] if method == "password"]
    assert ("publickey", USER) in origin.auth_attempts
    for argv, env, _kwargs in spawned:
        blob = json.dumps([argv, env])
        assert passphrase not in blob and "PRIVATE KEY" not in blob and "wrong-passphrase" not in blob
    home = await executor.discover(ExecutionSubject.of(_candidate(origin.url("/~/mine.bin"), methods=methods)),
                                   _password(facts=confirmed), depth=DiscoveryDepth.UNLIMITED)
    assert (home.kind, home.expected_bytes) == (RemoteObjectKind.FILE, 9)


async def test_a_server_without_rsync_is_a_definitive_protocol_failure(tmp_path):
    origin = await RsyncSshOrigin(tmp_path / "ssh", credentials=(USER, PASSWORD), remote_rsync=False).start()
    guard = guard_for()
    try:
        executor = _executor(tmp_path, guard)
        confirmed = _identity("rsync-ssh-origin.test", origin.fingerprint())
        with pytest.raises(Exception) as raised:
            await executor.discover(ExecutionSubject.of(_candidate(origin.url("/srv/f.bin"))),
                                    _password(facts=confirmed), depth=DiscoveryDepth.UNLIMITED)
        assert (raised.value.error.category, raised.value.error.retryability) == (
            Category.PROTOCOL_ERROR, Retryability.NEVER)
    finally:
        await origin.close()
        await guard.stop()


async def test_success_states_exactly_the_one_file_it_was_authorized_to_produce(tmp_path, daemon):
    origin, guard = daemon
    executor = _executor(tmp_path, guard)
    candidate = _candidate(origin.url("/pub/tree/a.txt"), size=5)
    target = tmp_path / "downloads" / "nested" / "a.txt"
    request = file_request(candidate, target, "attempt-result", root=tmp_path / "downloads")
    handle = executor.prepare(request)
    await executor.start(request, handle)
    done = await _settle(executor, handle)
    assert done.state == ExecutionState.SUCCEEDED
    assert [(entry.relative_path, entry.bytes) for entry in done.materialization.entries] == [("nested/a.txt", 5)]
