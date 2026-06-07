# Track C: Audit View Completion + Confidence Surfacing — Design Spec

**Date:** 2026-06-07
**Status:** APPROVED (Mahmoud, 2026-06-07, with 6 adjustments incorporated)
**Branch:** `feat/audit-track-c` (off `origin/main` 231fb5e)
**Scope:** One PR covering four Track C gaps. A pre-existing uncommitted draft
(working-tree, likely from a parallel Codex window) is kept as the base and
corrected to this spec.

## Goal

Complete the Audit dashboard surface (Load More, event-type filter, CSV
download) and surface human-readable confidence labels on the Command view —
all against existing backend read models, plus one new read-only CSV route.

## Non-Goals

- No severity column or migration; no `sensitive` query param (follow-up if
  business needs it).
- No export-job pipeline (`POST /exports`), artifact store, or new export type
  for the audit download.
- No ranking panel, no outside-CMS monitor (Phase 5).
- CloseView untouched (it does not consume net-revenue data).

## 1. Audit "Load More" (frontend-only)

`AuditTimelineFeed` accumulates pages locally alongside `useAuditEvents`:

- First page fetched with no cursor; when `pagination.next_cursor` is non-null
  and `has_more` is true, render a "Load More" button.
- Clicking sets `cursor_created_at`/`cursor_id` (both-or-neither contract);
  the next page's items append to the accumulated list.
- Changing the event-type filter resets accumulation and cursor.
- Append dedupes by event `id` (StrictMode / refetch safety).
- While loading a subsequent page, the button is disabled and a loading row
  renders; first-page loading keeps the existing skeleton.

## 2. Event-type filter (frontend-only)

The placeholder dropdown becomes a live filter passed through as the existing
`event_type` query param. UI label is **"Event type"** (honest naming — no
severity facet exists). Options use REAL `AuditEventType` values:

| Label | value |
|---|---|
| All event types | (none) |
| Exports | `EXPORT_DOWNLOADED` |
| Mapping changes | `CHANNEL_UPDATED` |
| Month locks | `MONTH_LOCKED` |
| Allocations committed | `ALLOCATION_COMMITTED` |
| Logins | `LOGIN` |

No backend change. Filter change resets pagination (section 1).

## 3. `GET /audit/events/export` — synchronous CSV (backend + frontend)

This is a log-view download, not a finance export. No job lifecycle.

**Route contract** (in `backend/ums_smart_revenue/api/audit.py`):

- Same fail-closed permission gate as the list route (`audit.view` at global
  scope); same sensitive-payload visibility rule
  (`audit.view_sensitive_payloads`).
- Same filter params as the list route: `event_type`, `entity_type`,
  `entity_id`. **No cursor params** — the export always represents the current
  filter set from the beginning (newest first), never the UI's loaded page
  state. The frontend must not pass `cursor_created_at`/`cursor_id`.
- Row cap: **10,000 rows**, gathered by internal cursor iteration over the
  repository (same `exclude_event_type=AUDIT_LOG_VIEWED` exclusion as the
  list route). When the cap is hit, set response header `X-Truncated: true`.
- **Snapshot-before-audit:** query and materialize all export rows BEFORE
  emitting the download audit event, so the CSV can never contain its own
  download event. Failures use existing audit logging behavior (typed errors
  → HTTPException at the boundary; no audit event on failed permission).
- Audit emission: one `EXPORT_DOWNLOADED` event,
  `entity_type="audit_events_export"`, `entity_id=<event_type filter or
  "all">`, details carrying the filter set, returned row count, truncated
  flag, and `details_redacted`.
- Response: `text/csv`, `Content-Disposition: attachment;
  filename="audit-events.csv"`.

**CSV determinism and safety:**

- Stable column order, exactly:
  `created_at, event_type, user_id, entity_type, entity_id, scope_type,
  scope_id, request_id, reason, sensitive, details_redacted, details`
- `created_at` as ISO-8601 timestamp (`isoformat()`).
- Booleans serialized lowercase (`true`/`false`).
- `details` serialized as stable JSON (`sort_keys=True`, compact separators)
  for non-redacted rows.
- Redacted rows: `details` is the EMPTY STRING (`""`) and
  `details_redacted=true` — never `{}` and never raw sensitive payloads.
- CSV/Excel formula-injection guard on every string cell: values starting
  with `=`, `+`, `-`, `@`, tab, or CR are prefixed with `'` (no existing repo
  helper — add one in `api/audit.py`).

**Frontend:** the Download control is a button (not a plain `<a>`) that
fetches the export as a blob with the CURRENT filter (event_type only — no
cursor), triggers a browser save, and reads `X-Truncated`; when true, it
surfaces a visible truncation notice after download (silent truncation is
unacceptable for audit work). Disabled for viewers without audit permission.

**Docs:** `Docs/12_BACKEND_API_SPEC.md` documents the endpoint, columns, cap,
and `X-Truncated` semantics.

## 4. Confidence labels on CommandView (frontend-only)

Replace raw confidence codes ("B_RECONCILED") with a human label + tone via a
shared helper in `frontend/src/lib/` (reused by TraceView's pattern so the two
don't drift):

- **Prefer any human label already returned by the API**; the prefix map is a
  FALLBACK only.
- Prefix map: `A` → "Verified" (green), `B` → "Reconciled" (green), `C`/`D` →
  "Estimated" (amber), `E` → "Missing" (red); unknown → raw code, neutral tone.
- The raw code is preserved in `title` and `aria-label`.

## Testing

Backend (`tests/api/test_audit_api.py`):
1. Export success: 200, `text/csv`, attachment header, exact column order,
   ISO timestamps, stable JSON details.
2. Permission denial: 403 fail-closed, no audit event written.
3. Filters honored: `event_type` filter narrows rows.
4. **Redaction no-leak:** a sensitive event exported by a non-sensitive viewer
   yields `details=""`, `details_redacted=true`, and the raw payload string
   absent from the whole response body.
5. **Filters-not-cursor:** passing cursor params to the export does not change
   the result — export starts from the beginning.
6. Cap behavior: when matching rows exceed the cap, exactly 10,000 rows return
   and `X-Truncated: true` is set (exercised with a lowered injectable cap if
   seeding 10k rows is impractical).
7. Snapshot-before-audit: the returned CSV does not contain the export's own
   `EXPORT_DOWNLOADED` event, but a subsequent list/export call shows it.
8. Formula-injection guard: a value starting with `=` exports with a leading
   `'`.

Frontend (vitest): Load More append/reset/dedupe; filter wiring to
`event_type`; download button blob flow + truncation notice on `X-Truncated`;
export URL carries filters but never cursor; confidence label mapping
(API-label preferred, prefix fallback, raw code in title/aria).

Gates: `python -m ruff check backend tests scripts`, `python -m pytest -q`
(with PG tier via `UMS_TEST_DATABASE_URL`), `npx tsc --noEmit`,
`npx vitest run`, `git diff --check`.

## Blast Radius

Read-only audit surface + one new read route mirroring the list route's gate.
No migration, no finance math, no auth weakening, no write path.
No graph projection impact detected (audit read path only:
`backend/ums_smart_revenue/api/audit.py`; `auth/audit_log.py` shape untouched).
