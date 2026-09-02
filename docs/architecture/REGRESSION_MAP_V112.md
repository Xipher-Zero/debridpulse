# v1.0.12 regression and replacement map

The frozen starting point is `61d5eec345c473532c46508f76c69a3d6b36747a`
(v1.0.11.1). The runtime replacement removes the manager inheritance chain,
captured-method coordinators and provider/executor forwarding gateways. Tests
whose fixtures construct those deleted owners were replaced at their public
behavior boundaries. Merely keeping their private method names would preserve
the architecture this sprint removes.

`LEGACY_TEST_MIGRATION.json` preserves the cutover census: 217 old test methods
across 37 files, plus 14 obsolete structure checks. Four native client adoption
checks were also superseded when that path was physically removed. This is a
traceability census, not a claim of one-to-one assertion equivalence or a complete
count of every later test edit. Some old tests independently implemented the
algorithm they purported to test; replacements call the actual engine.

## Behavioral evidence

All paths below are under `backend/tests/` unless explicitly stated otherwise.

| Preserved contract | Executable replacement or retained coverage |
| --- | --- |
| Identity before provider contact; actual HTTP submission and scheduler; core usable without production integrations | `test_application_runtime.py`, `test_universal_lifecycle.py`, `test_universal_boundaries.py` |
| Magnet/torrent requests, direct-link normalization, native provider response/error mapping | `test_alldebrid_provider_contract.py`, `test_native_integration_compatibility.py`, `test_direct_links.py`, `test_torrent_hash.py` |
| Cached readiness and provider preparation remain distinct from local completion; source outcomes excluded from physical progress | `test_universal_lifecycle.py`, `test_application_runtime.py`, `test_download_logic.py`, `test_universal_migration.py` |
| Durable pause before intake; individual/global resume; capacity; pause during start acknowledgement | `test_universal_lifecycle.py`, `test_universal_parity.py`, `test_application_runtime.py` |
| Path-stable retry, sibling preservation, partial retry, reacquisition, missing refreshed members, retry budgets and deadlines including zero | `test_universal_lifecycle.py`, `test_universal_parity.py`, `test_universal_hardening.py` |
| Restart and ambiguous acknowledgements; no duplicate dispatch; unknown occupies capacity | `test_universal_lifecycle.py`, `test_aria2_executor_contract.py` |
| Mirror source identity, same-host separation, metadata thresholds, sample evidence, three/five alternatives, partial retirement, no local-error cycling | `test_universal_lifecycle.py`, `test_aria2_executor_contract.py` |
| Deleted and completed path reuse; active/uncertain path reservations; exact-size/no-follow/sidecar proof; missing payload blocks completion | `test_transfer_integrity.py`, `test_universal_hardening.py`, `test_universal_lifecycle.py` |
| Delete racing provider creation or executor start; explicit versus automatic remote authority; incomplete inventory does not imply deletion | `test_universal_lifecycle.py`, `test_universal_parity.py` |
| Owned daemon isolation, changed connection binding, foreign native ID/path refusal, result retention, bounded observations, restart safety | `test_aria2_executor_contract.py`, `test_aria2_administration.py`, `test_aria2_result_retention.py` |
| SSRF, DNS rebinding, redirects, TLS identity/SNI, metadata disabled, shared-daemon boundaries | `test_v1111_aria2_security_boundary.py`, `test_security_hardening_v106.py`, `test_v106_external_aria2_ownership.py`, `test_aria2_executor_contract.py` |
| Separate post-processing outcomes; real ZIP extraction, retention, invalid archive preservation, no-archive skip, interrupted claim recovery | `test_universal_hardening.py`, `test_universal_lifecycle.py`, `test_universal_parity.py`, `test_extractor.py`, `test_extraction_lifecycle.py` |
| Namespaced settings and legacy translation, credential preservation/clear, ownership-sensitive changes, admission drain | `test_integration_configuration.py`, `test_universal_hardening.py`, `test_webhook_settings_integration.py`, retained authentication/settings suites |
| Neutral integration runtime-state persistence: opaque payload fidelity, provider/key isolation, schema markers, timestamps/staleness, restart recovery, provider-owned validation, last-known-good retention, compare-and-swap atomicity, disable/re-enable retention, transactional migration failure, and architecture boundaries | `test_provider_runtime_state.py`, `fake_runtime_state_provider.py`, `docs/architecture/PROVIDER_RUNTIME_STATE.md` |
| Upgrade retains parent/artifact identity, pause, source outcomes and ownership; verified backup and rollback | `test_universal_migration.py` |
| Real HTTP database wipe, canonical table backup, durable pause, uncertainty refusal; exclusive gates | `test_application_runtime.py`, `test_v106_corrective_regressions.py`, `test_v106_final_audit.py` |
| Common error domains and safe unknown/security policy; sanitized diagnostics; no native error parsing in UI | `test_universal_contracts.py`, `test_universal_boundaries.py`, `test_ui_error_semantics_contract.py`, `frontend/browser/app.spec.js` |
| Login, sessions, OIDC, token boundaries, settings layouts, dashboard/statistics/help, release promotion safeguards | Retained `test_auth_*`, `test_settings_*`, `test_ui_*`, `test_release_promotion_contract.py` and permanent browser suite |

