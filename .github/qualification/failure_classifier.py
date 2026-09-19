#!/usr/bin/env python3
"""Canonical candidate-vs-anchor failure classifier for DebridPulse qualification.

This file is the sole semantic owner of:

* failure identity normalization (browser + pytest),
* known-flake registry validation,
* the bounded diagnostic budget,
* qualification-anchor derivation,
* classification semantics, and the machine/human readable results.

Workflows own *execution* (they run tests and write result files); this module
only reads local files and local git objects. It never calls a network API and
never runs a test.

Subcommands (see docs/QUALIFICATION_DETERMINISM.md for the full policy)::

    budget            print the bounded diagnostic budget as KEY=VALUE lines
    resolve-anchor    derive QUALIFICATION_ANCHOR_SHA from the event (git only)
    metadata          publish CANDIDATE_SHA / ANCHOR / EVENT / RUN_ID identity
    plan              extract the failing cases of a full run and check the budget
    discriminate      list the cases that need the single bounded second-stage runs
    classify          classify a failed full run against candidate + anchor runs
    validate-registry validate the known-flake registry

Result directory layout consumed by ``classify`` (one directory per ref)::

    <candidate-results>/full/results.json|results.xml   original full run (immutable evidence)
    <candidate-results>/full/exit_code.txt               the runner's exit status for that run
    <candidate-results>/isolated/*.json|*.xml            bounded isolated runs of the failing cases
    <anchor-results>/isolated/*.json|*.xml               the same cases on the anchor
    <ref>/isolated/infrastructure_failure.txt            written by the workflow when the ref's
                                                         own checkout/build/install/start failed
    <ref>/isolated/budget_exhausted.txt                  written when the wall-time budget ran out

Exit codes (``classify``)::

    0   PASS                      original full run passed
    10  KNOWN_FLAKE               non-blocking; callers MUST surface the warning
    20  CANDIDATE_REGRESSION      blocking
    21  ANCHOR_REPRODUCED_FLAKE   blocking until a determinism decision/fix
    22  INCONCLUSIVE              blocking
    23  INFRASTRUCTURE_FAILURE    blocking

Only 0 and 10 may be treated as success. Usage errors exit 2 (argparse).
``plan`` exits 0 (no failures), 11 (failures within budget), 12 (failures over
budget), 13 (results unusable). ``discriminate`` exits 0 (no second stage needed)
or 14 (the listed cases need the second stage). ``validate-registry`` exits 0 or 3.
``resolve-anchor`` exits 0 (resolved) or 1 (unresolved, fail closed).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# The one effective owner of the bounded diagnostic budget.
# ---------------------------------------------------------------------------
MAX_CASES_TO_CLASSIFY = 3
ISOLATED_RUNS_PER_REF = 8
# The single bounded second stage: when the first stage cannot discriminate candidate from anchor,
# each ref gets this many additional isolated runs of only the affected case(s), once.
DISCRIMINATOR_RUNS_PER_REF = 16
# CANDIDATE_REGRESSION needs the candidate's failure rate to exceed the anchor's with a one-sided
# exact (Fisher) p-value at or below this. It is an evidentiary bar, not a failure-count threshold.
SPECIFICITY_ALPHA = 0.01
MAX_CLASSIFICATION_WALL_TIME_SECONDS = 15 * 60
FULL_SUITE_AUTOMATIC_RERUNS = 0

# Registry bounds ("no unlimited entries", "expiry required").
MAX_REGISTRY_ENTRIES = 10
MAX_REGISTRY_EXPIRY_DAYS = 180
MAX_REGISTRY_CLASSIFIER_RUNS = 50

PASS = "PASS"
CANDIDATE_REGRESSION = "CANDIDATE_REGRESSION"
ANCHOR_REPRODUCED_FLAKE = "ANCHOR_REPRODUCED_FLAKE"
KNOWN_FLAKE = "KNOWN_FLAKE"
INCONCLUSIVE = "INCONCLUSIVE"
INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"

CLASSIFICATIONS = (
    PASS,
    CANDIDATE_REGRESSION,
    ANCHOR_REPRODUCED_FLAKE,
    KNOWN_FLAKE,
    INCONCLUSIVE,
    INFRASTRUCTURE_FAILURE,
)

EXIT_CODES = {
    PASS: 0,
    KNOWN_FLAKE: 10,
    CANDIDATE_REGRESSION: 20,
    ANCHOR_REPRODUCED_FLAKE: 21,
    INCONCLUSIVE: 22,
    INFRASTRUCTURE_FAILURE: 23,
}
NON_BLOCKING = frozenset({PASS, KNOWN_FLAKE})

# Precedence when several failing cases disagree (most severe first).
_SEVERITY = (
    INFRASTRUCTURE_FAILURE,
    CANDIDATE_REGRESSION,
    INCONCLUSIVE,
    ANCHOR_REPRODUCED_FLAKE,
    KNOWN_FLAKE,
)

DOMAINS = ("browser", "pytest")
CASE_SEPARATOR = " › "
UNRESOLVED_ANCHOR = "UNRESOLVED"
ZERO_SHA = "0" * 40
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

PLAN_NO_FAILURES = 0
PLAN_WITHIN_BUDGET = 11
PLAN_OVER_BUDGET = 12
PLAN_UNUSABLE = 13
DISCRIMINATOR_NEEDED = 14
REGISTRY_INVALID = 3
ANCHOR_UNRESOLVED = 1


def is_sha(value: str | None) -> bool:
    return bool(value) and bool(_SHA_RE.match(str(value)))


# ---------------------------------------------------------------------------
# Normalization: volatile noise only.
# ---------------------------------------------------------------------------
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_EPOCH_MS_RE = re.compile(r"\b1[5-9]\d{11}\b")
_UUID_DASHED_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_UUID_HEX_RE = re.compile(r"\b[0-9a-f]{32}\b")
_UUID_ELIDED_RE = re.compile(r"\b[0-9a-f]{6,}\.\.\.[0-9a-f]{6,}\b")
_SHA40_RE = re.compile(r"\b[0-9a-f]{40}\b")
_RUN_ID_RE = re.compile(r"(?i)\b(workflow[_ -]?run[_ -]?id|run[_ -]?id|run[_ -]?number)([=: ]+)\d+")
_ORIGIN_RE = re.compile(r"(?:https?://)?(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\]):\d+")
_TMP_PATH_RE = re.compile(
    r"(?:/private)?(?:/tmp|/var/tmp|/var/folders|/home/runner/work/_temp|/run/user)/[^\s'\"):,;]*"
)
_WORKSPACE_RE = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][^\s\\/'\":)]+)+?[\\/](?=(?:frontend|backend|\.github)[\\/])")
_LOCATION_RE = re.compile(r":\d+:\d+\b")
_PYTEST_LOCATION_RE = re.compile(r"(\.py):\d+\b")
_WS_RE = re.compile(r"[ \t]+")


def normalize_text(text: str | None) -> str:
    """Strip volatile noise (temp paths, timestamps, ids, ports) and nothing else.

    Assertion text, exception categories, expected/actual semantic state and
    the failing test identity are deliberately preserved.
    """
    value = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = _ANSI_RE.sub("", value)
    value = _WORKSPACE_RE.sub("", value)
    value = _TMP_PATH_RE.sub("<tmp>", value)
    value = _ORIGIN_RE.sub("<origin>", value)
    value = _TIMESTAMP_RE.sub("<timestamp>", value)
    value = _EPOCH_MS_RE.sub("<epoch-ms>", value)
    value = _UUID_DASHED_RE.sub("<uuid>", value)
    value = _UUID_ELIDED_RE.sub("<uuid>", value)
    value = _SHA40_RE.sub("<sha>", value)
    value = _UUID_HEX_RE.sub("<uuid>", value)
    value = _RUN_ID_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<run-id>", value)
    value = _LOCATION_RE.sub(":<line>:<col>", value)
    value = _PYTEST_LOCATION_RE.sub(r"\1:<line>", value)
    lines = [_WS_RE.sub(" ", line).strip() for line in value.split("\n")]
    return "\n".join(line for line in lines if line)


def signature_id(signature: str) -> str:
    return "sha256:" + hashlib.sha256(signature.encode("utf-8")).hexdigest()


def slugify(case_id: str) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", case_id.lower()).strip("-")[:60].strip("-") or "case"
    digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:8]
    return f"{stem}-{digest}"


# ---------------------------------------------------------------------------
# Result parsing.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Outcome:
    case_id: str
    status: str  # passed | failed | skipped
    signature: str = ""
    location: str = ""
    selector: tuple[tuple[str, str], ...] = ()


@dataclass
class ParsedResults:
    outcomes: list[Outcome] = field(default_factory=list)
    runner_errors: list[str] = field(default_factory=list)
    unusable: str = ""
    exit_code: int | None = None

    @property
    def executed(self) -> int:
        return sum(1 for item in self.outcomes if item.status in ("passed", "failed"))

    @property
    def passed(self) -> int:
        return sum(1 for item in self.outcomes if item.status == "passed")

    @property
    def failed(self) -> int:
        return sum(1 for item in self.outcomes if item.status == "failed")

    @property
    def skipped(self) -> int:
        return sum(1 for item in self.outcomes if item.status == "skipped")


def _browser_signature(error: dict) -> str:
    message = str(error.get("message") or "")
    header = re.split(r"\n\s*Call log:", _ANSI_RE.sub("", message), maxsplit=1)[0]
    # Stack frames carry ref-specific absolute paths and evaluation offsets, not assertion semantics.
    header = "\n".join(line for line in header.split("\n") if not line.strip().startswith("at "))
    snippet = _ANSI_RE.sub("", str(error.get("snippet") or ""))
    code_line = ""
    for line in snippet.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith(">"):
            code_line = re.sub(r"^>\s*\d+\s*\|\s?", "", stripped)
            break
    return normalize_text(header + ("\n" + code_line if code_line else ""))


def _walk_playwright(suite: dict, titles: list[str], out: list[Outcome], file_hint: str) -> None:
    here = titles + ([suite["title"]] if suite.get("title") else [])
    for spec in suite.get("specs", []):
        file_path = str(spec.get("file") or file_hint or (here[0] if here else ""))
        describe = [title for title in here[1:]] if here else []
        case_id = CASE_SEPARATOR.join([file_path, *describe, str(spec.get("title", ""))])
        for test in spec.get("tests", []):
            results = test.get("results") or []
            status = str(test.get("status") or "")
            failing = [r for r in results if r.get("status") in ("failed", "timedOut", "interrupted")]
            if status == "skipped":
                out.append(Outcome(case_id, "skipped"))
                continue
            if status in ("unexpected", "flaky") or failing:
                first = (failing or results or [{}])[0]
                error = first.get("error") or (first.get("errors") or [{}])[0] or {}
                signature = _browser_signature(error) if isinstance(error, dict) else normalize_text(str(error))
                location = f"{spec.get('file', file_path)}:{spec.get('line', '')}"
                selector = (("file", file_path), ("title", str(spec.get("title", ""))))
                out.append(Outcome(case_id, "failed", signature, location, selector))
            else:
                selector = (("file", file_path), ("title", str(spec.get("title", ""))))
                out.append(Outcome(case_id, "passed", "", "", selector))
    for child in suite.get("suites", []):
        _walk_playwright(child, here, out, file_hint or str(suite.get("file") or ""))


def parse_playwright_json(path: Path) -> ParsedResults:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return ParsedResults(unusable=f"unreadable Playwright JSON {path.name}: {exc}")
    if not isinstance(document, dict) or "suites" not in document:
        return ParsedResults(unusable=f"{path.name} is not a Playwright JSON report")
    parsed = ParsedResults()
    for suite in document.get("suites", []):
        _walk_playwright(suite, [], parsed.outcomes, str(suite.get("file") or suite.get("title") or ""))
    for error in document.get("errors", []) or []:
        message = error.get("message") if isinstance(error, dict) else str(error)
        parsed.runner_errors.append(normalize_text(str(message))[:500])
    return parsed


def parse_junit_xml(path: Path) -> ParsedResults:
    try:
        root = ET.parse(path).getroot()  # noqa: S314 - result files are produced by our own workflows
    except (OSError, ET.ParseError) as exc:
        return ParsedResults(unusable=f"unreadable junit XML {path.name}: {exc}")
    parsed = ParsedResults()
    for case in root.iter("testcase"):
        file_path = case.get("file") or ""
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        module = re.sub(r"\.py$", "", file_path).replace("/", ".")
        if file_path and classname.startswith(module):
            classes = [part for part in classname[len(module):].split(".") if part]
            node = "::".join([file_path, *classes, name])
        elif file_path:
            node = f"{file_path}::{name}"
        else:
            node = "::".join(part for part in (classname.replace(".", "/") + ".py" if classname else "", name) if part)
        failure = case.find("failure")
        error = case.find("error")
        skipped = case.find("skipped")
        if failure is not None or error is not None:
            element = failure if failure is not None else error
            kind = "failure" if failure is not None else "error"
            message = element.get("message") or (element.text or "")
            signature = normalize_text(f"{kind}|{message}")
            parsed.outcomes.append(Outcome(node, "failed", signature, f"{file_path}:{case.get('line', '')}",
                                           (("nodeid", node),)))
        elif skipped is not None:
            parsed.outcomes.append(Outcome(node, "skipped"))
        else:
            parsed.outcomes.append(Outcome(node, "passed", "", "", (("nodeid", node),)))
    return parsed


def parse_results_file(domain: str, path: Path) -> ParsedResults:
    return parse_playwright_json(path) if domain == "browser" else parse_junit_xml(path)


_EXTENSION = {"browser": ".json", "pytest": ".xml"}
INFRA_SENTINEL = "infrastructure_failure.txt"
BUDGET_SENTINEL = "budget_exhausted.txt"


@dataclass
class RunSet:
    """All bounded isolated runs recorded for one ref."""

    present: bool = False
    infra_reason: str = ""
    budget_exhausted: bool = False
    unusable: str = ""
    files: int = 0
    by_case: dict[str, list[Outcome]] = field(default_factory=dict)


def load_run_set(domain: str, directory: Path | None) -> RunSet:
    runs = RunSet()
    if directory is None or not directory.is_dir():
        return runs
    runs.present = True
    isolated = directory / "isolated"
    for root in (directory, isolated):
        sentinel = root / INFRA_SENTINEL
        if sentinel.is_file():
            runs.infra_reason = sentinel.read_text(encoding="utf-8", errors="replace").strip() or "infrastructure failure"
        if (root / BUDGET_SENTINEL).is_file():
            runs.budget_exhausted = True
    if not isolated.is_dir():
        return runs
    for path in sorted(isolated.rglob(f"*{_EXTENSION[domain]}")):
        parsed = parse_results_file(domain, path)
        if parsed.unusable:
            runs.unusable = runs.unusable or parsed.unusable
            continue
        runs.files += 1
        for outcome in parsed.outcomes:
            if outcome.status in ("passed", "failed"):
                runs.by_case.setdefault(outcome.case_id, []).append(outcome)
    return runs


EXIT_CODE_FILE = "exit_code.txt"


def load_full_results(domain: str, candidate_dir: Path | None) -> ParsedResults | None:
    """The original full run: ``<candidate>/full/results.*`` plus the runner's ``exit_code.txt``."""
    if candidate_dir is None:
        return None
    full = candidate_dir / "full"
    if not full.is_dir():
        return None
    files = sorted(full.glob(f"*{_EXTENSION[domain]}"))
    if not files:
        return None
    merged = ParsedResults()
    for path in files:
        parsed = parse_results_file(domain, path)
        if parsed.unusable:
            merged.unusable = parsed.unusable
            continue
        merged.outcomes.extend(parsed.outcomes)
        merged.runner_errors.extend(parsed.runner_errors)
    code_file = full / EXIT_CODE_FILE
    if code_file.is_file():
        try:
            merged.exit_code = int(code_file.read_text(encoding="utf-8").strip())
        except ValueError:
            merged.unusable = merged.unusable or f"{EXIT_CODE_FILE} is not an integer"
    return merged


