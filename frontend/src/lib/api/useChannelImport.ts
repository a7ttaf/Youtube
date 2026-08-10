import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { ChannelImportResult } from "@/lib/api/types";

// ============================================================================
// Purpose: Imperative action hook for the Registry CSV import stepper: one
//   stable callback POSTing a roster CSV to /channels/import as multipart form
//   data, serving both the dry-run preview and the apply (the dry_run flag is
//   the only difference). camelCase hook args map to the backend's snake_case
//   wire form fields at this boundary — the deliberate frontend/backend casing
//   seam.
// Database/ORM: None (frontend) — calls the backend import endpoint.
// Standards: The FormData carries `file`, `content_owner_id`, `dry_run`
//   ("true"/"false"), and `reason`; `cms_status` is OMITTED so the backend
//   default (INSIDE_CMS) applies. useApiClient passes FormData through
//   verbatim (isRawBodyInit) with no JSON Content-Type, so fetch sets the
//   multipart boundary itself. Errors propagate as the typed ApiError (403 =
//   missing MANAGE_CHANNELS, or missing MANAGE_GROUPS on a Group_ID-bearing
//   roster; 422 = malformed upload/form OR an apply attempted while the plan
//   holds ERROR rows — that 422's `detail` is the full ChannelImportResult
//   payload; 409 = a plan-to-apply race or locked-month rejection) for the
//   calling view to translate. No client-side authorization is invented here;
//   the backend's own guarded, audited route stays the authority. This hook
//   adds NO error handling of its own — the calling view owns busy/error
//   presentation (the GroupsSyncFlow pattern).
// Blast Radius: Channel-registry inventory + row-created group membership
//   (write path) — but only via the backend's own guarded, audited route. No
//   revenue math, no allocation, no month-close (the backend's locked-month
//   guard rejects revenue_required flips at apply time).
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() POST +
//       X-UMS-Tenant; withJsonBody passes FormData through untouched.
//   - File: frontend/src/lib/api/types.ts -> ChannelImportResult.
//   - File: backend/ums_smart_revenue/api/channels.py:639 import_channels
//       -> POST /channels/import.
// ============================================================================

/**
 * A JSON OBJECT, excluding null and arrays — both of which `typeof` calls
 * "object" and both of which then throw where a record is indexed.
 */
const isPlainObject = (value: unknown): value is Record<string, unknown> => {
  return typeof value === "object" && value !== null && !Array.isArray(value);
};

/**
 * The DECLARED literal sets, mirroring ChannelImportOutcome and
 * ChannelImportGroupAction (backend org/channel_import.py). Accepting any
 * string here is not a smaller check, it is a different one: an unknown
 * `group_action` falls past GroupCell's label lookup and the preview goes
 * SILENT about whether the apply mints a new finance-scope SECTOR group or
 * joins an existing one — the single most consequential thing a Group_ID row
 * does, and the reason that column exists at all (review #184).
 */
const PLAN_OUTCOMES = ["CREATE", "UPDATE", "UNCHANGED", "ERROR"] as const;
const GROUP_ACTIONS = ["CREATE", "JOIN"] as const;

const isOutcome = (value: unknown): boolean => {
  return typeof value === "string" && PLAN_OUTCOMES.some((outcome) => outcome === value);
};

const isGroupAction = (value: unknown): boolean => {
  return value === null || GROUP_ACTIONS.some((action) => action === value);
};

/**
 * `counts` is a tally, so every value must be a count: a finite, non-negative
 * INTEGER. Accepting any plain object let a malformed or legacy payload put a
 * string, a float, a negative, or NaN in there, and CountsStrip coerces during
 * `value > 0` — so `"3"` renders as a plan total and `-1` or `NaN` silently
 * hides an outcome the operator needed to see, on a payload that stays
 * applicable (review #184). Unknown keys are rejected too: a count the strip
 * has no label for is a plan shape this UI does not understand.
 */
const isCountMap = (value: unknown): boolean => {
  if (!isPlainObject(value)) {
    return false;
  }
  const entries = Object.entries(value);
  // EXACTLY the declared outcomes. `every` alone is vacuously true for `{}`
  // and happily accepts a partial map, and the backend emits all four
  // unconditionally (`{outcome.value: 0 for outcome in ChannelImportOutcome}`)
  // — so a missing key is a payload it cannot have produced, and CountsStrip
  // would silently omit a total the operator is entitled to, including on the
  // Applied screen (review #184).
  if (entries.length !== PLAN_OUTCOMES.length) {
    return false;
  }
  return entries.every(
    ([outcome, count]) =>
      PLAN_OUTCOMES.some((declared) => declared === outcome) &&
      typeof count === "number" &&
      Number.isInteger(count) &&
      count >= 0,
  );
};

