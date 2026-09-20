# Qualification determinism policy

Canonical human policy for how DebridPulse qualification classifies failures. The
machine owner of every rule below is
[`.github/qualification/failure_classifier.py`](../.github/qualification/failure_classifier.py);
workflows own execution only. This document, the classifier, the registry and
`backend/tests/test_qualification_infrastructure_contract.py` must agree, and the
contract test enforces that they do.

> **Do not repeatedly rerun full qualification to chase green.**
> A failed full suite is evidence. Classify it candidate-vs-anchor, once, and act on the
> classification.

## 1. Non-negotiables

There is exactly one of each of the following, and nothing may add a second:

| Thing | Owner |
|---|---|
| Backend Tests workflow | `.github/workflows/tests.yml` |
| Browser Runtime workflow | `.github/workflows/browser-runtime.yml` |
| Failure classifier (normalization, registry validation, semantics, results) | `.github/qualification/failure_classifier.py` |
| Known-flake registry | `.github/qualification/known_flakes.json` |
| Diagnostic budget values | constants at the top of the classifier (`failure_classifier.py budget`) |

Forbidden, everywhere (enforced by the contract test): Playwright `retries` above zero,
any pytest rerun plugin or `--reruns`, `continue-on-error` used to turn a failure green,
`gh run rerun` or any automatic re-dispatch, sleeps or timeout inflation as a correctness
mechanism, a second classifier, a second registry, and any special-casing of a SHA, session
or agent. A test that is nondeterministic is **fixed**, not tolerated.

## 2. Qualification identity and anchor

Every Browser Runtime and Tests run publishes, to `$GITHUB_STEP_SUMMARY` and to an uploaded
`*-qualification-metadata-<sha>` artifact:

```text
CANDIDATE_SHA=
QUALIFICATION_ANCHOR_SHA=
EVENT=
WORKFLOW_RUN_ID=
```

The anchor is the known baseline that a failure is compared against. It is derived by
`failure_classifier.py resolve-anchor` using local git objects only (both workflows check
out with `fetch-depth: 0`):

| Event | Anchor |
|---|---|
| `push` | `github.event.before`, if non-zero and a commit reachable in the checkout |
| `pull_request` | `github.event.pull_request.base.sha` |
| `workflow_dispatch` | the optional `anchor_sha` input; if omitted, the candidate's first parent |

**Fail closed.** If no unambiguous anchor exists (a new branch or tag push has an all-zero
`before`, a force-push whose previous tip is not fetched, a root commit, an unknown event)
the anchor is recorded as `UNRESOLVED` with the reason, the summary carries a warning, and
nothing substitutes for it: never `main`, never a historical known-good SHA. An unresolved
anchor does not fail a *passing* run, because nothing needs comparing; if the suite fails,
the classification is `INFRASTRUCTURE_FAILURE` and the gate fails.

## 3. Classification

`failure_classifier.py classify` outputs exactly one of six values and exits with the paired
code. Only `PASS` and `KNOWN_FLAKE` may be treated as success.

| Value | Exit | Meaning | Gate |
|---|---|---|---|
| `PASS` | 0 | The original full suite passed (exit status 0, at least one test executed, no runner errors). | pass |
| `KNOWN_FLAKE` | 10 | Every failing case exactly matches an **active, unexpired** registry entry **and** the same normalized failure reproduced on the current anchor. | pass, with an explicit warning |
| `CANDIDATE_REGRESSION` | 20 | The candidate failure reproduces in bounded isolated runs, the same normalized failure does not reproduce on an anchor that completed all its bounded runs, **and the samples discriminate candidate from anchor** (see below). | fail |
| `ANCHOR_REPRODUCED_FLAKE` | 21 | The same normalized failure reproduces on candidate **and** anchor and is not an active registry entry. Candidate source is not blamed. Report once; the gate fails pending a determinism decision/fix. It never silently passes. | fail |
| `INCONCLUSIVE` | 22 | More failing cases than the budget; the candidate failure disappeared in isolation; the candidate reproduced but the evidence cannot discriminate it from the anchor; signatures differ or cannot be compared; required logs are missing; too few anchor runs; the wall-time budget ran out. | fail |
| `INFRASTRUCTURE_FAILURE` | 23 | The anchor is unresolved, missing or equals the candidate; anchor or candidate checkout/build/install/start failed; results are unusable; the runner failed without a failing case. Never called a regression or a flake. | fail |

