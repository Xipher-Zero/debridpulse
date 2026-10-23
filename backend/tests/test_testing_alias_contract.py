"""1.0.14-testing follows only the fully qualified CURRENT tip of 1.0.14.

``.github/qualification/testing_alias.py`` decides from facts alone (pure
functions, hand-authored GitHub API and registry facts here: nothing is
fetched, dispatched or written), and ``.github/workflows/testing-alias.yml``
is called as the last job of each of the six required workflows. These tests
pin the decision and that wiring; none of them publishes anything.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
SCRIPT = ROOT / ".github" / "qualification" / "testing_alias.py"
_spec = importlib.util.spec_from_file_location("testing_alias", SCRIPT)
alias = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(alias)

TIP = "8092e449fa2734e2e219082203ecdee7dcc0f910"
OLDER = "b81aee896d5c0cc02bce270eb40995d70ee49e48"
DIGEST = "sha256:d02f93b7b6a11a65a5f7c2ea4ea2f19bc959d68519f6ac9ba0c43fda943cd174"
AMD64 = "sha256:6d8c689cacf07704e8ffec0c222d630177b766db62937dac522f908666366d78"
ARM64 = "sha256:730ec557a21c99f7a23c181447057da583814b492c51cbc2b14555dde817028c"
PREVIOUS = "sha256:" + "1" * 64
REQUIRED_FILES = ("tests.yml", "browser-runtime.yml", "codeql.yml", "fork-image.yml", "container-security.yml",
                  "candidate-runtime-qualification.yml")


def run(path, *, sha=TIP, status="completed", conclusion="success", run_id=None, attempt=1, event="push",
        branch="1.0.14"):
    return {"id": run_id or 1000 + REQUIRED_FILES.index(Path(path).name), "path": f".github/workflows/{path}",
            "head_sha": sha, "status": status, "conclusion": conclusion if status == "completed" else None,
            "run_attempt": attempt, "event": event, "head_branch": branch}


def all_green(sha=TIP):
    return [run(name, sha=sha) for name in REQUIRED_FILES]


def replaced(runs, name, **changes):
    return [run(name, **changes) if item["path"].endswith("/" + name) else item for item in runs]


INDEX = {"annotations": {"org.opencontainers.image.revision": TIP}, "manifests": [
    {"digest": AMD64, "platform": {"os": "linux", "architecture": "amd64"}},
    {"digest": ARM64, "platform": {"os": "linux", "architecture": "arm64"}},
    {"digest": "sha256:" + "2" * 64, "platform": {"os": "unknown", "architecture": "unknown"},
     "annotations": {"vnd.docker.reference.type": "attestation-manifest"}},
]}


def attestation(kind, **changes):
    fields = {"security": {"vulnerability_gate": "passed"}, "runtime": {"runtime": "passed", "non_root": "passed"}}
    predicate = {"commit_sha": TIP, "manifest_digest": DIGEST, "result": "success", "platforms": {
        "linux/amd64": {"digest": AMD64, **fields[kind]}, "linux/arm64": {"digest": ARM64, **fields[kind]}}}
    predicate.update(changes)
    return [{"verificationResult": {"statement": {"predicate": predicate}}}]


def qualified(**changes):
    facts = {"tip": TIP, "runs": all_green(), "digest": DIGEST, "index": INDEX,
             "security": attestation("security"), "runtime": attestation("runtime"), "alias_digest": PREVIOUS}
    facts.update(changes)
    return alias.decide(**facts)


# -- eligibility ------------------------------------------------------------------------------

def test_six_passed_gates_and_verified_evidence_advance_the_alias_to_that_exact_index():
    decision = qualified()
    assert decision["action"] == "advance"
    assert (decision["commit"], decision["digest"], decision["platforms"]) == (TIP, DIGEST, {"amd64": AMD64,
                                                                                             "arm64": ARM64})
    assert decision["source_tag"] == f"ghcr.io/xipher-zero/debridpulse:sha-{TIP}"
    assert decision["alias"] == "ghcr.io/xipher-zero/debridpulse:1.0.14-testing"
    assert decision["previous_alias_digest"] == PREVIOUS
    assert {item["state"] for item in decision["gates"].values()} == {"success"}


def test_an_alias_already_at_that_index_is_left_alone():
    first = qualified(alias_digest=None)
    assert first["action"] == "advance"
    assert qualified(alias_digest=DIGEST)["action"] == "current"


@pytest.mark.parametrize("name", REQUIRED_FILES)
@pytest.mark.parametrize("change", [
    {"conclusion": "failure"}, {"conclusion": "cancelled"}, {"conclusion": "skipped"}, {"conclusion": "timed_out"},
    {"conclusion": "action_required"}, {"conclusion": None}, {"status": "in_progress"}, {"status": "queued"},
])
def test_any_gate_not_passed_leaves_the_alias(name, change):
    decision = qualified(runs=replaced(all_green(), name, **change))
    assert decision["action"] == "unchanged" and "not qualified" in decision["reason"]


@pytest.mark.parametrize("name", REQUIRED_FILES)
def test_a_missing_gate_leaves_the_alias_even_with_a_published_image(name):
    runs = [item for item in all_green() if not item["path"].endswith("/" + name)]
    assert qualified(runs=runs)["action"] == "unchanged"


def test_only_push_runs_of_1_0_14_for_the_tip_itself_count():
    for foreign in ({"event": "pull_request"}, {"event": "workflow_dispatch"}, {"event": "schedule"},
                    {"branch": "main"}, {"branch": "staging/x"}, {"sha": OLDER}):
        runs = [run(name, **foreign) for name in REQUIRED_FILES]
        assert qualified(runs=runs)["action"] == "unchanged", foreign
    # A run identified by its display name alone is not the required workflow.
    renamed = [{**item, "path": item["path"].replace(".yml", "-copy.yml")} for item in all_green()]
    assert qualified(runs=renamed)["action"] == "unchanged"


@pytest.mark.parametrize("facts,reason", [
    ({"digest": None}, "did not resolve"),
    ({"digest": "sha256:short"}, "did not resolve"),
    ({"index": None}, "does not name this commit"),
    ({"index": {**INDEX, "annotations": {"org.opencontainers.image.revision": OLDER}}}, "does not name this commit"),
    ({"index": {**INDEX, "manifests": INDEX["manifests"][:1]}}, "does not name this commit"),
    ({"index": {**INDEX, "manifests": [INDEX["manifests"][0]] * 2 + INDEX["manifests"][1:]}},
     "does not name this commit"),
    ({"security": []}, "no verified security attestation"),
    ({"security": None}, "no verified security attestation"),
    ({"runtime": []}, "no verified runtime attestation"),
    ({"security": attestation("security", commit_sha=OLDER)}, "security"),
    ({"security": attestation("security", manifest_digest=PREVIOUS)}, "security"),
    ({"security": attestation("security", result="failure")}, "security"),
    ({"security": attestation("security", platforms={"linux/amd64": {"digest": AMD64, "vulnerability_gate": "passed"},
                                                     "linux/arm64": {"digest": ARM64, "vulnerability_gate": "failed"}})},
     "security"),
    ({"runtime": attestation("runtime", platforms={"linux/amd64": {"digest": AMD64, "runtime": "passed",
                                                                   "non_root": "passed"},
                                                   "linux/arm64": {"digest": AMD64, "runtime": "passed",
                                                                   "non_root": "passed"}})}, "runtime"),
    ({"runtime": attestation("security")}, "runtime"),
])
def test_missing_or_mismatched_image_evidence_cannot_be_overridden(facts, reason):
    decision = qualified(**facts)
    assert decision["action"] == "unchanged" and reason in decision["reason"], decision["reason"]


def test_a_tip_that_is_not_a_full_commit_is_never_evaluated():
    for tip in (None, "", TIP[:12], TIP.upper(), TIP + "0"):
        assert qualified(tip=tip)["action"] == "unchanged"


# -- re-runs, staleness and concurrent finishes -----------------------------------------------

def test_a_failed_gate_that_passes_on_an_authorized_rerun_qualifies_the_current_tip():
    failed = replaced(all_green(), "candidate-runtime-qualification.yml", conclusion="failure", attempt=2)
    assert qualified(runs=failed)["action"] == "unchanged"
    # The same run, re-run: the API now reports its latest attempt.
    rerun = replaced(all_green(), "candidate-runtime-qualification.yml", conclusion="success", attempt=3)
    decision = qualified(runs=rerun)
    assert decision["action"] == "advance"
    assert decision["gates"]["Candidate Runtime Qualification"] == {"state": "success", "run_id": 1005, "attempt": 3}


def test_an_older_commits_late_success_never_moves_the_alias_past_a_newer_unqualified_tip():
    # The evaluation always reads the tip: the older commit's six green runs
    # (and its own image evidence) are not the tip's.
    runs = all_green(sha=OLDER) + [run("tests.yml", status="in_progress")]
    decision = qualified(runs=runs)
    assert decision["action"] == "unchanged" and decision["commit"] == TIP
    # Nor does the newest run of a workflow lose to an older one for the same commit.
    duplicate = all_green() + [run("tests.yml", run_id=5000, conclusion="failure")]
    assert qualified(runs=duplicate)["action"] == "unchanged"


def _jobs(*, request="in_progress", others=("success", "success")):
    listed = [{"name": f"job {index}", "status": "completed", "conclusion": conclusion}
              for index, conclusion in enumerate(others)]
    if request:
        listed.append({"name": "Testing alias / Move 1.0.14-testing to the qualified tip", "status": request,
                       "conclusion": None})
    return listed


def test_a_run_still_finishing_has_passed_only_once_its_testing_alias_job_exists_after_all_others_passed():
    # This run (evaluating now) and another finishing at the same moment.
    runs = replaced(replaced(all_green(), "tests.yml", status="in_progress"), "codeql.yml", status="in_progress")
    jobs = {"1000": _jobs(), "1002": _jobs(request="queued")}
    assert qualified(runs=runs, jobs=jobs)["action"] == "advance"
    for listed in (_jobs(request=None),                                  # its alias job not yet created
                   _jobs(others=("success", "failure")),
                   _jobs(others=("success", "skipped")),
                   [{"name": "job", "status": "in_progress", "conclusion": None}, *_jobs()],
                   _jobs(others=()), None, []):
        decision = qualified(runs=runs, jobs={"1000": listed, "1002": _jobs()})
        assert decision["action"] == "unchanged" and "Tests running" in decision["reason"], listed
    assert alias.unfinished(runs, TIP) == [1000, 1002]


def test_the_command_line_reads_the_same_facts(tmp_path):
    files = {"runs.json": {"total_count": 6, "workflow_runs": all_green()}, "index.json": INDEX,
             "security.json": attestation("security"), "runtime.json": attestation("runtime")}
    for name, value in files.items():
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
    command = [sys.executable, "-I", str(SCRIPT), "--tip", TIP, "--runs", str(tmp_path / "runs.json")]
    gates = json.loads(subprocess.run(command + ["--gates-only"], check=True, capture_output=True, text=True).stdout)
    assert gates["qualified"] is True
    decision = json.loads(subprocess.run(command + [
        "--digest", DIGEST, "--index", str(tmp_path / "index.json"), "--security", str(tmp_path / "security.json"),
        "--runtime", str(tmp_path / "runtime.json"), "--alias-digest", DIGEST,
    ], check=True, capture_output=True, text=True).stdout)
    assert decision["action"] == "current"
    nothing = subprocess.run(command + ["--unfinished"], check=True, capture_output=True, text=True).stdout
    assert nothing.strip() == ""


# -- workflow wiring --------------------------------------------------------------------------

def _yaml(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _text(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


PERMISSIONS = {"contents": "read", "actions": "read", "packages": "write", "attestations": "read",
               "statuses": "write"}
WRITER_GROUP = "dp-1.0.14-testing-alias-writer"


def test_each_required_gate_calls_testing_alias_last_and_only_for_its_own_1_0_14_push():
    for name in REQUIRED_FILES:
        jobs = _yaml(name)["jobs"]
        caller = jobs["testing-alias"]
        assert caller["name"] == alias.REQUEST_JOB, name
        assert caller["uses"] == "./.github/workflows/testing-alias.yml", name
        assert caller["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/1.0.14'", name
        assert set(caller["needs"]) == set(jobs) - {"testing-alias"}, name
        assert caller["permissions"] == PERMISSIONS, name
        # The callers add no concurrency of their own: the one writer group
        # is the called job's, so all six queue in the same group.
        assert "concurrency" not in caller, name
        # It is never a dependency of the gate's own verdict.
        assert not any("testing-alias" in (job.get("needs") or []) for key, job in jobs.items()
                       if key != "testing-alias"), name
    assert {path for path, _name in alias.REQUIRED} == {f".github/workflows/{name}" for name in REQUIRED_FILES}
    assert {name for _path, name in alias.REQUIRED} == {_yaml(name)["name"] for name in REQUIRED_FILES}
    # No other workflow calls it.
    callers = {path.name for path in WORKFLOWS.glob("*.yml") if "testing-alias.yml" in path.read_text()}
    assert callers == set(REQUIRED_FILES)


def test_testing_alias_is_only_callable_and_never_changes_a_gates_verdict():
    workflow = _yaml("testing-alias.yml")
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_call"} and not triggers["workflow_call"]
    assert workflow["permissions"] == {} and "concurrency" not in workflow
    assert list(workflow["jobs"]) == ["alias"]
    job = workflow["jobs"]["alias"]
    assert job["timeout-minutes"] <= 15 and job["permissions"] == PERMISSIONS
    assert "github.event_name == 'push'" in job["if"] and "github.ref == 'refs/heads/1.0.14'" in job["if"]
    assert "github.repository == 'Xipher-Zero/debridpulse'" in job["if"]
    assert all(step.get("continue-on-error") is True for step in job["steps"])
    assert workflow["env"]["ALIAS_TAG"] == "1.0.14-testing" and workflow["env"]["BRANCH"] == "1.0.14"


def test_every_evaluation_and_write_runs_in_one_queued_never_cancelled_writer_group():
    """GitHub's documented semantics (workflow syntax, ``concurrency``): one
    group runs one job at a time; ``queue: max`` keeps up to 100 pending,
    first in first out, instead of cancelling and replacing the pending one;
    ``queue: max`` with ``cancel-in-progress: true`` is invalid. So the six
    callers' evaluations of one push -- and of later pushes -- take turns,
    and no two registry writes can overlap."""
    job = _yaml("testing-alias.yml")["jobs"]["alias"]
    assert job["concurrency"] == {"group": WRITER_GROUP, "cancel-in-progress": False, "queue": "max"}
    # A literal: a called workflow shares github.workflow with its caller.
    assert "${{" not in job["concurrency"]["group"]
    # The only image write anywhere in 1.0.14's alias path is inside that job,
    # and every evaluation input is read inside the same turn.
    names = [step["name"] for step in job["steps"]]
    for read in ("Read the current tip and its six required gates", "Resolve the immutable source index and the alias",
                 "Verify the signed qualification attestations", "Decide"):
        assert names.index(read) < names.index("Move the alias to the qualified index")
    assert sum("imagetools create" in json.dumps(step) for step in job["steps"]) == 1
    # A turn is bounded: the job's own limit, and the write's.
    assert job["timeout-minutes"] <= 15 and "timeout 120 docker buildx imagetools create" in _text("testing-alias.yml")
    # A waiting evaluation never waits on another: a peer whose own alias job
    # is still queued already counts as passed (no circular wait).
    assert alias.passed_while_running(_jobs(request="pending"))
    assert alias.passed_while_running(_jobs(request="waiting"))


def test_testing_alias_writes_only_its_one_tag_by_reference_after_a_fresh_tip_check():
    text = _text("testing-alias.yml")
    writes = re.findall(r"imagetools create[^\n]*", text)
    assert writes == ['imagetools create --tag "$target" "${IMAGE_NAME}@${DIGEST}" || status=$?']
    assert 'target="${IMAGE_NAME}:${ALIAS_TAG}"' in text and "timeout 120 docker buildx imagetools create" in text
    move = text[text.index("- name: Move the alias"):text.index("- name: Record the outcome")]
    assert move.index('"$now" != "$TIP"') < move.index("imagetools create")
    # The client's exit code is recorded, never trusted: the registry is observed.
    assert '[ "$observed" = "$DIGEST" ]' in move and "index_identity" in move and 'echo "write=$write"' in move
    for forbidden in ("docker build ", "build-push-action", "docker push", ":latest", "type=raw",
                      "sha-${{", "--platform"):
        assert forbidden not in text, forbidden


def test_attestations_must_be_signed_by_the_qualifying_workflows_from_this_branch_for_this_commit():
    text = _text("testing-alias.yml")
    for flag in ('--signer-workflow "${GITHUB_REPOSITORY}/.github/workflows/${signer}"',
                 '--source-ref "refs/heads/${BRANCH}"', '--source-digest "$TIP"', "--deny-self-hosted-runners"):
        assert flag in text, flag
    assert 'signer="container-security.yml"' in text and 'signer="candidate-runtime-qualification.yml"' in text


def test_the_outcome_is_a_commit_status_apart_from_the_gates_and_no_token_is_printed():
    text = _text("testing-alias.yml")
    record = text[text.index("- name: Record the outcome"):]
    assert '"repos/${GITHUB_REPOSITORY}/statuses/${TIP:-$GITHUB_SHA}"' in record
    assert '-f context="$ALIAS_TAG"' in record and "--outcome decision.json" in record
    assert '--write "${WRITE:-}" --previous "${PREVIOUS:-}"' in record and "description[:" not in _text("testing-alias.yml")
    # Its own fallback, too, is bounded, actionable and claims nothing unobserved.
    fallback = json.loads(re.search(r"\|\| echo '(\{[^']*\})' > status.json", record).group(1))
    assert fallback["state"] == "failure" and len(fallback["description"]) <= alias.STATUS_LIMIT
    assert fallback["description"].endswith(RECONCILE) and "UNKNOWN" in fallback["description"]
    # A failure status is also a failed step with an annotation; an
    # unrecordable status is a failed step of its own.
    assert 'if [ "$state" = "failure" ]; then echo "::error::${description}"; exit 1; fi' in record
    assert "commit status could not be recorded" in record
    assert "GITHUB_STEP_SUMMARY" in record
    assert not re.search(r"echo[^\n]*(GH_TOKEN|GITHUB_TOKEN|secrets\.)", text)
    assert "persist-credentials: false" in text


# -- the reported outcome ----------------------------------------------------------------------
# Every status fits GitHub's 140-character limit with its recovery action whole;
# success only for an alias observed at this commit's index; "unchanged" only
# when the registry was observed still holding the previous alias.

RETRY = "Retry: re-run a Testing alias job on 1.0.14."
RECONCILE = "Reconcile: re-run a Testing alias job on 1.0.14."
LONG_GATES = [run(name, status="in_progress") for name in REQUIRED_FILES]


def _failures():
    """Every failure category, with the longest causes the decision produces."""
    advance = qualified()
    return {
        "not evaluated": alias.outcome(None),
        "garbage decision": alias.outcome({"action": "bogus"}),
        "source image unresolved": alias.outcome(qualified(digest=None)),
        "source index unverified": alias.outcome(qualified(index=None)),
        "security attestation unverified": alias.outcome(qualified(security=[])),
        "runtime attestation unverified": alias.outcome(qualified(runtime=[])),
        "write failed, previous observed": alias.outcome(advance, write="unchanged", previous=PREVIOUS),
        "write failed, alias absent": alias.outcome(advance, write="unchanged", previous=None),
        "write unobserved": alias.outcome(advance, write="unknown", previous=PREVIOUS),
        "no write report": alias.outcome(advance, write=None, previous=PREVIOUS),
    }


def test_every_failure_status_fits_the_limit_with_its_recovery_action_whole():
    for name, (state, description) in _failures().items():
        assert state == "failure", name
        assert len(description) <= alias.STATUS_LIMIT, (name, len(description), description)
        assert description.endswith(RETRY) or description.endswith(RECONCILE), (name, description)
        assert description.startswith("1.0.14-testing "), name
    # And the pending ones, however many gates are listed, stay within it too.
    state, description = alias.outcome(qualified(runs=LONG_GATES))
    assert state == "pending" and len(description) <= alias.STATUS_LIMIT and description.endswith("…")


def test_a_failure_never_claims_the_alias_unchanged_without_observing_it():
    failures = _failures()
    for name in ("write unobserved", "no write report"):
        description = failures[name][1]
        assert "UNKNOWN" in description and "unchanged" not in description.lower() and RECONCILE in description, name
    assert "write failed; registry observed" in failures["write failed, previous observed"][1]
    assert f"still holds {PREVIOUS[:19]}" in failures["write failed, previous observed"][1]
    assert "has no such tag" in failures["write failed, alias absent"][1]
    # A fault before any write says so -- that, not "unchanged", is what is known.
    for name in ("not evaluated", "source index unverified", "security attestation unverified"):
        assert "no write" in failures[name][1], name


def test_success_is_reported_only_for_an_alias_observed_at_this_commits_index():
    advance = qualified()
    assert alias.outcome(advance, write="advanced") == ("success", "1.0.14-testing is this commit's qualified index.")
    assert alias.outcome(qualified(alias_digest=DIGEST))[0] == "success"
    for write in ("unchanged", "unknown", None, "bogus"):
        assert alias.outcome(advance, write=write)[0] == "failure", write
    assert alias.outcome(advance, write="not_attempted") == (
        "pending", "1.0.14-testing not moved: 1.0.14 advanced before the write; no write was made.")


def test_unfinished_gates_are_pending_and_a_gate_failure_is_not_an_alias_fault():
    state, description = alias.outcome(qualified(runs=replaced(all_green(), "tests.yml", status="in_progress")))
    assert state == "pending" and "Tests running" in description
    state, description = alias.outcome(qualified(runs=replaced(all_green(), "tests.yml", conclusion="failure")))
    assert state == "pending" and "Tests failure" in description


def test_the_command_line_prints_the_same_bounded_status(tmp_path):
    (tmp_path / "decision.json").write_text(json.dumps(qualified()), encoding="utf-8")
    command = [sys.executable, "-I", str(SCRIPT), "--tip", TIP, "--runs", "/dev/null", "--outcome"]
    printed = json.loads(subprocess.run(command + [str(tmp_path / "decision.json"), "--write", "unknown"],
                                        check=True, capture_output=True, text=True).stdout)
    assert printed["state"] == "failure" and printed["description"].endswith(RECONCILE)
    missing = json.loads(subprocess.run(command + [str(tmp_path / "absent.json")],
                                        check=True, capture_output=True, text=True).stdout)
    assert missing["state"] == "failure" and len(missing["description"]) <= alias.STATUS_LIMIT


# -- the write step itself, against a fake registry -------------------------------------------
# The Move step's own script, run as written, with ``docker`` and ``gh``
# replaced on PATH: the registry's state is a file the fake ``imagetools create``
# may update before it reports failure (a lost response), exactly as a real
# registry may accept a write whose answer never reaches the client.

FAKE_DOCKER = r"""#!/bin/bash
state="$FAKE_DIR/registry"
if [ "$1 $2 $3" = "buildx imagetools create" ]; then
  case "$FAKE_CREATE" in
    accept) echo "$FAKE_DIGEST" > "$state"; exit 0;;
    accept-then-fail) echo "$FAKE_DIGEST" > "$state"; echo "error: response lost" >&2; exit 1;;
    refuse) echo "denied: write refused" >&2; exit 1;;
    hang) exit 124;;
    silent-ok) exit 0;;
  esac
