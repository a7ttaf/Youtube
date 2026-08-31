import { type ReactNode, useRef, useState } from "react";

import { useWriteInFlightControl } from "@/contexts/WriteInFlightContext";
import { ApiError } from "@/lib/api/client";
import { describeApiError } from "@/lib/api/errors";
import type {
  ChannelImportFieldChange,
  ChannelImportResult,
  ChannelImportRowResult,
} from "@/lib/api/types";
import {
  echoesRequestedTarget,
  isChannelImportResult,
  useChannelImport,
} from "@/lib/api/useChannelImport";
import { displayDigestMatchesDisclosedAsync } from "@/lib/displayDigest";
import { useContentOwners } from "@/lib/api/useContentOwners";
import type { Severity } from "@/lib/mock/data";
import { ActionStepper } from "../ActionStepper";
import { OutcomeTable, type OutcomeTableRow } from "../OutcomeTable";
import { Badge } from "../shared";
import { isValidAuditReason } from "./GroupsSyncFlow";
import {
  UNSCOPED_IMPORT_SCOPE,
  newApplyId,
  settleApplyIn,
  useUnsettledImport,
  type AdmissionResult,
} from "@/contexts/UnsettledImportContext";

// ============================================================================
// Purpose: The Registry CSV import flow (import/sync arc, PR-B): a three-step
//   state machine over ActionStepper (["Upload", "Preview", "Applied"]) that
//   mirrors GroupsSyncFlow. (1) collect the roster CSV, the content owner
//   (credential-fed picker), and the required audited reason, then fire a
//   READ-ONLY dry-run (useChannelImport dryRun:true); (2) render the per-row
//   plan via OutcomeTable and, only while the plan is ERROR-free, allow Apply
//   (dryRun:false — the API is all-or-nothing and 422s an erroring plan);
//   (3) echo the APPROVED PLAN's counts + reason (labelled as the plan: the
//   route answers an apply with its pre-write payload, and the durable tally
//   lives in the CHANNEL_IMPORTED audit event). Cancel at any step restores
//   the registry with NO refetch unless an apply already committed (then the
//   parent reloads). Every exit is closed while the APPLY is in flight — the
//   two this component renders (Cancel, Preview's Back) and the shell's own
//   sidebar, via the WriteInFlightContext latch — because the hook exposes no
//   abort, so leaving would neither stop nor invalidate a POST that still
//   commits. The read-only dry-run is deliberately NOT guarded: abandoning a
//   preview is safe, and the flow promises Cancel at any step. The two close
//   paths (onCancel / onDone) are supplied by RegistryView, which renders this
//   only behind canImportChannels.
// Database/ORM: None (frontend) — POSTs /channels/import (preview + apply) via
//   useChannelImport; authorization (MANAGE_CHANNELS always, MANAGE_GROUPS on
//   Group_ID-bearing rosters) and every 409/422 failure stay the backend's
//   authority and surface inline.
// Standards: No client-side authorization is invented. Apply is fail-closed
//   against plans with ERROR rows (the API 422s them) and against an in-flight
//   request (synchronous in-flight ref latch -> one request per click burst).
//   The flow is also fail-closed against UNMOUNTING mid-write: while
//   `applying`, neither local exit is clickable AND the shell's nav is latched
//   (WriteInFlightContext), so the UI can never report a cancelled or
//   abandoned import that the backend went on to commit. The guard keys off
//   `applying`, not `busy`: only the apply commits, so a read-only dry-run
//   stays abandonable. `applying` clears in the request's `finally` on success
//   and failure alike. That same `finally` owns latch release even if a render
//   crash unmounts this flow while the shell survives.
//   An apply whose response never arrived is INDETERMINATE, not failed: Apply
//   is disabled (a blind retry would append a second unconditional
//   CHANNEL_IMPORTED), Back is frozen (Upload owns the inputs the check
//   re-plans), and the flow offers "Check whether it landed", which RE-PLANS
//   the same roster via the dry run. That reports the END STATE — "the
//   registry now matches this roster" — and deliberately NOT authorship:
//   inventory equality cannot establish that THIS request committed, since
//   another writer may have landed the same values, so the flow never
//   advances to Applied on that evidence and points at the audit trail
//   instead. Rows carrying group keys are excluded from even that claim,
//   because `outcome` is computed from channel inventory and the planner
//   never loads memberships. The control is operator-triggered and repeatable
//   on purpose — the original POST may still be executing, and one automatic
//   shot would race it and report a false "did not commit".
//   The reason obeys the shared required + no-NUL audit contract via
//   isValidAuditReason (imported from GroupsSyncFlow, not copied). Backend
//   detail is shown only on canned-copy statuses (describeApiError). The 422
//   apply race (a concurrent editor changed the registry between preview and
//   apply) carries the refreshed plan as its `detail`; when parseable it
//   REPLACES the stale preview so the operator reviews reality. Unlike the
//   sync flow there is NO canned-503 passthrough: the import route emits no
//   503 (verified against import_channels in api/channels.py — that file's
//   503s belong to the sync route), so an infrastructure 5xx collapses to the
//   generic fallback + status. Reuses ActionStepper + OutcomeTable + Badge —
//   no parallel visual language.
// Blast Radius: Channel-registry inventory + row-created group membership (the
//   import write path) — but only via the backend's own guarded, audited
//   route. No revenue math; the backend's locked-month guard rejects
//   revenue_required flips at apply time.
// Connections:
//   - File: frontend/src/lib/api/useChannelImport.ts -> the POST action.
//   - File: frontend/src/lib/api/useContentOwners.ts -> owner picker read.
//   - File: frontend/src/components/srcc/ActionStepper.tsx -> step shell.
//   - File: frontend/src/components/srcc/OutcomeTable.tsx -> plan table.
//   - File: frontend/src/components/srcc/views/GroupsSyncFlow.tsx ->
//       isValidAuditReason (shared audit-reason contract) + the flow pattern.
//   - File: frontend/src/components/srcc/views/RegistryView.tsx -> renders
//       this behind canImportChannels.
//   - File: backend/ums_smart_revenue/api/channels.py -> import_channels
//       (POST /channels/import).
// ============================================================================

// The credential connector_key the import reads content owners from. Mirrors
// backend YOUTUBE_ANALYTICS_CONNECTOR ("youtube-analytics", connectors/keys.py)
// and the Groups view's picker: the roster's content owner comes from a
// youtube-analytics credential's account_id.
const YOUTUBE_ANALYTICS_KEY = "youtube-analytics";

/**
 * Map an import failure to operator-facing copy via the shared sanitizer. The
 * import route's own statuses (403 permission, 409 plan-to-apply races, 422
 * malformed upload/form) are all on the canned-detail allowlist; the 422
 * apply-race object detail is NOT a string, so it falls to the generic
 * fallback here and is handled separately via applyRaceDetail.
 */
const describeImportError = (err: unknown): string => {
  return describeApiError(err, "The import request failed");
};

/**
 * Extract the refreshed plan an apply rejection carries, or null for every
 * other failure. TWO statuses ship the full plan as their `detail`, and both
 * mean "the plan you saw is not the plan we would execute":
 *   422 — the RE-PLANNED roster now holds ERROR rows (all-or-nothing).
 *   409 — the plan diverged from the fingerprint the operator approved, so a
 *         reviewed CREATE may now be an UPDATE over someone else's channel.
 * Both replace the stale preview so the operator re-approves against reality.
 *
 * NOT every 409 carries a plan: because this flow always sends
 * expected_plan_fingerprint, it opts in to the backend's strict pre-state
 * check, whose 409 detail is a STRING naming the row that moved. The guard
 * above returns null for it and it lands in the ordinary banner verbatim
 * (409 is on the canned-detail allowlist), which is the right treatment — it
 * already tells the operator to re-run the preview.
 */
const PLAN_BEARING_STATUSES = new Set([409, 422]);

const planBearingDetail = (err: unknown): unknown | null => {
  if (!(err instanceof ApiError) || !PLAN_BEARING_STATUSES.has(err.status)) {
    return null;
  }
  const body = err.body as { detail?: unknown } | null;
  return body?.detail ?? null;
};

// ============================================================================
// Purpose: Decide whether an apply REJECTION carries a plan worth putting in
//   front of the operator, and hand it back only if it does. This is the one
//   path where a payload from a FAILED request replaces the preview the
//   operator already approved and becomes what the next fingerprint-bound
//   apply is reviewed against. Execution is async: the digest gate awaits
//   displayDigestMatchesDisclosedAsync (worker-backed with sync fallback).
// Database/ORM: None (frontend) — an async gate over a decoded error body.
//   It dispatches nothing and mutates no state; the caller decides what to do
//   with the plan it returns.
// Standards: Three gates, all required, checked in order after the status is
//   plan-bearing (409 or 422): (1) isChannelImportResult on the detail body,
//   (2) awaited displayDigestMatchesDisclosedAsync (worker-backed; on worker
//   failure the verify path yields and falls back to sync SHA-256 — worker
//   failure alone does not reject), (3) echoesRequestedTarget against the
//   CAPTURED owner. Returns null (fails closed) when the body is unrecognised,
//   the digest does not match disclosed rows, or digest recomputation itself
//   fails; otherwise the error falls through to the ordinary banner and the
//   operator keeps the plan they actually reviewed. Not every 409 carries a
//   plan — this flow always sends expected_plan_fingerprint, so it opts into
//   the backend's strict pre-state check, whose 409 detail is a STRING naming
//   the row that moved; that lands in the banner verbatim, which already says
//   to re-run the preview.
// Blast Radius: FINANCE + group membership, at one remove. A wrongly accepted
//   payload does not itself write; it changes what the operator APPROVES, and
//   the apply that follows is bound to a fingerprint for a plan they never
//   saw — a different group action or revenue-source classification reviewed
//   under the banner of the one they did.
// Connections: the three gates applied here and the wire contract behind them.
//   - File: frontend/src/lib/api/useChannelImport.ts -> isChannelImportResult
//       and echoesRequestedTarget (gates 1 and 3).
//   - File: frontend/src/lib/displayDigest.ts -> displayDigestMatchesDisclosedAsync
//       (gate 2) and its worker-failure → sync-fallback contract (review #184, C1).
//   - File: backend/ums_smart_revenue/api/channels.py -> the route that emits
//       the 409/422 whose `detail` is a full ChannelImportResult.
//   - File: Docs/12_BACKEND_API_SPEC.md -> the documented rejection contract.
// ============================================================================
const applyRaceDetail = async (
  err: unknown,
  contentOwnerId: string,
): Promise<ChannelImportResult | null> => {
  const detail = planBearingDetail(err);
  if (!isChannelImportResult(detail)) {
    return null;
  }
  if (!(await displayDigestMatchesDisclosedAsync(detail))) {
    return null;
  }
  if (!echoesRequestedTarget(detail, contentOwnerId)) {
    return null;
  }
  return detail;
};

