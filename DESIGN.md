# UMS Smart Revenue Design System

## Overview

Internal product UI for a finance/revenue command center. The design serves complex monthly close workflows, not brand promotion.

## Color

Use the Claude login reference palette with dark charcoal, warm cream, and Anthropic orange:

- Background: near-black charcoal `oklch(16% .006 72)`.
- Navigation: darker charcoal rail `oklch(13.5% .005 72)`.
- Surfaces: raised dark panels `oklch(20.5% .006 72)` with warm neutral borders.
- Primary action: warm cream text/button surface using the same contrast pattern as Claude login.
- Primary accent: Anthropic orange `oklch(66% .15 45)` for brand marks, selected states, graph emphasis, and finance highlight states.
- Finance positive: green with label support.
- Warning: amber for allocated or unresolved values.
- Critical: red for missing data or permission blocks.
- Graph/read-model accent: Anthropic orange, used sparingly for graph-related states.

## Typography

Use local Anthropic font files from `mockups/FontsPP`:

- Display: Anthropic Serif for page titles, major panel headings, and KPI values.
- Body/UI: Anthropic Sans for navigation, tables, forms, buttons, labels, and supporting text.
- Code-like labels: Anthropic Mono for formulas, identifiers, and chips.
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
