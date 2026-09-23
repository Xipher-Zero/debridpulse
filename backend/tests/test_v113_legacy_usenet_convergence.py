"""1.0.13: a Usenet transfer resolved BEFORE the staged-input correction must
still run after the upgrade.

The pre-upgrade durable representation carried the manifest inline, twice:

    transfer_requests.payload                  -> {"$bytes": <base64>}
    download_files.candidates[sel].context     -> {"nzb_base64": <base64>, ...}

and the current executor reads ``staged_input`` alone. A queued/retrying
transfer present at upgrade therefore fails permanently with
``invalid_request`` and never reaches the acquisition service.

These tests do not call a migration helper. They build the durable rows with
the ACTUAL old candidate shape, through the real repository and codec, and then
drive ordinary current-version execution through the real engine and executor.
"""
from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from transfers.convergence_engine import TransferEngine
from transfers.models import (
    Capability, IntegrationDescriptor, MaterializationKind, ResolutionResult, ResourceState,
    SourceIdentity, TransferCandidate, TransferRequest,
)
from transfers.applicability import ProviderApplicability
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry
from transfers.staged_input import StagedInputStore

from sab_fakes import FakeSab


VALID_NZB = b"""<?xml version="1.0" encoding="iso-8859-1" ?>
<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
 <file poster="p@e.net" date="1700000000" subject="upgrade [1/1] - &quot;upgrade.bin&quot; yEnc (1/1)">
  <groups><group>alt.binaries.test</group></groups>
  <segments><segment bytes="4096" number="1">a@e.net</segment></segments>
 </file>
</nzb>
"""

# The exact pre-upgrade context keys (baseline 3f67dfe9,
# providers/usenet/provider.py).
LEGACY_MANIFEST = "nzb_base64"
DECLARED_BYTES = "nzb_declared_bytes"


class LegacyUsenetProvider:
    """A verbatim reconstruction of the pre-upgrade provider's ``resolve``.

    Kept deliberately faithful: the candidate it returns is the one the older
    release actually persisted, so the durable rows under test are produced by
    the real repository and the real codec rather than hand-written JSON.
    """

    applicability = ProviderApplicability()
    descriptor = IntegrationDescriptor(
        "usenet", "Usenet", frozenset({Capability.RESOLVE}),
        request_types=frozenset({"nzb"}),
    )

    async def resolve(self, request: TransferRequest) -> ResolutionResult:
        payload = request.payload
        if isinstance(payload, str):
            payload = payload.encode("utf-8", "strict")
        name = "upgrade"
        candidate = TransferCandidate(
            name=name,
            endpoints=(),
            expected_bytes=4096,
            provider_id="usenet",
            materialization=MaterializationKind.COLLECTION,
            context={
                LEGACY_MANIFEST: base64.b64encode(bytes(payload)).decode("ascii"),
                DECLARED_BYTES: 4096,
            },
            source_identity=SourceIdentity("usenet", name),
        )
        return ResolutionResult(ResourceState.AVAILABLE, (candidate,))


async def build(tmp_path, monkeypatch, *, provider, staged_root=None, sab=None):
    from executors.sabnzbd.executor import SabnzbdConfiguration, SabnzbdExecutor

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    root = tmp_path / "payloads"
    root.mkdir(parents=True, exist_ok=True)
    staged = StagedInputStore(str(staged_root or (tmp_path / "staged")))
    # A restart re-attaches to the SAME acquisition service, so one may be
    # supplied: a fresh service would report every live job absent and the
    # engine would rightly resubmit, which tests nothing about migration.
    sab = sab or FakeSab(complete_dir=str(root / ".dpwork" / "complete"),
                         download_dir=str(root / ".dpwork" / "incomplete"))
    executor = SabnzbdExecutor(
        sab,
        SabnzbdConfiguration(local_root=str(root), working_directory=str(root / ".dpwork"),
                             complete_directory=str(root / ".dpwork" / "complete")),
        repository.authorize_execution,
        staged_input=staged,
        converge=repository.converge_staged_input,
    )
    registry.register_provider(provider)
    registry.register_executor(executor)
    now = [1000.0]
    engine = TransferEngine(repository, registry, download_root=str(root),
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                                                  max_active_executions=4),
                            clock=lambda: now[0])
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, registry=registry,
                           executor=executor, sab=sab, staged=staged, root=root, now=now)


