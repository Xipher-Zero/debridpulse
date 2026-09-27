"""DP 1.0.13 Transfer Trace completion: the non-durable evidence domains.

The trace observes the material on disk, the referenced executors' own view of
their executions and the current runtime context, each independently of the
durable snapshot and of one another, and says per domain whether it could.
Everything here goes through the one trace owner, ``services.transfer_trace``.
"""
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import re

import pytest

import db.database as database
from core import version
from services import transfer_trace
from test_transfer_trace_log import CapabilityProvider, _database_digest, traced  # noqa: F401 (fixture)
from transfers.errors import (
    Category, Domain, NormalizedError, Origin, Permanence, Retryability, Stage,
)
from transfers.models import HealthObservation, TransferRequest

REPO = Path(__file__).resolve().parents[2]


def _executor(traced):
    return traced.engine.registry.executors["memory-copy"]


def _owner_target(traced) -> Path:
    return traced.root / "part.rar"


def _target(trace, artifact_id):
    return next(item for item in trace["observations"]["filesystem"]["targets"] if item["artifact_id"] == artifact_id)


def _attempt(trace, artifact_id):
    return next(item for item in trace["observations"]["executors"]["attempts"] if item["artifact_id"] == artifact_id)


def _tree(root: Path) -> list:
    """Every path beneath ``root`` with its type, size and mtime."""
    found = []
    for current, dirs, files in os.walk(root):
        for name in sorted(dirs + files):
            info = os.lstat(os.path.join(current, name))
            found.append((os.path.relpath(os.path.join(current, name), root), info.st_mode, info.st_size,
                          info.st_mtime_ns))
    return sorted(found)


async def _complete_owner(traced):
    """Drive the owner's real execution to a verified, completed artifact."""
    executor = _executor(traced)
    handle = next(job.handle for job in executor.jobs.values())
    executor.finish(handle)
    await traced.engine.tick()
    assert (await traced.engine.repository.artifacts(traced.owner.id))[0].state == "completed"


# --- A: filesystem ------------------------------------------------------------

@pytest.mark.asyncio
async def test_material_is_observed_as_it_is_on_disk_with_size_compared_to_durable_belief(traced):
    target = _owner_target(traced)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"four")
    trace = await transfer_trace.build(traced.owner.id, traced.application)
    observed = _target(trace, traced.canonical.id)
    assert observed["exists"] is True and observed["type"] == "file" and observed["bytes"] == 4
    assert observed["durable_size_bytes"] == 4 and observed["size_matches_durable"] is True
    assert isinstance(observed["modified_at"], float)
    assert observed["execution_attempt_id"] == observed["material_owner_attempt_id"]

    target.write_bytes(b"seven b")
    observed = _target(await transfer_trace.build(traced.owner.id, traced.application), traced.canonical.id)
    assert observed["bytes"] == 7 and observed["size_matches_durable"] is False

    target.unlink()
    observed = _target(await transfer_trace.build(traced.owner.id, traced.application), traced.canonical.id)
    assert observed["exists"] is False and observed["type"] == "missing" and observed["size_matches_durable"] is None
    assert trace["collection_status"]["filesystem"]["status"] == "complete"


@pytest.mark.asyncio
async def test_a_directory_target_observes_only_its_durably_recorded_members(traced):
    collection = traced.root / "Collection"
    (collection / "sub").mkdir(parents=True)
    (collection / "sub" / "a.bin").write_bytes(b"aa")
    (collection / "unrecorded.bin").write_bytes(b"x")
    async with database.get_db() as db:
        await db.execute("UPDATE download_files SET local_path=? WHERE id=?", (str(collection), traced.canonical.id))
        await db.execute("UPDATE execution_attempts SET materialization=? WHERE artifact_id=?", (json.dumps(
            {"kind": "collection", "entries": [{"relative_path": "sub/a.bin", "bytes": 2},
                                               {"relative_path": "gone.bin", "bytes": 1}]}), traced.canonical.id))
        await db.commit()
    observed = _target(await transfer_trace.build(traced.owner.id, traced.application), traced.canonical.id)
    assert observed["type"] == "directory" and observed["bytes"] is None and observed["size_matches_durable"] is None
    members = {item["relative_path"]: item for item in observed["members"]}
    assert set(members) == {"sub/a.bin", "gone.bin"}, "a directory is never walked"
    assert members["sub/a.bin"]["type"] == "file" and members["sub/a.bin"]["bytes"] == 2
    assert members["gone.bin"]["type"] == "missing"


