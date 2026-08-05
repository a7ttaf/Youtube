# CMS group sync — design

Date: 2026-08-05
Status: approved for planning

## Context

PR #159 (squash `557ec1f0`) added the bulk channel inventory import and, with
it, the `channel_groups.cms_group_id` column (migration `20260803_0001`), the
`get_group_by_cms_id` / `create_group(cms_group_id=)` store surface, and a
manual `group_id` CSV column that attaches imported channels to CMS-keyed
groups. Its spec named this PR as the follow-up: make YouTube the source of
truth for grouping instead of an operator-maintained spreadsheet column.

The API surfaces (see the scope note after the list — `groupItems.list` needs
more than the already-granted `yt-analytics.readonly`):

- `GET https://youtubeanalytics.googleapis.com/v2/groups?onBehalfOfContentOwner=<CO>&mine=true`
  returns every group the content owner owns. Each group resource carries
  `id`, `snippet.title`, and `contentDetails.itemCount`. Paginated via
  `nextPageToken`.
- `GET https://youtubeanalytics.googleapis.com/v2/groupItems?groupId=<ID>&onBehalfOfContentOwner=<CO>`
  returns the group's members; channel members have
  `resource.kind == "youtube#channel"` and `resource.id` = the channel id.

**Scopes.** `groups.list` is covered by the already-granted
`yt-analytics.readonly`. `groupItems.list` is not: per Google's
[GroupItems: list authorization notes](https://developers.google.com/youtube/analytics/reference/groupItems/list)
it requires either `https://www.googleapis.com/auth/youtube` alone, or
`youtube.readonly` together with `yt-analytics.readonly`. Grouping is
homogeneous per group (`contentDetails.itemType` is one of `youtube#channel`,
`youtube#playlist`, `youtube#video`, `youtubePartner#asset`), and only
channel-type groups are mirrored.

## Goal

One operator call makes local channel grouping mirror the CMS exactly: real
group titles, real membership, for every group the content owner owns. After
this ships, grouping is edited in YouTube Content Manager, never locally.

## Decisions (owner-approved 2026-08-05)

1. **Full mirror — YouTube wins.** For groups carrying a `cms_group_id`:
   names are overwritten to YouTube's title, membership is reconciled with
   both adds and removals, and a group absent from YouTube is deactivated.
   Manually created groups (no `cms_group_id`) are never touched.
2. **Trigger = API route with mandatory dry-run**, following the operating
   pattern PR #159 established. No connector-orchestrator integration:
   `run_one` is month-shaped (`report_month`, source rows, normalization) and
   a group sync has neither a month nor source rows.

## Non-goals

- Scheduling / `ConnectorJobExecutor` integration (later PR; the route is the
  reusable core either way).
- Frontend wiring.
- Non-channel group members (`youtube#playlist`, `youtube#video`,
  `youtubePartner#asset`) — filtered out and counted, never stored.
- Any change to `POST /channels/import`. The `group_id` CSV column keeps
  working; sync converges whatever it created at the next run.
- Auto-creating channels discovered in CMS groups (see Unknown channels).

## Endpoint

`POST /channels/groups/sync`, JSON body:

| Field | Required | Notes |
| --- | --- | --- |
| `content_owner_id` | yes | Non-blank; the CMS to mirror |
| `dry_run` | yes | No default — nothing applies by accident |
| `reason` | yes | Threaded into every audit event (`GROUP_UPDATED` is `reason_required`) |

**Permission: `MANAGE_GROUPS` at global scope, fail-closed.** Sync spans
groups regardless of company mapping, and group writes must not bypass the
group API's own authorization (the PR #159 review round that added the
`MANAGE_GROUPS` gate on grouped imports established exactly this rule).

