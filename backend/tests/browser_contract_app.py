"""Test-only composition for the Browser Runtime INPUT_REQUIRED contract.

Serves the REAL application -- HTTP API, bounded ``/api/torrents`` read model,
engine, repository, durable INPUT_REQUIRED lifecycle, input endpoint and
static UI -- with exactly one substitution: the integration registry holds
the real ``GeneralHttpProvider`` and an in-memory transport for one locked
test host. The runtime's destination policy (rightly) admits no local
authentication-protected origin, so this is the one way a real backend
challenge can be raised deterministically in a browser run.

Never imported by production code and never collected by pytest. The Browser
Runtime workflow runs it in the candidate image:

    uvicorn browser_contract_app:app --app-dir /app/tests
"""
from __future__ import annotations

from pathlib import Path
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.composition import application  # noqa: E402
from fake_integrations import VaultExecutor, neutral_facts  # noqa: E402
from main import app  # noqa: E402
from providers.general_http.provider import GeneralHttpProvider  # noqa: E402
from transfers.errors import Category, Domain, NormalizedError, Stage  # noqa: E402
from transfers.models import (  # noqa: E402
    ExecutionObservation, ExecutionState, InputField, IntegrationDescriptor, TransferProgress,
)
from transfers.registry import IntegrationRegistry  # noqa: E402

CONTRACT_HOST = "locked.contract.test"
CONTRACT_PATH = "/modal-contract.bin"
USERNAME, PASSWORD = "contract-operator", "contract-password"


class LockedHttpTransport(VaultExecutor):
    """An HTTPS-claiming in-memory transport whose one host requires a login.

    A start without the login fails with the fake's definitive native auth
    diagnostic, so the executor raises the ordinary AUTH_REQUIRED challenge;
    continuing the challenged attempt with the right login completes it."""

    descriptor = IntegrationDescriptor("contract-transport", "Contract transport", frozenset())
    claim_schemes = frozenset({"https"})

    @staticmethod
    def _object(candidate):
        parts = urlsplit(candidate.endpoints[0].address)
        return f"{parts.hostname}{parts.path}"

    async def start(self, request, handle):
        observed = await super().start(request, handle)
        if observed.error is None:
            self.finish(handle)
        return observed

    async def start_with_input(self, request, handle, submitted):
        host = urlsplit(request.work.subject.candidate.endpoints[0].address).hostname
        accepted = (submitted.value(InputField.USERNAME), submitted.value(InputField.PASSWORD)) == self.locks.get(host)
        error = None if accepted else NormalizedError(Domain.EXECUTOR, Category.UNMAPPED_EXECUTOR_ERROR,
                                                      Stage.EXECUTION, native_code="vault-auth")
        observed = neutral_facts(ExecutionObservation(
            handle, ExecutionState.RUNNING if accepted else ExecutionState.FAILED, TransferProgress(4, 1, 1), error))
        self.jobs[handle.attempt_id] = observed
        if accepted:
            self.finish(handle)
        return observed


registry = IntegrationRegistry()
registry.register_provider(GeneralHttpProvider())
registry.register_executor(LockedHttpTransport(
    application.repository.authorize_execution,
    objects={f"{CONTRACT_HOST}{CONTRACT_PATH}": b"four"},
    locks={CONTRACT_HOST: (USERNAME, PASSWORD)},
))
application.engine.registry = registry
app.state.application = application
