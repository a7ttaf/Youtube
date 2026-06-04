import { useCallback, useState } from "react";

import { ApiError } from "@/lib/api/client";
import type {
  FinanceCloseBlocker,
  FinanceCloseLockErrorDetail,
  FinanceCloseReadinessResponse,
  FinanceMonthCloseStatus,
} from "@/lib/api/types";
import {
  useMonthClose,
  useMonthCloseActions,
  useMonthCloseReadiness,
} from "@/lib/api/useMonthClose";
import type { Severity } from "@/lib/mock/data";
import { RECON_NOTES } from "@/lib/mock/data";
import { Badge, Dot, ItemRow } from "../shared";
import { describeError } from "./CommandView";

// ============================================================================
// Purpose: The REAL-data Month-Close screen, extracted from AppShell. Renders a
//   finance month's close status (OPEN/LOCKED + lock/unlock actor & timestamps)
//   from GET /finance-close/{month} and its readiness checklist (blockers +
//   ready flag) from GET /finance-close/{month}/readiness, with explicit
//   loading / error / 403 states mirroring CommandView. Wires Lock and Unlock
//   actions (POST {reason}) behind a reason prompt + confirm step, mapping a
//   409 (blockers / wrong state) to a clear inline message and refetching
//   status on success. The Reconciliation Equation side panel stays on mock
//   data (not part of the close API) and is labelled as such.
// Database/ORM: None (frontend) — consumes the finance-close read endpoints and
//   the guarded, audited lock/unlock write endpoints.
// Standards: No client-side finance authorization is invented — the backend
//   gate (LOCK/UNLOCK_FINANCE_MONTH) is authoritative; a 403 surfaces as
//   no-permission copy and a 409 as the blocker message. No money values are
//   rendered here, so no finance gating is required on this screen.
// Blast Radius: Finance month locks (write path) via the backend's own routes
//   only. No source-of-truth finance number is computed or mutated client-side.
// Connections:
//   - File: frontend/src/lib/api/useMonthClose.ts -> status/readiness/action hooks.
//   - File: frontend/src/lib/api/types.ts -> FinanceMonthCloseStatus / readiness types.
//   - File: backend/ums_smart_revenue/api/finance_close.py -> the endpoints.
// ============================================================================

// Default to a recent, demo-seedable month per the task brief (matches CommandView).
const DEFAULT_MONTH = "2026-03";

// Months offered in the selector (most recent first). A simple dropdown by
// design — wiring real data is the priority, not month discovery.
const MONTH_OPTIONS = ["2026-03", "2026-02", "2026-01", "2025-12"];

type AccessPermissions = {
  canCloseMonth: boolean;
};

type LockState = {
  busy: boolean;
  error: string | null;
};

function statusTone(status: string | undefined): Severity {
  if (!status) return "blue";
  return status.toUpperCase() === "LOCKED" ? "green" : "amber";
}

function blockerTone(severity: string): Severity {
  return severity.toUpperCase() === "HIGH" ? "red" : "amber";
}

// Render an ISO timestamp without float math; fall back to a dash when absent.
function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// ============================================================================
// Purpose: Translate a lock/unlock failure into clear inline copy. A 409 carries
//   the readiness blocker detail (or an already-locked / wrong-state message);
//   a 403 means the backend denied the permission; anything else reuses the
//   shared describeError contract so the message matches the rest of the shell.
// ============================================================================
function describeActionError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 409) {
      const body = error.body;
      if (typeof body === "object" && body !== null) {
        const detail = (body as { detail?: unknown }).detail;
        if (typeof detail === "string" && detail.trim().length > 0) {
          return detail;
        }
        if (typeof detail === "object" && detail !== null) {
          const lockDetail = detail as FinanceCloseLockErrorDetail;
          const blockerCount = Array.isArray(lockDetail.blockers)
            ? lockDetail.blockers.length
            : 0;
          const base =
            typeof lockDetail.message === "string"
              ? lockDetail.message
              : "Finance month cannot be locked in its current state.";
          return blockerCount > 0
            ? `${base} (${blockerCount} blocker${blockerCount === 1 ? "" : "s"})`
            : base;
        }
      }
      return "Finance month cannot change state right now.";
    }
    if (error.status === 403) {
      return "Your role cannot lock or unlock this finance month.";
    }
    const { detail } = describeError(error);
    return detail;
  }
  if (error instanceof Error) return error.message;
  return "Could not reach the finance-close service.";
}

