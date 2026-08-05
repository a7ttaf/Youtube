# PR TBD — CMS Group Sync — Report

Branch: `feat/cms-group-sync`
Base: `origin/main` at `557ec1f0` (PR #159 squash)
Date: 2026-08-05

## Scope

Add `POST /channels/groups/sync`: one operator call mirrors a YouTube CMS
content owner's groups into `channel_groups` — real titles, membership with
adds AND removals, deactivation of groups that vanish upstream, reactivation
when a key returns. After this ships, channel-GROUP structure is edited in
YouTube Content Manager, never locally. This is the follow-up PR #159's spec
named: it makes YouTube the source of truth for grouping and turns the
import's manual `group_id` CSV column into a legacy-but-working input that
sync converges.

The API surface is `groups.list?onBehalfOfContentOwner=<CO>&mine=true` plus
`groupItems.list?groupId=<ID>`.

**Scope requirement (operator action may be needed).** `groups.list` is
authorized by `yt-analytics.readonly`, which the stored credential already
holds. `groupItems.list` is NOT: per Google's
[GroupItems: list authorization notes](https://developers.google.com/youtube/analytics/reference/groupItems/list)
it needs either `https://www.googleapis.com/auth/youtube` on its own, or
`youtube.readonly` **together with** `yt-analytics.readonly`. A credential
carrying only `yt-analytics.readonly` will therefore pass the group listing
and then fail during member fetch — surfacing as the route's canned 502
("YouTube groups fetch failed").

Before running a live sync, confirm the stored credential's `scopes` include
one of those combinations and re-consent if not. The connector reads scopes
from the stored secret payload, so this is a credential/runbook change, not a
code change.

## Non-goals

- Scheduling / `ConnectorJobExecutor` integration (the route is the reusable
  core for a later scheduled trigger).
- Frontend wiring.
- Non-channel group members (playlists/videos/assets) — filtered and counted,
  never stored.
- Any change to `POST /channels/import` semantics.
- Auto-creating channels discovered in CMS groups — unknown members are
  surfaced (capped id list + total) and skipped; the import remains the only
  channel-creation path, preserving its `cms_status`/`content_owner_id`
  contract.

## Behaviour

**Endpoint.** JSON body: `content_owner_id`, `dry_run` (required, no default),
`reason` (required — `GROUP_UPDATED` and `GROUPS_SYNCED` are both
reason-required audit events). Global `MANAGE_GROUPS`, fail-closed — group
writes must not bypass the group API's own authorization (the rule PR #159's
review rounds established for grouped imports).

**Mirror semantics — YouTube wins, synced groups only.** Local groups carrying
a `cms_group_id` participate; manual groups are invisible to sync in both
directions. Outcomes per CMS key: `CREATE` (new upstream group →
`group_type='SECTOR'`, title, members), `RENAME` (title overwritten),
`MEMBERS_CHANGED` (set-reconciled, adds and removals), `DEACTIVATE` (absent
upstream → `active=false`, never deleted — finance history survives and the
finance group-scope reader already excludes inactive groups), `REACTIVATE`
(key reappears → original local group returns, then rename/membership
reconcile in the same pass), `UNCHANGED`. An entry carries its full field
diff; the outcome is the dominant label.

**Dry-run is a truthful preview.** The response payload is identical in shape
and content for dry-run and apply — per-group outcome, name/active changes,
member add/remove lists, unknown-channel ids (capped at 50 with a full count),
non-channel-member total. Dry-run writes nothing, including no audit rows.

**Group API lockdown.** `PATCH /groups/{id}` with `name`, `POST
/groups/{id}/members`, and `DELETE /groups/{id}/members/{channel_id}` now
return a typed 409 (`managed by CMS sync; edit it in YouTube Content
Manager`) when the group carries a `cms_group_id`. An `active`-only PATCH
stays allowed (reversible; sync re-asserts state). Manual groups are
untouched — proven by tests on all four operations.

**Error taxonomy.** Canned messages only; exception text never reaches HTTP.
Credential missing/inactive → 503; OAuth refresh failure → 503; any other
credential-layer `GoogleConnectorError` (e.g. secret-fetch) → 503; fetch
failure → 502. The fetch completes fully before any store write.

**Audit.** One `GROUP_UPDATED` per changed group (source `cms_group_sync`,
field diff, member counts) + one `GROUPS_SYNCED` summary per applied sync.
Summary counts are accumulated from actual write-boundary outcomes, never
copied from the plan — enforced by a test that feeds a deliberately poisoned
plan-counts mapping and asserts the executed tally is reported instead.

## Architecture

- `connectors/google/youtube_groups_client.py` — thin fetch client over
  `GoogleHttpClient`; fail-closed shape validation, explicit 500-page cap.
- `org/channel_group_sync.py` — pure planner, no I/O.
- `org/channel_group_sync_apply.py` — apply + per-group audit, returns actual
  counts (mirrors `channel_import_apply.py`).
- `api/channels.py` — thin route; groups-client factory is a FastAPI
  dependency so tests inject canned snapshots and never touch the network.
- `api/groups.py` — lockdown helper `_reject_synced_group_edit`.

**One migration — `20260805_0001` (new Alembic head).** `channel_groups.
cms_group_id` shipped in PR #159 (`20260803_0001`). Review cleanup on this PR
adds `20260805_0001_channel_group_content_owner`, an additive nullable
`channel_groups.content_owner_id` that scopes a CMS-synced group to the
content owner whose sync created it (without it, syncing one owner deactivates
another owner's groups). It revises `20260803_0001`, so the head moves
`20260803_0001 -> 20260805_0001`.

**Deploy order matters:** run `alembic upgrade head` BEFORE the app code, which
reads and writes `channel_groups.content_owner_id`. Deploying the code first
would query a column the database does not have.

## Two defects found and fixed during implementation

**1. Audit rows were not atomic with group writes (proven, then fixed).** The
route initially took the generic `current_audit_sink` (independent platform
session) instead of the atomic wiring the import route uses. The Postgres
tier proved the consequence by measurement: on a tenant-commit failure after
a successful handler return, the group rows vanished but two audit rows
(`GROUP_UPDATED` + `GROUPS_SYNCED`) durably survived — an audit trail
describing a mirror that never happened (red run: tenant audit count 30 vs
expected 28). Fixed by renaming the import's dependency pair to
`current_atomic_audit_sink` / `sql_atomic_audit_sink_from_session`
(`PlatformLaneAuditSink` on the tenant transaction) and pointing BOTH routes
at it — one factory, two callers, no duplication. The new
`test_tenant_commit_failure_persists_no_audit_rows_on_postgres` was written
red-first and now passes.

**2. Credential-layer error gap + missing input hardening.** A
`GoogleConnectorError` subclass other than the three named ones (e.g.
`SecretFetchError`) escaped as a raw 500 during credential resolution — now a
canned 503. And `content_owner_id` lacked the import route's NUL/length
validation (it becomes `audit_logs.entity_id`, where an oversized value would
500 at the final audit append after passing every earlier check) — both
routes now share one `_validated_content_owner_id` helper.

## Validation

All local against Postgres 18 (`ums-mig-pg-test`).

| Gate | Result |
| --- | --- |
| `ruff check backend tests scripts` | All checks passed |
| `ruff format --check` (19 touched `.py`) | All formatted |
| 100-char guard on touched `.py` | No violations |
| Full suite `pytest -q` with Postgres | 2613 passed, 0 failed |
| Single Alembic head (one migration in PR) | `20260805_0001` |
| `git diff --check` | Clean |

Test tiers added: 12 client, 15 planner, 8 apply (incl. write-sequence
recording and the poisoned-plan-counts guard), 19 route (SQLite, incl. the
hardening cases), 6 lockdown, 4 Postgres (persist, RLS isolation via bare
un-filtered SELECT under tenant B, mid-apply rollback with the in-flight
anti-vacuity guard, lost-commit audit atomicity).

## Known limitations

- Concurrent syncs of the same tenant serialize at row level inside their own
  transactions; both mirror the same upstream, so convergence is identical.
  No advisory lock (deliberate — see spec).
- A store-level failure mid-apply surfaces as a 500; data stays consistent
  (full rollback proven on Postgres).
- The label-keyed-groups consequence (owner-acknowledged in the spec): roster
  CSVs that used labels rather than real YouTube group ids will see those
  label-keyed groups DEACTIVATED on first sync, with new groups created under
  the real ids. The dry run shows this before anything happens.

## Rollback

Revert the branch, then `alembic downgrade 20260803_0001` to drop
`channel_groups.content_owner_id` (the migration's `downgrade()` does exactly
that and nothing else). Order matters: revert the app code first, since it
reads that column. The route, client, planner, apply module, and lockdown
disappear cleanly. Groups already mirrored keep their synced names/membership
— correct data, no cleanup required; only the owner stamp is lost, and a
re-upgrade leaves it NULL, which sync tolerates (owner-NULL rows stay
matchable and are never deactivated by an owner that cannot claim them).

## Next PR candidates

- Scheduled sync via the connector-jobs executor (route is the core).
- Registry/frontend surface for sync results (unknown-channel list feeds the
  operator's import workflow).
