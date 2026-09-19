"""Qualification infrastructure contract and classifier self-tests.

Enforces docs/QUALIFICATION_DETERMINISM.md mechanically: exactly one classifier and one
registry, both workflows delegate to the classifier, no retry-until-green anywhere, a bounded
diagnostic budget, an explicit fail-closed anchor, an exact classification set, and the full
classification behavior matrix.
"""

from __future__ import annotations

import ast
import datetime
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
QUALIFICATION = ROOT / ".github" / "qualification"
CLASSIFIER_PATH = QUALIFICATION / "failure_classifier.py"
REGISTRY_PATH = QUALIFICATION / "known_flakes.json"
WORKFLOWS = ROOT / ".github" / "workflows"
TESTS_WORKFLOW = WORKFLOWS / "tests.yml"
BROWSER_WORKFLOW = WORKFLOWS / "browser-runtime.yml"
DOC = ROOT / "docs" / "QUALIFICATION_DETERMINISM.md"

spec = importlib.util.spec_from_file_location("failure_classifier", CLASSIFIER_PATH)
fc = importlib.util.module_from_spec(spec)
sys.modules["failure_classifier"] = fc
spec.loader.exec_module(fc)

CANDIDATE = "c" * 40
ANCHOR = "a" * 40
TODAY = datetime.date(2026, 9, 19)
SEP = fc.CASE_SEPARATOR


# --------------------------------------------------------------------------------------
# Result builders (synthetic Playwright JSON and pytest junit XML).
# --------------------------------------------------------------------------------------
def pw_error(header: str, code_line: str, *, call_log: str = "waiting for locator") -> dict:
    return {
        "message": f"Error: {header}\n\nCall log:\n  - {call_log}\n     19 × locator resolved",
        "snippet": f"  335 |   before();\n> 336 |   {code_line}\n      |   ^",
    }


def pw_doc(cases: list[tuple[str, str, dict | None]], *, root: str = "/home/runner/work/debridpulse/debridpulse/frontend/browser") -> dict:
    """cases: (spec file, title, error-or-None)."""
    suites: dict[str, dict] = {}
    for file, title, error in cases:
        suite = suites.setdefault(file, {"title": file, "file": file, "specs": [], "suites": []})
        result = {"status": "failed", "error": error} if error else {"status": "passed"}
        suite["specs"].append({
            "title": title, "file": file, "line": 10, "column": 1,
            "tests": [{"status": "unexpected" if error else "expected", "results": [result]}],
        })
    failed = sum(1 for _f, _t, e in cases if e)
    return {"config": {"rootDir": root}, "suites": list(suites.values()), "errors": [],
            "stats": {"expected": len(cases) - failed, "unexpected": failed, "flaky": 0, "skipped": 0}}


def junit_doc(cases: list[tuple[str, str, str, str | None]]) -> str:
    """cases: (file, classname, name, failure message or None)."""
    body = []
    for file, classname, name, message in cases:
        head = f'<testcase classname="{classname}" name="{name}" file="{file}" line="1" time="0.01"'
        if message is None:
            body.append(head + " />")
        else:
            body.append(f'{head}><failure message="{message}">traceback</failure></testcase>')
    return f'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" tests="{len(cases)}">{"".join(body)}</testsuite></testsuites>'


def write_run(directory: Path, name: str, domain: str, cases) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if domain == "browser":
        (directory / f"{name}.json").write_text(json.dumps(pw_doc(cases)), encoding="utf-8")
    else:
        (directory / f"{name}.xml").write_text(junit_doc(cases), encoding="utf-8")


def layout(tmp_path: Path, domain: str, *, full, candidate_runs, anchor_runs, exit_code=1,
           anchor_infra: str | None = None, candidate_infra: str | None = None, missing_anchor=False,
           budget_exhausted: bool = False):
    """full/candidate_runs/anchor_runs are lists of case lists (one entry per run)."""
    candidate = tmp_path / "candidate"
    anchor = tmp_path / "anchor"
    write_run(candidate / "full", "results", domain, full)
    (candidate / "full" / "exit_code.txt").write_text(str(exit_code), encoding="utf-8")
    for index, cases in enumerate(candidate_runs):
        write_run(candidate / "isolated", f"case.{index}", domain, cases)
    if not missing_anchor:
        (anchor / "isolated").mkdir(parents=True, exist_ok=True)
        for index, cases in enumerate(anchor_runs):
            write_run(anchor / "isolated", f"case.{index}", domain, cases)
    if anchor_infra:
        (anchor / "isolated").mkdir(parents=True, exist_ok=True)
        (anchor / "isolated" / fc.INFRA_SENTINEL).write_text(anchor_infra, encoding="utf-8")
    if candidate_infra:
        (candidate / "isolated").mkdir(parents=True, exist_ok=True)
        (candidate / "isolated" / fc.INFRA_SENTINEL).write_text(candidate_infra, encoding="utf-8")
    if budget_exhausted:
        (candidate / "isolated").mkdir(parents=True, exist_ok=True)
        (candidate / "isolated" / fc.BUDGET_SENTINEL).write_text("exhausted", encoding="utf-8")
    return candidate, (None if missing_anchor else anchor)


def registry_file(tmp_path: Path, entries: list[dict]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"schema_version": 1, "entries": entries}), encoding="utf-8")
    return path


def entry(**overrides) -> dict:
    base = {
        "id": "example-flake", "domain": "browser", "case": f"a.spec.js{SEP}focus survives",
        "failure_signature": "placeholder", "first_confirmed_sha": "b" * 40,
        "tracking_reference": "docs/QUALIFICATION_DETERMINISM.md#5", "expires": "2026-11-01",
        "classifier_runs": 8,
    }
    base.update(overrides)
    return base


def classify(tmp_path, domain, candidate, anchor, *, registry=None, anchor_sha=ANCHOR, candidate_sha=CANDIDATE):
    reg = registry if registry is not None else registry_file(tmp_path, [])
    state = fc.load_registry(reg, today=TODAY, version=(1, 0, 12))
    return fc.classify_cases(
        domain, candidate_sha, anchor_sha,
        fc.load_full_results(domain, candidate), fc.load_run_set(domain, candidate), fc.load_run_set(domain, anchor), state,
    )


CASE_FILE, CASE_TITLE = "a.spec.js", "focus survives"
BAD = pw_error("expect(locator).toBeFocused() failed\n\nExpected: focused\nReceived: inactive", "await expect(remove).toBeFocused();")
OTHER = pw_error("expect(locator).toBeVisible() failed\n\nExpected: visible\nReceived: hidden", "await expect(panel).toBeVisible();")
RUNS = fc.ISOLATED_RUNS_PER_REF


def failing(error=BAD, file=CASE_FILE, title=CASE_TITLE):
    return [(file, title, error)]


def passing(file=CASE_FILE, title=CASE_TITLE):
    return [(file, title, None)]


