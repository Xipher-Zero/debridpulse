"""WS6: the hosted desktop installer pipeline's contract, read from the
workflow, the installer script and the recipes as committed.

* The Windows installer requires the Microsoft Visual C++ x64 Redistributable
  14.51+ before it changes anything and never carries it.
* The handover runs only after every build and smoke check, and publishes only
  for the one commit an explicit approval names.
* RARLAB's RAR package is never cached or handed over as source.
* The checksum file covers the manifest and every delivered file, not itself.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "desktop-artifacts.yml").read_text(encoding="utf-8"))
JOBS = WORKFLOW["jobs"]
NSIS = (ROOT / "packaging" / "desktop" / "windows" / "installer.nsi").read_text(encoding="utf-8")


def _steps(job: str) -> list[dict]:
    return JOBS[job]["steps"]


def _run(job: str) -> str:
    return "\n".join(step.get("run", "") for step in _steps(job))


def test_the_installer_requires_the_redistributable_first_and_never_carries_it():
    assert "vc_redist" not in NSIS.replace("aka.ms/vc14/vc_redist.x64.exe", "")
    assert re.search(r"(?m)^\s*!define VCREDIST_MIN_MAJOR 14$", NSIS)
    assert re.search(r"(?m)^\s*!define VCREDIST_MIN_MINOR 51\b", NSIS)
    on_init = NSIS[NSIS.index("Function .onInit"):NSIS.index("FunctionEnd", NSIS.index("Function .onInit"))]
    assert on_init.index("Call RequireVcRedist") < on_init.index("Call TakeDataRootLock")
    require = NSIS[NSIS.index("Function RequireVcRedist"):NSIS.index("FunctionEnd", NSIS.index("Function RequireVcRedist"))]
    silent = require[require.index("${If} ${Silent}"):]
    assert silent.index("SetErrorLevel 3") < silent.index("Quit")             # silent: report, exit 3
    assert "MB_YESNOCANCEL" in require and "IDNO vcCheck" in require          # interactive: check again
    assert 'ExecShell "open" "${VCREDIST_DOWNLOAD}"' in require                # Microsoft's own download
    assert '!define VCREDIST_DOWNLOAD "https://aka.ms/vc14/vc_redist.x64.exe"' in NSIS
    assert "MicrosoftEdgeWebview2Setup.exe" in NSIS                            # WebView2 path kept
    builder = (ROOT / "packaging" / "desktop" / "windows" / "build-installer.sh").read_text(encoding="utf-8")
    assert re.search(r'(?m)^cp "\$prereq/MicrosoftEdgeWebview2Setup\.exe" "\$stage/prerequisites/"$', builder)
    assert "vc_redist" not in builder.replace('rm -f "$stage/prerequisites/vc_redist.x64.exe"', "")


def _windows_step(prefix: str) -> dict:
    return next(step for step in _steps("desktop") if step.get("name", "").startswith(prefix))


def test_windows_builds_its_installer_with_the_pinned_nsis_and_install_tests_that_exe_on_the_same_runner():
    pins = (ROOT / "packaging" / "desktop" / "windows" / "official.sha256").read_text(encoding="utf-8")
    assert re.search(r"(?m)^c7d27f780ddb6cffb4730138cd1591e841f4b7edb155856901cdf5f214394fa1  nsis-3\.11\.zip  "
                     r"https://downloads\.sourceforge\.net/project/nsis/NSIS%203/3\.11/nsis-3\.11\.zip$", pins)
    build = _windows_step("The Windows installer, on this runner")
    assert build["if"] == "runner.os == 'Windows'"
    assert "sha256sum -c -" in build["run"] and 'export MAKENSIS="$RUNNER_TEMP/nsis/nsis-3.11/makensis.exe"' in build["run"]
    assert 'packaging/desktop/windows/build-installer.sh "$RUNNER_TEMP/layout"' in build["run"]
    assert "TOOLCHAIN.txt" in build["run"]
    install = _windows_step("The same EXE installed silently")
    names = [step.get("name", "") for step in _steps("desktop")]
    assert names.index(build["name"]) < names.index(install["name"])           # one job, one runner
    run = install["run"]
    assert "desktop-build\\windows\\DebridPulse-*-windows-x64.exe" in run       # the EXE built just above
    assert "https://aka.ms/vc14/vc_redist.x64.exe" in run and "$env:RUNNER_TEMP\\vc_redist.x64.exe" in run
    assert run.index("$refused.ExitCode -ne 3") < run.index("Invoke-WebRequest") < run.index(
        "$install = Start-Process -FilePath $exe")
    assert "$failed += 'uninstall'" in run and "$failed += 'Usenet check'" in run
    for job, body in JOBS.items():                                             # nowhere else, never kept
        assert "vc_redist" not in yaml.safe_dump(body) or job == "desktop", job
    builder = (ROOT / "packaging" / "desktop" / "windows" / "build-installer.sh").read_text(encoding="utf-8")
    assert 'if [ -n "${MAKENSIS:-}" ]; then' in builder


def test_caches_carry_only_shareable_inputs_and_no_finished_bytes():
    saves = [(job, step) for job, body in JOBS.items() for step in body["steps"]
             if "actions/cache/save" in step.get("uses", "")]
    assert [job for job, _ in saves] == ["inputs"]
    for job, body in JOBS.items():
        for step in body["steps"]:
            if "actions/cache" in step.get("uses", ""):
                assert "desktop-build" not in step["with"]["path"] and "desktop-build" not in step["with"]["key"], job


APPROVED = "vars.DESKTOP_INSTALLERS_UPLOAD_SHA == github.sha"


def test_the_handover_follows_every_build_and_smoke_check_and_publishes_only_an_approved_commit():
    handover = JOBS["handover"]
    assert set(handover["needs"]) == {"inputs", "desktop"}
    assert handover["if"] == f"success() && {APPROVED}"
    uploads = [(job, step) for job, body in JOBS.items() for step in body["steps"]
               if "actions/upload-artifact" in step.get("uses", "")]
    assert [(job, step["with"]["retention-days"]) for job, step in uploads] == [("desktop", 1), ("handover", 14)]
    intermediate, final = uploads[0][1], uploads[1][1]
    assert intermediate["if"] == f"success() && {APPROVED}"
    assert intermediate["with"]["name"] == "desktop-intermediate-${{ matrix.name }}-${{ github.sha }}"
    assert final["if"] == APPROVED and final["with"]["name"] == "desktop-installers-${{ github.sha }}"
    download = next(step for step in _steps("handover") if "actions/download-artifact" in step.get("uses", ""))
    assert download["with"]["pattern"] == "desktop-intermediate-*-${{ github.sha }}"
    cleanup = JOBS["intermediates"]
    assert set(cleanup["needs"]) == {"desktop", "handover"} and cleanup["if"] == f"always() && {APPROVED}"
    assert cleanup["permissions"] == {"actions": "write"} and WORKFLOW["permissions"] == {"contents": "read"}
    assert 'startswith("desktop-intermediate-")' in _run("intermediates")
    assert "DESKTOP_INSTALLERS_UPLOAD:" not in yaml.safe_dump(WORKFLOW)
    handover_run = _run("handover")
    assert '[ "$(cat "$dir/COMMIT")" = "$GITHUB_SHA" ]' in handover_run
    assert '[ "$(cat "$dir/RUN")" = "$GITHUB_RUN_ID" ]' in handover_run


def _plan(variable: str, sha: str, smoke_passed: dict[str, bool]) -> dict:
    """What runs, by GitHub's documented if-semantics, for the exact conditions asserted above: a
    step's success() holds only when every earlier step of its job passed; a job's success() only when
    every job it needs passed; always() regardless."""
    approved = variable == sha
    return {"intermediate artifacts": sorted(name for name, passed in smoke_passed.items() if passed and approved),
            "handover and its artifact": approved and all(smoke_passed.values()),
            "intermediates deleted at the end": approved}


def test_unset_mismatched_and_matching_approvals_with_and_without_a_failed_smoke_check():
    sha = "55d9fbceb5fc95341a5ce61c3425f76cab6d0845"
    passing = {"macos-arm64": True, "windows-x64": True, "linux-x64": True}
    failing = {**passing, "windows-x64": False}
    nothing = {"intermediate artifacts": [], "handover and its artifact": False, "intermediates deleted at the end": False}
    assert _plan("", sha, passing) == nothing                                  # unset
    assert _plan("0" * 40, sha, passing) == nothing                            # another commit
    assert _plan(sha[:12], sha, passing) == nothing                            # not the FULL sha
    assert _plan(sha, sha, passing) == {"intermediate artifacts": ["linux-x64", "macos-arm64", "windows-x64"],
                                        "handover and its artifact": True, "intermediates deleted at the end": True}
    assert _plan(sha, sha, failing) == {"intermediate artifacts": ["linux-x64", "macos-arm64"],
                                        "handover and its artifact": False, "intermediates deleted at the end": True}


def test_the_rar_package_is_never_cached_or_handed_over_as_source():
    prepare = (ROOT / "packaging" / "macos-tools" / "prepare-sources.sh").read_text(encoding="utf-8")
    assert 'case "$name" in rar*) continue ;; esac' in prepare
    build = (ROOT / "packaging" / "macos-tools" / "build-macos.sh").read_text(encoding="utf-8")
    assert re.search(r'rar\*\) curl -sSfL -o "\$work/\$name" "\$url"', build)
    assert 'tar -xzf "$work/rarmacos-arm-723.tar.gz"' in build
    handover = _run("handover")
    assert "cp desktop-inputs/macos-sources/*" not in handover                  # a source-only selection
    assert 'compgen -G "$src/macos-and-flatpak-tools/rar*"' in handover
    assert "packaging/desktop/python-sources.py" in handover


def test_the_inputs_cache_and_the_checksums_cover_the_manifest_and_every_delivered_file():
    inputs = [step for step in _steps("inputs") if "actions/cache/save" in step.get("uses", "")]
    assert len(inputs) == 1
    assert inputs[0]["with"]["path"].split() == ["desktop-inputs/icons", "desktop-inputs/macos-sources",
                                                 "desktop-inputs/wheels", "desktop-inputs/windows-tools",
                                                 "desktop-inputs/sabctools-source"]
    step = next(s for s in _steps("handover") if s.get("name", "").startswith("The manifest, then SHA256SUMS"))
    run = step["run"]
    assert "! -name SHA256SUMS ! -name MANIFEST.md" in run
    assert run.index("> MANIFEST.md") < run.index("> SHA256SUMS")
    assert "sha256sum MANIFEST.md; } > SHA256SUMS" in run and "sha256sum -c SHA256SUMS" in run
