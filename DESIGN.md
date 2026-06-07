# UMS Smart Revenue Design System

## Overview

Internal product UI for a finance/revenue command center. The design serves complex monthly close workflows, not brand promotion.

## Color

Use the Soft Dark palette (GitHub dark_dimmed / Primer surfaces) with the Anthropic
orange accent — per the UMS Revenue Design System:

- Background: app canvas `#212830`; body gradient stop `#1b212a`.
- Navigation: rail well `#151b23`.
- Surfaces: raised dark panels `#262c36` (subtle `#2a313c`, raised `#2f3742`) with
  hairline borders `#3d444d` (`#656c76` on emphasis).
- Ink is tiered: chrome text `#d1d7e0`; KPI and money values near-white `#f0f6fc`.
- Primary action: ink-toned button surface (light-on-dark contrast pattern).
- Primary accent: Anthropic orange `oklch(66% .15 45)` for brand marks, selected
  states, the focus ring, graph emphasis, and finance highlight states.
- Finance positive: green `#57ab5a` with label support.
- Warning: amber `#c69026` for allocated or unresolved values.
- Critical: red `#e5534b` for missing data or permission blocks.
- Graph/read-model accent: Anthropic orange, used sparingly for graph-related states.

## Typography

Use the redistributable OFL webfonts (shipped in `frontend/public/fonts/`; sources in
`mockups/FontsGH/`):

- Display: Newsreader for page titles, major panel headings, and KPI/money values.
- Body/UI: Mona Sans for navigation, tables, forms, buttons, labels, and supporting text.
- Code-like labels: Monaspace Neon for formulas, identifiers, and chips.
- Variable-font weights are intentional (560/680/720/760 tiers) — table headers and
  labels sit above 700; serif display values stay at 400–600.
- Keep product UI sizes fixed and table text compact. Do not use viewport-scaled typography.

## Layout

Use an app shell with left navigation, top operational controls, dense KPI bands, table-first content, side explainability panel, and bottom workflow rail. Cards are reserved for repeated dashboard modules and must not be nested.

## Components

- Sidebar navigation with selected state.
- Month/currency/scope filters in the top bar.
- KPI tiles with confidence/source/lock metadata.
- Dense channel revenue table with explain actions.
- Issue queue with severity and owner.
- Month-close workflow rail.
- Permission-aware action buttons.
- Graph preview panel that makes read-only projection explicit.

## Motion

Use short state transitions only for hover, focus, row expansion, and panel selection. No decorative load choreography.