# --------------------------------------------------------------------------------------
# Structure: exactly one classifier, one registry, delegated semantics.
# --------------------------------------------------------------------------------------
def _walk_files(*suffixes: str):
    skip = {"node_modules", ".venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "test-results", "playwright-report"}
    for path in ROOT.rglob("*"):
        if path.is_file() and path.suffix in suffixes and not (skip & set(path.relative_to(ROOT).parts)):
            yield path


def test_exactly_one_classifier_and_one_registry() -> None:
    classifiers = [p for p in _walk_files(".py") if "classif" in p.name and "failure" in p.name]
    assert classifiers == [CLASSIFIER_PATH]
    registries = [p for p in _walk_files(".json", ".yml", ".yaml", ".toml", ".txt")
                  if re.search(r"flake", p.name, re.I) and "node_modules" not in p.parts]
    assert registries == [REGISTRY_PATH]
    # No other module re-declares the classification vocabulary.
    definers = [p for p in _walk_files(".py") if "ANCHOR_REPRODUCED_FLAKE" in p.read_text(encoding="utf-8", errors="ignore")
                and p not in (CLASSIFIER_PATH, Path(__file__).resolve())]
    assert definers == []


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps(path: Path) -> list[dict]:
    return list(next(iter(_workflow(path)["jobs"].values()))["steps"])


def _runs(path: Path) -> str:
    return "\n".join(step["run"] for step in _steps(path) if step.get("run"))


def test_both_workflows_delegate_to_the_one_classifier() -> None:
    for path, domain in ((TESTS_WORKFLOW, "pytest"), (BROWSER_WORKFLOW, "browser")):
        text = _runs(path)
        for subcommand in ("budget", "resolve-anchor", "metadata"):
            assert f"failure_classifier.py\" {subcommand}" in text or f"$classifier\" {subcommand}" in text, (path.name, subcommand)
        assert f'plan --domain {domain}' in text and f'classify --domain {domain}' in text, path.name
        assert ".github/qualification/known_flakes.json" in text
        assert text.count("failure_classifier.py") >= 1
    # Workflows own execution; semantics live only in the classifier.
    for path in (TESTS_WORKFLOW, BROWSER_WORKFLOW):
        raw = path.read_text(encoding="utf-8")
        for word in ("CANDIDATE_REGRESSION", "ANCHOR_REPRODUCED_FLAKE", "INCONCLUSIVE", "INFRASTRUCTURE_FAILURE"):
            assert word not in raw, f"{path.name} must not restate classification semantics ({word})"
        assert raw.count("KNOWN_FLAKE") == raw.count("::warning title=KNOWN_FLAKE::"), "KNOWN_FLAKE may only label the warning annotation"


def test_workflows_publish_identity_and_use_the_explicit_anchor_inputs() -> None:
    for path in (TESTS_WORKFLOW, BROWSER_WORKFLOW):
        workflow = _workflow(path)
        checkout = next(step for step in _steps(path) if step["name"] == "Checkout")
        assert checkout["with"]["fetch-depth"] == 0
        dispatch = (workflow.get("on") or workflow.get(True))["workflow_dispatch"]
        assert dispatch["inputs"]["anchor_sha"]["required"] is False
        resolve = next(step for step in _steps(path) if step["name"] == "Resolve qualification anchor and publish identity")
        assert resolve["env"]["QUALIFICATION_BEFORE"] == "${{ github.event.before }}"
        assert resolve["env"]["QUALIFICATION_PR_BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
        assert resolve["env"]["QUALIFICATION_INPUT_ANCHOR"] == "${{ inputs.anchor_sha }}"
        for token in ("CANDIDATE_SHA", "QUALIFICATION_ANCHOR_SHA", "GITHUB_RUN_ID", "--summary-file", "--output-json"):
            assert token in resolve["run"], (path.name, token)
        uploads = [s for s in _steps(path) if str(s.get("uses", "")).startswith("actions/upload-artifact")]
        assert any("qualification-metadata" in s["with"]["name"] for s in uploads), path.name
        # Anchor derivation is delegated, never a shell fallback to main or a fixed SHA.
        assert "origin/main" not in resolve["run"] and "refs/heads/main" not in resolve["run"]


def test_playwright_retries_remain_zero_everywhere() -> None:
    config = (ROOT / "frontend" / "browser" / "playwright.config.js").read_text(encoding="utf-8")
    assert re.findall(r"\bretries\s*:\s*(\S+?)\s*,", config) == ["0"]
    for path in (TESTS_WORKFLOW, BROWSER_WORKFLOW, ROOT / "frontend" / "browser" / "package.json"):
        assert "--retries" not in path.read_text(encoding="utf-8"), path.name
    for spec_file in (ROOT / "frontend" / "browser").glob("*.spec.js"):
        text = spec_file.read_text(encoding="utf-8")
        assert not re.search(r"\bretries\s*:", text), spec_file.name
        assert "test.describe.configure" not in text, spec_file.name


def test_no_pytest_rerun_plugin_or_rerun_flag() -> None:
    forbidden = ("pytest-rerunfailures", "rerunfailures", "pytest-retry", "pytest_retry", "flaky==", "pytest-flakefinder", "pytest-repeat")
    for manifest in (ROOT / "backend").glob("requirements*.*"):
        text = manifest.read_text(encoding="utf-8").lower()
        for name in forbidden:
            assert name not in text, f"{manifest.name} contains {name}"
    for path in (TESTS_WORKFLOW, BROWSER_WORKFLOW):
        raw = path.read_text(encoding="utf-8")
        for token in ("--reruns", "reruns-delay", "rerunfailures", "gh run rerun", "gh workflow run", "workflow_run:", "--retries"):
            assert token not in raw, f"{path.name} contains {token}"
        assert "continue-on-error" not in raw, path.name
    for name in ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"):
        assert not (ROOT / "backend" / name).exists()


def _loop_depths(script: str) -> list[tuple[int, str]]:
    """(loop depth, logical line) for every non-comment line of a shell script."""
    depth, result = 0, []
    for raw in script.replace("\\\n", " ").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        result.append((depth, line))
        depth += len(re.findall(r"^(?:for|while|until)\b", line)) - len(re.findall(r"^done\b", line))
    return result


def test_no_automatic_full_suite_rerun_loops() -> None:
    browser = [(d, l) for d, l in _loop_depths(_runs(BROWSER_WORKFLOW))]
    full_browser = [(d, l) for d, l in browser if re.search(r"\bnpm test\b", l)]
    assert len(full_browser) == 1 and full_browser[0][0] == 0, "the full Browser Runtime suite runs exactly once, outside any loop"
    for depth, line in browser:
        if "playwright test" in line and "--list" not in line:
            assert depth > 0 and " -g " in f" {line} ", f"only bounded, single-case isolated runs may invoke playwright: {line}"
    pytest_lines = [(d, l) for d, l in _loop_depths(_runs(TESTS_WORKFLOW)) if "pytest" in l and "-m pytest" in l]
    full = [(d, l) for d, l in pytest_lines if re.search(r"-m pytest tests/(\s|$)", l)]
    assert len(full) == 1 and full[0][0] == 0, "the full pytest suite runs exactly once, outside any loop"
    for depth, line in pytest_lines:
        if line in [l for _d, l in full]:
            continue
        assert '"${cases[@]}"' in line or (depth > 0 and '"$nodeid"' in line), f"unexpected pytest invocation: {line}"


def test_every_workflow_loop_is_bounded_by_the_classifier_budget() -> None:
    for path in (TESTS_WORKFLOW, BROWSER_WORKFLOW):
        for _depth, line in _loop_depths(_runs(path)):
            if re.match(r"^(while|until)\b", line):
                assert re.match(r"^while IFS=\$'\\t' read -r [\w ]+; do$", line), f"unbounded loop in {path.name}: {line}"
            if re.match(r"^for\b", line) and "seq" in line:
                assert 'seq 1 "$ISOLATED_RUNS_PER_REF"' in line or "seq 1 90" in line or 'seq 1 "$count"' in line, f"{path.name}: {line}"
        text = _runs(path)
        if 'seq 1 "$count"' in text:
            # the run-count variable may only ever be one of the two budget values
            assert set(re.findall(r'count="([^"]+)"', text)) == {"$DISCRIMINATOR_RUNS_PER_REF", "$ISOLATED_RUNS_PER_REF"}
        raw = path.read_text(encoding="utf-8")
        # The budget has one owner: workflows never restate its values.
        assert "seq 1 8" not in raw and "seq 1 3" not in raw
        assert not re.search(r"\b(900|MAX_CASES_TO_CLASSIFY=|ISOLATED_RUNS_PER_REF=|DISCRIMINATOR_RUNS_PER_REF=)", raw)
        assert "seq 1 16" not in raw
        assert "MAX_CLASSIFICATION_WALL_TIME_SECONDS" in raw and "ISOLATED_RUNS_PER_REF" in raw


def test_first_failure_evidence_is_preserved_before_any_isolated_run() -> None:
    browser = _runs(BROWSER_WORKFLOW)
    assert browser.index('cp -a test-results "$full/test-results"') < browser.index("run_cases ")
    assert '--output="$pwout/$slug.$tag$i"' in browser, "isolated runs must not clean the original test-results"
    assert 'echo "$test_status" > "$full/exit_code.txt"' in browser
    assert "$full/results.json" in browser
    backend = _runs(TESTS_WORKFLOW)
    assert '--junitxml="$full/results.xml"' in backend and 'echo "$status" > "$full/exit_code.txt"' in backend
    for path, marker in ((BROWSER_WORKFLOW, "classification"), (TESTS_WORKFLOW, "classification")):
        uploads = [s for s in _steps(path) if str(s.get("uses", "")).startswith("actions/upload-artifact") and marker in s["with"]["name"]]
        assert uploads and uploads[0]["if"] == "always()"
        paths = uploads[0]["with"]["path"]
        assert "qualification/candidate" in paths and "qualification/anchor" in paths and "classification.json" in paths and "classification.md" in paths
    # The anchor is its own tree: own image, own dependency manifests, separate ports/venv.
    assert "docker build" in browser and "anchor_src/frontend/browser" in browser and "8082" in browser and "8083" in browser
    assert "anchor-venv" in backend and 'anchor_src/backend/requirements-dev.txt' in backend


# --------------------------------------------------------------------------------------
# Classifier structure: bounded budget, exact vocabulary, no network, git-only subprocess.
# --------------------------------------------------------------------------------------
def _module_constants() -> dict:
    tree = ast.parse(CLASSIFIER_PATH.read_text(encoding="utf-8"))
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    return values


def test_classifier_budget_is_bounded_and_single_owner() -> None:
    constants = _module_constants()
    assert constants["MAX_CASES_TO_CLASSIFY"] == 3 == fc.MAX_CASES_TO_CLASSIFY
    assert constants["ISOLATED_RUNS_PER_REF"] == 8 == fc.ISOLATED_RUNS_PER_REF
    assert constants["FULL_SUITE_AUTOMATIC_RERUNS"] == 0 == fc.FULL_SUITE_AUTOMATIC_RERUNS
    assert constants["DISCRIMINATOR_RUNS_PER_REF"] == 16 == fc.DISCRIMINATOR_RUNS_PER_REF
    assert 0 < fc.SPECIFICITY_ALPHA <= 0.05 and constants["SPECIFICITY_ALPHA"] == fc.SPECIFICITY_ALPHA
    assert 0 < fc.MAX_CLASSIFICATION_WALL_TIME_SECONDS <= 15 * 60
    assert 0 < fc.MAX_REGISTRY_ENTRIES <= 10 and 0 < fc.MAX_REGISTRY_EXPIRY_DAYS <= 180
    printed = subprocess.run([sys.executable, str(CLASSIFIER_PATH), "budget"], capture_output=True, text=True, check=True).stdout
    assert dict(line.split("=") for line in printed.split()) == {
        "MAX_CASES_TO_CLASSIFY": "3", "ISOLATED_RUNS_PER_REF": "8", "DISCRIMINATOR_RUNS_PER_REF": "16",
        "MAX_CLASSIFICATION_WALL_TIME_SECONDS": "900", "FULL_SUITE_AUTOMATIC_RERUNS": "0",
    }


def test_classification_set_and_exit_codes_are_exact() -> None:
    assert fc.CLASSIFICATIONS == ("PASS", "CANDIDATE_REGRESSION", "ANCHOR_REPRODUCED_FLAKE", "KNOWN_FLAKE", "INCONCLUSIVE", "INFRASTRUCTURE_FAILURE")
    assert set(fc.EXIT_CODES) == set(fc.CLASSIFICATIONS) and len(set(fc.EXIT_CODES.values())) == 6
    assert fc.NON_BLOCKING == {"PASS", "KNOWN_FLAKE"}
    assert fc.EXIT_CODES["PASS"] == 0 and fc.EXIT_CODES["KNOWN_FLAKE"] == 10
    assert all(code >= 20 for name, code in fc.EXIT_CODES.items() if name not in fc.NON_BLOCKING)
    doc = DOC.read_text(encoding="utf-8")
    for name in fc.CLASSIFICATIONS:
        assert f"`{name}`" in doc


def test_classifier_uses_only_stdlib_never_the_network_and_only_git_subprocesses() -> None:
    tree = ast.parse(CLASSIFIER_PATH.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)
    assert not imported & {"urllib", "http", "socket", "ssl", "ftplib", "smtplib", "xmlrpc", "requests", "httpx", "aiohttp"}
    source = CLASSIFIER_PATH.read_text(encoding="utf-8")
    assert "api.github.com" not in source and "GITHUB_TOKEN" not in source
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr in {"run", "Popen", "call", "check_output"} and getattr(n.func.value, "id", "") == "subprocess"]
    assert len(calls) == 1
    argv = calls[0].args[0]
    assert isinstance(argv, ast.List) and isinstance(argv.elts[0], ast.Constant) and argv.elts[0].value == "git"


PRODUCT_PATHS = (
    "backend/api/", "backend/application/", "backend/auth/", "backend/core/", "backend/db/", "backend/executors/",
    "backend/integrations/", "backend/postprocessors/", "backend/providers/", "backend/services/", "backend/transfers/",
    "frontend/static/", "Dockerfile", "entrypoint.sh",
)
QUALIFICATION_OWNED = (
    ".github/workflows/browser-runtime.yml", ".github/workflows/tests.yml",
    ".github/qualification/failure_classifier.py", ".github/qualification/known_flakes.json",
    "docs/QUALIFICATION_DETERMINISM.md", "backend/tests/test_qualification_infrastructure_contract.py",
    "backend/tests/test_universal_lifecycle.py",
    "frontend/browser/ui-regression-restoration.spec.js", "frontend/browser/ui-fix-ws1-p2.spec.js",
    "frontend/browser/ui-fix-ws2-p1.spec.js", "frontend/browser/settings-directory-browser.spec.js",
    "frontend/browser/group-candidates.spec.js", "frontend/browser/ui-presentation-owners.spec.js", "CLAUDE.md",
)


def test_qualification_infrastructure_lives_outside_product_source() -> None:
    for owned in QUALIFICATION_OWNED:
        assert (ROOT / owned).is_file(), owned
        assert not any(owned == product or owned.startswith(product) for product in PRODUCT_PATHS), owned
    # Product code never imports or reads the qualification infrastructure.
    for base in ("api", "application", "auth", "core", "db", "executors", "integrations", "postprocessors", "providers", "services", "transfers"):
        for path in (ROOT / "backend" / base).rglob("*.py"):
            assert "failure_classifier" not in path.read_text(encoding="utf-8"), path
    assert "known_flakes" not in (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "failure_classifier" not in dockerfile and "known_flakes" not in dockerfile and ".github" not in dockerfile


def test_documentation_and_claude_md_point_to_the_policy() -> None:
    doc = DOC.read_text(encoding="utf-8")
    for heading in ("anchor", "classification", "budget", "registry", "first-failure", "adversarial", "Gate 9"):
        assert heading.lower() in doc.lower()
    assert "Do not repeatedly rerun full qualification to chase green." in doc
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "docs/QUALIFICATION_DETERMINISM.md" in claude
    assert "Do not repeatedly rerun full qualification to chase green." in claude
    assert "Use candidate-vs-anchor classification." in claude
    # The product defect behind the WS2-P1 focus test was carried, deliberately unfixed, until the canonical
    # modal owner corrected it at its lifecycle boundary; the record now says exactly that and no more.
    section = doc.split("## 10. Findings carried by this workstream")[1].split("## 11.")[0]
    flat = " ".join(section.split())
    assert "ui-settings-modal.js" in flat and "resolved" in flat.lower()
    assert "not fixed and not masked" not in flat
    assert "parked on `<body>`" in flat
    assert "intermittently flaky" not in claude, "the mirror failover test is deterministic now"
    assert "test_mirrors_share_one_artifact_and_failover_retires_partial_bytes" not in claude.split("## 10.")[1]


# Standing adversarial lifecycle/concurrency preflight policy.
PREFLIGHT_FIELDS = (
    "Invariant", "Canonical owner", "Acquisition transition", "Release/finalization transition",
    "Cancellation behavior", "Crash behavior", "Restart behavior", "Timeout/lease behavior",
    "Stale-owner behavior", "Concurrent-owner behavior", "Batch/serial timing behavior", "Fail-closed behavior",
)
PREFLIGHT_CHALLENGES = (
    "live owner cannot be stolen", "dead owner cannot block forever", "stale owner cannot finalize",
    "long operation crosses nominal lease/timeout", "later batch item gets fresh timing",
    "cancellation at await boundaries", "callee swallows cancellation", "restart during ownership",
    "two workers race acquisition", "missing required lifecycle state fails closed",
)
PREFLIGHT_TRIGGERS = (
    "Ownership, leases, claims, locks, retry, recovery, cleanup, reconciliation, scheduler state,",
    "resource binding, selection / input-required, failover, concurrency and restart/crash",
)


def test_lifecycle_concurrency_preflight_is_a_standing_policy() -> None:
    doc = DOC.read_text(encoding="utf-8")
    section = doc.split("## 8. Lifecycle / concurrency adversarial preflight")[1].split("## 9.")[0]
    for text in PREFLIGHT_FIELDS + PREFLIGHT_CHALLENGES + PREFLIGHT_TRIGGERS:
        assert text in section, text
    assert "Gate 9 must not be the first place these cases are considered." in section
    assert "before production edits" in section


# --------------------------------------------------------------------------------------
# Registry validation.
# --------------------------------------------------------------------------------------
def test_committed_registry_validates_and_has_no_active_entries() -> None:
    state = fc.load_registry(REGISTRY_PATH, today=datetime.datetime.now(datetime.timezone.utc).date(), version=fc.parse_version((ROOT / "VERSION").read_text()))
    assert state.fatal == "" and state.rejected == []
    assert len(state.active) <= fc.MAX_REGISTRY_ENTRIES
    # The four known flakes were fixed, not registered. Adding an entry is a Gate 9 change and
    # must update this expectation deliberately.
    assert state.active == []
    assert subprocess.run([sys.executable, str(CLASSIFIER_PATH), "validate-registry", "--registry", str(REGISTRY_PATH)], capture_output=True).returncode == 0


@pytest.mark.parametrize("mutation,expected", [
    ({"case": f"*.spec.js{SEP}anything"}, "wildcard"),
    ({"case": f"a.spec.js{SEP}t?"}, "wildcard"),
    ({"case": "a.spec.js"}, "whole-file"),
    ({"domain": "pytest", "case": "tests/test_x.py"}, "whole-file"),
    ({"domain": "pytest", "case": "tests/test_x.py::"}, "whole-file"),
    ({"domain": "other"}, "domain"),
    ({"expires": ""}, "expires"),
    ({"expires": "someday"}, "expires"),
    ({"expires": "2026-09-18"}, "expired"),
    ({"expires": "2030-01-01"}, "days away"),
    ({"expires": "release:1.0.12"}, "release boundary"),
    ({"first_confirmed_sha": "abc"}, "first_confirmed_sha"),
    ({"tracking_reference": " "}, "tracking_reference"),
    ({"classifier_runs": 0}, "classifier_runs"),
    ({"classifier_runs": 8.5}, "classifier_runs"),
    ({"id": "X"}, "id"),
    ({"failure_signature": ""}, "failure_signature"),
])
def test_malformed_or_expired_registry_entries_are_rejected(tmp_path, mutation, expected) -> None:
    state = fc.load_registry(registry_file(tmp_path, [entry(**mutation)]), today=TODAY, version=(1, 0, 12))
    assert state.active == [] and len(state.rejected) == 1
    assert expected in state.rejected[0]["reason"]


def test_registry_shape_limits_and_release_boundary(tmp_path) -> None:
    unknown = entry()
    unknown["skip"] = True
    assert "unknown keys" in fc.load_registry(registry_file(tmp_path, [unknown]), today=TODAY, version=None).rejected[0]["reason"]
    missing = entry()
    del missing["expires"]
    assert "missing keys" in fc.load_registry(registry_file(tmp_path, [missing]), today=TODAY, version=None).rejected[0]["reason"]
    many = [entry(id=f"flake-{n:02d}", case=f"a.spec.js{SEP}t{n}") for n in range(fc.MAX_REGISTRY_ENTRIES + 1)]
    assert "exceeds" in fc.load_registry(registry_file(tmp_path, many), today=TODAY, version=None).fatal
    duplicate = fc.load_registry(registry_file(tmp_path, [entry(), entry(id="another-id")]), today=TODAY, version=None)
    assert len(duplicate.active) == 1 and "duplicate case" in duplicate.rejected[0]["reason"]
    assert fc.load_registry(tmp_path / "missing.json", today=TODAY, version=None).fatal
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert fc.load_registry(bad, today=TODAY, version=None).fatal
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"schema_version": 2, "entries": []}), encoding="utf-8")
    assert fc.load_registry(wrong, today=TODAY, version=None).fatal
    ok_release = fc.load_registry(registry_file(tmp_path, [entry(expires="release:1.0.13")]), today=TODAY, version=(1, 0, 12))
    assert len(ok_release.active) == 1
    unknown_version = fc.load_registry(registry_file(tmp_path, [entry(expires="release:1.0.13")]), today=TODAY, version=None)
    assert unknown_version.active == [] and "unavailable" in unknown_version.rejected[0]["reason"]
    assert len(fc.load_registry(registry_file(tmp_path, [entry(expires=TODAY.isoformat())]), today=TODAY, version=None).active) == 1


