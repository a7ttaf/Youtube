# Import stepper in Registry (PR-B) — implementation plan

Date: 2026-08-09
Spec: `Docs/superpowers/specs/2026-08-07-import-sync-ui-design.md` (PR-B section; approved 2026-08-07)
Base: `main` @ `14fc27c7`

PR-A (#174, merged) built the primitives this PR reuses: `ActionStepper`
(steps + activeIndex + onCancel + children), `OutcomeTable`
(`{columns, rows: [{key, tone?, cells}], emptyLabel}`), the
credential-fed owner picker pattern (`useContentOwners("youtube-analytics")`
→ least-privilege `GET /connectors/content-owners`, MANAGE_GROUPS-gated),
and `describeApiError` (`lib/api/errors.ts:45`) for error copy.

## Verified backend contract (api/channels.py, all anchors re-read on this base)

- `POST /channels/import` (`channels.py:639`) — multipart form: `file`
  (UploadFile), `content_owner_id` (Form), `dry_run` (Form bool), `reason`
  (Form), `cms_status` (Form, default `"INSIDE_CMS"`).
- Gates (`channels.py:653-675`): `MANAGE_CHANNELS` at global scope always;
  **additionally** `MANAGE_GROUPS` at global scope when any parsed row
  carries `Group_ID`.
- Response `ChannelImportResult` (`channels.py:190`): `{dry_run,
  content_owner_id, cms_status, counts: {outcome: n}, rows:
  [ChannelImportRowResult]}` with `ChannelImportRowResult`
  (`channels.py:170`): `{row_number, youtube_channel_id, outcome,
  channel_name, group_id, revenue_required, changes: {field: {from, to}},
  reason}`.
- Outcomes (`org/channel_import.py:322`): closed enum `CREATE | UPDATE |
  UNCHANGED | ERROR`.
- Apply with plan errors → **422 whose `detail` is the full payload**
  (`channels.py:688-689`); dry run always returns 200 echoing the plan.
  The UI therefore blocks Apply client-side while any `ERROR` row exists
  (mirror of the sync stepper's CONFLICT-blocks-apply).

## Tasks

### 1. Backend: `can_import_channels` capability

`api/session.py`: `SessionCapabilities` gains `can_import_channels: bool`
(wire `canImportChannels` via the existing `to_camel` alias);
`_derive_capabilities` sets it to
`_can(Permission.MANAGE_CHANNELS) and _can(Permission.MANAGE_GROUPS)` with
a rationale comment: the route needs MANAGE_CHANNELS always and
MANAGE_GROUPS whenever the roster carries `Group_ID` values, so the
conservative render hint requires both — a channels-only principal would
otherwise see a live import control whose group-bearing rosters 403
mid-flow (the silent-403 trap the module's other comments name).
Tests: `tests/api/test_session_api.py` — extend the existing capability
assertions (both-permissions → true; each alone → false).

### 2. Frontend types (`lib/api/types.ts`)

`ChannelImportFieldChange` (`{from: string | boolean | null, to: …}`),
`ChannelImportRowResult` (snake_case wire, `outcome` as the 4-literal
union), `ChannelImportResult`, and `canImportChannels: boolean` on
`SessionCapabilities`. Transcribe field-for-field from the verified
models — no invented fields.

### 3. Hook `useChannelImport` (`lib/api/useChannelImport.ts`)

One mutation-style hook exposing
`importChannels({file, contentOwnerId, dryRun, reason}): Promise<ChannelImportResult>`
building a `FormData` (`file`, `content_owner_id`, `dry_run`
`"true"|"false"`, `reason`; `cms_status` omitted → backend default
INSIDE_CMS) posted via the existing client (`withJsonBody` passes
`FormData` through untouched — `isRawBodyInit`, `client.ts`). House
mutation-hook shape per `useGroupSync.ts` (busy flag + typed ApiError
propagation). Test in `lib/api/__tests__/useChannelImport.test.tsx` per
the `useGroupSync.test.tsx` fetch-mock idiom: assert method/path,
FormData entry names and values (including the file part), dry-run flag
round-trip, and ApiError propagation.

### 4. `RegistryImportFlow.tsx` (new, `components/srcc/views/`)

`ActionStepper` with steps Upload → Preview → Applied, state machine per
`GroupsSyncFlow.tsx` (same `renderStepBody` shape, const arrows,
leaf-first ordering — the analyzer rules are now enforced repo-wide):

- **Upload:** file input (`.csv`), required reason
  (`isValidAuditReason` from GroupsSyncFlow — import it, do not copy),
  owner picker fed by `useContentOwners("youtube-analytics")` (disabled +
  pointer to Connectors when empty), CSV contract stated inline: required
  `youtube_channel_id, channel_name`; optional `Group_ID, view_revenue`;
  unknown headers rejected by the API. "Preview" fires `dryRun: true`.
- **Preview:** `OutcomeTable` — one row per CSV row: outcome chip
  (`Badge` tones: CREATE green, UPDATE blue, UNCHANGED muted, ERROR red
  via `tone: "warn"` row + verbatim backend `reason` text), changes
  summary (joined `field: from → to`), group effect, and the
  **revenue flag** (`row.revenue_required` — spec-mandated; on CREATE
  rows the changes mapping is empty by design, so this column is the
  only preview surface for the finance-sensitive default-true flag
  before the all-or-nothing apply; plan correction 2026-08-09 — the
  first draft dropped it and adversarial review caught the deviation).
  Counts strip above.
  **Apply disabled while any ERROR row exists** with the inline
  explanation that the API is all-or-nothing (it would 422). Apply fires
  `dryRun: false`.
- **Applied:** counts + reason echo + "Back to Registry" triggering the
  caller's `onDone` (which refetches channels).
- Errors: `describeApiError` banners; 503 → pointer to Connectors; 422
  (concurrent-editor apply race) → banner + return to Preview with the
  payload from `detail` when parseable.

### 5. RegistryView integration

`RegistryPanelHeader` (`RegistryView.tsx:203`): the disabled
"Bulk Import" placeholder (and its spec-non-goal comment) becomes a live
**Import CSV** button, rendered only when `canImportChannels` (hidden,
not disabled, per house gating). `RegistryView` root: an
`importing` state swaps the main content area for `RegistryImportFlow`
(cancel restores untouched; done restores + `reload()`). Wire the
capability from the session (AppShell passes `capabilitiesToPermissions`
— add `canImportChannels` to `AccessPermissions` and the mapping).

### 6. Validation + trackers

Full gates: `bun run test`, `bunx tsc --noEmit`, `bun run build`
(frontend); `uv run pytest -q` full suite on a fresh disposable postgres
(backend); `uv run ruff check backend tests scripts`. Trackers inline:
`Docs/01_IMPLEMENTATION_PLAN.md` frontend note + `Docs/15` dated entry;
`Docs/12` gains the additive `canImportChannels` mention in the session
capabilities list. Zero suppression markers anywhere; NUL byte-scans on
every written file.

## Non-goals (per spec)

No scheduled-import UI, no CSV editing/repair UI, no group
creation/rename UI, no new backend endpoints beyond the one capability
field, no router migration.