fi
if [ "$1 $2 $3" = "buildx imagetools inspect" ]; then
  [ "$FAKE_REGISTRY" = down ] && { echo "error: connection reset by peer" >&2; exit 1; }
  current="$(cat "$state" 2>/dev/null || true)"
  [ -n "$current" ] || { echo "ERROR: manifest unknown" >&2; exit 1; }
  if [ "$4" = "--raw" ]; then
    [ "$current" = "$FAKE_DIGEST" ] && cat "$FAKE_DIR/index.json" || echo '{}'
  else
    printf 'Name:      x\nMediaType: application/vnd.oci.image.index.v1+json\nDigest:    %s\n' "$current"
  fi
  exit 0
fi
exit 2
"""
FAKE_GH = """#!/bin/bash
echo "$FAKE_TIP"
"""


def _move_script():
    steps = {step["name"]: step for step in _yaml("testing-alias.yml")["jobs"]["alias"]["steps"]}
    return steps["Move the alias to the qualified index"]["run"]


def _run_move(tmp_path, *, create, previous=PREVIOUS, registry="up", tip=TIP):
    bin_dir, work = tmp_path / "bin", tmp_path / "work"
    for path in (bin_dir, work / ".github" / "qualification"):
        path.mkdir(parents=True, exist_ok=True)
    (work / ".github" / "qualification" / "testing_alias.py").write_text(SCRIPT.read_text(encoding="utf-8"))
    for name, body in (("docker", FAKE_DOCKER), ("gh", FAKE_GH), ("sleep", "#!/bin/bash\nexit 0\n")):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    (work / "index.json").write_text(json.dumps(INDEX))
    (tmp_path / "index.json").write_text(json.dumps(INDEX))
    (tmp_path / "registry").write_text(previous or "")
    (work / "move.sh").write_text(_move_script())
    output = tmp_path / "output"
    output.write_text("")
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "GITHUB_OUTPUT": str(output), "GITHUB_REPOSITORY": "o/r",
           "IMAGE_NAME": "ghcr.io/xipher-zero/debridpulse", "ALIAS_TAG": "1.0.14-testing", "BRANCH": "1.0.14",
           "TIP": TIP, "DIGEST": DIGEST, "PREVIOUS": previous or "", "GH_TOKEN": "t", "FAKE_DIR": str(tmp_path),
           "FAKE_CREATE": create, "FAKE_DIGEST": DIGEST, "FAKE_REGISTRY": registry, "FAKE_TIP": tip}
    completed = subprocess.run(["bash", "move.sh"], cwd=work, env=env, capture_output=True, text=True, timeout=60)
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
    return completed.returncode, outputs, (tmp_path / "registry").read_text().strip()


def test_a_write_the_registry_accepted_before_the_client_failed_is_reported_advanced(tmp_path):
    # Negative control for "a failed client means unchanged": it does not.
    code, outputs, registry = _run_move(tmp_path, create="accept-then-fail")
    assert registry == DIGEST and outputs["write"] == "advanced" and outputs["observed"] == DIGEST and code == 0
    assert alias.outcome(qualified(), write=outputs["write"])[0] == "success"


@pytest.mark.parametrize("create,previous,registry,write,observed", [
    ("accept", PREVIOUS, "up", "advanced", DIGEST),
    ("accept", None, "up", "advanced", DIGEST),
    ("refuse", PREVIOUS, "up", "unchanged", PREVIOUS),
    ("refuse", None, "up", "unchanged", "absent"),
    ("hang", PREVIOUS, "down", "unknown", "no answer"),          # timed out, registry unreachable
    ("refuse", PREVIOUS, "down", "unknown", "no answer"),        # refused, but nothing confirms the alias
    ("silent-ok", PREVIOUS, "up", "unknown", PREVIOUS),          # client said yes, registry still previous
])
def test_the_write_step_reports_only_what_it_observed_in_the_registry(tmp_path, create, previous, registry, write,
                                                                      observed):
    code, outputs, _registry = _run_move(tmp_path, create=create, previous=previous, registry=registry)
    assert (outputs["write"], outputs["observed"]) == (write, observed)
    assert code == (0 if write == "advanced" else 1)                    # every non-advance is a failed step
    state, description = alias.outcome(qualified(), write=outputs["write"], previous=previous)
    assert state == ("success" if write == "advanced" else "failure")
    if write == "unknown":
        assert "UNKNOWN" in description and RECONCILE in description


def test_the_write_step_writes_nothing_when_the_tip_moved_in_its_turn(tmp_path):
    code, outputs, registry = _run_move(tmp_path, create="accept", tip="d" * 40)
    assert (code, outputs, registry) == (0, {"write": "not_attempted"}, PREVIOUS)


# -- serialized evaluations: ordering, re-runs and faults (model) -----------------------------
# A model of the one writer group: evaluations take turns in the order they
# began waiting (GitHub's FIFO), each reading the branch, the runs, the
# registry and the alias at the start of its turn and writing only within it.

NEWER = "c" * 40
NEWER_DIGEST = "sha256:" + "c" * 64


def _facts_for(sha, digest):
    index = {**INDEX, "annotations": {"org.opencontainers.image.revision": sha}}

    def evidence(kind):
        return [{"verificationResult": {"statement": {"predicate": {
            **attestation(kind)[0]["verificationResult"]["statement"]["predicate"],
            "commit_sha": sha, "manifest_digest": digest}}}}]
    return {"digest": digest, "index": index, "security": evidence("security"), "runtime": evidence("runtime")}


class World:
    """The branch, the six gates per commit, the registry and the alias."""

    def __init__(self):
        self.tip = TIP
        self.runs = []
        self.images = {TIP: _facts_for(TIP, DIGEST), NEWER: _facts_for(NEWER, NEWER_DIGEST)}
        self.alias = None
        self.writes = []
        self.statuses = {}
        self.registry_up = True

    def turn(self, label):
        """One evaluation's whole turn; nothing else runs meanwhile."""
        facts = self.images.get(self.tip, {})
        decision = alias.decide(tip=self.tip, runs=self.runs, alias_digest=self.alias,
                                **(facts if self.registry_up else {}))
        write = None
        if decision["action"] == "advance":
            if self.registry_up:
                self.alias = decision["digest"]
                self.writes.append((label, decision["commit"], decision["digest"]))
                write = "advanced"
            else:
                write = "unchanged"
        self.statuses[decision["commit"]] = alias.outcome(decision, write=write, previous=self.alias)
        return decision