# --------------------------------------------------------------------------------------
# Anchor derivation (explicit, git-only, fail closed, never main).
# --------------------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    shas = {}
    for name in ("root", "one", "two"):
        (path / f"{name}.txt").write_text(name, encoding="utf-8")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", name)
        shas[name] = _git(path, "rev-parse", "HEAD")
    _git(path, "checkout", "-q", "-b", "1.0.12")
    (path / "branch.txt").write_text("b", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "branch tip")
    shas["tip"] = _git(path, "rev-parse", "HEAD")
    shas["repo"] = path
    return shas


def test_anchor_rules_per_event(repo) -> None:
    path, tip = repo["repo"], repo["tip"]
    assert fc.resolve_anchor("push", tip, before=repo["two"], repo=path) == (repo["two"], "push: github.event.before")
    assert fc.resolve_anchor("pull_request", tip, pr_base_sha=repo["one"], repo=path)[0] == repo["one"]
    assert fc.resolve_anchor("workflow_dispatch", tip, input_anchor=repo["root"], repo=path)[0] == repo["root"]
    assert fc.resolve_anchor("workflow_dispatch", tip, repo=path) == (repo["two"], "workflow_dispatch: candidate first parent")


def test_anchor_fails_closed_and_never_falls_back_to_main(repo) -> None:
    path, tip = repo["repo"], repo["tip"]
    main_tip = _git(path, "rev-parse", "main")
    cases = [
        fc.resolve_anchor("push", tip, before=fc.ZERO_SHA, repo=path),
        fc.resolve_anchor("push", tip, before="", repo=path),
        fc.resolve_anchor("push", tip, before="d" * 40, repo=path),
        fc.resolve_anchor("push", tip, before="not-a-sha", repo=path),
        fc.resolve_anchor("pull_request", tip, pr_base_sha="", repo=path),
        fc.resolve_anchor("pull_request", tip, pr_base_sha="e" * 40, repo=path),
        fc.resolve_anchor("workflow_dispatch", tip, input_anchor="f" * 40, repo=path),
        fc.resolve_anchor("workflow_dispatch", repo["root"], repo=path),  # root commit has no first parent
        fc.resolve_anchor("push", tip, before=tip, repo=path),  # anchor equal to candidate
        fc.resolve_anchor("schedule", tip, before=repo["two"], repo=path),
        fc.resolve_anchor("push", "short", before=repo["two"], repo=path),
    ]
    for anchor, reason in cases:
        assert anchor == fc.UNRESOLVED_ANCHOR and reason, (anchor, reason)
        assert anchor != main_tip
    # A tag/new-branch push does not silently borrow main's tip or any parent.
    assert fc.resolve_anchor("push", tip, before=fc.ZERO_SHA, repo=path)[0] != main_tip