/**
 * The value types a diff side can hold. The backend only ever puts inventory
 * scalars here — names and cms_status are strings, revenue_required is a
 * boolean, content_owner_id is nullable — so anything else is a payload the
 * preview cannot render honestly. Checking only for the KEYS let
 * `{from: {}, to: "INSIDE_CMS"}` through, which the diff cell then rendered as
 * the unreviewable "[object Object] → INSIDE_CMS" (review #184).
 */
const isChangeSide = (value: unknown): boolean => {
  return value === null || typeof value === "string" || typeof value === "boolean";
};

const isFieldChange = (value: unknown): boolean => {
  if (!isPlainObject(value)) {
    return false;
  }
  return "from" in value && "to" in value && isChangeSide(value.from) && isChangeSide(value.to);
};

/**
 * `changes` must be a RECORD of from/to pairs, not merely a non-null object.
 * An array, or `{cms_status: null}`, satisfies `typeof === "object"` and then
 * throws downstream where the renderer reads `change.from` (review #184).
 */
const isChangeMap = (value: unknown): boolean => {
  if (!isPlainObject(value)) {
    return false;
  }
  return Object.values(value).every(isFieldChange);
};

const isNullableString = (value: unknown): boolean => {
  return value === null || typeof value === "string";
};

/**
 * `revenue_source_status` is null (the write left the classification alone) or
 * a from/to pair whose `to` is always present — the backend cannot emit a
 * change that moves the row to nothing. Named rather than inlined into the
 * field table so the predicate stays under the medium-risk complexity
 * threshold (DeepSource JS-R1005).
 */
const isSourceStatusChange = (value: unknown): boolean => {
  if (value === null) {
    return true;
  }
  if (!isPlainObject(value)) {
    return false;
  }
  return isNullableString(value.from) && typeof value.to === "string";
};

/**
 * EVERY field the preview renders, not just the ones it indexes into. A
 * nullable field carrying an OBJECT is the case an allowlist of the indexed
 * three still let through: `group_id: {}` is not null, so GroupCell falls past
 * its null branch and renders the object as a React child, which throws
 * (review #184). Nullable is about ABSENCE, not "anything goes".
 */
const PLAN_ROW_FIELDS: ReadonlyArray<readonly [string, (value: unknown) => boolean]> = [
  ["row_number", (value) => typeof value === "number"],
  ["outcome", isOutcome],
  ["changes", isChangeMap],
  ["youtube_channel_id", isNullableString],
  ["channel_name", isNullableString],
  ["group_id", isNullableString],
  ["group_action", isGroupAction],
  ["reason", isNullableString],
  ["revenue_required", (value) => value === null || typeof value === "boolean"],
  ["revenue_source_status", isSourceStatusChange],
];

/**
 * `group_action` is non-null EXACTLY when `group_id` is. Field-by-field checks
 * cannot see that: `{outcome: "UPDATE", group_id: "g1", group_action: null}`
 * passes each of them individually, GroupCell then renders the bare key, and
 * Apply stays enabled over a row whose finance-scope effect — mint a new
 * SECTOR group, or join an existing one — was never disclosed. The backend
 * reserves null for rows with no group and for ERROR rows, which carry no
 * group key either, so the relation is a biconditional (review #184).
 */
const hasConsistentGroupEffect = (row: Record<string, unknown>): boolean => {
  return (row.group_id === null) === (row.group_action === null);
};

const isPlanRow = (row: unknown): boolean => {
  if (!isPlainObject(row)) {
    return false;
  }
  return (
    PLAN_ROW_FIELDS.every(([field, isValid]) => isValid(row[field])) &&
    hasConsistentGroupEffect(row)
  );
};

/**
 * The fields a plan payload must carry. `plan_fingerprint` is the SECURITY
 * one: a plan accepted without it reaches the next Apply as `undefined`,
 * which omits `expected_plan_fingerprint` from the form and silently
 * DOWNGRADES the write to the backend's unbound, file-wins path — no
 * fingerprint compare and no write-boundary pre-state guard, under a request
 * the operator believes is still bound to the plan on screen (review #184).
 *
 * The header fields are here for the same reason the row fields are:
 * `content_owner_id` and `cms_status` are RENDERED by PreviewStep and
 * AppliedStep, so a payload carrying `content_owner_id: {}` would pass a
 * rows-only check and then throw inside React — and after an apply, that throw
 * lands where the write may already have committed, bypassing the
 * indeterminate handling that exists precisely for that case.
 */
const PLAN_PAYLOAD_FIELDS: ReadonlyArray<readonly [string, (value: unknown) => boolean]> = [
  ["rows", (value) => Array.isArray(value) && value.every(isPlanRow)],
  ["counts", isCountMap],
  ["plan_fingerprint", (value) => typeof value === "string" && value !== ""],
  ["content_owner_id", (value) => typeof value === "string"],
  ["cms_status", (value) => typeof value === "string"],
  ["dry_run", (value) => typeof value === "boolean"],
];

