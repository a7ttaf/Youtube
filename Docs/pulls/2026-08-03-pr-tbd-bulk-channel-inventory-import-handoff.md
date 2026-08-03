# PR TBD — Bulk Channel Inventory Import — Handoff

Branch: `feat/bulk-channel-inventory-import`
Base: `origin/main` at `6f867ec5`
Date: 2026-08-03

## What this unblocks

The Google CMS connector is proven — the 2026-06-22 operator smoke produced 25
real `monthly_channel_revenue_facts` totalling $79,057.76, reconciling to the
cent. It had never run at scale because there was no way to get the roster into
the registry; the smoke hand-seeded its 25 channels with raw SQL.

After this PR, loading the full ~169-channel United Media Services roster is one
call. The other three readable content owners (UMS News, Al Watan Newspaper,
El Masrya) are the same call with a different `content_owner_id`.

## Operator runbook

**Always dry-run first.** `dry_run` has no default; nothing applies by accident.

```bash
curl -X POST "$UMS_BASE/channels/import" \
  -H "$AUTH_HEADERS" \
  -F "file=@ums-roster.csv" \
  -F "content_owner_id=PlZrS5Fh56RMd9dmSL6XSA" \
  -F "cms_status=INSIDE_CMS" \
  -F "dry_run=true" \
  -F "reason=August 2026 UMS roster load"
```

Read the returned `counts` and `rows`. Every row shows its outcome and, for
updates, exactly which fields change and from what. When it looks right, re-send
with `dry_run=false`.

**If any row is in `ERROR`, nothing is written.** The response lists every bad
row at once with its 1-based row number — fix the file and re-run the whole
thing. Re-running a clean file is safe: unchanged rows report `UNCHANGED`.

### CSV format

```csv
youtube_channel_id,channel_name,group_id,view_revenue
UCB6sc84dcg6VQGB_d89sx2g,CBC Egypt,cms-tv,Yes
UC3Dci3BzZXDo4jw4dU8KqWg,CBC Drama,cms-tv,Yes
```

`youtube_channel_id` and `channel_name` are required; `group_id` and
`view_revenue` are optional and may be present on some rows only. Headers are
case-insensitive and order-independent. **An unrecognised header is rejected** —
that is deliberate, because a silently ignored column would import channels with
the wrong `cms_status`, and the connector then skips them with no error.

Export as UTF-8. A BOM is fine (Excel adds one). Arabic channel names are fine.

### What the fields do

- `content_owner_id` and `cms_status` are request-level and apply to every row —
  one file per content owner.
- `view_revenue=No` sets `revenue_required=false`. This matters: a channel whose
  revenue the credential cannot read can never produce a fact, and marking it
  required would block month-close permanently. Omitting the column entirely
  defaults every row to required.
- `group_id` creates (or reuses) a `channel_groups` row of type `SECTOR` keyed
  by that CMS group key and attaches the channel. Because PR #122 made `group` a
  finance scope, rollups by CMS group work immediately.
- Imported channels have **no company mapping**. Assign them through the
  Registry Map UI afterward.

## Environment notes for the next agent

Two things cost time this session; both are now pinned in the plan.

- **Every Python command must go through `uv run`.** A bare `python -m pytest`
  fails — alembic and the app deps live in the uv-managed environment.
- **Postgres-tier tests fail rather than skip** without
  `UMS_TEST_DATABASE_URL`. A run without it reports ~21 failures and ~65 errors
  that look like regressions and are not. Use:
  `UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/test_ums`
  Container: `docker run -d --name ums-mig-pg-test -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=test_ums -p 55432:5432 postgres:18-alpine`
- Alembic reads `UMS_DATABASE_URL` (not `UMS_TEST_DATABASE_URL`) for its own
  invocations.

## Validation performed

All local, against Postgres 18. Current to the final commit.

- `ruff check backend tests scripts` — passed
- `ruff format --check` on all 19 touched files — passed
- 100-char guard on touched `.py` — no violations
- Full suite `pytest -q` with Postgres — 2463 passed, 0 failed
- Migration upgrade → downgrade → upgrade — passed, single head `20260803_0001`
- `git diff --check` — clean

The Postgres tier proves two things by measurement rather than argument: tenant
isolation under RLS (a bare `SELECT` with no `WHERE tenant_id` under tenant B's
lane returns nothing), and that a mid-apply failure rolls back **both** the
tenant session's channel rows and the separate platform session's audit rows.
The rollback test carries an anti-vacuity guard asserting the audit rows
physically existed in-flight before the failure, so it cannot pass trivially.

## Known limitations

- Mid-apply failures surface as an opaque 500 rather than a typed 4xx. Data
  stays consistent; the operator experience is poor. Follow-up.
- Dry-run previews inventory changes only, not group membership. A row that
  would only gain a group shows as `UNCHANGED`. The write is correct.
- File-wins means a stale file can flip a deliberate `OUTSIDE_CMS` back to
  `INSIDE_CMS`. The dry-run diff surfaces this before it happens — read it.
- Row cap 5,000, size cap 2 MiB. Both fail closed rather than truncating.

## Rollback

Revert the branch. `downgrade()` drops the additive `cms_group_id` column and
its constraint. No data migration; no change to revenue, allocation,
reconciliation, or connector behaviour.

## Next PR

**CMS group sync.** Verified available with the existing credential and no new
access: `groups.list?onBehalfOfContentOwner=<CO>&mine=true` returns every group
a content owner owns, and `groupItems.list?groupId=<ID>` returns members with
`resource.kind == "youtube#channel"` and `resource.id` as the channel ID. Both
are authorized by `yt-analytics.readonly`, which the current token already
holds. That sync makes YouTube the source of truth for grouping, backfills real
titles into `channel_groups.name`, and retires the manual `group_id` column.

Also open, from the outside-CMS analysis: those channels remain unreadable while
they sit under a third-party MCN's content owner, and channel-level analytics
returns zero revenue for pre-transfer periods after a channel moves. The
practical consequence is that **Digisay-era revenue history only ever exists in
their statements** — securing it contractually is time-sensitive and outside
this PR.
