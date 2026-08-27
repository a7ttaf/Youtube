// ============================================================================
// Purpose: Derive the month keys the dashboard chrome offers from the CURRENT
//   calendar date instead of a frozen literal. Replaces the hardcoded
//   "2026-03" default and the ["2026-03".."2025-12"] option list, which went
//   stale the moment real time moved past March 2026 and left every wired view
//   defaulting to a month with no data.
// Database/ORM: None (frontend, pure functions over Date components).
// Standards: TIMEZONE-STABLE BY CONSTRUCTION. Months are derived from LOCAL
//   date components (getFullYear/getMonth) and integer arithmetic — never by
//   parsing a "YYYY-MM" or "YYYY-MM-DD" string with `new Date()`, which treats
//   it as UTC midnight and shifts the month for negative UTC offsets (the same
//   trap already documented in ConnectorsView.formatDate). No currency, no
//   finance math, no conversion of any kind. Every entry point takes an
//   injectable `now` so tests are deterministic.
// Blast Radius: Display/selection only — it decides which month a view asks the
//   API for by default. It stores nothing and changes no source-of-truth value;
//   a wrong month shows an empty view, it never mislabels a stored figure.
// Connections:
//   - File: frontend/src/components/srcc/shared.tsx -> DEFAULT_MONTH and
//     MONTH_OPTIONS are computed here; the five wired views consume those.
//   - File: frontend/src/components/srcc/AppShell.tsx -> Topbar month <select>
//     renders monthKeyLabel over the same MONTH_OPTIONS.
//   - File: frontend/src/components/srcc/views/ConnectorsView.tsx -> formatDate
//     documents the UTC-shift trap this module avoids.
// ============================================================================

/** How many months the rolling selector window offers, newest first. */
export const MONTH_WINDOW_SIZE = 4;

const MONTHS_PER_YEAR = 12;

const MONTH_KEY_PATTERN = /^(\d{4})-(\d{2})$/;

const MONTH_LABEL_FORMATTER = new Intl.DateTimeFormat("en-US", {
  month: "short",
  year: "numeric",
});

/**
 * Build a zero-padded "YYYY-MM" key from a year and a ZERO-BASED month index,
 * normalising an out-of-range index (e.g. -1 meaning "December of the previous
 * year") by pure integer arithmetic so no Date parsing — and therefore no
 * timezone shift — is involved.
 */
export const monthKey = (year: number, monthIndex: number): string => {
  const absoluteMonths = year * MONTHS_PER_YEAR + monthIndex;
  const normalisedYear = Math.floor(absoluteMonths / MONTHS_PER_YEAR);
  const normalisedMonth = absoluteMonths - normalisedYear * MONTHS_PER_YEAR;
  const yearPart = String(normalisedYear).padStart(4, "0");
  const monthPart = String(normalisedMonth + 1).padStart(2, "0");
  return `${yearPart}-${monthPart}`;
};

/**
 * The calendar month containing `now`, as "YYYY-MM", read from LOCAL date
 * components. `now` defaults to the current time and is injectable for tests.
 */
export const currentMonthKey = (now: Date = new Date()): string =>
  monthKey(now.getFullYear(), now.getMonth());

/**
 * The rolling month window: the month containing `now` plus the `size - 1`
 * months preceding it, newest first. A non-positive `size` yields an empty
 * window. `now` is injectable so tests never depend on the wall clock.
 */
export const rollingMonthWindow = (
  size: number = MONTH_WINDOW_SIZE,
  now: Date = new Date(),
): string[] =>
  Array.from({ length: Math.max(size, 0) }, (_unused, monthsBack) =>
    monthKey(now.getFullYear(), now.getMonth() - monthsBack),
  );

/**
 * Render a "YYYY-MM" key as the short human label used in the chrome selector
 * ("2026-03" -> "Mar 2026"). The Date is built from local components, so it
 * cannot slip to the previous month; an unrecognised key is echoed unchanged
 * rather than rendered as "Invalid Date".
 */
export const monthKeyLabel = (key: string): string => {
  const parsed = MONTH_KEY_PATTERN.exec(key);
  if (!parsed) return key;
  const [, year, month] = parsed;
  return MONTH_LABEL_FORMATTER.format(new Date(Number(year), Number(month) - 1, 1));
};