def test_six_simultaneous_evaluations_write_once_and_queue_without_loss():
    world = World()
    world.runs = all_green()
    # All six gates finished together: six evaluations queued in the group.
    actions = [world.turn(f"gate {index}")["action"] for index in range(6)]
    assert actions == ["advance"] + ["current"] * 5
    assert world.writes == [("gate 0", TIP, DIGEST)] and world.alias == DIGEST
    assert world.statuses[TIP][0] == "success"


def test_an_older_delayed_evaluation_never_overwrites_a_newer_qualified_image():
    world = World()
    world.runs = all_green()
    queued_from_older = "older commit's last gate"          # began waiting, then delayed
    world.tip = NEWER
    world.runs += all_green(sha=NEWER)
    world.turn("newer commit's last gate")
    assert world.alias == NEWER_DIGEST
    # The older evaluation's turn comes last: it reads the CURRENT tip.
    decision = world.turn(queued_from_older)
    assert decision["commit"] == NEWER and decision["action"] == "current"
    assert world.writes == [("newer commit's last gate", NEWER, NEWER_DIGEST)] and world.alias == NEWER_DIGEST
    # And with the newer tip not yet qualified, it leaves the older alias alone.
    world = World()
    world.runs, world.alias, world.tip = all_green(), DIGEST, NEWER
    assert world.turn("older commit's late gate")["action"] == "unchanged" and world.alias == DIGEST
    assert world.writes == []


