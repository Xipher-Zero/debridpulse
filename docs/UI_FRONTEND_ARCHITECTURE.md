# DebridPulse v1.0.11.1 Frontend Architecture

## Governing rule

The accepted v1.0.11.1 browser output is implemented directly by canonical owners. Stable page structure belongs in `frontend/static/index.html` or in the deliberate clean-room render owner for a page. Stable presentation belongs in the canonical CSS cascade. Runtime JavaScript owns application data, dynamic state, user actions, and narrowly scoped semantic/accessibility behavior; it must not recreate a second post-load presentation architecture.

Dashboard remains the visual reference surface for shared language. Reusable visual decisions belong in shared tokens/components/contracts, while page owners retain only geometry, behavior, content, and responsive rules that are genuinely page-specific.

## Effective browser bootstrap

`frontend/static/index.html` is the packaged base document. It directly loads:

- `style.css`, the retained compatibility stylesheet;
- `style-v11.css`, the canonical v1.0.11.1 stylesheet import root;
- `ui-theme-bootstrap.js` synchronously for first-paint theme restoration;
- the vendor Chart runtime;
- `app.js`, `operator-title.js`, and the canonical page/cross-page runtimes as direct deferred scripts.

The authenticated `/app.js` response is composed by the backend authentication surface so session/CSRF bootstrap precedes the application script. Authentication may load its own auth-specific shell/help assets, but it does not own application page composition.

`ui-theme-bootstrap.js` is first-paint policy only. It may restore the selected theme before paint and emit the theme state expected by the normal runtimes. It does not inject scripts or styles, perform application I/O, or sequence presentation layers.

There is no live presentation-loader/finalization bootstrap. Retired `ui-presentation-loader.*`, `ui-page-finalization.*`, shell-runtime migration layers, and authentication polish/finalization layers are not part of the production load graph and must not be reintroduced as a corrective mechanism.

## Core runtime ownership

- `app.js`: generic navigation, application API transport, transfer data/actions, Dashboard data hydration, Event Log data/rendering, shared operational controls, and other inherited application behavior that is still intentionally common.
- `auth.js`: application-session bootstrap, CSRF handling, login/logout/session behavior, and authentication-specific browser policy.
- `operator-title.js`: operator-title state plus canonical utility/status icon integration and guarded core-runtime fallbacks.
- `ui-runtime.js`: explicit-event coordination for shared shell/Dashboard/Event Log presentation that remains dynamic. It does not reconstruct canonical pages after load.
- `ui-downloads-runtime.js`: Downloads filtering, pagination, row interaction, and canonical dynamic Downloads behavior.
- `ui-accessibility-runtime.js`: cross-cutting semantics, focusability, ARIA state, and keyboard augmentation for inherited markup. It must not perform application I/O or own page composition.
- `ui-statistics.js`: Statistics data/rendering and chart lifecycle for the base Statistics structure.
- `ui-settings-page.js`: the deliberate clean-room Settings render owner. `#view-settings` is an empty base render root; this owner directly renders the accepted Settings architecture and emits its explicit lifecycle event.
- `ui-help-page.js`: the deliberate clean-room Help render owner. `#view-help` is an empty base render root; this owner directly renders the accepted Help architecture and emits its explicit lifecycle event.

Feature-specific Settings and Help modules extend those canonical owners for bounded functions such as maintenance, notifications, aria2 live state, legal-document dialogs, and card icons. They are not post-load page-replacement layers.

## Canonical page ownership

The supported application navigation contains exactly six surfaces:

1. Dashboard
2. Downloads
3. Event Log
4. Statistics
5. Settings
6. Help & License

Ownership is:

- **Dashboard** — stable markup in `index.html`; `ui-dashboard.css` owns the accepted Dashboard presentation, including calibration rules consolidated from historical batch/polish files; `app.js`/`ui-runtime.js` own dynamic data and explicit-event coordination.
- **Downloads** — stable shell/table/filter/bulk structure in `index.html`; `ui-downloads-page.css` and `ui-downloads-desktop.css` own geometry; `ui-downloads-runtime.js` owns dynamic filtering/pagination/interaction.
- **Event Log** — stable structure in `index.html`; `ui-activity-log-page.css` owns page presentation; `app.js` owns event data/rendering and `ui-runtime.js` handles bounded shared coordination.
- **Statistics** — stable structure in `index.html`; `ui-statistics-page.css` owns page geometry and `ui-statistics.js` owns data/chart lifecycle.
- **Settings** — empty `#view-settings` render root in `index.html`; `ui-settings-page.js` is the single clean-room page owner with feature-specific Settings modules/styles loaded directly.
- **Help & License** — empty `#view-help` render root in `index.html`; `ui-help-page.js` is the single clean-room page owner with the legal-document module and Help styles loaded directly.