### What `CANDIDATE_REGRESSION` requires

CANDIDATE_REGRESSION is an evidence-backed discrimination,
not merely "candidate happened to fail and anchor happened not to fail
in a small sample."

The anchor not reproducing a failure in a small bounded sample is **not** proof that the
candidate caused it: a rare pre-existing failure simply may not have appeared in the anchor's
first runs. A case found this way is `CANDIDATE_REGRESSION` only when the samples actually
discriminate the two failure rates: the candidate's reproduction count against the anchor's
must have a one-sided exact (Fisher) probability of at most `SPECIFICITY_ALPHA` (0.01) under a
shared failure rate. With the first stage of 8 runs per ref that means at least 6 of 8
candidate reproductions against a clean anchor; 1 of 8 against 0 of 8 has probability 0.5 and
proves nothing.

If the first stage does not discriminate, the classifier asks (`discriminate`, exit 14) for the
**single bounded second stage**: `DISCRIMINATOR_RUNS_PER_REF` (16) additional isolated runs per
ref of only the affected case(s), inside the same 900 s budget, executed once and never looped.
Its results are pooled with the first stage's. Then:

* the candidate's difference from the anchor is now established: `CANDIDATE_REGRESSION`
  (with 24 runs per ref this needs at least 7 of 24 candidate reproductions against a clean anchor);
* the same normalized failure appears on the anchor: `ANCHOR_REPRODUCED_FLAKE`, or
  `KNOWN_FLAKE` when and only when an active registry entry also matches;
* it still cannot be established (or the budget expired first): `INCONCLUSIVE`.

INCONCLUSIVE is the required classification when bounded evidence
cannot distinguish a rare candidate regression from low-rate
pre-existing nondeterminism.
It is still a blocking result, so this does not weaken the gate; it only stops the system from
telling a developer "your candidate caused this" when the evidence says "we do not yet know".
Agents must not alter production source solely because an isolated candidate sample contains a failure while a small anchor sample happens to contain none.
Fix the test oracle if the failure is nondeterministic, report it, or gather more evidence
through the classifier; never edit candidate source on sampling noise.

The `classify` step never emits `CANDIDATE_REGRESSION` without this evidence, whatever the
workflow did, so the rule cannot be bypassed by skipping the second stage.

When several failing cases disagree the most severe wins:
`INFRASTRUCTURE_FAILURE` > `CANDIDATE_REGRESSION` > `INCONCLUSIVE` > `ANCHOR_REPRODUCED_FLAKE` >
`KNOWN_FLAKE`. The result JSON (`candidate_sha`, `anchor_sha`, `cases`, `known_flake_matches`,
`reason`, `candidate_runs`, `anchor_runs`) and a Markdown summary always name the exact
candidate and anchor SHAs.

### Normalized failure identity

Browser: relative spec path, the full test title, and a normalized failure signature (the
assertion header without its `Call log`, plus the failing source line). pytest: the exact
node id and a normalized failure signature. Normalization removes volatile noise only:
temporary and workspace paths, timestamps, workflow/run ids, commit SHAs, UUID literals,
localhost ports, ANSI codes, stack-frame paths and line/column numbers. It never removes a
different assertion, a different exception category, a different failing test, or a
different expected/actual semantic state.

## 4. Bounded diagnostic budget

Owned by the constants in the classifier and printed by `failure_classifier.py budget`
(the workflows read it, they do not restate it):

```text
MAX_CASES_TO_CLASSIFY                = 3
ISOLATED_RUNS_PER_REF                = 8
DISCRIMINATOR_RUNS_PER_REF           = 16  (one bounded second stage, only when needed)
SPECIFICITY_ALPHA                    = 0.01
MAX_CLASSIFICATION_WALL_TIME_SECONDS = 900   (15 minutes)
FULL_SUITE_AUTOMATIC_RERUNS          = 0
```

The full suite runs **once**. On failure only the failing case(s) are run in isolation, at
most `ISOLATED_RUNS_PER_REF` times per ref, first on the candidate and then on the anchor,
plus the one bounded discriminator stage described above when the classifier asks for it.
More than `MAX_CASES_TO_CLASSIFY` distinct failures is `INCONCLUSIVE` without any extra run.
Runs that would exceed the wall-time budget are skipped and the ref is marked exhausted
(`INCONCLUSIVE`).

The anchor is never mixed with the candidate:

