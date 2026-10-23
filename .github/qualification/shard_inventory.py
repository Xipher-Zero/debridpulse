#!/usr/bin/env python3
"""Qualification shard planning and the exactly-once inventory (stdlib only).

Execution only: this never judges a failure (that is failure_classifier.py's).
It answers two questions for the Tests and Browser Runtime workflows:

* which whole spec files a Browser Runtime shard runs (``browser-plan``), a
  deterministic duration-weighted partition of the spec files that exist,
  from the committed ``browser_spec_seconds.json``: longest first onto the
  least-loaded shard; a file missing from the table weighs the table's median.
  A spec file never splits.
* whether every required test ran exactly once, in the shard that owns it
  (``pytest-manifest`` / ``pytest-verify``, ``browser-verify``). Missing shard
  evidence, a test that ran twice or not at all, an unexpected test, or a
  skipped one fails the inventory: it is never a pass by default.

Exit codes: 0 consistent, 1 inconsistent, 2 usage or unreadable evidence.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
BROWSER_TABLE = HERE / "browser_spec_seconds.json"


def parse_shard(value: str) -> tuple[int, int]:
    index, _, total = value.partition("/")
    shard, shards = int(index), int(total)
    if not 1 <= shard <= shards:
        raise SystemExit(f"shard must be k/n with 1 <= k <= n, not {value!r}")
    return shard, shards


def browser_plan(spec_dir: Path, shards: int, table_path: Path = BROWSER_TABLE) -> dict[str, int]:
    """Every spec file in ``spec_dir`` -> its 1-based shard."""
    seconds = {str(name): float(value) for name, value in
               json.loads(table_path.read_text(encoding="utf-8"))["seconds"].items()}
    files = sorted(path.name for path in spec_dir.glob("*.spec.js"))
    if not files:
        raise SystemExit(f"no spec files in {spec_dir}")
    default = statistics.median(seconds.values()) if seconds else 1.0
    load = dict.fromkeys(range(1, shards + 1), 0.0)
    plan: dict[str, int] = {}
    for name in sorted(files, key=lambda name: (-seconds.get(name, default), name)):
        target = min(load, key=lambda shard: (load[shard], shard))
        plan[name] = target
        load[target] += seconds.get(name, default)
    return plan


# -- pytest ------------------------------------------------------------------------

def node_case(node_id: str) -> tuple[str, str]:
    """A pytest node id as its xunit1 (classname, name), exactly as pytest's
    junitxml ``mangle_test_address`` derives them: only the part before the
    first ``[`` is split on ``::`` (a parameter may itself contain ``::``)."""
    path, bracket, params = node_id.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + params
    return ".".join(names[:-1]), names[-1]


def pytest_manifest(backend: Path, collected: dict[str, Path], shards: dict[str, int]) -> dict:
    sys.path.insert(0, str(backend / "tests"))
    from conftest import shard_of  # the one partition owner
    manifest = {}
    for layer, path in collected.items():
        nodes = sorted({line.strip() for line in path.read_text(encoding="utf-8").splitlines() if "::" in line})
        if not nodes:
            raise SystemExit(f"no {layer} tests collected in {path}")
        manifest[layer] = {"shards": shards[layer],
                           "nodes": {node: shard_of(node, shards[layer], layer) for node in nodes}}
        empty = sorted(set(range(1, shards[layer] + 1)) - set(manifest[layer]["nodes"].values()))
        if empty:
            # A shard owning nothing (a stale isolated entry, say) would run no test at all.
            raise SystemExit(f"{layer}: shard(s) {empty} own no collected test")
    return manifest


def read_manifest(path: Path) -> dict:
    """The inventory manifest: one file, or a directory holding every run
    attempt's copy (in any layout the download produced), which must agree."""
    copies = sorted(path.rglob("manifest.json")) if path.is_dir() else [path]
    if not copies:
        raise OSError(f"no manifest.json under {path}")
    texts = {copy.read_bytes() for copy in copies}
    if len(texts) != 1:
        raise ValueError(f"{len(copies)} manifest copies under {path} disagree")
    return json.loads(texts.pop())


def _latest_attempts(root: Path, pattern: str) -> list[Path]:
    """One evidence directory per shard: its latest run attempt."""
    latest: dict[str, tuple[int, Path]] = {}
    for directory in sorted(root.glob(pattern)):
        identity, _, attempt = directory.name.rpartition("-attempt-")
        number = int(attempt) if attempt.isdigit() else 0
        if identity not in latest or number > latest[identity][0]:
            latest[identity] = (number, directory)
    return [directory for _number, directory in sorted(latest.values(), key=lambda item: str(item[1]))]


def pytest_verify(manifest: dict, executed_root: Path) -> list[str]:
    problems: list[str] = []
    expected = {layer: {node_case(node): shard for node, shard in data["nodes"].items()}
                for layer, data in manifest.items()}
    seen = {layer: {} for layer in manifest}
    shards_seen = {layer: set() for layer in manifest}
    for directory in _latest_attempts(executed_root, "pytest-executed-*"):
        identity = (directory / "shard.txt").read_text(encoding="utf-8").split()
        layer, shard = identity[0], parse_shard(identity[1])[0]
        if layer not in manifest:
            problems.append(f"{directory.name}: unknown layer {layer}")
            continue
        shards_seen[layer].add(shard)
        results = directory / "results.xml"
        if not results.is_file():
            problems.append(f"{layer} shard {shard}: no results.xml (the shard did not finish)")
            continue
        here = set()                          # a call failure plus a teardown error is two elements, one test
        for case in ET.parse(results).getroot().iter("testcase"):
            key = (case.get("classname") or "", case.get("name") or "")
            if case.find("skipped") is not None:
                problems.append(f"{layer} shard {shard}: skipped {key[0]}::{key[1]}")
            here.add(key)
        for key in here:
            seen[layer].setdefault(key, []).append(shard)
    for layer, data in manifest.items():
        missing_shards = set(range(1, data["shards"] + 1)) - shards_seen[layer]
        if missing_shards:
            problems.append(f"{layer}: no evidence from shard(s) {sorted(missing_shards)}")
        for key, owner in sorted(expected[layer].items()):
            ran = seen[layer].get(key, [])
            if not ran and owner not in missing_shards:
                problems.append(f"{layer}: never ran {key[0]}::{key[1]} (owned by shard {owner})")
            elif len(ran) > 1:
                problems.append(f"{layer}: ran {len(ran)} times {key[0]}::{key[1]} (shards {ran})")
            elif ran and ran[0] != owner:
                problems.append(f"{layer}: {key[0]}::{key[1]} ran in shard {ran[0]}, owned by shard {owner}")
        for key in sorted(set(seen[layer]) - set(expected[layer])):
            problems.append(f"{layer}: ran an uninventoried test {key[0]}::{key[1]}")
    return problems


# -- browser ------------------------------------------------------------------------

def _browser_cases(suite: dict, path: tuple[str, ...], out: list) -> None:
    title = suite.get("title", "")
    here = path if not title or title == suite.get("file") else (*path, title)
    for spec in suite.get("specs", []):
        for test in spec.get("tests", []):
            out.append((f"{spec['file']}:{spec['line']}:{spec['column']}", " › ".join((*here, spec["title"])),
                        test.get("status")))
    for child in suite.get("suites", []):
        _browser_cases(child, here, out)


def browser_listed(text: str) -> list[tuple[str, str]]:
    """Cases of a ``playwright test --list`` listing as (file:line:col, title path)."""
    cases = []
    for line in text.splitlines():
        line = line.strip()
        if ".spec.js:" not in line or " › " not in line or line.startswith("Total:"):
            continue
        if line.startswith("[") and "] › " in line:          # a named project prefix
            line = line.split("] › ", 1)[1]
        location, _, title = line.partition(" › ")
        cases.append((location, title))
    return cases


def browser_verify(full_list: Path, executed_root: Path, shards: int, spec_dir: Path) -> list[str]:
    problems: list[str] = []
    expected = browser_listed(full_list.read_text(encoding="utf-8"))
    if not expected:
        return [f"{full_list}: no listed cases"]
    plan = browser_plan(spec_dir, shards)
    counts: dict[tuple[str, str], list[int]] = {}
    shards_seen = set()
    for directory in _latest_attempts(executed_root, "browser-executed-*"):
        shard = parse_shard((directory / "shard.txt").read_text(encoding="utf-8").split()[0])[0]
        shards_seen.add(shard)
        results = directory / "results.json"
        if not results.is_file():
            problems.append(f"shard {shard}: no results.json (the shard did not finish)")
            continue
        cases: list = []
        for suite in json.loads(results.read_text(encoding="utf-8")).get("suites", []):
            _browser_cases(suite, (), cases)
        for location, title, status in cases:
            if status == "skipped":
                problems.append(f"shard {shard}: skipped {location} › {title}")
            counts.setdefault((location, title), []).append(shard)
            owner = plan.get(location.split(":", 1)[0])
            if owner != shard:
                problems.append(f"{location} › {title} ran in shard {shard}, owned by shard {owner}")
    missing_shards = set(range(1, shards + 1)) - shards_seen
    if missing_shards:
        problems.append(f"no evidence from shard(s) {sorted(missing_shards)}")
    for case in expected:
        ran = counts.get(case, [])
        if not ran and plan.get(case[0].split(":", 1)[0]) not in missing_shards:
            problems.append(f"never ran {case[0]} › {case[1]}")
        elif len(ran) > 1:
            problems.append(f"ran {len(ran)} times {case[0]} › {case[1]} (shards {ran})")
    for case in sorted(set(counts) - set(expected)):
        problems.append(f"ran an unlisted case {case[0]} › {case[1]}")
    return problems


def _report(problems: list[str], what: str) -> int:
    if problems:
        print(f"{what}: INCONSISTENT ({len(problems)} problem(s))")
        for problem in problems[:200]:
            print(f"- {problem}")
        return 1
    print(f"{what}: every required test ran exactly once, in the shard that owns it")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("browser-plan")
    plan.add_argument("--shard", required=True)
    plan.add_argument("--dir", type=Path, default=Path("frontend/browser"))
    manifest = sub.add_parser("pytest-manifest")
    manifest.add_argument("--backend", type=Path, default=Path("backend"))
    manifest.add_argument("--contract", type=Path, required=True)
    manifest.add_argument("--contract-shards", type=int, required=True)
    manifest.add_argument("--runtime", type=Path, required=True)
    manifest.add_argument("--runtime-shards", type=int, required=True)
    manifest.add_argument("--out", type=Path, required=True)
    verify = sub.add_parser("pytest-verify")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--executed", type=Path, required=True)
    bverify = sub.add_parser("browser-verify")
    bverify.add_argument("--full-list", type=Path, required=True)
    bverify.add_argument("--executed", type=Path, required=True)
    bverify.add_argument("--shards", type=int, required=True)
    bverify.add_argument("--dir", type=Path, default=Path("frontend/browser"))
    args = parser.parse_args(argv)
    try:
        if args.command == "browser-plan":
            shard, shards = parse_shard(args.shard)
            files = [name for name, owner in sorted(browser_plan(args.dir, shards).items()) if owner == shard]
            print("\n".join(files))
            return 0 if files else 1
        if args.command == "pytest-manifest":
            data = pytest_manifest(args.backend, {"contract": args.contract, "runtime": args.runtime},
                                   {"contract": args.contract_shards, "runtime": args.runtime_shards})
            args.out.write_text(json.dumps(data, indent=0, sort_keys=True), encoding="utf-8")
            for layer, layer_data in data.items():
                counts = {}
                for owner in layer_data["nodes"].values():
                    counts[owner] = counts.get(owner, 0) + 1
                print(f"{layer}: {len(layer_data['nodes'])} tests over {layer_data['shards']} shards: "
                      + ", ".join(f"{shard}={counts.get(shard, 0)}" for shard in range(1, layer_data["shards"] + 1)))
            return 0
        if args.command == "pytest-verify":
            return _report(pytest_verify(read_manifest(args.manifest), args.executed),
                           "pytest inventory")
        return _report(browser_verify(args.full_list, args.executed, args.shards, args.dir), "browser inventory")
    except (OSError, ValueError, KeyError, ET.ParseError) as error:
        print(f"inventory evidence unreadable: {error}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
