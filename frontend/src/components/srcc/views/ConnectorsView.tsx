import { useState } from "react";

import { ApiError } from "@/lib/api/client";
import type {
  AdsensePayment,
  ConnectorCredential,
  ConnectorJobResponse,
} from "@/lib/api/types";
import {
  useAdsensePayments,
  useAdsenseSyncActions,
} from "@/lib/api/useAdsense";
import {
  useConnectorCredentials,
  useConnectorJobActions,
} from "@/lib/api/useConnectors";
import type { Severity } from "@/lib/mock/data";
import {
  Badge,
  DEFAULT_MONTH,
  Dot,
  financeDisplay,
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
//   (execution_status "recorded_not_executed"), surfaced honestly. There is no
//   GET connector-runs route today, so a clearly-labelled "Run history not yet
//   available" note states the gap rather than inventing a feed. Loading /
//   error / 403 states mirror the other wired views.
// Database/ORM: None (frontend) — consumes GET /connectors/credentials, POST
//   /connectors/jobs (audited record-only), GET /adsense/payments, and POST
//   /adsense/sync-payments (audited payment upsert).
// Standards: No client-side authorization is invented — the backend gates
//   (MANAGE_CONNECTORS for the credentials list, RUN_CONNECTOR_JOBS @connector
//   for jobs + AdSense sync, VIEW_FINALIZED_PAYMENTS @finance_month for the
//   payment list) are authoritative; a 403 surfaces as no-permission copy. The
//   connector secret is never returned (only has_secret_ref); payment_amount is
//   a backend STRING formatted for display only (no float math).
// Blast Radius: Connector job audit write + AdSense payment write — both via the
//   backend's own guarded, audited routes only. No source-of-truth finance
//   number is computed or mutated client-side.
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
function credentialStatusTone(status: string): Severity {
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
function paymentStatusTone(status: string): Severity {
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
function formatDate(value: string): string {
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
export default function ConnectorsView({
  canRunConnectors,
  canViewFinance,
}: {
  canRunConnectors: boolean;
  canViewFinance: boolean;
}) {
  const [month, setMonth] = useState<string>(DEFAULT_MONTH);
  const [reason, setReason] = useState<string>("");

  const credentials = useConnectorCredentials();
  const jobActions = useConnectorJobActions();

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
          reason={reason}
          onReason={setReason}
          jobError={jobActions.error}
          jobResult={jobActions.data}
          requestingJob={jobActions.loading}
          onRequestSync={onRequestSync}
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
          syncActions={syncActions}
          onSynced={() => payments.reload()}
        />
      </div>
    </section>
  );
}

/**
 * Left column of the Connectors screen: the data-sources header, job-request
 * banners, the inline audited reason field, the credentials table, and the
 * synced AdSense payments section. Extracted to keep the root JSX tree shallow.
 */
function DataSourcesPanel({
  credentials,
  credentialsLoading,
  credentialsError,
  onReloadCredentials,
  canRunConnectors,
  reason,
  onReason,
  jobError,
  jobResult,
  requestingJob,
  onRequestSync,
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
  reason: string;
  onReason: (value: string) => void;
  jobError: ApiError | Error | null;
  jobResult: ConnectorJobResponse | null;
  requestingJob: boolean;
  onRequestSync: (credential: ConnectorCredential) => void;
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
        reason={reason}
        requestingJob={requestingJob}
        onRequestSync={onRequestSync}
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
function SyncReasonField({
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
function ConnectorSidebar({
  month,
  canRunConnectors,
  syncActions,
  onSynced,
}: {
  month: string;
  canRunConnectors: boolean;
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

      <RunHistoryNote />
    </aside>
  );
}

/**
 * The AdSense Payment Sync panel header: title/subtitle and the connector-job /
 * restricted badge. Extracted so the sidebar JSX tree stays within the nesting
 * limit; the badge reflects whether the viewer may run connector jobs.
 */
function AdsenseSyncHeader({ canRunConnectors }: { canRunConnectors: boolean }) {
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

/**
 * The run-history panel. No connector-runs read endpoint exists yet, so this
 * states the gap honestly rather than faking a timeline.
 */
function RunHistoryNote() {
  return (
    <section className="panel">
      <div className="panel-header">
        <div className="panel-title">
          <strong>Run History</strong>
          <span>Status available today: credentials + last request result</span>
        </div>
        <Badge tone="blue">Status</Badge>
      </div>
      {/* The ConnectorRunORM table exists but has NO read route yet, so a
          live run-history feed cannot be shown without inventing an
          endpoint. State the gap honestly instead of faking a timeline. */}
      <div className="permission-band" role="note">
        <Dot tone="amber" />
        <span>
          <strong>Run history not yet available</strong>
          <span>
            No connector-runs read endpoint exists yet. A &quot;Request
            sync&quot; records and audits the intent (recorded, not executed);
            the last result appears above when you trigger one.
          </span>
        </span>
        <Badge tone="amber">Gap</Badge>
      </div>
    </section>
  );
}

/** Banner shown when a connector job-request POST fails (nothing was recorded). */
function RequestJobError({ error }: { error: ApiError | Error }) {
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

function RequestJobSuccess({ result }: { result: ConnectorJobResponse }) {
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
 * "Request sync" button. Each button is disabled while the viewer cannot run
 * connectors, the typed sync reason is empty, or a request is already in flight.
 */
function ConnectorCredentialsTable({
  credentials,
  loading,
  error,
  canRunConnectors,
  reason,
  requestingJob,
  onRequestSync,
}: {
  credentials: ConnectorCredential[];
  loading: boolean;
  error: ApiError | Error | null;
  canRunConnectors: boolean;
  reason: string;
  requestingJob: boolean;
  onRequestSync: (credential: ConnectorCredential) => void;
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
              requestDisabled={requestDisabled}
              requestingJob={requestingJob}
              onRequestSync={onRequestSync}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Column header row for the connector data-sources table. Extracted to keep nesting shallow. */
function ConnectorCredentialsTableHead() {
  return (
    <thead>
      <tr>
        <th scope="col">Connector</th>
        <th scope="col">Account</th>
        <th scope="col">Status</th>
        <th scope="col">Secret</th>
        <th scope="col">Action</th>
      </tr>
    </thead>
  );
}

/**
 * A single connector data-source row: connector key, account, status badge,
 * secret-configured badge, and the per-row audited "Request sync" button. The
 * button shows the connector-operations hint when the viewer lacks the role.
 */
function ConnectorCredentialRow({
  credential,
  canRunConnectors,
  requestDisabled,
  requestingJob,
  onRequestSync,
}: {
  credential: ConnectorCredential;
  canRunConnectors: boolean;
  requestDisabled: boolean;
  requestingJob: boolean;
  onRequestSync: (credential: ConnectorCredential) => void;
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
    </tr>
  );
}

/** Render the secret-status badge for a credential row. */
function CredentialSecretBadge({ hasSecretRef }: { hasSecretRef: boolean }) {
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
function AdsensePaymentsSection({
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
function AdsensePaymentsTable({
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
function AdsensePaymentsTableHead() {
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
function AdsensePaymentRow({
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
function AdsenseSyncForm({
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
      .then(() => {
        setReason("");
        onSynced();
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
function SyncError({ error }: { error: ApiError | Error }) {
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
function SyncSuccess({ count }: { count: number }) {
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
