"""1.0.14-testing is a rolling development alias that follows Fork Image.

It moves to the immutable ``sha-<commit>`` index that a 1.0.14 commit's Fork
Image run has just published, when that commit is still the branch tip -- one
writer at a time, verified by what the registry holds. It is no qualification
or release target: no required gate gates it, and nothing it does changes a
gate. ``.github/qualification/testing_alias.py`` decides from hand-authored
facts here (nothing is fetched, dispatched or written) and
``.github/workflows/testing-alias.yml`` is called only by Fork Image.
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

TIP = "c63f1048c34df0d8915d125ae99c68850906ea8f"
OLDER = "37a6759d8a3c776cf98062977962cd7e66fbcf71"
NEWER = "c" * 40
DIGEST = "sha256:beea1b9361d1eb39af977ba8adc5d35a03f322c64cc03a9e8ceacd836776a3aa"
AMD64 = "sha256:de75f696bf45c8746197d28ed5f4deaf80c61623aaa6232afe12117bb013b590"
ARM64 = "sha256:2df2b0dd32d02a3cc827603fd0bee65a2b5cdc0b910a3795b5e4c96341b3f99c"
PREVIOUS = "sha256:" + "1" * 64
QUALIFIERS = ("tests.yml", "browser-runtime.yml", "codeql.yml", "container-security.yml",
              "candidate-runtime-qualification.yml")
RETRY = "Retry: re-run Fork Image's Testing alias job."
RECONCILE = "Reconcile: re-run Fork Image's Testing alias job."


def index_for(sha, amd64=AMD64, arm64=ARM64):
    return {"annotations": {"org.opencontainers.image.revision": sha}, "manifests": [
        {"digest": amd64, "platform": {"os": "linux", "architecture": "amd64"}},
        {"digest": arm64, "platform": {"os": "linux", "architecture": "arm64"}},
        {"digest": "sha256:" + "2" * 64, "platform": {"os": "unknown", "architecture": "unknown"},
         "annotations": {"vnd.docker.reference.type": "attestation-manifest"}},
    ]}


INDEX = index_for(TIP)


def published(**changes):
    facts = {"tip": TIP, "sha": TIP, "digest": DIGEST, "index": INDEX, "alias_digest": PREVIOUS}
    facts.update(changes)
    return alias.decide(**facts)


# -- the decision ---------------------------------------------------------------------------------

def test_the_tips_published_image_moves_the_alias_to_that_exact_index():
    decision = published()
    assert decision["action"] == "advance"
    assert (decision["commit"], decision["digest"], decision["platforms"]) == (TIP, DIGEST, {"amd64": AMD64,
                                                                                             "arm64": ARM64})
    assert decision["source_tag"] == f"ghcr.io/xipher-zero/debridpulse:sha-{TIP}"
    assert decision["alias"] == "ghcr.io/xipher-zero/debridpulse:1.0.14-testing"
    assert published(alias_digest=None)["action"] == "advance"           # the very first publication
    assert published(alias_digest=DIGEST)["action"] == "current"         # a repeat: no write


def test_no_qualification_gate_or_attestation_decides_it():
    # The decision has no input for gates or signed evidence at all: Fork
    # Image's own successful publication is the whole condition.
    import inspect
    assert set(inspect.signature(alias.decide).parameters) == {"tip", "sha", "digest", "index", "alias_digest"}
    for retired in ("gates", "attested", "passed_while_running", "unfinished", "REQUIRED", "REQUEST_JOB"):
        assert not hasattr(alias, retired), retired


def test_a_commit_that_is_no_longer_the_tip_never_moves_it():
    decision = published(sha=OLDER, index=index_for(OLDER))
    assert decision["action"] == "superseded" and decision["digest"] is None
    assert alias.outcome(decision) == ("pending", f"1.0.14-testing not moved: 1.0.14 is now {TIP[:12]}; "
                                                  "no write was made.")


@pytest.mark.parametrize("facts,cause", [
    ({"tip": None}, "branch tip unreadable"),
    ({"tip": TIP[:12]}, "branch tip unreadable"),
    ({"sha": "HEAD"}, "commit unreadable"),
    ({"digest": None}, "image unresolved"),
    ({"digest": "sha256:short"}, "image unresolved"),
    ({"index": None}, "image index unverified"),
    ({"index": index_for(OLDER)}, "image index unverified"),                      # names another commit
    ({"index": {**INDEX, "manifests": INDEX["manifests"][:1]}}, "image index unverified"),   # no arm64
    ({"index": index_for(TIP, arm64=AMD64)}, "image index unverified"),           # one image twice
    ({"index": {**INDEX, "manifests": [INDEX["manifests"][0]] * 2 + INDEX["manifests"][1:]}},
     "image index unverified"),
])
def test_an_unverifiable_image_or_tip_is_a_fault_and_never_a_write(facts, cause):
    decision = published(**facts)
    assert decision["action"] == "unchanged" and decision["cause"] == cause
    state, description = alias.outcome(decision)
    assert state == "failure" and cause in description and "no write" in description


# -- the reported outcome ---------------------------------------------------------------------------

def _failures():
    advance = published()
    return {
        "not evaluated": alias.outcome(None),
        "garbage decision": alias.outcome({"action": "bogus"}),
        "branch tip unreadable": alias.outcome(published(tip=None)),
        "image unresolved": alias.outcome(published(digest=None)),
        "image index unverified": alias.outcome(published(index=None)),
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
    for state, description in (alias.outcome(published(sha=OLDER, index=index_for(OLDER))),
                               alias.outcome(published(), write="not_attempted"),
                               alias.outcome(published(), write="advanced")):
        assert len(description) <= alias.STATUS_LIMIT


def test_a_failure_never_claims_the_alias_unchanged_without_observing_it():
    failures = _failures()
    for name in ("write unobserved", "no write report"):
        description = failures[name][1]
        assert "UNKNOWN" in description and "unchanged" not in description.lower() and RECONCILE in description, name
    assert f"still holds {PREVIOUS[:19]}" in failures["write failed, previous observed"][1]
    assert "has no such tag" in failures["write failed, alias absent"][1]
    for name in ("not evaluated", "branch tip unreadable", "image index unverified"):
        assert "no write" in failures[name][1], name


def test_success_is_reported_only_for_an_alias_observed_at_this_commits_image():
    advance = published()
    assert alias.outcome(advance, write="advanced") == ("success", "1.0.14-testing is this commit's image.")
    assert alias.outcome(published(alias_digest=DIGEST)) == ("success", "1.0.14-testing is this commit's image.")
    for write in ("unchanged", "unknown", None, "bogus"):
        assert alias.outcome(advance, write=write)[0] == "failure", write
    assert alias.outcome(advance, write="not_attempted")[0] == "pending"


def test_the_command_line_decides_and_reports_the_same(tmp_path):
    (tmp_path / "index.json").write_text(json.dumps(INDEX), encoding="utf-8")
    command = [sys.executable, "-I", str(SCRIPT)]
    decision = json.loads(subprocess.run(command + ["--tip", TIP, "--sha", TIP, "--digest", DIGEST, "--index",
                                                    str(tmp_path / "index.json"), "--alias-digest", PREVIOUS],
                                         check=True, capture_output=True, text=True).stdout)
    assert decision["action"] == "advance"
    (tmp_path / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
    status = json.loads(subprocess.run(command + ["--outcome", str(tmp_path / "decision.json"), "--write", "unknown"],
                                       check=True, capture_output=True, text=True).stdout)
    assert status["state"] == "failure" and status["description"].endswith(RECONCILE)
    missing = json.loads(subprocess.run(command + ["--outcome", str(tmp_path / "absent.json")],
                                        check=True, capture_output=True, text=True).stdout)
    assert missing["state"] == "failure" and len(missing["description"]) <= alias.STATUS_LIMIT


# -- the write step itself, against a fake registry -------------------------------------------------
# The Move step's own script, run as written, with ``docker`` and ``gh``
# replaced on PATH: the registry's state is a file the fake ``imagetools create``
# may update before it reports failure (a lost response).

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


def _yaml(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _text(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _steps():
    return {step["name"]: step for step in _yaml("testing-alias.yml")["jobs"]["alias"]["steps"]}


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
    (work / "move.sh").write_text(_steps()["Move the alias to this commit's image"]["run"])
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
    code, outputs, registry = _run_move(tmp_path, create="accept-then-fail")
    assert registry == DIGEST and outputs["write"] == "advanced" and outputs["observed"] == DIGEST and code == 0
    assert alias.outcome(published(), write=outputs["write"])[0] == "success"


@pytest.mark.parametrize("create,previous,registry,write,observed", [
    ("accept", PREVIOUS, "up", "advanced", DIGEST),
    ("accept", None, "up", "advanced", DIGEST),
    ("refuse", PREVIOUS, "up", "unchanged", PREVIOUS),
    ("refuse", None, "up", "unchanged", "absent"),
    ("hang", PREVIOUS, "down", "unknown", "no answer"),
    ("refuse", PREVIOUS, "down", "unknown", "no answer"),
    ("silent-ok", PREVIOUS, "up", "unknown", PREVIOUS),
])
def test_the_write_step_reports_only_what_it_observed_in_the_registry(tmp_path, create, previous, registry, write,
                                                                      observed):
    code, outputs, _registry = _run_move(tmp_path, create=create, previous=previous, registry=registry)
    assert (outputs["write"], outputs["observed"]) == (write, observed)
    assert code == (0 if write == "advanced" else 1)
    state, description = alias.outcome(published(), write=outputs["write"], previous=previous)
    assert state == ("success" if write == "advanced" else "failure")
    if write == "unknown":
        assert "UNKNOWN" in description and RECONCILE in description


def test_the_write_step_writes_nothing_when_the_tip_moved_in_its_turn(tmp_path):
    code, outputs, registry = _run_move(tmp_path, create="accept", tip=NEWER)
    assert (code, outputs, registry) == (0, {"write": "not_attempted"}, PREVIOUS)


# -- serialized publications (model) ---------------------------------------------------------------
# One writer group: evaluations take turns; each reads the tip, its own image
# and the alias at the start of its turn and writes only within it.

class Registry:
    def __init__(self):
        self.tip, self.alias, self.writes = TIP, None, []
        self.images = {TIP: (DIGEST, INDEX), OLDER: ("sha256:" + "a" * 64, index_for(OLDER)),
                       NEWER: ("sha256:" + "c" * 64, index_for(NEWER))}

    def turn(self, sha):
        """Fork Image of ``sha`` published; its alias evaluation takes its turn."""
        digest, index = self.images[sha]
        decision = alias.decide(tip=self.tip, sha=sha, digest=digest, index=index, alias_digest=self.alias)
        if decision["action"] == "advance":
            self.alias = decision["digest"]
            self.writes.append(sha)
        return decision["action"]


def test_an_older_publication_finishing_late_never_overwrites_a_newer_image():
    registry = Registry()
    registry.tip = NEWER                                   # NEWER was pushed after TIP
    assert registry.turn(NEWER) == "advance"
    assert registry.turn(TIP) == "superseded"              # TIP's slower run finishes afterwards
    assert registry.turn(OLDER) == "superseded"
    assert registry.alias == registry.images[NEWER][0] and registry.writes == [NEWER]


def test_a_fork_image_rerun_republishes_idempotently_and_a_stale_rerun_is_ignored():
    registry = Registry()
    assert registry.turn(TIP) == "advance"
    assert registry.turn(TIP) == "current"                 # a re-run of the same publication
    registry.tip = NEWER
    assert registry.turn(TIP) == "superseded"              # a re-run of an older commit, later
    assert registry.writes == [TIP] and registry.alias == DIGEST


# -- workflow wiring --------------------------------------------------------------------------------

def test_only_fork_image_calls_it_after_its_own_publication():
    callers = {path.name for path in WORKFLOWS.glob("*.yml") if "testing-alias.yml" in path.read_text()}
    assert callers == {"fork-image.yml"}
    jobs = _yaml("fork-image.yml")["jobs"]
    assert jobs["testing-alias"] == {
        "name": "Testing alias",
        "needs": ["build-and-smoke-test", "publish"],
        "if": "github.event_name == 'push' && github.ref == 'refs/heads/1.0.14'",
        "permissions": {"contents": "read", "packages": "write", "statuses": "write"},
        "uses": "./.github/workflows/testing-alias.yml",
    }
    # Nothing in Fork Image depends on it, and the builder still publishes
    # only its write-once sha-<commit> tag.
    assert not any("testing-alias" in (job.get("needs") or []) for key, job in jobs.items() if key != "testing-alias")
    assert "1.0.14-testing" not in json.dumps({key: job for key, job in jobs.items() if key != "testing-alias"})


def test_no_qualification_gate_carries_or_awaits_the_alias():
    for name in QUALIFIERS:
        text = _text(name)
        assert "testing-alias" not in text and "1.0.14-testing" not in text and "statuses: write" not in text, name


def test_testing_alias_is_only_callable_and_never_changes_fork_images_verdict():
    workflow = _yaml("testing-alias.yml")
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_call"} and not triggers["workflow_call"]
    assert workflow["permissions"] == {} and "concurrency" not in workflow and list(workflow["jobs"]) == ["alias"]
    job = workflow["jobs"]["alias"]
    assert job["permissions"] == {"contents": "read", "packages": "write", "statuses": "write"}
    assert job["timeout-minutes"] <= 15
    assert "github.event_name == 'push'" in job["if"] and "github.ref == 'refs/heads/1.0.14'" in job["if"]
    assert "github.repository == 'Xipher-Zero/debridpulse'" in job["if"]
    assert all(step.get("continue-on-error") is True for step in job["steps"])
    assert workflow["env"]["ALIAS_TAG"] == "1.0.14-testing" and workflow["env"]["BRANCH"] == "1.0.14"
    text = _text("testing-alias.yml")
    for retired in ("attestation verify", "actions/runs?", "--jobs", "--runs", "PREDICATE"):
        assert retired not in text, retired


def test_every_evaluation_and_write_runs_in_one_queued_never_cancelled_writer_group():
    job = _yaml("testing-alias.yml")["jobs"]["alias"]
    assert job["concurrency"] == {"group": "dp-1.0.14-testing-alias-writer", "cancel-in-progress": False,
                                  "queue": "max"}
    names = [step["name"] for step in job["steps"]]
    for read in ("Read the current tip", "Resolve this commit's image and the alias", "Decide"):
        assert names.index(read) < names.index("Move the alias to this commit's image")
    assert sum("imagetools create" in json.dumps(step) for step in job["steps"]) == 1


def test_it_writes_only_its_one_tag_by_reference_after_a_fresh_tip_check():
    text = _text("testing-alias.yml")
    assert re.findall(r"imagetools create[^\n]*", text) == [
        'imagetools create --tag "$target" "${IMAGE_NAME}@${DIGEST}" || status=$?']
    assert 'target="${IMAGE_NAME}:${ALIAS_TAG}"' in text and "timeout 120 docker buildx imagetools create" in text
    move = _steps()["Move the alias to this commit's image"]["run"]
    assert move.index('"$now" != "$TIP"') < move.index("imagetools create")
    assert '[ "$observed" = "$DIGEST" ]' in move and "index_identity" in move and 'echo "write=$write"' in move
    # Only this commit's own image is ever read as the source.
    assert 'source_ref="${IMAGE_NAME}:sha-${GITHUB_SHA}"' in text
    for forbidden in ("docker build ", "build-push-action", "docker push", ":latest", "type=raw", "--platform"):
        assert forbidden not in text, forbidden


def test_the_outcome_is_a_bounded_commit_status_on_the_published_commit_and_no_token_is_printed():
    record = _steps()["Record the outcome"]["run"]
    assert '"repos/${GITHUB_REPOSITORY}/statuses/${GITHUB_SHA}"' in record and '-f context="$ALIAS_TAG"' in record
    assert '--outcome decision.json --write "${WRITE:-}"' in record
    assert 'if [ "$state" = "failure" ]; then echo "::error::${description}"; exit 1; fi' in record
    fallback = json.loads(re.search(r"\|\| echo '(\{[^']*\})' > status.json", record).group(1))
    assert fallback["state"] == "failure" and len(fallback["description"]) <= alias.STATUS_LIMIT
    assert "UNKNOWN" in fallback["description"] and "Reconcile:" in fallback["description"]
    text = _text("testing-alias.yml")
    assert not re.search(r"echo[^\n]*(GH_TOKEN|GITHUB_TOKEN|secrets\.)", text)
    assert "persist-credentials: false" in text


def test_release_promotion_and_its_tags_are_untouched():
    promotion = _text("release-promotion.yml")
    assert "1.0.14-testing" not in promotion and "testing-alias" not in promotion
    assert 'target_tag="latest"' in promotion