## Intentional corrections

- An unknown provider/executor result is not absence, success or automatic retry authority.
- Native job IDs, URLs and matching paths do not establish mutation ownership.
- Provider readiness, historical completion and a stopped executor result do not prove current local possession.
- Failed source alternatives do not inflate the physical-artifact denominator; failed physical work still fails.
- Post-processing failure is reported separately from successful byte delivery.
- Native client retries no longer hide attempts from core policy.
- Reconfiguration cannot abandon referenced daemon/path ownership, and it drains admitted operations before replacing integrations.
- Provider runtime state is operational persistence, not user configuration or transfer history. Disablement does not imply deletion, and the neutral store never interprets provider payload fields.
- Runtime-state replacement is atomic; provider validation happens before replacement, and optional generation compare-and-swap prevents stale refreshes from overwriting newer known-good state.
- The source version is 1.0.12 while production installation examples remain on the published 1.0.11.1 image until explicit promotion.

## Qualification procedure and limits

Freeze all source, test, documentation and workflow changes before selecting a
candidate SHA. Run Tests (full pytest, undefined-name checks, compile, JavaScript
syntax, pip-audit and Bandit), Browser Runtime (including npm audit), CodeQL
(Python, JavaScript and Actions), Container Security (health, non-root, writable
paths, vulnerability report and fixable High/Critical gate), and Fork Image
(build/smoke, OCI identity and immutable multi-architecture publication) against
that exact SHA. A fix creates a new candidate and resets every gate.

Roadmap Item 2 additionally requires the real SQLite runtime-state tests to prove
fresh schema creation, upgrade preservation, idempotency, transactional rollback,
opaque restart persistence, provider/key isolation, provider-owned schema and
payload interpretation, stale metadata, last-known-good retention, concurrent
replacement behavior, and disable/re-enable retention. The neutral implementation
must remain absent from transfer-core parsing and concrete provider packages.

The five real aria2 integration tests require `aria2c` and OpenSSL, installed by CI.
A local environment lacking aria2 may skip those tests; such a local result does
not substitute for the full CI gate. Deterministic provider responses prove the
integration contract, not a live account transaction. Additional production
providers and executors remain future work; fake implementations are test fixtures.
There is intentionally no production consumer of provider runtime-state persistence
in Roadmap Item 2. AllDebrid dynamic host-support, HTTP(S), classifier/applicability,
authentication-flow/UI, provenance and later routing remain deferred.

The consolidated final report records the actual final SHA, run links, totals,
image digest and residual findings outside the qualified source tree, so adding
the evidence does not silently alter the candidate being qualified.
