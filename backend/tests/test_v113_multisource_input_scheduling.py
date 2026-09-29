"""DP 1.0.13 adverse multi-source convergence, Pass A: interactive input scheduling.

"One outstanding operator question per transfer" never means "one complete
source decision must finish before the next question may even be discovered".
The moment an answered question's transport accepts (or refuses) the answer,
the question is retired and the transfer is released to the one resolution
scheduler: a sibling source that needs its own question asks it at once,
while the answered source's non-interactive listing, evidence, equivalence and
materialization go on through the ordinary owned lifecycle. Every slow step
here is held on an ``asyncio.Event``, so ordering is proven, never timed; the
wall-clock bounds only turn a hang into a failure.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import pytest

import db.database as database
from fake_integrations import VaultExecutor, VaultProvider
from test_discovery_validated_first_writer_input import RemoteLogin
from test_input_required_lifecycle import base  # noqa: F401  (fixture)
from test_v113_transfer_auth_context import lab  # noqa: F401  (fixture)
from transfers.models import ArtifactFingerprint, FingerprintKind, InputField, InputOrigin, TransferRequest, TransferState

pytestmark = pytest.mark.asyncio

PROMPT = 3.0  # seconds: a bound that turns "never asked" into a failure, not a latency budget


async def _question_events(transfer_id):
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT kind FROM application_events WHERE transfer_id=? AND kind='input_required'",
                                 (transfer_id,))
    return len(rows)


async def _asked(engine, transfer_id, request_id, *, timeout=PROMPT):
    """The transfer's current question once it names ``request_id``; ``None``
    when that never happened within ``timeout`` real seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        current = await engine.challenges.current(transfer_id)
        if current is not None and current.request_id == request_id:
            return current
        await asyncio.sleep(0.01)
    return None


async def _retired(engine, transfer_id, challenge, *, timeout=PROMPT):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        current = await engine.challenges.current(transfer_id)
        if current is None or current.id != challenge.id:
            return True
        await asyncio.sleep(0.01)
    return False


async def _first_question(engine, transfer_id, now, ticks=8):
    for _ in range(ticks):
        now[0] += 5
        await engine.tick()
        current = await engine.challenges.current(transfer_id)
        if current is not None:
            return current
    raise AssertionError("no question was ever asked")


# ── A1: the next question is not blocked by slow post-auth work ─────────────

class HeldListing(RemoteLogin):
    """Discovery reports the transport's acceptance of a correct login at
    once (as the real SSH/FTP transports do); the listing that follows is
    held for the hosts named in ``hold``."""

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.hold = set()
        self.held = asyncio.Event()
        self.release = asyncio.Event()

    async def discover(self, subject, submitted=None):
        scheme, host = self._where(subject.candidate)
        if (submitted is not None and host in self.locks and self._identity_ok(scheme, host, submitted)
                and (submitted.value(InputField.USERNAME), submitted.value(InputField.PASSWORD)) == self.locks[host]):
            submitted.transport_accepted()
            if host in self.hold:
                self.held.set()
                await self.release.wait()
        return await super().discover(subject, submitted)


def _listing_lab(lab):
    repository, registry, engine, _provider, objects, locks, now = lab
    from providers.general_ftp.provider import GeneralFtpProvider
    registry.register_provider(GeneralFtpProvider())
    executor = HeldListing(repository.authorize_execution, objects=objects, locks=locks)
    registry.register_executor(executor)
    return repository, engine, executor, locks, now


async def _answer(engine, repository, transfer_id, challenge, locks):
    record = next(item for item in await repository.requests(transfer_id) if item.id == challenge.request_id)
    user, password = locks[urlsplit(record.request.payload).hostname]
    await engine.submit_input(transfer_id, challenge.id, "username_password", {"username": user, "password": password})