def test_resolve_anchor_cli_exit_codes(repo) -> None:
    base = [sys.executable, str(CLASSIFIER_PATH), "resolve-anchor", "--repo", str(repo["repo"]), "--candidate-sha", repo["tip"]]
    ok = subprocess.run([*base, "--event", "push", "--before", repo["two"]], capture_output=True, text=True)
    assert ok.returncode == 0 and f"QUALIFICATION_ANCHOR_SHA={repo['two']}" in ok.stdout
    bad = subprocess.run([*base, "--event", "push", "--before", fc.ZERO_SHA], capture_output=True, text=True)
    assert bad.returncode == fc.ANCHOR_UNRESOLVED and "QUALIFICATION_ANCHOR_SHA=UNRESOLVED" in bad.stdout and "QUALIFICATION_ANCHOR_REASON=" in bad.stdout


# --------------------------------------------------------------------------------------
# Normalization: volatile noise only.
# --------------------------------------------------------------------------------------
def test_normalization_removes_only_volatile_noise() -> None:
    a = ("Error: expect(locator).toBeChecked() failed at /home/runner/work/debridpulse/debridpulse/frontend/browser/x.spec.js:272:3 "
         "http://127.0.0.1:8081/api/settings 2026-09-19T05:44:15.5805Z run_id: 35413335456 "
         "id 20670d5cec91443e84b9af12e1505182 sha " + "a" * 40 + " tmp /tmp/claude-1000/x/scratchpad/pristine/out.png")
    b = ("Error: expect(locator).toBeChecked() failed at /tmp/qualification/anchor-src/frontend/browser/x.spec.js:280:9 "
         "http://127.0.0.1:18081/api/settings 2027-01-02T00:00:01Z run_id: 99 "
         "id 09f77fb742414d9f9533174eab1a9284 sha " + "b" * 40 + " tmp /private/var/folders/zz/q/out.png")
    assert fc.normalize_text(a) == fc.normalize_text(b)
    assert fc.normalize_text("assert '20670d5cec91...9af12e1505182' == '09f77fb74241...3174eab1a9284'") == \
        fc.normalize_text("assert 'b57c94cc37e7...0424a94dd3a37' == '17d6a1e0ee5f...f2e6b1cdb8b21'")
    # Semantic differences are never normalized away.
    assert fc.normalize_text("Expected: checked\nReceived: unchecked") != fc.normalize_text("Expected: checked\nReceived: checked")
    assert fc.normalize_text("expect(a).toBe(1)\nExpected: 1\nReceived: 2") != fc.normalize_text("expect(a).toBe(1)\nExpected: 1\nReceived: 3")
    assert fc.normalize_text("TypeError: x is undefined") != fc.normalize_text("ReferenceError: x is undefined")
    assert fc.normalize_text("Timeout: 8000ms") != fc.normalize_text("Timeout: 45000ms")


def test_browser_signature_ignores_call_log_and_stack_but_not_assertion_or_code_line() -> None:
    def signature(error):
        document = pw_doc([(CASE_FILE, CASE_TITLE, error)])
        outcome = [o for o in _outcomes(document) if o.status == "failed"][0]
        return outcome.signature

    same_a = signature(pw_error("expect(x) failed\nExpected: 1\nReceived: 2", "await expect(x).toBe(1);", call_log="19 × waiting"))
    same_b = signature(pw_error("expect(x) failed\nExpected: 1\nReceived: 2", "await expect(x).toBe(1);", call_log="3 × different noise"))
    assert same_a == same_b
    assert same_a != signature(pw_error("expect(x) failed\nExpected: 1\nReceived: 3", "await expect(x).toBe(1);"))
    assert same_a != signature(pw_error("expect(x) failed\nExpected: 1\nReceived: 2", "await expect(y).toBe(1);"))
    with_stack = {"message": "TypeError: boom\n    at rect (eval at evaluate (:311:30))\n    at /home/runner/a.spec.js:343:30", "snippet": ""}
    without_stack = {"message": "TypeError: boom\n    at /tmp/other/b.spec.js:12:1", "snippet": ""}
    assert signature(with_stack) == signature(without_stack)


def _outcomes(document: dict):
    parsed = fc.ParsedResults()
    for suite in document["suites"]:
        fc._walk_playwright(suite, [], parsed.outcomes, suite["file"])
    return parsed.outcomes


# --------------------------------------------------------------------------------------
# The classification matrix, browser domain.
# --------------------------------------------------------------------------------------
def test_clean_suite_is_pass(tmp_path) -> None:
    candidate, anchor = layout(tmp_path, "browser", full=passing(), candidate_runs=[], anchor_runs=[], exit_code=0, missing_anchor=True)
    result = classify(tmp_path, "browser", candidate, anchor, anchor_sha=fc.UNRESOLVED_ANCHOR)
    assert result["classification"] == "PASS" and result["exit_code"] == 0
    assert result["candidate_sha"] == CANDIDATE and result["anchor_sha"] == fc.UNRESOLVED_ANCHOR


def test_candidate_failure_with_clean_anchor_is_candidate_regression(tmp_path) -> None:
    candidate, anchor = layout(tmp_path, "browser", full=failing(), candidate_runs=[failing()] * 7 + [passing()] * 1,
                               anchor_runs=[passing()] * RUNS)
    result = classify(tmp_path, "browser", candidate, anchor)
    assert result["classification"] == "CANDIDATE_REGRESSION" and result["exit_code"] == 20
    case = result["cases"][0]
    assert (case["candidate_reproductions"], case["candidate_runs"], case["anchor_reproductions"], case["anchor_runs"]) == (7, 8, 0, 8)
    assert case["specificity_p_value"] <= fc.SPECIFICITY_ALPHA and not case["needs_discriminator"]
    assert result["candidate_runs"] == 8 and result["anchor_runs"] == 8


