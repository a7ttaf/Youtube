import { useCallback, useRef, useState } from "react";

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
import {
  Badge,
  DEFAULT_MONTH,
  Dot,
  formatTimestamp,
  ItemRow,
  MONTH_OPTIONS,
} from "../shared";
import { describeError } from "./CommandView";

// The locale-format options the close screen renders its lock/unlock timestamps
// with. Passed to the shared formatTimestamp so the rendered strings are
// identical to the close screen's previous local helper.
const CLOSE_TIMESTAMP_OPTIONS: Intl.DateTimeFormatOptions = {
  year: "numeric",
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
};

/** Format a lock/unlock ISO timestamp for the close screen (en-US, short date + time). */
const formatCloseTimestamp = (value: string | null | undefined): string =>
  formatTimestamp(value, CLOSE_TIMESTAMP_OPTIONS);

// ============================================================================
// Purpose: The REAL-data Month-Close screen, extracted from AppShell. Renders a
//   finance month's close status (OPEN/LOCKED + lock/unlock actor & timestamps)
//   from GET /finance-close/{month} and its readiness checklist (blockers +
//   ready flag) from GET /finance-close/{month}/readiness, with explicit
//   loading / error / 403 states mirroring CommandView. Wires Lock and Unlock
//   actions (POST {reason}) behind an inline audited-reason input and a two-step
//   arm/confirm latch (first click arms, second click executes), mapping a
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

type AccessPermissions = {
  canCloseMonth: boolean;
};

// ============================================================================
// Purpose: Translate a lock/unlock failure into clear inline copy. A 409 carries
//   the readiness blocker detail (or an already-locked / wrong-state message);
//   a 403 means the backend denied the permission; anything else reuses the
//   shared describeError contract so the message matches the rest of the shell.
// ============================================================================
const statusHandlers: Record<number, (error: ApiError) => string> = {
  409: error => {
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
  },
  403: () => "You do not have permission to perform this action."
};

const describeActionError = (error: unknown): string => {
  if (error instanceof ApiError) {
    const handler = statusHandlers[error.status];
    if (handler) {
      return handler(error);
    }
  }
  return describeError(error);
};
    const describeError = (error: unknown) => {
      return "Your role cannot lock or unlock this finance month.";
    }
    const { detail } = describeError(error);
    return detail;
  }
  if (error instanceof Error) return error.message;
  return "Could not reach the finance-close service.";
}

