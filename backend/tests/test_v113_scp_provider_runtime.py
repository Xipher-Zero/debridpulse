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

import asyncio

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


class WriterGatedOrigin(SftpOrigin):
    """An SFTP origin that serves content reads from the in-process evidence
    sampler (an AsyncSSH client) at once, but holds a real aria2 writer's reads
    (libssh2) until the test opens ``writers``. The writer's lifetime is then
    the test's to decide, never a race against download speed."""

    def __init__(self, root, credentials=(USER, PASSWORD)):
        super().__init__(root, credentials)
        self.writers = asyncio.Event()

    async def start(self, algorithms=("ssh-rsa", "ssh-ed25519", "ecdsa-sha2-nistp256")):
        import asyncssh
        from test_v113_transport_evidence_sampling import _SshServer
        origin = self

        class Files(asyncssh.SFTPServer):
            def __init__(self, chan):
                super().__init__(chan, chroot=str(origin.root))

            async def read(self, file_obj, offset, size):
                if "libssh2" in str(self.connection.get_extra_info("client_version", "")):
                    await origin.writers.wait()
                origin.read_bytes.append(size)
                return super().read(file_obj, offset, size)

        self.server = await asyncssh.listen(
            "127.0.0.1", 0, server_host_keys=[self.keys[alg] for alg in algorithms],
            server_factory=lambda: _SshServer(origin), sftp_factory=Files, allow_scp=False)
        self.port = self.server.sockets[0].getsockname()[1]
        return self


async def test_scp_and_sftp_mirrors_of_the_same_bytes_converge_under_existing_equivalence(tmp_path, monkeypatch):
    # The SCP writer is held live until the SFTP mirror (classified by core-run
    # discovery, so it resolves only after its own first-contact question) has
    # been proven equivalent to it. A mirror that resolves only after its
    # sibling's writer COMPLETED is the separately deferred same-transfer
    # completed-equivalence finding, not what this proves.
    scp_origin = await WriterGatedOrigin(_origin_root(tmp_path, "scp"), credentials=(USER, PASSWORD)).start()
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
        # One first-contact identity question per mirror: the SCP file's from pre-writer
        # evidence, the SFTP path's from the core-run discovery that classifies it.
        assert sorted(reasons) == [("evidence", "server_identity_required"), ("provider", "server_identity_required")]
        scp_origin.writers.set()
        body = await _answer_until(runtime, transfer.id, lambda: _completed_bytes(runtime, transfer.id),
                                   label="completion", reasons=reasons)
        assert body == PAYLOAD
        providers = {await runtime.repository.bound_route_provider(item.id)
                     for item in await runtime.repository.requests(transfer.id)}
        assert providers == {"general_scp", "general_ftp"}
        assert PASSWORD not in await _durable_text()
    finally:
        scp_origin.writers.set()
        await runtime.close()


# ── Generalized authentication + trusted discovery, against a real SSH server ──

WRONG_PASSWORD = "runtime-wrong-password-sentinel"
FILES = {"one.iso": PAYLOAD, "two.iso": PAYLOAD[::-1], "notes.txt": b"not selected"}


def _directory_root(tmp_path, name="dir"):
    root = tmp_path / name
    (root / "pub" / "nested").mkdir(parents=True)
    for file_name, body in FILES.items():
        (root / "pub" / file_name).write_bytes(body)
    (root / "pub" / "nested" / "deep.iso").write_bytes(b"never listed")  # a subdirectory is never entered
    (root / "pub" / "link.iso").symlink_to(root / "pub" / "one.iso")      # a link is never followed
    return root


async def _drive_answers(runtime, transfer_id, predicate, answers, seen):
    queue = list(answers)

    async def step():
        current = await runtime.engine.challenges.current(transfer_id)
        if current is not None and current.id not in {item.id for item in seen}:
            seen.append(current)
            if queue:
                method, password = queue.pop(0)
                values = {} if method == "server_identity" else {"username": USER, "password": password}
                await runtime.engine.submit_input(transfer_id, current.id, method, values)
        return await predicate()

    return await runtime.until(step, label="directory transfer")


async def _completed_members(runtime, transfer_id, count):
    artifacts = await runtime.repository.artifacts(transfer_id)
    if len(artifacts) == count and all(item.state == "completed" for item in artifacts):
        return {item.name: open(item.target, "rb").read() for item in artifacts}
    return None


async def test_embedded_credentials_list_a_directory_behind_one_identity_confirmation(tmp_path, monkeypatch):
    origin = await SftpOrigin(_directory_root(tmp_path), credentials=(USER, PASSWORD)).start()
    runtime = await _scp_runtime(tmp_path, monkeypatch, (origin,))
    try:
        transfer = await runtime.engine.submit((TransferRequest(
            "ssh", f"ssh://{USER}:{PASSWORD}@scp-origin.test:{origin.port}/pub/"),), deduplicate=False)
        seen = []
        members = await _drive_answers(runtime, transfer.id, lambda: _completed_members(runtime, transfer.id, 3),
                                       [("server_identity", None)], seen)
        # Credentials came with the link: only the server identity was asked, once, for everything.
        assert [(item.reason.value, [m.method.value for m in item.methods]) for item in seen] == [
            ("server_identity_required", ["server_identity"])]
        assert members == {name: body for name, body in FILES.items()}
        # One listing, the sibling cohort's one bootstrap self-evidence proof (core
        # policy, made with the same lineage material), then one login per writer.
        assert origin.auth_attempts.count(USER) == 1 + 1 + 3
        uris = await _aria2_uris(runtime)
        assert uris and all(uri.startswith(f"sftp://scp-origin.test:{origin.port}/pub/") for uri in uris)
        assert PASSWORD not in await _durable_text()
    finally:
        await runtime.close()


