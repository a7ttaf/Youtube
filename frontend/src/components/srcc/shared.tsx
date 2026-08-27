import type { ReactNode } from "react";

import type { Severity } from "@/lib/mock/data";

// ============================================================================
// Purpose: Shared presentational helpers for the SRCC shell views. Extracted
//   from AppShell so the wired CommandView (and future wired views) reuse the
//   exact same design-system primitives (Badge/Dot/ItemRow/SummaryTile) and the
//   single RESTRICTED_FINANCE_VALUE / finance-formatting contract.
// Database/ORM: None (presentation only).
// Standards: No business logic; money formatting takes API strings (never float
//   math) and renders RESTRICTED_FINANCE_VALUE when the viewer lacks finance
//   permission. Pure render helpers, no side effects.
// Blast Radius: Finance display gating (permission-gated cells) — keep
//   RESTRICTED_FINANCE_VALUE the single source so no view leaks money.
// ============================================================================

// Sentinel shown wherever a finance value is withheld from the current viewer.
export const RESTRICTED_FINANCE_VALUE = "Restricted";

/**
 * The month each wired view (Command/Close/Trace/Exports/Connectors) defaults
 * to: a recent, demo-seedable month per the MVP task brief. Shared from here so
 * the five views stay in lockstep instead of each copying the literal.
 */
export const DEFAULT_MONTH = "2026-03";

/**
 * Months offered in every wired view's month selector (most recent first). The
 * selector is a simple dropdown by design — wiring real data is the priority,
 * not month discovery — and DEFAULT_MONTH is its first entry.
 */
export const MONTH_OPTIONS = ["2026-03", "2026-02", "2026-01", "2025-12"];

const USD_FORMATTER = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

/** True when a backend money string is absent: null, undefined, or empty. */
const isAbsentMoney = (value: string | null | undefined): value is null | undefined | "" =>
  value === null || value === undefined || value === "";

/**
 * True for an explicit currency code other than "USD"; an absent or empty code
 * falls through to the USD formatter, matching the prior truthiness check.
 */
const isNonUsdCurrency = (currency: string | undefined): currency is string =>
  currency !== undefined && currency !== "" && currency !== "USD";

/**
 * Format a non-USD amount as "<amount> <code>". Only USD is backend-supported
 * today; showing the raw amount plus the code avoids mislabeling a non-USD
 * value with a USD symbol.
 */
const formatNonUsdMoney = (amount: number, currency: string): string =>
  `${amount.toLocaleString("en-US", { maximumFractionDigits: 2 })} ${currency}`;

// ============================================================================
// Purpose: Format a backend decimal-as-string money value for display WITHOUT
//   doing float math on it for any business purpose. We parse only to drive the
//   display formatter; if the string is missing or unparsable we fall back to a
//   safe placeholder rather than rendering "NaN".
// Database/ORM: None (frontend) — formats an already-serialized API string; it
//   reads no store and issues no request.
// Standards: Backend serializes finance decimals as strings (decimal_to_api);
//   null means "unknown". Display-only — never feed the parsed number back into
//   a calculation that affects a stored/exported number. Total by construction:
//   an absent value returns the placeholder and an unparsable one echoes the raw
//   string, so a money cell never renders "NaN" and never throws.
// Blast Radius: Finance display. The Number() parse here exists ONLY to drive
//   Intl formatting — routing it back into a stored or exported figure would put
//   float math on a value the backend deliberately serialized as a decimal
//   string. Permission gating is NOT done here: callers must go through
//   financeDisplay, which substitutes RESTRICTED_FINANCE_VALUE, so calling
//   formatMoney directly on a money value would bypass that gate.
// Connections: financeDisplay (the permission-gated wrapper callers must use),
//   backend decimal_formatting.decimal_to_api (produces the input strings).
//   - File: frontend/src/components/srcc/shared.tsx -> financeDisplay wraps this
//     with the canViewFinance gate; views call that wrapper, not this.
//   - File: backend/ums_smart_revenue/finance/decimal_formatting.py ->
//     decimal_to_api produces the decimal-as-string inputs formatted here.
// ============================================================================
export const formatMoney = (
  value: string | null | undefined,
  options: { currency?: string; placeholder?: string } = {},
): string => {
  const { currency, placeholder = "—" } = options;
  if (isAbsentMoney(value)) {
    return placeholder;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return value;
  }
  if (isNonUsdCurrency(currency)) {
    return formatNonUsdMoney(parsed, currency);
  }
  return USD_FORMATTER.format(parsed);
};

