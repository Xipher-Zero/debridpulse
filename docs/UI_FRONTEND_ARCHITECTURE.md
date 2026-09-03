# DebridPulse v1.0.12 Frontend Architecture

This document describes the post-audit canonical frontend ownership model. It records final owners, not the historical correction layers used while the UI overhaul was being built. The frontend architecture reports `1.0.12` for the current development tree and does not promote that tree as a final release baseline.

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
