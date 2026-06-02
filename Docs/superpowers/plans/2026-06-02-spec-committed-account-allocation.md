# Committed Account Allocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist a versioned, audited snapshot of the existing `gross_revenue_proportional` account-allocation compute via a new `POST .../commit` endpoint and four new tables — the first allocation **write** path — with **no reader changes**.

**Architecture:** Four new `FinanceBase` tables + one Alembic migration; a repository that, on the shared request session, holds the finance-month advisory lock across idempotency lookup → open-check → method-validate → compute → reject-on-unallocated → insert; a thin `POST` route sibling to the existing allocation read route; a new `ALLOCATION_COMMITTED` audit event. Readers (net-revenue, the allocation read endpoint, exports) keep computing live.

**Tech Stack:** Python 3, SQLAlchemy 2.x (ORM + Alembic), FastAPI, PostgreSQL (source of truth), SQLite (unit tests), pytest.

**Source spec:** `Docs/superpowers/specs/2026-06-02-spec-committed-account-allocation-design.md` (HEAD `dcfde2e`).
**Branch:** `spec/committed-account-allocation` (off `main` `e1fe227`; spec commits `6e510d5`, `938f6e8`, `a6e39c9`, `dcfde2e`).

---

## Critical context (read before Task 1)

**Pinned decisions (do not drift — these were settled over four spec-review rounds):**
- **Write-only.** No reader switch. Net-revenue (`api/revenue.py`), the allocation read endpoint (`api/allocation.py` GET), and exports keep calling `compute_month_account_allocation` live. A regression test asserts net-revenue is byte-identical before/after a commit (Task 3).
- **Month-scoped idempotency:** unique + lookup on `(tenant_id, month, idempotency_key)`. The same key in a *different* month is a distinct, allowed commit (never a conflict).
- **Fingerprint** = `blake2b(digest_size=16)` hex over canonical JSON of `{allocation_method, reason}` only (month is the lookup scope, not in the fingerprint).
- **Lock held through compute:** the repository, on the **shared request session**, holds the finance-month advisory lock + close-row `FOR UPDATE` across: idempotency lookup → OPEN-month guard → **method validation (before compute)** → `compute_month_account_allocation` → **reject-on-unallocated (after compute)** → `commit_version = max+1` → insert. It **does not open or commit its own session/transaction** — the FastAPI session dependency commits after the route returns.
- **Reject-on-unallocated (422):** committed runs are always fully allocated; the `committed_allocation_unallocated` table ships for snapshot-schema fidelity but is empty in v1.
- **FK rules:** `tenant_id → tenants.id` `ondelete=RESTRICT` (runs header only); `run_id → committed_allocation_runs.id` `ondelete=CASCADE` (the three child tables).
- **Auth:** `VIEW_REVENUE@global` + `VIEW_FINALIZED_PAYMENTS@finance_month` + `CHANGE_ALLOCATION_RULE@finance_month`; new `ALLOCATION_COMMITTED` audit (`reason_required=True`, `permission=CHANGE_ALLOCATION_RULE`, summary-only detail).
- **Method:** only `gross_revenue_proportional`; any other → 422.
- **NOT in scope:** commit-on-lock, readiness blocker, `/revenue/recalculate` change, additional methods.

**Verified anchors (re-read before editing — line numbers may drift):**
- **Alembic head = `20260531_0001`** (confirmed via `python -m alembic heads`) → the new migration's `down_revision`.
- `finance/allocation.py`: `AllocationLine` (`:97-110`: `adsense_account_id, youtube_channel_id, component_kind, source_system, component_key, basis_source_kind` str; `basis_gross_usd, basis_share, allocated_amount_usd` Decimal; `net_applicable` bool), `UnallocatedIssue` (`:113-122`: `scope_id, component_kind, component_key` str; `amount_usd` Decimal; `issue_code, detail` str), `AllocationNote` (`:125-131`: `note_code, youtube_channel_id, detail` str), `AllocationSummary` (`:134-144`: `component_count, allocated_component_count, unallocated_component_count` int; `allocated_total_usd, unallocated_total_usd, net_applicable_total_usd, reconciliation_total_usd` Decimal), `AccountAllocationResult` (`:147-156`), `ALLOCATION_METHOD = "gross_revenue_proportional"` (`:21`).
- `api/allocation.py`: `_result_to_api(result)` serializes root keys `month, allocation_method, allocations, unallocated, notes, summary` (Decimals → strings via `decimal_to_api`); `router = APIRouter(prefix="/revenue", tags=["account-allocations"])`; `_require_permission(user, permission, scope)` → 403; `_require_valid_month(month)` → 422; the GET route's DI providers.
- `finance/allocation_inputs.py`: `compute_month_account_allocation(*, month, deduction_repository, revenue_repository, link_repository, adsense_account_id=None) -> AccountAllocationResult`.
- `finance/month_close.py`: `acquire_finance_month_advisory_lock(session, month, *, tenant_id=None)` (PG `pg_advisory_xact_lock`, SQLite no-op); `get_or_create_month_close_row(session, month, *, tenant_id=None, for_update=False) -> FinanceMonthCloseORM` (acquires the advisory lock + `SELECT ... FOR UPDATE` when `for_update=True`); the LOCKED guard pattern `if row.status == "LOCKED": raise ...`.
- `db/finance_models.py`: `_TENANT_ID_DEFAULT` / `_TENANT_ID_DEFAULT_VALUE`, `_month_format_check(col)`, UUID-PK + `Numeric(20,6)` + timestamp idioms, `.ddl_if(dialect="postgresql")` for PG-only CHECKs, `TenantORM`.
- `auth/audit.py`: `AuditEventType` StrEnum; `AuditEventDefinition(event_type, reason_required=False, permission=None)`; `AUDIT_EVENT_DEFINITIONS: dict[AuditEventType, AuditEventDefinition]`; `ALLOCATION_RULE_CHANGED` entry = `reason_required=True, permission=Permission.CHANGE_ALLOCATION_RULE`.
- `auth/permissions.py`: `Permission.VIEW_REVENUE/VIEW_FINALIZED_PAYMENTS/CHANGE_ALLOCATION_RULE`; `CHANGE_ALLOCATION_RULE` `sensitive=True`. `auth/scopes.py`: `AccessScope.global_scope()` / `AccessScope.finance_month(month)`.
- DI providers (in `api/revenue.py`): `current_deduction_component_repository`, `current_revenue_fact_repository`, `current_channel_account_link_repository`, `current_finance_month_close_repository` — each `(session: Annotated[Session, Depends(current_db_session)])`. `current_audit_sink` (`api/channels.py`) — overridden to a SQL sink in the app factory. The session dependency **commits after the route returns**.
- Test scaffolding: `tests/db/test_deduction_components_migration_postgres.py` (PG round-trip; `from _postgres_helpers import require_postgres_url`; `REPO_ROOT = Path(__file__).resolve().parents[2]`; `alembic_config`/`fresh_engine` fixtures) and `tests/db/test_channel_account_map_models.py` (SQLite `_engine(tmp_path)` → `FinanceBase.metadata.create_all`; `TENANT = UUID(UMS_TENANT_ID)`).

**Validation commands:** `python -m ruff check backend tests scripts` · `python -m pytest <files> -q` · full `python -m pytest -q` (PG-tier needs `UMS_TEST_DATABASE_URL` → `ums-mig-pg-test`; migration tests RAISE, never skip) · `git diff --check`.

**Commit discipline:** every commit message trailer-free (no `Co-Authored-By`, no Claude/Generated footer). Do NOT push or open a PR.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/ums_smart_revenue/db/finance_models.py` | +4 ORM models (runs/lines/unallocated/notes) |
| `backend/ums_smart_revenue/db/alembic/versions/20260602_0001_committed_account_allocation.py` | migration (create 4 tables; dialect-guarded PG CHECKs; FK-safe downgrade) |
| `backend/ums_smart_revenue/finance/committed_allocation.py` | repository + entries + `CommitAllocationOutcome` + typed errors |
| `backend/ums_smart_revenue/auth/audit.py` | `ALLOCATION_COMMITTED` event + definition |
| `backend/ums_smart_revenue/api/allocation.py` | `POST .../commit` route + `current_committed_allocation_repository` provider + fingerprint helper |
| `tests/db/test_committed_allocation_migration_postgres.py` | PG migration round-trip + constraints |
| `tests/db/test_committed_allocation_models.py` | SQLite model + CHECK/FK coverage |
| `tests/finance/test_committed_allocation.py` | repository behavior |
| `tests/api/test_committed_allocation_api.py` | endpoint + audit + reader-untouched |
| `Docs/01_IMPLEMENTATION_PLAN.md`, `Docs/15_DELIVERY_BACKLOG.md` | status |

---

## Task 1: ORM models + migration + DB tests

**Files:**
- Modify: `backend/ums_smart_revenue/db/finance_models.py`
- Create: `backend/ums_smart_revenue/db/alembic/versions/20260602_0001_committed_account_allocation.py`
- Create: `tests/db/test_committed_allocation_models.py`, `tests/db/test_committed_allocation_migration_postgres.py`

- [ ] **Step 1: Write the SQLite model test (failing)**

Create `tests/db/test_committed_allocation_models.py`:

```python
"""SQLite model + constraint coverage for the committed-allocation tables."""
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import (
    CommittedAllocationLineORM,
    CommittedAllocationNoteORM,
    CommittedAllocationRunORM,
    CommittedAllocationUnallocatedORM,
    FinanceBase,
)
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

TENANT = UUID(UMS_TENANT_ID)


def _engine(tmp_path):
    """Fresh on-disk SQLite engine with the finance schema, the tenants parent
    table + a tenant row, and FK enforcement on."""
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    # committed_allocation_runs FKs tenant_id -> tenants.id; `tenants` lives on
    # TenantBase (a separate base/metadata), so it must be created AND seeded with
    # the parent row before any run is inserted under FK enforcement.
    TenantBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(TenantORM(
            id=TENANT, slug="ums", display_name="UMS",
            primary_currency="USD", status="ACTIVE",
        ))
        session.commit()
    return engine


