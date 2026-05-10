# UMS Revenue Facts Stabilization Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:verification-before-completion before claiming any status. This is a recovery/stabilization plan; do not add new product scope until the checks below are complete.

**Goal:** Verify and harden the Python backend work added after PR #1, keeping only code that is consistent with the UMS source-of-truth, audit, finance-locking, and authorization requirements.

**Architecture:** SQL remains the financial source of truth. The stabilization pass reviews revenue facts, reconciliation, manual overrides, adjusted summaries, and month-close readiness as SQL-backed services and FastAPI routes. Neo4j and dashboards are out of scope for this pass.

**Tech Stack:** Python FastAPI, SQLAlchemy, Alembic migrations, pytest.

---

## Operating Rules

- No destructive git commands.
- Do not edit restored planning docs or mockups except this stabilization plan.
- Treat the branch `codex/revenue-facts-foundation-20260510` as suspect until each Python change is reviewed.
- Prefer narrow fixes with focused tests.
- Use `pytest -q -p no:cacheprovider --basetemp .pytest-tmp` because the default Windows temp directory is permission-blocked on this machine.

## Current Evidence

- Base branch: `main` at merged PR #1.
- Current branch: `codex/revenue-facts-foundation-20260510`.
- Commits after `main`:
  - `631a208 Add revenue facts foundation`
  - `3209c60 Add revenue reconciliation preview`
  - `121893a Add revenue reconciliation issue queue`
  - `8dcba67 Add revenue manual override workflow`
  - `ba95abf Add adjusted revenue summary`
  - `abe884c Add finance close readiness gate`
  - `545e20c Restore UMS planning docs and mockups`
- Full suite with workspace temp before this stabilization pass: `121 passed`.
- Full suite after the focused hardening pass: `125 passed`.
- Default temp pytest run fails from `PermissionError` on `C:\Users\Mrmah\AppData\Local\Temp\pytest-of-Mrmah`, not from application assertions.

## Files Under Review

- `backend/ums_smart_revenue/api/revenue.py`
- `backend/ums_smart_revenue/api/finance_close.py`
- `backend/ums_smart_revenue/app.py`
- `backend/ums_smart_revenue/auth/audit.py`
- `backend/ums_smart_revenue/db/finance_models.py`
- `backend/ums_smart_revenue/db/alembic/versions/20260510_0004_revenue_facts.py`
- `backend/ums_smart_revenue/db/alembic/versions/20260510_0005_manual_overrides.py`
- `backend/ums_smart_revenue/finance/revenue_facts.py`
- `backend/ums_smart_revenue/finance/reconciliation.py`
- `backend/ums_smart_revenue/finance/manual_overrides.py`
- `backend/ums_smart_revenue/finance/revenue_summary.py`
- `backend/ums_smart_revenue/finance/month_close_readiness.py`
- Related tests under `tests/api`, `tests/db`, and `tests/finance`.

## Stabilization Tasks

### Task 1: Audit API Authorization and Audit Events

- [x] Verify every revenue and finance-close route checks permission before sensitive reads or writes.
- [x] Verify revenue reads and manual override reads record audit events when they expose money data.
- [x] Verify write routes include a nonblank reason and actor identity.
- [x] Add tests for any missing negative permission case before changing code.

Result:
- Added a failing test for manual override approval ID probing, then hardened the approval route so callers with no approval permission get `403` before ID parsing or row lookup.
- Added coverage for invalid revenue source kinds, malformed import actor IDs, and manual override self-approval.

Run after changes:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp .pytest-tmp tests/api/test_revenue_facts_api.py tests/api/test_manual_overrides_api.py tests/api/test_finance_close_api.py
```

### Task 2: Audit SQL Model and Migration Parity

- [x] Compare `finance_models.py` to Alembic revisions `0003`, `0004`, and `0005`.
- [x] Verify constraints are equivalent for month format, source kinds, nonnegative metrics, override status, and approval fields.
- [x] Verify indexes support company/channel/month reads used by the APIs.
- [x] Add or adjust migration/model tests for any drift.

Result:
- Existing model and migration coverage passed. No migration drift fix was required in this pass.

Run after changes:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp .pytest-tmp tests/db
```

### Task 3: Audit Finance Logic

- [x] Verify revenue fact imports reject locked months, missing channels, invalid months, invalid source kinds, and malformed actor IDs.
- [x] Verify reconciliation does not invent revenue and only explains differences among stored facts.
- [x] Verify manual overrides require approval, cannot self-approve, cannot apply in locked months, and only approved overrides affect adjusted summaries.
- [x] Verify month close readiness blocks on unresolved reconciliation issues and pending manual overrides.

Result:
- Focused API tests passed with `30 passed`.
- Database tests passed with `18 passed`.
- Finance logic tests passed with `4 passed`.

Run after changes:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp .pytest-tmp tests/finance
```

### Task 4: Keep or Reject Branch Scope

- [x] If Tasks 1-3 find no serious issues, keep the Python work and prepare a narrow PR that excludes restored docs/mockups unless intentionally requested.
- [ ] If Tasks 1-3 find serious architecture drift, create a new branch from `main` and cherry-pick only verified commits.
- [ ] Do not push until the final changed-file list is explicit.

Result:
- No serious architecture drift was found in the Python foundation.
- Python baseline was updated from `3.14.4` to `3.14.5` after checking python.org. Node remains `24.15.0` LTS.

Final verification:

```powershell
git diff --check main..HEAD
$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp .pytest-tmp
```
