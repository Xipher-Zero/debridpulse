"""DP 1.0.12 canonical lifecycle/recovery/completion rework, Section 3.3/5.2
(Gate 9 revision) and the Transfer 291 size-reconciliation correction: unit
coverage for the one canonical size-evidence owner
(``transfers.size_evidence``), its three-state projection
(``transfers.filesystem.size_knowledge``), the one material-verification
operation (``transfers.filesystem.stable_material_size``), and a
scope-boundary proof that no provider or executor currently wired into this
codebase supplies affirmative known-zero evidence.

Neither the provider-reported size nor the executor-observed total is trusted
over the other: compatible-but-unequal facts are reconciled against the stable
local payload, and incompatible ones are never silently resolved.
"""
from __future__ import annotations

import hashlib
import inspect

import pytest

from transfers import filesystem
from transfers.filesystem import size_knowledge, stable_material_size
from transfers.models import IntegrityMetadata, SizeKnowledge
from transfers.size_evidence import SizeEvidenceKind, classify_size_evidence, positive_size

# Transfer 291 production facts (provider report vs the executor total that
# equalled the stable local file).
T291_REPORTED = 11_038_065_950
T291_OBSERVED = 11_035_235_262


@pytest.mark.parametrize(
    ("reported", "observed", "kind", "sizes"),
    [
        (10, 10, SizeEvidenceKind.EXACT, (10,)),                                            # A
        (10, 0, SizeEvidenceKind.REPORTED_ONLY, (10,)),                                     # B
        (0, 4, SizeEvidenceKind.OBSERVED_ONLY, (4,)),                                       # C
        (1_000_000, 999_744, SizeEvidenceKind.BOUNDED_CONFLICT, (1_000_000, 999_744)),      # D
        (T291_REPORTED, T291_OBSERVED, SizeEvidenceKind.BOUNDED_CONFLICT, (T291_REPORTED, T291_OBSERVED)),
        (1_000_000, 990_000, SizeEvidenceKind.INCOMPATIBLE_CONFLICT, ()),                   # E
        (10, 4, SizeEvidenceKind.INCOMPATIBLE_CONFLICT, ()),
        (0, 0, SizeEvidenceKind.UNKNOWN, ()),                                               # F
        (-1, 0, SizeEvidenceKind.UNKNOWN, ()),
        (0, -1, SizeEvidenceKind.UNKNOWN, ()),
    ],
)
def test_size_evidence_classification_is_source_neutral(reported, observed, kind, sizes):
    evidence = classify_size_evidence(reported, observed)
    assert evidence.kind == kind
    assert evidence.verifiable_sizes == sizes
    # Reconciliation must not depend on which side carried which number.
    assert classify_size_evidence(observed, reported).kind == (
        SizeEvidenceKind.REPORTED_ONLY if kind == SizeEvidenceKind.OBSERVED_ONLY
        else SizeEvidenceKind.OBSERVED_ONLY if kind == SizeEvidenceKind.REPORTED_ONLY
        else kind
    )


def test_a_provider_report_no_longer_wins_over_the_observed_total():
    """The former ``known_positive_size(10, 4) == 10`` "provider always wins"
    completion-truth rule is gone: for unequal positive facts neither number
    is chosen -- a compatible pair yields BOTH candidates for the local payload
    to decide between, an incompatible pair yields none."""
    bounded = classify_size_evidence(T291_REPORTED, T291_OBSERVED)
    assert bounded.verifiable_sizes == (T291_REPORTED, T291_OBSERVED)
    assert classify_size_evidence(10, 4).verifiable_sizes == ()
    assert not hasattr(filesystem, "known_positive_size")


def test_bool_is_not_a_byte_count():
    # bool is a subclass of int in Python; True/False must never be read as
    # a byte count.
    assert positive_size(True) is None
    assert classify_size_evidence(True, 0).kind == SizeEvidenceKind.UNKNOWN
    assert classify_size_evidence(0, True).kind == SizeEvidenceKind.UNKNOWN
    assert size_knowledge(True, 0) == (SizeKnowledge.UNKNOWN, ())


def test_size_knowledge_reports_known_positive_when_a_positive_size_exists():
    assert size_knowledge(10, 0) == (SizeKnowledge.KNOWN_POSITIVE, (10,))
    assert size_knowledge(0, 4) == (SizeKnowledge.KNOWN_POSITIVE, (4,))
    assert size_knowledge(10, 10) == (SizeKnowledge.KNOWN_POSITIVE, (10,))
    assert size_knowledge(1_000_000, 999_744) == (SizeKnowledge.KNOWN_POSITIVE, (1_000_000, 999_744))
    # An incompatible conflict is still known-positive knowledge, but nothing
    # is verifiable: callers must not pick one of the conflicting numbers.
    assert size_knowledge(10, 4) == (SizeKnowledge.KNOWN_POSITIVE, ())


def test_size_knowledge_is_unknown_by_default_when_both_are_zero():
    assert size_knowledge(0, 0) == (SizeKnowledge.UNKNOWN, ())


