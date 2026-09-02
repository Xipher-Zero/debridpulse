# v1.0.12 regression and replacement map

The frozen production starting point is `61d5eec345c473532c46508f76c69a3d6b36747a`
(v1.0.11.1). Roadmap Item 4 begins from the qualified Item 3 baseline
`69dc939d0f5f9bcd578851a071935b0dbc3b9db0` and adds only the generic
browser-side `Authentication Required` interaction plus its permanent coverage
and documentation.

The earlier v1.0.12 runtime replacement removes the manager inheritance chain,
captured-method coordinators and provider/executor forwarding gateways. Tests
whose fixtures construct those deleted owners were replaced at their public
behavior boundaries. `LEGACY_TEST_MIGRATION.json` preserves the cutover census:
217 old test methods across 37 files, plus 14 obsolete structure checks. Four
native client adoption checks were also superseded when that path was physically
removed. This remains a traceability census, not a claim of one-to-one assertion
equivalence.

## Behavioral evidence

All paths below are under `backend/tests/` unless explicitly stated otherwise.

| Preserved contract | Executable replacement or retained coverage |
| --- | --- |
| Identity before provider contact; actual submission and scheduler; core usable without production integrations | `test_application_runtime.py`, `test_universal_lifecycle.py`, `test_universal_boundaries.py` |
| Magnet/torrent requests, direct-link normalization, native provider response/error mapping | `test_alldebrid_provider_contract.py`, `test_native_integration_compatibility.py`, `test_direct_links.py`, `test_torrent_hash.py` |
| Cached readiness and provider preparation remain distinct from local completion; source outcomes excluded from physical progress | `test_universal_lifecycle.py`, `test_application_runtime.py`, `test_download_logic.py`, `test_universal_migration.py` |
| Durable pause before intake; individual/global resume; capacity; pause during start acknowledgement | `test_universal_lifecycle.py`, `test_universal_parity.py`, `test_application_runtime.py` |
| Path-stable retry, sibling preservation, partial retry, reacquisition, missing refreshed members, retry budgets and deadlines including zero | `test_universal_lifecycle.py`, `test_universal_parity.py`, `test_universal_hardening.py` |
| Restart and ambiguous acknowledgements; no duplicate dispatch; unknown occupies capacity | `test_universal_lifecycle.py`, `test_aria2_executor_contract.py` |
| Mirror source identity, same-host separation, metadata thresholds, sample evidence, alternatives, partial retirement, no local-error cycling | `test_universal_lifecycle.py`, `test_aria2_executor_contract.py` |
| Deleted/completed path reuse; active/uncertain reservations; exact-size/no-follow/sidecar proof; missing payload blocks completion | `test_transfer_integrity.py`, `test_universal_hardening.py`, `test_universal_lifecycle.py` |
| Delete racing provider creation or executor start; explicit versus automatic remote authority; incomplete inventory does not imply deletion | `test_universal_lifecycle.py`, `test_universal_parity.py` |
| Owned daemon isolation, changed connection binding, foreign native ID/path refusal, result retention, bounded observations, restart safety | `test_aria2_executor_contract.py`, `test_aria2_administration.py`, `test_aria2_result_retention.py` |
| SSRF, DNS rebinding, redirects, TLS identity/SNI, metadata disabled, shared-daemon boundaries | `test_v1111_aria2_security_boundary.py`, `test_security_hardening_v106.py`, `test_v106_external_aria2_ownership.py`, `test_aria2_executor_contract.py` |
| Separate post-processing outcomes; real ZIP extraction, retention, invalid archive preservation, interrupted claim recovery | `test_universal_hardening.py`, `test_universal_lifecycle.py`, `test_universal_parity.py`, `test_extractor.py`, `test_extraction_lifecycle.py` |
| Namespaced settings and legacy translation, credential preservation/clear, ownership-sensitive changes, admission drain | `test_integration_configuration.py`, `test_universal_hardening.py`, `test_webhook_settings_integration.py`, retained authentication/settings suites |
| Neutral integration runtime-state persistence: opaque payload fidelity, provider/key isolation, schema markers, timestamps/staleness, restart recovery, provider-owned validation, last-known-good retention, CAS atomicity, disable/re-enable retention, transactional migration failure, architecture boundaries | `test_provider_runtime_state.py`, `fake_runtime_state_provider.py`, `docs/architecture/PROVIDER_RUNTIME_STATE.md` |
| Neutral `INPUT_REQUIRED`/`AUTH_REQUIRED`: same transfer identity, durable non-secret challenge generation, provider/executor continuation, optional private-key passphrase, no retry-budget consumption, pause/cancel/delete/capacity behavior, restart, stale/duplicate rejection, sibling execution observation, API bounds, backup/SQLite/log leakage sentinels | `test_input_required_lifecycle.py`, `test_input_required_api.py`, `test_input_required_acceptance.py`, `test_input_required_architecture.py`, `docs/architecture/INPUT_REQUIRED_LIFECYCLE.md` |
| Generic browser `Authentication Required` interaction: challenge-driven password/key rendering, native key picker, local key rejection, textual green selected state, optional passphrase, key replacement, in-flight busy state, rejected-auth field preservation, challenge regeneration, queued/paused success closure, canonical cancel/Escape, deterministic multi-transfer queue, reload-without-secrets, browser persistence/URL/console leakage sentinels, accessibility and mobile geometry | `frontend/browser/input-required.spec.js`, `frontend/browser/auth-required-modal.spec.js`, `frontend/static/ui-auth-required.js`, `frontend/static/ui-auth-required.css`, `test_input_required_architecture.py` |
| Upgrade retains parent/artifact identity, pause, source outcomes and ownership; verified backup and rollback | `test_universal_migration.py` |
| Real HTTP database wipe, canonical table backup, durable pause, uncertainty refusal; exclusive gates | `test_application_runtime.py`, `test_v106_corrective_regressions.py`, `test_v106_final_audit.py` |
| Common error domains and safe unknown/security policy; sanitized diagnostics; no native error parsing in UI | `test_universal_contracts.py`, `test_universal_boundaries.py`, `test_ui_error_semantics_contract.py`, `frontend/browser/app.spec.js` |
| Login, sessions, OIDC, token boundaries, settings layouts, dashboard/statistics/help, release promotion safeguards | Retained `test_auth_*`, `test_settings_*`, `test_ui_*`, `test_release_promotion_contract.py` and permanent browser suite |

