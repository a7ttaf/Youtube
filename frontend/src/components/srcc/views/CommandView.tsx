import { useMemo, useState } from "react";
import type { ReactNode } from "react";

import { ApiError } from "@/lib/api/client";
import type {
  ChannelIssue,
  ChannelIssuesResponse,
  ChannelNetRevenue,
  GapExplanationComponent,
  MonthBankReconciliationSummary,
  MonthGapExplanation,
  MonthRankingsResponse,
  NetRevenueResponse,
  OutsideCmsItem,
  OutsideCmsResponse,
  RankedEntry,
  RankingMetric,
  RevenueScopeOption,
  ScopedFinanceViewHint,
  SmartAlert,
  SmartAlertSeverity,
  SmartAlertsSummary,
} from "@/lib/api/types";
import { useBankReconciliation } from "@/lib/api/useBankReconciliation";
import { useChannelIssues } from "@/lib/api/useChannelIssues";
import { useGapExplanation } from "@/lib/api/useGapExplanation";
import { useNetRevenue } from "@/lib/api/useNetRevenue";
import { useOutsideCmsChannels } from "@/lib/api/useOutsideCmsChannels";
import { useRankings } from "@/lib/api/useRankings";
import { useRevenueScopes } from "@/lib/api/useRevenueScopes";
import { useSmartAlerts } from "@/lib/api/useSmartAlerts";
import type { Severity } from "@/lib/mock/data";
import { confidenceDisplay } from "@/lib/confidence";
import { LockIcon } from "../icons";
import {
  Badge,
  DEFAULT_MONTH,
  financeDisplay,
  ItemRow,
  MONTH_OPTIONS,
  RESTRICTED_FINANCE_VALUE,
  SummaryTile,
} from "../shared";

// ============================================================================
// Purpose: The first REAL-data screen. Renders the monthly net-revenue summary
//   (gross / net / deductions / status / allocation source) for a selected
//   month + scope via the useNetRevenue hook, with explicit loading and error
//   states. Establishes the data-wiring pattern the other six views follow.
//   Every panel on this screen is API-backed: the mock Issue Queue, Month Close
//   Controls and Export Readiness side panels were DELETED in P1.4 rather than
//   refilled — the first duplicated the real Channel Issues / Smart Alerts
//   panels already on this screen, and the other two had no data source here
//   (month close lives in CloseView, export readiness in ExportsView).
// Database/ORM: None (frontend) — consumes GET /revenue/months/{month}/net-revenue.
// Standards: Money values are backend strings, formatted for display only (no
//   float math); finance cells are permission-gated via financeDisplay; 403 ->
//   "no permission" copy, other ApiError -> the typed message.
// Blast Radius: Finance display (permission-gated money cells). No mutation.
// Connections:
//   - File: frontend/src/lib/api/useNetRevenue.ts -> the fetch hook.
//   - File: frontend/src/lib/api/types.ts -> NetRevenueResponse contract.
//   - File: backend/ums_smart_revenue/api/revenue.py -> get_month_net_revenue.
// ============================================================================

type ScopeOption = {
  label: string;
  scopeType: string;
  scopeId: string | null;
};

// ============================================================================
// Purpose: Build a stable identity key for a scope selection so the selector and
//   the scope state can survive an async option-list change (the fetched option
//   set arrives after the first render). The key is the scope_type alone for the
//   global option (no id) and `type:id` otherwise — this is the <option> value
//   AND the lookup key when an onChange fires.
// Standards: Pure helper; no money, no authorization. Global carries no scope_id.
// Blast Radius: None detected (UI selection identity only).
// ============================================================================
const scopeOptionKey = (scopeType: string, scopeId: string | null): string => {
  return scopeId ? `${scopeType}:${scopeId}` : scopeType;
};

// The guaranteed fallback when the authorized-scope fetch is loading, errors, or
// returns nothing: a single GLOBAL option. A scoped viewer with no global grant
// never sees this injected on top of a successful response — it is ONLY the
// fail-open default so the screen renders while the real, fail-closed option set
// (which the backend returns with global present ONLY when authorized) loads. The
// panels themselves fail-closed on the actual scoped reads.
const GLOBAL_SCOPE_FALLBACK: ScopeOption = {
  label: "Global",
  scopeType: "global",
  scopeId: null,
};

// ============================================================================
// Purpose: Resolve the scope selector's options from the authorized-scopes hook
//   state. On a successful, non-empty fetch the options ARE the viewer's
//   authorized scopes verbatim (the backend already includes global only when
//   authorized, so a scoped viewer correctly gets no global option — the
//   anti-scope-leak guarantee). While loading, on a 403/error, or on an empty
//   list, fall back to global-only so the screen never blocks.
// Standards: Pure mapping; no client-side authorization invented — the fetched
//   set is the fail-closed source of truth. No money handling here.
// Blast Radius: Authorization (the selector's option source). No mutation.
// Connections:
//   - File: frontend/src/lib/api/useRevenueScopes.ts -> the option source.
//   - File: frontend/src/lib/api/types.ts -> RevenueScopeOption.
// ============================================================================
const resolveScopeOptions = (scopes: RevenueScopeOption[] | null): ScopeOption[] => {
  if (!scopes || scopes.length === 0) {
    return [GLOBAL_SCOPE_FALLBACK];
  }
  return scopes.map((scope) => ({
    label: scope.label,
    scopeType: scope.scope_type,
    scopeId: scope.scope_id,
  }));
};

const ALLOCATION_SOURCE_COPY: Record<
  NetRevenueResponse["allocation_source"],
  { label: string; tone: Severity }
> = {
  committed_snapshot: { label: "Committed snapshot", tone: "green" },
  live_compute: { label: "Live compute", tone: "blue" },
  live_fallback: { label: "Live fallback", tone: "amber" },
};

const BANK_RECONCILIATION_STATUS_COPY: Record<
  string,
  { label: string; badge: string; tone: Severity; metricTone: string }
> = {
  BANK_CONFIRMED: {
    label: "Bank confirmed",
    badge: "Confirmed",
    tone: "green",
    metricTone: "is-close",
  },
  BANK_VARIANCE: {
    label: "Bank variance",
    badge: "Variance",
    tone: "red",
    metricTone: "is-risk",
  },
  MISSING_ADSENSE_PAYMENT: {
    label: "Missing AdSense payment",
    badge: "Payment missing",
    tone: "amber",
    metricTone: "is-review",
  },
  MISSING_BANK_RECEIPT: {
    label: "Missing bank receipt",
    badge: "Receipt missing",
    tone: "amber",
    metricTone: "is-review",
  },
};

/** Keyword → tone dispatch table; first matching keyword wins. */
const STATUS_KEYWORD_TONE: Record<string, Severity> = {
  LOCK: "green",
  OK: "green",
  CALCULATED: "green",
  MISSING: "amber",
  PENDING: "amber",
  ERROR: "red",
  BLOCK: "red",
};

/** Map a month/channel status string to a design-system badge tone. */
const statusTone = (status: string): Severity => {
  const normalized = status.toUpperCase();
  for (const [keyword, tone] of Object.entries(STATUS_KEYWORD_TONE)) {
    if (normalized.includes(keyword)) return tone;
  }
  return "blue";
};

/** Map a bank-reconciliation status code to its display copy and tones. */
const bankReconciliationStatusCopy = (status: string) =>
  BANK_RECONCILIATION_STATUS_COPY[status] ?? {
    label: status,
    badge: status,
    tone: statusTone(status),
    metricTone: "is-payment",
  };

/** Format a count with the singular or plural label appropriate to its value. */
const countLabel = (count: number, singular: string, plural = `${singular}s`): string =>
  `${count} ${count === 1 ? singular : plural}`;

/**
 * Human-readable confidence badge. Renders the resolved label + tone from the
 * shared confidenceDisplay helper while preserving the RAW backend code in the
 * title and aria-label so the underlying value is never lost for power users or
 * assistive tech. Channel rows carry no separate API human label, so the helper
 * falls back to its prefix map for these.
 */
const ConfidenceBadge = ({ code }: { code: string }) => {
  const { label, tone } = confidenceDisplay(code);
  return (
    <span title={code} aria-label={`Confidence: ${code}`}>
      <Badge tone={tone}>{label}</Badge>
    </span>
  );
};

// Map an alert severity to a design-system badge tone (HIGH -> red, MEDIUM ->
// amber, LOW -> blue). Unknown severities fall back to blue.
const severityTone = (severity: SmartAlertSeverity | string): Severity => {
  switch (severity) {
    case "HIGH":
      return "red";
    case "MEDIUM":
      return "amber";
    case "LOW":
      return "blue";
    default:
      return "blue";
  }
};

/** Human-facing label for a channel row (its YouTube channel id for now). */
const channelDisplayName = (channel: ChannelNetRevenue): string => {
  return channel.youtube_channel_id;
};

/** Two-letter avatar initials derived from a channel's id. */
const channelAvatar = (channel: ChannelNetRevenue): string => {
  const id = channel.youtube_channel_id.replace(/[^a-zA-Z0-9]/g, "");
  return (id.slice(-2) || "--").toUpperCase();
};

// ============================================================================
// Purpose: Map an ApiError/Error to friendly UI copy. 403 -> no-permission
//   message (matches the finance fail-closed model); other ApiError -> the
//   typed status + message; non-ApiError -> generic network failure.
// ============================================================================
const extractApiErrorDetail = (error: ApiError): string => {
  const body = error.body as { detail?: unknown } | null;
  if (body && typeof body.detail === "string") return body.detail;
  return error.message;
};

/**
 * Map an API or network failure to safe screen copy.
 * @param error The typed API or ordinary network error to describe.
 * @param forbiddenDetail Domain-specific copy for a 403 response.
 */
const describeError = (
  error: ApiError | Error,
  forbiddenDetail = "Your role cannot view net revenue for this month or scope.",
): { title: string; detail: string } => {
  if (error instanceof ApiError) {
    if (error.status === 403)
      return {
        title: "No permission",
        detail: forbiddenDetail,
      };
    return { title: `Request failed (${error.status})`, detail: extractApiErrorDetail(error) };
  }
  return {
    title: "Network error",
    detail: error.message || "Could not reach the revenue service.",
  };
};

// Header badge: surfaces the overall status + highest severity at a glance, and
// degrades to Loading / Error / No permission without breaking the panel header.
const alertErrorBadge = (error: ApiError | Error): { tone: Severity; children: string } => {
  const isForbidden = error instanceof ApiError && error.status === 403;
  return { tone: isForbidden ? "blue" : "red", children: isForbidden ? "No permission" : "Error" };
};

/** Map the loaded smart-alert summary to its header badge. */
const alertDataBadge = (data: SmartAlertsSummary): { tone: Severity; children: string } => {
  if (data.status === "CLEAR") return { tone: "green", children: "Clear" };
  const severity = data.highest_severity;
  return {
    tone: severity ? severityTone(severity) : "amber",
    children: severity ?? "Attention",
  };
};

