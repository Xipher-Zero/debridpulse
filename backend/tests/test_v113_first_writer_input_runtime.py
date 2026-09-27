"""DP 1.0.13 real-runtime proof: discovery-validated input reaches the first writer.

Transfer 417 against a real SSH/SFTP origin and a real ``aria2c``: the server
identity and login the operator confirmed during core-run discovery start the
FIRST execution attempt. There is no preceding attempt that meets aria2's
fail-closed sentinel host key ("Unexpected SSH host key: expected 000...").
The same holds for a selected SFTP or SCP directory child and for the members
of a locked FTP directory.
"""
from __future__ import annotations

import time

import pytest

import db.database as database
from providers.general_scp.provider import ScpProvider
from test_v113_egress_guard_route_scope import FtpOrigin
from test_v113_ftp_sftp_convergence_runtime import PASSWORD, PAYLOAD, USER, _runtime
from test_v113_scp_provider_runtime import _durable_text
from test_v113_transport_evidence_sampling import SftpOrigin
from transfers.models import TransferRequest, TransferState

pytestmark = pytest.mark.asyncio

MEMBERS = {"one.iso": PAYLOAD, "two.iso": PAYLOAD[::-1]}


def _root(tmp_path):
    root = tmp_path / "sftp-root"
    (root / "pub").mkdir(parents=True)
    (root / "pub" / "big.iso").write_bytes(PAYLOAD)
    for name, body in MEMBERS.items():
        (root / "pub" / name).write_bytes(body)
    return root


async def _attempts(transfer_id):
    async with database.get_db() as db:
        return await db.fetchall("SELECT state,error FROM execution_attempts WHERE transfer_id=?", (transfer_id,))


async def _trace_kinds(transfer_id):
    async with database.get_db() as db:
        return [row["kind"] for row in await db.fetchall(
            "SELECT kind FROM application_events WHERE transfer_id=? ORDER BY id", (transfer_id,))]


async def _drive(runtime, transfer_id, until, *, select=None):
    asked = []

    async def step():
        current = await runtime.engine.challenges.current(transfer_id)
        if current is not None and current.id not in {item.id for item in asked}:
            asked.append(current)
            await runtime.engine.submit_input(transfer_id, current.id, "username_password",
                                              {"username": USER, "password": PASSWORD})
        if select is not None:
            view = await runtime.repository.file_selection_presentation(transfer_id, now=time.time())
            if view and view.get("decision") == "pending":
                chosen = [entry["entry_id"] for entry in view["entries"] if entry["name"] == select]
                await runtime.repository.confirm_file_selection(transfer_id, view["manifest_id"], chosen,
                                                                now=time.time())
        return await until()

    await runtime.until(step, label="first-writer transfer")
    return asked


def _completed(runtime, transfer_id):
    async def check():
        return (await runtime.repository.get(transfer_id)).state == TransferState.COMPLETED
    return check


def _no_sentinel(attempts):
    return all("0000000000000000000000000000000000000000" not in str(row["error"] or "") for row in attempts)


async def test_transfer_417_an_exact_sftp_file_needs_exactly_one_execution_attempt(tmp_path, monkeypatch):
    origin = await SftpOrigin(_root(tmp_path), credentials=(USER, PASSWORD)).start()
    runtime = await _runtime(tmp_path, monkeypatch, origins=(origin,))
    try:
        transfer = await runtime.engine.submit((TransferRequest(
            "sftp", origin.url("/pub/big.iso", host="sftp-417.test")),), deduplicate=False)
        asked = await _drive(runtime, transfer.id, _completed(runtime, transfer.id))
        assert [(item.origin.value, item.reason.value) for item in asked] == [("provider", "server_identity_required")]
        attempts = await _attempts(transfer.id)
        assert len(attempts) == 1 and attempts[0]["error"] is None and _no_sentinel(attempts)
        kinds = await _trace_kinds(transfer.id)
        for kind in ("server_identity_confirmed", "auth_accepted", "discovery_completed", "queued"):
            assert kind in kinds, kind
        assert kinds.index("discovery_completed") < kinds.index("queued")
        [artifact] = await runtime.repository.artifacts(transfer.id)
        assert open(artifact.target, "rb").read() == PAYLOAD
        assert PASSWORD not in await _durable_text()
    finally:
        await runtime.close()


@pytest.mark.parametrize("scheme", ["sftp", "scp"])
async def test_a_selected_directory_child_needs_exactly_one_execution_attempt(tmp_path, monkeypatch, scheme):
    origin = await SftpOrigin(_root(tmp_path), credentials=(USER, PASSWORD)).start()
    runtime = await _runtime(tmp_path, monkeypatch, origins=(origin,))
    runtime.registry.register_provider(ScpProvider())
    try:
        url = f"{scheme}://sftp-dir.test:{origin.port}/pub/"
        transfer = await runtime.engine.submit((TransferRequest(scheme, url, selection_mode="interactive"),),
                                               deduplicate=False)
        asked = await _drive(runtime, transfer.id, _completed(runtime, transfer.id), select="two.iso")
        assert len(asked) == 1  # the discovery question only; the selected child asks nothing
        attempts = await _attempts(transfer.id)
        assert len(attempts) == 1 and attempts[0]["error"] is None and _no_sentinel(attempts)
        [artifact] = await runtime.repository.artifacts(transfer.id)
        assert artifact.name == "two.iso" and open(artifact.target, "rb").read() == MEMBERS["two.iso"]
        [child] = [record for record in await runtime.repository.requests(transfer.id) if record.parent_id]
        assert child.request.kind == scheme  # provider provenance is the operator's own
    finally:
        await runtime.close()


async def test_a_locked_ftp_directory_needs_one_execution_attempt_per_member(tmp_path, monkeypatch):
    files = {f"/pub/{name}": body for name, body in MEMBERS.items()}
    origin = await FtpOrigin(files, users={USER: PASSWORD}, anonymous=False).start()
    runtime = await _runtime(tmp_path, monkeypatch, origins=(origin,))
    try:
        transfer = await runtime.engine.submit((TransferRequest(
            "ftp", f"ftp://locked-ftp.test:{origin.port}/pub/"),), deduplicate=False)
        asked = await _drive(runtime, transfer.id, _completed(runtime, transfer.id))
        assert [(item.origin.value, item.reason.value) for item in asked] == [("provider", "auth_required")]
        attempts = await _attempts(transfer.id)
        assert len(attempts) == len(MEMBERS) and all(row["error"] is None for row in attempts)
        assert PASSWORD not in await _durable_text()
    finally:
        await runtime.close()