def _run(**overrides):
    """Build a valid CommittedAllocationRunORM with overridable fields."""
    base = dict(
        tenant_id=TENANT, month="2026-04", commit_version=1,
        allocation_method="gross_revenue_proportional",
        idempotency_key="key-1", request_fingerprint="fp-1",
        component_count=1, allocated_component_count=1, unallocated_component_count=0,
        allocated_total_usd=Decimal("100.000000"), unallocated_total_usd=Decimal("0"),
        net_applicable_total_usd=Decimal("100.000000"),
        reconciliation_total_usd=Decimal("0"),
        committed_by=TENANT, reason="month close",
    )
    base.update(overrides)
    return CommittedAllocationRunORM(**base)


def test_run_and_children_persist(tmp_path):
    """A run plus a line/unallocated/note row persist and read back."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        run = _run()
        session.add(run)
        session.flush()
        session.add(CommittedAllocationLineORM(
            run_id=run.id, adsense_account_id="pub-1", youtube_channel_id="chA",
            component_kind="DEDUCTION", source_system="adsense_management",
            component_key="k1", basis_source_kind="ADSENSE",
            basis_gross_usd=Decimal("1000.000000"), basis_share=Decimal("1.000000"),
            allocated_amount_usd=Decimal("100.000000"), net_applicable=True,
        ))
        session.add(CommittedAllocationNoteORM(
            run_id=run.id, note_code="CHANNEL_IN_MULTIPLE_ACCOUNTS",
            youtube_channel_id="chA", detail="x",
        ))
        session.commit()
        assert session.query(CommittedAllocationLineORM).count() == 1
        assert session.query(CommittedAllocationNoteORM).count() == 1


def test_method_check_rejects_other_method(tmp_path):
    """allocation_method CHECK rejects anything but gross_revenue_proportional."""
    engine = _engine(tmp_path)
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(_run(allocation_method="company_level"))
        session.commit()


def test_commit_version_check_rejects_zero(tmp_path):
    """commit_version CHECK rejects values < 1."""
    engine = _engine(tmp_path)
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(_run(commit_version=0))
        session.commit()


def test_month_format_check_rejects_bad_month(tmp_path):
    """month CHECK rejects malformed YYYY-MM."""
    engine = _engine(tmp_path)
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(_run(month="2026-13"))
        session.commit()


def test_version_unique_per_tenant_month(tmp_path):
    """(tenant_id, month, commit_version) is unique."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        session.add(_run(commit_version=1, idempotency_key="k1"))
        session.commit()
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(_run(commit_version=1, idempotency_key="k2"))
        session.commit()


def test_idempotency_unique_is_month_scoped(tmp_path):
    """Same idempotency_key collides within a month but is allowed across months."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        session.add(_run(month="2026-04", commit_version=1, idempotency_key="dup"))
        session.commit()
    # Same key, different month -> allowed.
    with Session(engine) as session:
        session.add(_run(month="2026-05", commit_version=1, idempotency_key="dup"))
        session.commit()
    # Same key, same month (new version) -> rejected by the idempotency unique.
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(_run(month="2026-04", commit_version=2, idempotency_key="dup"))
        session.commit()


def test_line_run_fk_cascade_delete(tmp_path):
    """Deleting a run cascades to its lines (FK ondelete=CASCADE)."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        run = _run()
        session.add(run)
        session.flush()
        run_id = run.id
        session.add(CommittedAllocationLineORM(
            run_id=run_id, adsense_account_id="pub-1", youtube_channel_id="chA",
            component_kind="DEDUCTION", source_system="adsense_management",
            component_key="k1", basis_source_kind="ADSENSE",
            basis_gross_usd=Decimal("1000.000000"), basis_share=Decimal("1.000000"),
            allocated_amount_usd=Decimal("100.000000"), net_applicable=True,
        ))
        session.commit()
        session.execute(
            text("DELETE FROM committed_allocation_runs WHERE id = :id"),
            {"id": str(run_id)},
        )
        session.commit()
        assert session.query(CommittedAllocationLineORM).count() == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/db/test_committed_allocation_models.py -q`
Expected: FAIL at import (`ImportError: cannot import name 'CommittedAllocationRunORM'`).

- [ ] **Step 3: Add the four ORM models**

In `backend/ums_smart_revenue/db/finance_models.py`, append the four models (after the existing finance models). Use the established idioms (`_TENANT_ID_DEFAULT(_VALUE)`, `_month_format_check`, `Numeric(20, 6)`, `.ddl_if(dialect="postgresql")`, `TenantORM`). Confirm `Integer`, `Boolean` are imported (add to the existing `from sqlalchemy import ...` if missing).

```python
class CommittedAllocationRunORM(FinanceBase):
    """Versioned, audited snapshot header of one gross_revenue_proportional commit."""

    # ========================================================================
    # Purpose: One committed account-allocation run (header) for a month — the
    #   durable, versioned snapshot of compute_month_account_allocation output.
    # Database/ORM: committed_allocation_runs (FinanceBase). Tenant-scoped;
    #   month-scoped idempotency on (tenant_id, month, idempotency_key);
    #   versioned on (tenant_id, month, commit_version).
    # Standards: append-only; finite USD totals (PG NaN/Inf-guarded via ddl_if);
    #   USD-only gross_revenue_proportional method.
    # Blast Radius: Finance write substrate (new table; additive). Drives no
    #   reader number yet (read-switch deferred). No auth/Neo4j.
    # Connections:
    #   - File: backend/ums_smart_revenue/finance/committed_allocation.py -> writer.
    #   - File: Docs/superpowers/specs/2026-06-02-spec-committed-account-allocation-design.md
    # ========================================================================
    __tablename__ = "committed_allocation_runs"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True,
        default=uuid4, server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE, server_default=_TENANT_ID_DEFAULT,
    )
    month: Mapped[str] = mapped_column(Text, nullable=False)
    commit_version: Mapped[int] = mapped_column(Integer, nullable=False)
    allocation_method: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    component_count: Mapped[int] = mapped_column(Integer, nullable=False)
    allocated_component_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unallocated_component_count: Mapped[int] = mapped_column(Integer, nullable=False)
    allocated_total_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    unallocated_total_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    net_applicable_total_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    reconciliation_total_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    committed_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    committed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"], [TenantORM.id],
            name="fk_committed_allocation_runs_tenant", ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id", "month", "commit_version",
            name="uq_committed_allocation_runs_version",
        ),
        UniqueConstraint(
            "tenant_id", "month", "idempotency_key",
            name="uq_committed_allocation_runs_idempotency",
        ),
        CheckConstraint(
            _month_format_check("month"),
            name="ck_committed_allocation_runs_month_format",
        ),
        CheckConstraint(
            "allocation_method = 'gross_revenue_proportional'",
            name="ck_committed_allocation_runs_method",
        ),
        CheckConstraint(
            "commit_version >= 1", name="ck_committed_allocation_runs_version_positive"
        ),
        CheckConstraint(
            "length(idempotency_key) >= 1",
            name="ck_committed_allocation_runs_idempotency_nonempty",
        ),
        CheckConstraint(
            "length(reason) >= 1", name="ck_committed_allocation_runs_reason_nonempty"
        ),
        CheckConstraint(
            "allocated_total_usd > '-Infinity'::numeric "
            "AND allocated_total_usd < 'Infinity'::numeric",
            name="ck_committed_allocation_runs_allocated_total_finite",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "unallocated_total_usd > '-Infinity'::numeric "
            "AND unallocated_total_usd < 'Infinity'::numeric",
            name="ck_committed_allocation_runs_unallocated_total_finite",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "net_applicable_total_usd > '-Infinity'::numeric "
            "AND net_applicable_total_usd < 'Infinity'::numeric",
            name="ck_committed_allocation_runs_net_applicable_total_finite",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "reconciliation_total_usd > '-Infinity'::numeric "
            "AND reconciliation_total_usd < 'Infinity'::numeric",
            name="ck_committed_allocation_runs_reconciliation_total_finite",
        ).ddl_if(dialect="postgresql"),
        Index("ix_committed_allocation_runs_tenant_month", "tenant_id", "month"),
    )


class CommittedAllocationLineORM(FinanceBase):
    """One persisted AllocationLine belonging to a committed run."""

    __tablename__ = "committed_allocation_lines"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True,
        default=uuid4, server_default=text("gen_random_uuid()"),
    )
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    adsense_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    youtube_channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    component_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    component_key: Mapped[str] = mapped_column(Text, nullable=False)
    basis_source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    basis_gross_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    basis_share: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    allocated_amount_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    net_applicable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id"], ["committed_allocation_runs.id"],
            name="fk_committed_allocation_lines_run", ondelete="CASCADE",
        ),
        CheckConstraint(
            "basis_gross_usd > '-Infinity'::numeric "
            "AND basis_gross_usd < 'Infinity'::numeric "
            "AND basis_share > '-Infinity'::numeric "
            "AND basis_share < 'Infinity'::numeric "
            "AND allocated_amount_usd > '-Infinity'::numeric "
            "AND allocated_amount_usd < 'Infinity'::numeric",
            name="ck_committed_allocation_lines_amounts_finite",
        ).ddl_if(dialect="postgresql"),
        Index("ix_committed_allocation_lines_run", "run_id"),
        Index(
            "ix_committed_allocation_lines_run_channel",
            "run_id", "youtube_channel_id",
        ),
    )


