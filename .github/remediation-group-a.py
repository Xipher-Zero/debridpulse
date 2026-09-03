"""Temporary Group A remediation applicator. Removed by the successful runner."""
from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match, found {count}")
    target.write_text(text.replace(old, new, 1))


replace_once(
    "backend/transfers/registry.py",
    "from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError\n",
    "from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage, TransferError\n",
)
replace_once(
    "backend/transfers/registry.py",
    '''    def provider_for(self, request: TransferRequest) -> Provider:\n        providers = self.eligible_providers(request)\n        if not providers:\n            raise TransferError(NormalizedError(\n                Domain.REQUEST, Category.UNSUPPORTED_REQUEST, Stage.RESOLUTION,\n                retryability=Retryability.NEVER,\n            ))\n        return providers[0]\n\n''',
    '''    def provider_for(self, request: TransferRequest) -> Provider:\n        providers = self.eligible_providers(request)\n        if not providers:\n            raise TransferError(NormalizedError(\n                Domain.REQUEST, Category.UNSUPPORTED_REQUEST, Stage.RESOLUTION,\n                retryability=Retryability.NEVER,\n            ))\n        return providers[0]\n\n    def provider_for_bound_route(self, provider_id: str, request: TransferRequest) -> Provider:\n        """Recover an existing route without reopening global provider selection.\n\n        Applicability is an initial-routing fact. Once selected, the durable route\n        remains authoritative across retries and restart. Enablement/health retain\n        their existing admitted-work semantics, but neither may select a replacement.\n        """\n        provider = self.providers.get(provider_id)\n        if (provider is None or Capability.RESOLVE not in provider.descriptor.capabilities\n                or request.kind not in provider.descriptor.request_types):\n            raise TransferError(NormalizedError(\n                Domain.REQUEST, Category.UNSUPPORTED_CAPABILITY, Stage.RESOLUTION,\n                retryability=Retryability.NEVER, integration_id=provider_id,\n            ))\n        if not provider.descriptor.enabled:\n            raise TransferError(NormalizedError(\n                Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION,\n                retryability=Retryability.NEVER, recovery=Recovery.FAIL,\n                integration_id=provider_id,\n            ))\n        if provider_id in self._unhealthy:\n            raise TransferError(NormalizedError(\n                Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION,\n                retryability=Retryability.BACKOFF, recovery=Recovery.BACKOFF,\n                integration_id=provider_id,\n            ))\n        return provider\n\n''',
)

replace_once(
    "backend/transfers/repository.py",
    '''    async def begin_resolution(self, request_id: str, provider_id: str) -> ResolutionAttempt | None:\n''',
    '''    async def bound_route_provider(self, request_id: str) -> str | None:\n        """Return the provider owning the latest durable route for this request."""\n        async with get_db() as db:\n            row = await db.fetchone(\n                """SELECT a.provider_id FROM route_attempt_provenance p\n                JOIN resolution_attempts a ON a.id=p.resolution_attempt_id\n                WHERE a.request_id=? ORDER BY p.ordinal DESC LIMIT 1""",\n                (request_id,),\n            )\n        return str(row["provider_id"]) if row and row.get("provider_id") else None\n\n    async def begin_resolution(self, request_id: str, provider_id: str) -> ResolutionAttempt | None:\n''',
)
replace_once(
    "backend/transfers/repository.py",
    '''    async def resolution(self, attempt: ResolutionAttempt, result: ResolutionResult) -> bool:\n        async with get_db() as db:\n''',
    '''    async def resolution(self, attempt: ResolutionAttempt, result: ResolutionResult) -> bool:\n        # Defense in depth: route identity is selected by the universal core.\n        identities = [candidate.provider_id for candidate in result.candidates]\n        identities.extend(candidate.resource.provider_id for candidate in result.candidates if candidate.resource)\n        if result.observation:\n            identities.append(result.observation.resource.provider_id)\n        if any(identity and identity != attempt.provider_id for identity in identities):\n            raise TransferError(NormalizedError(\n                Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION,\n                integration_id=attempt.provider_id,\n            ))\n        async with get_db() as db:\n''',
)