/** The banner for a refreshed plan, worded for which rejection produced it. */
const refreshedPlanMessage = (status: number): string => {
  if (status === 409) {
    return (
      "The registry changed since this preview, so the import no longer does " +
      "what you approved. The refreshed plan below is what would be written — " +
      "review it and apply again."
    );
  }
  return (
    "The registry changed since this preview; the refreshed plan below " +
    "shows the ERROR rows that blocked the apply."
  );
};

/**
 * Statuses on which the import route definitively wrote NOTHING. Every one is
 * raised before `apply_channel_import` runs, or aborts the single transaction
 * that wraps it: 400/401/403 authorization and form validation, 409 the
 * plan-to-apply conflicts, the fingerprint mismatch and the pre-state guard,
 * 413 the payload cap, 422 malformed uploads and ERROR-row plans.
 *
 * 404 is on the list because the import route raises none of its own — the
 * only 404 this flow can receive comes from the tenancy resolver, which
 * answers inside ASGI middleware without ever awaiting the app (or from a
 * request that never reached the backend at all). Either way the handler did
 * not run, so treating it as "may have committed" would strand the operator
 * on a retry lockout for a request that provably wrote nothing.
 *
 * Everything ELSE is ambiguous and must be treated as such — the two cases
 * that make this a set rather than a `status < 500` test:
 *   - A 2xx carrying malformed JSON. The client raises ApiError with the
 *     ORIGINAL 2xx status (client.ts), so the server said OK; the import
 *     almost certainly committed and only the body was unreadable.
 *   - A gateway 502/503/504. The request may have reached the backend and
 *     committed with the response lost on the way home.
 * Both were previously read as definite refusals purely because they arrived
 * as an ApiError (review #184).
 */
const DEFINITE_REJECTION_STATUSES = new Set([400, 401, 403, 404, 409, 413, 422]);

// ============================================================================
// Purpose: Classify a FAILED apply as "definitely wrote nothing" or "outcome
//   unknown" — the decision that keeps the durable pending-write guard up, or
//   retires it and re-enables Apply.
// Database/ORM: None (frontend) — a pure read of the caught error. It performs
//   no request and settles nothing itself; the caller acts on the verdict.
// Standards: Fail-safe BY CONSTRUCTION, and the construction is the point.
//   It is an allowlist of statuses that establish rejection, not a
//   `status < 500` test, so an unanticipated failure mode defaults to
//   indeterminate rather than to "safe to retry". Two cases make that
//   difference concrete: a 2xx carrying malformed JSON (client.ts raises with
//   the ORIGINAL 2xx status, so the server said OK and the import almost
//   certainly committed), and a gateway 502/503/504 (the request may have
//   reached the backend and committed with the response lost on the way home).
//   A rejected fetch is never an ApiError at all, so no response means unknown.
//   Every status ON the list is one the route raises before
//   apply_channel_import runs, or one that aborts the single transaction
//   wrapping it — see DEFINITE_REJECTION_STATUSES for the per-status warrant,
//   including why 404 belongs there.
// Blast Radius: Both errors are real and opposite. Calling an indeterminate
//   apply definite retires the guard over a write that may still be
//   committing, so the operator can dispatch a DUPLICATE audited import;
//   calling a definite rejection indeterminate strands them on a retry lockout
//   for a request that provably wrote nothing. No authorization meaning and no
//   revenue math; the CHANNEL_IMPORTED audit event remains the authority on
//   what committed.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> ApiError, and the 2xx-with-bad-
//       body case that keeps the original status.
//   - File: backend/ums_smart_revenue/api/channels.py -> import_channels,
//       which raises every status on the definite list.
//   - File: frontend/src/contexts/UnsettledImportContext.tsx -> the pending
//       record this verdict decides the fate of.
// ============================================================================
/**
 * True when an apply failure leaves the outcome UNKNOWN: no response at all
 * (a rejected fetch is never an ApiError), or a response that does not
 * establish rejection. Fail-safe by construction — anything not on the
 * definite-rejection list is indeterminate.
 */
const isIndeterminateApplyFailure = (caught: unknown): boolean => {
  if (!(caught instanceof ApiError)) {
    return true;
  }
  return !DEFINITE_REJECTION_STATUSES.has(caught.status);
};

/** True when any planned row is an ERROR (the API 422s an apply of such a plan). */
const hasErrorRows = (result: ChannelImportResult): boolean => {
  return result.rows.some((row) => row.outcome === "ERROR");
};

/**
 * Upload-step submit guard: a roster file is chosen, an owner is selected, and
 * the audited reason is valid. A type predicate on `file` so callers that pass
 * the guard get the non-null File without a cast.
 */
const canSubmitUpload = (
  file: File | null,
  ownerId: string,
  reason: string,
): file is File => {
  return file !== null && ownerId !== "" && isValidAuditReason(reason);
};

/**
 * Apply guard: the roster file is still held and the previewed plan is free of
 * ERROR rows (the API 422s an erroring plan — all-or-nothing). Returns the
 * pair rather than a type predicate, because the apply needs BOTH narrowed —
 * the File for the multipart body and the plan for its fingerprint — and a
 * function may only declare a predicate on one parameter.
 */
const approvedApply = (
  file: File | null,
  preview: ChannelImportResult | null,
): { file: File; plan: ChannelImportResult } | null => {
  if (file === null || preview === null || hasErrorRows(preview)) {
    return null;
  }
  return { file, plan: preview };
};

/** Outcome chip tone: CREATE green, UPDATE blue, ERROR red — matching shared
 * Badge tones. UNCHANGED is deliberately absent and renders as muted text. */
const OUTCOME_TONES: Partial<Record<ChannelImportRowResult["outcome"], Severity>> = {
  CREATE: "green",
  UPDATE: "blue",
  ERROR: "red",
};

/** Render one outcome as its toned Badge, or as muted text when it has none.
 * Object.hasOwn guards the lookup (the same prototype-chain hardening as the
 * sync flow's chip): an unexpected wire string stays muted, never a Badge. */
const outcomeChip = (outcome: ChannelImportRowResult["outcome"]): ReactNode => {
  const tone = Object.hasOwn(OUTCOME_TONES, outcome)
    ? OUTCOME_TONES[outcome]
    : undefined;
  if (!tone) {
    return <span className="muted">{outcome}</span>;
  }
  return <Badge tone={tone}>{outcome}</Badge>;
};

/** Format one side of a field change; null renders as an em-dash. */
const changeValue = (value: string | boolean | null): string => {
  return value === null ? "—" : String(value);
};

/**
 * Render a row's field diffs as "field: from → to" lines (or a muted dash when
 * the mapping is empty — a CREATE row has no diff by design). Field names are
 * unique within a row's `changes` Record, so they key the lines.
 */
const ChangesCell = ({ changes }: {
  changes: Record<string, ChannelImportFieldChange>;
}) => {
  const entries = Object.entries(changes);
  if (entries.length === 0) {
    return <span className="muted">—</span>;
  }
  return (
    <>
      {entries.map(([field, change]) => (
        <div key={field}>
          {`${field}: ${changeValue(change.from)} → ${changeValue(change.to)}`}
        </div>
      ))}
    </>
  );
};

/** A nullable wire string as itself, or a muted em-dash when null. */
const orMutedDash = (value: string | null): ReactNode => {
  return value ?? <span className="muted">—</span>;
};

/**
 * Channel cell: the display name over its durable youtube_channel_id. BOTH are
 * shown, never one as a fallback for the other — channel_name is mutable and
 * not unique, so two roster rows can carry the same name while a CREATE or an
 * UPDATE keys on the id alone. Showing only the name would leave the operator
 * unable to tell which channel identity an all-or-nothing apply will touch;
 * the steady-state Registry table shows both for the same reason. ERROR rows
 * can lack either half, and each missing half renders as a muted dash.
 */
const ChannelCell = ({ row }: { row: ChannelImportRowResult }) => {
  return (
    <>
      <div>{orMutedDash(row.channel_name)}</div>
      <div className="item-sub">{orMutedDash(row.youtube_channel_id)}</div>
    </>
  );
};

/** Operator wording for each planned group effect. CREATE is called out as a
 * NEW group because that is the finance-scope consequence a bare key hides;
 * JOIN says "adds to" rather than promising a write, since a channel already
 * in the group is a no-op (the plan reads group keys in bulk and deliberately
 * does not load memberships for a 5000-row roster). */
const GROUP_ACTION_LABELS: Record<
  NonNullable<ChannelImportRowResult["group_action"]>,
  string
> = {
  CREATE: "new group",
  JOIN: "adds to existing",
};

/**
 * The Group cell: the CMS key plus what the import will DO with it. The key
 * alone is ambiguous between two effects with different blast radii —
 * creating a new SECTOR group (a finance-scope object, stamped to this
 * content owner at birth) versus attaching a channel to one that already
 * exists — and the roster is approved all-or-nothing, so the operator has to
 * see which before applying rather than reconstruct it from the audit trail
 * afterwards. Object.hasOwn guards the lookup exactly as the outcome chip
 * does: an unexpected wire value degrades to the bare key, never a wrong
 * claim about a group write.
 */
const GroupCell = ({ row }: { row: ChannelImportRowResult }) => {
  const { group_id, group_action } = row;
  if (group_id === null) {
    return <span className="muted">—</span>;
  }
  const label =
    group_action !== null && Object.hasOwn(GROUP_ACTION_LABELS, group_action)
      ? GROUP_ACTION_LABELS[group_action]
      : null;
  return (
    <>
      <div>{group_id}</div>
      {label ? <div className="item-sub">{label}</div> : null}
    </>
  );
};