# ---------------------------------------------------------------------------
# Known-flake registry.
# ---------------------------------------------------------------------------
_ENTRY_KEYS = {
    "id", "domain", "case", "failure_signature", "first_confirmed_sha",
    "tracking_reference", "expires", "classifier_runs",
}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RELEASE_RE = re.compile(r"^release:(\d+(?:\.\d+){1,3})$")
_WILDCARD_CHARS = ("*", "?")


def parse_version(text: str) -> tuple[int, ...] | None:
    match = re.match(r"^\s*v?(\d+(?:\.\d+){0,3})\s*$", text or "")
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


@dataclass(frozen=True)
class RegistryEntry:
    id: str
    domain: str
    case: str
    failure_signature: str
    first_confirmed_sha: str
    tracking_reference: str
    expires: str
    classifier_runs: int


@dataclass
class RegistryState:
    active: list[RegistryEntry] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    fatal: str = ""
    path: str = ""


def _validate_entry(raw: object, *, today: datetime.date, version: tuple[int, ...] | None) -> tuple[RegistryEntry | None, str]:
    if not isinstance(raw, dict):
        return None, "entry is not an object"
    unknown = set(raw) - _ENTRY_KEYS
    missing = _ENTRY_KEYS - set(raw)
    if unknown:
        return None, f"unknown keys: {sorted(unknown)}"
    if missing:
        return None, f"missing keys: {sorted(missing)}"
    entry_id, domain, case = raw["id"], raw["domain"], raw["case"]
    if not isinstance(entry_id, str) or not _ID_RE.match(entry_id):
        return None, "id must match ^[a-z0-9][a-z0-9-]{2,63}$"
    if domain not in DOMAINS:
        return None, f"domain must be one of {list(DOMAINS)}"
    if not isinstance(case, str) or not case.strip() or any(ch in case for ch in _WILDCARD_CHARS):
        return None, "case must be an exact test identity without wildcards"
    if domain == "browser" and (CASE_SEPARATOR not in case or not case.split(CASE_SEPARATOR, 1)[0].endswith(".spec.js")):
        return None, "browser case must be '<spec path> › <test title>' (no whole-file exemptions)"
    if domain == "pytest" and ("::" not in case or case.endswith("::")):
        return None, "pytest case must be an exact node id containing '::' (no whole-file exemptions)"
    signature = raw["failure_signature"]
    if not isinstance(signature, str) or not signature.strip():
        return None, "failure_signature must be a non-empty normalized signature"
    if not is_sha(raw["first_confirmed_sha"]) or raw["first_confirmed_sha"] == ZERO_SHA:
        return None, "first_confirmed_sha must be a 40-char commit SHA"
    if not isinstance(raw["tracking_reference"], str) or not raw["tracking_reference"].strip():
        return None, "tracking_reference is required"
    runs = raw["classifier_runs"]
    if isinstance(runs, bool) or not isinstance(runs, int) or not 1 <= runs <= MAX_REGISTRY_CLASSIFIER_RUNS:
        return None, f"classifier_runs must be an integer in [1, {MAX_REGISTRY_CLASSIFIER_RUNS}]"
    expires = raw["expires"]
    if not isinstance(expires, str):
        return None, "expires is required"
    if _DATE_RE.match(expires):
        try:
            expiry = datetime.date.fromisoformat(expires)
        except ValueError:
            return None, "expires is not a valid date"
        if expiry < today:
            return None, f"expired on {expires}"
        if (expiry - today).days > MAX_REGISTRY_EXPIRY_DAYS:
            return None, f"expiry is more than {MAX_REGISTRY_EXPIRY_DAYS} days away"
    else:
        release = _RELEASE_RE.match(expires)
        if not release:
            return None, "expires must be YYYY-MM-DD or release:<version>"
        boundary = parse_version(release.group(1))
        if version is None:
            return None, "current release version unavailable; cannot prove release boundary is not reached"
        if version >= boundary:
            return None, f"release boundary {expires} reached"
    return RegistryEntry(entry_id, domain, case, signature, raw["first_confirmed_sha"],
                         raw["tracking_reference"], expires, runs), ""