export default function CloseView({
  permissions,
}: {
  permissions: AccessPermissions;
}) {
  const { canCloseMonth } = permissions;
  const [month, setMonth] = useState<string>(DEFAULT_MONTH);
  const [lockState, setLockState] = useState<LockState>({
    busy: false,
    error: null,
  });

  const {
    data: status,
    loading: statusLoading,
    error: statusError,
    reload: reloadStatus,
  } = useMonthClose({ month });
  const {
    data: readiness,
    loading: readinessLoading,
    error: readinessError,
    reload: reloadReadiness,
  } = useMonthCloseReadiness({ month });
  const actions = useMonthCloseActions({ month });

  const isLocked = status?.status?.toUpperCase() === "LOCKED";

  // ==========================================================================
  // Purpose: Confirm + reason prompt + POST for a lock/unlock action. On
  //   success it refetches both status and readiness so the UI reflects the
  //   new state; on failure it surfaces the typed 409/403/other message inline
  //   and leaves the existing data untouched.
  // ==========================================================================
  const runAction = useCallback(
    async (kind: "lock" | "unlock") => {
      const verb = kind === "lock" ? "Lock" : "Unlock";
      const reason = window.prompt(`${verb} ${month}: reason (required)`);
      if (reason === null) return; // user cancelled the prompt
      const trimmed = reason.trim();
      if (!trimmed) {
        setLockState({ busy: false, error: "A reason is required." });
        return;
      }
      if (!window.confirm(`${verb} finance month ${month}?`)) return;
      setLockState({ busy: true, error: null });
      try {
        await actions[kind](trimmed);
        setLockState({ busy: false, error: null });
        reloadStatus();
        reloadReadiness();
      } catch (caught) {
        setLockState({ busy: false, error: describeActionError(caught) });
      }
    },
    [actions, month, reloadReadiness, reloadStatus],
  );

  return (
    <section className="view-page" aria-labelledby="closeViewTitle">
      <CloseStatusSummary
        status={status}
        loading={statusLoading}
        error={statusError}
        readiness={readiness}
      />

      <div className="view-grid">
        <section className="panel">
          <div className="panel-header">
            <div className="panel-title">
              <strong id="closeViewTitle">Month Close Workbench</strong>
              <span>Deterministic workflow from report ingestion to locked exports</span>
            </div>
            <Badge tone={canCloseMonth ? "amber" : "red"}>
              {canCloseMonth ? "Finance Admin" : "Restricted"}
            </Badge>
          </div>

          <div className="control-row" aria-label="Month close filters" style={{ marginBottom: 13 }}>
            <select
              className="control"
              aria-label="Month"
              value={month}
              onChange={(e) => {
                setMonth(e.target.value);
                setLockState({ busy: false, error: null });
              }}
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
              aria-label="Refresh month close"
              title="Refresh month close"
              onClick={() => {
                reloadStatus();
                reloadReadiness();
              }}
            >
              ↻
            </button>
          </div>

          <ReadinessChecklist
            readiness={readiness}
            loading={readinessLoading}
            error={readinessError}
          />
        </section>

        <aside className="view-stack">
          <section className="panel">
            <div className="panel-header">
              <div className="panel-title">
                <strong>Lock Controls</strong>
                <span>The backend rejects a lock until blockers are cleared</span>
              </div>
              <Badge tone={statusTone(status?.status)}>
                {status?.status ?? "—"}
              </Badge>
            </div>
            <div className="detail-grid">
              <div className="detail-cell">
                <span>Locked by</span>
                <strong>{status?.locked_by ?? "—"}</strong>
              </div>
              <div className="detail-cell">
                <span>Locked at</span>
                <strong>{formatTimestamp(status?.locked_at)}</strong>
              </div>
              <div className="detail-cell">
                <span>Unlocked by</span>
                <strong>{status?.unlocked_by ?? "—"}</strong>
              </div>
              <div className="detail-cell">
                <span>Unlocked at</span>
                <strong>{formatTimestamp(status?.unlocked_at)}</strong>
              </div>
            </div>
            {lockState.error ? (
              <div className="permission-band" role="alert" style={{ marginTop: 8 }}>
                <Dot tone="red" />
                <span>
                  <strong>Action failed</strong>
                  <span>{lockState.error}</span>
                </span>
                <Badge tone="red">Blocked</Badge>
              </div>
            ) : null}
            <div className="action-row">
              <button
                className="danger-button"
                type="button"
                disabled={!canCloseMonth || lockState.busy || !isLocked}
                onClick={() => runAction("unlock")}
              >
                {lockState.busy ? "Working…" : "Unlock Month"}
              </button>
              <button
                className="primary-button"
                type="button"
                disabled={!canCloseMonth || lockState.busy || isLocked}
                onClick={() => runAction("lock")}
              >
                {lockState.busy ? "Working…" : "Lock Month"}
              </button>
            </div>
          </section>

          <section className="panel">
            <div className="panel-header">
              <div className="panel-title">
                <strong>Reconciliation Equation</strong>
                <span>Sample data — not yet wired to the API</span>
              </div>
              <Badge tone="amber">Mock</Badge>
            </div>
            <div className="formula" style={{ margin: 13 }}>
              gross_reported - official_tax - payment_fees - allocation_gap + approved_overrides = locked_net
            </div>
            <div className="issue-list" role="list">
              {RECON_NOTES.map((n) => (
                <ItemRow
                  key={n.title}
                  tone={n.tone}
                  title={n.title}
                  sub={n.sub}
                  trailing={<Badge tone={n.badge.tone}>{n.badge.text}</Badge>}
                />
              ))}
            </div>
          </section>
        </aside>
      </div>
    </section>
  );
}

