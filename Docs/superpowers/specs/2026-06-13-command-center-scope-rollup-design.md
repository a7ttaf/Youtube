# Command Center Group/Sector Rollup Selection — Design + Plan

**Date:** 2026-06-13
**Branch:** `feat/group-sector-rollup` (off `main` 6afa941)
**Type:** One combined PR (backend authorized-scope endpoint + frontend selector + docs).
**Goal:** Close the Phase 1/5 acceptance gate — a Command Center user picks a month **and** an
org scope (global / sector / company) and gets aggregated source-backed gross/deduction/net for
that scope. Grounded in the 3-agent understanding sweep (`wx6108zdl`); anchors verified.

## What already exists (do NOT rebuild)
- `GET /revenue/months/{month}/net-revenue` and `/rankings` already accept `scope_type` ∈
  {global,sector,company,channel} + `scope_id`, resolve via `_revenue_read_scope_to_channel_ids`
  (revenue.py:2376), aggregate server-side (MonthNetRevenueSummary month totals are the sum of the
  scoped per-channel rows), and gate with VIEW_REVENUE+VIEW_CONFIDENCE@target_scope +
  VIEW_FINALIZED_PAYMENTS@finance_month. Out-of-scope `scope_id` → 403; bad type/missing id → 422.
- CommandView already renders `<select aria-label="Scope">` bound to `SCOPE_OPTIONS` and threads the
  chosen `scopeType/scopeId` into `useNetRevenue` AND `RankingsPanel`→`useRankings`. **The only gap
  is that `SCOPE_OPTIONS` is hardcoded to a single global entry.**
- `GET /org-units` + `useOrgUnits` exist (names), and `_granted_scopes_for_permission` /
  `_authorized_channel_ids_for_permission` (revenue.py) are the granted-scope walk to reuse.

## The security decision (why a new endpoint, not just useOrgUnits)
`GET /org-units` returns **all** active units in the tenant and is VIEW_ANALYTICS-gated. Populating
the selector from it would (a) **over-list** every company/sector to a scoped viewer (org-structure
leak), and (b) offer options the viewer cannot actually roll up, because the rollup read needs
VIEW_REVENUE, not VIEW_ANALYTICS. So the selector's options MUST be the viewer's
**VIEW_REVENUE-authorized** scopes. No existing endpoint returns that. → new endpoint.

