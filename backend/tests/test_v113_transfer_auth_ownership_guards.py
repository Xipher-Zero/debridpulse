"""Static guards: one owner for transfer authentication, trust and discovery.

These read production source, so a later change that reintroduces a second
credential store, an executor pre-authentication side channel, an SCP/SSH
process fallback or a second host-trust decision fails here, not in review.
"""
from __future__ import annotations

from pathlib import Path
import re

BACKEND = Path(__file__).resolve().parents[1]


def _production():
    for path in sorted(BACKEND.rglob("*.py")):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        yield path, path.read_text(encoding="utf-8")


def _defining(pattern: str) -> set[str]:
    return {str(path.relative_to(BACKEND)) for path, text in _production() if re.search(pattern, text, re.M)}


def test_one_authentication_input_owner():
    assert _defining(r"^class EphemeralInputBroker\b") == {"transfers/input_required.py"}
    assert _defining(r"^class SubmittedInput\b") == {"transfers/input_required.py"}
    # Nothing else keeps USER_SUPPLIED material or authentication contexts.
    assert _defining(r"^\s*def split_user_supplied\b") == {"transfers/input_required.py"}
    assert _defining(r"\bsplit_user_supplied\(") == {
        "transfers/input_required.py", "transfers/_engine_base.py", "transfers/engine.py"}


def test_providers_hold_no_credentials_and_open_no_connections():
    for path, text in _production():
        if path.relative_to(BACKEND).parts[0] != "providers":
            continue
        if "general_" not in str(path):
            continue
        for forbidden in ("input_required", "SubmittedInput", "EphemeralInputBroker", "asyncssh", "socket",
                          "aiohttp", "subprocess", "known_hosts", "ssh-host-key"):
            assert forbidden not in text, f"{path.relative_to(BACKEND)} references {forbidden}"


def test_no_executor_pre_authentication_side_channel():
    for path, text in _production():
        assert not re.search(r"def \w*pre_?auth\w*\(", text, re.I), path
        assert "get_pre_auth_credentials" not in text, path


def test_no_scp_or_ssh_process_fallback_anywhere():
    for path, text in _production():
        for call in re.findall(r"(?:create_subprocess_exec|create_subprocess_shell|subprocess\.\w+|os\.system)\((.*)",
                               text):
            assert not re.search(r"""["'](?:scp|ssh|sftp|sshpass)["']""", call), (path, call)


def test_one_server_identity_decision():
    # The host key is judged in exactly one session primitive and pinned into
    # the native writer by exactly one executor option owner.
    assert _defining(r"def validate_host_public_key\(") == {"services/artifact_sampling.py"}
    assert _defining(r"async def _sftp_session\(") == {"services/artifact_sampling.py"}
    assert _defining(r"ssh-host-key-md") == {"executors/aria2/executor.py"}
    assert _defining(r"def _confirmed_evidence_identity\(") == {"executors/aria2/executor.py"}
    assert _defining(r"def _sftp_access\(") == {"executors/aria2/executor.py"}


def test_discovery_runs_only_through_core():
    # Providers request discovery; only core invokes the executor capability.
    assert _defining(r"\.discover\(") == {"transfers/_engine_base.py"}
