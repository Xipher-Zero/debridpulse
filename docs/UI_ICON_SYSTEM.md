# DebridPulse UI Icon System

This document is the source of truth for icon use in the v1.0.11 UI overhaul.

## Decision

DebridPulse uses a two-layer icon system:

1. **Custom DebridPulse semantic SVG assets** for visually prominent/status-bearing concepts from the approved mockups.
2. **Lucide** for ordinary navigation, controls, form adornments, and utility actions.

Do not replace custom semantic icons with generic line icons merely for implementation convenience. Do not create custom artwork for ordinary controls that Lucide already covers.

## Hard SVG asset rules

All files under `frontend/static/icons/dp/` must be genuine vector SVGs.

- SVGs may contain vector geometry (`path`, `rect`, `circle`, `polygon`, gradients, masks, filters, etc.).
- SVGs **must not** contain embedded raster images (`<image>`), `data:image/...` payloads, `foreignObject`, scripts, event-handler attributes, or external embedded resources.
- Every icon must have a `viewBox` so CSS can control rendered size independent of source dimensions.
- Generated raster artwork wrapped in an `.svg` container is not an acceptable asset.
- Image generation may be used only as a visual reference. Final SVG deliverables must be real vector geometry.
- Keep custom SVGs as separate external files. Do not paste several custom SVG documents directly into the page DOM: many assets intentionally use simple internal gradient/filter IDs that are safely scoped when loaded as external images but could collide if inlined together.

The regression test `backend/tests/test_ui_icon_assets.py` enforces these rules.

## Rendering tiers

### Tier 1 — DP card icons

Self-contained colorful semantic tiles used where the icon is a major visual element:

- `card-download.svg`
- `card-checkmark.svg`
- `card-play.svg`
- `card-clock.svg`
- `card-error.svg`
- `card-caution.svg`
- `card-disk.svg`
- `card-link.svg`
- `card-document-stack.svg`
- `hierarchy.svg`

Use these as external `<img>` assets. Do not wrap them in another icon tile.

### Tier 2 — DP semantic glyphs

Transparent-background custom assets used for DP-specific or highly branded semantics:

- `broadcast-signal.svg`
- `calendar.svg`
- `calendar-24.svg`
- `calendar-7.svg`
- `clock-outline.svg`
- `crown.svg`
- `cube.svg`
- `document.svg`
- `down-triangle-green.svg`
- `disk-space-guard.svg`
- `heartbeat-outline.svg`
- `heart-health.svg`
- `key.svg`
- `lightning.svg`
- `lock.svg`
- `retry-borderless.svg`
- `retry.svg`
- `chain.svg`
- `test-flask.svg`
- `trash.svg`
- `verified-badge.svg`
- `rocket.svg`

Where the mockup places one of these inside a colored rounded-square surface, the **UI supplies the surface** using `.dp-icon-frame`; the SVG remains transparent.

### Tier 3 — Lucide utility icons

Lucide is the canonical utility/navigation family. Use it for ordinary interface vocabulary, including:

- Dashboard grid
- Downloads
- Event Log/list
- Statistics/bar chart
- Settings/gear
- Help/question circle
- Log out
- Search
- Refresh
- Eye / Eye Off
- User
- Chevrons and arrows
- Overflow menu
- Pause
- Upload/import
- Checkboxes
- Filter/dropdown
- Authentication shield
- Extract/package
- Notifications/bell
- Database
- Wrench/tools
- Book/open book
- Integrations/puzzle
- License/scales
- Save
- Theme sun/moon
- Timestamp clock

Lucide utility glyphs should use `currentColor` so navigation, hover, active, disabled, light-theme, and dark-theme states remain CSS-driven.

DebridPulse is a self-contained server UI; when Lucide assets are introduced, vendor the required distribution/assets locally rather than depending on a runtime CDN. Record the MIT license/attribution in the existing dependency-license surfaces at the same time.

## Semantic color language

