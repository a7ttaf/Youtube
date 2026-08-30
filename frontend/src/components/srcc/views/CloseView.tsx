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
  canUnlockMonth: boolean;
};

type LockState = {
  busy: boolean;
  error: string | null;
};

type LockAction = "lock" | "unlock";

/** Map a close status to a badge tone: green when LOCKED, amber when open, blue when unknown. */
const statusTone = (status: string | undefined): Severity => {
  if (!status) return "blue";
  return status.toUpperCase() === "LOCKED" ? "green" : "amber";
};

/** Map a blocker severity to a badge tone: red for HIGH, amber otherwise. */
const blockerTone = (severity: string): Severity => {
  return severity.toUpperCase() === "HIGH" ? "red" : "amber";
};

/** Narrow an unknown to a non-null object so its fields can be probed. */
const isNonNullObject = (value: unknown): value is Record<string, unknown> => {
  return typeof value === "object" && value !== null;
};

/** Format the structured 409 lock detail: its message plus a blocker-count suffix. */
const describeLockBlockerDetail = (lockDetail: FinanceCloseLockErrorDetail): string => {
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
};

// ============================================================================
// Purpose: Pull the operator-facing copy out of a finance-close 409 body. A
//   non-empty plain-string detail wins; a structured lock detail formats via its
//   blocker count; anything else falls back to generic wrong-state copy.
// Database/ORM: None (frontend) — reads an already-parsed ApiError.body.
// Standards: Total and defensive. `body` is typed `unknown` because a 409 body
//   is not guaranteed to match the schema, so every branch is shape-checked
//   before use and the final return is the fail-safe. No throw, so an unexpected
//   payload degrades to generic copy instead of blanking the panel.
// Blast Radius: Finance close — this is the only place a rejected lock/unlock
//   becomes words the operator reads. Getting it wrong hides a real readiness
//   blocker behind generic text, which is what makes the month look
//   inexplicably stuck. It reports the refusal; it never suppresses one.
// Connections: finance_close.py lock/unlock (raise the 409 this reads),
//   describeLockBlockerDetail (formats the structured branch).
//   - File: backend/ums_smart_revenue/api/finance_close.py:142
//     lock_finance_month / :179 unlock_finance_month -> raise the 409 whose
//     body shape this reads.
//   - File: frontend/src/components/srcc/views/CloseView.tsx ->
//     describeLockBlockerDetail formats the structured branch.
// ============================================================================
const describeConflictBody = (body: unknown): string => {
  const detail = isNonNullObject(body) ? body.detail : null;
  if (typeof detail === "string" && detail.trim().length > 0) {
    return detail;
  }
  if (isNonNullObject(detail)) {
    return describeLockBlockerDetail(detail as FinanceCloseLockErrorDetail);
  }
  return "Finance month cannot change state right now.";
};

// ============================================================================
// Purpose: Translate a lock/unlock failure into clear inline copy. A 409 carries
//   the readiness blocker detail (or an already-locked / wrong-state message);
//   a 403 means the backend denied the permission; anything else reuses the
//   shared describeError contract so the message matches the rest of the shell.
// Database/ORM: None (frontend) — reads an already-parsed ApiError; issues no
//   request and retries nothing.
// Standards: Total over the three branches and side-effect free — every status
//   yields copy, so a lock failure can never render blank. The 403 string is
//   fixed UI copy; the 409 text comes from the backend body, and the default
//   arm defers to the shared describeError so one screen cannot invent its own
//   error vocabulary.
// Blast Radius: Finance close — this is the only place a refused lock or unlock
//   becomes words the operator reads. It REPORTS a refusal and must never
//   soften or swallow one: collapsing the 409 arm would hide a real readiness
//   blocker, and collapsing the 403 arm would present an authorization denial
//   as a transient error and invite a pointless retry. It changes no state and
//   grants nothing.
// Connections: finance_close.py lock/unlock (raise the 409/403 this maps),
//   describeConflictBody (409 body shapes), CommandView describeError (default).
//   - File: backend/ums_smart_revenue/api/finance_close.py:142
//     lock_finance_month / :179 unlock_finance_month -> the statuses mapped here.
//   - File: frontend/src/components/srcc/views/CloseView.tsx ->
//     describeConflictBody handles the 409 body shapes.
//   - File: frontend/src/components/srcc/views/CommandView.tsx -> describeError
//     supplies the shared fallback contract.
// ============================================================================
/** Map a typed finance-close ApiError (409 conflict / 403 denial / other) to inline copy. */
const describeApiActionError = (error: ApiError): string => {
  if (error.status === 409) {
    return describeConflictBody(error.body);
  }
  if (error.status === 403) {
    return "Your role cannot lock or unlock this finance month.";
  }
  const { detail } = describeError(error);
  return detail;
};

