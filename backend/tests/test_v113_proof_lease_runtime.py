"""DP 1.0.13 real-runtime proof: six authenticated routes to one SFTP file.

Six separately admitted transfers -- sftp/ssh/scp, each as the exact file and
as a directory with that one file selected -- reach one real SSH/SFTP server
through the real engine and a real, bandwidth-bounded ``aria2c``; as in the
reported case, the first route is already downloading when the other five
arrive. Each lineage
answers its own identity/login question. The canonical member needs
authentication, so every later route is proven against it with a proof-only
lease of the canonical lineage's already-validated login: all five
contributors become VERIFIED members of the one canonical artifact (never
``unverified``), one writer materializes the file, and one Transfer Trace of
the canonical transfer carries the whole consolidation component.
"""
from __future__ import annotations

import json
import time

import pytest

import db.database as database
from providers.general_scp.provider import ScpProvider
from services import transfer_trace
from test_v113_ftp_sftp_convergence_runtime import PASSWORD, PAYLOAD, USER, _aria2_jobs, _runtime
from test_v113_transport_evidence_sampling import SftpOrigin
from transfers.models import TransferRequest, TransferState

pytestmark = pytest.mark.asyncio


def _root(tmp_path):
    root = tmp_path / "sftp-root"
    (root / "myth" / "ISOs").mkdir(parents=True)
    (root / "myth" / "ISOs" / "debian.qcow2").write_bytes(PAYLOAD)
    (root / "myth" / "ISOs" / "other.qcow2").write_bytes(PAYLOAD[::-1])
    return root


async def test_six_authenticated_routes_become_verified_members_of_one_canonical_file(tmp_path, monkeypatch):
    origin = await SftpOrigin(_root(tmp_path), credentials=(USER, PASSWORD)).start()
    # Bandwidth-bounded, so the canonical writer is still live while every
    # other route proves itself against it.
    runtime = await _runtime(tmp_path, monkeypatch, origins=(origin,), limit="24K")
    runtime.registry.register_provider(ScpProvider())
    try:
        base = f"mikrobob.test:{origin.port}/myth/ISOs/"
        urls = [f"{scheme}://{base}{leaf}" for scheme in ("sftp", "ssh", "scp")
                for leaf in ("debian.qcow2", "")]

        async def submit(url):
            return (await runtime.engine.submit((TransferRequest(
                url.split(":", 1)[0], url, selection_mode="interactive" if url.endswith("/") else "all"),),
                deduplicate=False)).id

        # As in the reported case, the first route is already downloading when the others arrive.
        ids = [await submit(urls[0])]
        answered = set()

        async def step():
            for transfer_id in ids:
                current = await runtime.engine.challenges.current(transfer_id)
                if current is not None and current.id not in answered:
                    answered.add(current.id)
                    await runtime.engine.submit_input(transfer_id, current.id, "username_password",
                                                      {"username": USER, "password": PASSWORD})
                view = await runtime.repository.file_selection_presentation(transfer_id, now=time.time())
                if view and view.get("decision") == "pending":
                    chosen = [entry["entry_id"] for entry in view["entries"] if entry["name"] == "debian.qcow2"]
                    await runtime.repository.confirm_file_selection(transfer_id, view["manifest_id"], chosen,
                                                                    now=time.time())
            states = [(await runtime.repository.get(transfer_id)).state for transfer_id in ids]
            return all(state in {TransferState.COMPLETED, TransferState.CONSOLIDATED} for state in states)

        async def writing():
            await step()
            artifacts = await runtime.repository.artifacts(ids[0])
            return bool(artifacts) and artifacts[0].state == "downloading"

        await runtime.until(writing, label="first route writing")
        ids += [await submit(url) for url in urls[1:]]
        await runtime.until(step, label="six routes complete")

        async with database.get_db() as db:
            marks = ",".join("?" for _ in ids)
            material = await db.fetchall(
                f"SELECT * FROM download_files WHERE torrent_id IN ({marks}) "  # nosec B608
                "AND COALESCE(mirror_state,'')!='standby'", tuple(ids))
            leaves = [row for row in await db.fetchall(
                f"SELECT * FROM transfer_requests WHERE transfer_id IN ({marks})", tuple(ids))  # nosec B608
                if json.loads(row["payload"])["payload"].endswith("/debian.qcow2")]
        [canonical] = material
        assert len(await _aria2_jobs(runtime)) == 1  # one physical writer
        assert open(canonical["local_path"], "rb").read() == PAYLOAD
        assert len(leaves) == 6
        contributors = [row for row in leaves if row["transfer_id"] != canonical["torrent_id"]]
        assert len(contributors) == 5
        # Verified membership from actual material -- none left unverified.
        assert {(row["equivalence_disposition"], row["equivalence_reason"]) for row in contributors} == {
            ("recovered", "full_content_sample")}
        origins = {str(origin["request_id"]) for binding in await runtime.engine.canonical.bindings(canonical["id"])
                   for origin in binding["origins"]}
        assert {row["id"] for row in leaves} <= origins
        assert {binding["provider_id"] for binding in await runtime.engine.canonical.bindings(canonical["id"])} == {
            "general_ftp", "general_scp"}

        # One trace of the canonical transfer answers the whole consolidation story.
        trace = await transfer_trace.build(canonical["torrent_id"], None)
        component = trace["metadata"]["closure"]["component"]
        assert component["truncated"] is False and component["transfer_ids"] == sorted(ids)
        exported = {item["row"]["id"]: item["row"] for item in trace["data"]["transfer_requests"]}
        for row in contributors:
            assert exported[row["id"]]["equivalence_disposition"] == "recovered"
        kinds = {item["row"]["kind"] for item in trace["data"]["application_events"]}
        assert "proof_lease_used" in kinds
        text = json.dumps(trace)
        assert PASSWORD not in text and USER not in text
    finally:
        await runtime.close()