class CommittedAllocationUnallocatedORM(FinanceBase):
    """One persisted UnallocatedIssue belonging to a committed run (empty in v1)."""

    __tablename__ = "committed_allocation_unallocated"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True,
        default=uuid4, server_default=text("gen_random_uuid()"),
    )
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    scope_id: Mapped[str] = mapped_column(Text, nullable=False)
    component_kind: Mapped[str] = mapped_column(Text, nullable=False)
    component_key: Mapped[str] = mapped_column(Text, nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    issue_code: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id"], ["committed_allocation_runs.id"],
            name="fk_committed_allocation_unallocated_run", ondelete="CASCADE",
        ),
        Index("ix_committed_allocation_unallocated_run", "run_id"),
    )


class CommittedAllocationNoteORM(FinanceBase):
    """One persisted AllocationNote belonging to a committed run."""

    __tablename__ = "committed_allocation_notes"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True,
        default=uuid4, server_default=text("gen_random_uuid()"),
    )
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    note_code: Mapped[str] = mapped_column(Text, nullable=False)
    youtube_channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id"], ["committed_allocation_runs.id"],
            name="fk_committed_allocation_notes_run", ondelete="CASCADE",
        ),
        Index("ix_committed_allocation_notes_run", "run_id"),
    )
```

- [ ] **Step 4: Run the SQLite model test to verify it passes**

Run: `python -m pytest tests/db/test_committed_allocation_models.py -q`
Expected: PASS (7 tests). If the FK-cascade test fails, confirm the `PRAGMA foreign_keys=ON` connect listener is wired.

- [ ] **Step 5: Write the PostgreSQL migration test (failing)**

Create `tests/db/test_committed_allocation_migration_postgres.py`:

```python
"""PostgreSQL round-trip for 20260602_0001 (committed account allocation)."""
from pathlib import Path

import pytest
from _postgres_helpers import require_postgres_url
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def postgres_url() -> str:
    """Return the PostgreSQL database URL for testing."""
    return require_postgres_url()


@pytest.fixture
def alembic_config(postgres_url: str) -> Config:
    """Alembic config pointed at the test Postgres URL + script location."""
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", postgres_url)
    cfg.set_main_option(
        "script_location",
        str(REPO_ROOT / "backend" / "ums_smart_revenue" / "db" / "alembic"),
    )
    return cfg


@pytest.fixture
def fresh_engine(postgres_url: str):
    """A fresh engine with a clean public schema for each test."""
    engine = create_engine(postgres_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


_RUN_COLS = (
    "tenant_id, month, commit_version, allocation_method, idempotency_key, "
    "request_fingerprint, component_count, allocated_component_count, "
    "unallocated_component_count, allocated_total_usd, unallocated_total_usd, "
    "net_applicable_total_usd, reconciliation_total_usd, committed_by, reason"
)


def _insert_run_sql(**ov) -> tuple[str, dict]:
    params = dict(
        tenant=UMS_TENANT_ID, month="2026-04", version=1,
        method="gross_revenue_proportional", key="k1", fp="fp1",
        reason="close",
    )
    params.update(ov)
    sql = text(
        f"INSERT INTO committed_allocation_runs ({_RUN_COLS}) VALUES "
        "(:tenant, :month, :version, :method, :key, :fp, 1, 1, 0, "
        "100.000000, 0, 100.000000, 0, :tenant, :reason)"
    )
    return sql, params


def test_upgrade_creates_tables_constraints_indexes(alembic_config, fresh_engine):
    """Upgrade to head creates all four tables with constraints + indexes."""
    command.upgrade(alembic_config, "head")
    inspector = inspect(fresh_engine)
    names = set(inspector.get_table_names())
    assert {
        "committed_allocation_runs", "committed_allocation_lines",
        "committed_allocation_unallocated", "committed_allocation_notes",
    } <= names
    uniques = {
        c["name"]: tuple(c["column_names"])
        for c in inspector.get_unique_constraints("committed_allocation_runs")
    }
    assert uniques["uq_committed_allocation_runs_version"] == (
        "tenant_id", "month", "commit_version"
    )
    assert uniques["uq_committed_allocation_runs_idempotency"] == (
        "tenant_id", "month", "idempotency_key"
    )
    run_fks = {c["name"]: c for c in inspector.get_foreign_keys("committed_allocation_runs")}
    assert run_fks["fk_committed_allocation_runs_tenant"]["referred_table"] == "tenants"
    line_fks = {c["name"]: c for c in inspector.get_foreign_keys("committed_allocation_lines")}
    assert (
        line_fks["fk_committed_allocation_lines_run"]["referred_table"]
        == "committed_allocation_runs"
    )
    assert line_fks["fk_committed_allocation_lines_run"]["options"]["ondelete"] == "CASCADE"
    checks = {
        c["name"] for c in inspector.get_check_constraints("committed_allocation_runs")
    }
    assert "ck_committed_allocation_runs_method" in checks
    assert "ck_committed_allocation_runs_version_positive" in checks
    assert "ck_committed_allocation_runs_month_format" in checks


def test_round_trip_idempotency(alembic_config, fresh_engine):
    """upgrade -> downgrade -> upgrade keeps the schema consistent."""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "20260531_0001")
    inspector = inspect(fresh_engine)
    assert "committed_allocation_runs" not in inspector.get_table_names()
    command.upgrade(alembic_config, "head")
    assert "committed_allocation_runs" in inspect(fresh_engine).get_table_names()


def test_duplicate_version_rejected(alembic_config, fresh_engine):
    """(tenant_id, month, commit_version) uniqueness is enforced."""
    command.upgrade(alembic_config, "head")
    sql, params = _insert_run_sql(version=1, key="a")
    with fresh_engine.begin() as conn:
        conn.execute(sql, params)
    sql2, params2 = _insert_run_sql(version=1, key="b")
    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(sql2, params2)


def test_idempotency_unique_month_scoped(alembic_config, fresh_engine):
    """Same key collides within a month; the same key in another month inserts."""
    command.upgrade(alembic_config, "head")
    s1, p1 = _insert_run_sql(month="2026-04", version=1, key="dup")
    s2, p2 = _insert_run_sql(month="2026-05", version=1, key="dup")  # different month -> OK
    with fresh_engine.begin() as conn:
        conn.execute(s1, p1)
        conn.execute(s2, p2)
    s3, p3 = _insert_run_sql(month="2026-04", version=2, key="dup")  # same month -> reject
    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(s3, p3)


def test_orphan_tenant_rejected(alembic_config, fresh_engine):
    """Runs must reference a real tenant (FK RESTRICT)."""
    command.upgrade(alembic_config, "head")
    sql, params = _insert_run_sql(tenant="00000000-0000-0000-0000-0000000000aa", key="x")
    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(sql, params)


def test_bad_method_rejected(alembic_config, fresh_engine):
    """allocation_method CHECK rejects non-gross_revenue_proportional methods."""
    command.upgrade(alembic_config, "head")
    sql, params = _insert_run_sql(method="company_level", key="x")
    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(sql, params)


def test_run_delete_cascades_to_lines(alembic_config, fresh_engine):
    """Deleting a run cascades to its lines."""
    command.upgrade(alembic_config, "head")
    sql, params = _insert_run_sql(key="x")
    with fresh_engine.begin() as conn:
        conn.execute(sql, params)
        run_id = conn.execute(
            text("SELECT id FROM committed_allocation_runs LIMIT 1")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO committed_allocation_lines "
                "(run_id, adsense_account_id, youtube_channel_id, component_kind, "
                "source_system, component_key, basis_source_kind, basis_gross_usd, "
                "basis_share, allocated_amount_usd, net_applicable) VALUES "
                "(:rid, 'pub-1', 'chA', 'DEDUCTION', 'adsense_management', 'k1', "
                "'ADSENSE', 1000.000000, 1.000000, 100.000000, true)"
            ),
            {"rid": run_id},
        )
        conn.execute(
            text("DELETE FROM committed_allocation_runs WHERE id = :rid"), {"rid": run_id}
        )
        remaining = conn.execute(
            text("SELECT count(*) FROM committed_allocation_lines")
        ).scalar_one()
    assert remaining == 0