@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
@pytest.mark.asyncio
async def test_a_stat_failure_is_unavailable_never_missing_and_the_domain_is_partial(traced):
    locked = traced.root / "locked"
    locked.mkdir(parents=True)
    async with database.get_db() as db:
        await db.execute("UPDATE download_files SET local_path=? WHERE id=?",
                         (str(locked / "part.rar"), traced.canonical.id))
        await db.commit()
    locked.chmod(0)
    try:
        trace = await transfer_trace.build(traced.later.id, traced.application)
    finally:
        locked.chmod(0o755)
    observed = _target(trace, traced.canonical.id)
    assert observed["observation"] == "unavailable" and observed["exists"] is None
    assert observed["type"] == "unavailable" and observed["error"] == "EACCES"
    status = trace["collection_status"]["filesystem"]
    assert status["status"] == "partial" and status["counts"]["unavailable"] == 1


# --- B: executors ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_only_referenced_executions_are_observed_and_absence_is_the_executors_answer(traced):
    executor = _executor(traced)
    running = next(iter(executor.jobs.values()))
    # Unrelated native work the executor also holds: never exported.
    executor.jobs["unrelated-attempt"] = running
    trace = await transfer_trace.build(traced.later.id, traced.application)
    attempts = trace["observations"]["executors"]["attempts"]
    assert [item["execution_attempt_id"] for item in attempts] == [running.handle.attempt_id]
    assert "unrelated-attempt" not in json.dumps(trace)
    observed = attempts[0]
    assert observed["dp_owned"] is True and observed["observation"] == "observed"
    assert observed["durable_state"] == "running" and observed["executor"]["state"] == "running"
    assert observed["executor"]["native_exists"] is True
    assert observed["executor"]["completed_bytes"] == 1 and observed["executor"]["total_bytes"] == 4

    # The native job disappears while durable state still says running: both
    # sides are exported, neither is rewritten.
    del executor.jobs[running.handle.attempt_id]
    trace = await transfer_trace.build(traced.later.id, traced.application)
    observed = _attempt(trace, traced.canonical.id)
    assert observed["durable_state"] == "running"
    assert observed["executor"]["state"] == "absent" and observed["executor"]["native_exists"] is False
    assert trace["collection_status"]["executor"]["status"] == "complete"
    row = next(item["row"] for item in trace["data"]["execution_attempts"])
    assert row["state"] == "running"


@pytest.mark.asyncio
async def test_an_unreachable_or_hung_executor_is_unavailable_and_the_trace_still_succeeds(traced, monkeypatch):
    executor = _executor(traced)

    async def unreachable(handles):
        raise ConnectionError("executor RPC refused at http://127.0.0.1:6800/jsonrpc?token=RPCSECRET")

    executor.observe_many = unreachable
    trace = await transfer_trace.build(traced.later.id, traced.application)
    observed = _attempt(trace, traced.canonical.id)
    assert observed["observation"] == "unavailable" and observed["reason"] == "executor_state_unknown"
    assert observed["executor"]["native_exists"] is None, "no answer is never 'the job does not exist'"
    assert observed["executor"]["error"]["category"] == "unmapped_executor_error"
    assert trace["collection_status"]["executor"]["status"] == "unavailable"
    assert "RPCSECRET" not in json.dumps(trace)

    async def hung(handles):
        await asyncio.Event().wait()

    executor.observe_many = hung
    monkeypatch.setattr(transfer_trace, "OBSERVATION_TIMEOUT_SECONDS", 0.05)
    trace = await asyncio.wait_for(transfer_trace.build(traced.later.id, traced.application), 5)
    observed = _attempt(trace, traced.canonical.id)
    assert observed["observation"] == "unavailable" and observed["reason"] == "executor_timeout"
    assert "executor" not in observed


@pytest.mark.asyncio
async def test_attempts_dp_no_longer_owns_are_not_observed_and_say_so(traced):
    async with database.get_db() as db:
        await db.execute("UPDATE execution_attempts SET authorized=0")
        await db.commit()
    before = list(_executor(traced).calls)
    trace = await transfer_trace.build(traced.later.id, traced.application)
    observed = _attempt(trace, traced.canonical.id)
    assert observed["dp_owned"] is False and observed["observation"] == "not_applicable"
    assert observed["reason"] == "attempt_not_dp_owned"
    assert _executor(traced).calls == before
    assert trace["collection_status"]["executor"]["status"] == "not_applicable"


# --- C: runtime context ---------------------------------------------------------

class _AuthRejectedProvider(CapabilityProvider):
    async def health(self):
        return HealthObservation(False, NormalizedError(
            Domain.PROVIDER, Category.AUTHENTICATION_FAILED, Stage.RESOLUTION, retryability=Retryability.AFTER_REAUTH,
            origin=Origin.PROVIDER, permanence=Permanence.PERMANENT, operator_action_required=True,
            integration_id="provider-a", native_code="AUTH_BAD_APIKEY",
            diagnostic="api_key=APIKEY-LIVE-777 rejected; Authorization: Bearer BEARER-LIVE-888",
            context={"http_status": 401, "api_key": "APIKEY-LIVE-777", "endpoint": "https://api.example.com/v4/user"}))