def test_size_knowledge_only_reports_known_zero_with_explicit_affirmative_evidence():
    """SIZE_UNKNOWN and SIZE_KNOWN(0) are distinct facts: the same (0, 0)
    inputs must resolve differently depending on whether the caller can
    supply genuine affirmative-zero evidence, never from the inputs alone."""
    assert size_knowledge(0, 0, affirmative_zero=True) == (SizeKnowledge.KNOWN_ZERO, (0,))
    knowledge, _ = size_knowledge(0, 0, affirmative_zero=False)
    assert knowledge == SizeKnowledge.UNKNOWN


def test_affirmative_zero_never_overrides_a_real_known_positive_size():
    """A caller-asserted affirmative-zero claim must never be allowed to
    contradict an already-known positive size -- known-positive always
    takes precedence over a claimed-zero input."""
    assert size_knowledge(10, 0, affirmative_zero=True) == (SizeKnowledge.KNOWN_POSITIVE, (10,))


# --- One material-verification operation, against real stable local files ---

def _payload(tmp_path, size, name="payload.bin"):
    target = tmp_path / name
    target.write_bytes(b"x" * size)
    return str(target)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reported", "observed", "actual", "accepted"),
    [
        (10, 10, 10, 10),                          # A: exact agreement.
        (10, 0, 10, 10),                           # B: provider-only, strict against the report.
        (10, 0, 9, None),
        (0, 4, 4, 4),                              # C: executor-only, against the executor total.
        (0, 4, 5, None),
        (1_000_000, 999_744, 999_744, 999_744),    # D: bounded conflict; the payload proves the observed size...
        (1_000_000, 999_744, 1_000_000, 1_000_000),  # ...or the reported one -- never a blind preference.
        (T291_REPORTED, T291_OBSERVED, 999_744, None),   # D but the payload matches neither asserted size.
        (1_000_000, 990_000, 990_000, None),       # E: incompatible; file == executor total is not enough.
        (1_000_000, 990_000, 1_000_000, None),     # E: nor is file == provider report.
        (0, 0, 4, None),                           # F: unknown size is never verified/fabricated...
        (0, 0, 0, None),                           # ...including a zero-byte file.
    ],
)
async def test_stable_material_size_reconciles_size_evidence_against_the_local_payload(
    tmp_path, reported, observed, actual, accepted,
):
    path = _payload(tmp_path, actual)
    assert await stable_material_size(path, reported, observed, delay=0) == accepted


@pytest.mark.asyncio
async def test_bounded_drift_never_overrides_an_integrity_mismatch(tmp_path):
    path = _payload(tmp_path, 999_744)
    good = IntegrityMetadata("sha256", hashlib.sha256(b"x" * 999_744).hexdigest())
    bad = IntegrityMetadata("sha256", "0" * 64)
    assert await stable_material_size(path, 1_000_000, 999_744, integrity=(good,), delay=0) == 999_744
    assert await stable_material_size(path, 1_000_000, 999_744, integrity=(bad,), delay=0) is None


@pytest.mark.asyncio
async def test_bounded_drift_keeps_sidecar_symlink_and_missing_path_guarantees(tmp_path):
    path = _payload(tmp_path, 999_744)
    sidecar = tmp_path / "payload.bin.aria2"
    sidecar.write_bytes(b"resume")
    assert await stable_material_size(path, 1_000_000, 999_744, sidecars=(str(sidecar),), delay=0) is None
    sidecar.unlink()

    link = tmp_path / "link.bin"
    link.symlink_to(path)
    assert await stable_material_size(str(link), 1_000_000, 999_744, delay=0) is None
    assert await stable_material_size(str(tmp_path / "missing.bin"), 1_000_000, 999_744, delay=0) is None


# --- Recorded (bookkeeping) size is not a provider report ---
#
# ``artifact.expected_bytes`` can be learned from earlier executor progress
# (``accept_execution_total``) or a bounded sample. It is never an upstream
# provider report, so it must never be used to reject the executor's final
# total, and never to override a positive provider report; it is consulted only
# when there is neither a report nor a final total.

@pytest.mark.parametrize(
    ("reported", "observed", "recorded", "kind", "sizes"),
    [
        (0, 999_744, 4, SizeEvidenceKind.OBSERVED_ONLY, (999_744,)),   # the removed override's shape.
        (0, 999_744, 1_000_000, SizeEvidenceKind.OBSERVED_ONLY, (999_744,)),
        (0, 10, 4, SizeEvidenceKind.OBSERVED_ONLY, (10,)),              # even an incompatible recorded size.
        (T291_REPORTED, T291_OBSERVED, 4, SizeEvidenceKind.BOUNDED_CONFLICT, (T291_REPORTED, T291_OBSERVED)),
        (0, 0, 4, SizeEvidenceKind.RECORDED_ONLY, (4,)),                # no final total: recorded is all there is.
        (1_000_000, 0, 999_744, SizeEvidenceKind.REPORTED_ONLY, (1_000_000,)),  # bookkeeping never overrides a report.
        (1_000_000, 0, 0, SizeEvidenceKind.REPORTED_ONLY, (1_000_000,)),
        (0, 0, 0, SizeEvidenceKind.UNKNOWN, ()),
        (0, 0, True, SizeEvidenceKind.UNKNOWN, ()),                     # bool is not a byte count.
    ],
)
def test_recorded_bookkeeping_is_consulted_only_when_there_is_no_final_total(
    reported, observed, recorded, kind, sizes,
):
    evidence = classify_size_evidence(reported, observed, recorded)
    assert evidence.kind == kind
    assert evidence.verifiable_sizes == sizes


