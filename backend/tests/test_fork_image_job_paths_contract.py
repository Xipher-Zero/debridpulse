"""Fork Image's two event-exclusive job paths (``.github/workflows/fork-image.yml``).

A push (or an authorized publishing dispatch) runs ONE job, ``publish``: the
build and smoke qualification of ``build-and-smoke-test`` -- the same steps, as
YAML aliases of the anchored ones, never a copy -- and then, on the same
runner, the immutable publication. Every other event runs
``build-and-smoke-test`` alone, with the workflow's read-only token. So a
publication never waits for a second runner after its smoke test, and a pull
request never holds package-write authority.

The two ``if:`` expressions are evaluated here, for every event shape the
workflow accepts, by a small evaluator of exactly the expression grammar they
use; the checks run on the parsed workflow and on its text."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "fork-image.yml"
SMOKE = ["Checkout", "Read application version", "Log in to Docker Hub", "Set up Docker Buildx",
         "Generate test image metadata", "Build test image", "Smoke test image"]
PUBLICATION = ["Log in to GHCR", "Generate immutable SHA tag", "Verify generated image metadata and immutable tag policy",
               "Check for an existing candidate for this source SHA", "Build and publish",
               "Select the immutable candidate digest", "Verify candidate digest and OCI identity"]


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _jobs() -> dict:
    return _workflow()["jobs"]


def _flat(expression: str) -> str:
    return re.sub(r"\s+", " ", expression).strip()


def _evaluate(expression: str, *, event: str, ref: str, publish_sha: bool) -> bool:
    """The job condition for one event, in exactly the grammar these two
    conditions use: ``!``, ``||``, ``&&``, parentheses, ``==`` on string
    literals, ``startsWith(github.ref, '...')``, ``github.event_name``,
    ``github.ref`` and ``inputs.publish_sha``. Anything else is refused."""
    text = _flat(expression)
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    python = text
    python = re.sub(r"startsWith\(github\.ref, '([^']*)'\)", lambda m: f"REF.startswith({m.group(1)!r})", python)
    python = python.replace("github.event_name", "EVENT").replace("github.ref", "REF").replace("inputs.publish_sha", "PUBLISH")
    python = python.replace("||", " or ").replace("&&", " and ").replace("!(", " not (")
    leftover = re.sub(r"'[^']*'|\b(?:REF|EVENT|PUBLISH|or|and|not|startswith)\b|[()=.\s]", "", python)
    assert leftover == "", f"unexpected expression syntax: {leftover!r} in {text!r}"
    return bool(eval(python, {"__builtins__": {}}, {"REF": ref, "EVENT": event, "PUBLISH": publish_sha}))  # noqa: S307


# (event, ref, publish_sha) -> the one job that runs
CASES = [
    ("push", "refs/heads/1.0.14", False, "publish"),
    ("push", "refs/heads/main", False, "publish"),
    ("push", "refs/heads/staging/x", False, "publish"),
    ("push", "refs/tags/v1.0.14", False, "publish"),
    ("push", "refs/tags/internal-v1.0.14", False, "publish"),
    ("pull_request", "refs/pull/7/merge", False, "build-and-smoke-test"),
    ("pull_request", "refs/pull/7/merge", True, "build-and-smoke-test"),
    ("workflow_dispatch", "refs/heads/main", True, "publish"),
    ("workflow_dispatch", "refs/heads/feature/x", True, "publish"),
    ("workflow_dispatch", "refs/heads/fix/x", True, "publish"),
    ("workflow_dispatch", "refs/heads/chore/x", True, "publish"),
    ("workflow_dispatch", "refs/heads/main", False, "build-and-smoke-test"),
    ("workflow_dispatch", "refs/heads/1.0.14", True, "build-and-smoke-test"),
    ("workflow_dispatch", "refs/heads/staging/x", True, "build-and-smoke-test"),
]


@pytest.mark.parametrize("event,ref,publish_sha,expected", CASES)
def test_every_event_takes_exactly_one_path(event, ref, publish_sha, expected):
    jobs = _jobs()
    runs = {name for name in ("build-and-smoke-test", "publish")
            if _evaluate(jobs[name]["if"], event=event, ref=ref, publish_sha=publish_sha)}
    assert runs == {expected}


def test_the_two_conditions_are_exact_complements():
    jobs = _jobs()
    publishes = _flat(jobs["publish"]["if"])
    smoke = _flat(jobs["build-and-smoke-test"]["if"])
    assert smoke == f"${{{{ !( {publishes} ) }}}}"
    assert set(_workflow().get("on", _workflow().get(True))) == {"push", "pull_request", "workflow_dispatch"}


def test_the_non_publishing_path_holds_no_publication_authority():
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    smoke = workflow["jobs"]["build-and-smoke-test"]
    assert "permissions" not in smoke and "needs" not in smoke and "outputs" not in smoke
    assert [step["name"] for step in smoke["steps"]] == SMOKE
    body = yaml.safe_dump(smoke)
    for forbidden in ("ghcr.io", "GITHUB_TOKEN", "packages: write", "push: true", "login-action", "setup-qemu"):
        assert forbidden not in body, forbidden


def test_publication_runs_the_same_smoke_qualification_first_on_its_own_runner():
    jobs = _jobs()
    publish = jobs["publish"]
    assert "needs" not in publish                                    # no handoff to wait for
    assert publish["permissions"] == {"contents": "read", "packages": "write"}
    assert set(publish["outputs"]) == {"candidate_ref", "candidate_digest", "candidate_reused"}
    names = [step["name"] for step in publish["steps"]]
    smoke_steps = [step for step in publish["steps"] if step["name"] in SMOKE]
    # The very same steps, in the same order, with identical bodies.
    assert smoke_steps == jobs["build-and-smoke-test"]["steps"]
    # QEMU only adds emulation; every smoke step precedes any registry login or push.
    assert names[: len(SMOKE) + 1] == SMOKE[:3] + ["Set up QEMU"] + SMOKE[3:]
    assert names[len(SMOKE) + 1:] == PUBLICATION
    ids = [step["id"] for step in publish["steps"] if "id" in step]
    assert len(ids) == len(set(ids))


def test_the_smoke_steps_exist_once_as_text_and_are_reused_by_alias():
    text = WORKFLOW.read_text(encoding="utf-8")
    for anchor, name in (("checkout", "Checkout"), ("read-version", "Read application version"),
                         ("docker-hub-login", "Log in to Docker Hub"), ("setup-buildx", "Set up Docker Buildx"),
                         ("test-image-metadata", "Generate test image metadata"), ("build-test-image", "Build test image"),
                         ("smoke-test-image", "Smoke test image")):
        assert text.count(f"- &{anchor}\n        name: {name}\n") == 1, anchor
        assert text.count(f"- *{anchor}\n") == 1, anchor
        assert text.count(f"name: {name}\n") == 1, name
    assert "<<:" not in text                                          # merge keys are not supported by Actions


def test_the_testing_alias_follows_the_one_publishing_job():
    alias = _jobs()["testing-alias"]
    assert alias["needs"] == "publish"
    assert alias["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/1.0.14'"