/**
 * The real-data Month-Close screen: status summary, readiness checklist, and the
 * inline reason + arm/confirm lock/unlock workflow wired to the finance-close API.
 */
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
  // Audited free-text reason and the two-step arm latch for the pending action.
  const [reason, setReason] = useState<string>("");
  const [armed, setArmed] = useState<LockAction | null>(null);
  // Synchronous in-flight latch so a same-tick double-click on the armed confirm
  // button cannot fire two lock/unlock POSTs (the state `busy` guard cannot see
  // the second click in time — both read the same stale busy=false render).
  const runInFlightRef = useRef(false);

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
  // Purpose: POST a lock/unlock action using the trimmed, audited reason already
  //   captured in component state (no native prompt/confirm — the arm/confirm UI
  //   gates the call). On success it clears the reason + armed latch and refetches
  //   both status and readiness so the UI reflects the new state; on failure it
  //   surfaces the typed 409/403/other message inline and leaves the data and the
  //   armed action untouched so the operator can retry.
  // Dedupe: a synchronous runInFlightRef latch drops a same-tick double-click on
  //   the armed Confirm button BEFORE a second POST fires. Without it both clicks
  //   read the same stale busy=false render and enter runAction, so the first
  //   POST succeeds and the second 409s — surfacing a misleading "Action failed"
  //   banner for what was really a duplicate click. The ref clears in finally so
  //   a later, non-overlapping action proceeds.
  // ==========================================================================
  const runAction = useCallback(
    async (kind: LockAction) => {
      const trimmed = reason.trim();
      if (!trimmed) {
        setLockState({ busy: false, error: "A reason is required." });
        return;
      }
      // FIX: drop a same-tick duplicate confirm click before the POST fires; the
      // state `busy` guard cannot catch it (both clicks read the same stale
      // busy=false render), so without this the second click POSTs and 409s.
      if (runInFlightRef.current) return;
      runInFlightRef.current = true;
      setLockState({ busy: true, error: null });
      try {
        await actions[kind](trimmed);
        setLockState({ busy: false, error: null });
        setReason("");
        setArmed(null);
        reloadStatus();
        reloadReadiness();
      } catch (caught) {
        setLockState({ busy: false, error: describeActionError(caught) });
      } finally {
        runInFlightRef.current = false;
      }
    },
    [actions, reason, reloadReadiness, reloadStatus],
  );

  // ==========================================================================
  // Purpose: Drive the two-step lock/unlock latch. The first click arms the
  //   action (revealing the Confirm/Cancel affordances); a second click on the
  //   same action executes it with the captured reason. Switching months or
  //   clicking Cancel disarms via resetWorkflow.
  // ==========================================================================
  const handleActionClick = useCallback(
    (kind: LockAction) => {
      if (armed === kind) {
        // runAction captures every failure into lockState, so this never rejects;
        // the catch is defensive belt-and-braces for an unexpected throw.
        runAction(kind).catch(() => {
          setLockState({
            busy: false,
            error: "Could not reach the finance-close service.",
          });
        });
        return;
      }
      setArmed(kind);
      setLockState((prev) => ({ busy: false, error: prev.error }));
    },
    [armed, runAction],
  );

  // Clear the reason, armed latch, and any prior error (used by Cancel + month change).
  const resetWorkflow = useCallback(() => {
    setReason("");
    setArmed(null);
    setLockState({ busy: false, error: null });
  }, []);

  return (
    <section className="view-page" aria-labelledby="closeViewTitle">
      <CloseStatusSummary
        status={status}
        loading={statusLoading}
        error={statusError}
        readiness={readiness}
      />

      <div className="view-grid">
        <MonthCloseWorkbench
          month={month}
          canCloseMonth={canCloseMonth}
          readiness={readiness}
          readinessLoading={readinessLoading}
          readinessError={readinessError}
          onMonthChange={(value) => {
            setMonth(value);
            resetWorkflow();
          }}
          onRefresh={() => {
            reloadStatus();
            reloadReadiness();
          }}
        />

        <aside className="view-stack">
          <LockControlsPanel
            status={status}
            month={month}
            canCloseMonth={canCloseMonth}
            isLocked={isLocked}
            lockState={lockState}
            reason={reason}
            armed={armed}
            onReasonChange={setReason}
            onActionClick={handleActionClick}
            onCancel={resetWorkflow}
          />
          <ReconciliationPanel />
        </aside>
      </div>
    </section>
  );
}

/**
 * Left workbench panel: title, the month selector + refresh control, and the
 * readiness checklist. Extracted so the parent CloseView tree stays shallow.
 */
function MonthCloseWorkbench({
  month,
  canCloseMonth,
  readiness,
  readinessLoading,
  readinessError,
  onMonthChange,
  onRefresh,
}: {
  month: string;
  canCloseMonth: boolean;
  readiness: FinanceCloseReadinessResponse | null;
  readinessLoading: boolean;
  readinessError: ApiError | Error | null;
  onMonthChange: (value: string) => void;
  onRefresh: () => void;
}) {
  return (
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
          onChange={(e) => onMonthChange(e.target.value)}
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
          onClick={onRefresh}
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
  );
}

/**
 * Inline "Action failed" alert band that surfaces a typed lock/unlock error.
 * Extracted so the Lock Controls tree stays shallow (JSX nesting).
 */