/**
 * The row's revenue flag as operator text. Spec-mandated column: on CREATE
 * rows the changes mapping is empty by design, so this cell is the ONLY place
 * the preview shows revenue_required (which defaults to true when the CSV
 * omits view_revenue) before the all-or-nothing apply. Null (ERROR rows)
 * renders as a muted dash.
 */
const revenueFlagLabel = (revenue_required: boolean | null): ReactNode => {
  if (revenue_required === null) {
    return <span className="muted">—</span>;
  }
  return revenue_required ? "Yes" : "No";
};

/**
 * The Revenue cell: the flag the roster asserts, plus the source
 * classification the write DERIVES from it when that changes.
 *
 * The second line is not decoration. The registry re-classifies
 * `revenue_source_status` whenever `revenue_required` flips, and that status
 * drives `missing_official_revenue` and the registry's recommended action —
 * so flipping the flag off silently replaces a proven OFFICIAL_CMS_REVENUE
 * with PERFORMANCE_ONLY. `changes` never carries it (it holds the operator's
 * own field edits), so without this the operator approves a finance-source
 * mutation nothing on screen mentions (review #184).
 */
const RevenueCell = ({ row }: { row: ChannelImportRowResult }) => {
  const source = row.revenue_source_status;
  return (
    <>
      <div>{revenueFlagLabel(row.revenue_required)}</div>
      {source ? (
        <div className="item-sub">
          {source.from === null
            ? `source: ${source.to}`
            : `source: ${source.from} → ${source.to}`}
        </div>
      ) : null}
    </>
  );
};

/** Map one per-row import result to an OutcomeTable row (ERROR -> warn tone). */
const importOutcomeRow = (row: ChannelImportRowResult): OutcomeTableRow => {
  return {
    key: `row-${row.row_number}`,
    tone: row.outcome === "ERROR" ? "warn" : undefined,
    cells: [
      row.row_number,
      // Constant keys are unique among these siblings (one element literal per
      // key); OutcomeTable re-keys each cell by column anyway.
      <ChannelCell key="channel" row={row} />,
      outcomeChip(row.outcome),
      <ChangesCell key="changes" changes={row.changes} />,
      <GroupCell key="group" row={row} />,
      <RevenueCell key="revenue" row={row} />,
      // The backend's verbatim row note (ERROR rows name the failure).
      orMutedDash(row.reason),
    ],
  };
};

/**
 * Non-zero outcome counts as one "CREATE: 2 · UPDATE: 1" line (or nothing).
 * `label` prefixes the line so a step can name WHOSE counts these are — the
 * Applied step must not present a plan tally as a re-read of the write.
 */
const CountsStrip = ({ counts, label }: {
  counts: Record<string, number>;
  label?: string;
}) => {
  const entries = Object.entries(counts).filter(([, value]) => value > 0);
  if (entries.length === 0) {
    return null;
  }
  const text = entries.map(([outcome, value]) => `${outcome}: ${value}`).join(" · ");
  return <p className="item-sub">{label ? `${label} — ${text}` : text}</p>;
};

/** Inline error banner shown in the current step; backend detail verbatim. */
const ImportErrorBanner = ({ title, detail }: { title: string; detail: string }) => {
  return (
    <div className="form-error" role="alert">
      <strong>{title}</strong>
      <span>{detail}</span>
    </div>
  );
};

/** Short muted note under the owner picker for its non-ready states (or null). */
const ownerPickerNote = (
  state: ReturnType<typeof useContentOwners>,
  ownerCount: number,
): string | null => {
  if (state.error) {
    return "Couldn't load content owners.";
  }
  if (!state.data) {
    return "Loading content owners…";
  }
  if (ownerCount === 0) {
    return "Register a youtube-analytics credential in Connectors first.";
  }
  return null;
};

type OwnerFieldProps = {
  ownerState: ReturnType<typeof useContentOwners>;
  ownerId: string;
  onOwnerChange: (ownerId: string) => void;
  /** Locked while a request is in flight — see UploadStep. */
  locked: boolean;
};

/**
 * The content-owner picker, fed by the least-privilege GET
 * /connectors/content-owners read (ACTIVE youtube-analytics account ids only,
 * MANAGE_GROUPS-gated — a permission every canImportChannels holder has).
 * Disabled with a Connectors pointer while empty, loading, or failed.
 */
const OwnerField = ({ ownerState, ownerId, onOwnerChange, locked }: OwnerFieldProps) => {
  const owners = ownerState.data?.items.map((item) => item.account_id) ?? [];
  const note = ownerPickerNote(ownerState, owners.length);
  return (
    <div className="field-row">
      <label htmlFor="importOwner">Content owner</label>
      <select
        id="importOwner"
        value={ownerId}
        disabled={locked || owners.length === 0}
        onChange={(event) => onOwnerChange(event.target.value)}
      >
        <option value="">Select a content owner…</option>
        {owners.map((accountId) => (
          <option key={accountId} value={accountId}>
            {accountId}
          </option>
        ))}
      </select>
      {note ? <span className="muted">{note}</span> : null}
    </div>
  );
};

/** The CSV contract, stated inline so the operator needn't leave the step. */
const CsvContractNote = () => {
  return (
    <p className="muted" role="note">
      CSV columns — required: youtube_channel_id, channel_name; optional:
      group_id, view_revenue. Headers are case-insensitive; unknown or
      duplicate headers are rejected.
    </p>
  );
};

type UploadStepProps = {
  ownerState: ReturnType<typeof useContentOwners>;
  ownerId: string;
  onOwnerChange: (ownerId: string) => void;
  file: File | null;
  onFileChange: (file: File | null) => void;
  reason: string;
  onReasonChange: (value: string) => void;
  onRun: () => void;
  busy: boolean;
  error: string | null;
};

/**
 * Step 1: roster CSV + content owner + required, audited reason; "Preview"
 * fires the read-only dry-run. The selected filename is echoed beside the
 * input because a return trip from Preview remounts the (uncontrolled) file
 * input empty while the held File survives in flow state.
 */
const UploadStep = ({
  ownerState,
  ownerId,
  onOwnerChange,
  file,
  onFileChange,
  reason,
  onReasonChange,
  onRun,
  busy,
  error,
}: UploadStepProps) => {
  const canRun = canSubmitUpload(file, ownerId, reason) && !busy;
  return (
    <div className="confirm-panel" role="group" aria-label="Import upload">
      <div className="panel-title">
        <strong>Import a channel roster</strong>
        <span>
          Runs a read-only dry-run first. A reason is required and recorded on
          the audit event.
        </span>
      </div>
      <CsvContractNote />
      <div className="field-row">
        <label htmlFor="importCsvFile">Roster CSV</label>
        <input
          id="importCsvFile"
          type="file"
          accept=".csv"
          disabled={busy}
          onChange={(event) => onFileChange(event.target.files?.[0] ?? null)}
        />
        {file ? <span className="muted">Selected: {file.name}</span> : null}
      </div>
      <OwnerField
        ownerState={ownerState}
        ownerId={ownerId}
        onOwnerChange={onOwnerChange}
        locked={busy}
      />
      <div className="field-row">
        <label htmlFor="importReason">Reason (required, audited)</label>
        <input
          id="importReason"
          value={reason}
          disabled={busy}
          onChange={(event) => onReasonChange(event.target.value)}
          placeholder="Why import this roster"
        />
      </div>
      {error ? <ImportErrorBanner title="Dry-run failed" detail={error} /> : null}
      <div className="action-row">
        <button
          className="primary-button"
          type="button"
          disabled={!canRun}
          onClick={onRun}
        >
          {busy ? "Running…" : "Preview"}
        </button>
      </div>
    </div>
  );
};

/**
 * The error-rows remedy line, shown only when the plan carries an ERROR row.
 * The import is all-or-nothing: the API 422s an apply while any row errors,
 * so Apply stays blocked until a clean preview.
 */
const ErrorRowsNote = ({ show }: { show: boolean }) => {
  return show ? (
    <p className="muted" role="note">
      Error rows block apply: the import is all-or-nothing (the API rejects the
      whole plan with 422). Fix the CSV, then go Back and preview again.
    </p>
  ) : null;
};

const IMPORT_COLUMNS = ["Row", "Channel", "Outcome", "Changes", "Group", "Revenue", "Note"];

/**
 * Why the flow refuses to be left while a request is in flight. The hook has
 * no abort channel and the backend commits independently of this component, so
 * an unmounted flow cannot be told the write landed: the operator would be
 * shown a cancelled/abandoned import that actually committed. Both exits
 * (Cancel, Back) carry this as their disabled title.
 */
const APPLY_IN_FLIGHT_NOTE =
  "Wait for the request to finish — it cannot be aborted, and leaving now " +
  "would hide an import that still commits.";

/**
 * Why Apply is refused after an apply whose response never arrived. Retrying
 * blind would submit the roster a second time and append a second
 * unconditional CHANNEL_IMPORTED audit event for what may already have
 * committed; only a reload can establish which.
 */
const APPLY_INDETERMINATE_NOTE =
  "This import may already have committed — reload the registry and check " +
  "before importing again.";

// Not this tab's request: another document holds an apply whose outcome is
// still unknown. Named separately from APPLY_INDETERMINATE_NOTE because the
// remedy is different — there is nothing to reload here, the operator has to
// go back to the tab that is waiting.
const OTHER_APPLY_PENDING_NOTE =
  "Another tab has an import whose outcome is not settled yet — finish or " +
  "account for that one before applying this roster.";

// The OTHER refusal, and it needs its own words: there is no other tab to go
// back to and nothing to reload. Without durable site storage the flow cannot
// record that this import is in flight, so a reload or a second tab would have
// no idea it happened — and this write appends an audit event. Refusing is the
// safe direction, but the operator has to be told what to change.
const APPLY_NOT_DURABLE_NOTE =
  "This browser is not storing site data, so the import cannot be tracked " +
  "while it runs — and an untracked import can be submitted twice without " +
  "either attempt knowing about the other. Allow site data (cookies and " +
  "storage) for this site, or use a normal window instead of a private one, " +
  "then try again.";