Returns `200` in both modes with the same payload shape:
`{dry_run, content_owner_id, counts, groups: [...], unknown_channel_ids,
non_channel_member_count}` — the dry-run/apply equivalence is what makes the
dry run a truthful preview. Each group entry also carries
`will_adopt_content_owner`, so the one write the mirror diff cannot express —
backfilling `content_owner_id` on an owner-NULL legacy row — is previewed too.
`counts` is the only field whose SOURCE differs by mode: a dry run reports the
plan's tally (what would happen), an apply reports the write boundary's (what
did), which is the same tally `GROUPS_SYNCED` persists.

## Data flow

1. **Credential** — resolved through the existing
   `resolve_connector_credentials` chokepoint with connector key
   `youtube-analytics` (alias handling included), so refresh telemetry is
   stamped exactly as connector runs stamp it. No new credential surface.
2. **Fetch** — `groups.list` (paginated, fail-closed page cap of 500 pages
   mirroring the reporting client's pattern), then `groupItems.list` per
   group (follow `nextPageToken` if present, same cap). Both through
   `GoogleHttpClient` (auth + retry + JSON decode). A new thin
   `YouTubeGroupsClient` in `connectors/google/youtube_groups_client.py`
   wraps the two calls; response-shape validation is fail-closed typed errors
   (`_response_object_list` pattern from the reporting client).
3. **Plan** — pure `org/channel_group_sync.py::plan_group_sync(snapshot,
   local_groups, known_channel_ids)`; no I/O, no session, following
   `channel_import.py`. Produces per-group entries with outcome
   `CREATE | RENAME | MEMBERS_CHANGED | DEACTIVATE | REACTIVATE | UNCHANGED`
   (an entry can carry both a rename and a membership diff; outcome is the
   dominant label, the entry carries the full field diff).
4. **Apply** — executes the plan through the existing
   `ChannelGroupRegistryStore` in the request's single tenant transaction.
   Dry-run returns the plan and writes nothing, including no audit rows.

## Mirror semantics, concrete

- **Scope guard.** Only local groups with `cms_group_id IS NOT NULL`
  participate. Manual groups are invisible to sync in both directions.
- **CREATE.** A CMS group with no local counterpart is created:
  `group_type='SECTOR'`, `active=true`, `name` = YouTube title,
  `cms_group_id` = YouTube group id, members = the snapshot's known channels.
- **RENAME.** Local name differs from YouTube title → overwritten,
  unconditionally.
- **MEMBERS_CHANGED.** Membership is set-reconciled: missing members added,
  extra members removed. Removal uses the store's existing `remove_member`.
- **DEACTIVATE.** A local synced group whose `cms_group_id` is not in the
  snapshot → `active=false`. Never deleted: finance history and membership
  rows survive, and the finance group-scope reader already excludes inactive
  groups.
- **REACTIVATE.** A deactivated synced group whose key reappears →
  `active=true`, then normal rename/membership reconciliation.
- **Unknown channels.** A CMS member channel absent from
  `youtube_channels` is skipped and counted per group
  (`unknown_channel_ids`, capped at 50 ids in the response with a total
  count). Channel creation is the import's job; creating channels here would
  bypass its `cms_status`/`content_owner_id` contract. The response makes the
  gap visible so the operator runs the import first.
- **Non-channel members.** Filtered out, counted once per response.

### Label-keyed groups consequence (owner-acknowledged)

If roster CSVs used labels ("TV Sector") rather than real YouTube group ids
as `group_id`, the first sync creates new groups under the real ids and
**deactivates** the label-keyed ones — they do not exist on YouTube, and that
is the mirror working as specified. The dry run shows this plainly before
anything happens. Operators who want continuity should either use real CMS
group ids in future CSVs or stop supplying `group_id` and let sync own
grouping.

## Group API lockdown

Manual edits to synced groups would silently survive until the next sync and
then vanish. Fail-closed instead: in `api/groups.py`, `PATCH /groups/{id}`
**rename** and `POST /groups/{id}/members` / `DELETE
/groups/{id}/members/{channel_id}` return a typed `409` with detail
`synced group <id> is managed by CMS sync; edit it in YouTube Content
Manager` when the target group carries a `cms_group_id`.

Allowed on synced groups: `PATCH` with `active` only (deactivation is
reversible and sync re-asserts state anyway). `group_type` is not editable
via the existing API, so no rule is needed for it.

## Audit

- Per changed group (CREATE / RENAME / MEMBERS_CHANGED / DEACTIVATE /
  REACTIVATE): one `GROUP_UPDATED` event — already `reason_required=True` and
  marked with `MANAGE_GROUPS`, exactly matching this route's gate — with the
  field-level diff, member add/remove counts, and `cms_group_id` in details.
- One `GROUPS_SYNCED` summary event (new `AuditEventType` value) per applied
  sync: content owner, counts by outcome, unknown-channel total,
  non-channel-member total. Summary counts are accumulated from the actual
  write-boundary outcomes, not copied from the plan (the PR #159 review rule).
- Dry-run writes no audit rows (preview is a read; `GET /revenue/scopes`
  precedent).

## Error handling

- Google auth/HTTP failures surface as the connector error taxonomy already
  used by the credential probe: 502 for upstream errors, 503 when the
  credential cannot be resolved/refreshed. Nothing is written — the fetch
  completes before any store write begins.
- Response-shape surprises (missing `id`, non-object items) are typed errors,
  fail-closed, no partial application.
- Mid-apply failure rolls back the whole transaction — same single-tenant-
  transaction guarantee PR #159 proved (audit rides `PlatformLaneAuditSink`
  through the same transaction).
- Concurrent sync of the same tenant: last-writer-wins at row level inside
  one transaction; both runs mirror the same upstream, so convergence is
  identical. No advisory lock in this PR.
- A synced group archived through the (still-permitted) `active`-only PATCH
  between planning and the apply is REACTIVATED in that same run when it is
  present upstream. The write boundary derives active from upstream presence,
  never from the plan's `active_change` diff, which is `None` for a group that
  was already active and therefore cannot express "should be active".
- Losing a race on a group's `content_owner_id` — the row was owner-NULL when
  this sync planned it and someone else claimed it before the apply took the
  row lock — is a typed `409`, not a partial mirror. The locked re-read
  re-verifies the entry's scoping premise, not only its mirrored fields;
  writing anyway would mutate the rival's group and misattribute the audit
  row. Same treatment as a CREATE that loses the tenant-unique key race.

## Testing

- **Pure planner** (no DB): create/rename/members-add/members-remove/
  deactivate/reactivate/unchanged; combined rename+membership entries;
  unknown-channel counting and cap; non-channel filtering; label-keyed
  deactivation scenario; deterministic ordering.
- **Client** (fake `GoogleHttpClient`): pagination following, page cap,
  shape-validation failures.
- **Route (SQLite):** 403 without global `MANAGE_GROUPS`; missing
  reason/dry_run/content_owner → 422; dry-run writes nothing (groups and
  audit both unchanged); apply mirrors (each outcome verified end-to-end);
  re-sync is all-`UNCHANGED`; lockdown 409s for rename/member-edit on synced
  groups while manual groups stay editable; `active`-only PATCH still allowed.
- **Postgres tier** (PR #159 fixture pattern): tenant isolation of synced
  groups; mid-apply failure rolls back groups, memberships, and audit rows
  together.

## Tracker updates (per-PR rule)

- `Docs/01_IMPLEMENTATION_PLAN.md` — Phase 1 "Company/sector/group mapping"
  and the Registry-gaps note: CMS group sync shipped; grouping source of
  truth is YouTube.
- `Docs/15_DELIVERY_BACKLOG.md` — add the sync under the Registry/grouping
  items; note the import's `group_id` column is now legacy-but-working.

## Rollback

Revert the branch. No migration in this PR (the column shipped in #159); the
lockdown and route disappear cleanly. Groups already mirrored keep their
synced names/membership — correct data, no cleanup needed.