def test_same_signature_on_candidate_and_anchor_is_anchor_reproduced_flake_and_fails(tmp_path) -> None:
    candidate, anchor = layout(tmp_path, "browser", full=failing(), candidate_runs=[failing()] * 2 + [passing()] * 6,
                               anchor_runs=[failing()] * 1 + [passing()] * 7)
    result = classify(tmp_path, "browser", candidate, anchor)
    assert result["classification"] == "ANCHOR_REPRODUCED_FLAKE" and result["exit_code"] == 21
    assert result["classification"] not in fc.NON_BLOCKING and result["known_flake_matches"] == []


def _flake_registry(tmp_path, signature, **overrides):
    return registry_file(tmp_path, [entry(failure_signature=signature, **overrides)])


def _known_signature() -> str:
    return _outcomes(pw_doc(failing()))[0].signature


def test_active_registry_plus_current_anchor_reproduction_is_known_flake(tmp_path) -> None:
    candidate, anchor = layout(tmp_path, "browser", full=failing(), candidate_runs=[failing()] * 3 + [passing()] * 5,
                               anchor_runs=[failing()] * 2 + [passing()] * 6)
    for signature in (_known_signature(), fc.signature_id(_known_signature())):
        result = classify(tmp_path, "browser", candidate, anchor, registry=_flake_registry(tmp_path, signature))
        assert result["classification"] == "KNOWN_FLAKE" and result["exit_code"] == 10
        assert result["known_flake_matches"] == ["example-flake"] and result["cases"][0]["known_flake_id"] == "example-flake"
    assert "WARNING" in fc.render_summary(result) and "KNOWN_FLAKE" in fc.render_summary(result)


def test_expired_registry_entry_is_not_known_flake(tmp_path) -> None:
    candidate, anchor = layout(tmp_path, "browser", full=failing(), candidate_runs=[failing()] * 3 + [passing()] * 5,
                               anchor_runs=[failing()] * 2 + [passing()] * 6)
    result = classify(tmp_path, "browser", candidate, anchor, registry=_flake_registry(tmp_path, _known_signature(), expires="2026-09-18"))
    assert result["classification"] == "ANCHOR_REPRODUCED_FLAKE"
    assert result["registry"]["active_entries"] == 0 and "expired" in result["registry"]["rejected_entries"][0]["reason"]


def test_registry_match_without_anchor_reproduction_is_not_known_flake(tmp_path) -> None:
    candidate, anchor = layout(tmp_path, "browser", full=failing(), candidate_runs=[failing()] * 8,
                               anchor_runs=[passing()] * RUNS)
    result = classify(tmp_path, "browser", candidate, anchor, registry=_flake_registry(tmp_path, _known_signature()))
    assert result["classification"] == "CANDIDATE_REGRESSION" and result["known_flake_matches"] == []


def test_registry_entry_never_waives_a_different_failure_signature(tmp_path) -> None:
    candidate, anchor = layout(tmp_path, "browser", full=failing(OTHER), candidate_runs=[failing(OTHER)] * 3 + [passing()] * 5,
                               anchor_runs=[failing(OTHER)] * 2 + [passing()] * 6)
    result = classify(tmp_path, "browser", candidate, anchor, registry=_flake_registry(tmp_path, _known_signature()))
    assert result["classification"] == "ANCHOR_REPRODUCED_FLAKE" and result["known_flake_matches"] == []
    other_test = registry_file(tmp_path, [entry(failure_signature=_outcomes(pw_doc(failing(OTHER)))[0].signature, case=f"b.spec.js{SEP}other")])
    assert classify(tmp_path, "browser", candidate, anchor, registry=other_test)["classification"] == "ANCHOR_REPRODUCED_FLAKE"


def test_different_signatures_on_candidate_and_anchor_are_not_known_flake(tmp_path) -> None:
    candidate, anchor = layout(tmp_path, "browser", full=failing(BAD), candidate_runs=[failing(BAD)] * 8,
                               anchor_runs=[failing(OTHER)] * 3 + [passing()] * 5)
    result = classify(tmp_path, "browser", candidate, anchor, registry=_flake_registry(tmp_path, _known_signature()))
    assert result["classification"] == "CANDIDATE_REGRESSION" and "different signature" in result["reason"]
    assert result["classification"] != "KNOWN_FLAKE"


def test_known_flake_needs_every_failing_case_to_be_covered(tmp_path) -> None:
    two = [(CASE_FILE, CASE_TITLE, BAD), (CASE_FILE, "second", OTHER)]
    rest_pass = [(CASE_FILE, CASE_TITLE, None), (CASE_FILE, "second", None)]
    candidate, anchor = layout(tmp_path, "browser", full=two, candidate_runs=[two] * 2 + [rest_pass] * 6, anchor_runs=[two] * 2 + [rest_pass] * 6)
    result = classify(tmp_path, "browser", candidate, anchor, registry=_flake_registry(tmp_path, _known_signature()))
    assert result["classification"] == "ANCHOR_REPRODUCED_FLAKE"
    assert sorted(case["verdict"] for case in result["cases"]) == ["ANCHOR_REPRODUCED_FLAKE", "KNOWN_FLAKE"]


def test_too_many_failing_cases_is_inconclusive_without_needing_any_run(tmp_path) -> None:
    many = [(CASE_FILE, f"case {n}", BAD) for n in range(fc.MAX_CASES_TO_CLASSIFY + 1)]
    candidate, anchor = layout(tmp_path, "browser", full=many, candidate_runs=[], anchor_runs=[], missing_anchor=True)
    result = classify(tmp_path, "browser", candidate, anchor, anchor_sha=fc.UNRESOLVED_ANCHOR)
    assert result["classification"] == "INCONCLUSIVE" and "exceed the classification budget" in result["reason"]
    plan_doc = tmp_path / "plan.json"
    code = fc.main(["plan", "--domain", "browser", "--candidate-results", str(candidate), "--output-json", str(plan_doc), "--output-tsv", str(tmp_path / "plan.tsv")])
    assert code == fc.PLAN_OVER_BUDGET and (tmp_path / "plan.tsv").read_text() == ""


@pytest.mark.parametrize("scenario", ["unresolved", "missing_dir", "infra_sentinel", "same_sha", "zero_sha", "candidate_infra", "corrupt_run"])
def test_missing_or_unusable_anchor_is_infrastructure_failure(tmp_path, scenario) -> None:
    kwargs = dict(full=failing(), candidate_runs=[failing()] * 3 + [passing()] * 5, anchor_runs=[failing()] * 2 + [passing()] * 6)
    anchor_sha = ANCHOR
    if scenario == "unresolved":
        anchor_sha = fc.UNRESOLVED_ANCHOR
    elif scenario == "missing_dir":
        kwargs["missing_anchor"] = True
    elif scenario == "infra_sentinel":
        kwargs["anchor_infra"] = "could not build the anchor image from its own tree"
    elif scenario == "same_sha":
        anchor_sha = CANDIDATE
    elif scenario == "zero_sha":
        anchor_sha = fc.ZERO_SHA
    elif scenario == "candidate_infra":
        kwargs["candidate_infra"] = "candidate runtime pair did not become healthy"
    candidate, anchor = layout(tmp_path, "browser", **kwargs)
    if scenario == "corrupt_run":
        (anchor / "isolated" / "case.0.json").write_text("{truncated", encoding="utf-8")
    result = classify(tmp_path, "browser", candidate, anchor, anchor_sha=anchor_sha)
    assert result["classification"] == "INFRASTRUCTURE_FAILURE" and result["exit_code"] == 23
    assert "flake" not in result["reason"].lower().replace("flaky", "")


def test_infrastructure_failure_outranks_other_case_verdicts(tmp_path) -> None:
    candidate, anchor = layout(tmp_path, "browser", full=failing(), candidate_runs=[failing()] * 3 + [passing()] * 5,
                               anchor_runs=[], anchor_infra="anchor checkout failed")
    assert classify(tmp_path, "browser", candidate, anchor)["classification"] == "INFRASTRUCTURE_FAILURE"


def test_candidate_failure_that_disappears_with_clean_anchor_is_inconclusive(tmp_path) -> None:
    candidate, anchor = layout(tmp_path, "browser", full=failing(), candidate_runs=[passing()] * RUNS, anchor_runs=[passing()] * RUNS)
    result = classify(tmp_path, "browser", candidate, anchor)
    assert result["classification"] == "INCONCLUSIVE" and "did not reproduce" in result["reason"]


