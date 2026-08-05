# PR TBD — CMS Group Sync — Handoff

Branch: `feat/cms-group-sync`
Base: `origin/main` at `557ec1f0`
Date: 2026-08-05

## Operator runbook

**Always dry-run first.** `dry_run` has no default.

```bash
curl -X POST "$UMS_BASE/channels/groups/sync" \
  -H "$AUTH_HEADERS" -H "Content-Type: application/json" \
  -d '{
    "content_owner_id": "<your-content-owner-id>",
    "dry_run": true,
    "reason": "August 2026 CMS group mirror"
  }'
```

Read the plan: per-group `outcome` (`CREATE` / `RENAME` / `MEMBERS_CHANGED` /
`DEACTIVATE` / `REACTIVATE` / `UNCHANGED`), name/active changes, member
add/remove lists, `unknown_channel_ids` (first 50 + full count per group),
and the non-channel-member total. When it looks right, re-send with
`"dry_run": false`.

Things to expect on a first run:

- **Label-keyed groups from old roster CSVs get DEACTIVATED** and re-created
  under their real YouTube group ids (they don't exist upstream under the
  label). This is the mirror working as designed — the dry run shows it
  before anything happens. Owner-acknowledged in the spec.
- **Unknown channels are not created.** A CMS group member missing from the
  registry is listed in the response; run `POST /channels/import` for those
  channels first, then re-sync.
- Re-running a converged sync returns all-`UNCHANGED`.

After sync, synced groups are **locked**: rename and member edits through the
groups API return 409 pointing at YouTube Content Manager. `active`-only
PATCH still works. Manual groups (no CMS key) are unaffected.

Permission needed: `MANAGE_GROUPS` at **global** scope. Credential needed: an
active `youtube-analytics` credential row for the content owner (the same row
the revenue connector uses).

**Scope check before the first live sync — the credential may need
re-consent.** `groups.list` is covered by the already-granted
`yt-analytics.readonly`, but `groupItems.list` is not: per Google's
[GroupItems: list authorization notes](https://developers.google.com/youtube/analytics/reference/groupItems/list)
it needs either `https://www.googleapis.com/auth/youtube` alone, or
`youtube.readonly` **together with** `yt-analytics.readonly`. A credential
holding only `yt-analytics.readonly` lists the groups and then fails every
live sync at member fetch (canned 502). Verify the stored credential's
`scopes` cover one of those combinations, and re-consent if not.

## Environment notes for the next agent

- Every Python command through `uv run`; alembic reads `UMS_DATABASE_URL`.
- Pytest needs `UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/test_ums`
  or the Postgres tier fails (raises, never skips). Container:
  `docker run -d --name ums-mig-pg-test -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=test_ums -p 55432:5432 postgres:18-alpine`
- The sync route's SQLite tests override `current_audit_sink`; the atomic
  wiring (`current_atomic_audit_sink`) deliberately passes through it — see
  the dependency's docstring before "simplifying" that.

## The audit-atomicity invariant (do not regress)

Both bulk import and group sync write their audit rows through
`current_atomic_audit_sink` → `PlatformLaneAuditSink` **on the tenant
transaction**. This is what makes a lost tenant commit take its audit rows
down with it. It was initially wired wrong on the sync route and proven wrong
by measurement (two orphan audit rows surviving a failed commit) before being
fixed. Any new route that writes domain rows + audit rows in one request must
use the same wiring. The regression tests are in
`tests/api/test_channel_group_sync_postgres.py` and
`tests/api/test_channels_import_postgres.py`.

## Validation performed

- Full suite with Postgres: **2613 passed, 0 failed**
- ruff check + format, 100-char guard: clean
- Single Alembic head `20260805_0001` — this PR adds ONE migration,
  `20260805_0001_channel_group_content_owner` (additive nullable
  `channel_groups.content_owner_id`, revises `20260803_0001`). Run
  `alembic upgrade head` BEFORE deploying the app code, which reads that
  column.
- Postgres tier proves: persistence, RLS tenant isolation (bare un-filtered
  SELECT under tenant B returns nothing), mid-apply rollback of groups AND
  audit together (with an in-flight anti-vacuity guard), and the lost-commit
  audit-atomicity path.

## The ownership invariant (do not regress)

`channel_groups.content_owner_id` is **adopt-only and monotonic**: NULL → an
owner is allowed (that is adoption), owner → different owner is refused by the
store, and no route clears a stamp. Two consequences that are easy to break:

1. The planner's scoped read deliberately returns owner-NULL rows so an
   existing `cms_group_id` cannot be re-planned as CREATE and collide on the
   tenant-unique key — but the DEACTIVATE gate must keep skipping them, or one
   owner retires another's groups.
2. The apply's locked re-read must re-verify **ownership**, not only the
   mirrored fields. A row that was owner-NULL at plan time can be claimed
   before the lock; mirroring it anyway writes the rival's group and stamps
   this owner's id on the audit row describing it. That path is a typed 409
   (`ChannelGroupOwnerReassignmentError`), covered by
   `test_apply_refuses_a_group_another_owner_claimed_mid_flight` and
   `test_group_claimed_by_another_owner_mid_apply_returns_409`.

Corollary for whoever picks this up: a wrong stamp is currently unrecoverable
through the API. See the report's known-limitations entry on the proposed
clear-stamp admin action before adding any new writer of this column.

## The write-boundary rule behind all of it

Everything above is one rule wearing three hats: **a plan is a snapshot; the
locked re-read is the record.** Anything the apply can re-derive from current
state it must, and anything it cannot must ride the plan explicitly rather than
be inferred from a diff:

- `active` comes from `upstream_present`, not `active_change` — a diff against
  the plan-time snapshot is `None` for a group that was already active and so
  cannot express "should be active" after a mid-flight archive.
- Ownership is re-verified, not assumed (above).
- An apply's response — `counts` AND the per-group entries — comes from what
  executed, never from the plan, so the body and the `GROUPS_SYNCED` row agree
  group by group. `apply_group_sync` returns a `GroupSyncExecution` (tally plus
  one `GroupSyncAppliedEntry` per plan entry, including the ones that wrote
  nothing) precisely so the route has something truthful to render.
- Conversely, a write the diff cannot express — the owner backfill — rides the
  plan as `will_adopt_content_owner` so the dry run still previews it.

**Membership is the one field still delta-based, and that is deliberate.**
`members_added`/`members_removed` are plan-time deltas filtered against current
state at the write boundary, not a set reconciled against the upstream roster.
So the analogous race exists: a same-owner import that adds a channel absent
upstream to a synced group between plan and apply leaves that channel in place
for this run — it is in neither delta set — and only the next sync removes it.
Manual edits cannot reach it (the group API 409s membership edits on synced
groups), so the only writer is a racing same-owner import: one interval, one
run, self-healing. Converging it would mean carrying the full upstream member
set on every entry and set-reconciling under the lock — a real change, not a
cleanup. If you do it, do it there, not by widening the deltas.

## Open threads beyond this PR

- **Owner decision needed:** should the bulk import adopt owner-NULL groups at
  all, or 409 with "sync first"? See the report's open-design-question section.
- **Clear-stamp admin action** — the recovery path a wrong stamp has no route
  to today.
- Scheduled sync (connector-jobs executor) — the route is the reusable core.
- Frontend surface for sync results.
- The standing outside-CMS reality: Digisay-held channels remain unreadable;
  Digisay-era revenue history exists only in their statements — the
  contractual data-delivery ask is time-sensitive and tracked outside this PR.
