# DebridPulse v1.0.11 Frontend Architecture

## Governing rule

Dashboard is the accepted visual reference implementation for the application. Reusable visual decisions belong in shared tokens, components, or cross-page contracts. Page owners may add only geometry, behavior, content, or responsive requirements that are genuinely page-specific.

The accepted UI at the frozen v1.0.11 baseline is the visual and behavioral contract. Architecture cleanup must not change that contract without concrete evidence of a bug, accessibility defect, or security requirement.

## Effective bootstrap

The normal browser path has two deliberate stages:

1. `index.html` loads the compatibility stylesheet, the v1.0.11 overlay, and the core parser-owned runtimes.
2. `ui-presentation-loader.js` loads presentation-only styles and runtimes sequentially in an explicit order.

The presentation loader is deterministic and failure-contained. A missing presentation asset is logged and skipped rather than preventing the remaining presentation layers or core application from initializing.

`ui-theme-bootstrap.js` applies stored theme state synchronously after `<body>` begins and before the visible shell is parsed. This prevents a stored light theme from first painting as dark.

## Static cascade ownership

The v1.0.11 overlay is ordered as:

1. design and language tokens
2. foundation and component primitives
3. shared dropdown, icon, universal, modal, and cross-page contracts
4. shell owners
5. Dashboard calibration
6. page-specific geometry and content
7. shared panel and transfer semantics
8. cross-page visual accents
9. shell signal field

Purpose-based cross-page owners include:

- `ui-utility-controls.css`: shared utility and recovery control presentation
- `ui-visual-accents.css`: semantic glow, failure rail, scrollbar, event-point, and elevation accents
- `ui-shell-signal-field.css`: text-only version datum and sidebar signal field
- `ui-transfer-contract.css`: shared transfer-row status, progress, and action presentation
- `ui-panel-surface-treatment.css`: shared large-panel interior richness

## Page owners

The deliberate page owners are:

- Dashboard: `ui-dashboard.css` plus the retained calibration stack described below
- Downloads: `ui-downloads-page.css`, `ui-downloads-desktop.css`, and `ui-downloads-runtime.js`
- Activity Log: `ui-activity-log-page.css`
- Statistics: `ui-statistics-page.css`, `ui-statistics.css`, and `ui-statistics.js`
- Settings: clean-room `ui-settings-page.css` and `ui-settings-page.js`, with feature-specific Settings components loaded after it
- Help: clean-room `ui-help-page.css` and `ui-help-page.js`, with Help chrome and local legal-document components

`ui-page-finalization.css` and `ui-page-finalization.js` own accepted cross-page final details that are not yet rendered directly by every canonical page owner.

## Canonical component language

New or deliberately rebuilt markup should use the `dp-*` component classes from `ui-components.css`.

The application still contains legacy markup and generated HTML. `ui-universal-language.css` bridges those live legacy families onto the same shared visual defaults. This bridge is compatibility infrastructure, not permission to add new legacy-class markup.

## Interaction semantics

`ui-accessibility-runtime.js` is a cross-cutting semantic layer for inherited markup that cannot yet be rebuilt without broader churn. It may add roles, ARIA state, focusability, and keyboard activation while delegating actual actions to established application handlers. It must not perform API calls, transfer mutations, polling, or backend work.

## Retained live architecture debt

Two areas remain intentionally uncollapsed because source inspection alone cannot prove a neutral visual rewrite.

### Dashboard calibration stack

The following layers are still live and order-sensitive:

- `ui-dashboard-batch5.css`
- `ui-dashboard-polish.css`
- `ui-dashboard-polish-final.css`
- `ui-dashboard-consistency.css`

They contain accepted final rendering rules mixed across Dashboard, shell, provider, and transfer presentation. Earlier Dashboard review layers were retired as their responsibilities became provably owned elsewhere. These four remain until browser-backed visual comparison can prove a further fold preserves the accepted dark/light and responsive baseline.

### Settings Authentication sequence

Authentication remains split across the base Authentication owner plus later OIDC, callback, and presentation layers. These layers are live, and prior MutationObserver regressions make speculative consolidation unsafe without direct browser verification.

### Cross-page finalization observer

`ui-page-finalization.js` preserves accepted copy, hierarchy, and Downloads bulk-strip placement. It still observes multiple page subtrees. This is known runtime architecture debt. It should be folded into canonical render owners only when the resulting DOM and behavior can be verified directly.

## Test policy

Frontend tests protect final ownership, effective load order, user-visible behavior, accessibility semantics, safety boundaries, and accepted presentation contracts. Tests should not exist only to preserve retired batch filenames or migration ordering.

Where a live historical source layer must remain, tests should describe the accepted behavior it protects and explicitly identify the layer as retained debt rather than treating its historical name as architecture.

## Readiness interpretation

A green qualification proves the candidate passes the repository's regression, security, syntax, container, and packaging gates. It does not prove pixel-level visual parity without browser automation.

Until the retained Dashboard and Authentication stacks are browser-proven and folded, the architecture audit result is `REQUIRES ADDITIONAL FRONTEND REMEDIATION`. That recommendation describes architecture cleanliness only. It is not release authorization and does not advance `VERSION`.
