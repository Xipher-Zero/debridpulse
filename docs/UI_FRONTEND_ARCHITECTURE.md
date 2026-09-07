# DebridPulse v1.0.12 Frontend Architecture

This document describes the live post-audit frontend ownership model. It records current owners and boot relationships; it does not describe historical correction layers as though they were still canonical.

The application reports `1.0.12` for the current development tree.

## Core rule

Every visible behavior has one bounded structural/render owner and intentionally composed styling. Broad post-render correction runtimes, correction-named stylesheets, and compatibility globals that replace unrelated page owners are prohibited.

`frontend/static/index.html` owns the static shell and the six reachable navigation surfaces: Dashboard, Downloads, Activity Log, Statistics, Settings, and Help & License. The retired `view-changelog`, `view-aria2queue`, and `view-support` surfaces are absent from the shell.

## Boot graph

`index.html` parser-loads the established application/runtime owners. `ui-provider-status.js`, which executes after `app.js`, starts `ui-presentation-loader.js`. The presentation loader is orchestration-only: it performs no API I/O and no DOM repair. It deterministically loads the bounded presentation owners below and exposes only its immutable manifest for qualification.

The presentation owners are:

- `ui-toast-contract.js` — public toast copy/timing bridge to the canonical `operator-title.js` presenter.
- `ui-dashboard-transfer-presentation.js` — Dashboard Recent Activity row/source presentation.
- `ui-downloads-presentation.js` — Downloads date options, pagination, and measured desktop capacity.
- `ui-processing-presentation.js` — authoritative pause-state projection and scheduler-capacity synchronization.
- `ui-activity-log-runtime.js` — Activity Log server-side filtering, timestamps, and filter-control projection.
- `ui-settings-archive-passwords.js` — Automatic Extraction archive-password editor interaction.

There is no `DPUICorrectionBatch1`, `DPUICorrectionBatch1Final`, or `DPUICorrectionP4Repair` runtime contract in the boot graph.

## Runtime ownership map

| Surface | Structure/render owner | Behavior owner | Styling owner |
| --- | --- | --- | --- |
| Application shell/navigation | `index.html` + `app.js` | `app.js` | shell styles |
| Provider status | shell target + `ui-provider-status.js` | `ui-provider-status.js` | `ui-shell-provider-status.css`, `ui-provider-summary.css` |
| Dashboard | `index.html` + `app.js` | `app.js` plus `ui-dashboard-transfer-presentation.js` | `ui-dashboard.css`, `ui-dashboard-transfer-presentation.css` |
| Downloads | `index.html` + `app.js` | `app.js` plus `ui-downloads-presentation.js` | `ui-downloads-page.css`, `ui-downloads-desktop.css`, `ui-downloads-presentation.css` |
| Processing/topbar projection | shell/app controls | `app.js`, `ui-topbar-concurrency.js`, `ui-processing-presentation.js` | owning shell/page styles |
| Activity Log | `index.html` + runtime rows | `ui-activity-log-runtime.js` | `ui-activity-log-page.css`, `ui-activity-log-controls.css` |
| Details -> Files | `app.js` | `app.js` | `ui-transfer-contract.css`, `ui-detail-files.css` |
| Statistics | generated page | `ui-statistics.js` | `ui-statistics-page.css` |
| Settings | generated clean-room markup | `ui-settings-page.js` plus named subfeature owners | Settings styles plus `ui-settings-archive-passwords.css` |
| Help | generated page | `ui-help-page.js` and legal-document helper | Help styles |
| Accessibility/dropdowns | owner-page markup | `ui-accessibility-runtime.js` projection only | shared accessibility/dropdown styles |
| Toasts | shell target | `operator-title.js` presenter; `ui-toast-contract.js` public bridge | `ui-toast-contract.css` |

## CSS composition

`style-v11.css` is the canonical import graph. Correction-named Batch-1 styles are absent. Accepted geometry is assigned to explicit component owners: provider summary, Dashboard transfer presentation, Downloads presentation, Activity Log controls, archive-password editor, and Details file-status geometry.

`style.css` remains the accepted baseline dependency and `style-v11.css` retains its established URL. Multiple stylesheets are legitimate only when their responsibilities are intentionally different.

## Accessibility and cross-cutting runtime

`ui-accessibility-runtime.js` remains a deliberately narrow cross-cutting module for ARIA, keyboard behavior, and universal select/dropdown projection across dynamically rendered controls. Its `MutationObserver` is not a general presentation-repair mechanism.

`operator-title.js` owns canonical icon/toast geometry and is not a runtime loader or DOM-repair layer.

## Permanent qualification

Static architecture tests must prove correction-named runtime/style assets and legacy correction globals are absent; the presentation loader has unique paths/markers and contains no application API I/O; bounded owners contain their declared behavior and do not reclaim unrelated surfaces; the six canonical views remain the only shell navigation surfaces; and the canonical CSS graph references only current owners.

Browser Runtime remains the real-browser smoke gate for load, six-surface navigation/reload, theme behavior, auth/error presentation, Dashboard/Downloads/Activity/Settings contracts, and absence of requests for retired correction assets.

## Change rule

New frontend work modifies the canonical owner or introduces a genuinely scoped owner. It must not add a new correction stylesheet generation, broad post-render patch script, or compatibility global whose purpose is to undo another current owner.