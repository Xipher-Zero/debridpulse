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
7. Shell layers — sidebar, topbar, navigation and shared page frame.
8. Dashboard calibration layers — Dashboard-only composition and approved exceptions.
9. Page layers — page-specific geometry/behavior/content details only.

The universal component language **must load before page layers**. A universal-last cascade guard is prohibited because it prevents legitimate page exceptions and creates specificity/`!important` escalation.

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

Page CSS may own dimensions and arrangement of those components, but not duplicate their base material.

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

A page stylesheet should not contain copied shared material literals such as the standard card gradient, standard lavender table band, standard segmented-tab gradient, standard field background, or standard contact shadow.

## Downloads migration example

Downloads previously accumulated multiple layers that independently recreated Dashboard card, table, field, tab and control styling. The active v1.0.11 cascade now uses one `ui-downloads-page.css` layer.

That layer owns only the Downloads workspace geometry, sticky table viewport, title/icon sizing, search-band composition, pagination placement, empty-download icon, and responsive layout. Card, field, table, tab, button, badge and progress material is inherited from the universal language.

## Dashboard calibration files

The existing Dashboard review/batch files remain active because they record the iterative calibration that produced the approved reference page. They are not a template for new page files.

When a Dashboard rule is determined to be generally reusable, it should be promoted into `ui-language-tokens.css` / `ui-universal-language.css`. The Dashboard copy can then eventually be simplified as later cleanup work, provided the approved Dashboard rendering remains unchanged.

## Review checklist for future frontend changes

Before adding a page-local visual rule, answer these questions:

1. Is this visual property already represented on Dashboard or another canonical component?
2. Would the same rule make sense on more than one page?
3. Is this material/appearance, or is it genuinely page-specific geometry/behavior?
4. Can the requirement be expressed through a shared token or component modifier instead?
5. Does the page still inherit the universal default when the exception is removed?

If the rule is reusable material, it belongs in the shared language. If it is page-specific composition, it belongs in the page layer.

## Contract tests

Frontend contract tests should verify ownership, not screenshots encoded as duplicated CSS literals. In particular they should enforce that:

- universal language loads before Dashboard and page layers;
- major legacy component families are bridged to shared defaults;
- page layers do not reproduce known shared material literals;
- Dashboard calibration remains after the universal base;
- presentation shims do not introduce backend/API behavior.

This ownership model is the prerequisite for continuing the v1.0.11 page migrations without turning each visual correction into a cross-page consistency exercise.