def test_a_failed_then_rerun_gate_advances_the_alias_on_the_reruns_own_turn():
    world = World()
    world.runs = replaced(all_green(), "container-security.yml", conclusion="failure")
    assert [world.turn(f"gate {index}")["action"] for index in range(5)] == ["unchanged"] * 5
    assert world.alias is None and world.statuses[TIP][0] == "pending"
    world.runs = replaced(all_green(), "container-security.yml", attempt=2)
    assert world.turn("Container Security re-run")["action"] == "advance" and world.alias == DIGEST


def test_a_registry_fault_leaves_the_last_qualified_alias_and_reports_an_actionable_failure():
    world = World()
    world.runs, world.alias = all_green(), PREVIOUS
    world.registry_up = False                                 # unreadable index and evidence
    world.turn("gate")
    assert world.alias == PREVIOUS and world.writes == []
    state, description = world.statuses[TIP]
    assert state == "failure" and RETRY in description
    # The write itself failing is the same: unchanged, failure, retry named.
    world = World()
    world.runs, world.alias = all_green(), PREVIOUS
    decision = alias.decide(tip=TIP, runs=world.runs, alias_digest=PREVIOUS, **world.images[TIP])
    assert decision["action"] == "advance"
    state, description = alias.outcome(decision, write="unchanged", previous=PREVIOUS)
    assert state == "failure" and RETRY in description and world.alias == PREVIOUS
    # Re-running an evaluation once the registry answers again reconciles it.
    world.registry_up = True
    world.turn("re-run")
    assert world.alias == DIGEST and world.statuses[TIP][0] == "success"


def test_release_promotion_and_its_tags_are_untouched():
    promotion = _text("release-promotion.yml")
    assert "1.0.14-testing" not in promotion and "testing-alias" not in promotion
    assert 'target_tag="latest"' in promotion
    # The builder still publishes only its write-once sha-<commit> tag.
    fork = _yaml("fork-image.yml")["jobs"]
    assert "1.0.14-testing" not in json.dumps({key: job for key, job in fork.items() if key != "testing-alias"})
