import { useEffect, useState, type Dispatch, type SetStateAction } from "react";

import { ApiError } from "@/lib/api/client";
import type {
  AdsensePayment,
  ConnectorCredential,
  ConnectorJobResponse,
  ConnectorRun,
  ConnectorRunPagination,
  ConnectorTestStatus,
} from "@/lib/api/types";
import {
  useAdsensePayments,
  useAdsenseSyncActions,
} from "@/lib/api/useAdsense";
import {
  useConnectorCredentials,
  useConnectorJobActions,
} from "@/lib/api/useConnectors";
import { useConnectorRuns } from "@/lib/api/useConnectorRuns";
import { useConnectorTest } from "@/lib/api/useConnectorTest";
import type { Severity } from "@/lib/mock/data";
import {
  Badge,
  DEFAULT_MONTH,
  Dot,
  financeDisplay,
  formatTimestamp,
  MONTH_OPTIONS,
} from "../shared";
import { describeError } from "./CommandView";

// ============================================================================
// Purpose: The REAL-data Connectors / data-source screen, extracted from
//   AppShell. It reads the configured connector credentials (the "data sources
//   configured" list) from GET /connectors/credentials and the synced AdSense
//   payments from GET /adsense/payments (month-filtered), and wires two write
//   actions: "Request sync" (POST /connectors/jobs) and "Sync payments" (POST
//   /adsense/sync-payments). Each connector credential row exposes a Request
//   sync button that records — but does NOT execute — a job-run intent
//   (execution_status "recorded_not_executed"), surfaced honestly. The view
//   now also consumes GET /connectors/runs for the newest-first run-history
//   feed, with keyset pagination and fail-closed 403 handling. Loading / error
//   / empty / 403 states mirror the other wired views.
// Database/ORM: None (frontend) — consumes GET /connectors/credentials, POST
//   /connectors/jobs (audited record-only), GET /adsense/payments, and POST
//   /adsense/sync-payments (audited payment upsert).
// Standards: No client-side authorization is invented — the backend gates
//   (MANAGE_CONNECTORS for the credentials list, RUN_CONNECTOR_JOBS @connector
//   for jobs + AdSense sync, VIEW_FINALIZED_PAYMENTS @finance_month for the
//   payment list) are authoritative; a 403 surfaces as no-permission copy. The
//   connector secret is never returned (only has_secret_ref); payment_amount is
//   a backend STRING formatted for display only (no float math).
// Blast Radius: Connector job audit write + connector test probes + AdSense
//   payment write — all via the backend's own guarded, audited routes only. No
//   source-of-truth finance number is computed or mutated client-side.
// Connections:
//   - File: frontend/src/lib/api/useConnectors.ts -> credentials + job action hooks.
//   - File: frontend/src/lib/api/useAdsense.ts -> payments + sync action hooks.
//   - File: frontend/src/lib/api/types.ts -> ConnectorCredential / AdsensePayment.
//   - File: backend/ums_smart_revenue/api/connectors.py -> credentials/jobs routes.
//   - File: backend/ums_smart_revenue/api/adsense.py -> payments/sync routes.
// ============================================================================

// Hint shown wherever a connector-operations control is disabled because the
// viewer's role cannot run connector jobs (mirrors the honest no-permission UX).
const CONNECTOR_ROLE_HINT = "Requires a connector-operations role.";

/** Map a connector credential status to a tone for its display badge. */
function credentialStatusTone(status: string): Severity { // skipcq: JS-0067, JS-R1005
  switch (status.toUpperCase()) {
    case "ACTIVE":
    case "CONNECTED":
    case "OK":
      return "green";
    case "DISABLED":
    case "REVOKED":
    case "ERROR":
      return "red";
    case "PENDING":
      return "amber";
    default:
      return "blue";
  }
}

/** Map an AdSense payment status to a tone for its display badge. */
function paymentStatusTone(status: string): Severity { // skipcq: JS-0067
  switch (status.toUpperCase()) {
    case "PAID":
      return "green";
    case "PENDING":
      return "amber";
    case "UNPAID":
      return "red";
    case "CANCELLED":
      return "blue";
    default:
      return "blue";
  }
}

/** Format an ISO date string for display; echoes the raw value if unparsable. */
function formatDate(value: string): string { // skipcq: JS-0067
  // FIX: parse YYYY-MM-DD components directly so new Date() does not treat the
  // string as UTC midnight and shift the displayed day in negative-offset
  // timezones (e.g. "2026-03-01" shows as "Feb 28" for US/Americas users).
  let date: Date;
  const isoDate = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (isoDate) {
    const [, y, m, d] = isoDate;
    date = new Date(Number(y), Number(m) - 1, Number(d));
  } else {
    date = new Date(value);
  }
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "2-digit",
  });
}

/**
 * The REAL-data Connectors / data-source screen: lists configured connector
 * credentials and synced AdSense payments, and wires the audited "Request sync"
 * and "Sync payments" write actions. `canRunConnectors` gates every write
 * control; when false the controls render disabled with an honest role hint.
 * `canViewFinance` gates the source-of-truth payment amounts: a non-finance
 * viewer sees the RESTRICTED_FINANCE_VALUE sentinel via the shared financeDisplay
 * gate rather than the real money value.
 */