def load_registry(path: Path | None, *, today: datetime.date, version: tuple[int, ...] | None) -> RegistryState:
    state = RegistryState(path=str(path) if path else "")
    if path is None or not path.is_file():
        state.fatal = "known-flake registry file is missing"
        return state
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        state.fatal = f"known-flake registry is not valid JSON: {exc}"
        return state
    if not isinstance(document, dict) or set(document) - {"schema_version", "description", "entries"}:
        state.fatal = "known-flake registry has an unexpected top-level shape"
        return state
    if document.get("schema_version") != SCHEMA_VERSION or not isinstance(document.get("entries"), list):
        state.fatal = f"known-flake registry must have schema_version {SCHEMA_VERSION} and an entries list"
        return state
    if len(document["entries"]) > MAX_REGISTRY_ENTRIES:
        state.fatal = f"known-flake registry exceeds {MAX_REGISTRY_ENTRIES} entries"
        return state
    seen_ids: set[str] = set()
    seen_cases: set[tuple[str, str]] = set()
    for raw in document["entries"]:
        entry, reason = _validate_entry(raw, today=today, version=version)
        label = raw.get("id") if isinstance(raw, dict) else None
        if entry is not None and entry.id in seen_ids:
            entry, reason = None, "duplicate id"
        if entry is not None and (entry.domain, entry.case) in seen_cases:
            entry, reason = None, "duplicate case"
        if entry is None:
            state.rejected.append({"id": label, "reason": reason})
            continue
        seen_ids.add(entry.id)
        seen_cases.add((entry.domain, entry.case))
        state.active.append(entry)
    return state


