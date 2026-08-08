import { useEffect, useMemo, useState } from "react";

import { ApiError } from "@/lib/api/client";
import type {
  ChannelNetRevenue,
  ExplanationComponent,
  ExplanationMetric,
  NumberExplanation,
} from "@/lib/api/types";
import { useExplanation } from "@/lib/api/useExplanation";
import { useNetRevenue } from "@/lib/api/useNetRevenue";
import type { Role, Severity } from "@/lib/mock/data";
import { confidenceDisplay } from "@/lib/confidence";
import {
  Badge,
  DEFAULT_MONTH,
  Dot,
  financeDisplay,
  ItemRow,
  MONTH_OPTIONS,
} from "../shared";
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

// The two metrics the explain endpoint accepts (SUPPORTED_METRICS); labelled
// for the dropdown. The net metric carries the extra finalized-payment gate.
const METRIC_OPTIONS: Array<{ value: ExplanationMetric; label: string }> = [
  { value: "adjusted_gross_revenue_usd", label: "Adjusted gross revenue" },
  { value: "net_revenue_usd", label: "Net revenue" },
];

/**
 * Map a confidence label (HIGH/MEDIUM/LOW) to the badge tone, blue otherwise.
 * Delegates to the shared confidenceDisplay helper (label-only call) so Command
 * and Trace stay tone-consistent; behavior is unchanged from the prior local
 * switch (its ConfidenceTone union is a subset of Severity).
 */
const confidenceTone = (label: string | undefined): Severity => {
  return confidenceDisplay("", label).tone;
};

// Component breakdown tone: the value row (positive contribution) is green,
// deductions/overrides amber/blue; falls back to blue for unknown keys.
const componentTone = (key: string): Severity => {
  if (key.includes("deduction")) return "amber";
  if (key.includes("override")) return "blue";
  return "green";
};

/**
 * Static role -> permission-filter detail rows shown in the side panel. A closed
 * Record over the Role union (finance/assistant/company) lets us index by the
 * typed role directly with no fallback or non-null assertion — every role has an
 * entry, so the lookup is total. Replaces the prior nested ternary chain.
 */
const PERMISSION_DETAILS: Record<Role, Array<{ label: string; value: string }>> = {
  finance: [
    { label: "Role", value: "Finance Admin" },
    { label: "Scope", value: "Global finance month" },
    { label: "Companies", value: "All allowed" },
    { label: "Raw files", value: "Hidden unless explicitly granted" },
  ],
  assistant: [
    { label: "Role", value: "Assistant Analyst" },
    { label: "Scope", value: "Assigned non-finance work" },
    { label: "Companies", value: "Visible without money cells" },
    { label: "Raw files", value: "Hidden unless explicitly granted" },
  ],
  company: [
    { label: "Role", value: "Company Manager" },
    { label: "Scope", value: "Assigned company only" },
    { label: "Companies", value: "Company-scoped" },
    { label: "Raw files", value: "Hidden unless explicitly granted" },
  ],
};

/** Resolve which channel to explain: the current selection if present, else the first channel. */
const resolveEffectiveChannelId = (
  selectedChannelId: string,
  channels: Array<{ youtube_channel_id: string }>,
): string => {
  return (
    channels.find((c) => c.youtube_channel_id === selectedChannelId)
      ?.youtube_channel_id ??
    channels[0]?.youtube_channel_id ??
    ""
  );
};

/** Title block for the Explain Number panel. */
const TraceHeader = () => {
  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong id="traceViewTitle">Explain Number</strong>
        <span>
          Generate the source-linked breakdown of a channel-month metric from
          SQL-backed finance data
        </span>
      </div>
      <Badge tone="violet">Audited read</Badge>
    </div>
  );
};

/** Option list for the channel selector: a placeholder when empty, otherwise rows. */
const ChannelOptions = ({
  channels,
  channelsLoading,
}: {
  channels: ChannelNetRevenue[];
  channelsLoading: boolean;
}) => {
  if (channels.length === 0) {
    return (
      <option value="">
        {channelsLoading ? "Loading channels…" : "No channels"}
      </option>
    );
  }
  return (
    <>
      {channels.map((c) => (
        <option key={c.youtube_channel_id} value={c.youtube_channel_id}>
          {c.youtube_channel_id}
        </option>
      ))}
    </>
  );
};

/** Month / channel / metric selectors plus the Explain trigger for the trace screen. */
const ExplanationFilters = ({
  month,
  onMonthChange,
  effectiveChannelId,
  channels,
  channelsLoading,
  onChannelChange,
  metric,
  onMetricChange,
  explaining,
  onExplain,
}: {
  month: string;
  onMonthChange: (value: string) => void;
  effectiveChannelId: string;
  channels: ChannelNetRevenue[];
  channelsLoading: boolean;
  onChannelChange: (value: string) => void;
  metric: ExplanationMetric;
  onMetricChange: (value: ExplanationMetric) => void;
  explaining: boolean;
  onExplain: () => void;
}) => {
  return (
    <div
      className="control-row"
      aria-label="Explanation filters"
      style={{ margin: 13 }}
    >
      <select
        className="control"
        aria-label="Month"
        value={month}
        onChange={(e) => onMonthChange(e.target.value)}
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
        onChange={(e) => onChannelChange(e.target.value)}
      >
        <ChannelOptions channels={channels} channelsLoading={channelsLoading} />
      </select>
      <select
        className="control"
        aria-label="Metric"
        value={metric}
        onChange={(e) => onMetricChange(e.target.value as ExplanationMetric)}
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
        disabled={!effectiveChannelId || explaining}
        onClick={onExplain}
      >
        {explaining ? "Explaining…" : "Explain"}
      </button>
    </div>
  );
};