export default function ConnectorsView({ // skipcq: JS-0067
  canRunConnectors,
  canManageConnectors,
  canViewFinance,
  canViewConnectorHealth,
}: {
  canRunConnectors: boolean;
  canManageConnectors: boolean;
  canViewFinance: boolean;
  canViewConnectorHealth: boolean;
}) {
  const [month, setMonth] = useState<string>(DEFAULT_MONTH);
  const [reason, setReason] = useState<string>("");

  const credentials = useConnectorCredentials();
  const jobActions = useConnectorJobActions();
  const connectorTest = useConnectorTest();

  const payments = useAdsensePayments({ month });
  const syncActions = useAdsenseSyncActions();

  const credentialRows = credentials.data?.items ?? [];
  const paymentRows = payments.data?.items ?? [];

  // ==========================================================================
  // Purpose: Request a connector job run for one credential row using the reason
  //   typed into the inline, always-visible "Sync reason" field (the button is
  //   already disabled while the reason is empty or a request is in flight). The
  //   backend records + audits the intent but does NOT execute it; on success the
  //   recorded result is shown in a banner. Errors are captured by the hook.
  // ==========================================================================
  const onRequestSync = (credential: ConnectorCredential) => {
    const trimmed = reason.trim();
    if (!canRunConnectors || jobActions.loading || !trimmed) return;
    jobActions
      .requestJob({
        connector_key: credential.connector_key,
        account_id: credential.account_id,
        reason: trimmed,
      })
      .catch(() => {
        // The hook already captured the typed error in jobActions.error and
        // surfaces it in the banner; nothing more to do here.
      });
  };

  return (
    <section className="view-page" aria-labelledby="connectorsTitle">
      <div className="view-grid">
        <DataSourcesPanel
          credentials={credentialRows}
          credentialsLoading={credentials.loading}
          credentialsError={credentials.error}
          onReloadCredentials={() => credentials.reload()}
          canRunConnectors={canRunConnectors}
          canManageConnectors={canManageConnectors}
          reason={reason}
          onReason={setReason}
          jobError={jobActions.error}
          jobResult={jobActions.data}
          requestingJob={jobActions.loading}
          onRequestSync={onRequestSync}
          connectorTest={connectorTest}
          month={month}
          onMonth={setMonth}
          payments={paymentRows}
          paymentsLoading={payments.loading}
          paymentsError={payments.error}
          onReloadPayments={() => payments.reload()}
          canViewFinance={canViewFinance}
        />

        <ConnectorSidebar
          month={month}
          canRunConnectors={canRunConnectors}
          canViewConnectorHealth={canViewConnectorHealth}
          syncActions={syncActions}
          onSynced={() => payments.reload()}
        />
      </div>
    </section>
  );
}

/**
 * Left column of the Connectors screen: the data-sources header, job-request
 * banners, the inline audited reason field, the credentials table and test
 * probes, and the synced AdSense payments section. Extracted to keep the root
 * JSX tree shallow.
 */
function DataSourcesPanel({ // skipcq: JS-0067
  credentials,
  credentialsLoading,
  credentialsError,
  onReloadCredentials,
  canRunConnectors,
  canManageConnectors,
  reason,
  onReason,
  jobError,
  jobResult,
  requestingJob,
  onRequestSync,
  connectorTest,
  month,
  onMonth,
  payments,
  paymentsLoading,
  paymentsError,
  onReloadPayments,
  canViewFinance,
}: {
  credentials: ConnectorCredential[];
  credentialsLoading: boolean;
  credentialsError: ApiError | Error | null;
  onReloadCredentials: () => void;
  canRunConnectors: boolean;
  canManageConnectors: boolean;
  reason: string;
  onReason: (value: string) => void;
  jobError: ApiError | Error | null;
  jobResult: ConnectorJobResponse | null;
  requestingJob: boolean;
  onRequestSync: (credential: ConnectorCredential) => void;
  connectorTest: ReturnType<typeof useConnectorTest>;
  month: string;
  onMonth: (value: string) => void;
  payments: AdsensePayment[];
  paymentsLoading: boolean;
  paymentsError: ApiError | Error | null;
  onReloadPayments: () => void;
  canViewFinance: boolean;
}) {
  return (
    <section className="panel">
      <div className="panel-header">
        <div className="panel-title">
          <strong id="connectorsTitle">Data Sources</strong>
          <span>
            Configured connector connections and the synced data that has flowed
            in — read from SQL-backed source-of-truth tables
          </span>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="Refresh data sources"
          title="Refresh data sources"
          onClick={onReloadCredentials}
        >
          ↻
        </button>
      </div>

      {jobError ? <RequestJobError error={jobError} /> : null}
      {jobResult ? <RequestJobSuccess result={jobResult} /> : null}

      <SyncReasonField
        canRunConnectors={canRunConnectors}
        reason={reason}
        onReason={onReason}
      />

      <ConnectorCredentialsTable
        credentials={credentials}
        loading={credentialsLoading}
        error={credentialsError}
        canRunConnectors={canRunConnectors}
        canManageConnectors={canManageConnectors}
        reason={reason}
        requestingJob={requestingJob}
        onRequestSync={onRequestSync}
        connectorTest={connectorTest}
      />

      <AdsensePaymentsSection
        month={month}
        onMonth={onMonth}
        payments={payments}
        loading={paymentsLoading}
        error={paymentsError}
        onRefresh={onReloadPayments}
        canViewFinance={canViewFinance}
      />
    </section>
  );
}

