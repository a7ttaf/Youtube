// ============================================================================
// Purpose: Derive the month keys the dashboard views offer from the CURRENT
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
//   - File: frontend/src/components/srcc/views/CommandView.tsx -> its request-
//     wired Month control consumes MONTH_OPTIONS through shared.tsx.
//   - File: frontend/src/components/srcc/views/ConnectorsView.tsx -> formatDate
//     documents the UTC-shift trap this module avoids; its month state seeds
//     from lastCompleteMonthKey (the ingest/payment WRITE default).
//   - File: frontend/src/components/srcc/views/RegistryView.tsx -> the account-
//     link proposal's effective month seeds from currentMonthKey.
// ============================================================================

/** How many months the rolling selector window offers, newest first. */
export const MONTH_WINDOW_SIZE = 4;

const MONTHS_PER_YEAR = 12;

const MONTH_KEY_PATTERN = /^(\d{4})-(\d{2})$/;

const ISO_DATE_INPUT_PATTERN = /^(\d{4})-(\d{2})-\d{2}$/;

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

// ============================================================================
// Purpose: The "YYYY-MM" month of a "YYYY-MM-DD" date-input value — the wire
//   format <input type="date"> produces. Pure string slicing + monthKey
//   arithmetic: a date-input value is a calendar date with NO timezone
//   semantics, so reading it this way (instead of new Date(value), which
//   parses it as UTC midnight) cannot shift the month for negative offsets.
// Database/ORM: None (frontend, pure function).
// Standards: Total and fail-closed — returns an EMPTY STRING for anything
//   that is not exactly the YYYY-MM-DD shape (empty, partial, or a datetime
//   with a time part), so a required-field/non-empty gate downstream can never
//   pass a guessed month through to a finance write.
// Blast Radius: One finance WRITE default: it decides the month a manually
//   synced AdSense payment row files under, mirroring the automated AdSense
//   mapping's payment-date-derived settlement month. It stores nothing itself;
//   the backend upsert stays the authority.
// Connections:
//   - File: frontend/src/components/srcc/views/ConnectorsView.tsx ->
//     AdsenseSyncForm derives the manual payment row's month from the entered
//     payment date with this, NOT from the screen's write-month default, and
//     feeds the result into its non-empty required-fields submit gate.
// ============================================================================
export const monthKeyOfDateInput = (value: string): string => {
  const parsed = ISO_DATE_INPUT_PATTERN.exec(value.trim());
  if (!parsed) {
    return "";
  }
  return monthKey(Number(parsed[1]), Number(parsed[2]) - 1);
};

// ============================================================================
// Purpose: The month a WRITE should default to — the last COMPLETE calendar
//   month, i.e. the one before the month containing `now`.
// Database/ORM: None (frontend, integer arithmetic over local Date components).
// Standards: WHY writes default here and reads default to currentMonthKey: a
//   connector run and an AdSense payment sync both address a WHOLE calendar
//   month. The Google clients request the full month range and the backend
//   validates only the "YYYY-MM" FORMAT, so submitting the in-progress month
//   ingests a PARTIAL month and stores it as if it were the finished figure.
//   The last complete month is the newest month that cannot be partial. Reads
//   are unaffected — showing an in-progress month is honest, writing one is not.
//   Same integer arithmetic as the rest of this module (no Date parsing, so no
//   timezone shift) and the same injectable `now` for deterministic tests.
// Blast Radius: The DEFAULT month a connector/payment write proposes. It is a
//   default, not a constraint — the operator can still pick any offered month,
//   including the current one — and it stores no value itself.
// Connections:
//   - File: frontend/src/components/srcc/shared.tsx -> WRITE_DEFAULT_MONTH
//     derives the write default FROM the frozen MONTH_OPTIONS (its index-1
//     entry), so the two can never disagree. Call this function directly only
//     when you can inject the same `now` the option list was built from: a
//     separate wall-clock read (the PR #211 review bug) let a long-lived tab
//     seed a month the frozen selector no longer offered.
//   - File: frontend/src/components/srcc/views/ConnectorsView.tsx -> its month
//     state (connector-run report_month + AdSense payment month) seeds from
//     WRITE_DEFAULT_MONTH, not from this function's live clock read.
// ============================================================================
export const lastCompleteMonthKey = (now: Date = new Date()): string =>
  monthKey(now.getFullYear(), now.getMonth() - 1);

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
  if (!parsed) {
    return key;
  }
  const [, year, month] = parsed;
  return MONTH_LABEL_FORMATTER.format(new Date(Number(year), Number(month) - 1, 1));
};
