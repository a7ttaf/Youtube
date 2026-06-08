# UMS Revenue Design System Kody Reference

This note condenses the UMS Revenue Design System pack into repository-local
review guidance for Kody. It is a reference file for the centralized Kody rules
under `Youtube/.kody-rules/review/`.

## Product Intent

UMS Smart Revenue Control Center is an internal finance and revenue command
center. It is a numbers engine first and a dashboard second. UI changes must
prioritize source, formula, confidence, permissions, and export readiness over
decoration.

Primary users are finance operators, revenue operations, sector leadership,
company managers, audit viewers, and analysts during monthly close,
reconciliation, export preparation, and issue investigation for 300+ YouTube
channels.

## Review Principles

- Numbers before decoration. Revenue, confidence, source, lock state,
  unresolved issues, and export readiness must be visible before visual polish.
- Permission awareness is part of the UI contract. Sensitive money, raw files,
  exports, and graph reads must visibly respect role boundaries.
- Explainability is mandatory. Money cells and KPI values need source,
  formula, confidence, and trace affordances.
- Close workflow comes first. Month status, payment gaps, overrides, exports,
  and locking should read as one operational flow.
- Dense but readable. Avoid generic SaaS dashboards, marketing hero sections,
  oversized stat cards, decorative grids, crypto-style finance visuals, and
  UI that hides confidence or unresolved issues.

## Visual System

- Theme: Soft Dark, based on GitHub dark dimmed surfaces with a restrained
  Anthropic orange accent.
- Background and surfaces:
  - App canvas `#212830`.
  - Body gradient rail tone `#1b212a`.
  - Navigation well `#151b23`.
  - Panels `#262c36`, soft panels `#2a313c`, raised panels `#2f3742`.
  - Hairline borders `#3d444d`, emphasis borders `#656c76`.
- Text:
  - Chrome text `#d1d7e0`.
  - KPI and money values near-white `#f0f6fc`.
  - Muted metadata `#9198a1`.
- Status:
  - Official or reconciled values use green `#57ab5a`.
  - Allocated, pending, or review states use amber `#c69026`.
  - Missing, blocked, or permission-sensitive states use red `#e5534b`.
  - Status must always be color plus a label. Color alone is never enough.
- Accent:
  - Anthropic orange `oklch(0.66 0.15 45)` is reserved for nav-active,
    selected rows, focus rings, KPI deltas, graph emphasis, and finance
    highlights.
- Typography:
  - Display and money: Newsreader.
  - Body and controls: Mona Sans.
  - Formulas, identifiers, and trace keys: Monaspace Neon.
  - Sizes are fixed pixels, not viewport-scaled.
  - Tables remain compact and tabular.
- Components:
  - Use the app shell pattern: left navigation, sticky top operational bar,
    status strip, table-first workspace, side explainability column, and bottom
    workflow rail.
  - Panels do not nest.
  - Cards use small radii, quiet shadows, and hairline borders.
  - Avatars are rounded squares, not circles.
  - Icons are custom line icons, 24x24, currentColor, stroke width 2, round
    caps and joins, no fill.
- Motion:
  - Use short functional transitions for hover, focus, press, row expansion,
    and panel selection.
  - No decorative load choreography or looping animation.
  - Respect reduced motion.

## Content Rules

- Voice is precise, controlled, and accountable.
- Use impersonal or system voice for explanatory copy.
- Use imperative verbs for actions such as `Create Export`, `Lock month`, and
  `Review`.
- Use sentence case for descriptions and helper text.
- Use uppercase for small structural labels such as column headers, KPI labels,
  nav section titles, and field labels.
- Avoid "we", "I", emoji, exclamation marks, promotional wording, and filler.
- Identifiers and formulas use monospace, for example `UC-DRAMA-01`,
  `channel:ums-drama`, and `net = gross + adjustments - tax - fees`.

## Finance UI Contract

- Official finance results must not be calculated directly in the UI.
- Money values must preserve source, formula, confidence, permissions, and
  export value.
- Withheld money renders as `Restricted`, never blank and never `$0`.
- Confidence must be labeled:
  - `A Official`
  - `B Reconciled` or `B Matched`
  - `C Allocated`
- CMS status must be labeled, for example `Inside CMS` or `Outside CMS`.
- Export blockers, evidence gaps, lock state, and unresolved alerts must be
  visible near the affected money or action.
- Role-gated controls must expose disabled or restricted states without
  implying the action succeeded.

## Source References For Review

Kody should cross-check frontend UI changes against these stable repository
references:

- `DESIGN.md`
- `PRODUCT.md`
- `frontend/src/styles.css`
- `frontend/src/components/srcc/shared.tsx`
- `frontend/src/components/srcc/icons.tsx`
- `frontend/src/components/srcc/AppShell.tsx`
- `mockups/ums-smart-revenue-command-center-soft-dark.html`
- `Docs/superpowers/specs/2026-06-05-registry-view-design.md`
