import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { ChannelImportResult, ChannelImportRowResult } from "@/lib/api/types";

// ============================================================================
// Purpose: Imperative action hook for the Registry CSV import stepper: one
//   stable callback POSTing a roster CSV to /channels/import as multipart form
//   data, serving both the dry-run preview and the apply (the dry_run flag is
//   the only difference). camelCase hook args map to the backend's snake_case
//   wire form fields at this boundary — the deliberate frontend/backend casing
//   seam.
// Database/ORM: None (frontend) — calls the backend import endpoint.
// Standards: The FormData carries `file`, `content_owner_id`, `cms_status`,
//   `dry_run` ("true"/"false"), and `reason` — plus `expected_plan_fingerprint`
//   when the caller is binding to a reviewed plan. `cms_status` is sent
//   EXPLICITLY, with the same value the route defaults to: the response echoes
//   it, and a value the client never sent is one it cannot check the echo
//   against (review #184). useApiClient passes FormData through
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
/**
 * The only destinations a planned source-status change can name. Both the
 * CREATE stamp and the flip derivation produce one of these two; every other
 * declared status (OFFICIAL_CMS_REVENUE, OFFICIAL_MANUAL_IMPORT) can only be
 * a `from`, never a `to`.
 */
const REVENUE_REQUIRED_STATUS = "MISSING_REVENUE_SOURCE";
const REVENUE_OPTIONAL_STATUS = "PERFORMANCE_ONLY";
const DERIVED_SOURCE_STATUSES = [REVENUE_REQUIRED_STATUS, REVENUE_OPTIONAL_STATUS] as const;

/** The inventory fields an UPDATE's diff may mention — _inventory_changes
 * compares exactly these four and emits only the ones that differ. */
const INVENTORY_FIELDS = [
  "channel_name",
  "cms_status",
  "content_owner_id",
  "revenue_required",
] as const;

const PLAN_OUTCOMES = ["CREATE", "UPDATE", "UNCHANGED", "ERROR"] as const;
const GROUP_ACTIONS = ["CREATE", "JOIN"] as const;

/** A present, non-empty string — the backend never emits a blank for these. */
const isNonBlankString = (value: unknown): boolean => {
  return typeof value === "string" && value !== "";
};

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

/**
 * `row_number` is the CSV record the operator has to go and fix, and it is the
 * React key each preview row is rendered under. The parser enumerates data
 * rows with `enumerate(reader, start=1)`, so the backend emits 1-based
 * integers, one per input row — a fractional, zero, negative or duplicated
 * value is unemittable, and a duplicate would collide as a React key so a
 * refreshed preview could reuse or mis-show a row (review #184, codex P2).
 * Uniqueness is checked across the plan in isPlanRows.
 */
const isRowNumber = (value: unknown): boolean => {
  return typeof value === "number" && Number.isInteger(value) && value >= 1;
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
  // `to` is CONSTRAINED, not merely a string. It is always the DERIVED value,
  // and derive_revenue_source_status returns the existing status when the flag
  // is unchanged (which yields null here, not a pair) or exactly one of these
  // two when it flips; _created_revenue_source_status returns the same two. So
  // any other destination is a classification the backend cannot plan — an
  // OFFICIAL_* status can be a `from`, never a `to` (review #184, codex P2).
  return (
    isNullableString(value.from) &&
    DERIVED_SOURCE_STATUSES.some((status) => status === value.to)
  );
};

/**
 * A CREATE always DISCLOSES its source status: the planner stamps
 * `(None, _created_revenue_source_status(...))` on every CREATE, so null there
 * is unemittable — and it is exactly the case where silence hides the finance
 * classification a new channel is born with. An ERROR row writes nothing and
 * carries null.
 *
 * NOT extended to "any row whose revenue_required changes", which the finding
 * also proposed: a flip can legitimately derive the SAME status the row
 * already has, and _planned_revenue_source_status then returns None on purpose
 * so a re-import does not read as a reclassification. Requiring disclosure
 * there would reject payloads the backend does emit.
 */
const disclosesSourceStatus = (row: Record<string, unknown>): boolean => {
  if (row.outcome === "ERROR") {
    return row.revenue_source_status === null;
  }
  if (row.outcome !== "CREATE") {
    return true;
  }
  const change = row.revenue_source_status;
  return isPlainObject(change) && change.from === null;
};

/**
 * EVERY field the preview renders, not just the ones it indexes into. A
 * nullable field carrying an OBJECT is the case an allowlist of the indexed
 * three still let through: `group_id: {}` is not null, so GroupCell falls past
 * its null branch and renders the object as a React child, which throws
 * (review #184). Nullable is about ABSENCE, not "anything goes".
 */
