"""Test-only composition for the Browser Runtime INPUT_REQUIRED contract.

Serves the REAL application -- HTTP API, bounded ``/api/torrents`` read model,
engine, repository, durable INPUT_REQUIRED lifecycle, input endpoint and
static UI -- with exactly one substitution: the integration registry holds
the real ``GeneralHttpProvider``, ``GeneralFtpProvider`` and ``ScpProvider``
and one in-memory transport for HTTPS, FTP and SFTP: one locked HTTPS test
host, and one open remote host whose paths the transport classifies (a
``dir`` path is a multi-file directory, anything else one regular file)
through the real core-run discovery. The runtime's destination policy
(rightly) admits no local authentication-protected or private origin, so this
is the one way real backend challenges and remote collections can be raised
deterministically in a browser run.

Never imported by production code and never collected by pytest. The Browser
Runtime workflow runs it in the candidate image:

    uvicorn browser_contract_app:app --app-dir /app/tests
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from application.composition import application  # noqa: E402
from fake_integrations import VaultExecutor, neutral_facts  # noqa: E402
from main import app  # noqa: E402
from providers.general_ftp.provider import GeneralFtpProvider  # noqa: E402
from providers.general_http.provider import GeneralHttpProvider  # noqa: E402
from providers.general_scp.provider import ScpProvider  # noqa: E402
from transfers.canonical import CanonicalOwnership  # noqa: E402
from transfers.errors import Category, Domain, NormalizedError, Stage  # noqa: E402
from transfers.models import (  # noqa: E402
    ArtifactFingerprint, DiscoveredEntry, DiscoveryResult, ExecutionObservation, ExecutionState, ExecutorCapabilities,
    FingerprintKind, InputField, IntegrationDescriptor, RemoteObjectKind, TransferProgress,
)
from transfers.registry import IntegrationRegistry  # noqa: E402

CONTRACT_HOST = "locked.contract.test"
CONTRACT_PATH = "/modal-contract.bin"
USERNAME, PASSWORD = "contract-operator", "contract-password"
REMOTE_HOST = "remote.contract.test"
REMOTE_FILE = "/file.bin"
REMOTE_MEMBERS = ("alpha.bin", "beta.bin", "gamma.bin")
# The evidence-origin scenario: an in-progress canonical copy on an open host,
# and an equivalent on a locked host whose pre-writer sampling needs the login.
# The canonical attach that follows the answered sample (the rest of that
# materialization decision) is held for EVIDENCE_HOLD_SECONDS, so the modal's
# timing can be told apart from the decision's.
EVIDENCE_SEED_HOST = "seed.contract.test"
EVIDENCE_HOST = "evidence.contract.test"
EVIDENCE_PATH = "/evidence.bin"
EVIDENCE_HOLD_SECONDS = 8.0
# Multi-source convergence scenarios (DP 1.0.13), around their own
# in-progress canonical copy (``seed.contract.test/multi.bin``):
# * two locked sources of ONE transfer, each needing its own login; the
#   answered source's canonical attach is held like the evidence one, so the
#   next source's question must be presented while that work is still held;
# * a contributor whose one source's proof only ever times out (a transient,
#   unverified contribution) beside a verified one;
# * a canonical whose selected route's writer is admitted and then disappears
#   with no progress, beside a verified alternate that works.
MULTI_PATH = "/multi.bin"
MULTI_HOSTS = ("multi-a.contract.test", "multi-b.contract.test")
PROOF_GOOD_HOST = "good.contract.test"
PROOF_TIMEOUT_HOST = "slow.contract.test"
VANISH_HOST = "vanish.contract.test"
ALIVE_HOST = "alive.contract.test"
RETRY_PATH = "/retry.bin"


class LockedHttpTransport(VaultExecutor):
    """An HTTPS/FTP/SFTP-claiming in-memory transport; one host requires a login.

    A start without the login fails with the fake's definitive native auth
    diagnostic, so the executor raises the ordinary AUTH_REQUIRED challenge;
    continuing the challenged attempt with the right login completes it.
    Remote discovery reports read-only facts only: a ``dir`` path is a
    directory of ``REMOTE_MEMBERS``, anything else one regular file."""

    descriptor = IntegrationDescriptor("contract-transport", "Contract transport", frozenset())
    capabilities = ExecutorCapabilities(candidate_sampling=True, per_execution_pause=True, transient_input=True,
                                        remote_discovery=True)
    claim_schemes = frozenset({"https", "ftp", "sftp"})

    @staticmethod
    def _object(candidate):
        parts = urlsplit(candidate.endpoints[0].address)
        return f"{parts.hostname}{unquote(parts.path)}"

    async def discover(self, subject, submitted=None):
        path = unquote(urlsplit(subject.candidate.endpoints[0].address).path)
        if path.rstrip("/").endswith("/dir"):
            return DiscoveryResult(tuple(DiscoveredEntry(name, 4) for name in REMOTE_MEMBERS), path)
        return DiscoveryResult(kind=RemoteObjectKind.FILE, expected_bytes=4)

    async def fingerprint(self, subject):
        # The transient scenario's proof never answers in time.
        if urlsplit(subject.candidate.endpoints[0].address).hostname == PROOF_TIMEOUT_HOST:
            return ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "timeout")
        return await super().fingerprint(subject)

    async def start(self, request, handle):
        observed = await super().start(request, handle)
        host = urlsplit(request.work.subject.candidate.endpoints[0].address).hostname
        if observed.error is None and host == VANISH_HOST:
            # Admitted, then gone without any progress: no live job, no record.
            self.jobs.pop(handle.attempt_id, None)
        # The evidence scenario's canonical copy stays in progress.
        elif observed.error is None and host != EVIDENCE_SEED_HOST:
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


class HeldCanonicalOwnership(CanonicalOwnership):
    """The one canonical owner, with the evidence scenario's attach -- work
    unrelated to authentication -- deliberately slow."""

    async def attach(self, primary, record, candidates, size):
        if any(urlsplit(item.endpoints[0].address).hostname in {EVIDENCE_HOST, *MULTI_HOSTS} for item in candidates):
            await asyncio.sleep(EVIDENCE_HOLD_SECONDS)
        return await super().attach(primary, record, candidates, size)


registry = IntegrationRegistry()
for provider in (GeneralHttpProvider(), GeneralFtpProvider(), ScpProvider()):
    registry.register_provider(provider)
registry.register_executor(LockedHttpTransport(
    application.repository.authorize_execution,
    objects={f"{CONTRACT_HOST}{CONTRACT_PATH}": b"four", f"{REMOTE_HOST}{REMOTE_FILE}": b"four",
             f"{EVIDENCE_SEED_HOST}{EVIDENCE_PATH}": b"four", f"{EVIDENCE_HOST}{EVIDENCE_PATH}": b"four",
             f"{EVIDENCE_SEED_HOST}/refused{EVIDENCE_PATH}": b"four", f"{EVIDENCE_HOST}/refused{EVIDENCE_PATH}": b"four",
             f"{EVIDENCE_SEED_HOST}{MULTI_PATH}": b"four",
             f"{PROOF_GOOD_HOST}{MULTI_PATH}": b"four", f"{PROOF_TIMEOUT_HOST}{MULTI_PATH}": b"four",
             f"{VANISH_HOST}{RETRY_PATH}": b"four", f"{ALIVE_HOST}{RETRY_PATH}": b"four"}
    | {f"{REMOTE_HOST}/dir/{name}": b"four" for name in REMOTE_MEMBERS}
    | {f"{host}{MULTI_PATH}": b"four" for host in MULTI_HOSTS},
    locks={CONTRACT_HOST: (USERNAME, PASSWORD), EVIDENCE_HOST: (USERNAME, PASSWORD)}
    | {host: (USERNAME, PASSWORD) for host in MULTI_HOSTS},
))
application.engine.registry = registry
application.engine.canonical = HeldCanonicalOwnership(application.repository)
# Execution retries back off seconds, not a minute, so a bounded failover is
# observable within one browser test (never changed through the settings API:
# a settings change recomposes the real registry).
application.engine.configure_policy(replace(application.engine.policy, retry_delay=3.0))
app.state.application = application