def registry_match(state: RegistryState, domain: str, case_id: str, signature: str) -> RegistryEntry | None:
    digest = signature_id(signature)
    for entry in state.active:
        if entry.domain == domain and entry.case == case_id and entry.failure_signature in (signature, digest):
            return entry
    return None


# ---------------------------------------------------------------------------
# Classification.
# ---------------------------------------------------------------------------
def specificity_p_value(candidate_failures: int, candidate_runs: int, anchor_failures: int, anchor_runs: int) -> float:
    """One-sided exact (Fisher) probability of a candidate failure count at least this large if
    candidate and anchor shared one failure rate. Deterministic, stdlib only."""
    if candidate_runs <= 0 or anchor_runs <= 0:
        return 1.0
    total_runs = candidate_runs + anchor_runs
    failures = candidate_failures + anchor_failures
    tail = sum(
        math.comb(failures, x) * math.comb(total_runs - failures, candidate_runs - x)
        for x in range(candidate_failures, min(failures, candidate_runs) + 1)
    )
    return tail / math.comb(total_runs, candidate_runs)


def _split(outcomes: list[Outcome], signature: str) -> tuple[int, int, int]:
    failing = [item for item in outcomes if item.status == "failed"]
    matching = sum(1 for item in failing if item.signature == signature)
    return len(outcomes), matching, len(failing) - matching


