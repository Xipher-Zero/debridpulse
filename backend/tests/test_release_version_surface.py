"""Final release version surface contract."""

import json
import os
import re
import runpy
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")




def test_release_container_metadata_uses_authoritative_version_file() -> None:
    workflow = read(ROOT / ".github" / "workflows" / "fork-image.yml")
    dockerfile = read(ROOT / "Dockerfile")

    assert 'version=$(tr -d \'\\r\\n\' < VERSION)' in workflow
    assert "org.opencontainers.image.version=${{ steps.version.outputs.version }}" in workflow
    assert "APP_VERSION=${{ steps.version.outputs.version }}" in workflow
    assert 'ARG APP_VERSION=unknown' in dockerfile
    assert 'org.opencontainers.image.version="${APP_VERSION}"' in dockerfile

    # The release branch is derived from the one authoritative version owner
    # (VERSION), never restated here, so this stays correct across releases and
    # actually guards the branch the current release is developed on.
    # The guarded set is exactly the qualification set Release Promotion
    # requires, so a branch cannot be opened for development while one of the
    # gates that must pass before promotion silently ignores it.
    release_branch = read(ROOT / "VERSION").strip()
    for workflow_name in (
        "tests.yml",
        "browser-runtime.yml",
        "codeql.yml",
        "container-security.yml",
        "candidate-runtime-qualification.yml",
        "fork-image.yml",
    ):
        workflow_text = read(ROOT / ".github" / "workflows" / workflow_name)
        assert f"'{release_branch}'" in workflow_text, (
            f"{workflow_name} does not gate the current release branch {release_branch}"
        )

    # Fork Image is now immutable-only. Mutable aliases are owned by the
    # exact-SHA Release Promotion gate after all independent qualifiers pass.
    promotion = read(ROOT / ".github" / "workflows" / "release-promotion.yml")
    assert "type=raw,value=latest" not in workflow
    assert "type=ref,event=tag" not in workflow
    assert 'target_tag="latest"' in promotion
    assert 'target_tag="$GITHUB_REF_NAME"' in promotion
    assert '"Fork Image"' in promotion


# --- release.py: the GitHub Release body is exactly the version's own entry ---

RELEASE = ROOT / "release.py"
extract_entry = runpy.run_path(str(RELEASE), run_name="release")["extract_entry"]

SAMPLE = """# Changelog

## [2.0.1] — Development

2.0.1 is in development. It once said `## [2.0.0]` in passing.

## [2.0.0] — A release

### [2.0.0] notes
- the one change

## [2.0.00] — A different version

- not part of 2.0.0
"""


def test_only_the_selected_versions_own_entry_is_extracted() -> None:
    body = extract_entry(SAMPLE, "2.0.0")
    assert body == "## [2.0.0] — A release\n\n### [2.0.0] notes\n- the one change"
    # a heading quoted in another entry's text, a deeper heading, a neighbouring
    # Development entry and a longer version starting the same never leak in
    assert "in passing" not in body and "Development" not in body and "2.0.00" not in body
    assert extract_entry(SAMPLE, "2.0") is None
    assert extract_entry(SAMPLE, "9.9.9") is None


def test_every_real_entry_is_extracted_from_its_own_heading_to_the_next() -> None:
    changelog = read(ROOT / "CHANGELOG.md")
    headings = list(re.finditer(r"^## \[([^\]]+)\]", changelog, re.M))
    assert headings
    first = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(changelog)
        first.setdefault(heading.group(1), changelog[heading.start():end].strip())
    # (a few inherited upstream versions are headed twice; the newer, first one is the release)
    for version, entry in first.items():
        assert extract_entry(changelog, version) == entry, version


def _fake_curl(tmp_path: Path, responses: dict[str, tuple[int, dict]]) -> dict:
    """A curl stand-in on PATH: it records each call (method, URL, stdin) and
    answers from ``responses`` by method (with the HTTP status line when -w asks for it)."""
    log = tmp_path / "calls.jsonl"
    (tmp_path / "responses.json").write_text(json.dumps(responses))
    script = tmp_path / "bin" / "curl"
    script.parent.mkdir()
    script.write_text(f"""#!{sys.executable}
import json, sys
args = sys.argv[1:]
method = args[args.index("-X") + 1] if "-X" in args else "GET"
data = sys.stdin.read() if "@-" in args else (args[args.index("-d") + 1] if "-d" in args else None)
with open({str(log)!r}, "a") as f:
    f.write(json.dumps({{"method": method, "url": args[-1], "stdin": data, "argv_bytes": sum(len(a.encode()) for a in args)}}) + "\\n")
status, answer = json.load(open({str(tmp_path / "responses.json")!r}))[method]
sys.stdout.write(json.dumps(answer) + ("\\n" + str(status) if "-w" in args else ""))
""")
    script.chmod(0o755)
    env = {**os.environ, "PATH": f"{script.parent}{os.pathsep}{os.environ['PATH']}", "GITHUB_PAT": "test-token"}
    return {"env": env, "log": log}


