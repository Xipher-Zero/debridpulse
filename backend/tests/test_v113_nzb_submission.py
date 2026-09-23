"""1.0.13 Gate-9 rev-3, item 3: an operator can actually submit an NZB.

Upload -> canonical ApplicationService seam -> TransferRequest(kind="nzb") ->
Usenet provider -> universal core routing -> executor. No HTTP route and no
frontend code ever speaks to the acquisition service, and there is no second
execution path.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


# --- the service seam ----------------------------------------------------

def test_the_application_exposes_a_canonical_nzb_submission():
    from application.service import ApplicationService
    assert callable(getattr(ApplicationService, "submit_nzb", None))


def test_submission_builds_the_canonical_request_kind():
    from application.service import ApplicationService
    source = inspect.getsource(ApplicationService.submit_nzb)
    assert 'TransferRequest("nzb"' in source
    # It goes through the ONE submission seam, never a private execution path.
    assert "self.submit(" in source


def code_without_prose(source: str) -> str:
    """Source with docstrings stripped, so prose cannot satisfy or break a check."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body.pop(0)
    return ast.unparse(tree)


def test_submission_never_touches_the_acquisition_service():
    from application.service import ApplicationService
    source = code_without_prose(
        textwrap.dedent(inspect.getsource(ApplicationService.submit_nzb))).lower()
    for forbidden in ("sab", "nzo", "addfile", "executor"):
        assert forbidden not in source, forbidden


# --- the HTTP route ------------------------------------------------------

def test_an_upload_route_exists_and_delegates_to_the_service():
    routes = (REPO / "backend/api/routes.py").read_text(encoding="utf-8")
    assert '@router.post("/usenet/add-file")' in routes
    assert "application.submit_nzb(" in routes


def test_the_route_does_not_bypass_provider_routing():
    routes = (REPO / "backend/api/routes.py").read_text(encoding="utf-8")
    start = routes.index('@router.post("/usenet/add-file")')
    end = routes.index("@router", start + 10)
    handler = code_without_prose(
        textwrap.dedent(routes[start:end].split("\n", 1)[1])).lower()
    for forbidden in ("sabnzbd", "addfile(", "nzo_id", "client."):
        assert forbidden not in handler, forbidden


def test_no_frontend_code_calls_the_acquisition_service_directly():
    for path in (REPO / "frontend/static").glob("*.js"):
        text = path.read_text(encoding="utf-8").lower()
        for forbidden in ("sabnzbd", "/api?mode=", "nzo_id"):
            assert forbidden not in text, f"{path.name}: {forbidden}"


# --- end to end through the real engine ----------------------------------

@pytest.mark.asyncio
async def test_a_submitted_nzb_reaches_the_executor_through_normal_routing(tmp_path, monkeypatch):
    import asyncio
    import db.database as database
    from executors.sabnzbd.executor import SabnzbdConfiguration, SabnzbdExecutor
    from providers.usenet.provider import UsenetProvider
    from sab_fakes import FakeSab
    from transfers.convergence_engine import TransferEngine
    from transfers.policy import TransferPolicy
    from transfers.recovery_repository import TransferRepository
    from transfers.registry import IntegrationRegistry
    from application.service import ApplicationService

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    root = tmp_path / "payloads"
    (root / ".dpwork" / "complete").mkdir(parents=True)
    (root / ".dpwork" / "incomplete").mkdir(parents=True)
    sab = FakeSab(complete_dir=str(root / ".dpwork" / "complete"),
                  download_dir=str(root / ".dpwork" / "incomplete"))
    registry = IntegrationRegistry()
    registry.register_provider(UsenetProvider())
    registry.register_executor(SabnzbdExecutor(
        sab, SabnzbdConfiguration(local_root=str(root),
                                  working_directory=str(root / ".dpwork"),
                                  complete_directory=str(root / ".dpwork" / "complete")),
        repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(root),
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                                                  max_active_executions=2),
                            clock=lambda: 1000.0)
    await engine.initialize()
    service = ApplicationService(engine)

    nzb = (b'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
           b'<file poster="p@e.net" date="1700000000" subject="x [1/1] - &quot;x.bin&quot; yEnc (1/1)">'
           b"<groups><group>alt.binaries.test</group></groups>"
           b'<segments><segment bytes="1024" number="1">a@e.net</segment></segments>'
           b"</file></nzb>")
    result = await service.submit_nzb(nzb, "posting.nzb")
    assert result and result.get("id")

    for _ in range(8):
        await engine.tick()
        await asyncio.sleep(0)

    # The operator's upload became exactly one native job via normal routing.
    assert len(sab.submissions) == 1


@pytest.mark.asyncio
async def test_an_empty_upload_is_refused_without_format_knowledge(tmp_path, monkeypatch):
    """The application layer refuses only what needs no NZB knowledge."""
    import db.database as database
    from transfers.convergence_engine import TransferEngine
    from transfers.policy import TransferPolicy
    from transfers.recovery_repository import TransferRepository
    from transfers.registry import IntegrationRegistry
    from application.service import ApplicationService

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    engine = TransferEngine(TransferRepository(), IntegrationRegistry(),
                            download_root=str(tmp_path), policy=TransferPolicy())
    await engine.initialize()
    with pytest.raises(ValueError):
        await ApplicationService(engine).submit_nzb(b"", "empty.nzb")


@pytest.mark.asyncio
async def test_a_malformed_posting_is_refused_by_the_provider_not_the_service(tmp_path, monkeypatch):
    """Format validation belongs to the provider, during resolution.

    A malformed upload is admitted as a transfer and then rejected by the
    Usenet provider, so it NEVER reaches the executor and no native work is
    created. The application layer stays free of NZB knowledge.
    """
    import asyncio
    import db.database as database
    from executors.sabnzbd.executor import SabnzbdConfiguration, SabnzbdExecutor
    from providers.usenet.provider import UsenetProvider
    from sab_fakes import FakeSab
    from transfers.convergence_engine import TransferEngine
    from transfers.policy import TransferPolicy
    from transfers.recovery_repository import TransferRepository
    from transfers.registry import IntegrationRegistry
    from application.service import ApplicationService

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    root = tmp_path / "payloads"
    (root / ".dpwork" / "complete").mkdir(parents=True)
    (root / ".dpwork" / "incomplete").mkdir(parents=True)
    sab = FakeSab(complete_dir=str(root / ".dpwork" / "complete"),
                  download_dir=str(root / ".dpwork" / "incomplete"))
    registry = IntegrationRegistry()
    registry.register_provider(UsenetProvider())
    registry.register_executor(SabnzbdExecutor(
        sab, SabnzbdConfiguration(local_root=str(root),
                                  working_directory=str(root / ".dpwork"),
                                  complete_directory=str(root / ".dpwork" / "complete")),
        repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(root),
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0))
    await engine.initialize()
    service = ApplicationService(engine)

    for payload in (b"not xml at all", b"<nzb></nzb>", b"<nzb><file></file></nzb>"):
        await service.submit_nzb(payload, "bad.nzb")
    for _ in range(8):
        await engine.tick()
        await asyncio.sleep(0)

    assert sab.submissions == [], "a malformed posting must never reach the executor"
