# Bulk channel inventory import — design

Date: 2026-08-03
Status: approved for planning

## Context

The Google CMS connector is proven end-to-end. The 2026-06-22 live smoke ran
`run_google_connector.py --month 2026-04` against content owner
`PlZrS5Fh56RMd9dmSL6XSA` and produced 25 `monthly_channel_revenue_facts`
totalling $79,057.76, reconciling to the cent against the source rows.

It has never run at real scale. The blocker is not the connector — it is that
there is no way to get the channel roster into the registry.
`backend/ums_smart_revenue/api/channels.py` exposes exactly one write route for
creation, `POST /channels`, one channel per call. The June smoke hand-seeded its
25 channels with raw SQL.

`Docs/01_IMPLEMENTATION_PLAN.md` names this blocker four times:

- Phase 0 — "At least 300+ channels listed/classified/grouped"
- Phase 1 — "Channel master table — remaining: schema exists; bulk inventory load"
- Phase 1 — "Every active channel assigned or in unmapped list — remaining: bulk"
- Phase 2 — "Monthly revenue facts — not shipped (no real ingestion)"

`Docs/15_DELIVERY_BACKLOG.md` previously marked bulk import "definition-blocked:
bulk inventory import format". That is no longer true — the operator holds a
169-row United Media Services CSV, and this document defines the format.

## Goal

Load the full United Media Services channel roster (~169 channels), and later
the other three readable content owners (UMS News, Al Watan Newspaper, El Masrya
for Printing), into `youtube_channels` with the fields the connector requires,
so a real full-roster monthly ingest can run.

## Non-goals

- Outside-CMS and YPP-only channels. Revenue for channels held in a third-party
  MCN's CMS is not API-readable and is out of scope. YPP-connected channels are
  a separate future PR.
- CMS group sync from the YouTube API. Deferred to a follow-up PR (see
  "Follow-up" below).
- Creating org units. Import never creates `org_units` rows.
- Company/sector hierarchy mapping via `primary_org_unit_id`. Channels import
  unmapped on that axis; operators map them through the existing Registry UI.
- Frontend wiring. The endpoint is API-first; the Registry "Bulk Import" button
  is a later change.

## Critical hazard this addresses

`youtube_channels.cms_status` defaults to `UNKNOWN`. `list_target_channels` in
`connectors/google/youtube_analytics_client.py` requires
`cms_status == 'INSIDE_CMS'` **and** a matching `content_owner_id`. A channel
imported without both is silently skipped by the connector, and its revenue
never reaches `monthly_channel_revenue_facts` — with no error at ingest time.
The import must set both explicitly, and the dry-run must make that visible
before anything is written.

## CSV contract

UTF-8, BOM-tolerant (Excel exports a BOM; Arabic channel names make this
load-bearing). Header row required. Column order irrelevant. Header matching is
case-insensitive.

| Column | Required | Notes |
| --- | --- | --- |
| `youtube_channel_id` | yes | Validated `^UC[A-Za-z0-9_-]{22}$` |
| `channel_name` | yes | Non-empty after trim; UTF-8, Arabic expected |
| `group_id` | no | CMS group / sector key. Rows may omit it. |
| `view_revenue` | no | `yes/no/true/false/1/0`, case-insensitive |

**Unknown headers are rejected**, not ignored. A mistyped header that silently
drops a column is the same failure class as the `cms_status` hazard above.

Row-level validation, each producing a typed row error rather than aborting the
parse:

- Blank or malformed `youtube_channel_id`.
- Blank `channel_name`.
- `view_revenue` present but blank, or an unrecognised token.
- Duplicate `youtube_channel_id` within the file — **every** copy is flagged,
  not silently last-wins.

## Field mapping

| Registry field | Source |
| --- | --- |
| `youtube_channel_id` | CSV column |
| `channel_name` | CSV column |
| `content_owner_id` | Request field, applied to all rows |
| `cms_status` | Request field, default `INSIDE_CMS` |
| `revenue_required` | Derived from `view_revenue` (see below) |
| `active` | `true` |
| `primary_org_unit_id` | Untouched — out of scope |

### `view_revenue` → `revenue_required`