// A THIRD refusal, and the only TEMPORARY one: the namespace that isolates
// these records is still being resolved. The scope is built from the tenant id,
// and a session whose body carries no tenant gets that id from /tenants/me — so
// admitting during that window files the record under the missing-tenant
// namespace and then has the scope change out from under it, stranding the
// record where neither this tab's guard nor another tab's looks for it (review
// #184). A tenant that FAILS to resolve does NOT count as settled: two tabs
// disagreeing about the tenant build different namespaces and different Web
// Locks, so neither sees the other and both dispatch. A reload re-runs the
// bootstrap, which is why refusing here is recoverable rather than a wedge.
const IMPORT_SCOPE_UNSETTLED_NOTE =
  "Still confirming which workspace this import belongs to — this usually " +
  "takes a moment, so try Apply again shortly. If it keeps saying this, " +
  "reload the page: importing without a confirmed workspace could let the " +
  "same roster be submitted twice from different tabs without either knowing.";

// A FOURTH refusal, and the one that is not a decision at all: admission
// itself threw. navigator.locks.request() rejects before ever invoking its
// callback in a restricted or opaque-origin document (SecurityError) and in a
// detached one — so no key was written and, crucially, NO REQUEST WAS SENT.
// It needs its own words because the alternative is far worse than unhelpful:
// routed through handleApplyFailure, a non-ApiError classifies as
// INDETERMINATE, and the flow would tell the operator their import may have
// committed, freeze Back, and offer to reconcile a write that provably never
// left the browser (review #184, codex P2).
const ADMISSION_FAILED_NOTE =
  "Could not start the import: this browser refused the lock that keeps two " +
  "tabs from submitting the same roster. Nothing was sent, so nothing was " +
  "imported. Reload the page, or open it in a normal window if this is a " +
  "restricted or embedded one, then try again.";

/**
 * Which refusal the operator is looking at. Named rather than inlined at the
 * dispatch site: the remedies are genuinely different — go back to the other
 * tab, versus change this browser's storage setting — and keeping the choice
 * here also keeps `apply` under the analyzer's complexity threshold
 * (DeepSource JS-R1005), conformed rather than suppressed.
 */
const admissionRefusalNote = (admission: AdmissionResult): string => {
  return admission === "not-durable" ? APPLY_NOT_DURABLE_NOTE : OTHER_APPLY_PENDING_NOTE;
};

// ============================================================================
// Purpose: Run the cross-document admission claim and fold a THROW into the
//   RESULT rather than letting it propagate. Returning a note instead of
//   raising is the whole point: it keeps a failure to ADMIT from being
//   classified as a failure to WRITE.
// Database/ORM: None (frontend). The claim is browser-local (the cross-tab
//   lock behind `admit`); no request is dispatched from here.
// Standards: Admission runs strictly BEFORE the POST, so every failure mode it
//   has shares one fact — nothing was sent — which is the exact opposite of
//   the INDETERMINATE default every other error in `apply` correctly falls
//   into. The `catch` is deliberately broad because the failures are
//   environmental (a browser refusing storage, an embedded or restricted
//   window), not conditions worth distinguishing: they share both the fact and
//   the remedy. The refusal notes stay distinct from the throw note because
//   their remedies differ — go back to the other tab, versus change this
//   browser's storage setting.
// Blast Radius: Whether an AUDITED import is dispatched at all, and whether
//   the durable duplicate-write guard stays raised. Getting this wrong in the
//   permissive direction dispatches a second copy of a roster; getting it
//   wrong in the other direction tells the operator their import MAY have
//   committed, freezes Back, and offers to reconcile a write that provably
//   never left the browser (review #184, codex P2). No finance math here — it
//   decides whether the write happens, not what it contains.
// Connections:
//   - File: frontend/src/contexts/UnsettledImportContext.tsx -> the admission
//       claim and its AdmissionResult states, which this classifies.
//   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx ->
//       `apply`, the only caller, whose indeterminate default this exists to
//       keep off the not-dispatched path.
// ============================================================================
const admitForApply = async (
  admit: (applyId: string) => Promise<AdmissionResult>,
  applyId: string,
): Promise<{ admitted: boolean; note: string }> => {
  try {
    const admission = await admit(applyId);
    return admission === "admitted"
      ? { admitted: true, note: "" }
      : { admitted: false, note: admissionRefusalNote(admission) };
  } catch {
    return { admitted: false, note: ADMISSION_FAILED_NOTE };
  }
};

// The outcome literal both reconciliation predicates key off. Named because
// a typo in either one silently changes a verdict about whether a write
// landed, and the two must always mean the same thing.
const OUTCOME_UNCHANGED = "UNCHANGED";

// ============================================================================
// Purpose: Answer the one question a re-plan CAN settle after a write of
//   unknown outcome — does the registry now match this roster? — and refuse
//   the question it cannot. It is the evidence behind telling an operator that
//   nothing remains to apply.
// Database/ORM: None (frontend) — a predicate over a freshly re-planned
//   payload. The re-plan itself is a dry run, so asking this question writes
//   nothing.
// Standards: This is deliberately a STATE claim, never an authorship one.
//   Inventory equality cannot establish who wrote the values: the request may
//   never have reached the backend while another writer landed the same ones,
//   and a roster can preview as all-UNCHANGED before any apply at all. Nothing
//   reachable from this endpoint carries a durable import identity, so the
//   flow reports what it can prove and points at the audit trail for the
//   event. (A `CHANNEL_IMPORTED` identity to reconcile against is the open
//   API-surface question flagged to the owner on this PR.) Rows carrying group
//   keys are excluded from even that weaker claim, because `outcome` is
//   computed from channel INVENTORY only — the planner never loads
//   memberships — so the channels can match while the attachments the same
//   import owed are still missing; `hasUnverifiableGroupEffects` below is the
//   branch that says so out loud rather than letting silence read as success.
// Blast Radius: AUDIT follow-up and duplicate-import guidance. A false TRUE
//   tells an operator whose write outcome is unknown that there is nothing
//   left to do, closing out an import that may have half-landed; a false FALSE
//   invites them to re-apply a roster that already committed. Neither writes
//   anything itself — both steer what the operator does next.
// Connections:
//   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx ->
//       hasUnverifiableGroupEffects, the sibling that keeps the group-bearing
//       case from silently reading as a match.
//   - File: backend/ums_smart_revenue/org/channel_import.py -> the planner
//       whose inventory-only `outcome` is exactly why group rows are excluded.
//   - File: frontend/src/lib/api/useChannelImport.ts -> the re-plan whose
//       result this reads.
// ============================================================================
const rosterMatchesRegistry = (plan: ChannelImportResult): boolean => {
  return plan.rows.every(
    (row) => row.outcome === OUTCOME_UNCHANGED && row.group_id === null,
  );
};

// ============================================================================
// Purpose: True when a re-plan is inconclusive BECAUSE the roster carries
//   group keys — the case rosterMatchesRegistry above must refuse rather than
//   answer. It exists so the group-bearing shape gets its own verdict instead
//   of falling into the same "nothing left to do" bucket as a clean match.
// Database/ORM: None (frontend) — a predicate over a re-planned payload.
// Standards: Deliberately NOT the negation of rosterMatchesRegistry. Both
//   require every row UNCHANGED; they differ only on whether any row carries a
//   group key, so a plan with outstanding work satisfies neither and reaches
//   the divergence copy instead. That three-way split is the point: match,
//   unverifiable, and diverged are distinct verdicts, and collapsing the
//   middle one into either neighbour is how silence starts reading as success.
// Blast Radius: AUDIT follow-up after a write of UNKNOWN outcome. `outcome` is
//   computed from channel INVENTORY only — the planner never loads
//   memberships — so an all-UNCHANGED re-plan proves nothing about the
//   attachments the same import owed. Reporting this shape as a match would
//   close out an import whose group work may never have landed; reporting it
//   as divergence would invite re-applying a roster that already committed.
//   Neither writes anything — both steer what the operator does next.
// Connections:
//   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx ->
//       rosterMatchesRegistry, the sibling verdict this one carves out of.
//   - File: backend/ums_smart_revenue/org/channel_import.py -> the planner
//       whose inventory-only outcome makes memberships unverifiable here.
// ============================================================================
const hasUnverifiableGroupEffects = (plan: ChannelImportResult): boolean => {
  return (
    plan.rows.every((row) => row.outcome === OUTCOME_UNCHANGED) &&
    plan.rows.some((row) => row.group_id !== null)
  );
};

/**
 * Copy for a re-plan that still shows work the registry has not taken.
 *
 * Says DIVERGENCE, never "your import failed" — the inverse branch cannot
 * establish authorship any more than the matching one can. The apply may well
 * have committed and another writer may have edited or archived a row since,
 * which puts UPDATE or ERROR work back into the plan. So this reports the
 * comparison and leaves the request's outcome unknown (review #184).
 */
const RECONCILE_PENDING_NOTE =
  "The registry does not match this roster: the refreshed plan below still " +
  "shows work. That does NOT establish that your import failed — it may have " +
  "committed and been changed since. If the request is simply still running, " +
  "this will settle shortly, so check again in a moment. Do not re-import " +
  "until you know which it was.";

/**
 * Copy for a re-plan whose channels all match but whose rows carry group
 * keys. The plan cannot see membership, so this is honestly inconclusive
 * rather than a "did not commit" — and it must not read as one.
 */
const RECONCILE_GROUPS_UNVERIFIABLE_NOTE =
  "The channels in this roster already match the registry, but it also " +
  "assigns groups, and a re-plan cannot tell whether those memberships were " +
  "attached. Check the Groups view before importing again.";

/**
 * Copy for a re-plan showing the registry already matches the roster. Says
 * what is proven (the end state) and where the answer to "was it MY import"
 * actually lives (the audit trail) — rather than claiming an authorship this
 * check cannot establish.
 */
const RECONCILE_MATCHES_NOTE =
  "The registry now matches this roster, so there is nothing left to apply. " +
  "This shows the end state, not which request produced it — check the Audit " +
  "view for the import event if you need to confirm yours was recorded.";

