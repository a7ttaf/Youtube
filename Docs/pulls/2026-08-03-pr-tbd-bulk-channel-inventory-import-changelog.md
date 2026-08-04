# PR TBD — Bulk Channel Inventory Import — Changelog

Branch: `feat/bulk-channel-inventory-import`
Base: `origin/main` at `6f867ec5`
Date: 2026-08-03

## Added

- **`POST /channels/import`** — bulk channel roster load from operator CSV.
  `multipart/form-data` with `file`, `content_owner_id`, `cms_status`
  (default `INSIDE_CMS`), `dry_run`, and `reason`. Gated on `MANAGE_CHANNELS`
  at global scope, fail-closed; a roster carrying any `group_id` value
  additionally requires `MANAGE_GROUPS` at global scope (the permissions are
  independently grantable, and imports must not bypass the group API's
  checks). Returns a per-row plan
  (`CREATE`/`UPDATE`/`UNCHANGED`/`ERROR`) with counts and field-level diffs.
- **`backend/ums_smart_revenue/org/channel_import.py`** — pure, DB-free core:
  - `parse_channel_import_csv(text)` — UTF-8/BOM-tolerant CSV parsing,
    case-insensitive and order-independent headers, per-row validation with
    1-based row numbers.
  - `plan_channel_import(...)` — diffs parsed rows against the registry.
  - `ChannelImportOutcome`, `ChannelImportPlan`, `ChannelImportPlanEntry`,
    `ChannelImportRow`, `ChannelImportRowError`, `ParsedChannelImport`,
    `ChannelImportFormatError`.
- **`channel_groups.cms_group_id`** — additive nullable column plus
  `UNIQUE (tenant_id, cms_group_id)`. Migration `20260803_0001`
  (`down_revision = 20260620_0001`).
- **`ChannelRegistryStore.update_inventory(...)`** — replaces `channel_name`,
  `cms_status`, `content_owner_id`, and `revenue_required` in one call,
  preserving the `revenue_source_status` invariant that `create_channel`
  maintains. Implemented on both the in-memory and SQL stores.
- **`ChannelGroupRegistryStore.get_group_by_cms_id(...)`** and a
  `cms_group_id` keyword on `create_group(...)`. `ChannelGroupEntry` gains a
  `cms_group_id` field, surfaced through `to_api()`.
- **`AuditEventType.CHANNEL_IMPORTED`** — summary event for an applied import.
- **Tests** — 9 new files: 14 parser, 12 planner, 15 API (SQLite), 5 API
  (Postgres — the repo's first API-tier Postgres tests), plus store and schema
  coverage.

## Changed

- `Docs/15_DELIVERY_BACKLOG.md` — the Google ingestion foundation and the
  source-rows→facts normalization bridge lifted from ⏳ to ✅. Their remaining
  notes claimed "live Google connector credential setup (B2)" and "no live data
  source has produced facts yet"; both were satisfied by the 2026-06-22
  operator smoke (25 facts, $79,057.76, PRs #132/#134/#135). Remaining on both:
  FX/conversion (B3).
- `Docs/01_IMPLEMENTATION_PLAN.md` — "Channel inventory file format" marked ✅
  with the CSV contract; the three Phase 0/1 bulk-inventory items updated to
  note the load mechanism shipped; Status header refreshed to 2026-08-03.

## Dependencies

- Added `python-multipart==0.0.20` (exact-pinned, hashed in `uv.lock`, declared
  in `tests/test_version_baseline.py`). Required by FastAPI for `Form`/`File`;
  it raises at route-registration time, not request time.

## Fixed (within this branch)

- `424d4daa` — group membership was silently dropped for `UNCHANGED` rows, so
  adding a `Group_ID` column and re-importing an existing roster attached no
  groups at all while returning 200. Membership is now reconciled for `CREATE`,
  `UPDATE`, and `UNCHANGED` alike.
- Required `reason` added to the endpoint. `CHANNEL_UPDATED` is
  `reason_required=True` and the audit service raises without one, so every
  upsert-update row would otherwise have returned a 500.

## Not changed

No change to revenue calculation, allocation, reconciliation, month-close,
exports, connector behaviour, or existing permission semantics.
