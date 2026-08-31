# DebridPulse v1.0.11 Frontend Architecture

## Governing rule

Dashboard is the accepted visual reference implementation for the application. Reusable visual decisions belong in shared tokens, components, or cross-page contracts. Page owners may add only geometry, behavior, content, or responsive requirements that are genuinely page-specific.

The accepted UI at the v1.0.11 RC baseline is the visual and behavioral contract. Architecture cleanup must preserve that output unless there is concrete evidence of a bug, accessibility defect, or security requirement.

## Effective browser bootstrap

The production browser path is deliberately layered but bounded:

1. `frontend/static/index.html` loads the compatibility stylesheet `style.css`, the v1.0.11 overlay `style-v11.css`, the synchronous first-paint theme bootstrap, and the parser-owned core runtimes.
2. `/app.js` is served by the authentication route as `auth.js` followed by `app.js`. `auth.js` owns application-session/CSRF bootstrap and loads the independent authenticated-shell assets `auth-ux.css` and `auth-help.js`.
3. `ui-theme-bootstrap.js` performs only first-paint palette restoration, then schedules `ui-presentation-loader.js` after `DOMContentLoaded`.
4. `ui-presentation-loader.js` loads presentation-only styles and runtimes sequentially in an explicit order. Individual presentation failures are logged and skipped rather than preventing the core application from initializing.
5. `style-v11.css` is the canonical stylesheet import root for the shared v1.0.11 visual system and page geometry.

`ui-theme-bootstrap.js` must remain free of application I/O and page-state ownership. The presentation loader is live runtime infrastructure; it is not migration debris.

## Core runtime ownership

- `app.js`: generic navigation, application API I/O, inherited operational rendering, transfer actions, and baseline Settings/Help markup that canonical presentation runtimes may replace or normalize.
- `auth.js`: authenticated application bootstrap, same-origin CSRF injection, session refresh/logout, and independent auth shell/help assets.
- `operator-title.js`: operator-title state plus canonical utility/status icon integration and guarded core-runtime fallbacks.
- `ui-runtime.js`: shared structural presentation for page headings, Dashboard presentation, Activity Log normalization, and Statistics KPI placement.
- `ui-downloads-runtime.js`: Downloads presentation and row interaction normalization.
- `ui-accessibility-runtime.js`: cross-cutting semantic/accessibility augmentation only; no application I/O.
- `ui-presentation-loader.js`: deterministic post-core presentation sequencing.

## Static cascade ownership

The `style-v11.css` cascade is ordered as:

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

`style.css` is the one retained compatibility stylesheet. The byte-identical, unreferenced `style-legacy.css` duplicate was removed during the RC adversarial cleanup and must not return.

## Page owners

The deliberate page owners are:

- Dashboard: `ui-dashboard.css`, live calibration layers described below, and final Dashboard owner `ui-dashboard-final.css`
- Downloads: `ui-downloads-page.css`, `ui-downloads-desktop.css`, and `ui-downloads-runtime.js`
- Activity Log: `ui-activity-log-page.css` plus structural normalization in `ui-runtime.js`
- Statistics: `ui-statistics-page.css`, `ui-statistics.css`, and `ui-statistics.js`
- Settings: clean-room `ui-settings-page.css` and `ui-settings-page.js`, with feature-specific Settings components loaded after it
- Help: clean-room `ui-help-page.css` and `ui-help-page.js`, with Help chrome and local legal-document components

`ui-page-finalization.css` and `ui-page-finalization.js` own accepted cross-page details that are not yet rendered directly by every canonical page owner. The runtime uses one bounded MutationObserver on `#content`; it does not create one observer per page subtree.

## Canonical component language

New or deliberately rebuilt markup uses the `dp-*` component classes from `ui-components.css` and the shared token system.

The application still contains inherited markup and generated HTML. `ui-universal-language.css` bridges those live families onto the shared visual defaults. This bridge is compatibility infrastructure, not permission to add new legacy-class markup.

