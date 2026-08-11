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

/**
 * Every status the column may hold — the `ck_youtube_channels_revenue_source_status`
 * CHECK constraint's five literals. A transition's `from` is the row's CURRENT
 * status, so it is drawn from all five; only `to` is narrowed to the two the
 * planner derives.
 */
const SOURCE_STATUSES = [
  "OFFICIAL_CMS_REVENUE",
  "OFFICIAL_MANUAL_IMPORT",
  "ALLOCATED_FROM_PAYMENT_POOL",
  REVENUE_OPTIONAL_STATUS,
  REVENUE_REQUIRED_STATUS,
] as const;

/**
 * The one inventory name this module spells OUTSIDE the field tables:
 * `sourceTransitionIsValid` asks whether a row's diff mentions it, which is a
 * third site the other three names do not have. Named once for the same reason
 * OWNER_FIELD and its two neighbours are — a typo at one site and the check
 * silently stops firing (review #184, qodo).
 */
const REVENUE_REQUIRED_FIELD = "revenue_required";

/** The inventory fields an UPDATE's diff may mention — _inventory_changes
 * compares exactly these four and emits only the ones that differ. */
const INVENTORY_FIELDS = [
  "channel_name",
  "cms_status",
  "content_owner_id",
  REVENUE_REQUIRED_FIELD,
] as const;

// Every outcome literal this module reasons about by NAME. All four are named,
// not just the two that happened to need it first: a typo in a comparison
// silently DISABLES the rule containing it rather than failing, so the set that
// is safe to spell inline is empty.
const OUTCOME_CREATE = "CREATE";
const OUTCOME_UPDATE = "UPDATE";
const OUTCOME_UNCHANGED = "UNCHANGED";
const OUTCOME_ERROR = "ERROR";
const PLAN_OUTCOMES = [OUTCOME_CREATE, OUTCOME_UPDATE, OUTCOME_UNCHANGED, OUTCOME_ERROR] as const;

/**
 * The three literals `ck_youtube_channels_cms_status` permits. A diff side is a
 * value of that column — `from` the stored one, `to` the request target — so
 * "any string" was never the constraint. `cms_status: {from: "GARBAGE", to:
 * "INSIDE_CMS"}` showed the operator a prior state the database cannot hold,
 * while the bound apply wrote from the real one they never reviewed (review
 * #184, codex P2).
 */
// The status this client imports under, declared BEFORE the set so the set can
// be built from it. Neither is derived by index: CMS_STATUSES[0] tied the value
// actually sent on /channels/import — and the echo it is checked against — to
// array order, so a reordering for readability would have silently changed the
// protocol (review #184, qodo).
const IMPORT_CMS_STATUS = "INSIDE_CMS";
const CMS_STATUSES = [IMPORT_CMS_STATUS, "OUTSIDE_CMS", "UNKNOWN"] as const;

const isCmsStatus = (value: unknown): boolean => {
  return CMS_STATUSES.some((status) => status === value);
};

// Deliberately NOT reusing OUTCOME_CREATE: ChannelImportGroupAction and
// ChannelImportOutcome are independent backend enums that happen to spell one
// member the same way. Sharing a constant would couple them, so renaming an
// outcome would silently move a group label.
const GROUP_ACTIONS = ["CREATE", "JOIN"] as const;

/**
 * A reason the operator can actually READ. Non-empty is not enough: an ERROR
 * row disables Apply and its `reason` is the only row-specific remediation, so
 * `"   "` renders an effectively blank Note cell and leaves them unable to tell
 * what to fix while the payload stays trusted (review #184, codex P2).
 *
 * JS `trim()` is the right test here, unlike isParserText: this asks whether
 * the string RENDERS as blank, not whether it matches the parser's
 * normalization -- and a reason is composed by the backend, never parsed from
 * the operator's CSV.
 */
const isNonBlankString = (value: unknown): boolean => {
  return typeof value === "string" && value.trim() !== "";
};

