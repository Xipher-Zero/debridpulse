"""DP 1.0.13 real-runtime proof: SCP/SSH exact files through the existing SFTP path.

Real owners end to end -- the convergence ``TransferEngine``, the real
repository/canonical owners, the real ``ScpProvider`` beside the real
``GeneralFtpProvider``, the real ``Aria2Executor`` with a real ``aria2c`` daemon
and the real ``DownloaderEgressGuard`` -- against an in-process SSH/SFTP origin.
The SCP provider only interprets the request; the existing SFTP server-identity
challenge, credential continuation, evidence acquisition and equivalence decide
everything else, and the submitted request stays the operator's own SCP/SSH URL.
"""
from __future__ import annotations

import pytest

import db.database as database
from providers.general_scp.provider import ScpProvider
from test_v113_ftp_sftp_convergence_runtime import PASSWORD, PAYLOAD, USER, _aria2_uris, _completed_bytes, _runtime
from test_v113_transport_evidence_sampling import SftpOrigin
from transfers.models import TransferRequest

pytestmark = pytest.mark.asyncio


def _origin_root(tmp_path, name):
    root = tmp_path / name
    (root / "pub").mkdir(parents=True)
    (root / "pub" / "big.iso").write_bytes(PAYLOAD)
    return root


async def _scp_runtime(tmp_path, monkeypatch, origins):
    runtime = await _runtime(tmp_path, monkeypatch, origins=origins)
    runtime.registry.register_provider(ScpProvider())
    return runtime


async def _answer_until(runtime, transfer_id, predicate, *, label, reasons):
    """Answer each challenge the existing lifecycle raises, exactly once, with the
    same neutral submission the one modal sends."""
    answered = set()

    async def step():
        current = await runtime.engine.challenges.current(transfer_id)
        if current is not None and current.id not in answered:
            answered.add(current.id)
            reasons.append((current.origin.value, current.reason.value))
            await runtime.engine.submit_input(transfer_id, current.id, "username_password",
                                              {"username": USER, "password": PASSWORD})
        return await predicate()

    return await runtime.until(step, label=label)


async def _durable_text():
    async with database.get_db() as db:
        tables = [row["name"] for row in await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        chunks = []
        for table in tables:
            for row in await db.fetchall(f'SELECT * FROM "{table}"'):  # nosec B608 - fixed schema names
                chunks.append(repr(dict(row)))
    return "\n".join(chunks)


@pytest.mark.parametrize("scheme", ["scp", "ssh"])
async def test_exact_scp_or_ssh_file_downloads_through_the_existing_sftp_identity_path(tmp_path, monkeypatch, scheme):
    origin = await SftpOrigin(_origin_root(tmp_path, "sftp"), credentials=(USER, PASSWORD)).start()
    runtime = await _scp_runtime(tmp_path, monkeypatch, (origin,))
    try:
        submitted = f"{scheme}://scp-origin.test:{origin.port}/pub/big.iso"
        transfer = await runtime.engine.submit((TransferRequest(scheme, submitted, name="big.iso"),),
                                               name="big.iso", deduplicate=False)
        first = await runtime.until(lambda: runtime.engine.challenges.current(transfer.id), label="challenge")
        # The existing fail-closed SFTP owner asks first: identity, then credentials.
        assert first.reason.value == "server_identity_required"
        facts = {fact.name.value: fact.value for fact in first.facts}
        assert facts["server_host"] == "scp-origin.test"
        assert facts["server_identity_algorithm"] == "sha-1"
        assert origin.auth_attempts == []

        reasons = []
        body = await _answer_until(runtime, transfer.id, lambda: _completed_bytes(runtime, transfer.id),
                                   label="SCP exact file completes", reasons=reasons)
        assert body == PAYLOAD
        assert reasons == [(first.origin.value, "server_identity_required")]  # asked once, never twice

        requests = await runtime.repository.requests(transfer.id)
        assert [item.request.payload for item in requests] == [submitted]  # provenance is the operator's URL
        assert await runtime.repository.bound_route_provider(requests[0].id) == "general_scp"
        uris = await _aria2_uris(runtime)
        assert uris and all(uri == f"sftp://scp-origin.test:{origin.port}/pub/big.iso" for uri in uris)
        assert PASSWORD not in await _durable_text()
    finally:
        await runtime.close()


async def test_scp_and_sftp_mirrors_of_the_same_bytes_converge_under_existing_equivalence(tmp_path, monkeypatch):
    scp_origin = await SftpOrigin(_origin_root(tmp_path, "scp"), credentials=(USER, PASSWORD)).start()
    sftp_origin = await SftpOrigin(_origin_root(tmp_path, "sftp"), credentials=(USER, PASSWORD)).start()
    runtime = await _scp_runtime(tmp_path, monkeypatch, (scp_origin, sftp_origin))
    try:
        transfer = await runtime.engine.submit((
            TransferRequest("scp", f"scp://scp-mirror.test:{scp_origin.port}/pub/big.iso"),
            TransferRequest("sftp", sftp_origin.url("/pub/big.iso", host="sftp-mirror.test")),
        ), name="big.iso", deduplicate=False)

        async def converged():
            artifacts = await runtime.repository.artifacts(transfer.id)
            if len(artifacts) != 1:
                return None
            return artifacts[0] if len(await runtime.engine.canonical.bindings(artifacts[0].id)) == 2 else None

        reasons = []
        await _answer_until(runtime, transfer.id, converged, label="SCP/SFTP convergence", reasons=reasons)
        # Both mirrors were proven by the existing pre-writer evidence owner.
        assert reasons and all(item == ("evidence", "server_identity_required") for item in reasons)
        body = await _answer_until(runtime, transfer.id, lambda: _completed_bytes(runtime, transfer.id),
                                   label="completion", reasons=reasons)
        assert body == PAYLOAD
        providers = {await runtime.repository.bound_route_provider(item.id)
                     for item in await runtime.repository.requests(transfer.id)}
        assert providers == {"general_scp", "general_ftp"}
        assert PASSWORD not in await _durable_text()
    finally:
        await runtime.close()
