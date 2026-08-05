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
for dry-run and apply — per-group outcome, name/active changes, member
add/remove lists, unknown-channel ids (capped at 50 with a full count),
non-channel-member total, and `will_adopt_content_owner` (the one write the
mirror diff cannot express). Dry-run writes nothing, including no audit rows.
`counts` is the only field whose SOURCE differs: the plan's tally for a dry
run, the write boundary's for an apply — the same tally `GROUPS_SYNCED`
persists, so body and audit row can never disagree.

**Group API lockdown.** `PATCH /groups/{id}` with `name`, `POST
/groups/{id}/members`, and `DELETE /groups/{id}/members/{channel_id}` now
return a typed 409 (`managed by CMS sync; edit it in YouTube Content
Manager`) when the group carries a `cms_group_id`. An `active`-only PATCH
stays allowed (reversible; sync re-asserts state). Manual groups are
untouched — proven by tests on all four operations.

Because that archive is allowed, the apply takes its target active state from
UPSTREAM PRESENCE (`upstream_present` on the plan entry), not from the plan's
`active_change` diff. The diff is `None` whenever the group was already active,
so a group archived in the plan-to-apply window would otherwise stay inactive
while present upstream, and the sync would report success on a mirror it had
not restored. `active_change` remains the operator-facing "what will visibly
flip".

**Error taxonomy.** Canned messages only; exception text never reaches HTTP.
Credential missing/inactive → 503; OAuth refresh failure → 503; any other
credential-layer `GoogleConnectorError` (e.g. secret-fetch) → 503; fetch
failure → 502. The fetch completes fully before any store write.

**Audit.** One `GROUP_UPDATED` per changed group (source `cms_group_sync`,
field diff, member counts) + one `GROUPS_SYNCED` summary per applied sync.
Summary counts are accumulated from actual write-boundary outcomes, never
copied from the plan — enforced by a test that feeds a deliberately poisoned
plan-counts mapping and asserts the executed tally is reported instead. An
apply's HTTP response reports that same executed tally, so the body and the
audit row can never disagree; a dry run still reports the plan's counts,
because "what would happen" is the whole point of that mode.

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

**Existing groups are adopted, never reassigned.** Every group predating the
migration is owner-NULL. Matching an owner's upstream CMS key is what proves
ownership, so the first sync (or a grouped import) under that owner backfills
the stamp; the write is audited (`adopted_content_owner`) and the dry run
previews it per group (`will_adopt_content_owner`). Adoption is one-way — the
store raises `ChannelGroupOwnerReassignmentError` on any attempt to move an
already-stamped group to a different owner, and the import route returns 409
rather than attaching a channel into another owner's group. Until a group is
adopted it stays matchable (so its `cms_group_id` cannot be re-planned as
CREATE and collide on the tenant-unique key) but is never deactivated by an
owner that cannot claim it.

Because owner-NULL rows are visible to *every* owner's plan, two owners can
race the same adoption. The apply's locked re-read therefore re-verifies the
entry's SCOPING PREMISE, not only its mirrored fields: a group claimed by
someone else in the plan-to-apply window raises
`ChannelGroupOwnerReassignmentError` → 409 for the whole sync. Declining only
the stamp would not be enough — the rename and membership writes would still
land on the rival's group, and the `GROUP_UPDATED` row would carry the losing
owner's `content_owner_id` on it, so the trail would misattribute the change.
Since the stamp is monotonic (no route clears one, reassignment is refused),
divergence from the dry run is one-directional: a previewed adoption may be
refused, a non-previewed one can never occur.

The import's cross-owner conflict is classified at PLANNING as well, via one
bulk `list_foreign_owner_cms_group_ids` lookup alongside the existing archived-
key query. It was previously only enforced at the write boundary, so a
`dry_run=true` roster referencing another owner's group returned a clean plan
and the real run then 409'd. Owner-NULL keys are excluded from that set — they
are adoptable, not conflicting — and the write-boundary recheck stays for the
plan-to-apply race.

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