async def test_a_final_component_pattern_downloads_only_its_matches_after_one_answer(tmp_path, monkeypatch):
    origin = await SftpOrigin(_directory_root(tmp_path), credentials=(USER, PASSWORD)).start()
    runtime = await _scp_runtime(tmp_path, monkeypatch, (origin,))
    try:
        transfer = await runtime.engine.submit((TransferRequest(
            "scp", f"scp://scp-origin.test:{origin.port}/pub/*.iso"),), deduplicate=False)
        seen = []
        members = await _drive_answers(runtime, transfer.id, lambda: _completed_members(runtime, transfer.id, 2),
                                       [("username_password", PASSWORD)], seen)
        assert len(seen) == 1 and seen[0].reason.value == "server_identity_required"
        assert members == {"one.iso": FILES["one.iso"], "two.iso": FILES["two.iso"]}
        assert PASSWORD not in await _durable_text()
    finally:
        await runtime.close()


async def test_a_bad_embedded_password_is_tried_once_then_asked_once(tmp_path, monkeypatch):
    origin = await SftpOrigin(_directory_root(tmp_path), credentials=(USER, PASSWORD)).start()
    runtime = await _scp_runtime(tmp_path, monkeypatch, (origin,))
    try:
        transfer = await runtime.engine.submit((TransferRequest(
            "scp", f"scp://{USER}:{WRONG_PASSWORD}@scp-origin.test:{origin.port}/pub/"),), deduplicate=False)
        seen = []
        members = await _drive_answers(runtime, transfer.id, lambda: _completed_members(runtime, transfer.id, 3),
                                       [("server_identity", None), ("username_password", PASSWORD)], seen)
        assert [[m.method.value for m in item.methods] for item in seen] == [["server_identity"],
                                                                              ["username_password"]]
        assert members == {name: body for name, body in FILES.items()}
        # The bad password once and never again; then the listing, the cohort's
        # bootstrap self-evidence proof and one login per member writer.
        assert origin.auth_attempts.count(USER) == 1 + 1 + 1 + 3
        text = await _durable_text()
        assert WRONG_PASSWORD not in text and PASSWORD not in text
    finally:
        await runtime.close()


async def test_keyboard_interactive_only_server_is_unsupported_not_a_credential_prompt(tmp_path, monkeypatch):
    import asyncssh
    from test_v113_transport_evidence_sampling import _SshServer

    class KeyboardInteractiveOnly(_SshServer):
        def password_auth_supported(self):
            return False

        def kbdint_auth_supported(self):
            return True

        def get_kbdint_challenge(self, username, lang, submethods):
            return "", "", "", [("Password: ", False)]

    origin = SftpOrigin(_directory_root(tmp_path), credentials=(USER, PASSWORD))

    class Files(asyncssh.SFTPServer):
        def __init__(self, chan):
            super().__init__(chan, chroot=str(origin.root))

    origin.server = await asyncssh.listen("127.0.0.1", 0, server_host_keys=list(origin.keys.values()),
                                          server_factory=lambda: KeyboardInteractiveOnly(origin),
                                          sftp_factory=Files, allow_scp=False)
    origin.port = origin.server.sockets[0].getsockname()[1]
    runtime = await _scp_runtime(tmp_path, monkeypatch, (origin,))
    try:
        transfer = await runtime.engine.submit((TransferRequest(
            "ssh", f"ssh://{USER}:{PASSWORD}@scp-origin.test:{origin.port}/pub/"),), deduplicate=False)
        seen = []

        async def failed():
            records = await runtime.repository.requests(transfer.id)
            return records[0] if records[0].error is not None else None

        record = await _drive_answers(runtime, transfer.id, failed, [("server_identity", None)], seen)
        assert record.error.category.value == "unsupported_capability"
        assert [[m.method.value for m in item.methods] for item in seen] == [["server_identity"]]
        assert origin.auth_attempts == []  # the password was never offered to a mechanism it cannot answer
    finally:
        await runtime.close()


async def test_a_home_relative_file_is_resolved_by_the_server_before_aria2_sees_it(tmp_path, monkeypatch):
    # The in-process server's login directory is its chroot, so ``~/pub`` is
    # what SFTP ``realpath`` resolves to ``/pub`` -- no shell, no guessing.
    origin = await SftpOrigin(_origin_root(tmp_path, "home"), credentials=(USER, PASSWORD)).start()
    runtime = await _scp_runtime(tmp_path, monkeypatch, (origin,))
    try:
        transfer = await runtime.engine.submit((TransferRequest(
            "scp", f"scp://{USER}:{PASSWORD}@scp-origin.test:{origin.port}/~/pub/big.iso", name="big.iso"),),
            deduplicate=False)
        seen = []
        body = await _drive_answers(runtime, transfer.id, lambda: _completed_bytes(runtime, transfer.id),
                                    [("server_identity", None)], seen)
        assert body == PAYLOAD
        assert [[m.method.value for m in item.methods] for item in seen] == [["server_identity"]]
        uris = await _aria2_uris(runtime)
        assert uris and all(uri == f"sftp://scp-origin.test:{origin.port}/pub/big.iso" for uri in uris)
        requests = await runtime.repository.requests(transfer.id)
        assert requests[0].request.payload == f"scp://scp-origin.test:{origin.port}/~/pub/big.iso"
        assert PASSWORD not in await _durable_text()
    finally:
        await runtime.close()
