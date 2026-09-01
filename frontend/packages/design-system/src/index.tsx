import type { Severity } from "@/types/domain";

type MoneyCellProps = {
  value: string;
  currency?: string;
  className?: string;
};

// ============================================================================
// Purpose: Render a finance value without deriving or mutating its meaning.
// Database/ORM: None; the value is supplied by an API-backed view model.
// Standards: Keep presentation-only formatting at the design-system boundary.
// Blast Radius: None detected; this component does not calculate finance.
// Connections:
//   - File: frontend/src/styles.css -> shared money-cell presentation tokens.
// ============================================================================
/** Render an API-provided money value with its display currency metadata. */
export const MoneyCell = ({
  value,
  currency = "USD",
  className,
}: MoneyCellProps) => {
  return (
    <span className={className} data-currency={currency} data-testid="money-cell">
      {value}
    </span>
  );
};

type ConfidenceBadgeProps = {
  label: string;
  tone: Severity;
  className?: string;
};

const TONE_CLASS: Record<Severity, string> = {
  green: "green",
  amber: "amber",
  red: "red",
  blue: "blue",
  violet: "violet",
};

// ============================================================================
// Purpose: Render a confidence/severity label using the shared badge contract.
// Database/ORM: None; the tone is supplied by the typed domain model.
// Standards: Preserve the base `badge` class so global design tokens apply.
// Blast Radius: None detected; this component is presentation-only.
// Connections:
//   - File: frontend/src/styles.css -> `.badge` plus the typed tone classes.
// ============================================================================
/** Render a typed confidence label with the shared badge treatment. */
export const ConfidenceBadge = ({
  label,
  tone,
  className,
}: ConfidenceBadgeProps) => {
  return (
    <span
      className={["badge", TONE_CLASS[tone], className].filter(Boolean).join(" ")}
      data-testid="confidence-badge"
    >
      {label}
    </span>
  );
};

export type { MoneyCellProps, ConfidenceBadgeProps };
