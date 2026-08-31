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

- Dashboard: `ui-dashboard.css`, the retained mixed calibration layers below, and final Dashboard owner `ui-dashboard-final.css`
- Downloads: `ui-downloads-page.css`, `ui-downloads-desktop.css`, and `ui-downloads-runtime.js`
- Activity Log: `ui-activity-log-page.css`
- Statistics: `ui-statistics-page.css`, `ui-statistics.css`, and `ui-statistics.js`
- Settings: clean-room `ui-settings-page.css` and `ui-settings-page.js`, with feature-specific Settings components loaded after it
- Help: clean-room `ui-help-page.css` and `ui-help-page.js`, with Help chrome and local legal-document components

`ui-page-finalization.css` and `ui-page-finalization.js` own accepted cross-page details that are not yet rendered directly by every canonical page owner. The runtime uses one bounded MutationObserver on `#content`; it no longer creates one observer per page subtree.

## Canonical component language

New or deliberately rebuilt markup should use the `dp-*` component classes from `ui-components.css`.

The application still contains legacy markup and generated HTML. `ui-universal-language.css` bridges those live legacy families onto the same shared visual defaults. This bridge is compatibility infrastructure, not permission to add new legacy-class markup.

## Interaction semantics

`ui-accessibility-runtime.js` is a cross-cutting semantic layer for inherited markup that cannot yet be rebuilt without broader churn. It may add roles, ARIA state, focusability, and keyboard activation while delegating actual actions to established application handlers. It must not perform API calls, transfer mutations, polling, or backend work.

## Retained RC1 browser-validation debt

The release-candidate gate distinguishes proven static/runtime cleanup from visual changes that require direct browser comparison. The following areas remain live by design for `1.0.11rc1` local testing.

### Dashboard mixed calibration stack

The following order-sensitive layers remain:

- `ui-dashboard-batch5.css`
- `ui-dashboard-polish.css`
- `ui-dashboard-polish-final.css`

They contain accepted rendering rules mixed across Dashboard, shell, provider, and transfer presentation. Earlier Dashboard review layers were retired and Dashboard structural ownership was folded into `ui-dashboard.css`. `ui-dashboard-final.css` is now a deliberate final Dashboard owner rather than migration debt.

Further decomposition of these three mixed layers requires browser-backed comparison across dark/light and responsive layouts. RC1 local testing is the intended validation stage for that work.

### Settings Authentication sequence

Authentication remains split across the base Authentication owner plus later presentation, OIDC, and callback layers. These layers are live and heavily regression-tested. Prior MutationObserver regressions make speculative consolidation inappropriate before the first local RC browser pass.

The clean-room `ui-settings-page.js` remains observer-free. Authentication layering may be consolidated after RC1 browser validation proves the accepted Settings behavior and layout are stable.

### Cross-page finalization

`ui-page-finalization.js` still owns accepted copy, hierarchy, and Downloads bulk-strip placement that are not rendered directly by each page owner. Its observer debt has been reduced from five page-subtree observers to one bounded `#content` observer. Direct ownership by each page remains a later cleanup opportunity, not an RC1 blocker.

## RC1 version ownership

`VERSION` is the authoritative release version source.

- backend `/health`, `/version`, OpenAPI metadata, and `/api/stats` derive from `read_version()`
- the main sidebar hydrates its visible version from `/api/stats`
- the server-rendered login page renders `read_version()` directly
- container build metadata reads `VERSION` and propagates it to the OCI version label

No independent user-visible application version should be hardcoded in frontend presentation assets.

## Test policy

Frontend tests protect final ownership, effective load order, user-visible behavior, accessibility semantics, safety boundaries, accepted presentation contracts, and release-version propagation. Tests should not exist only to preserve retired batch filenames or migration ordering.

Where a live historical source layer must remain, tests should describe the accepted behavior it protects and explicitly identify the layer as retained browser-validation debt rather than treating its historical name as architecture.

## Readiness interpretation

A green qualification proves the candidate passes the repository's regression, security, syntax, container, and packaging gates. It does not prove pixel-level visual parity without browser automation.

For `1.0.11rc1`, the remaining Dashboard and Authentication layers are accepted as explicit browser-validation debt because local RC testing is the mechanism for validating them. This permits RC advancement when the static/runtime audit and release qualification are green.

The architecture recommendation remains `REQUIRES ADDITIONAL FRONTEND REMEDIATION` until those browser-sensitive layers are proven and folded. That recommendation does not by itself block the local RC candidate; it remains a gate for claiming final architectural cleanliness before the v1.0.11 release is finalized.