/** Render the smart-alert header badge for loading, error, empty, or data state. */
const SmartAlertsHeaderBadge = ({
  data,
  loading,
  error,
}: {
  data: SmartAlertsSummary | null;
  loading: boolean;
  error: ApiError | Error | null;
}) => {
  if (error) return <Badge {...alertErrorBadge(error)} />;
  if (loading && !data) return <Badge tone="blue">Loading</Badge>;
  if (!data) return <Badge tone="amber">Empty</Badge>;
  return <Badge {...alertDataBadge(data)} />;
};

// Read alerts defensively: a missing/non-array field is treated as "no alerts"
// rather than throwing inside the panel. Extracted to keep SmartAlertsBody's
// cyclomatic complexity below the DeepSource medium-risk threshold.
const safeAlerts = (data: SmartAlertsSummary | null): SmartAlert[] =>
  data && Array.isArray(data.alerts) ? data.alerts : [];

/** Build the empty-state subtext for a missing or clear smart-alert response. */
const emptyAlertSubText = (data: SmartAlertsSummary | null): string =>
  data
    ? `Status ${data.status} — nothing needs attention for ${data.month}.`
    : "No smart-alert data returned.";

/** Body of the smart-alerts panel: error, loading, empty, and alert-row states. */
const SmartAlertsBody = ({
  data,
  loading,
  error,
}: {
  data: SmartAlertsSummary | null;
  loading: boolean;
  error: ApiError | Error | null;
}) => {
  if (error) {
    const { title, detail } = describeError(
      error,
      "Your role cannot view smart alerts for this month.",
    );
    return (
      <div className="issue-list" role="alert">
        <ItemRow
          tone="blue"
          title={title}
          sub={detail}
          trailing={<Badge tone="blue">—</Badge>}
        />
      </div>
    );
  }

  if (!data)
    return loading ? (
      <div className="issue-list" role="list" aria-busy="true">
        <ItemRow
          tone="blue"
          title="Loading smart alerts…"
          sub="Aggregating payment, bank, lock, and override signals"
          trailing={<Badge tone="blue">Loading</Badge>}
        />
      </div>
    ) : (
      <div className="issue-list" role="list">
        <ItemRow
          tone="green"
          title="No active alerts"
          sub="No smart-alert data returned."
          trailing={<Badge tone="green">Clear</Badge>}
        />
      </div>
    );

  const alerts = safeAlerts(data);
  if (alerts.length === 0)
    return (
      <div className="issue-list" role="list">
        <ItemRow
          tone="green"
          title="No active alerts"
          sub={emptyAlertSubText(data)}
          trailing={<Badge tone="green">Clear</Badge>}
        />
      </div>
    );

  return (
    <div className="issue-list" role="list">
      {alerts.map((alert: SmartAlert) => (
        <ItemRow
          key={alert.code}
          tone={severityTone(alert.severity)}
          title={alert.message}
          sub={`${alert.source} · ${alert.code}`}
          trailing={<Badge tone={severityTone(alert.severity)}>{alert.severity}</Badge>}
        />
      ))}
    </div>
  );
};

// ============================================================================
// Purpose: Smart Alerts / Problem Panel for the Command Center. Fetches the
//   monthly smart-alerts summary (overall status + highest severity + the
//   prioritized alert rows) via its OWN useSmartAlerts hook so it fails
//   INDEPENDENTLY of the net-revenue content: a 403 (the panel is gated behind
//   four finance-month permissions the net-revenue read does not all require) or
//   any other error renders inside this card only — the channel table, status
//   strip, and explain panel keep rendering. Loading / error / 403 / empty
//   states mirror the rest of CommandView and reuse describeError.
// Database/ORM: None (frontend) — consumes GET /revenue/months/{month}/smart-alerts.
// Standards: No money is rendered here (alerts carry messages, not gated finance
//   cells), so no canViewFinance gating is needed; severity drives the badge
//   tone only. Read-only — no mutation.
// Blast Radius: None detected (read-only finance health display). The panel does
//   NOT block the surrounding net-revenue render on its own 403/error.
// Connections:
//   - File: frontend/src/lib/api/useSmartAlerts.ts -> the fetch hook.
//   - File: frontend/src/lib/api/types.ts -> SmartAlertsSummary contract.
//   - File: backend/ums_smart_revenue/api/revenue.py -> get_month_smart_alerts.
// ============================================================================
const SmartAlertsPanel = ({ month }: { month: string }) => {
  const { data, loading, error, reload } = useSmartAlerts({ month });

  return (
    <section className="panel" aria-labelledby="smartAlertsTitle" style={{ marginBottom: 16 }}>
      <div className="panel-header">
        <div className="panel-title">
          <strong id="smartAlertsTitle">Smart Alerts / Problem Panel</strong>
          <span>Cross-domain finance health signals for {month}</span>
        </div>
        <SmartAlertsHeaderBadge data={data} loading={loading} error={error} />
        <button
          type="button"
          className="icon-button"
          aria-label="Refresh smart alerts"
          title="Refresh smart alerts"
          onClick={reload}
        >
          ↻
        </button>
      </div>
      <SmartAlertsBody data={data} loading={loading} error={error} />
    </section>
  );
};

// Build the metric-cell descriptors for the net-revenue status strip. Extracted
// from NetRevenueStatusStrip so the component's cyclomatic complexity stays
// below the DeepSource medium-risk threshold — the ternaries and nullish
// coalescing live here instead.
type RevenueMetric = {
  id: string;
  tone: string;
  label: string;
  value: string;
  badge: { text: string; tone: Severity };
  note: [string, string];
  finance: boolean;
  locked?: boolean;
};

/** Build permission-aware metric descriptors from the backend revenue response. */
const buildRevenueMetrics = (
  data: NetRevenueResponse,
  canViewFinance: boolean,
  currency: string,
): RevenueMetric[] => {
  const allocation = ALLOCATION_SOURCE_COPY[data.allocation_source] ?? {
    label: data.allocation_source,
    tone: "blue" as Severity,
  };
  return [
    {
      id: "gross",
      tone: "is-revenue",
      label: "Adjusted gross",
      value: financeDisplay(data.total_adjusted_gross_revenue_usd, canViewFinance, { currency }),
      badge: { text: currency, tone: "green" },
      note: [`${data.channel_count} channels`, `${data.calculated_channel_count} calculated`],
      finance: true,
    },
    {
      id: "net",
      tone: "is-net",
      label: "Net revenue",
      value: financeDisplay(data.total_net_revenue_usd, canViewFinance, { currency }),
      badge: { text: data.status, tone: statusTone(data.status) },
      note: ["After deductions", `${data.missing_net_source_count} missing source`],
      finance: true,
    },
    {
      id: "deductions",
      tone: "is-review",
      label: "Deductions",
      value: financeDisplay(data.total_deduction_amount_usd, canViewFinance, { currency }),
      badge: { text: "Total", tone: "amber" },
      note: [
        canViewFinance
          ? `Direct ${financeDisplay(data.total_channel_direct_deduction_amount_usd, canViewFinance, { currency })}`
          : RESTRICTED_FINANCE_VALUE,
        canViewFinance
          ? `Allocated ${financeDisplay(data.total_account_allocated_deduction_amount_usd, canViewFinance, { currency })}`
          : "",
      ],
      finance: true,
    },
    {
      id: "allocation",
      tone: "is-close",
      label: "Allocation source",
      value: allocation.label,
      badge: { text: data.status, tone: statusTone(data.status) },
      note: [
        data.committed_run?.commit_version != null
          ? `Snapshot v${data.committed_run.commit_version}`
          : "Computed at read time",
        `Status ${data.status}`,
      ],
      finance: false,
      locked: data.allocation_source === "committed_snapshot",
    },
  ];
};

/** Top metric strip summarising the month's gross, net, deductions, and allocation source. */
const NetRevenueStatusStrip = ({
  data,
  loading,
  error,
  canViewFinance,
  currency,
}: {
  data: NetRevenueResponse | null;
  loading: boolean;
  error: ApiError | Error | null;
  canViewFinance: boolean;
  currency: string;
}) => {
  if (error) {
    const { title, detail } = describeError(error);
    return (
      <section className="status-strip" aria-label="Revenue summary" role="alert">
        <article className="metric is-risk">
          <header>
            <span className="metric-label">{title}</span>
            <Badge tone="red">Error</Badge>
          </header>
          <div className="metric-value">—</div>
          <div className="metric-note">
            <span>{detail}</span>
          </div>
        </article>
      </section>
    );
  }

  if (loading && !data) {
    return (
      <section className="status-strip" aria-label="Revenue summary" aria-busy="true">
        {["Gross revenue", "Net revenue", "Deductions", "Month state"].map((label) => (
          <article key={label} className="metric">
            <header>
              <span className="metric-label">{label}</span>
              <Badge tone="blue">Loading</Badge>
            </header>
            <div className="metric-value">…</div>
            <div className="metric-note">
              <span>Fetching net revenue</span>
              <span>Please wait</span>
            </div>
          </article>
        ))}
      </section>
    );
  }

  if (!data) {
    return (
      <section className="status-strip" aria-label="Revenue summary">
        <article className="metric">
          <header>
            <span className="metric-label">No data</span>
            <Badge tone="amber">Empty</Badge>
          </header>
          <div className="metric-value">—</div>
          <div className="metric-note">
            <span>No net revenue returned</span>
          </div>
        </article>
      </section>
    );
  }

  const metrics = buildRevenueMetrics(data, canViewFinance, currency);

  return (
    <section className="status-strip" aria-label="Revenue summary">
      {metrics.map((k) => (
        <article key={k.id} className={`metric ${k.tone}`}>
          <header>
            <span className="metric-label">{k.label}</span>
            <Badge tone={k.badge.tone}>{k.badge.text}</Badge>
          </header>
          <div className={`metric-value${k.finance ? " finance-data" : ""}`}>{k.value}</div>
          <div className="metric-note">
            <span>{k.note[0]}</span>
            {k.locked ? (
              <span className="locked">
                <LockIcon />
                {k.note[1]}
              </span>
            ) : (
              <span>{k.note[1]}</span>
            )}
          </div>
        </article>
      ))}
    </section>
  );
};

type BankReconciliationMetric = {
  id: string;
  label: string;
  value: string;
  badge: { text: string; tone: Severity };
  note: [string, string];
  tone: string;
};

const BANK_RECONCILIATION_CARD_LABELS = [
  "AdSense payment",
  "Bank received",
  "Unresolved gap",
] as const;

const BANK_RECONCILIATION_USD_CURRENCY = "USD";

/** Build the AdSense payment badge from the backend payment counts. */
const adsensePaymentBadge = (
  data: MonthBankReconciliationSummary,
): BankReconciliationMetric["badge"] => {
  if (data.paid_payment_count > 0) {
    return { text: "Paid", tone: "green" };
  }
  return { text: "Missing", tone: "amber" };
};