```

- [ ] **Step 6: Run to verify it fails**

Run: `$env:UMS_TEST_DATABASE_URL='postgresql+psycopg://postgres:ums@localhost:55432/test_ums'; python -m pytest tests/db/test_committed_allocation_migration_postgres.py -q`
Expected: FAIL (the migration doesn't exist yet → `command.upgrade` cannot create the tables / table not found).

- [ ] **Step 7: Write the migration**

Create `backend/ums_smart_revenue/db/alembic/versions/20260602_0001_committed_account_allocation.py`, mirroring `20260529_0002_deduction_components.py`:

```python
"""Create committed account-allocation snapshot tables (Phase 4 Spec 2b)."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

revision = "20260602_0001"
down_revision = "20260531_0001"
branch_labels = None
depends_on = None


def _finite(column: str) -> str:
    """Postgres-only finite (non-NaN, non-Inf) guard for a numeric column."""
    return f"{column} > '-Infinity'::numeric AND {column} < 'Infinity'::numeric"


def upgrade() -> None:
    """Create the four committed-allocation tables with constraints + indexes."""
    op.create_table(
        "committed_allocation_runs",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Uuid(), nullable=False, server_default=sa.text(f"'{UMS_TENANT_ID}'")),
        sa.Column("month", sa.Text(), nullable=False),
        sa.Column("commit_version", sa.Integer(), nullable=False),
        sa.Column("allocation_method", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.Column("component_count", sa.Integer(), nullable=False),
        sa.Column("allocated_component_count", sa.Integer(), nullable=False),
        sa.Column("unallocated_component_count", sa.Integer(), nullable=False),
        sa.Column("allocated_total_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("unallocated_total_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("net_applicable_total_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("reconciliation_total_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("committed_by", sa.Uuid(), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_committed_allocation_runs_tenant", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "tenant_id", "month", "commit_version",
            name="uq_committed_allocation_runs_version",
        ),
        sa.UniqueConstraint(
            "tenant_id", "month", "idempotency_key",
            name="uq_committed_allocation_runs_idempotency",
        ),
        sa.CheckConstraint(
            "length(month) = 7 AND substr(month, 5, 1) = '-' "
            "AND substr(month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 7, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_committed_allocation_runs_month_format",
        ),
        sa.CheckConstraint(
            "allocation_method = 'gross_revenue_proportional'",
            name="ck_committed_allocation_runs_method",
        ),
        sa.CheckConstraint("commit_version >= 1", name="ck_committed_allocation_runs_version_positive"),
        sa.CheckConstraint("length(idempotency_key) >= 1", name="ck_committed_allocation_runs_idempotency_nonempty"),
        sa.CheckConstraint("length(reason) >= 1", name="ck_committed_allocation_runs_reason_nonempty"),
    )
    op.create_table(
        "committed_allocation_lines",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("adsense_account_id", sa.Text(), nullable=False),
        sa.Column("youtube_channel_id", sa.Text(), nullable=False),
        sa.Column("component_kind", sa.Text(), nullable=False),
        sa.Column("source_system", sa.Text(), nullable=False),
        sa.Column("component_key", sa.Text(), nullable=False),
        sa.Column("basis_source_kind", sa.Text(), nullable=False),
        sa.Column("basis_gross_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("basis_share", sa.Numeric(20, 6), nullable=False),
        sa.Column("allocated_amount_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("net_applicable", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["run_id"], ["committed_allocation_runs.id"],
            name="fk_committed_allocation_lines_run", ondelete="CASCADE",
        ),
    )
    op.create_table(
        "committed_allocation_unallocated",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("component_kind", sa.Text(), nullable=False),
        sa.Column("component_key", sa.Text(), nullable=False),
        sa.Column("amount_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("issue_code", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["run_id"], ["committed_allocation_runs.id"],
            name="fk_committed_allocation_unallocated_run", ondelete="CASCADE",
        ),
    )
    op.create_table(
        "committed_allocation_notes",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("note_code", sa.Text(), nullable=False),
        sa.Column("youtube_channel_id", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["run_id"], ["committed_allocation_runs.id"],
            name="fk_committed_allocation_notes_run", ondelete="CASCADE",
        ),
    )
    # Postgres-only finite guards (invalid SQLite CREATE syntax), mirroring the
    # ORM's .ddl_if(dialect="postgresql") CHECKs in finance_models.py.
    if op.get_bind().dialect.name == "postgresql":
        for col in (
            "allocated_total_usd", "unallocated_total_usd",
            "net_applicable_total_usd", "reconciliation_total_usd",
        ):
            op.create_check_constraint(
                f"ck_committed_allocation_runs_{col}_finite",
                "committed_allocation_runs", _finite(col),
            )
        op.create_check_constraint(
            "ck_committed_allocation_lines_amounts_finite",
            "committed_allocation_lines",
            f"{_finite('basis_gross_usd')} AND {_finite('basis_share')} "
            f"AND {_finite('allocated_amount_usd')}",
        )
    op.create_index(
        "ix_committed_allocation_runs_tenant_month",
        "committed_allocation_runs", ["tenant_id", "month"],
    )
    op.create_index(
        "ix_committed_allocation_lines_run", "committed_allocation_lines", ["run_id"]
    )
    op.create_index(
        "ix_committed_allocation_lines_run_channel",
        "committed_allocation_lines", ["run_id", "youtube_channel_id"],
    )
    op.create_index(
        "ix_committed_allocation_unallocated_run",
        "committed_allocation_unallocated", ["run_id"],
    )
    op.create_index(
        "ix_committed_allocation_notes_run", "committed_allocation_notes", ["run_id"]
    )


def downgrade() -> None:
    """Drop the four tables (children first for FK safety)."""
    op.drop_table("committed_allocation_notes")
    op.drop_table("committed_allocation_unallocated")
    op.drop_table("committed_allocation_lines")
    op.drop_table("committed_allocation_runs")
```

- [ ] **Step 8: Run both DB test files to verify they pass**

Run: `$env:UMS_TEST_DATABASE_URL='postgresql+psycopg://postgres:ums@localhost:55432/test_ums'; python -m pytest tests/db/test_committed_allocation_models.py tests/db/test_committed_allocation_migration_postgres.py -q`
Expected: PASS (all). Then `python -m ruff check backend tests` → clean.

- [ ] **Step 9: Commit**

```bash
git add backend/ums_smart_revenue/db/finance_models.py backend/ums_smart_revenue/db/alembic/versions/20260602_0001_committed_account_allocation.py tests/db/test_committed_allocation_models.py tests/db/test_committed_allocation_migration_postgres.py
git commit -m "feat(finance): committed account-allocation snapshot tables + migration"
```

---

## Task 2: Repository (`finance/committed_allocation.py`) + tests

The repository owns the locked commit unit on the **shared request session** (no session creation, no commit).

**Files:**
- Create: `backend/ums_smart_revenue/finance/committed_allocation.py`
- Test: `tests/finance/test_committed_allocation.py`

- [ ] **Step 1: Write the repository tests (failing)**

Create `tests/finance/test_committed_allocation.py`. Reuse the real allocation compute by seeding deduction-component / revenue-fact / channel-account-link repositories on a SQLite session (the same way `tests/api/test_exports_account_allocation.py` and `tests/finance/test_net_revenue_account_allocations.py` build inputs). Build a small local fixture that produces exactly one ACCOUNT component allocating cleanly to one verified channel (fully allocated, zero unallocated).

```python
"""Repository behavior for committed account allocation (write path)."""
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    ContentOwnerChannelLinkORM,
    DeductionComponentORM,
    FinanceBase,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, YouTubeChannelORM
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.finance.channel_account_links import (
    SqlAlchemyChannelAccountLinkRepository,
)
from ums_smart_revenue.finance.committed_allocation import (
    CommittedAllocationIdempotencyConflictError,
    CommittedAllocationLockedMonthError,
    CommittedAllocationValidationError,
    SqlAlchemyCommittedAllocationRepository,
)
from ums_smart_revenue.finance.deduction_ingestion import (
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.revenue_facts import SqlAlchemyRevenueFactRepository
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

TENANT = UUID(UMS_TENANT_ID)
MONTH = "2026-04"
ACTOR = str(TENANT)  # any UUID-literal actor; the repo maps it via actor_identity_uuid


def _session(tmp_path) -> Session:
    """Fresh SQLite session with org + finance schema and FK enforcement on."""
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    # YouTubeChannelORM lives on OrgBase; the finance rows live on FinanceBase; and
    # `tenants` (the FK parent for deduction/link rows) lives on TenantBase, a
    # separate base. All three schemas must exist, and the tenant parent row must be
    # inserted before any tenant-scoped row under FK enforcement.
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    session = Session(engine)
    session.add(TenantORM(
        id=TENANT, slug="ums", display_name="UMS",
        primary_currency="USD", status="ACTIVE",
    ))
    session.commit()
    return session


def _seed_account_deduction(session, *, mapped: bool, status: str = "OPEN") -> None:
    """Seed one ACCOUNT DEDUCTION (pub-1, 100.00) over channel chA (ADSENSE 1000.00).

    Field-for-field the shape of `_seed_missing_net_with_components` in
    tests/api/test_exports_account_allocation.py, reduced to a single ACCOUNT
    component. With mapped=True the account resolves to chA via a VERIFIED
    Adsense->owner link plus an active owner->channel link, so the compute returns
    exactly one fully-allocated line and zero unallocated issues. With mapped=False
    the two link rows are omitted, so pub-1 resolves to no channel and the compute
    yields one UnallocatedIssue. `status` seeds the finance-month close row
    ("OPEN" or "LOCKED") so the OPEN-month guard can be exercised without going
    through the month-close readiness path.
    """
    session.add(YouTubeChannelORM(
        id=uuid4(), tenant_id=TENANT, youtube_channel_id="chA",
        channel_name="A", active=True,
    ))
    # Flush the channel before the fact: monthly_channel_revenue_facts has a
    # composite FK (tenant_id, youtube_channel_id) -> youtube_channels that crosses
    # the Org/Finance registries, so the unit-of-work does NOT order the channel
    # insert before the dependent fact on its own. Required under FK enforcement.
    session.flush()
    session.add(MonthlyChannelRevenueFactORM(
        id=uuid4(), tenant_id=TENANT, month=MONTH, youtube_channel_id="chA",
        source_kind="ADSENSE", gross_revenue_usd=Decimal("1000.00"),
        net_revenue_usd=None,
    ))
    session.add(DeductionComponentORM(
        id=uuid4(), tenant_id=TENANT, month=MONTH, component_kind="DEDUCTION",
        scope_kind="ACCOUNT", scope_id="pub-1", amount_usd=Decimal("100.00"),
        currency_code="USD", source_system="adsense_management",
        source_table="google_revenue_source_rows", component_key="ad-1",
        raw_payload={},
    ))
    if mapped:
        session.add(AdsenseContentOwnerLinkORM(
            id=uuid4(), tenant_id=TENANT, adsense_account_id="pub-1",
            content_owner_id="owner-1", verification_status="VERIFIED",
            provenance_kind="OPERATOR_ASSERTED", provenance_payload={},
            effective_month_start="2026-01",
        ))
        session.add(ContentOwnerChannelLinkORM(
            id=uuid4(), tenant_id=TENANT, content_owner_id="owner-1",
            youtube_channel_id="chA", provenance_kind="SOURCE_ROW", active=True,
            effective_month_start="2026-01",
        ))
    session.add(FinanceMonthCloseORM(
        tenant_id=TENANT, month=MONTH, status=status, allocation_rule_payload={},
    ))
    session.commit()


def _repos(session):
    """Return (committed_repo, deduction_repo, revenue_repo, link_repo) on `session`."""
    return (
        SqlAlchemyCommittedAllocationRepository(session),
        SqlAlchemyDeductionComponentRepository(session),
        SqlAlchemyRevenueFactRepository(session),
        SqlAlchemyChannelAccountLinkRepository(session),
    )


def _commit(committed, ded, rev, link, *, key="k1", fp="fp1", reason="close",
            method="gross_revenue_proportional"):
    return committed.commit_allocation(
        month=MONTH, allocation_method=method, idempotency_key=key,
        request_fingerprint=fp, reason=reason, committed_by=ACTOR,
        deduction_repository=ded, revenue_repository=rev, link_repository=link,
    )


def test_first_commit_creates_version_1(tmp_path):
    """A fresh commit creates run v1 with created=True and persists the line."""
    session = _session(tmp_path)
    _seed_account_deduction(session, mapped=True)
    committed, ded, rev, link = _repos(session)
    outcome = _commit(committed, ded, rev, link)
    assert outcome.created is True
    assert outcome.run.commit_version == 1
    assert outcome.run.allocated_total_usd == Decimal("100.000000")
    assert len(outcome.lines) == 1
    assert outcome.lines[0].youtube_channel_id == "chA"


def test_idempotent_replay_returns_same_run(tmp_path):
    """Same (month, key, fingerprint) returns the existing run, created=False."""
    session = _session(tmp_path)
    _seed_account_deduction(session, mapped=True)
    committed, ded, rev, link = _repos(session)
    first = _commit(committed, ded, rev, link, key="dup", fp="same")
    replay = _commit(committed, ded, rev, link, key="dup", fp="same")
    assert replay.created is False
    assert replay.run.id == first.run.id
    assert replay.run.commit_version == 1  # no new version


def test_same_key_different_fingerprint_conflicts(tmp_path):
    """Same (month, key) with a different fingerprint raises a conflict."""
    session = _session(tmp_path)
    _seed_account_deduction(session, mapped=True)
    committed, ded, rev, link = _repos(session)
    _commit(committed, ded, rev, link, key="dup", fp="fp-a")
    with pytest.raises(CommittedAllocationIdempotencyConflictError):
        _commit(committed, ded, rev, link, key="dup", fp="fp-b")


def test_new_key_same_month_increments_version(tmp_path):
    """A new key in the same month creates the next commit_version."""
    session = _session(tmp_path)
    _seed_account_deduction(session, mapped=True)
    committed, ded, rev, link = _repos(session)
    _commit(committed, ded, rev, link, key="k1", fp="f1")
    second = _commit(committed, ded, rev, link, key="k2", fp="f2")
    assert second.created is True
    assert second.run.commit_version == 2


def test_locked_month_rejected(tmp_path):
    """Committing a LOCKED month raises CommittedAllocationLockedMonthError."""
    session = _session(tmp_path)
    _seed_account_deduction(session, mapped=True, status="LOCKED")
    committed, ded, rev, link = _repos(session)
    with pytest.raises(CommittedAllocationLockedMonthError):
        _commit(committed, ded, rev, link)


def test_unsupported_method_rejected_before_compute(tmp_path):
    """A non-gross_revenue_proportional method is rejected (validation error)."""
    session = _session(tmp_path)
    _seed_account_deduction(session, mapped=True)
    committed, ded, rev, link = _repos(session)
    with pytest.raises(CommittedAllocationValidationError):
        _commit(committed, ded, rev, link, method="company_level")


def test_reject_on_unallocated(tmp_path):
    """An unmapped account (no verified channel link) blocks the commit."""
    session = _session(tmp_path)
    _seed_account_deduction(session, mapped=False)
    committed, ded, rev, link = _repos(session)
    with pytest.raises(CommittedAllocationValidationError):
        _commit(committed, ded, rev, link)
```

The seed sets the close row's `status` directly ("OPEN" / "LOCKED"), so the
locked-month path is exercised without invoking the month-close readiness flow.
For mapped=False the two link rows are omitted, so `pub-1` resolves to no channel
and the compute yields one `UnallocatedIssue` (the fail-closed UNALLOCATED path),
which `commit_allocation` rejects with `CommittedAllocationValidationError`.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/finance/test_committed_allocation.py -q`
Expected: FAIL at collection/import (`cannot import name 'SqlAlchemyCommittedAllocationRepository' from 'ums_smart_revenue.finance.committed_allocation'` — the module does not exist yet). The seed helpers reference only existing repositories/ORMs, so the failure is the missing committed-allocation module, not the fixtures.

- [ ] **Step 3: Implement the repository**

Create `backend/ums_smart_revenue/finance/committed_allocation.py`:

```python
"""Committed account-allocation write path (Phase 4 Spec 2b).

