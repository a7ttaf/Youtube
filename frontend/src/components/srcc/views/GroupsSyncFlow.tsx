import { type ReactNode, useRef, useState } from "react";

import { ApiError } from "@/lib/api/client";
import type { GroupSyncGroupResult, GroupSyncResult } from "@/lib/api/types";
import { useGroupSyncAction } from "@/lib/api/useGroupSync";
import { ActionStepper } from "../ActionStepper";
import { OutcomeTable, type OutcomeTableRow } from "../OutcomeTable";
import { Badge } from "../shared";

// ============================================================================
// Purpose: The CMS group-sync flow for the Groups view (import/sync arc, PR-A),
//   extracted from GroupsView so the steady-state table and the sync stepper
//   stay small, dumb, and separately testable. Drives a three-step state machine
//   over ActionStepper (["Reason", "Preview", "Applied"]): (1) collect the
//   required, audited reason and fire a READ-ONLY dry-run
//   (useGroupSyncAction dryRun:true); (2) render the per-group diff via
//   OutcomeTable and, only when the plan is conflict-free, allow Apply
//   (dryRun:false); (3) echo the applied counts + reason. Cancel at any step
//   restores the table with NO refetch unless an apply already committed (then
//   the parent reloads). The owning content owner id + the two close paths
//   (onCancel / onDone) are supplied by GroupsView.
// Database/ORM: None (frontend) — POSTs /channels/groups/sync (preview + apply)
//   via useGroupSyncAction; all authorization + the 409 conflict / 503 credential
//   failures stay the backend's authority and surface inline verbatim.
// Standards: No client-side authorization is invented. The apply button is
//   fail-closed against CONFLICT plans (the API 409s them) and against an
//   in-flight apply (synchronous in-flight ref latch -> one request per click
//   burst). The reason obeys the same required + no-NUL audit contract as the
//   table's row panels via the shared isValidAuditReason helper (backend 422s a
//   NUL reason), so a knowably-invalid reason never round-trips. Backend detail
//   is shown verbatim (operator-facing canned copy); a 503 appends a Connectors
//   pointer. Reuses ActionStepper + OutcomeTable + Badge — no parallel visual
//   language.
// Blast Radius: Channel-group membership/ownership/active-state (the sync write
//   path) — but only via the backend's own guarded, audited sync route.
// Connections:
//   - File: frontend/src/lib/api/useGroupSync.ts -> useGroupSyncAction.
//   - File: frontend/src/components/srcc/ActionStepper.tsx -> step shell.
//   - File: frontend/src/components/srcc/OutcomeTable.tsx -> diff table.
//   - File: frontend/src/components/srcc/views/GroupsView.tsx -> renders this +
//       reuses isValidAuditReason for its row confirm panels.
//   - File: backend/ums_smart_revenue/api/channels.py:926 sync_channel_groups.
// ============================================================================

/**
 * The audit-reason contract shared by every group write: required (non-blank
 * after trim) and free of NUL (the backend audit_logs path 422-rejects a NUL in
 * a reason). Extracted here so the sync Reason step and the table's row confirm
 * panels (GroupsView) validate identically instead of each inlining the rule.
 */
export function isValidAuditReason(reason: string): boolean {
  return reason.trim() !== "" && !reason.includes("\u0000");
}

/**
 * Map a sync failure to operator-facing copy — backend detail verbatim, with a
 * Connectors pointer appended on 503 (the sync route 503s when the youtube
 * -analytics credential is missing/expired, which is fixed in the Connectors
 * view). Distinct from GroupsView's describeMutationError by that 503 behavior.
 */
function describeSyncError(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.body as { detail?: unknown } | null;
    const base = String(body?.detail ?? err.message);
    return err.status === 503
      ? `${base} — check the credential in the Connectors view.`
      : base;
  }
  return err instanceof Error ? err.message : String(err);
}

/** Outcome chip tone: green create/reactivate, amber deactivate, red conflict,
 * blue for other changes, muted for UNCHANGED — matching shared Badge tones. */
function outcomeChip(outcome: GroupSyncGroupResult["outcome"]): ReactNode {
  switch (outcome) {
    case "CREATE":
    case "REACTIVATE":
      return <Badge tone="green">{outcome}</Badge>;
    case "DEACTIVATE":
      return <Badge tone="amber">{outcome}</Badge>;
    case "CONFLICT":
      return <Badge tone="red">{outcome}</Badge>;
    case "RENAME":
    case "MEMBERS_CHANGED":
      return <Badge tone="blue">{outcome}</Badge>;
    default:
      return <span className="muted">{outcome}</span>;
  }
}