/** Title block for the permission-filter side panel. */
const PermissionFilterHeader = () => {
  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong>Permission Filter</strong>
        <span>Applied before the explanation query is authorized</span>
      </div>
      <Badge tone="violet">Scoped trace</Badge>
    </div>
  );
};

/** A single label/value cell in the permission-filter detail grid. */
const PermissionDetailCell = ({
  label,
  value,
}: {
  label: string;
  value: string;
}) => {
  return (
    <div className="detail-cell">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
};

/** Static role-context side panel describing the scope applied before authorization. */
const PermissionFilterPanel = ({
  details,
}: {
  details: Array<{ label: string; value: string }>;
}) => {
  return (
    <aside className="view-stack">
      <section className="panel">
        <PermissionFilterHeader />
        <div className="detail-grid">
          {details.map((d) => (
            <PermissionDetailCell key={d.label} label={d.label} value={d.value} />
          ))}
        </div>
      </section>
    </aside>
  );
};

/** Error band shown when the channel directory (net-revenue summary) fails to load. */
const ChannelLoadError = ({ error }: { error: ApiError | Error }) => {
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
};

/** Build a component row subtitle from its source kind/report, item count, or key. */
const componentSubtitle = (component: ExplanationComponent): string => {
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
};

/** One explanation component row with a permission-gated money cell. */
const ComponentRow = ({
  component,
  canViewFinance,
  currency,
}: {
  component: ExplanationComponent;
  canViewFinance: boolean;
  currency: string;
}) => {
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
};

/** Render a returned explanation: value, confidence, formula, components, warnings. */
const ExplanationResult = ({
  explanation,
  canViewFinance,
  currency,
}: {
  explanation: NumberExplanation;
  canViewFinance: boolean;
  currency: string;
}) => {
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
};

/** Render the explain hook's state: error, loading, idle prompt, or the result. */
const ExplanationPanel = ({
  state,
  canViewFinance,
  currency,
}: {
  state: ReturnType<typeof useExplanation>;
  canViewFinance: boolean;
  currency: string;
}) => {
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
};

/**
 * Trace / Explain-Number screen: pick month + channel + metric, then POST to the
 * guarded explain endpoint and render the source-linked, permission-gated breakdown.
 */
const TraceView = ({
  canViewFinance,
  role,
  presetChannelId,
}: {
  canViewFinance: boolean;
  role: Role;
  presetChannelId?: string;
}) => {
  const [month, setMonth] = useState<string>(DEFAULT_MONTH);
  const [metric, setMetric] = useState<ExplanationMetric>(
    "adjusted_gross_revenue_usd",
  );
  // presetChannelId seeds the INITIAL selection only (Registry "Review" nav);
  // the existing resolution below still falls back to the first channel when
  // the preset is absent from the selected month's channel list.
  const [selectedChannelId, setSelectedChannelId] = useState<string>(
    presetChannelId ?? "",
  );

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

  const effectiveChannelId = resolveEffectiveChannelId(selectedChannelId, channels);

  const currency = netRevenue?.currency ?? "USD";

  /** Generate the explanation for the selected channel-month-metric. */
  const runExplain = () => {
    if (!effectiveChannelId) {
      return;
    }
    // The hook captures its own error state; swallow the rejection here so an
    // un-actioned promise does not surface as an unhandled rejection.
    // FIX: dropped the `void` operator and let .catch() be the fire-and-forget
    // sink, satisfying JS-0098 without changing behavior.
    explanation
      .run({
        channelId: effectiveChannelId,
        month,
        metric,
      })
      .catch(() => {
        /* The hook stored the error in state; nothing else to do here. */
      });
  };

  // Clear the rendered explanation whenever the explain inputs change so a
  // result that was in flight under the previous channel/month/metric cannot
  // commit and render under the new filters. reset() also abandons that in-flight
  // request (token bump) so it is discarded when it settles. The first render is
  // a no-op (state already empty); the latch clear keeps the next Explain free.
  const explanationReset = explanation.reset;
  useEffect(() => {
    explanationReset();
  }, [effectiveChannelId, month, metric, explanationReset]);

  const permissionDetails = PERMISSION_DETAILS[role];

  return (
    <section className="view-page" aria-labelledby="traceViewTitle">
      <div className="view-grid wide-side">
        <section className="panel">
          <TraceHeader />

          <ExplanationFilters
            month={month}
            onMonthChange={setMonth}
            effectiveChannelId={effectiveChannelId}
            channels={channels}
            channelsLoading={channelsLoading}
            onChannelChange={setSelectedChannelId}
            metric={metric}
            onMetricChange={setMetric}
            explaining={explanation.loading}
            onExplain={runExplain}
          />

          {channelsError ? (
            <ChannelLoadError error={channelsError} />
          ) : null}

          <ExplanationPanel
            state={explanation}
            canViewFinance={canViewFinance}
            currency={currency}
          />
        </section>

        <PermissionFilterPanel details={permissionDetails} />
      </div>
    </section>
  );
};

export default TraceView;