Persists a versioned, audited snapshot of the gross_revenue_proportional
compute. Runs on the shared request session and holds the finance-month
advisory lock across idempotency lookup, OPEN-month guard, method validation,
compute, reject-on-unallocated, version assignment, and the row inserts. It
NEVER opens or commits its own session/transaction — the FastAPI session
dependency commits after the route returns.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.actor_identity import actor_identity_uuid
from ums_smart_revenue.db.finance_models import (
    CommittedAllocationLineORM,
    CommittedAllocationNoteORM,
    CommittedAllocationRunORM,
    CommittedAllocationUnallocatedORM,
)
from ums_smart_revenue.finance.allocation import ALLOCATION_METHOD, AccountAllocationResult
from ums_smart_revenue.finance.allocation_inputs import compute_month_account_allocation
from ums_smart_revenue.finance.month_close import get_or_create_month_close_row
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)


class CommittedAllocationValidationError(ValueError):
    """Unsupported method, unallocated components, or invalid actor/tenant (-> 422)."""


class CommittedAllocationLockedMonthError(RuntimeError):
    """The finance month is LOCKED; a new commit is rejected (-> 409)."""


class CommittedAllocationIdempotencyConflictError(RuntimeError):
    """The idempotency key was reused with a different request (-> 409)."""


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    """Resolve explicit, ambient, or default tenant UUID for repository scoping."""
    if isinstance(tenant_id, UUID):
        return tenant_id
    if tenant_id is None:
        current_tenant = get_current_tenant()
        if current_tenant is not None:
            return current_tenant.id
        return _DEFAULT_TENANT_UUID
    try:
        return UUID(str(tenant_id).strip())
    except ValueError as exc:
        raise CommittedAllocationValidationError(
            f"invalid tenant_id: {tenant_id!r}"
        ) from exc


def _actor_identity_uuid(value: str) -> UUID:
    """Parse/derive the committed_by UUID (UUID literal or gateway subject)."""
    try:
        return actor_identity_uuid(value)
    except ValueError as exc:
        raise CommittedAllocationValidationError(str(exc)) from exc


@dataclass(frozen=True)
class CommitAllocationOutcome:
    """A committed run plus its child rows and a created/replayed flag."""

    run: CommittedAllocationRunORM
    lines: tuple[CommittedAllocationLineORM, ...]
    unallocated: tuple[CommittedAllocationUnallocatedORM, ...]
    notes: tuple[CommittedAllocationNoteORM, ...]
    created: bool


