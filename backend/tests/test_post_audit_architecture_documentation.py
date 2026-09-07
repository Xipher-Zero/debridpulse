from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_retry_and_provenance_docs_preserve_post_audit_contract() -> None:
    doc = read("docs/ROUTE_PROVIDER_PROVENANCE.md")
    assert "ordinary resolution retry and re-resolution remain bound to that selected provider" in doc
    assert "Automatic cross-provider production failover is deferred" in doc
    assert "never reconstructed later from the submitted URL" in doc


def test_applicability_docs_require_explicit_provider_contract() -> None:
    doc = read("docs/architecture/PROVIDER_APPLICABILITY.md")
    assert "Missing applicability is not a compatibility signal" in doc
    assert "explicit `ProviderApplicability()`" in doc
    assert "ordinary retry remains bound to that provider" in doc


def test_core_docs_record_cancellation_and_migration_ownership() -> None:
    doc = read("docs/architecture/UNIVERSAL_TRANSFER_CORE.md")
    assert "Logical cancellation authority is committed on the parent transfer before remote executor cancellation" in doc
    assert "cannot revive it" in doc
    assert "Normal repository initialization" in doc
    assert "db/migrations/v112.py" in doc


def test_frontend_docs_describe_live_bounded_owners() -> None:
    doc = read("docs/UI_FRONTEND_ARCHITECTURE.md")
    assert doc.startswith("# DebridPulse v1.0.12 Frontend Architecture")
    assert "six reachable navigation surfaces" in doc
    assert "`ui-presentation-loader.js`" in doc
    assert "orchestration-only" in doc
    for owner in ("ui-dashboard-transfer-presentation.js", "ui-downloads-presentation.js", "ui-processing-presentation.js", "ui-activity-log-runtime.js", "ui-settings-archive-passwords.js"):
        assert owner in doc
    assert "There is no `DPUICorrectionBatch1`, `DPUICorrectionBatch1Final`, or `DPUICorrectionP4Repair`" in doc
    assert "Correction-named Batch-1 styles are absent" in doc
    assert "`style-v11.css` is the canonical import graph" in doc


def test_canonical_bundle_comment_no_longer_claims_v111_overlay() -> None:
    style = read("frontend/static/style-v11.css")
    assert "v1.0.12 canonical visual import graph" in style
    assert "v1.0.11 visual system overlay" not in style
    assert "UI Correction Batch" not in style
