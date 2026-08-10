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
 * The per-ROW fields any consumer of a plan INDEXES into rather than merely
 * renders, each with the check it must pass.
 *
 *   `row_number` — the preview's React key; a missing one collides.
 *   `outcome`    — selects the row chip and the ERROR tone.
 *   `changes`    — a RECORD of from/to pairs; the renderer reads both
 *                  sides of every value, so an array or a null entry
 *                  throws just as surely as a missing map.
 */
const isFieldChange = (value: unknown): boolean => {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  return "from" in value && "to" in value;
};

/**
 * `changes` must be a RECORD of from/to pairs, not merely a non-null object.
 * An array, or `{cms_status: null}`, satisfies `typeof === "object"` and then
 * throws downstream where the renderer reads `change.from` (review #184).
 */
/**
 * A JSON OBJECT, excluding null and arrays — both of which `typeof` calls
 * "object" and both of which then throw where a record is indexed.
 */
const isPlainObject = (value: unknown): value is Record<string, unknown> => {
  return typeof value === "object" && value !== null && !Array.isArray(value);
};

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
  ["outcome", (value) => typeof value === "string"],
  ["changes", isChangeMap],
  ["youtube_channel_id", isNullableString],
  ["channel_name", isNullableString],
  ["group_id", isNullableString],
  ["group_action", isNullableString],
  ["reason", isNullableString],
  ["revenue_required", (value) => value === null || typeof value === "boolean"],
  ["revenue_source_status", isSourceStatusChange],
];

const isPlanRow = (row: unknown): boolean => {
  if (typeof row !== "object" || row === null) {
    return false;
  }
  const candidate = row as Record<string, unknown>;
  return PLAN_ROW_FIELDS.every(([field, isValid]) => isValid(candidate[field]));
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
  ["counts", isPlainObject],
  ["plan_fingerprint", (value) => typeof value === "string" && value !== ""],
  ["content_owner_id", (value) => typeof value === "string"],
  ["cms_status", (value) => typeof value === "string"],
  ["dry_run", (value) => typeof value === "boolean"],
];

/**
 * Structural check that an unknown payload is a usable import plan.
 *
 * Shared by BOTH directions on purpose: the rejection `detail` a 409/422
 * carries, and — since `client.post` only CASTS the body to its type
 * parameter — every successful 200 as well. A legacy or malformed success
 * body is not a smaller version of a plan; trusting one is what lets an
 * unbound apply through the front door.
 */
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
          return result;
        });
    },
    [client],
  );
};