/** Copy for a reconciliation attempt with no roster left to re-plan. */
const RECONCILE_NO_ROSTER_NOTE =
  "The roster is no longer loaded, so the earlier import's outcome cannot be " +
  "checked here. Reload the registry to see the actual state.";

const RECONCILE_FAILED_PREFIX = "Could not check the registry: ";
const RECONCILE_STILL_UNKNOWN = "The earlier import's outcome is still unknown.";

/**
 * Why Back is refused while an apply's outcome is unknown. Upload owns the
 * file, owner and reason the reconciliation RE-PLANS, so letting the operator
 * back out and change them would let "Check whether it landed" answer about a
 * DIFFERENT roster and declare the earlier import applied on that evidence.
 * The inputs stay frozen until the outcome is settled; Cancel is still open,
 * and it forces a registry reload rather than pretending anything is known.
 */
const RECONCILE_FROZEN_NOTE =
  "This import's outcome is still unknown — check whether it landed, or " +
  "cancel to reload the registry. Changing the roster now would make that " +
  "check answer about a different import.";

/** Back's disabled title: why the exit is refused, or undefined when live. */
const backBlockedTitle = (
  applying: boolean,
  indeterminate: boolean,
): string | undefined => {
  if (applying) {
    return APPLY_IN_FLIGHT_NOTE;
  }
  return indeterminate ? RECONCILE_FROZEN_NOTE : undefined;
};

/** The plan itself is what the API refuses here, not anything about this tab. */
const ERROR_ROWS_NOTE = "The API refuses plans with error rows (422)";

/**
 * Apply's disabled title: why the button is refused, or undefined when live.
 *
 * An ORDERED table, first match wins, rather than a chain of ifs — order is the
 * whole contract here (several reasons can hold at once and only the truest one
 * should be shown), and a table states the precedence in one readable place
 * instead of spreading it across early returns. It also keeps the function
 * under the analyzer's complexity threshold (DeepSource JS-R1005) — conformed,
 * not suppressed.
 */
const applyBlockedTitle = (
  hasErrors: boolean,
  indeterminate: boolean,
  otherApplyPending: boolean,
  applying: boolean,
  scopeUnsettled: boolean,
): string | undefined => {
  const rules: readonly (readonly [boolean, string | undefined])[] = [
    [indeterminate, APPLY_INDETERMINATE_NOTE],
    // `applying` ahead of both shared-record reasons, with NO note: this tab
    // raises the shared unsettled record the moment it admits its own apply, so
    // without this the button would blame "another tab" for the request this
    // very tab is running (review #184, qodo).
    [applying, undefined],
    // And ahead of otherApplyPending: while the scope is still moving, that
    // flag was read out of a bucket this session may not end up owning, so
    // blaming another tab would be a guess. This reason is the true one.
    [scopeUnsettled, IMPORT_SCOPE_UNSETTLED_NOTE],
    [otherApplyPending, OTHER_APPLY_PENDING_NOTE],
    [hasErrors, ERROR_ROWS_NOTE],
  ];
  // No match and a matched rule carrying no note both mean "say nothing", which
  // is exactly what the `applying` row wants.
  return rules.find(([applies]) => applies)?.[1];
};

/**
 * Whether Apply is refused, as one named predicate rather than a chain inside
 * the action row. Every clause is a distinct reason: an ERROR row the API
 * would 422 anyway, this tab's own request already in flight, this tab's
 * request whose outcome was never learned, ANOTHER tab's request in the same
 * state, and a namespace that has not stopped moving. Extracted to keep
 * PreviewActions under the analyzer's medium-risk complexity threshold
 * (DeepSource JS-R1005) — conformed, not suppressed. Kept beside
 * applyBlockedTitle on purpose: the two must stay in step, or the button
 * refuses a click with no explanation of why.
 */
const applyRefused = (
  hasErrors: boolean,
  busy: boolean,
  indeterminate: boolean,
  otherApplyPending: boolean,
  scopeUnsettled: boolean,
): boolean => {
  return hasErrors || busy || indeterminate || otherApplyPending || scopeUnsettled;
};

type PreviewActionsProps = {
  hasErrors: boolean;
  onBack: () => void;
  onApply: () => void;
  busy: boolean;
  applying: boolean;
  indeterminate: boolean;
  otherApplyPending: boolean;
  scopeUnsettled: boolean;
};

/**
 * Preview's action row: Back and Apply, each with its own refusal rule. Split
 * out of PreviewStep because both buttons are conditional twice over (disabled
 * state plus the title explaining it), and carrying four such branches
 * alongside the panel's layout pushed the step's cyclomatic complexity past
 * the analyzer's medium-risk threshold.
 *
 * Back is refused while the APPLY is in flight — `applying`, not `busy`.
 * Leaving would neither abort nor invalidate that POST: a late success would
 * commit the OLD roster while the operator, already back on Upload, believes
 * the attempt was abandoned — and then `setStep("applied")` would yank them
 * out of the upload they had started onto an Applied screen for a write they
 * thought they had dropped. A read-only dry-run carries none of that risk and
 * stays abandonable. `applying` clears in the apply's `finally` on success AND
 * failure, so this never traps anyone.
 */
const PreviewActions = ({
  hasErrors,
  onBack,
  onApply,
  busy,
  applying,
  indeterminate,
  otherApplyPending,
  scopeUnsettled,
}: PreviewActionsProps) => {
  return (
    <div className="action-row">
      <button
        className="ghost-button"
        type="button"
        disabled={applying || indeterminate}
        title={backBlockedTitle(applying, indeterminate)}
        onClick={onBack}
      >
        Back
      </button>
      <button
        className="primary-button"
        type="button"
        disabled={applyRefused(
          hasErrors,
          busy,
          indeterminate,
          otherApplyPending,
          scopeUnsettled,
        )}
        title={applyBlockedTitle(
          hasErrors,
          indeterminate,
          otherApplyPending,
          applying,
          scopeUnsettled,
        )}
        onClick={onApply}
      >
        {/* `applying`, not `busy`: the reconciliation dry run also sets busy,
            and labelling that "Applying…" tells the operator a write is
            running when nothing is being written (review #184, qodo). */}
        {applying ? "Applying…" : "Apply"}
      </button>
    </div>
  );
};

type PreviewStepProps = {
  result: ChannelImportResult;
  onBack: () => void;
  onApply: () => void;
  onReconcile: () => void;
  busy: boolean;
  applying: boolean;
  indeterminate: boolean;
  otherApplyPending: boolean;
  scopeUnsettled: boolean;
  error: string | null;
};

/**
 * Step 2: the planned per-row outcome. Apply is disabled while any row is an
 * ERROR (the API 422s such a plan — all-or-nothing) or while an apply is in
 * flight. A counts strip above the table summarizes the plan's effect.
 */
const PreviewStep = ({
  result,
  onBack,
  onApply,
  onReconcile,
  busy,
  applying,
  indeterminate,
  otherApplyPending,
  scopeUnsettled,
  error,
}: PreviewStepProps) => {
  const hasErrors = hasErrorRows(result);
  const rows = result.rows.map(importOutcomeRow);
  return (
    <div className="confirm-panel" role="group" aria-label="Import preview">
      <div className="panel-title">
        <strong>Review the plan</strong>
        {/* The CMS status is echoed, not decorative: it decides whether the
            connector's revenue pull targets a channel at all, and a CREATE
            row's empty `changes` shows it nowhere else. An operator must not
            approve a roster without seeing what every new channel gets.
            The request sends this field EXPLICITLY as a fixed target
            (IMPORT_CMS_STATUS), which is what lets the hook reject a preview
            echoing a different one before it is approved — it is load-bearing,
            not a redundant restatement of the route default (review #184). */}
        <span>
          Roster plan for content owner {result.content_owner_id} · CMS status{" "}
          {result.cms_status}.
        </span>
      </div>
      <CountsStrip counts={result.counts} />
      <OutcomeTable
        columns={IMPORT_COLUMNS}
        rows={rows}
        emptyLabel="No rows in this roster."
      />
      <ErrorRowsNote show={hasErrors} />
      {/* "failed" would be a claim the flow cannot make when the outcome is
          unknown — the write may well have landed. */}
      {error ? (
        <ImportErrorBanner
          title={indeterminate ? "Apply outcome unknown" : "Apply failed"}
          detail={error}
        />
      ) : null}
      {/* The way out of "unknown" is EVIDENCE, not a verdict. Re-planning the
          same roster reports whether the registry currently matches it; it
          cannot say which request made it match, so the flow never advances
          to Applied on this and the outcome stays unknown until the audit
          trail settles it. Repeatable, since the original POST may still be
          in flight when the operator first asks. */}
      {indeterminate ? (
        <div className="action-row">
          <button
            className="ghost-button"
            type="button"
            disabled={busy}
            onClick={onReconcile}
            title="Re-plan this roster against the registry to see whether the import landed"
          >
            {busy ? "Checking…" : "Check whether it landed"}
          </button>
        </div>
      ) : null}
      <PreviewActions
        hasErrors={hasErrors}
        onBack={onBack}
        onApply={onApply}
        busy={busy}
        applying={applying}
        indeterminate={indeterminate}
        otherApplyPending={otherApplyPending}
        scopeUnsettled={scopeUnsettled}
      />
    </div>
  );
};

type AppliedStepProps = {
  result: ChannelImportResult;
  reason: string;
  onDone: () => void;
};

/**
 * Step 3: the APPROVED PLAN's counts (non-zero only) + reason echo.
 *
 * The counts are deliberately labelled as the plan, not as the committed
 * result. The route returns the pre-write plan payload for an apply too
 * (channels.py builds it before calling apply_channel_import), while the
 * backend re-reads every row under its write-boundary lock and tallies what
 * it ACTUALLY wrote into the durable CHANNEL_IMPORTED audit event. A
 * concurrent writer between preview and that lock can turn a planned UPDATE
 * into a no-op — or let an UNCHANGED row heal real drift — so presenting this
 * tally as "what committed" would disagree with the audit trail. The note
 * below names the trail as the authority.
 */