const ActionFailedBand = ({ message }: { message: string }) => {
  return (
    <div className="permission-band" role="alert" style={{ marginTop: 8 }}>
      <Dot tone="red" />
      <span>
        <strong>Action failed</strong>
        <span>{message}</span>
      </span>
      <Badge tone="red">Blocked</Badge>
    </div>
  );
};

 /**
  * Compute the label for one lock/unlock action button: "Working…" while that
  * action is in flight, "Confirm lock/unlock {month}" once armed, otherwise the
  * default verb label. Behaviour-identical to the previous inline ternary chain.
  */
 function lockActionLabel(
   kind: LockAction,
   month: string,
   busy: boolean,
   isArmed: boolean,
 ): string {
   if (busy && isArmed) return "Working…";
   const confirmMap: Record<LockAction, string> = {
     unlock: `Confirm unlock ${month}`,
     lock: `Confirm lock ${month}`,
   };
   const defaultMap: Record<LockAction, string> = {
     unlock: "Unlock Month",
     lock: "Lock Month",
   };
   if (isArmed) return confirmMap[kind];
   return defaultMap[kind];
 }

 /**
  * Derive whether a lock/unlock action button is disabled. Both actions share the
  * no-permission / busy / empty-reason guards; lock additionally requires the
  * month to be OPEN and unlock requires it LOCKED.
  */
 const lockActionDisabled = (
   kind: LockAction,
   canCloseMonth: boolean,
   busy: boolean,
   isLocked: boolean,
   reasonEmpty: boolean,
 ): boolean => {
   const shared = !canCloseMonth || busy || reasonEmpty;
   const disabledMap: Record<LockAction, boolean> = {
     unlock: shared || !isLocked,
     lock: shared || isLocked,
   };
   return disabledMap[kind];
 };

 /**
  * One arm/confirm lock or unlock button. Owns its own label + disabled derivation
  * so the parent panel stays low-complexity; calls back with its action kind on click.
  */
 export function LockActionButton({
  month: string;
  canCloseMonth: boolean;
  isLocked: boolean;
  busy: boolean;
  reasonEmpty: boolean;
  isArmed: boolean;
  onActionClick: (kind: LockAction) => void;
}) {
  const className = kind === "unlock" ? "danger-button" : "primary-button";
  return (
    <button
      className={className}
      type="button"
      disabled={lockActionDisabled(kind, canCloseMonth, busy, isLocked, reasonEmpty)}
      onClick={() => onActionClick(kind)}
    >
      {lockActionLabel(kind, month, busy, isArmed)}
    </button>
  );
}

/**
 * The lock/unlock actor + timestamp detail grid. Extracted so the Lock Controls
 * panel stays low-complexity and its JSX tree stays shallow.
 */
const LockDetailGrid = ({ status }: { status: FinanceMonthCloseStatus | null }) => {
  return (
    <div className="detail-grid">
      <div className="detail-cell">
        <span>Locked by</span>
        <strong>{status?.locked_by ?? "—"}</strong>
      </div>
      <div className="detail-cell">
        <span>Locked at</span>
        <strong>{formatCloseTimestamp(status?.locked_at)}</strong>
      </div>
      <div className="detail-cell">
        <span>Unlocked by</span>
        <strong>{status?.unlocked_by ?? "—"}</strong>
      </div>
      <div className="detail-cell">
        <span>Unlocked at</span>
        <strong>{formatCloseTimestamp(status?.unlocked_at)}</strong>
      </div>
    </div>
  );
};

/**
 * Lock Controls panel: status badge, lock/unlock actor + timestamp grid, the
 * audited reason input, and the two-step arm/confirm lock & unlock buttons. The
 * parent owns all state; this component is presentational and calls back on intent.
 */
