# DebridPulse v1.0.11 Design Token Contract

This document is the visual source of truth for color, surfaces, semantic states, shadows, glows, and gradient use in the v1.0.11 UI overhaul. The target is approximately 95% visual consistency with the approved dark/light mockups while keeping the implementation coherent and maintainable.

The canonical CSS values live in `frontend/static/design-tokens.css`.

## Reconstruction policy

The mockups are flattened raster references, so exact original source hex values, blur radii, and opacity recipes cannot be recovered with certainty. The values in `design-tokens.css` are deliberate reconstructions chosen to preserve the visible relationships across all supplied screens rather than literal samples from individual antialiased pixels.

When implementation differs from the mockup, prefer the token contract unless a visual test demonstrates that a token needs global adjustment. Do not fix palette mismatches by adding one-off page-local colors.

## Core visual language

### Dark theme

- Near-black navy application canvas rather than pure black.
- Sidebar remains slightly differentiated from the main canvas but does not become a separate gray slab.
- Cards use dark blue/navy surfaces with subtle borders and restrained elevation.
- Primary text is cool white; secondary and metadata text use blue-lavender slate.
- Purple is the DebridPulse identity/accent color.
- Blue supports active/download concepts and forms the cool end of primary gradients.
- Cyan is reserved for connectivity/external-control semantics.
- Semantic green, amber, and red are saturated enough to remain immediately legible on dark surfaces.

### Light theme

- Light mode is a complementary design, not an inversion of dark mode.
- Application canvas is very pale cool gray/blue.
- Primary cards remain white or nearly white.
- Surface separation comes from subtle borders plus restrained shadow rather than broad blue tinting.
- Text remains dark navy instead of black.
- Purple remains the primary identity/accent color. It must not be replaced by generic enterprise blue merely because the theme is light.
- Semantic colors are slightly darker than their dark-theme equivalents to maintain contrast on white.

The light-theme card shadow is intentionally a little stronger than the rendered mockup bloom. This is a pragmatic real-display adjustment: the mockup's white-on-white separation is partly created by image-generation bloom and would otherwise disappear on many displays.

## Semantic state mapping

The following meanings are fixed across the application:

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

The approved mockups use a purple-to-electric-blue identity gradient. It is intentionally limited.

Gradient is appropriate for:

- primary CTA buttons;
- DebridPulse brand/pulse accents;
- selected/high-emphasis decorative pulse treatments;
- the login/background waveform language where called for by the mockup.

Gradient should generally not be applied to:

- every card;
- routine secondary buttons;
- table rows;
- status badges whose semantic color already carries meaning;
- ordinary Lucide navigation glyphs.

This restraint is necessary to preserve the mockups' visual hierarchy.

## Navigation states

- Normal navigation is neutral/slate.
- Hover receives a very low-opacity purple tint.
- Selected navigation uses the purple/blue family with a bright purple left rail and mild glow/tint.
- Selected navigation must remain visually strong in both themes without becoming a solid neon block.

The theme toggle is globally accessible in the upper-right application chrome during the shell overhaul. It should not remain a permanent sidebar control simply because the pre-overhaul CSS currently places it there.

## Focus, hover, pressed, and disabled states

These states are not directly shown in the static mockups and are therefore standardized as follows:

- **Focus:** accessible purple ring derived from `--dp-focus-ring`.
- **Hover:** slight surface/border lift or accent strengthening; no large movement.
- **Pressed:** subtle 1 px/downscale feedback is acceptable for buttons, but avoid cartoonish movement.
- **Disabled:** lower contrast/opacity while preserving readable labels.
- **Reduced motion:** honor `prefers-reduced-motion`; interaction meaning must not depend on animation.

## Motion policy

Decorative pulse/waveform artwork is **static by default**. It may be visually energetic, but it should not continuously animate merely because the product is named DebridPulse. Motion is reserved for meaningful state changes, progress, short interaction feedback, or loading.

This keeps the finished application closer to the supplied static mockups and avoids unnecessary visual noise.

## Shadows and glows

Dark theme uses border/surface contrast first and shadow second. Purple glow is restrained and reserved for selected/brand emphasis.

Light theme uses slightly stronger real CSS elevation than the raster references so card boundaries survive ordinary display conditions. Do not add heavy gray drop shadows around every surface.

Semantic glows exist for icon/status emphasis but are intentionally low opacity.

## Progress language

- Active → blue.
- Complete → green.
- Paused → purple.
- Processing/extracting → amber.
- Error → red.
- Track → subdued theme-appropriate neutral.

Unknown/indeterminate progress may animate at the component layer, but the semantic color remains tied to the current state.

## Surface hierarchy

Use the semantic surface tokens rather than arbitrary background values:

- `--dp-bg-app` / `--dp-bg-app-alt`: application canvas.
- `--dp-bg-sidebar`: navigation shell.
- `--dp-surface-1`: normal cards/panels.
- `--dp-surface-2`: raised/secondary panels.
- `--dp-surface-3`: inset controls/tertiary surface.
- `--dp-surface-input`: text fields and search controls.
- `--dp-surface-overlay`: dialogs/popovers/dropdowns.

Likewise use the `--dp-border-*` and `--dp-text-*` hierarchy rather than selecting literal colors locally.

## Compatibility / migration

The current frontend predates the v1.0.11 token architecture and uses legacy variables such as `--bg`, `--surface`, `--accent`, `--green`, and `--red`. `design-tokens.css` supplies compatibility aliases so pages can be migrated incrementally.

During the actual UI overhaul:

1. Load `design-tokens.css` after the legacy stylesheet so its semantic values become authoritative.
2. Migrate new/reworked components to `--dp-*` tokens directly.
3. Remove page-local color literals when the page is rebuilt.
4. Do not break backend state semantics merely to fit presentation categories.
5. After the last page is migrated, remove compatibility aliases only after a repository-wide usage audit.

## Relationship to icon system

`frontend/static/icon-system.css` must derive its semantic frames from these token colors. Custom SVG artwork can contain its own approved internal gradients, but UI-created frames, borders, glows, and Lucide colors must use the shared token language.

See `docs/UI_ICON_SYSTEM.md` for the custom-vs-Lucide boundary and the true-vector SVG requirements.

## Visual acceptance

The final pass is judged against the approved mockups, not against the pre-v1.0.11 UI. Adjust a shared token when the same mismatch appears across several screens. Use component-specific overrides only when the mockup clearly establishes a genuinely different role.
