# DebridPulse v1.0.11 Frontend Architecture

## Governing rule

**Dashboard is the visual reference implementation for the application.**

When a reusable visual decision is approved on Dashboard, the reusable part of that decision belongs in the shared frontend language. Other pages inherit it by default. A page stylesheet may add or override only requirements that are genuinely specific to that page's content, geometry, viewport behavior, or workflow.

A page must never copy Dashboard colors, gradients, shadows, field treatments, table bands, tab materials, button materials, or other reusable styling merely to make itself look consistent.

## Cascade order

The v1.0.11 overlay is intentionally ordered as:

1. `design-tokens.css` — application semantic palette and legacy token aliases.
2. `ui-language-tokens.css` — Dashboard-derived reusable material tokens.
3. `ui-foundation.css` — type, spacing, radius, control and viewport geometry tokens.
4. `ui-components.css` — canonical `dp-*` component primitives.
5. `icon-system.css` — icon geometry and semantic icon rules.
6. `ui-universal-language.css` — authoritative reusable application defaults plus temporary legacy-class bridge selectors.
7. `ui-shared-contract.css` — small audited cross-cutting corrections that belong to shared presentation rather than JavaScript or a page layer.
8. Shell layers — sidebar, topbar, navigation and shared page frame/geometry.
9. Dashboard calibration layers — Dashboard-only composition and approved exceptions.
10. Page layers — page-specific geometry/behavior/content details only.

The universal/shared component language **must load before shell, Dashboard and page layers**. A universal-last cascade guard is prohibited because it prevents legitimate page exceptions and creates specificity/`!important` escalation.

## Bootstrap and cache ownership

The normal application path is parser-owned and deterministic. `index.html` loads the legacy compatibility stylesheet first, then the v1.0.11 overlay, and statically declares the presentation runtimes. `operator-title.js` retains guarded runtime-loader fallbacks only for compatibility; the static script markers make those injectors no-ops during normal application boot.

Theme state is applied by the small synchronous `ui-theme-bootstrap.js` immediately after `<body>` begins and before the visible shell is parsed. This prevents a stored light theme from first rendering as dark and then flipping after deferred JavaScript executes.

Cache generations are owned by the resource that changed. The audited outer overlay is generation 21 and imports the changed `ui-shared-contract.css` at generation 21. Established unchanged sublayers remain at generation 20. This is intentional targeted invalidation; advancing an outer loader does not require gratuitously invalidating every unchanged CSS asset underneath it.

## Shared material ownership

The following are application defaults and must be owned by the shared language:

- application/surface palette and raised panel material
- standard card frame, shadow and section-header wash
- KPI/metric-card material and semantic accent treatment
- field/search/select material
- operational table header, row divider and hover material
- tabs and segmented controls
- primary, secondary, danger and caution button material
- status pills and compact progress treatment
- generic empty-state typography
- typography, radii and spacing defaults
- shared keyboard focus presentation for inherited interactive surfaces

Page CSS may own dimensions and arrangement of those components, but not duplicate their base material. Shell selectors such as `.sidebar-footer`, navigation, topbar and shell datum rules belong to shell layers even when a particular page state is part of the selector.

## Canonical components and migration bridge

New or deliberately rebuilt markup should use the `dp-*` component classes from `ui-components.css`.

The existing application still contains legacy classes and runtime-generated markup. `ui-universal-language.css` therefore bridges these legacy families onto the same visual defaults during migration:

- `.card`, `.scard`, `.list-card` -> standard raised panel
- `.metric-card`, `.stat-card`, `.dash-hero-stat` -> metric/KPI surface
- `.input` -> field
- `.filter-tabs`, `.stabs`, `.ftab`, `.stab` -> segmented tabs
- `.btn` role classes -> canonical action hierarchy
- `.t-table` -> operational table
- `.badge` -> status pill
- `.prog` -> compact progress

The bridge is transitional. As pages are touched for substantive work, their markup should move toward the canonical `dp-*` classes without changing the computed visual result.

## Page-layer rules

A page stylesheet may contain items such as:

- desktop/mobile layout
- flex/grid arrangement
- page-specific min/max heights
- scroll containment and sticky positioning
- column widths
- content-specific icon size/placement
- search-band or toolbar composition
- pagination placement
- responsive hiding/reflow rules
- semantic `--c` assignments for page-specific metrics

A page stylesheet should not contain copied shared material literals such as the standard card gradient, standard lavender table band, standard segmented-tab gradient, standard field background, or standard contact shadow. It also must not own shell selectors merely because the shell changes position while that page is active.

## Downloads migration example

Downloads previously accumulated multiple layers that independently recreated Dashboard card, table, field, tab and control styling. The active v1.0.11 cascade now uses one `ui-downloads-page.css` layer.

That layer owns only the Downloads workspace geometry, sticky table viewport, title/icon sizing, search-band composition, pagination placement, empty-download icon, and responsive layout. Card, field, table, tab, button, badge and progress material is inherited from the universal language. Provider Status alignment and the desktop Downloads bottom datum are shell behavior and therefore live in shell ownership rather than the Downloads page layer.

## Interaction semantics

`ui-accessibility-runtime.js` is a cross-cutting interaction-semantics layer for inherited markup that cannot yet be rebuilt without broader application churn. It may add roles, ARIA state, focusability and keyboard activation while delegating the actual action to the established application handlers. It must not perform API calls, transfer mutations, polling or backend work.

Where a true tablist exists, arrow/Home/End keyboard behavior and `aria-selected` belong to that semantic tablist. Segmented filters that immediately change a dataset are treated as grouped toggle buttons with `aria-pressed`, avoiding tab semantics where no associated tabpanel exists.

## Dashboard calibration files

The existing Dashboard review/batch files remain active because they record the iterative calibration that produced the approved reference page. They are not a template for new page files.

When a Dashboard rule is determined to be generally reusable, it should be promoted into `ui-language-tokens.css` / `ui-universal-language.css` or the small shared-contract layer when appropriate. The Dashboard copy can then eventually be simplified as later cleanup work, provided the approved Dashboard rendering remains unchanged.

## Review checklist for future frontend changes

Before adding a page-local visual rule, answer these questions:

1. Is this visual property already represented on Dashboard or another canonical component?
2. Would the same rule make sense on more than one page?
3. Is this material/appearance, shell behavior, or genuinely page-specific geometry/behavior?
4. Can the requirement be expressed through a shared token or component modifier instead?
5. Does the page still inherit the universal default when the exception is removed?

If the rule is reusable material, it belongs in the shared language. If it is shell behavior, it belongs in a shell layer. If it is page-specific composition, it belongs in the page layer.

## Contract tests

Frontend contract tests should verify ownership, not screenshots encoded as duplicated CSS literals. In particular they should enforce that:

- the v1.0.11 overlay is statically declared on the normal bootstrap path;
- first-paint theme selection occurs before visible shell parsing;
- universal/shared language loads before shell, Dashboard and page layers;
- major legacy component families are bridged to shared defaults;
- page layers do not reproduce known shared material literals or own shell selectors;
- Dashboard calibration remains after the universal base;
- presentation/interaction shims do not introduce backend/API behavior;
- every first-party browser runtime is syntax-checked in CI.

This ownership model is the prerequisite for continuing the v1.0.11 page migrations without turning each visual correction into a cross-page consistency exercise.