const PLAN_ROW_FIELDS: ReadonlyArray<readonly [string, (value: unknown) => boolean]> = [
  ["row_number", isRowNumber],
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

/**
 * A WRITABLE row must CARRY the values it will write. The field checks above
 * are outcome-blind — `youtube_channel_id`, `channel_name` and
 * `revenue_required` are each independently nullable, because an ERROR row
 * legitimately carries none of them — so a CREATE/UPDATE/UNCHANGED row with all
 * three null passes them, passes hasConsistentGroupEffect, and can still tally
 * against `counts`. The preview then renders dashes where the channel, its
 * name and its revenue flag belong, Apply stays enabled, and if the payload
 * kept a valid fingerprint the backend goes on to write the REAL CSV values —
 * which the operator was never shown (review #184, codex P2).
 *
 * The backend cannot emit such a row: every non-ERROR entry is constructed
 * from a parsed row whose `youtube_channel_id` and `channel_name` are typed
 * `str`, with `revenue_required` defaulted to a bool (channel_import.py, the
 * UNCHANGED and CREATE/UPDATE entry constructions). ERROR rows are exempt here
 * for the same reason they are nullable there: a parse failure has no channel
 * to name.
 */
const hasWriteFields = (row: Record<string, unknown>): boolean => {
  if (row.outcome === "ERROR") {
    return true;
  }
  return (
    isNonBlankString(row.youtube_channel_id) &&
    isNonBlankString(row.channel_name) &&
    typeof row.revenue_required === "boolean"
  );
};

/**
 * The OUTCOME LABEL must match the diff it is labelling. The planner sets
 * `outcome = UPDATE if changes else UNCHANGED` and gives CREATE an empty
 * `changes` by construction, so the relation is exact — a row labelled
 * UNCHANGED while carrying a real diff shows "no change" over fields the apply
 * will write, and an UPDATE with an emptied diff claims a write it does not
 * describe (review #184, codex P2).
 *
 * The KEY set is checked for the same reason: _inventory_changes compares
 * exactly the four INVENTORY_FIELDS, so a diff naming anything else describes
 * a write this route cannot perform.
 *
 * LIMIT, stated because it bounds the guarantee: a real UPDATE relabelled as
 * UNCHANGED *with its diff removed* is indistinguishable from a genuine
 * UNCHANGED row and no client-side check can catch it. The plan fingerprint is
 * what binds the apply to the reviewed plan in that case.
 */
const outcomeMatchesChanges = (row: Record<string, unknown>): boolean => {
  const names = Object.keys(row.changes as Record<string, unknown>);
  if (!names.every((name) => INVENTORY_FIELDS.some((field) => field === name))) {
    return false;
  }
  // CREATE, UNCHANGED and ERROR all carry an empty diff; UPDATE never does.
  return row.outcome === "UPDATE" ? names.length > 0 : names.length === 0;
};

/**
 * Every condition a row must meet, as a list for the same reason RESULT_CHECKS
 * is one: each has the same consequence — the row is not one the backend could
 * have planned — so the conjunction carried no information the list does not,
 * and naming them keeps isPlanRow under the analyzer's complexity threshold
 * (DeepSource JS-R1005), conformed rather than suppressed.
 *
 * The FIELD check stays first: every rule after it reads fields at their
 * declared types, which is only sound once that one has passed. `every`
 * short-circuits, so the ordering holds at runtime.
 */
/**
 * `reason` is the row-specific diagnosis the operator needs to fix the roster,
 * and it belongs to ERROR rows EXACTLY. Every backend error entry carries one
 * — a parse failure's message, or the blocked-group / archived-channel text —
 * and no writable entry passes `reason` at all, so it defaults to None there.
 *
 * The field-level nullable-string check sees neither half: an ERROR row with
 * `reason: null` or `""` renders a dash in the Note column, withholding the
 * only explanation of why Apply is blocked; and a writable row carrying a
 * reason is a shape the planner cannot produce (review #184, codex P2).
 */
const explainsErrorRows = (row: Record<string, unknown>): boolean => {
  return row.outcome === "ERROR" ? isNonBlankString(row.reason) : row.reason === null;
};

/**
 * The destination is DETERMINED by the row's revenue flag, not merely one of
 * two literals. Both derivations agree on the rule — `_created_revenue_source_status`
 * and `derive_revenue_source_status` each return MISSING_REVENUE_SOURCE when
 * revenue is required and PERFORMANCE_ONLY when it is not — so a row pairing
 * `revenue_required: true` with `to: "PERFORMANCE_ONLY"` (or the reverse) is
 * one the planner cannot produce.
 *
 * Checking only membership let that pair through, and RevenueCell renders the
 * two together: the operator would approve a finance classification that is
 * the opposite of what the backend goes on to persist (review #184, codex P2).
 */
const sourceStatusMatchesRevenueFlag = (row: Record<string, unknown>): boolean => {
  const change = row.revenue_source_status;
  if (!isPlainObject(change)) {
    return true;
  }
  return change.to === (row.revenue_required ? REVENUE_REQUIRED_STATUS : REVENUE_OPTIONAL_STATUS);
};

const ROW_CHECKS: ReadonlyArray<(row: Record<string, unknown>) => boolean> = [
  (row) => PLAN_ROW_FIELDS.every(([field, isValid]) => isValid(row[field])),
  hasConsistentGroupEffect,
  hasWriteFields,
  outcomeMatchesChanges,
  disclosesSourceStatus,
  sourceStatusMatchesRevenueFlag,
  explainsErrorRows,
];

const isPlanRow = (row: unknown): boolean => {
  if (!isPlainObject(row)) {
    return false;
  }
  return ROW_CHECKS.every((holds) => holds(row));
};

/**
 * A successful plan always has at least ONE row: parse_channel_import_csv
 * rejects a header-only or blank-only roster outright ("CSV contains no data
 * rows"), which is a format error carrying a string detail, never a plan. An
 * empty-rows body with four zero counts satisfies countsMatchRows, so without
 * this the preview would say the roster is empty while Apply stayed enabled
 * and the bound request executed the real file (review #184, codex P2).
 *
 * Row numbers must also be DISTINCT — they are the preview's React keys, and
 * the parser emits exactly one per input row.
 */
const isPlanRows = (value: unknown): boolean => {
  if (!Array.isArray(value) || value.length === 0 || !value.every(isPlanRow)) {
    return false;
  }
  const numbers = value.map((row: { row_number: number }) => row.row_number);
  return new Set(numbers).size === numbers.length;
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
/**
 * The three wire names that appear on BOTH sides of this boundary — named once
 * so the request builder and the response checks cannot drift apart on a
 * spelling. The response-only names stay inline in the tables: there is no
 * second site for them to disagree with (review #184, qodo).
 */
const OWNER_FIELD = "content_owner_id";
const CMS_STATUS_FIELD = "cms_status";
const DRY_RUN_FIELD = "dry_run";

const PLAN_PAYLOAD_FIELDS: ReadonlyArray<readonly [string, (value: unknown) => boolean]> = [
  ["rows", isPlanRows],
  ["counts", isCountMap],
  ["plan_fingerprint", (value) => typeof value === "string" && value !== ""],
  [OWNER_FIELD, (value) => typeof value === "string"],
  [CMS_STATUS_FIELD, (value) => typeof value === "string"],
  [DRY_RUN_FIELD, (value) => typeof value === "boolean"],
];

/**
 * `counts` must be the TALLY of `rows`, not merely a well-formed map. The
 * backend derives each count by counting rows with that outcome, so a payload
 * where they disagree — 99 CREATEs beside one UPDATE row — is one it cannot
 * emit, and it would put contradictory totals on the preview and carry them
 * onto the Applied screen as the approved plan (review #184).
 */
/**
 * For each diff field: the type BOTH sides hold, and the value its `to` must
 * equal. `_inventory_changes` compares exactly these four and takes `to` from
 * the planned value — the row's own for channel_name and revenue_required, the
 * REQUEST target for cms_status and content_owner_id.
 */
const CHANGE_FIELD_RULES: Record<
  string,
  {
    side: (value: unknown) => boolean;
    target: (row: Record<string, unknown>, plan: Record<string, unknown>) => unknown;
  }
> = {
  channel_name: { side: (value) => typeof value === "string", target: (row) => row.channel_name },
  revenue_required: {
    side: (value) => typeof value === "boolean",
    target: (row) => row.revenue_required,
  },
  cms_status: { side: (value) => typeof value === "string", target: (_row, plan) => plan.cms_status },
  content_owner_id: { side: isNullableString, target: (_row, plan) => plan.content_owner_id },
};

/**
 * One diff entry must describe ITS OWN field, and describe a real change.
 * `_inventory_changes` keeps only pairs where `pair[0] != pair[1]`, so equal
 * sides are unemittable; and its `to` is the value the write will persist, so
 * a `to` disagreeing with the row or the request target is describing a write
 * that will not happen.
 *
 * The key allowlist alone accepted `revenue_required: {from: false, to: false}`
 * beside `revenue_required: true` — a diff saying "no change" next to a field
 * saying the opposite, with Apply live and the retained fingerprint letting the
 * real value through (review #184, codex P2).
 */
const changeIsWellFormed = (
  field: string,
  change: { from: unknown; to: unknown },
  row: Record<string, unknown>,
  plan: Record<string, unknown>,
): boolean => {
  const rule = CHANGE_FIELD_RULES[field];
  if (rule === undefined) {
    return false;
  }
  return (
    rule.side(change.from) &&
    rule.side(change.to) &&
    change.from !== change.to &&
    change.to === rule.target(row, plan)
  );
};

/** Every row's diff, checked against the row and the plan's target. */
const diffsMatchTheirFields = (plan: Record<string, unknown>): boolean => {
  const rows = plan.rows as Record<string, unknown>[];
  return rows.every((row) => {
    const changes = row.changes as Record<string, { from: unknown; to: unknown }>;
    return Object.entries(changes).every(([field, change]) =>
      changeIsWellFormed(field, change, row, plan),
    );
  });
};

const countsMatchRows = (candidate: Record<string, unknown>): boolean => {
  const counts = candidate.counts as Record<string, number>;
  const rows = candidate.rows as ChannelImportRowResult[];
  return PLAN_OUTCOMES.every(
    (outcome) => counts[outcome] === rows.filter((row) => row.outcome === outcome).length,
  );
};

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
  if (!isPlainObject(payload)) {
    return false;
  }
  // The field checks run FIRST and the tally second: countsMatchRows reads
  // both as their declared types, which is only sound once they have passed.
  return (
    PLAN_PAYLOAD_FIELDS.every(([field, isValid]) => isValid(payload[field])) &&
    countsMatchRows(payload) &&
    diffsMatchTheirFields(payload)
  );
};

/**
 * The CMS status this flow imports under. Sent EXPLICITLY rather than left to
 * the route's identical default, so the request states its own target: the
 * response echoes `cms_status`, and a value the client never sent is one it
 * cannot check the echo against. Import is CMS-only by design — the roster is
 * a CMS content-owner roster — so there is one value, not a choice.
 */
const IMPORT_CMS_STATUS = "INSIDE_CMS";

/**
 * The echoed target must be the target that was ASKED for. `plan_fingerprint`
 * covers the plan but cannot police this on its own: the digest is computed
 * server-side over the request's actual owner and CMS status, so a malformed or
 * misrouted body that keeps a valid fingerprint while changing
 * `content_owner_id`/`cms_status` is internally consistent from the client's
 * side — and the client cannot recompute the digest, which also takes the
 * server-resolved tenant. Preview would then render the ALTERED target while
 * Apply still sends the captured owner, so the write lands somewhere other
 * than what the operator reviewed (review #184, codex P2).
 *
 * The owner is compared trimmed because that is exactly what the route echoes:
 * `_validated_import_form` strips it once at the boundary and returns the
 * normalized value, so a padded " owner-1 " legitimately comes back as
 * "owner-1" and must not be read as a mismatch.
 */
export const echoesRequestedTarget = (
  result: ChannelImportResult,
  contentOwnerId: string,
): boolean => {
  return (
    result.content_owner_id === contentOwnerId.trim() &&
    result.cms_status === IMPORT_CMS_STATUS
  );
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

/** What the request asked for, against which a response is judged. */
type ImportRequestTarget = {
  dryRun: boolean;
  contentOwnerId: string;
  expectedPlanFingerprint?: string;
};

/**
 * Every condition a usable 2xx body must meet, ORDERED from structural to
 * semantic. A list rather than a chain of ifs: each entry has the same
 * consequence — the body is unusable — so the branching carried no information
 * the order does not, and stating them as data keeps the check under the
 * analyzer's complexity threshold (DeepSource JS-R1005), conformed rather than
 * suppressed.
 *
 * The FIRST entry must stay first: everything after it reads `result` as a
 * ChannelImportResult, which is only sound once the structural check has
 * passed. `every` short-circuits, so that ordering is enforced at runtime and
 * not merely intended.
 */
const RESULT_CHECKS: ReadonlyArray<
  (result: ChannelImportResult, request: ImportRequestTarget) => boolean
> = [
  // A 2xx is not a promise about SHAPE — client.post casts, it does not
  // validate. Rejecting keeps a malformed dry run a read-only failure, and
  // routes a malformed apply into the flow's INDETERMINATE path (this is not an
  // ApiError, so it is not on the definite-rejection list) — which is right:
  // the write may well have committed, only the body was unusable.
  (result) => isChannelImportResult(result),
  // The MODE must match what was asked for. A structural check only proves
  // `dry_run` is a boolean, so a malformed or legacy apply response carrying
  // `dry_run: true` passed it — and the flow then advanced to Applied and told
  // the operator the import committed, on a body that identifies itself as a
  // preview (review #184). Treated as unusable rather than coerced.
  (result, request) => result.dry_run === request.dryRun,
  // And it must be the plan we BOUND to. A stale, misrouted or legacy-server
  // 2xx can be structurally perfect and describe a different plan entirely;
  // accepting it lets the flow clear the unsettled record and present an
  // unrelated payload as the approved one. The route returns the same digest it
  // compared against on success (channels.py: the 409 fires when they differ),
  // so an inequality is never a legitimate response. Unbound callers sent no
  // expectation and so have nothing to compare.
  (result, request) =>
    request.expectedPlanFingerprint === undefined ||
    result.plan_fingerprint === request.expectedPlanFingerprint,
  // And it must describe the TARGET this request named. Unlike the fingerprint
  // check this one applies to the dry run too, which is the important half: the
  // preview is what the operator approves, so an altered target has to be
  // refused before it is rendered rather than caught one step later.
  (result, request) => echoesRequestedTarget(result, request.contentOwnerId),
  // An APPLY cannot succeed over a plan holding ERROR rows: import_channels
  // raises 422 before applying whenever plan.has_errors, so a 2xx carrying
  // them is a response the route cannot produce. Without this the flow would
  // settle the pending-write record and show "Import applied" for a write the
  // backend refuses to perform — clearing the durable guard on a body that
  // proves nothing landed. Routing it to the indeterminate path is right: the
  // body is unusable, and what actually happened is unknown (review #184).
  (result, request) => request.dryRun || result.counts.ERROR === 0,
];

/**
 * Throw unless the body is usable. Throws rather than returning a verdict
 * because every failure above is the same outcome for the caller — on an apply
 * the write may well have committed and only the body was unreadable.
 */
const assertUsableResult = (result: ChannelImportResult, request: ImportRequestTarget): void => {
  if (!RESULT_CHECKS.every((holds) => holds(result, request))) {
    throw new ChannelImportShapeError();
  }
};

// ============================================================================
// Purpose: The REQUEST side of this boundary — build the multipart form and
//   dispatch it. The module block above states the wire contract this file
//   implements; this one covers the dispatch itself, which is 400 lines below
//   it now that the response machinery sits between them.
// Database/ORM: None (frontend) — POSTs /channels/import. Every guard that
//   matters (MANAGE_CHANNELS, MANAGE_GROUPS on group-bearing rosters, the plan
//   fingerprint, the write-boundary pre-state check, the locked-month rule)
//   belongs to that route and is untouched by anything here.
// Standards: One stable useCallback so a re-render cannot mint a second
//   dispatcher mid-flight. Both modes go through this single path; `dry_run`
//   is the only difference, which is why the response validator checks the
//   echoed flag rather than trusting the caller's intent.
//   `expected_plan_fingerprint` is appended ONLY when the caller supplies it:
//   sending an empty or stale value would either 409 a legitimate apply or, if
//   omitted by accident, silently downgrade the write to the backend's
//   unbound file-wins path (review #184). `cms_status` is sent explicitly so
//   the echoed target can be checked against a value this request named.
//   Adds NO error handling of its own — the calling view owns busy/error
//   presentation — and nothing here retries: a retry of an audited bulk write
//   is the operator's decision, made against the audit trail.
// Blast Radius: Channel-registry inventory + row-created group membership, via
//   the backend's guarded audited route. A mistake here shows as a refused or
//   misdirected import, never as an unpermitted one.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() POST, which
//       passes FormData through verbatim so fetch sets the multipart boundary.
//   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx -> the
//       only caller; supplies contentOwnerId, the reason, and the bound
//       fingerprint.
//   - File: backend/ums_smart_revenue/api/channels.py -> import_channels, the
//       route this dispatches to and whose echo assertUsableResult checks.
// ============================================================================
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
      form.append(OWNER_FIELD, contentOwnerId);
      form.append(CMS_STATUS_FIELD, IMPORT_CMS_STATUS);
      form.append(DRY_RUN_FIELD, dryRun ? "true" : "false");
      form.append("reason", reason);
      if (expectedPlanFingerprint !== undefined) {
        form.append("expected_plan_fingerprint", expectedPlanFingerprint);
      }
      return client
        .post<ChannelImportResult>("/channels/import", form)
        .then((result) => {
          assertUsableResult(result, { dryRun, contentOwnerId, expectedPlanFingerprint });
          return result;
        });
    },
    [client],
  );
};