def test_recorded_bookkeeping_is_retained_for_diagnostics_but_never_an_accepted_size_beside_a_report():
    evidence = classify_size_evidence(1_000_000, 0, 999_744)
    assert evidence.kind == SizeEvidenceKind.REPORTED_ONLY
    assert evidence.recorded == 999_744  # kept on the evidence object...
    assert evidence.verifiable_sizes == (1_000_000,)  # ...but never a verifiable size next to a provider report.
    assert 999_744 not in evidence.verifiable_sizes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reported", "observed", "recorded", "actual", "accepted"),
    [
        (0, 999_744, 4, 999_744, 999_744),     # unknown provider size + executor final + payload == final: completes.
        (0, 999_744, 4, 4, None),              # payload == stale bookkeeping is not enough against a final total.
        (0, 0, 4, 4, 4),                       # no report, no final total: the recorded-only fallback verifies.
        (1_000_000, 0, 999_744, 999_744, None),        # no final total: a positive report is NOT displaced by bookkeeping...
        (1_000_000, 0, 999_744, 1_000_000, 1_000_000),  # ...only the provider report verifies.
        (0, 0, 4, 0, None),                    # executor zero/unknown never yields a zero-byte success...
        (0, 0, 4, 9, None),
        (0, 0, 0, 0, None),                    # ...nor does wholly unknown size.
        (1_000_000, 999_744, 999_744, 999_744, 999_744),
        (1_000_000, 990_000, 990_000, 990_000, None),   # out-of-tolerance provider conflict still fails closed.
    ],
)
async def test_stable_material_size_never_lets_recorded_bookkeeping_override_or_fabricate(
    tmp_path, reported, observed, recorded, actual, accepted,
):
    path = _payload(tmp_path, actual)
    assert await stable_material_size(
        path, reported, observed, recorded_bytes=recorded, delay=0,
    ) == accepted


@pytest.mark.asyncio
async def test_final_total_acceptance_still_requires_integrity(tmp_path):
    path = _payload(tmp_path, 999_744)
    good = IntegrityMetadata("sha256", hashlib.sha256(b"x" * 999_744).hexdigest())
    bad = IntegrityMetadata("sha256", "0" * 64)
    assert await stable_material_size(path, 0, 999_744, recorded_bytes=4, integrity=(good,), delay=0) == 999_744
    assert await stable_material_size(path, 0, 999_744, recorded_bytes=4, integrity=(bad,), delay=0) is None


def test_general_http_and_aria2_never_pass_affirmative_zero():
    """DP 1.0.12 canonical lifecycle/recovery/completion rework, Section 5.2
    (Gate 9 revision, finding 3): ``affirmative_zero`` must only ever be set
    from a genuine, positively-confirmed zero-length signal. No provider or
    executor currently wired into this codebase has one -- General HTTP and
    aria2 both resolve a reported size through a ``value or 0``-shaped
    fallback that cannot distinguish an explicitly reported zero from an
    absent field. This is a static, source-level proof of that scope
    boundary: it must fail loudly (not silently regress into a fabricated
    affirmative-zero signal) the moment someone adds
    ``affirmative_zero=True`` to a real call site without first building a
    genuine evidence channel for it."""
    import providers.general_http.provider as general_http_provider
    import executors.aria2.client as aria2_client
    import executors.aria2.executor as aria2_executor
    import executors.aria2.translation as aria2_translation
    import transfers._engine_base as engine_base
    import transfers._engine_recovery as engine_recovery

    for module in (
        general_http_provider, aria2_client, aria2_executor, aria2_translation,
        engine_base, engine_recovery,
    ):
        source = inspect.getsource(module)
        assert "affirmative_zero=True" not in source, (
            f"{module.__name__} passes affirmative_zero=True without a real evidence "
            "channel backing it"
        )


@pytest.mark.parametrize(
    ("reported", "observed"),
    [(0, 0), (4, 0), (0, 4), (10, 10), (1_000_000, 999_744), (10, 4), (True, 0)],
)
def test_size_knowledge_is_a_pure_projection_of_the_canonical_classification(reported, observed):
    """``size_knowledge`` must never diverge from ``classify_size_evidence`` --
    it is a projection of it, not a parallel reimplementation."""
    evidence = classify_size_evidence(reported, observed)
    knowledge, sizes = size_knowledge(reported, observed)
    if evidence.kind == SizeEvidenceKind.UNKNOWN:
        assert knowledge == SizeKnowledge.UNKNOWN and sizes == ()
    else:
        assert knowledge == SizeKnowledge.KNOWN_POSITIVE and sizes == evidence.verifiable_sizes