/**
 * The inline, always-visible "Sync reason" field that every per-row Request sync
 * uses; the reason is recorded on the audit event. When the viewer cannot run
 * connectors the field is disabled and shows the connector-operations hint.
 */
function SyncReasonField({ // skipcq: JS-0067
  canRunConnectors,
  reason,
  onReason,
}: {
  canRunConnectors: boolean;
  reason: string;
  onReason: (value: string) => void;
}) {
  return (
    <div className="field-row" style={{ margin: 13 }}>
      <label htmlFor="connectorSyncReason">Sync reason (required, audited)</label>
      <input
        id="connectorSyncReason"
        value={reason}
        disabled={!canRunConnectors}
        placeholder="Why this sync is being requested"
        onChange={(e) => onReason(e.target.value)}
      />
      {canRunConnectors ? null : (
        <span className="item-sub" role="note">
          {CONNECTOR_ROLE_HINT}
        </span>
      )}
    </div>
  );
}

/**
 * Right column of the Connectors screen: the AdSense payment-sync form panel and
 * the run-history honesty note. Extracted to keep the root JSX tree shallow.
 */
function ConnectorSidebar({ // skipcq: JS-0067
  month,
  canRunConnectors,
  canViewConnectorHealth,
  syncActions,
  onSynced,
}: {
  month: string;
  canRunConnectors: boolean;
  canViewConnectorHealth: boolean;
  syncActions: ReturnType<typeof useAdsenseSyncActions>;
  onSynced: () => void;
}) {
  return (
    <aside className="view-stack">
      <section className="panel">
        <AdsenseSyncHeader canRunConnectors={canRunConnectors} />
        <AdsenseSyncForm
          defaultMonth={month}
          canRunConnectors={canRunConnectors}
          actions={syncActions}
          onSynced={onSynced}
        />
      </section>

      <RunHistory canViewConnectorHealth={canViewConnectorHealth} />
    </aside>
  );
}

/**
 * The AdSense Payment Sync panel header: title/subtitle and the connector-job /
 * restricted badge. Extracted so the sidebar JSX tree stays within the nesting
 * limit; the badge reflects whether the viewer may run connector jobs.
 */
function AdsenseSyncHeader({ canRunConnectors }: { canRunConnectors: boolean }) { // skipcq: JS-0067
  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong>AdSense Payment Sync</strong>
        <span>Supply a payment row to upsert into the finance source</span>
      </div>
      <Badge tone={canRunConnectors ? "amber" : "red"}>
        {canRunConnectors ? "Connector job" : "Restricted"}
      </Badge>
    </div>
  );
}

/** Map a connector run lifecycle status to a tone for its display badge. */
function runStatusTone(status: ConnectorRun["status"]): Severity { // skipcq: JS-0067
  switch (status) {
    case "SUCCEEDED":
      return "green";
    case "PARTIAL":
      return "amber";
    case "FAILED":
      return "red";
    case "RUNNING":
    default:
      return "blue";
  }
}

/**
 * The run-history panel. Fail-closed: a viewer lacking the connector-health
 * capability sees a restricted placeholder and NO fetch fires — the live feed
 * subcomponent (which mounts the run-history hook) is only rendered when
 * permitted, mirroring the AuditTimeline -> AuditTimelineFeed gate. The backend
 * VIEW_CONNECTOR_HEALTH 403 remains authoritative and surfaces as no-permission
 * copy inside the feed.
 */
function RunHistory({ // skipcq: JS-0067
  canViewConnectorHealth,
}: {
  canViewConnectorHealth: boolean;
}) {
  return (
    <section className="panel" aria-labelledby="runHistoryTitle">
      <div className="panel-header">
        <div className="panel-title">
          <strong id="runHistoryTitle">Run History</strong>
          <span>Connector pull runs, newest first — read-only operational log</span>
        </div>
        <Badge tone={canViewConnectorHealth ? "blue" : "red"}>
          {canViewConnectorHealth ? "Live" : "Restricted"}
        </Badge>
      </div>
      {canViewConnectorHealth ? (
        <RunHistoryFeed />
      ) : (
        <div className="permission-band" role="note">
          <Dot tone="red" />
          <span>
            <strong>Run history restricted</strong>
            <span>
              Connector run history requires the VIEW_CONNECTOR_HEALTH
              permission.
            </span>
          </span>
          <Badge tone="red">Restricted</Badge>
        </div>
      )}
    </section>
  );
}

/**
 * Append a fetched run page to the accumulated rows, deduping by run id when the
 * cursor window repeats a row. With no active cursor the page replaces the set.
 */
