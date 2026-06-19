import { useMemo, useState } from "react";
import type { ReactNode } from "react";

import { ApiError } from "@/lib/api/client";
import type {
  ChannelIssue,
  ChannelIssuesResponse,
  ChannelNetRevenue,
  MonthBankReconciliationSummary,
  MonthRankingsResponse,
  NetRevenueResponse,
  OutsideCmsItem,
  OutsideCmsResponse,
  RankedEntry,
  RankingMetric,
  RevenueScopeOption,
  SmartAlert,
  SmartAlertSeverity,
  SmartAlertsSummary,
} from "@/lib/api/types";
import { useBankReconciliation } from "@/lib/api/useBankReconciliation";
import { useChannelIssues } from "@/lib/api/useChannelIssues";
import { useNetRevenue } from "@/lib/api/useNetRevenue";
import { useOutsideCmsChannels } from "@/lib/api/useOutsideCmsChannels";
import { useRankings } from "@/lib/api/useRankings";
import { useRevenueScopes } from "@/lib/api/useRevenueScopes";
import { useSmartAlerts } from "@/lib/api/useSmartAlerts";
import {
  CLOSE_STEPS,
  EXPORT_READINESS,
  ISSUES,
} from "@/lib/mock/data";
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
//   states. Establishes the data-wiring pattern the other six views follow;
//   side panels (Issue Queue, Month Close, Export Readiness) stay on mock data
//   for now and are clearly labelled as such.
// Database/ORM: None (frontend) — consumes GET /revenue/months/{month}/net-revenue.
// Standards: Money values are backend strings, formatted for display only (no
//   float math); finance cells are permission-gated via financeDisplay; 403 ->
//   "no permission" copy, other ApiError -> the typed message.
// Blast Radius: Finance display (permission-gated money cells). No mutation.
// Connections:
//   - File: frontend/src/lib/api/useNetRevenue.ts -> the fetch hook.
//   - File: frontend/src/lib/api/types.ts -> NetRevenueResponse contract.
//   - File: backend/ums_smart_revenue/api/revenue.py:1088 -> the endpoint.
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

const bankReconciliationStatusCopy = (status: string) =>
  BANK_RECONCILIATION_STATUS_COPY[status] ?? {
    label: status,
    badge: status,
    tone: statusTone(status),
    metricTone: "is-payment",
  };

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

/** Mock Issue Queue panel (not yet wired to the API). */
const IssueQueuePanel = () => {
  return (
    <section className="panel" aria-labelledby="issuesTitle">
      <div className="panel-header">
        <div className="panel-title">
          <strong id="issuesTitle">Issue Queue</strong>
          <span>Sample data — not yet wired to the API</span>
        </div>
        <Badge tone="amber">Mock</Badge>
      </div>
      <div className="issue-list" role="list">
        {ISSUES.map((i) => (
          <ItemRow
            key={i.title}
            tone={i.tone}
            title={i.title}
            sub={i.sub}
            trailing={<Badge tone={i.badge.tone}>{i.badge.text}</Badge>}
          />
        ))}
      </div>
    </section>
  );
};

/** Trailing control for a close step: a status badge or an action button. */
const CloseStepAction = ({ step }: { step: (typeof CLOSE_STEPS)[number] }) => {
  if (step.badge) {
    return <Badge tone={step.badge.tone}>{step.badge.text}</Badge>;
  }
  return (
    <button className="mini-button" type="button">
      {step.action}
    </button>
  );
};

/** Mock Month Close Controls panel (not yet wired to the API). */
const MonthCloseControlsPanel = () => {
  return (
    <section className="panel" aria-labelledby="closeTitle">
      <div className="panel-header">
        <div className="panel-title">
          <strong id="closeTitle">Month Close Controls</strong>
          <span>Sample data — not yet wired to the API</span>
        </div>
        <Badge tone="amber">Mock</Badge>
      </div>
      <div className="close-list" role="list">
        {CLOSE_STEPS.map((s) => (
          <ItemRow
            key={s.title}
            tone={s.tone}
            title={s.title}
            sub={s.sub}
            className="close-item"
            trailing={<CloseStepAction step={s} />}
          />
        ))}
      </div>
    </section>
  );
};