The retired `view-changelog`, `view-aria2queue`, and `view-support` surfaces have no supported inbound navigation path and are not packaged. Changelog access is the external repository link already exposed by the shell; aria2 operational controls live on supported Downloads/Settings surfaces; project/legal information lives under Help & License.

## CSS ownership

`style.css` is the one intentionally retained inherited compatibility stylesheet. New stable presentation must not be added there when a canonical v1.0.11.1 owner exists.

`style-v11.css` is the canonical import root. Its effective order is:

1. design/language tokens;
2. foundation, components, dropdown/icon/universal/shared/modal contracts;
3. application shell owners;
4. canonical Dashboard owner and shared utility controls;
5. page-specific Statistics, Event Log, Downloads, Settings, and Help owners;
6. shared panel and transfer semantics;
7. cross-page visual accents and shell signal field;
8. directly required Settings/Help feature styles.

Historical `ui-dashboard-batch5.css`, `ui-dashboard-polish.css`, and `ui-dashboard-polish-final.css` are not live layers. Their accepted rules were folded, in source order where required, into `ui-dashboard.css`. Tests may forbid those retired filenames while continuing to protect the accepted rendered contract.

## Intentionally retained compatibility surfaces

Compatibility is retained only where it still has a supported runtime consumer:

- `style.css` for inherited base classes not yet worth rebuilding;
- common `app.js` helpers and application handlers used by supported pages;
- the backend-composed authentication bootstrap;
- accessibility augmentation for inherited markup that remains canonical product surface;
- purpose-specific feature modules that extend Settings/Help without replacing their page owners.

Historical filenames, detached navigation, `display:none`, or prior use during the overhaul are not reasons to retain source. Reachability and supported ownership are the criteria.

## Runtime versus source-owned structure

Runtime code may:

- fetch and render dynamic application data;
- update progress/status/KPI/chart values;
- respond to navigation and explicit lifecycle events;
- manage filters, pagination, modals, focus, form state, authentication state, and user actions;
- render Settings and Help because those two pages deliberately expose empty canonical render roots owned by their direct page scripts.

Runtime code must not:

- reconstruct stable Dashboard/Downloads/Event Log/Statistics architecture that belongs in the base document;
- inject a second stylesheet/script graph to converge the UI after load;
- reintroduce finalization/polish/correction layers to override canonical owners;
- resurrect unsupported views or hidden legacy navigation;
- use a broad MutationObserver as a substitute for direct ownership when an explicit lifecycle event or source-owned structure is available.

## Dead-source and reachability policy

Before deleting a frontend source, check the complete supported inbound graph: direct `index.html` references, backend-composed browser assets, direct imports, runtime-created references that are still intentional, and tests/legal surfaces. A source with no supported inbound path should be physically removed rather than hidden or suppressed.

Conversely, shared code remains when a supported canonical surface still consumes it. Removal of an unreachable view does not justify deleting a shared API or helper used by Settings, Downloads, or another live page.

## Version ownership

`VERSION` is the authoritative application version source and remains `1.0.11.1` for this corrective release.

- backend health/version/OpenAPI/statistics surfaces derive from `read_version()`;
- the sidebar hydrates its visible version from application data;
- the server-rendered login surface derives its version from the backend;
- container metadata reads `VERSION` and propagates it to the OCI version label.

No independent user-visible release version should be hard-coded into presentation assets.

## Qualification policy

Static source contracts protect ownership, direct dependencies, retired-source non-recurrence, JavaScript syntax, and final semantic invariants. They are necessary but not sufficient.

Permanent CI also executes a deliberately narrow real-browser smoke contract. It protects bootstrap, the six canonical navigation surfaces, repeated/rapid navigation, representative reloads, Dashboard/Downloads/Statistics/Settings/Help runtime presence, authentication bootstrap, modal focus lifecycle, theme initialization, representative API failure behavior, and the absence of retired presentation-loader/finalization dependencies.

Any application-source change creates a new candidate SHA and requires qualification on that exact SHA. Browser qualification establishes runtime behavior for the tested candidate; it is not inferred from static contracts alone.