@pytest.mark.asyncio
async def test_runtime_context_is_narrow_current_and_keeps_auth_failures_classifiable(traced):
    registry = traced.engine.registry
    registry.providers["provider-a"] = _AuthRejectedProvider("provider-a")
    registry.register_provider(CapabilityProvider("provider-unreferenced"))
    trace = await transfer_trace.build(traced.later.id, traced.application)
    context = trace["runtime_context"]
    assert context["temporal_scope"] == "export_time"
    assert "do not describe any earlier moment" in trace["metadata"]["observation_boundary"]
    integrations = {item["identity"]: item for item in context["integrations"]}
    assert set(integrations) == {"provider-a", "provider-b", "memory-copy"}
    assert {item["role"] for item in integrations.values()} == {"provider", "executor"}
    assert integrations["provider-b"]["readiness"] == {
        "observation": "unsupported", "reason": "provider_declares_no_health_contract"}
    assert integrations["memory-copy"]["readiness"]["reachable"] is True

    readiness = integrations["provider-a"]["readiness"]
    assert readiness["observation"] == "observed" and readiness["healthy"] is False
    error = readiness["error"]
    assert error["category"] == "authentication_failed" and error["domain"] == "provider"
    assert error["retryability"] == "after_reauth" and error["permanence"] == "permanent"
    assert error["operator_action_required"] is True and error["origin"] == "provider"
    assert error["native_code"] == "AUTH_BAD_APIKEY" and error["context"]["http_status"] == 401
    # The canonical error owner already redacted the context URL at
    # construction; the key -- the fact that an endpoint was involved -- stays.
    assert error["stage"] == "resolution" and error["context"]["endpoint"] == "<capability-url>"
    text = json.dumps(trace)
    for secret in ("APIKEY-LIVE-777", "BEARER-LIVE-888", "/v4/user"):
        assert secret not in text, secret
    # Current readiness contradicts durable history (the provider resolved
    # successfully): both are exported as they are.
    resolved = [item["row"] for item in trace["data"]["resolution_attempts"]
                if item["row"].get("provider_id") == "provider-a"]
    assert resolved and all(row["state"] != "failed" for row in resolved)

    execution = context["transfer_execution"]
    assert execution["download_root"] == "<redacted-path-root-1>"
    assert execution["policy"]["max_active_executions"] == traced.engine.policy.max_active_executions
    assert execution["globally_paused"] is False
    assert not {"options", "api_key", "password"} & {key for item in integrations.values() for key in item}
    assert trace["collection_status"]["runtime_context"]["status"] == "complete"


@pytest.mark.asyncio
async def test_a_hung_readiness_probe_makes_runtime_context_partial(traced, monkeypatch):
    class Hung(CapabilityProvider):
        async def health(self):
            await asyncio.Event().wait()

    traced.engine.registry.providers["provider-b"] = Hung("provider-b")
    monkeypatch.setattr(transfer_trace, "OBSERVATION_TIMEOUT_SECONDS", 0.05)
    trace = await transfer_trace.build(traced.later.id, traced.application)
    readiness = next(item["readiness"] for item in trace["runtime_context"]["integrations"]
                     if item["identity"] == "provider-b")
    assert readiness == {"observation": "unavailable", "reason": "health_timeout"}
    assert trace["collection_status"]["runtime_context"]["status"] == "partial"


# --- D: completeness ------------------------------------------------------------

@pytest.mark.asyncio
async def test_every_domain_reports_its_collection_status(traced):
    status = (await transfer_trace.build(traced.later.id, traced.application))["collection_status"]
    assert {key: value["status"] for key, value in status.items()} == {
        "durable_state": "complete", "filesystem": "complete", "executor": "complete", "runtime_context": "complete"}

    # A transfer with no material target and no execution yet.
    fresh = await traced.engine.submit((TransferRequest("parcel", "https://fresh.example.com/f", name="f.rar"),),
                                       name="fresh", deduplicate=False)
    status = (await transfer_trace.build(fresh.id, traced.application))["collection_status"]
    assert status["filesystem"]["status"] == "not_applicable" and status["filesystem"]["reason"]
    assert status["executor"]["status"] == "not_applicable" and status["executor"]["reason"]

    # No runtime to observe through: unavailable, never omitted.
    trace = await transfer_trace.build(traced.later.id, None)
    status = trace["collection_status"]
    assert status["executor"]["status"] == "unavailable"
    assert status["runtime_context"] == {"status": "unavailable", "reason": "no_application_runtime"}
    assert _attempt(trace, traced.canonical.id)["reason"] == "no_application_runtime"