/**
 * A channel id the PARSER would have accepted. `CHANNEL_ID_PATTERN` in
 * channel_import.py is `^UC[A-Za-z0-9_-]{22}$` and a row failing it becomes an
 * ERROR, so any other shape on a writable row is unemittable — and it would
 * put a misidentified channel on the preview while the bound apply wrote the
 * real one (review #184, codex P2).
 */
const CHANNEL_ID_PATTERN = /^UC[A-Za-z0-9_-]{22}$/u;

const isChannelId = (value: unknown): boolean => {
  return typeof value === "string" && CHANNEL_ID_PATTERN.test(value);
};

/**
 * The characters Python's `str.strip()` removes -- Py_UNICODE_ISSPACE, which is
 * NOT the set `String.prototype.trim()` removes.
 *
 * The two disagree in both directions, and one direction breaks a valid import:
 * JS trims U+FEFF and Python does not, so `_parse_text_fields` accepts and
 * emits a BOM-prefixed name verbatim while `value === value.trim()` judged it
 * untrimmed and refused the whole payload. Such a roster works through the API
 * and leaves the SPA unable to reach Preview (review #184, codex P2).
 * Measured, not assumed:
 *
 *   python: "﻿Alpha".strip() -> unchanged
 *   node:   "﻿Alpha".trim()  -> "Alpha"
 *
 * The other direction is harmless: U+001C-U+001F are Python-space and not
 * JS-space, but the parser has already stripped them from the ends, so no
 * emitted value can carry one there.
 */
const PYTHON_SPACE = "\\t\\n\\v\\f\\r\\u001c-\\u001f \\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";

/** Leading or trailing Python-whitespace -- text `strip()` would have cut. */
const PYTHON_UNSTRIPPED = new RegExp(`^[${PYTHON_SPACE}]|[${PYTHON_SPACE}]$`, "u");

// ============================================================================
// Purpose: What Python's `str.strip()` would return — for the times this file
//   must REPRODUCE the backend's normalization rather than merely detect it.
//   PYTHON_UNSTRIPPED above answers "would strip() have cut this?"; this
//   answers "what would strip() have left?", and the two must never disagree.
// Database/ORM: None (frontend) — a pure string function. It issues no
//   request, reads no state, and is safe to call on any decoded field.
// Standards: Built from the SAME PYTHON_SPACE class as the detector, so the
//   two cannot drift into two different notions of whitespace; that class is
//   exactly the 29 codepoints `str.strip()` removes, verified by set equality
//   in both directions, not by inspection. `String.prototype.trim` is not a
//   substitute in either direction and the difference is not cosmetic: trim()
//   cuts U+FEFF, which strip() KEEPS. Anchored at both ends only — `$` without
//   the `m` flag is end-of-STRING, so an interior run survives exactly as it
//   does in Python. The `g` flag on a module-level RegExp is deliberate and
//   safe HERE because `String.prototype.replace` resets `lastIndex` around the
//   call; reusing this same object with `.test()` or `.exec()` would NOT be
//   safe, which is why the detector above is a separate, non-global RegExp.
// Blast Radius: Whether a preview the backend ACCEPTED is refused. The owner
//   echo is an equality check, so normalizing differently from the route does
//   not warn — assertUsableResult raises ChannelImportShapeError and the whole
//   import UI stops for a roster the API handles fine. No finance math, no
//   write; the damage is availability of the operator's only preview.
// Connections:
//   - File: backend/ums_smart_revenue/api/channels.py ->
//       _validated_content_owner_id, whose `raw.strip()` this mirrors and
//       whose normalized value is what the route echoes back.
//   - File: frontend/src/lib/api/useChannelImport.ts -> echoesRequestedTarget,
//       the equality this exists to make correct.
//   - File: Docs/12_BACKEND_API_SPEC.md -> the import contract that documents
//       the echoed target fields.
// ============================================================================
const PYTHON_STRIPPABLE = new RegExp(`^[${PYTHON_SPACE}]+|[${PYTHON_SPACE}]+$`, "gu");