/** Build the primary AdSense payment count note. */
const adsensePaymentPrimaryNote = (data: MonthBankReconciliationSummary): string =>
  countLabel(data.paid_payment_count, "paid USD payment");

/** Build the secondary AdSense payment note for unsupported or unpaid rows. */
const adsensePaymentSecondaryNote = (data: MonthBankReconciliationSummary): string => {
  if (data.unsupported_payment_currency_count > 0) {
    return countLabel(data.unsupported_payment_currency_count, "unsupported currency payment");
  }
  if (data.non_paid_payment_count > 0) {
    return countLabel(data.non_paid_payment_count, "unpaid USD payment");
  }
  return "AdSense source";
};

/** Map AdSense payment availability to the metric CSS tone. */
const adsensePaymentTone = (data: MonthBankReconciliationSummary): string => {
  if (data.paid_payment_count > 0) {
    return "is-payment";
  }
  return "is-review";
};

/** Build the bank-receipt badge from the backend entry count. */
const bankReceiptBadge = (
  data: MonthBankReconciliationSummary,
): BankReconciliationMetric["badge"] => {
  if (data.entry_count > 0) {
    return { text: countLabel(data.entry_count, "receipt"), tone: "green" };
  }
  return { text: countLabel(data.entry_count, "receipt"), tone: "amber" };
};

/** Build the bank receipt source note, including unsupported currencies. */
const bankReceiptSourceNote = (data: MonthBankReconciliationSummary): string => {
  if (data.entry_count > 0) {
    return "Bank source loaded";
  }
  return "Waiting for bank";
};

/** Map bank receipt availability to the metric CSS tone. */
const bankReceiptTone = (data: MonthBankReconciliationSummary): string => {
  if (data.entry_count > 0) {
    return "is-revenue";
  }
  return "is-review";
};

/** Build the bank transfer-fee note from the backend fee amount. */
const bankTransferFeeNote = (
  data: MonthBankReconciliationSummary,
  canViewFinance: boolean,
): string =>
  financeDisplay(data.transfer_fee_usd, canViewFinance, {
    currency: BANK_RECONCILIATION_USD_CURRENCY,
    placeholder: "No fee",
  });

/** Build the bank-gap secondary note from the backend reconciliation summary. */
const bankGapSecondaryNote = (
  data: MonthBankReconciliationSummary,
  canViewFinance: boolean,
): string => {
  if (data.bank_gap_usd === null) {
    return "Needs both sources";
  }
  return `Tolerance ${financeDisplay(data.tolerance_usd, canViewFinance, {
    currency: BANK_RECONCILIATION_USD_CURRENCY,
  })}`;
};

// ============================================================================
// Purpose: Convert the backend month bank-reconciliation summary into the three
//   Command Center metric cards without recomputing official finance values.
// Database/ORM: None (frontend). Consumes backend values derived from AdSense
//   payments and bank_reconciliation_entries.
// Standards: Official money values are rendered only through financeDisplay;
//   notes distinguish paid USD payments from unsupported non-USD payment rows.
// Blast Radius: Finance display only. No mutation, export, lock, or audit write.
// Connections:
//   - File: frontend/src/lib/api/types.ts -> MonthBankReconciliationSummary.
//   - File: backend/ums_smart_revenue/finance/bank_reconciliation.py -> money
//     fields and provenance serialized by MonthBankReconciliationSummary.to_api().
// ============================================================================
const buildBankReconciliationMetrics = (
  data: MonthBankReconciliationSummary,
  canViewFinance: boolean,
): BankReconciliationMetric[] => {
  const currency = BANK_RECONCILIATION_USD_CURRENCY;
  const status = bankReconciliationStatusCopy(data.status);
  return [
    {
      id: "adsense-payment",
      label: "AdSense payment",
      value: financeDisplay(data.adsense_paid_amount_usd, canViewFinance, { currency }),
      badge: adsensePaymentBadge(data),
      note: [adsensePaymentPrimaryNote(data), adsensePaymentSecondaryNote(data)],
      tone: adsensePaymentTone(data),
    },
    {
      id: "bank-received",
      label: "Bank received",
      value: financeDisplay(data.bank_received_amount_usd, canViewFinance, { currency }),
      badge: bankReceiptBadge(data),
      note: [bankReceiptSourceNote(data), bankTransferFeeNote(data, canViewFinance)],
      tone: bankReceiptTone(data),
    },
    {
      id: "bank-gap",
      label: "Unresolved gap",
      value: financeDisplay(data.bank_gap_usd, canViewFinance, { currency }),
      badge: { text: status.badge, tone: status.tone },
      note: [status.label, bankGapSecondaryNote(data, canViewFinance)],
      tone: status.metricTone,
    },
  ];
};

// ============================================================================
// Purpose: Translate bank-reconciliation fetch failures into safe inline copy
//   for the strip without exposing backend diagnostic details.
// Database/ORM: None (frontend).
// Standards: 403s get a role/permission message; non-permission API failures
//   retain only the status code and a generic retryable detail.
// Blast Radius: Error display only. No authorization, finance, audit, or export
//   behavior changes.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> ApiError status boundary.
//   - File: backend/ums_smart_revenue/api/revenue.py -> endpoint may return
//     typed 403/422/5xx responses that must not leak details in this card.
// ============================================================================
const bankReconciliationErrorCopy = (
  error: ApiError | Error,
): { title: string; detail: string } => {
  if (error instanceof ApiError) {
    if (error.status === 403) {
      return {
        title: "No permission",
        detail: "Your role cannot view payment and bank reconciliation for this month.",
      };
    }
    return {
      title: `Request failed (${error.status})`,
      detail: "Could not load payment and bank reconciliation for this month.",
    };
  }
  return {
    title: "Network error",
    detail: "Could not reach the bank reconciliation service.",
  };
};

/** Map reconciliation error copy to the badge tone shown in the status strip. */
const bankReconciliationErrorTone = (title: string): Severity => {
  if (title === "No permission") {
    return "blue";
  }
  return "red";
};

/** Return true while reconciliation is loading without a prior response. */
const isInitialBankReconciliationLoad = (
  loading: boolean,
  data: MonthBankReconciliationSummary | null,
): boolean => loading && !data;

/** Render the shared semantic shell for payment-reconciliation status states. */
const BankReconciliationShell = ({
  children,
  role,
  busy = false,
}: {
  children: ReactNode;
  role?: "alert";
  busy?: boolean;
}) => (
  <section
    className="status-strip reconciliation-strip"
    aria-label="Payment reconciliation summary"
    aria-busy={busy ? true : undefined}
    role={role}
  >
    {children}
  </section>
);

/** Render the fail-closed reconciliation placeholder when permission is absent. */
const RestrictedBankReconciliationStrip = () => (
  <BankReconciliationShell>
    {BANK_RECONCILIATION_CARD_LABELS.map((label) => (
      <article key={label} className="metric is-review">
        <header>
          <span className="metric-label">{label}</span>
          <Badge tone="red">Restricted</Badge>
        </header>
        <div className="metric-value finance-data">{RESTRICTED_FINANCE_VALUE}</div>
        <div className="metric-note">
          <span>Payment and bank permissions required</span>
        </div>
      </article>
    ))}
  </BankReconciliationShell>
);

/** Render a contained reconciliation error without masking it as empty data. */
const BankReconciliationErrorStrip = ({ error }: { error: ApiError | Error }) => {
  const { title, detail } = bankReconciliationErrorCopy(error);
  return (
    <BankReconciliationShell role="alert">
      <article className="metric is-risk">
        <header>
          <span className="metric-label">Payment reconciliation unavailable</span>
          <Badge tone={bankReconciliationErrorTone(title)}>{title}</Badge>
        </header>
        <div className="metric-value">—</div>
        <div className="metric-note">
          <span>{detail}</span>
        </div>
      </article>
    </BankReconciliationShell>
  );
};

/** Render loading placeholders for the selected month's reconciliation cards. */
const LoadingBankReconciliationStrip = ({ month }: { month: string }) => (
  <BankReconciliationShell busy>
    {BANK_RECONCILIATION_CARD_LABELS.map((label) => (
      <article key={label} className="metric">
        <header>
          <span className="metric-label">{label}</span>
          <Badge tone="blue">Loading</Badge>
        </header>
        <div className="metric-value">…</div>
        <div className="metric-note">
          <span>Fetching reconciliation</span>
          <span>{month}</span>
        </div>
      </article>
    ))}
  </BankReconciliationShell>
);

/** Render the honest empty reconciliation state when no rows were returned. */
const EmptyBankReconciliationStrip = () => (
  <BankReconciliationShell>
    <article className="metric is-review">
      <header>
        <span className="metric-label">Payment reconciliation</span>
        <Badge tone="amber">Empty</Badge>
      </header>
      <div className="metric-value">—</div>
      <div className="metric-note">
        <span>No reconciliation summary returned</span>
      </div>
    </article>
  </BankReconciliationShell>
);

/** Render the loaded reconciliation metrics supplied by the backend. */
const PopulatedBankReconciliationStrip = ({
  metrics,
}: {
  metrics: BankReconciliationMetric[];
}) => (
  <BankReconciliationShell>
    {metrics.map((metric) => (
      <article key={metric.id} className={`metric ${metric.tone}`}>
        <header>
          <span className="metric-label">{metric.label}</span>
          <Badge tone={metric.badge.tone}>{metric.badge.text}</Badge>
        </header>
        <div className="metric-value finance-data">{metric.value}</div>
        <div className="metric-note">
          <span>{metric.note[0]}</span>
          <span>{metric.note[1]}</span>
        </div>
      </article>
    ))}
  </BankReconciliationShell>
);

// ============================================================================
// Purpose: Payment reconciliation cards for the Command Center. Fetches the
//   month-level bank reconciliation summary independently from net revenue and
//   smart alerts, then displays the backend-sourced AdSense paid amount, bank
//   received amount, and unresolved bank gap.
// Database/ORM: None (frontend) — consumes GET /revenue/months/{month}/bank-reconciliation.
// Standards: Official finance values are backend strings rendered via
//   financeDisplay; USD-suffixed fields are always rendered as USD with no
//   browser-side finance calculation. The read is disabled
//   unless the session has both backend-derived payment and bank-reconciliation
//   read grants, and endpoint 403s render inside this strip without replacing
//   the rest of CommandView.
// Blast Radius: Finance display only. No mutation, export, lock, or audit write
//   from the browser beyond the backend endpoint's read audit events.
// Connections:
//   - File: frontend/src/lib/api/useBankReconciliation.ts -> the fetch hook.
//   - File: frontend/src/lib/api/types.ts -> MonthBankReconciliationSummary.
//   - File: backend/ums_smart_revenue/api/revenue.py -> get_month_bank_reconciliation().
// ============================================================================
const BankReconciliationDataStrip = ({ month }: { month: string }) => {
  const { data, loading, error } = useBankReconciliation({
    month,
    enabled: true,
  });

  if (error) {
    return <BankReconciliationErrorStrip error={error} />;
  }

  if (isInitialBankReconciliationLoad(loading, data)) {
    return <LoadingBankReconciliationStrip month={month} />;
  }

  if (!data) {
    return <EmptyBankReconciliationStrip />;
  }

  const metrics = buildBankReconciliationMetrics(data, true);
  return <PopulatedBankReconciliationStrip metrics={metrics} />;
};