/**
 * Translate any lock/unlock failure into inline copy: typed ApiErrors go through
 * the 409/403 mapping above, plain Errors surface their message, and anything
 * else falls back to the generic network-failure copy.
 */
const describeActionError = (error: unknown): string => {
  if (error instanceof ApiError) return describeApiActionError(error);
  if (error instanceof Error) return error.message;
  return "Could not reach the finance-close service.";
};

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

/** The pre-arm verb label for one lock/unlock action button. */
const lockActionIdleLabel = (kind: LockAction): string =>
  kind === "unlock" ? "Unlock Month" : "Lock Month";

/** The armed confirm label for one action button, naming the month it executes against. */
const lockActionConfirmLabel = (kind: LockAction, month: string): string =>
  kind === "unlock" ? `Confirm unlock ${month}` : `Confirm lock ${month}`;

/**
 * Compute the label for one lock/unlock action button: "Working…" while that
 * action is in flight, "Confirm lock/unlock {month}" once armed, otherwise the
 * default verb label. Behaviour-identical to the previous inline ternary chain.
 */
const lockActionLabel = (
  kind: LockAction,
  month: string,
  busy: boolean,
  isArmed: boolean,
): string => {
  if (busy && isArmed) return "Working…";
  if (isArmed) return lockActionConfirmLabel(kind, month);
  return lockActionIdleLabel(kind);
};

/** Which permission gates one action: unlock needs canUnlockMonth, lock canCloseMonth. */
const lockActionPermitted = (
  kind: LockAction,
  canCloseMonth: boolean,
  canUnlockMonth: boolean,
): boolean => (kind === "unlock" ? canUnlockMonth : canCloseMonth);

/** The per-action month-state guard: unlock requires a LOCKED month, lock an OPEN one. */
const lockActionStateBlocked = (kind: LockAction, isLocked: boolean): boolean =>
  kind === "unlock" ? !isLocked : isLocked;

// ============================================================================
// Purpose: Derive whether a lock/unlock action button is disabled. Both actions
//   share the no-permission / busy / empty-reason guards; lock additionally
//   requires an OPEN month and unlock a LOCKED one.
// Database/ORM: None (frontend) — a pure predicate over resolved props.
// Standards: No client-side authorization is invented. `canCloseMonth` and
//   `canUnlockMonth` are capabilities the backend already derived; this gates
//   the affordance only, and the backend's LOCK_FINANCE_MONTH /
//   UNLOCK_FINANCE_MONTH check plus its readiness recheck stay authoritative.
//   Side-effect free and total.
// Blast Radius: Finance close — locking a month makes its facts immutable and
//   unlocking reopens them, so both are audited writes. This predicate is what
//   keeps a blank audit reason from reaching the endpoint and what stops the
//   button firing the wrong transition for the month's current state; the
//   `busy` guard stops a double-click filing a duplicate audited action. It is
//   a usability gate, not the authorization or readiness boundary.
// Connections: finance_close.py lock/unlock (authoritative gate),
//   lockActionPermitted + lockActionStateBlocked (the two local halves).
//   - File: backend/ums_smart_revenue/api/finance_close.py:142
//     lock_finance_month / :179 unlock_finance_month -> the authoritative
//     permission gate, readiness recheck, and audited state change.
//   - File: frontend/src/components/srcc/views/CloseView.tsx ->
//     lockActionPermitted and lockActionStateBlocked supply the two halves.
// ============================================================================
const lockActionDisabled = (
  kind: LockAction,
  canCloseMonth: boolean,
  canUnlockMonth: boolean,
  busy: boolean,
  isLocked: boolean,
  reasonEmpty: boolean,
): boolean => {
  const canAct = lockActionPermitted(kind, canCloseMonth, canUnlockMonth);
  return !canAct || busy || reasonEmpty || lockActionStateBlocked(kind, isLocked);
};

/**
 * One arm/confirm lock or unlock button. Owns its own label + disabled derivation
 * so the parent panel stays low-complexity; calls back with its action kind on click.
 */
const LockActionButton = ({
  kind,
  month,
  canCloseMonth,
  canUnlockMonth,
  isLocked,
  busy,
  reasonEmpty,
  isArmed,
  onActionClick,
}: {
  kind: LockAction;
  month: string;
  canCloseMonth: boolean;
  canUnlockMonth: boolean;
  isLocked: boolean;
  busy: boolean;
  reasonEmpty: boolean;
  isArmed: boolean;
  onActionClick: (kind: LockAction) => void;
}) => {
  const className = kind === "unlock" ? "danger-button" : "primary-button";
  return (
    <button
      className={className}
      type="button"
      disabled={lockActionDisabled(kind, canCloseMonth, canUnlockMonth, busy, isLocked, reasonEmpty)}
      onClick={() => onActionClick(kind)}
    >
      {lockActionLabel(kind, month, busy, isArmed)}
    </button>
  );
};

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