const LockControlsPanel = ({
  status,
  month,
  canCloseMonth,
  isLocked,
  lockState,
  reason,
  armed,
  onReasonChange,
  onActionClick,
  onCancel,
}: {
  status: FinanceMonthCloseStatus | null;
  month: string;
  canCloseMonth: boolean;
  isLocked: boolean;
  lockState: LockState;
  reason: string;
  armed: LockAction | null;
  onReasonChange: (value: string) => void;
  onActionClick: (kind: LockAction) => void;
  onCancel: () => void;
}) => {
  const reasonEmpty = reason.trim().length === 0;

  return (
    <section className="panel">
      <div className="panel-header">
        <div className="panel-title">
          <strong>Lock Controls</strong>
          <span>The backend rejects a lock until blockers are cleared</span>
        </div>
        <Badge tone={statusTone(status?.status)}>{status?.status ?? "—"}</Badge>
      </div>
      <LockDetailGrid status={status} />
      <div className="control-row" style={{ marginTop: 8 }}>
        <label className="field-label" htmlFor="closeReason">
          Reason (required, audited)
        </label>
        <input
          id="closeReason"
          className="control"
          type="text"
          value={reason}
          placeholder="Why is this month being locked or unlocked?"
          disabled={!canCloseMonth || lockState.busy}
          onChange={(e) => onReasonChange(e.target.value)}
        />
      </div>
      {lockState.error ? <ActionFailedBand message={lockState.error} /> : null}
      <div className="action-row">
        <LockActionButton
          kind="unlock"
          month={month}
          canCloseMonth={canCloseMonth}
          isLocked={isLocked}
          busy={lockState.busy}
          reasonEmpty={reasonEmpty}
          isArmed={armed === "unlock"}
          onActionClick={onActionClick}
        />
        <LockActionButton
          kind="lock"
          month={month}
          canCloseMonth={canCloseMonth}
          isLocked={isLocked}
          busy={lockState.busy}
          reasonEmpty={reasonEmpty}
          isArmed={armed === "lock"}
          onActionClick={onActionClick}
        />
        {armed ? (
          <button
            className="ghost-button"
            type="button"
            disabled={lockState.busy}
            onClick={onCancel}
          >
            Cancel
          </button>
        ) : null}
      </div>
    </section>
  );
}

/**
 * Static Reconciliation Equation reference panel. Still on mock data and labelled
 * as such — not part of the close API.
 */
export function ReconciliationPanel() {
  return (
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
  );
}

export function CloseStatusSummary({
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
      <article className="summary-tile">
        <span>Allocation method</span>
        <strong>{status?.allocation_method ?? "Not set"}</strong>
        <small>Recorded on this close row</small>
      </article>
    </div>
  );
}

/**
 * The close readiness checklist: a ready banner when clear, otherwise one row per
 * blocker, with explicit error, loading, and no-data states.
 */
const ReadinessChecklist = ({
  readiness,
  loading,
  error,
}: {
  readiness: FinanceCloseReadinessResponse | null;
  loading: boolean;
  error: ApiError | Error | null;
}) => {
  const renderMap: { [key: string]: React.ReactNode } = {
    error: (() => {
      const { title, detail } = describeError(error!);
      return (
        <div className="table-wrap" role="alert">
          <div style={{ padding: 16 }}>
            <strong>{title}</strong>
            <p className="item-sub">{detail}</p>
          </div>
        </div>
      );
    })(),
    loading: (
      <div className="table-wrap" aria-busy="true">
        <div style={{ padding: 16 }} className="item-sub">
          Loading readiness…
        </div>
      </div>
    ),
    noData: null,
    ready: (
      <div className="table-wrap">
        <div style={{ padding: 16 }} className="item-sub">
          All checks passed
        </div>
      </div>
    ),
    blockers: (
      <div className="table-wrap">
        {readiness!.blockers.map((blocker) => (
          <div key={blocker.name} className="item-sub">
            <strong>{blocker.name}</strong>
            {blocker.detail && <p>{blocker.detail}</p>}
          </div>
        ))}
      </div>
    ),
  };

  let stateKey: string;
  if (error) stateKey = 'error';
  else if (loading && !readiness) stateKey = 'loading';
  else if (!readiness) stateKey = 'noData';
  else if (readiness.blockers.length === 0) stateKey = 'ready';
  else stateKey = 'blockers';

  return <>{renderMap[stateKey]}</>;
};
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
