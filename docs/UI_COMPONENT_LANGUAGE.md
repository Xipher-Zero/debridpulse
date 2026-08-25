# DebridPulse v1.0.11 Component Language

This document defines the reusable UI component language for the v1.0.11 overhaul. The goal is to reproduce the approved dark/light mockups closely while avoiding page-local styling drift. These primitives are presentation-only and do not change backend lifecycle semantics.

## General rule

Rebuilt v1.0.11 surfaces should use the `dp-*` component classes in `frontend/static/ui-components.css` together with the semantic tokens in `design-tokens.css`, geometry/type tokens in `ui-foundation.css`, and the icon rules in `icon-system.css`.

Do not globally restyle legacy markup merely to make old pages look partially new. Activate these primitives as each shell/page is deliberately migrated.

## Buttons

Four normal roles are canonical:

- **Primary**: purple-to-blue gradient, reserved for the page's dominant action such as Add, Save, Sign In, Apply, or a similarly clear commit action.
- **Secondary**: neutral raised surface with normal border; used for common actions that should remain visible without competing with the primary CTA.
- **Ghost**: transparent/quiet action used for low-priority controls in headers and compact toolbars.
- **Danger / Caution**: tinted semantic controls. Red means destructive or genuine failure-related action; amber means caution/degraded-success/recovery.

Routine controls are 36px high. Small compact controls are 30px; large CTA/input-aligned controls are 42px. Icon-only buttons use the same 36px square geometry.

Do not put multiple gradient-primary buttons beside each other unless the workflow genuinely has multiple equally dominant commit actions.

## Fields

Inputs use the 42px geometry from the foundation contract, an 8px radius, dark inset surface / near-white light surface, and restrained border transitions.

- Labels: 12px semibold secondary text.
- Help/error copy: 11px.
- Placeholder text is muted and not used as the only field label.
- Invalid fields use the red semantic border but must also expose textual/accessible error state.
- Textareas resize vertically. Multi-link input may grow to its separate functional maximum, but starts at the normal input height.
- Search/password/icon-adorned fields use `.dp-input-wrap` so icon placement remains consistent.

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

Routine duplicate/failover/operational-note events should use neutral informational presentation rather than error styling unless the backend result is actually an error.

## Progress

Standard progress bars are 6px; compact row progress is 3px. State mapping:

- active: blue
- complete: green
- paused: purple
- processing: amber
- error: red

Unknown-duration work may use indeterminate animation. `prefers-reduced-motion` disables that motion.

## Cards

Normal cards use:

- 12px radius
- semantic surface gradient
- normal border
- restrained tokenized shadow
- 18px body padding
- 50px header target

The mockups derive hierarchy from surface/border/spacing more than large shadows. Avoid floating every panel aggressively.

Section/card heading icons use the custom DP semantic icon system when the mockup shows one; ordinary controls use Lucide.

## Tables

Tables preserve the compact operational feel:

- 38px header
- 52px row
- 11px uppercase headers
- 13px body
- subtle row dividers
- very restrained hover tint
- selected rows use the shared purple selection tint

Filenames remain proportional text and truncate to one line where necessary. Supporting metadata may use a smaller muted line. Numeric columns use tabular figures and right alignment where it helps scanning.

Responsive pages may hide low-priority columns or horizontally scroll; do not crush important content into unreadable widths.

## Pagination

Pagination controls are compact 30px squares/segments. The current page uses the selected purple surface treatment. Pagination should remain visually subordinate to the data itself.

## Tooltips

Tooltips are allowed for icon-only controls and truncated values. They use an elevated semantic overlay, 11px text, and should stay short. Tooltips are supplemental; critical instructions/statuses cannot exist only in hover content.

## Dialogs and drawers

Dialogs use the previously defined 420 / 620 / 860px target widths. Default is 620px. Transfer details may use the 520px right-side drawer or the large dialog depending on the final page interaction choice.

Dialogs and drawers use strong elevation only because they intentionally sit above the application shell. Focus trapping, Escape behavior, and accessible names are implementation requirements when the JS behavior is added.

## Toasts

Toasts live in the lower-right on desktop and use a thin semantic rail rather than a full saturated colored block. Routine successful operations should not spam toasts. Use them when the user needs confirmation or when an operation has a meaningful warning/error outcome.

Recommended semantics:

- success: green rail
- caution: amber rail
- error: red rail
- informational/default: purple rail

## Loading and empty states

Skeletons are neutral surfaces, not bright animated gradients. Empty states remain compact and operational rather than marketing-style illustrations. Reduced-motion mode disables shimmer.

## Accessibility and interaction

- Every interactive primitive has a visible `:focus-visible` treatment using the DP focus-ring token.
- Icon-only controls need an accessible name.
- Statuses need text/accessible labels in addition to color/icon treatment.
- Disabled controls must be visibly disabled and non-interactive.
- `prefers-reduced-motion` disables nonessential transitions/animations.
- Native semantics should be preferred over ARIA recreation when practical.

## Mockup fidelity decisions made where screenshots are silent

The mockups do not reveal hover, pressed, focus, disabled, loading, modal, toast, or reduced-motion states. The initial implementation therefore uses conservative extensions of the visible design language:

- hover = modest surface/border lift, not large glow
- pressed = 1px visual compression
- focus = purple 3px ring
- disabled = reduced opacity/desaturation
- modal backdrop = dark translucent blur; lighter neutral overlay in light theme
- toast = neutral panel with semantic rail
- motion = short and functional only

These are baseline decisions for the first test build. They should be tuned only when real use or direct mockup comparison exposes a mismatch.

## Implementation order

After this component contract, the next implementation target is the application shell: sidebar, brand block, navigation, top operational strip, page heading, theme toggle, and shared content frame. The shell should consume these primitives rather than introducing a second button/tab/badge/card language.