function appendRunRows( // skipcq: JS-0067
  previous: ConnectorRun[],
  items: ConnectorRun[],
  cursorStartedAt: string | undefined,
  cursorId: string | undefined,
): ConnectorRun[] {
  if (!(cursorStartedAt && cursorId)) return items;
  const seen = new Set(previous.map((row) => row.id));
  const appended = items.filter((row) => !seen.has(row.id));
  return [...previous, ...appended];
}

// ============================================================================
// Purpose: Own the run-history cursor state, page stitching, and pagination
//   actions behind a small hook so the visual feed stays simple. ALWAYS calls
//   useConnectorRuns() unconditionally (it is only mounted when permitted), so
//   the hook stays rules-of-hooks safe. Append + dedupe by run id; reset on
//   filter change (no filters in this first surface, but the reset effect keeps
//   the pattern consistent with the audit feed).
// Database/ORM: None (frontend).
// Standards: cursor is both-or-neither; loadMore advances only when idle and a
//   next page exists. Read-only operational metadata.
// Blast Radius: Connector run read only.
// ============================================================================
type RunHistoryFeedState = {
  error: ApiError | Error | null;
  runs: ConnectorRun[];
  hasMore: boolean;
  loading: boolean;
  loadMore: () => void;
};

/** Merge a fetched run page into the accumulated rows + capture pagination. */
function syncRunPage( // skipcq: JS-0067
  page: { items: ConnectorRun[]; pagination: ConnectorRunPagination },
  cursorStartedAt: string | undefined,
  cursorId: string | undefined,
  setRows: Dispatch<SetStateAction<ConnectorRun[]>>,
  setPagination: Dispatch<SetStateAction<ConnectorRunPagination | null>>,
): void {
  setRows((previous) => appendRunRows(previous, page.items, cursorStartedAt, cursorId));
  setPagination(page.pagination);
}

/** Resolve the run-history feed state (rows, cursor, loadMore). Unconditional hook. */
function useRunHistoryFeedState(): RunHistoryFeedState { // skipcq: JS-0067
  const [rows, setRows] = useState<ConnectorRun[]>([]);
  const [pagination, setPagination] = useState<ConnectorRunPagination | null>(null);
  const [cursorStartedAt, setCursorStartedAt] = useState<string>();
  const [cursorId, setCursorId] = useState<string>();

  const { data, loading, error } = useConnectorRuns({
    cursor_started_at: cursorStartedAt,
    cursor_id: cursorId,
  });

  useEffect(() => {
    if (!data) return;
    syncRunPage(data, cursorStartedAt, cursorId, setRows, setPagination);
  }, [data, cursorStartedAt, cursorId]);

  const hasMore = Boolean(pagination?.has_more && pagination.next_cursor);
  const nextCursor = pagination?.next_cursor;

  /** Advance the cursor to the next window when idle and a next page exists. */
  const loadMore = (): void => {
    if (!nextCursor || loading) return;
    setCursorStartedAt(nextCursor.started_at);
    setCursorId(nextCursor.id);
  };

  return { error, runs: rows, hasMore, loading, loadMore };
}

/**
 * The live connector run-history feed: maps loading / error (403 -> no-permission
 * copy) / empty / loaded states and consumes pagination.next_cursor for a
 * "Load More" append flow (dedupe by run id).
 */
function RunHistoryFeed() { // skipcq: JS-0067
  const { error, runs, hasMore, loading, loadMore } = useRunHistoryFeedState();

  if (error) return <RunHistoryError error={error} />;
  if (loading && runs.length === 0) return <RunHistoryLoadingState />;
  if (runs.length === 0) return <RunHistoryEmptyState />;
  return <RunHistoryList runs={runs} hasMore={hasMore} loading={loading} loadMore={loadMore} />;
}

/** Skeleton-free loading note for the initial run-history fetch. */
function RunHistoryLoadingState() { // skipcq: JS-0067
  return (
    <div className="permission-band" role="note" aria-busy="true">
      <Dot tone="blue" />
      <span>
        <strong>Loading run history…</strong>
        <span>Reading the connector run log.</span>
      </span>
      <Badge tone="blue">Loading</Badge>
    </div>
  );
}

/** Empty-state note shown when no connector run rows exist yet. */
function RunHistoryEmptyState() { // skipcq: JS-0067
  return (
    <div className="permission-band" role="note">
      <Dot tone="amber" />
      <span>
        <strong>No connector runs recorded</strong>
        <span>No connector pull runs have been recorded yet.</span>
      </span>
      <Badge tone="amber">Empty</Badge>
    </div>
  );
}

/** Render the loaded connector run list and its append pagination control. */
function RunHistoryList({ // skipcq: JS-0067
  runs,
  hasMore,
  loading,
  loadMore,
}: {
  runs: ConnectorRun[];
  hasMore: boolean;
  loading: boolean;
  loadMore: () => void;
}) {
  return (
    <div className="timeline" role="list">
      {runs.map((run) => (
        <RunHistoryRow key={run.id} run={run} />
      ))}
      {hasMore && (
        <div className="timeline-item" role="listitem">
          <button
            className="ghost-button"
            type="button"
            onClick={loadMore}
            disabled={loading}
          >
            {loading ? "Loading more…" : "Load More"}
          </button>
        </div>
      )}
    </div>
  );
}

