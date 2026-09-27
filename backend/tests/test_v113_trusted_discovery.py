"""DP 1.0.13 trusted authenticated discovery, proven without aria2 or SSH.

The real ``ScpProvider`` asks core for one read-only listing of a directory;
core runs it through the executor the claim router selects, answers any
requirement through the one authentication-input owner (or the one
INPUT_REQUIRED lifecycle), and hands the neutral result back. The member set
becomes the existing file manifest and is never re-enumerated. The executor
here is an in-memory fake that claims ``sftp`` endpoints -- nothing below is
coupled to aria2, libssh2 or asyncssh.

Chronology: this file imports ``DiscoveryResult``/``DiscoveredEntry``, which do
not exist on the pre-generalization checkpoint, so its RED is a collection
error there (see the Gate 9 packet).
"""
from __future__ import annotations

from urllib.parse import unquote, urlsplit

import pytest

from test_v113_transfer_auth_context import (
    FINGERPRINT, PASSWORD, USER, WRONG, CountingVault, IdentityVault, _answer, _current, _db_text, _events, lab,  # noqa: F401
)
from transfers.errors import Category
from transfers.input_required import auth_required, username_password
from transfers.models import (
    DiscoveredEntry, DiscoveryResult, ExecutorCapabilities, InputField, IntegrationDescriptor, TransferRequest,
    TransferState,
)

pytestmark = pytest.mark.asyncio

LISTING = {"locked.example/dir/": ["a.bin", "b.bin", "c.bin"],
           "locked.example/mixed/": ["one.bin", "two.bin", "notes.txt"]}


class Listing(CountingVault):
    """In-memory SFTP-claiming transport with read-only directory discovery."""

    descriptor = IntegrationDescriptor("sftp-memory", "SFTP memory", frozenset())
    capabilities = ExecutorCapabilities(candidate_sampling=True, per_execution_pause=True, transient_input=True,
                                        remote_discovery=True)
    claim_schemes = frozenset({"sftp"})

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.listing = {key: list(value) for key, value in LISTING.items()}
        self.discoveries = []

    @staticmethod
    def _object(candidate):
        parts = urlsplit(candidate.endpoints[0].address)
        return f"{parts.hostname}{unquote(parts.path)}"

    def _denied(self, host, submitted):
        if host not in self.locks:
            return False
        if submitted is None:
            return True
        return (submitted.value(InputField.USERNAME), submitted.value(InputField.PASSWORD)) != self.locks[host]

    async def discover(self, subject, submitted=None):
        parts = urlsplit(subject.candidate.endpoints[0].address)
        self.discoveries.append(submitted.value(InputField.PASSWORD) if submitted is not None else None)
        if submitted is not None and submitted.value(InputField.PASSWORD) == WRONG:
            self.wrong_attempts += 1
        if self._denied(parts.hostname, submitted):
            return self._discovery_requirement(parts.hostname)
        names = self.listing[f"{parts.hostname}{parts.path}"]
        return DiscoveryResult(tuple(DiscoveredEntry(name, 4) for name in names))

    def _discovery_requirement(self, host):
        return auth_required(username_password())


class IdentityListing(Listing, IdentityVault):
    descriptor = IntegrationDescriptor("sftp-identity-memory", "SFTP identity memory", frozenset())

    def _discovery_requirement(self, host):
        return self._requirement(host)

    async def discover(self, subject, submitted=None):
        if submitted is not None:
            facts = {fact.name.value: fact.value for fact in submitted.facts}
            assert facts.get("server_identity_fingerprint") == FINGERPRINT, "discovery trusted an unconfirmed server"
        return await super().discover(subject, submitted)


def _setup(lab, kind=Listing):
    from providers.general_scp.provider import ScpProvider
    repository, registry, engine, *_rest, now = lab
    registry.register_provider(ScpProvider())
    executor = kind(repository.authorize_execution, objects={f"locked.example/dir/{name}": b"four"
                                                             for name in ("a.bin", "b.bin", "c.bin", "new.bin")}
                    | {f"locked.example/mixed/{name}": b"four" for name in ("one.bin", "two.bin", "notes.txt")},
                    locks={"locked.example": (USER, PASSWORD)})
    registry.register_executor(executor)
    return repository, engine, executor, now


async def _drive(engine, repository, transfer_id, now, *, answers=(), count=80):
    """Tick; answer each new challenge with the next queued answer."""
    queue = list(answers)
    seen = []
    for _ in range(count):
        now[0] += 5
        await engine.tick()
        current = await _current(engine, transfer_id)
        if current is not None and current.id not in {item.id for item in seen}:
            seen.append(current)
            if queue:
                method, password = queue.pop(0)
                await _answer(engine, transfer_id, current, method=method, password=password)
        if (await repository.get(transfer_id)).state in {TransferState.COMPLETED, TransferState.FAILED}:
            break
    return seen


async def _members(repository, transfer_id):
    return sorted(record.request.payload for record in await repository.requests(transfer_id) if record.parent_id)


