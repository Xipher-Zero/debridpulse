# DebridPulse v1.0.12 Frontend Architecture

This document describes the live post-audit frontend ownership model. It records current owners and boot relationships; it does not describe historical correction layers as though they were still canonical.

The application reports `1.0.12` for the current development tree.

## Core rule

Every visible behavior has one bounded structural/render owner and intentionally composed styling. Broad post-render correction runtimes, correction-named stylesheets, and compatibility globals that replace unrelated page owners are prohibited.

`frontend/static/index.html` owns the static shell and the six reachable navigation surfaces: Dashboard, Downloads, Activity Log, Statistics, Settings, and Help & License. The retired `view-changelog`, `view-aria2queue`, and `view-support` surfaces are absent from the shell.

## Boot graph

`index.html` parser-loads the established application and canonical page owners. The provider-status owner executes after `app.js` and performs a one-time ordered startup of the bounded presentation owners needed by existing shell subfeatures. The startup list is fixed, contains no application API I/O, and does not perform post-render correction.

`ui-presentation-loader.js` is physically absent. It is a retired presentation-loader dependency and must not return under another correction-layer role.

The bounded presentation owners are:

- `ui-toast-contract.js` — public toast copy/timing bridge to the canonical `operator-title.js` presenter.
- `ui-dashboard-transfer-presentation.js` — Dashboard Recent Activity row/source presentation.
- `ui-downloads-presentation.js` — Downloads date options, pagination, and measured desktop capacity.
- `ui-processing-presentation.js` — authoritative pause-state projection and scheduler-capacity synchronization.
- `ui-activity-log-runtime.js` — Activity Log server-side filtering, timestamps, and filter-control projection.
- `ui-settings-archive-passwords.js` — Automatic Extraction archive-password editor interaction.

There is no `DPUICorrectionBatch1`, `DPUICorrectionBatch1Final`, or `DPUICorrectionP4Repair` runtime contract in the boot graph.

`ui-group-candidates.js` (`window.DPGroupCandidates`) is parser-loaded as an ordered `defer` script alongside `ui-detail-candidates.js` — it is not in the bounded-owner startup list because it must be ready for the first Downloads / Recent list render. It is the single owner of transfer-level **common-source group switching**, and keeps two facts strictly independent: **membership** — which source hosts exist as a canonical candidate on every current authoritative file of a transfer (`commonHosts`, independent of `switch_eligible`/artifact state/selection) — and **actionability** — which of those common hosts the group can currently converge to (`actionableHosts`). Membership drives the launcher count/visibility; actionability drives only whether a common host's row offers "Switch to this source". It also owns the shared chooser popover and switch orchestration over the existing exact per-file candidate endpoint, and owns no switching engine, candidate model, routing, recovery, provenance, or lifecycle. Downloads, Dashboard Recent Items, and the Details Files-section header all invoke the same `DPGroupCandidates.launcherMarkup()` / `DPGroupCandidates.open()`; none carries its own group semantics. The per-file candidate disclosure (`ui-detail-candidates.js`) and the individual rows are a separate owner and are untouched by it.

## Runtime ownership map