// ============================================================================
// Purpose: Render an ISO timestamp string for display WITHOUT float/epoch math
//   for any business purpose. Returns a dash for an absent value and echoes the
//   raw string when it is unparsable, rather than rendering "Invalid Date". When
//   `options` is supplied the locale-aware format is pinned to "en-US" so the
//   rendered string is deterministic; with no `options` it uses the runtime's
//   default `toLocaleString()` (the legacy ExportsView format). Each caller
//   passes the options it needs so the previously-rendered strings are unchanged.
// Standards: Display-only — never feed the parsed Date back into a calculation
//   that affects a stored/exported number. Shared from here so CloseView and
//   ExportsView stop maintaining near-identical local copies.
// ============================================================================
/**
 * Format an ISO timestamp for display: "—" when absent, the raw value when
 * unparsable, otherwise a locale string. With `options` the format is pinned to
 * "en-US" for determinism; without it the runtime default locale is used.
 */
export const formatTimestamp = (
  value: string | null | undefined,
  options?: Intl.DateTimeFormatOptions,
): string => {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return options ? parsed.toLocaleString("en-US", options) : parsed.toLocaleString();
};

/**
 * Format a money value for display, returning the RESTRICTED_FINANCE_VALUE
 * sentinel when the viewer lacks finance permission so no view leaks amounts.
 */
export const financeDisplay = (
  value: string | null | undefined,
  canViewFinance: boolean,
  options?: { currency?: string; placeholder?: string },
): string => {
  if (!canViewFinance) return RESTRICTED_FINANCE_VALUE;
  return formatMoney(value, options);
};

/** Render a tone-colored status badge wrapping its children. */
export const Badge = ({ tone, children }: { tone: Severity; children: ReactNode }) => {
  return <span className={`badge ${tone}`}>{children}</span>;
};

/** Render a small decorative status dot in the given tone. */
export const Dot = ({ tone }: { tone?: Severity }) => {
  return <span className={`dot${tone ? ` ${tone}` : ""}`} aria-hidden="true" />;
};

// ============================================================================
// Purpose: Render the repeated placeholder row used by audit timeline states.
// Database/ORM: None.
// Standards: Presentation-only helper; no side effects.
// Blast Radius: Audit read only.
// ============================================================================
export const TimelinePlaceholderRow = ({
  tone,
  title,
  sub,
  badge,
}: {
  tone: Severity;
  title: string;
  sub: string;
  badge: string;
}) => {
  return (
    <>
      <span className="timeline-time">--:--</span>
      <Dot tone={tone} />
      <span>
        <span className="item-title">{title}</span>
        <span className="item-sub">{sub}</span>
      </span>
      <Badge tone={tone}>{badge}</Badge>
    </>
  );
};

/** Render a list row with a tone dot, title, subtitle, and trailing slot. */
export const ItemRow = ({
  tone,
  title,
  sub,
  trailing,
  className = "issue-item",
}: {
  tone: Severity;
  title: string;
  sub: string;
  trailing: ReactNode;
  className?: string;
}) => {
  return (
    <div className={className} role="listitem">
      <Dot tone={tone} />
      <span>
        <span className="item-title">{title}</span>
        <span className="item-sub">{sub}</span>
      </span>
      {trailing}
    </div>
  );
};

/**
 * Render a labelled summary tile; finance tiles show RESTRICTED_FINANCE_VALUE
 * when the viewer lacks finance permission.
 */
export const SummaryTile = ({
  label,
  value,
  note,
  finance,
  canViewFinance = true,
}: {
  label: string;
  value: string;
  note: string;
  finance?: boolean;
  canViewFinance?: boolean;
}) => {
  const displayValue = finance && !canViewFinance ? RESTRICTED_FINANCE_VALUE : value;
  return (
    <article className="summary-tile">
      <span>{label}</span>
      <strong className={finance ? "finance-data" : undefined}>{displayValue}</strong>
      <small>{note}</small>
    </article>
  );
};
