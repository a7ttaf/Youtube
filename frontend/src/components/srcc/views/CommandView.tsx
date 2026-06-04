import { useMemo, useState } from "react";

import { ApiError } from "@/lib/api/client";
import type {
  ChannelNetRevenue,
  NetRevenueResponse,
  SmartAlert,
  SmartAlertSeverity,
  SmartAlertsSummary,
} from "@/lib/api/types";
import { useNetRevenue } from "@/lib/api/useNetRevenue";
import { useSmartAlerts } from "@/lib/api/useSmartAlerts";
import {
  CLOSE_STEPS,
  EXPORT_READINESS,
  ISSUES,
} from "@/lib/mock/data";
import type { Severity } from "@/lib/mock/data";
import { LockIcon } from "../icons";
import {
  Badge,
  DEFAULT_MONTH,
  financeDisplay,
  ItemRow,
  MONTH_OPTIONS,
  RESTRICTED_FINANCE_VALUE,
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

// Global to start (per the task). Scoped options are listed so the selector
// shape matches the eventual org hierarchy; they pass scope_type/scope_id
// straight through to the backend, which resolves + authorizes them.
const SCOPE_OPTIONS: ScopeOption[] = [
  { label: "UMS Holding (global)", scopeType: "global", scopeId: null },
];

const ALLOCATION_SOURCE_COPY: Record<
  NetRevenueResponse["allocation_source"],
  { label: string; tone: Severity }
> = {
  committed_snapshot: { label: "Committed snapshot", tone: "green" },
  live_compute: { label: "Live compute", tone: "blue" },
  live_fallback: { label: "Live fallback", tone: "amber" },
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
function statusTone(status: string): Severity { // skipcq: JS-0067, JS-R1005
  const normalized = status.toUpperCase();
  for (const [keyword, tone] of Object.entries(STATUS_KEYWORD_TONE)) {
    if (normalized.includes(keyword)) return tone;
  }
  return "blue";
}

// Map an alert severity to a design-system badge tone (HIGH -> red, MEDIUM ->
// amber, LOW -> blue). Unknown severities fall back to blue.
function severityTone(severity: SmartAlertSeverity | string): Severity { // skipcq: JS-0067
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
}

/** Human-facing label for a channel row (its YouTube channel id for now). */
function channelDisplayName(channel: ChannelNetRevenue): string { // skipcq: JS-0067
  return channel.youtube_channel_id;
}

/** Two-letter avatar initials derived from a channel's id. */
function channelAvatar(channel: ChannelNetRevenue): string { // skipcq: JS-0067
  const id = channel.youtube_channel_id.replace(/[^a-zA-Z0-9]/g, "");
  return (id.slice(-2) || "--").toUpperCase();
}

/**
 * Command Center screen: month/scope filters, the real net-revenue status strip
 * and channel table, the smart-alerts panel, and the per-channel explanation.
 */
export default function CommandView({ // skipcq: JS-0067, JS-R1005
  canViewFinance,
}: {
  canViewFinance: boolean;
}) {
  const [month, setMonth] = useState<string>(DEFAULT_MONTH);
  const [scopeIndex, setScopeIndex] = useState<number>(0);
  const [selectedChannelId, setSelectedChannelId] = useState<string | null>(null);

  // FIX: Replaced the non-null assertion on SCOPE_OPTIONS[0] with explicit
  // narrowing; the selected index may be out of range, and the fallback is the
  // first option, which must exist (the constant is defined non-empty above).
  const fallbackScope = SCOPE_OPTIONS[0];
  if (!fallbackScope) {
    throw new Error("SCOPE_OPTIONS must define at least one scope option");
  }
  const scope = SCOPE_OPTIONS[scopeIndex] ?? fallbackScope;
  const { data, loading, error, reload } = useNetRevenue({
    month,
    scopeType: scope.scopeType,
    scopeId: scope.scopeId,
  });

  const currency = data?.currency ?? "USD";
  const channels = useMemo(() => data?.channels ?? [], [data]);
  const selectedChannel =
    channels.find((c) => c.youtube_channel_id === selectedChannelId) ??
    channels[0] ??
    null;

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
          value={scopeIndex}
          onChange={(e) => {
            setScopeIndex(Number(e.target.value));
            setSelectedChannelId(null);
          }}
        >
          {SCOPE_OPTIONS.map((s, index) => (
            <option key={s.label} value={index}>
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

      {/* smart-alerts / problem panel — REAL data, fails independently */}
      <SmartAlertsPanel month={month} />

      <CommandWorkspace
        data={data}
        loading={loading}
        error={error}
        canViewFinance={canViewFinance}
        currency={currency}
        channelCount={channels.length}
        selectedChannel={selectedChannel}
        selectedChannelId={selectedChannel?.youtube_channel_id ?? null}
        month={month}
        onSelect={setSelectedChannelId}
      />
    </>
  );
}

/**
 * Two-column Command Center workspace: the real channel table plus mock issue,
 * close, explanation, and export-readiness panels. Splits into named panels so
 * each JSX subtree stays shallow.
 */
function CommandWorkspace({ // skipcq: JS-0067
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
}) {
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
}

/** Channel Revenue Table panel: header badge plus the real net-revenue table. */
function ChannelRevenuePanel({ // skipcq: JS-0067
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
}) {
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
}

/** Mock Issue Queue panel (not yet wired to the API). */
function IssueQueuePanel() { // skipcq: JS-0067
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
}

/** Mock Month Close Controls panel (not yet wired to the API). */
function MonthCloseControlsPanel() { // skipcq: JS-0067
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
}

/** Trailing control for a close step: a status badge or an action button. */
function CloseStepAction({ step }: { step: (typeof CLOSE_STEPS)[number] }) { // skipcq: JS-0067
  if (step.badge) {
    return <Badge tone={step.badge.tone}>{step.badge.text}</Badge>;
  }
  return (
    <button className="mini-button" type="button">
      {step.action}
    </button>
  );
}

/** Net-revenue explanation card for the selected channel (or an empty state). */
function ExplainCard({ // skipcq: JS-0067
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
}) {
  return (
    <section className="panel explain-card">
      <div className="explain-head">
        <div>
          <h2>{selectedChannel ? channelDisplayName(selectedChannel) : "No channel"}</h2>
          <p>Net revenue explanation, {month}</p>
        </div>
        {selectedChannel ? (
          <Badge tone={statusTone(selectedChannel.status)}>{selectedChannel.confidence}</Badge>
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
}

/** Mock Export Readiness panel (not yet wired to the API). */
function ExportReadinessPanel() { // skipcq: JS-0067
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
}

// ============================================================================
// Purpose: Map an ApiError/Error to friendly UI copy. 403 -> no-permission
//   message (matches the finance fail-closed model); other ApiError -> the
//   typed status + message; non-ApiError -> generic network failure.
// ============================================================================
function describeError(error: ApiError | Error): { title: string; detail: string } { // skipcq: JS-0067, JS-R1005
  if (error instanceof ApiError) {
    if (error.status === 403) {
      return {
        title: "No permission",
        detail: "Your role cannot view net revenue for this month or scope.",
      };
    }
    const detail =
      typeof error.body === "object" &&
      error.body !== null &&
      typeof (error.body as { detail?: unknown }).detail === "string"
        ? (error.body as { detail: string }).detail
        : error.message;
    return { title: `Request failed (${error.status})`, detail };
  }
  return {
    title: "Network error",
    detail: error.message || "Could not reach the revenue service.",
  };
}

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
function SmartAlertsPanel({ month }: { month: string }) { // skipcq: JS-0067
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
}

// Header badge: surfaces the overall status + highest severity at a glance, and
// degrades to Loading / Error / No permission without breaking the panel header.
function SmartAlertsHeaderBadge({ // skipcq: JS-0067, JS-R1005
  data,
  loading,
  error,
}: {
  data: SmartAlertsSummary | null;
  loading: boolean;
  error: ApiError | Error | null;
}) {
  if (error) {
    const isForbidden = error instanceof ApiError && error.status === 403;
    return <Badge tone={isForbidden ? "blue" : "red"}>{isForbidden ? "No permission" : "Error"}</Badge>;
  }
  if (loading && !data) {
    return <Badge tone="blue">Loading</Badge>;
  }
  if (!data) {
    return <Badge tone="amber">Empty</Badge>;
  }
  if (data.status === "CLEAR") {
    return <Badge tone="green">Clear</Badge>;
  }
  return (
    <Badge tone={data.highest_severity ? severityTone(data.highest_severity) : "amber"}>
      {data.highest_severity ?? "Attention"}
    </Badge>
  );
}

/** Body of the smart-alerts panel: error, loading, empty, and alert-row states. */
function SmartAlertsBody({ // skipcq: JS-0067, JS-R1005
  data,
  loading,
  error,
}: {
  data: SmartAlertsSummary | null;
  loading: boolean;
  error: ApiError | Error | null;
}) {
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

  if (loading && !data) {
    return (
      <div className="issue-list" role="list" aria-busy="true">
        <ItemRow
          tone="blue"
          title="Loading smart alerts…"
          sub="Aggregating payment, bank, lock, and override signals"
          trailing={<Badge tone="blue">Loading</Badge>}
        />
      </div>
    );
  }

  // Read alerts defensively: a missing/non-array field (e.g. an unexpected body
  // shape) is treated as "no alerts" rather than throwing inside the panel.
  const alerts = Array.isArray(data?.alerts) ? data.alerts : [];
  if (!data || alerts.length === 0) {
    return (
      <div className="issue-list" role="list">
        <ItemRow
          tone="green"
          title="No active alerts"
          sub={
            data
              ? `Status ${data.status} — nothing needs attention for ${data.month}.`
              : "No smart-alert data returned."
          }
          trailing={<Badge tone="green">Clear</Badge>}
        />
      </div>
    );
  }

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
}

/** Top metric strip summarising the month's gross, net, deductions, and allocation source. */
function NetRevenueStatusStrip({ // skipcq: JS-0067, JS-R1005
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
}) {
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

  const allocation = ALLOCATION_SOURCE_COPY[data.allocation_source] ?? {
    label: data.allocation_source,
    tone: "blue" as Severity,
  };

  const metrics: Array<{
    id: string;
    tone: string;
    label: string;
    value: string;
    badge: { text: string; tone: Severity };
    note: [string, string];
    finance: boolean;
    locked?: boolean;
  }> = [
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
}

/** Selectable per-channel revenue table with error, loading, and empty states. */
function NetRevenueChannelTable({ // skipcq: JS-0067, JS-R1005
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
}) {
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

  if (loading && !data) {
    return (
      <div className="table-wrap" aria-busy="true">
        <div style={{ padding: 16 }} className="item-sub">
          Loading channels…
        </div>
      </div>
    );
  }

  const channels = data?.channels ?? [];
  if (channels.length === 0) {
    return (
      <div className="table-wrap">
        <div style={{ padding: 16 }} className="item-sub">
          No channels for this month and scope.
        </div>
      </div>
    );
  }

  return (
    <div className="table-wrap">
      <table role="grid" aria-label="Channel revenue">
        <ChannelTableHead />
        <tbody>
          {channels.map((c) => (
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
}

/** Static header row for the channel revenue table. */
function ChannelTableHead() { // skipcq: JS-0067
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
}

/** Single selectable channel row: name, status, permission-gated money, and issues. */
function ChannelRow({ // skipcq: JS-0067
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
}) {
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
        <Badge tone={statusTone(channel.status)}>{channel.confidence}</Badge>
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
}

/** Avatar + name + source-kind cell for a channel row. */
function ChannelNameCell({ channel }: { channel: ChannelNetRevenue }) { // skipcq: JS-0067
  return (
    <span className="channel-cell">
      <span className="avatar">{channelAvatar(channel)}</span>
      <span className="channel-copy">
        <span className="channel-name">{channelDisplayName(channel)}</span>
        <span className="channel-id">{channel.primary_source_kind ?? "no source"}</span>
      </span>
    </span>
  );
}

/** Explanation rows for the selected channel: gross, deductions, and resulting net. */
function ChannelExplainRows({ // skipcq: JS-0067
  channel,
  canViewFinance,
  currency,
}: {
  channel: ChannelNetRevenue;
  canViewFinance: boolean;
  currency: string;
}) {
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
}

export { describeError };
