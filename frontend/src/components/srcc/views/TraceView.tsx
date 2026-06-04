import { useMemo, useState } from "react";

import { ApiError } from "@/lib/api/client";
import type {
  ExplanationComponent,
  ExplanationMetric,
  NumberExplanation,
} from "@/lib/api/types";
import { useExplanation } from "@/lib/api/useExplanation";
import { useNetRevenue } from "@/lib/api/useNetRevenue";
import type { Role, Severity } from "@/lib/mock/data";
import { Badge, Dot, financeDisplay, ItemRow } from "../shared";
import { describeError } from "./CommandView";

// ============================================================================
// Purpose: The REAL-data Trace / Explain-Number screen, extracted from AppShell.
//   The operator picks a month + channel (channels reused from the net-revenue
//   summary, avoiding a new auth surface) + metric, then "Explain" POSTs to
//   /revenue/channels/{channel}/months/{month}/explain?metric={metric}, which
//   generates+persists+audits the explanation and returns the metric value,
//   formula, component/contributor breakdown (with the net deduction split),
//   confidence, and warnings. Money cells are permission-gated; loading / error
//   / 403 states mirror CommandView and CloseView. The Permission Filter side
//   panel stays as static role context (it is descriptive, not API data).
// Database/ORM: None (frontend) — consumes GET .../net-revenue (channel list)
//   and POST .../explain (generate+persist server-side).
// Standards: No client-side finance authorization is invented — the backend
//   gate (VIEW_REVENUE + VIEW_CONFIDENCE @channel, plus VIEW_FINALIZED_PAYMENTS
//   @finance_month for the net metric) is authoritative; a 403 surfaces as
//   no-permission copy. Money values are backend strings, formatted for display
//   only (no float math) and gated via financeDisplay.
// Blast Radius: Finance number explanations (write path) via the backend's own
//   guarded, audited route only. No source-of-truth finance number is computed
//   or mutated client-side.
// Connections:
//   - File: frontend/src/lib/api/useExplanation.ts -> the POST action hook.
//   - File: frontend/src/lib/api/useNetRevenue.ts -> the channel dropdown source.
//   - File: frontend/src/lib/api/types.ts -> NumberExplanation contract.
//   - File: backend/ums_smart_revenue/api/revenue.py:1358 explain endpoint.
// ============================================================================

// Default to a recent, demo-seedable month per the task brief (matches the
// other wired views).
const DEFAULT_MONTH = "2026-03";

// Months offered in the selector (most recent first). A simple dropdown by
// design — wiring real data is the priority, not month discovery.
const MONTH_OPTIONS = ["2026-03", "2026-02", "2026-01", "2025-12"];

// The two metrics the explain endpoint accepts (SUPPORTED_METRICS); labelled
// for the dropdown. The net metric carries the extra finalized-payment gate.
const METRIC_OPTIONS: Array<{ value: ExplanationMetric; label: string }> = [
  { value: "adjusted_gross_revenue_usd", label: "Adjusted gross revenue" },
  { value: "net_revenue_usd", label: "Net revenue" },
];

function confidenceTone(label: string | undefined): Severity {
  switch ((label ?? "").toUpperCase()) {
    case "HIGH":
      return "green";
    case "MEDIUM":
      return "amber";
    case "LOW":
      return "red";
    default:
      return "blue";
  }
}

// Component breakdown tone: the value row (positive contribution) is green,
// deductions/overrides amber/blue; falls back to blue for unknown keys.
function componentTone(key: string): Severity {
  if (key.includes("deduction")) return "amber";
  if (key.includes("override")) return "blue";
  return "green";
}