async def test_a1_the_next_sources_question_is_asked_while_the_answered_sources_listing_is_still_held(lab):
    repository, engine, executor, locks, now = _listing_lab(lab)
    transfer = await engine.submit((TransferRequest("sftp", "sftp://locked.example/solo.bin"),
                                    TransferRequest("sftp", "sftp://alt.example/solo.bin")), deduplicate=False)
    first = await _first_question(engine, transfer.id, now)
    assert first.origin == InputOrigin.PROVIDER
    records = {item.id: item for item in await repository.requests(transfer.id)}
    (sibling,) = [item for item in records.values() if item.parent_id is None and item.id != first.request_id]
    executor.hold.add(urlsplit(records[first.request_id].request.payload).hostname)

    await _answer(engine, repository, transfer.id, first, locks)
    cycle = asyncio.ensure_future(engine.tick())
    try:
        await asyncio.wait_for(executor.held.wait(), timeout=10)
        # The answered question retired at the transport's acceptance ...
        assert await _retired(engine, transfer.id, first), "the answered question outlived its acceptance"
        # ... and the sibling asks its own question while the answered
        # source's post-authentication listing is still held.
        second = await _asked(engine, transfer.id, sibling.id)
        assert second is not None, "the sibling's question waited for the answered source's post-auth work"
        assert not executor.release.is_set() and not cycle.done()
        # One question at a time: exactly the two, in order, never the first again.
        assert await _question_events(transfer.id) == 2
        assert (await engine.challenges.current(transfer.id)).id == second.id
    finally:
        executor.release.set()
    await asyncio.wait_for(cycle, timeout=10)
    await _answer(engine, repository, transfer.id, second, locks)
    for _ in range(10):
        now[0] += 5
        await engine.tick()
    assert await engine.challenges.current(transfer.id) is None
    assert await _question_events(transfer.id) == 2


# ── A2: the first question is prompt ────────────────────────────────────────

class SlowSampling(RemoteLogin):
    """Evidence sampling of the hosts in ``slow`` is held (a sibling whose
    proof acquisition is slow)."""

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.slow = set()
        self.held = asyncio.Event()
        self.release = asyncio.Event()

    async def fingerprint(self, subject):
        if self._where(subject.candidate)[1] in self.slow and not self.release.is_set():
            self.held.set()
            await self.release.wait()
        return await super().fingerprint(subject)


async def test_a2_the_first_provider_question_never_waits_for_a_siblings_slow_evidence(lab):
    repository, registry, engine, _provider, objects, locks, now = lab
    from providers.general_ftp.provider import GeneralFtpProvider
    registry.register_provider(GeneralFtpProvider())
    executor = SlowSampling(repository.authorize_execution, objects=objects, locks=locks)
    executor.slow.add("open.example")
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("sftp", "sftp://open.example/solo.bin"),
                                    TransferRequest("sftp", "sftp://locked.example/solo.bin")), deduplicate=False)
    locked = next(item for item in await repository.requests(transfer.id) if "locked" in item.request.payload)
    cycle = asyncio.ensure_future(engine.tick())
    try:
        await asyncio.wait_for(executor.held.wait(), timeout=10)
        asked = await _asked(engine, transfer.id, locked.id)
        assert asked is not None and asked.origin == InputOrigin.PROVIDER
        assert not cycle.done()  # the sibling's evidence is still held
    finally:
        executor.release.set()
    await asyncio.wait_for(cycle, timeout=10)


class DelayedLockedProvider(VaultProvider):
    """The locked source resolves only once the slow sibling's evidence is
    being acquired, so the sibling's decision is the one already deciding."""

    def __init__(self, gate):
        super().__init__()
        self.gate = gate

    async def resolve(self, request):
        if str(request.payload).startswith("locked.example/"):
            await self.gate.wait()
        return await super().resolve(request)


class SlowVault(VaultExecutor):
    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.held = asyncio.Event()
        self.release = asyncio.Event()

    async def fingerprint(self, subject):
        if self._object(subject.candidate).startswith("slow.example/") and not self.release.is_set():
            self.held.set()
            await self.release.wait()
        return await super().fingerprint(subject)


async def test_a2_the_first_evidence_question_never_waits_for_a_siblings_slow_evidence(base):
    repository, registry, engine, now = base
    vault = SlowVault(repository.authorize_execution, objects={
        "slow.example/item.bin": b"same", "locked.example/item.bin": b"same"},
        locks={"locked.example": ("evidence-user", "evidence-secret")})
    registry.register_provider(DelayedLockedProvider(vault.held))
    registry.register_executor(vault)
    transfer = await engine.submit((TransferRequest("vault", "slow.example/item.bin", name="item.bin"),
                                    TransferRequest("vault", "locked.example/item.bin", name="item.bin")),
                                   deduplicate=False)
    locked = next(item for item in await repository.requests(transfer.id) if "locked" in item.request.payload)
    cycle = asyncio.ensure_future(engine.tick())
    try:
        await asyncio.wait_for(vault.held.wait(), timeout=10)
        asked = await _asked(engine, transfer.id, locked.id)
        assert asked is not None, "the first question waited for an unrelated sibling's evidence"
        assert asked.origin == InputOrigin.EVIDENCE
        assert not cycle.done()
    finally:
        vault.release.set()
    await asyncio.wait_for(cycle, timeout=10)