* Browser Runtime builds the anchor's own Docker image from its own tree, starts it on
  separate ports (8082/8083), installs the anchor's own locked browser dependencies and runs
  the anchor's own specs.
* Tests checks the anchor out to an isolated directory, installs the anchor's own
  `requirements-dev.txt` into its own virtual environment and runs the anchor's own tests.

## 5. Known-flake registry

`.github/qualification/known_flakes.json` is **not a skip list**. It has zero active entries
after the determinism hardening that introduced it. Every entry has exactly these fields:
`id`, `domain` (`browser` or `pytest`), `case` (exact test identity), `failure_signature`
(the exact normalized signature, or its `sha256:` digest as printed in the classifier
output), `first_confirmed_sha` (40-char SHA), `tracking_reference`, `expires` and
`classifier_runs`. Rules:

* no wildcards and no whole-file exemptions (a browser case must be
  `<spec path> › <test title>`, a pytest case a full node id);
* an expiry is mandatory: `YYYY-MM-DD` (at most 180 days out) or `release:<version>`;
  expired or malformed entries are rejected and can never produce `KNOWN_FLAKE`;
* at most 10 entries; unknown keys are rejected;
* a match needs the same case **and** the same normalized signature — a different failure in
  the same test is not covered;
* the entry never waives anything on its own: the failure must also reproduce on the current
  anchor (`KNOWN_FLAKE` requires both);
* editing the registry is a normal Gate 9 change.

## 6. First-failure preservation

A later isolated success must never erase the original full-suite failure. Both workflows
keep, under `$RUNNER_TEMP/qualification/` and uploaded as `*-classification-<sha>`:

```text
candidate/full/         original full-run log, result file (results.json / results.xml),
                        exit_code.txt and (browser) test-results/traces copied before any other run
candidate/plan.json     the original failed-case list (plan.tsv is its execution input)
candidate/isolated/     bounded candidate runs
anchor/isolated/        bounded anchor runs (+ build/install logs, sentinel files)
*-classification.json   classifier result
*-classification.md     classifier summary (also appended to the step summary)
```

Isolated Playwright runs write to their own `--output` directories and never touch the
original `test-results`.

## 7. Test-oracle determinism

A test fix replaces a nondeterministic assumption with the real invariant. It does not
tolerate the flake and does not weaken the behavior under test. Rules of thumb:

* derive identities by ID (`candidate.id`), never by incidental ordering or UUID order;
* synchronize on the owner's real boundary (a request it issues, a render it performs, a
  control returning to idle), never on a transient count or a delay;
* park a request behind a test-owned gate to hold the system in a known state; do not sleep;
* an assertion that passes on the *staged* state and on the *saved* state proves nothing
  about completion: wait for the completion boundary before continuing.

Corrected under this policy (each reproduced on the pre-change tree, then stressed):

* the mirror-failover oracle derives the active and alternate candidate by ID;
* Details candidate refresh uses revision-stamped reads and a gated refresh;
* WS1-P2 staged Enable and the Settings directory-browser Confirm/Save test wait for the
  Apply control to return to idle before continuing (the save-completion re-render restores
  the saved form, so an edit made before it lands is lost);
* WS2-P1 single-row Remove parks the Downloads owner's next list read so no render can replace
  the opener mid-dialog;
* the Dashboard geometry tests park the Dashboard Recent owner's next read so the injected
  rows are measured in a DOM no render can replace;
* the group-candidates keyboard test (focus a list control, then press Enter) parks the
  owning surface's next read first, so the focused control cannot be replaced before the key
  arrives;
* the Downloads provider/source artwork geometry test and the Downloads pager geometry test
  park the Downloads owner's next read so the elements they measure cannot be replaced (a
  detached element reports zero rects, a hidden one a null bounding box) mid-probe.

## 8. Lifecycle / concurrency adversarial preflight (standing requirement)

Ownership, leases, claims, locks, retry, recovery, cleanup, reconciliation, scheduler state,
resource binding, selection / input-required, failover, concurrency and restart/crash
recovery are where correct-looking changes fail late. Gate 9 must not be the first place these cases are considered.
For *any* change involving them, the implementation session must, before production edits,
enumerate:

```text
Invariant
Canonical owner
Acquisition transition
Release/finalization transition
Cancellation behavior
Crash behavior
Restart behavior
Timeout/lease behavior
Stale-owner behavior
Concurrent-owner behavior
Batch/serial timing behavior
Fail-closed behavior
```

and explicitly test or challenge:

