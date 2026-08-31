# DebridPulse v1.0.11 Component Language

This document defines the reusable UI component language for the v1.0.11 interface. These primitives are presentation-only and do not change backend lifecycle semantics.

## General rule

Rebuilt v1.0.11 surfaces use the `dp-*` component classes in `frontend/static/ui-components.css` together with the semantic tokens in `design-tokens.css`, geometry/type tokens in `ui-foundation.css`, and the icon rules in `icon-system.css`.

Inherited markup remains where replacing it would create unnecessary behavioral risk. Shared compatibility styling may normalize that markup, but new presentation work should not introduce additional legacy class families.

## Buttons

Four normal roles are canonical:

- **Primary**: purple-to-blue gradient, reserved for the page's dominant commit action such as Add, Save, Sign In, or Apply.
- **Secondary**: neutral raised surface with normal border; used for common actions that should remain visible without competing with the primary CTA.
- **Ghost**: transparent/quiet action used for low-priority controls in headers and compact toolbars.
- **Danger / Caution**: tinted semantic controls. Red means destructive or genuine failure-related action; amber means caution, degraded success, or recovery.

Routine controls are 36px high. Small compact controls are 30px; large CTA/input-aligned controls are 42px. Icon-only buttons use the same 36px square geometry.

Do not put multiple gradient-primary buttons beside each other unless the workflow genuinely has multiple equally dominant commit actions.

## Fields

Inputs use the 42px geometry from the foundation contract, an 8px radius, dark inset surface / near-white light surface, and restrained border transitions.

- Labels: 12px semibold secondary text.
- Help/error copy: 11px.
- Placeholder text is muted and is not the only field label.
- Invalid fields use the red semantic border and expose textual/accessible error state.
- Textareas resize vertically where the interaction permits it.
- Search/password/icon-adorned fields use `.dp-input-wrap` where that shared wrapper owns the geometry.

## Checkboxes, radios, toggles

Native checkbox/radio controls retain native semantics and use the DP accent color. Binary settings toggles use the compact 38x22 treatment with a purple selected state.

Color is not sufficient to convey state; labels and accessible state attributes remain required.

## Tabs

Tabs use a quiet inset container, 38px total height, and a restrained purple selected surface. This applies to Settings, Help/License, Downloads filters where the interaction is genuinely tab-like, and similar segmented controls.

Active state is indicated through both color and background treatment. Do not use large filled-gradient tabs.

## Badges and status pills

Status pills are deliberately compact and semantic:

- Done / success: green.
- Active/downloading: blue.
- Processing/extracting: amber.
- Completed With Errors / caution: yellow-amber.
- Error: red.
- Paused: purple.
- Ready / queued: neutral blue-slate.
- Connectivity/external control: cyan.

The error/caution split is mandatory. `Completed With Errors` is not a red error state.

Routine duplicate/failover/operational-note events use neutral informational presentation unless the backend result is actually an error.

## Progress

Standard progress bars are 6px; compact row progress is 3px. State mapping:

- active: blue
- complete: green
- paused: purple
- processing: amber
- error: red

Unknown-duration work may use indeterminate animation. `prefers-reduced-motion` disables nonessential motion.

## Cards

Normal cards use:

- 12px radius
- semantic surface gradient
- normal border
- restrained tokenized shadow
- 18px body padding
- 50px header target

Hierarchy comes from surface, border, and spacing more than large shadows. Avoid floating every panel aggressively.

Section/card heading icons use the custom DP semantic icon system when the approved UI assigns one; ordinary controls use Lucide.

## Tables

Tables preserve the compact operational feel:

- 38px header
- 52px row
- 11px uppercase headers
- 13px body
- subtle row dividers
- restrained hover tint
- selected rows use the shared purple selection tint

Filenames remain proportional text and truncate to one line where necessary. Supporting metadata may use a smaller muted line. Numeric columns use tabular figures and right alignment where it helps scanning.

Responsive pages may hide low-priority columns or horizontally scroll; do not crush important content into unreadable widths.

## Pagination

Pagination controls are compact 30px squares/segments. The current page uses the selected purple surface treatment. Pagination remains visually subordinate to the data itself.

## Tooltips

Tooltips are allowed for icon-only controls and truncated values. They use an elevated semantic overlay and short copy. Tooltips are supplemental; critical instructions/statuses cannot exist only in hover content.

## Dialogs and drawers

Dialogs use the established small/normal/large sizing hierarchy. Focus trapping, Escape behavior, and accessible names are required for interactive dialogs. Destructive Settings confirmations use the first-party modal contract rather than browser `confirm()`/`prompt()` UI.

## Toasts

Toasts use a neutral panel with a thin semantic rail rather than a saturated colored block. Routine successful operations should not spam toasts.

- success: green rail
- caution: amber rail
- error: red rail
- informational/default: purple rail

## Loading and empty states

Skeletons are neutral surfaces, not bright animated gradients. Empty states remain compact and operational rather than marketing-style illustrations. Reduced-motion mode disables shimmer.

## Accessibility and interaction

- Every interactive primitive has a visible `:focus-visible` treatment using the DP focus-ring token.
- Icon-only controls have an accessible name.
- Statuses use text/accessible labels in addition to color/icon treatment.
- Disabled controls are visibly disabled and non-interactive.
- `prefers-reduced-motion` disables nonessential transitions/animations.
- Native semantics are preferred over ARIA recreation when practical.

## Interaction behavior where static mockups are silent

The approved static references do not directly specify every hover, pressed, focus, disabled, loading, modal, toast, or reduced-motion state. DebridPulse therefore standardizes conservative extensions of the visible design language:

- hover = modest surface/border lift, not large glow
- pressed = subtle compression where useful
- focus = visible purple focus ring
- disabled = reduced emphasis while preserving readable labels
- modal backdrop = restrained translucent overlay appropriate to the active theme
- toast = neutral panel with semantic rail
- motion = short and functional only

## Release-state implementation

The shell and all v1.0.11 pages now consume this language through the canonical shared sources plus explicitly documented live compatibility/calibration layers. `style-v11.css` is the stylesheet import root and `ui-presentation-loader.js` owns deterministic post-core presentation runtime sequencing.

The browser-validated 1.0.11 pre-release output is the acceptance boundary. Cleanup may remove unreachable duplicates, but a live layer is not deleted or reordered solely because its filename reflects an earlier implementation batch. Consolidating live calibration into a canonical owner is a separate behavior-preserving refactor and requires renewed qualification and browser comparison.
