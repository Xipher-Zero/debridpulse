"""DebridPulse's FFmpeg is one immutable supply-chain artifact.

Debian's ffmpeg source (Debian's own patches, nothing added), configured by ONE
script for local stream copy only -- file/pipe protocols, the finalization
containers and subtitle formats, parsers, bitstream filters and native
decoders; no encoder, device, network protocol or external library -- packaged
as Debian's `ffmpeg` +dp1 with its Debian source recorded, qualified against
the installed binaries' own reports, built natively per architecture and
published write-once to ghcr.io/xipher-zero/debridpulse-ffmpeg
(docs/SUPPLY_CHAIN_POLICY.md 4b).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PACKAGING = ROOT / "packaging" / "ffmpeg"
WORKFLOW = ROOT / ".github" / "workflows" / "ffmpeg-package.yml"


def _workflow() -> dict:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML reads the bare ``on`` key as boolean True.
    document["on"] = document.pop(True, document.get("on"))
    return document


def _configure(script: str) -> list[str]:
    block = script[script.index("./configure "):]
    return block[:block.index("\ngrep ")].replace("\\\n", " ").split()


def test_one_script_owns_the_source_configuration_package_identity_and_suffix() -> None:
    script = (PACKAGING / "build-package.sh").read_text(encoding="utf-8")
    assert "DP_SUFFIX=dp1" in script
    assert "apt-get source --only-source" in script
    flags = _configure(script)
    # Everything is off, then exactly the local stream-copy surface is on.
    for flag in ("--disable-everything", "--disable-autodetect", "--disable-network", "--disable-ffplay",
                 "--disable-avdevice", "--enable-protocol=file,pipe", "--enable-bsfs"):
        assert flag in flags, flag
    assert not any(flag.startswith(("--enable-encoder", "--enable-gpl", "--enable-nonfree", "--enable-lib",
                                    "--enable-indev", "--enable-outdev", "--enable-filter")) for flag in flags)
    enabled = {flag.split("=", 1)[0]: set(flag.split("=", 1)[1].split(",")) for flag in flags if "=" in flag
               and flag.startswith("--enable-")}
    assert {"mov", "matroska", "mpegts", "flv", "webvtt", "srt", "ass"} <= enabled["--enable-demuxer"]
    assert {"mp4", "ipod", "mov", "webm", "matroska", "mp3", "ogg", "opus", "flac"} <= enabled["--enable-muxer"]
    # The build itself refuses a GPL or network-capable configuration.
    assert "grep -qx '#define CONFIG_GPL 0' config.h && grep -qx '#define CONFIG_NETWORK 0' config.h" in script
    # Debian's package identity, so dpkg, the SBOM and scanners resolve it.
    assert "Package: ffmpeg\nSource: ffmpeg (${source_version})\nVersion: ${version}" in script
    assert "dpkg-shlibdeps" in script and "debian/copyright" in script
    assert "dpkg-deb --root-owner-group" in script and 'SOURCE_DATE_EPOCH="$(dpkg-parsechangelog -STimestamp)"' in script
    # Neither consumer restates what the script owns.
    dockerfile = (PACKAGING / "Dockerfile").read_text(encoding="utf-8")
    assert "build-package.sh" in dockerfile and "--disable-everything" not in dockerfile


def test_the_capability_set_is_qualified_from_the_installed_binaries() -> None:
    verify = (PACKAGING / "verify-features.sh").read_text(encoding="utf-8")
    assert "dpkg-query -W -f='${Version}' ffmpeg" in verify
    assert "${source:Package} ${source:Version}" in verify
    assert 'if [ "$protocols" != " file pipe| file pipe" ]' in verify
    assert "-encoders" in verify and "ffmpeg has encoders" in verify
    assert "aac_adtstoasc" in verify and "GNU Lesser General Public" in verify and "ldd" in verify
    dockerfile = (PACKAGING / "Dockerfile").read_text(encoding="utf-8")
    assert 'bash /dp/ffmpeg/verify-features.sh "${FFMPEG_PACKAGE_VERSION}"' in dockerfile
    # A package carrier, not a runtime: the published stage is scratch.
    stages = re.findall(r"^FROM (\S+)", dockerfile, re.M)
    assert stages[-1] == "scratch"
    assert re.fullmatch(r"python:3\.12\.14-slim-trixie@sha256:[0-9a-f]{64}", stages[0])
    assert set(re.findall(r"^ARG FFMPEG_SOURCE_VERSION=(\S+)$", dockerfile, re.M)) == {"7:7.1.5-0+deb13u1"}
    assert set(re.findall(r"^ARG FFMPEG_PACKAGE_VERSION=(\S+)$", dockerfile, re.M)) == {"7:7.1.5-0+deb13u1+dp1"}
    assert 'org.opencontainers.image.licenses="LGPL-2.1-or-later"' in dockerfile


def test_the_package_image_is_built_natively_and_published_write_once() -> None:
    workflow = _workflow()
    on = workflow["on"]
    assert on["push"]["paths"] == ["packaging/ffmpeg/**", ".github/workflows/ffmpeg-package.yml"]
    assert workflow["env"]["IMAGE_NAME"] == "ghcr.io/xipher-zero/debridpulse-ffmpeg"
    jobs = workflow["jobs"]
    assert jobs["build-amd64"]["runs-on"] == "ubuntu-24.04"
    assert jobs["build-arm64"]["runs-on"] == "ubuntu-24.04-arm"
    for job in ("build-amd64", "build-arm64"):
        build = next(step for step in jobs[job]["steps"] if step.get("id") == "build")
        assert build["with"]["file"] == "packaging/ffmpeg/Dockerfile"
        assert build["with"]["provenance"] == "mode=max" and build["with"]["sbom"] is True
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "setup-qemu" not in text  # never emulated
    # The epoch never reaches a tag; inputs bind a version to exact bytes.
    assert 'tag="${version#*:}"' in text and 'echo "tag=${tag//+/-}"' in text
    assert "git ls-files -z packaging/ffmpeg" in text and "io.debridpulse.ffmpeg.inputs" in text
    assert "refusing to overwrite it" in text
    assert workflow["concurrency"] == {"group": "ffmpeg-package", "cancel-in-progress": False}
    # Every action is pinned by commit, like every other workflow.
    for uses in re.findall(r"uses: (\S+)", text):
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", uses), uses


def test_the_image_and_the_runtime_layer_install_the_one_published_package() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    (image,) = re.findall(r"^FROM (ghcr\.io/xipher-zero/debridpulse-ffmpeg@sha256:[0-9a-f]{64}) AS ffmpeg-packages$",
                          dockerfile, re.M)
    assert "ARG FFMPEG_PACKAGE_VERSION=7:7.1.5-0+deb13u1+dp1" in dockerfile
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    # The runtime layer takes the package from the digest the Dockerfile pins,
    # verifies it as the image does, and never installs the archive's FFmpeg
    # (or any other finalizer).
    assert "debridpulse-ffmpeg@sha256" in workflow.split("AS ffmpeg-packages")[0]
    assert '"$RUNNER_TEMP"/ffmpeg/ffmpeg_*.deb' in workflow
    assert 'bash "$GITHUB_WORKSPACE/packaging/ffmpeg/verify-features.sh" "$FFMPEG_PACKAGE_VERSION"' in workflow
    assert not re.search(r"apt-get install[^\n]*\s(ffmpeg|mkvtoolnix)(\s|$)", workflow, re.M)
    assert image not in workflow  # read from the Dockerfile, never restated


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