```text
live owner cannot be stolen
dead owner cannot block forever
stale owner cannot finalize
long operation crosses nominal lease/timeout
later batch item gets fresh timing
cancellation at await boundaries
callee swallows cancellation
restart during ownership
two workers race acquisition
missing required lifecycle state fails closed
```

Record the enumeration in the change's design notes or Gate 9 packet, with the test that
proves each challenge. The cleanup-lease correction is the model: a token lease with a
per-call heartbeat, dated per claim, where a lost or swallowed cancellation is ownership
loss. A change that skips this preflight is incomplete regardless of green CI.

## 9. Gate 9 expectations

* One full run per workflow. A failure is reported with its classification, its exact
  candidate and anchor SHAs, and the evidence artifacts — never with "reran until green".
* `ANCHOR_REPRODUCED_FLAKE`: stop once, report once, propose a determinism fix or (with
  approval) a registry entry.
* Any change to `known_flakes.json`, the classifier, or these workflows needs its own Gate 9
  tree. Product/runtime source is never changed by qualification work; if a flaky-test
  investigation proves a product defect, preserve the reproducer, report it, and fix it in a
  separate corrective workstream.

## 10. Findings carried by this workstream

**There are no accepted or registered known flakes.** `known_flakes.json` has zero active
entries, every qualification flake known when this workstream was frozen was corrected at its
oracle, and no waiver was granted for anything below. An empty registry means nothing has been
excused — not that nothing is outstanding.

### 10.1 Deferred determinism finding — Browser Runtime load sensitivity

One test-infrastructure finding **is** outstanding and is deliberately carried rather than
excused: a family of Browser Runtime specs is sensitive to runner load rather than to the
tree under test.

* **Observed cases.** `provider-status-generation.spec.js` (several cases),
  `stage10-provenance.spec.js`, `details-candidates.spec.js`, `ui-fix-ws2-p1.spec.js`.
* **Why it is not a candidate regression.** The same failures reproduce on untouched anchor
  trees. In the bounded classification that recorded this, the candidate failed 2 of 3 full
  runs and the anchor 1 of 3, on differing cases — Fisher two-sided p = 1.0, far above the
  classifier's threshold, so the recorded verdict was `INCONCLUSIVE`, the classification this
  policy requires when bounded evidence cannot separate a rare candidate regression from
  pre-existing nondeterminism. A later A/B pass that always ran the candidate first produced
  an apparent 3/7-vs-0/7 "regression" that disappeared once the order was alternated
  (final: candidate 5/12, anchor 2/12, differing cases).
* **What is known about the mechanism.** These specs run against an empty database, where the
  changed paths of the commits that surfaced them are no-ops. They pass 3/3 in isolation on a
  fresh container pair, they have not been observed on CI's 2-worker runner (a 16-core
  developer machine defaults to 8 workers and adds the load), and re-running specs against
  already-used containers makes it worse because some of them mutate real settings.
* **Status.** Open. It has **not** been fixed at its oracle and **must not** be registered,
  waived, retried or slept around. Until it is fixed, a failure in this family is classified
  by the ordinary rules like any other — it is never pre-excused, and it still fails the gate.

### 10.2 Resolved product finding

One **product** finding was carried here while it was deliberately left unfixed, and is now
**resolved** by the Canonical Release Remediation: *focus restoration across a background list
refresh.* The old confirmation dialog restored focus to the element that was focused when it
opened only if that element was still connected, so a Downloads refresh that landed while the
dialog was open (measured-capacity refresh, SSE `torrent_updated`, the 15 s polling fallback)
replaced the row controls and cancelling left focus on `<body>`. The dialog shell now has one
owner, `frontend/static/ui-settings-modal.js`, and restoration is resolved at its settlement
boundary: the initiating control if it is still focusable, otherwise its equivalent replacement
inside the nearest surviving ancestor, otherwise the first focusable control of the nearest
surviving region. Focus is never parked on `<body>` and a detached control is never focused.
The WS2-P1 tests no longer park the owner's list reads to avoid the defect; dedicated
tests let a refresh land while the dialog is open and assert the restored target.

## 11. Reproducing locally

Build the candidate image (`docker build` or `podman build`), start the open and
password-authenticated containers exactly as `browser-runtime.yml` does (choose free ports and
set `DP_BASE_URL` / `DP_AUTH_BASE_URL`), then run the specs from `frontend/browser`. For the
classifier: `python .github/qualification/failure_classifier.py --help`.
