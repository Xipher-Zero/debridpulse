"""Container Security and Candidate Runtime Qualification wait on the publisher.

They qualify ``sha-<commit>`` only once this commit's Fork Image run has
published it, so they wait while that run is queued or running (a re-run
included) instead of exhausting a fixed window, and fail -- saying why -- once
it finished without publishing, never appeared, or the bound passed.
``.github/qualification/candidate_image.py`` decides from hand-authored run and
registry facts here; nothing is fetched.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
SCRIPT = ROOT / ".github" / "qualification" / "candidate_image.py"
_spec = importlib.util.spec_from_file_location("candidate_image", SCRIPT)
waiter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(waiter)

SHA = "c63f1048c34df0d8915d125ae99c68850906ea8f"
QUALIFIERS = ("container-security.yml", "candidate-runtime-qualification.yml")


def fork(*, status="completed", conclusion="success", run_id=900, attempt=1, sha=SHA, event="push",
         path=waiter.FORK_IMAGE):
    return {"id": run_id, "path": path, "head_sha": sha, "event": event, "status": status,
            "conclusion": conclusion if status == "completed" else None, "run_attempt": attempt}


class Timeline:
    """A clock that advances only by the waiter's own sleeps, and the facts
    observed at each moment: ``script`` is ``[(from_second, runs, image)]``."""

    def __init__(self, script):
        self.now, self.script, self.observed, self.slept = 0.0, script, 0, 0.0

    def facts(self):
        current = [entry for entry in self.script if entry[0] <= self.now][-1]
        return current[1], current[2]

    def runs(self):
        self.observed += 1
        runs = self.facts()[0]
        if isinstance(runs, Exception):
            raise runs
        return runs

    def image(self):
        return self.facts()[1]

    def sleep(self, seconds):
        self.now += seconds
        self.slept += seconds

    def wait(self, **kwargs):
        return waiter.wait(SHA, fetch_runs=self.runs, probe=self.image, sleep=self.sleep, clock=lambda: self.now,
                           log=lambda line: None, **kwargs)


def test_a_slow_queued_and_publishing_fork_image_is_waited_for_not_timed_out():
    # Exactly 37a6759d's and c63f1048's shape: about 7 minutes queued for the
    # publish runner, then a multi-arch push -- well past the old 10 minutes.
    timeline = Timeline([
        (0, [fork(status="in_progress")], "absent"),
        (7 * 60, [fork(status="in_progress")], "absent"),       # publish job queued, then pushing
        (15 * 60, [fork(status="in_progress")], "present"),     # the index lands before the run ends
    ])
    ready, why = timeline.wait()
    assert ready and why == "the exact candidate image is published"
    assert 15 * 60 <= timeline.now < 16 * 60


def test_an_image_already_published_is_used_at_once():
    # A scheduled scan of an old commit, or a re-run after publication.
    timeline = Timeline([(0, [], "present")])
    assert timeline.wait() == (True, "the exact candidate image is published") and timeline.slept == 0


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "skipped", "startup_failure", None])
def test_a_publisher_that_finished_without_publishing_fails_at_once_saying_so(conclusion):
    timeline = Timeline([(0, [fork(status="in_progress")], "absent"),
                         (300, [fork(conclusion=conclusion, attempt=1)], "absent")])
    ready, why = timeline.wait()
    assert not ready and "Fork Image run 900 (attempt 1) concluded" in why and "not published" in why
    assert timeline.now < 300 + 2 * waiter.POLL


def test_an_authorized_fork_image_rerun_is_waited_for():
    # Attempt 1 failed (the qualifier would fail if it looked then), but the
    # re-run is already in progress when it looks: it waits for attempt 2.
    timeline = Timeline([(0, [fork(status="in_progress", attempt=2)], "absent"),
                         (600, [fork(status="completed", conclusion="success", attempt=2)], "present")])
    assert timeline.wait()[0] is True


def test_the_newest_publisher_run_is_the_one_that_counts():
    runs = [fork(run_id=800, conclusion="failure"), fork(run_id=900, status="in_progress")]
    assert waiter.verdict(runs, "absent", sha=SHA, waited=0, settled_for=None)[0] == "wait"
    runs = [fork(run_id=800, conclusion="success"), fork(run_id=900, conclusion="cancelled")]
    state, why = waiter.verdict(runs, "absent", sha=SHA, waited=0, settled_for=None)
    assert state == "fail" and "run 900" in why


def test_a_failed_newest_run_fails_at_once_whatever_an_older_run_is_doing():
    # Negative control: an older run still queued must not keep the wait alive.
    runs = [fork(run_id=800, status="queued"), fork(run_id=900, conclusion="failure", attempt=2)]
    timeline = Timeline([(0, runs, "absent")])
    ready, why = timeline.wait()
    assert not ready and why == "Fork Image run 900 (attempt 2) concluded failure: the candidate was not published"
    assert timeline.slept == 0


def test_a_succeeded_newest_run_starts_propagation_whatever_an_older_run_is_doing():
    # Negative control: an older run still queued must not postpone the
    # propagation allowance that the newest run's success starts.
    runs = [fork(run_id=800, status="queued"), fork(run_id=900, conclusion="success")]
    timeline = Timeline([(0, runs, "absent")])
    ready, why = timeline.wait()
    assert not ready and why == ("Fork Image run 900 (attempt 1) succeeded, but the exact candidate image is absent "
                                 "from the registry")
    assert waiter.PROPAGATION <= timeline.now < waiter.PROPAGATION + 2 * waiter.POLL


def test_only_this_commits_own_fork_image_push_runs_count():
    for foreign in (fork(sha="d" * 40, status="in_progress"), fork(event="pull_request", status="in_progress"),
                    fork(event="workflow_dispatch", status="in_progress"),
                    fork(path=".github/workflows/tests.yml", status="in_progress")):
        assert waiter.publishers([foreign], SHA) == []
    timeline = Timeline([(0, [fork(sha="d" * 40, status="in_progress")], "absent")])
    ready, why = timeline.wait()
    assert not ready and "no Fork Image push run" in why
    assert waiter.REGISTRATION <= timeline.now < waiter.REGISTRATION + 2 * waiter.POLL


def test_a_successful_publisher_whose_image_never_appears_fails_after_propagation():
    timeline = Timeline([(0, [fork()], "absent")])
    ready, why = timeline.wait()
    assert not ready and "succeeded, but the exact candidate image is absent" in why
    assert waiter.PROPAGATION <= timeline.now < waiter.PROPAGATION + 2 * waiter.POLL
    timeline = Timeline([(0, [fork()], "unreachable")])
    ready, why = timeline.wait()
    assert not ready and "registry did not answer" in why
    # A late registry answer within propagation is enough.
    timeline = Timeline([(0, [fork()], "absent"), (60, [fork()], "present")])
    assert timeline.wait()[0] is True


def test_the_wait_is_bounded_even_while_the_publisher_stays_active_or_the_api_is_silent():
    for runs in ([fork(status="queued")], RuntimeError("API down")):
        timeline = Timeline([(0, runs, "absent")])
        ready, why = timeline.wait()
        assert not ready and why.startswith(f"still waiting after {waiter.CAP // 60} min")
        assert waiter.CAP <= timeline.now < waiter.CAP + 2 * waiter.POLL
        assert timeline.observed <= waiter.CAP // waiter.POLL + 2       # bounded API use


def test_an_api_outage_does_not_hide_a_published_image():
    timeline = Timeline([(0, RuntimeError("API down"), "absent"), (100, RuntimeError("API down"), "present")])
    assert timeline.wait()[0] is True


def test_the_command_line_refuses_anything_but_the_commits_own_full_tag():
    for args in (["--sha", SHA[:12], "--ref", f"x:sha-{SHA[:12]}"], ["--sha", SHA, "--ref", "x:latest"],
                 ["--sha", SHA, "--ref", f"x:sha-{'d' * 40}"]):
        completed = subprocess.run([sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True,
                                   env={"GITHUB_REPOSITORY": "o/r", "PATH": "/usr/bin:/bin"})
        assert completed.returncode == 1 and "::error::" in completed.stdout, args


# -- workflow wiring --------------------------------------------------------------------------------

def _step(name):
    job = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))["jobs"]["identity"]
    return job, {step["name"]: step for step in job["steps"]}


@pytest.mark.parametrize("name", QUALIFIERS)
def test_both_qualifiers_wait_through_the_one_waiter_then_verify_identity_as_before(name):
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    job, steps = _step(name)
    run = steps["Wait for and resolve exact published candidate"]["run"]
    assert 'python3 .github/qualification/candidate_image.py --sha "$GITHUB_SHA" --ref "$source_ref"' in run
    assert steps["Wait for and resolve exact published candidate"]["env"] == {"GH_TOKEN": "${{ github.token }}"}
    # The fixed window is gone; the identity verification after it is unchanged.
    assert "seq 1 120" not in run and "not published for exact SHA" not in run
    order = [run.index(marker) for marker in ('source_ref="${IMAGE_NAME}:sha-${GITHUB_SHA}"', "candidate_image.py",
                                              'index_revision" != "$GITHUB_SHA"', 'platform.architecture == "amd64"',
                                              'platform.architecture == "arm64"')]
    assert order == sorted(order)
    # It reads the Actions API (runs) and nothing more is granted for it.
    workflow = yaml.safe_load(text)
    assert workflow["permissions"]["actions"] == "read"
    # The waiter is on disk before it is run.
    names = list(steps)
    checkout = next(step for step in names if step.startswith("Checkout"))
    assert names.index(checkout) < names.index("Wait for and resolve exact published candidate")


def test_the_two_qualifiers_wait_identically():
    waits = [_step(name)[1]["Wait for and resolve exact published candidate"]["run"] for name in QUALIFIERS]
    head = [run.split('index_revision="')[0] for run in waits]
    assert head[0] == head[1]