class SqlAlchemyCommittedAllocationRepository:
    """Persist committed allocation runs on the shared request session."""

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None) -> None:
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    # ========================================================================
    # Purpose: Commit a versioned snapshot of the gross_revenue_proportional
    #   compute for one month, under the finance-month advisory lock.
    # Database/ORM: committed_allocation_runs/_lines/_unallocated/_notes;
    #   reads FinanceMonthCloseORM (lock) via month_close helpers.
    # Standards: shared request session (no commit here); typed errors -> route
    #   422/409; method-before-compute; reject-on-unallocated.
    # Blast Radius: Finance write; first allocation persistence. No reader change.
    # ========================================================================
    def commit_allocation(
        self,
        *,
        month: str,
        allocation_method: str,
        idempotency_key: str,
        request_fingerprint: str,
        reason: str,
        committed_by: str,
        deduction_repository: object,
        revenue_repository: object,
        link_repository: object,
    ) -> CommitAllocationOutcome:
        """Compute + persist a committed run, or replay an idempotent retry."""
        committed_by_uuid = _actor_identity_uuid(committed_by)
        # Hold the finance-month advisory lock + close-row FOR UPDATE for the
        # whole unit (the lock is transaction-scoped on Postgres; no-op on SQLite).
        close_row = get_or_create_month_close_row(
            self._session, month, tenant_id=self._tenant_id, for_update=True
        )

        existing = self._session.scalars(
            select(CommittedAllocationRunORM).where(
                CommittedAllocationRunORM.tenant_id == self._tenant_id,
                CommittedAllocationRunORM.month == month,
                CommittedAllocationRunORM.idempotency_key == idempotency_key,
            )
        ).one_or_none()
        if existing is not None:
            if existing.request_fingerprint != request_fingerprint:
                raise CommittedAllocationIdempotencyConflictError(
                    "idempotency key reused with a different request"
                )
            return self._replay(existing)

        if close_row.status == "LOCKED":
            raise CommittedAllocationLockedMonthError(f"Finance month is locked: {month}")

        if allocation_method != ALLOCATION_METHOD:
            raise CommittedAllocationValidationError(
                f"unsupported allocation method: {allocation_method}"
            )

        result: AccountAllocationResult = compute_month_account_allocation(
            month=month,
            deduction_repository=deduction_repository,
            revenue_repository=revenue_repository,
            link_repository=link_repository,
        )
        if result.unallocated:
            raise CommittedAllocationValidationError(
                f"cannot commit: {len(result.unallocated)} unallocated component(s)"
            )

        next_version = (
            self._session.scalars(
                select(CommittedAllocationRunORM.commit_version).where(
                    CommittedAllocationRunORM.tenant_id == self._tenant_id,
                    CommittedAllocationRunORM.month == month,
                ).order_by(CommittedAllocationRunORM.commit_version.desc())
            ).first()
            or 0
        ) + 1

        run = CommittedAllocationRunORM(
            tenant_id=self._tenant_id, month=month, commit_version=next_version,
            allocation_method=result.allocation_method,
            idempotency_key=idempotency_key, request_fingerprint=request_fingerprint,
            component_count=result.summary.component_count,
            allocated_component_count=result.summary.allocated_component_count,
            unallocated_component_count=result.summary.unallocated_component_count,
            allocated_total_usd=result.summary.allocated_total_usd,
            unallocated_total_usd=result.summary.unallocated_total_usd,
            net_applicable_total_usd=result.summary.net_applicable_total_usd,
            reconciliation_total_usd=result.summary.reconciliation_total_usd,
            committed_by=committed_by_uuid, reason=reason,
        )
        self._session.add(run)
        self._session.flush()  # assign run.id

        lines = tuple(
            CommittedAllocationLineORM(
                run_id=run.id, adsense_account_id=ln.adsense_account_id,
                youtube_channel_id=ln.youtube_channel_id,
                component_kind=ln.component_kind, source_system=ln.source_system,
                component_key=ln.component_key, basis_source_kind=ln.basis_source_kind,
                basis_gross_usd=ln.basis_gross_usd, basis_share=ln.basis_share,
                allocated_amount_usd=ln.allocated_amount_usd,
                net_applicable=ln.net_applicable,
            )
            for ln in result.lines
        )
        notes = tuple(
            CommittedAllocationNoteORM(
                run_id=run.id, note_code=note.note_code,
                youtube_channel_id=note.youtube_channel_id, detail=note.detail,
            )
            for note in result.notes
        )
        # result.unallocated is empty here (reject-on-unallocated above); the
        # comprehension is retained for snapshot fidelity / future draft state.
        unallocated = tuple(
            CommittedAllocationUnallocatedORM(
                run_id=run.id, scope_id=iss.scope_id, component_kind=iss.component_kind,
                component_key=iss.component_key, amount_usd=iss.amount_usd,
                issue_code=iss.issue_code, detail=iss.detail,
            )
            for iss in result.unallocated
        )
        for child in (*lines, *notes, *unallocated):
            self._session.add(child)
        self._session.flush()
        return CommitAllocationOutcome(
            run=run, lines=lines, unallocated=unallocated, notes=notes, created=True
        )

    def _replay(self, run: CommittedAllocationRunORM) -> CommitAllocationOutcome:
        """Load an existing run's children for an idempotent replay."""
        lines = tuple(self._session.scalars(
            select(CommittedAllocationLineORM).where(
                CommittedAllocationLineORM.run_id == run.id
            )
        ).all())
        unallocated = tuple(self._session.scalars(
            select(CommittedAllocationUnallocatedORM).where(
                CommittedAllocationUnallocatedORM.run_id == run.id
            )
        ).all())
        notes = tuple(self._session.scalars(
            select(CommittedAllocationNoteORM).where(
                CommittedAllocationNoteORM.run_id == run.id
            )
        ).all())
        return CommitAllocationOutcome(
            run=run, lines=lines, unallocated=unallocated, notes=notes, created=False
        )

    def get_latest_run(self, month: str) -> CommittedAllocationRunORM | None:
        """Return the highest-version run for a month (NOT wired into readers)."""
        return self._session.scalars(
            select(CommittedAllocationRunORM).where(
                CommittedAllocationRunORM.tenant_id == self._tenant_id,
                CommittedAllocationRunORM.month == month,
            ).order_by(CommittedAllocationRunORM.commit_version.desc())
        ).first()

    def get_run_by_idempotency_key(
        self, month: str, idempotency_key: str
    ) -> CommittedAllocationRunORM | None:
        """Return the run for a month-scoped idempotency key, if any."""
        return self._session.scalars(
            select(CommittedAllocationRunORM).where(
                CommittedAllocationRunORM.tenant_id == self._tenant_id,
                CommittedAllocationRunORM.month == month,
                CommittedAllocationRunORM.idempotency_key == idempotency_key,
            )
        ).one_or_none()
```

(Re-confirm `get_or_create_month_close_row` / `acquire_finance_month_advisory_lock` signatures + the `status` attribute name against `month_close.py` before finalizing; `get_or_create_month_close_row(for_update=True)` already acquires the advisory lock, so an explicit `acquire_finance_month_advisory_lock` call is redundant — keep the single `for_update=True` call.)

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/finance/test_committed_allocation.py -q`
Expected: PASS. Then `python -m ruff check backend tests` → clean.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/committed_allocation.py tests/finance/test_committed_allocation.py
git commit -m "feat(finance): committed allocation repository (lock-held compute + versioned snapshot)"
```

---

## Task 3: Audit event + commit endpoint + API tests

**Files:**
- Modify: `backend/ums_smart_revenue/auth/audit.py`
- Modify: `backend/ums_smart_revenue/api/allocation.py`
- Test: `tests/api/test_committed_allocation_api.py`

- [ ] **Step 1: Write the API tests (failing)**

Create `tests/api/test_committed_allocation_api.py`, modeled on the existing allocation/net-revenue API tests (`tests/api/test_allocation_api.py`, `tests/api/test_net_revenue_api.py`): build the app via `create_app(database_url=...)`, seed a fully-allocated month, and post to the commit route with a global finance principal. Cover: **201** + audit (summary-only), **200** idempotent replay (no second audit), **409** locked, **409** idempotency conflict, **422** unallocated, **422** malformed month, **422** unsupported method, **403** for each missing gate (parametrized), and the **reader-untouched regression** (net-revenue byte-identical before/after a commit).

```python
"""API tests for POST /revenue/months/{month}/account-allocations/commit."""
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    ContentOwnerChannelLinkORM,
    DeductionComponentORM,
    FinanceBase,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

MONTH = "2026-03"
TENANT = UUID(UMS_TENANT_ID)
SECTOR_ID = UUID("00000000-0000-0000-0000-0000000a0101")
COMPANY_ID = UUID("00000000-0000-0000-0000-0000000a0201")
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-0000000a0301")
USER_ID = UUID("00000000-0000-0000-0000-0000000a0401")
COMMIT_PATH = f"/revenue/months/{MONTH}/account-allocations/commit"


def build_database_url(tmp_path) -> str:
    """Return a unique SQLite URL under pytest's temp path."""
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def _seed(database_url: str, *, mapped: bool = True, status: str = "OPEN") -> None:
    """Seed org/security/finance rows for the commit endpoint.

    chA has ADSENSE gross 1000 and NO source net; an ACCOUNT DEDUCTION (pub-1, 100)
    is the only deduction. mapped=True wires pub-1 -> chA via a VERIFIED Adsense
    link plus an active owner->channel link, so the compute fully allocates with
    zero unallocated. mapped=False omits the links, so pub-1 resolves to no channel
    (one UnallocatedIssue). `status` seeds the finance-month close row.
    """
    engine = create_engine(database_url)
    # `tenants` (FK parent for deduction/link/run rows) lives on TenantBase, a
    # separate base; create it alongside org/security/finance and seed the parent
    # row so create_app's commit resolves the tenant FK regardless of its
    # FK-enforcement setting.
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            TenantORM(
                id=TENANT, slug="ums", display_name="UMS",
                primary_currency="USD", status="ACTIVE",
            ),
            OrgUnitORM(id=SECTOR_ID, parent_id=None, type="SECTOR", name="S", active=True),
            OrgUnitORM(
                id=COMPANY_ID, parent_id=SECTOR_ID, type="COMPANY", name="C", active=True,
            ),
            YouTubeChannelORM(
                id=CHANNEL_ROW_ID, tenant_id=TENANT, youtube_channel_id="chA",
                channel_name="A", primary_org_unit_id=COMPANY_ID,
                cms_status="INSIDE_CMS", revenue_required=True, active=True,
            ),
            MonthlyChannelRevenueFactORM(
                id=uuid4(), tenant_id=TENANT, month=MONTH, youtube_channel_id="chA",
                source_kind="ADSENSE", source_report_id=None,
                gross_revenue_usd=Decimal("1000.00"), net_revenue_usd=None,
                views=0, watch_time_minutes=Decimal("0"),
                confidence_score=Decimal("0.95"), imported_by=USER_ID,
            ),
            DeductionComponentORM(
                id=uuid4(), tenant_id=TENANT, month=MONTH, component_kind="DEDUCTION",
                scope_kind="ACCOUNT", scope_id="pub-1", amount_usd=Decimal("100.00"),
                currency_code="USD", source_system="adsense_management",
                source_table="google_revenue_source_rows", component_key="ad-1",
                raw_payload={},
            ),
            UserORM(id=USER_ID, email="commit@example.com", display_name="Commit User"),
            FinanceMonthCloseORM(
                tenant_id=TENANT, month=MONTH, status=status, allocation_rule_payload={},
            ),
        ])
        if mapped:
            session.add_all([
                AdsenseContentOwnerLinkORM(
                    id=uuid4(), tenant_id=TENANT, adsense_account_id="pub-1",
                    content_owner_id="owner-1", verification_status="VERIFIED",
                    provenance_kind="OPERATOR_ASSERTED", provenance_payload={},
                    effective_month_start="2026-01",
                ),
                ContentOwnerChannelLinkORM(
                    id=uuid4(), tenant_id=TENANT, content_owner_id="owner-1",
                    youtube_channel_id="chA", provenance_kind="SOURCE_ROW",
                    active=True, effective_month_start="2026-01",
                ),
            ])
        session.commit()


def _principal(*, revenue: bool = True, payments: bool = True, change: bool = True):
    """Global finance principal; flags drop one gate at a time for the 403 cases."""
    grants = []
    if revenue:
        grants.append(PermissionGrant(Permission.VIEW_REVENUE, AccessScope.global_scope()))
    if payments:
        grants.append(
            PermissionGrant(Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month(MONTH))
        )
    if change:
        grants.append(
            PermissionGrant(Permission.CHANGE_ALLOCATION_RULE, AccessScope.finance_month(MONTH))
        )
    return UserPrincipal(
        user_id=str(USER_ID), email="commit@example.com",
        direct_permissions=tuple(grants),
    )


