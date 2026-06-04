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
import { Badge, Dot, formatMoney } from "../shared";
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

// Default to a recent, demo-seedable month per the task brief (matches the
// other wired views).
const DEFAULT_MONTH = "2026-03";

// Months offered in the selector (most recent first). A simple dropdown by
// design — wiring real data is the priority, not month discovery.
const MONTH_OPTIONS = ["2026-03", "2026-02", "2026-01", "2025-12"];

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

function formatDate(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "2-digit",
  });
}

export default function ConnectorsView({
  canRunConnectors,
}: {
  canRunConnectors: boolean;
}) {
  const [month, setMonth] = useState<string>(DEFAULT_MONTH);

  const credentials = useConnectorCredentials();
  const jobActions = useConnectorJobActions();

  const payments = useAdsensePayments({ month });
  const syncActions = useAdsenseSyncActions();

  const credentialRows = credentials.data?.items ?? [];
  const paymentRows = payments.data?.items ?? [];

  // ==========================================================================
  // Purpose: Request a connector job run for one credential row. The backend
  //   records + audits the intent but does NOT execute it; on success the
  //   recorded result is shown in a banner. Errors are captured by the hook.
  // ==========================================================================
  const onRequestSync = (credential: ConnectorCredential) => {
    if (!canRunConnectors || jobActions.loading) return;
    const reason = window.prompt(
      `Request a sync for ${credential.connector_key} / ${credential.account_id}: reason (required)`,
    );
    if (reason === null) return; // user cancelled
    const trimmed = reason.trim();
    if (!trimmed) return;
    void jobActions
      .requestJob({
        connector_key: credential.connector_key,
        account_id: credential.account_id,
        reason: trimmed,
      })
      .catch(() => {});
  };

  return (
    <section className="view-page" aria-labelledby="connectorsTitle">
      <div className="view-grid">
        <section className="panel">
          <div className="panel-header">
            <div className="panel-title">
              <strong id="connectorsTitle">Data Sources</strong>
              <span>
                Configured connector connections and the synced data that has
                flowed in — read from SQL-backed source-of-truth tables
              </span>
            </div>
            <button
              type="button"
              className="icon-button"
              aria-label="Refresh data sources"
              title="Refresh data sources"
              onClick={() => credentials.reload()}
            >
              ↻
            </button>
          </div>

          {jobActions.error ? (
            <RequestJobError error={jobActions.error} />
          ) : null}
          {jobActions.data ? <RequestJobSuccess result={jobActions.data} /> : null}

          <ConnectorCredentialsTable
            credentials={credentialRows}
            loading={credentials.loading}
            error={credentials.error}
            canRunConnectors={canRunConnectors}
            requestingJob={jobActions.loading}
            onRequestSync={onRequestSync}
          />

          <AdsensePaymentsSection
            month={month}
            onMonth={setMonth}
            payments={paymentRows}
            loading={payments.loading}
            error={payments.error}
            onRefresh={() => payments.reload()}
          />
        </section>

        <aside className="view-stack">
          <section className="panel">
            <div className="panel-header">
              <div className="panel-title">
                <strong>AdSense Payment Sync</strong>
                <span>Supply a payment row to upsert into the finance source</span>
              </div>
              <Badge tone={canRunConnectors ? "amber" : "red"}>
                {canRunConnectors ? "Connector job" : "Restricted"}
              </Badge>
            </div>
            <AdsenseSyncForm
              defaultMonth={month}
              canRunConnectors={canRunConnectors}
              actions={syncActions}
              onSynced={() => payments.reload()}
            />
          </section>

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
                  No connector-runs read endpoint exists yet. A "Request sync"
                  records and audits the intent (recorded, not executed); the
                  last result appears above when you trigger one.
                </span>
              </span>
              <Badge tone="amber">Gap</Badge>
            </div>
          </section>
        </aside>
      </div>
    </section>
  );
}

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

function ConnectorCredentialsTable({
  credentials,
  loading,
  error,
  canRunConnectors,
  requestingJob,
  onRequestSync,
}: {
  credentials: ConnectorCredential[];
  loading: boolean;
  error: ApiError | Error | null;
  canRunConnectors: boolean;
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

  return (
    <div className="table-wrap">
      <table aria-label="Connector data sources">
        <thead>
          <tr>
            <th scope="col">Connector</th>
            <th scope="col">Account</th>
            <th scope="col">Status</th>
            <th scope="col">Secret</th>
            <th scope="col">Action</th>
          </tr>
        </thead>
        <tbody>
          {credentials.map((credential) => (
            <tr key={credential.id}>
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
                {credential.has_secret_ref ? (
                  <Badge tone="green">Configured</Badge>
                ) : (
                  <Badge tone="amber">Missing</Badge>
                )}
              </td>
              <td>
                <button
                  className="mini-button"
                  type="button"
                  disabled={!canRunConnectors || requestingJob}
                  onClick={() => onRequestSync(credential)}
                >
                  {requestingJob ? "Working…" : "Request sync"}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AdsensePaymentsSection({
  month,
  onMonth,
  payments,
  loading,
  error,
  onRefresh,
}: {
  month: string;
  onMonth: (value: string) => void;
  payments: AdsensePayment[];
  loading: boolean;
  error: ApiError | Error | null;
  onRefresh: () => void;
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
      <AdsensePaymentsTable payments={payments} loading={loading} error={error} />
    </>
  );
}

function AdsensePaymentsTable({
  payments,
  loading,
  error,
}: {
  payments: AdsensePayment[];
  loading: boolean;
  error: ApiError | Error | null;
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
        <thead>
          <tr>
            <th scope="col">Account</th>
            <th scope="col">Payment</th>
            <th scope="col">Date</th>
            <th scope="col">Amount</th>
            <th scope="col">Status</th>
          </tr>
        </thead>
        <tbody>
          {payments.map((payment) => (
            <tr key={payment.id}>
              <td>{payment.source_account_id}</td>
              <td>{payment.payment_name}</td>
              <td>{formatDate(payment.payment_date)}</td>
              <td className="money finance-data">
                {formatMoney(payment.payment_amount, {
                  currency: payment.payment_currency,
                })}
              </td>
              <td>
                <Badge tone={paymentStatusTone(payment.payment_status)}>
                  {payment.payment_status}
                </Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

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

  const onSubmit = () => {
    if (!canSubmit) return;
    // The backend rejects an empty batch; supply exactly the one payment row the
    // operator entered. month is derived from the screen's selected month.
    void actions
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
      .catch(() => {});
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