@pytest.mark.parametrize("build,reason", [
    (lambda tmp: layout(tmp, "browser", full=failing(), candidate_runs=[failing()] * 2, anchor_runs=[passing()] * 3), "only 3 of 8"),
    (lambda tmp: layout(tmp, "browser", full=failing(), candidate_runs=[failing()] * 2, anchor_runs=[]), "no anchor isolated runs"),
    (lambda tmp: layout(tmp, "browser", full=failing(), candidate_runs=[], anchor_runs=[passing()] * RUNS), "no candidate isolated runs"),
    (lambda tmp: layout(tmp, "browser", full=failing(), candidate_runs=[failing()] * 2, anchor_runs=[passing()] * RUNS, budget_exhausted=True), "wall-time"),
    (lambda tmp: layout(tmp, "browser", full=failing(BAD), candidate_runs=[failing(OTHER)] * 3 + [passing()] * 5, anchor_runs=[passing()] * RUNS), "different signature than the full run"),
])
def test_missing_or_partial_evidence_is_inconclusive_never_a_verdict(tmp_path, build, reason) -> None:
    candidate, anchor = build(tmp_path)
    result = classify(tmp_path, "browser", candidate, anchor)
    assert result["classification"] == "INCONCLUSIVE" and reason in result["reason"]


def test_full_run_evidence_rules(tmp_path) -> None:
    # No exit code recorded -> required logs missing (INCONCLUSIVE); nonzero exit without a failing case -> infra.
    candidate, anchor = layout(tmp_path, "browser", full=passing(), candidate_runs=[], anchor_runs=[], exit_code=0, missing_anchor=True)
    (candidate / "full" / "exit_code.txt").unlink()
    assert classify(tmp_path, "browser", candidate, anchor)["classification"] == "INCONCLUSIVE"
    (candidate / "full" / "exit_code.txt").write_text("2", encoding="utf-8")
    result = classify(tmp_path, "browser", candidate, anchor)
    assert result["classification"] == "INFRASTRUCTURE_FAILURE" and "exited 2" in result["reason"]
    assert classify(tmp_path, "browser", None, None)["classification"] == "INCONCLUSIVE"
    empty = tmp_path / "empty"
    (empty / "full").mkdir(parents=True)
    assert classify(tmp_path, "browser", empty, None)["classification"] == "INCONCLUSIVE"


def test_a_later_isolated_success_never_erases_the_original_full_suite_failure(tmp_path) -> None:
    candidate, anchor = layout(tmp_path, "browser", full=failing(), candidate_runs=[passing()] * RUNS, anchor_runs=[passing()] * RUNS)
    registry = registry_file(tmp_path, [])
    before = _tree_digest(tmp_path)
    result = classify(tmp_path, "browser", candidate, anchor, registry=registry)
    assert result["classification"] != "PASS" and result["classification"] in fc.CLASSIFICATIONS
    assert [case["id"] for case in result["cases"]] == [f"{CASE_FILE}{SEP}{CASE_TITLE}"]
    assert _tree_digest(tmp_path) == before, "classification must not modify the recorded evidence"


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode() + path.read_bytes())
    return digest.hexdigest()


def test_ansi_and_environment_noise_do_not_split_the_same_failure(tmp_path) -> None:
    ansi = pw_error("expect(locator).toBeFocused() failed\n\nExpected: focused\nReceived: inactive", "await expect(remove).toBeFocused();")
    ansi["message"] = ansi["message"].replace("Expected:", "\x1b[2mExpected:\x1b[22m").replace("failed", "failed at http://127.0.0.1:8081/x /tmp/a/b.png")
    anchor_side = dict(BAD)
    anchor_side["message"] = anchor_side["message"].replace("failed", "failed at http://127.0.0.1:18081/x /tmp/zzz/c.png")
    candidate, anchor = layout(tmp_path, "browser", full=failing(ansi), candidate_runs=[failing(ansi)] * 2 + [passing()] * 6,
                               anchor_runs=[failing(anchor_side)] * 2 + [passing()] * 6)
    assert classify(tmp_path, "browser", candidate, anchor)["classification"] == "ANCHOR_REPRODUCED_FLAKE"


# --------------------------------------------------------------------------------------
# pytest domain.
# --------------------------------------------------------------------------------------
PY_FILE, PY_CLASS, PY_NAME = "tests/test_universal_lifecycle.py", "tests.test_universal_lifecycle", "test_mirror"
PY_FAIL = "AssertionError: assert '20670d5cec91...9af12e1505182' == '09f77fb74241...3174eab1a9284'"
PY_FAIL_OTHER = "AssertionError: assert 1 == 2"


def py_case(message=PY_FAIL, name=PY_NAME, classname=PY_CLASS):
    return [(PY_FILE, classname, name, message)]


def py_ok(name=PY_NAME, classname=PY_CLASS):
    return [(PY_FILE, classname, name, None)]


def test_pytest_domain_classification_matrix(tmp_path) -> None:
    node = f"{PY_FILE}::{PY_NAME}"
    candidate, anchor = layout(tmp_path / "reg", "pytest", full=py_case(), candidate_runs=[py_case()] * 8, anchor_runs=[py_ok()] * RUNS)
    result = classify(tmp_path / "reg", "pytest", candidate, anchor)
    assert result["classification"] == "CANDIDATE_REGRESSION" and result["cases"][0]["id"] == node

    both, anchor2 = layout(tmp_path / "both", "pytest", full=py_case(), candidate_runs=[py_case()] * 2 + [py_ok()] * 6,
                           anchor_runs=[py_case("AssertionError: assert 'b57c94cc37e7...0424a94dd3a37' == '17d6a1e0ee5f...f2e6b1cdb8b21'")] + [py_ok()] * 7)
    assert classify(tmp_path / "both", "pytest", both, anchor2)["classification"] == "ANCHOR_REPRODUCED_FLAKE"

    signature = fc.parse_junit_xml(both / "full" / "results.xml").outcomes[0].signature
    known = registry_file(tmp_path / "both", [entry(domain="pytest", case=node, failure_signature=signature)])
    assert classify(tmp_path / "both", "pytest", both, anchor2, registry=known)["classification"] == "KNOWN_FLAKE"

    diff, anchor3 = layout(tmp_path / "diff", "pytest", full=py_case(), candidate_runs=[py_case()] * 8, anchor_runs=[py_case(PY_FAIL_OTHER)] * 3 + [py_ok()] * 5)
    assert classify(tmp_path / "diff", "pytest", diff, anchor3)["classification"] == "CANDIDATE_REGRESSION"

    clean, _ = layout(tmp_path / "clean", "pytest", full=py_ok(), candidate_runs=[], anchor_runs=[], exit_code=0, missing_anchor=True)
    assert classify(tmp_path / "clean", "pytest", clean, None, anchor_sha=fc.UNRESOLVED_ANCHOR)["classification"] == "PASS"

    many, _ = layout(tmp_path / "many", "pytest", full=[(PY_FILE, PY_CLASS, f"test_{n}", PY_FAIL) for n in range(4)], candidate_runs=[], anchor_runs=[], missing_anchor=True)
    assert classify(tmp_path / "many", "pytest", many, None)["classification"] == "INCONCLUSIVE"

    unusable, none = layout(tmp_path / "noanchor", "pytest", full=py_case(), candidate_runs=[py_case()] * 3, anchor_runs=[], missing_anchor=True)
    assert classify(tmp_path / "noanchor", "pytest", unusable, none)["classification"] == "INFRASTRUCTURE_FAILURE"


def test_pytest_node_ids_include_classes_params_and_are_uuid_independent(tmp_path) -> None:
    xml = junit_doc([
        (PY_FILE, PY_CLASS + ".TestGroup", "test_in_class", "AssertionError: boom"),
        (PY_FILE, PY_CLASS, "test_param[a-1]", "ValueError: bad"),
        (PY_FILE, PY_CLASS, "test_ok", None),
    ])
    path = tmp_path / "r.xml"
    path.write_text(xml, encoding="utf-8")
    parsed = fc.parse_junit_xml(path)
    assert [(o.case_id, o.status) for o in parsed.outcomes] == [
        (f"{PY_FILE}::TestGroup::test_in_class", "failed"),
        (f"{PY_FILE}::test_param[a-1]", "failed"),
        (f"{PY_FILE}::test_ok", "passed"),
    ]


# --------------------------------------------------------------------------------------
# CLI contract and exit codes.
# --------------------------------------------------------------------------------------
def _cli(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CLASSIFIER_PATH), *args], capture_output=True, text=True)


def _classify_cli(tmp_path, candidate, anchor, registry, *, anchor_sha=ANCHOR) -> tuple[subprocess.CompletedProcess, dict, str]:
    out_json, out_md = tmp_path / "out.json", tmp_path / "out.md"
    done = _cli("classify", "--domain", "browser", "--candidate-sha", CANDIDATE, "--anchor-sha", anchor_sha,
                "--candidate-results", str(candidate), "--anchor-results", str(anchor), "--registry", str(registry),
                "--output-json", str(out_json), "--output-summary", str(out_md), "--today", TODAY.isoformat(), "--current-version", "1.0.12")
    return done, json.loads(out_json.read_text()), out_md.read_text()