## Intentional corrections and Item 4 invariants

- An unknown provider/executor result is not absence, success or automatic retry authority.
- Native job IDs, URLs and matching paths do not establish mutation ownership.
- Provider readiness, historical completion and a stopped executor result do not prove current local possession.
- Failed source alternatives do not inflate the physical-artifact denominator; failed physical work still fails.
- Post-processing failure is reported separately from successful byte delivery.
- Native client retries no longer hide attempts from core policy.
- Reconfiguration cannot abandon referenced daemon/path ownership, and it drains admitted operations before replacing integrations.
- Provider runtime state is operational persistence, not user configuration or transfer history. Disablement does not imply deletion, and the neutral store never interprets provider payload fields.
- Runtime-state replacement is atomic; provider validation happens before replacement, and optional generation compare-and-swap prevents stale refreshes from overwriting newer known-good state.
- `INPUT_REQUIRED` is a nonterminal transfer condition rather than an error or retry. Authentication waits preserve top-level transfer identity and do not consume the automatic resolution retry budget merely because user input is absent.
- Challenge metadata, integration runtime state and submitted authentication bundles are separate categories. Only the non-secret challenge descriptor is durable; submitted username/password/private-key/passphrase values are transient and never stored in the runtime-state table.
- `username_private_key` has an optional `passphrase` field. Passphrase is not a separate authentication method and is not universally required.
- The browser invokes authentication only from canonical `INPUT_REQUIRED / AUTH_REQUIRED` presentation. It does not parse URL schemes, provider/executor identity, filenames, failure strings, or native error codes to choose authentication behavior.
- `Select Keyfile` exists only when `username_private_key` is advertised. Locally accepted key material produces a textual green **Key supplied** state; that state does not claim remote authentication success.
- `Continue` leaves the modal open and busy. A successful submission response alone never closes it; closure follows backend challenge resolution and the same transfer leaving `INPUT_REQUIRED`.
- Active downloading is not an authentication-success condition. The modal closes if authentication resolves while the transfer remains queued or paused.
- Remote rejection preserves current-session values and adopts a regenerated challenge before retry. A stale challenge identity is never reused.
- Cancel, Escape, and overlay dismissal cannot strand a hidden pending challenge; permitted dismissal routes through canonical transfer cancellation.
- Browser reload preserves only backend challenge metadata. Username/password/private-key/passphrase values are intentionally lost and must be re-entered.
- The source version remains 1.0.12 while production installation examples remain on the published 1.0.11.1 image until explicit promotion.

