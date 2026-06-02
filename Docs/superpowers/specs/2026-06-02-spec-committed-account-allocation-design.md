# Spec 2b — Persisted / Committed Account Allocation (Design)

**Status:** In review — 2026-06-02 (not yet approved for planning)
**Branch:** `spec/committed-account-allocation` (off `main` `e1fe227` = Spec 2b PR-4 merged, #61)
**Phase:** 4, Spec 2b (allocation engine) — the first allocation **write** path, after PR-1 (compute+read #58), PR-2 (net integration #59), PR-3 (explanation #60), PR-4 (export breakdown #61).

---

## 1. Goal

Persist a durable, **versioned, audited snapshot** of the existing `gross_revenue_proportional` account-allocation compute, via a new commit endpoint and new tables. **No reader changes** — net-revenue, the allocation read endpoint, and finance exports keep computing live exactly as today. This PR is the WRITE substrate only; switching consumers to read committed snapshots is a deliberate follow-up PR.

## 2. Scope and non-goals

**In scope:**
- Four new tables (`committed_allocation_runs` + `_lines` + `_unallocated` + `_notes`) + one Alembic migration.
- A repository that commits a computed `AccountAllocationResult` as an append-only versioned run, under the finance-month advisory lock, with idempotency-key retry semantics.
- A `POST /revenue/months/{month}/account-allocations/commit` endpoint that computes → validates → persists → audits, returning the committed run.
- A new `ALLOCATION_COMMITTED` audit event (reason-required, summary-only detail).
- Tests: repository, API, ORM (SQLite), and Postgres migration round-trip/constraint tests.
- `Docs/01_IMPLEMENTATION_PLAN.md` + `Docs/15_DELIVERY_BACKLOG.md` status updates.

**Explicitly NOT in scope (deferred to later PRs):**
- **No reader switch.** Net-revenue (`api/revenue.py:1130`), the allocation read endpoint (`api/allocation.py`), and exports (`api/exports.py:1085`) keep calling `compute_month_account_allocation` live. They do not read committed snapshots in this PR. (Eliminates the double-application / scope-leak / export-drift risks from the first migration.)
- **No commit-on-lock.** Commit is a standalone action; the month-lock ceremony (`lock_month`) is unchanged and gains no allocation step.
- **No new readiness blocker.** `month_close_readiness` keeps its three blockers; a locked month carries no committed-allocation guarantee (a future PR may add that).
- **No `/revenue/recalculate` change.** The `dry_run=false` guard (`recalculation.py:104` / `api/revenue.py:438`) stays; recalc remains decoupled.
- **No additional allocation methods.** Only `gross_revenue_proportional` (`allocation.py:21`); any other method hard-fails.

## 3. Verified current state (anchors)

- **Compute (pure, no DB):** `build_account_allocation(*, month, components, verified_channels, gross_basis) -> AccountAllocationResult` (`finance/allocation.py:147-156`, method constant `ALLOCATION_METHOD = "gross_revenue_proportional"` `:21`). Orchestrated by `compute_month_account_allocation(*, month, deduction_repository, revenue_repository, link_repository, adsense_account_id=None)` (`finance/allocation_inputs.py:38-68`) — never persists.
- **Result dataclasses (frozen)** in `finance/allocation.py`:
  - `AllocationLine` (`:97-110`): `adsense_account_id, youtube_channel_id, component_kind, source_system, component_key, basis_source_kind` (all `str`), `basis_gross_usd, basis_share, allocated_amount_usd` (`Decimal`), `net_applicable` (`bool`).
  - `UnallocatedIssue` (`:113-122`): `scope_id, component_kind, component_key` (`str`), `amount_usd` (`Decimal`), `issue_code, detail` (`str`).
  - `AllocationNote` (`:125-131`): `note_code, youtube_channel_id, detail` (`str`).
  - `AllocationSummary` (`:134-144`): `component_count, allocated_component_count, unallocated_component_count` (`int`), `allocated_total_usd, unallocated_total_usd, net_applicable_total_usd, reconciliation_total_usd` (`Decimal`).
  - `AccountAllocationResult` (`:147-156`): `month, allocation_method` (`str`), `lines, unallocated, notes` (tuples), `summary`.
  - Serialization is centralized in `_result_to_api(result)` in `api/allocation.py:65-117` (no `to_api()` on the dataclasses); root keys are `month`, `allocation_method`, `allocations` (the lines), `unallocated`, `notes`, `summary`; all `Decimal` rendered via `decimal_to_api` → string.
- **Lock:** `FinanceMonthCloseORM` (`db/finance_models.py:57-105`), `(tenant_id, month)`, `status` ∈ {`OPEN`,`LOCKED`}. Advisory lock helper `acquire_finance_month_advisory_lock(session, month, *, tenant_id)` (`finance/month_close.py:219-232`) → `pg_advisory_xact_lock(_finance_month_advisory_lock_key(...))` where the key is `blake2b("finance-month-close:{tenant_id}:{month}", digest_size=8)` signed bigint (`:265-271`); **SQLite no-op** (returns early when dialect != postgresql). `get_or_create_month_close_row(session, month, *, for_update=True)` (`:158-191`) acquires the advisory lock + `SELECT ... FOR UPDATE`. The OPEN-month guard pattern: `if row.status == "LOCKED": raise ...` (e.g. `record_allocation_rule` `:107-122`).
- **Write-endpoint pattern:** `POST /{month}/allocate` (`api/finance_close.py:197-227`): `_validate_month(month)`; `scope = AccessScope.finance_month(month)`; `_require_permission(user, Permission.CHANGE_ALLOCATION_RULE, scope)`; repo call in `try/except ValueError -> HTTPException(409)`; `record_audit_event(... AuditEventType.ALLOCATION_RULE_CHANGED ... reason=payload.reason ...)`; returns `{...close..., audit_event}`.
- **Audit:** `AuditEventType` StrEnum (`auth/audit.py:7-43`) — has `ALLOCATION_RULE_CHANGED`, `RECALCULATION_REQUESTED`; **no `ALLOCATION_COMMITTED`**. `AUDIT_EVENT_DEFINITIONS` entries carry `reason_required` + `permission`; `record_audit_event(*, sink, actor, event_type, entity_type, entity_id, scope, details, reason, request_id, permission_override)` (`auth/audit_service.py:41-89`) raises if `reason_required` and no reason; `sensitive` is derived from `permission ∈ SENSITIVE_PERMISSIONS`.
- **Permission:** `CHANGE_ALLOCATION_RULE = "finance.change_allocation_rule"` (`auth/permissions.py:16`), sensitive + audit_on_use, scope-types `{GLOBAL, FINANCE_MONTH}` (`auth/user_permissions.py:48`). The read endpoint (`api/allocation.py:136-198`) gates `VIEW_REVENUE@global` + `VIEW_FINALIZED_PAYMENTS@finance_month` and audits `REVENUE_VIEWED` + `PAYMENT_VIEWED`.
- **Migration + ORM conventions:** UUID PK `gen_random_uuid()`; `tenant_id` via `_TENANT_ID_DEFAULT` + FK→`tenants.id` `ondelete="RESTRICT"`; month CHECK via `_month_format_check(col)` (`finance_models.py:43-54`); typed `Numeric(20,6)` amounts; PG-only CHECKs (NaN/Inf finite guards, `jsonb_typeof`) via `.ddl_if(dialect="postgresql")` on the ORM and `if op.get_bind().dialect.name == "postgresql": op.create_check_constraint(...)` in the migration (`20260529_0002_deduction_components.py`, `20260531_0001_channel_account_map.py`). Indexes via `Index(...)` / `op.create_index(...)`; `downgrade()` drops indexes then tables.

## 4. Architecture

Thin route → service/repository. The commit endpoint:
1. Validates the month + permissions.
2. **Computes** the allocation live via `compute_month_account_allocation` (server-side — the client never supplies financial results).
3. **Validates** the result (method, no-unallocated) — §8.
4. **Persists** an append-only versioned run (header + lines + unallocated + notes) under the finance-month advisory lock, with idempotency-key dedup — §6, §7.
5. **Audits** `ALLOCATION_COMMITTED` (summary-only) — §9.
6. Returns the committed run.

Readers are untouched (§2). The committed tables exist but drive no number this PR.

## 5. Reused helpers (no new copies)

- Compute: `compute_month_account_allocation` (`allocation_inputs.py`).
- Advisory lock + OPEN-month guard: `acquire_finance_month_advisory_lock` / `get_or_create_month_close_row(for_update=True)` (`month_close.py`).
- Audit: `record_audit_event` (`audit_service.py`); new `ALLOCATION_COMMITTED` event definition.
- Permission: `_require_permission` + `AccessScope.global_scope()` / `AccessScope.finance_month(month)`.

## 6. Schema (RISK #3 — FK + index layout)

One migration, mirroring the `20260531_0001` / `20260529_0002` patterns (UUID PK, tenant FK `ondelete=RESTRICT`, `_month_format_check`, PG-only CHECKs via dialect guard / `.ddl_if`, `Numeric(20,6)` amounts, `created_at`/`updated_at`). `downgrade()` drops the four tables (and their indexes) in FK-safe order.

### 6.1 `committed_allocation_runs` (header)
| column | type | notes |
|---|---|---|
| `id` | UUID PK | `gen_random_uuid()` |
| `tenant_id` | UUID NOT NULL | `_TENANT_ID_DEFAULT`; **FK→`tenants.id` `ondelete=RESTRICT`** |
| `month` | Text NOT NULL | `_month_format_check("month")` |
| `commit_version` | Integer NOT NULL | CHECK `>= 1` |
| `allocation_method` | Text NOT NULL | CHECK `= 'gross_revenue_proportional'` (v1) |
| `idempotency_key` | Text NOT NULL | CHECK `length >= 1` |
| `request_fingerprint` | Text NOT NULL | hex digest (§7) |
| `component_count` | Integer NOT NULL | from `AllocationSummary` |
| `allocated_component_count` | Integer NOT NULL | |
| `unallocated_component_count` | Integer NOT NULL | `== 0` in v1 (precondition §8) |
| `allocated_total_usd` | Numeric(20,6) NOT NULL | PG finite CHECK |
| `unallocated_total_usd` | Numeric(20,6) NOT NULL | PG finite CHECK |
| `net_applicable_total_usd` | Numeric(20,6) NOT NULL | PG finite CHECK |
| `reconciliation_total_usd` | Numeric(20,6) NOT NULL | PG finite CHECK |
| `committed_by` | UUID NOT NULL | actor `user_id` |
| `committed_at` | DateTime(tz) NOT NULL | `func.now()` |
| `reason` | Text NOT NULL | CHECK `length >= 1` (commit requires a reason) |
| `created_at`, `updated_at` | DateTime(tz) NOT NULL | `func.now()` (+ `onupdate` on `updated_at`) |

Constraints/indexes:
- `UniqueConstraint(tenant_id, month, commit_version)` → `uq_committed_allocation_runs_version` (one row per version).
- `UniqueConstraint(tenant_id, idempotency_key)` → `uq_committed_allocation_runs_idempotency` (the idempotency guard — §7).
- `Index(tenant_id, month)` → `ix_committed_allocation_runs_tenant_month` (latest-version lookup).
- PG-only finite CHECKs on the four `*_total_usd` columns (`.ddl_if`/dialect guard).

### 6.2 `committed_allocation_lines`
`id` UUID PK; `run_id` UUID NOT NULL **FK→`committed_allocation_runs.id` `ondelete=CASCADE`**; then the 10 `AllocationLine` fields: `adsense_account_id, youtube_channel_id, component_kind, source_system, component_key, basis_source_kind` (Text, `length>=1` where identity), `basis_gross_usd, basis_share, allocated_amount_usd` (Numeric(20,6), PG finite CHECK), `net_applicable` (Boolean NOT NULL). Indexes: `Index(run_id)` and `Index(run_id, youtube_channel_id)` (future scope-filter). No tenant FK on lines — tenant/month derive from the run (kept lean; the future read-switch joins the run).

### 6.3 `committed_allocation_unallocated`
`id` UUID PK; `run_id` FK→runs `ondelete=CASCADE`; the 6 `UnallocatedIssue` fields: `scope_id, component_kind, component_key` (Text), `amount_usd` (Numeric(20,6), PG finite CHECK), `issue_code, detail` (Text). `Index(run_id)`. **Empty in v1** (reject-on-unallocated §8) — present for snapshot-schema fidelity with `AccountAllocationResult` (so the persisted snapshot mirrors the full compute result shape); a future partial/draft state would additionally add a status column.

### 6.4 `committed_allocation_notes`
`id` UUID PK; `run_id` FK→runs `ondelete=CASCADE`; the 3 `AllocationNote` fields: `note_code, youtube_channel_id, detail` (Text). `Index(run_id)`. Persisted so informational notes (e.g. `CHANNEL_IN_MULTIPLE_ACCOUNTS`) survive in the durable snapshot rather than only in audit detail.

**ondelete rationale:** `tenant_id → tenants` is `RESTRICT` (a tenant purge must not silently drop committed financial runs — consistent with every finance table). `run_id → runs` is `CASCADE` (lines/unallocated/notes are wholly owned by their run; runs are append-only and not deleted in normal operation, so CASCADE only matters for migration downgrade / explicit teardown).

## 7. Idempotency & versioning (RISK #1 — fingerprint contents)

- **Versioned, append-only.** Each intentional commit for `(tenant_id, month)` gets `commit_version = max(existing) + 1`, computed **under the finance-month advisory lock** (`acquire_finance_month_advisory_lock`) + `SELECT ... FOR UPDATE` so two concurrent commits serialize (one gets vN, the other vN+1; no lost update). The **current** snapshot is the highest `commit_version`. Prior versions are retained (full audit trail). No content-dedup — an intentional re-commit with a new key is a real new version even if the numbers are identical.
- **`idempotency_key`** is client-generated, **required** on the request, unique per `(tenant_id, idempotency_key)`. It is the HTTP-retry guard (the repo has `request_id` plumbing but no general server idempotency store, so the run table is the guard).
- **`request_fingerprint`** = a stable `blake2b(..., digest_size=16)` hex digest of the **canonicalized request payload** — exactly the client-controlled fields that define *what* is being committed: **`month` + `allocation_method` + `reason`** (sorted-key JSON, then hashed). It does **not** include the computed result (the key dedups the request, not the data state).
- **Retry semantics** (matched on `(tenant_id, idempotency_key)`):
  - **No existing key** → compute + validate + persist new version → **201**.
  - **Existing key, same `request_fingerprint`** → return the existing run, **no recompute, no new rows, no second `ALLOCATION_COMMITTED` audit** → **200**.
  - **Existing key, different `request_fingerprint`** (key reused for a different month/method/reason) → **409**.
- **Locked month** → cannot commit (no new version); corrections require reopen → change inputs → commit vN+1.

## 8. Validation gates & failure modes (RISK #2 — exact response shapes)

Order inside the endpoint (after permission checks):
1. `_validate_month(month)` malformed → **422** `{"detail": "..."}`.
2. Acquire the finance-month advisory lock + load the close row `for_update` (serializes concurrent same-month / same-key commits).
3. **Idempotency lookup** (§7), matched on `(tenant_id, idempotency_key)`: same-key + same `request_fingerprint` → **200** (return the existing run; no recompute, no new rows, no new audit) — **this succeeds even if the month is now LOCKED**, since it replays a prior commit; same-key + different `request_fingerprint` → **409** `{"detail": "idempotency key reused with a different request"}`.
4. **(New commit only)** if `status == "LOCKED"` → **409** `{"detail": "Finance month is locked: <month>"}` (mirrors `record_allocation_rule`'s LOCKED→409).
5. `allocation_method` (request, default `gross_revenue_proportional`) not `gross_revenue_proportional` → **422** `{"detail": "unsupported allocation method: <m>"}` (hard-fail, never silently persist a no-op method).
6. Compute via `compute_month_account_allocation`. If `result.unallocated` is non-empty → **422** `{"detail": "cannot commit: <n> unallocated component(s)"}` (reject-on-unallocated; no draft state in v1).
7. Persist run vN + children, emit audit → **201**.

**Request body** `CommitAllocationRequest`: `{ "idempotency_key": str (required, non-empty), "reason": str (required, non-empty), "allocation_method": str = "gross_revenue_proportional" }`.

**201 / 200 success body** (typed Pydantic response, mirroring `AccountOwnerLinkMutationResponse` + `_result_to_api`):
```json
{
  "run": {
    "run_id": "<uuid>", "month": "2026-04", "commit_version": 1,
    "allocation_method": "gross_revenue_proportional",
    "idempotency_key": "<client-key>", "committed_by": "<uuid>",
    "committed_at": "<iso8601>", "reason": "<text>",
    "summary": {
      "component_count": <int>, "allocated_component_count": <int>,
      "unallocated_component_count": 0,
      "allocated_total_usd": "<str>", "unallocated_total_usd": "<str>",
      "net_applicable_total_usd": "<str>", "reconciliation_total_usd": "<str>"
    }
  },
  "allocations": [ { ...AllocationLine fields, Decimals as strings... } ],
  "unallocated": [],
  "notes": [ { "note_code": "...", "youtube_channel_id": "...", "detail": "..." } ],
  "audit_event": { ... } | null
}
```
`audit_event` is the recorded event on a fresh **201**; on an idempotent **200** replay it is `null` (no new event). `allocations`/`unallocated`/`notes`/`summary` reuse the exact `_result_to_api` field shapes (Decimals as strings).

**422** body: `{"detail": "<reason>"}` (malformed month / unsupported method / unallocated present). **409** body: `{"detail": "<reason>"}` (locked month / idempotency conflict).

## 9. Permission & audit

- **Auth (all three required):** `VIEW_REVENUE@global` + `VIEW_FINALIZED_PAYMENTS@finance_month(month)` (read gates — the endpoint computes and returns financial detail, matching the read boundary) **+ `CHANGE_ALLOCATION_RULE@finance_month(month)`** (write authority). Missing any → **403** (fail-closed, as `_require_permission` does today).
- **New audit event `ALLOCATION_COMMITTED`** added to `AuditEventType` + `AUDIT_EVENT_DEFINITIONS` with `reason_required=True`, `permission=CHANGE_ALLOCATION_RULE` (→ sensitive via `SENSITIVE_PERMISSIONS`). Emitted on a fresh commit with **summary-only detail**: `{run_id, commit_version, month, allocation_method, component_count, allocated_component_count, unallocated_component_count, allocated_total_usd, net_applicable_total_usd, note_count}` — **never the ~500 line rows** (avoids audit-payload explosion). `entity_type="committed_allocation_run"`, `entity_id=run_id`, `scope=finance_month(month)`, `reason` from the request.

## 10. Repository & API layer

- **`backend/ums_smart_revenue/db/finance_models.py`** — four ORM models (§6), `FinanceBase`, mirroring the `DeductionComponentORM` / link-table conventions incl. `.ddl_if(dialect="postgresql")` CHECKs and the contract-block comment header.
- **`backend/ums_smart_revenue/db/alembic/versions/<rev>_committed_account_allocation.py`** — the migration (§6), dialect-guarded PG-only CHECKs, `down_revision` = the current Alembic head (confirm at plan time; §14).
- **`backend/ums_smart_revenue/finance/committed_allocation.py`** — `SqlAlchemyCommittedAllocationRepository` with `commit_allocation(*, month, result, allocation_method, idempotency_key, request_fingerprint, reason, committed_by) -> CommittedAllocationRun` (acquires advisory lock, OPEN-month guard, idempotency lookup, `commit_version = max+1`, inserts run+children in one transaction) + read helpers `get_latest_run(month)` / `get_run_by_idempotency_key(key)` (used by the endpoint's idempotency branch; NOT wired into net-revenue/exports). Typed errors: `CommittedAllocationLockedMonthError` (→409), `CommittedAllocationIdempotencyConflictError` (→409), `CommittedAllocationValidationError` (→422).
- **`backend/ums_smart_revenue/api/allocation.py`** — add the `POST /revenue/months/{month}/account-allocations/commit` route to the existing allocation router (sibling of the read route), with the auth (§9), the validation order (§8), the `request_fingerprint` computation, the compute call, the repo call, the audit, and the typed response.

## 11. Testing (RISK #4 — migration tests: Postgres constraints + SQLite compatibility)

Follow the established split (`tests/db/test_deduction_components_migration_postgres.py` for PG; `tests/db/test_channel_account_map_models.py` for SQLite).

- **Postgres migration tests** (`tests/db/test_committed_allocation_migration_postgres.py`, `require_postgres_url()` + `fresh_engine` DROP/CREATE `public` + `command.upgrade(cfg, "head")`):
  - inspect all four tables' columns, FKs (the tenant FK `fk_committed_allocation_runs_tenant` → `tenants(id)` on the **runs** table only; the `run_id` FK → `committed_allocation_runs(id)` on each of the three child tables), unique constraints (`uq_committed_allocation_runs_version`, `uq_committed_allocation_runs_idempotency`), CHECKs (month format, method, `commit_version>=1`, finite amounts), and indexes.
  - round-trip `upgrade head → downgrade <prev> → upgrade head`.
  - `IntegrityError` on: duplicate `(tenant_id, month, commit_version)`; duplicate `(tenant_id, idempotency_key)`; orphan tenant; malformed month; non-`gross_revenue_proportional` method (CHECK); and **`run_id` FK CASCADE** (deleting a run removes its lines/unallocated/notes).
- **SQLite model tests** (`tests/db/test_committed_allocation_models.py`, in-memory `create_all`, `PRAGMA foreign_keys=ON` for the FK-cascade assertion): insert run + lines + notes; portable CHECK violations (month format, method, `commit_version>=1`, non-empty reason) raise `IntegrityError`. Note in a comment that the PG-only finite-amount CHECKs are **not** enforced on SQLite (by design).
- **Repository tests** (`tests/finance/test_committed_allocation.py`): version increments (v1, v2); idempotency (same key+fingerprint → same run, no new row; same key+different fingerprint → conflict error); OPEN-month guard (LOCKED → `CommittedAllocationLockedMonthError`); reject-on-unallocated; method hard-fail; advisory-lock no-op on SQLite still produces correct versions.
- **API tests** (`tests/api/test_committed_allocation_api.py`): **201** happy path + `ALLOCATION_COMMITTED` audit (summary-only, reason present); **200** idempotent replay (identical body, **no second audit**, same `run_id`); **409** locked month; **409** idempotency conflict; **422** unallocated present; **422** malformed month; **422** unsupported method; **403** for each missing gate (no `CHANGE_ALLOCATION_RULE`; no `VIEW_REVENUE`; no `VIEW_FINALIZED_PAYMENTS`).
- **Reader-untouched regression** (`tests/api/test_committed_allocation_api.py` or net-revenue test): assert the net-revenue endpoint response is **byte-identical before and after a commit** for the same month — proving no read-switch leaked in.

## 12. Validation

```
python -m ruff check backend tests scripts
python -m pytest tests/db/test_committed_allocation_migration_postgres.py \
  tests/db/test_committed_allocation_models.py \
  tests/finance/test_committed_allocation.py \
  tests/api/test_committed_allocation_api.py \
  tests/api/test_allocations_api.py tests/api/test_net_revenue_api.py -q
python -m pytest -q          # full suite; PG-tier needs UMS_TEST_DATABASE_URL -> ums-mig-pg-test
git diff --check
```
Plus Alembic review: the new migration applies cleanly head-to-head on the disposable Postgres container and downgrades cleanly.

## 13. Blast radius

- **Tables/ORM:** four NEW tables + one migration. Additive. Tenant FK `RESTRICT`; run→children `CASCADE`.
- **PostgreSQL source of truth:** unchanged — this ADDS a persisted artifact that **drives no number this PR** (readers untouched).
- **Neo4j / graph projection:** **No graph projection impact detected** — finance write substrate only.
- **Authorization / audit:** reuse `CHANGE_ALLOCATION_RULE` + read gates; add `ALLOCATION_COMMITTED` (additive, fail-closed, reason-required). No existing gate weakened.
- **Finance results / locks / overrides / payment matching:** unchanged — commit is OPEN-only, alters no existing compute, lock semantics, or override flow. Existing month-lock writers are untouched.
- **Migration reversibility:** additive new tables; `downgrade()` drops them (destructive of committed data, acceptable for disposable pre-alpha data and documented as such).

Statement: **No graph projection impact detected.** **Disposable pre-alpha data: additive migration, downgrade drops the new tables.**

## 14. Plan-time mechanical confirmations
- The exact `down_revision` — the current Alembic head revision id on `main` (a `git`/`ls` lookup of `db/alembic/versions/`), not the `e1fe227` squash-commit sha.
- Whether the SQLite FK-cascade test needs `PRAGMA foreign_keys=ON` wiring (confirm against the existing SQLite test fixture in `tests/db/`).