// ============================================================================
// Purpose: The typed boundary between an untrusted HTTP body and trusted UI
//   state. Nothing may replace the plan on screen — not a 200 apply result,
//   not the refreshed plan inside a 409/422 `detail` — without passing here.
// Database/ORM: None (frontend) — a pure structural predicate over a decoded
//   JSON body. It issues no request and reads no state.
// Standards: Shared by BOTH directions on purpose. `client.post` only CASTS
//   the body to its type parameter, so a successful 200 is exactly as
//   unverified as a rejection payload, and a legacy or malformed success body
//   is not a smaller version of a plan. Fails CLOSED: an unrecognised payload
//   raises ChannelImportShapeError rather than being coerced, which routes an
//   apply into the flow's indeterminate handling instead of letting it look
//   like a clean result. Checks every field the UI renders or indexes, not
//   just the typed few — a nullable field carrying an object passes a
//   "nullable" check and then throws inside React.
// Blast Radius: Whether the next Apply stays FINGERPRINT-BOUND. A plan
//   accepted without `plan_fingerprint` reaches Apply as `undefined`, which
//   omits `expected_plan_fingerprint` from the form and silently downgrades
//   the audited bulk write to the backend's unbound, file-wins path — no
//   fingerprint compare and no write-boundary pre-state guard — under a
//   request the operator believes is still bound to the plan on screen. It
//   also decides whether a malformed body crashes the renderer at a point
//   where the write may already have committed.
// Connections:
//   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx ->
//       the only consumer of a plan; renders every field checked here.
//   - File: backend/ums_smart_revenue/api/channels.py -> emits the plan and
//       computes plan_fingerprint over the disclosed payload.
//   - File: Docs/12_BACKEND_API_SPEC.md -> the plan contract this mirrors.
// ============================================================================
export const isChannelImportResult = (payload: unknown): payload is ChannelImportResult => {
  if (typeof payload !== "object" || payload === null) {
    return false;
  }
  const candidate = payload as Record<string, unknown>;
  return PLAN_PAYLOAD_FIELDS.every(([field, isValid]) => isValid(candidate[field]));
};

/** Thrown when the backend answers 2xx with something that is not a plan. */
export class ChannelImportShapeError extends Error {
  constructor() {
    super(
      "The import responded with an unrecognised result, so the plan could " +
        "not be read.",
    );
    this.name = "ChannelImportShapeError";
  }
}

export const useChannelImport = (): ((
  args: {
    file: File;
    contentOwnerId: string;
    dryRun: boolean;
    reason: string;
    /**
     * The `plan_fingerprint` of the dry run the operator approved. The apply
     * re-plans from CURRENT state, so sending it binds the write to the plan
     * that was actually reviewed: the backend 409s on divergence and returns
     * the refreshed plan. Omitted on the dry run itself (nothing to bind to).
     */
    expectedPlanFingerprint?: string;
  },
) => Promise<ChannelImportResult>) => {
  const client = useApiClient();
  return useCallback(
    ({ file, contentOwnerId, dryRun, reason, expectedPlanFingerprint }) => {
      const form = new FormData();
      form.append("file", file);
      form.append("content_owner_id", contentOwnerId);
      form.append("dry_run", dryRun ? "true" : "false");
      form.append("reason", reason);
      if (expectedPlanFingerprint !== undefined) {
        form.append("expected_plan_fingerprint", expectedPlanFingerprint);
      }
      return client
        .post<ChannelImportResult>("/channels/import", form)
        .then((result) => {
          // A 2xx is not a promise about SHAPE — client.post casts, it does
          // not validate. Rejecting here keeps a malformed dry run a
          // read-only failure, and routes a malformed apply into the flow's
          // INDETERMINATE path (this is not an ApiError, so it is not on the
          // definite-rejection list) — which is right: the write may well
          // have committed, only the body was unusable.
          if (!isChannelImportResult(result)) {
            throw new ChannelImportShapeError();
          }
          // The MODE must match what was asked for. A structural check only
          // proves `dry_run` is a boolean, so a malformed or legacy apply
          // response carrying `dry_run: true` passed it — and the flow then
          // advanced to Applied and told the operator the import committed,
          // on a body that identifies itself as a preview (review #184).
          // Treated as unusable rather than coerced, which routes an apply
          // into the indeterminate path where an unreadable outcome belongs.
          if (result.dry_run !== dryRun) {
            throw new ChannelImportShapeError();
          }
          // And it must be the plan we BOUND to. A stale, misrouted or
          // legacy-server 2xx can be structurally perfect and describe a
          // different plan entirely; accepting it lets the flow clear the
          // unsettled record and present an unrelated payload as the approved
          // one. The route returns the same digest it compared against on
          // success (channels.py: the 409 fires when they differ), so an
          // inequality here is never a legitimate response — it goes down the
          // indeterminate path, because the write may still have happened
          // (review #184).
          if (
            expectedPlanFingerprint !== undefined &&
            result.plan_fingerprint !== expectedPlanFingerprint
          ) {
            throw new ChannelImportShapeError();
          }
          return result;
        });
    },
    [client],
  );
};