def test_classify_cli_exit_codes_and_output_schema(tmp_path) -> None:
    expectations = {}
    layouts = {
        "PASS": dict(full=passing(), candidate_runs=[], anchor_runs=[], exit_code=0),
        "CANDIDATE_REGRESSION": dict(full=failing(), candidate_runs=[failing()] * 8, anchor_runs=[passing()] * RUNS),
        "ANCHOR_REPRODUCED_FLAKE": dict(full=failing(), candidate_runs=[failing()] * 3 + [passing()] * 5, anchor_runs=[failing()] * 2 + [passing()] * 6),
        "INCONCLUSIVE": dict(full=failing(), candidate_runs=[passing()] * RUNS, anchor_runs=[passing()] * RUNS),
        "INFRASTRUCTURE_FAILURE": dict(full=failing(), candidate_runs=[failing()] * 3, anchor_runs=[], anchor_infra="anchor build failed"),
    }
    for name, kwargs in layouts.items():
        base = tmp_path / name
        candidate, anchor = layout(base, "browser", **kwargs)
        anchor = anchor or (base / "anchor")
        done, document, summary = _classify_cli(base, candidate, anchor, registry_file(base, []))
        expectations[name] = done.returncode
        assert document["classification"] == name and done.returncode == fc.EXIT_CODES[name]
        assert {"classification", "candidate_sha", "anchor_sha", "cases", "known_flake_matches", "reason", "candidate_runs", "anchor_runs"} <= set(document)
        assert document["candidate_sha"] == CANDIDATE and document["anchor_sha"] == ANCHOR
        assert name in summary and CANDIDATE in summary and ANCHOR in summary
    base = tmp_path / "KNOWN"
    candidate, anchor = layout(base, "browser", full=failing(), candidate_runs=[failing()] * 3 + [passing()] * 5, anchor_runs=[failing()] * 2 + [passing()] * 6)
    done, document, _ = _classify_cli(base, candidate, anchor, _flake_registry(base, _known_signature()))
    assert done.returncode == 10 and document["classification"] == "KNOWN_FLAKE"
    assert expectations == {"PASS": 0, "CANDIDATE_REGRESSION": 20, "ANCHOR_REPRODUCED_FLAKE": 21, "INCONCLUSIVE": 22, "INFRASTRUCTURE_FAILURE": 23}
    assert _cli("classify", "--domain", "browser", "--candidate-sha", "nothex", "--anchor-sha", ANCHOR, "--candidate-results", "x",
                "--anchor-results", "y", "--registry", "z", "--output-json", str(tmp_path / "a"), "--output-summary", str(tmp_path / "b")).returncode == 2
    assert _cli("classify", "--domain", "cypress").returncode == 2


def test_plan_cli_exit_codes_and_execution_selectors(tmp_path) -> None:
    def run(base, **kwargs):
        candidate, _ = layout(base, "browser", candidate_runs=[], anchor_runs=[], missing_anchor=True, **kwargs)
        done = _cli("plan", "--domain", "browser", "--candidate-results", str(candidate), "--output-json", str(base / "plan.json"), "--output-tsv", str(base / "plan.tsv"))
        return done.returncode, json.loads((base / "plan.json").read_text()), (base / "plan.tsv").read_text()

    code, document, tsv = run(tmp_path / "clean", full=passing(), exit_code=0)
    assert code == fc.PLAN_NO_FAILURES and tsv == "" and document["failed"] == 0
    code, document, tsv = run(tmp_path / "one", full=[(CASE_FILE, "it's [odd] (title)", BAD)] + passing(), exit_code=1)
    assert code == fc.PLAN_WITHIN_BUDGET and document["distinct_failures"] == 1 and document["passed"] == 1
    slug, file, pattern = tsv.rstrip("\n").split("\t")
    assert file == CASE_FILE and re.fullmatch(pattern, "it's [odd] (title)") and slug == document["failing_cases"][0]["slug"]
    code, _, _ = run(tmp_path / "crash", full=passing(), exit_code=137)
    assert code == fc.PLAN_UNUSABLE
    plan_missing = _cli("plan", "--domain", "pytest", "--candidate-results", str(tmp_path / "nothing"), "--output-json", str(tmp_path / "m.json"))
    assert plan_missing.returncode == fc.PLAN_UNUSABLE


def test_pytest_plan_emits_exact_node_ids(tmp_path) -> None:
    candidate, _ = layout(tmp_path, "pytest", full=py_case() + py_ok("test_other"), candidate_runs=[], anchor_runs=[], missing_anchor=True)
    done = _cli("plan", "--domain", "pytest", "--candidate-results", str(candidate), "--output-json", str(tmp_path / "p.json"), "--output-tsv", str(tmp_path / "p.tsv"))
    assert done.returncode == fc.PLAN_WITHIN_BUDGET
    assert (tmp_path / "p.tsv").read_text().rstrip("\n").split("\t")[1] == f"{PY_FILE}::{PY_NAME}"


def test_metadata_cli_publishes_identity(tmp_path) -> None:
    summary = tmp_path / "summary.md"
    done = _cli("metadata", "--candidate-sha", CANDIDATE, "--anchor-sha", ANCHOR, "--event", "push", "--run-id", "12345",
                "--workflow", "Tests", "--output-json", str(tmp_path / "meta.json"), "--summary-file", str(summary))
    assert done.returncode == 0
    for text in (summary.read_text(), done.stdout):
        for line in (f"CANDIDATE_SHA={CANDIDATE}", f"QUALIFICATION_ANCHOR_SHA={ANCHOR}", "EVENT=push", "WORKFLOW_RUN_ID=12345"):
            assert line in text
    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["candidate_sha"] == CANDIDATE and meta["qualification_anchor_sha"] == ANCHOR and meta["workflow_run_id"] == "12345"
    unresolved = _cli("metadata", "--candidate-sha", CANDIDATE, "--anchor-sha", fc.UNRESOLVED_ANCHOR, "--anchor-reason", "push has no previous tip",
                      "--event", "push", "--run-id", "1", "--output-json", str(tmp_path / "m2.json"))
    assert "WARNING" in unresolved.stdout and "INFRASTRUCTURE_FAILURE" in unresolved.stdout


def test_validate_registry_cli_exit_codes(tmp_path) -> None:
    ok = registry_file(tmp_path, [entry()])
    assert _cli("validate-registry", "--registry", str(ok), "--today", TODAY.isoformat(), "--current-version", "1.0.12").returncode == 0
    bad = registry_file(tmp_path, [entry(expires="2026-01-01")])
    assert _cli("validate-registry", "--registry", str(bad), "--today", TODAY.isoformat()).returncode == fc.REGISTRY_INVALID


# --------------------------------------------------------------------------------------
# Evidentiary semantics: CANDIDATE_REGRESSION must discriminate candidate from anchor.
# Modeled on the failure found while building this classifier: candidate 1/8 vs anchor 0/8
# was reported as a regression although candidate and anchor were identical code and the
# test fails ~4% of the time on the anchor too.
# --------------------------------------------------------------------------------------
STAGE2 = RUNS + fc.DISCRIMINATOR_RUNS_PER_REF


def sample(failures: int, runs: int, error=BAD):
    return [failing(error)] * failures + [passing()] * (runs - failures)


def _pooled(tmp_path, *, cand, anchor, registry=None, domain="browser"):
    """cand/anchor are (failures, runs); the full run always failed with BAD."""
    candidate, anchor_dir = layout(tmp_path, "browser", full=failing(), candidate_runs=sample(*cand), anchor_runs=sample(*anchor))
    return classify(tmp_path, "browser", candidate, anchor_dir, registry=registry), candidate, anchor_dir


def test_specificity_test_is_exact_deterministic_and_monotonic() -> None:
    p = fc.specificity_p_value
    assert p(8, 8, 0, 8) == pytest.approx(1 / 12870)
    assert p(1, 8, 0, 8) == pytest.approx(0.5)
    assert p(0, 8, 0, 8) == 1.0 and p(3, 0, 0, 8) == 1.0 and p(3, 8, 0, 0) == 1.0
    assert p(3, 8, 0, 8) == pytest.approx(56 / 560)
    # the candidate must fail clearly more than the anchor: equal rates never discriminate
    assert p(4, 8, 4, 8) > 0.5 and p(1, 24, 1, 24) > 0.5
    fixed_anchor = [p(k, 8, 0, 8) for k in range(1, 9)]
    assert fixed_anchor == sorted(fixed_anchor, reverse=True)
    # first-stage bar at alpha=0.01: >= 6/8 against a clean anchor discriminates, 5/8 does not
    assert p(6, 8, 0, 8) <= fc.SPECIFICITY_ALPHA < p(5, 8, 0, 8)
    # after the bounded second stage: >= 7/24 discriminates, 6/24 does not
    assert p(7, 24, 0, 24) <= fc.SPECIFICITY_ALPHA < p(6, 24, 0, 24)


def test_sparse_first_stage_evidence_is_inconclusive_never_candidate_regression(tmp_path) -> None:
    result, candidate, anchor = _pooled(tmp_path, cand=(1, 8), anchor=(0, 8))
    assert result["classification"] == "INCONCLUSIVE" and result["exit_code"] == 22
    assert result["classification"] != "CANDIDATE_REGRESSION"
    case = result["cases"][0]
    assert (case["candidate_reproductions"], case["candidate_runs"], case["anchor_reproductions"], case["anchor_runs"]) == (1, 8, 0, 8)
    assert case["specificity_p_value"] == pytest.approx(0.5) and case["needs_discriminator"] is True
    assert case["id"] == f"{CASE_FILE}{SEP}{CASE_TITLE}" and result["discriminator_needed"] == [case["id"]]
    assert "do not establish candidate specificity" in result["reason"] and "rare pre-existing failure" in result["reason"]
    summary = fc.render_summary(result)
    assert "NOT a candidate regression" in summary and "do not change candidate source" in summary


def _discriminate_cli(tmp_path, candidate, anchor, domain="browser") -> tuple[int, str]:
    tsv = tmp_path / "discriminate.tsv"
    done = _cli("discriminate", "--domain", domain, "--candidate-sha", CANDIDATE, "--anchor-sha", ANCHOR,
                "--candidate-results", str(candidate), "--anchor-results", str(anchor),
                "--registry", str(registry_file(tmp_path, [])), "--output-tsv", str(tsv))
    return done.returncode, tsv.read_text()


