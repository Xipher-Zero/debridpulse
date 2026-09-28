"""The one SSH exec channel a subprocess transport reaches a server through.

A native tool that speaks its own protocol over a remote shell (rsync over
SSH) runs this module as its remote-shell program. It is not a second SSH
client policy: the server identity, the credential and the connection are all
decided by the owners that decide them for every other SSH consumer.

* The connection is one DebridPulse already opened through the egress guard
  (``DownloaderEgressGuard.open_tunnel``), handed down as an inherited
  descriptor -- the tool never resolves or connects to anything itself.
* Identity, then authentication, run through the one SSH step
  (``services.artifact_sampling.ssh_connection``) with the same host-key
  preference, so the identity the operator confirmed for the scope is exactly
  the one verified here.
* The channel's material -- username, password or key (and passphrase), the
  confirmed identity -- arrives once over an inherited pipe: never argv, the
  environment or a file.
* Its outcome is reported as typed status records over a second inherited
  pipe, so the owning executor never interprets free text for a security
  decision.

The remote command is the tool's own argument list joined with spaces, exactly
as an OpenSSH client forwards it; the tool quotes it for the remote shell.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys

# Status records (one JSON object per line on the status pipe).
CONNECTED = "connected"
IDENTITY_REQUIRED = "identity_required"
IDENTITY_CHANGED = "identity_changed"
AUTHENTICATION_REJECTED = "authentication_rejected"
METHOD_UNSUPPORTED = "method_unsupported"
KEY_UNUSABLE = "key_unusable"
UNAVAILABLE = "unavailable"
EXIT = "exit"

# The placeholder host the native tool is given; the real one is in the spec,
# so no hostname (or IPv6 literal) is ever parsed out of a tool argument.
CHANNEL_HOST = "dp-ssh-channel"
_MAX_SPEC_BYTES = 64 * 1024
_RELAY_CHUNK = 64 * 1024


def remote_shell(python: str, *, spec_fd: int, status_fd: int, tunnel_fd: int) -> list[str]:
    """The remote-shell program argv for a native tool (it appends
    ``[-l user] host command...``)."""
    return [python, "-m", "services.ssh_channel", "--spec-fd", str(int(spec_fd)),
            "--status-fd", str(int(status_fd)), "--tunnel-fd", str(int(tunnel_fd))]


def channel_spec(*, host: str, username: str, password: str = "", private_key: str = "", passphrase: str = "",
                 identity: str | None, host_key_algorithms, timeout: float) -> bytes:
    """The one-shot material a channel reads from its inherited pipe."""
    return json.dumps({
        "host": host, "username": username, "password": password, "private_key": private_key,
        "passphrase": passphrase, "identity": identity or None,
        "host_key_algorithms": list(host_key_algorithms), "timeout": float(timeout),
    }, separators=(",", ":")).encode("utf-8")


def read_status(data: bytes) -> list[dict]:
    """Parse a channel's status records; malformed lines are ignored."""
    records = []
    for line in data.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("event"), str):
            records.append(value)
    return records


def _read_all(fd: int, limit: int) -> bytes:
    chunks, total = [], 0
    while True:
        chunk = os.read(fd, 4096)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise ValueError("channel material too large")
        chunks.append(chunk)


def _parse(argv: list[str]):
    options, rest = {}, list(argv)
    while rest and rest[0] in {"--spec-fd", "--status-fd", "--tunnel-fd"}:
        if len(rest) < 2:
            raise ValueError("incomplete channel option")
        options[rest[0]] = int(rest[1])
        rest = rest[2:]
    if len(options) != 3:
        raise ValueError("channel descriptors missing")
    user = None
    if rest[:1] == ["-l"]:
        if len(rest) < 2:
            raise ValueError("incomplete login option")
        user, rest = rest[1], rest[2:]
    if len(rest) < 2 or rest[0] != CHANNEL_HOST:
        raise ValueError("unexpected channel host or empty command")
    return options["--spec-fd"], options["--status-fd"], options["--tunnel-fd"], user, rest[1:]