function CloseStatusSummary({
  status,
  loading,
  error,
  readiness,
}: {
  status: FinanceMonthCloseStatus | null;
  loading: boolean;
  error: ApiError | Error | null;
  readiness: FinanceCloseReadinessResponse | null;
}) {
  if (error) {
    const { title, detail } = describeError(error);
    return (
      <div className="view-summary" aria-label="Month close summary" role="alert">
        <article className="summary-tile">
          <span>{title}</span>
          <strong>—</strong>
          <small>{detail}</small>
        </article>
      </div>
    );
  }

  if (loading && !status) {
    return (
      <div className="view-summary" aria-label="Month close summary" aria-busy="true">
        <article className="summary-tile">
          <span>Status</span>
          <strong>…</strong>
          <small>Loading month close</small>
        </article>
      </div>
    );
  }

  const blockerCount = readiness?.blockers.length ?? 0;
  const readyValue = readiness
    ? readiness.ready
      ? "Ready"
      : "Blocked"
    : "—";

  return (
    <div className="view-summary" aria-label="Month close summary">
      <article className="summary-tile">
        <span>Month</span>
        <strong>{status?.month ?? "—"}</strong>
        <small>Finance close control</small>
      </article>
      <article className="summary-tile">
        <span>Status</span>
        <strong>{status?.status ?? "—"}</strong>
        <small>{status?.status === "LOCKED" ? "Exports allowed" : "Open for edits"}</small>
      </article>
      <article className="summary-tile">
        <span>Readiness</span>
        <strong>{readyValue}</strong>
        <small>
          {blockerCount > 0
            ? `${blockerCount} blocker${blockerCount === 1 ? "" : "s"}`
            : "No blockers"}
        </small>
      </article>
      <article className="summary-tile">
        <span>Allocation method</span>
        <strong>{status?.allocation_method ?? "Not set"}</strong>
        <small>Recorded on this close row</small>
      </article>
    </div>
  );
}

function ReadinessChecklist({
  readiness,
  loading,
  error,
}: {
  readiness: FinanceCloseReadinessResponse | null;
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

  if (loading && !readiness) {
    return (
      <div className="table-wrap" aria-busy="true">
        <div style={{ padding: 16 }} className="item-sub">
          Loading readiness…
        </div>
      </div>
    );
  }

  if (!readiness) {
    return (
      <div className="table-wrap">
        <div style={{ padding: 16 }} className="item-sub">
          No readiness data for this month.
        </div>
      </div>
    );
  }

  if (readiness.ready) {
    return (
      <div className="permission-band">
        <Dot tone="green" />
        <span>
          <strong>Month is ready to lock</strong>
          <span>No unresolved close blockers for {readiness.month}.</span>
        </span>
        <Badge tone="green">Ready</Badge>
      </div>
    );
  }

  return (
    <div className="issue-list" role="list" aria-label="Close blockers">
      {readiness.blockers.map((blocker: FinanceCloseBlocker) => (
        <ItemRow
          key={blocker.blocker_type}
          tone={blockerTone(blocker.severity)}
          title={blocker.message}
          sub={`${blocker.blocker_type} · ${blocker.severity}`}
          trailing={<Badge tone={blockerTone(blocker.severity)}>{`${blocker.count}`}</Badge>}
        />
      ))}
    </div>
  );
}

export { describeActionError };
