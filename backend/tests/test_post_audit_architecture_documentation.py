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
    assert "`ui-presentation-loader.js` is physically absent" in doc
    assert "bounded presentation owners" in doc
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


def test_container_entrypoint_bootstrap_diagnostics_preserve_runtime_contract() -> None:
    entrypoint = read("entrypoint.sh")

    # Identity establishment must not silently continue after a real failure.
    assert 'fatal "failed to create runtime group for PGID=${PGID}"' in entrypoint
    assert 'fatal "failed to create runtime user for PUID=${PUID} PGID=${PGID}"' in entrypoint
    assert 'fatal "failed to set primary group for ${RUN_USER} to PGID=${PGID} (PUID=${PUID})"' in entrypoint
    assert 'groupadd -g "${PGID}" appgroup 2>/dev/null || true' not in entrypoint
    assert 'useradd -u "${PUID}" -g "${PGID}" -M -s /bin/sh appuser 2>/dev/null || true' not in entrypoint
    assert 'usermod -g "${PGID}" "${RUN_USER}" 2>/dev/null || true' not in entrypoint

    # Host-controlled mount reconciliation remains non-fatal but observable.
    assert 'if ! chown -R "${PUID}:${PGID}" "${DIR}"; then' in entrypoint
    assert 'continuing so runtime storage checks can diagnose mount permissions' in entrypoint

    # /download recursion remains explicit opt-in; default behavior touches only
    # the mount root and must not regress to an unconditional recursive chown.
    assert 'if [ "${CHOWN_DOWNLOADS_RECURSIVE:-false}" = "true" ]; then' in entrypoint
    assert 'if ! chown -R "${PUID}:${PGID}" /download; then' in entrypoint
    assert 'if ! chown "${PUID}:${PGID}" /download; then' in entrypoint


def test_container_default_identity_contract_is_99_100() -> None:
    entrypoint = read("entrypoint.sh")
    dockerfile = read("Dockerfile")

    assert 'PUID="${PUID:-99}"' in entrypoint
    assert 'PGID="${PGID:-100}"' in entrypoint
    assert "# Directories - owned by 99:100 by default" in dockerfile
    assert "65534:100" not in dockerfile
    assert "chown -R 99:100 /app /download" in dockerfile