def _client(database_url: str, principal_factory) -> TestClient:
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = principal_factory
    return TestClient(app)


def _committed_audit_rows(database_url: str):
    engine = create_engine(database_url)
    with Session(engine) as session:
        return [
            row
            for row in session.scalars(select(AuditLogORM)).all()
            if row.event_type == "ALLOCATION_COMMITTED"
        ]


def test_commit_creates_run_and_summary_only_audit(tmp_path):
    """First commit -> 201 with one allocation, summary-only audit, one audit row."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(COMMIT_PATH, json={"idempotency_key": "k1", "reason": "month close"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["run"]["commit_version"] == 1
    assert body["allocations"]  # one fully-allocated line
    assert body["unallocated"] == []
    assert body["audit_event"]["event_type"] == "ALLOCATION_COMMITTED"
    assert "details" not in body["audit_event"]  # API-surface audit is summary-only
    rows = _committed_audit_rows(db)
    assert len(rows) == 1
    detail = rows[0].details
    assert detail["run_id"] == body["run"]["run_id"]
    assert detail["commit_version"] == 1
    assert "allocated_total_usd" in detail
    assert "allocations" not in detail and "lines" not in detail  # no per-line dump


def test_idempotent_replay_returns_200_without_second_audit(tmp_path):
    """Re-POST with the same key + identical body -> 200, no second audit row."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    payload = {"idempotency_key": "dup", "reason": "month close"}
    first = client.post(COMMIT_PATH, json=payload)
    second = client.post(COMMIT_PATH, json=payload)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["audit_event"] is None
    assert second.json()["run"]["run_id"] == first.json()["run"]["run_id"]
    assert len(_committed_audit_rows(db)) == 1


def test_same_key_different_reason_conflicts_409(tmp_path):
    """Same key, different reason (different fingerprint) -> 409."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    client.post(COMMIT_PATH, json={"idempotency_key": "dup", "reason": "first"})
    conflict = client.post(COMMIT_PATH, json={"idempotency_key": "dup", "reason": "second"})
    assert conflict.status_code == 409


def test_locked_month_conflicts_409(tmp_path):
    """A LOCKED month rejects the commit with 409."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True, status="LOCKED")
    client = _client(db, _principal)
    resp = client.post(COMMIT_PATH, json={"idempotency_key": "k", "reason": "r"})
    assert resp.status_code == 409


def test_unallocated_month_rejected_422(tmp_path):
    """An unmapped account (one unallocated issue) rejects the commit with 422."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=False)
    client = _client(db, _principal)
    resp = client.post(COMMIT_PATH, json={"idempotency_key": "k", "reason": "r"})
    assert resp.status_code == 422


def test_malformed_month_rejected_422(tmp_path):
    """A malformed month path segment is rejected with 422 before any compute."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(
        "/revenue/months/2026-13/account-allocations/commit",
        json={"idempotency_key": "k", "reason": "r"},
    )
    assert resp.status_code == 422


def test_unsupported_method_rejected_422(tmp_path):
    """A non-gross_revenue_proportional method is rejected with 422."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(
        COMMIT_PATH,
        json={"idempotency_key": "k", "reason": "r", "allocation_method": "company_level"},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("missing", ["revenue", "payments", "change"])
def test_missing_permission_forbidden_403(tmp_path, missing):
    """Dropping any one of the three required gates yields 403."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, lambda: _principal(**{missing: False}))
    resp = client.post(COMMIT_PATH, json={"idempotency_key": "k", "reason": "r"})
    assert resp.status_code == 403


def test_net_revenue_unchanged_by_commit(tmp_path):
    """READER-UNTOUCHED REGRESSION: live net-revenue is identical (modulo the
    volatile audit_events block) before and after a commit; the snapshot drives no
    reader number.
    """
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    net_path = f"/revenue/months/{MONTH}/net-revenue?scope_type=global"

    before = client.get(net_path)
    assert before.status_code == 200
    commit = client.post(COMMIT_PATH, json={"idempotency_key": "k", "reason": "r"})
    assert commit.status_code == 201
    after = client.get(net_path)
    assert after.status_code == 200

    def _stable(payload: dict) -> dict:
        return {k: v for k, v in payload.items() if k != "audit_events"}

    assert _stable(before.json()) == _stable(after.json())
```

All nine functions above are complete and runnable against the implemented route;
`test_net_revenue_unchanged_by_commit` (the reader-untouched regression) is mandatory.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/api/test_committed_allocation_api.py -q`
Expected: FAIL (route 404 / `ALLOCATION_COMMITTED` missing).

- [ ] **Step 3: Add the `ALLOCATION_COMMITTED` audit event**

In `backend/ums_smart_revenue/auth/audit.py`, add the enum member to `AuditEventType` (next to `ALLOCATION_RULE_CHANGED`):

```python
    ALLOCATION_RULE_CHANGED = "ALLOCATION_RULE_CHANGED"
    ALLOCATION_COMMITTED = "ALLOCATION_COMMITTED"
    RECALCULATION_REQUESTED = "RECALCULATION_REQUESTED"
```

And the definition in `AUDIT_EVENT_DEFINITIONS` (mirroring `ALLOCATION_RULE_CHANGED`):

```python
    AuditEventType.ALLOCATION_COMMITTED: AuditEventDefinition(
        AuditEventType.ALLOCATION_COMMITTED,
        reason_required=True,
        permission=Permission.CHANGE_ALLOCATION_RULE,
    ),
```

- [ ] **Step 4: Add the commit endpoint + provider + fingerprint helper**

In `backend/ums_smart_revenue/api/allocation.py`: add imports (`hashlib`, `json`, `status` already imported; `BaseModel` from pydantic; `current_db_session` from `api.dependencies`; the committed-allocation repository + errors; `current_finance_month_close_repository` is not needed — the repo uses the session via `month_close` helpers; `Session` from sqlalchemy.orm), a local repository provider, a request model, the fingerprint helper, and the route.

```python
import hashlib
import json

from pydantic import BaseModel
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import current_db_session
from ums_smart_revenue.finance.committed_allocation import (
    CommittedAllocationIdempotencyConflictError,
    CommittedAllocationLockedMonthError,
    CommittedAllocationValidationError,
    SqlAlchemyCommittedAllocationRepository,
)


def current_committed_allocation_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyCommittedAllocationRepository:
    """Build the committed-allocation repository bound to the request session."""
    return SqlAlchemyCommittedAllocationRepository(session)


class CommitAllocationRequest(BaseModel):
    """Body for a commit: a client idempotency key + a required reason."""

    idempotency_key: str
    reason: str
    allocation_method: str = "gross_revenue_proportional"


class CommitAllocationResponse(BaseModel):
    """Committed run (with its summary) + the snapshot payload + audit event."""

    run: dict[str, object]
    allocations: list[dict[str, object]]
    unallocated: list[dict[str, object]]
    notes: list[dict[str, object]]
    audit_event: dict[str, object] | None


def _request_fingerprint(*, allocation_method: str, reason: str) -> str:
    """Stable digest of the client-controlled request (month is the lookup scope)."""
    canonical = json.dumps(
        {"allocation_method": allocation_method, "reason": reason}, sort_keys=True
    )
    return hashlib.blake2b(canonical.encode(), digest_size=16).hexdigest()


def _run_to_api(run) -> dict[str, object]:  # noqa: ANN001
    """Serialize a committed run header (Decimals as strings)."""
    return {
        "run_id": str(run.id),
        "month": run.month,
        "commit_version": run.commit_version,
        "allocation_method": run.allocation_method,
        "idempotency_key": run.idempotency_key,
        "committed_by": str(run.committed_by),
        "committed_at": run.committed_at.isoformat() if run.committed_at else None,
        "reason": run.reason,
        "summary": {
            "component_count": run.component_count,
            "allocated_component_count": run.allocated_component_count,
            "unallocated_component_count": run.unallocated_component_count,
            "allocated_total_usd": decimal_to_api(run.allocated_total_usd),
            "unallocated_total_usd": decimal_to_api(run.unallocated_total_usd),
            "net_applicable_total_usd": decimal_to_api(run.net_applicable_total_usd),
            "reconciliation_total_usd": decimal_to_api(run.reconciliation_total_usd),
        },
    }
```

Add the route (sibling to the GET read route):

