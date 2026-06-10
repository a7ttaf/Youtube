# Audit summary endpoint + tracker-doc hygiene

**Date:** 2026-06-10
**Branch:** `feat/audit-summary-endpoint` (off main `e92efd2`, #88)
**Status:** Approved direction (Mahmoud: "proceed", recommendations pre-approved)

## Problem

The Audit screen's summary tiles render hardcoded mock figures
(`AUDIT_SUMMARY` from `frontend/src/lib/mock/data.ts`) with a visible "live aggregate endpoint
coming" disclaimer. There is no `GET /audit/summary` aggregate endpoint. Separately, several
tracker-doc lines still claim Registry is "mock-only" (it has been wired to `GET /channels` since
PR #73/#78) and the P0 backlog marks the month-close screen + smart-issue panel "UI not built"
though both shipped in PR #69.

## Goal

1. Add a thin, tenant-scoped, fail-closed `GET /audit/summary` aggregate endpoint and wire the
   Audit tiles to it (replacing the mock), removing the disclaimer.
2. Reconcile the stale tracker-doc lines so the docs match merged reality.

No schema change, no migration, no new permission, no auth change.

## Part 1 — `GET /audit/summary`

### Contract (locked decisions)
- **Route:** `GET /audit/summary` on the existing audit router (`api/audit.py`, prefix `/audit`).
- **Gate:** `_require_permission(user, Permission.VIEW_AUDIT_LOG, AccessScope.global_scope())` —
  identical to `GET /events`, fail-closed (403 `"Missing permission: audit.view"`). Do **NOT**
  require `VIEW_SENSITIVE_AUDIT_PAYLOADS`: the response is counts only — no `details`, no payload,
  no per-row content — so it is redaction-safe for a plain audit viewer.
- **No self-audit.** A read aggregate must not emit an `AUDIT_LOG_VIEWED` row: it would be
  inconsistent with other read-aggregate endpoints and would pollute the very counts it returns
  (which is exactly why `GET /events` had to exclude `AUDIT_LOG_VIEWED` from its own listing).
- **Exclude `AUDIT_LOG_VIEWED`** from every count, for parity with the `/events` list view's notion
  of "real" events (it already excludes that self-event).
- **Tenant scoping:** reuse `current_audit_log_repository` (already tenant-bound via
  `_resolve_tenant_id`); the new aggregate query filters `WHERE tenant_id = self._tenant_id`.
- **Drop "Denied attempts":** not derivable — permission denials raise `HTTPException(403)` at the
  route boundary and write **no** audit row (proven by `test_audit_events_export_denied_without_permission`).
- **Window param:** `window_hours: Annotated[int, Query(ge=1, le=8760)] = 24` for the
  recent-activity count; total/sensitive are lifetime (tenant-scoped, window-independent).

### Response shape
```json
{ "total_events": 1840, "sensitive_events": 230, "recent_count": 184, "window_hours": 24 }
```
- `total_events` — lifetime tenant count, excluding `AUDIT_LOG_VIEWED`.
- `sensitive_events` — lifetime tenant count where `sensitive = true`, excluding `AUDIT_LOG_VIEWED`.
- `recent_count` — count where `created_at >= now() - window_hours`, excluding `AUDIT_LOG_VIEWED`.
- `window_hours` — echoed back.

`by_event_type` breakdown is intentionally **out of scope** (YAGNI — no tile consumes it; can be a
later additive field if a breakdown UI is built).

### Layering (CLAUDE.md non-negotiables)
- Route stays thin: gate → call repository aggregate → shape the Pydantic response. Translate
  `AuditLogValidationError` → `HTTPException(422)` at the boundary, mirroring `GET /events`.
- Add the aggregate as a **repository method** `count_summary(*, window_hours: int)` on
  `SqlAlchemyAuditLogRepository`, returning a small typed result (e.g. `AuditSummaryCounts`
  dataclass). No inline SQL in the route. Counts use the existing indexes
  (`ix_audit_logs_event_created`, `ix_audit_logs_tenant_id`).

### Frontend
- New `useAuditSummary` hook (`frontend/src/lib/api/`) → `GET /audit/summary`, mirroring the
  existing audit hooks' fail-closed no-fetch-when-restricted pattern.
- `AuditView.tsx`: replace the `AUDIT_SUMMARY` mock import with tiles fed by the hook:
  - "Events (24h)" → `recent_count`
  - "High sensitivity" → `sensitive_events`
  - "Total events" → `total_events`
  - "Retention" → keep as a **static frontend constant** (policy text, not an aggregate).
  Remove the "live aggregate endpoint coming" disclaimer (JSX comment + visible note). Honest
  loading/error/empty states; fail-closed (no fetch when the viewer lacks the capability).

### Tests
- Backend (mirror `tests/api/test_audit_api.py` patterns + roles): 200 for `audit_viewer`;
  403 fail-closed for `assistant_analyst`; `sensitive_events` counts the sensitive row;
  `recent_count` respects `window_hours` (seed a row outside the window, assert excluded);
  `AUDIT_LOG_VIEWED` excluded from counts; 422 on out-of-range `window_hours`; tenant isolation
  if a cross-tenant fixture exists; no audit row written on success (no self-audit).
- Frontend: hook + tiles render from a stubbed response; restricted viewer → no fetch.

## Part 2 — Tracker-doc hygiene (exact punch list)

Update (Registry is wired to `GET /channels`, PR #73/#78):
- `Docs/01_IMPLEMENTATION_PLAN.md` lines ~57, ~633-634, ~651-652, ~664-665, ~676-677.
- `Docs/15_DELIVERY_BACKLOG.md` lines ~490, ~504, ~510.

Flip ⏳→✅ to match the plan (both shipped PR #69):
- `Docs/15_DELIVERY_BACKLOG.md` ~148-149 (Finance month-close screen / CloseView) and
  ~165-167 (Smart issue panel).

**Leave intact** (legitimate "mock" references):
- `Docs/09_SMART_DASHBOARD_UI.md` ~142-159 (the static HTML product mockups).
- The connector "mock end-to-end ingestion gate" test references: `Docs/01` ~240-241/264-265,
  `Docs/15` ~63-64/87-88.
- Correct historical "replacing the mock" narrative (`Docs/01` ~704/709, `Docs/15` ~506/534).

Per-PR doc discipline: also add the PR-B entry to `Docs/01` and/or `Docs/15`.

## Blast-radius review

- **Tables/models:** `audit_logs` (`AuditLogORM`) — READ-ONLY aggregate (COUNTs). No write, no
  schema change, no migration.
- **PostgreSQL remains source of truth.** Yes.
- **Authorization more permissive?** No — same `VIEW_AUDIT_LOG@global` gate as `/events`,
  fail-closed; counts disclose no payload (redaction-safe), so no sensitive-payload gate needed and
  none removed.
- **Neo4j:** `No graph projection impact detected.`
- **Finance results / locks / overrides:** unaffected (audit-only).
- **Backward compatible / rollback:** additive endpoint + frontend wiring + doc edits; code-only
  revert.