async def _relay(process) -> int:
    """Relay the tool's stdin to the remote command and its output back, until
    the remote command exits; returns its exit status (255 when it has none)."""
    loop = asyncio.get_running_loop()
    stdin = asyncio.StreamReader(limit=_RELAY_CHUNK)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdin), os.fdopen(0, "rb", buffering=0))

    async def writer_for(fd: int):
        transport, protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin,
                                                            os.fdopen(fd, "wb", buffering=0))
        return asyncio.StreamWriter(transport, protocol, None, loop)

    stdout, stderr = await writer_for(1), await writer_for(2)

    async def upstream():
        while chunk := await stdin.read(_RELAY_CHUNK):
            process.stdin.write(chunk)
            await process.stdin.drain()
        process.stdin.write_eof()

    async def downstream(reader, writer):
        while chunk := await reader.read(_RELAY_CHUNK):
            writer.write(chunk)
            await writer.drain()

    feeder = asyncio.ensure_future(upstream())
    try:
        await asyncio.gather(downstream(process.stdout, stdout), downstream(process.stderr, stderr))
        completed = await process.wait()
    finally:
        feeder.cancel()
    status = completed.exit_status
    return status if isinstance(status, int) and status >= 0 else 255


def _refusal(outcome, expected: str | None) -> tuple[str, dict]:
    """The typed status record for a session the one SSH step refused."""
    from services.artifact_sampling import AccessRequired
    if isinstance(outcome, AccessRequired):
        # Before a confirmed identity nothing is sent: the key is only observed.
        return (IDENTITY_REQUIRED, {"observed": outcome.server_identity}) if expected is None \
            else (AUTHENTICATION_REJECTED, {})
    reason = outcome[3] if isinstance(outcome, tuple) and len(outcome) > 3 else ""
    if reason == "auth_method_unsupported":
        return METHOD_UNSUPPORTED, {}
    if reason == "key_unusable":
        return KEY_UNUSABLE, {}
    if reason == "destination_rejected" and expected is not None:
        # A presented key other than the confirmed one is refused during key
        # exchange: a changed identity, never a new question.
        return IDENTITY_CHANGED, {}
    return UNAVAILABLE, {"reason": reason or "unavailable"}


async def _run(spec: dict, status, tunnel_fd: int, command: list[str]) -> int:
    from contextlib import AsyncExitStack

    import asyncssh
    from services.artifact_sampling import _SessionRefused, ssh_connection

    def report(event: str, **facts):
        status.write(json.dumps({"event": event, **facts}, separators=(",", ":")) + "\n")
        status.flush()

    sock = socket.socket(fileno=tunnel_fd)
    expected = spec.get("identity") or None
    timeout = max(1.0, float(spec["timeout"]))
    async with AsyncExitStack() as stack:
        try:
            async with asyncio.timeout(timeout):
                connection = await stack.enter_async_context(ssh_connection(
                    spec["host"], sock=sock, host_key_algorithms=spec["host_key_algorithms"],
                    host_identity=expected, username=spec["username"], password=spec.get("password") or "",
                    private_key=spec.get("private_key") or "", passphrase=spec.get("passphrase") or "",
                    timeout=timeout))
        except _SessionRefused as refused:
            event, facts = _refusal(refused.outcome, expected)
            report(event, **facts)
            return 255
        except TimeoutError:
            report(UNAVAILABLE, reason="timeout")
            return 255
        except (OSError, asyncssh.Error):
            report(UNAVAILABLE, reason="connection_failed")
            return 255
        report(CONNECTED)
        process = await connection.create_process(" ".join(command), encoding=None)
        code = await _relay(process)
        report(EXIT, status=code)
        return code


def main(argv: list[str]) -> int:
    try:
        spec_fd, status_fd, tunnel_fd, user, command = _parse(argv)
    except ValueError:
        return 255
    with os.fdopen(status_fd, "w", encoding="utf-8") as status:
        try:
            spec = json.loads(_read_all(spec_fd, _MAX_SPEC_BYTES))
        except (OSError, ValueError):
            return 255
        finally:
            os.close(spec_fd)
        if user is not None and user != spec.get("username"):
            return 255
        return asyncio.run(_run(spec, status, tunnel_fd, command))


if __name__ == "__main__":  # pragma: no cover - exercised as a real subprocess
    sys.exit(main(sys.argv[1:]))
