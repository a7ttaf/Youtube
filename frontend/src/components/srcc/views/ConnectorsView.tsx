import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";

import { ApiError } from "@/lib/api/client";
import type {
  AdsensePayment,
  AdsenseSyncResponse,
  ConnectorCredential,
  ConnectorCredentialHealth,
  ConnectorCredentialHealthState,
  ConnectorJobResponse,
  ConnectorRun,
  ConnectorRunPagination,
  ConnectorTestStatus,
} from "@/lib/api/types";
import {
  useAdsensePayments,
  useAdsenseSyncActions,
} from "@/lib/api/useAdsense";
import { useConnectorCredentialHealth } from "@/lib/api/useConnectorHealth";
import {
  useConnectorCredentials,
  useConnectorJobActions,
} from "@/lib/api/useConnectors";
import { useConnectorRuns } from "@/lib/api/useConnectorRuns";
import { useConnectorTest } from "@/lib/api/useConnectorTest";
import type { Severity } from "@/types/domain";
import { monthKeyLabel, monthKeyOfDateInput } from "@/lib/months";
import {
  Badge,
  Dot,
  financeDisplay,
  formatTimestamp,
  MONTH_OPTIONS,
  WRITE_DEFAULT_MONTH,
} from "../shared";
import { describeError } from "./CommandView";

// ============================================================================
// Purpose: The REAL-data Connectors / data-source screen, extracted from
//   AppShell. It reads the configured connector credentials (the "data sources
//   configured" list) from GET /connectors/credentials and the synced AdSense
//   payments from GET /adsense/payments (month-filtered), and wires two write
//   actions: "Run pull" (POST /connectors/jobs) and "Sync payments" (POST
//   /adsense/sync-payments). Each connector credential row exposes a Run pull
//   button that submits an audited connector job for the screen's selected
//   report_month (with an optional dry-run validate-only pass); the backend
//   returns execution_status "submitted" on the executing path (a disabled
//   executor returns 503, surfaced as an error). On a "submitted" result the
//   run-history feed refetches.
//   The view also consumes GET /connectors/runs for the newest-first run-history
//   feed, with keyset pagination and fail-closed 403 handling, and GET
//   /connectors/credentials/health for the token-health panel (server-derived
//   health_state + OAuth refresh telemetry per credential). Both connector-health
//   surfaces fail closed on canViewConnectorHealth: when the viewer lacks the
//   capability the gated subtree mounts no hook and issues no request. Loading /
//   error / empty / 403 states mirror the other wired views.
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
//   - File: frontend/src/lib/api/useConnectorHealth.ts -> credential-health hook.
//   - File: frontend/src/lib/api/useAdsense.ts -> payments + sync action hooks.
//   - File: frontend/src/lib/api/types.ts -> ConnectorCredential / AdsensePayment.
//   - File: backend/ums_smart_revenue/api/connectors.py -> credentials/jobs/health routes.
//   - File: backend/ums_smart_revenue/api/adsense.py -> payments/sync routes.
// ============================================================================

// Hint shown wherever a connector-operations control is disabled because the
// viewer's role cannot run connector jobs (mirrors the honest no-permission UX).
const CONNECTOR_ROLE_HINT = "Requires a connector-operations role.";
/**
 * State updater that clears a run-history cursor param back to undefined so
 * the next useConnectorRuns fetch restarts from the first page (typed to
 * return undefined, matching the string | undefined cursor state).
 */
const clearCursorValue = (): undefined => undefined;

// Wire-string -> badge-tone map for connector credential statuses. Statuses
// arrive from the backend as free strings, so credentialStatusTone guards the
// lookup with Object.hasOwn (own properties only — an unexpected key can never
// walk the prototype chain) and falls back to "blue" for unknown statuses.
const CREDENTIAL_STATUS_TONES: Record<string, Severity> = {
  ACTIVE: "green",
  CONNECTED: "green",
  OK: "green",
  DISABLED: "red",
  REVOKED: "red",
  ERROR: "red",
  PENDING: "amber",
};

/** Map a connector credential status to a tone for its display badge. */
const credentialStatusTone = (status: string): Severity => {
  const key = status.toUpperCase();
  return Object.hasOwn(CREDENTIAL_STATUS_TONES, key)
    ? CREDENTIAL_STATUS_TONES[key]
    : "blue";
};

/** Map an AdSense payment status to a tone for its display badge. */
const paymentStatusTone = (status: string): Severity => {
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
};

/** Format an ISO date string for display; echoes the raw value if unparsable. */
const formatDate = (value: string): string => {
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
};

/** Banner shown when a connector job-request POST fails (nothing was recorded). */
const RequestJobError = ({ error }: { error: ApiError | Error }) => {
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
};

/** Banner shown when a connector job request was submitted to the executor. */
const RequestJobSuccess = ({ result }: { result: ConnectorJobResponse }) => {
  // The executing path returns execution_status "submitted": the job was handed to
  // the executor and the run-history feed updates once it starts. A disabled
  // executor returns 503 (surfaced via RequestJobError), so the success banner only
  // ever renders the submitted state.
  // FIX: a dry-run returns 202 'submitted' but the backend intentionally
  // creates no connector_runs row, so the run-history feed cannot change
  // for a dry-run. Branch the success copy on the submitted dry-run
  // state so dry-run users are pointed to the audit outcome (the
  // job_dry_run_completed CONNECTOR_JOB_RUN row) instead of a feed that
  // will never show their pull.
  const message = result.dry_run
    ? "Submitted to executor (dry run) — see audit log for counts + per-report failures"
    : "Submitted to executor — run history will update";
  return (
    <div className="permission-band" role="status" style={{ margin: 13 }}>
      <Dot tone="green" />
      <span>
        <strong>Sync requested</strong>
        <span>{`${result.connector_key} · ${result.account_id} — ${message}`}</span>
      </span>
      <Badge tone="green">{result.execution_status}</Badge>
    </div>
  );
};

