"""Exact-SHA release promotion and browser dependency reproducibility contracts."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
BROWSER = ROOT / "frontend" / "browser"


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_browser_dependency_graph_is_lockfile_reproducible() -> None:
    package = json.loads((BROWSER / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((BROWSER / "package-lock.json").read_text(encoding="utf-8"))

    assert package["devDependencies"]["@playwright/test"] == "1.62.1"
    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["devDependencies"] == package["devDependencies"]
    assert lock["packages"]["node_modules/@playwright/test"]["version"] == "1.62.1"
    assert lock["packages"]["node_modules/playwright"]["version"] == "1.62.1"
    assert lock["packages"]["node_modules/playwright-core"]["version"] == "1.62.1"

    workflow = _workflow("browser-runtime.yml")
    assert "npm ci --ignore-scripts" in workflow
    assert "npm install --ignore-scripts" not in workflow
    assert "npm audit --audit-level=high" in workflow


def test_fork_image_only_publishes_immutable_sha_tags() -> None:
    workflow = _workflow("fork-image.yml")

    assert "type=sha,prefix=sha-,format=short" in workflow
    assert "type=raw,value=latest" not in workflow
    assert "type=ref,event=tag" not in workflow
    assert "Immutable publication must generate exactly one tag" in workflow
    assert "Publication tag is not SHA-only" in workflow


def test_security_qualification_consumes_exact_published_platform_digests() -> None:
    workflow = _workflow("container-security.yml")

    assert 'source_ref="${IMAGE_NAME}:sha-${GITHUB_SHA:0:7}"' in workflow
    assert "docker build --pull" not in workflow
    assert "docker build " not in workflow
    assert "docker/build-push-action" not in workflow
    assert 'platform.architecture == "amd64"' in workflow
    assert 'platform.architecture == "arm64"' in workflow
    assert 'arch: [amd64, arm64]' in workflow
    assert "${{ env.IMAGE_NAME }}@${{ env.CHILD_DIGEST }}" in workflow
    assert "Block fixable high or critical vulnerabilities on exact child" in workflow
    assert "actions/attest@" in workflow
    assert "subject-digest: ${{ needs.identity.outputs.manifest_digest }}" in workflow
    assert "container-security/v1" in workflow


def test_runtime_qualification_consumes_exact_children_without_rebuild() -> None:
    workflow = _workflow("candidate-runtime-qualification.yml")

    assert 'source_ref="${IMAGE_NAME}:sha-${GITHUB_SHA:0:7}"' in workflow
    assert "docker build " not in workflow
    assert "docker/build-push-action" not in workflow
    assert 'platform.architecture == "amd64"' in workflow
    assert 'platform.architecture == "arm64"' in workflow
    assert 'arch: [amd64, arm64]' in workflow
    assert "docker/setup-qemu-action@" in workflow
    assert 'docker pull --platform "linux/${{ matrix.arch }}" "$image"' in workflow
    assert '--platform "linux/${{ matrix.arch }}"' in workflow
    assert 'uid" = "99"' in workflow
    assert 'gid" = "100"' in workflow
    assert "actions/attest@" in workflow
    assert "subject-digest: ${{ needs.identity.outputs.manifest_digest }}" in workflow
    assert "candidate-runtime/v1" in workflow


def test_mutable_promotion_requires_exact_sha_workflows_and_signed_digest_evidence() -> None:
    workflow = _workflow("release-promotion.yml")

    for required in (
        '"Tests"',
        '"Browser Runtime"',
        '"CodeQL"',
        '"Container Security"',
        '"Candidate Runtime Qualification"',
        '"Fork Image"',
    ):
        assert required in workflow

    assert "head_sha=${CANDIDATE_SHA}" in workflow
    assert ".head_sha == $sha" in workflow
    assert '.status == "completed"' in workflow
    assert '.conclusion == "success"' in workflow
    assert 'source_tag=sha-${candidate_sha:0:7}' in workflow
    assert 'platform.architecture == "amd64"' in workflow
    assert 'platform.architecture == "arm64"' in workflow
    assert 'image_revision" != "$CANDIDATE_SHA"' in workflow
    assert "gh attestation verify" in workflow
    assert "container-security/v1" in workflow
    assert "candidate-runtime/v1" in workflow
    assert '$p.manifest_digest == $manifest' in workflow
    assert '$p.platforms["linux/amd64"].digest == $amd64' in workflow
    assert '$p.platforms["linux/arm64"].digest == $arm64' in workflow
    assert "QUALIFIED_DIGEST == PROMOTED_SOURCE_DIGEST" in workflow
    assert 'docker buildx imagetools create --tag "$target_ref" "$source_ref"' in workflow
    assert 'target_digest" != "$SOURCE_DIGEST"' in workflow
    assert "docker build " not in workflow
    assert "docker/build-push-action" not in workflow


def test_release_tag_pushes_run_every_independent_qualifier() -> None:
    for name in (
        "tests.yml",
        "browser-runtime.yml",
        "codeql.yml",
        "container-security.yml",
        "candidate-runtime-qualification.yml",
        "fork-image.yml",
    ):
        workflow = _workflow(name)
        assert "- 'v*'" in workflow, name
        assert "- 'internal-v*'" in workflow, name


def test_release_promotion_is_the_only_mutable_image_tag_owner() -> None:
    promotion = _workflow("release-promotion.yml")
    assert 'target_tag="latest"' in promotion
    assert 'target_tag="$GITHUB_REF_NAME"' in promotion
    assert "dry_run=\"false\"" in promotion
    assert "No mutable image tag was changed." in promotion

    for path in WORKFLOWS.glob("*.yml"):
        if path.name == "release-promotion.yml":
            continue
        text = path.read_text(encoding="utf-8")
        assert "type=raw,value=latest" not in text, path.name
