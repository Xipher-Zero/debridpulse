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


# --- the bundled Usenet acquisition service (1.0.13) ----------------------
#
# The service runs on the image's own interpreter, so its Python runtime
# closure is part of the shipped runtime and must obey the same lock policy.
# A second install transaction would escape --require-hashes AND be free to
# replace packages the locked one already selected, leaving two contradictory
# package authorities in one environment.

_REQUIREMENTS_IN = (ROOT / "backend" / "requirements.in").read_text()

# What the service actually needs at runtime on Linux, and what it must never
# drag into the shipped image. Both taken from SABnzbd-5.1.3/requirements.txt.
_SERVICE_RUNTIME_PACKAGES = (
    "sabctools", "cheroot", "cherrypy", "feedparser", "configobj", "apprise",
    "guessit", "rebulk", "babelfish", "rarfile", "puremagic", "portend",
    "tempora", "zc-lockfile", "ct3", "sgmllib3k", "hachoir", "ujson", "orjson",
)
_SERVICE_TEST_ONLY_PACKAGES = (
    "selenium", "tavern", "tavalidate", "pyfakefs", "flaky", "pytest-httpbin",
    "pytest-httpserver", "black", "flask", "werkzeug", "xmltodict",
)


def _locked_packages(text):
    return {m.group(1).lower() for m in re.finditer(r"^([A-Za-z0-9_.\-]+)==", text, re.M)}


def test_the_image_runs_exactly_one_python_install_transaction():
    """One lock, one install. Two would be two package authorities."""
    installs = re.findall(r"pip install[^\n\\]*", _DOCKERFILE)
    real = [i for i in installs if "--dry-run" not in i]
    assert len(real) == 1, f"expected exactly one pip install transaction, found: {real}"
    assert "--require-hashes" in real[0]


def test_the_image_never_installs_the_services_own_requirements_file():
    """That file is unhashed, and it also carries the service's test tooling."""
    assert not re.search(r"pip install[^\n]*/app/usenet/requirements\.txt", _DOCKERFILE)
    assert not re.search(r"pip install(?![^\n]*--require-hashes)", _DOCKERFILE)


def test_the_service_runtime_closure_is_in_the_shipped_lock():
    locked = _locked_packages(_REQUIREMENTS)
    missing = [name for name in _SERVICE_RUNTIME_PACKAGES if name not in locked]
    assert not missing, f"the bundled service needs these at runtime, unlocked: {missing}"


def test_the_service_test_tooling_is_not_shipped_in_the_runtime_lock():
    locked = _locked_packages(_REQUIREMENTS)
    shipped = [name for name in _SERVICE_TEST_ONLY_PACKAGES if name in locked]
    assert not shipped, f"test-only packages leaked into the runtime image: {shipped}"


def test_the_service_runtime_closure_is_declared_in_the_canonical_input():
    """Not merely resolved by accident: the canonical input names them, so a
    later recompile cannot quietly drop the service's dependencies."""
    declared = _locked_packages(_REQUIREMENTS_IN)
    for name in ("sabctools", "cherrypy", "feedparser", "configobj", "apprise"):
        assert name in declared, f"{name} is not declared in requirements.in"


def test_the_service_source_tarball_stays_checksum_pinned():
    assert re.search(r"^ARG USENET_SERVICE_VERSION=\d+\.\d+\.\d+$", _DOCKERFILE, re.M)
    assert re.search(r"^ARG USENET_SERVICE_SHA256=[0-9a-f]{64}$", _DOCKERFILE, re.M)
    assert "sha256sum -c -" in _DOCKERFILE


def test_the_repair_and_licensing_material_is_retained():
    """par2 does the posting's verification/repair; unrar is required for the
    service to start acquiring, and its licence file must ship with it."""
    assert re.search(r"^\s*par2 \\$", _DOCKERFILE, re.M)
    assert re.search(r"^\s*unrar \\$", _DOCKERFILE, re.M)
    assert "path-include=/usr/share/doc/unrar/copyright" in _DOCKERFILE


def test_supply_chain_policy_document_exists_and_states_the_authority_contract():
    assert "authoritative release artifact" in _POLICY.lower()
    assert "manifest" in _POLICY.lower() and "digest" in _POLICY.lower()
    assert "sha-<40-char-git-sha>" in _POLICY


def test_supply_chain_policy_does_not_claim_bit_identical_rebuilds():
    lowered = _POLICY.lower()
    assert "not assumed" in lowered or "not guarantee" in lowered or "does not claim" in lowered
    assert "bit-identical" in lowered or "reproducib" in lowered


def _workflow(name: str) -> str:
    return (ROOT / ".github" / "workflows" / name).read_text()


def _step(workflow: str, name: str) -> str:
    """Text of one workflow step, from its ``- name:`` line to the next step."""
    marker = f"- name: {name}"
    assert workflow.count(marker) == 1, f"expected exactly one step named {name!r}"
    start = workflow.index(marker)
    following = workflow.find("\n      - name:", start + len(marker))
    return workflow[start:] if following == -1 else workflow[start:following]