def test_discriminate_lists_only_the_affected_case_and_cannot_loop(tmp_path) -> None:
    # only the case whose evidence cannot discriminate is listed: A fails 1/8 vs 0/8, B fails 8/8 vs 0/8
    mixed = [(CASE_FILE, CASE_TITLE, BAD), (CASE_FILE, "second", OTHER)]
    clean = [(CASE_FILE, CASE_TITLE, None), (CASE_FILE, "second", None)]
    candidate, anchor = layout(tmp_path / "s1", "browser", full=mixed,
                               candidate_runs=[mixed] + [[(CASE_FILE, CASE_TITLE, None), (CASE_FILE, "second", OTHER)]] * 7,
                               anchor_runs=[clean] * 8)
    code, tsv = _discriminate_cli(tmp_path / "s1", candidate, anchor)
    assert code == fc.DISCRIMINATOR_NEEDED == 14
    rows = tsv.rstrip("\n").split("\n")
    assert len(rows) == 1 and rows[0].split("\t")[2] == re.escape(CASE_TITLE) and "second" not in rows[0]
    # sparse case: 1/8 vs 0/8 needs the second stage
    sparse_cand, sparse_anchor = layout(tmp_path / "s2", "browser", full=failing(), candidate_runs=sample(1, 8), anchor_runs=sample(0, 8))
    code, tsv = _discriminate_cli(tmp_path / "s2", sparse_cand, sparse_anchor)
    slug, file, pattern = tsv.rstrip("\n").split("\t")
    assert code == 14 and file == CASE_FILE and re.fullmatch(pattern, CASE_TITLE) and slug == fc.slugify(f"{CASE_FILE}{SEP}{CASE_TITLE}")
    # the second stage is spent once: pooled evidence never asks for another
    spent_cand, spent_anchor = layout(tmp_path / "s3", "browser", full=failing(), candidate_runs=sample(2, STAGE2), anchor_runs=sample(0, STAGE2))
    code, tsv = _discriminate_cli(tmp_path / "s3", spent_cand, spent_anchor)
    assert code == 0 and tsv == ""
    # a discriminating first stage, a clean candidate, and an infrastructure failure need no second stage
    for name, kwargs in {
        "clear": dict(candidate_runs=sample(8, 8), anchor_runs=sample(0, 8)),
        "clean": dict(candidate_runs=sample(0, 8), anchor_runs=sample(0, 8)),
        "infra": dict(candidate_runs=sample(1, 8), anchor_runs=[], anchor_infra="anchor build failed"),
    }.items():
        cand_dir, anch_dir = layout(tmp_path / name, "browser", full=failing(), **kwargs)
        assert _discriminate_cli(tmp_path / name, cand_dir, anch_dir)[0] == 0, name


def test_pytest_discriminate_emits_the_exact_node_id(tmp_path) -> None:
    candidate, anchor = layout(tmp_path, "pytest", full=py_case(), candidate_runs=[py_case()] + [py_ok()] * 7, anchor_runs=[py_ok()] * 8)
    code, tsv = _discriminate_cli(tmp_path, candidate, anchor, domain="pytest")
    assert code == 14 and tsv.rstrip("\n").split("\t")[1] == f"{PY_FILE}::{PY_NAME}"
    result = classify(tmp_path, "pytest", candidate, anchor)
    assert result["classification"] == "INCONCLUSIVE" and result["discriminator_needed"] == [f"{PY_FILE}::{PY_NAME}"]


def test_clearly_candidate_specific_evidence_is_a_regression_on_the_fast_path_and_after_the_bounded_stage(tmp_path) -> None:
    fast, _, _ = _pooled(tmp_path / "fast", cand=(8, 8), anchor=(0, 8))
    assert fast["classification"] == "CANDIDATE_REGRESSION" and fast["cases"][0]["needs_discriminator"] is False
    # 4/8 vs 0/8 is not yet discriminating; after the bounded stage 12/24 vs 0/24 is
    first, _, _ = _pooled(tmp_path / "first", cand=(4, 8), anchor=(0, 8))
    assert first["classification"] == "INCONCLUSIVE" and first["cases"][0]["needs_discriminator"] is True
    staged, _, _ = _pooled(tmp_path / "staged", cand=(12, STAGE2), anchor=(0, STAGE2))
    assert staged["classification"] == "CANDIDATE_REGRESSION" and staged["exit_code"] == 20
    assert staged["cases"][0]["specificity_p_value"] <= fc.SPECIFICITY_ALPHA and staged["discriminator_needed"] == []
    assert "discriminate candidate from anchor" in staged["reason"]


def test_a_rare_common_flake_observed_on_the_anchor_in_the_bounded_stage_is_not_a_candidate_regression(tmp_path) -> None:
    result, _, _ = _pooled(tmp_path / "plain", cand=(4, STAGE2), anchor=(1, STAGE2))
    assert result["classification"] == "ANCHOR_REPRODUCED_FLAKE" and result["exit_code"] == 21
    known, _, _ = _pooled(tmp_path / "known", cand=(4, STAGE2), anchor=(1, STAGE2),
                          registry=registry_file(tmp_path / "known", [entry(failure_signature=_known_signature())]))
    assert known["classification"] == "KNOWN_FLAKE" and known["known_flake_matches"] == ["example-flake"]
    # a registry entry alone still never waives a failure the anchor did not reproduce
    weak, _, _ = _pooled(tmp_path / "weak_reg", cand=(1, 8), anchor=(0, 8),
                         registry=registry_file(tmp_path / "weak_reg", [entry(failure_signature=_known_signature())]))
    assert weak["classification"] == "INCONCLUSIVE" and weak["known_flake_matches"] == []


def test_weak_evidence_stays_inconclusive_after_the_bounded_stage(tmp_path) -> None:
    for candidate_failures in (1, 3, 6):
        result, candidate, anchor = _pooled(tmp_path / str(candidate_failures), cand=(candidate_failures, STAGE2), anchor=(0, STAGE2))
        assert result["classification"] == "INCONCLUSIVE", candidate_failures
        assert result["cases"][0]["needs_discriminator"] is False and "already spent" in result["reason"]
        assert _discriminate_cli(tmp_path / str(candidate_failures), candidate, anchor)[0] == 0
    strong, _, _ = _pooled(tmp_path / "7", cand=(7, STAGE2), anchor=(0, STAGE2))
    assert strong["classification"] == "CANDIDATE_REGRESSION"


def test_a_failed_full_run_with_no_candidate_reproduction_is_inconclusive_and_preserves_the_failure(tmp_path) -> None:
    result, candidate, anchor = _pooled(tmp_path, cand=(0, STAGE2), anchor=(0, STAGE2))
    assert result["classification"] == "INCONCLUSIVE" and "did not reproduce" in result["reason"]
    case = result["cases"][0]
    assert case["id"] == f"{CASE_FILE}{SEP}{CASE_TITLE}" and case["signature"] and case["needs_discriminator"] is False
    assert result["discriminator_needed"] == [] and _discriminate_cli(tmp_path, candidate, anchor)[0] == 0
    assert json.loads((candidate / "full" / "results.json").read_text())["stats"]["unexpected"] == 1


def test_the_discovered_scenario_exits_22_through_the_cli_not_20(tmp_path) -> None:
    candidate, anchor = layout(tmp_path, "browser", full=failing(), candidate_runs=sample(1, 8), anchor_runs=sample(0, 8))
    done, document, summary = _classify_cli(tmp_path, candidate, anchor, registry_file(tmp_path, []))
    assert done.returncode == 22 and document["classification"] == "INCONCLUSIVE"
    assert document["discriminator_needed"] and "NOT a candidate regression" in summary


def test_workflows_run_the_bounded_second_stage_once_and_never_in_a_loop() -> None:
    for path, domain in ((TESTS_WORKFLOW, "pytest"), (BROWSER_WORKFLOW, "browser")):
        lines = _loop_depths(_runs(path))
        calls = [(depth, line) for depth, line in lines if f"discriminate --domain {domain}" in line]
        assert len(calls) == 1 and calls[0][0] == 0, path.name
        text = _runs(path)
        assert text.count('"$DISCRIMINATOR_RUNS_PER_REF"') == 1 and text.index("discriminate --domain") > text.index("plan --domain")
        # the second-stage runs are gated on the classifier's own exit code
        assert '[ "$?" -eq 14 ]' in text
        # the classifier owns the decision: no verdict logic or thresholds in shell
        assert "specificity" not in text.lower() and "alpha" not in text.lower()


def test_documentation_states_the_evidentiary_rule() -> None:
    doc = " ".join(DOC.read_text(encoding="utf-8").split())
    claude = " ".join((ROOT / "CLAUDE.md").read_text(encoding="utf-8").split())
    for text in (doc, claude):
        assert "CANDIDATE_REGRESSION is an evidence-backed discrimination" in text
        assert "not merely \"candidate happened to fail and anchor happened not to fail in a small sample.\"" in text
        assert "INCONCLUSIVE is the required classification when bounded evidence cannot distinguish a rare candidate regression from low-rate pre-existing nondeterminism." in text
        assert "must not alter production source solely because an isolated candidate sample contains a failure while a small anchor sample happens to contain none" in text