/** Error state for the run-history feed; 403 maps to connector-health copy. */
function RunHistoryError({ error }: { error: ApiError | Error }) { // skipcq: JS-0067
  const described = describeError(error);
  const is403 = error instanceof ApiError && error.status === 403;
  const detail = is403
    ? "Your role cannot view connector run history."
    : described.detail;
  return (
    <div className="permission-band" role="alert">
      <Dot tone="red" />
      <span>
        <strong>{described.title}</strong>
        <span>{detail}</span>
      </span>
      <Badge tone="red">Error</Badge>
    </div>
  );
}

/**
 * A single connector run-history row: connector_key + account, status badge,
 * report month, started/finished timestamps, the counts breakdown, and the
 * error_summary when the run failed or partially failed.
 */
function RunHistoryRow({ run }: { run: ConnectorRun }) { // skipcq: JS-0067
  const counts = run.counts;
  return (
    <div className="timeline-item" role="listitem">
      <span className="timeline-time">{formatTimestamp(run.started_at)}</span>
      <Dot tone={runStatusTone(run.status)} />
      <span>
        <span className="item-title">
          {run.connector_key} · {run.account_id}
        </span>
        <span className="item-sub">
          {`month=${run.report_month} · started ${formatTimestamp(
            run.started_at,
          )} · finished ${formatTimestamp(run.finished_at)}`}
        </span>
        <span className="item-sub" role="note">
          {`reports ${counts.reports_succeeded}/${counts.reports_attempted} ok` +
            `${counts.reports_failed > 0 ? ` · ${counts.reports_failed} failed` : ""}` +
            ` · rows +${counts.rows_upserted_created}/~${counts.rows_upserted_updated}/=` +
            `${counts.rows_upserted_unchanged} (${counts.rows_upserted_total} total)`}
        </span>
        {run.error_summary ? (
          <span className="item-sub" role="note">
            {`error: ${run.error_summary}`}
          </span>
        ) : null}
      </span>
      <Badge tone={runStatusTone(run.status)}>{run.status}</Badge>
    </div>
  );
}

/** Banner shown when a connector job-request POST fails (nothing was recorded). */
function RequestJobError({ error }: { error: ApiError | Error }) { // skipcq: JS-0067
  const { title, detail } = describeError(error);
  return (
    <div className="permission-band" role="alert" style={{ margin: 13 }}>
      <Dot tone="red" />
      <span>
        <strong>{title}</strong>
        <span>{`Sync request failed — ${detail}`}</span>
      </span>
      <Badge tone="red">Not recorded</Badge>
    </div>
  );
}

function RequestJobSuccess({ result }: { result: ConnectorJobResponse }) { // skipcq: JS-0067
  // The backend records but does NOT execute the job; surface that honestly so
  // an operator never assumes data was pulled.
  const recordedOnly = result.execution_status === "recorded_not_executed";
  return (
    <div className="permission-band" role="status" style={{ margin: 13 }}>
      <Dot tone={recordedOnly ? "amber" : "green"} />
      <span>
        <strong>Sync requested</strong>
        <span>
          {`${result.connector_key} · ${result.account_id} — ${
            recordedOnly
              ? "Queued (recorded, not yet executed)"
              : result.execution_status
          }`}
        </span>
      </span>
      <Badge tone={recordedOnly ? "amber" : "green"}>
        {result.execution_status}
      </Badge>
    </div>
  );
}

/**
 * The configured connector data-sources table with a per-row audited
 * "Request sync" button. The request button is disabled while the viewer
 * cannot run connectors, the typed sync reason is empty, or a request is
 * already in flight; the per-row Test button is separately gated on
 * canManageConnectors.
 */