const activeLabel = (active: boolean): string => (active ? "active" : "archived");

/**
 * Render a group's name/active transitions as "old → new" lines (or a muted dash
 * when nothing changed). name_change/active_change are [from, to] tuples.
 */
function ChangesCell({ group }: { group: GroupSyncGroupResult }) {
  const lines: string[] = [];
  if (group.name_change) {
    lines.push(`${group.name_change[0]} → ${group.name_change[1]}`);
  }
  if (group.active_change) {
    lines.push(
      `${activeLabel(group.active_change[0])} → ${activeLabel(group.active_change[1])}`,
    );
  }
  if (lines.length === 0) return <span className="muted">—</span>;
  return (
    <>
      {lines.map((line, index) => (
        <div key={index}>{line}</div>
      ))}
    </>
  );
}

/** Map one per-group dry-run result to an OutcomeTable row (CONFLICT -> warn). */
function groupOutcomeRow(group: GroupSyncGroupResult): OutcomeTableRow {
  return {
    key: group.cms_group_id,
    tone: group.outcome === "CONFLICT" ? "warn" : undefined,
    cells: [
      group.title ?? group.cms_group_id,
      outcomeChip(group.outcome),
      <ChangesCell group={group} />,
      `+${group.members_added.length} / -${group.members_removed.length}`,
      group.unknown_channel_count,
    ],
  };
}

/** Inline error banner shown in the current step; backend detail verbatim. */
function SyncErrorBanner({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="form-error" role="alert">
      <strong>{title}</strong>
      <span>{detail}</span>
    </div>
  );
}

type ReasonStepProps = {
  reason: string;
  onReasonChange: (value: string) => void;
  onRun: () => void;
  busy: boolean;
  error: string | null;
};

/** Step 1: required, audited reason + a "Run dry-run" trigger. */
function ReasonStep({ reason, onReasonChange, onRun, busy, error }: ReasonStepProps) {
  const canRun = isValidAuditReason(reason) && !busy;
  return (
    <div className="confirm-panel" role="group" aria-label="Sync reason">
      <div className="panel-title">
        <strong>Preview a group sync</strong>
        <span>
          Runs a read-only dry-run first. A reason is required and recorded on the
          audit event.
        </span>
      </div>
      <div className="field-row">
        <label htmlFor="groupSyncReason">Reason (required, audited)</label>
        <input
          id="groupSyncReason"
          value={reason}
          onChange={(event) => onReasonChange(event.target.value)}
          placeholder="Why run this sync"
        />
      </div>
      {error ? <SyncErrorBanner title="Dry-run failed" detail={error} /> : null}
      <div className="action-row">
        <button
          className="primary-button"
          type="button"
          disabled={!canRun}
          onClick={onRun}
        >
          {busy ? "Running…" : "Run dry-run"}
        </button>
      </div>
    </div>
  );
}

const SYNC_COLUMNS = ["Group", "Outcome", "Changes", "Members", "Unknown"];

type PreviewStepProps = {
  result: GroupSyncResult;
  onBack: () => void;
  onApply: () => void;
  busy: boolean;
  error: string | null;
};

/**
 * Step 2: the dry-run diff. Apply is disabled while any row is a CONFLICT (the
 * API 409s a conflicted plan) or while an apply is in flight. A conflict remedy
 * line and an unknown-channels note render below the table when applicable.
 */
function PreviewStep({ result, onBack, onApply, busy, error }: PreviewStepProps) {
  const hasConflict = result.groups.some((group) => group.outcome === "CONFLICT");
  const rows = result.groups.map(groupOutcomeRow);
  return (
    <div className="confirm-panel" role="group" aria-label="Sync preview">
      <div className="panel-title">
        <strong>Review the plan</strong>
        <span>Dry-run for content owner {result.content_owner_id}.</span>
      </div>
      <OutcomeTable
        columns={SYNC_COLUMNS}
        rows={rows}
        emptyLabel="No group changes for this content owner."
      />
      {hasConflict ? (
        <p className="muted" role="note">
          Conflicts block apply: clear the wrong owner stamp (Groups table → Clear
          stamp) or run the sync under the owning content owner.
        </p>
      ) : null}
      {result.unknown_channel_total > 0 ? (
        <p className="muted" role="note">
          {result.unknown_channel_total} channels are in CMS but not in the
          registry — import them first.
        </p>
      ) : null}
      {error ? <SyncErrorBanner title="Apply failed" detail={error} /> : null}
      <div className="action-row">
        <button className="ghost-button" type="button" onClick={onBack}>
          Back
        </button>
        <button
          className="primary-button"
          type="button"
          disabled={hasConflict || busy}
          title="The API refuses conflicted plans (409)"
          onClick={onApply}
        >
          {busy ? "Applying…" : "Apply"}
        </button>
      </div>
    </div>
  );
}

