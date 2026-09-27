"""DP 1.0.13 real-runtime proof: (S)FTP directories through core-run discovery.

Real owners end to end -- the convergence ``TransferEngine``, the real
repository and selection owners, the real ``GeneralFtpProvider``, the real
``Aria2Executor`` with a real ``aria2c`` daemon and the real
``DownloaderEgressGuard`` -- against deterministic in-process FTP and SSH/SFTP
origins. The provider only interprets the path; the executor classifies it over
its own transport, trust and login owners; a multi-file directory is held for
the one neutral selection decision before any member request or writer exists.
"""
from __future__ import annotations

import time

import pytest

from test_v113_egress_guard_route_scope import FtpOrigin
from test_v113_ftp_sftp_convergence_runtime import PASSWORD, PAYLOAD, USER, _runtime
from test_v113_scp_provider_runtime import _durable_text
from test_v113_transport_evidence_sampling import SftpOrigin
from transfers.models import TransferRequest

pytestmark = pytest.mark.asyncio

MEMBERS = {"one.iso": PAYLOAD, "two.iso": PAYLOAD[::-1], "three.iso": PAYLOAD[:4096]}
FTP_FILES = {f"/pub/iso/{name}": body for name, body in MEMBERS.items()} | {"/pub/iso/nested/deep.iso": b"deep"}


def _sftp_root(tmp_path):
    root = tmp_path / "sftp-root"
    (root / "pub" / "iso" / "nested").mkdir(parents=True)
    for name, body in MEMBERS.items():
        (root / "pub" / "iso" / name).write_bytes(body)
    (root / "pub" / "iso" / "nested" / "deep.iso").write_bytes(b"never listed")
    (root / "pub" / "iso" / "link.iso").symlink_to(root / "pub" / "iso" / "one.iso")
    return root


async def _held_for_selection(runtime, transfer_id, answers=()):
    """Answer each asked question once (in order), until the selector is pending."""
    queue, asked = list(answers), []

    async def step():
        current = await runtime.engine.challenges.current(transfer_id)
        if current is not None and current.id not in {item.id for item in asked}:
            asked.append(current)
            if queue:
                await runtime.engine.submit_input(transfer_id, current.id, *queue.pop(0))
        view = await runtime.repository.file_selection_presentation(transfer_id, now=time.time())
        return view if view and view.get("decision") == "pending" else None

    view = await runtime.until(step, label="selection pending")
    return view, asked


async def _no_member_exists(runtime, transfer_id):
    records = await runtime.repository.requests(transfer_id)
    assert [item for item in records if item.parent_id] == []
    assert await runtime.repository.artifacts(transfer_id) == ()


async def _completed(runtime, transfer_id, count):
    artifacts = await runtime.repository.artifacts(transfer_id)
    if len(artifacts) == count and all(item.state == "completed" for item in artifacts):
        return {item.name: open(item.target, "rb").read() for item in artifacts}
    return None


async def test_an_anonymous_ftp_directory_is_listed_held_and_only_the_confirmed_subset_downloads(tmp_path, monkeypatch):
    origin = await FtpOrigin(dict(FTP_FILES)).start()
    runtime = await _runtime(tmp_path, monkeypatch, origins=(origin,))
    try:
        # No trailing slash: the server, not the spelling, proves it a directory.
        transfer = await runtime.engine.submit((TransferRequest(
            "ftp", f"ftp://open-ftp.test:{origin.port}/pub/iso", selection_mode="interactive"),), deduplicate=False)
        view, asked = await _held_for_selection(runtime, transfer.id)
        assert asked == []  # anonymous: nothing to ask
        assert sorted(entry["name"] for entry in view["entries"]) == sorted(MEMBERS)
        await _no_member_exists(runtime, transfer.id)

        chosen = [entry["entry_id"] for entry in view["entries"] if entry["name"] in {"one.iso", "three.iso"}]
        await runtime.repository.confirm_file_selection(transfer.id, view["manifest_id"], chosen, now=time.time())
        members = await runtime.until(lambda: _completed(runtime, transfer.id, 2), label="subset completes")
        assert members == {"one.iso": MEMBERS["one.iso"], "three.iso": MEMBERS["three.iso"]}
        payloads = sorted(item.request.payload for item in await runtime.repository.requests(transfer.id)
                          if item.parent_id)
        assert payloads == [f"ftp://open-ftp.test:{origin.port}/pub/iso/{name}" for name in ("one.iso", "three.iso")]
    finally:
        await runtime.close()


async def test_a_locked_ftp_directory_asks_the_generalized_login_once_and_headless_takes_every_member(
        tmp_path, monkeypatch):
    origin = await FtpOrigin(dict(FTP_FILES), users={USER: PASSWORD}, anonymous=False).start()
    runtime = await _runtime(tmp_path, monkeypatch, origins=(origin,))
    try:
        transfer = await runtime.engine.submit((TransferRequest(
            "ftp", f"ftp://locked-ftp.test:{origin.port}/pub/iso/"),), deduplicate=False)
        asked = []

        async def step():
            current = await runtime.engine.challenges.current(transfer.id)
            if current is not None and current.id not in {item.id for item in asked}:
                asked.append(current)
                await runtime.engine.submit_input(transfer.id, current.id, "username_password",
                                                  {"username": USER, "password": PASSWORD})
            return await _completed(runtime, transfer.id, len(MEMBERS))

        members = await runtime.until(step, label="every member completes")
        assert [(item.origin.value, item.reason.value) for item in asked] == [("provider", "auth_required")]
        assert members == MEMBERS
        assert PASSWORD not in await _durable_text()
    finally:
        await runtime.close()


async def test_an_sftp_directory_confirms_identity_then_closing_the_selector_keeps_every_member(tmp_path, monkeypatch):
    origin = await SftpOrigin(_sftp_root(tmp_path), credentials=(USER, PASSWORD)).start()
    runtime = await _runtime(tmp_path, monkeypatch, origins=(origin,))
    try:
        transfer = await runtime.engine.submit((TransferRequest(
            "sftp", origin.url("/pub/iso", host="sftp-dir.test"), selection_mode="interactive"),), deduplicate=False)
        view, asked = await _held_for_selection(runtime, transfer.id, [
            ("username_password", {"username": USER, "password": PASSWORD})])
        # The existing fail-closed identity owner asks first, strictly before any credential.
        assert [(item.origin.value, item.reason.value) for item in asked] == [
            ("provider", "server_identity_required")]
        # Links are never followed and a subdirectory is never entered.
        assert sorted(entry["name"] for entry in view["entries"]) == sorted(MEMBERS)
        await _no_member_exists(runtime, transfer.id)

        await runtime.repository.dismiss_file_selection(transfer.id, view["manifest_id"], now=time.time())
        members = await runtime.until(lambda: _completed(runtime, transfer.id, len(MEMBERS)), label="all complete")
        assert members == MEMBERS
        assert PASSWORD not in await _durable_text()
    finally:
        await runtime.close()
