import type { ReactNode } from "react";

import type { Severity, WorkflowTone } from "@/lib/mock/data";

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

// ============================================================================
// Purpose: Format a backend decimal-as-string money value for display WITHOUT
//   doing float math on it for any business purpose. We parse only to drive the
//   display formatter; if the string is missing or unparsable we fall back to a
//   safe placeholder rather than rendering "NaN".
// Standards: Backend serializes finance decimals as strings (decimal_to_api);
//   null means "unknown". Display-only — never feed the parsed number back into
//   a calculation that affects a stored/exported number.
// ============================================================================
export function formatMoney( // skipcq: JS-0067, JS-R1005
  value: string | null | undefined,
  options: { currency?: string; placeholder?: string } = {},
): string {
  const { currency, placeholder = "—" } = options;
  if (value === null || value === undefined || value === "") return placeholder;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return value;
  if (currency && currency !== "USD") {
    // Only USD is backend-supported today; show the raw amount + code rather
    // than mislabel a non-USD value with a USD symbol.
    return `${parsed.toLocaleString("en-US", { maximumFractionDigits: 2 })} ${currency}`;
  }
  return USD_FORMATTER.format(parsed);
}

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
export function formatTimestamp( // skipcq: JS-0067
  value: string | null | undefined,
  options?: Intl.DateTimeFormatOptions,
): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return options ? parsed.toLocaleString("en-US", options) : parsed.toLocaleString();
}

/**
 * Format a money value for display, returning the RESTRICTED_FINANCE_VALUE
 * sentinel when the viewer lacks finance permission so no view leaks amounts.
 */
export function financeDisplay( // skipcq: JS-0067
  value: string | null | undefined,
  canViewFinance: boolean,
  options?: { currency?: string; placeholder?: string },
): string {
  if (!canViewFinance) return RESTRICTED_FINANCE_VALUE;
  return formatMoney(value, options);
}

/** Render a tone-colored status badge wrapping its children. */
export function Badge({ tone, children }: { tone: Severity; children: ReactNode }) { // skipcq: JS-0067
  return <span className={`badge ${tone}`}>{children}</span>;
}

/** Render a small decorative status dot in the given tone. */
export function Dot({ tone }: { tone?: Severity }) { // skipcq: JS-0067
  return <span className={`dot${tone ? ` ${tone}` : ""}`} aria-hidden="true" />;
}

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

/** Map a workflow tone to a Dot severity; "primary" renders an untoned dot. */
export function workflowDotTone(tone: WorkflowTone): Severity | undefined { // skipcq: JS-0067
  return tone === "primary" ? undefined : tone;
}

/** Render a list row with a tone dot, title, subtitle, and trailing slot. */
export function ItemRow({ // skipcq: JS-0067
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
}) {
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
}

/**
 * Render a labelled summary tile; finance tiles show RESTRICTED_FINANCE_VALUE
 * when the viewer lacks finance permission.
 */
export function SummaryTile({ // skipcq: JS-0067
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
}) {
  const displayValue = finance && !canViewFinance ? RESTRICTED_FINANCE_VALUE : value;
  return (
    <article className="summary-tile">
      <span>{label}</span>
      <strong className={finance ? "finance-data" : undefined}>{displayValue}</strong>
      <small>{note}</small>
    </article>
  );
}