| Surface | Structure/render owner | Behavior owner | Styling owner |
| --- | --- | --- | --- |
| Application shell/navigation | `index.html` + `app.js` | `app.js` | shell styles |
| Provider status | shell target + `ui-provider-status.js` | `ui-provider-status.js` | `ui-shell-provider-status.css`, `ui-provider-summary.css` |
| Dashboard | `index.html` + `app.js` | `app.js` plus `ui-dashboard-transfer-presentation.js`, `ui-group-candidates.js` | `ui-dashboard.css`, `ui-dashboard-transfer-presentation.css` |
| Downloads | `index.html` + `app.js` | `app.js` plus `ui-downloads-presentation.js`, `ui-group-candidates.js` | `ui-downloads-page.css`, `ui-downloads-desktop.css`, `ui-downloads-presentation.css` |
| Processing/topbar projection | shell/app controls | `app.js`, `ui-topbar-concurrency.js`, `ui-processing-presentation.js` | owning shell/page styles |
| Activity Log | `index.html` + runtime rows | `ui-activity-log-runtime.js` | `ui-activity-log-page.css`, `ui-activity-log-controls.css` |
| Details -> Files | `app.js` | `app.js` plus `ui-detail-candidates.js` (per-file), `ui-group-candidates.js` (transfer-level group launcher in the section header) | `ui-transfer-contract.css`, `ui-detail-files.css`, `ui-detail-candidates.css`, `ui-group-candidates.css` |
| Statistics | generated page | `ui-statistics.js` | `ui-statistics-page.css` |
| Settings | generated clean-room markup | `ui-settings-page.js` plus named subfeature owners | Settings styles plus `ui-settings-archive-passwords.css` |
| Help | generated page | `ui-help-page.js` and legal-document helper | Help styles |
| Accessibility/dropdowns | owner-page markup | `ui-accessibility-runtime.js` projection only | shared accessibility/dropdown styles |
| Toasts | shell target | `operator-title.js` presenter; `ui-toast-contract.js` public bridge | `ui-toast-contract.css` |

## CSS composition

`style-v11.css` is the canonical import graph. Correction-named Batch-1 styles are absent. Accepted geometry is assigned to explicit component owners: provider summary, Dashboard transfer presentation, Downloads presentation, Activity Log controls, archive-password editor, and Details file-status geometry.

`style.css` remains the accepted baseline dependency and `style-v11.css` retains its established URL. Multiple stylesheets are legitimate only when their responsibilities are intentionally different.

`ui-detail-candidates.css` is the single bounded owner of candidate disclosure/panel styling and is loaded through the `style-v11.css` `@import` graph (not runtime-injected). The multi-source candidate chip has one shared visual contract: `.dp-candidate-chip` material and geometry live in `ui-transfer-contract.css` (which already owns the reusable transfer/provider row contract); surface files add only bounded layout differences — Details interactive hover/focus in `ui-detail-candidates.css`, constrained Downloads-row geometry in `ui-downloads-presentation.css`. The Details per-file disclosure and the transfer-level group launcher (`DPGroupCandidates.launcherMarkup()`, reused verbatim by Downloads and Dashboard Recent Items) are both interactive `<button>` elements sharing that chip family; the group launcher's interactive treatment and the chooser popover rows — including the muted no-action row style for a common host that is not currently actionable — live in `ui-group-candidates.css` (imported once through the `style-v11.css` graph), never a second base definition of `.dp-candidate-chip`. The chooser reuses the canonical body-level `.dp-dropdown-menu` shell. The Lucide `network` glyph geometry has exactly one owner, the `LUCIDE` set in `operator-title.js`, rendered through `window.DPIcons.svg('network', …)`.

## Accessibility and cross-cutting runtime

`ui-accessibility-runtime.js` remains a deliberately narrow cross-cutting module for ARIA, keyboard behavior, and universal select/dropdown projection across dynamically rendered controls. Its `MutationObserver` is not a general presentation-repair mechanism.

`operator-title.js` owns canonical icon/toast geometry and is not a runtime loader or DOM-repair layer.

## Permanent qualification

Permanent CI uses Browser Runtime as the real-browser smoke contract for all six canonical navigation surfaces. Static architecture tests additionally prove correction-named runtime/style assets and legacy correction globals are absent; bounded owners contain their declared behavior and do not reclaim unrelated surfaces; the canonical CSS graph references only current owners; and the retired presentation-loader/finalization dependencies remain absent.

`ui-runtime.js` and `ui-downloads-runtime.js` are physically absent and must not be reintroduced as a corrective mechanism.

Browser Runtime validates load, six-surface navigation/reload, theme behavior, auth/error presentation, Dashboard/Downloads/Activity/Settings contracts, and absence of requests for retired correction assets.

## Change rule

New frontend work modifies the canonical owner or introduces a genuinely scoped owner. It must not add a new correction stylesheet generation, broad post-render patch script, presentation-loader/finalization dependency, or compatibility global whose purpose is to undo another current owner.