/** Select the reconciliation strip state from permission and request lifecycle. */
const BankReconciliationStatusStrip = ({
  month,
  canViewBankReconciliationSummary,
}: {
  month: string;
  canViewBankReconciliationSummary: boolean;
}) => {
  if (!canViewBankReconciliationSummary) {
    return <RestrictedBankReconciliationStrip />;
  }

  return <BankReconciliationDataStrip month={month} />;
};

// Leg/month gap-explanation status codes -> badge tones. INCOMPLETE is the
// worst status (finance cannot see the whole chain), so it reads red like
// UNEXPLAINED; unknown codes stay neutral.
const GAP_STATUS_TONES: Record<string, Severity> = {
  MATCHED: "green",
  FULLY_EXPLAINED: "green",
  PARTIALLY_EXPLAINED: "amber",
  UNEXPLAINED: "red",
  INCOMPLETE: "red",
};

/** Tone for a gap-explanation status code; unknown codes stay neutral. */
const gapStatusTone = (status: string): Severity => GAP_STATUS_TONES[status] ?? "blue";

/** Pluralized evidence-count sub-line for a gap component row. */
const gapEvidenceSub = (count: number): string =>
  count === 1 ? "1 source row" : `${count} source rows`;

// Display descriptor for one chain leg: every money value is pre-formatted in
// buildGapLegDescriptors so the row components stay pure presentation.
type GapComponentRow = {
  key: string;
  label: string;
  evidenceSub: string;
  amountDisplay: string;
  confidenceLabel: string;
};

type GapLegDescriptor = {
  id: string;
  label: string;
  operands: string;
  status: string;
  components: GapComponentRow[];
  residualDisplay: string;
  residualConfidenceLabel: string;
  // The leg's API-provided deterministic narrative. Load-bearing on an
  // INCOMPLETE leg: it names WHICH operand source is missing — the design
  // deliberately emits no duplicate incompleteness warning because this
  // sentence carries the reason.
  narrative: string;
};

// Element-level defensive read: a malformed payload can pass the container
// checks with null/primitive/array ELEMENTS (e.g. components: [null] or
// [[]]), which would crash or render undefined fields on the first property
// dereference. Keep only plain-object rows — arrays are objects to typeof
// and must be excluded explicitly.
const objectRowsOf = <T,>(rows: T[]): T[] =>
  (Array.isArray(rows) ? rows : []).filter(
    (row) => typeof row === "object" && row !== null && !Array.isArray(row),
  );

// Fallback copy when a malformed payload drops a leg's narrative: the
// residual row's sub-line is load-bearing (on an INCOMPLETE leg it is the
// only place naming the missing source), so a blank line is data loss —
// say explicitly that the explanation is missing instead.
const MISSING_LEG_NARRATIVE = "No explanation returned for this leg.";

/** The leg narrative, or the explicit missing-narrative copy — never blank. */
const legNarrativeOf = (narrative: string): string =>
  typeof narrative === "string" && narrative.trim() ? narrative : MISSING_LEG_NARRATIVE;

/** Pre-format one leg's component rows (display strings only). */
const buildGapComponentRows = (
  components: GapExplanationComponent[],
  money: (value: string | null) => string,
): GapComponentRow[] =>
  // Read defensively (the safeAlerts precedent): a missing/non-array field —
  // or a non-object element inside a well-formed array — renders as no row
  // rather than throwing inside the panel.
  objectRowsOf(components).map((component) => ({
    key: component.key,
    label: component.label,
    evidenceSub: gapEvidenceSub(component.evidence_count),
    amountDisplay: money(component.amount_usd),
    confidenceLabel: component.confidence?.label ?? "—",
  }));

// Read the payload defensively before rendering legs: a malformed body (e.g.
// a contract drift serving {} with HTTP 200) must degrade to the empty state,
// never crash CommandView — the safeAlerts precedent.
const isRenderableGapExplanation = (
  data: MonthGapExplanation | null,
): data is MonthGapExplanation => Boolean(data?.payment_leg && data?.bank_leg);

// Translate gap-explanation fetch failures into safe inline copy for THIS
// panel (the bankReconciliationErrorCopy idiom): 403s get a role/permission
// message naming the gap narrative — the capability gate is a coarse hint,
// so a month-scope-mismatched grant can still 403 here and must read
// accurately — while other failures keep only the status code.
const gapNarrativeErrorCopy = (
  error: ApiError | Error,
): { title: string; detail: string } => {
  if (error instanceof ApiError) {
    if (error.status === 403) {
      return {
        title: "No permission",
        detail: "Your role cannot view the gap narrative for this month.",
      };
    }
    return {
      title: `Request failed (${error.status})`,
      detail: "Could not load the gap narrative for this month.",
    };
  }
  return {
    title: "Network error",
    detail: "Could not reach the gap explanation service.",
  };
};

// ============================================================================
// Purpose: Convert the composed gap explanation into the two display-ready leg
//   descriptors (operand chain, component rows, residual). Pure formatting of
//   backend decimal strings — every gap, component sum, and residual is
//   backend-computed; the browser never derives a finance number.
// Database/ORM: None (frontend) — formats the gap-explanation payload only.
// Standards: Money renders via financeDisplay (permission-gated sentinel);
//   signs pass through untouched; confidence labels come from the API and
//   are never recomputed; missing/malformed fields degrade to "—" instead of
//   throwing (the panel's defensive-read contract).
// Blast Radius: Gap-narrative display only. No mutation, no authorization,
//   no finance calculation.
// Connections:
//   - File: backend/ums_smart_revenue/finance/gap_explanation.py -> the money
//     and status semantics rendered here.
// ============================================================================
const buildGapLegDescriptors = (
  data: MonthGapExplanation,
  canViewFinance: boolean,
  currency: string,
): GapLegDescriptor[] => {
  /** Format a gap-leg amount through the shared finance visibility guard. */
  const money = (value: string | null) => financeDisplay(value, canViewFinance, { currency });
  const payment = data.payment_leg;
  const bank = data.bank_leg;
  return [
    {
      id: "payment-leg",
      label: "Payment leg · YouTube revenue → AdSense paid",
      operands:
        `${money(payment.youtube_revenue_total_usd)} → ` +
        `${money(payment.adsense_paid_amount_usd)} · gap ${money(payment.payment_gap_usd)}`,
      status: payment.status,
      components: buildGapComponentRows(payment.components, money),
      residualDisplay: money(payment.unexplained_residual_usd),
      residualConfidenceLabel: payment.unexplained_residual_confidence?.label ?? "—",
      narrative: legNarrativeOf(payment.narrative),
    },
    {
      id: "bank-leg",
      label: "Bank leg · AdSense paid → bank received",
      operands:
        `${money(bank.adsense_paid_amount_usd)} → ` +
        `${money(bank.bank_received_amount_usd)} · gap ${money(bank.bank_gap_usd)}`,
      status: bank.status,
      components: buildGapComponentRows(bank.components, money),
      residualDisplay: money(bank.unexplained_residual_usd),
      residualConfidenceLabel: bank.unexplained_residual_confidence?.label ?? "—",
      narrative: legNarrativeOf(bank.narrative),
    },
  ];
};

/** One leg: header row (operands → gap), component rows, and the residual row. */
const GapLegRows = ({ leg }: { leg: GapLegDescriptor }) => (
  <>
    <ItemRow
      tone={gapStatusTone(leg.status)}
      title={leg.label}
      sub={leg.operands}
      trailing={<Badge tone={gapStatusTone(leg.status)}>{leg.status}</Badge>}
    />
    {leg.components.map((component) => (
      <ItemRow
        key={`${leg.id}-${component.key}`}
        tone="blue"
        title={component.label}
        sub={component.evidenceSub}
        trailing={
          <>
            <span className="money finance-data">{component.amountDisplay}</span>
            <Badge tone={confidenceDisplay("", component.confidenceLabel).tone}>
              {component.confidenceLabel}
            </Badge>
          </>
        }
      />
    ))}
    <ItemRow
      tone={gapStatusTone(leg.status)}
      title="Unexplained residual"
      // The leg's own narrative, not generic copy: on an INCOMPLETE leg this
      // sentence is the ONLY place naming which operand source is missing.
      sub={leg.narrative}
      trailing={
        <>
          <span className="money finance-data">{leg.residualDisplay}</span>
          <Badge tone={confidenceDisplay("", leg.residualConfidenceLabel).tone}>
            {leg.residualConfidenceLabel}
          </Badge>
        </>
      }
    />
  </>
);

/** Header badge for the gap-narrative panel: error / loading / empty / status. */
const GapNarrativeHeaderBadge = ({
  data,
  loading,
  error,
}: {
  data: MonthGapExplanation | null;
  loading: boolean;
  error: ApiError | Error | null;
}) => {
  if (error) {
    return <Badge tone="blue">—</Badge>;
  }
  if (loading && !data) {
    return <Badge tone="blue">Loading</Badge>;
  }
  if (!isRenderableGapExplanation(data)) {
    return <Badge tone="amber">Empty</Badge>;
  }
  return <Badge tone={gapStatusTone(data.status)}>{data.status}</Badge>;
};

// Read warnings defensively (the safeAlerts precedent): a missing/non-array
// field — or a non-object element inside the array — renders as no warning
// rows rather than throwing inside the panel.
const safeGapWarnings = (data: MonthGapExplanation) => objectRowsOf(data.warnings);

/** Loading/empty fallback rows shown before a renderable payload exists. */
const GapNarrativePendingRows = ({
  loading,
  data,
}: {
  loading: boolean;
  data: MonthGapExplanation | null;
}) =>
  loading && !data ? (
    <div className="issue-list" role="list" aria-busy="true">
      <ItemRow
        tone="blue"
        title="Loading gap narrative…"
        sub="Decomposing the payment and bank gaps"
        trailing={<Badge tone="blue">Loading</Badge>}
      />
    </div>
  ) : (
    <div className="issue-list" role="list">
      <ItemRow
        tone="amber"
        title="No gap explanation returned"
        sub="The month may not have finance data yet."
        trailing={<Badge tone="amber">Empty</Badge>}
      />
    </div>
  );

/** Read-only close-state badge for the month narrative row. */
const GapCloseBadge = ({ closeStatus }: { closeStatus: string }) => (
  <Badge tone={closeStatus === "LOCKED" ? "amber" : "green"}>{closeStatus}</Badge>
);