These are semantically different. `view_revenue` is a Google CMS *permission*
flag (can this credential read the channel's revenue). `revenue_required` is a
*finance* flag that blocks month-close when a required channel has no revenue
fact.

Mapping `yes → true`, `no → false` is nonetheless correct in practice: a channel
whose revenue the credential cannot read can never produce a fact, so marking it
required would block month-close permanently with no way to satisfy it. The
mapping is self-correcting — if revenue access is later granted, a re-import
flips the flag.

When the `view_revenue` column is absent entirely, `revenue_required` defaults
to `true` (a CMS revenue roster is expected to earn).

The raw `view_revenue` token is recorded in the per-channel audit detail so the
provenance of `revenue_required` is not lost.

## Group mapping

`group_id` maps to `channel_groups` + `channel_group_members`, not to
`org_units`.

Rationale: YouTube CMS groups are many-to-many (a channel may belong to several),
which `channel_group_members` mirrors with its `(group_id, channel_id)` composite
primary key. `channel_groups.group_type` already permits `SECTOR`. PR #122 already
made `group` a runtime finance scope type, so revenue rollups by CMS group work
immediately with no new finance code. Mapping to `org_units` would be
single-valued and would skip the `COMPANY` layer the hierarchy expects between a
channel and a sector.

**Schema change required.** `channel_groups` has no column for an external CMS
group key — its columns are `id`, `name`, `group_type`, `active`, timestamps,
`tenant_id`. This design adds one additive nullable column:

- `channel_groups.cms_group_id TEXT NULL`
- `UNIQUE (tenant_id, cms_group_id)`. A plain unique constraint is correct here:
  Postgres treats NULLs as distinct, so pre-existing groups with no CMS key do
  not collide with each other. The column is nullable precisely so groups
  created through other paths remain valid.

Groups are created on demand: a `group_id` with no matching `cms_group_id` for
the tenant creates a `channel_groups` row with `group_type='SECTOR'`,
`active=true`, `cms_group_id` set, and `name` initialised to the same value.
This is robust whether the operator's `Group_ID` is an opaque YouTube group ID
or a human label like "TV Sector" — the follow-up sync PR backfills real titles
from `groups.list` `snippet.title`.

Membership is idempotent by the composite primary key.

## API

`POST /channels/import`, `multipart/form-data`.

| Field | Required | Notes |
| --- | --- | --- |
| `file` | yes | CSV |
| `content_owner_id` | yes | Applied to every row |
| `cms_status` | no | Default `INSIDE_CMS`; validated against the `youtube_channels` CHECK values |
| `dry_run` | yes | **No default.** Nothing is applied by accident. |

Returns `200` in both modes — a batch with mixed per-row outcomes is not a
`201`-shaped result.

### Permission

`MANAGE_CHANNELS` at **global** scope, fail-closed.

The existing `POST /channels` checks `MANAGE_CHANNELS` at
`AccessScope.company(payload.primary_company_id)`. That per-company model does
not extend here: this import deliberately leaves `primary_org_unit_id` unset, so
there is no company to scope against, and the known unmapped-channel
global-grant dead-zone applies. A full-roster registry load is an administrative
operation; requiring a global grant is the fail-closed reading.

## Execution semantics

Each row resolves to exactly one outcome:

- `CREATE` — not present in the registry.
- `UPDATE` — present, with at least one differing field. Carries a field-level
  diff (only changed fields).
- `UNCHANGED` — present, no field differs.
- `ERROR` — validation failure, with a typed reason and the 1-based row number.

**Upsert, file wins.** Existing channels are updated to match the file, so the
CSV is the source of truth and a corrected file can be safely re-run across all
four content owners.

**Reporting is batch; applying is all-or-nothing.** Every invalid row is
reported in a single pass — the operator never fixes one error at a time. But if
*any* row is in `ERROR`, the apply writes nothing and returns the plan.
Importing 166 of 169 rows and not noticing is precisely the failure this system
exists to prevent.

`dry_run=true` returns the plan and writes nothing, including no audit row — a
preview is a read, matching the no-audit precedent of `GET /revenue/scopes` and
`GET /org-units`.

`dry_run=false` executes all `CREATE` and `UPDATE` outcomes in a single
transaction. `UNCHANGED` rows are skipped.

## Architecture

Three layers, matching existing repo shape.

**Pure core** — `backend/ums_smart_revenue/org/channel_import.py`:

- `parse_channel_import_csv(text) -> ParsedImport` — decode, header validation,
  per-row validation, duplicate detection. No I/O.
- `plan_channel_import(rows, existing, defaults) -> ImportPlan` — diff parsed
  rows against existing registry entries, produce per-row outcomes and counts.
  No I/O.

Both are pure and unit-testable without a database, following
`finance/rankings.py::build_month_rankings`,
`finance/revenue_scopes.py::build_authorized_revenue_scopes`, and
`derive_credential_health_state`.

**Route** — thin. Parses multipart, enforces permission, calls the pure core,
executes the plan through the registry store, maps typed errors to HTTP.

**Storage** — existing `ChannelRegistryStore`. `list_channels_by_ids` fetches
the entire existing set for the diff in one batched read. Group writes go
through the existing channel-group repository.

## Audit

- Per channel: existing `AuditEventType.CHANNEL_CREATED` for creates and
  `AuditEventType.CHANNEL_UPDATED` for updates — both already defined in
  `backend/ums_smart_revenue/auth/audit.py`. Detail includes `content_owner_id`,
  `cms_status`, `revenue_required`, and the raw `view_revenue` token.
- One import-summary event per applied import, carrying `content_owner_id`, row
  counts by outcome, and the source filename. This requires a **new**
  `CHANNEL_IMPORTED` value on the `AuditEventType` enum — no equivalent exists.

Dry-run writes no audit rows.

## Limits

Fail-closed, returning a typed error rather than truncating:

- Maximum upload size: 2 MiB.
- Maximum data rows: 5,000.

Silent truncation of a finance roster is unacceptable; exceeding either limit is
an error.

## Error taxonomy

| Condition | Status |
| --- | --- |
| Missing/insufficient permission | 403 |
| Unknown or missing required header | 422 |
| Any row in `ERROR` on apply | 422, with the full plan |
| Invalid `cms_status` or `content_owner_id` | 422 |
| Upload exceeds size or row cap | 413 / 422 |

## Testing

**Pure core (no DB):** header casing and ordering, BOM handling, Arabic names,
unknown header rejection, duplicate IDs flagged on every copy, channel-ID shape
validation, all `view_revenue` token forms plus blank-when-present, absent
column defaulting; planner outcomes for create/update/unchanged/error and
field-diff correctness.

**API:** 403 without a global grant; dry-run writes nothing (registry and audit
both unchanged); apply with any error row writes nothing; happy-path creates
channels, groups, memberships, and audit rows; re-running the same file yields
all `UNCHANGED`; a changed file updates; size and row caps enforced.

**Postgres tier:** tenant isolation under RLS, group uniqueness constraint, and
transaction rollback on a mid-apply failure.

## Tracker updates

Per the per-PR plan-status rule, this PR also corrects drift the June-22 live
smoke already invalidated:

- `Docs/15_DELIVERY_BACKLOG.md` — flip the two ⏳ Google ingestion items whose
  remaining note reads "live Google connector credential setup (B2)" and "no
  live data source has produced facts yet". Both were satisfied by the live
  smoke and PRs #132/#134/#135.
- `Docs/01_IMPLEMENTATION_PLAN.md` — mark the Phase 0 / Phase 1 bulk-inventory
  items and refresh the Status header date.

## Follow-up (separate PR)

CMS group sync. Verified available with the existing credential:

- `groups.list?onBehalfOfContentOwner=<CO>&mine=true` returns every group the
  content owner owns.
- `groupItems.list?groupId=<ID>&onBehalfOfContentOwner=<CO>` returns members,
  where `resource.kind == "youtube#channel"` and `resource.id` is the channel ID.
- Authorized by `yt-analytics.readonly`, which the current token already holds.
  No new access, consent, or credential is required.

That sync makes YouTube the source of truth for grouping, backfills real group
titles into `channel_groups.name`, and supersedes the manual `group_id` column.

## Rollback

Revert the branch. The only schema change is the additive nullable
`channel_groups.cms_group_id` column and its uniqueness constraint; its
`downgrade()` drops both. No data migration, no change to existing revenue,
allocation, or connector behaviour.