# ── A3: no race regression ──────────────────────────────────────────────────

LOCKS = {"locked.example": ("locked-user", "locked-secret"), "other.example": ("other-user", "other-secret")}


class RaceVault(VaultExecutor):
    """Two locked sources and one open canonical. After the first answer is
    accepted, sampling of the canonical (the answered source's own
    equivalence proof) is held until released."""

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.accepted = asyncio.Event()
        self.held = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def fingerprint(self, subject):
        if (self._object(subject.candidate).startswith("open.example/") and self.accepted.is_set()
                and not self.release.is_set()):
            self.held.set()
            await self.release.wait()
        return await super().fingerprint(subject)

    async def fingerprint_with_input(self, subject, submitted):
        sample = await super().fingerprint_with_input(subject, submitted)
        if isinstance(sample, ArtifactFingerprint) and sample.kind != FingerprintKind.UNAVAILABLE:
            self.accepted.set()
        return sample


async def test_a3_one_writer_no_overwrite_no_scope_leak_and_no_resurrection(base):
    repository, registry, engine, now = base
    registry.register_provider(VaultProvider())
    vault = RaceVault(repository.authorize_execution, objects={
        "open.example/item.bin": b"same", "locked.example/item.bin": b"same", "other.example/item.bin": b"same"},
        locks=LOCKS)
    registry.register_executor(vault)
    canonical = await engine.submit((TransferRequest("vault", "open.example/item.bin", name="item.bin"),),
                                    deduplicate=False)
    for _ in range(3):
        now[0] += 5
        await engine.tick()
    incoming = await engine.submit((TransferRequest("vault", "locked.example/item.bin", name="item.bin"),
                                    TransferRequest("vault", "other.example/item.bin", name="item.bin")),
                                   deduplicate=False)
    first = await _first_question(engine, incoming.id, now, ticks=4)
    for _ in range(2):
        now[0] += 5
        await engine.tick()
    # A second question never overwrites the one outstanding.
    assert await _question_events(incoming.id) == 1, "a sibling's question overwrote the outstanding one"
    assert (await engine.challenges.current(incoming.id)).id == first.id

    records = {item.id: item for item in await repository.requests(incoming.id)}

    def lock_of(challenge):
        return LOCKS[records[challenge.request_id].request.payload.partition("/")[0]]

    vault.release.clear()
    user, password = lock_of(first)
    await engine.submit_input(incoming.id, first.id, "username_password", {"username": user, "password": password})
    cycle = asyncio.ensure_future(engine.tick())
    try:
        await asyncio.wait_for(vault.held.wait(), timeout=10)
        (sibling,) = [item for item in records if item != first.request_id]
        second = await _asked(engine, incoming.id, sibling)
        assert second is not None
        user, password = lock_of(second)
        # The sibling is answered while the first source's post-auth work is still held.
        await engine.submit_input(incoming.id, second.id, "username_password", {"username": user, "password": password})
        await asyncio.sleep(0.2)
        assert not cycle.done()
    finally:
        vault.release.set()
    await asyncio.wait_for(cycle, timeout=10)
    for _ in range(8):
        now[0] += 5
        await engine.tick()
    # Both sources are proven members of the canonical object: one writer ever.
    assert (await repository.get(incoming.id)).state == TransferState.CONSOLIDATED
    assert len([call for call in vault.calls if call[0] == "start"]) == 1
    assert (await repository.get(canonical.id)).state != TransferState.FAILED
    # No credential ever crossed its lineage and scope.
    for candidate_id, username in vault.samples:
        if username is None:
            continue
        address = next(item for item in [*await repository.resolved_candidates(first.request_id),
                                          *await repository.resolved_candidates(second.request_id)]
                       if str(item.id) == candidate_id).endpoints[0].address
        assert LOCKS[address.removeprefix("vault://").partition("/")[0]][0] == username
    # Exactly the two questions were ever asked; none came back.
    assert await _question_events(incoming.id) == 2
    assert await engine.challenges.current(incoming.id) is None