function ConnectorCredentialsTable({ // skipcq: JS-0067, JS-R1005
  credentials,
  loading,
  error,
  canRunConnectors,
  canManageConnectors,
  reason,
  requestingJob,
  onRequestSync,
  connectorTest,
}: {
  credentials: ConnectorCredential[];
  loading: boolean;
  error: ApiError | Error | null;
  canRunConnectors: boolean;
  canManageConnectors: boolean;
  reason: string;
  requestingJob: boolean;
  onRequestSync: (credential: ConnectorCredential) => void;
  connectorTest: ReturnType<typeof useConnectorTest>;
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

  if (loading && credentials.length === 0) {
    return (
      <div className="table-wrap" aria-busy="true">
        <div style={{ padding: 16 }} className="item-sub">
          Loading data sources…
        </div>
      </div>
    );
  }

  if (credentials.length === 0) {
    return (
      <div className="table-wrap">
        <div style={{ padding: 16 }} className="item-sub">
          No connector data sources configured.
        </div>
      </div>
    );
  }

  // The button is enabled only when the viewer may run connectors, a sync reason
  // has been typed, and no other request is in flight.
  const requestDisabled =
    !canRunConnectors || reason.trim().length === 0 || requestingJob;

  return (
    <div className="table-wrap">
      <table aria-label="Connector data sources">
        <ConnectorCredentialsTableHead />
        <tbody>
          {credentials.map((credential) => (
            <ConnectorCredentialRow
              key={credential.id}
              credential={credential}
              canRunConnectors={canRunConnectors}
              canManageConnectors={canManageConnectors}
              requestDisabled={requestDisabled}
              requestingJob={requestingJob}
              onRequestSync={onRequestSync}
              connectorTest={connectorTest}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Column header row for the connector data-sources table. Extracted to keep nesting shallow. */
function ConnectorCredentialsTableHead() { // skipcq: JS-0067
  return (
    <thead>
      <tr>
        <th scope="col">Connector</th>
        <th scope="col">Account</th>
        <th scope="col">Status</th>
        <th scope="col">Secret</th>
        <th scope="col">Action</th>
        <th scope="col">Test</th>
      </tr>
    </thead>
  );
}

/**
 * A single connector data-source row: connector key, account, status badge,
 * secret-configured badge, and the per-row audited "Request sync" button. The
 * button shows the connector-operations hint when the viewer lacks the role.
 */
function ConnectorCredentialRow({ // skipcq: JS-0067
  credential,
  canRunConnectors,
  canManageConnectors,
  requestDisabled,
  requestingJob,
  onRequestSync,
  connectorTest,
}: {
  credential: ConnectorCredential;
  canRunConnectors: boolean;
  canManageConnectors: boolean;
  requestDisabled: boolean;
  requestingJob: boolean;
  onRequestSync: (credential: ConnectorCredential) => void;
  connectorTest: ReturnType<typeof useConnectorTest>;
}) {
  return (
    <tr>
      <td>
        <span className="code-chip">{credential.connector_key}</span>
      </td>
      <td>{credential.account_id}</td>
      <td>
        <Badge tone={credentialStatusTone(credential.status)}>
          {credential.status}
        </Badge>
      </td>
      <td>
        <CredentialSecretBadge hasSecretRef={credential.has_secret_ref} />
      </td>
      <td>
        <button
          className="mini-button"
          type="button"
          disabled={requestDisabled}
          onClick={() => onRequestSync(credential)}
        >
          {requestingJob ? "Working…" : "Request sync"}
        </button>
        {canRunConnectors ? null : (
          <span className="item-sub" role="note">
            {CONNECTOR_ROLE_HINT}
          </span>
        )}
      </td>
      <td>
        <ConnectorTestCell
          credential={credential}
          canManageConnectors={canManageConnectors}
          connectorTest={connectorTest}
        />
      </td>
    </tr>
  );
}

/**
 * Per-credential Test Connection cell. Fires the one-click probe (fixed audited
 * reason) and surfaces the result as a status badge + detail. The button is
 * latched disabled while this row's probe is in flight; a non-404 failure
 * surfaces via the shared describeError pattern. Surfaced only where the
 * management-gated credentials table is shown.
 */
function ConnectorTestCell({ // skipcq: JS-0067
  credential,
  canManageConnectors,
  connectorTest,
}: {
  credential: ConnectorCredential;
  canManageConnectors: boolean;
  connectorTest: ReturnType<typeof useConnectorTest>;
}) {
  const key = `${credential.connector_key}::${credential.account_id}`;
  const result = connectorTest.results[key];
  const error = connectorTest.errors[key];
  const pending = Boolean(connectorTest.pending[key]);
  /**
   * Trigger the one-click test probe for this credential row when the viewer
   * may manage connectors and the row is idle.
   */
  const onTest = () => {
    if (!canManageConnectors || pending) return;
    connectorTest.test(credential.connector_key, credential.account_id).catch(() => {
      // The hook already captured the typed error in connectorTest.errors;
      // the cell renders it below. Nothing more to do here.
    });
  };
  return (
    <>
      <button
        className="mini-button"
        type="button"
        disabled={!canManageConnectors || pending}
        onClick={onTest}
      >
        {pending ? "Testing…" : "Test"}
      </button>
      {result ? (
        <span style={{ display: "block", marginTop: 4 }}>
          <Badge tone={testStatusTone(result.status)}>{result.status}</Badge>
          <span className="item-sub" role="status">
            {result.detail}
          </span>
        </span>
      ) : null}
      {error ? <ConnectorTestError error={error} /> : null}
    </>
  );
}

/** Map a test-connection probe status to a tone for its result badge. */
function testStatusTone(status: ConnectorTestStatus): Severity { // skipcq: JS-0067
  const tones: Record<ConnectorTestStatus, Severity> = {
    ok: "green",
    inactive_credential: "amber",
    auth_failed: "red",
    error: "red",
    not_found: "red",
  };
  return tones[status] ?? "red";
}

/** Inline error note for a failed (non-404) test-connection probe. */
function ConnectorTestError({ error }: { error: ApiError | Error }) { // skipcq: JS-0067
  const { title, detail } = describeError(error);
  return (
    <span className="item-sub" role="alert" style={{ display: "block", marginTop: 4 }}>
      {`${title} — ${detail}`}
    </span>
  );
}

/** Render the secret-status badge for a credential row. */
function CredentialSecretBadge({ hasSecretRef }: { hasSecretRef: boolean }) { // skipcq: JS-0067
  return hasSecretRef ? (
    <Badge tone="green">Configured</Badge>
  ) : (
    <Badge tone="amber">Missing</Badge>
  );
}

/**
 * The synced-AdSense-payments section: a month selector + refresh control above
 * the payments table for the selected month.
 */
function AdsensePaymentsSection({ // skipcq: JS-0067
  month,
  onMonth,
  payments,
  loading,
  error,
  onRefresh,
  canViewFinance,
}: {
  month: string;
  onMonth: (value: string) => void;
  payments: AdsensePayment[];
  loading: boolean;
  error: ApiError | Error | null;
  onRefresh: () => void;
  canViewFinance: boolean;
}) {
  return (
    <>
      <div className="panel-header" style={{ marginTop: 13 }}>
        <div className="panel-title">
          <strong>AdSense Payments</strong>
          <span>Synced payment rows that have flowed in for this month</span>
        </div>
        <div className="control-row" aria-label="AdSense payment filters">
          <select
            className="control"
            aria-label="AdSense month"
            value={month}
            onChange={(e) => onMonth(e.target.value)}
          >
            {MONTH_OPTIONS.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="icon-button"
            aria-label="Refresh AdSense payments"
            title="Refresh AdSense payments"
            onClick={onRefresh}
          >
            ↻
          </button>
        </div>
      </div>
      <AdsensePaymentsTable
        payments={payments}
        loading={loading}
        error={error}
        canViewFinance={canViewFinance}
      />
    </>
  );
}

/**
 * The synced-AdSense-payments table for the selected month, with loading, error,
 * and empty states. Amounts are backend strings formatted for display only and
 * gated through financeDisplay so a non-finance viewer sees the Restricted
 * sentinel instead of the source-of-truth payment value.
 */
function AdsensePaymentsTable({ // skipcq: JS-0067
  payments,
  loading,
  error,
  canViewFinance,
}: {
  payments: AdsensePayment[];
  loading: boolean;
  error: ApiError | Error | null;
  canViewFinance: boolean;
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

  if (loading && payments.length === 0) {
    return (
      <div className="table-wrap" aria-busy="true">
        <div style={{ padding: 16 }} className="item-sub">
          Loading AdSense payments…
        </div>
      </div>
    );
  }

  if (payments.length === 0) {
    return (
      <div className="table-wrap">
        <div style={{ padding: 16 }} className="item-sub">
          No AdSense payments synced for this month.
        </div>
      </div>
    );
  }

  return (
    <div className="table-wrap">
      <table aria-label="AdSense payments">
        <AdsensePaymentsTableHead />
        <tbody>
          {payments.map((payment) => (
            <AdsensePaymentRow
              key={payment.id}
              payment={payment}
              canViewFinance={canViewFinance}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Column header row for the AdSense payments table. Extracted to keep nesting shallow. */
function AdsensePaymentsTableHead() { // skipcq: JS-0067
  return (
    <thead>
      <tr>
        <th scope="col">Account</th>
        <th scope="col">Payment</th>
        <th scope="col">Date</th>
        <th scope="col">Amount</th>
        <th scope="col">Status</th>
      </tr>
    </thead>
  );
}

/**
 * A single synced AdSense payment row. The amount is a backend source-of-truth
 * string rendered for display only (no float math) and gated via financeDisplay,
 * so a non-finance viewer sees the Restricted sentinel rather than the value.
 */
function AdsensePaymentRow({ // skipcq: JS-0067
  payment,
  canViewFinance,
}: {
  payment: AdsensePayment;
  canViewFinance: boolean;
}) {
  return (
    <tr>
      <td>{payment.source_account_id}</td>
      <td>{payment.payment_name}</td>
      <td>{formatDate(payment.payment_date)}</td>
      <td className="money finance-data">
        {financeDisplay(payment.payment_amount, canViewFinance, {
          currency: payment.payment_currency,
        })}
      </td>
      <td>
        <Badge tone={paymentStatusTone(payment.payment_status)}>
          {payment.payment_status}
        </Badge>
      </td>
    </tr>
  );
}

/**
 * The AdSense payment-sync form: collects one payment row plus an audited reason
 * and POSTs it for upsert into the finance source. Disabled with a role hint
 * when the viewer cannot run connectors; the submit button stays disabled until
 * every required field (and the reason) is filled.
 */
function AdsenseSyncForm({ // skipcq: JS-0067, JS-R1005
  defaultMonth,
  canRunConnectors,
  actions,
  onSynced,
}: {
  defaultMonth: string;
  canRunConnectors: boolean;
  actions: ReturnType<typeof useAdsenseSyncActions>;
  onSynced: () => void;
}) {
  const [accountId, setAccountId] = useState<string>("");
  const [paymentName, setPaymentName] = useState<string>("");
  const [paymentDate, setPaymentDate] = useState<string>("");
  const [amount, setAmount] = useState<string>("");
  const [currency, setCurrency] = useState<string>("USD");
  const [reason, setReason] = useState<string>("");

  const canSubmit =
    canRunConnectors &&
    !actions.loading &&
    accountId.trim().length > 0 &&
    paymentName.trim().length > 0 &&
    paymentDate.trim().length > 0 &&
    amount.trim().length > 0 &&
    reason.trim().length > 0;

  /** Submit the single entered payment row for audited upsert into finance. */
  const onSubmit = () => {
    if (!canSubmit) return;
    // The backend rejects an empty batch; supply exactly the one payment row the
    // operator entered. month is derived from the screen's selected month.
    actions
      .syncPayments({
        connector_key: "adsense",
        source_report_id: null,
        reason: reason.trim(),
        payments: [
          {
            source_account_id: accountId.trim(),
            month: defaultMonth,
            payment_name: paymentName.trim(),
            payment_date: paymentDate.trim(),
            payment_amount: amount.trim(),
            payment_currency: currency.trim(),
            payment_status: "PAID",
            raw_payload: {},
          },
        ],
      })
      // FIX: gate side effects on a non-null result — syncPayments() resolves
      // with null when a same-tick duplicate is dropped by the in-flight guard
      // (no POST fired); clearing the reason and calling onSynced() before the
      // real in-flight POST settles would refresh the list and discard the
      // operator's audit reason if that surviving request then fails.
      .then((synced) => {
        if (synced !== null) {
          setReason("");
          onSynced();
        }
      })
      .catch(() => {
        // The hook already captured the typed error in actions.error and
        // surfaces it in the SyncError banner; nothing more to do here.
      });
  };

  return (
    <div className="form-grid" aria-label="Sync AdSense payment" style={{ margin: 13 }}>
      <div className="field-row">
        <label htmlFor="adsenseAccountId">Account id</label>
        <input
          id="adsenseAccountId"
          value={accountId}
          disabled={!canRunConnectors}
          placeholder="e.g. pub-1"
          onChange={(e) => setAccountId(e.target.value)}
        />
      </div>
      <div className="field-row">
        <label htmlFor="adsensePaymentName">Payment name</label>
        <input
          id="adsensePaymentName"
          value={paymentName}
          disabled={!canRunConnectors}
          placeholder="e.g. AdSense payment March 2026"
          onChange={(e) => setPaymentName(e.target.value)}
        />
      </div>
      <div className="field-row">
        <label htmlFor="adsensePaymentDate">Payment date</label>
        <input
          id="adsensePaymentDate"
          type="date"
          value={paymentDate}
          disabled={!canRunConnectors}
          onChange={(e) => setPaymentDate(e.target.value)}
        />
      </div>
      <div className="field-row">
        <label htmlFor="adsenseAmount">Amount</label>
        <input
          id="adsenseAmount"
          value={amount}
          disabled={!canRunConnectors}
          placeholder="e.g. 930"
          inputMode="decimal"
          onChange={(e) => setAmount(e.target.value)}
        />
      </div>
      <div className="field-row">
        <label htmlFor="adsenseCurrency">Currency</label>
        <input
          id="adsenseCurrency"
          value={currency}
          disabled={!canRunConnectors}
          onChange={(e) => setCurrency(e.target.value)}
        />
      </div>
      <div className="field-row">
        <label htmlFor="adsenseReason">Reason</label>
        <input
          id="adsenseReason"
          value={reason}
          disabled={!canRunConnectors}
          placeholder="Recorded on the audit event"
          onChange={(e) => setReason(e.target.value)}
        />
      </div>

      {actions.error ? <SyncError error={actions.error} /> : null}
      {actions.data ? <SyncSuccess count={actions.data.synced_count} /> : null}

      {canRunConnectors ? null : (
        <span className="item-sub" role="note">
          {CONNECTOR_ROLE_HINT}
        </span>
      )}

      <div className="action-row">
        <button
          className="primary-button"
          type="button"
          disabled={!canSubmit}
          onClick={onSubmit}
        >
          {actions.loading ? "Syncing…" : "Sync payments"}
        </button>
      </div>
    </div>
  );
}

/** Banner shown when the AdSense payment-sync POST fails (nothing was synced). */
function SyncError({ error }: { error: ApiError | Error }) { // skipcq: JS-0067
  const { title, detail } = describeError(error);
  return (
    <div className="permission-band" role="alert">
      <Dot tone="red" />
      <span>
        <strong>{title}</strong>
        <span>{`Payment sync failed — ${detail}`}</span>
      </span>
      <Badge tone="red">Not synced</Badge>
    </div>
  );
}

/** Banner confirming how many AdSense payments were upserted into finance. */
function SyncSuccess({ count }: { count: number }) { // skipcq: JS-0067
  return (
    <div className="permission-band" role="status">
      <Dot tone="green" />
      <span>
        <strong>Payments synced</strong>
        <span>{`${count} payment${count === 1 ? "" : "s"} upserted into the finance source`}</span>
      </span>
      <Badge tone="green">Synced</Badge>
    </div>
  );
}
