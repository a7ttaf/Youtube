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
export function formatMoney(
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

export function financeDisplay(
  value: string | null | undefined,
  canViewFinance: boolean,
  options?: { currency?: string; placeholder?: string },
): string {
  if (!canViewFinance) return RESTRICTED_FINANCE_VALUE;
  return formatMoney(value, options);
}

export function Badge({ tone, children }: { tone: Severity; children: ReactNode }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export function Dot({ tone }: { tone?: Severity }) {
  return <span className={`dot${tone ? ` ${tone}` : ""}`} aria-hidden="true" />;
}

export function workflowDotTone(tone: WorkflowTone): Severity | undefined {
  return tone === "primary" ? undefined : tone;
}

export function ItemRow({
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

export function SummaryTile({
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
