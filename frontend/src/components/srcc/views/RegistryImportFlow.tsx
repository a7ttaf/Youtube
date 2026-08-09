import { type ReactNode, useRef, useState } from "react";

import { useWriteInFlightControl } from "@/contexts/WriteInFlightContext";
import { ApiError } from "@/lib/api/client";
import { describeApiError } from "@/lib/api/errors";
import type {
  ChannelImportFieldChange,
  ChannelImportResult,
  ChannelImportRowResult,
} from "@/lib/api/types";
import { useChannelImport } from "@/lib/api/useChannelImport";
import { useContentOwners } from "@/lib/api/useContentOwners";
import type { Severity } from "@/lib/mock/data";
import { ActionStepper } from "../ActionStepper";
import { OutcomeTable, type OutcomeTableRow } from "../OutcomeTable";
import { Badge } from "../shared";
import { isValidAuditReason } from "./GroupsSyncFlow";

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
//   and failure alike, and the latch also releases on unmount, so nothing
//   traps the operator or strands the shell.
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

/** Structural check that an unknown 422 `detail` is the refreshed import plan
 * the apply race returns (channels.py raises the full payload as `detail`). */
const isImportResultPayload = (detail: unknown): detail is ChannelImportResult => {
  if (typeof detail !== "object" || detail === null) {
    return false;
  }
  return Array.isArray((detail as { rows?: unknown }).rows);
};

/**
 * Extract the refreshed plan from a 422 apply race, or null for every other
 * failure. The backend 422s an apply whose RE-PLANNED roster holds ERROR rows
 * and ships that full plan as the error `detail` — the one shape this reads.
 */
