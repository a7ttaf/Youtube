# Phase 5 Analytics & Monitoring Surface — Design + Plan

**Date:** 2026-06-13
**Branch:** `feat/phase5-analytics-monitoring` (off `main` 60a3b38, #97)
**Type:** One combined PR (backend + frontend + doc reconciliation).
**Goal:** Close the highest-value, fully-unblocked Phase 1 / Phase 5 / Phase 7 acceptance-gate
gaps in one coherent PR, and reconcile the original plan docs (stale at ~#71) to reality
through #97.

This is grounded in the 2026-06-13 plan-vs-code reconciliation (8-agent audit) and the
6-agent integration-point sweep. All anchors below were verified against the live code.

---

## Components

### A. `canViewAnalytics` session capability (backend + FE) — small
The SPA cannot gate an analytics panel: `SessionCapabilities` has no analytics flag.
`Permission.VIEW_ANALYTICS` already exists; nearly every role holds it, many only at
company/sector scope.

- **Backend** `api/session.py`: add `can_view_analytics: bool` to `SessionCapabilities`;
  derive it **scope-aware** (true if the principal holds ANY active `VIEW_ANALYTICS` grant —
  direct or via role — at any scope), mirroring the `connector_health` scope-aware pattern,
  NOT the global-only `_can()` helper (a global-only check would hide the panel from a
  legitimately company-scoped analytics user). Fail-closed: disabled → false.
- **FE** `types.ts` `SessionCapabilities`: add `canViewAnalytics: boolean`.
- **FE** `AppShell.tsx` `capabilitiesToPermissions`: map `canViewAnalytics` into the
  `AccessPermissions` object; thread to CommandView via `ViewRouter`.
- **Tests**: `tests/api/test_session_api.py` — assert `canViewAnalytics` per role
  (true for analytics-holding roles incl. scoped company_manager; false for a role without it).

### B. Mapping-route month-lock enforcement (backend) — small/medium
`PATCH /channels/{id}/mapping` currently re-parents a channel with NO finance-lock check, so
an org-mapping admin can silently rewrite a LOCKED month's company/sector attribution.

- **New typed error** `ChannelMappingLockedMonthError` (in `org/channel_registry.py`,
  alongside the existing `ChannelRegistryConflictError`/`ChannelRegistryValidationError`).
- **Service guard** `SqlAlchemyChannelRegistry.update_mapping` (`org/sql_channel_registry.py`):
  before mutating `primary_org_unit_id`, run a **read-only** query — does this channel have any
  `MonthlyChannelRevenueFactORM` row whose `month` is a `FinanceMonthCloseORM(status='LOCKED')`
  for the tenant? If yes → raise `ChannelMappingLockedMonthError` naming the locked month(s).
  Read-only (no `get_or_create` row creation → RLS-safe, no platform-only-write lane issue).
  Tenant-consistent (same `self._tenant_id`). The concurrent-close race (a month locking
  between check and flush) is a narrow, documented limitation (mirrors the PR #57 N9 posture).
- **In-memory** `ChannelRegistry.update_mapping`: unchanged (no DB/lock concept) — the guard
  is SQL-path only, so production cannot bypass it; bootstrap tests stay green.
- **Idempotent / no-op PATCH**: a request whose `primary_company_id` matches the row's
  current `primary_org_unit_id` MUST short-circuit BEFORE the locked-month lookup and MUST
  return 200 unchanged. The guard is meant to prevent re-parenting that would rewrite a
  closed month's attribution; a request that doesn't change the mapping cannot rewrite
  anything, so safe retries and "resubmit current value" flows stay 200. Implementation:
  parse the requested `primary_company_id` into a UUID first, compare to
  `row.primary_org_unit_id`, return `self._to_entry(row)` early on match. This also keeps
  the org-only caller path valid (no finance schema needed) when the request is a no-op.
  The route suppresses the `CHANNEL_UPDATED` audit when the request is a no-op (the audit
  decision lives at the route boundary, where actor + reason are bound).
- **Route** `api/channels.py` `update_channel_mapping`: add
  `except ChannelMappingLockedMonthError as exc: raise HTTPException(409, detail=str(exc))`
  to the existing try/except (after the permission gate, before audit — a rejected change must
  NOT audit). 409 CONFLICT (codebase uses 409 universally for locked months; no 423 anywhere).
  Permission gate + audit unchanged for the change-path; suppress the `CHANNEL_UPDATED` audit
  when `current_channel.primary_company_id == payload.primary_company_id` (the no-op path
  returns 200 with `audit_event: null`).
- **Tests**: `tests/api/test_channels_api.py` — SQL-backed app (copy
  `test_channel_account_links_api.py::test_verify_locked_month_returns_409`): seed a channel
  with a fact in a `FinanceMonthCloseORM(status='LOCKED')` month → PATCH → 409 + 'locked' in
  detail; an OPEN-month fact → 200; a no-op PATCH against a LOCKED-month channel → 200 with
  no audit. `tests/org/test_sql_channel_registry.py` service unit test (no-op returns
  without finance schema; org-only fixture stays valid through the no-op short-circuit);
  `tests/org/test_channel_registry.py` asserts in-memory impl unchanged.

### C. Missing-channel coverage detection (backend) — medium
Extend the smart-alerts engine with a per-channel coverage gap. Closes the open half of the
Phase 7 detection gate.

- **Service** `finance/smart_alerts.py` (stays PURE — no DB): add two new keyword args
  `missing_revenue_fact_channel_count: int = 0` and
  `missing_revenue_fact_channel_sample: Sequence[str] = ()` (pre-read in the route, server-side
  `LIMIT MISSING_FACT_CHANNEL_SAMPLE_LIMIT` cap on the sample) and a new alert block emitting
  code **`CHANNELS_MISSING_REVENUE_FACTS`**, severity `HIGH`, confidence `E_MISSING`, when
  `count > 0` or the sample is non-empty. `details = {"channel_count": int,
  "sample_channel_ids": [...capped at 20, sorted...]}` (count + capped sample, mirroring the
  close-readiness pattern; do not dump the full registry). The count + sample shape keeps
  the read bounded: a tenant with thousands of factless channels does not materialize a
  full id list on the application side or across the wire. Append after
  `MISSING_REVENUE_SOURCE`, before `PAYMENT_NOT_MATCHED`. Distinct from the month-level
  `MISSING_REVENUE_SOURCE` (zero-YouTube-revenue) — this is per-channel coverage, matching
  close-readiness `MISSING_REVENUE_FACTS`.
- **Route** `api/revenue.py` `get_month_smart_alerts`: read the active+revenue_required
  channels with no fact for the month using the SAME query shape as
  `month_close_readiness._missing_required_revenue_fact_count` (LEFT JOIN facts, `active.is_(True)`
  AND `revenue_required.is_(True)` AND `fact.id IS NULL`, tenant-scoped, **no** for_update lock —
  read-only), in TWO bounded queries: a `func.count()` aggregate and a `LIMIT
  MISSING_FACT_CHANNEL_SAMPLE_LIMIT` ordered sample. Pass the (count, sample) pair into the
  builder. No new permission (stays within the existing finance gate; uses only finance
  source-of-truth tables). The same helper also accepts an optional
  `youtube_channel_ids: set[str] | None` parameter used by the export helper to
  scope the read to a frozen channel set; the smart-alerts API endpoint
  passes None to keep the tenant-global view.
- **Missing-REPORT detection DEFERRED** — `connector_runs` carries no channel dimension and there
  is no stored "expected connectors/accounts per tenant-month" baseline; surfacing connector
  operational metadata would also widen the finance-only gate. Recorded as a named follow-up
  (needs an expectation/coverage-baseline model).
- **Tests**: `tests/finance/test_smart_alerts.py` (pure: list non-empty → code present at the
  right ordering; empty → absent; details shape); `tests/api/test_smart_alerts_api.py`
  (seed a 2nd active revenue_required channel with no fact → code present; update the existing
  ordered-`[alert.code …]` assertions). `Docs/12_BACKEND_API_SPEC.md` alert-code list updated.

### D. Company/sector/channel rankings (backend + FE) — large
New finance-gated, scope-safe ranking over the existing per-channel net-revenue summary,
rolled up by org-unit. Pure finance service + thin route; client renders only.

- **Service** `finance/rankings.py` (NEW, pure): `build_month_rankings(*, summary:
  MonthNetRevenueSummary, channel_company: dict[str,str], channel_sector: dict[str,str],
  company_names: dict[str,str], sector_names: dict[str,str], metric: str, limit: int) ->
  MonthRankingsSummary`. Rolls the per-channel `ChannelNetRevenueSummary` rows up to company and
  sector (summing `adjusted_gross_revenue_usd`, `net_revenue_usd`, `deduction_amount_usd`;
  None-net handled — a group's net is None only if it has NO non-None member, else sum of
  non-None... **decision: a group total sums the non-None contributions; if every member's
  metric is None the group metric is None**). Ranks each dimension desc by `metric`
  (`gross|net|deduction`, default `gross`), None → `Decimal('-Infinity')` sink, stable tie-break
  by id asc (matches `branded_slide_pack`). Top-`limit` per dimension (default 10, max 100).
  Returns `{month, metric, channels:[RankedEntry], companies:[RankedEntry], sectors:[RankedEntry]}`
  where `RankedEntry = {rank, entity_id, entity_name, gross_revenue_usd, net_revenue_usd,
  deduction_amount_usd}` (money via `decimal_to_api`, None preserved).
- **Route** `api/revenue.py` `GET /months/{month}/rankings` on the existing `/revenue` router:
  params `scope_type="global"`, `scope_id=None`, `metric="gross"`, `limit=10`. Copy
  `get_month_net_revenue` EXACTLY for: `_revenue_read_scope_to_channel_ids` →
  VIEW_REVENUE@target + VIEW_CONFIDENCE@target + VIEW_FINALIZED_PAYMENTS@finance_month gates
  (BEFORE any read) → `resolve_month_account_allocation` (committed snapshot for LOCKED) →
  `build_month_net_revenue_summary` → `filter_account_allocations_to_scope` (scoped) →
  `build_month_rankings` → REVENUE_VIEWED + PAYMENT_VIEWED audit (same entity_id
  `f"{month}:{scope_type}:{scope_id}"`). Scoped read restricts the channel set BEFORE ranking
  (zero-channels → empty ranking, NOT 403). Company/sector names via `SqlAlchemyOrgUnitReader`
  (raw-id fallback). 422 on bad month/scope/metric/limit.
- **FE** `useRankings.ts` (`{month, scopeType, scopeId, metric, limit}`, URLSearchParams,
  path-encoded month) + `types.ts` `MonthRankingsResponse`/`RankedEntry` (money `MoneyString`).
- **FE** rankings panel in `CommandView` — own hook (fails independently, SmartAlertsPanel
  template), money via `financeDisplay`/SummaryTile gated on `canViewFinance`, surfaces the
  `allocation_source` so a `live_fallback` isn't read as authoritative; metric toggle
  (gross/net/deduction). Gated: panel mounts only when `canViewFinance` (it shows money).
- **Tests**: `tests/finance/test_rankings.py` (pure: roll-up sums, sort, tie-breaks, None
  handling, top-N); `tests/api/test_revenue_rankings_api.py` (403 default, 422 bad input,
  scope-isolation, locked-month snapshot consistency, audit); FE hook + panel Vitest.

### E. Outside-CMS / channel-issues monitor panel (FE) — medium
Wire the already-tested `GET /channels/outside-cms` + `GET /channels/issues` (both
VIEW_ANALYTICS-gated, scope-filtered, `{items, summary}`, no money, no audit) into a CommandView
monitor panel, replacing the mock "Open issues" KPI + "Outside CMS" tile.

- **FE** `useOutsideCmsChannels.ts` + `useChannelIssues.ts` (copy `useChannels` shape) +
  `types.ts` `OutsideCmsResponse`/`OutsideCmsItem`/`ChannelIssuesResponse`/`ChannelIssue`
  (1:1 with the backend serializers; `recommended_action`/`severity`/`issue_type` strings).
- **FE** monitor panel in `CommandView`: own hooks, **no-fetch-when-restricted** (mount only
  when `canViewAnalytics`; else restricted placeholder + zero requests, AuditView pattern);
  403 → denied copy (NOT "no issues" — masking authz is forbidden), 503 → unavailable. Summary
  tiles: outside-CMS count / missing-official-revenue / open-issues (high+medium). Distinguishes
  "outside CMS but covered" (OFFICIAL_MANUAL_IMPORT) from "outside CMS + missing source".
- **Tests**: hook Vitest (`__tests__/useOutsideCmsChannels.test.tsx`, `useChannelIssues.test.tsx`)
  + panel test (independent-failure containment, 403 placeholder, no-fetch-when-restricted).

### F. Doc reconciliation (docs-only) — small
Apply the 13 corrections from the 2026-06-13 reconciliation to `Docs/01_IMPLEMENTATION_PLAN.md`,
`Docs/15_DELIVERY_BACKLOG.md`, `Docs/12_BACKEND_API_SPEC.md`: refresh both status headers to
2026-06-13/through #97; remove the 4 self-contradictions (audit-summary aggregate-count;
Hard Problem #1; recalculate writes; Track-D run-history ⏳); credit #94/#95/#96/#97; fix the
`20260602_0001` migration-id mislabel; correct the outside-CMS / company-mapping / FX / Shorts /
C1-normalizer / Phase-2-ingestion marks; describe the `app_tenant_context` table + SECURITY
DEFINER RLS mechanism. Plus add the Phase 5 items shipped by THIS PR (rankings, coverage code,
mapping month-lock, outside-CMS panel, canViewAnalytics) per the per-PR plan-status rule.

---

## Cross-cutting rules (every component)
- **Fail-closed**: never weaken a gate to pass a test; lock-store/auth errors REJECT, not allow.
- **Scope-leak**: scoped reads intersect the authorized channel set BEFORE compute and re-check
  per row; `filter_account_allocations_to_scope` for any deduction surface.
- **Tenant**: every query filters tenant; PostgreSQL source of truth; no Neo4j.
- **Money**: server-side only; `decimal_to_api`, None preserved (never coalesce to 0); FE display
  via `financeDisplay`/`formatMoney`, gated by `canViewFinance`.
- **Layering**: thin routes; logic in finance services; reads in repos. Typed errors → HTTP at
  boundary only; no bare except.
- **Lines ≤100 chars** (DeepSource FLK-E501). **Trailer-free commits.** `python -m ruff` /
  `python -m pytest`. Vitest from `frontend/`. Clean-room PG for touched PG-tier suites.

## Build order (TDD per task: failing test → run-to-fail → minimal impl → run-to-pass → commit)
1. A — `canViewAnalytics` capability (backend + FE type/AppShell).
2. B — mapping month-lock guard.
3. C — coverage detection.
4. D — rankings backend (service + route).
5. D/E — FE types + hooks (rankings, outside-cms, issues).
6. D/E — FE panels in CommandView + AppShell threading.
7. F — doc reconciliation (Docs/01, /12, /15).
8. Validation gate + adversarial review.