/** The audited-reason input is editable only for a role that can lock or unlock,
 * and never while an action is in flight. */
const lockReasonDisabled = (
  canCloseMonth: boolean,
  canUnlockMonth: boolean,
  busy: boolean,
): boolean => (!canCloseMonth && !canUnlockMonth) || busy;

// FIX (PR #211 review): hoisted above LockControlsPanel, whose badge renders
// the same absent-row fallback the summary tiles use — defining it lower made
// the analyzer flag a use-before-define even though the TDZ is unreachable at
// render time.
/** The "no close row yet" status label — an untouched month is simply OPEN. */
const NO_CLOSE_RECORD_STATUS = "OPEN";
const NO_CLOSE_RECORD_NOTE = "No close record yet";

/** The Status tile's value: the close row's status, or OPEN when no row exists yet. */
const closeStatusValue = (status: FinanceMonthCloseStatus | null): string =>
  status?.status ?? NO_CLOSE_RECORD_STATUS;

/** The Status tile's footnote: LOCKED allows exports; no row means not started. */
const closeStatusNote = (status: FinanceMonthCloseStatus | null): string => {
  if (!status) {
    return NO_CLOSE_RECORD_NOTE;
  }
  return status.status === "LOCKED" ? "Exports allowed" : "Open for edits";
};

/**
 * The Lock Controls badge value: the close status with the absent-row OPEN
 * fallback — but ONLY once the status read has SETTLED without an error. A
 * still-loading or failed read is UNKNOWN, not open: the badge then renders an
 * em dash so it never asserts OPEN for a month whose state the panel does not
 * actually have (PR #211 review).
 */
const lockStatusBadgeValue = (
  status: FinanceMonthCloseStatus | null,
  statusKnown: boolean,
): string | undefined => (statusKnown ? closeStatusValue(status) : undefined);

/**
 * Lock Controls panel: status badge, lock/unlock actor + timestamp grid, the
 * audited reason input, and the two-step arm/confirm lock & unlock buttons. The
 * parent owns all state; this component is presentational and calls back on intent.
 */