replace_once(
    "backend/transfers/engine.py",
    "    TransferState, new_identity,\n",
    "    TransferCandidate, TransferState, new_identity,\n",
)
replace_once(
    "backend/transfers/engine.py",
    '''    async def submit(self, requests: tuple[TransferRequest, ...], *, name="", source="manual", priority=0, reacquire=True, deduplicate=True):\n''',
    '''    @classmethod\n    def _authoritative_provider_result(cls, provider_id: str, result: ResolutionResult) -> ResolutionResult:\n        """Validate and stamp provider output with the selected route identity."""\n        if not isinstance(result, ResolutionResult):\n            raise TransferError(cls._error(\n                Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION, domain=Domain.PROVIDER,\n                retryability=Retryability.NEVER,\n            ))\n\n        def authoritative_resource(value):\n            if value is None:\n                return None\n            if value.provider_id and value.provider_id != provider_id:\n                raise TransferError(cls._error(\n                    Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION, domain=Domain.PROVIDER,\n                    retryability=Retryability.NEVER,\n                ))\n            return value if value.provider_id == provider_id else replace(value, provider_id=provider_id)\n\n        candidates = []\n        for candidate in result.candidates:\n            if not isinstance(candidate, TransferCandidate):\n                raise TransferError(cls._error(\n                    Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION, domain=Domain.PROVIDER,\n                    retryability=Retryability.NEVER,\n                ))\n            if candidate.provider_id and candidate.provider_id != provider_id:\n                raise TransferError(cls._error(\n                    Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION, domain=Domain.PROVIDER,\n                    retryability=Retryability.NEVER,\n                ))\n            candidates.append(replace(\n                candidate, provider_id=provider_id, resource=authoritative_resource(candidate.resource),\n            ))\n\n        observation = result.observation\n        if observation is not None:\n            observation = replace(observation, resource=authoritative_resource(observation.resource))\n        return replace(result, candidates=tuple(candidates), observation=observation)\n\n    async def submit(self, requests: tuple[TransferRequest, ...], *, name="", source="manual", priority=0, reacquire=True, deduplicate=True):\n''',
)
replace_once(
    "backend/transfers/engine.py",
    '''            provider = self.registry.provider_for(record.request)\n            async with self._resolution_slots:\n''',
    '''            bound_provider_id = await self.repository.bound_route_provider(record.id)\n            provider = (\n                self.registry.provider_for_bound_route(bound_provider_id, record.request)\n                if bound_provider_id else self.registry.provider_for(record.request)\n            )\n            async with self._resolution_slots:\n''',
)
replace_once(
    "backend/transfers/engine.py",
    '''    async def _apply_resolution(self, record: RequestRecord, attempt: ResolutionAttempt, provider, result: ResolutionResult,\n                                *, challenge: InputChallenge | None = None):\n        if not isinstance(result, ResolutionResult):\n            raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION))\n''',
    '''    async def _apply_resolution(self, record: RequestRecord, attempt: ResolutionAttempt, provider, result: ResolutionResult,\n                                *, challenge: InputChallenge | None = None):\n        result = self._authoritative_provider_result(provider.descriptor.id, result)\n''',
)