## Qualification procedure and limits

Freeze all source, test, documentation and workflow changes before selecting a
candidate SHA. Run Tests (full pytest, undefined-name checks, compile, JavaScript
syntax, pip-audit and Bandit), Browser Runtime (including npm audit), CodeQL
(Python, JavaScript/TypeScript and Actions), Container Security (health, non-root,
writable paths, vulnerability report and fixable High/Critical gate), and Fork
Image (build/smoke, OCI identity and immutable multi-architecture publication)
against that exact SHA. A tracked change creates a new candidate and resets every
gate.

Roadmap Item 2 additionally requires the real SQLite runtime-state tests to prove
fresh schema creation, upgrade preservation, idempotency, transactional rollback,
opaque restart persistence, provider/key isolation, provider-owned schema and
payload interpretation, stale metadata, last-known-good retention, concurrent
replacement behavior, and disable/re-enable retention. The neutral implementation
must remain absent from transfer-core parsing and concrete provider packages.

Roadmap Item 3 adds exact proofs that `INPUT_REQUIRED` remains nonterminal; the
same logical transfer resumes after fresh input; provider and executor challenge
ownership remain independent; challenge generations reject stale responses; waits
do not burn retry budget or ordinary execution capacity; pause/global pause,
cancel and delete retain their authority; current external sibling work remains
observable; and submitted authentication values never reach durable SQLite rows,
runtime state, backup payloads, application/API responses or captured logs.

Roadmap Item 4 extends Browser Runtime with direct proofs for password-only and
password+key challenge rendering; local key validation; key replacement; optional
passphrase; successful and rejected authentication; challenge regeneration;
closure while queued/paused; canonical Cancel/Escape; multiple simultaneous
waiting transfers; page reload; browser secret-leakage sentinels; focus trapping;
light-theme hooks; and narrow/mobile geometry. The Item 3 neutral transfer-row
presentation remains intact while the same canonical `AUTH_REQUIRED` metadata now
also invokes the generic modal.

The five real aria2 integration tests require `aria2c` and OpenSSL, installed by
CI. A local environment lacking aria2 may skip those tests; such a local result
does not substitute for the full CI gate. Deterministic provider responses prove
the integration contract, not a live account transaction. Additional production
providers and executors remain future work; fake implementations are test
fixtures. There remains no production consumer of provider runtime-state
persistence introduced by Item 2.

Still deferred after Item 4: saved credential discovery/persistence, SSH-agent or
filesystem credential discovery, production HTTP(S)/FTP/SCP/SFTP/SSH/rsync
integrations, classifier/applicability, AllDebrid dynamic host support, and later
routing/provenance stages. No protocol-specific authentication UI is authorized.

The consolidated final report records the actual final SHA, run/job evidence,
totals, image digest and residual findings outside the qualified source tree, so
adding the evidence does not silently alter the candidate being qualified.
