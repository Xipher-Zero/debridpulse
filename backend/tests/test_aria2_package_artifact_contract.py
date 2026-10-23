"""DebridPulse's aria2 is one immutable supply-chain artifact.

Debian's aria2 source, rebuilt by ONE script with exactly the repo-owned
changes in packaging/aria2/ (the CONNECT exact-read patch and BitTorrent
compiled out), versioned +dp2, qualified against the installed binary's own
feature report, built natively per architecture and published write-once to
ghcr.io/xipher-zero/debridpulse-aria2 (docs/SUPPLY_CHAIN_POLICY.md 4a).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PACKAGING = ROOT / "packaging" / "aria2"
WORKFLOW = ROOT / ".github" / "workflows" / "aria2-package.yml"


def _workflow() -> dict:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML reads the bare ``on`` key as boolean True.
    document["on"] = document.pop(True, document.get("on"))
    return document


def test_one_script_owns_the_patch_set_configuration_and_version_suffix() -> None:
    script = (PACKAGING / "build-package.sh").read_text(encoding="utf-8")
    assert "DP_SUFFIX=dp2" in script
    assert "apt-get source --only-source" in script
    assert "echo connect-tunnel-exact-read.patch >> debian/patches/series" in script
    assert 'patch -p1 --forward --fuzz=0 < "$here/disable-bittorrent.rules.patch"' in script
    assert "dpkg-buildpackage -b -uc -us" in script
    rules_patch = (PACKAGING / "disable-bittorrent.rules.patch").read_text(encoding="utf-8")
    assert "+\t\t--disable-bittorrent" in rules_patch
    # BitTorrent is the ONE feature removed: no other configure flag changes.
    added = [line for line in rules_patch.splitlines() if line.startswith("+\t")]
    assert added == ["+\t\t--enable-libaria2 \\", "+\t\t--disable-bittorrent"]
    # Neither consumer restates what the script owns.
    dockerfile = (PACKAGING / "Dockerfile").read_text(encoding="utf-8")
    assert "build-package.sh" in dockerfile
    assert "debian/patches/series" not in dockerfile and "--disable-bittorrent" not in dockerfile


def test_the_feature_set_is_qualified_from_the_installed_binary() -> None:
    verify = (PACKAGING / "verify-features.sh").read_text(encoding="utf-8")
    assert "aria2c --version" in verify and "Enabled Features" in verify
    assert '*", BitTorrent,"*' in verify
    assert "for feature in HTTPS SFTP Metalink" in verify
    assert "dpkg-query -W -f='${Version}'" in verify
    dockerfile = (PACKAGING / "Dockerfile").read_text(encoding="utf-8")
    assert 'bash /dp/aria2/verify-features.sh "${ARIA2_PACKAGE_VERSION}"' in dockerfile
    # A package carrier, not a runtime: the published stage is scratch.
    stages = re.findall(r"^FROM (\S+)", dockerfile, re.M)
    assert stages[-1] == "scratch"
    assert re.fullmatch(r"python:3\.12\.14-slim-trixie@sha256:[0-9a-f]{64}", stages[0])
    versions = set(re.findall(r"^ARG ARIA2_PACKAGE_VERSION=(\S+)$", dockerfile, re.M))
    assert versions == {"1.37.0+debian-3+dp2"}


def test_the_artifact_is_built_natively_per_architecture_and_published_write_once() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert jobs["build-amd64"]["runs-on"] == "ubuntu-24.04"
    assert jobs["build-arm64"]["runs-on"] == "ubuntu-24.04-arm"
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "setup-qemu" not in text  # never emulated
    for name in ("build-amd64", "build-arm64"):
        build = next(step for step in jobs[name]["steps"] if step.get("id") == "build")
        assert build["with"]["file"] == "packaging/aria2/Dockerfile"
        assert "push-by-digest=true" in build["with"]["outputs"]
        assert build["with"]["provenance"] == "mode=max" and build["with"]["sbom"] is True
    existing = next(step for step in jobs["identity"]["steps"] if step.get("id") == "existing")["run"]
    # A published version is never rebuilt from different inputs, and the
    # publish job re-checks with its write credentials before creating the tag.
    assert "io.debridpulse.aria2.inputs" in existing and "bump ARIA2_PACKAGE_VERSION" in existing
    create = next(step for step in jobs["publish"]["steps"] if "imagetools create" in step.get("run", ""))["run"]
    assert create.index("refusing to overwrite") < create.index("imagetools create")
    assert jobs["publish"]["if"] == "github.event_name != 'pull_request' && needs.identity.outputs.exists != 'true'"
    assert set(workflow["on"]["push"]["paths"]) == {"packaging/aria2/**", ".github/workflows/aria2-package.yml"}
    assert all(re.search(r"@[0-9a-f]{40}", line) for line in re.findall(r"uses: \S+", text))


_FAKE_DOCKER = """#!/usr/bin/env python3
# docker buildx imagetools inspect REF, written in many pieces as buildx does:
# its listing (bounded at 1 MiB) is far larger than a pipe buffer, so a reader
# that leaves after the Digest line always breaks the pipe, with no timing
# involved. Like the docker CLI when its buildx plugin dies of SIGPIPE, exit 255
# when the reader is gone. MODE: ok | fail | nodigest
import os, sys
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "fail":
    sys.stderr.write("ERROR: failed to fetch\\n"); sys.exit(1)
