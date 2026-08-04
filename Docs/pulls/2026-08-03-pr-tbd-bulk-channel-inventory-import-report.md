# PR TBD — Bulk Channel Inventory Import — Report

Branch: `feat/bulk-channel-inventory-import`
Base: `origin/main` at `6f867ec5`
Date: 2026-08-03

## Scope

Add `POST /channels/import` so an operator can load a YouTube CMS channel roster
(~169 channels for United Media Services, then three more content owners) from a
CSV in one call. Until now the only creation route was `POST /channels`, one
channel per call — the 2026-06-22 live smoke had to hand-seed its 25 channels
with raw SQL.

This is the blocker `Docs/01_IMPLEMENTATION_PLAN.md` names four separate times
(Phase 0 "300+ channels listed/classified", Phase 1 "Channel master table —
bulk inventory load", Phase 1 "Every active channel assigned", Phase 2 "Monthly
revenue facts — no real ingestion"). A full-roster ingest cannot run until the
roster exists in the registry with the right `content_owner_id` and
`cms_status`.

## Non-goals

- Outside-CMS and YPP-only channels (owner decision: no API path exists while
  those channels sit in a third-party MCN's CMS; tracked separately).
- CMS group sync from the YouTube API — deferred to a follow-up PR (see below).
- Creating org units, or mapping channels to companies via `primary_org_unit_id`.
  Imported channels land unmapped and are assigned through the existing Registry
  Map UI.
- Frontend wiring. The endpoint is API-first.

## The hazard this addresses

`youtube_channels.cms_status` defaults to `UNKNOWN`, and
`connectors/google/youtube_analytics_client.py::list_target_channels` selects
only `cms_status='INSIDE_CMS'` rows with a matching `content_owner_id`. A
channel imported without both is **silently skipped by ingest** — no error, no
alert, and its revenue never reaches `monthly_channel_revenue_facts`.

Three design choices follow directly from that:

1. Unknown CSV headers are **rejected**, not ignored. A mistyped header that
   silently drops a column is the same failure class.
2. A mandatory dry-run returns a field-level diff before anything is written.
3. Applying is all-or-nothing.

## Files changed

| Area | File |
| --- | --- |
| Schema | `db/org_models.py`, `db/alembic/versions/20260803_0001_channel_group_cms_id.py` |
| Pure core | `org/channel_import.py` (new, 291 lines) |
| Stores | `org/channel_registry.py`, `org/sql_channel_registry.py`, `org/channel_groups.py`, `org/sql_channel_groups.py` |
| Audit | `auth/audit.py` |
| Route | `api/channels.py` |
| Deps | `pyproject.toml`, `uv.lock`, `tests/test_version_baseline.py` |
| Tests | 9 files, ~1,240 lines |
| Trackers | `Docs/01_IMPLEMENTATION_PLAN.md`, `Docs/15_DELIVERY_BACKLOG.md` |

## Behaviour

**Endpoint.** `POST /channels/import`, `multipart/form-data`: `file` (CSV),
`content_owner_id` (required), `cms_status` (default `INSIDE_CMS`, validated
against the table CHECK), `dry_run` (required — no default, so nothing applies
by accident), `reason` (required — see Audit below).

**Permission.** `MANAGE_CHANNELS` at global scope, fail-closed. The existing
`POST /channels` scopes to `AccessScope.company(...)`, which does not extend
here: this import deliberately leaves `primary_org_unit_id` unset, so there is
no company to scope against, and the known unmapped-channel global-grant
dead-zone applies. A roster carrying any nonblank `group_id` additionally
requires `MANAGE_GROUPS` at global scope: the two permissions are
independently grantable, and group creation/membership must not bypass the
group API's authorization. Provision operators who import grouped rosters
with BOTH grants, or their grouped imports return 403.

**CSV contract.** Required `youtube_channel_id` (validated
`^UC[A-Za-z0-9_-]{22}$`) and `channel_name`. Optional `group_id` and
`view_revenue`. UTF-8, BOM-tolerant (Excel emits a BOM; Arabic channel names
make this load-bearing), case-insensitive and order-independent headers.

**Outcomes.** Each row resolves to `CREATE`, `UPDATE` (with a field-level diff),
`UNCHANGED`, or `ERROR`. Upsert is file-wins.

**Error handling.** Reporting is batch — every invalid row is returned in one
pass, so the operator never fixes one error at a time. Applying is
all-or-nothing — if any row is in `ERROR`, nothing is written.

**`view_revenue` → `revenue_required`.** These are semantically different: one
is a Google CMS permission flag, the other blocks month-close. The mapping is
nonetheless correct in practice — a channel whose revenue the credential cannot
read can never produce a fact, so marking it required would block month-close
permanently with no way to satisfy it. The raw token is recorded in the
per-channel audit detail so provenance is not lost.

**Grouping.** `group_id` maps to `channel_groups` + `channel_group_members`,
not `org_units`. YouTube CMS groups are many-to-many, which
`channel_group_members` mirrors with its composite primary key;
`channel_groups.group_type` already permits `SECTOR`; and PR #122 already made
`group` a finance scope type, so revenue rollups by CMS group work with no new
finance code.

## Schema change

One additive column: `channel_groups.cms_group_id TEXT NULL`, plus
`UNIQUE (tenant_id, cms_group_id)`. `channel_groups` previously had no column
for an external key. Nullable so groups created through other paths stay valid;
Postgres treats NULLs as distinct, so pre-existing groups do not collide.

Migration `20260803_0001`, `down_revision = 20260620_0001`. Verified upgrade →
downgrade → upgrade against Postgres 18.

## New dependency

`python-multipart==0.0.20` (exact-pinned, hashed in `uv.lock`, declared in
`tests/test_version_baseline.py`). FastAPI requires it for `Form`/`File` and
raises at **route-registration time** — without it `api/channels.py` becomes
unimportable and the whole suite breaks. No existing endpoint in the repo used
multipart, so it was not already present.

## Validation

All run locally against Postgres 18 (`ums-mig-pg-test`, port 55432).

| Gate | Result |
| --- | --- |
| `ruff check backend tests scripts` | All checks passed |
| `ruff format --check` (19 touched files) | All formatted |
| 100-char guard on touched `.py` | No violations |
| Full suite (`pytest -q`, PG set) | 2520 passed, 0 failed (post-review rounds) |
| Migration upgrade→downgrade→upgrade | Passed, single head `20260803_0001` |
| `git diff --check` | Clean |

Test counts by tier: 14 parser, 12 planner, 15 API (SQLite), 5 API (Postgres),
plus store and schema tests.

## Two defects found and fixed during implementation

**1. Missing audit reason (would have 500'd in production).**
`AUDIT_EVENT_DEFINITIONS` marks `CHANNEL_UPDATED` with `reason_required=True`,
and `audit_service.py::_normalize_audit_reason` **raises** when one is missing.
The original design had no `reason` field, so every upsert-update row would have
returned a 500. The endpoint now takes a required `reason`, threaded into all
three `record_audit_event` calls, with a test for the missing case.

**2. Group membership silently dropped on re-import.** The first implementation
skipped `UNCHANGED` rows entirely, including group attachment. Because plan
outcomes are computed only from inventory fields, this real sequence lost data
silently: import a roster with no `group_id`, then add the column and re-import
— every row is `UNCHANGED`, so no membership is ever attached, with a 200 and no
error. Fixed in `424d4daa`: membership is reconciled for `CREATE`, `UPDATE`, and
`UNCHANGED` alike, with a regression test.

## Risks and limitations

- **Mid-apply failures return an opaque 500, not a typed 4xx.** A concurrent
  create between the plan read and the apply would conflict. Data stays
  consistent (rollback verified), but the operator sees a 500. Follow-up.
- **Dry-run does not preview group membership changes.** The diff covers
  inventory fields only, so a row that would only gain group membership shows as
  `UNCHANGED`. The write is correct; the preview is imprecise.
- **`cms_status` is request-level, so file-wins can flip a deliberate
  `OUTSIDE_CMS` back to `INSIDE_CMS`.** This is the chosen upsert semantic and
  the dry-run diff surfaces it before it happens.
- The in-memory `ChannelRegistry` has no transaction, so all-or-nothing holds
  only on the SQL path. Production wires the SQL registry.

## Rollback

Revert the branch. The only schema change is the additive nullable column and
its constraint; `downgrade()` drops both. No change to existing revenue,
allocation, reconciliation, or connector behaviour.

**Downgrade is data-destructive once imports have run.** Dropping
`channel_groups.cms_group_id` discards the CMS group keys while leaving the
groups and memberships in place; if the schema is later re-upgraded and a
roster re-imported, `get_group_by_cms_id` cannot find the now-keyless legacy
groups and creates a SECOND group per CMS key. Before downgrading a database
where imports have populated the column, export a backup of
`(channel_groups.id, cms_group_id)` — e.g.
`COPY (SELECT id, cms_group_id FROM channel_groups WHERE cms_group_id IS NOT
NULL) TO ...` — and on re-upgrade backfill it into the recreated column BEFORE
any re-import (or merge/retire the duplicate groups it would otherwise
create).

## Next PR recommendation

CMS group sync. Verified available with the existing credential:
`groups.list?onBehalfOfContentOwner=<CO>&mine=true` returns every group a
content owner owns, and `groupItems.list` returns members with
`resource.kind == "youtube#channel"`. Authorized by `yt-analytics.readonly`,
which the current token already holds — no new access or consent. That sync
makes YouTube the source of truth for grouping, backfills real titles into
`channel_groups.name`, and supersedes the manual `group_id` column.