def classify_cases(
    domain: str,
    candidate_sha: str,
    anchor_sha: str,
    full: ParsedResults | None,
    candidate: RunSet,
    anchor: RunSet,
    registry: RegistryState,
    *,
    runs_required: int = ISOLATED_RUNS_PER_REF,
    max_cases: int = MAX_CASES_TO_CLASSIFY,
) -> dict:
    """Pure classification. Returns the machine-readable result document."""
    result: dict = {
        "schema_version": SCHEMA_VERSION,
        "classification": INCONCLUSIVE,
        "domain": domain,
        "candidate_sha": candidate_sha,
        "anchor_sha": anchor_sha,
        "cases": [],
        "known_flake_matches": [],
        "discriminator_needed": [],
        "specificity_alpha": SPECIFICITY_ALPHA,
        "reason": "",
        "candidate_runs": sum(len(v) for v in candidate.by_case.values()),
        "anchor_runs": sum(len(v) for v in anchor.by_case.values()),
        "budget": budget_document(),
        "registry": {
            "path": registry.path,
            "active_entries": len(registry.active),
            "rejected_entries": registry.rejected,
        },
    }

    def done(classification: str, reason: str) -> dict:
        result["classification"] = classification
        result["reason"] = reason
        result["exit_code"] = EXIT_CODES[classification]
        return result

    # 1. The original full run is the evidence; without it nothing is claimable.
    if full is None:
        return done(INCONCLUSIVE, "required case logs are missing: no original full-run result file")
    if full.unusable and not full.outcomes:
        return done(INCONCLUSIVE, f"required case logs are unusable: {full.unusable}")
    if full.exit_code is None:
        return done(INCONCLUSIVE, "required case logs are missing: no recorded runner exit code for the full run")
    failed = sorted({item.case_id for item in full.outcomes if item.status == "failed"})
    if not failed:
        if full.runner_errors:
            return done(INFRASTRUCTURE_FAILURE, "runner reported errors without a failing case: " + "; ".join(full.runner_errors[:3]))
        if full.exit_code != 0:
            return done(INFRASTRUCTURE_FAILURE, f"the full run exited {full.exit_code} without a failing case (runner, collection or configuration failure)")
        if full.executed == 0:
            return done(INCONCLUSIVE, "the original full run executed zero tests")
        return done(PASS, "original full suite passed")

    # 2. Bounded budget: never classify an unbounded number of distinct failures.
    if len(failed) > max_cases:
        return done(INCONCLUSIVE, f"{len(failed)} distinct failing cases exceed the classification budget of {max_cases}")

    # 3. Anchor + evidence usability (fail closed, never call it a flake).
    if not is_sha(anchor_sha) or anchor_sha == ZERO_SHA:
        return done(INFRASTRUCTURE_FAILURE, "qualification anchor is missing or unresolved; no comparison is possible")
    if anchor_sha == candidate_sha:
        return done(INFRASTRUCTURE_FAILURE, "qualification anchor equals the candidate; no independent comparison is possible")
    if registry.fatal:
        return done(INFRASTRUCTURE_FAILURE, registry.fatal)
    if not anchor.present:
        return done(INFRASTRUCTURE_FAILURE, "anchor results are missing; the anchor run could not be produced")
    if anchor.infra_reason:
        return done(INFRASTRUCTURE_FAILURE, "anchor infrastructure failure: " + anchor.infra_reason)
    if candidate.infra_reason:
        return done(INFRASTRUCTURE_FAILURE, "candidate isolated-run infrastructure failure: " + candidate.infra_reason)
    if anchor.unusable or candidate.unusable:
        return done(INFRASTRUCTURE_FAILURE, "unusable run result: " + (anchor.unusable or candidate.unusable))
    if not candidate.present:
        return done(INCONCLUSIVE, "required case logs are missing: no candidate isolated-run results")
    if candidate.budget_exhausted or anchor.budget_exhausted:
        return done(INCONCLUSIVE, "the classification wall-time budget was exhausted before evidence was complete")

    # 4. Per-case comparison of normalized failures.
    verdicts: list[str] = []
    full_by_case: dict[str, Outcome] = {}
    for item in full.outcomes:
        if item.status == "failed":
            full_by_case.setdefault(item.case_id, item)
    for case_id in failed:
        original = full_by_case[case_id]
        signature = original.signature
        c_runs, c_match, c_other = _split(candidate.by_case.get(case_id, []), signature)
        a_runs, a_match, a_other = _split(anchor.by_case.get(case_id, []), signature)
        case = {
            "id": case_id,
            "signature": signature,
            "signature_id": signature_id(signature),
            "location": original.location,
            "candidate_runs": c_runs,
            "candidate_reproductions": c_match,
            "candidate_other_failures": c_other,
            "anchor_runs": a_runs,
            "anchor_reproductions": a_match,
            "anchor_other_failures": a_other,
            "known_flake_id": None,
            "specificity_p_value": None,
            "needs_discriminator": False,
        }
        if not signature:
            verdict, why = INCONCLUSIVE, "failure signature is empty; signatures cannot be compared safely"
        elif c_runs == 0:
            verdict, why = INCONCLUSIVE, "required case logs are missing: no candidate isolated runs for this case"
        elif c_match == 0:
            why = ("candidate isolated runs failed with a different signature than the full run; signatures cannot be compared safely"
                   if c_other else "the candidate failure did not reproduce in bounded isolated runs")
            verdict = INCONCLUSIVE
        elif a_match:
            entry = registry_match(registry, domain, case_id, signature)
            if entry is not None:
                verdict, why = KNOWN_FLAKE, f"active registry entry {entry.id} matches and the failure reproduced on the current anchor"
                case["known_flake_id"] = entry.id
                result["known_flake_matches"].append(entry.id)
            else:
                verdict, why = ANCHOR_REPRODUCED_FLAKE, "the same normalized failure reproduced on candidate and anchor and is not an active registry entry"
        elif a_runs >= runs_required:
            # The anchor did not reproduce the failure in its sample. That is only evidence of
            # candidate specificity if the samples actually discriminate the two failure rates.
            p_value = specificity_p_value(c_match, c_runs, 0, a_runs)
            case["specificity_p_value"] = round(p_value, 6)
            note = " (the anchor failed with a different signature)" if a_other else ""
            if p_value <= SPECIFICITY_ALPHA:
                verdict = CANDIDATE_REGRESSION
                why = (f"the candidate failure reproduced {c_match}/{c_runs} times and the same normalized failure did not "
                       f"reproduce on the anchor (0/{a_runs}); the samples discriminate candidate from anchor "
                       f"(one-sided exact p={p_value:.4g} <= {SPECIFICITY_ALPHA}){note}")
            else:
                verdict = INCONCLUSIVE
                staged = c_runs >= runs_required + DISCRIMINATOR_RUNS_PER_REF and a_runs >= runs_required + DISCRIMINATOR_RUNS_PER_REF
                case["needs_discriminator"] = not staged
                why = (f"the candidate failure reproduced {c_match}/{c_runs} times and did not reproduce on the anchor "
                       f"(0/{a_runs}), but the samples do not establish candidate specificity "
                       f"(one-sided exact p={p_value:.4g} > {SPECIFICITY_ALPHA}); a rare pre-existing failure cannot be "
                       f"distinguished from a candidate regression{note}"
                       + ("; the bounded discriminator stage was already spent" if staged else ""))
        elif a_runs == 0:
            verdict, why = INCONCLUSIVE, "required case logs are missing: no anchor isolated runs for this case"
        else:
            verdict, why = INCONCLUSIVE, f"only {a_runs} of {runs_required} required anchor runs completed; anchor cleanliness cannot be claimed"
        case["verdict"] = verdict
        case["reason"] = why
        result["cases"].append(case)
        verdicts.append(verdict)

    result["discriminator_needed"] = [case["id"] for case in result["cases"] if case["needs_discriminator"]]
    overall = next(level for level in _SEVERITY if level in verdicts)
    reasons = [f"{case['id']}: {case['reason']}" for case in result["cases"] if case["verdict"] == overall]
    return done(overall, "; ".join(reasons))