export default function TraceView({
  canViewFinance,
  role,
}: {
  canViewFinance: boolean;
  role: Role;
}) {
  const [month, setMonth] = useState<string>(DEFAULT_MONTH);
  const [metric, setMetric] = useState<ExplanationMetric>(
    "adjusted_gross_revenue_usd",
  );
  const [selectedChannelId, setSelectedChannelId] = useState<string>("");

  // Reuse the net-revenue summary purely as the channel directory for the
  // dropdown (global scope, matching CommandView's default).
  const {
    data: netRevenue,
    loading: channelsLoading,
    error: channelsError,
  } = useNetRevenue({ month, scopeType: "global", scopeId: null });

  const channels = useMemo(
    () => netRevenue?.channels ?? [],
    [netRevenue],
  );

  const explanation = useExplanation();

  // Resolve the channel to explain: the explicit selection if it still exists
  // in this month, otherwise the first channel.
  const effectiveChannelId =
    channels.find((c) => c.youtube_channel_id === selectedChannelId)
      ?.youtube_channel_id ??
    channels[0]?.youtube_channel_id ??
    "";

  const currency = netRevenue?.currency ?? "USD";

  const runExplain = () => {
    if (!effectiveChannelId) return;
    // The hook captures its own error state; swallow the rejection here so an
    // un-actioned promise does not surface as an unhandled rejection.
    void explanation.run({
      channelId: effectiveChannelId,
      month,
      metric,
    }).catch(() => {});
  };

  const permissionDetails =
    role === "finance"
      ? [
          { label: "Role", value: "Finance Admin" },
          { label: "Scope", value: "Global finance month" },
          { label: "Companies", value: "All allowed" },
          { label: "Raw files", value: "Hidden unless explicitly granted" },
        ]
      : role === "assistant"
        ? [
            { label: "Role", value: "Assistant Analyst" },
            { label: "Scope", value: "Assigned non-finance work" },
            { label: "Companies", value: "Visible without money cells" },
            { label: "Raw files", value: "Hidden unless explicitly granted" },
          ]
        : [
            { label: "Role", value: "Company Manager" },
            { label: "Scope", value: "Assigned company only" },
            { label: "Companies", value: "Company-scoped" },
            { label: "Raw files", value: "Hidden unless explicitly granted" },
          ];

  return (
    <section className="view-page" aria-labelledby="traceViewTitle">
      <div className="view-grid wide-side">
        <section className="panel">
          <div className="panel-header">
            <div className="panel-title">
              <strong id="traceViewTitle">Explain Number</strong>
              <span>
                Generate the source-linked breakdown of a channel-month metric
                from SQL-backed finance data
              </span>
            </div>
            <Badge tone="violet">Audited read</Badge>
          </div>

          <div
            className="control-row"
            aria-label="Explanation filters"
            style={{ margin: 13 }}
          >
            <select
              className="control"
              aria-label="Month"
              value={month}
              onChange={(e) => setMonth(e.target.value)}
            >
              {MONTH_OPTIONS.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
            <select
              className="control"
              aria-label="Channel"
              value={effectiveChannelId}
              disabled={channelsLoading || channels.length === 0}
              onChange={(e) => setSelectedChannelId(e.target.value)}
            >
              {channels.length === 0 ? (
                <option value="">
                  {channelsLoading ? "Loading channels…" : "No channels"}
                </option>
              ) : (
                channels.map((c) => (
                  <option key={c.youtube_channel_id} value={c.youtube_channel_id}>
                    {c.youtube_channel_id}
                  </option>
                ))
              )}
            </select>
            <select
              className="control"
              aria-label="Metric"
              value={metric}
              onChange={(e) => setMetric(e.target.value as ExplanationMetric)}
            >
              {METRIC_OPTIONS.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </select>
            <button
              className="primary-button"
              type="button"
              disabled={!effectiveChannelId || explanation.loading}
              onClick={runExplain}
            >
              {explanation.loading ? "Explaining…" : "Explain"}
            </button>
          </div>

          {channelsError ? (
            <ChannelLoadError error={channelsError} />
          ) : null}

          <ExplanationPanel
            state={explanation}
            canViewFinance={canViewFinance}
            currency={currency}
          />
        </section>

        <aside className="view-stack">
          <section className="panel">
            <div className="panel-header">
              <div className="panel-title">
                <strong>Permission Filter</strong>
                <span>Applied before the explanation query is authorized</span>
              </div>
              <Badge tone="violet">Scoped trace</Badge>
            </div>
            <div className="detail-grid">
              {permissionDetails.map((d) => (
                <div key={d.label} className="detail-cell">
                  <span>{d.label}</span>
                  <strong>{d.value}</strong>
                </div>
              ))}
            </div>
          </section>
        </aside>
      </div>
    </section>
  );
}

function ChannelLoadError({ error }: { error: ApiError | Error }) {
  const { title, detail } = describeError(error);
  return (
    <div
      className="permission-band"
      role="alert"
      style={{ margin: 13 }}
    >
      <Dot tone="red" />
      <span>
        <strong>{title}</strong>
        <span>{`Channel list unavailable — ${detail}`}</span>
      </span>
      <Badge tone="red">No channels</Badge>
    </div>
  );
}

function ExplanationPanel({
  state,
  canViewFinance,
  currency,
}: {
  state: ReturnType<typeof useExplanation>;
  canViewFinance: boolean;
  currency: string;
}) {
  const { data, loading, error } = state;

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

  if (loading) {
    return (
      <div className="table-wrap" aria-busy="true">
        <div style={{ padding: 16 }} className="item-sub">
          Generating explanation…
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="table-wrap">
        <div style={{ padding: 16 }} className="item-sub">
          Pick a month, channel, and metric, then select Explain to generate the
          source-linked breakdown.
        </div>
      </div>
    );
  }

  return (
    <ExplanationResult
      explanation={data}
      canViewFinance={canViewFinance}
      currency={currency}
    />
  );
}

function ExplanationResult({
  explanation,
  canViewFinance,
  currency,
}: {
  explanation: NumberExplanation;
  canViewFinance: boolean;
  currency: string;
}) {
  const displayCurrency = explanation.currency || currency;

  return (
    <div style={{ padding: 13 }}>
      <div className="explain-head">
        <div>
          <h2>{explanation.entity_id}</h2>
          <p>
            {explanation.metric} · {explanation.month}
          </p>
        </div>
        <Badge tone={confidenceTone(explanation.confidence?.label)}>
          {explanation.confidence?.label ?? "—"}
        </Badge>
      </div>

      <div className="detail-grid" style={{ marginBottom: 13 }}>
        <div className="detail-cell">
          <span>Value</span>
          <strong className="finance-data">
            {financeDisplay(explanation.value, canViewFinance, {
              currency: displayCurrency,
            })}
          </strong>
        </div>
        <div className="detail-cell">
          <span>Confidence score</span>
          <strong>{explanation.confidence?.score ?? "—"}</strong>
        </div>
      </div>

      <div className="formula" role="text" aria-label="Metric formula">
        {explanation.formula}
      </div>

      <div className="explain-list" role="list" aria-label="Explanation components">
        {explanation.components.map((component) => (
          <ComponentRow
            key={component.key}
            component={component}
            canViewFinance={canViewFinance}
            currency={displayCurrency}
          />
        ))}
      </div>

      {explanation.warnings.length > 0 ? (
        <div
          className="issue-list"
          role="list"
          aria-label="Explanation warnings"
          style={{ marginTop: 13 }}
        >
          {explanation.warnings.map((warning) => (
            <ItemRow
              key={`${warning.code}:${warning.message}`}
              tone="amber"
              title={warning.message || warning.code}
              sub={warning.code}
              trailing={<Badge tone="amber">Warning</Badge>}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function componentSubtitle(component: ExplanationComponent): string {
  if (component.source_kind) {
    return component.source_report_id
      ? `${component.source_kind} · ${component.source_report_id}`
      : component.source_kind;
  }
  if (typeof component.count === "number") {
    const items = component.count === 1 ? "item" : "items";
    return `${component.count} ${items}`;
  }
  return component.key;
}

function ComponentRow({
  component,
  canViewFinance,
  currency,
}: {
  component: ExplanationComponent;
  canViewFinance: boolean;
  currency: string;
}) {
  return (
    <ItemRow
      tone={componentTone(component.key)}
      title={component.label}
      sub={componentSubtitle(component)}
      className="explain-row"
      trailing={
        <span className="money finance-data">
          {financeDisplay(component.value, canViewFinance, { currency })}
        </span>
      }
    />
  );
}
