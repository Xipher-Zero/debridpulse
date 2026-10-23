"""The one decision behind ``ghcr.io/xipher-zero/debridpulse:1.0.14-testing``.

``1.0.14-testing`` is a rolling development alias: it follows the newest
1.0.14 commit whose Fork Image run has published its complete multi-architecture
image. It is not a qualification or release target -- the six required
workflows stay independently authoritative, and ``latest`` and the release
tags stay Release Promotion's.

Fork Image's last job calls ``.github/workflows/testing-alias.yml`` once its own
publication succeeded. That job gathers facts -- the branch tip, this commit's
immutable ``sha-<commit>`` index and what the alias resolves to now -- and this
module alone decides from them whether the alias moves. A commit that is no
longer the tip never moves it (the newer commit's own publication does).
Standard library only: no network, no registry, no token.
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
PLATFORMS = ("amd64", "arm64")
REVISION = "org.opencontainers.image.revision"
_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


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


def decide(*, tip, sha, digest=None, index=None, alias_digest=None) -> dict:
    """``advance`` (move the alias to ``digest``), ``current`` (it already
    resolves there), ``superseded`` (``sha`` is no longer the tip: no write)
    or ``unchanged`` (a fault, with the reason and a short ``cause``) for the
    commit ``sha`` whose Fork Image run just published."""
    result = {"alias": f"{IMAGE}:{ALIAS}", "branch": BRANCH, "commit": sha, "tip": tip, "source_tag": None,
              "digest": None, "platforms": None, "previous_alias_digest": alias_digest}

    def unchanged(reason, cause):
        return {**result, "action": "unchanged", "reason": reason, "cause": cause}

    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        return unchanged("the published commit is not a full commit SHA", "commit unreadable")
    if not isinstance(tip, str) or not _SHA.fullmatch(tip):
        return unchanged(f"the {BRANCH} tip could not be read", "branch tip unreadable")
    result["source_tag"] = f"{IMAGE}:sha-{sha}"
    if tip != sha:
        return {**result, "action": "superseded",
                "reason": f"{BRANCH} is now {tip[:12]}; its own Fork Image publication moves the alias"}
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        return unchanged("this commit's immutable image did not resolve to an index digest", "image unresolved")
    result["digest"] = digest
    children = index_identity(index, sha)
    if children is None:
        return unchanged("the image index does not name this commit with one amd64 and one arm64 image",
                         "image index unverified")
    result["platforms"] = children
    if alias_digest == digest:
        return {**result, "action": "current", "reason": "the alias already resolves to this commit's image"}
    return {**result, "action": "advance", "reason": "this commit is the tip and its image is published"}


# GitHub's limit for a commit status description.
STATUS_LIMIT = 140
RETRY = "Retry: re-run Fork Image's Testing alias job."
RECONCILE = "Reconcile: re-run Fork Image's Testing alias job."
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
    image (``write`` "advanced", or nothing to write: "current"). ``pending``
    when this commit is no longer the one to publish (superseded, or the tip
    moved in the turn before any write). ``failure`` for every fault, each
    with its recovery action whole: nothing decided or a fault before any
    write (no write was made); a failed write after which the registry was
    observed still holding ``previous`` ("unchanged"); and a write whose
    result could not be observed ("unknown", or no report) -- reported as
    unknown, never as unchanged, since the registry may have accepted it
    before the client failed."""
    if not isinstance(decision, dict) or decision.get("action") not in {"advance", "current", "superseded",
                                                                        "unchanged"}:
        return "failure", _status(f"{ALIAS} NOT updated:", "not evaluated (API error); no write was made.", RETRY)
    action = decision["action"]
    if action == "current":
        return "success", f"{ALIAS} is this commit's image."
    if action == "superseded":
        tip = str(decision.get("tip") or "")[:12]
        return "pending", f"{ALIAS} not moved: {BRANCH} is now {tip}; no write was made."
    if action == "advance":
        if write == "advanced":
            return "success", f"{ALIAS} is this commit's image."
        if write == "not_attempted":
            return "pending", f"{ALIAS} not moved: {BRANCH} advanced before the write; no write was made."
        if write == "unchanged":
            still = f"still holds {previous[:19]}" if previous and previous.startswith("sha256:") else "has no such tag"
            return "failure", _status(f"{ALIAS} NOT updated: write failed; registry observed", f"{still}.", RETRY)
        return "failure", _status(f"{ALIAS} state UNKNOWN: write unconfirmed, inspect the tag.", "", RECONCILE)
    cause = str(decision.get("cause") or decision.get("reason") or "")
    return "failure", _status(f"{ALIAS} NOT updated, no write made:", cause + ".", RETRY)


def _load(path):
    if not path:
        return None
    text = Path(path).read_text(encoding="utf-8").strip()
    return json.loads(text) if text else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tip", default="")
    parser.add_argument("--sha", default="")
    parser.add_argument("--digest", default="")
    parser.add_argument("--index", default="", help="JSON: this commit's raw OCI index")
    parser.add_argument("--alias-digest", default="")
    parser.add_argument("--outcome", default="", help="JSON: a decision; print its commit status as JSON")
    parser.add_argument("--write", default="", help="What the write step observed: " + ", ".join(sorted(WRITES)))
    parser.add_argument("--previous", default="", help="The alias digest before the write ('' if absent)")
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
    try:
        index = _load(args.index)
    except (OSError, ValueError):
        index = None
    print(json.dumps(decide(tip=args.tip or None, sha=args.sha or None, digest=args.digest or None, index=index,
                            alias_digest=args.alias_digest or None)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
