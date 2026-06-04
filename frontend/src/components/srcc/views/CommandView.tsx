// File: frontend/src/components/srcc/views/CommandView.tsx
// (other imports and code unchanged)

// Change the declaration of describeError to be exported
export function describeError(error: ApiError): string {
  // existing implementation
  if (error.status === 403) {
    return "You do not have permission to view this resource.";
  }
  return `Error ${error.status}: ${error.message}`;
}
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
function CommandWorkspace({
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
export function ChannelRevenuePanel({
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
export function IssueQueuePanel() {
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
export function MonthCloseControlsPanel() {
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
function CloseStepAction({ step }: { step: (typeof CLOSE_STEPS)[number] }) {
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
function ExplainCard({
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
export function ExportReadinessPanel() {
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
const describeError = (error: ApiError | Error): { title: string; detail: string } => {
  if (!(error instanceof ApiError)) {
    return {
      title: "Network error",
      detail: error.message || "Could not reach the revenue service.",
    };
  }
  const detail =
    typeof error.body === "object" &&
    error.body !== null &&
    typeof (error.body as { detail?: unknown }).detail === "string"
      ? (error.body as { detail: string }).detail
      : error.message;
  const errorMap: Record<number, { title: string; detail: string }> = {
    403: {
      title: "No permission",
      detail: "Your role cannot view net revenue for this month or scope.",
    },
  };
  return (
    errorMap[error.status] || {
      title: `Request failed (${error.status})`,
      detail,
    }
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
export function SmartAlertsPanel({ month }: { month: string }) {
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
const SmartAlertsHeaderBadge = ({
  data,
  loading,
  error,
}: {
  data: SmartAlertsSummary | null;
  loading: boolean;
  error: ApiError | Error | null;
}) => {
  const badgeMap: Record<string, { tone: string; label: string }> = {
    error_forbidden: { tone: 'blue', label: 'No permission' },
    error: { tone: 'red', label: 'Error' },
    loading: { tone: 'blue', label: 'Loading' },
    empty: { tone: 'amber', label: 'Empty' },
    CLEAR: { tone: 'green', label: 'Clear' },
  };

  const key = error
    ? error instanceof ApiError && error.status === 403
      ? 'error_forbidden'
      : 'error'
    : loading && !data
    ? 'loading'
    : !data
    ? 'empty'
    : data.status === 'CLEAR'
    ? 'CLEAR'
    : 'DEFAULT';

  if (key !== 'DEFAULT') {
    const { tone, label } = badgeMap[key];
    return <Badge tone={tone}>{label}</Badge>;
  }

  return (
    <Badge tone={data.highest_severity ? severityTone(data.highest_severity) : 'amber'}>
      {data.highest_severity ?? 'Attention'}
    </Badge>
  );
};

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
function ChannelTableHead() {
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
function ChannelRow({
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