const pythonStrip = (value: string): string => value.replace(PYTHON_STRIPPABLE, "");

/**
 * Text as the PARSER would have left it. `_parse_text_fields` strips the cell,
 * ERRORs an empty result, and ERRORs a NUL-bearing one — so a writable row's
 * text is always trimmed, non-empty and NUL-free.
 *
 * Trim-EQUALITY, not merely a non-blank trim: `" Alpha "` is a value the
 * backend cannot emit, and accepting it lets a malformed response render a
 * padded name on the preview while the bound apply writes the normalized real
 * one — the same substitution, just quieter than a blank cell (review #184,
 * codex P2).
 */
const isParserText = (value: unknown): boolean => {
  return (
    typeof value === "string" &&
    !PYTHON_UNSTRIPPED.test(value) &&
    value !== "" &&
    !value.includes("\u0000")
  );
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
/**
 * The largest row number `parse_channel_import_csv` can emit.
 *
 * `enumerate(reader, start=1)` counts every line of the data section, and the
 * route caps BOTH tallies at MAX_IMPORT_ROWS (channels.py:111) -- 5000 data
 * rows and 5000 blank rows -- so 10000 is the ceiling, reached only by a file
 * that alternates them.
 *
 * Without a bound, `Number.isInteger` accepts 1e100: a malformed response could
 * keep the real fingerprint, ascend through absurd numbers, pass every other
 * check, and point the operator at CSV lines that do not exist (review #184,
 * codex P2).
 */
const MAX_IMPORT_ROWS = 5000;
const MAX_PLAN_ROW_NUMBER = MAX_IMPORT_ROWS * 2;

const isRowNumber = (value: unknown): boolean => {
  return (
    typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= 1 &&
    value <= MAX_PLAN_ROW_NUMBER
  );
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
  if (row.outcome === OUTCOME_ERROR) {
    return row.revenue_source_status === null;
  }
  if (row.outcome !== OUTCOME_CREATE) {
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
  [REVENUE_REQUIRED_FIELD, (value) => value === null || typeof value === "boolean"],
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
  // An ERROR row performs NO writes, so the planner leaves BOTH fields null —
  // it never even computes group_action past the block check. The biconditional
  // alone is satisfied by both being non-null, which told the operator a
  // rejected row would create or join a group, on the same screen that says
  // error rows write nothing (review #184, codex P2).
  if (row.outcome === OUTCOME_ERROR) {
    return row.group_id === null && row.group_action === null;
  }
  // For a writable row the relation is the biconditional: null is reserved for
  // rows with no group at all.
  return (row.group_id === null) === (row.group_action === null);
};

/**
 * A group key the PARSER would have produced. `_text_fields` in
 * channel_import.py normalizes the cell before anything else sees it:
 * `group_raw.strip() if group_raw and group_raw.strip() else None` maps a
 * blank or whitespace-only cell to null, a NUL-bearing key becomes an ERROR
 * row, and one over MAX_GROUP_ID_CHARS (255) does too. So a non-null key on a
 * writable row is always trimmed, non-blank, NUL-free and within the cap.
 *
 * Without this, `group_id: " "` satisfies isNullableString and pairs happily
 * with a non-null `group_action`, so the biconditional passes: the Group cell
 * renders a blank identifier, Apply stays enabled, and the bound request goes
 * on to write the REAL CSV group the retained fingerprint stands for — the
 * same substitution hasWriteFields closes for the channel columns (review
 * #184, codex P2).
 */
const MAX_GROUP_ID_CHARS = 255;

const isGroupKey = (value: unknown): boolean => {
  // Everything isParserText requires — trimmed, non-empty, NUL-free — plus
  // the index-size cap that applies to the KEY alone; a channel name has no
  // equivalent bound. No interior-space rule either: the parser strips only
  // the ENDS, so "Music EMEA" is a legal key the backend can emit.
  // CODE POINTS, not UTF-16 units. The backend caps `len(group_id)`, which
  // counts code points, while JS `.length` counts UTF-16 units — so a key of
  // 200 non-BMP characters is 200 to Python and 400 here, and the plain
  // `.length` check refused a roster the backend had already accepted, taking
  // the import UI down with it (review #184, qodo). Spreading iterates code
  // points, which is the same unit Python measures.
  return isParserText(value) && [...(value as string)].length <= MAX_GROUP_ID_CHARS;
};

/** Null (no group write) or a key the parser could have emitted. */
const carriesUsableGroupKey = (row: Record<string, unknown>): boolean => {
  return row.group_id === null || isGroupKey(row.group_id);
};

/**
 * An ERROR row makes NO claim about the write it is refusing.
 *
 * Being exempt from the writable-row rules is not the same as being exempt
 * from every rule, and the fields below are the ones that reach the operator's
 * eyes. The planner never sets `channel_name` or `revenue_required` on an
 * ERROR entry — all three constructions leave them at their `None` defaults —
 * and the `youtube_channel_id` it does keep comes from a row whose id already
 * cleared CHANNEL_ID_PATTERN in the parser, so it is either null (a parse
 * failure, with no channel to name) or a real id.
 *
 * Accepting anything else lets a malformed response print a channel and a
 * revenue flag beside the remediation text, sending the operator to correct
 * the wrong line of their CSV — while the retained fingerprint still
 * authorises the real plan (review #184, codex P2).
 */
const errorRowClaimsNothing = (row: Record<string, unknown>): boolean => {
  if (row.outcome !== OUTCOME_ERROR) {
    return true;
  }
  return (
    (row.youtube_channel_id === null || isChannelId(row.youtube_channel_id)) &&
    row.channel_name === null &&
    row.revenue_required === null
  );
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
  if (row.outcome === OUTCOME_ERROR) {
    return true;
  }
  return (
    isChannelId(row.youtube_channel_id) &&
    isParserText(row.channel_name) &&
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
  return row.outcome === OUTCOME_UPDATE ? names.length > 0 : names.length === 0;
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
  return row.outcome === OUTCOME_ERROR ? isNonBlankString(row.reason) : row.reason === null;
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

// ============================================================================
// Purpose: Whether a row's `revenue_source_status` transition is one the
//   planner could have emitted. `_planned_revenue_source_status` takes the
//   FROM-state from `current.revenue_source_status`, which the database
//   constrains to the five declared literals and which is never null on an
//   existing row, and it returns None outright when `planned == current` — so
//   a real transition always names a known literal and always differs.
// Database/ORM: None directly (frontend), but it mirrors the
//   `revenue_source_status` column's CHECK-constrained literal set; the five
//   values in SOURCE_STATUSES are that set, not a UI convenience list.
// Standards: Fails CLOSED on anything unrecognised. A non-CREATE transition
//   must ALSO carry a `revenue_required` diff, because the transition exists
//   only when the flag flipped — derive_revenue_source_status returns the
//   current status untouched otherwise. CREATE is exempt because it has no
//   prior status: its `from` is null, which disclosesSourceStatus already
//   requires, so the exemption cannot be used to smuggle in a null FROM on an
//   UPDATE. Runs as one of ROW_CHECKS, before the plan becomes trusted state.
// Blast Radius: FINANCE + AUDIT. This is the classification the operator
//   approves. Accepting any string let a row present
//   `GARBAGE -> MISSING_REVENUE_SOURCE`, or a fake no-op transition, while
//   Apply stayed enabled and the retained fingerprint authorised the backend's
//   REAL source mutation — the operator signs off on one reclassification and
//   a different one is persisted and audited (review #184, codex P2).
// Connections:
//   - File: backend/ums_smart_revenue/org/channel_import.py ->
//       _planned_revenue_source_status and _created_revenue_source_status,
//       the only producers of the transitions accepted here.
//   - File: backend/ums_smart_revenue/org/channel_registry.py ->
//       derive_revenue_source_status, the rule tying the status to
//       revenue_required that the paired-diff requirement enforces.
//   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx -> the
//       preview that renders this transition for approval.
//   - File: Docs/12_BACKEND_API_SPEC.md -> the documented plan-row contract.
// ============================================================================
const sourceTransitionIsValid = (row: Record<string, unknown>): boolean => {
  const change = row.revenue_source_status;
  if (!isPlainObject(change)) {
    return true;
  }
  if (change.from === change.to) {
    return false;
  }
  if (row.outcome === OUTCOME_CREATE) {
    return true;
  }
  // Both halves of the Standards clause above, in the order they are checked:
  // a FROM the planner could have read, and the `revenue_required` diff that is
  // the only thing able to produce a transition at all.
  const changes = row.changes as Record<string, unknown>;
  return (
    SOURCE_STATUSES.some((status) => status === change.from) &&
    Object.hasOwn(changes, REVENUE_REQUIRED_FIELD)
  );
};

const ROW_CHECKS: ReadonlyArray<(row: Record<string, unknown>) => boolean> = [
  (row) => PLAN_ROW_FIELDS.every(([field, isValid]) => isValid(row[field])),
  hasConsistentGroupEffect,
  carriesUsableGroupKey,
  hasWriteFields,
  errorRowClaimsNothing,
  outcomeMatchesChanges,
  disclosesSourceStatus,
  sourceStatusMatchesRevenueFlag,
  sourceTransitionIsValid,
  explainsErrorRows,
];

const isPlanRow = (row: unknown): boolean => {
  if (!isPlainObject(row)) {
    return false;
  }
  return ROW_CHECKS.every((holds) => holds(row));
};

/**
 * ONE effect per group key, across the whole plan.
 *
 * Every per-row predicate is row-local, so two writable rows carrying the same
 * `group_id` with different `group_action`s pass all of them, tally correctly,
 * and render side by side — one cell promising a NEW SECTOR group and the
 * other promising a join of an existing one, for the same key.
 *
 * The backend cannot emit that: `_planned_group_action` derives the label from
 * that key's existence for the request's content owner, which is one fact per
 * key, and the apply then batches the key into a SINGLE group operation. So a
 * plan showing two effects is showing at least one effect the import will not
 * perform, while the retained fingerprint authorises the one real effect
 * (review #184, codex P2).
 */
const groupActionsAgree = (rows: ReadonlyArray<Record<string, unknown>>): boolean => {
  const actionByKey = new Map<string, unknown>();
  for (const row of rows) {
    const key = row.group_id;
    if (typeof key !== "string") {
      continue;
    }
    if (!actionByKey.has(key)) {
      actionByKey.set(key, row.group_action);
      continue;
    }
    if (actionByKey.get(key) !== row.group_action) {
      return false;
    }
  }
  return true;
};

/** The four invariants, for ONE channel's writable rows. */
const channelCopiesAgree = (group: ReadonlyArray<Record<string, unknown>>): boolean => {
  const [first] = group;
  if (
    !group.every(
      (row) =>
        row.channel_name === first.channel_name &&
        row.revenue_required === first.revenue_required,
    )
  ) {
    return false;
  }
  // Each copy must name a DISTINCT group. Absence is a value, not a wildcard:
  // the parser rejects a repeated null group key exactly as it rejects a
  // repeated real one, so anything that is not a string collapses to one slot.
  const groupKeys = group.map((row) => (typeof row.group_id === "string" ? row.group_id : null));
  if (new Set(groupKeys).size !== groupKeys.length) {
    return false;
  }
  const deciding = group.filter((row) => row.outcome !== OUTCOME_UNCHANGED);
  if (deciding.length > 1) {
    return false;
  }
  if (deciding.length === 0) {
    return true;
  }
  const lowest = Math.min(...group.map((row) => row.row_number as number));
  return deciding[0].row_number === lowest;
};

/**
 * The planner's REPEATED-CHANNEL contract, which no row-local rule can see.
 *
 * A roster legitimately lists one channel several times — CMS membership is
 * many-to-many and `group_id` carries one association per row — but the parser
 * rejects copies that disagree on the inventory fields, so every surviving
 * copy agrees on `channel_name` and `revenue_required`. And only the FIRST
 * copy owns the inventory decision: later copies plan as UNCHANGED so the
 * apply attaches their group without a second inventory outcome.
 *
 * Each copy must also name a DISTINCT group, because repeating a channel is
 * only meaningful ACROSS groups. The parser rejects a repeated
 * `(youtube_channel_id, group_id)` pair outright, so a plan carrying one did
 * not come from a roster — which is the whole reason to refuse it here, on a
 * boundary whose job is malformed RESPONSES rather than malformed files.
 *
 * The backend's reason differs by shape and only one half is about groups: a
 * repeated pair that NAMES a group is collapsed by the write pass to a single
 * membership, so Preview would promise the group work twice while the retained
 * fingerprint authorises the one real association; a repeated pair carrying NO
 * group produces no membership at all and is refused because the second copy
 * is a phantom UNCHANGED row (review #184, codex P2). This check covers both
 * because it keys on the pair, with absence as an ordinary value.
 *
 * Checked here rather than per-row for the usual reason — no row-local
 * predicate can see another row.
 *
 * Without this, a malformed response can repeat one valid id with two names or
 * two revenue flags, or give several copies their own CREATE/UPDATE diffs.
 * Every per-row predicate and the count tally still pass, so the operator
 * approves a plan showing inventory work the import will not do — twice over —
 * while the retained fingerprint authorises the real one (review #184).
 *
 * Keyed on the LOWEST row_number rather than array position: the backend sorts
 * its entries, but nothing here requires the array to arrive sorted, and "the
 * first copy" is a statement about the operator's file.
 */
const channelRowsAgree = (rows: ReadonlyArray<Record<string, unknown>>): boolean => {
  const copies = new Map<string, Array<Record<string, unknown>>>();
  for (const row of rows) {
    const id = row.youtube_channel_id;
    if (row.outcome === OUTCOME_ERROR || typeof id !== "string") {
      continue;
    }
    const group = copies.get(id);
    if (group === undefined) {
      copies.set(id, [row]);
    } else {
      group.push(row);
    }
  }
  return [...copies.values()].every(channelCopiesAgree);
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
/**
 * Row numbers ASCEND strictly — distinct, and in the order the backend sorted
 * them (`entries.sort(key=row_number)`), which is the order the preview renders.
 *
 * Strictly ascending rather than merely distinct: an out-of-order payload puts
 * the operator's rows on screen in an order their file does not have, so a
 * remediation reason lands beside the wrong line even though every number is
 * individually plausible.
 *
 * Deliberately NOT `1..rows.length`. `enumerate(reader, start=1)` numbers every
 * line of the data section and blank lines are skipped after consuming their
 * index, so a roster with a blank line between records legitimately produces
 * 1, 2, 4. The number names the operator's LINE, which is the whole point of
 * showing it; requiring contiguity would refuse those rosters outright.
 */
const rowNumbersAscend = (rows: ReadonlyArray<Record<string, unknown>>): boolean => {
  return rows.every(
    (row, index) => index === 0 || (rows[index - 1].row_number as number) < (row.row_number as number),
  );
};

// ============================================================================
// Purpose: The parser's ROW BUDGET, which no per-row bound can express.
//   `isRowNumber` caps a single number at 10000 because that is the largest
//   the parser can reach, but reaching it COSTS something: `row_number` counts
//   every record and only 5000 of them may be blank, so a row at (zero-based)
//   index `i` has at most `i` data records and 5000 blank records ahead of it
//   — bounding it at `i + 5001`. And because every plan entry comes from a
//   data row, a plan can never carry more than MAX_IMPORT_ROWS entries at all.
// Database/ORM: None (frontend) — a structural predicate over a decoded JSON
//   body, run before that body is allowed to become trusted UI state.
// Standards: Reads `index` as the SORTED position, which rowNumbersAscend has
//   already established — it runs first in PLAN_CHECKS and `.every`
//   short-circuits, so a non-ascending plan is refused before this is reached.
//   That ordering is load-bearing: reversing PLAN_CHECKS would leave this
//   check comparing row numbers against arbitrary array positions. Both limits
//   mirror the parser's own constants; if MAX_IMPORT_ROWS or the blank-record
//   budget moves in channel_import.py, this moves with it. Fails CLOSED — an
//   over-budget plan is refused, never truncated to fit.
// Blast Radius: Which CSV LINE the operator is sent to. A malformed response
//   can keep the real fingerprint and either pad the plan to 10000 plausible
//   rows or hand back a single row numbered 10000 — a line no one-row file has
//   — so the operator reviews and remediates a line that does not exist while
//   the bound apply executes the real roster (review #184, codex P2). No
//   finance math; the damage is that the reviewed plan is not the applied one.
// Connections:
//   - File: backend/ums_smart_revenue/org/channel_import.py -> the parser
//       whose 5000-data-row and 5000-blank-record caps produce this bound.
//   - File: frontend/src/lib/api/useChannelImport.ts -> rowNumbersAscend,
//       which must stay ahead of this in PLAN_CHECKS, and isRowNumber, whose
//       per-row ceiling this completes.
//   - File: Docs/12_BACKEND_API_SPEC.md -> the documented import row limits.
// ============================================================================
const rowBudgetHolds = (rows: ReadonlyArray<Record<string, unknown>>): boolean => {
  if (rows.length > MAX_IMPORT_ROWS) {
    return false;
  }
  return rows.every((row, index) => (row.row_number as number) <= index + MAX_IMPORT_ROWS + 1);
};

/**
 * The checks a plan's ROWS must satisfy together, as a list for the same
 * reason ROW_CHECKS is one: each has the identical consequence — this is not a
 * plan the backend could have produced — and naming them keeps isPlanRows
 * under the analyzer's complexity threshold (DeepSource JS-R1005), conformed
 * rather than suppressed.
 */
const PLAN_CHECKS: ReadonlyArray<(rows: ReadonlyArray<Record<string, unknown>>) => boolean> = [
  rowNumbersAscend,
  rowBudgetHolds,
  groupActionsAgree,
  channelRowsAgree,
];

const isPlanRows = (value: unknown): boolean => {
  if (!Array.isArray(value) || value.length === 0 || !value.every(isPlanRow)) {
    return false;
  }
  const rows = value as ReadonlyArray<Record<string, unknown>>;
  return PLAN_CHECKS.every((holds) => holds(rows));
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
 * spelling. A response-only name stays inline in its table while the table is
 * its only site; `revenue_required` earned REVENUE_REQUIRED_FIELD above when a
 * later check gave it a second one (review #184, qodo).
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
  cms_status: { side: isCmsStatus, target: (_row, plan) => plan.cms_status },
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
 * The owner is compared STRIPPED because that is exactly what the route
 * echoes: `_validated_content_owner_id` calls `raw.strip()` once at the
 * boundary (channels.py:830) and returns the normalized value, so a padded
 * " owner-1 " legitimately comes back as "owner-1" and must not be read as a
 * mismatch.
 *
 * `pythonStrip`, never `trim()`. They disagree on U+FEFF, which `strip()`
 * keeps and `trim()` cuts, so an owner id carrying a BOM is echoed verbatim by
 * the backend and compared against a shortened string here — a mismatch on a
 * value the route accepted, which fails the WHOLE preview rather than
 * warning, and takes the import UI down for a roster the API handles fine
 * (review #184, codex P2). Same divergence, same consequence, as the
 * `isParserText` case above.
 */
export const echoesRequestedTarget = (
  result: ChannelImportResult,
  contentOwnerId: string,
): boolean => {
  return (
    result.content_owner_id === pythonStrip(contentOwnerId) &&
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