/** Body of the gap-narrative panel: error, loading, empty, and data states. */
const GapNarrativeBody = ({
  data,
  loading,
  error,
}: {
  data: MonthGapExplanation | null;
  loading: boolean;
  error: ApiError | Error | null;
}) => {
  if (error) {
    const { title, detail } = gapNarrativeErrorCopy(error);
    return (
      <div className="issue-list" role="alert">
        <ItemRow tone="blue" title={title} sub={detail} trailing={<Badge tone="blue">—</Badge>} />
      </div>
    );
  }

  if (!isRenderableGapExplanation(data)) {
    return <GapNarrativePendingRows loading={loading} data={data} />;
  }

  // The mount condition (the backend's FOUR-gate set: global revenue +
  // global confidence + month-satisfying payments and bank grants) already
  // embeds finance visibility, so money renders unrestricted here — the
  // bank strip's pattern. Every one of the four gates is load-bearing for
  // this bypass of financeDisplay redaction.
  const legs = buildGapLegDescriptors(data, true, data.currency);
  return (
    <div className="issue-list" role="list">
      {legs.map((leg) => (
        <GapLegRows key={leg.id} leg={leg} />
      ))}
      <ItemRow
        tone={gapStatusTone(data.status)}
        title="Month narrative"
        sub={data.narrative}
        trailing={<GapCloseBadge closeStatus={data.close_status} />}
      />
      {safeGapWarnings(data).map((warning) => (
        <ItemRow
          key={warning.code}
          tone="amber"
          title={warning.message}
          sub={warning.code}
          trailing={<Badge tone="amber">Warning</Badge>}
        />
      ))}
    </div>
  );
};

// One id literal for the panel heading — shared by both panel variants and
// their aria-labelledby references so the identifier cannot drift.
const GAP_NARRATIVE_TITLE_ID = "gapNarrativeTitle";

/** Restricted variant: no fetch, no money — permission copy only. */
const RestrictedGapNarrativePanel = () => (
  <section className="panel" aria-labelledby={GAP_NARRATIVE_TITLE_ID} style={{ marginBottom: 16 }}>
    <div className="panel-header">
      <div className="panel-title">
        <strong id={GAP_NARRATIVE_TITLE_ID}>Gap narrative</strong>
        {/* Same holding-wide/all-scopes signal as the data variant so the
            restricted state cannot read as scope-selector-dependent. */}
        <span>Holding-wide payment and bank gaps (all scopes)</span>
      </div>
      <Badge tone="red">Restricted</Badge>
    </div>
    <div className="permission-band" role="note">
      <ItemRow
        tone="red"
        title="Gap narrative restricted"
        sub={
          "Revenue, confidence, payment, and bank permissions for this month " +
          `are required. ${RESTRICTED_FINANCE_VALUE}.`
        }
        trailing={<Badge tone="red">Restricted</Badge>}
      />
    </div>
  </section>
);

/** Data variant: owns the gap-explanation fetch and fails independently. */
const GapNarrativeDataPanel = ({ month }: { month: string }) => {
  const { data, loading, error, reload } = useGapExplanation({ month });

  return (
    <section
      className="panel"
      aria-labelledby={GAP_NARRATIVE_TITLE_ID}
      style={{ marginBottom: 16 }}
    >
      <div className="panel-header">
        <div className="panel-title">
          <strong id={GAP_NARRATIVE_TITLE_ID}>Gap narrative</strong>
          {/* Holding-wide by contract: the composed endpoint aggregates ALL
              tenant facts/payments/bank entries — it does NOT follow the
              view's company/sector scope selector, and the label must say
              so next to scope-following panels. */}
          <span>Holding-wide payment and bank gaps for {month} (all scopes)</span>
        </div>
        <GapNarrativeHeaderBadge data={data} loading={loading} error={error} />
        <button
          type="button"
          className="icon-button"
          aria-label="Refresh gap narrative"
          title="Refresh gap narrative"
          onClick={reload}
        >
          ↻
        </button>
      </div>
      <GapNarrativeBody data={data} loading={loading} error={error} />
    </section>
  );
};

// ============================================================================
// Purpose: Gap narrative panel for the Command Center (Hard Problem #3).
//   Renders the composed month gap explanation — both chain legs as compact
//   rows (operands → gap), the evidence components with confidence badges
//   (this is where fx_difference_usd finally reaches the UI), each leg's
//   unexplained residual, the month narrative line, and any data warnings.
// Database/ORM: None (frontend) — consumes GET /revenue/months/{month}/gap-explanation.
// Standards: Mounts its data variant ONLY behind the backend's FOUR-gate set
//   (the smart-alerts set): VIEW_REVENUE @ global + VIEW_CONFIDENCE @ global
//   (the response is confidence-bearing) + VIEW_FINALIZED_PAYMENTS +
//   VIEW_BANK_RECONCILIATION satisfied for the SELECTED month via the
//   month-resolution grant hints; anything less renders the restricted band
//   and fires NO request. Money values are backend strings formatted for
//   display; the browser derives no finance number. Fails independently of
//   the surrounding view (SmartAlertsPanel template).
// Blast Radius: Finance display only. Read-only — the backend endpoint's
//   read audit events are its only side effect.
// Connections:
//   - File: frontend/src/lib/api/useGapExplanation.ts -> the fetch hook.
//   - File: frontend/src/lib/api/types.ts -> MonthGapExplanation contract.
//   - File: backend/ums_smart_revenue/api/revenue.py -> get_month_gap_explanation.
// ============================================================================
const GapNarrativePanel = ({
  month,
  canViewGapNarrative,
}: {
  month: string;
  canViewGapNarrative: boolean;
}) => {
  if (!canViewGapNarrative) {
    return <RestrictedGapNarrativePanel />;
  }

  return <GapNarrativeDataPanel month={month} />;
};

/** Static header row for the channel revenue table. */
const ChannelTableHead = () => {
  return (
    <thead>
      <tr>
        <th scope="col">Channel</th>
        <th scope="col">Status</th>
        <th scope="col">Gross</th>
        <th scope="col">Deductions</th>
        <th scope="col">Net</th>
        <th scope="col">Confidence</th>
        <th scope="col">Issues</th>
      </tr>
    </thead>
  );
};

/** Avatar + name + source-kind cell for a channel row. */
const ChannelNameCell = ({ channel }: { channel: ChannelNetRevenue }) => {
  return (
    <span className="channel-cell">
      <span className="avatar">{channelAvatar(channel)}</span>
      <span className="channel-copy">
        <span className="channel-name">{channelDisplayName(channel)}</span>
        <span className="channel-id">{channel.primary_source_kind ?? "no source"}</span>
      </span>
    </span>
  );
};

/** Single selectable channel row: name, status, permission-gated money, and issues. */
const ChannelRow = ({
  channel,
  canViewFinance,
  currency,
  selected,
  onSelect,
}: {
  channel: ChannelNetRevenue;
  canViewFinance: boolean;
  currency: string;
  selected: boolean;
  onSelect: (id: string) => void;
}) => {
  return (
    <tr
      role="row"
      tabIndex={0}
      aria-selected={selected}
      className={selected ? "is-selected" : undefined}
      onClick={() => onSelect(channel.youtube_channel_id)}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect(channel.youtube_channel_id);
        }
      }}
    >
      <td>
        <ChannelNameCell channel={channel} />
      </td>
      <td>
        <Badge tone={statusTone(channel.status)}>{channel.status}</Badge>
      </td>
      <td className="money finance-data">
        {financeDisplay(channel.adjusted_gross_revenue_usd, canViewFinance, { currency })}
      </td>
      <td className="money finance-data">
        {financeDisplay(channel.deduction_amount_usd, canViewFinance, { currency })}
      </td>
      <td className="money finance-data">
        {financeDisplay(channel.net_revenue_usd, canViewFinance, { currency })}
      </td>
      <td>
        <ConfidenceBadge code={channel.confidence} />
      </td>
      <td>
        {channel.issues.length > 0 ? (
          <Badge tone="amber">{`${channel.issues.length}`}</Badge>
        ) : (
          <span className="muted">None</span>
        )}
      </td>
    </tr>
  );
};

