# DebridPulse UI Component Language

This document defines the shared component language used by the DebridPulse v1.0.11.1 frontend. It complements `UI_DESIGN_TOKENS.md` and `UI_FRONTEND_ARCHITECTURE.md`: tokens define values, this document defines reusable visual/interaction families, and the architecture document defines source ownership.

## Governing rule

Dashboard is the reference surface for shared visual language. A page may vary geometry or content where its function requires it, but common cards, controls, state semantics, typography, focus behavior, modal behavior, and interaction affordances should read as one application.

Stable presentation belongs in canonical CSS owners. Runtime code may update state/data and deliberate dynamic render roots; it should not reconstruct presentation layers after load.

## Naming and ownership

New or deliberately rebuilt markup uses `dp-*` component classes where a shared component abstraction exists. Existing inherited markup may continue to use historical class names when rebuilding it would add churn without product value; `ui-universal-language.css` and shared contracts bridge those supported surfaces onto the current language.

Compatibility is not a license to create new legacy-class markup. Prefer the canonical component family for new work.

## Surface hierarchy

The visual hierarchy is intentionally shallow:

1. application background/shell;
2. primary page workspace or master card;
3. secondary cards/panels inside that workspace;
4. controls and data rows;
5. transient overlays such as menus, tooltips, toasts, and modals.

Nested surfaces should not accumulate borders and shadows merely because a legacy implementation did so. Use spacing, tone, and typography to separate structure before adding another visible frame.

## Cards and panels

Shared cards use the application surface/border/radius/elevation language from the token system. The exact component family depends on purpose:

- master/workspace cards define the primary page region;
- list/workspace surfaces hold tables, logs, or long dynamic content;
- metric/KPI cards emphasize a value plus concise explanatory copy;
- settings cards group one coherent configuration concern;
- modal cards temporarily own focus and action priority.

Page-specific CSS may change grid span, min/max size, or responsive arrangement. It should not redefine the shared border/radius/elevation language without a documented reason.

## Typography

Use the shared typography tokens and existing hierarchy rather than local arbitrary sizes.

- page title: highest application-page hierarchy;
- page subtitle/flavor text: concise contextual explanation, visually subordinate;
- card title: clear local hierarchy;
- field/metric label: concise and scannable;
- secondary/flavor text: lower contrast but still readable;
- monospace: identifiers, paths, hashes, URLs, versions, and similar machine-oriented values.

All-caps micro-labels are reserved for compact KPI/status language where the established design uses them; they are not a default heading style.

## Buttons and utility controls

Buttons use the canonical button families and shared state behavior. Action prominence follows consequence and frequency:

- primary: the main affirmative action in the local context;
- ghost/secondary: normal utility actions;
- danger: destructive or high-consequence actions;
- icon/utility controls: compact shell/table actions with an accessible name.

Do not encode important state solely by color. Disabled controls remain readable and visibly unavailable. Loading state should preserve control geometry to avoid layout jumps.

## Inputs and forms

Text inputs, password fields, selects/dropdowns, text areas, toggles, and checkbox/radio controls use shared field geometry and focus semantics. Page owners may set widths or responsive placement; shared controls retain their canonical interior padding, border, focus ring, disabled treatment, and theme behavior.

Settings uses the established title/control/flavor-text relationship. Labels and explanatory copy should align with the actual control rather than with unrelated card geometry.

## Dropdowns

Application dropdowns use the shared dropdown contract rather than browser/legacy per-page styling where the product owns the menu. The trigger determines the menu width/alignment unless the component has a deliberate different contract. Menus remain within the viewport at responsive sizes and use the current theme tokens.

## Status and semantic color

Status color is semantic rather than decorative. Shared state colors cover at least:

- success/completed;
- active/informational;
- caution/degraded/completed-with-errors;
- error/failed;
- paused/inactive/neutral.

The same semantic state should use the same family across badges, progress accents, event points, rails, and icon frames unless the component requires a lower-emphasis variant.

## Icons

Use the canonical icon system described by `UI_ICON_SYSTEM.md`.

- DebridPulse-specific artwork is used where product identity or a domain-specific symbol matters;
- Lucide is preferred for ordinary navigation/utility semantics;
- UI-created frames, borders, and glows derive from shared tokens;
- icons that are purely decorative are hidden from assistive technology;
- icon-only actions require an accessible label/title appropriate to the control.

## Tables and list workspaces

Tables and long lists share row density, header hierarchy, hover/focus behavior, semantic state treatment, and scrollbar language. Page owners may control column widths or special responsive behavior, but generic table styling should not be duplicated in page-specific corrective layers.

Bulk-selection/action surfaces belong in the canonical page composition rather than being relocated after load by a finalization runtime.

## KPI and sparkline language

Dashboard KPI cards establish the shared metric language: prominent value, fully readable label/copy, semantic icon treatment, and quiet contextual sparkline where meaningful. Statistics may use denser analytical variants while preserving shared typography and semantic colors.

Sparklines are supporting context, not the sole representation of a value or state. Their runtime only updates data geometry; the card structure and presentation remain source-owned.

## Modals and transient overlays

Modals must:

- trap focus while open;
- expose a meaningful dialog name;
- support keyboard dismissal where safe;
- return focus to the invoking control on close;
- keep destructive confirmation explicit;
- use the canonical modal/backdrop contract.

Menus, toasts, and other transient surfaces must not become alternate page-layout owners.

## Responsive behavior

Responsive rules should preserve task priority rather than merely shrink desktop geometry. Controls may wrap or stack, tables may become horizontally scrollable, and multi-column cards may collapse. Primary actions and essential state must remain discoverable.

Responsive corrections belong to the owning page or shared component stylesheet, not to a generic late override file.

## Accessibility

The shared minimum contract includes:

- keyboard-reachable interactive controls;
- visible focus indication;
- semantic labels/ARIA state where native semantics are insufficient;
- adequate text contrast in both themes;
- non-color state cues where needed;
- predictable focus lifecycle for dialogs and menus.

`ui-accessibility-runtime.js` may augment supported inherited markup with semantics and keyboard behavior. It must not perform API work or act as a presentation-convergence layer.

## Interaction behavior where static mockups are silent

The approved static references do not directly specify every hover, pressed, focus, disabled, loading, modal, toast, or reduced-motion state. DebridPulse therefore standardizes conservative extensions of the visible design language:

- hover = modest surface/border lift, not large glow;
- pressed = subtle compression where useful;
- focus = visible purple focus ring;
- disabled = reduced emphasis while preserving readable labels;
- modal backdrop = restrained translucent overlay appropriate to the active theme;
- toast = neutral panel with semantic rail;
- motion = short and functional only.

## Release-state implementation

The v1.0.11.1 shell and pages consume this language from the canonical shared sources and direct page owners. `style-v11.css` is the stylesheet import root; canonical JavaScript page/runtime assets are loaded directly by the base document rather than sequenced through a presentation loader.

Historical Dashboard batch/polish rules that remain behaviorally required have already been folded into `ui-dashboard.css`. The old batch files are not live runtime layers. Cleanup may remove unreachable source, but shared compatibility code with a supported consumer remains until a deliberate canonical rewrite replaces it.

Any change to shared tokens, component contracts, or folded presentation rules requires renewed browser validation in both themes and the affected responsive contexts before accepting a new candidate boundary.