# 43.1 -- no credentials: one interaction, discovery, every sibling runs unasked.
async def test_directory_needs_one_answer_then_every_member_reuses_it(lab):
    repository, engine, executor, now = _setup(lab)
    transfer = await engine.submit((TransferRequest("ssh", "ssh://locked.example/dir/"),), deduplicate=False)
    seen = await _drive(engine, repository, transfer.id, now, answers=[("username_password", PASSWORD)])
    assert len(seen) == 1 and seen[0].origin.value == "provider"
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert await _members(repository, transfer.id) == [f"ssh://locked.example/dir/{name}" for name in ("a.bin", "b.bin", "c.bin")]
    assert [user for _candidate, user in executor.input_starts] == [USER, USER, USER]
    assert executor.discoveries == [None, PASSWORD]


# 43.2 / 43.10 / 19 -- embedded credentials: identity still confirmed, once, for every member.
async def test_embedded_credentials_leave_one_identity_confirmation_for_the_whole_directory(lab):
    repository, engine, executor, now = _setup(lab, IdentityListing)
    transfer = await engine.submit((TransferRequest("ssh", f"ssh://{USER}:{PASSWORD}@locked.example/dir/"),),
                                   deduplicate=False)
    seen = await _drive(engine, repository, transfer.id, now, answers=[("server_identity", None)])
    assert [(item.reason.value, [m.method.value for m in item.methods]) for item in seen] == [
        ("server_identity_required", ["server_identity"])]
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert [user for _candidate, user in executor.input_starts] == [USER, USER, USER]
    text = await _db_text()
    assert PASSWORD not in text and f"{USER}:" not in text


# 43.3 -- a bad embedded password: tried once, one prompt, the answer is reused.
async def test_bad_embedded_password_is_tried_once_then_one_prompt(lab):
    repository, engine, executor, now = _setup(lab)
    transfer = await engine.submit((TransferRequest("scp", f"scp://{USER}:{WRONG}@locked.example/dir/"),),
                                   deduplicate=False)
    seen = await _drive(engine, repository, transfer.id, now, answers=[("username_password", PASSWORD)])
    assert len(seen) == 1
    assert executor.wrong_attempts == 1
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert (await _events(transfer.id)).count("auth_rejected") == 1


# 43.12 -- the persisted member set is authoritative; new remote files never appear.
async def test_member_set_is_frozen_at_discovery(lab):
    repository, engine, executor, now = _setup(lab)
    transfer = await engine.submit((TransferRequest("scp", f"scp://{USER}:{PASSWORD}@locked.example/dir/"),),
                                   deduplicate=False)
    await _drive(engine, repository, transfer.id, now, count=2)
    listed = len(executor.discoveries)
    assert executor.discoveries.count(PASSWORD) == 1  # one authenticated listing
    executor.listing["locked.example/dir/"].append("new.bin")
    await _drive(engine, repository, transfer.id, now)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert "scp://locked.example/dir/new.bin" not in await _members(repository, transfer.id)
    assert len(executor.discoveries) == listed, "the directory was re-enumerated"


# 43.13 -- a final-component pattern selects only matching immediate files.
async def test_final_component_pattern_selects_only_matching_members(lab):
    repository, engine, _executor, now = _setup(lab)
    transfer = await engine.submit((TransferRequest("scp", f"scp://{USER}:{PASSWORD}@locked.example/mixed/*.bin"),),
                                   deduplicate=False)
    await _drive(engine, repository, transfer.id, now)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert await _members(repository, transfer.id) == ["scp://locked.example/mixed/one.bin",
                                                       "scp://locked.example/mixed/two.bin"]


async def test_a_pattern_matching_nothing_is_a_missing_source_not_a_literal_download(lab):
    repository, engine, executor, now = _setup(lab)
    transfer = await engine.submit((TransferRequest("scp", f"scp://{USER}:{PASSWORD}@locked.example/mixed/*.iso"),),
                                   deduplicate=False)
    await _drive(engine, repository, transfer.id, now, count=6)
    assert await _members(repository, transfer.id) == []
    assert executor.calls == []  # nothing was ever handed to a writer
    records = await repository.requests(transfer.id)
    assert records[0].error is not None and records[0].error.category == Category.SOURCE_NOT_FOUND


# 43.7 -- cancellation during a pending discovery question destroys the context.
async def test_cancel_during_discovery_destroys_the_context(lab):
    repository, engine, executor, now = _setup(lab, IdentityListing)
    transfer = await engine.submit((TransferRequest("ssh", f"ssh://{USER}:{PASSWORD}@locked.example/dir/"),),
                                   deduplicate=False)
    seen = await _drive(engine, repository, transfer.id, now, count=3)
    assert len(seen) == 1 and await engine.inputs.holds(transfer.id)
    await engine.cancel(transfer.id)
    assert not await engine.inputs.holds(transfer.id)
    assert executor.input_starts == []


async def test_interactive_selection_offers_every_discovered_member(lab):
    repository, engine, _executor, now = _setup(lab)
    transfer = await engine.submit((TransferRequest("scp", f"scp://{USER}:{PASSWORD}@locked.example/dir/",
                                                    selection_mode="interactive"),), deduplicate=False)
    await _drive(engine, repository, transfer.id, now, count=3)
    from db.database import get_db
    async with get_db() as db:
        rows = await db.fetchall("""SELECT e.relative_path FROM transfer_file_manifest_entries e
            JOIN transfer_file_manifests m ON m.id=e.manifest_id WHERE m.transfer_id=? ORDER BY e.relative_path""",
                                 (transfer.id,))
    assert [row["relative_path"] for row in rows] == ["a.bin", "b.bin", "c.bin"]