const AppliedStep = ({ result, reason, onDone }: AppliedStepProps) => {
  return (
    <div className="confirm-panel" role="group" aria-label="Import applied">
      <div className="panel-title">
        <strong>Import applied</strong>
        <span>
          The roster for content owner {result.content_owner_id} is applied.
        </span>
      </div>
      <CountsStrip counts={result.counts} label="Approved plan" />
      <p className="muted" role="note">
        These are the counts of the plan you approved, not a re-read of the
        write. The backend re-checks every row under its write lock, so a
        concurrent edit can make a planned UPDATE a no-op; the durable record
        of what committed is the CHANNEL_IMPORTED audit event.
      </p>
      <p className="item-sub">{`Reason: ${reason}`}</p>
      <div className="action-row">
        <button className="primary-button" type="button" onClick={onDone}>
          Back to Registry
        </button>
      </div>
    </div>
  );
};

export type RegistryImportFlowProps = {
  /** Namespaces the unsettled-import records to one tenant + principal. MUST
   * match what RegistryView reads, or the flow's raise and the view's notice
   * would look at different buckets. */
  importScope?: string;
  /** False while `importScope` can still change under a write — the tenant half
   * is resolved asynchronously for sessions whose body carries no tenant.
   * Defaults to true: a caller that supplies no scope has no resolution to wait
   * on. */
  importScopeSettled?: boolean;
  /** Close the flow WITHOUT reloading the registry (no apply committed). */
  onCancel: () => void;
  /** Close the flow AND reload the registry (an apply committed, or leaving
   * Applied). */
  onDone: () => void;
};

type ImportStep = "upload" | "preview" | "applied";
const IMPORT_STEPS = ["Upload", "Preview", "Applied"];
const STEP_INDEX: Record<ImportStep, number> = { upload: 0, preview: 1, applied: 2 };

/**
 * The three-step Registry CSV import flow. Owns ALL flow state (step, file,
 * owner, reason, the dry-run plan, the applied result, busy/error);
 * RegistryView owns only whether the flow is open. A superseded-safe in-flight
 * ref latch collapses a same-tick double submit so one click burst fires one
 * request.
 */
