"""Wait for the exact candidate image that this commit's Fork Image run publishes.

Container Security and Candidate Runtime Qualification start on the same push
as Fork Image, but can only qualify ``ghcr.io/xipher-zero/debridpulse:sha-<commit>``
once Fork Image has published it -- which takes as long as that run's build,
its runner queue and its multi-architecture push take. So they wait on that
run, not on a clock: while the commit's Fork Image push run is queued or
running (including an authorized re-run of it), they keep waiting; once the
image is in the registry they go on; and once the run has finished without it
they fail, saying why. The wait is still bounded (``CAP``).

This only decides whether the image is there to qualify. Its identity -- the
revision it names, its two platform children -- is still verified by the
calling step, exactly as before.

Standard library only. ``wait`` takes its observations (the GitHub API's runs,
the registry's answer, the clock) as functions, so the decision is tested with
hand-authored facts; the command line supplies the real ones through ``gh`` and
``docker buildx imagetools``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

FORK_IMAGE = ".github/workflows/fork-image.yml"
# Run states in which the publisher may still publish (the API's run statuses).
ACTIVE = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})
POLL = 20                      # seconds between observations
CAP = 60 * 60                  # the longest any wait may last
REGISTRATION = 10 * 60         # how long a push's Fork Image run may take to appear
PROPAGATION = 2 * 60           # how long a finished publication may take to be readable
_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def publishers(runs, sha: str) -> list[dict]:
    """The Fork Image push runs for exactly ``sha``, newest first. A re-run
    updates its own run, so each one's status is its latest attempt's. The
    first is the one authority: an older run, active or not, decides nothing."""
    found = [run for run in runs or () if isinstance(run, dict) and run.get("path") == FORK_IMAGE
             and run.get("head_sha") == sha and run.get("event") == "push"]
    return sorted(found, key=lambda run: int(run.get("id") or 0), reverse=True)


def _named(run: dict) -> str:
    return f"Fork Image run {run.get('id')} (attempt {run.get('run_attempt') or 1})"


def verdict(runs, image: str, *, sha: str, waited: float, settled_for: float | None) -> tuple[str, str]:
    """``("ready" | "wait" | "fail", why)`` from one observation.

    ``runs`` is the API's run list (``None`` when the API did not answer),
    ``image`` the registry's answer for the exact tag ("present", "absent" or
    "unreachable"), ``sha`` the commit, ``waited`` the seconds since waiting began and
    ``settled_for`` how long the newest publisher run has been seen finished
    (``None`` while it is not). Only that newest run decides; the cap is the
    caller's."""
    if image == "present":
        return "ready", "the exact candidate image is published"
    if runs is None:
        return "wait", f"image {image}; the GitHub API did not answer"
    found = publishers(runs, sha)
    if not found:
        if waited < REGISTRATION:
            return "wait", f"image {image}; no Fork Image push run for this commit yet"
        return "fail", (f"no Fork Image push run for this commit appeared within {REGISTRATION // 60} min: "
                        "nothing will publish the candidate")
    newest = found[0]
    if newest.get("status") in ACTIVE or newest.get("status") != "completed":
        return "wait", f"image {image}; {_named(newest)} is {newest.get('status')}"
    if newest.get("conclusion") != "success":
        return "fail", (f"{_named(newest)} concluded {newest.get('conclusion') or 'without a conclusion'}: "
                        "the candidate was not published")
    if settled_for is None or settled_for < PROPAGATION:
        return "wait", f"image {image}; {_named(newest)} succeeded, waiting for the registry to serve it"
    if image == "unreachable":
        return "fail", f"{_named(newest)} succeeded, but the registry did not answer for {PROPAGATION // 60} min"
    return "fail", f"{_named(newest)} succeeded, but the exact candidate image is absent from the registry"


def wait(sha: str, *, fetch_runs, probe, sleep=time.sleep, clock=time.monotonic, cap: float = CAP,
         poll: float = POLL, log=print) -> tuple[bool, str]:
    """Observe until the image is published (``True``), the publisher has
    finished without it, or ``cap`` passes (``False``); with why."""
    start, settled_at = clock(), None
    while True:
        now = clock()
        waited = now - start
        image = probe()
        try:
            runs = list(fetch_runs())
        except Exception as exc:  # noqa: BLE001 -- an unanswered API is an observation, not a verdict
            log(f"[{int(waited)}s] GitHub API: {type(exc).__name__}")
            runs = None
        found = publishers(runs, sha) if runs is not None else []
        finished = bool(found) and found[0].get("status") == "completed"      # the newest run alone
        settled_at = (settled_at if settled_at is not None else now) if finished else None
        state, why = verdict(runs, image, sha=sha, waited=waited,
                             settled_for=None if settled_at is None else now - settled_at)
        log(f"[{int(waited)}s] {why}")
        if state == "ready":
            return True, why
        if state == "fail":
            return False, why
        if waited >= cap:
            return False, f"still waiting after {int(cap // 60)} min: {why}"
        sleep(poll)


def _fetch_runs(repository: str, sha: str):
    def fetch():
        output = subprocess.run(
            ["gh", "api", "-H", "X-GitHub-Api-Version: 2022-11-28",
             f"repos/{repository}/actions/workflows/fork-image.yml/runs?head_sha={sha}&event=push&per_page=100"],
            check=True, capture_output=True, text=True, timeout=60).stdout
        return json.loads(output).get("workflow_runs") or []
    return fetch


def _probe(reference: str):
    def probe():
        try:
            answer = subprocess.run(["docker", "buildx", "imagetools", "inspect", reference],
                                    capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return "unreachable"
        if answer.returncode == 0 and _DIGEST.search(answer.stdout or ""):
            return "present"
        if re.search(r"(?i)not found|manifest unknown", (answer.stderr or "") + (answer.stdout or "")):
            return "absent"
        return "unreachable"
    return probe


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sha", required=True)
    parser.add_argument("--ref", required=True, help="the exact image reference, ghcr.io/...:sha-<commit>")
    args = parser.parse_args(argv)
    if not _SHA.fullmatch(args.sha) or not args.ref.endswith(f":sha-{args.sha}"):
        print("::error::The candidate must be a full commit SHA and its own sha-<commit> tag")
        return 1
    ready, why = wait(args.sha, fetch_runs=_fetch_runs(os.environ["GITHUB_REPOSITORY"], args.sha),
                      probe=_probe(args.ref))
    if not ready:
        print(f"::error::Immutable candidate {args.ref} is not available: {why}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