```python
@router.post(
    "/months/{month}/account-allocations/commit",
    response_model=CommitAllocationResponse,
)
def commit_account_allocations(
    month: str,
    payload: CommitAllocationRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    committed_repository: Annotated[
        SqlAlchemyCommittedAllocationRepository,
        Depends(current_committed_allocation_repository),
    ],
    deduction_repository: Annotated[
        SqlAlchemyDeductionComponentRepository,
        Depends(current_deduction_component_repository),
    ],
    revenue_repository: Annotated[
        SqlAlchemyRevenueFactRepository, Depends(current_revenue_fact_repository)
    ],
    link_repository: Annotated[
        SqlAlchemyChannelAccountLinkRepository,
        Depends(current_channel_account_link_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
    response: Response,
) -> CommitAllocationResponse:
    """Commit a versioned snapshot of the month's account allocation."""
    _require_valid_month(month)
    revenue_scope = AccessScope.global_scope()
    payment_scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.VIEW_REVENUE, revenue_scope)
    _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, payment_scope)
    _require_permission(user, Permission.CHANGE_ALLOCATION_RULE, payment_scope)

    fingerprint = _request_fingerprint(
        allocation_method=payload.allocation_method, reason=payload.reason
    )
    try:
        outcome = committed_repository.commit_allocation(
            month=month, allocation_method=payload.allocation_method,
            idempotency_key=payload.idempotency_key, request_fingerprint=fingerprint,
            reason=payload.reason, committed_by=user.user_id,  # str; repo -> UUID
            deduction_repository=deduction_repository,
            revenue_repository=revenue_repository, link_repository=link_repository,
        )
    except CommittedAllocationValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except (
        CommittedAllocationLockedMonthError,
        CommittedAllocationIdempotencyConflictError,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    audit_event: dict[str, object] | None = None
    if outcome.created:
        audit_event = audit_record_to_api(
            record_audit_event(
                sink=audit_sink, actor=user,
                event_type=AuditEventType.ALLOCATION_COMMITTED,
                entity_type="committed_allocation_run", entity_id=str(outcome.run.id),
                scope=payment_scope, reason=payload.reason,
                details={
                    "run_id": str(outcome.run.id),
                    "commit_version": outcome.run.commit_version,
                    "month": month,
                    "allocation_method": outcome.run.allocation_method,
                    "component_count": outcome.run.component_count,
                    "allocated_component_count": outcome.run.allocated_component_count,
                    "unallocated_component_count": outcome.run.unallocated_component_count,
                    "allocated_total_usd": decimal_to_api(outcome.run.allocated_total_usd),
                    "net_applicable_total_usd": decimal_to_api(
                        outcome.run.net_applicable_total_usd
                    ),
                    "note_count": len(outcome.notes),
                },
            )
        )
        response.status_code = status.HTTP_201_CREATED

    return CommitAllocationResponse(
        run=_run_to_api(outcome.run),
        allocations=[
            {
                "adsense_account_id": ln.adsense_account_id,
                "youtube_channel_id": ln.youtube_channel_id,
                "component_kind": ln.component_kind, "source_system": ln.source_system,
                "component_key": ln.component_key, "basis_source_kind": ln.basis_source_kind,
                "basis_gross_usd": decimal_to_api(ln.basis_gross_usd),
                "basis_share": decimal_to_api(ln.basis_share),
                "allocated_amount_usd": decimal_to_api(ln.allocated_amount_usd),
                "net_applicable": ln.net_applicable,
            }
            for ln in outcome.lines
        ],
        unallocated=[
            {
                "scope_id": iss.scope_id, "component_kind": iss.component_kind,
                "component_key": iss.component_key,
                "amount_usd": decimal_to_api(iss.amount_usd),
                "issue_code": iss.issue_code, "detail": iss.detail,
            }
            for iss in outcome.unallocated
        ],
        notes=[
            {"note_code": n.note_code, "youtube_channel_id": n.youtube_channel_id,
             "detail": n.detail}
            for n in outcome.notes
        ],
        audit_event=audit_event,
    )
```

Add `Response` to the `fastapi` import line (`from fastapi import APIRouter, Depends, HTTPException, Query, Response, status`). Default response status is 200 (replay); a fresh commit sets 201 explicitly. No `app.py` change is needed — the route is on the already-mounted `allocation_router`.

- [ ] **Step 5: Run to verify it passes**

Run: `python -m pytest tests/api/test_committed_allocation_api.py -q`
Expected: PASS (all). Then `python -m ruff check backend tests` → clean.

- [ ] **Step 6: Commit**

```bash
git add backend/ums_smart_revenue/auth/audit.py backend/ums_smart_revenue/api/allocation.py tests/api/test_committed_allocation_api.py
git commit -m "feat(api): commit-account-allocations endpoint + ALLOCATION_COMMITTED audit"
```

---

## Task 4: Docs status + full validation gate

**Files:** `Docs/01_IMPLEMENTATION_PLAN.md`, `Docs/15_DELIVERY_BACKLOG.md`

- [ ] **Step 1: Update `Docs/15_DELIVERY_BACKLOG.md`**

In the Spec 2b allocation-engine entry, update the `Remaining:` line. Change:

```
  Remaining: PAYMENT-grain (needs a
  payment→account hop); persisted/committed allocation; other allocation methods.
```

to:

```
  PR-5 shipped (this branch): persisted/committed allocation — a versioned, audited
  POST /revenue/months/{month}/account-allocations/commit writes a snapshot of the
  gross_revenue_proportional compute (4 new tables; month-scoped idempotency;
  lock-held compute; reject-on-unallocated; CHANGE_ALLOCATION_RULE + ALLOCATION_COMMITTED).
  Readers still compute live (read-switch deferred).
  Remaining: read-switch to committed snapshots; PAYMENT-grain (needs a
  payment→account hop); other allocation methods.
```

(If the live `Remaining:` wording differs, edit it to the same effect — mark persisted/committed shipped, add "read-switch to committed snapshots" as remaining, keep PAYMENT-grain + other-methods.)

- [ ] **Step 2: Update `Docs/01_IMPLEMENTATION_PLAN.md`**

In the Spec 2b allocation-rules entry, update its `Remaining:` line similarly:

```
  PR-5 SHIPPED (this branch): persisted/committed allocation — write-only versioned
  snapshot endpoint + 4 tables + ALLOCATION_COMMITTED audit; readers unchanged.
  Remaining: read-switch to committed snapshots; PAYMENT-grain; other methods.
```

(Edit the live line to the same effect if its wording differs.)

- [ ] **Step 3: Verify whitespace and commit**

Run: `git diff --check` → no output.

```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "docs(plan): mark Spec 2b persisted/committed allocation shipped"
```

- [ ] **Step 4: Full validation gate**

```
python -m ruff check backend tests scripts
$env:UMS_TEST_DATABASE_URL='postgresql+psycopg://postgres:ums@localhost:55432/test_ums'
python -m pytest tests/db/test_committed_allocation_models.py tests/db/test_committed_allocation_migration_postgres.py tests/finance/test_committed_allocation.py tests/api/test_committed_allocation_api.py tests/api/test_allocation_api.py tests/api/test_net_revenue_api.py -q
python -m pytest -q
git diff --check
```
Expected: ruff clean; targeted set green; full suite green (PG container running); `git diff --check` no output. The migration tests must actually run (they RAISE if `UMS_TEST_DATABASE_URL` is unset — never skip).

---

## Self-Review

**1. Spec coverage**

| Spec section | Task |
|---|---|
| §6 four tables + FK/index/CHECK layout | Task 1 (ORM + migration) |
| §6 PG-only finite CHECKs via ddl_if / dialect guard | Task 1 |
| §7 month-scoped idempotency + versioning + fingerprint{method,reason} | Task 2 (repo) + Task 1 (unique) + Task 3 (fingerprint helper) |
| §7 lock held across idempotency→open→method→compute→unallocated→insert | Task 2 |
| §8 validation order + 201/200/422/409 response shapes | Task 3 (route) |
| §8 method-before-compute, reject-on-unallocated-after | Task 2 + Task 3 |
| §9 read gates + CHANGE_ALLOCATION_RULE + ALLOCATION_COMMITTED summary-only | Task 3 |
| §10 repository shared request session, no repo commit, `created` flag | Task 2 |
| §2 readers untouched + reader-untouched regression | Task 3 (regression test) |
| §11 PG migration tests + SQLite model tests + repo + API tests | Tasks 1–3 |
| §13 additive migration, downgrade drops 4 tables | Task 1 |
| Docs/01 + Docs/15 | Task 4 |

No gaps.

**2. Placeholder scan:** No placeholders remain. Task 2's seeding is a single executable `_seed_account_deduction(session, *, mapped, status)` helper (field-for-field the verified `_seed_missing_net_with_components` shape from `tests/api/test_exports_account_allocation.py`, reduced to one ACCOUNT component over chA: 1000 gross, 100 deduction), with `mapped=False` exercising the unallocated path and `status="LOCKED"` the locked path — no `NotImplementedError`, no "build as the cited module does" hand-waving. Task 3's API tests are nine complete, runnable functions (own `build_database_url` / `_seed` / `_principal` / `_client` helpers, modeled on the verified `tests/api/test_net_revenue_api.py` idioms — `create_app(database_url=...)` + `dependency_overrides[current_principal_from_headers]`), covering 201 + summary-only audit, 200 replay, 409 locked, 409 conflict, 422 unallocated/malformed/unsupported, parametrized 403 per gate, and the reader-untouched regression. Every production code block (ORM, migration, repository, audit, endpoint) is complete. The Task 4 doc edits include a fallback-wording note (a guard, not a placeholder).

**3. Type consistency:** `CommitAllocationOutcome(run, lines, unallocated, notes, created)` is produced by `commit_allocation` (Task 2) and consumed by the route (Task 3). `commit_allocation(...)` keyword args match between the repo, the repo tests, and the route call. ORM class names (`CommittedAllocationRunORM`/`LineORM`/`UnallocatedORM`/`NoteORM`) are identical across the models, migration FK targets, repository, and tests. `request_fingerprint` over `{allocation_method, reason}` matches §7. Typed errors map to the documented status codes (Validation→422, Locked/IdempotencyConflict→409).

**4. SQLite FK enforcement (verified by a live repro):** All three FK-on SQLite helpers (Task 1 `_engine`, Task 2 `_session`, Task 3 `_seed`) create `TenantBase.metadata` and insert a `TenantORM(id=TENANT, slug="ums", display_name="UMS", primary_currency="USD", status="ACTIVE")` parent row before any tenant-scoped row. This is required because `tenant_id`→`tenants.id` FKs exist on `DeductionComponentORM`, `AdsenseContentOwnerLinkORM`, `ContentOwnerChannelLinkORM`, and `CommittedAllocationRunORM`, and `tenants` lives on `TenantBase` — a base whose metadata neither `OrgBase`/`FinanceBase` (which share metadata) nor `SecurityBase` creates. Under `PRAGMA foreign_keys=ON`, omitting it raises `OperationalError: no such table: main.tenants` (Task 1/2) and the route's run insert would fail. Additionally, Task 2's `_seed_account_deduction` flushes the channel before adding the dependent fact: `monthly_channel_revenue_facts`'s composite FK `(tenant_id, youtube_channel_id)→youtube_channels` crosses the Org/Finance registries, so the unit-of-work does not order the channel insert first on its own. These bugs surface only here because these are the first seeds to enable FK enforcement (the existing `test_exports_account_allocation.py` seed runs FK-off, so SQLite silently ignores the FKs).