def budget_document() -> dict:
    return {
        "max_cases_to_classify": MAX_CASES_TO_CLASSIFY,
        "isolated_runs_per_ref": ISOLATED_RUNS_PER_REF,
        "discriminator_runs_per_ref": DISCRIMINATOR_RUNS_PER_REF,
        "max_classification_wall_time_seconds": MAX_CLASSIFICATION_WALL_TIME_SECONDS,
        "full_suite_automatic_reruns": FULL_SUITE_AUTOMATIC_RERUNS,
    }


def render_summary(result: dict) -> str:
    lines = [
        "### Qualification failure classification",
        "",
        f"**{result['classification']}** ({result['domain']})",
        "",
        f"- CANDIDATE_SHA={result['candidate_sha']}",
        f"- QUALIFICATION_ANCHOR_SHA={result['anchor_sha']}",
        f"- Reason: {result['reason']}",
        f"- Isolated runs: candidate={result['candidate_runs']} anchor={result['anchor_runs']}",
        f"- Budget: {result['budget']['max_cases_to_classify']} cases, "
        f"{result['budget']['isolated_runs_per_ref']} runs per ref (+{result['budget']['discriminator_runs_per_ref']} once, "
        f"only if the first stage cannot discriminate), "
        f"{result['budget']['max_classification_wall_time_seconds']}s wall time, "
        f"{result['budget']['full_suite_automatic_reruns']} automatic full-suite reruns",
    ]
    if result["classification"] == KNOWN_FLAKE:
        lines += ["", "> WARNING: non-blocking KNOWN_FLAKE. This failure is visible and tracked; it is not a skip.",
                  f"> Registry entries: {', '.join(result['known_flake_matches'])}"]
    if result["classification"] == INCONCLUSIVE and any(
            case.get("specificity_p_value") is not None for case in result["cases"]):
        lines += ["", "> The candidate reproduced a failure the anchor did not reproduce in its bounded sample, but the "
                  "evidence does not discriminate candidate from anchor. This is NOT a candidate regression conclusion; "
                  "do not change candidate source on this evidence."]
    if result["classification"] == ANCHOR_REPRODUCED_FLAKE:
        lines += ["", "> The same failure reproduces on the anchor and is NOT a registered known flake. "
                  "The gate fails once pending a determinism decision/fix; candidate source is not blamed."]
    for case in result["cases"]:
        lines += [
            "",
            f"#### {case['verdict']}: `{case['id']}`",
            f"- signature: `{case['signature_id']}`",
            f"- candidate: {case['candidate_reproductions']}/{case['candidate_runs']} reproduced "
            f"({case['candidate_other_failures']} other failures)",
            f"- anchor: {case['anchor_reproductions']}/{case['anchor_runs']} reproduced "
            f"({case['anchor_other_failures']} other failures)",
            *([f"- specificity: one-sided exact p={case['specificity_p_value']} (alpha {result['specificity_alpha']})"]
              if case.get("specificity_p_value") is not None else []),
            f"- {case['reason']}",
            "",
            "```text",
            case["signature"][:1500].replace("```", "'''"),
            "```",
        ]
    rejected = result["registry"]["rejected_entries"]
    if rejected:
        lines += ["", f"Rejected registry entries: {json.dumps(rejected)}"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Anchor derivation (git only; no network API).
# ---------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> tuple[int, str]:
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False,
    )
    return completed.returncode, completed.stdout.strip()


def _commit_exists(repo: Path, sha: str) -> bool:
    return is_sha(sha) and _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")[0] == 0


def resolve_anchor(event: str, candidate_sha: str, *, before: str = "", pr_base_sha: str = "",
                   input_anchor: str = "", repo: Path = Path(".")) -> tuple[str, str]:
    """Return (anchor_sha, reason). anchor_sha is UNRESOLVED when no unambiguous anchor exists.

    Never falls back to ``main`` or to a historical known-good SHA.
    """
    def unresolved(reason: str) -> tuple[str, str]:
        return UNRESOLVED_ANCHOR, reason

    if not is_sha(candidate_sha):
        return unresolved("candidate SHA is not a 40-char SHA")
    if event == "push":
        if not before or before == ZERO_SHA:
            return unresolved("push has no previous tip (new branch or tag); no unambiguous anchor")
        if not _commit_exists(repo, before):
            return unresolved("push 'before' commit is not reachable in this checkout")
        anchor, reason = before, "push: github.event.before"
    elif event == "pull_request":
        if not pr_base_sha:
            return unresolved("pull request base SHA is missing")
        if not _commit_exists(repo, pr_base_sha):
            return unresolved("pull request base commit is not reachable in this checkout")
        anchor, reason = pr_base_sha, "pull_request: base.sha"
    elif event == "workflow_dispatch":
        if input_anchor:
            if not _commit_exists(repo, input_anchor):
                return unresolved("anchor_sha input is not a reachable commit")
            anchor, reason = input_anchor, "workflow_dispatch: anchor_sha input"
        else:
            code, parent = _git(repo, "rev-parse", "--verify", "--quiet", f"{candidate_sha}^1")
            if code != 0 or not _commit_exists(repo, parent):
                return unresolved("candidate has no first parent")
            anchor, reason = parent, "workflow_dispatch: candidate first parent"
    else:
        return unresolved(f"no anchor rule exists for event '{event}'")
    if anchor == candidate_sha:
        return unresolved("anchor equals candidate")
    return anchor, reason


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------
def _today(value: str | None) -> datetime.date:
    return datetime.date.fromisoformat(value) if value else datetime.datetime.now(datetime.timezone.utc).date()


