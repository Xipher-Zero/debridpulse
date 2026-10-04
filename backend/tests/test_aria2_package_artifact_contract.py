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
