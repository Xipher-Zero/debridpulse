# DebridPulse — Project Notes for Claude Code

Reference notes for future sessions. Verify against the live tree before relying on
specifics — line numbers and counts drift. Branch of record for active work: **`1.0.12`**
(`main` lags behind it). Last substantive update: 2026-09-19.

---

## 1. What this is

GPL-2.0-or-later fork of [`kroeberd/alldebrid-client`](https://github.com/kroeberd/alldebrid-client)
(originally MIT). Self-hosted transfer manager. Flow:

```
direct link / magnet / .torrent
  → provider (AllDebrid  or  General HTTP(S))
  → executor (aria2)
  → optional post-processor (archive extraction)
```

Identity, lifecycle, retry, ownership, recovery, consolidation, auth-continuation and
post-processing belong to the **Universal Transfer Core** (`backend/transfers/`), not to
any provider or executor. Providers resolve sources into canonical candidates and expose
neutral applicability facts; executors execute; the classifier is provider-neutral.

**Executor contract (1.0.13 Universal Executor Leveling).** Core speaks one generalized
contract (`transfers/contracts.py` `Executor`: `claim`, `footprint`, `prepare`, `start`,
`observe_many`, `cancel`, `health` + declared `ExecutorCapabilities`). Executor selection
is the one claim router `IntegrationRegistry.claimants(ExecutionSubject)` /
`executor_for_subject` (viability, evidence sampling, input continuation, dispatch,
recovery) — there is no scheme routing and `IntegrationDescriptor` has no `schemes`.
`ExecutionHandle` = `(executor_id, attempt_id, correlation, native)`; native binds once via
`bind_execution_handle`; `_engine_base._accept_observation` is the one acceptance point.
`ExecutionState.RUNNING` (not `transferring`) + `ExecutionActivity`; controls come from the
current observation; `cancel` returns observed truth. Global bandwidth:
`transfers/runtime_coordination.py`; global concurrency: core admission only (aria2 keeps a
fixed native queue width). Materialization verify/cleanup: `transfers/filesystem.py`
(`verify_materialization`, `retire_materialization`). Managed executors reach the
application via `integrations/definition.py` `ManagedIntegration`/`AdministeredIntegration`
— `application/composition.py` names no executor. Non-aria2 proofs: `tests/executor_fakes.py`.

`VERSION` = `1.0.12` (development; not a released baseline — published images still tag
`v1.0.11.1` in `docker-compose.yml`).

---

## 2. Repository layout

| Path | Contents |
|---|---|
| `backend/` | Python FastAPI/ASGI app. ~40k LOC, **237** `test_*.py` files. Flat package imports; `main.py` is the ASGI entry (`uvicorn main:app`). |
| `frontend/static/` | The served app: `index.html` shell + `app.js` (~3.6k LOC) + ~30 `*.js` + ~52 `*.css`. **No bundler, no `npm install` for the app itself.** |
| `frontend/browser/` | Separate Playwright contract-test suite (**27** `*.spec.js`), own `package.json` (`@playwright/test@1.62.1` only). |
| `frontend/host-icons.parts/` | 44 base64 chunks of a zipped host-logo archive, reassembled + SHA-256-verified at image build time. |
| `index.html` (repo root) | **Standalone marketing/landing page. NOT the app.** Not copied into the image. The real shell is `frontend/static/index.html`. |
| `docs/` | Architecture (`docs/architecture/*.md`, e.g. `UNIVERSAL_TRANSFER_CORE.md`, `MULTI_PROVIDER_HTTP_SLICE.md`) and UI docs (`docs/UI_FRONTEND_ARCHITECTURE.md`, `UI_DESIGN_TOKENS.md`, `UI_ICON_SYSTEM.md`). |
| `Dockerfile`, `entrypoint.sh`, `docker-compose.yml` | Packaging (see §5). |
| `release.py`, `CHANGELOG.md` (~257 KB), `licenses/` + `LICENSES/` + `NOTICE` | Release + dependency-license bookkeeping (enforced in CI). |
| `.github/workflows/` | 8 workflows (see §7). |
| `.github/qualification/` | The one failure classifier (`failure_classifier.py`) and the one known-flake registry (`known_flakes.json`). Policy: `docs/QUALIFICATION_DETERMINISM.md` (see §6a). |

### Backend packages (`backend/`)

| Package | ~LOC | Role |
|---|---|---|
| `transfers/` | ~13k | **Universal Transfer Core.** Identity, lifecycle, retry, ownership, recovery, consolidation/convergence engine (`convergence_engine.py`, `engine.py`), repositories (`repository.py`, `recovery_repository.py`, `presentation_repository.py`), `policy.py`, `models.py`, `contracts.py`, `registry.py` (capability routing), `applicability.py`. |
| `api/` | ~4.3k | FastAPI routers: `routes.py` (main), `auth_routes.py`, `auth_config_routes.py`, `operational_downloads.py`, `settings_validation_routes.py`, `storage_health_routes.py`; `serializers.py`. |
| `auth/` | ~3k | No-auth mode, username/password (argon2) + HTTP Basic, provider-neutral OIDC (`authlib`/`joserfc`), bearer API tokens, CSRF, sessions, lockout/throttle, transition state machine. |
| `services/` | ~3k | `backup`, `db_maintenance`, `downloader_egress_guard`, `duplicates`, `event_bus`, `extraction_safety`, `maintenance_gate`, `network_safety`, `notification_service`/`notifications` (Discord), `page_cache`, `stats`. |
| `providers/` | ~1.6k | `alldebrid/` and `general_http/`. Each: `client`, `definition`, `provider`, `migration`, `runtime_state`, `translation`. |
| `executors/` | ~1.6k | `aria2/`: `client`, `executor`, `admin`, `runtime`, `migration`, `presentation`, `translation`. |
| `application/` | ~0.9k | **`composition.py` — the one place concrete plugins are wired** (`compose()` → `configure()`). Also `service.py`, `dependencies.py`, `observability.py`, `consolidation_events.py`, `manual_candidate_failover.py`. |
| `postprocessors/` | ~1k | `archive/`: optional auto-extraction (7zip + rar, zstd), security-hardened `extractor.py`. |
| `core/` | ~1.2k | `config.py` + `config_validator.py`, `logging_utils.py`, `scheduler.py`, `branding.py`, `version.py` (`read_version()`), `secure_files.py`, `performance.py`, `presentation_safety.py`. |
| `db/` | ~0.9k | SQLite via `aiosqlite` (WAL). `database.py` + `migrations/` (`v112.py`). |
| `integrations/` | ~0.5k | `catalog.py`, `configuration.py` (`normalize_settings()`), `definition.py`, `runtime_state.py`. |

### Runtime dependencies

`backend/requirements.in` → compiled `requirements.txt`. Key pins: `fastapi`, `uvicorn`,
`uvloop`, `httptools`, `aiohttp`, `aiosqlite`, `pydantic` v2, `bencode2`, `google-re2`,
`python-multipart`, `prometheus-client`, `argon2-cffi`, `authlib`, `httpx`, `joserfc`.
Dev (`requirements-dev.in`): `pytest`, `pytest-asyncio`, `pytest-cov`.
Lint/security qualification tooling (`requirements-qa.in` → compiled
`requirements-qa.txt`, full transitive closure, constrained to `requirements-dev.txt`):
`ruff`, `bandit`, `pip-audit` — pinned as of DEP-001 (2026-09-09); `tests.yml`
installs this set instead of `pip install ruff` / `pip install pip-audit bandit`.
All three `requirements*.in` files are compiled with `pip-compile … --strip-extras`.

---

## 3. Frontend and how it relates to the backend at runtime

**Single-origin, no build step.** FastAPI serves JSON/SSE under `/api/*` and mounts
`frontend/static` at `/` via `StaticFiles(html=True)` (`backend/main.py`, `app.mount("/", …)`).

- Static-dir search (`main.py`): `STATIC_DIR` env → `<repo>/frontend/static` →
  `/app/frontend/static` → `/app/static`. The app **refuses to start** if no candidate
  has `index.html`.
- `request_id_middleware` forces `Cache-Control: no-cache, must-revalidate` on `/`,
  `*.html`, `*.js`, `*.css` so a new container can't run a stale JS/CSS mix.
- Comms: REST + Server-Sent Events ("the pulse") + Prometheus metrics at `/api/metrics`.

### Middleware stack (`backend/main.py`)

Registered (in source order): `application_mutation_admission_middleware` (serializes
mutations, rejects DB-backed work when storage unsafe / maintenance active) → conditional
`CORSMiddleware` (only if `CORS_ORIGINS` set) → `authentication_boundary_middleware`
(`enforce_authentication`) → `RequestBodyLimitMiddleware` (`add_middleware`, pure-ASGI,
per-path ceilings) → `general_web_security_middleware` (`enforce_general_web_security`) →
`request_id_middleware` (X-Request-ID + baseline security headers + the cache rule).
Starlette runs the last-registered outermost, so `request_id` is outermost and
`mutation_admission` innermost. Custom exception handlers normalize `TransferError`,
`sqlite3.OperationalError`, `RequestValidationError` (strips submitted secrets),
`PermissionError`, and the maintenance-active exceptions.

Routers included (`main.py`, all `prefix="/api"` except the auth routers): `auth_config_router`,
`auth_router`, `settings_validation_router`, `storage_health_router`,
`operational_downloads_router`, then the generic `router` from `api/routes.py`.
`operational_downloads` is the sole declaring owner of `GET /api/torrents` and
`GET /api/events` (`list_operational_torrents` / `list_activity_events`); `api/routes.py`
declares neither, and `main.py` no longer performs any startup-time `router.routes[:]`
surgery (ARCH-001, 2026-09-09). Include order is not load-bearing. Regression coverage:
`backend/tests/test_canonical_http_route_ownership.py`. The one remaining intentional
same-path dual registration is `GET /auth/oidc/callback` (pending-aware callback tried
before the session-issuing one) — ordered registration, not route-list surgery.

### Frontend architecture rules (`docs/UI_FRONTEND_ARCHITECTURE.md`)

- **One bounded structural/render owner per visible behavior. No broad post-render
  correction runtimes, correction-named stylesheets, or compatibility globals that
  replace unrelated owners.** Enforced by static-analysis tests
  (`backend/tests/test_ui_presentation_ownership_contract.py`,
  `test_post_audit_architecture_documentation.py`, `test_settings_architecture_ui.py`,
  `test_ui_runtime_architecture_contract.py`) and the Browser Runtime workflow.
- `frontend/static/index.html` owns the shell + six nav surfaces: Dashboard, Downloads,
  Activity Log, Statistics, Settings, Help & License.
- CSS: `style.css` is the baseline; `style-v11.css` is the canonical `@import` graph;
  per-surface `ui-*.css`; `design-tokens.css`. Assets cache-busted with `?v=N`.
- **Bounded presentation owners are lazy-loaded by `ui-provider-status.js`'s
  `bootPresentationOwners()`**, not by `<script>` tags in `index.html`. The
  `PRESENTATION_OWNERS` list (as of 2026-09-09):
  `ui-toast-contract.js` (`DPToastContract`), `ui-processing-presentation.js`
  (`DPProcessingPresentation`), `ui-dashboard-transfer-presentation.js`
  (`DPDashboardTransferPresentation`), `ui-downloads-presentation.js`
  (`DPDownloadsPresentation`), `ui-activity-log-runtime.js` (`DPActivityLog`),
  `ui-settings-archive-passwords.js` (`DPArchivePasswords`).
- `ui-settings-page.js` is the **sole** Settings markup owner (every tab/panel/card/icon/ARIA
  attribute, rendered once; also persistence). No satellite script may rewrite it after render
  — the former `ui-settings-downloads-completion.js`, `-notifications.js`, `-maintenance-wipe.js`,
  `-card-icons.js`, `ui-provider-cards.js` were folded in and deleted. Bounded feature owners
  that remain: `ui-settings-directory-picker.js` (folder-browse modal), `ui-settings-aria2-live.js`
  (aria2 engine queue; behavior only), `ui-settings-archive-passwords.js`.
- Global processing pause is **operational state**: the durable application state
  (`TransferRepository.globally_paused()`) is the only authority. `AppSettings` has no `paused`;
  a pre-1.0.12 `config.json` value is a one-shot migration input (`legacy_paused_input()` →
  `db/migrations/v112.py`). `/stats`, pause/resume results and SSE project from it; the frontend
  keeps a non-persisted `processingPaused` (never in `settingsData`).
- `NormalizedError.recovery` / `operator_action_required` are **output-only** projections stamped by
  `policy.compatibility_error` from `policy.recovery_action(error)` (a pure function of canonical
  facts). Nothing reads them back; emitters must not pass `recovery=`.
  Proof: `backend/tests/test_recovery_projection_boundary.py`.
- **Multi-source candidate chip** (2026-09-09): `/api/torrents` list rows carry
  `candidate_source_max` — the largest per-artifact distinct canonical candidate count
  across a transfer's eligible artifacts, computed inside the one bounded projection SQL
  in `api/operational_downloads.py` (a `canonical_candidate_bindings` CTE; never a
  per-row query, never `repository.presentation(..., details=True)`). It is a **max**,
  not a sum, because artifacts of one transfer may carry different candidate-set sizes;
  tooltip wording is "up to N". Chip renders only when `> 1`. Shared visual contract:
  `.dp-candidate-chip` material lives in `ui-transfer-contract.css`; the passive
  Dashboard/Downloads chip (`role="img"`, non-interactive) is built by
  `DPDashboardTransferPresentation.candidateChipMarkup()` and reused by
  `ui-downloads-presentation.js`; the Details disclosure keeps its interactive
  `<button>` (`ui-detail-candidates.js`) but shares that family and drops the old
  circular count badge. The Lucide `network` glyph has one geometry owner — the
  `LUCIDE` set in `operator-title.js` — rendered via `window.DPIcons.svg('network')`.
  `ui-detail-candidates.css` is now loaded through the `style-v11.css` `@import` graph
  (it was previously runtime-injected by `ui-detail-candidates.js`).

---

## 4. Host-icon mapping (single source of truth)

`frontend/static/ui-dashboard-transfer-presentation.js`:

- `HOST_ASSETS` — frozen array of `['<registrable-domain>', '<asset-file>']` pairs.
- `hostAsset(host)` → `/icons/hosts/<file>` or `''`. Match is **exact registrable
  domain** or subdomain via `normalized === domain || normalized.endsWith('.' + domain)`.
  **No stem/fuzzy matching**: visually similar but unrelated services need their own
  rows even when they share an asset (`mega.nz` and `megaup.net` both → `mega.svg`,
  added separately; `megaup.net` row landed in commit `410d8c0c`, 2026-09-08, with a
  regression assertion in `frontend/browser/ui-regression-restoration.spec.js`).
- `sourceIconMarkup(identity)` / `sourceSlot(identity)` — `identity.kind` is
  `'host' | 'magnet' | 'torrent_file' | 'link'`; only `'host'` uses `hostAsset`.
- The Downloads page reuses this via
  `window.DPDashboardTransferPresentation.sourceIconMarkup()` — **do not duplicate icon
  logic anywhere else.**
- `identity` comes from the backend as `current_source_identity`
  (`transfers/presentation_repository.py` `public_source_identity()` / `_candidate_source()`,
  which validate the host via `core/presentation_safety.py` `safe_public_host()`;
  `api/operational_downloads.py` `_bounded_source_identity()`). The
  provider sets `SourceIdentity("host", <hostname of the original request URL>)`
  (`providers/alldebrid/provider.py`, `providers/general_http/provider.py`).
- Icon PNG/SVG assets ship via `frontend/host-icons.parts/` → reassembled ZIP in the
  Dockerfile; adding an asset means regenerating that archive and its `expected_sha256`.
  Reusing an existing asset (e.g. `mega.svg`) needs no archive change.

---

## 5. Dockerfile

**Single-stage** (not multistage) — `FROM python:3.12.14-slim-trixie` — organized as
ordered layers for cache efficiency and supply-chain rigor:

1. **OS packages** — `apt-get update && apt-get upgrade -y` *first* (deliberate: the
   runtime CVE gate must see patched Trixie packages, not the base snapshot), enable the
   `non-free` component, install `aria2 curl gosu zstd 7zip 7zip-rar`. `dpkg`
   `path-include` rules (`zz-` prefixed) re-add the `7zip-rar` license/notice files that
   the slim base strips.
2. **Python deps** — `COPY backend/requirements.txt` then `pip install` (own cached layer).
3. **App code** — `COPY backend/ /app/` + `COPY frontend/ /app/frontend/`, then an inline
   `python` heredoc reassembles `frontend/host-icons.parts/*` → base64 → ZIP, verifies
   `expected_sha256` (`2bfb7cadf647f6d4093ce4ad7d13159e137a190925a1b840f8a50a7f579be90f`)
   and an exact expected file-name set, extracts to `frontend/static/icons/hosts/`,
   deletes the parts dir.
4. **License/metadata files** — `CHANGELOG.md`, `VERSION`, `LICENSE`, `NOTICE`,
   `SOURCE_OFFER.md`, `LICENSES/`, `licenses/`, `docs/DEPENDENCY_LICENSES.md`.
5. **Entrypoint** — `COPY entrypoint.sh /entrypoint.sh` + `chmod +x`.
6. **Runtime dirs** — `mkdir -p /app/data /app/config /download` + `chown -R 99:100`.

`ARG APP_VERSION` / `VCS_REF` feed the OCI labels. `EXPOSE 8080`. `HEALTHCHECK` curls
`/api/health`. `ENTRYPOINT ["/entrypoint.sh"]`; `CMD ["uvicorn", "main:app", "--host",
"0.0.0.0", "--port", "8080", "--workers", "1"]`.

**`entrypoint.sh`** (POSIX sh): reads `PUID`/`PGID` (default `99:100`; `PUID=0` = explicit
root), creates/adjusts user+group tolerating concurrent-bootstrap races, applies `UMASK`
(default `002`), chowns `/app/data` `/app/config` (and `/download`, recursive only if
`CHOWN_DOWNLOADS_RECURSIVE=true`) — **chown failures are non-fatal but logged** so
storage-health checks can diagnose bad mounts — chmods `700` config/data, `600`
`config.json`, then `exec gosu $RUN_USER "$@"` (or `exec "$@"` as root).

`.dockerignore` excludes `.git`, caches, venvs, `data/`, `config/`, `docs/LOGO.md`.
`docker-compose.yml` is an Unraid-style example pinned to
`ghcr.io/xipher-zero/debridpulse:v1.0.11.1` (not auto-bumped per release).

### Local image testing

A push to `1.0.12` triggers **Fork Image** which publishes a write-once
`ghcr.io/xipher-zero/debridpulse:sha-<full 40-char sha>` multi-arch image (amd64 + arm64,
provenance + SBOM). Pull that tag into a local compose file to verify a change before it
gets a release tag. The image is pullable once Fork Image's "Publish immutable image"
job completes; Container Security and Candidate Runtime Qualification then run against
that exact digest.

---

## 6. Tests & the curated manifest system

### Running tests

- **Backend:** `cd backend && python -m pytest tests/`. **No pytest config file exists**
  anywhere (`pytest.ini` / `pyproject.toml` / `setup.cfg` / `tox.ini` — none). Tests are
  run from inside `backend/` so `rootdir`/`sys.path` resolve; many test modules
  `import` each other directly (e.g. `from test_universal_lifecycle import core`).
  `pytest-asyncio` runs in default strict mode — every async test has an explicit
  `@pytest.mark.asyncio`. `tests/conftest.py` only does `import aiosqlite` early to stop
  inherited legacy modules from stubbing it.
- **Frontend:** `cd frontend/browser && npm ci --ignore-scripts && npx playwright install
  chromium && npm test`. Playwright, Chromium only, headless, `fullyParallel: false`,
  `retries: 0`, 45 s timeout, `baseURL` from `DP_BASE_URL`.
- **Local Python env:** repo targets 3.12; there is no committed venv. `uv venv --python
  3.12` + `uv pip install -r backend/requirements-dev.txt` works. `google-re2` needs a
  wheel for the interpreter.
- `frontend/browser/node_modules/`, `test-results/`, `playwright-report/` are git-ignored.

### The three curated qualification manifests (`backend/tests/*.txt`)

Plain-text lists of pytest targets (one per line; `#`/blank lines stripped; `::node_id`
selectors allowed). CI feeds them to pytest via
`mapfile -t cases < <(grep -Ev '^[[:space:]]*(#|$)' tests/<file>.txt)`.

| File | ~cases | Purpose | Run by |
|---|---|---|---|
| `post_audit_qualification.txt` | 13 | Frozen v1.0.12 post-audit "six-finding" gate (ROUTE/CORE/STATE/DB/UIARCH). Header says "changes to the qualified tree require qualification from zero." | `tests.yml` |
| `two_provider_checkpoint_qualification.txt` | 32 | Broad two-provider (AllDebrid + General HTTP) regression gate — routing, applicability, provenance, consolidation, settings, license policy. | `tests.yml` |
| `ws3p1_adversarial_qualification.txt` | 9 | Consolidation-boundary adversarial gate (provider-neutral, deterministic). | `ws3-adversarial.yml` only |

**When editing code covered by a manifest, keep the manifest and its tests consistent** —
CI runs the manifest as a distinct gated step. Contract tests that read source files as
strings (`test_settings_*`, `test_ui_*`) will break on refactors and must be updated in
the same change; some are inside `post_audit_qualification.txt` and `two_provider_…txt`
(frozen), so a manifest-covered contract change should be deliberate.

### 6a. Qualification determinism (read `docs/QUALIFICATION_DETERMINISM.md`)

`Tests` and `Browser Runtime` run their full suite **once**; a failure is classified
candidate-vs-anchor by the single classifier, `.github/qualification/failure_classifier.py`
(`PASS` / `KNOWN_FLAKE` pass; `CANDIDATE_REGRESSION`, `ANCHOR_REPRODUCED_FLAKE`,
`INCONCLUSIVE`, `INFRASTRUCTURE_FAILURE` fail). Every run names `CANDIDATE_SHA` and
`QUALIFICATION_ANCHOR_SHA`; an anchor that cannot be resolved unambiguously fails closed (never
`main`). The diagnostic budget (3 cases, 8 isolated runs per ref + one bounded 16-run discriminator stage, 15 min, 0 automatic full-suite
reruns) has one owner, the classifier. `.github/qualification/known_flakes.json` has zero active
entries and is not a skip list. Enforced by `backend/tests/test_qualification_infrastructure_contract.py`.

CANDIDATE_REGRESSION is an evidence-backed discrimination,
not merely "candidate happened to fail and anchor happened not to fail
in a small sample." A candidate failure the anchor did not reproduce in its bounded sample gets
one bounded second stage (`discriminate`), and INCONCLUSIVE is the required classification when bounded evidence
cannot distinguish a rare candidate regression from low-rate
pre-existing nondeterminism. Agents must not alter production source solely because an isolated candidate sample contains a failure while a small anchor sample happens to contain none.

**Do not repeatedly rerun full qualification to chase green. Use candidate-vs-anchor classification.**
A flaky test is fixed at its oracle (replace the nondeterministic assumption with the real
invariant), never retried, slept around, or registered. Lifecycle/concurrency/ownership changes
require the adversarial preflight in that document *before* production edits.

---

## 7. GitHub Actions workflows (`.github/workflows/`)

All gate on: `main`, `1.0.11`, `1.0.12`, `staging/**`, some `audit/**`, plus `v*` /
`internal-v*` tags. All `uses:` are SHA-pinned. `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true`.

| Workflow | What it does |
|---|---|
| **tests.yml** (`Tests`) | Job `test`: resolves + publishes `QUALIFICATION_ANCHOR_SHA` (checkout `fetch-depth: 0`); Python 3.12; `ruff check … --select F821,F822,F823` (undefined names) over the backend packages; run `post_audit_qualification.txt`, then `two_provider_checkpoint_qualification.txt`, then full `pytest tests/` exactly once (junit XML captured), then the classifier decides the gate (bounded isolated candidate + anchor runs of only the failing node ids, anchor in its own venv); `python -m compileall -q .`; `node --check` on every `frontend/static/*.js` + `playwright.config.js` + `*.spec.js`. Installs `aria2`+`openssl` for downloader regression tests. Job `security` (needs `test`): `pip-audit -r requirements.txt` + `bandit -r . --exclude ./tests --severity-level high --confidence-level high`. |
| **browser-runtime.yml** (`Browser Runtime`) | Builds the real candidate image; runs two containers (open + password-auth, config generated via the image's own `auth.passwords.hash_password`); waits on `/api/health`; `npm ci --ignore-scripts` + `npm audit --audit-level=high` + `playwright install chromium`; runs the Playwright suite exactly once (`retries: 0`, JSON + line reporters); on failure the classifier drives bounded failing-case runs on a fresh candidate pair and on the anchor's own image/specs (ports 8082/8083); inventory must reconcile (`discovered == running == passed + failed`). Uploads traces, classification evidence + `checkpoint-*.png`. |
| **codeql.yml** (`CodeQL`) | `security-and-quality` queries for `python`, `javascript-typescript`, `actions`. Weekly cron (Thu 20:17 UTC). |
| **fork-image.yml** (`Fork Image`) | Build (`linux/amd64`) + extensive smoke test (OCI labels, Debian Trixie, `7zip`/`7zip-rar`/`aria2`/`gosu` present, RAR codec registered, 7z round-trip, license files, health version). The `publish` job runs **on any `push` event** (`if: github.event_name == 'push' || …`) — so a push to `1.0.12` publishes. It builds `linux/amd64,linux/arm64` and pushes a **write-once `sha-<full-sha>` tag only** (an existing valid candidate for that SHA is reused, never overwritten; a revision mismatch fails closed; runs for one SHA are serialized) to `ghcr.io/xipher-zero/debridpulse` with `provenance: mode=max` + SBOM, then verifies the published digest/annotations converged. (The `workflow_dispatch` `publish_sha` path is separately restricted to `main`/`feature/`/`fix/`/`chore/`.) |
| **container-security.yml** (`Container Security`) | Never rebuilds. Waits for the exact `sha-<full-sha>` digest, resolves per-arch child digests, runs **Trivy** twice per arch (report all MEDIUM+, then fail on fixable HIGH/CRITICAL), writes + signs a `container-security/v1` attestation to the registry. Weekly cron (Tue 19:31 UTC). |
| **candidate-runtime-qualification.yml** (`Candidate Runtime Qualification`) | Pulls the published amd64 + arm64 children by digest (arm64 via QEMU), verifies OCI labels, non-root runtime (uid/gid 99/100), writable `/app/data` `/app/config` `/download`, `/api/health` version, AllDebrid integration status, a live `GeneralHttpProvider.resolve()` call, and a 7z+RAR round-trip; signs a `candidate-runtime/v1` attestation. |
| **release-promotion.yml** (`Release Promotion`) | On push to `main` (→ `latest`) or a `v*`/`internal-v*` tag: waits for all **6 required workflows** green for the exact SHA — `Tests`, `Browser Runtime`, `CodeQL`, `Container Security`, `Candidate Runtime Qualification`, `Fork Image` — re-verifies both signed attestations target that digest, then `docker buildx imagetools create` to move the mutable tag to the already-qualified digest. **No rebuild. `WS3 Adversarial` is not in the required set.** Does not run for plain `1.0.12` pushes. |
| **ws3-adversarial.yml** (`WS3 Adversarial`) | `1.0.12` branch only. Runs `ws3p1_adversarial_qualification.txt`. |

Supporting config: `.github/dependabot.yml` (weekly pip updates for `/backend`, grouped
minor/patch; weekly Actions updates), issue templates (incl. `source_request.yml` for
GPL source offers), PR template.

**CI philosophy:** source is tested by `Tests`/`CodeQL`; the *image is the artifact* —
built once immutably by SHA, then scanned and runtime-qualified across both architectures
with signed in-registry attestations; mutable tags (`latest`/`v*`) are only ever
re-pointed at a digest that already passed every gate.

---

## 8. Settings secret-merge model (`backend/api/routes.py`)

- `_SECRET_SETTINGS` = `{discord_webhook_url, discord_webhook_added,
  stats_report_webhook_url, auth_password, extraction_password}` (integration-owned
  secrets such as the AllDebrid `api_key` live in their `integrations.<id>` namespace).
- `GET /api/settings` (`_public_settings`) **redacts every secret to `""`** and adds
  `<field>_configured: bool`.
- `PUT /api/settings` (`SettingsUpdate` = `AppSettings` + `clear_secrets: list[str]`),
  merged by `_merge_secret_settings(new, previous)`:
  1. a **supplied non-empty value is authoritative** — it overrides a contradictory
     `clear_secrets` entry (guard added 2026-09-09, `5c6be0b2`);
  2. else if the field is in `clear_secrets` → set `""`;
  3. else (blank, not cleared) → keep `previous`.
- Integration-namespaced secrets are merged separately in
  `integrations/configuration.py` `normalize_settings()` (`clear_legacy_secrets`,
  `definition.secret_fields`); `extraction_password` is a top-level field and is **not**
  in that path.

---

### Settings authority (final audit, 2026-09)

`integrations.<id>` (incl. AllDebrid credentials and every aria2 option), `transfer_policy` and
`execution_runtime_limits` are the **only** persisted/runtime authorities. The flat names
(`aria2_*`, `max_concurrent_downloads`, `alldebrid_api_key`, `poll_interval_seconds`, ...) are
not `AppSettings` fields; they are read only by `integrations.configuration.migrate_legacy_settings`
while `core.config.load_settings()` loads a file, and `GET /settings` derives them read-only
(`api/legacy_settings_view.py`, named in `compatibility_fields`). `PUT /settings` never writes a
canonical namespace (it carries `previous` forward); use `PATCH /integrations/{id}/configuration`,
`PATCH /transfer-policy`, `PATCH /execution/runtime-limits`. aria2 has one topology: DebridPulse
runs the daemon (`executors.aria2.runtime.Aria2Runtime`), which owns the loopback RPC endpoint/secret
and constructs the client (`rpc_service`); the client never reads settings. Metrics are `debridpulse_*`.

## 9. Presentation-owner consolidation — status

The "consolidate presentation owners" refactor (commit `7f652197`, 2026-09-07) was
landed **incomplete**: it added new dedicated owners but left the old implementations
in place, and the two would race and corrupt shared DOM/state.

### Archive Passwords editor — **RESOLVED** (2026-09-09, commits `6ee03459`, `5c6be0b2`, `4e30bc65`)

| File | State |
|---|---|
| `frontend/static/ui-settings-archive-passwords.js` (`window.DPArchivePasswords`) | **Sole owner of editor behavior.** `ui-settings-page.js` renders the whole field (hidden form-field `[data-setting="extraction_password"]` textarea, editor container, reveal button, hint); this file only fills the rows, binds its own events, hydrates from `GET /api/settings/extraction-passwords`, exposes `DPArchivePasswords.hydrated`. |
| `frontend/static/ui-settings-downloads-completion.js` (**file since deleted entirely**, Settings final-audit fold) | Archive-password code **deleted** (was `extractionPasswords`, `buildPasswordEditor`, `renderPasswordRows`, `loadExtractionPasswords`, `syncExtractionPasswordSource`, `setRevealAll`, the hidden `data-dp-extraction-clear-compat` checkbox). Keeps only non-password Extraction/Downloads layout. |
| `frontend/static/app.js` | Dead `_extractionPasswords` block **deleted**. |
| `frontend/static/ui-settings-downloads-completion.css` | Stale `.dp-settings-clear-secret:has([data-clear-secret="extraction_password"]) { display:none }` rule **removed** so the visible "Clear stored archive passwords" checkbox shows. |
| `frontend/static/ui-settings-page.js` | `nonAuthPayload()` routes `extraction_password` through `extractionPasswordValue()` — returns `''` (backend keeps stored list) while the editor is mounted but `!DPArchivePasswords.hydrated`; `clearSecrets()` drops a pre-hydration `extraction_password` clear. |
| `backend/api/routes.py` | `_merge_secret_settings` guard (see §8). |

**Historical data-loss bug (fixed):** with both owners live, owner #1's hidden clear
checkbox armed during a transient-empty render window, `PUT /api/settings` sent
`clear_secrets: ["extraction_password"]`, and the old `_merge_secret_settings` wiped the
stored value. Symptom was ~8 saved passwords collapsing to the last 2 typed. Any
passwords lost before the fix are **unrecoverable in-app** — restore from a pre-2026-09-07
`config.json` (`/app/data/backups/` if backups were on).

### Pattern to watch for

Before trusting a "this component was refactored / consolidated" claim: **grep for the
old owner's symbols and confirm it was actually deleted, not just superseded.** The
`7f652197` failure mode — new owner added via `PRESENTATION_OWNERS`, old owner left
mutating the same DOM — could recur in other settings panels. Check
`ui-settings-page.js` and `app.js` for stragglers if similar
"state resets on navigate-away" bugs surface elsewhere.

---

## 10. Working notes for sessions

- The maintainer historically worked directly on GitHub (no local clone); now clones
  locally. Active branch: **`1.0.12`**. Direct pushes to `1.0.12` are the established
  pattern for this dev branch (no PR, no merge to `main`, no force-push).
- Honor **"explore and report, don't change anything yet"** literally when given.
- For local verification, prefer building/pulling a Docker image (or running
  `uvicorn main:app` from a 3.12 venv with a temp `CONFIG_PATH`/`DB_PATH`) before pushing
  and triggering the full GitHub image pipeline.
- Known-flaky-test folklore is retired: the mirror-failover test and the WS1-P2 / WS2-P1 /
  Details-refresh browser cases were made deterministic (2026-09-19). A failure there is a
  real signal; classify it per §6a instead of rerunning.
