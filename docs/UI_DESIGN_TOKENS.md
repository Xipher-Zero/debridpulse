# DebridPulse v1.0.11 Design Token Contract

This document is the visual source of truth for color, surfaces, semantic states, shadows, glows, and gradient use in the v1.0.11 interface. The canonical CSS values live in `frontend/static/design-tokens.css`.

## Reconstruction policy

The original design references are flattened raster images, so exact source hex values, blur radii, and opacity recipes cannot be recovered with certainty. The values in `design-tokens.css` are deliberate reconstructions chosen to preserve visible relationships across the accepted dark/light UI rather than literal samples from individual antialiased pixels.

When implementation differs from the accepted UI, prefer the token contract unless browser comparison demonstrates that a token needs global adjustment. Do not fix palette mismatches by adding one-off page-local colors.

## Core visual language

### Dark theme

- Near-black navy application canvas rather than pure black.
- Sidebar remains slightly differentiated from the main canvas without becoming a separate gray slab.
- Cards use dark blue/navy surfaces with subtle borders and restrained elevation.
- Primary text is cool white; secondary and metadata text use blue-lavender slate.
- Purple is the DebridPulse identity/accent color.
- Blue supports active/download concepts and forms the cool end of primary gradients.
- Cyan is reserved for connectivity/external-control semantics.
- Semantic green, amber, and red remain immediately legible on dark surfaces.

### Light theme

- Light mode is a complementary design, not an inversion of dark mode.
- Application canvas is very pale cool gray/blue.
- Primary cards remain white or nearly white.
- Surface separation comes from subtle borders plus restrained shadow rather than broad blue tinting.
- Text remains dark navy instead of black.
- Purple remains the primary identity/accent color.
- Semantic colors are adjusted for contrast on light surfaces.

The light-theme card shadow is intentionally strong enough to preserve card boundaries on ordinary displays.

## Semantic state mapping

| State | Token | Meaning |
|---|---|---|
| Success | `--dp-state-success` | Completed, healthy, successful |
| Active | `--dp-state-active` | Downloading, active file/network work |
| Processing | `--dp-state-processing` | Processing/extraction/in-progress post-work |
| Caution | `--dp-state-caution` | Warning, degraded success, **Completed With Errors** |
| Error | `--dp-state-error` | Actual failed/unrecoverable operation |
| Connectivity | `--dp-state-connectivity` | External control/network connectivity |
| Paused | `--dp-state-paused` | User/system paused; not an error |
| Ready | `--dp-state-ready` | Queued/ready/standby neutral state |
| Recoverable | `--dp-state-recoverable` | Recover/retry opportunity; caution family |

### Hard semantic rule

**Completed With Errors is amber/yellow, not red.** Red is reserved for genuine failure/destructive semantics. Routine duplicate, failover, or informational events must not be colored red merely because they are unusual.

## Brand accents and gradients

The accepted design uses a purple-to-electric-blue identity gradient. It is intentionally limited.

Gradient is appropriate for primary CTAs, brand/pulse accents, selected high-emphasis decorative treatments, and the login waveform language where called for by the accepted UI.

Gradient should generally not be applied to every card, routine secondary buttons, table rows, semantic status badges, or ordinary Lucide navigation glyphs.

## Navigation states

- Normal navigation is neutral/slate.
- Hover receives a very low-opacity purple tint.
- Selected navigation uses the purple/blue family with a bright purple left rail and mild glow/tint.
- Selected navigation remains visually strong in both themes without becoming a solid neon block.
- The theme toggle is globally accessible in the upper-right application chrome.

## Focus, hover, pressed, and disabled states

- **Focus:** accessible purple ring derived from `--dp-focus-ring`.
- **Hover:** slight surface/border lift or accent strengthening; no large movement.
- **Pressed:** subtle compression is acceptable for buttons.
- **Disabled:** lower contrast/opacity while preserving readable labels.
- **Reduced motion:** honor `prefers-reduced-motion`; interaction meaning must not depend on animation.

## Motion policy

Decorative pulse/waveform artwork is static by default. Motion is reserved for meaningful state changes, progress, short interaction feedback, or loading.

## Shadows and glows

Dark theme uses border/surface contrast first and shadow second. Purple glow is restrained and reserved for selected/brand emphasis.

Light theme uses enough elevation for card boundaries to survive ordinary display conditions. Do not add heavy gray drop shadows around every surface.

Semantic glows exist for icon/status emphasis but remain low opacity.

## Progress language

- Active → blue.
- Complete → green.
- Paused → purple.
- Processing/extracting → amber.
- Error → red.
- Track → subdued theme-appropriate neutral.

Unknown/indeterminate progress may animate at the component layer, but semantic color remains tied to current state.

## Surface hierarchy

Use semantic surface tokens rather than arbitrary background values:

- `--dp-bg-app` / `--dp-bg-app-alt`: application canvas.
- `--dp-bg-sidebar`: navigation shell.
- `--dp-surface-1`: normal cards/panels.
- `--dp-surface-2`: raised/secondary panels.
- `--dp-surface-3`: inset controls/tertiary surface.
- `--dp-surface-input`: text fields and search controls.
- `--dp-surface-overlay`: dialogs/popovers/dropdowns.

Likewise use the `--dp-border-*` and `--dp-text-*` hierarchy rather than selecting literal colors locally.

## Release-state compatibility

The application still contains inherited markup and the canonical `style.css` compatibility stylesheet. `design-tokens.css` supplies compatibility aliases such as `--bg`, `--surface`, `--accent`, `--green`, and `--red` because live inherited selectors still consume them.

For v1.0.11 maintenance:

1. `style.css` remains the single compatibility stylesheet loaded before `style-v11.css`.
2. `design-tokens.css` remains authoritative for shared semantic values.
3. New or deliberately rebuilt components use `--dp-*` tokens directly.
4. Page-local literal colors are removed when ownership is intentionally refactored, not through speculative release cleanup.
5. Compatibility aliases are removed only after a repository-wide live-usage audit proves no accepted runtime depends on them.
6. The retired `style-legacy.css` duplicate must not be reintroduced; it was byte-identical to `style.css` and unreachable from the production bootstrap graph.

## Relationship to icon system

`frontend/static/icon-system.css` derives semantic frames from these token colors. Custom SVG artwork can contain its approved internal gradients, but UI-created frames, borders, glows, and Lucide colors use the shared token language.

See `docs/UI_ICON_SYSTEM.md` for the custom-vs-Lucide boundary and true-vector SVG requirements.

## Visual acceptance

The consolidated pre-release candidate for `1.0.11` was browser-compared with the accepted pre-consolidation local build and found visually/behaviorally equivalent from the user perspective. That comparison validates the current token/cascade output; it does not make live calibration layers dead source.

When future maintenance changes a shared token or folds a live calibration layer, repeat browser validation in both themes and relevant responsive layouts before accepting the new candidate boundary.