Scope: `/revenue/scopes` returns the viewer's **VIEW_REVENUE-authorized** scopes. Those are readable
for the standard finance roles, which couple VIEW_REVENUE with VIEW_CONFIDENCE@scope +
VIEW_FINALIZED_PAYMENTS@finance_month (the rollup read's other gates). It is not an absolute "never a
dead option" guarantee: a hand-crafted VIEW_REVENUE-only grant could be offered a scope whose rollup
read then 403s — the FE degrades that to an in-card "No permission" (not a leak). The role model
couples the three perms, so refactoring the endpoint for that unreachable case is not warranted.

---

## Components

### A. Backend — `GET /revenue/scopes` (authorized rollup scope options) — medium
- **Route** in `api/revenue.py` on the `/revenue` router. No path params. Deps: principal,
  `current_org_access_index`, `current_org_unit_reader` (names), `current_db_session`.
- **Service** `finance/revenue_scopes.py` (NEW, pure): `build_authorized_revenue_scopes(*, granted,
  org_index, sector_names, company_names) -> list[RevenueScopeOption]` where `RevenueScopeOption =
  {scope_type, scope_id|None, label}`. Logic:
  - If the viewer holds a **global** VIEW_REVENUE grant → emit the `global` option + **all** active
    SECTOR options + **all** active COMPANY options.
  - Else expand the granted VIEW_REVENUE scopes (no global):
    - granted **SECTOR(S)** → sector S + every COMPANY `c` with `org_index.company_sector[c]==S`
      (a sector grant contains its companies, matching `OrgAccessIndex.contains`).
    - granted **COMPANY(C)** → company C only (a company grant does NOT confer its sector).
  - Dedup by (scope_type, scope_id); names via the org-unit reader with **raw-id fallback**; drop
    HOLDING (not a revenue scope_type); deterministic order (global first, then sectors by name,
    then companies by name). The `global` option is present **only** when a global grant exists.
- **Auth**: fail-closed — `_require_permission`-style: disabled OR no active VIEW_REVENUE grant in
  ANY scope → **403** `Missing permission: finance.view_revenue` (matches the rollup read gate). A
  scoped viewer with at least one VIEW_REVENUE grant → 200 with only their authorized options.
- **Response**: `{"scopes": [RevenueScopeOption, ...]}`. Read-only; **no audit** (it's a metadata
  helper, like `/org-units`, not a revenue-number disclosure — it returns only ids/names the viewer
  is authorized for; emit no REVENUE_VIEWED).
- **Tests** `tests/api/test_revenue_scopes_api.py` + `tests/finance/test_revenue_scopes.py`:
  - global finance viewer → global + all sectors + all companies (names resolved).
  - company-scoped viewer → ONLY their company (no other company/sector leaks; assert a foreign
    company id is absent — mirror `test_net_revenue_scoped_excludes_out_of_scope_account_allocation`).
  - sector-scoped viewer → their sector + its companies, not other sectors' companies.
  - viewer without any VIEW_REVENUE grant (e.g. assistant_analyst) → 403.
  - raw-id fallback when a unit name is missing/deactivated.
  - pure-service unit tests for the expansion + dedup + ordering + global-only-when-global.

### B. Frontend — dynamic scope selector in CommandView — medium
- **Hook** `useRevenueScopes.ts` (NEW): `useRevenueScopes(): AsyncState<RevenueScopeOption[]>` →
  `client.get("/revenue/scopes")` returning `.scopes`. Copy `useOrgUnits` shape; 403 → typed
  ApiError (degrade handled in the view). Types in `types.ts`: `RevenueScopeOption = {scope_type:
  string; scope_id: string|null; label: string}`.
- **CommandView**:
  - Call `useRevenueScopes()` once at the view root.
  - Replace the hardcoded `SCOPE_OPTIONS` with: a guaranteed **Global** fallback option +
    the fetched options (the backend already includes global only when authorized; for a scoped
    viewer with no global grant, do NOT inject a global option — show only their authorized scopes).
    While loading or on 403/error → fall back to **global-only** (never block the screen; the
    panels themselves fail-closed on the actual reads).
  - **Refactor scope state from the positional `scopeIndex` to a stable key** `{scopeType,
    scopeId}` (async option lists make an index brittle). Selecting an option sets the pair; the
    pair threads unchanged into `useNetRevenue` + `RankingsPanel` (already wired). Preserve the
    existing "reset selected channel on scope change" behavior.
  - Send `scope_id` only for non-global (the hooks already omit empty params; ensure global sends
    no `scope_id`).
  - Smart Alerts panel stays month-only/global (backend is global-only) — leave as is; its label
    already implies global.
- **Tests** (`__tests__/CommandView.test.tsx` + a focused selector test, mirroring
  `RankingsPanel.test.tsx`'s `userEvent.selectOptions` + URL-capture pattern):
  - selector is populated from the `/revenue/scopes` fixture (global + a company).
  - selecting a company → `scope_type=company&scope_id=<id>` appears in BOTH the `/net-revenue` and
    `/rankings` request URLs (the regression guard the rankings test established for global).
  - on `/revenue/scopes` 403/error → selector degrades to global-only and the screen still renders.

### C. Docs — small
- `Docs/12_BACKEND_API_SPEC.md`: document `GET /revenue/scopes` (auth, response, no-audit).
- `Docs/01_IMPLEMENTATION_PLAN.md` + `Docs/15_DELIVERY_BACKLOG.md`: mark the Phase 1/5 acceptance
  gate "select month + group/sector → source-backed totals" as met for global/sector/company;
  record channel-GROUP revenue scope as a separate follow-up that was later shipped in PR #122.

## Resolved after this design
- ✅ **Channel-GROUP revenue scope** (TV_BRAND/CUSTOM_GROUP etc.) shipped in
  PR #122. `group` is now a runtime finance scope for selector options,
  net-revenue reads, rankings, and recalculation dry-run previews; reads resolve
  active member channel IDs through the channel-group registry and authorize as
  the AND of per-channel `AccessScope.channel(cid)` checks.

## Cross-cutting rules
- Fail-closed; no scope leak (the whole point of the new endpoint); tenant-scoped; PostgreSQL
  source of truth. Thin route; logic in `finance/revenue_scopes.py`; typed errors→HTTP at boundary.
- Lines ≤100 chars; trailer-free commits; `python -m ruff` / `python -m pytest`; Vitest from
  `frontend/`; clean-room PG for the touched API suites.

## Build order (TDD per task)
1. A — `finance/revenue_scopes.py` service + `GET /revenue/scopes` route + tests.
2. B — `useRevenueScopes` hook + types + CommandView dynamic selector + stable scope key + tests.
3. C — docs (12 + 01 + 15).
4. Validation gate + adversarial review.