out = sys.stdout.buffer
try:
    out.write(b"Name: x\\nMediaType: application/vnd.oci.image.index.v1+json\\n")
    if mode == "ok":
        out.write(b"Digest: sha256:" + b"a" * 64 + b"\\n")
    out.flush()
    for _ in range(256):
        out.write(b"  Name: x@sha256:" + b"b" * 64 + b"\\n" * 4000); out.flush()
except BrokenPipeError:
    os._exit(255)
"""


def _digest_read(tmp_path, script: str, mode: str):
    import os
    import subprocess
    fake = tmp_path / "docker"
    fake.write_text(_FAKE_DOCKER, encoding="utf-8")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "REF": "ghcr.io/example/package:tag",
           "FAKE_MODE": mode}
    return subprocess.run(["bash", "-c", "set -euo pipefail\n" + script + '\necho "digest=$digest"'],
                          env=env, capture_output=True, text=True, timeout=30)


def test_the_published_digest_is_read_without_breaking_the_pipe_under_docker(tmp_path) -> None:
    """Post-1.0.13: `imagetools inspect | awk '... exit'` let awk leave the pipe
    while docker was still writing; buildx died of SIGPIPE and docker exited 255
    under pipefail, failing the run exactly when the package already existed.
    The workflow's own lines are run against a docker that streams its listing."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'imagetools inspect "$REF" | awk' not in text
    lines = [line.strip() for line in text.splitlines()]
    start = [i for i, line in enumerate(lines) if line == 'inspected="$(docker buildx imagetools inspect "$REF")"']
    assert len(start) == 2                       # the existing-image check and the publish summary
    for index in start:
        read = "\n".join(lines[index:index + 3])
        assert read.endswith('{ echo "Could not read the manifest digest of $REF"; exit 1; }')
        done = _digest_read(tmp_path, read, "ok")
        assert done.returncode == 0 and done.stdout.strip() == "digest=sha256:" + "a" * 64, done
        for mode in ("fail", "nodigest"):         # never an empty or guessed digest
            assert _digest_read(tmp_path, read, mode).returncode != 0, mode
    # The former single pipeline, against the same docker, is the defect itself.
    former = """digest="$(docker buildx imagetools inspect "$REF" | awk '/^Digest:/ {print $2; exit}')\""""
    assert _digest_read(tmp_path, former, "ok").returncode == 255