@pytest_asyncio.fixture
async def legacy(tmp_path, monkeypatch):
    return await build(tmp_path, monkeypatch, provider=LegacyUsenetProvider())


async def converge(core, rounds=10):
    """Drive the real engine. Time advances, so a retry actually becomes due --
    a failure that is retryable only in principle proves nothing."""
    import asyncio
    for _ in range(rounds):
        await core.engine.tick()
        await asyncio.sleep(0)
        core.now[0] += 30


async def pre_upgrade(core, payload=VALID_NZB):
    """Durable state exactly as the older release left it.

    Resolution runs (writing the old candidate through the real repository and
    codec) while dispatch is withheld, so the rows under test are the ones an
    upgrade actually finds: resolved, not yet executed. Nothing about the
    transfer is special-cased -- only the moment the snapshot is taken.
    """
    core.engine.dispatch_permitted = False
    transfer_id = await submit(core, payload)
    await converge(core, rounds=6)
    before = await durable(core, transfer_id)
    core.engine.dispatch_permitted = True
    return transfer_id, before


async def submit(core, payload=VALID_NZB):
    result = await core.engine.submit(
        (TransferRequest("nzb", payload, name="upgrade.nzb"),),
        name="upgrade", deduplicate=False)
    return getattr(result, "id", result)


async def durable(core, transfer_id):
    """The two durable places the submitted input lives, read back raw."""
    from db.database import get_db
    from transfers import codec
    async with get_db() as db:
        request = await db.fetchone(
            "SELECT payload FROM transfer_requests WHERE transfer_id=? ORDER BY id LIMIT 1",
            (transfer_id,))
        artifact = await db.fetchone(
            "SELECT candidates,selected_candidate FROM download_files WHERE torrent_id=? LIMIT 1",
            (transfer_id,))
    payload = codec.load(request["payload"], {}).get("payload") if request else None
    candidates = codec.load(artifact["candidates"], []) if artifact else []
    selected = artifact["selected_candidate"] if artifact else 0
    context = (candidates[selected].get("context") or {}) if candidates else {}
    return SimpleNamespace(request_payload=payload, context=context,
                           candidates=candidates, selected=selected)


async def artifact_error(core, transfer_id):
    for item in await core.repository.artifacts(transfer_id):
        if item.error is not None:
            return item.error
    return None


# --- 1. the pre-upgrade queued candidate --------------------------------------

@pytest.mark.asyncio
async def test_pre_upgrade_queued_candidate_reaches_the_service(legacy):
    transfer_id, before = await pre_upgrade(legacy)
    assert "$bytes" in (before.request_payload or {}), "fixture is not the old durable shape"
    assert LEGACY_MANIFEST in before.context and "staged_input" not in before.context

    await converge(legacy)

    after = await durable(legacy, transfer_id)
    assert len(legacy.sab.submissions) == 1, "exactly one native submission"
    assert "$staged" in (after.request_payload or {}), "durable request did not converge"
    assert "staged_input" in after.context, "candidate did not converge"
    assert LEGACY_MANIFEST not in after.context, "obsolete representation was retained"
    assert after.context["staged_input"] == after.request_payload["$staged"], \
        "request and candidate must reference ONE staged payload"


@pytest.mark.asyncio
async def test_convergence_preserves_every_unrelated_candidate_fact(legacy):
    transfer_id, before = await pre_upgrade(legacy)
    await converge(legacy)
    after = await durable(legacy, transfer_id)

    assert len(after.candidates) == len(before.candidates)
    assert after.selected == before.selected
    unchanged = dict(before.candidates[before.selected])
    changed = dict(after.candidates[after.selected])
    assert unchanged.pop("context") is not None
    changed.pop("context")
    assert changed == unchanged, "a fact other than the input representation changed"
    assert after.context[DECLARED_BYTES] == before.context[DECLARED_BYTES]


