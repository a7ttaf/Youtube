import type { Severity } from "@/types/domain";

type MoneyCellProps = {
  value: string;
  currency?: string;
  className?: string;
};

export function MoneyCell({ value, currency = "USD", className }: MoneyCellProps) {
  return (
    <span className={className} data-currency={currency} data-testid="money-cell">
      {value}
    </span>
  );
}

type ConfidenceBadgeProps = {
  label: string;
  tone: Severity;
  className?: string;
};

const TONE_CLASS: Record<Severity, string> = {
  green: "badge-tone-green",
  amber: "badge-tone-amber",
  red: "badge-tone-red",
  blue: "badge-tone-blue",
  violet: "badge-tone-violet",
};

export function ConfidenceBadge({ label, tone, className }: ConfidenceBadgeProps) {
  return (
    <span
      className={[TONE_CLASS[tone], className].filter(Boolean).join(" ")}
      data-testid="confidence-badge"
    >
      {label}
    </span>
  );
}

export type { MoneyCellProps, ConfidenceBadgeProps };
