"""Narrow Workspace 4 equivalence hardening regressions."""
from types import SimpleNamespace

import pytest

import executors.aria2.executor as aria2_executor
from executors.aria2.executor import Aria2Configuration, Aria2Executor
from transfers.models import Endpoint, FingerprintKind, TransferCandidate


@pytest.mark.asyncio
async def test_aria2_fingerprint_binds_sampler_to_declared_candidate_size(tmp_path, monkeypatch):
    captured = {}

    async def sampled(uri, **kwargs):
        captured["uri"] = uri
        captured.update(kwargs)
        return (123, "full-signature", FingerprintKind.FULL_CONTENT_SAMPLE, "", "prefix-signature")

    monkeypatch.setattr(aria2_executor, "sampled_public_artifact_fingerprint", sampled)

    async def authorize(_handle, _action):
        return True

    executor = Aria2Executor(
        SimpleNamespace(url="http://aria2.invalid/jsonrpc"),
        Aria2Configuration(local_root=str(tmp_path)),
        authorize,
    )
    candidate = TransferCandidate(
        "payload.bin",
        (Endpoint("https", "https://example.invalid/payload.bin"),),
        expected_bytes=123,
        provider_id="provider-test",
    )

    fingerprint = await executor.fingerprint(candidate)

    assert captured["expected_bytes"] == 123
    assert captured["uri"] == "https://example.invalid/payload.bin"
    assert fingerprint.total_bytes == 123
    assert fingerprint.kind == FingerprintKind.FULL_CONTENT_SAMPLE