@pytest.mark.asyncio
async def test_staged_bytes_are_exactly_the_submitted_manifest(legacy):
    transfer_id, _ = await pre_upgrade(legacy)
    await converge(legacy)
    after = await durable(legacy, transfer_id)
    from transfers.staged_input import StagedPayload
    reference = StagedPayload.from_context(after.context["staged_input"])
    assert legacy.staged.read(reference) == VALID_NZB


# --- 2. restart after convergence ---------------------------------------------

@pytest.mark.asyncio
async def test_restart_after_convergence_does_not_migrate_again(tmp_path, monkeypatch):
    staged_root = tmp_path / "staged"
    first = await build(tmp_path, monkeypatch, provider=LegacyUsenetProvider(), staged_root=staged_root)
    transfer_id, _ = await pre_upgrade(first)
    await converge(first)
    converged = await durable(first, transfer_id)
    staged_files = sorted(p.name for p in staged_root.glob("*.input"))
    assert len(first.sab.submissions) == 1

    # A fresh repository/engine over the SAME database, staged root and
    # acquisition service -- which is what a restart actually is.
    second = await build(tmp_path, monkeypatch, provider=LegacyUsenetProvider(),
                         staged_root=staged_root, sab=first.sab)
    await converge(second)
    again = await durable(second, transfer_id)
    assert again.request_payload == converged.request_payload
    assert again.context["staged_input"] == converged.context["staged_input"]
    assert sorted(p.name for p in staged_root.glob("*.input")) == staged_files, \
        "restart staged a duplicate payload"
    assert len(second.sab.submissions) == 1, "restart submitted the job a second time"


# --- 3. failure after staging, before durable commit ---------------------------