def test_fork_image_workflow_publishes_only_the_full_sha_tag():
    workflow = _workflow("fork-image.yml")
    # Exactly one tag rule, and it is the full source SHA -- never a truncation.
    assert workflow.count("type=raw,value=sha-${{ github.sha }}") == 2
    assert "type=sha" not in workflow
    assert "format=short" not in workflow
    assert "GITHUB_SHA:0:7" not in workflow
    assert "latest=false" in workflow
    # The publish job's own runtime guard: the one generated tag must equal the
    # full-SHA candidate ref, and may never be a mutable latest/version tag.
    assert '"${generated_tags[0]}" != "${IMAGE_NAME}:sha-${GITHUB_SHA}"' in workflow
    assert "^[0-9a-f]{40}$" in workflow
    assert '":latest"' in workflow and '":v"' in workflow


def test_fork_image_checks_for_an_existing_candidate_before_publishing():
    workflow = _workflow("fork-image.yml")
    check = workflow.index("- name: Check for an existing candidate for this source SHA")
    build = workflow.index("- name: Build and publish")
    assert check < build, "the existence check must precede the build/push step"

    existing = _step(workflow, "Check for an existing candidate for this source SHA")
    assert "docker buildx imagetools inspect" in existing
    # An inconclusive registry answer must never be read as "absent".
    assert "Unable to determine whether" in existing
    assert "denied|unauthorized|forbidden" in existing

    publish = _step(workflow, "Build and publish")
    assert "if: steps.existing.outputs.exists != 'true'" in publish
    assert "push: true" in publish
    # Exactly one push in the whole workflow, and it is the guarded step.
    assert workflow.count("push: true") == 1


def test_fork_image_reuses_a_valid_existing_candidate_without_overwriting_it():
    workflow = _workflow("fork-image.yml")
    existing = _step(workflow, "Check for an existing candidate for this source SHA")
    assert 'echo "exists=true" >> "$GITHUB_OUTPUT"' in existing
    assert 'echo "digest=$digest" >> "$GITHUB_OUTPUT"' in existing
    assert "will be reused unchanged" in existing
    # A reuse path performs no build and no push of its own.
    assert "build-push-action" not in existing
    assert "imagetools create" not in existing
    assert "docker push" not in existing

    selected = _step(workflow, "Select the immutable candidate digest")
    assert 'if [ "$EXISTING" = "true" ]' in selected
    assert "An existing candidate was reused but a build also reported a digest" in selected

    # Later verification is keyed on the selected digest, for both paths.
    verify = _step(workflow, "Verify candidate digest and OCI identity")
    assert "steps.candidate.outputs.digest" in verify
    assert "steps.publish.outputs.digest" not in verify


def test_fork_image_fails_closed_when_an_existing_tag_records_another_revision():
    workflow = _workflow("fork-image.yml")
    existing = _step(workflow, "Check for an existing candidate for this source SHA")
    assert 'existing_revision" != "$GITHUB_SHA"' in existing
    assert "refusing to reuse or overwrite" in existing
    revision_check = existing.index('existing_revision" != "$GITHUB_SHA"')
    assert "exit 1" in existing[revision_check:revision_check + 400]
    # Reuse still requires both platform children to be present.
    assert 'for arch in amd64 arm64' in existing


def test_fork_image_serializes_runs_for_the_same_source_sha():
    workflow = _workflow("fork-image.yml")
    head = workflow[:workflow.index("\njobs:")]
    assert "concurrency:" in head
    assert "group: fork-image-${{ github.sha }}" in head
    # Never cancel a publication that is already in flight.
    assert "cancel-in-progress: false" in head
    assert "cancel-in-progress: true" not in workflow


def test_no_candidate_consumer_or_promotion_step_truncates_the_source_sha():
    for name in (
        "fork-image.yml",
        "container-security.yml",
        "candidate-runtime-qualification.yml",
        "release-promotion.yml",
    ):
        text = _workflow(name)
        assert "GITHUB_SHA:0:7" not in text, name
        assert "candidate_sha:0:7" not in text, name
        assert "format=short" not in text, name
    for name in ("container-security.yml", "candidate-runtime-qualification.yml"):
        assert 'source_ref="${IMAGE_NAME}:sha-${GITHUB_SHA}"' in _workflow(name), name
    promotion = _workflow("release-promotion.yml")
    assert 'source_tag=sha-${candidate_sha}' in promotion
    assert "^[0-9a-f]{40}$" in promotion


def test_supply_chain_policy_states_the_write_once_full_sha_rule():
    lowered = _POLICY.lower()
    assert "write-once" in lowered
    assert "sha-<40-char-git-sha>" in _POLICY
    assert "sha-<short7>" not in _POLICY
    assert "reuses" in lowered and "fails closed" in lowered
    assert "new candidate requires a new source sha" in lowered
    assert "cancel-in-progress: false" in _POLICY
    # The policy must not both promise a write-once tag and treat a same-SHA
    # rebuild as a fresh candidate.
    assert "a rebuild is a new candidate" not in lowered


def test_release_promotion_remains_digest_preserving_no_rebuild():
    workflow = (ROOT / ".github" / "workflows" / "release-promotion.yml").read_text()
    assert "imagetools create" in workflow
    assert re.search(r"docker\s+build\b(?!x)", workflow) is None