Path("backend/tests/test_audit_remediation_group_a.py").write_text(r'''"""Adversarial ROUTE-001 + CORE-001 contract tests."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

import db.database as database
from fake_integrations import ParcelProvider
from transfers.engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage, TransferError
from transfers.models import ResolutionResult, ResourceState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository

pytestmark = pytest.mark.asyncio


async def core(tmp_path, monkeypatch, name="group-a.sqlite3"):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / name)
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    first = ParcelProvider("provider_a")
    second = ParcelProvider("provider_b")
    registry.register_provider(first)
    registry.register_provider(second)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "downloads"),
        policy=TransferPolicy(resolution_max_attempts=4, resolution_retry_delay=0, adoption_stability_seconds=0),
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    return SimpleNamespace(repository=repository, registry=registry, first=first, second=second, engine=engine)


def retryable_failure():
    return NormalizedError(
        Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION,
        retryability=Retryability.IMMEDIATE, recovery=Recovery.RETRY,
    )


async def test_retry_stays_bound_when_fresh_selection_would_choose_other_provider(tmp_path, monkeypatch):
    c = await core(tmp_path, monkeypatch)
    c.first.responses = [ResolutionResult(ResourceState.UNKNOWN, error=retryable_failure())]
    transfer = await c.engine.submit((TransferRequest("parcel", "box", name="payload.bin"),), deduplicate=False)
    await c.engine.resolve_pending()
    assert [call for call in c.first.calls if call[0] == "resolve"]
    assert not [call for call in c.second.calls if call[0] == "resolve"]

    c.first.descriptor = replace(c.first.descriptor, enabled=False)
    await c.engine.resolve_pending()
    assert not [call for call in c.second.calls if call[0] == "resolve"]
    details = await c.repository.presentation(transfer.id, details=True)
    assert {item["provider_id"] for item in details["route_attempts"]} == {"provider_a"}


async def test_health_drift_cannot_reopen_global_selection(tmp_path, monkeypatch):
    c = await core(tmp_path, monkeypatch, "health.sqlite3")
    c.first.responses = [ResolutionResult(ResourceState.UNKNOWN, error=retryable_failure())]
    transfer = await c.engine.submit((TransferRequest("parcel", "box", name="payload.bin"),), deduplicate=False)
    await c.engine.resolve_pending()
    c.registry.mark_health("provider_a", healthy=False)
    await c.engine.resolve_pending()
    assert not [call for call in c.second.calls if call[0] == "resolve"]
    assert (await c.repository.presentation(transfer.id, details=True))["current_provider_id"] == "provider_a"


async def test_restart_recovers_bound_provider_without_reclassification(tmp_path, monkeypatch):
    c = await core(tmp_path, monkeypatch, "restart.sqlite3")
    c.first.responses = [ResolutionResult(ResourceState.UNKNOWN, error=retryable_failure())]
    transfer = await c.engine.submit((TransferRequest("parcel", "box", name="payload.bin"),), deduplicate=False)
    await c.engine.resolve_pending()

    c.first.descriptor = replace(c.first.descriptor, priority=0)
    c.second.descriptor = replace(c.second.descriptor, priority=100)
    restarted = TransferEngine(
        TransferRepository(), c.registry, download_root=c.engine.root, policy=c.engine.policy, clock=lambda: 1000.0,
    )
    await restarted.initialize()
    before_b = len([call for call in c.second.calls if call[0] == "resolve"])
    await restarted.resolve_pending()
    assert len([call for call in c.second.calls if call[0] == "resolve"]) == before_b
    details = await c.repository.presentation(transfer.id, details=True)
    assert {item["provider_id"] for item in details["route_attempts"]} == {"provider_a"}


@pytest.mark.parametrize("claimed_identity", ["provider_b", "general_http", "alldebrid"])
async def test_adapter_cannot_forge_selected_provider_identity(tmp_path, monkeypatch, claimed_identity):
    c = await core(tmp_path, monkeypatch, f"forge-{claimed_identity}.sqlite3")
    forged = replace(c.first.candidate("payload.bin"), provider_id=claimed_identity)
    c.first.responses = [ResolutionResult(ResourceState.AVAILABLE, (forged,))]
    transfer = await c.engine.submit((TransferRequest("parcel", "box", name="payload.bin"),), deduplicate=False)
    await c.engine.resolve_pending()
    assert not await c.repository.artifacts(transfer.id)
    details = await c.repository.presentation(transfer.id, details=True)
    assert details["route_attempts"][0]["provider_id"] == "provider_a"
    assert details["route_attempts"][0]["outcome"] == "failed"
    assert details["route_attempts"][0]["candidates"] == []


async def test_core_stamps_provider_neutral_candidate_with_selected_route(tmp_path, monkeypatch):
    c = await core(tmp_path, monkeypatch, "stamp.sqlite3")
    neutral = replace(c.first.candidate("payload.bin"), provider_id="")
    c.first.responses = [ResolutionResult(ResourceState.AVAILABLE, (neutral,))]
    transfer = await c.engine.submit((TransferRequest("parcel", "box", name="payload.bin"),), deduplicate=False)
    await c.engine.resolve_pending()
    artifacts = await c.repository.artifacts(transfer.id)
    assert artifacts and artifacts[0].candidates[0].provider_id == "provider_a"
    details = await c.repository.presentation(transfer.id, details=True)
    assert details["route_attempts"][0]["candidates"][0]["provider_id"] == "provider_a"


async def test_repository_rejects_contradictory_provider_before_persistence(tmp_path, monkeypatch):
    c = await core(tmp_path, monkeypatch, "repository-guard.sqlite3")
    transfer = await c.engine.submit((TransferRequest("parcel", "box", name="payload.bin"),), deduplicate=False)
    record = (await c.repository.requests(transfer.id))[0]
    attempt = await c.repository.begin_resolution(record.id, "provider_a")
    forged = replace(c.first.candidate("payload.bin"), provider_id="provider_b")
    with pytest.raises(TransferError):
        await c.repository.resolution(attempt, ResolutionResult(ResourceState.AVAILABLE, (forged,)))
    details = await c.repository.presentation(transfer.id, details=True)
    assert details["route_attempts"][0]["candidates"] == []
''')