type AppliedStepProps = {
  result: GroupSyncResult;
  reason: string;
  onDone: () => void;
};

/** Step 3: the applied outcome counts (non-zero only) + reason echo. */
function AppliedStep({ result, reason, onDone }: AppliedStepProps) {
  const counts = Object.entries(result.counts).filter(([, value]) => value > 0);
  return (
    <div className="confirm-panel" role="group" aria-label="Sync applied">
      <div className="panel-title">
        <strong>Sync applied</strong>
        <span>Groups for content owner {result.content_owner_id} are updated.</span>
      </div>
      <div>
        {counts.map(([outcome, value]) => (
          <div className="item-sub" key={outcome}>{`${outcome}: ${value}`}</div>
        ))}
      </div>
      <p className="item-sub">{`Reason: ${reason}`}</p>
      <div className="action-row">
        <button className="primary-button" type="button" onClick={onDone}>
          Back to groups
        </button>
      </div>
    </div>
  );
}

export type GroupsSyncFlowProps = {
  /** The content owner whose CMS groups are being synced (from the picker). */
  contentOwnerId: string;
  /** Close the flow WITHOUT reloading the list (no apply committed). */
  onCancel: () => void;
  /** Close the flow AND reload the list (an apply committed, or leaving Applied). */
  onDone: () => void;
};

type SyncStep = "reason" | "preview" | "applied";
const SYNC_STEPS = ["Reason", "Preview", "Applied"];
const STEP_INDEX: Record<SyncStep, number> = { reason: 0, preview: 1, applied: 2 };

/**
 * The three-step CMS group-sync flow. Owns ALL flow state (step, reason, the
 * dry-run preview, the applied result, busy/error); GroupsView owns only whether
 * the flow is open and which content owner it targets. A superseded-safe
 * in-flight ref latch collapses a same-tick double submit so one click burst
 * fires one request.
 */
export function GroupsSyncFlow({ contentOwnerId, onCancel, onDone }: GroupsSyncFlowProps) {
  const runSync = useGroupSyncAction();
  const [step, setStep] = useState<SyncStep>("reason");
  const [reason, setReason] = useState("");
  const [preview, setPreview] = useState<GroupSyncResult | null>(null);
  const [applied, setApplied] = useState<GroupSyncResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlightRef = useRef(false);

  const trimmedReason = reason.trim();

  /** Fire the read-only dry-run; on success advance to Preview, else stay + show. */
  const runDryRun = async () => {
    if (!isValidAuditReason(reason) || busy || inFlightRef.current) return;
    inFlightRef.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await runSync({ contentOwnerId, dryRun: true, reason: trimmedReason });
      setPreview(result);
      setStep("preview");
    } catch (caught) {
      // Stay on Reason; the button re-enables in finally for a retry.
      setError(describeSyncError(caught));
    } finally {
      setBusy(false);
      inFlightRef.current = false;
    }
  };

  /** Commit the plan; refuse when any row conflicts (the API 409s it anyway). */
  const apply = async () => {
    if (busy || inFlightRef.current) return;
    if (preview?.groups.some((group) => group.outcome === "CONFLICT")) return;
    inFlightRef.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await runSync({ contentOwnerId, dryRun: false, reason: trimmedReason });
      setApplied(result);
      setStep("applied");
    } catch (caught) {
      // Stay on Preview; the Apply button re-enables in finally for a retry.
      setError(describeSyncError(caught));
    } finally {
      setBusy(false);
      inFlightRef.current = false;
    }
  };

  const backToReason = () => {
    setError(null);
    setStep("reason");
  };

  // Cancel restores the table WITHOUT a refetch — unless an apply already
  // committed, in which case the list changed and the parent must reload.
  const handleCancel = () => {
    if (applied) onDone();
    else onCancel();
  };

  return (
    <ActionStepper steps={SYNC_STEPS} activeIndex={STEP_INDEX[step]} onCancel={handleCancel}>
      {step === "reason" ? (
        <ReasonStep
          reason={reason}
          onReasonChange={setReason}
          onRun={runDryRun}
          busy={busy}
          error={error}
        />
      ) : null}
      {step === "preview" && preview ? (
        <PreviewStep
          result={preview}
          onBack={backToReason}
          onApply={apply}
          busy={busy}
          error={error}
        />
      ) : null}
      {step === "applied" && applied ? (
        <AppliedStep result={applied} reason={trimmedReason} onDone={onDone} />
      ) : null}
    </ActionStepper>
  );
}