const applyRaceDetail = (err: unknown): ChannelImportResult | null => {
  if (!(err instanceof ApiError) || err.status !== 422) {
    return null;
  }
  const body = err.body as { detail?: unknown } | null;
  const detail = body?.detail;
  if (isImportResultPayload(detail)) {
    return detail;
  }
  return null;
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
 * ERROR rows (the API 422s an erroring plan — all-or-nothing). A type
 * predicate on `file` so the apply closure gets the non-null File.
 */
const canApplyImport = (
  file: File | null,
  preview: ChannelImportResult | null,
): file is File => {
  return file !== null && preview !== null && !hasErrorRows(preview);
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
      revenueFlagLabel(row.revenue_required),
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
};

/**
 * The content-owner picker, fed by the least-privilege GET
 * /connectors/content-owners read (ACTIVE youtube-analytics account ids only,
 * MANAGE_GROUPS-gated — a permission every canImportChannels holder has).
 * Disabled with a Connectors pointer while empty, loading, or failed.
 */
const OwnerField = ({ ownerState, ownerId, onOwnerChange }: OwnerFieldProps) => {
  const owners = ownerState.data?.items.map((item) => item.account_id) ?? [];
  const note = ownerPickerNote(ownerState, owners.length);
  return (
    <div className="field-row">
      <label htmlFor="importOwner">Content owner</label>
      <select
        id="importOwner"
        value={ownerId}
        disabled={owners.length === 0}
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
          onChange={(event) => onFileChange(event.target.files?.[0] ?? null)}
        />
        {file ? <span className="muted">Selected: {file.name}</span> : null}
      </div>
      <OwnerField
        ownerState={ownerState}
        ownerId={ownerId}
        onOwnerChange={onOwnerChange}
      />
      <div className="field-row">
        <label htmlFor="importReason">Reason (required, audited)</label>
        <input
          id="importReason"
          value={reason}
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

/** Apply's disabled title: why the button is refused, or undefined when live. */
const applyBlockedTitle = (
  hasErrors: boolean,
  indeterminate: boolean,
): string | undefined => {
  if (indeterminate) {
    return APPLY_INDETERMINATE_NOTE;
  }
  if (hasErrors) {
    return "The API refuses plans with error rows (422)";
  }
  return undefined;
};

type PreviewActionsProps = {
  hasErrors: boolean;
  onBack: () => void;
  onApply: () => void;
  busy: boolean;
  applying: boolean;
  indeterminate: boolean;
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
 * the attempt was abandoned, and its setState would land on the Preview step
 * it had left. A read-only dry-run carries none of that risk and stays
 * abandonable. `applying` clears in the apply's `finally` on success AND
 * failure, so this never traps anyone.
 */
const PreviewActions = ({
  hasErrors,
  onBack,
  onApply,
  busy,
  applying,
  indeterminate,
}: PreviewActionsProps) => {
  return (
    <div className="action-row">
      <button
        className="ghost-button"
        type="button"
        disabled={applying}
        title={applying ? APPLY_IN_FLIGHT_NOTE : undefined}
        onClick={onBack}
      >
        Back
      </button>
      <button
        className="primary-button"
        type="button"
        disabled={hasErrors || busy || indeterminate}
        title={applyBlockedTitle(hasErrors, indeterminate)}
        onClick={onApply}
      >
        {busy ? "Applying…" : "Apply"}
      </button>
    </div>
  );
};

type PreviewStepProps = {
  result: ChannelImportResult;
  onBack: () => void;
  onApply: () => void;
  busy: boolean;
  applying: boolean;
  indeterminate: boolean;
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
  busy,
  applying,
  indeterminate,
  error,
}: PreviewStepProps) => {
  const hasErrors = hasErrorRows(result);
  const rows = result.rows.map(importOutcomeRow);
  return (
    <div className="confirm-panel" role="group" aria-label="Import preview">
      <div className="panel-title">
        <strong>Review the plan</strong>
        <span>Roster plan for content owner {result.content_owner_id}.</span>
      </div>
      <CountsStrip counts={result.counts} />
      <OutcomeTable
        columns={IMPORT_COLUMNS}
        rows={rows}
        emptyLabel="No rows in this roster."
      />
      <ErrorRowsNote show={hasErrors} />
      {error ? <ImportErrorBanner title="Apply failed" detail={error} /> : null}
      <PreviewActions
        hasErrors={hasErrors}
        onBack={onBack}
        onApply={onApply}
        busy={busy}
        applying={applying}
        indeterminate={indeterminate}
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
export const RegistryImportFlow = ({ onCancel, onDone }: RegistryImportFlowProps) => {
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
  // request is already running but the nav is still live. It also releases on
  // unmount, so a teardown mid-request cannot strand the shell.
  const navLatch = useWriteInFlightControl();

  const trimmedReason = reason.trim();

  /** Fire the read-only dry-run; on success advance to Preview, else stay + show. */
  const runDryRun = async () => {
    if (busy || inFlightRef.current) return;
    if (!canSubmitUpload(file, ownerId, reason)) return;
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

  /**
   * Apply-failure translation: the 422 apply race (a concurrent editor changed
   * the registry between preview and apply) replaces the stale plan with the
   * refreshed payload the backend ships as `detail`, so the operator reviews
   * reality; every other failure surfaces as banner copy only.
   */
  const handleApplyFailure = (caught: unknown) => {
    const race = applyRaceDetail(caught);
    if (race) {
      setPreview(race);
      setError(
        "The registry changed since this preview; the refreshed plan below " +
          "shows the ERROR rows that blocked the apply.",
      );
      return;
    }
    // The client only raises ApiError once an HTTP response exists, so
    // anything else means the POST was dispatched and never answered — the
    // roster may already be committed, audit event and all. Treating that as
    // a plain failure would re-arm Apply (a second unconditional
    // CHANNEL_IMPORTED) and let Cancel take the no-reload path over a changed
    // registry. It is INDETERMINATE, and only a reload can settle it.
    if (!(caught instanceof ApiError)) {
      setIndeterminate(true);
      setError(
        "The import request was sent but no response came back, so it may " +
          "have committed. Reload the registry to see the actual state " +
          "before importing again.",
      );
      return;
    }
    setError(describeImportError(caught));
  };

  /**
   * Refuse a second apply dispatch. A FUNCTION, not a render-time value: the
   * `inFlightRef` read has to happen at call time, since beating a same-tick
   * double click is exactly what the ref is for and a value captured during
   * render would already be stale. `indeterminate` joins it because a blind
   * retry after a lost response could double-submit a roster that already
   * committed.
   */
  const applyDispatchBlocked = (): boolean => {
    return busy || inFlightRef.current || indeterminate;
  };

  /** Commit the plan; refuse while any row errors (the API 422s it anyway). */
  const apply = async () => {
    if (applyDispatchBlocked()) return;
    if (!canApplyImport(file, preview)) return;
    inFlightRef.current = true;
    setBusy(true);
    setApplying(true);
    // Same event handler, so this batches with setApplying: the shell's nav
    // and this flow's exits disable in ONE commit, leaving no window in which
    // the request is running but navigation is still live.
    navLatch.arm(APPLY_IN_FLIGHT_NOTE);
    setError(null);
    try {
      const result = await importChannels({
        file,
        contentOwnerId: ownerId,
        dryRun: false,
        reason: trimmedReason,
      });
      setApplied(result);
      setStep("applied");
    } catch (caught) {
      // Stay on Preview; the Apply button re-enables in finally for a retry.
      handleApplyFailure(caught);
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

  // Cancel restores the registry WITHOUT a refetch — unless an apply already
  // committed, in which case the inventory changed and the parent must reload.
  // It is unreachable while `applying` (ActionStepper disables the control),
  // which is what keeps that `applied`-null test honest: without the guard, a
  // Cancel fired mid-apply would take the no-reload path even though the
  // request goes on to commit, and the parent would restore a stale registry.
  const handleCancel = () => {
    // `indeterminate` joins `applied` on the reloading path: an apply whose
    // response was lost may have committed, and leaving without a refetch
    // would restore a registry that no longer matches the database.
    if (applied || indeterminate) onDone();
    else onCancel();
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
          busy={busy}
          applying={applying}
          indeterminate={indeterminate}
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