/** Selectable per-channel revenue table with error, loading, and empty states. */
const NetRevenueChannelTable = ({
  data,
  loading,
  error,
  canViewFinance,
  currency,
  selectedChannelId,
  onSelect,
}: {
  data: NetRevenueResponse | null;
  loading: boolean;
  error: ApiError | Error | null;
  canViewFinance: boolean;
  currency: string;
  selectedChannelId: string | null;
  onSelect: (id: string) => void;
}) => {
  if (error) {
    const { title, detail } = describeError(error);
    return (
      <div className="table-wrap" role="alert">
        <div style={{ padding: 16 }}>
          <strong>{title}</strong>
          <p className="item-sub">{detail}</p>
        </div>
      </div>
    );
  }

  if (!data)
    return loading ? (
      <div className="table-wrap" aria-busy="true">
        <div style={{ padding: 16 }} className="item-sub">
          Loading channels…
        </div>
      </div>
    ) : (
      <div className="table-wrap">
        <div style={{ padding: 16 }} className="item-sub">
          No channels for this month and scope.
        </div>
      </div>
    );

  if (data.channels.length === 0)
    return (
      <div className="table-wrap">
        <div style={{ padding: 16 }} className="item-sub">
          No channels for this month and scope.
        </div>
      </div>
    );

  return (
    <div className="table-wrap">
      <table role="grid" aria-label="Channel revenue">
        <ChannelTableHead />
        <tbody>
          {data.channels.map((c) => (
            <ChannelRow
              key={c.youtube_channel_id}
              channel={c}
              canViewFinance={canViewFinance}
              currency={currency}
              selected={c.youtube_channel_id === selectedChannelId}
              onSelect={onSelect}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
};

/** Channel Revenue Table panel: header badge plus the real net-revenue table. */
const ChannelRevenuePanel = ({
  data,
  loading,
  error,
  canViewFinance,
  currency,
  channelCount,
  selectedChannelId,
  onSelect,
}: {
  data: NetRevenueResponse | null;
  loading: boolean;
  error: ApiError | Error | null;
  canViewFinance: boolean;
  currency: string;
  channelCount: number;
  selectedChannelId: string | null;
  onSelect: (id: string) => void;
}) => {
  return (
    <section className="panel channel-table" aria-labelledby="channelTableTitle">
      <div className="panel-header">
        <div className="panel-title">
          <strong id="channelTableTitle">Channel Revenue Table</strong>
          <span>Money values are source-linked and permission-gated</span>
        </div>
        <Badge tone="blue">{`${channelCount} channels`}</Badge>
      </div>
      <NetRevenueChannelTable
        data={data}
        loading={loading}
        error={error}
        canViewFinance={canViewFinance}
        currency={currency}
        selectedChannelId={selectedChannelId}
        onSelect={onSelect}
      />
    </section>
  );
};

/** Explanation rows for the selected channel: gross, deductions, and resulting net. */
const ChannelExplainRows = ({
  channel,
  canViewFinance,
  currency,
}: {
  channel: ChannelNetRevenue;
  canViewFinance: boolean;
  currency: string;
}) => {
  const rows: Array<{ key: string; tone: Severity; title: string; sub: string; value: string | null }> = [
    {
      key: "gross",
      tone: "green",
      title: "Adjusted gross revenue",
      sub: channel.primary_source_kind ?? "no source",
      value: channel.adjusted_gross_revenue_usd,
    },
    {
      key: "direct",
      tone: "blue",
      title: "Channel-direct deductions",
      sub: "Recorded against this channel",
      value: channel.channel_direct_deduction_amount_usd,
    },
    {
      key: "allocated",
      tone: "amber",
      title: "Account-allocated deductions",
      sub: "Share of account-level deductions",
      value: channel.account_allocated_deduction_amount_usd,
    },
    {
      key: "net",
      tone: statusTone(channel.status),
      title: "Net revenue",
      sub: `Status ${channel.status}`,
      value: channel.net_revenue_usd,
    },
  ];

  return (
    <>
      {rows.map((r) => (
        <ItemRow
          key={r.key}
          tone={r.tone}
          title={r.title}
          sub={r.sub}
          className="explain-row"
          trailing={
            <span className="money finance-data">
              {financeDisplay(r.value, canViewFinance, { currency })}
            </span>
          }
        />
      ))}
    </>
  );
};

/** Net-revenue explanation card for the selected channel (or an empty state). */
const ExplainCard = ({
  selectedChannel,
  canViewFinance,
  currency,
  loading,
  month,
}: {
  selectedChannel: ChannelNetRevenue | null;
  canViewFinance: boolean;
  currency: string;
  loading: boolean;
  month: string;
}) => {
  return (
    <section className="panel explain-card">
      <div className="explain-head">
        <div>
          <h2>{selectedChannel ? channelDisplayName(selectedChannel) : "No channel"}</h2>
          <p>Net revenue explanation, {month}</p>
        </div>
        {selectedChannel ? (
          <ConfidenceBadge code={selectedChannel.confidence} />
        ) : (
          <Badge tone="blue">—</Badge>
        )}
      </div>
      <div className="formula" role="text" aria-label="Revenue formula">
        net = adjusted_gross - channel_direct_deductions - account_allocated_deductions
      </div>
      <div className="explain-list" role="list">
        {selectedChannel ? (
          <ChannelExplainRows
            channel={selectedChannel}
            canViewFinance={canViewFinance}
            currency={currency}
          />
        ) : (
          <ItemRow
            tone="blue"
            title="No channel selected"
            sub={loading ? "Loading net revenue…" : "No channels in this scope/month"}
            className="explain-row"
            trailing={<span className="muted">—</span>}
          />
        )}
      </div>
    </section>
  );
};

/**
 * Two-column Command Center workspace: the real channel table beside the real
 * explanation card. Both read the SAME net-revenue response the parent fetched,
 * so the workspace can never show a number the table does not.
 */
const CommandWorkspace = ({
  data,
  loading,
  error,
  canViewFinance,
  currency,
  channelCount,
  selectedChannel,
  selectedChannelId,
  month,
  onSelect,
}: {
  data: NetRevenueResponse | null;
  loading: boolean;
  error: ApiError | Error | null;
  canViewFinance: boolean;
  currency: string;
  channelCount: number;
  selectedChannel: ChannelNetRevenue | null;
  selectedChannelId: string | null;
  month: string;
  onSelect: (id: string) => void;
}) => {
  return (
    <section className="workspace" aria-label="Command workspace">
      <div className="work-left">
        <ChannelRevenuePanel
          data={data}
          loading={loading}
          error={error}
          canViewFinance={canViewFinance}
          currency={currency}
          channelCount={channelCount}
          selectedChannelId={selectedChannelId}
          onSelect={onSelect}
        />
      </div>

      {/* explain card — REAL data, derived from the selected net-revenue row */}
      <aside className="side-stack" aria-label="Explanation">
        <ExplainCard
          selectedChannel={selectedChannel}
          canViewFinance={canViewFinance}
          currency={currency}
          loading={loading}
          month={month}
        />
      </aside>
    </section>
  );
};

/** Restricted placeholder: NO hook mounted, NO request fired (fail-closed). */
const OutsideCmsRestrictedBand = () => {
  return (
    <div className="permission-band" role="note">
      <ItemRow
        tone="red"
        title="Channel monitor restricted"
        sub="Analytics access is required to view outside-CMS and channel issues."
        trailing={<Badge tone="red">Restricted</Badge>}
      />
    </div>
  );
};

/** Summary tiles: outside-CMS count, missing-official-revenue, open issues. */
const OutsideCmsSummaryTiles = ({
  outsideCms,
  issues,
}: {
  outsideCms: OutsideCmsResponse | null;
  issues: ChannelIssuesResponse | null;
}) => {
  // Read defensively: an unexpected body shape (missing summary) renders the
  // dash placeholder rather than throwing inside the panel.
  const outsideCount = outsideCms?.summary?.outside_cms_channel_count ?? null;
  const missingCount = outsideCms?.summary?.missing_official_revenue_count ?? null;
  const openCount = issues?.summary?.total_issue_count ?? null;
  /** Format a monitor count while preserving the empty dash state. */
  const fmt = (value: number | null): string =>
    value === null ? "—" : value.toLocaleString();
  return (
    <div className="view-summary" aria-label="Channel monitor summary">
      <SummaryTile
        label="Outside CMS"
        value={fmt(outsideCount)}
        note="Channels outside a managed CMS"
      />
      <SummaryTile
        label="Missing official revenue"
        value={fmt(missingCount)}
        note="Outside CMS and missing a revenue source"
      />
      <SummaryTile
        label="Open issues"
        value={fmt(openCount)}
        note="Channel registry issues (high + medium)"
      />
    </div>
  );
};

/** One outside-CMS row: distinguishes "covered" from "missing source". */
const OutsideCmsRow = ({ item }: { item: OutsideCmsItem }) => {
  const tone: Severity = item.missing_official_revenue ? "amber" : "green";
  const status = item.missing_official_revenue
    ? "Missing official revenue"
    : "Covered";
  return (
    <ItemRow
      tone={tone}
      title={item.channel_name}
      sub={`${status} · ${item.recommended_action}`}
      trailing={<Badge tone={tone}>{item.revenue_source_status}</Badge>}
    />
  );
};

/** Map a channel-issue severity string to a design-system badge tone. */
const issueSeverityTone = (severity: string): Severity => {
  const normalized = severity.toLowerCase();
  if (normalized === "high") return "red";
  if (normalized === "medium") return "amber";
  return "blue";
};

/** One channel-issue row. */
const ChannelIssueRow = ({ issue }: { issue: ChannelIssue }) => {
  const tone = issueSeverityTone(issue.severity);
  return (
    <ItemRow
      tone={tone}
      title={issue.message}
      sub={`${issue.channel_name} · ${issue.issue_type}`}
      trailing={<Badge tone={tone}>{issue.severity}</Badge>}
    />
  );
};

/**
 * Shared list shell for a monitor half: surfaces a contained error (403 -> denied
 * copy, never masked as "no issues"), a loading state, an empty state, or the
 * caller's rows. Keeps each half's JSX subtree shallow and consistent.
 */
const MonitorList = ({
  error,
  loading,
  empty,
  emptyTitle,
  emptySub,
  forbiddenDetail,
  children,
}: {
  error: ApiError | Error | null;
  loading: boolean;
  empty: boolean;
  emptyTitle: string;
  emptySub: string;
  forbiddenDetail: string;
  children: ReactNode;
}) => {
  if (error) {
    const { title, detail } = describeError(error, forbiddenDetail);
    return (
      <div className="issue-list" role="alert">
        <ItemRow tone="blue" title={title} sub={detail} trailing={<Badge tone="blue">—</Badge>} />
      </div>
    );
  }
  if (loading) {
    return (
      <div className="issue-list" role="list" aria-busy="true">
        <ItemRow
          tone="blue"
          title="Loading…"
          sub="Fetching channel monitor signals"
          trailing={<Badge tone="blue">Loading</Badge>}
        />
      </div>
    );
  }
  if (empty) {
    return (
      <div className="issue-list" role="list">
        <ItemRow
          tone="green"
          title={emptyTitle}
          sub={emptySub}
          trailing={<Badge tone="green">Clear</Badge>}
        />
      </div>
    );
  }
  return (
    <div className="issue-list" role="list">
      {children}
    </div>
  );
};

/** Outside-CMS half: loading / error / empty / row states (fails on its own). */
const OutsideCmsHalf = ({
  data,
  loading,
  error,
}: {
  data: OutsideCmsResponse | null;
  loading: boolean;
  error: ApiError | Error | null;
}) => {
  // Read items defensively so an unexpected body shape cannot throw in the panel.
  const items = Array.isArray(data?.items) ? data.items : [];
  return (
    <section className="panel" aria-labelledby="outsideCmsHalfTitle">
      <div className="panel-header">
        <div className="panel-title">
          <strong id="outsideCmsHalfTitle">Outside CMS</strong>
          <span>Coverage by channel</span>
        </div>
      </div>
      <MonitorList
        error={error}
        loading={loading}
        empty={items.length === 0}
        emptyTitle="No outside-CMS channels"
        emptySub="Every channel in scope is inside a managed CMS."
        forbiddenDetail="Your role cannot view outside-CMS coverage for this scope."
      >
        {items.map((item) => (
          <OutsideCmsRow key={item.youtube_channel_id} item={item} />
        ))}
      </MonitorList>
    </section>
  );
};

/** Channel-issues half: loading / error / empty / row states (fails on its own). */
const ChannelIssuesHalf = ({
  data,
  loading,
  error,
}: {
  data: ChannelIssuesResponse | null;
  loading: boolean;
  error: ApiError | Error | null;
}) => {
  // Read items defensively so an unexpected body shape cannot throw in the panel.
  const items = Array.isArray(data?.items) ? data.items : [];
  return (
    <section className="panel" aria-labelledby="channelIssuesHalfTitle">
      <div className="panel-header">
        <div className="panel-title">
          <strong id="channelIssuesHalfTitle">Channel Issues</strong>
          <span>Registry-health signals</span>
        </div>
      </div>
      <MonitorList
        error={error}
        loading={loading}
        empty={items.length === 0}
        emptyTitle="No channel issues"
        emptySub="No registry-health issues in this scope."
        forbiddenDetail="Your role cannot view channel issues for this scope."
      >
        {items.map((issue) => (
          <ChannelIssueRow
            // FIX: key by channel id + issue_type. The same channel can have
            // multiple issues (e.g. MISSING_SECTOR + OUTSIDE_CMS_REVENUE_REQUIRED);
            // keying on youtube_channel_id alone gives React duplicate keys and
            // can recycle the wrong row across issue-type / refresh changes
            // (review #98 T3).
            key={`${issue.youtube_channel_id}:${issue.issue_type}`}
            issue={issue}
          />
        ))}
      </MonitorList>
    </section>
  );
};

/**
 * The live monitor body: each half owns its own hook so an outside-cms failure
 * cannot blank the issues half (and vice-versa). Only mounted when permitted, so
 * both hooks stay rules-of-hooks safe and fire exactly once at this view root.
 */
const OutsideCmsMonitorBody = () => {
  const outsideCms = useOutsideCmsChannels();
  const issues = useChannelIssues();
  return (
    <>
      <OutsideCmsSummaryTiles
        outsideCms={outsideCms.data}
        issues={issues.data}
      />
      <div className="layout-split">
        <OutsideCmsHalf
          data={outsideCms.data}
          loading={outsideCms.loading}
          error={outsideCms.error}
        />
        <ChannelIssuesHalf
          data={issues.data}
          loading={issues.loading}
          error={issues.error}
        />
      </div>
    </>
  );
};

// ============================================================================
// Purpose: Outside-CMS / channel-issues monitor for the Command Center. Wires
//   the two VIEW_ANALYTICS-gated reads (GET /channels/outside-cms +
//   GET /channels/issues) into one card that replaces the mock "Open issues" KPI
//   and "Outside CMS" tile. NO-FETCH-WHEN-RESTRICTED: the hook-owning halves are
//   mounted ONLY when canViewAnalytics, so a narrower principal fires ZERO
//   requests and sees a restricted placeholder (AuditView gate pattern). A 403
//   renders a denied state (NEVER masked as "no issues" — masking authz is
//   forbidden); a 503/other error renders the typed request-failed copy. Each
//   half owns its hook and fails INDEPENDENTLY (SmartAlertsPanel template).
// Database/ORM: None (frontend) — consumes the backend analytics monitor reads.
// Standards: No money is rendered here (both endpoints carry no finance cells),
//   so no canViewFinance gating is needed. Read-only — no mutation. The backend
//   403 stays authoritative and surfaces as denied copy.
// Blast Radius: Authorization (analytics gating — UI never grants the surface
//   without the backend capability). No finance number, no source-of-truth write.
// Connections:
//   - File: frontend/src/lib/api/useOutsideCmsChannels.ts -> the outside-cms hook.
//   - File: frontend/src/lib/api/useChannelIssues.ts -> the channel-issues hook.
//   - File: frontend/src/lib/api/types.ts -> OutsideCmsResponse/ChannelIssuesResponse.
//   - File: backend/ums_smart_revenue/api/channels.py -> the two monitor routes.
// ============================================================================
const OutsideCmsMonitorPanel = ({
  canViewAnalytics,
}: {
  canViewAnalytics: boolean;
}) => {
  return (
    <section
      className="panel"
      aria-labelledby="outsideCmsTitle"
      style={{ marginBottom: 16 }}
    >
      <div className="panel-header">
        <div className="panel-title">
          <strong id="outsideCmsTitle">Outside-CMS &amp; Channel Issues</strong>
          <span>Coverage and registry-health signals (no money)</span>
        </div>
        <Badge tone={canViewAnalytics ? "blue" : "red"}>
          {canViewAnalytics ? "Live" : "Restricted"}
        </Badge>
      </div>
      {canViewAnalytics ? (
        <OutsideCmsMonitorBody />
      ) : (
        <OutsideCmsRestrictedBand />
      )}
    </section>
  );
};

/** Metric selector for the rankings panel (gross / net / deduction). */
const RankingMetricToggle = ({
  metric,
  onChange,
  disabled,
}: {
  metric: RankingMetric;
  onChange: (next: RankingMetric) => void;
  disabled: boolean;
}) => {
  return (
    <select
      className="control"
      aria-label="Ranking metric"
      value={metric}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value as RankingMetric)}
    >
      <option value="gross">Gross</option>
      <option value="net">Net</option>
      <option value="deduction">Deduction</option>
    </select>
  );
};

/** Restricted placeholder: NO hook mounted, NO request fired (fail-closed). */
const RankingsRestrictedBand = () => {
  return (
    <div className="permission-band" role="note">
      <ItemRow
        tone="red"
        title="Rankings restricted"
        sub={`Finance visibility is required to view ranked revenue. ${RESTRICTED_FINANCE_VALUE}.`}
        trailing={<Badge tone="red">Restricted</Badge>}
      />
    </div>
  );
};

/** Select the money value for the active metric from a ranked entry. */
const rankingMetricValue = (
  row: RankedEntry,
  metric: RankingMetric,
): string | null => {
  if (metric === "net") return row.net_revenue_usd;
  if (metric === "deduction") return row.deduction_amount_usd;
  return row.gross_revenue_usd;
};

/** One ranked dimension (companies / sectors / channels): rows or empty state. */
const RankingDimension = ({
  title,
  rows,
  metric,
  canViewFinance,
}: {
  title: string;
  rows: RankedEntry[];
  metric: RankingMetric;
  canViewFinance: boolean;
}) => {
  const safeRows = Array.isArray(rows) ? rows : [];
  return (
    <section className="panel" aria-label={`${title} ranking`}>
      <div className="panel-header">
        <div className="panel-title">
          <strong>{title}</strong>
          <span>Top {safeRows.length} by {metric}</span>
        </div>
      </div>
      <div className="issue-list" role="list">
        {safeRows.length === 0 ? (
          <ItemRow
            tone="blue"
            title="No entries"
            sub="No ranked entities in this scope."
            trailing={<Badge tone="blue">—</Badge>}
          />
        ) : (
          safeRows.map((row) => (
            <ItemRow
              key={row.entity_id}
              tone="blue"
              title={row.entity_name}
              sub={`#${row.rank} · ${row.entity_id}`}
              trailing={
                <span className="money finance-data">
                  {financeDisplay(rankingMetricValue(row, metric), canViewFinance, {
                    currency: "USD",
                  })}
                </span>
              }
            />
          ))
        )}
      </div>
    </section>
  );
};

/** Resolved rankings content: allocation source + the three ranked dimensions. */
const RankingsContent = ({
  data,
  metric,
  canViewFinance,
}: {
  data: MonthRankingsResponse | null;
  metric: RankingMetric;
  canViewFinance: boolean;
}) => {
  if (!data) {
    return (
      <div className="issue-list" role="list">
        <ItemRow
          tone="amber"
          title="No rankings"
          sub="No ranking data returned for this month and scope."
          trailing={<Badge tone="amber">Empty</Badge>}
        />
      </div>
    );
  }
  const allocation = data.allocation_source
    ? ALLOCATION_SOURCE_COPY[data.allocation_source]
    : null;
  return (
    <>
      {allocation ? (
        <div className="control-row" aria-label="Rankings provenance" style={{ marginBottom: 8 }}>
          <Badge tone={allocation.tone}>{allocation.label}</Badge>
        </div>
      ) : null}
      <div className="layout-split">
        <RankingDimension
          title="Companies"
          rows={data.companies}
          metric={metric}
          canViewFinance={canViewFinance}
        />
        <RankingDimension
          title="Sectors"
          rows={data.sectors}
          metric={metric}
          canViewFinance={canViewFinance}
        />
      </div>
      <RankingDimension
        title="Channels"
        rows={data.channels}
        metric={metric}
        canViewFinance={canViewFinance}
      />
    </>
  );
};

/**
 * Live rankings body: owns useRankings (only mounted when canViewFinance) and
 * renders the three ranked dimensions plus the allocation-source provenance.
 */
const RankingsBody = ({
  month,
  metric,
  canViewFinance,
  scopeType,
  scopeId,
  scopesReady,
}: {
  month: string;
  metric: RankingMetric;
  canViewFinance: boolean;
  // FIX: Thread the active Command Center scope (global | sector | company |
  // channel) into the rankings request. The endpoint supports a scoped read;
  // without these props the hook defaulted to global every time, leaving a
  // sector/company/channel viewer looking at a global top-N while the rest of
  // the screen showed scoped numbers (review #98 T2: dashboard-internal
  // consistency + out-of-scope leak).
  scopeType: string;
  scopeId: string | null;
  // FIX (review #102 Qodo #3): Gate the rankings read until the authorized-scope
  // verdict resolves (see CommandView.scopesReady).
  scopesReady: boolean;
}) => {
  const { data, loading, error } = useRankings({
    month,
    metric,
    scopeType,
    scopeId,
    enabled: scopesReady,
  });
  if (error) {
    const { title, detail } = describeError(
      error,
      "Your role cannot view rankings for this month or scope.",
    );
    return (
      <div className="issue-list" role="alert">
        <ItemRow tone="blue" title={title} sub={detail} trailing={<Badge tone="blue">—</Badge>} />
      </div>
    );
  }
  if (loading && !data) {
    return (
      <div className="issue-list" role="list" aria-busy="true">
        <ItemRow
          tone="blue"
          title="Loading rankings…"
          sub="Ranking companies, sectors, and channels"
          trailing={<Badge tone="blue">Loading</Badge>}
        />
      </div>
    );
  }
  return <RankingsContent data={data} metric={metric} canViewFinance={canViewFinance} />;
};

// ============================================================================
// Purpose: Company/sector/channel rankings panel for the Command Center. Wires
//   the finance-gated GET /revenue/months/{month}/rankings into a card with a
//   metric toggle (gross/net/deduction). FINANCE-GATED: the hook-owning body is
//   mounted ONLY when canViewFinance (the panel shows money), so a non-finance
//   viewer fires ZERO requests and sees a restricted placeholder. Money is
//   rendered via financeDisplay; None is preserved (never coalesced to 0). The
//   panel surfaces allocation_source so a `live_fallback` is not read as
//   authoritative. Owns its hook and fails INDEPENDENTLY (SmartAlertsPanel
//   template): a 403/error renders inside this card only.
// Database/ORM: None (frontend) — consumes the backend finance rankings read.
// Standards: Backend money values are decimal-as-strings; display-only formatting
//   via financeDisplay, gated on canViewFinance. The backend finance 403 stays
//   authoritative and surfaces as denied copy. Read-only — no mutation.
// Blast Radius: Finance display (permission-gated money cells). No mutation.
// Connections:
//   - File: frontend/src/lib/api/useRankings.ts -> the fetch hook.
//   - File: frontend/src/lib/api/types.ts -> MonthRankingsResponse/RankedEntry.
//   - File: backend/ums_smart_revenue/api/revenue.py -> get_month_rankings.
// ============================================================================
const RankingsPanel = ({
  month,
  canViewFinance,
  scopeType,
  scopeId,
  scopesReady,
}: {
  month: string;
  canViewFinance: boolean;
  // FIX: Forward the active scope to RankingsBody so the panel re-queries
  // /revenue/months/{month}/rankings with the same scope_type/scope_id the
  // status strip and channel table use, keeping the screen internally
  // consistent and avoiding an out-of-scope global ranking on a scoped view
  // (review #98 T2).
  scopeType: string;
  scopeId: string | null;
  // FIX (review #102 Qodo #3): Gate the rankings read until the authorized-scope
  // verdict resolves so the panel does not fire a global read during the load
  // window (mirrors the net-revenue gate above).
  scopesReady: boolean;
}) => {
  const [metric, setMetric] = useState<RankingMetric>("gross");
  return (
    <section className="panel" aria-labelledby="rankingsTitle" style={{ marginBottom: 16 }}>
      <div className="panel-header">
        <div className="panel-title">
          <strong id="rankingsTitle">Rankings</strong>
          <span>Top companies, sectors, and channels for {month}</span>
        </div>
        <RankingMetricToggle
          metric={metric}
          onChange={setMetric}
          disabled={!canViewFinance}
        />
        <Badge tone={canViewFinance ? "blue" : "red"}>
          {canViewFinance ? "Live" : "Restricted"}
        </Badge>
      </div>
      {canViewFinance ? (
        <RankingsBody
          month={month}
          metric={metric}
          canViewFinance={canViewFinance}
          scopeType={scopeType}
          scopeId={scopeId}
          scopesReady={scopesReady}
        />
      ) : (
        <RankingsRestrictedBand />
      )}
    </section>
  );
};

/**
 * Command Center screen: month/scope filters, the real net-revenue status strip
 * and channel table, the smart-alerts panel, and the per-channel explanation.
 */
// Fail-closed default for the month-resolution grant hints: no global grant,
// no months — a missing hint can never mount a month-bound finance surface.
const NO_FINANCE_MONTH_SCOPES: ScopedFinanceViewHint = {
  globalScope: false,
  financeMonths: [],
};

/** True when a month-bound read can possibly succeed for the selected month. */
const financeMonthHintSatisfies = (
  hint: ScopedFinanceViewHint,
  month: string,
): boolean => hint.globalScope || hint.financeMonths.includes(month);

/** Render the Command Center's scoped finance, alert, monitor, and ranking panels. */
const CommandView = ({
  canViewFinance,
  canViewAnalytics = false,
  canViewPayments = false,
  canViewBankReconciliation = false,
  canViewRevenueGlobal = false,
  canViewConfidence = false,
  paymentsViewScopes = NO_FINANCE_MONTH_SCOPES,
  bankReconciliationViewScopes = NO_FINANCE_MONTH_SCOPES,
}: {
  canViewFinance: boolean;
  // Optional so the existing prop contract (canViewFinance-only) stays valid; a
  // missing flag fails closed (false) — the analytics monitor never mounts and
  // fires no request without an explicit, backend-derived grant.
  canViewAnalytics?: boolean;
  // Optional session-derived gates for the bank reconciliation endpoint. Missing
  // values fail closed so standalone tests cannot accidentally grant this read.
  canViewPayments?: boolean;
  canViewBankReconciliation?: boolean;
  // Global-scope-only revenue gate: the gap-explanation endpoint requires
  // VIEW_REVENUE at GLOBAL scope, which the scope-aware canViewFinance hint
  // cannot certify (a company-scoped revenue viewer holds it too). Missing
  // fails closed.
  canViewRevenueGlobal?: boolean;
  // Confidence-visibility gate: the gap-explanation response carries
  // confidence labels/scores on every component and residual, so its backend
  // boundary also requires global VIEW_CONFIDENCE. Missing fails closed.
  canViewConfidence?: boolean;
  // Month-resolution grant hints for the payments/bank views, so the
  // gap-narrative panel restricts per SELECTED month instead of firing a
  // guaranteed-403 fetch for a month the grants cannot cover. Missing fails
  // closed.
  paymentsViewScopes?: ScopedFinanceViewHint;
  bankReconciliationViewScopes?: ScopedFinanceViewHint;
}) => {
  const [month, setMonth] = useState<string>(DEFAULT_MONTH);
  // Stable {scopeType, scopeId} identity instead of a positional index: the
  // option list arrives asynchronously, so an index would point at the wrong
  // (or a vanished) scope once the fetched set replaces the global-only fallback.
  const [selectedScopeKey, setSelectedScopeKey] = useState<string>(
    scopeOptionKey(GLOBAL_SCOPE_FALLBACK.scopeType, GLOBAL_SCOPE_FALLBACK.scopeId),
  );
  const [selectedChannelId, setSelectedChannelId] = useState<string | null>(null);

  // Fetch the viewer's VIEW_REVENUE-authorized scopes ONCE at the view root. The
  // selector is populated ONLY from these (fail-closed against an org-structure
  // leak); while loading or on a 403/error it degrades to global-only so the
  // screen never blocks (the panels fail-closed on the actual scoped reads).
  const { data: scopesData, error: scopesError } = useRevenueScopes();
  // FIX (review #102 Qodo #3): Hold the net-revenue + rankings reads until the
  // authorized-scope fetch has a verdict (data OR error). While it is still
  // loading, scopesData is null and `scope` resolves to the global fallback —
  // firing it immediately would trigger an unauthorized global read (likely a
  // noisy 403) for a scoped viewer before their real options arrive. Once the
  // scopes fetch resolves (success -> real options, or error -> global fallback),
  // the gated reads fire with the correct scope.
  const scopesReady = useMemo(
    () => scopesData !== null || scopesError !== null,
    [scopesData, scopesError],
  );
  const scopeOptions = useMemo(
    () => resolveScopeOptions(scopesData),
    [scopesData],
  );
  // Resolve the active scope from the stable key, falling back to the first
  // option (always present — resolveScopeOptions guarantees >=1) when the key is
  // not in the current list (e.g. before the fetch resolves). The fallback is
  // global while loading, never an out-of-scope unit.
  const scope = useMemo(
    () =>
      scopeOptions.find(
        (option) => scopeOptionKey(option.scopeType, option.scopeId) === selectedScopeKey,
      ) ??
      scopeOptions[0] ??
      GLOBAL_SCOPE_FALLBACK,
    [scopeOptions, selectedScopeKey],
  );
  const { data, loading, error, reload } = useNetRevenue({
    month,
    scopeType: scope.scopeType,
    scopeId: scope.scopeId,
    enabled: scopesReady,
  });

  const currency = useMemo(() => data?.currency ?? "USD", [data]);
  const channels = useMemo(() => data?.channels ?? [], [data]);
  const selectedChannel = useMemo(
    () =>
      channels.find((c) => c.youtube_channel_id === selectedChannelId) ??
      channels[0] ??
      null,
    [channels, selectedChannelId],
  );
  const activeChannelId = useMemo(
    () => selectedChannel?.youtube_channel_id ?? null,
    [selectedChannel],
  );
  const canViewBankReconciliationSummary = canViewPayments && canViewBankReconciliation;
  // The composed gap-explanation endpoint enforces the smart-alerts gate set
  // (VIEW_REVENUE + VIEW_CONFIDENCE @ global, payments + bank @ the
  // requested finance month) — mirror it client-side so a session that
  // cannot possibly pass renders the restricted band and fires nothing. The
  // revenue term is the GLOBAL-scope capability (a company-scoped revenue
  // viewer must not fire a guaranteed-403 fetch), and the payments/bank
  // terms are MONTH-RESOLUTION hints checked against the SELECTED month: a
  // grant scoped only to another month restricts here instead of fetching.
  // The backend still re-checks every gate — these hints never broaden.
  const canViewGapNarrative =
    canViewRevenueGlobal &&
    canViewConfidence &&
    financeMonthHintSatisfies(paymentsViewScopes, month) &&
    financeMonthHintSatisfies(bankReconciliationViewScopes, month);

  return (
    <>
      {/* month + scope selector */}
      <section className="control-row" aria-label="Net revenue filters" style={{ marginBottom: 16 }}>
        <select
          className="control"
          aria-label="Month"
          value={month}
          onChange={(e) => {
            setMonth(e.target.value);
            setSelectedChannelId(null);
          }}
        >
          {MONTH_OPTIONS.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
        <select
          className="control"
          aria-label="Scope"
          // Drive the shown option from the RESOLVED active scope, not the raw
          // selectedScopeKey: after a /revenue/scopes reload returns a different
          // authorized set, the stored key may name an option no longer present.
          // `scope` already falls back to the first option (global while
          // loading), so the displayed option always matches the scope being read
          // — no desynced selection that reads one scope but shows another.
          value={scopeOptionKey(scope.scopeType, scope.scopeId)}
          onChange={(e) => {
            // Store the stable scope key; the active {scopeType, scopeId} is
            // resolved from it against the current option list. Reset the
            // selected channel so the explain card never shows a channel from
            // the prior scope.
            setSelectedScopeKey(e.target.value);
            setSelectedChannelId(null);
          }}
        >
          {scopeOptions.map((s) => (
            <option
              key={scopeOptionKey(s.scopeType, s.scopeId)}
              value={scopeOptionKey(s.scopeType, s.scopeId)}
            >
              {s.label}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="icon-button"
          aria-label="Refresh net revenue"
          title="Refresh net revenue"
          onClick={reload}
        >
          ↻
        </button>
      </section>

      {/* status strip — REAL net-revenue summary */}
      <NetRevenueStatusStrip
        data={data}
        loading={loading}
        error={error}
        canViewFinance={canViewFinance}
        currency={currency}
      />

      {/* payment/bank reconciliation — REAL data, sourced from the backend summary */}
      <BankReconciliationStatusStrip
        month={month}
        canViewBankReconciliationSummary={canViewBankReconciliationSummary}
      />

      {/* gap narrative — REAL data, composed payment+bank gap decomposition,
          fails independently, no-fetch-when-restricted (the four-gate set:
          global revenue + confidence, month-satisfying payments + bank) */}
      <GapNarrativePanel month={month} canViewGapNarrative={canViewGapNarrative} />

      {/* smart-alerts / problem panel — REAL data, fails independently */}
      <SmartAlertsPanel month={month} />

      {/* outside-CMS + channel-issues monitor — REAL data, fails independently,
          no-fetch-when-restricted (mounts only when canViewAnalytics) */}
      <OutsideCmsMonitorPanel canViewAnalytics={canViewAnalytics} />

      {/* company/sector/channel rankings — REAL data, fails independently,
           finance-gated (shows money; mounts only when canViewFinance) */}
      <RankingsPanel
        month={month}
        canViewFinance={canViewFinance}
        scopeType={scope.scopeType}
        scopeId={scope.scopeId}
        scopesReady={scopesReady}
      />

      <CommandWorkspace
        data={data}
        loading={loading}
        error={error}
        canViewFinance={canViewFinance}
        currency={currency}
        channelCount={channels.length}
        selectedChannel={selectedChannel}
        selectedChannelId={activeChannelId}
        month={month}
        onSelect={setSelectedChannelId}
      />
    </>
  );
};

export default CommandView;

export { describeError };