const LockControlsPanel = ({
  status,
  statusKnown,
  month,
  canCloseMonth,
  canUnlockMonth,
  isLocked,
  lockState,
  reason,
  armed,
  onReasonChange,
  onActionClick,
  onCancel,
}: {
  status: FinanceMonthCloseStatus | null;
  statusKnown: boolean;
  month: string;
  canCloseMonth: boolean;
  canUnlockMonth: boolean;
  isLocked: boolean;
  lockState: LockState;
  reason: string;
  armed: LockAction | null;
  onReasonChange: (value: string) => void;
  onActionClick: (kind: LockAction) => void;
  onCancel: () => void;
}) => {
  const reasonEmpty = reason.trim().length === 0;
  const badgeStatus = lockStatusBadgeValue(status, statusKnown);

  return (
    <section className="panel">
      <div className="panel-header">
        <div className="panel-title">
          <strong>Lock Controls</strong>
          <span>The backend rejects a lock until blockers are cleared</span>
        </div>
        {/* FIX (PR #211 review): the same absent-row fallback as the summary
            tiles — a mapped 404 (no close row yet) reads OPEN here too, never
            "—", or the two status indicators on one screen disagree. But the
            fallback applies ONLY to a SETTLED, error-free read: while the
            status request is loading or has failed, the month's state is
            unknown and the badge says so instead of asserting OPEN. */}
        <Badge tone={statusTone(badgeStatus)}>{badgeStatus ?? "—"}</Badge>
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
          disabled={lockReasonDisabled(canCloseMonth, canUnlockMonth, lockState.busy)}
          onChange={(e) => onReasonChange(e.target.value)}
        />
      </div>
      {lockState.error ? <ActionFailedBand message={lockState.error} /> : null}
      <div className="action-row">
        <LockActionButton
          kind="unlock"
          month={month}
          canCloseMonth={canCloseMonth}
          canUnlockMonth={canUnlockMonth}
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
          canUnlockMonth={canUnlockMonth}
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
};

/**
 * Static Reconciliation Equation reference panel. Still on mock data and labelled
 * as such — not part of the close API.
 */
const ReconciliationPanel = () => {
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
};

// A month with no finance_month_close row is honestly OPEN: nothing has locked
// it, and the backend only creates the row once a finance write touches the
// month. This is not a UI invention — the backend itself already resolves a
// missing close row to "OPEN" (backend/ums_smart_revenue/api/revenue.py:1742
// and :3060, `close.status if close else "OPEN"`), which is the same verdict
// this screen now states. useMonthClose maps that GET's 404 to
// (data=null, error=null), so these two strings are what the summary shows for
// a month nobody has closed yet — the state the rolling CURRENT-month default
// lands on.
/** The Readiness tile value: Ready/Blocked once readiness has loaded, an em dash before. */
const readinessTileValue = (readiness: FinanceCloseReadinessResponse | null): string => {
  if (!readiness) return "—";
  return readiness.ready ? "Ready" : "Blocked";
};

/** The Readiness tile footnote: the blocker count, pluralised, or "No blockers". */
const blockerCountLabel = (blockerCount: number): string =>
  blockerCount > 0
    ? `${blockerCount} blocker${blockerCount === 1 ? "" : "s"}`
    : "No blockers";

/**
 * The loaded month/status/readiness/allocation summary tiles. Reached only once
 * the status read has SETTLED without an error, so a null `status` here means
 * the month simply has no close record yet (the mapped 404) — it falls back to
 * the selected `month` and the honest not-started copy rather than em dashes.
 */
const CloseSummaryTiles = ({
  month,
  status,
  readiness,
}: {
  month: string;
  status: FinanceMonthCloseStatus | null;
  readiness: FinanceCloseReadinessResponse | null;
}) => {
  const blockerCount = readiness?.blockers.length ?? 0;
  return (
    <div className="view-summary" aria-label="Month close summary">
      <article className="summary-tile">
        <span>Month</span>
        <strong>{status?.month ?? month}</strong>
        <small>Finance close control</small>
      </article>
      <article className="summary-tile">
        <span>Status</span>
        <strong>{closeStatusValue(status)}</strong>
        <small>{closeStatusNote(status)}</small>
      </article>
      <article className="summary-tile">
        <span>Readiness</span>
        <strong>{readinessTileValue(readiness)}</strong>
        <small>{blockerCountLabel(blockerCount)}</small>
      </article>
      <article className="summary-tile">
        <span>Allocation method</span>
        <strong>{status?.allocation_method ?? "Not set"}</strong>
        <small>Recorded on this close row</small>
      </article>
    </div>
  );
};

/**
 * Top summary tiles for the close screen: month, status, readiness, and allocation
 * method, with explicit error and initial-loading states mirroring CommandView.
 *
 * Branch order is load-bearing. `error` still wins, so a 403/5xx/network failure
 * keeps its role="alert" tile exactly as before; useMonthClose has already
 * turned the "no close row yet" 404 into (status=null, error=null), which falls
 * through to the tiles and their not-started copy instead of an error banner.
 */
const CloseStatusSummary = ({
  month,
  status,
  loading,
  error,
  readiness,
}: {
  month: string;
  status: FinanceMonthCloseStatus | null;
  loading: boolean;
  error: ApiError | Error | null;
  readiness: FinanceCloseReadinessResponse | null;
}) => {
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

  return <CloseSummaryTiles month={month} status={status} readiness={readiness} />;
};

/** True while the readiness fetch is in flight with nothing loaded yet. */
const isInitialReadinessLoad = (
  loading: boolean,
  readiness: FinanceCloseReadinessResponse | null,
): boolean => loading && !readiness;

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

  if (isInitialReadinessLoad(loading, readiness)) {
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
};

/**
 * Left workbench panel: title, the month selector + refresh control, and the
 * readiness checklist. Extracted so the parent CloseView tree stays shallow.
 */
const MonthCloseWorkbench = ({
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
}) => {
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
};

/**
 * The real-data Month-Close screen: status summary, readiness checklist, and the
 * inline reason + arm/confirm lock/unlock workflow wired to the finance-close API.
 */
const CloseView = ({
  permissions,
}: {
  permissions: AccessPermissions;
}) => {
  const { canCloseMonth, canUnlockMonth } = permissions;
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
        month={month}
        status={status}
        loading={statusLoading}
        error={statusError}
        readiness={readiness}
      />

      <div className="view-grid">
        <MonthCloseWorkbench
          month={month}
          canCloseMonth={canCloseMonth || canUnlockMonth}
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
            statusKnown={!statusLoading && statusError === null}
            month={month}
            canCloseMonth={canCloseMonth}
            canUnlockMonth={canUnlockMonth}
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
};

export default CloseView;

export { describeActionError };