# --- E/F: build identity and process timing -------------------------------------

@pytest.mark.asyncio
async def test_build_revision_is_the_packaged_revision_and_null_only_without_one(traced, monkeypatch):
    revision = "0123456789abcdef0123456789abcdef01234567"
    for value, expected in ((revision, revision), ("unknown", None), (None, None)):
        if value is None:
            monkeypatch.delenv(version.BUILD_REVISION_ENV, raising=False)
        else:
            monkeypatch.setenv(version.BUILD_REVISION_ENV, value)
        version.read_build_revision.cache_clear()
        metadata = (await transfer_trace.build(traced.later.id, traced.application))["metadata"]
        assert metadata["build_revision"] == expected, value
    version.read_build_revision.cache_clear()
    dockerfile = (REPO / "Dockerfile").read_text()
    assert "ENV DEBRIDPULSE_BUILD_REVISION=${VCS_REF}" in dockerfile
    assert version.BUILD_REVISION_ENV == "DEBRIDPULSE_BUILD_REVISION"


@pytest.mark.asyncio
async def test_process_start_and_uptime_are_present_and_sane(traced):
    metadata = (await transfer_trace.build(traced.later.id, traced.application))["metadata"]
    started = datetime.fromisoformat(metadata["process"]["started_at"].replace("Z", "+00:00"))
    generated = datetime.fromisoformat(metadata["generated_at"].replace("Z", "+00:00"))
    assert started <= generated
    assert 0 <= metadata["process"]["uptime_seconds"] <= (generated - started).total_seconds() + 1
    assert set(metadata["process"]) == {"started_at", "uptime_seconds"}


# --- G: sanitization ------------------------------------------------------------

@pytest.mark.asyncio
async def test_path_roots_are_per_export_tokens_that_keep_the_relative_layout(traced):
    _owner_target(traced).parent.mkdir(parents=True, exist_ok=True)
    _owner_target(traced).write_bytes(b"four")
    trace = await transfer_trace.build(traced.later.id, traced.application)
    text = json.dumps(trace)
    assert str(traced.root) not in text and str(traced.root.parent) not in text
    root = trace["runtime_context"]["transfer_execution"]["download_root"]
    assert re.fullmatch(r"<redacted-path-root-\d+>", root)
    # Every copy of the root -- durable row, handle correlation, observation --
    # is the same token, and the filename beneath it survives.
    assert _target(trace, traced.canonical.id)["path"] == f"{root}/part.rar"
    assert all(item["row"]["local_path"] == f"{root}/part.rar" for item in trace["data"]["download_files"])
    correlation = _attempt(trace, traced.canonical.id)["handle"]["correlation"]
    assert correlation["root"] == root and correlation["destination"] == f"{root}/part.rar"
    tokens = set(re.findall(r"<redacted-[a-z-]+-\d+>", text))
    assert tokens and not any(re.search(r"[0-9a-f]{8,}", token) for token in tokens)


# --- H/I: independence and the read-only guarantee ------------------------------

@pytest.mark.asyncio
async def test_a_completed_artifact_whose_material_is_gone_is_exported_as_a_contradiction(traced):
    await _complete_owner(traced)
    _owner_target(traced).unlink()
    trace = await transfer_trace.build(traced.owner.id, traced.application)
    observed = _target(trace, traced.canonical.id)
    assert observed["durable_status"] == "completed" and observed["type"] == "missing"
    row = next(item["row"] for item in trace["data"]["download_files"] if item["row"]["id"] == traced.canonical.id)
    assert row["status"] == "completed"
    assert not _owner_target(traced).exists(), "observation never repairs"


@pytest.mark.asyncio
async def test_trace_generation_mutates_no_database_file_executor_or_recovery_state(traced):
    _owner_target(traced).parent.mkdir(parents=True, exist_ok=True)
    _owner_target(traced).write_bytes(b"four")
    executor = _executor(traced)
    database_before = await _database_digest()
    files_before = _tree(traced.root.parent)
    jobs_before = dict(executor.jobs)
    calls_before = list(executor.calls)
    for transfer in (traced.later.id, traced.owner.id):
        await transfer_trace.export(transfer, traced.application)
    assert await _database_digest() == database_before
    assert _tree(traced.root.parent) == files_before
    assert executor.jobs == jobs_before
    # Only reads reached the executor: no start, pause, resume or cancel.
    # (Recovery claims, retries and reconciliation are durable rows: the
    # database digest above already proves none was taken or written.)
    assert {call for call, _ in executor.calls[len(calls_before):]} == {"observe"}
