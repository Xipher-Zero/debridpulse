from pathlib import Path


def append_once(path: str, marker: str, section: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if marker in text:
        raise SystemExit(f"{path}: marker already exists")
    p.write_text(text.rstrip() + "\n\n" + section.strip() + "\n", encoding="utf-8")


append_once(
    "docs/ROUTE_PROVIDER_PROVENANCE.md",
    "## Post-audit retry isolation (v1.0.12)",
    """## Post-audit retry isolation (v1.0.12)

Initial routing and ordinary retry are separate decisions. A new logical route uses the neutral provider-selection policy: enabled SPECIALIZED applicability wins over GENERIC applicability, then the normal same-class selection policy applies. Once that route has selected a provider, ordinary resolution retry and re-resolution remain bound to that selected provider. Provider enablement, health, priority, or dynamic host-applicability changes do not silently reopen the global provider set for an existing route.

Automatic cross-provider production failover is deferred. A future explicit failover mechanism may create a new provider route attempt and append truthful provenance such as Provider A failed -> Provider B completed, but ordinary retry is not that mechanism. Provider identity recorded on route, candidate, artifact, and execution provenance is durable historical truth and is never reconstructed later from the submitted URL or current applicability state.
""",
)

append_once(
    "docs/architecture/PROVIDER_APPLICABILITY.md",
    "## Explicit applicability contract (post-audit v1.0.12)",
    """## Explicit applicability contract (post-audit v1.0.12)

Every production provider that participates in neutral routing declares applicability explicitly. Missing applicability is not a compatibility signal and does not make a provider eligible for URL routing. Providers with intentionally empty neutral applicability use an explicit `ProviderApplicability()` value; protocol-generic providers declare generic schemes; specialized providers declare provider-owned claims translated into neutral facts.

The classifier remains provider-name-neutral. Static magnet/torrent capability routing remains separate. For a new URL route, SPECIALIZED matches suppress GENERIC matches. After a provider is selected, ordinary retry remains bound to that provider and does not rerun global applicability selection; cross-provider failover is a separate deferred policy concern.
""",
)

append_once(
    "docs/architecture/UNIVERSAL_TRANSFER_CORE.md",
    "## Post-audit ownership invariants (v1.0.12)",
    """## Post-audit ownership invariants (v1.0.12)

### Provider selection and retry

The universal core owns provider identity for a route attempt. Initial routing may select among eligible providers, but once selected, ordinary resolution retry and re-resolution stay bound to that provider. Adapter output may omit provider identity and be stamped by the core; contradictory provider identity is rejected before persistence. Ordinary retry never silently becomes cross-provider failover. Broad automatic production failover remains deferred to an explicit future route-transition policy.

### Cancellation serialization

Logical cancellation authority is committed on the parent transfer before remote executor cancellation is attempted. Once the parent is cancelled, later executor observations, reconciliation, completion, or materialization activity cannot revive it. Remote cancellation or cleanup errors are recorded as control-plane/cleanup outcomes and do not revoke the already-authoritative logical cancellation.

### Database startup and migration

Current-schema startup and historical migration are distinct owners. Normal repository initialization ensures the current schema required by runtime code; it does not reconstruct historical migration state. Supported predecessor upgrades are prepared and applied by the explicit v1.0.12 migration owner, including historical provenance backfill, with backup-before-current-mutation semantics. Migration helpers may live beside runtime repositories, but production migration invocation remains in `db/migrations/v112.py`.
""",
)

Path("docs/UI_FRONTEND_ARCHITECTURE.md").write_text("""# DebridPulse v1.0.12 Frontend Architecture

This document describes the post-audit canonical frontend ownership model. It records final owners, not the historical correction layers used while the UI overhaul was being built.

## Core rule

Every visible behavior has a canonical structural/render owner and intentionally composed styling. Current markup must be final when rendered; broad post-render DOM repair, duplicate semantic style generations, and "fix whatever the legacy page emitted" runtimes are not part of the architecture.

`frontend/static/index.html` carries `data-dp-ui="v1.0.12-canonical"` and owns the static application shell structure. `frontend/static/app.js` owns the main application page state/rendering for Dashboard, Activity Log, Downloads, Details, navigation state, transfer presentation, and provider-status rendering. Page-specific clean-room modules own pages that were intentionally separated from `app.js`.

## Runtime ownership map

| Surface | Markup / render owner | JavaScript behavior owner | Styling owner |
| --- | --- | --- | --- |
| Application shell and sidebar | `index.html` | `app.js` for navigation state; `operator-title.js` for canonical icon SVG geometry only | `ui-shell.css`, `ui-shell-structural.css`, `ui-shell-brand.css`, `ui-shell-signal-field.css` |
| Top controls and theme first paint | `index.html` | `app.js`; `ui-theme-bootstrap.js` only for pre-paint stored-theme application | `ui-topbar-first-paint.css`, `ui-utility-controls.css` |
| Provider status | `index.html` shell target + final markup from `app.js` | `app.js`, using provider-specific backend state | **single canonical owner** `ui-shell-provider-status.css` |
| Dashboard | `index.html` + `app.js` dynamic content | `app.js` | `ui-dashboard.css` plus shared contracts |
| Activity Log | `index.html` + `app.js` dynamic content | `app.js` | `ui-activity-log-page.css` plus shared transfer contracts |
| Downloads | `index.html` + final rows/pagination from `app.js` | `app.js` | `ui-downloads-page.css`, `ui-downloads-desktop.css`, shared transfer contracts |
| Details | static overlay shell in `index.html`, dynamic content from `app.js` | `app.js` | shared modal/transfer/shell contracts |
| Statistics | page shell + `ui-statistics.js` rendering | `ui-statistics.js` is the single detailed-statistics I/O owner | `ui-statistics-page.css` |
| Settings shell and Sources & Providers | generated clean-room markup | `ui-settings-page.js` is authoritative; scoped Settings modules handle their named subfeatures | `ui-settings-page.css`, `ui-settings-chrome.css`, and narrowly scoped Settings styles |
| Help | generated Help page markup | `ui-help-page.js` and local legal-document helper | `ui-help-page.css` and scoped Help styles |
| Shared cards/forms/buttons/toggles/dropdowns | canonical markup from the owning page renderer | page owner; `ui-accessibility-runtime.js` may project accessibility/dropdown semantics only | `ui-foundation.css`, `ui-components.css`, `ui-dropdown-contract.css`, `ui-shared-contract.css`, `ui-universal-language.css` |
| Modal shell | owner page markup | owner page runtime | `ui-modal-contract.css` |
| Authentication Required | `ui-auth-required.js` | `ui-auth-required.js` using only the neutral INPUT_REQUIRED challenge contract | `ui-auth-required.css` + modal contract |
| Transfer provider/provenance presentation | final transfer markup from `app.js` | `app.js` consumes durable backend projection | `ui-transfer-contract.css` and owning page styles |
| Responsive/theme modifiers | existing canonical structures only | no DOM-repair runtime | base/page styles using scoped responsive and light-theme modifiers |

## Provider-status authority

Generic application/API health is not provider health. `app.js` renders AllDebrid status only from provider-specific backend state and distinguishes disabled, unconfigured, authentication-required, healthy, unhealthy, and neutral/unknown states. A successful generic `/stats` or application-health request must never manufacture `AllDebrid: online`.

The premium-status DOM is emitted in final form by `app.js`, including `.dp-provider-premium-until` and `.dp-provider-premium-days`. There is one provider-status stylesheet, `ui-shell-provider-status.css`. `ui-shell-provider-status-v2.css` is retired and mechanically forbidden by architecture tests.

## Accessibility and dropdown runtime

`ui-accessibility-runtime.js` is the retained cross-cutting compatibility module. Its scope is intentionally narrow: accessibility semantics, keyboard behavior, ARIA state, and universal native-select dropdown projection. It performs no application API I/O and does not repair canonical Activity Log naming, Downloads geometry, provider premium labels, provider status, or canonical page markup.

A `MutationObserver` remains only to support its universal accessibility/dropdown projection across dynamically rendered controls. That observer is not a presentation-repair mechanism.

## Retired correction runtimes

`ui-runtime.js` and `ui-downloads-runtime.js` are physically absent and unreferenced. Their former presentation-repair responsibilities were moved into `index.html`, `app.js`, or the actual page owner. Archived runtimes such as `sidebar-v2.js`, `hamburger-v2.js`, and `provider-ui.js` are also mechanically barred from the effective boot graph.

`operator-title.js` is not a loader or DOM-repair layer. It owns the canonical Lucide-compatible SVG geometry exposed through the icon contract and does not install runtimes, reparent markup, bind navigation, or inject corrective CSS.

## CSS composition

`style.css` remains the legacy baseline stylesheet required by the accepted frontend. `style-v11.css` retains its filename for asset compatibility but is the canonical v1.0.12 import graph, not an audit-fix overlay. It composes tokens, foundation/components, shared language/contracts, shell, page-specific geometry, transfer semantics, and scoped final accents in a deterministic order.

Multiple stylesheets are legitimate when responsibility is intentionally split (for example base component language plus light-theme/responsive modifiers, or a page stylesheet plus a shared transfer contract). Duplicate generations that successively redefine the same semantic owner are not legitimate. The provider-status `-v2` generation was removed rather than concatenated into the canonical owner.

## Page-specific modules

Settings is a deliberate clean-room page owner: `ui-settings-page.js` owns generated Settings markup, API contracts, serialization, and navigation entry. Its narrowly scoped companion modules own named subfeatures such as aria2 live state, completion behavior, maintenance wipe, notifications, and card icons; they are not generic post-render correction layers.

Help and Statistics similarly have explicit page owners. Main application pages remain in `app.js`; this boundary is tested so generic correction runtimes cannot reclaim them.

## Architectural tests

Permanent tests prove semantic ownership rather than historical coexistence. In particular:

- `test_uiarch001_e1_ownership.py` proves retired correction runtimes are absent and shell/download markup is final at render time.
- `test_uiarch001_e2_ownership.py` proves one provider-status style owner, final Activity Log/Downloads/provider markup, and a non-repairing accessibility runtime.
- `test_ui_runtime_architecture_contract.py` proves direct canonical shell/download owners and rejects archived runtimes.
- `test_ui_frontend_deep_audit_contract.py` proves a bounded first-paint bootstrap, unique effective asset loading, I/O-free accessibility runtime, and CI syntax coverage.
- page/component contract tests preserve the approved dark/light and responsive presentation.

## Compatibility retained intentionally

The filename `style-v11.css` is retained because it is the established canonical import-bundle URL and renaming it provides no architectural gain; its contents now document current v1.0.12 ownership. `style.css` remains an accepted baseline dependency. `ui-accessibility-runtime.js` remains because dynamic accessibility and dropdown projection is a legitimate cross-cutting concern. None of these retained pieces is permitted to repair canonical page markup after render.

## Change rule

New frontend work must modify the canonical owner or introduce a genuinely new scoped owner. It must not add a new correction stylesheet generation, broad post-render patch script, provider-specific override generation, or compatibility shim whose purpose is to undo another current owner.
""", encoding="utf-8")

style = Path("frontend/static/style-v11.css")
style_text = style.read_text(encoding="utf-8")
old_style_header = "/* DebridPulse v1.0.11 visual system overlay.\n *"
if old_style_header not in style_text:
    raise SystemExit("style-v11 canonical header anchor missing")
style_text = style_text.replace(
    old_style_header,
    "/* DebridPulse v1.0.12 canonical visual import graph.\n * The style-v11.css filename is retained as the established asset URL.\n *",
    1,
)
style_text = style_text.replace(
    "/* Canonical Dashboard presentation owns its accepted v1.0.11 calibration directly.",
    "/* Canonical Dashboard presentation owns its accepted calibration directly.",
    1,
)
style.write_text(style_text, encoding="utf-8")

deep = Path("backend/tests/test_ui_frontend_deep_audit_contract.py")
deep_text = deep.read_text(encoding="utf-8")
old_deep = '"""Final-state frontend architecture contracts for the v1.0.11 UI branch.'
if old_deep not in deep_text:
    raise SystemExit("deep audit docstring anchor missing")
deep.write_text(
    deep_text.replace(old_deep, '"""Final-state frontend architecture contracts for the v1.0.12 canonical UI.', 1),
    encoding="utf-8",
)

Path("backend/tests/test_post_audit_architecture_documentation.py").write_text('''from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_retry_and_provenance_docs_preserve_post_audit_contract() -> None:
    doc = read("docs/ROUTE_PROVIDER_PROVENANCE.md")
    assert "ordinary resolution retry and re-resolution remain bound to that selected provider" in doc
    assert "Automatic cross-provider production failover is deferred" in doc
    assert "never reconstructed later from the submitted URL" in doc


def test_applicability_docs_require_explicit_provider_contract() -> None:
    doc = read("docs/architecture/PROVIDER_APPLICABILITY.md")
    assert "Missing applicability is not a compatibility signal" in doc
    assert "explicit `ProviderApplicability()`" in doc
    assert "ordinary retry remains bound to that provider" in doc


def test_core_docs_record_cancellation_and_migration_ownership() -> None:
    doc = read("docs/architecture/UNIVERSAL_TRANSFER_CORE.md")
    assert "Logical cancellation authority is committed on the parent transfer before remote executor cancellation" in doc
    assert "cannot revive it" in doc
    assert "Normal repository initialization" in doc
    assert "db/migrations/v112.py" in doc


def test_frontend_docs_describe_current_canonical_owners() -> None:
    doc = read("docs/UI_FRONTEND_ARCHITECTURE.md")
    assert doc.startswith("# DebridPulse v1.0.12 Frontend Architecture")
    assert "Activity Log" in doc
    assert "**single canonical owner** `ui-shell-provider-status.css`" in doc
    assert "`ui-accessibility-runtime.js` is the retained cross-cutting compatibility module" in doc
    assert "`ui-runtime.js` and `ui-downloads-runtime.js` are physically absent and unreferenced" in doc
    assert "`style-v11.css` retains its filename for asset compatibility" in doc


def test_canonical_bundle_comment_no_longer_claims_v111_overlay() -> None:
    style = read("frontend/static/style-v11.css")
    assert "v1.0.12 canonical visual import graph" in style
    assert "v1.0.11 visual system overlay" not in style
''', encoding="utf-8")

Path("backend/tests/post_audit_qualification.txt").write_text('''# Permanent focused post-audit v1.0.12 six-finding qualification gate.
# ROUTE-001 + CORE-001
tests/test_audit_remediation_group_a.py
# STATE-001
tests/test_audit_remediation_state.py
# DB-001
tests/test_database_migration_ownership.py
# UIARCH-002 provider-specific health presentation
tests/test_alldebrid_status_contract.py
tests/test_ui_provider_status_contract.py
# UIARCH-001 canonical frontend ownership
tests/test_uiarch001_e1_ownership.py
tests/test_uiarch001_e2_ownership.py
tests/test_ui_runtime_architecture_contract.py
# Post-audit documentation and secondary applicability boundary
tests/test_post_audit_architecture_documentation.py
tests/test_applicability_explicit_contract.py
''', encoding="utf-8")

manifest = Path("backend/tests/two_provider_checkpoint_qualification.txt")
mtext = manifest.read_text(encoding="utf-8")
old_manifest = """# This is the permanent focused gate for the early Stage 17/18 shim checkpoint.
# It preserves every qualified Item 11 production-path owner and adds canonical
# architecture/current-state documentation/license owners. It is not final
# v1.0.12 release qualification; deferred Items 12–16 require later convergence."""
new_manifest = """# This remains the broad canonical two-provider regression gate.
# It preserves the qualified Item 11 production path, architecture, documentation,
# and license owners alongside the separate post-audit six-finding gate. It is not
# final release promotion; deferred provider/protocol work remains out of scope."""
if old_manifest not in mtext:
    raise SystemExit("two-provider manifest comment anchor missing")
manifest.write_text(mtext.replace(old_manifest, new_manifest, 1), encoding="utf-8")

workflow = Path(".github/workflows/tests.yml")
wtext = workflow.read_text(encoding="utf-8")
anchor = "      - name: Run v1.0.12 two-provider canonical qualification\n"
if wtext.count(anchor) != 1:
    raise SystemExit("Tests workflow qualification anchor mismatch")
block = '''      - name: Run v1.0.12 post-audit six-finding qualification
        run: |
          set -o pipefail
          cd backend
          mapfile -t cases < <(grep -Ev '^[[:space:]]*(#|$)' tests/post_audit_qualification.txt)
          python -m pytest "${cases[@]}" -v --tb=short 2>&1 | tee "$RUNNER_TEMP/post-audit.log"

      - name: Upload post-audit qualification output
        if: always()
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
        with:
          name: post-audit-qualification-${{ github.sha }}
          path: ${{ runner.temp }}/post-audit.log
          if-no-files-found: error
          retention-days: 14

'''
workflow.write_text(wtext.replace(anchor, block + anchor, 1), encoding="utf-8")