/** Map a test-connection probe status to a tone for its result badge. */
const testStatusTone = (status: ConnectorTestStatus): Severity => {
  const tones: Record<ConnectorTestStatus, Severity> = {
    ok: "green",
    inactive_credential: "amber",
    auth_failed: "red",
    error: "red",
    not_found: "red",
  };
  return tones[status] ?? "red";
};

/** Inline error note for a failed (non-404) test-connection probe. */
const ConnectorTestError = ({ error }: { error: ApiError | Error }) => {
  const { title, detail } = describeError(error);
  return (
    <span className="item-sub" role="alert" style={{ display: "block", marginTop: 4 }}>
      {`${title} — ${detail}`}
    </span>
  );
};

/**
 * Per-credential Test Connection cell. Fires the one-click probe (fixed audited
 * reason) and surfaces the result as a status badge + detail. The button is
 * latched disabled while this row's probe is in flight; a non-404 failure
 * surfaces via the shared describeError pattern. Surfaced only where the
 * management-gated credentials table is shown.
 */
const ConnectorTestCell = ({
  credential,
  canManageConnectors,
  connectorTest,
}: {
  credential: ConnectorCredential;
  canManageConnectors: boolean;
  connectorTest: ReturnType<typeof useConnectorTest>;
}) => {
  const key = `${credential.connector_key}::${credential.account_id}`;
  const result = connectorTest.results[key];
  const error = connectorTest.errors[key];
  const pending = Boolean(connectorTest.pending[key]);
  /**
   * Trigger the one-click test probe for this credential row when the viewer
   * may manage connectors and the row is idle.
   */
  const onTest = () => {
    if (!canManageConnectors || pending) {
      return;
    }
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
};

/** Render the secret-status badge for a credential row. */
const CredentialSecretBadge = ({ hasSecretRef }: { hasSecretRef: boolean }) => {
  return hasSecretRef ? (
    <Badge tone="green">Configured</Badge>
  ) : (
    <Badge tone="amber">Missing</Badge>
  );
};

/**
 * A single connector data-source row: connector key, account, status badge,
 * secret-configured badge, and the per-row audited "Request sync" button. The
 * button shows the connector-operations hint when the viewer lacks the role.
 */
const ConnectorCredentialRow = ({
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
}) => {
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
          {requestingJob ? "Working…" : "Run pull"}
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
};

/** Column header row for the connector data-sources table. Extracted to keep nesting shallow. */
const ConnectorCredentialsTableHead = () => {
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
};

// ============================================================================
// Purpose: True while the per-row "Run pull" button must stay disabled — the
//   viewer cannot run connectors, no sync reason has been typed, or another
//   request is already in flight.
// Database/ORM: None (frontend) — a pure predicate over resolved props.
// Standards: No client-side authorization is invented. `canRunConnectors` is a
//   capability the backend already derived; this gates the affordance only, and
//   the backend's RUN_CONNECTOR_JOBS @connector check stays authoritative (a 403
//   surfaces as no-permission copy). Side-effect free and total.
// Blast Radius: Connector execution — a pull submits an audited connector job
//   that ingests source rows and feeds the finance projection. This predicate is
//   what keeps a blank audit reason from reaching POST /connectors/jobs, and the
//   in-flight guard is what stops a double-click submitting the same run twice.
//   It is a usability gate, not the authorization boundary.
// Connections: connectors.py request_connector_job (authoritative gate +
//   audited submit), useConnectors.ts useConnectorJobActions (POST + in-flight).
//   - File: backend/ums_smart_revenue/api/connectors.py:710
//     request_connector_job -> the authoritative permission gate and the
//     audited job submission.
//   - File: frontend/src/lib/api/useConnectors.ts -> useConnectorJobActions
//     owns the POST and the in-flight flag passed in here.
// ============================================================================
const runPullDisabled = (
  canRunConnectors: boolean,
  reason: string,
  requestingJob: boolean,
): boolean =>
  !canRunConnectors || reason.trim().length === 0 || requestingJob;

/**
 * The configured connector data-sources table with a per-row audited
 * "Request sync" button. The request button is disabled while the viewer
 * cannot run connectors, the typed sync reason is empty, or a request is
 * already in flight; the per-row Test button is separately gated on
 * canManageConnectors.
 */
const ConnectorCredentialsTable = ({
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
  const requestDisabled = runPullDisabled(canRunConnectors, reason, requestingJob);

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
};

/** Column header row for the AdSense payments table. Extracted to keep nesting shallow. */
const AdsensePaymentsTableHead = () => {
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
};

/**
 * A single synced AdSense payment row. The amount is a backend source-of-truth
 * string rendered for display only (no float math) and gated via financeDisplay,
 * so a non-finance viewer sees the Restricted sentinel rather than the value.
 */
const AdsensePaymentRow = ({
  payment,
  canViewFinance,
}: {
  payment: AdsensePayment;
  canViewFinance: boolean;
}) => {
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
};

/**
 * The synced-AdSense-payments table for the selected month, with loading, error,
 * and empty states. Amounts are backend strings formatted for display only and
 * gated through financeDisplay so a non-finance viewer sees the Restricted
 * sentinel instead of the source-of-truth payment value.
 */
const AdsensePaymentsTable = ({
  payments,
  loading,
  error,
  canViewFinance,
}: {
  payments: AdsensePayment[];
  loading: boolean;
  error: ApiError | Error | null;
  canViewFinance: boolean;
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
};

/**
 * The synced-AdSense-payments section: a month selector + refresh control above
 * the payments table for the selected month.
 */
const AdsensePaymentsSection = ({
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
}) => {
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
};

/**
 * The inline, always-visible "Sync reason" field that every per-row Run pull
 * uses; the reason is recorded on the audit event. It also carries the dry-run
 * toggle (validate-only, no facts written), the pull request honors, and the
 * PULL MONTH control — the one and only value submitted as the connector run's
 * report_month. That control is deliberately separate from the AdSense
 * payments selector: a payment sync moves the payments filter for visibility
 * but can never retarget a whole-month pull, and the pull's visible value is
 * always exactly what the next Run pull submits (PR #211 review). When the
 * viewer cannot run connectors the controls are disabled and the field shows
 * the connector-operations hint.
 */
const SyncReasonField = ({
  canRunConnectors,
  reason,
  onReason,
  dryRun,
  onDryRun,
  reportMonth,
  onReportMonth,
}: {
  canRunConnectors: boolean;
  reason: string;
  onReason: (value: string) => void;
  dryRun: boolean;
  onDryRun: (value: boolean) => void;
  reportMonth: string;
  onReportMonth: (value: string) => void;
}) => {
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
      <label htmlFor="connectorPullMonth" className="item-sub">
        {" Pull month (becomes report_month)"}
      </label>
      <select
        id="connectorPullMonth"
        className="control"
        aria-label="Pull month"
        value={reportMonth}
        disabled={!canRunConnectors}
        onChange={(e) => onReportMonth(e.target.value)}
      >
        {MONTH_OPTIONS.map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
      </select>
      <label htmlFor="connectorDryRun" className="item-sub">
        <input
          id="connectorDryRun"
          type="checkbox"
          checked={dryRun}
          disabled={!canRunConnectors}
          onChange={(e) => onDryRun(e.target.checked)}
        />
        {" Dry run (validate only, no facts written)"}
      </label>
      <span className="item-sub" role="status">
        {
          "Pulls report a WHOLE calendar month — the payments selector above does not change this control."
        }
      </span>
      {canRunConnectors ? null : (
        <span className="item-sub" role="note">
          {CONNECTOR_ROLE_HINT}
        </span>
      )}
    </div>
  );
};

/**
 * Left column of the Connectors screen: the data-sources header, job-request
 * banners, the inline audited reason field, the credentials table and test
 * probes, and the synced AdSense payments section. Extracted to keep the root
 * JSX tree shallow.
 */
const DataSourcesPanel = ({
  credentials,
  credentialsLoading,
  credentialsError,
  onReloadCredentials,
  canRunConnectors,
  canManageConnectors,
  reason,
  onReason,
  dryRun,
  onDryRun,
  jobError,
  jobResult,
  requestingJob,
  onRequestSync,
  connectorTest,
  month,
  onMonth,
  reportMonth,
  onReportMonth,
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
  dryRun: boolean;
  onDryRun: (value: boolean) => void;
  jobError: ApiError | Error | null;
  jobResult: ConnectorJobResponse | null;
  requestingJob: boolean;
  onRequestSync: (credential: ConnectorCredential) => void;
  connectorTest: ReturnType<typeof useConnectorTest>;
  month: string;
  onMonth: (value: string) => void;
  reportMonth: string;
  onReportMonth: (value: string) => void;
  payments: AdsensePayment[];
  paymentsLoading: boolean;
  paymentsError: ApiError | Error | null;
  onReloadPayments: () => void;
  canViewFinance: boolean;
}) => {
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
        dryRun={dryRun}
        onDryRun={onDryRun}
        reportMonth={reportMonth}
        onReportMonth={onReportMonth}
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
};

/**
 * The AdSense Payment Sync panel header: title/subtitle and the connector-job /
 * restricted badge. Extracted so the sidebar JSX tree stays within the nesting
 * limit; the badge reflects whether the viewer may run connector jobs.
 */
const AdsenseSyncHeader = ({ canRunConnectors }: { canRunConnectors: boolean }) => {
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
};

/** Banner shown when the AdSense payment-sync POST fails (nothing was synced). */
const SyncError = ({ error }: { error: ApiError | Error }) => {
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
};

/** Banner confirming how many AdSense payments were upserted into finance. */
/**
 * Success banner for a payment sync. Names the month the rows were filed under
 * (derived from the payment date): when that month is outside the selector's
 * rolling window the rows cannot be shown by switching the filter, so the
 * banner is the only on-screen record of where they landed.
 */
const SyncSuccess = ({ count, filedMonth }: { count: number; filedMonth: string }) => {
  return (
    <div className="permission-band" role="status">
      <Dot tone="green" />
      <span>
        <strong>Payments synced</strong>
        <span>{`${count} payment${count === 1 ? "" : "s"} upserted into the finance source under ${monthKeyLabel(filedMonth)}`}</span>
      </span>
      <Badge tone="green">Synced</Badge>
    </div>
  );
};

// ============================================================================
// Purpose: True when the AdSense payment-sync form may submit — the viewer can
//   run connectors, no sync request is in flight, and every required field
//   (including the audited reason) is non-blank after trimming.
// Database/ORM: None (frontend) — a pure predicate over resolved form values.
// Standards: No client-side authorization is invented. `canRunConnectors` is a
//   backend-derived capability, so this gates the affordance only and the
//   backend's RUN_CONNECTOR_JOBS check stays authoritative. Required fields are
//   checked after trimming, so whitespace never passes as a supplied value.
//   Side-effect free and total.
// Blast Radius: Finance write — the sync upserts AdSense payment rows into the
//   finance source, so this predicate is what keeps a blank audit reason or a
//   whitespace-only payment field from reaching POST /adsense/sync-payments,
//   and the in-flight guard is what stops a double-click upserting twice. It is
//   a usability gate, not the authorization boundary.
// Connections: adsense.py sync_adsense_payments (authoritative gate + audited
//   upsert), useAdsense.ts useAdsenseSyncActions (POST + loading flag).
//   - File: backend/ums_smart_revenue/api/adsense.py:133 sync_adsense_payments
//     -> the authoritative permission gate and the audited payment upsert.
//   - File: frontend/src/lib/api/useAdsense.ts -> useAdsenseSyncActions owns
//     the POST and the loading flag passed in here.
// ============================================================================
const adsenseSyncCanSubmit = (
  canRunConnectors: boolean,
  loading: boolean,
  requiredFields: readonly string[],
): boolean =>
  canRunConnectors &&
  !loading &&
  requiredFields.every((value) => value.trim().length > 0);

/**
 * The hint under the payment-date field: which month the entered payment will
 * file under, derived from the payment date itself. Empty until the date field
 * holds a complete YYYY-MM-DD value.
 */
const paymentFilingHint = (filingMonth: string): string => {
  return filingMonth
    ? `Files under ${monthKeyLabel(filingMonth)} — the month of the payment date, matching the automated AdSense mapping.`
    : "The payment files under the month of its payment date.";
};

/**
 * The sync-success banner, rendered only when a completed sync result AND its
 * captured filed month are both present. The month is stored at submit time
 * (see filedSuccessMonth), so a later edit of the date field cannot relabel
 * the completed payment; holding this guard outside AdsenseSyncForm also keeps
 * that component's branch count under the complexity threshold.
 */
const SyncSuccessBanner = ({
  result,
  filedMonth,
}: {
  result: AdsenseSyncResponse | null;
  filedMonth: string | null;
}) => {
  if (result === null || filedMonth === null) {
    return null;
  }
  return <SyncSuccess count={result.synced_count} filedMonth={filedMonth} />;
};

/**
 * The AdSense payment-sync form: collects one payment row plus an audited reason
 * and POSTs it for upsert into the finance source. The row's month is DERIVED
 * from the entered payment date (the same settlement-month derivation the
 * automated AdSense mapping uses) and shown next to the field — never inherited
 * from the screen's month state, which opens on the last COMPLETE month for
 * connector pulls and would misfile a current-month payment under the previous
 * month. Disabled with a role hint when the viewer cannot run connectors; the
 * submit button stays disabled until every required field (and the reason) is
 * filled.
 */
const AdsenseSyncForm = ({
  canRunConnectors,
  actions,
  onSynced,
}: {
  canRunConnectors: boolean;
  actions: ReturnType<typeof useAdsenseSyncActions>;
  onSynced: (filedMonth: string) => void;
}) => {
  const [accountId, setAccountId] = useState<string>("");
  const [paymentName, setPaymentName] = useState<string>("");
  const [paymentDate, setPaymentDate] = useState<string>("");
  const [amount, setAmount] = useState<string>("");
  const [currency, setCurrency] = useState<string>("USD");
  const [reason, setReason] = useState<string>("");
  // ==========================================================================
  // Purpose: The month the LAST successful payment sync filed under, captured
  //   in the submit closure at POST time. FIX (PR #211 review): SyncSuccess
  //   must not read the live paymentDate derivation — editing or clearing the
  //   date field after a submit relabeled the completed payment.
  // Database/ORM: None (frontend form state over an already-posted value).
  // Standards: Cleared when a new request starts, mirroring actions.data being
  //   cleared while a sync is in flight, so the banner and its month always
  //   belong to the same completed request; the banner renders only when both
  //   are present (SyncSuccessBanner), so a data-without-month edge renders
  //   nothing rather than a guessed month.
  // Blast Radius: Display only (the success banner's label); no write path —
  //   the posted month itself comes from filingMonth in the request body.
  // Connections:
  //   - File: SyncSuccessBanner / SyncSuccess (this file) -> render it.
  //   - File: frontend/tests/.../ConnectorsView.test.tsx -> pins that editing
  //     the date after submit does not relabel the banner.
  // ==========================================================================
  const [filedSuccessMonth, setFiledSuccessMonth] = useState<string | null>(null);

  // FIX (PR #211 review): the month this payment row files under comes from
  // the entered payment date, so an August 21 payment files under August even
  // while the screen selector opens on the last complete month (July). Empty
  // until the date field holds a complete YYYY-MM-DD value, and that emptiness
  // is part of the submit gate — a malformed date can never POST a guessed
  // month.
  const filingMonth = monthKeyOfDateInput(paymentDate);

  const canSubmit = adsenseSyncCanSubmit(canRunConnectors, actions.loading, [
    accountId,
    paymentName,
    paymentDate,
    amount,
    reason,
    filingMonth,
  ]);

  /** Submit the single entered payment row for audited upsert into finance. */
  const onSubmit = () => {
    if (!canSubmit) return;
    // A new request invalidates the previous success banner's month, mirroring
    // actions.data being cleared while a sync is in flight.
    setFiledSuccessMonth(null);
    // The backend rejects an empty batch; supply exactly the one payment row the
    // operator entered, filed under the month of its payment date (canSubmit's
    // required-field gate already proved filingMonth non-empty).
    actions
      .syncPayments({
        connector_key: "adsense",
        source_report_id: null,
        reason: reason.trim(),
        payments: [
          {
            source_account_id: accountId.trim(),
            month: filingMonth,
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
          // Capture the month THIS submit filed under, not the live field —
          // the banner must keep naming the posted month even if the operator
          // edits or clears the date field afterwards.
          setFiledSuccessMonth(filingMonth);
          onSynced(filingMonth);
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
      <p className="item-sub" role="status">
        {paymentFilingHint(filingMonth)}
      </p>
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
      <SyncSuccessBanner result={actions.data} filedMonth={filedSuccessMonth} />

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
};

/** Map a connector run lifecycle status to a tone for its display badge. */
const runStatusTone = (status: ConnectorRun["status"]): Severity => {
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
 * Append a fetched run page to the accumulated rows, deduping by run id when the
 * cursor window repeats a row. With no active cursor the page replaces the set.
 */
const appendRunRows = (
  previous: ConnectorRun[],
  items: ConnectorRun[],
  cursorStartedAt: string | undefined,
  cursorId: string | undefined,
): ConnectorRun[] => {
  if (!(cursorStartedAt && cursorId)) return items;
  const seen = new Set(previous.map((row) => row.id));
  const appended = items.filter((row) => !seen.has(row.id));
  return [...previous, ...appended];
};

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
const syncRunPage = (
  page: { items: ConnectorRun[]; pagination: ConnectorRunPagination },
  cursorStartedAt: string | undefined,
  cursorId: string | undefined,
  setRows: Dispatch<SetStateAction<ConnectorRun[]>>,
  setPagination: Dispatch<SetStateAction<ConnectorRunPagination | null>>,
): void => {
  setRows((previous) => appendRunRows(previous, page.items, cursorStartedAt, cursorId));
  setPagination(page.pagination);
};

/** Resolve the run-history feed state (rows, cursor, loadMore). Unconditional hook. */
const useRunHistoryFeedState = (reloadToken: number): RunHistoryFeedState => {
  const [rows, setRows] = useState<ConnectorRun[]>([]);
  const [pagination, setPagination] = useState<ConnectorRunPagination | null>(null);
  const [cursorStartedAt, setCursorStartedAt] = useState<string>();
  const [cursorId, setCursorId] = useState<string>();

  const { data, loading, error, reload } = useConnectorRuns({
    cursor_started_at: cursorStartedAt,
    cursor_id: cursorId,
  });

  // FIX: when a Run pull submits a new job (reloadToken bumps), reset the cursor
  // window back to page 1 and refetch so the newest run surfaces at the top
  // without a manual refresh. Skip the initial mount (reloadToken === 0).
  useEffect(() => {
    if (reloadToken === 0) return;
    setRows([]);
    setPagination(null);
    setCursorStartedAt(clearCursorValue);
    setCursorId(clearCursorValue);
    reload();
  }, [reloadToken, reload]);

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
};

/** Skeleton-free loading note for the initial run-history fetch. */
const RunHistoryLoadingState = () => {
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
};

/** Empty-state note shown when no connector run rows exist yet. */
const RunHistoryEmptyState = () => {
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
};

/** Error state for the run-history feed; 403 maps to connector-health copy. */
const RunHistoryError = ({ error }: { error: ApiError | Error }) => {
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
};

/**
 * A single connector run-history row: connector_key + account, status badge,
 * report month, started/finished timestamps, the counts breakdown, and the
 * error_summary when the run failed or partially failed.
 */
const RunHistoryRow = ({ run }: { run: ConnectorRun }) => {
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
};

/** Render the loaded connector run list and its append pagination control. */
const RunHistoryList = ({
  runs,
  hasMore,
  loading,
  loadMore,
}: {
  runs: ConnectorRun[];
  hasMore: boolean;
  loading: boolean;
  loadMore: () => void;
}) => {
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
};

/**
 * The live connector run-history feed: maps loading / error (403 -> no-permission
 * copy) / empty / loaded states and consumes pagination.next_cursor for a
 * "Load More" append flow (dedupe by run id). reloadToken refetches page 1 after
 * a successful Run pull submit.
 */
const RunHistoryFeed = ({ reloadToken }: { reloadToken: number }) => {
  const { error, runs, hasMore, loading, loadMore } = useRunHistoryFeedState(reloadToken);

  if (error) return <RunHistoryError error={error} />;
  if (loading && runs.length === 0) return <RunHistoryLoadingState />;
  if (runs.length === 0) return <RunHistoryEmptyState />;
  return <RunHistoryList runs={runs} hasMore={hasMore} loading={loading} loadMore={loadMore} />;
};

/**
 * The run-history panel. Fail-closed: a viewer lacking the connector-health
 * capability sees a restricted placeholder and NO fetch fires — the live feed
 * subcomponent (which mounts the run-history hook) is only rendered when
 * permitted, mirroring the AuditTimeline -> AuditTimelineFeed gate. The backend
 * VIEW_CONNECTOR_HEALTH 403 remains authoritative and surfaces as no-permission
 * copy inside the feed.
 */
const RunHistory = ({
  canViewConnectorHealth,
  reloadToken,
}: {
  canViewConnectorHealth: boolean;
  reloadToken: number;
}) => {
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
        <RunHistoryFeed reloadToken={reloadToken} />
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
};

const HEALTH_STATE_TONES: Record<ConnectorCredentialHealthState, Severity> = {
  healthy: "green",
  expiring: "amber",
  auth_failed: "red",
  missing: "red",
  unknown: "blue",
};

/** Map a server-derived credential health_state to a tone for its display badge. */
const healthStateTone = (state: ConnectorCredentialHealthState): Severity => HEALTH_STATE_TONES[state];

/** Skeleton-free loading note for the initial credential-health fetch. */
const TokenHealthLoadingState = () => {
  return (
    <div className="permission-band" role="note" aria-busy="true">
      <Dot tone="blue" />
      <span>
        <strong>Loading token health…</strong>
        <span>Reading the connector credential telemetry.</span>
      </span>
      <Badge tone="blue">Loading</Badge>
    </div>
  );
};

/** Empty-state note shown when no connector credentials exist yet. */
const TokenHealthEmptyState = () => {
  return (
    <div className="permission-band" role="note">
      <Dot tone="amber" />
      <span>
        <strong>No connector credentials configured</strong>
        <span>No connector credentials are available to report health for.</span>
      </span>
      <Badge tone="amber">Empty</Badge>
    </div>
  );
};

/** Error state for the credential-health feed; 403 maps to connector-health copy. */
const TokenHealthError = ({ error }: { error: ApiError | Error }) => {
  const described = describeError(error);
  const is403 = error instanceof ApiError && error.status === 403;
  const detail = is403
    ? "Your role cannot view connector credential health."
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
};

/**
 * A single credential-health row: connector_key + account, the server-derived
 * health_state badge, token expiry, last refresh attempt + status, and the
 * refresh error class when the last refresh recorded one. All values are
 * server-derived; the view formats timestamps for display only.
 */
const TokenHealthRow = ({
  credential,
}: {
  credential: ConnectorCredentialHealth;
}) => {
  const tone = healthStateTone(credential.health_state);
  const status = credential.last_refresh_status ?? "never run";
  return (
    <div className="timeline-item" role="listitem">
      <Dot tone={tone} />
      <span>
        <span className="item-title">
          {credential.connector_key} · {credential.account_id}
        </span>
        <span className="item-sub">
          {`expires ${formatTimestamp(credential.token_expiry_at)}` +
            ` · last attempt ${formatTimestamp(credential.last_refresh_attempt_at)}` +
            ` · refresh ${status}`}
        </span>
        {credential.last_refresh_error_class ? (
          <span className="item-sub" role="note">
            {`error: ${credential.last_refresh_error_class}`}
          </span>
        ) : null}
      </span>
      <Badge tone={tone}>{credential.health_state}</Badge>
    </div>
  );
};

/** Render the loaded credential-health list (one row per credential). */
const TokenHealthList = ({
  credentials,
}: {
  credentials: ConnectorCredentialHealth[];
}) => {
  return (
    <div className="timeline" role="list">
      {credentials.map((credential) => (
        <TokenHealthRow key={credential.id} credential={credential} />
      ))}
    </div>
  );
};

/**
 * The live credential-health feed: maps loading / error (403 -> no-permission
 * copy) / empty / loaded states from GET /connectors/credentials/health. The
 * health_state and telemetry are server-derived; the view never recomputes them.
 */
type TokenHealthFeedView = "error" | "loading" | "empty" | "list";

/**
 * Pick the credential-health feed's render state with strict precedence:
 * error wins over everything, "loading" applies only while no rows are on
 * screen yet (a background refetch over existing rows keeps the list — no
 * skeleton flash), then empty vs list by row count.
 */
const tokenHealthFeedView = (
  error: ApiError | Error | null,
  loading: boolean,
  rowCount: number,
): TokenHealthFeedView => {
  if (error) return "error";
  if (loading && rowCount === 0) return "loading";
  if (rowCount === 0) return "empty";
  return "list";
};

/**
 * Render the credential-health feed from GET /connectors/credentials/health:
 * one of the four states chosen by tokenHealthFeedView, with a 403 surfacing
 * as the no-permission copy inside the error state. Only mounted inside the
 * health panel, which itself returns null when the viewer lacks
 * canViewConnectorHealth, so the fetch never runs for a viewer who cannot see
 * it.
 */
const TokenHealthFeed = () => {
  const { data, loading, error } = useConnectorCredentialHealth();
  const rows = data ?? [];
  const view = tokenHealthFeedView(error, loading, rows.length);

  if (view === "error") {
    return <TokenHealthError error={error as ApiError | Error} />;
  }

  if (view === "loading") {
    return <TokenHealthLoadingState />;
  }

  if (view === "empty") {
    return <TokenHealthEmptyState />;
  }

  return <TokenHealthList credentials={rows} />;
};

/**
 * The token-health panel. Fail-closed: a viewer lacking the connector-health
 * capability sees NOTHING (the panel is not rendered) and NO fetch fires — the
 * live feed subcomponent (which mounts the credential-health hook) is only
 * rendered when permitted, mirroring RunHistory. The backend
 * VIEW_CONNECTOR_HEALTH 403 remains authoritative and surfaces as no-permission
 * copy inside the feed.
 */
const TokenHealth = ({
  canViewConnectorHealth,
}: {
  canViewConnectorHealth: boolean;
}) => {
  // FIX: fail-closed — render the panel only when the viewer holds the
  // capability so a non-permitted viewer mounts no hook and issues no
  // /connectors/credentials/health request (defense in depth alongside the
  // authoritative backend gate), mirroring the AuditTimeline -> feed gate.
  if (!canViewConnectorHealth) {
    return null;
  }
  return (
    <section
      className="panel"
      aria-labelledby="tokenHealthTitle"
    >
      <div className="panel-header">
        <div className="panel-title">
          <strong id="tokenHealthTitle">Token Health</strong>
          <span>OAuth credential refresh telemetry — read-only operational log</span>
        </div>
        <Badge tone="blue">Live</Badge>
      </div>
      <TokenHealthFeed />
    </section>
  );
};

/**
 * Right column of the Connectors screen: the AdSense payment-sync form panel and
 * the run-history honesty note. Extracted to keep the root JSX tree shallow.
 */
const ConnectorSidebar = ({
  canRunConnectors,
  canViewConnectorHealth,
  syncActions,
  onSynced,
  reloadToken,
}: {
  canRunConnectors: boolean;
  canViewConnectorHealth: boolean;
  syncActions: ReturnType<typeof useAdsenseSyncActions>;
  onSynced: (filedMonth: string) => void;
  reloadToken: number;
}) => {
  return (
    <aside className="view-stack">
      <section className="panel">
        <AdsenseSyncHeader canRunConnectors={canRunConnectors} />
        <AdsenseSyncForm
          canRunConnectors={canRunConnectors}
          actions={syncActions}
          onSynced={onSynced}
        />
      </section>

      <RunHistory
        canViewConnectorHealth={canViewConnectorHealth}
        reloadToken={reloadToken}
      />

      <TokenHealth canViewConnectorHealth={canViewConnectorHealth} />
    </aside>
  );
};

/**
 * The REAL-data Connectors / data-source screen: lists configured connector
 * credentials and synced AdSense payments, and wires the audited "Request sync"
 * and "Sync payments" write actions. `canRunConnectors` gates every write
 * control; when false the controls render disabled with an honest role hint.
 * `canViewFinance` gates the source-of-truth payment amounts: a non-finance
 * viewer sees the RESTRICTED_FINANCE_VALUE sentinel via the shared financeDisplay
 * gate rather than the real money value.
 */
export const ConnectorsView = ({
  canRunConnectors,
  canManageConnectors,
  canViewFinance,
  canViewConnectorHealth,
}: {
  canRunConnectors: boolean;
  canManageConnectors: boolean;
  canViewFinance: boolean;
  canViewConnectorHealth: boolean;
}) => {
  // FIX: this screen's month state is a WRITE default, not just a filter — it
  // becomes the connector run's report_month and the AdSense payment row's
  // month. The Google clients pull a whole calendar month and the backend only
  // validates the "YYYY-MM" format, so seeding the IN-PROGRESS month (the
  // rolling DEFAULT_MONTH the read views open on) would ingest a partial month
  // as if it were final. Seed the last COMPLETE month instead — and from the
  // SAME module-load snapshot as MONTH_OPTIONS: WRITE_DEFAULT_MONTH is that
  // frozen window's index-1 entry, never a fresh clock read at mount. Reading
  // the wall clock here again (the previous code) let a tab left open across
  // two month boundaries seed a month absent from the frozen <option> list,
  // rendering a blank selector that still submitted its invisible month
  // (PR #211 review). The selector still offers every MONTH_OPTIONS entry,
  // current month included, so this fixes the default, not the operator's
  // choice.
  const [month, setMonth] = useState<string>(WRITE_DEFAULT_MONTH);
  // FIX (PR #211 review): the payments-list FILTER is deliberately separate
  // from the connector-run REPORT MONTH. A successful payment sync switches
  // the filter so the new row is visible, but that auto-switch must never
  // retarget the next connector pull — a current-month payment would
  // otherwise silently move report_month onto the in-progress month and
  // ingest a partial month as final, the exact hazard the write default
  // prevents. The AdSense selector drives ONLY this filter; the pull month
  // has its own visible control in SyncReasonField bound to `month`, so each
  // control's displayed value is exactly what it submits or queries.
  const [filterMonth, setFilterMonth] = useState<string>(WRITE_DEFAULT_MONTH);
  const [reason, setReason] = useState<string>("");
  const [dryRun, setDryRun] = useState<boolean>(false);
  // Nonce bumped after a successful executing-path submit so the run-history feed
  // refetches (the new run should appear newest-first without a manual refresh).
  // FIX: the worker does not create the connector_runs row until after credential
  // resolution + OAuth refresh inside _run_live, so a single immediate refetch
  // can easily run before the RUNNING row exists and the feed stays stale until
  // the operator manually refreshes. We bump the nonce a few times over the
  // next few seconds to catch the row once the worker has committed start_run.
  // The worker takes ~1-2s on the OAuth refresh + DB commit, so a 0/1/3/5s
  // schedule covers the worst case (the first 0s tick handles the happy path;
  // the later ticks catch the row once start_run commits). The timer ids are
  // captured in a ref and cleared on unmount so a fast navigation away from
  // the view cannot trigger setState on an unmounted component.
  const [reloadToken, setReloadToken] = useState<number>(0);
  // FIX: capture the setTimeout ids in a ref so the unmount cleanup can
  // clear them; previously the timers persisted after the component
  // unmounted and could fire setReloadToken on an unmounted React tree.
  const pollTimersRef = useRef<number[]>([]);
  /**
   * Bump the run-history reload token now and again at 1/3/5s so the feed
   * catches the connector_runs row once the worker commits it after its OAuth
   * refresh (a single immediate refetch can run before the RUNNING row
   * exists). Timer ids land in pollTimersRef and are cleared on unmount so a
   * navigation away cannot setState on a dead tree.
   */
  const runsReloadPoll = () => {
    setReloadToken((n) => n + 1);
    const timers = pollTimersRef.current;
    timers.push(window.setTimeout(() => setReloadToken((n) => n + 1), 1000));
    timers.push(window.setTimeout(() => setReloadToken((n) => n + 1), 3000));
    timers.push(window.setTimeout(() => setReloadToken((n) => n + 1), 5000));
  };

  // FIX: clear any pending poll timers on unmount so a navigation away
  // from the view cannot fire setReloadToken on a dead React tree. Empty
  // cleanup is a no-op when no poll is in flight.
  useEffect(() => {
    return () => {
      const timers = pollTimersRef.current;
      for (const id of timers) window.clearTimeout(id);
      timers.length = 0;
    };
  }, []);

  const credentials = useConnectorCredentials();
  const jobActions = useConnectorJobActions();
  const connectorTest = useConnectorTest();

  const payments = useAdsensePayments({ month: filterMonth });
  const syncActions = useAdsenseSyncActions();

  const credentialRows = credentials.data?.items ?? [];
  const paymentRows = payments.data?.items ?? [];

  const handleReloadCredentials = useCallback(
    () => credentials.reload(),
    [credentials],
  );
  const handleReloadPayments = useCallback(
    () => payments.reload(),
    [payments],
  );
  // ==========================================================================
  // Purpose: The AdSense payments selector is now PURELY the list filter. FIX
  //   (PR #211 review, codex deDWi): while it also implied the connector
  //   pull's report_month, a payment sync that moved the filter left the
  //   displayed month and the submitted report_month silently divergent — and
  //   re-selecting the displayed value emits no change event, so an operator
  //   could pull "the month on screen" and write the previous month's report.
  //   The pull month now has its OWN visible control (SyncReasonField's
  //   "Pull month" select); the displayed value and the submitted value are
  //   the same state by construction.
  // Database/ORM: None (frontend filter/pull-target state).
  // Standards: One control, one concern, one state: the AdSense selector
  //   drives ONLY the payments query; the pull select drives ONLY
  //   report_month. No cross-moving, no hidden defaults after a sync.
  // Blast Radius: Which month the payments list filters and which month a
  //   connector run reports — both now exactly what their controls display.
  // Connections:
  //   - File: AdsensePaymentsSection (this file) -> the filter selector.
  //   - File: SyncReasonField (this file) -> the pull-month control.
  //   - File: onRequestSync (this file) -> submits report_month = month.
  // ==========================================================================
  const handleMonthChange = useCallback((value: string) => {
    setFilterMonth(value);
  }, []);

  // ==========================================================================
  // Purpose: Post-sync visibility for a filed payment WITHOUT retargeting the
  //   connector pull. FIX (PR #211 review, Devin): a synced payment files under
  //   the month of its payment date, which can differ from the filter month.
  // Database/ORM: None (frontend filter state; the row itself was already
  //   posted by AdsenseSyncForm).
  // Standards: Only the payments FILTER follows the filed month — when it IS
  //   one of the rolling options the filter switches so the just-written row
  //   appears (the month change refetches the list); when it is OLDER than the
  //   window, switching would render a value with no matching option (the same
  //   blank-selector defect this PR fixes elsewhere), so the filter stays and
  //   the success banner names the month instead. The report month for
  //   connector pulls NEVER moves here: an auto-switch once retargeted the
  //   next pull onto the in-progress month, silently scheduling a partial-
  //   month ingest (the exact hazard WRITE_DEFAULT_MONTH exists to prevent).
  // Blast Radius: The payments list filter only; display. The connector-run
  //   report_month path is untouched by construction.
  // Connections:
  //   - File: AdsenseSyncForm (this file) -> calls onSynced(filingMonth) from
  //     the submit closure, so the month here is the POSTED one.
  //   - File: frontend/src/components/srcc/shared.tsx -> MONTH_OPTIONS, the
  //     offered window membership is checked against.
  //   - File: SyncReasonField (this file) -> always displays the pull's
  //     report month, which this callback no longer moves.
  // ==========================================================================
  const handleSynced = useCallback(
    (filedMonth: string) => {
      if (MONTH_OPTIONS.includes(filedMonth) && filterMonth !== filedMonth) {
        setFilterMonth(filedMonth); // the filter change itself refetches the list
        return;
      }
      payments.reload();
    },
    [filterMonth, payments],
  );

  // ==========================================================================
  // Purpose: Run a connector pull for one credential row using the reason typed
  //   into the inline, always-visible "Sync reason" field (the button is already
  //   disabled while the reason is empty, a request is in flight, or no month is
  //   selected). The request carries the screen's selected report_month + the
  //   dry-run toggle. The backend submits the job to the executor and audits it;
  //   on a "submitted" result the run-history feed is refetched so the new run
  //   surfaces. Errors are captured by the hook and shown in the banner.
  // ==========================================================================
  const onRequestSync = (credential: ConnectorCredential) => {
    const trimmed = reason.trim();
    if (!canRunConnectors || jobActions.loading || !trimmed || !month) return;
    jobActions
      .requestJob({
        connector_key: credential.connector_key,
        account_id: credential.account_id,
        report_month: month,
        dry_run: dryRun,
        reason: trimmed,
      })
      .then((result) => {
        // Refetch the run-history feed only when the executor accepted the job
        // AND the viewer can see the feed; a record-only fallback, a dry-run
        // (the backend intentionally creates no connector_runs row for
        // dry-runs so the feed cannot change), or a viewer without
        // connector-health does not need (or get) a refresh. The poll
        // variant spreads the refetch over 0/1/3/5s so the worker has time to
        // commit start_run (after the OAuth refresh) before the feed is
        // re-queried -- a single immediate refetch would miss the row.
        if (
          result !== null &&
          result.execution_status === "submitted" &&
          !dryRun &&
          canViewConnectorHealth
        ) {
          runsReloadPoll();
        }
      })
      .catch((_err: unknown) => {
        // The hook already captured the typed error in jobActions.error and
        // surfaces it in the banner; log here for traceability at this call site.
        console.error("[ConnectorsView] connector job request failed:", _err);
      });
  };

  return (
    <section className="view-page" aria-labelledby="connectorsTitle">
      <div className="view-grid">
        <DataSourcesPanel
          credentials={credentialRows}
          credentialsLoading={credentials.loading}
          credentialsError={credentials.error}
          onReloadCredentials={handleReloadCredentials}
          canRunConnectors={canRunConnectors}
          canManageConnectors={canManageConnectors}
          reason={reason}
          onReason={setReason}
          dryRun={dryRun}
          onDryRun={setDryRun}
          jobError={jobActions.error}
          jobResult={jobActions.data}
          requestingJob={jobActions.loading}
          onRequestSync={onRequestSync}
          connectorTest={connectorTest}
          month={filterMonth}
          onMonth={handleMonthChange}
          reportMonth={month}
          onReportMonth={setMonth}
          payments={paymentRows}
          paymentsLoading={payments.loading}
          paymentsError={payments.error}
          onReloadPayments={handleReloadPayments}
          canViewFinance={canViewFinance}
        />

        <ConnectorSidebar
          canRunConnectors={canRunConnectors}
          canViewConnectorHealth={canViewConnectorHealth}
          syncActions={syncActions}
          onSynced={handleSynced}
          reloadToken={reloadToken}
        />
      </div>
    </section>
  );
};
