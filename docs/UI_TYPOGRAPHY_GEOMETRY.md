# DebridPulse v1.0.11 Typography and Geometry

This document defines the typography and global geometry targets for the v1.0.11 UI overhaul. The values are reconstructed from the approved dark/light mockups with the explicit goal of approximately 95% visual consistency at normal desktop sizes. They are expected to be tuned only after a real test build exposes a clear mismatch.

## Typography

DebridPulse keeps the existing type families:

- **Outfit** for the application UI.
- **JetBrains Mono** for hashes, IDs, versions, transfer metadata, speeds/sizes where fixed-width numerics improve scanability, and other technical strings.

The mockups use a compact hierarchy rather than large marketing-style typography. The primary roles are:

| Role | Size | Weight | Notes |
| --- | ---: | ---: | --- |
| Page title | 30 px | 700 | Slight negative tracking, compact 1.12 line-height |
| Page subtitle | 13 px | 400 | Muted secondary copy |
| Section title | 18 px | 600 | Slight negative tracking |
| Card title | 14 px | 600 | Default panel heading |
| Body | 14 px | 400 | 1.5 line-height |
| Small body | 13 px | 400 | Dense descriptions/tables |
| Caption | 11 px | 400–600 | Timestamps/metadata |
| Controls | 13 px | 600 | Buttons, tabs, compact actions |
| Sidebar nav | 14 px | 500 | Active state 600 |
| Sidebar group | 10 px | 700 | Uppercase, 0.14em tracking |
| Table header | 11 px | 700 | Uppercase, 0.06em tracking |
| Table body | 13 px | 400 | 1.35 line-height |
| Metric value | 28 px | 800 | Tabular numerics |
| Metric label | 10 px | 700 | Uppercase, 0.08em tracking |
| Secondary KPI value | 18 px | 800 | Compact historical metrics |
| Badge | 10 px | 700 | Slight tracking |
| Monospace technical text | 12 px | 500 | Tabular numerics |
| Version text | 10 px | 500 | Monospace |

### Typography rules

- Do not enlarge page titles beyond the mockup hierarchy merely to create emphasis; the UI is operational, not promotional.
- Use `font-variant-numeric: tabular-nums` for continuously changing numeric values such as transfer speeds, progress, sizes, counts, and KPI values.
- Uppercase micro-labels are reserved for compact metadata roles: sidebar groups, metric labels, table headers, and badges.
- Filenames remain proportional Outfit text unless a technical identifier is being shown beneath them.
- Hashes, raw IDs, versions, host/source diagnostics, and similarly technical strings use JetBrains Mono.
- Single-line filenames and identifiers truncate with ellipsis; the full value should remain available through the relevant details view or tooltip where appropriate.

## Global geometry

The approved mockups read as a compact desktop application with a roughly 250 px navigation rail, modest page gutters, 12 px cards, and 36–42 px controls. The canonical values are therefore:

### Shell

| Geometry | Value |
| --- | ---: |
| Sidebar width | 252 px |
| Collapsed sidebar target | 76 px |
| Sidebar brand block | 78 px |
| Top operational bar | 64 px |
| Horizontal page gutter | 28 px |
| Vertical page gutter | 24 px |
| Maximum content width | 1760 px |

`1760px` is effectively uncapped at the approved 1920px desktop mockup once the sidebar and page gutters are accounted for, while preventing the application from becoming excessively stretched on ultrawide monitors.

### Repeated spacing

The base spacing scale is:

`4, 6, 8, 10, 12, 14, 16, 20, 24, 28, 32, 40, 48px`

Primary layout roles:

| Role | Value |
| --- | ---: |
| Section gap | 18 px |
| Card/grid gap | 14 px |
| Standard card padding | 18 px |
| Large card padding | 20 px |
| Compact card padding | 14 px |

### Shape language

| Radius | Value | Typical use |
| --- | ---: | --- |
| XS | 6 px | tiny controls/chips |
| SM | 8 px | buttons, inputs |
| MD | 10 px | icon frames, compact panels |
| LG | 12 px | normal cards/panels |
| XL | 16 px | modal/hero surfaces where required |
| Pill | 999 px | badges/pills only |

Avoid mixing arbitrary radii. The 12px card / 8px control relationship is the normal application rhythm.

### Controls and data presentation

| Element | Value |
| --- | ---: |
| Small control | 30 px |
| Standard control | 36 px |
| Large control | 42 px |
| Standard input | 42 px |
| Tabs | 38 px |
| Table header | 38 px |
| Table row | 52 px |
| Primary metric card minimum | 96 px |
| Secondary KPI strip minimum | 62 px |
| Progress bar | 6 px |
| Compact progress line | 3 px |

The mockups are dense enough that 44–48px generic enterprise controls would look oversized. Forty-two pixels is reserved for primary inputs/large controls; routine buttons remain 36px.

### Overlay targets

- Small modal: 420 px
- Medium modal: 620 px
- Large modal: 860 px
- Right-side details drawer: 520 px

These are implementation defaults, not rigid limits; transfer details may use the large modal or drawer treatment depending on the final interaction decision.

## Responsive decisions not visible in the mockups

The references are desktop-first, so responsive behavior is a sane extension rather than something recoverable from the screenshots. Use these breakpoints when page-specific implementation begins:

- **1440 px and above:** full desktop composition.
- **1180–1439 px:** compact desktop; preserve sidebar and reduce/wrap wide card groups.
- **900–1179 px:** tablet/narrow desktop; sidebar may collapse to the 76 px icon rail or drawer depending on final shell implementation.
- **700–899 px:** narrow tablet; data tables progressively hide low-priority columns or scroll.
- **Below 700 px:** mobile drawer navigation and stacked content.

Breakpoints should be encoded directly in media queries; CSS custom properties are not used as media-query conditions.

## Mockup fidelity rules

- Preserve deliberate whitespace. Do not fill open dashboard areas simply because space exists.
- Card density should come from compact typography and spacing, not tiny hit targets.
- The Dashboard top metric row is expected to remain a single row on the approved desktop target, with historical/secondary metrics moved to Statistics as previously decided.
- White/light surfaces need enough separation to survive real displays, but do not exaggerate shadows beyond the design-token contract.
- Avoid page-local geometry unless a mockup or functional requirement clearly demands it. Repeated components should consume these tokens.
- Test-build comparison against the mockups is authoritative for small adjustments; these values are the initial implementation baseline, not claims about inaccessible original design-source measurements.

## Implementation

Canonical token definitions live in `frontend/static/ui-foundation.css`.

The stylesheet intentionally defines both semantic tokens and a small set of type-role helpers. It is not globally activated against all legacy v1.0.10 markup yet; the v1.0.11 component/shell migration should load and consume it as those surfaces are rebuilt. Legacy aliases for `--font`, `--mono`, `--radius`, `--radius-sm`, `--sidebar`, and `--chrome-header-height` are included to ease that migration.
