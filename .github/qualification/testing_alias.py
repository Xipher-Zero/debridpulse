"""The one decision behind ``ghcr.io/xipher-zero/debridpulse:1.0.14-testing``.

Each of the six required workflows ends, on a 1.0.14 push, with a ``Testing
alias`` job that calls ``.github/workflows/testing-alias.yml`` (a reusable
workflow: GitHub runs ``workflow_dispatch``, ``workflow_run``,
``repository_dispatch`` and ``schedule`` workflows only from the default
branch, so a 1.0.14-only workflow can be started by nothing else). That job
gathers facts -- the current tip of the branch, the push-event runs of the six
required workflows for that exact commit (with the jobs of any still running),
the commit's immutable ``sha-<commit>`` OCI index, its two signed
qualification attestations and what the alias resolves to now -- and this
module alone decides from them whether the alias may move. Standard library
only: no network, no registry, no token; every fact arrives as JSON and the
answer leaves as JSON.

The alias follows only the CURRENT tip, and only once that exact commit is fully
qualified: every one of the six required workflows' latest attempt passed, its
index carries both architectures and names that commit as its revision, and
both signed attestations bind that commit to that index and to both of its
platform images. Anything else leaves the alias where it is.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BRANCH = "1.0.14"
ALIAS = "1.0.14-testing"
IMAGE = "ghcr.io/xipher-zero/debridpulse"
# The six required workflows, by the file that defines them (a run's ``path``),
# never by display name.
REQUIRED = (
    (".github/workflows/tests.yml", "Tests"),
    (".github/workflows/browser-runtime.yml", "Browser Runtime"),
    (".github/workflows/codeql.yml", "CodeQL"),
    (".github/workflows/fork-image.yml", "Fork Image"),
    (".github/workflows/container-security.yml", "Container Security"),
    (".github/workflows/candidate-runtime-qualification.yml", "Candidate Runtime Qualification"),
)
PLATFORMS = ("amd64", "arm64")
REVISION = "org.opencontainers.image.revision"
_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


# The name every required workflow gives its final job, the one calling
# ``testing-alias.yml``; GitHub lists that call's job as "<name> / <job>".
REQUEST_JOB = "Testing alias"


def _request_job(job) -> bool:
    name = str((job or {}).get("name") or "")
    return name == REQUEST_JOB or name.startswith(REQUEST_JOB + " /")


def passed_while_running(jobs) -> bool:
    """Whether a run that has not finished has nonetheless passed: its
    ``Testing alias`` job has been created -- which happens only once every
    job it needs, that is every other job of the run, succeeded -- and every
    other job listed for its latest attempt completed successfully. That is
    how the run evaluating (its own alias job still running) and another
    finishing at the same moment both count as passed."""
    jobs = [job for job in jobs or () if isinstance(job, dict)]
    request = [job for job in jobs if _request_job(job)]
    others = [job for job in jobs if not _request_job(job)]
    return bool(request) and bool(others) and all(
        job.get("status") == "completed" and job.get("conclusion") == "success" for job in others)


def gates(runs, sha: str, jobs=None) -> dict:
    """Each required workflow's verdict for ``sha``: its push run on
    ``BRANCH`` for exactly that commit, the newest if there were several. A
    re-run attempt updates that same run, so its status and conclusion are
    always the latest attempt's. ``success`` for a completed, successful run,
    or for a running one that ``passed_while_running`` from ``jobs`` (run id
    to its latest attempt's jobs); ``missing``, ``running`` or the run's own
    conclusion otherwise."""
    jobs = jobs or {}
    verdicts = {}
    for path, name in REQUIRED:
        candidates = [run for run in runs or () if isinstance(run, dict) and run.get("path") == path
                      and run.get("head_sha") == sha and run.get("event") == "push"
                      and run.get("head_branch") == BRANCH]
        run = max(candidates, key=lambda item: int(item.get("id") or 0), default=None)
        if run is None:
            verdicts[name] = {"state": "missing", "run_id": None, "attempt": None}
            continue
        if run.get("status") != "completed":
            listed = jobs.get(str(run.get("id"))) or jobs.get(run.get("id"))
            state = "success" if passed_while_running(listed) else "running"
        else:
            state = "success" if run.get("conclusion") == "success" else str(run.get("conclusion") or "unknown")
        verdicts[name] = {"state": state, "run_id": run.get("id"), "attempt": run.get("run_attempt")}
    return verdicts


def unfinished(runs, sha: str) -> list:
    """The ids of the required push runs for ``sha`` that have not finished:
    the only runs whose jobs ``gates`` needs."""
    verdicts = gates(runs, sha)
    return [item["run_id"] for item in verdicts.values() if item["state"] == "running"]


def index_identity(index, sha: str) -> dict | None:
    """The index's two platform image digests, when it names ``sha`` as its
    revision and carries exactly one Linux image per required architecture."""
    if not isinstance(index, dict) or (index.get("annotations") or {}).get(REVISION) != sha:
        return None
    children = {}
    for platform in PLATFORMS:
        found = [item.get("digest") for item in index.get("manifests") or ()
                 if isinstance(item, dict) and (item.get("platform") or {}).get("os") == "linux"
                 and (item.get("platform") or {}).get("architecture") == platform]
        if len(found) != 1 or not isinstance(found[0], str) or not _DIGEST.fullmatch(found[0]):
            return None
        children[platform] = found[0]
    return children if len(set(children.values())) == len(PLATFORMS) else None


def attested(verifications, kind: str, sha: str, digest: str, children: dict) -> bool:
    """Whether one verified attestation of ``kind`` ("security" or
    "runtime") binds ``sha`` to ``digest`` and both platform images, with
    every gate it records passed -- the predicate Release Promotion reads."""
    passed = {"security": ("vulnerability_gate",), "runtime": ("runtime", "non_root")}[kind]
    for item in verifications or ():
        predicate = (((item or {}).get("verificationResult") or {}).get("statement") or {}).get("predicate") or {}
        platforms = predicate.get("platforms") or {}
        if (predicate.get("commit_sha") == sha and predicate.get("manifest_digest") == digest
                and predicate.get("result") == "success"
                and all((platforms.get(f"linux/{platform}") or {}).get("digest") == children[platform]
                        and all((platforms.get(f"linux/{platform}") or {}).get(field) == "passed" for field in passed)
                        for platform in PLATFORMS)):
            return True
    return False


def decide(*, tip, runs, jobs=None, digest=None, index=None, security=None, runtime=None,
           alias_digest=None) -> dict:
    """``advance`` (move the alias to ``digest``), ``current`` (it already
    resolves there) or ``unchanged`` (leave it, with the reason). Every fact
    must hold for the one commit at the branch tip."""
    result = {"alias": f"{IMAGE}:{ALIAS}", "branch": BRANCH, "commit": tip, "source_tag": None,
              "digest": None, "platforms": None, "gates": {}, "previous_alias_digest": alias_digest}

    def unchanged(reason, cause):
        return {**result, "action": "unchanged", "reason": reason, "cause": cause}

    if not isinstance(tip, str) or not _SHA.fullmatch(tip):
        return unchanged("the branch tip is not a full commit SHA", "branch tip unreadable")
    result["source_tag"] = f"{IMAGE}:sha-{tip}"
    result["gates"] = verdicts = gates(runs, tip, jobs)
    pending = [name for name, item in verdicts.items() if item["state"] != "success"]
    if pending:
        return unchanged("not qualified: " + ", ".join(f"{name} {verdicts[name]['state']}" for name in pending),
                         "gates: " + ", ".join(f"{name} {verdicts[name]['state']}" for name in pending))
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        return unchanged("the immutable source image did not resolve to an index digest", "source image unresolved")
    result["digest"] = digest
    children = index_identity(index, tip)
    if children is None:
        return unchanged("the source index does not name this commit with one amd64 and one arm64 image",
                         "source index unverified")
    result["platforms"] = children
    for kind, verifications in (("security", security), ("runtime", runtime)):
        if not attested(verifications, kind, tip, digest, children):
            return unchanged(f"no verified {kind} attestation binds this commit to this index and both images",
                             f"{kind} attestation unverified")
    if alias_digest == digest:
        return {**result, "action": "current", "reason": "the alias already resolves to this qualified index"}
    return {**result, "action": "advance", "reason": "every required qualification passed for the current tip"}


# GitHub's limit for a commit status description.
STATUS_LIMIT = 140
RETRY = f"Retry: re-run a Testing alias job on {BRANCH}."
RECONCILE = f"Reconcile: re-run a Testing alias job on {BRANCH}."
# What the write step observed in the registry after its attempt.
WRITES = frozenset({"advanced", "unchanged", "unknown", "not_attempted"})


def _status(head: str, detail: str = "", tail: str = "") -> str:
    """``head`` and ``tail`` whole; ``detail`` between them shortened at a
    word boundary (with an ellipsis) to keep the whole within
    ``STATUS_LIMIT``. A recovery instruction is always a ``tail``: it is
    never what gets cut."""
    parts = [head, detail, tail]
    room = STATUS_LIMIT - len(" ".join(part for part in (head, tail) if part)) - (1 if detail else 0)
    if detail and len(detail) > room:
        cut = detail[:max(0, room - 1)].rsplit(" ", 1)[0].rstrip(",;: ")
        parts[1] = (cut + "…") if cut else ""
    return " ".join(part for part in parts if part)


def outcome(decision, *, write: str | None = None, previous: str | None = None) -> tuple[str, str]:
    """The ``1.0.14-testing`` commit status for one evaluation: ``(state,
    description)``, every description within ``STATUS_LIMIT``.

    ``success`` only when the registry was observed holding this commit's
    qualified index (``write`` "advanced", or nothing to write: "current").
    ``failure`` for every fault, each with its recovery action whole: nothing
    decided (no write was made); six passed gates whose evidence did not
    verify (no write was made); a failed write after which the registry was
    observed still holding ``previous`` ("unchanged"); and a write whose result could not be
    observed ("unknown", or no observation at all) -- reported as unknown,
    never as unchanged, since the registry may have accepted it before the
    client failed. ``pending`` while this commit is not (yet) the one to
    publish, including a tip that moved in the turn before any write."""
    if not isinstance(decision, dict) or decision.get("action") not in {"advance", "current", "unchanged"}:
        return "failure", _status(f"{ALIAS} NOT updated:", "not evaluated (API error); no write was made.", RETRY)
    action = decision["action"]
    qualified = bool(decision.get("gates")) and all(item.get("state") == "success"
                                                    for item in decision["gates"].values())
    if action == "current":
        return "success", f"{ALIAS} is this commit's qualified index."
    if action == "advance":
        if write == "advanced":
            return "success", f"{ALIAS} is this commit's qualified index."
        if write == "not_attempted":
            return "pending", f"{ALIAS} not moved: {BRANCH} advanced before the write; no write was made."
        if write == "unchanged":
            still = f"still holds {previous[:19]}" if previous and previous.startswith("sha256:") else "has no such tag"
            return "failure", _status(f"{ALIAS} NOT updated: write failed; registry observed", f"{still}.", RETRY)
        return "failure", _status(f"{ALIAS} state UNKNOWN: write unconfirmed, inspect the tag.", "", RECONCILE)
    cause = str(decision.get("cause") or decision.get("reason") or "")
    if qualified:
        return "failure", _status(f"{ALIAS} NOT updated, no write made:", cause + ".", RETRY)
    return "pending", _status(f"{ALIAS} not moved yet:", cause)


def _load(path):
    if not path:
        return None
    text = Path(path).read_text(encoding="utf-8").strip()
    return json.loads(text) if text else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tip", required=True)
    parser.add_argument("--runs", required=True, help="JSON: the API's workflow_runs list (or its envelope)")
    parser.add_argument("--jobs", default="", help="JSON: {run id: that run's latest-attempt jobs list}")
    parser.add_argument("--digest", default="")
    parser.add_argument("--index", default="", help="JSON: the source tag's raw OCI index")
    parser.add_argument("--security", default="", help="JSON: gh attestation verify output")
    parser.add_argument("--runtime", default="", help="JSON: gh attestation verify output")
    parser.add_argument("--alias-digest", default="")
    parser.add_argument("--unfinished", action="store_true", help="Print the unfinished required run ids, one per line.")
    parser.add_argument("--outcome", default="", help="JSON: a decision; print its commit status as JSON")
    parser.add_argument("--write", default="", help="What the write step observed: " + ", ".join(sorted(WRITES)))
    parser.add_argument("--previous", default="", help="The alias digest before the write ('' if absent)")
    parser.add_argument("--gates-only", action="store_true",
                        help="Decide only from the workflow runs whether the tip is qualified.")
    args = parser.parse_args(argv)
    if args.outcome:
        try:
            decision = _load(args.outcome)
        except (OSError, ValueError):
            decision = None
        state, description = outcome(decision, write=args.write if args.write in WRITES else None,
                                     previous=args.previous or None)
        print(json.dumps({"state": state, "description": description}))
        return 0
    runs = _load(args.runs)
    runs = runs.get("workflow_runs") if isinstance(runs, dict) else runs
    jobs = _load(args.jobs) or {}
    if args.unfinished:
        print("\n".join(str(item) for item in unfinished(runs, args.tip)))
        return 0
    if args.gates_only:
        verdicts = gates(runs, args.tip, jobs)
        print(json.dumps({"commit": args.tip, "gates": verdicts,
                          "qualified": all(item["state"] == "success" for item in verdicts.values())}))
        return 0
    print(json.dumps(decide(tip=args.tip, runs=runs, jobs=jobs, digest=args.digest or None, index=_load(args.index),
                            security=_load(args.security), runtime=_load(args.runtime),
                            alias_digest=args.alias_digest or None)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
