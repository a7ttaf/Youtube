// SVG icons used across the command center sidebar and controls.
// Stroke is currentColor; sized via the .icon wrapper.

import type { JSX } from "react";

/** Wrap SVG paths in the shared presentation-only icon container. */
const wrap = (path: JSX.Element) => (
  <span className="icon" aria-hidden="true">
    <svg viewBox="0 0 24 24">{path}</svg>
  </span>
);

export const NAV_ICONS: Record<string, JSX.Element> = {
  command: wrap(
    <>
      <path d="M4 13h6V4H4v9Z" />
      <path d="M14 20h6V4h-6v16Z" />
      <path d="M4 20h6v-3H4v3Z" />
    </>,
  ),
  registry: wrap(
    <>
      <path d="M4 7h16" />
      <path d="M4 12h16" />
      <path d="M4 17h10" />
    </>,
  ),
  groups: wrap(
    <>
      <path d="M3 6h4l2 2h8v5H3V6Z" />
      <path d="M7 10h4l2 2h6v5H7V10Z" />
    </>,
  ),
  close: wrap(
    <>
      <path d="M4 18V6" />
      <path d="M8 18v-5" />
      <path d="M12 18V9" />
      <path d="M16 18v-7" />
      <path d="M20 18V4" />
    </>,
  ),
  trace: wrap(
    <>
      <path d="M12 4v16" />
      <path d="M4 12h16" />
      <circle cx="12" cy="12" r="3" />
    </>,
  ),
  exports: wrap(
    <>
      <path d="M4 7h16v10H4V7Z" />
      <path d="M8 11h8" />
      <path d="M8 15h5" />
    </>,
  ),
  connectors: wrap(
    <>
      <path d="M6 6h12v12H6V6Z" />
      <path d="M9 3v3" />
      <path d="M15 3v3" />
      <path d="M9 18v3" />
      <path d="M15 18v3" />
    </>,
  ),
  audit: wrap(
    <>
      <path d="M5 12h14" />
      <path d="M12 5v14" />
      <path d="M7 7l10 10" />
      <path d="M17 7 7 17" />
    </>,
  ),
};

/** Render the command-center brand glyph. */
export const BrandIcon = () =>
  wrap(
    <>
      <path d="M5 17V7l7-4 7 4v10l-7 4-7-4Z" />
      <path d="M9 15V9l3-2 3 2v6l-3 2-3-2Z" />
    </>,
  );

// ============================================================================
// Purpose: Render the lock glyph used for protected finance status and actions.
// Database/ORM: None (frontend presentation only).
// Standards: Pure SVG presentation; no state, data fetching, or authorization
//   decision is made here. Owning views retain the permission checks.
// Blast Radius: Presentation only; changing the paths affects lock affordances.
// Connections:
//   - File: frontend/src/components/srcc/views/CommandView.tsx -> renders this
//     icon for protected finance status surfaces.
//   - File: frontend/src/components/srcc/AppShell.tsx -> keeps authorization
//     decisions at the routed-view boundary, outside this icon.
// ============================================================================
/** Render the lock glyph for a protected finance surface. */
export const LockIcon = () =>
  wrap(
    <>
      <path d="M7 11V8a5 5 0 0 1 10 0v3" />
      <path d="M6 11h12v9H6v-9Z" />
    </>,
  );
