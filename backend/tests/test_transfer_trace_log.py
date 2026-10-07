"""DP 1.0.13 Transfer Trace Log: the one read-only, sanitized transfer export.

The fixture builds real durable state through the engine: an earlier transfer
owns the canonical artifact and a later transfer's equivalent member is
consolidated beneath it, so the later transfer's trace must carry foreign
context to be intelligible.
"""
from dataclasses import replace
import hashlib
import json
import re
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import db.database as database
from api.routes import router
from application.service import ApplicationService
from fake_integrations import MemoryExecutor, ParcelProvider
from services import transfer_trace
from transfers import codec
from transfers.engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Retryability, Stage
from transfers.models import (
    Endpoint, OutcomeKind, ResolutionResult, ResourceState, TransferOutcome, TransferRequest,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository

# One capability URL both providers resolve to (so the two submissions are
# provably one artifact), carrying a path token and a signed query.
CAPABILITY = "https://cdn.example.org/dl/CAPTOKEN-X/part.rar?exp=1&sig=SIGSECRET"
FIRST_SOURCE = "https://alice:S3cretPass@files.example.com/d/CAPTOKEN-A/part.rar?sig=QSECRET-A"
SECOND_SOURCE = "https://mirror.example.net/get/CAPTOKEN-B?token=QSECRET-B"
SECRETS = ("alice", "S3cretPass", "CAPTOKEN-A", "QSECRET-A", "CAPTOKEN-B", "QSECRET-B", "CAPTOKEN-X", "SIGSECRET",
           "HDR-SECRET-123", "SECRETBLOB", "APIKEY-SECRET-9")


class CapabilityProvider(ParcelProvider):
    async def resolve(self, request):
        candidate = self.candidate("part.rar", payload="unused")
        endpoint = Endpoint("memory", CAPABILITY, {"Authorization": "Bearer HDR-SECRET-123"})
        return ResolutionResult(ResourceState.AVAILABLE, (replace(candidate, endpoints=(endpoint,)),))


@pytest_asyncio.fixture
async def traced(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    first, second = CapabilityProvider("provider-a"), CapabilityProvider("provider-b")
    registry.register_provider(first)
    registry.register_provider(second)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                            policy=TransferPolicy(adoption_stability_seconds=0), clock=lambda: 1000.0)
    await engine.initialize()
    owner = await engine.submit((TransferRequest("parcel", FIRST_SOURCE, name="part.rar",
                                                 preferred_provider="provider-a"),), name="owner", deduplicate=False)
    await engine.tick()
    later = await engine.submit((TransferRequest("parcel", SECOND_SOURCE, name="part.rar",
                                                 preferred_provider="provider-b"),), name="later", deduplicate=False)
    await engine.tick()
    async with database.get_db() as db:
        # Opaque submitted material and a secret-named field, as other writers
        # persist them for a transfer.
        await db.execute("INSERT INTO deferred_provider_submissions(torrent_id,kind,payload,filename) VALUES(?,?,?,?)",
                         (later.id, "torrent", b"d8:announce SECRETBLOB e", "part.torrent"))
        await db.execute("INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,?,?)",
                         (later.id, "diagnostic", json.dumps({"api_key": "APIKEY-SECRET-9", "note": "kept"})))
        await db.commit()
    canonical = (await repository.artifacts(owner.id))[0]
    return SimpleNamespace(owner=owner, later=later, canonical=canonical, engine=engine,
                           application=ApplicationService(engine), root=tmp_path / "payloads")


async def _table_columns(table):
    async with database.get_db() as db:
        return [row["name"] for row in await db.fetchall(f"PRAGMA table_info({table})")]


async def _database_digest():
    digest = hashlib.sha256()
    async with database.get_db() as db:
        tables = [row["name"] for row in await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for table in tables:
            for row in await db.fetchall(f"SELECT * FROM {table} ORDER BY rowid"):
                digest.update(repr((table, sorted(row.items()))).encode())
    return digest.hexdigest()


@pytest.mark.asyncio
async def test_trace_is_a_complete_transfer_scoped_export_with_metadata_and_inventory(traced):
    trace = await transfer_trace.build(traced.later.id, traced.application)
    metadata = trace["metadata"]
    assert metadata["requested_transfer_id"] == metadata["primary_transfer_id"] == traced.later.id
    assert metadata["trace_format"] == "debridpulse.transfer-trace" and metadata["trace_format_version"] == 6
    assert metadata["sanitization"]["applied"] is True and metadata["sanitization"]["replaced_values"] > 0
    assert metadata["generated_at"].endswith("Z") and metadata["application_version"]
    assert re.fullmatch(r"[0-9a-f]{64}", metadata["schema"]["columns_sha256"])

    inventory = {item["table"]: item for item in trace["inventory"]}
    for table in ("torrents", "transfer_requests", "resolution_attempts", "route_attempt_provenance",
                  "download_files", "canonical_candidate_bindings", "canonical_candidate_origins",
                  "artifact_consolidations", "deferred_provider_submissions", "application_events", "events"):
        assert inventory[table]["status"] == "populated", table
        assert inventory[table]["rows"] == len(trace["data"][table])
    assert inventory["transfer_file_manifests"]["status"] == "empty"
    assert inventory["stats_snapshots"]["status"] == "omitted"
    assert inventory["integration_runtime_state"]["status"] == "omitted"
    assert "stats_snapshots" not in trace["data"]
    # Complete rows: every column of every exported row, not a curated subset.
    for table in ("torrents", "transfer_requests", "download_files", "resolution_attempts"):
        columns = await _table_columns(table)
        assert all(list(item["row"]) == columns for item in trace["data"][table]), table
    own_request = next(item["row"] for item in trace["data"]["transfer_requests"] if item["scope"] == "primary")
    assert own_request["transfer_id"] == traced.later.id and own_request["state"] == "resolved"
    assert own_request["equivalence_disposition"] == "recovered"

    # A table this schema does not have is reported unsupported, not empty.
    async with database.get_db() as db:
        await db.execute("DROP TABLE postprocess_attempts")
        await db.commit()
    inventory = {item["table"]: item for item in (await transfer_trace.build(traced.later.id, traced.application))["inventory"]}
    assert inventory["postprocess_attempts"]["status"] == "unsupported"


@pytest.mark.asyncio
async def test_trace_follows_consolidation_into_the_foreign_canonical_owner(traced):
    trace = await transfer_trace.build(traced.later.id, traced.application)
    data = trace["data"]
    # The owner is a direct participant of the consolidation component: it is
    # exported with its own transfer-scoped rows (scope 'component').
    assert trace["metadata"]["component_transfer_ids"] == [traced.owner.id]
    assert trace["metadata"]["context_transfer_ids"] == []
    consolidation = next(item["row"] for item in data["artifact_consolidations"])
    assert consolidation["source_transfer_id"] == traced.later.id
    assert consolidation["canonical_artifact_id"] == traced.canonical.id
    # The foreign canonical artifact, its request, its transfer and its
    # candidate provenance are present -- explicitly as the component.
    canonical = next(item for item in data["download_files"] if item["row"]["id"] == traced.canonical.id)
    assert canonical["scope"] == "component" and canonical["row"]["torrent_id"] == traced.owner.id
    standby = next(item for item in data["download_files"] if item["row"]["torrent_id"] == traced.later.id)
    assert standby["scope"] == "primary" and standby["row"]["mirror_group_id"] == traced.canonical.id
    assert {item["scope"] for item in data["torrents"] if item["row"]["id"] == traced.owner.id} == {"component"}
    assert any(item["scope"] == "component" and item["row"]["id"] == canonical["row"]["request_id"]
               for item in data["transfer_requests"])
    bindings = [item["row"] for item in data["canonical_candidate_bindings"]]
    assert {row["canonical_artifact_id"] for row in bindings} == {traced.canonical.id}
    assert {row["provider_id"] for row in bindings} == {"provider-a", "provider-b"}
    origins = {row["contributing_transfer_id"] for row in (item["row"] for item in data["canonical_candidate_origins"])}
    assert origins == {traced.owner.id, traced.later.id}
    assert not [item for item in trace["references"] if item["status"] == "absent"]


@pytest.mark.asyncio
async def test_trace_sanitizes_credentials_and_capabilities_but_keeps_structure(traced):
    trace = await transfer_trace.build(traced.later.id, traced.application)
    text = json.dumps(trace)
    for secret in SECRETS:
        assert secret not in text, secret
    assert "files.example.com" in text and "mirror.example.net" in text
    # Every stored copy of the one capability URL becomes the same per-export
    # token, and the same header secret likewise: still correlatable.
    addresses, headers = set(), set()
    for item in trace["data"]["download_files"]:
        for candidate in json.loads(item["row"]["candidates"]):
            for endpoint in candidate["endpoints"]:
                addresses.add(endpoint["address"])
                headers.add(endpoint["headers"]["Authorization"])
    assert len(addresses) == 1 and re.fullmatch(r"https://cdn\.example\.org/<redacted-resource-\d+>", addresses.pop())
    assert len(headers) == 1 and re.fullmatch(r"<redacted-secret-\d+>", headers.pop())
    assert text.count(json.dumps(CAPABILITY)[1:-1]) == 0
    request = next(item["row"] for item in trace["data"]["transfer_requests"]
                   if item["row"]["transfer_id"] == traced.owner.id)
    owner_payload = json.loads(request["payload"])["payload"]
    # The submitted userinfo never reached durable state at all: admission split
    # it out as USER_SUPPLIED material, so only the capability path is redacted.
    assert re.fullmatch(r"https://files\.example\.com/<redacted-resource-\d+>", owner_payload)
    # Rows are kept; only the unsafe value is replaced.
    blob = trace["data"]["deferred_provider_submissions"][0]["row"]
    assert blob["filename"] == "part.torrent" and blob["payload"]["$redacted"] == "opaque"
    assert blob["payload"]["length"] == len(b"d8:announce SECRETBLOB e")
    detail = json.loads(next(item["row"]["detail"] for item in trace["data"]["application_events"]
                             if item["row"]["kind"] == "diagnostic"))
    assert detail["note"] == "kept" and re.fullmatch(r"<redacted-secret-\d+>", detail["api_key"])
    # Tokens are per export: nothing value-derived crosses traces.
    assert "<redacted-" in text and not re.search(r"[0-9a-f]{32,}", "".join(re.findall(r"<redacted-[^>]*>", text)))


@pytest.mark.asyncio
async def test_trace_exports_diagnostic_evidence_on_the_error_it_explains(traced):
    evidence = {"provider_operation": "torrent_manifest", "native_status": "downloaded",
                "native_file_count": 2, "native_selected_count": 2, "link_count": 1,
                "files": [{"ordinal": 0, "native_id": 1, "relative_path": "movie.mkv", "bytes": 22576859233,
                           "selected": True},
                          {"ordinal": 1, "native_id": 2, "relative_path": "movie.nfo", "bytes": 400, "selected": True}],
                "links": [{"ordinal": 0, "scheme": "https", "host": "real-debrid.com", "port": None,
                           "has_resource_component": True}],
                "note": "https://real-debrid.com/d/EVIDENCE-CAPABILITY"}
    error = NormalizedError(Domain.PROVIDER, Category.PROVIDER_PROTOCOL_VIOLATION, Stage.CANDIDATE_PREPARATION,
                            Retryability.NEVER, origin=Origin.PROVIDER, integration_id="realdebrid",
                            diagnostic="selected files and links do not reconcile", diagnostic_evidence=evidence)
    await traced.engine.repository.outcome(traced.later.id, TransferOutcome(OutcomeKind.FAILURE, error))
    # A row that bypassed the durable boundary still meets the trace's own
    # recursive sanitizer: the second defense is not relaxed for evidence.
    bypass = {**json.loads(codec.dump(error)),
              "diagnostic_evidence": {"link": "https://real-debrid.com/d/BYPASS-CAPABILITY", "token": "BYPASS-TOKEN"}}
    async with database.get_db() as db:
        await db.execute("UPDATE resolution_attempts SET error=? WHERE request_id IN "
                         "(SELECT id FROM transfer_requests WHERE transfer_id=?)", (json.dumps(bypass), traced.later.id))
        await db.commit()
    trace = await transfer_trace.build(traced.later.id, traced.application)
    assert trace["metadata"]["trace_format_version"] == 6
    assert not [key for key in trace if "evidence" in key]
    text = json.dumps(trace)
    for secret in ("EVIDENCE-CAPABILITY", "BYPASS-CAPABILITY", "BYPASS-TOKEN", "/d/"):
        assert secret not in text, secret
    (outcome,) = [json.loads(item["row"]["payload"]) for item in trace["data"]["transfer_outcomes"]
                  if item["row"]["transfer_id"] == traced.later.id and item["row"]["kind"] == "failure"]
    exported = outcome["error"]["diagnostic_evidence"]
    assert {key: exported[key] for key in ("native_file_count", "native_selected_count", "link_count")} == {
        "native_file_count": 2, "native_selected_count": 2, "link_count": 1}
    assert [(item["relative_path"], item["bytes"], item["selected"]) for item in exported["files"]] == [
        ("movie.mkv", 22576859233, True), ("movie.nfo", 400, True)]
    assert exported["links"] == evidence["links"] and exported["note"] == "<capability-url>"
    bypassed = {json.dumps(json.loads(item["row"]["error"])["diagnostic_evidence"], sort_keys=True)
                for item in trace["data"]["resolution_attempts"] if item["row"]["error"]}
    (only,) = [json.loads(item) for item in bypassed]
    assert re.fullmatch(r"https://real-debrid\.com/<redacted-resource-\d+>", only["link"])
    assert re.fullmatch(r"<redacted-secret-\d+>", only["token"])


@pytest.mark.asyncio
async def test_trace_generation_never_mutates_durable_state(traced):
    before = await _database_digest()
    await transfer_trace.export(traced.later.id, traced.application)
    await transfer_trace.export(traced.owner.id, traced.application)
    assert await _database_digest() == before


@pytest.mark.asyncio
async def test_trace_endpoint_returns_a_downloadable_json_file(traced):
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.application = traced.application
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/torrents/{traced.later.id}/trace")
        missing = await client.get("/api/torrents/999999/trace")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert re.fullmatch(
        rf'attachment; filename="debridpulse-transfer-{traced.later.id}-trace-\d{{8}}T\d{{6}}Z\.json"',
        response.headers["content-disposition"])
    assert response.json()["metadata"]["requested_transfer_id"] == traced.later.id
    assert missing.status_code == 404 and missing.json() == {"detail": "Transfer not found"}