def _current_version(explicit: str | None, registry: Path | None) -> tuple[int, ...] | None:
    if explicit:
        return parse_version(explicit)
    candidates = []
    if registry is not None:
        candidates.append(registry.resolve().parent.parent.parent / "VERSION")
    candidates.append(Path("VERSION"))
    for path in candidates:
        if path.is_file():
            return parse_version(path.read_text(encoding="utf-8"))
    return None


def _write(path: str | None, text: str) -> None:
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")


def cmd_budget(_args: argparse.Namespace) -> int:
    print(f"MAX_CASES_TO_CLASSIFY={MAX_CASES_TO_CLASSIFY}")
    print(f"ISOLATED_RUNS_PER_REF={ISOLATED_RUNS_PER_REF}")
    print(f"DISCRIMINATOR_RUNS_PER_REF={DISCRIMINATOR_RUNS_PER_REF}")
    print(f"MAX_CLASSIFICATION_WALL_TIME_SECONDS={MAX_CLASSIFICATION_WALL_TIME_SECONDS}")
    print(f"FULL_SUITE_AUTOMATIC_RERUNS={FULL_SUITE_AUTOMATIC_RERUNS}")
    return 0


def cmd_resolve_anchor(args: argparse.Namespace) -> int:
    anchor, reason = resolve_anchor(
        args.event, args.candidate_sha, before=args.before or "", pr_base_sha=args.pr_base_sha or "",
        input_anchor=args.input_anchor or "", repo=Path(args.repo),
    )
    print(f"QUALIFICATION_ANCHOR_SHA={anchor}")
    print(f"QUALIFICATION_ANCHOR_REASON={reason}")
    return 0 if anchor != UNRESOLVED_ANCHOR else ANCHOR_UNRESOLVED


def cmd_metadata(args: argparse.Namespace) -> int:
    document = {
        "candidate_sha": args.candidate_sha,
        "qualification_anchor_sha": args.anchor_sha,
        "qualification_anchor_reason": args.anchor_reason or "",
        "event": args.event,
        "workflow_run_id": args.run_id,
        "workflow": args.workflow or "",
    }
    _write(args.output_json, json.dumps(document, indent=2, sort_keys=True) + "\n")
    lines = [
        f"### Qualification identity ({args.workflow or 'workflow'})",
        "",
        "```text",
        f"CANDIDATE_SHA={args.candidate_sha}",
        f"QUALIFICATION_ANCHOR_SHA={args.anchor_sha}",
        f"EVENT={args.event}",
        f"WORKFLOW_RUN_ID={args.run_id}",
        "```",
    ]
    if args.anchor_sha == UNRESOLVED_ANCHOR:
        lines += ["", f"> WARNING: no unambiguous qualification anchor ({args.anchor_reason or 'unresolved'}). "
                  "A failure in this run cannot be classified and will fail closed as INFRASTRUCTURE_FAILURE."]
    text = "\n".join(lines) + "\n"
    if args.summary_file:
        with open(args.summary_file, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text, end="")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    parsed = load_full_results(args.domain, Path(args.candidate_results))
    plan: dict = {
        "domain": args.domain,
        "executed": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "exit_code_of_full_run": None,
        "runner_errors": [],
        "failing_cases": [],
        "distinct_failures": 0,
        "within_budget": True,
        "budget": budget_document(),
    }
    code = PLAN_UNUSABLE
    if parsed is None or (parsed.unusable and not parsed.outcomes):
        plan["unusable"] = (parsed.unusable if parsed else "") or "no original full-run result file"
    elif parsed.exit_code is None:
        plan["unusable"] = "no recorded runner exit code for the full run"
    else:
        plan.update(executed=parsed.executed, passed=parsed.passed, failed=parsed.failed, skipped=parsed.skipped,
                    exit_code_of_full_run=parsed.exit_code, runner_errors=parsed.runner_errors)
        seen: dict[str, Outcome] = {}
        for item in parsed.outcomes:
            if item.status == "failed":
                seen.setdefault(item.case_id, item)
        plan["distinct_failures"] = len(seen)
        plan["within_budget"] = len(seen) <= MAX_CASES_TO_CLASSIFY
        rows: list[list[str]] = []
        for case_id, item in sorted(seen.items()):
            selector = dict(item.selector)
            entry = {"id": case_id, "slug": slugify(case_id), "signature": item.signature,
                     "signature_id": signature_id(item.signature), "location": item.location, **selector}
            if args.domain == "browser":
                entry["grep"] = re.escape(selector.get("title", ""))
                fields = [entry["slug"], selector.get("file", ""), entry["grep"]]
            else:
                fields = [entry["slug"], selector.get("nodeid", case_id)]
            plan["failing_cases"].append(entry)
            rows.append(fields)
        if any("\t" in field or "\n" in field for row in rows for field in row):
            plan["unusable"] = "a failing case identity contains control characters"
        elif seen:
            code = PLAN_WITHIN_BUDGET if plan["within_budget"] else PLAN_OVER_BUDGET
        elif parsed.runner_errors or parsed.exit_code != 0 or parsed.executed == 0:
            plan["unusable"] = ("; ".join(parsed.runner_errors[:3]) or
                                (f"the full run exited {parsed.exit_code} without a failing case" if parsed.exit_code else "the run executed zero tests"))
        else:
            code = PLAN_NO_FAILURES
        if args.output_tsv:
            _write(args.output_tsv, "".join("\t".join(row) + "\n" for row in (rows if code == PLAN_WITHIN_BUDGET else [])))
    plan["exit_code"] = code
    _write(args.output_json, json.dumps(plan, indent=2, sort_keys=True) + "\n")
    print(f"plan: executed={plan['executed']} passed={plan['passed']} failed={plan['failed']} "
          f"distinct_failures={plan['distinct_failures']} within_budget={str(plan['within_budget']).lower()} exit={code}")
    return code