async def test_six_routes_admitted_at_once_still_have_one_writer(tmp_path, monkeypatch):
    """All six routes submitted in the same moment: the exact SSH and SCP
    routes resolve before any lineage holds validated input, so nothing can be
    proven yet -- the later one never seeds a second writer beside the still
    deciding first one; every route ends associated with one canonical file."""
    origin = await SftpOrigin(_root(tmp_path), credentials=(USER, PASSWORD)).start()
    runtime = await _runtime(tmp_path, monkeypatch, origins=(origin,), limit="24K")
    runtime.registry.register_provider(ScpProvider())
    try:
        base = f"mikrobob.test:{origin.port}/myth/ISOs/"
        urls = [f"{scheme}://{base}{leaf}" for scheme in ("sftp", "ssh", "scp") for leaf in ("debian.qcow2", "")]
        ids = [(await runtime.engine.submit((TransferRequest(
            url.split(":", 1)[0], url, selection_mode="interactive" if url.endswith("/") else "all"),),
            deduplicate=False)).id for url in urls]
        answered = set()

        async def step():
            for transfer_id in ids:
                current = await runtime.engine.challenges.current(transfer_id)
                if current is not None and current.id not in answered:
                    answered.add(current.id)
                    await runtime.engine.submit_input(transfer_id, current.id, "username_password",
                                                      {"username": USER, "password": PASSWORD})
                view = await runtime.repository.file_selection_presentation(transfer_id, now=time.time())
                if view and view.get("decision") == "pending":
                    chosen = [entry["entry_id"] for entry in view["entries"] if entry["name"] == "debian.qcow2"]
                    await runtime.repository.confirm_file_selection(transfer_id, view["manifest_id"], chosen,
                                                                    now=time.time())
            states = [(await runtime.repository.get(transfer_id)).state for transfer_id in ids]
            return all(state in {TransferState.COMPLETED, TransferState.CONSOLIDATED} for state in states)

        await runtime.until(step, label="six simultaneous routes complete")
        async with database.get_db() as db:
            marks = ",".join("?" for _ in ids)
            material = await db.fetchall(
                f"SELECT * FROM download_files WHERE torrent_id IN ({marks}) "  # nosec B608
                "AND COALESCE(mirror_state,'')!='standby'", tuple(ids))
            leaves = [row for row in await db.fetchall(
                f"SELECT * FROM transfer_requests WHERE transfer_id IN ({marks})", tuple(ids))  # nosec B608
                if json.loads(row["payload"])["payload"].endswith("/debian.qcow2")]
        [canonical] = material
        assert len(await _aria2_jobs(runtime)) == 1  # one physical writer
        assert open(canonical["local_path"], "rb").read() == PAYLOAD
        contributors = [row for row in leaves if row["transfer_id"] != canonical["torrent_id"]]
        assert len(contributors) == 5
        assert {row["equivalence_disposition"] for row in contributors} <= {"recovered", "unverified"}
    finally:
        await runtime.close()