/** Mock Export Readiness panel (not yet wired to the API). */
const ExportReadinessPanel = () => {
  return (
    <section className="panel">
      <div className="panel-header">
        <div className="panel-title">
          <strong>Export Readiness</strong>
          <span>Sample data — not yet wired to the API</span>
        </div>
        <Badge tone="amber">Mock</Badge>
      </div>
      <div className="issue-list" role="list">
        {EXPORT_READINESS.map((r) => (
          <ItemRow
            key={r.title}
            tone={r.tone}
            title={r.title}
            sub={r.sub}
            trailing={<Badge tone={r.badge.tone}>{r.badge.text}</Badge>}
          />
        ))}
      </div>
    </section>
  );
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

const describeError = (error: ApiError | Error): { title: string; detail: string } => {
  if (error instanceof ApiError) {
    if (error.status === 403)
      return {
        title: "No permission",
        detail: "Your role cannot view net revenue for this month or scope.",
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

const alertDataBadge = (data: SmartAlertsSummary): { tone: Severity; children: string } => {
  if (data.status === "CLEAR") return { tone: "green", children: "Clear" };
  const severity = data.highest_severity;
  return {
    tone: severity ? severityTone(severity) : "amber",
    children: severity ?? "Attention",
  };
};

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
    const { title, detail } = describeError(error);
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
//   - File: backend/ums_smart_revenue/api/revenue.py:844 get_month_smart_alerts.
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

const buildBankReconciliationMetrics = (
  data: MonthBankReconciliationSummary,
  canViewFinance: boolean,
  fallbackCurrency: string,
): BankReconciliationMetric[] => {
  const currency = data.currency || fallbackCurrency;
  const status = bankReconciliationStatusCopy(data.status);
  return [
    {
      id: "adsense-payment",
      label: "AdSense payment",
      value: financeDisplay(data.adsense_paid_amount_usd, canViewFinance, { currency }),
      badge: {
        text: data.paid_payment_count > 0 ? "Paid" : "Missing",
        tone: data.paid_payment_count > 0 ? "green" : "amber",
      },
      note: [
        `${data.paid_payment_count}/${data.payment_count} paid`,
        data.non_paid_payment_count > 0
          ? countLabel(data.non_paid_payment_count, "unpaid row")
          : "AdSense source",
      ],
      tone: data.paid_payment_count > 0 ? "is-payment" : "is-review",
    },
    {
      id: "bank-received",
      label: "Bank received",
      value: financeDisplay(data.bank_received_amount_usd, canViewFinance, { currency }),
      badge: {
        text: countLabel(data.entry_count, "receipt"),
        tone: data.entry_count > 0 ? "green" : "amber",
      },
      note: [
        data.entry_count > 0 ? "Bank source loaded" : "Waiting for bank",
        financeDisplay(data.transfer_fee_usd, canViewFinance, {
          currency,
          placeholder: "No fee",
        }),
      ],
      tone: data.entry_count > 0 ? "is-revenue" : "is-review",
    },
    {
      id: "bank-gap",
      label: "Unresolved gap",
      value: financeDisplay(data.bank_gap_usd, canViewFinance, { currency }),
      badge: { text: status.badge, tone: status.tone },
      note: [
        status.label,
        data.bank_gap_usd === null
          ? "Needs both sources"
          : `Tolerance ${financeDisplay(data.tolerance_usd, canViewFinance, { currency })}`,
      ],
      tone: status.metricTone,
    },
  ];
};

const bankReconciliationErrorCopy = (
  error: ApiError | Error,
): { title: string; detail: string } => {
  if (error instanceof ApiError) {
    if (error.status === 403)
      return {
        title: "No permission",
        detail: "Your role cannot view payment and bank reconciliation for this month.",
      };
    return { title: `Request failed (${error.status})`, detail: extractApiErrorDetail(error) };
  }
  return {
    title: "Network error",
    detail: error.message || "Could not reach the bank reconciliation service.",
  };
};

// ============================================================================
// Purpose: Payment reconciliation cards for the Command Center. Fetches the
//   month-level bank reconciliation summary independently from net revenue and
//   smart alerts, then displays the backend-sourced AdSense paid amount, bank
//   received amount, and unresolved bank gap.
// Database/ORM: None (frontend) — consumes GET /revenue/months/{month}/bank-reconciliation.
// Standards: Official finance values are backend strings rendered via
//   financeDisplay; no browser-side finance calculation. The read is disabled
//   when finance values are withheld, and endpoint 403s render inside this strip
//   without replacing the rest of CommandView.
// Blast Radius: Finance display only. No mutation, export, lock, or audit write
//   from the browser beyond the backend endpoint's read audit events.
// Connections:
//   - File: frontend/src/lib/api/useBankReconciliation.ts -> the fetch hook.
//   - File: frontend/src/lib/api/types.ts -> MonthBankReconciliationSummary.
//   - File: backend/ums_smart_revenue/api/revenue.py -> get_month_bank_reconciliation().
// ============================================================================
const BankReconciliationStatusStrip = ({
  month,
  canViewFinance,
  currency,
}: {
  month: string;
  canViewFinance: boolean;
  currency: string;
}) => {
  const { data, loading, error } = useBankReconciliation({
    month,
    enabled: canViewFinance,
  });

  if (!canViewFinance) {
    return (
      <section
        className="status-strip reconciliation-strip"
        aria-label="Payment reconciliation summary"
      >
        {["AdSense payment", "Bank received", "Unresolved gap"].map((label) => (
          <article key={label} className="metric is-review">
            <header>
              <span className="metric-label">{label}</span>
              <Badge tone="red">Restricted</Badge>
            </header>
            <div className="metric-value finance-data">{RESTRICTED_FINANCE_VALUE}</div>
            <div className="metric-note">
              <span>Finance access required</span>
            </div>
          </article>
        ))}
      </section>
    );
  }

  if (error) {
    const { title, detail } = bankReconciliationErrorCopy(error);
    return (
      <section
        className="status-strip reconciliation-strip"
        aria-label="Payment reconciliation summary"
        role="alert"
      >
        <article className="metric is-risk">
          <header>
            <span className="metric-label">Payment reconciliation unavailable</span>
            <Badge tone={title === "No permission" ? "blue" : "red"}>{title}</Badge>
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
      <section
        className="status-strip reconciliation-strip"
        aria-label="Payment reconciliation summary"
        aria-busy="true"
      >
        {["AdSense payment", "Bank received", "Unresolved gap"].map((label) => (
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
      </section>
    );
  }

  if (!data) {
    return (
      <section
        className="status-strip reconciliation-strip"
        aria-label="Payment reconciliation summary"
      >
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
      </section>
    );
  }

  const metrics = buildBankReconciliationMetrics(data, canViewFinance, currency);

  return (
    <section
      className="status-strip reconciliation-strip"
      aria-label="Payment reconciliation summary"
    >
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
    </section>
  );
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
 * Two-column Command Center workspace: the real channel table plus mock issue,
 * close, explanation, and export-readiness panels. Splits into named panels so
 * each JSX subtree stays shallow.
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

        {/* issue + close split — still mock data (not part of net-revenue API) */}
        <div className="layout-split">
          <IssueQueuePanel />
          <MonthCloseControlsPanel />
        </div>
      </div>

      {/* explain + readiness */}
      <aside className="side-stack" aria-label="Explanation and readiness">
        <ExplainCard
          selectedChannel={selectedChannel}
          canViewFinance={canViewFinance}
          currency={currency}
          loading={loading}
          month={month}
        />
        <ExportReadinessPanel />
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
  children,
}: {
  error: ApiError | Error | null;
  loading: boolean;
  empty: boolean;
  emptyTitle: string;
  emptySub: string;
  children: ReactNode;
}) => {
  if (error) {
    const { title, detail } = describeError(error);
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
    const { title, detail } = describeError(error);
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
const CommandView = ({
  canViewFinance,
  canViewAnalytics = false,
}: {
  canViewFinance: boolean;
  // Optional so the existing prop contract (canViewFinance-only) stays valid; a
  // missing flag fails closed (false) — the analytics monitor never mounts and
  // fires no request without an explicit, backend-derived grant.
  canViewAnalytics?: boolean;
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
        canViewFinance={canViewFinance}
        currency={currency}
      />

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