def _release(tmp_path: Path, changelog: str, version: str, responses: dict) -> tuple[subprocess.CompletedProcess, list[dict]]:
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    fake = _fake_curl(tmp_path, responses)
    run = subprocess.run([sys.executable, str(RELEASE), version], cwd=tmp_path, env=fake["env"],
                         capture_output=True, text=True, timeout=60)
    calls = [json.loads(line) for line in fake["log"].read_text().splitlines()] if fake["log"].exists() else []
    return run, calls


CREATED = {"GET": (404, {"message": "Not Found"}), "POST": (201, {"html_url": "https://example.invalid/r/1"})}


def test_a_release_is_created_from_its_whole_entry_sent_through_stdin(tmp_path) -> None:
    # Large and non-ASCII: the JSON payload exceeds the 131072-byte single
    # argument limit, so it must not travel on curl's command line.
    long_line = "- " + "Überprüfung — " * 8000
    changelog = f"# Changelog\n\n## [3.0.0] — Big\n\n{long_line}\n\n## [2.9.9] — Older\n\n- old\n"
    run, calls = _release(tmp_path, changelog, "v3.0.0", CREATED)
    assert run.returncode == 0, run.stdout + run.stderr
    assert [call["method"] for call in calls] == ["GET", "POST"]
    sent = json.loads(calls[1]["stdin"])
    assert sent["tag_name"] == "v3.0.0"
    assert sent["body"] == f"## [3.0.0] — Big\n\n{long_line}".strip()
    assert len(calls[1]["stdin"].encode()) > 131072 > calls[1]["argv_bytes"]
    assert "erstellt: https://example.invalid/r/1" in run.stdout


def test_an_existing_release_is_updated_in_place(tmp_path) -> None:
    run, calls = _release(tmp_path, "## [3.0.0] — R\n\n- x\n", "3.0.0",
                          {"GET": (200, {"id": 42}), "PATCH": (200, {"html_url": "https://example.invalid/r/42"})})
    assert run.returncode == 0, run.stdout
    assert [(call["method"], call["url"].rsplit("/", 1)[-1]) for call in calls] == [("GET", "v3.0.0"), ("PATCH", "42")]


def test_a_development_entry_and_an_oversized_body_are_refused_before_anything_is_sent(tmp_path) -> None:
    for name in ("dev", "fits", "over"):
        (tmp_path / name).mkdir()
    development, calls = _release(tmp_path / "dev", "## [3.1.0] — Development\n\n- planned\n", "3.1.0", CREATED)
    assert development.returncode == 1 and "Development" in development.stdout and calls == []

    limit = runpy.run_path(str(RELEASE), run_name="release")["MAX_BODY_CHARS"]
    assert limit == 125_000
    heading = "## [3.0.0] — Too big\n\n"
    exactly = heading + "x" * (limit - len(heading))
    fits, calls = _release(tmp_path / "fits", exactly, "3.0.0", CREATED)
    assert fits.returncode == 0, fits.stdout
    assert len(json.loads(calls[1]["stdin"])["body"]) == limit
    over, calls = _release(tmp_path / "over", exactly + "y", "3.0.0", CREATED)
    assert over.returncode == 1 and str(limit + 1) in over.stdout and calls == []


def test_a_failed_github_call_fails_the_script_instead_of_reporting_success(tmp_path) -> None:
    cases = {
        "lookup": {"GET": (500, {"message": "Server Error"})},
        "lookup unauthorized": {"GET": (401, {"message": "Bad credentials"})},
        "create": {"GET": (404, {"message": "Not Found"}),
                   "POST": (422, {"message": "Validation Failed", "errors": [{"code": "too_long"}]})},
        "update": {"GET": (200, {"id": 7}), "PATCH": (403, {"message": "Forbidden"})},
    }
    for name, responses in cases.items():
        directory = tmp_path / name.replace(" ", "-")
        directory.mkdir()
        run, calls = _release(directory, "## [3.0.0] — R\n\n- x\n", "3.0.0", responses)
        assert run.returncode == 1, name
        assert "ERROR" in run.stdout and "None" not in run.stdout, (name, run.stdout)
        if name.startswith("lookup"):
            assert [call["method"] for call in calls] == ["GET"], name  # nothing is created over an unread state