export const RegistryImportFlow = ({
  importScope,
  importScopeSettled = true,
  onCancel,
  onDone,
}: RegistryImportFlowProps) => {
  const importChannels = useChannelImport();
  const ownerState = useContentOwners(YOUTUBE_ANALYTICS_KEY);
  const [step, setStep] = useState<ImportStep>("upload");
  const [file, setFile] = useState<File | null>(null);
  const [ownerId, setOwnerId] = useState("");
  const [reason, setReason] = useState("");
  const [preview, setPreview] = useState<ChannelImportResult | null>(null);
  const [applied, setApplied] = useState<ChannelImportResult | null>(null);
  const [busy, setBusy] = useState(false);
  // The WRITE half of `busy`. The exits key off this, never off `busy`:
  // abandoning a read-only dry-run is safe and the flow promises Cancel at any
  // step, so a slow (or never-settling) preview must not lock the operator in.
  // Only the apply commits, and only the apply cannot be taken back.
  const [applying, setApplying] = useState(false);
  // An apply whose outcome is UNKNOWN: the request was dispatched but no
  // response came back (transport failure), so the roster may well have
  // committed. Distinct from `error`, which reports a definite refusal the
  // backend actually answered with.
  const [indeterminate, setIndeterminate] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlightRef = useRef(false);

  // The exits this component renders are only half the story: the shell's
  // sidebar sits outside this tree and would unmount the flow regardless
  // (review #184). Armed IMPERATIVELY from the apply handler rather than from
  // an effect — an effect arms a commit late, leaving a window in which the
  // request is already running but the nav is still live. Release belongs to
  // the request's `finally`, which still runs if a boundary unmounts this flow.
  const navLatch = useWriteInFlightControl();
  // Raised BEFORE the apply is dispatched and cleared only once the response
  // establishes an outcome. The nav latch lives in this document; this one
  // survives it, which is what covers an operator who closes the tab, reloads,
  // or leaves via the sidebar instead of Cancel (review #184, codex P1 x2).
  const unsettledImport = useUnsettledImport(importScope);
  // The LATEST store binding, not the one captured when `apply` was defined.
  // A tenant that resolves mid-apply migrates the pending record to the new
  // scope, but the in-flight continuation still held a settleApply closed over
  // the PRE-migration scope — so a 2xx removed the now-empty old key and left
  // the migrated one behind, warning about a completed import and blocking the
  // next apply until the operator manually acknowledged it (review #184).
  // Settlement has to follow the id, so it reads through this ref.
  const unsettledRef = useRef(unsettledImport);
  unsettledRef.current = unsettledImport;
  // The id of the apply currently in flight, so the failure handler retires
  // exactly the request it is handling and never another tab's.
  const applyIdRef = useRef<string | null>(null);
  // The scope this apply was RECORDED under. Kept because the record does not
  // always end up where the current binding points: a tenant resolution adopts
  // it forward, and any other scope change leaves it behind.
  const admittedScopeRef = useRef<string | null>(null);
  // ==========================================================================
  // Purpose: Retire THIS apply's pending record — the one whose id is in
  //   flight — in every scope it could be sitting in. It is the only way the
  //   cross-document duplicate guard comes down for a write that has been
  //   answered.
  // Database/ORM: None (frontend) — removes localStorage keys and notifies.
  //   No request; what it clears is the client-side guard, never server state.
  // Standards: Settles in BOTH the current scope (where a tenant resolution
  //   would have adopted the record forward) and the scope the apply was
  //   RECORDED under (where any other scope change would have left it behind).
  //   Whichever does not hold the record is a no-op, because removal touches
  //   exactly one key — so doing both is safe and doing one is not. Reads the
  //   store through a ref rather than the render-time binding: settlement has
  //   to follow the ID, and the binding can have moved to a different scope
  //   since dispatch. Returns early on a null id, so a failure path that never
  //   dispatched cannot retire another tab's record.
  // Blast Radius: Whether the operator is blocked from their next import, and
  //   whether the duplicate guard still stands over a write that may still be
  //   committing. Both directions are bad: strand a completed apply and they
  //   are warned about a write that demonstrably landed and blocked until they
  //   acknowledge it; retire too early and the guard comes down over a request
  //   that has not been answered (review #184).
  // Connections:
  //   - File: frontend/src/contexts/UnsettledImportContext.tsx -> settleApply
  //       and settleApplyIn, the named-scope removals this pairs.
  //   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx ->
  //       handleApplyFailure, which calls this only on an ESTABLISHED
  //       rejection, never on an indeterminate one.
  // ==========================================================================
  const settleThisApply = () => {
    const applyId = applyIdRef.current;
    if (applyId === null) {
      return;
    }
    // Current scope first (where adoption would have carried it), then the one
    // that recorded it (where a non-adopting change would have left it). The
    // other is a no-op — removal touches exactly one key.
    unsettledRef.current.settleApply(applyId);
    const admitted = admittedScopeRef.current;
    if (admitted !== null) {
      settleApplyIn(admitted, applyId);
    }
  };

  const trimmedReason = reason.trim();

  /** Fire the read-only dry-run; on success advance to Preview, else stay + show. */
  const runDryRun = async () => {
    if (busy || inFlightRef.current) {
      return;
    }
    if (!canSubmitUpload(file, ownerId, reason)) {
      return;
    }
    inFlightRef.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await importChannels({
        file,
        contentOwnerId: ownerId,
        dryRun: true,
        reason: trimmedReason,
      });
      setPreview(result);
      setStep("preview");
    } catch (caught) {
      // Stay on Upload; the button re-enables in finally for a retry.
      setError(describeImportError(caught));
    } finally {
      setBusy(false);
      inFlightRef.current = false;
    }
  };

  // ==========================================================================
  // Purpose: Produce the VERDICT on an indeterminate apply — read a refreshed
  //   plan and choose the strongest claim it supports: the roster matches, it
  //   diverges, or its group effects cannot be checked at all.
  // Database/ORM: None (frontend) — pure over an already-fetched dry-run
  //   result; it issues no request and writes nothing. The read that produced
  //   its input belongs to reconcileIndeterminate.
  // Standards: NO AUTHORSHIP, in both directions, because a re-plan observes
  //   end state and nothing else. All-UNCHANGED proves the registry matches
  //   the roster, not that THIS request made it match — so the flow stays on
  //   Preview and points at the audit trail rather than advancing to Applied.
  //   Remaining CREATE/UPDATE work is the mirror image: it proves divergence,
  //   not failure, since a completed import followed by another writer's edit
  //   produces exactly this plan; the copy says "check again", never "it did
  //   not commit". Fails CLOSED on the third case — the backend labels group
  //   effects without loading membership, so a group-bearing roster's
  //   all-UNCHANGED inventory cannot speak for its group writes, and that is
  //   reported as unverifiable instead of being folded into the match branch.
  //   Every branch sets the banner only; none clears the pending record, which
  //   settles on the apply's own outcome.
  // Blast Radius: What the operator is told about a write whose outcome is
  //   unknown, and therefore whether they re-import. Over-claiming a match
  //   invites a duplicate CHANNEL_IMPORTED; over-claiming failure sends them
  //   to reconcile a registry that is already correct. No requests, no
  //   authorization meaning.
  // Connections:
  //   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx ->
  //       reconcileIndeterminate, the only caller, which supplies the dry run.
  //   - File: backend/ums_smart_revenue/org/channel_import.py -> the
  //       membership-free group labelling that makes the third branch
  //       necessary.
  // ==========================================================================
  /**
   * Turn a refreshed plan into the strongest claim it actually supports.
   *
   * NEITHER branch establishes authorship, and the copy for each says so.
   *
   * All-UNCHANGED means the registry matches the roster — the operationally
   * important fact — but not that this request is what made it match, so the
   * flow stays on Preview instead of advancing to Applied and points at the
   * audit trail.
   *
   * Remaining CREATE/UPDATE work is the mirror image and was wrong for the
   * same reason: it means the registry DIVERGES from the roster, not that the
   * write failed. A completed import followed by another writer's edit
   * produces exactly this plan. The note therefore says "check again" when it
   * may simply still be running, and never "it did not commit".
   */
  const settleFromReplan = (refreshed: ChannelImportResult) => {
    setPreview(refreshed);
    if (!rosterMatchesRegistry(refreshed)) {
      setError(
        hasUnverifiableGroupEffects(refreshed)
          ? RECONCILE_GROUPS_UNVERIFIABLE_NOTE
          : RECONCILE_PENDING_NOTE,
      );
      return;
    }
    setError(RECONCILE_MATCHES_NOTE);
  };

  /**
   * ============================================================================
   * Purpose: Settle an INDETERMINATE apply by re-planning the same roster, and
   *   report only what that evidence supports.
   * Database/ORM: None (frontend) — one READ-ONLY dry run via useChannelImport.
   *   It writes nothing, so checking costs the registry nothing.
   * Standards: Reports the END STATE, never authorship. The apply is one
   *   all-or-nothing transaction, so an all-UNCHANGED re-plan proves the
   *   registry matches the roster — but not that THIS request is what made it
   *   match, which is why the flow stays on Preview rather than advancing to
   *   Applied. Operator-triggered and REPEATABLE by design: the original POST
   *   may still be running, and a single automatic shot would race it into a
   *   false "did not commit". Fails closed when the roster file is gone rather
   *   than re-planning a different roster and calling the answer decisive.
   * Blast Radius: What the operator is told about an import whose outcome is
   *   unknown, and whether the flow leaves the indeterminate state. Apply stays
   *   disabled throughout, so no duplicate CHANNEL_IMPORTED is reachable here.
   * Connections:
   *   - File: frontend/src/lib/api/useChannelImport.ts -> the dry-run POST.
   *   - File: Docs/12_BACKEND_API_SPEC.md -> records the dry run as the
   *       reconciliation tool for a lost apply response.
   * ============================================================================
   */
  const reconcileIndeterminate = async () => {
    if (busy || inFlightRef.current) {
      return;
    }
    // Re-planning needs the same roster. The file survives the apply in this
    // closure, so this only trips if a future edit clears it — fail closed
    // rather than re-plan a DIFFERENT roster and call the answer decisive.
    if (file === null) {
      setError(RECONCILE_NO_ROSTER_NOTE);
      return;
    }
    inFlightRef.current = true;
    setBusy(true);
    try {
      settleFromReplan(
        await importChannels({
          file,
          contentOwnerId: ownerId,
          dryRun: true,
          reason: trimmedReason,
        }),
      );
    } catch (caught) {
      setError(`${RECONCILE_FAILED_PREFIX}${describeImportError(caught)} ${RECONCILE_STILL_UNKNOWN}`);
    } finally {
      setBusy(false);
      inFlightRef.current = false;
    }
  };

  // ==========================================================================
  // Purpose: Translate an apply failure into the THREE things it can mean, and
  //   settle the durable pending-apply record accordingly. Every failure lands
  //   in exactly one branch: a plan-bearing rejection, an indeterminate
  //   outcome, or a definite rejection.
  // Database/ORM: None (frontend). It writes no request; what it settles is
  //   the client-side duplicate-import guard over a write already answered.
  // Standards: BOTH plan-bearing statuses take the first branch, not just the
  //   422 an earlier version of this comment named — applyRaceDetail admits
  //   409 as well, and each means "the plan you saw is not the plan we would
  //   execute": 422 for a re-planned roster now holding ERROR rows, 409 for
  //   divergence from the approved fingerprint. Both are ESTABLISHED refusals
  //   at the boundary, so nothing committed and this apply is retired; the
  //   refreshed payload replaces the preview, which also re-binds the
  //   fingerprint so the next Apply sends the digest of what the operator is
  //   actually looking at. Ordering is load-bearing: the indeterminate test
  //   runs BEFORE the definite fallback, because only a failure that
  //   ESTABLISHES rejection may retire the record.
  // Blast Radius: Whether a roster can be submitted TWICE, and whether the
  //   operator is told the truth about a write they cannot see. Retiring the
  //   record on an indeterminate failure re-arms Apply over a roster that may
  //   have committed — a second unconditional CHANNEL_IMPORTED — and lets
  //   Cancel take the no-reload path over a changed registry. Holding the
  //   record on an established refusal strands the operator behind a guard for
  //   a write that provably did not happen.
  // Connections:
  //   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx ->
  //       applyRaceDetail, which decides what counts as plan-bearing, and
  //       settleThisApply, which retires the record in both scopes.
  //   - File: frontend/src/contexts/UnsettledImportContext.tsx -> the durable
  //       record this settles.
  //   - File: backend/ums_smart_revenue/api/channels.py -> the route emitting
  //       the 409/422 whose `detail` carries the refreshed plan.
  // ==========================================================================
  const handleApplyFailure = async (caught: unknown) => {
    const race = await applyRaceDetail(caught, ownerId);
    if (race) {
      // Replacing the preview also re-binds the fingerprint: the next Apply
      // sends the refreshed plan's digest, so approval always tracks what the
      // operator is actually looking at.
      setPreview(race);
      setError(refreshedPlanMessage((caught as ApiError).status));
      // A 409 is an established refusal: the write was rejected at the
      // boundary, so nothing committed and THIS apply is retired.
      settleThisApply();
      return;
    }
    // Anything that does not ESTABLISH rejection leaves the roster possibly
    // committed, audit event and all. Treating such a failure as definite
    // would re-arm Apply (a second unconditional CHANNEL_IMPORTED) and let
    // Cancel take the no-reload path over a changed registry. Only a reload
    // can settle it.
    if (isIndeterminateApplyFailure(caught)) {
      setIndeterminate(true);
      setError(
        "The import request did not return a usable result, so it may have " +
          "committed. Reload the registry to see the actual state before " +
          "importing again.",
      );
      return;
    }
    // Everything left is a DEFINITE rejection (the indeterminate branch above
    // returned already), so this write did not happen and its id comes down.
    settleThisApply();
    setError(describeImportError(caught));
  };

  // ==========================================================================
  // Purpose: Every reason a second apply dispatch must not start, in one
  //   place. Four independent conditions, each covering a window the others
  //   cannot see.
  // Database/ORM: None (frontend) — a predicate. It dispatches nothing; it
  //   decides whether POST /channels/import is reached, and therefore whether
  //   a second CHANNEL_IMPORTED can be appended.
  // Standards: A FUNCTION, not a render-time value. The `inFlightRef` read has
  //   to happen at CALL time — beating a same-tick double click is exactly
  //   what the ref is for, and a value captured during render is already
  //   stale. `busy` is the rendered state, `inFlightRef` the same-tick guard,
  //   `indeterminate` this document's lost-response lockout, and
  //   `unsettledImport.unsettled` the only one that can see ANOTHER TAB's
  //   pending apply. Dropping any one of the four leaves a live double-submit
  //   path.
  // Blast Radius: Whether one roster can be applied TWICE. A second dispatch
  //   is a second unconditional CHANNEL_IMPORTED over the same registry, and
  //   after a lost response it lands on a roster that may already have
  //   committed. No finance math here; it gates whether the audited write
  //   happens at all.
  // Connections:
  //   - File: frontend/src/contexts/UnsettledImportContext.tsx -> the
  //       cross-document flag behind `unsettled`, the cross-TAB half.
  //   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx ->
  //       handleApplyFailure, which raises `indeterminate`, and the apply
  //       handler that sets `busy` and `inFlightRef` in one batch.
  // ==========================================================================
  const applyDispatchBlocked = (): boolean => {
    // `unsettledImport.unsettled` covers what this component cannot see: a
    // SECOND TAB holding a preview of its own, whose apply is still pending.
    // `indeterminate` only knows about this document's request.
    return busy || inFlightRef.current || indeterminate || unsettledImport.unsettled;
  };

  /**
   * ============================================================================
   * Purpose: The ADMISSION PRECONDITION for the audited bulk import write.
   *   Every reason a dispatch stops before it starts, in one place: a namespace
   *   that can still change, a request already running, and a plan that is not
   *   applicable. Returns the approved file + plan to dispatch, or null having
   *   already told the operator whatever they need to know.
   * Database/ORM: None (frontend) — issues no request itself. It decides
   *   whether POST /channels/import is reached at all, and therefore whether a
   *   CHANNEL_IMPORTED audit event can be appended.
   * Standards: Ordered, and the order is load-bearing. The scope check is
   *   FIRST because it is a statement about the namespace every pending-write
   *   record below is filed under, not about this request: admitting while the
   *   scope can still change files the record in the missing-tenant bucket and
   *   then moves the bucket, leaving a dispatched apply that neither this tab's
   *   guard nor another tab's can find (review #184).
   *   Fails CLOSED in every branch — a reason to doubt is a reason not to
   *   dispatch, because the write it gates is unabortable and audited.
   *   Two REPORTING modes on purpose: the scope refusal sets operator-facing
   *   copy, because that state is transient and otherwise invisible and a
   *   silent no-op would read as a dead button; the dispatch-latch refusal is
   *   silent, because each of its reasons already has a visible explanation on
   *   the control the operator just used. Neither is a substitute for the
   *   backend's own guards: permissions, the plan fingerprint and the
   *   write-boundary pre-state check all remain the authority.
   *   Extracted from `apply` so that handler stays under the analyzer's
   *   complexity threshold (DeepSource JS-R1005) — conformed, not suppressed —
   *   and because the three really are one step: none touches the request, and
   *   all run before anything is latched.
   * Blast Radius: Whether a second audited bulk import can be dispatched, and
   *   whether a dispatched one is trackable at all. No authorization meaning
   *   and no revenue math; a mistake here shows as a refused or duplicated
   *   import, never as an unpermitted one.
   * Connections:
   *   - File: frontend/src/contexts/UnsettledImportContext.tsx -> the scope
   *       whose stability this waits on, and the store `apply` admits into.
   *   - File: frontend/src/components/srcc/AppShell.tsx -> resolves
   *       importScopeSettled from the session and the tenant bootstrap.
   *   - File: backend/ums_smart_revenue/api/channels.py -> import_channels,
   *       the route a passing preflight allows `apply` to reach.
   * ============================================================================
   */
  const preflightApply = (): { file: File; plan: ChannelImportResult } | null => {
    // FIRST, because this is a statement about the NAMESPACE every record below
    // is filed under, not about this request. Admitting while the scope can
    // still change writes the record into the missing-tenant bucket and then
    // moves the bucket, leaving a dispatched apply that neither this tab's
    // guard nor another tab's can find (review #184). Reported rather than
    // dropped: the state is transient and otherwise invisible, so a silent
    // no-op would read as a dead button.
    if (!importScopeSettled) {
      setError(IMPORT_SCOPE_UNSETTLED_NOTE);
      return null;
    }
    // Silent by contrast, and deliberately: each of its reasons already has a
    // visible explanation on the control the operator just used.
    if (applyDispatchBlocked()) {
      return null;
    }
    return approvedApply(file, preview);
  };

  /**
   * ============================================================================
   * Purpose: The frontend's WRITE DISPATCH boundary for the audited bulk
   *   import. It admits (or refuses) the request across documents, records the
   *   apply as unsettled before it leaves, binds it to the reviewed plan, sends
   *   it, and classifies the outcome as settled or indeterminate.
   * Database/ORM: None directly (frontend) — POSTs /channels/import with
   *   dry_run=false via useChannelImport. That route writes channel inventory,
   *   creates or joins CMS groups, and appends CHANNEL_IMPORTED. This is the
   *   only place in the UI that can cause any of it.
   * Standards: The ORDER of the opening steps is the contract, not a style
   *   choice. `inFlightRef` is set synchronously so a same-tick double click
   *   cannot pass; `navLatch.arm` batches with setApplying so the shell's nav
   *   and this flow's exits disable in ONE commit, leaving no window where the
   *   write is running and navigation is live; and admission is AWAITED before
   *   dispatch, so a second tab cannot slip a concurrent apply through the gap
   *   between reading the guard and recording the claim. A refusal writes
   *   nothing and sends nothing.
   *   `expected_plan_fingerprint` binds the write to the plan on screen, and
   *   `expected_display_digest` — the recomputable disclosed-plan companion
   *   (review #184, C1) — binds it again over exactly the fields the operator
   *   saw. Both are always sent, which also opts this caller into the
   *   backend's strict pre-state guard — the flow is asking for "the diff I
   *   reviewed, or none of it", not the unbound file-wins path.
   *   Failure classification is fail-CLOSED: only an ESTABLISHED rejection
   *   retires the apply's record. Anything else — a lost response, a gateway
   *   5xx, an unreadable body — stays INDETERMINATE, because the write may
   *   have committed and the flow must not re-arm Apply over it. Never retry
   *   from here; a blind second apply appends a second CHANNEL_IMPORTED.
   * Blast Radius: Channel-registry inventory, CMS group membership, and the
   *   CHANNEL_IMPORTED audit trail — via the backend's own guarded, audited
   *   route, whose permission checks and fingerprint guard remain the
   *   authority. No revenue math; the locked-month guard rejects
   *   revenue_required flips server-side.
   * Connections:
   *   - File: frontend/src/contexts/UnsettledImportContext.tsx -> admit()
   *       (cross-document admission) and settleApply().
   *   - File: frontend/src/contexts/WriteInFlightContext.tsx -> the shell nav
   *       latch armed for the duration of the request.
   *   - File: frontend/src/lib/api/useChannelImport.ts -> the POST and the
   *       payload validation every response passes through.
   *   - File: backend/ums_smart_revenue/api/channels.py -> import_channels,
   *       the route this dispatches to.
   * ============================================================================
   */
  const apply = async () => {
    const approved = preflightApply();
    if (approved === null) {
      return;
    }
    inFlightRef.current = true;
    setBusy(true);
    setApplying(true);
    // Same event handler, so this batches with setApplying: the shell's nav
    // and this flow's exits disable in ONE commit, leaving no window in which
    // the request is running but navigation is still live.
    navLatch.arm(APPLY_IN_FLIGHT_NOTE);
    // BEFORE the request leaves. From here the outcome is unknown by default,
    // and it stays unknown until something establishes otherwise — including
    // across a tab close or a reload, which discards this document's fetch
    // handler while the backend goes on committing.
    const applyId = newApplyId();
    applyIdRef.current = applyId;
    setError(null);
    try {
      // ATOMIC admission, not the render-time `unsettled` read. That read and
      // the record were two steps, so two tabs could both see "nothing
      // pending" and both dispatch; this checks and records under one
      // cross-document lock. A refusal writes nothing and sends no request.
      const admission = await admitForApply(unsettledImport.admit, applyId);
      if (!admission.admitted) {
        applyIdRef.current = null;
        admittedScopeRef.current = null;
        setError(admission.note);
        return;
      }
      // The claim was made under THIS scope; remember it so settlement can
      // reach the record even if the namespace moves under the request.
      admittedScopeRef.current = importScope ?? UNSCOPED_IMPORT_SCOPE;
      const result = await importChannels({
        file: approved.file,
        contentOwnerId: ownerId,
        dryRun: false,
        reason: trimmedReason,
        // Binds this write to the exact plan on screen. The route re-plans
        // from current state, so without it a row reviewed as CREATE could
        // commit as an UPDATE over a channel created since the preview.
        expectedPlanFingerprint: approved.plan.plan_fingerprint,
        // The disclosed-plan half of the same binding (review #184, C1): the
        // digest a client can recompute from the rendered plan, checked
        // independently by the route. Both tokens come from the SAME approved
        // plan object, so they can never bind to two different previews.
        expectedDisplayDigest: approved.plan.display_digest,
      });
      // A 2xx settles THIS apply: the write committed and the flow can say so.
      settleThisApply();
      setApplied(result);
      setStep("applied");
    } catch (caught) {
      // Stay on Preview; the Apply button re-enables in finally for a retry.
      await handleApplyFailure(caught);
    } finally {
      setBusy(false);
      setApplying(false);
      navLatch.release();
      inFlightRef.current = false;
    }
  };

  const backToUpload = () => {
    setError(null);
    setStep("upload");
  };

  // ==========================================================================
  // Purpose: Route Cancel to the exit that matches what the registry now
  //   holds — `onDone` when it may have changed under this flow, `onCancel`
  //   when it provably has not.
  // Database/ORM: None (frontend) — chooses between two parent callbacks. The
  //   difference between them is whether the parent REFETCHES.
  // Standards: `indeterminate` joins `applied` on the reloading path: an apply
  //   whose response was lost may have committed, so leaving without a refetch
  //   would restore a registry that no longer matches the database.
  //   Unreachable while `applying`, because ActionStepper disables the control
  //   — which is what keeps the `applied`-null case honest: without that guard
  //   a Cancel fired mid-apply would take the no-reload path while the request
  //   went on to commit. The reload it triggers can race the original POST and
  //   return pre-write rows; that is deliberately NOT signalled through this
  //   callback, because the shell-level unsettled flag was raised before
  //   dispatch and is still up, so the warning is already correct however the
  //   operator leaves — including by the sidebar, which never reaches this
  //   handler at all.
  // Blast Radius: Whether the operator is returned to a STALE registry after a
  //   write that may have landed. No request and no write of its own; it
  //   decides what the parent believes about state it did not observe (review
  //   #184, codex P1).
  // Connections:
  //   - File: frontend/src/components/srcc/views/RegistryView.tsx -> the
  //       onDone / onCancel pair whose difference is the refetch.
  //   - File: frontend/src/components/srcc/ActionStepper.tsx -> the control
  //       this handler backs, disabled while `applying`.
  //   - File: frontend/src/contexts/UnsettledImportContext.tsx -> the
  //       shell-level warning that stays correct on every exit path.
  // ==========================================================================
  const handleCancel = () => {
    // `indeterminate` joins `applied` on the reloading path: an apply whose
    // response was lost may have committed, and leaving without a refetch
    // would restore a registry that no longer matches the database. It exits
    // The reload it triggers races the original POST and can return pre-write
    // rows. That is NOT signalled through this callback: the shell-level
    // unsettled flag was raised before the request was dispatched and is still
    // up, so the warning is already correct however the operator leaves —
    // including by the sidebar, which never reaches this handler at all
    // (review #184, codex P1).
    if (applied || indeterminate) {
      onDone();
    } else {
      onCancel();
    }
  };

  /**
   * The active step's panel. Exactly one step renders; a step whose result has
   * not arrived yet renders nothing — the same shape as the sync flow's body.
   * A plain helper (invoked, not mounted as a component) so the flow's state
   * stays in this closure and the element tree is unchanged.
   */
  const renderStepBody = (): ReactNode => {
    if (step === "upload") {
      return (
        <UploadStep
          ownerState={ownerState}
          ownerId={ownerId}
          onOwnerChange={setOwnerId}
          file={file}
          onFileChange={setFile}
          reason={reason}
          onReasonChange={setReason}
          onRun={runDryRun}
          busy={busy}
          error={error}
        />
      );
    }
    if (step === "preview") {
      return preview ? (
        <PreviewStep
          result={preview}
          onBack={backToUpload}
          onApply={apply}
          onReconcile={reconcileIndeterminate}
          busy={busy}
          applying={applying}
          indeterminate={indeterminate}
          otherApplyPending={unsettledImport.unsettled}
          scopeUnsettled={!importScopeSettled}
          error={error}
        />
      ) : null;
    }
    return applied ? (
      <AppliedStep result={applied} reason={trimmedReason} onDone={onDone} />
    ) : null;
  };

  return (
    <ActionStepper
      steps={IMPORT_STEPS}
      activeIndex={STEP_INDEX[step]}
      onCancel={handleCancel}
      cancelDisabledReason={applying ? APPLY_IN_FLIGHT_NOTE : undefined}
      // On Applied the write has COMMITTED: channel, group-membership and
      // audit rows are durable and nothing here can roll them back. A button
      // labelled Cancel would misstate that outcome — it only reloads an
      // already-mutated registry — so the step's own "Back to Registry"
      // action is the only exit offered.
      cancelHidden={step === "applied"}
    >
      {renderStepBody()}
    </ActionStepper>
  );
};