- **Purple / lavender:** DebridPulse identity, downloads, links, primary app actions.
- **Green:** completed, successful, healthy.
- **Blue:** active, files, download/network activity.
- **Cyan:** connectivity and externally controlled state.
- **Amber / yellow:** processing, caution, recovery, test/configuration emphasis.
- **Red:** genuine errors and destructive actions.
- **Neutral slate:** ordinary navigation, metadata, and secondary controls.

`card-error.svg` is for actual failure/error states.
`card-caution.svg` is for warning/degraded-success states such as **Completed With Errors**.
Do not use red merely because an operational event is unusual; routine duplicate/failover notes remain subdued informational events.

## Mockup coverage

### Dashboard

- Total Downloads → `card-download.svg`
- Completed → `card-checkmark.svg`
- Active Now → `card-play.svg`
- Processing → `card-clock.svg`
- Errors → `card-error.svg`
- Completed With Errors → `card-caution.svg`
- Total Downloaded → `card-disk.svg`
- Queue Health → `heartbeat-outline.svg`
- Last 24 H → `calendar-24.svg`
- Last 7 Days → `calendar-7.svg`
- Success Rate → `verified-badge.svg`
- Average Duration → `clock-outline.svg`
- Average Size → `cube.svg`
- Quick Add → `card-link.svg`
- Recent Activity → `card-document-stack.svg`
- Recover → `retry-borderless.svg`

### Statistics

- Downloads → `card-download.svg`
- Completed Size → `card-checkmark.svg`
- Completed → `card-play.svg` where matching the approved mockup treatment
- In Progress → `card-clock.svg`
- Success Rate → `heart-health.svg`
- File Status → `document.svg`

### Application/system/settings/help

- Externally Controlled → `broadcast-signal.svg`
- Slot/count down indicator → `down-triangle-green.svg`
- Premium → `crown.svg`
- AllDebrid → `key.svg`
- Stored Secrets → `lock.svg`
- General → `lightning.svg`
- Disk Space Guard → `disk-space-guard.svg`
- Test AllDebrid → `test-flask.svg`
- Link/source → `chain.svg`
- Delete → `trash.svg`
- Help / Quick Start branded rocket → `rocket.svg`

Everything else should default to Lucide unless a later approved mockup establishes a new DP-specific semantic treatment.

## Size and layout contract

Never rely on an SVG file's native `width`/`height` for layout. Use the shared CSS classes in `frontend/static/icon-system.css`.

Standard rendered sizes:

- `dp-icon--xs`: 14 px — tiny metadata/status adornments.
- `dp-icon--sm`: 16 px — compact controls.
- `dp-icon--md`: 20 px — standard buttons/forms/navigation.
- `dp-icon--lg`: 24 px — section/tab emphasis.
- `dp-icon--xl`: 32 px — secondary semantic glyphs.
- `dp-icon--metric`: 48 px — primary metric/card artwork.
- `dp-icon--hero`: 64 px — large branded/login/help use.

A component may override size with `--dp-icon-size` when a mockup requires a specific value, but do not create arbitrary per-screen pixel rules without a visual reason.

## Theme behavior

Transparent Tier-2 glyphs should use CSS-created semantic frames where required so dark/light surfaces can differ without regenerating assets.

The current Tier-1 card assets are dark-theme-oriented self-contained SVGs. During the light-theme parity pass, evaluate them in situ. If they are too dark against the light UI, create **vector-only light derivatives from the existing SVG source**; do not use image generation to create replacements and do not embed raster data.

## Accessibility

- Decorative icon `<img>` elements use `alt=""` and are hidden from assistive semantics.
- Icon-only controls must have an accessible name (`aria-label` or equivalent) on the control.
- When an icon conveys a status, the status must also be present in text/accessible labeling; color alone is never the only signal.
- Do not put redundant prose in both `alt` and adjacent visible text.

## Implementation boundary

This icon system is presentation-only. It must not alter v1.0.10 backend lifecycle/state semantics. UI mapping should translate existing backend states into the approved operator-facing presentation rather than changing backend behavior to simplify icon selection.