def cmd_discriminate(args: argparse.Namespace) -> int:
    """List the case(s) whose first-stage evidence cannot discriminate candidate from anchor."""
    domain = args.domain
    candidate_dir = Path(args.candidate_results)
    anchor_dir = Path(args.anchor_results)
    registry_path = Path(args.registry) if args.registry else None
    registry = load_registry(registry_path, today=_today(args.today), version=_current_version(args.current_version, registry_path))
    full = load_full_results(domain, candidate_dir)
    result = classify_cases(domain, args.candidate_sha, args.anchor_sha, full,
                            load_run_set(domain, candidate_dir), load_run_set(domain, anchor_dir), registry)
    needed = set(result["discriminator_needed"])
    rows: list[str] = []
    if needed and full is not None:
        seen: dict[str, Outcome] = {}
        for item in full.outcomes:
            if item.status == "failed" and item.case_id in needed:
                seen.setdefault(item.case_id, item)
        for case_id, item in sorted(seen.items()):
            selector = dict(item.selector)
            if domain == "browser":
                rows.append("\t".join([slugify(case_id), selector.get("file", ""), re.escape(selector.get("title", ""))]))
            else:
                rows.append("\t".join([slugify(case_id), selector.get("nodeid", case_id)]))
    _write(args.output_tsv, "".join(row + "\n" for row in rows))
    print(f"discriminate: {len(rows)} case(s) need {DISCRIMINATOR_RUNS_PER_REF} additional isolated runs per ref")
    return DISCRIMINATOR_NEEDED if rows else 0


def cmd_validate_registry(args: argparse.Namespace) -> int:
    path = Path(args.registry)
    state = load_registry(path, today=_today(args.today), version=_current_version(args.current_version, path))
    problems = ([state.fatal] if state.fatal else []) + [f"{item['id']}: {item['reason']}" for item in state.rejected]
    for problem in problems:
        print(f"registry: {problem}", file=sys.stderr)
    print(f"registry: {len(state.active)} active entries, {len(state.rejected)} rejected")
    return REGISTRY_INVALID if problems else 0


def cmd_classify(args: argparse.Namespace) -> int:
    domain = args.domain
    if not is_sha(args.candidate_sha) or args.candidate_sha == ZERO_SHA:
        print("classify: --candidate-sha must be a 40-char commit SHA", file=sys.stderr)
        return 2
    candidate_dir = Path(args.candidate_results) if args.candidate_results else None
    anchor_dir = Path(args.anchor_results) if args.anchor_results else None
    registry_path = Path(args.registry) if args.registry else None
    registry = load_registry(registry_path, today=_today(args.today), version=_current_version(args.current_version, registry_path))
    result = classify_cases(
        domain, args.candidate_sha, args.anchor_sha,
        load_full_results(domain, candidate_dir),
        load_run_set(domain, candidate_dir),
        load_run_set(domain, anchor_dir),
        registry,
    )
    result["candidate_runs_required_per_ref"] = ISOLATED_RUNS_PER_REF
    _write(args.output_json, json.dumps(result, indent=2, sort_keys=True) + "\n")
    summary = render_summary(result)
    _write(args.output_summary, summary)
    print(f"{result['classification']}: {result['reason']}")
    return EXIT_CODES[result["classification"]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="failure_classifier.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("budget", help="print the bounded diagnostic budget").set_defaults(func=cmd_budget)

    p = sub.add_parser("resolve-anchor", help="derive QUALIFICATION_ANCHOR_SHA from the workflow event")
    p.add_argument("--event", required=True)
    p.add_argument("--candidate-sha", required=True)
    p.add_argument("--before", default="")
    p.add_argument("--pr-base-sha", default="")
    p.add_argument("--input-anchor", default="")
    p.add_argument("--repo", default=".")
    p.set_defaults(func=cmd_resolve_anchor)

    p = sub.add_parser("metadata", help="publish the qualification identity")
    p.add_argument("--candidate-sha", required=True)
    p.add_argument("--anchor-sha", required=True)
    p.add_argument("--anchor-reason", default="")
    p.add_argument("--event", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--workflow", default="")
    p.add_argument("--output-json", required=True)
    p.add_argument("--summary-file", default="")
    p.set_defaults(func=cmd_metadata)

    p = sub.add_parser("plan", help="extract failing cases of a full run and check the budget")
    p.add_argument("--domain", choices=DOMAINS, required=True)
    p.add_argument("--candidate-results", required=True)
    p.add_argument("--output-json", required=True)
    p.add_argument("--output-tsv", default="")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("discriminate", help="list cases that need the single bounded second-stage runs")
    p.add_argument("--domain", choices=DOMAINS, required=True)
    p.add_argument("--candidate-sha", required=True)
    p.add_argument("--anchor-sha", required=True)
    p.add_argument("--candidate-results", required=True)
    p.add_argument("--anchor-results", required=True)
    p.add_argument("--registry", required=True)
    p.add_argument("--output-tsv", required=True)
    p.add_argument("--today", default="")
    p.add_argument("--current-version", default="")
    p.set_defaults(func=cmd_discriminate)

    p = sub.add_parser("classify", help="classify a failed full run")
    p.add_argument("--domain", choices=DOMAINS, required=True)
    p.add_argument("--candidate-sha", required=True)
    p.add_argument("--anchor-sha", required=True)
    p.add_argument("--candidate-results", required=True)
    p.add_argument("--anchor-results", required=True)
    p.add_argument("--registry", required=True)
    p.add_argument("--output-json", required=True)
    p.add_argument("--output-summary", required=True)
    p.add_argument("--today", default="", help="override today's date (YYYY-MM-DD) for deterministic registry expiry")
    p.add_argument("--current-version", default="", help="override the release version used for release-boundary expiry")
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("validate-registry", help="validate the known-flake registry")
    p.add_argument("--registry", required=True)
    p.add_argument("--today", default="")
    p.add_argument("--current-version", default="")
    p.set_defaults(func=cmd_validate_registry)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