Test tiers added: 15 client, 19 planner, 17 apply (incl. write-sequence
recording, the poisoned-plan-counts guard, the cross-owner mid-flight claim,
and the mid-flight archive), 28 route (SQLite, incl. the hardening cases, the
dry-run adoption preview, and the 409 for a group claimed mid-apply), 4
lockdown, 6 Postgres (persist, RLS isolation via bare un-filtered SELECT under
tenant B, mid-apply rollback with the in-flight anti-vacuity guard,
lost-commit audit atomicity, owner stamping, cross-owner scoping), 3 migration.
Plus the import-side additions: the cross-owner planning classification at
planner, store (in-memory and SQL, the latter pinning that an owner-NULL row
is NOT a conflict), and route-preview tiers.

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
- **A wrong owner stamp has no API-level recovery path.** Adoption is
  adopt-only-forever by design, so any route into a wrong stamp — losing the
  adoption race above, or an import CSV that CREATEs a group under a
  `cms_group_id` that is really another owner's upstream key — leaves a row
  only hand-run SQL can repair, and the affected owner's sync 409s on every
  subsequent cycle. Single-content-owner deployments cannot reach this state.
  The companion fix is a guarded, audited, `MANAGE_GROUPS`-global *clear-stamp*
  admin action (reset to NULL, reassignment still forbidden): clearing asserts
  nothing false, returns the row to the unclaimed pool, and the rightful
  owner's next sync re-claims it with upstream proof through planner logic that
  already exists. Deliberately NOT in this PR — it is a new write surface, and
  it belongs with the open question of whether the import should adopt at all
  (see below).

## Rollback

Revert the branch, then `alembic downgrade 20260803_0001` to drop
`channel_groups.content_owner_id` (the migration's `downgrade()` does exactly
that and nothing else). Order matters: revert the app code first, since it
reads that column. The route, client, planner, apply module, and lockdown
disappear cleanly. Groups already mirrored keep their synced names/membership
— correct data, no cleanup required; only the owner stamp is lost, and a
re-upgrade leaves it NULL, which sync tolerates (owner-NULL rows stay
matchable and are never deactivated by an owner that cannot claim them).

## Open decision for the owner: import adoption, Path A or Path B

Not an open-ended question — two paths, both cheap now, and the status quo is
the one position neither defends.

The principle this PR establishes is that anything knowable from stored state
belongs to PLANNING, because a preview that reports a clean plan for work the
apply will reject is a preview that lies. That is why the import's cross-owner
conflict moved to planning above. **Adoption is equally knowable at planning
time** — group exists, key matches, `content_owner_id IS NULL` — so as things
stand the import's preview surfaces the *retryable* outcome (conflict) and
hides the *permanent* one (a stamp minted on CSV evidence and never revocable).

The evidence asymmetry is the crux. Owner-NULL does not mean *unclaimed by
anyone*; it means *claim unknown* — the row predates the column and somebody's
CMS probably does own it upstream. Sync has proof an import cannot get: an
authenticated `groups.list(onBehalfOfContentOwner=X)` returning that key is
Google co-signing the claim. A CSV cell is not that.

- **Path A — the import stops adopting.** Owner-NULL becomes a third branch of
  `_blocked_group_reason`: "legacy group unclaimed: run POST
  /channels/groups/sync for owner X to claim it, then retry". One predicate in
  a pass that now exists. It also closes the pre-stamp membership-injection
  window and makes the adoption race unreachable — a CMS key is upstream in at
  most one owner's CMS, so syncs alone can never race each other. Cost: a
  one-time sync-before-import ordering for legacy rows, per owner. (This
  deployment has one content owner, so the friction is ~zero.)
- **Path B — the import keeps adopting.** Then it must at minimum surface a
  per-row adoption flag in its preview, mirroring the sync's
  `will_adopt_content_owner`, and the clear-stamp action below stops being a
  nice-to-have: a wrong stamp still bricks the rightful owner's next sync
  through the CREATE-collision path, which this PR's guard does not change.

Neither is decided here — both alter import semantics or add surface, and that
is not a review-cleanup call.

## Next PR candidates

- Scheduled sync via the connector-jobs executor (route is the core).
- Registry/frontend surface for sync results (unknown-channel list feeds the
  operator's import workflow).