## Interaction semantics

`ui-accessibility-runtime.js` is a cross-cutting semantic layer for inherited markup that cannot yet be rebuilt without broader churn. It may add roles, ARIA state, focusability, and keyboard activation while delegating actual actions to established application handlers. It must not perform API calls, transfer mutations, polling, or backend work.

## RC1 browser validation

The consolidated candidate `dd6984c940ee9dcffd20d8566f568d6eec9cbd3d` was browser-validated after consolidation against the previously accepted local build. The user-visible application was reported as visually and behaviorally indistinguishable from that accepted baseline.

That validation proves the current effective load graph reproduces the accepted presentation. It does **not** prove that every live layer can be deleted or reordered. A source is dead only when it has no inbound path from the production bootstrap graph or another live runtime.

## Retained live calibration debt

The following layers are **live calibration**, not dead source. They remain because their rules are part of the browser-validated cascade and removing or folding them changes the candidate until revalidated.

### Dashboard mixed calibration stack

The following order-sensitive layers remain live:

- `ui-dashboard-batch5.css`
- `ui-dashboard-polish.css`
- `ui-dashboard-polish-final.css`

They contain accepted rendering rules mixed across Dashboard, shell, provider, and transfer presentation. Their historical names are not desirable final ownership, but the current rules participate in the accepted cascade. A future fold must preserve their exact relative order in canonical owners and requires a new browser comparison.

### Settings Authentication sequence

Authentication remains split across the clean-room Settings owner plus later presentation, OIDC, callback, resilience, and feature-specific layers. These files are loaded by `ui-presentation-loader.js` and are heavily regression-tested. They are therefore live implementation, not retained dead code.

Further consolidation is allowed only as a behavior-preserving ownership refactor followed by full qualification and browser validation.

### Cross-page finalization

`ui-page-finalization.js` still owns accepted copy, hierarchy, and Downloads bulk-strip placement that are not rendered directly by every page owner. Its observer scope is bounded to `#content`. Moving this behavior into direct page ownership is a future structural refactor, not dead-source deletion.

## Dead-source policy

A frontend source is eligible for deletion only after the full reachability closure is checked across:

- direct `index.html` script/style references
- backend-composed browser assets such as `/app.js`
- dynamically injected assets from live runtimes such as `auth.js` and `ui-shell-runtime.js`
- `ui-theme-bootstrap.js` and `ui-presentation-loader.js`
- `style-v11.css` imports
- runtime-created script/link references
- tests or packaged/legal surfaces that intentionally consume the source

Historical naming is not evidence that a file is dead. Conversely, an unreachable byte-for-byte duplicate such as the retired `style-legacy.css` is not retained merely because it existed during implementation.

## RC1 version ownership

`VERSION` is the authoritative release version source.

- backend `/health`, `/version`, OpenAPI metadata, and `/api/stats` derive from `read_version()`
- the main sidebar hydrates its visible version from `/api/stats`
- the server-rendered login page renders `read_version()` directly
- container build metadata reads `VERSION` and propagates it to the OCI version label

No independent user-visible application version should be hardcoded in frontend presentation assets.

## Test policy

Frontend tests protect final ownership, effective load order, user-visible behavior, accessibility semantics, safety boundaries, accepted presentation contracts, and release-version propagation. Tests should describe behavior or ownership contracts rather than preserve historical filenames merely because they once existed.

Live historical layers may be named in tests only when their presence/order is itself part of the currently accepted load graph. Dead-source cleanup should add a narrow regression contract when recurrence is plausible.

## Readiness interpretation

A green qualification proves the candidate passes the repository's regression, security, syntax, container, and packaging gates. Browser validation independently establishes user-visible parity for the tested candidate.

The remaining Dashboard, Settings, and cross-page calibration layers are structural debt, but they are live and browser-validated. They are not classified as dead code and should not be deleted during release cleanup. Any further consolidation creates a new candidate boundary and requires the same qualification and browser comparison discipline used for RC1.
