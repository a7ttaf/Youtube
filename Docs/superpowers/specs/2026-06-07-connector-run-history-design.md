# Track D: Connector Run History + Test Connection — Design Spec

**Date:** 2026-06-07
**Status:** APPROVED (Mahmoud, 2026-06-07)
**Branch:** `feat/connector-run-history` (off `origin/main` 4eba7f4)
**Scope:** One PR. The single fully-buildable Track D chunk — no credentials,
no schema migration. The rest of Track D (OAuth consent flow, live pulls,
token-expiry/last-error schema, background monitoring) stays blocked.

## Goal

Surface connector run history on the dashboard (read API + UI, replacing the
"Run history not yet available" placeholder) and wire the existing
test-connection probe as a per-credential Test Connection button.

## Buildability Context (verdict that scoped this PR)

- `ConnectorRunORM` is schema-complete (`status`, `started_at`,
  `finished_at`, `counts_json`, `error_summary`, `triggered_by_user_id`,
  `connector_key`, `account_id`, `report_month`) but the repository has only
  writers (`start_run`/`link_raw_file`/`finish_run`) — **no list read method**
  — and there is **no `GET /connectors/runs` route**. The frontend
  `RunHistoryNote` explicitly says history is unavailable.
- `POST /connectors/credentials/{key}/{account}/test` already exists (PR #72),
  is gated by `MANAGE_CONNECTORS`, audits `CONNECTOR_TESTED`, and returns
  `status` ∈ {`ok`, `inactive_credential`, `auth_failed`, `error`} + detail.
  It only refreshes OAuth — no live data pull. It is NOT surfaced in the UI.
- `VIEW_CONNECTOR_HEALTH` (`connectors.view_health`) permission already exists
  and is purpose-named for read-only health/run visibility.

## Non-Goals (blocked, do not build)

- Public OAuth consent / authorization-code flow (intentional design non-goal).
- Live YouTube/AdSense pulls (needs real Google credentials).
- Token-expiry alerts, `last_refreshed_at`/`last_error` columns, background
  scheduler/monitor (needs new schema AND a scheduler — two missing
  substrates).
- No write path, no execution of jobs (the existing `POST /jobs` stays
  record-only).

## 1. Repository read method (`connectors/runs/repository.py`)

Add `list_runs(session, *, tenant_id, connector_key=None, account_id=None,
cursor_started_at=None, cursor_id=None, limit) -> ConnectorRunPage`.

- New frozen `ConnectorRunPage` dataclass: `items: list[ConnectorRunEntry]`,
  `limit: int`, `next_cursor: dict[str, str] | None`.
- Ordering: newest-first, `(started_at DESC, id DESC)` — deterministic tie-break
  on id.
- Cursor: both-or-neither. Supplying only one half raises
  `ConnectorRunValidationError` (mirrors the audit log's half-cursor contract).
  The cursor walks the `(started_at, id)` tuple: rows strictly older than the
  cursor position.
- Tenant-scoped: filter `tenant_id` always. Optional `connector_key` /
  `account_id` equality filters.
- `limit` validated 1..`MAX_CONNECTOR_RUN_PAGE_SIZE` (new module constant, 100
  to match the audit cap). Fetch `limit + 1` to compute `has_more`/next_cursor,
  return `limit`.
- Reuse the existing `_to_entry` serializer; `ConnectorRunEntry` gains a
  `to_api()` if it lacks one (counts, status, timestamps as ISO, error_summary,
  triggered_by_user_id, connector_key, account_id, report_month).

## 2. Read route `GET /connectors/runs` (`api/connectors.py`)

- Fail-closed `VIEW_CONNECTOR_HEALTH` gate at global scope (a dedicated
  `_require_connector_health` check; a viewer without it gets 403 and no data).
- Query params: `connector_key`, `account_id` (optional filters),
  `cursor_started_at` (datetime), `cursor_id` (str), `limit` (1..100, default
  50).
- Response envelope mirrors the audit list:
  `{items: [...], pagination: {limit, returned, has_more, next_cursor}}`
  where `next_cursor` is `{started_at, id}` or null.
- Typed `ConnectorRunValidationError` → 422 at the boundary.
- No audit emission (read of operational metadata; consistent with the
  credential-list route, which does not self-audit).

## 3. Frontend run history (`ConnectorsView.tsx` + hook)

- New `useConnectorRuns({connector_key?, account_id?, cursor...})` hook
  (`frontend/src/lib/api/useConnectorRuns.ts`), useAsync pattern, memoized
  params, builds `GET /connectors/runs`. Cursor both-or-neither (never send a
  half cursor).
- Replace `RunHistoryNote` with a RunHistory panel:
  - Fail-closed: render the restricted placeholder and fire NO fetch when the
    viewer lacks connector-health capability (same capability-gated pattern as
    the Audit page uses for `canViewAudit`). Capability comes from the session
    capability set already in AppShell.
  - Each run row: connector_key + account, status badge
    (`RUNNING`→blue, `SUCCEEDED`→green, `PARTIAL`→amber, `FAILED`→red), month,
    started/finished timestamps, counts breakdown (attempted/succeeded/failed,
    rows created/updated/unchanged), and `error_summary` when present.
  - Load More via `next_cursor`: append + dedupe by run `id` + reset on filter
    change (the pattern shipped in Track C AuditView).
  - loading / empty / error (403 → no-permission copy) states.

## 4. Test Connection button (`ConnectorsView.tsx` + hook)

- Per-credential button in the existing credentials table.
- New `useConnectorTest` hook → `POST /connectors/credentials/{key}/{account}/
  test` with a FIXED reason `"operator connection health check"` (one-click;
  the `CONNECTOR_TESTED` audit still records actor/credential/time).
- After probe: status badge (`ok`→green, `inactive_credential`→amber,
  `auth_failed`→red, `error`→red) + detail text. In-flight latch disables the
  button during the probe. Errors surface via the shared describeError pattern.
- Surfaced only for users who can manage connectors (the credentials table is
  already management-gated).

## Testing

Backend (`tests/connectors/runs/` + `tests/api/test_connectors_api.py`):
- `list_runs`: newest-first ordering, cursor walk + next_cursor correctness,
  half-cursor `ConnectorRunValidationError`, tenant isolation (other tenant's
  runs excluded), connector_key/account_id filters, `has_more` boundary.
- Route: 200 envelope + items shape; 403 fail-closed for a principal without
  `VIEW_CONNECTOR_HEALTH`; filters honored; half-cursor → 422; limit cap
  enforced.

Frontend (vitest): RunHistory render/empty/error/403; Load More append +
id-dedupe + filter reset; fail-closed no-fetch when lacking capability; Test
button happy (`ok`), `auth_failed`, in-flight disable, error.

Gates: `python -m ruff check backend tests scripts`, `python -m pytest -q`
(PG tier via `UMS_TEST_DATABASE_URL`), `npx tsc --noEmit`, `npx vitest run`,
`git diff --check`. All lines ≤100 chars in touched Python files.

## Blast Radius

Read-only run-history surface + one new read route + surfacing an existing
test route. No migration, no finance math, no write path, no auth weakening
(the new gate is a READ permission). No graph projection impact detected
(connector operational metadata only; `connectors/runs/repository.py`,
`api/connectors.py`).