@pytest.mark.asyncio
async def test_persistence_failure_leaves_legacy_state_retryable(legacy, monkeypatch):
    transfer_id, _ = await pre_upgrade(legacy)
    calls = []

    original = legacy.repository.converge_staged_input

    async def failing(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("durable convergence unavailable")

    monkeypatch.setattr(legacy.executor, "converge", failing)
    await converge(legacy)

    assert calls, "the executor never attempted durable convergence"
    assert len(legacy.sab.submissions) == 0, "submitted before durable convergence"
    still = await durable(legacy, transfer_id)
    assert LEGACY_MANIFEST in still.context, "legacy representation was destroyed"
    assert "$bytes" in (still.request_payload or {})
    error = await artifact_error(legacy, transfer_id)
    assert error is not None and error.retryability.value != "never", \
        "a persistence failure was terminalized as permanent invalid input"

    # Retry with the real operation converges.
    monkeypatch.setattr(legacy.executor, "converge", original)
    await converge(legacy)
    done = await durable(legacy, transfer_id)
    assert "staged_input" in done.context and "$staged" in done.request_payload


@pytest.mark.asyncio
async def test_staging_failure_is_infrastructure_not_malformed_input(legacy, monkeypatch):
    from transfers.staged_input import StagedInputError
    transfer_id, _ = await pre_upgrade(legacy)

    async def unavailable(chunks):
        raise StagedInputError("staged input storage is unavailable")

    monkeypatch.setattr(legacy.staged, "stage", unavailable)
    await converge(legacy)
    assert len(legacy.sab.submissions) == 0
    error = await artifact_error(legacy, transfer_id)
    assert error is not None and error.retryability.value != "never"
    still = await durable(legacy, transfer_id)
    assert LEGACY_MANIFEST in still.context


# --- 5. malformed legacy input -------------------------------------------------

@pytest.mark.asyncio
async def test_malformed_base64_fails_closed_permanently(tmp_path, monkeypatch):
    class Malformed(LegacyUsenetProvider):
        async def resolve(self, request):
            result = await LegacyUsenetProvider.resolve(self, request)
            candidate = result.candidates[0]
            broken = {**candidate.context, LEGACY_MANIFEST: "not*valid*base64!!"}
            from dataclasses import replace
            return ResolutionResult(result.state, (replace(candidate, context=broken),))

    core = await build(tmp_path, monkeypatch, provider=Malformed())
    transfer_id, _ = await pre_upgrade(core)
    await converge(core)
    assert len(core.sab.submissions) == 0
    error = await artifact_error(core, transfer_id)
    assert error is not None
    assert error.category.value == "invalid_request"
    assert error.retryability.value == "never"
    state = await durable(core, transfer_id)
    assert "staged_input" not in state.context, "a partial durable conversion was left behind"
    assert "$bytes" in (state.request_payload or {})


@pytest.mark.asyncio
async def test_empty_legacy_payload_fails_closed(tmp_path, monkeypatch):
    class Empty(LegacyUsenetProvider):
        async def resolve(self, request):
            result = await LegacyUsenetProvider.resolve(self, request)
            candidate = result.candidates[0]
            from dataclasses import replace
            return ResolutionResult(result.state, (replace(
                candidate, context={**candidate.context, LEGACY_MANIFEST: ""}),))

    core = await build(tmp_path, monkeypatch, provider=Empty())
    transfer_id, _ = await pre_upgrade(core)
    await converge(core)
    assert len(core.sab.submissions) == 0
    error = await artifact_error(core, transfer_id)
    assert error is not None and error.retryability.value == "never"


# --- 6. request/candidate disagreement -----------------------------------------

@pytest.mark.asyncio
async def test_request_candidate_mismatch_fails_closed(tmp_path, monkeypatch):
    other = VALID_NZB.replace(b"upgrade.bin", b"different.bin")

    class Divergent(LegacyUsenetProvider):
        async def resolve(self, request):
            result = await LegacyUsenetProvider.resolve(self, request)
            candidate = result.candidates[0]
            from dataclasses import replace
            return ResolutionResult(result.state, (replace(candidate, context={
                **candidate.context,
                LEGACY_MANIFEST: base64.b64encode(other).decode("ascii")}),))

    core = await build(tmp_path, monkeypatch, provider=Divergent())
    transfer_id, _ = await pre_upgrade(core)
    await converge(core)
    assert len(core.sab.submissions) == 0, "submitted on unproven authority"
    error = await artifact_error(core, transfer_id)
    assert error is not None and error.retryability.value == "never"
    state = await durable(core, transfer_id)
    assert "$bytes" in (state.request_payload or {}), "a guessed convergence was committed"
    assert LEGACY_MANIFEST in state.context


# --- 7. a current candidate is untouched ---------------------------------------

@pytest.mark.asyncio
async def test_current_staged_candidate_is_not_rewritten(tmp_path, monkeypatch):
    from providers.usenet.provider import UsenetProvider
    staged_root = tmp_path / "staged"
    core = await build(tmp_path, monkeypatch,
                       provider=UsenetProvider(staged_input=StagedInputStore(str(staged_root))),
                       staged_root=staged_root)
    transfer_id, _ = await pre_upgrade(core)
    await converge(core)
    state = await durable(core, transfer_id)
    assert "staged_input" in state.context and LEGACY_MANIFEST not in state.context
    assert len(core.sab.submissions) == 1
    assert len(list(staged_root.glob("*.input"))) == 1, "the current path staged twice"


# --- 8. staged-input lifetime --------------------------------------------------

@pytest.mark.asyncio
async def test_sweep_retains_a_migrated_live_input_and_reclaims_it_when_terminal(legacy):
    import time
    transfer_id, _ = await pre_upgrade(legacy)
    await converge(legacy)
    state = await durable(legacy, transfer_id)
    identity = state.context["staged_input"]["id"]

    live = await legacy.repository.referenced_staged_inputs()
    assert identity in live, "a migrated live input is invisible to the survivor set"
    future = time.time() + 10 * 3600
    assert legacy.staged.sweep(live, now=future) == 0
    assert (legacy.staged.root / f"{identity}.input").exists()

    from db.database import get_db
    async with get_db() as db:
        await db.execute("UPDATE torrents SET status='completed' WHERE id=?", (transfer_id,))
        await db.commit()
    terminal = await legacy.repository.referenced_staged_inputs()
    assert identity not in terminal
    assert legacy.staged.sweep(terminal, now=future) >= 1
    assert not (legacy.staged.root / f"{identity}.input").exists()


# --- 9. an upgrade caught mid-retry --------------------------------------------

@pytest.mark.asyncio
async def test_a_retrying_upgrade_state_is_not_terminalized_as_invalid(legacy):
    """A transfer whose first native attempt already failed once before the
    upgrade must converge on its next attempt, not die of its own age."""
    from sab_fakes import SabTransportError
    transfer_id, _ = await pre_upgrade(legacy)

    # The first submission fails in transport: an ordinary retryable condition
    # that says nothing about the input. The service stays observable, so core
    # keeps dispatching to it -- an executor core considers unavailable is
    # never asked to start at all, which would prove nothing here.
    original = legacy.sab.addfile
    attempts = []

    async def flaky(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise SabTransportError("connection reset")
        return await original(*args, **kwargs)

    legacy.sab.addfile = flaky
    await converge(legacy, rounds=4)
    error = await artifact_error(legacy, transfer_id)
    if error is not None:
        assert error.retryability.value != "never", "an old representation became permanent"
    # Durable convergence precedes submission, so it already happened.
    mid = await durable(legacy, transfer_id)
    assert "staged_input" in mid.context, "convergence waited on the native service"
    assert attempts, "no submission was ever attempted"

    await converge(legacy, rounds=10)
    state = await durable(legacy, transfer_id)
    assert "staged_input" in state.context
    assert "$staged" in state.request_payload
    assert len(legacy.sab.submissions) >= 1


# --- 4. crash immediately after durable commit ---------------------------------

@pytest.mark.asyncio
async def test_after_commit_execution_needs_no_legacy_capability(tmp_path, monkeypatch):
    """Once the rows are canonical, the migration capability is not required.

    Proves the convergence is genuinely one-way: an executor with no durable
    convergence seam at all still runs the transfer.
    """
    staged_root = tmp_path / "staged"
    first = await build(tmp_path, monkeypatch, provider=LegacyUsenetProvider(), staged_root=staged_root)
    transfer_id, _ = await pre_upgrade(first)
    await converge(first)
    assert "staged_input" in (await durable(first, transfer_id)).context

    second = await build(tmp_path, monkeypatch, provider=LegacyUsenetProvider(),
                         staged_root=staged_root, sab=first.sab)
    second.executor.converge = None
    await converge(second)
    state = await durable(second, transfer_id)
    assert "$staged" in state.request_payload
    assert LEGACY_MANIFEST not in state.context


# --- 6b. partial / unexpected durable request representations ------------------
#
# The candidate still carries the obsolete inline form while the durable
# REQUEST carries something the migration did not expect. Nothing about a
# freshly staged copy of the candidate's bytes makes it authority to overwrite
# a durable request, so every one of these must fail closed.

async def rewrite_request_payload(transfer_id, payload_value):
    """Put an arbitrary payload representation on the root request row."""
    from db.database import get_db
    from transfers import codec
    async with get_db() as db:
        row = await db.fetchone(
            "SELECT id,payload FROM transfer_requests WHERE transfer_id=? ORDER BY id LIMIT 1",
            (transfer_id,))
        document = codec.load(row["payload"], {})
        document["payload"] = payload_value
        await db.execute("UPDATE transfer_requests SET payload=? WHERE id=?",
                         (codec.dump(document), row["id"]))
        await db.commit()


@pytest.mark.asyncio
async def test_request_already_staged_to_a_different_digest_fails_closed(legacy):
    """request = staged reference A, candidate = legacy bytes B, A != B.

    The previous revision proved the request only when it happened to carry
    ``$bytes``, so this shape fell through and A was overwritten with a newly
    staged B -- an unproven durable authority replaced by a guess.
    """
    transfer_id, before = await pre_upgrade(legacy)
    other = legacy.staged.stage_bytes(VALID_NZB.replace(b"upgrade.bin", b"other.bin"))
    await rewrite_request_payload(transfer_id, {"$staged": other.as_context()})

    await converge(legacy)

    assert len(legacy.sab.submissions) == 0, "submitted on unproven authority"
    state = await durable(legacy, transfer_id)
    assert state.request_payload == {"$staged": other.as_context()}, \
        "a durable staged reference was overwritten without proof"
    assert LEGACY_MANIFEST in state.context, "the candidate was converged anyway"
    error = await artifact_error(legacy, transfer_id)
    assert error is not None and error.retryability.value == "never"


@pytest.mark.asyncio
async def test_request_already_staged_to_the_same_bytes_converges_onto_it(legacy):
    """A genuinely half-converged row: request canonical, candidate obsolete.

    Recoverable, but only onto the reference that is ALREADY durable -- never
    onto the caller's newer copy of the same bytes.
    """
    transfer_id, _ = await pre_upgrade(legacy)
    existing = legacy.staged.stage_bytes(VALID_NZB)
    await rewrite_request_payload(transfer_id, {"$staged": existing.as_context()})

    await converge(legacy)

    state = await durable(legacy, transfer_id)
    assert state.request_payload == {"$staged": existing.as_context()}, \
        "the already-durable reference was replaced"
    assert state.context["staged_input"] == existing.as_context()
    assert LEGACY_MANIFEST not in state.context
    assert len(legacy.sab.submissions) == 1


@pytest.mark.asyncio
async def test_request_payload_of_an_unrecognised_shape_fails_closed(legacy):
    transfer_id, _ = await pre_upgrade(legacy)
    await rewrite_request_payload(transfer_id, {"$unknown": "something else"})

    await converge(legacy)

    assert len(legacy.sab.submissions) == 0
    state = await durable(legacy, transfer_id)
    assert state.request_payload == {"$unknown": "something else"}
    assert LEGACY_MANIFEST in state.context
    error = await artifact_error(legacy, transfer_id)
    assert error is not None and error.retryability.value == "never"


@pytest.mark.asyncio
async def test_request_payload_that_is_not_an_object_fails_closed(legacy):
    transfer_id, _ = await pre_upgrade(legacy)
    await rewrite_request_payload(transfer_id, "a plain string payload")

    await converge(legacy)

    assert len(legacy.sab.submissions) == 0
    state = await durable(legacy, transfer_id)
    assert state.request_payload == "a plain string payload"
    assert LEGACY_MANIFEST in state.context


@pytest.mark.asyncio
async def test_request_inline_bytes_that_do_not_match_the_candidate_fail_closed(legacy):
    """Both sides inline, but disagreeing: no authority to pick one."""
    import base64 as b64
    transfer_id, _ = await pre_upgrade(legacy)
    await rewrite_request_payload(transfer_id, {"$bytes": b64.b64encode(
        VALID_NZB.replace(b"upgrade.bin", b"divergent.bin")).decode("ascii")})

    await converge(legacy)

    assert len(legacy.sab.submissions) == 0
    state = await durable(legacy, transfer_id)
    assert "$bytes" in state.request_payload
    assert LEGACY_MANIFEST in state.context
    error = await artifact_error(legacy, transfer_id)
    assert error is not None and error.retryability.value == "never"


@pytest.mark.asyncio
async def test_the_durable_convergence_is_proven_after_the_commit(legacy):
    """The frozen ordering ends in a re-read that proves the committed rows.

    Driven by making the post-commit read observe a non-canonical row: the
    convergence must be reported as a durable inconsistency rather than as
    success, and nothing may be submitted on it.
    """
    transfer_id, _ = await pre_upgrade(legacy)
    repository = legacy.repository
    original = repository._staged_input_is_canonical
    calls = []

    async def once(db, artifact_id, staged_context, context_key, retire):
        calls.append(1)
        proven = await original(db, artifact_id, staged_context, context_key, retire)
        # True inside the transaction, False on the post-commit re-read.
        return proven if len(calls) == 1 else False

    repository._staged_input_is_canonical = once
    try:
        await converge(legacy, rounds=2)
    finally:
        repository._staged_input_is_canonical = original

    assert len(calls) >= 2, "the committed rows were never re-read"
    assert len(legacy.sab.submissions) == 0, "submitted on an unproven convergence"
