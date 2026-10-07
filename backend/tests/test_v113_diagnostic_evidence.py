"""Bounded, durable, diagnostics-only evidence on the one normalized failure.

``NormalizedError.diagnostic_evidence`` is safe before it is persisted, travels
in the error's own durable encoding, and never takes part in what the failure
means: not equality, not the failure signature, not recovery accounting, not
the ordinary error projection."""
from __future__ import annotations

from dataclasses import replace
import json

import pytest

from test_recovery_state_store import _recovery_audit_rows, runtime, start_transfer  # noqa: F401 -- fixture
from transfers import codec
from transfers.errors import (
    Category, Domain, NormalizedError, Origin, Retryability, Stage, safe_diagnostic_evidence,
)
from transfers.policy import failure_signature, recovery_action

HOSTILE_URL = "https://user:password@real-debrid.com/d/SECRET?token=TOKEN"
SECRETS = ("password", "SECRET", "TOKEN", "BEARERVALUE", "COOKIEVALUE", "APIKEYVALUE", "HEADERVALUE", "/d/")


def protocol_failure(evidence=None) -> NormalizedError:
    return NormalizedError(Domain.PROVIDER, Category.PROVIDER_PROTOCOL_VIOLATION, Stage.CANDIDATE_PREPARATION,
                           Retryability.NEVER, origin=Origin.PROVIDER, integration_id="realdebrid",
                           native_code="native", diagnostic="selected files and links do not reconcile",
                           context={"attempt": 1}, diagnostic_evidence=evidence or {})


def compact_bytes(value) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8"))


def test_capability_and_credential_material_never_reaches_the_durable_encoding():
    error = protocol_failure({
        "link": HOSTILE_URL,
        "note": f"fetched {HOSTILE_URL} with Authorization: Bearer BEARERVALUE",
        "Authorization": "Bearer BEARERVALUE", "cookie": "sid=COOKIEVALUE", "api_key": "APIKEYVALUE",
        "nested": {"items": ["Bearer BEARERVALUE", {"headers": {"X-Auth": "HEADERVALUE"}}], "bytes": b"raw"},
        "file_count": 5,
    })
    durable = codec.dump(error)
    for secret in SECRETS:
        assert secret not in durable, secret
    evidence = error.as_dict(diagnostics=True)["diagnostic_evidence"]
    assert evidence["link"] == "<capability-url>" and evidence["file_count"] == 5
    assert evidence["Authorization"] == evidence["cookie"] == evidence["api_key"] == "<redacted>"
    assert evidence["nested"] == {"items": ["<credential>", {"headers": "<redacted>"}], "bytes": "<bytes>"}


def test_the_evidence_survives_the_codec_and_historical_errors_decode_without_any():
    error = protocol_failure({"native_file_count": 5, "files": [{"ordinal": 0, "relative_path": "a.mkv",
                                                                 "bytes": 7, "selected": True}]})
    decoded = codec.error(codec.dump(error))
    assert decoded == error
    assert decoded.as_dict(diagnostics=True) == error.as_dict(diagnostics=True)
    assert decoded.as_dict(diagnostics=True)["diagnostic_evidence"]["files"][0]["relative_path"] == "a.mkv"
    # An error recorded before the field existed decodes with empty evidence,
    # and an error without evidence keeps exactly that historical encoding.
    historical = json.loads(codec.dump(error))
    historical.pop("diagnostic_evidence")
    old = codec.error(json.dumps(historical))
    assert old == error and dict(old.diagnostic_evidence) == {}
    assert "diagnostic_evidence" not in codec.dump(old) and codec.dump(old) == json.dumps(
        historical, separators=(",", ":"), sort_keys=True)


def test_the_ordinary_projection_never_carries_the_evidence_and_the_evidence_is_immutable():
    source = {"files": [{"relative_path": "a.mkv"}], "native_file_count": 1}
    error = protocol_failure(source)
    public = error.as_dict()
    for name in ("native_code", "diagnostic", "context", "diagnostic_evidence"):
        assert name not in public, name
    assert error.as_dict(diagnostics=True)["diagnostic_evidence"] == source
    # The caller's objects are not reachable behind the frozen error.
    source["files"][0]["relative_path"] = "changed.mkv"
    source["native_file_count"] = 99
    assert error.as_dict(diagnostics=True)["diagnostic_evidence"] == {
        "files": [{"relative_path": "a.mkv"}], "native_file_count": 1}
    with pytest.raises(TypeError):
        error.diagnostic_evidence["native_file_count"] = 2
    with pytest.raises(TypeError):
        error.diagnostic_evidence["files"][0]["relative_path"] = "x"


def test_oversized_evidence_is_pruned_deterministically_within_the_byte_ceiling():
    # Within every structural bound (64 entries, depth, nodes) yet far past
    # 16 KiB: 64 records of 256 four-byte characters each.
    wide = "\U0001F4E6" * 300
    hostile = {"file_count": 64, "link_count": 3, "label": wide,
               "files": [{"ordinal": index, "relative_path": wide} for index in range(64)],
               "links": [{"ordinal": index, "host": wide} for index in range(64)]}
    first = safe_diagnostic_evidence(hostile)
    assert compact_bytes(first) <= 16_384
    # ...and so is the escaped form it is persisted in.
    assert len(json.dumps(first, separators=(",", ":"), sort_keys=True)) <= 16_384
    assert first["_truncated"] is True
    assert (first["file_count"], first["link_count"]) == (64, 3)
    # Whole entries are pruned from the ends; what is kept is a native-order
    # prefix of complete records.
    for name in ("files", "links"):
        assert [item["ordinal"] for item in first[name]] == list(range(len(first[name])))
        assert all(set(item) == {"ordinal", "relative_path" if name == "files" else "host"} for item in first[name])
    assert safe_diagnostic_evidence(hostile) == first                          # deterministic
    assert safe_diagnostic_evidence(first) == first                            # idempotent
    error = protocol_failure(hostile)
    assert compact_bytes(error.as_dict(diagnostics=True)["diagnostic_evidence"]) <= 16_384
    # Structural bounds also state that they cut, and never raise.
    deep = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}, "many": list(range(500)), "nan": float("nan")}
    bounded = safe_diagnostic_evidence(deep)
    assert bounded["_truncated"] is True and len(bounded["many"]) == 64 and bounded["nan"] is None
    assert bounded["a"]["b"]["c"]["d"] == {"e": "<truncated>"}


def test_evidence_never_changes_equality_the_failure_signature_or_recovery():
    left = protocol_failure({"link_count": 1, "files": [{"ordinal": 0}]})
    right = protocol_failure({"link_count": 5})
    assert left == right and left == replace(right, diagnostic_evidence={})
    assert failure_signature(left) == failure_signature(right)
    assert recovery_action(left) == recovery_action(right)


@pytest.mark.asyncio
async def test_evidence_never_splits_same_signature_recovery_accounting(runtime):  # noqa: F811
    repository = runtime[0]
    transfer, artifact = await start_transfer(runtime)
    failure = NormalizedError(Domain.NETWORK, Category.REMOTE_READ_FAILED, Stage.EXECUTION,
                              retryability=Retryability.BACKOFF, origin=Origin.REMOTE_SOURCE,
                              integration_id="memory-copy")
    await repository.record_source_failure(artifact.id, replace(failure, diagnostic_evidence={"observed": 1}))
    await repository.record_source_failure(artifact.id, replace(failure, diagnostic_evidence={"observed": 2}))
    audits = await _recovery_audit_rows(transfer.id, "source_failure")
    assert [item["same_signature_failures"] for item in audits] == [1, 2]
