"""DP 1.0.12 leveling remediation (DEP-001): qualified-artifact authority and
build-input hardening contract.

Source-level assertions against the Dockerfile, the runtime Python lock, and
the supply-chain policy document. Does not build the image or touch a
registry -- Fork Image / Container Security / Candidate Runtime
Qualification remain the authority for the actual built artifact (see
docs/SUPPLY_CHAIN_POLICY.md).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_DOCKERFILE = (ROOT / "Dockerfile").read_text()
_REQUIREMENTS = (ROOT / "backend" / "requirements.txt").read_text()
_POLICY = (ROOT / "docs" / "SUPPLY_CHAIN_POLICY.md").read_text()

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def test_dockerfile_base_image_is_pinned_by_verified_manifest_digest():
    match = re.search(
        r"^FROM\s+python:3\.12\.14-slim-trixie@(sha256:[0-9a-f]{64})\s*$",
        _DOCKERFILE, re.MULTILINE,
    )
    assert match, "Dockerfile FROM line must pin python:3.12.14-slim-trixie by sha256 digest"
    assert _DIGEST_RE.fullmatch(match.group(1))


def test_dockerfile_does_not_run_a_blanket_apt_upgrade():
    assert "apt-get upgrade" not in _DOCKERFILE
    assert "apt-get update" in _DOCKERFILE


def test_dockerfile_uses_no_install_recommends_for_apt():
    assert "--no-install-recommends" in _DOCKERFILE


def test_dockerfile_installs_python_deps_with_hash_enforcement():
    assert re.search(r"pip install[^\n]*--require-hashes", _DOCKERFILE)
    assert "requirements.txt" in _DOCKERFILE


def test_runtime_requirements_are_hash_pinned_for_every_package():
    """Every top-level pinned requirement (``name==version``) must carry at
    least one ``--hash=sha256:...`` entry, or ``--require-hashes`` would
    refuse the whole install at build time."""
    lines = _REQUIREMENTS.splitlines()
    package_line_re = re.compile(r"^[A-Za-z0-9_.\-]+==[^\s;\\]+")
    packages = [i for i, line in enumerate(lines) if package_line_re.match(line)]
    assert packages, "expected at least one pinned package in requirements.txt"
    for index in packages:
        # Hash continuation lines for this package run until the next
        # unindented package line (or a "# via ..." comment / EOF).
        window = []
        for line in lines[index:index + 400]:
            if line.startswith("    --hash=sha256:"):
                window.append(line)
            elif window:
                break
        assert window, f"requirements.txt package at line {index + 1} has no --hash entries: {lines[index]!r}"


def test_supply_chain_policy_document_exists_and_states_the_authority_contract():
    assert "authoritative release artifact" in _POLICY.lower()
    assert "manifest" in _POLICY.lower() and "digest" in _POLICY.lower()
    assert "sha-<short7>" in _POLICY or "sha-" in _POLICY


def test_supply_chain_policy_does_not_claim_bit_identical_rebuilds():
    lowered = _POLICY.lower()
    assert "not assumed" in lowered or "not guarantee" in lowered or "does not claim" in lowered
    assert "bit-identical" in lowered or "reproducib" in lowered


def test_fork_image_workflow_remains_sha_only_immutable_publication():
    workflow = (ROOT / ".github" / "workflows" / "fork-image.yml").read_text()
    assert "type=sha,prefix=sha-,format=short" in workflow
    assert "latest=false" in workflow
    # The publish job's own runtime guard: refuse to proceed if the tag it
    # is about to push is not a sha- tag, or if it resolves to a mutable
    # latest/version tag.
    assert 'generated_tags[0]}" != "${IMAGE_NAME}:sha-"*' in workflow
    assert '":latest"' in workflow and '":v"' in workflow


def test_release_promotion_remains_digest_preserving_no_rebuild():
    workflow = (ROOT / ".github" / "workflows" / "release-promotion.yml").read_text()
    assert "imagetools create" in workflow
    assert re.search(r"docker\s+build\b(?!x)", workflow) is None
