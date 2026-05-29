# Deduction-Evidence Substrate + Ingestion — PR-A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `deduction_components` substrate + three source-reported ingestion adapters + an operator CLI + a new sensitive audit event, so Phase 4 has typed, labeled, idempotent deduction evidence to build on.

**Architecture:** A new `deduction_components` table (`FinanceBase`) holds typed, source-labeled evidence rows. A pure module (`finance/deduction_components.py`) maps existing source-of-truth rows (Google source rows, bank entries, AdSense earnings vs payments) into `DeductionComponentInput`s. A repository/service module (`finance/deduction_ingestion.py`) idempotently upserts them under the month-lock gate and records one `DEDUCTION_COMPONENTS_INGESTED` audit event. An operator CLI (`scripts/run_deduction_ingestion.py`) drives it.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, Alembic, pytest, SQLite (unit) + PostgreSQL (migration round-trip), ruff.

**Spec:** `Docs/superpowers/specs/2026-05-29-spec-deduction-components-design.md` (this plan = **PR-A**; net_revenue wiring + read endpoint are **PR-B**, out of scope here).

**Scope guards (do NOT do in PR-A):** no `net_revenue` change, no read endpoint, no allocation, no committed `/recalculate`, no manual deduction entry, no parser change to *emit* tax/deduction rows, no currency conversion (USD-only; non-USD skipped + counted).

---

## File Structure

- **Modify** `backend/ums_smart_revenue/auth/audit.py` — add `AuditEventType.DEDUCTION_COMPONENTS_INGESTED` + its `AUDIT_EVENT_DEFINITIONS` entry (`permission=Permission.RUN_CONNECTOR_JOBS`, which is `sensitive=True`).
- **Modify** `backend/ums_smart_revenue/db/finance_models.py` — add `DeductionComponentORM`.
- **Create** `backend/ums_smart_revenue/db/alembic/versions/20260529_0002_deduction_components.py` — create-table migration (`down_revision = "20260529_0001"`), with Postgres-only finite/object CHECKs.
- **Create** `backend/ums_smart_revenue/finance/deduction_components.py` — pure: `DeductionComponentInput`, `DeductionComponent`, the three mappers, USD-only handling. No DB/I/O.
- **Create** `backend/ums_smart_revenue/finance/deduction_ingestion.py` — typed errors, `SqlAlchemyDeductionComponentRepository` (idempotent upsert by `component_key`, `list_month_components`, month-lock gate), `DeductionIngestionService`, `DeductionIngestionResult`.
- **Create** `scripts/run_deduction_ingestion.py` — operator CLI (service principal, exit codes).
- **Create** tests: `tests/finance/test_deduction_components.py`, `tests/finance/test_deduction_ingestion.py`, `tests/db/test_deduction_components_migration_postgres.py`, `tests/scripts/test_run_deduction_ingestion_cli.py`, and an audit-registry assertion in `tests/auth/test_deduction_audit_event.py`.

**Conventions (do not deviate):** finance pure tests import the module-under-test via `importlib.import_module(...)` inside a helper (collection precedes implementation). Repos use `ON CONFLICT DO UPDATE` via the `_dialect_insert` helper. Postgres-only DDL uses `.ddl_if(dialect="postgresql")` on the ORM and `if op.get_bind().dialect.name == "postgresql":` in the migration. USD-only: skip + count non-USD; never convert.

---

## Task 1: New sensitive audit event `DEDUCTION_COMPONENTS_INGESTED`

**Files:**
- Modify: `backend/ums_smart_revenue/auth/audit.py`
- Test: `tests/auth/test_deduction_audit_event.py`

- [ ] **Step 1: Write the failing test**

Create `tests/auth/test_deduction_audit_event.py`:

```python
"""The deduction ingestion audit event must be registered and sensitive."""
from ums_smart_revenue.auth.audit import (
    AUDIT_EVENT_DEFINITIONS,
    AuditEventType,
)
from ums_smart_revenue.auth.permissions import SENSITIVE_PERMISSIONS


def test_deduction_components_ingested_event_exists():
    assert AuditEventType.DEDUCTION_COMPONENTS_INGESTED.value == "DEDUCTION_COMPONENTS_INGESTED"


def test_deduction_components_ingested_is_sensitive_via_run_connector_jobs():
    definition = AUDIT_EVENT_DEFINITIONS[AuditEventType.DEDUCTION_COMPONENTS_INGESTED]
    assert definition.permission is not None
    assert definition.permission in SENSITIVE_PERMISSIONS
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/auth/test_deduction_audit_event.py -q`
Expected: FAIL — `AttributeError: DEDUCTION_COMPONENTS_INGESTED` (enum member missing).

- [ ] **Step 3: Add the enum member**

In `backend/ums_smart_revenue/auth/audit.py`, inside `class AuditEventType(StrEnum):`, add this line immediately after `ADSENSE_PAYMENT_SYNCED = "ADSENSE_PAYMENT_SYNCED"`:

```python
    DEDUCTION_COMPONENTS_INGESTED = "DEDUCTION_COMPONENTS_INGESTED"
```

- [ ] **Step 4: Add the registry entry**

In the `AUDIT_EVENT_DEFINITIONS` dict, immediately after the `AuditEventType.ADSENSE_PAYMENT_SYNCED` entry, add:

```python
    AuditEventType.DEDUCTION_COMPONENTS_INGESTED: AuditEventDefinition(
        AuditEventType.DEDUCTION_COMPONENTS_INGESTED,
        permission=Permission.RUN_CONNECTOR_JOBS,
    ),
```

(`Permission.RUN_CONNECTOR_JOBS` is declared `sensitive=True` in `permissions.py`, so `record_audit_event` marks this event `sensitive=True` automatically — no extra wiring. `reason_required` stays default `False`, matching `ADSENSE_PAYMENT_SYNCED` / `REPORT_IMPORTED`.)

- [ ] **Step 5: Run it to verify it passes**

Run: `python -m pytest tests/auth/test_deduction_audit_event.py -q`
Expected: PASS — 2 passed.

- [ ] **Step 6: Lint + commit**

```bash
python -m ruff check backend/ums_smart_revenue/auth/audit.py tests/auth/test_deduction_audit_event.py
git add backend/ums_smart_revenue/auth/audit.py tests/auth/test_deduction_audit_event.py
git commit -m "feat(audit): add sensitive DEDUCTION_COMPONENTS_INGESTED event"
```
End the commit message with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## Task 2: `deduction_components` table + migration + Postgres round-trip

**Files:**
- Modify: `backend/ums_smart_revenue/db/finance_models.py`
- Create: `backend/ums_smart_revenue/db/alembic/versions/20260529_0002_deduction_components.py`
- Test: `tests/db/test_deduction_components_migration_postgres.py`

- [ ] **Step 1: Add the ORM model**

In `backend/ums_smart_revenue/db/finance_models.py`, append this class at the end of the file (it uses already-imported symbols: `CheckConstraint`, `Index`, `Numeric`, `Text`, `UniqueConstraint`, `Uuid`, `DateTime`, `func`, `text`, `postgresql`, `mapped_column`, `Mapped`, `_TENANT_ID_DEFAULT`, `_TENANT_ID_DEFAULT_VALUE`):

```python
class DeductionComponentORM(FinanceBase):
    """Source-reported, strictly-labeled deduction-evidence component."""

    # ========================================================================
    # Purpose: One typed, source-labeled deduction-evidence row (TAX, DEDUCTION,
    #   TRANSFER_FEE, signed FX_VARIANCE, or UNRESOLVED_PAYMENT_GAP) at CHANNEL,
    #   ACCOUNT, or PAYMENT scope. Substrate only — no allocation, no net math.
    # Database/ORM: deduction_components (FinanceBase). Tenant-scoped; idempotent
    #   on (tenant_id, component_key).
    # Standards: signed finite amounts (Postgres NaN-guarded via ddl_if);
    #   object-only NOT NULL raw_payload; USD-only amounts (non-USD skipped at
    #   ingestion, never converted).
    # Blast Radius: Finance source-of-truth (new table; additive). No auth/Neo4j.
    # Connections:
    #   - File: backend/ums_smart_revenue/finance/deduction_ingestion.py -> writer.
    #   - File: Docs/superpowers/specs/2026-05-29-spec-deduction-components-design.md
    # ========================================================================
    __tablename__ = "deduction_components"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE,
        server_default=_TENANT_ID_DEFAULT,
    )
    month: Mapped[str] = mapped_column(Text, nullable=False)
    component_kind: Mapped[str] = mapped_column(Text, nullable=False)
    scope_kind: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str] = mapped_column(Text, nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    amount_native: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    currency_code: Mapped[str] = mapped_column(Text, nullable=False)
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    source_table: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_report_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=False,
        server_default=text("'{}'"),
    )
    component_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "component_key", name="uq_deduction_components_key"
        ),
        CheckConstraint(
            "length(month) = 7 AND substr(month, 5, 1) = '-' "
            "AND substr(month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_deduction_components_month_format",
        ),
        CheckConstraint(
            "component_kind IN ('TAX', 'DEDUCTION', 'TRANSFER_FEE', "
            "'FX_VARIANCE', 'UNRESOLVED_PAYMENT_GAP')",
            name="ck_deduction_components_kind",
        ),
        CheckConstraint(
            "scope_kind IN ('CHANNEL', 'ACCOUNT', 'PAYMENT')",
            name="ck_deduction_components_scope_kind",
        ),
        CheckConstraint(
            "length(currency_code) = 3 "
            "AND currency_code = upper(currency_code) "
            "AND substr(currency_code, 1, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(currency_code, 2, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(currency_code, 3, 1) BETWEEN 'A' AND 'Z'",
            name="ck_deduction_components_currency_code",
        ),
        CheckConstraint(
            "length(scope_id) >= 1", name="ck_deduction_components_scope_id_nonempty"
        ),
        CheckConstraint(
            "length(component_key) >= 1",
            name="ck_deduction_components_component_key_nonempty",
        ),
        # Postgres NUMERIC can store NaN (and `>= 0` would admit it); these
        # finite bounds reject NaN + ±Infinity for the signed amounts. Postgres-
        # only: the 'Infinity'::numeric cast is invalid SQLite (create_all path).
        CheckConstraint(
            "amount_usd > '-Infinity'::numeric AND amount_usd < 'Infinity'::numeric",
            name="ck_deduction_components_amount_usd_finite",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "amount_native IS NULL OR (amount_native > '-Infinity'::numeric "
            "AND amount_native < 'Infinity'::numeric)",
            name="ck_deduction_components_amount_native_finite",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "jsonb_typeof(raw_payload) = 'object'",
            name="ck_deduction_components_raw_payload_object",
        ).ddl_if(dialect="postgresql"),
        Index("ix_deduction_components_tenant_month", "tenant_id", "month"),
        Index(
            "ix_deduction_components_tenant_scope",
            "tenant_id", "scope_kind", "scope_id",
        ),
        Index(
            "ix_deduction_components_tenant_month_kind",
            "tenant_id", "month", "component_kind",
        ),
    )
```

- [ ] **Step 2: Write the failing Postgres migration round-trip test**

Create `tests/db/test_deduction_components_migration_postgres.py`:

```python
"""PostgreSQL round-trip for 20260529_0002 (deduction_components).

upgrade 20260529_0001 -> 20260529_0002 (create deduction_components) and the
reverse downgrade. Verifies the live Postgres schema matches DeductionComponentORM.
"""
from pathlib import Path

import pytest
from _postgres_helpers import require_postgres_url
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def postgres_url() -> str:
    return require_postgres_url()


@pytest.fixture
def alembic_config(postgres_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", postgres_url)
    cfg.set_main_option(
        "script_location",
        str(REPO_ROOT / "backend" / "ums_smart_revenue" / "db" / "alembic"),
    )
    return cfg


@pytest.fixture
def fresh_engine(postgres_url: str):
    engine = create_engine(postgres_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


def test_upgrade_creates_table_constraints_and_indexes(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    inspector = inspect(fresh_engine)
    cols = {c["name"]: c for c in inspector.get_columns("deduction_components")}
    assert cols["amount_usd"]["nullable"] is False
    assert cols["amount_native"]["nullable"] is True
    assert cols["raw_payload"]["nullable"] is False
    uniques = {
        c["name"]: tuple(c["column_names"])
        for c in inspector.get_unique_constraints("deduction_components")
    }
    assert uniques["uq_deduction_components_key"] == ("tenant_id", "component_key")
    checks = {c["name"] for c in inspector.get_check_constraints("deduction_components")}
    assert "ck_deduction_components_kind" in checks
    assert "ck_deduction_components_scope_kind" in checks
    assert "ck_deduction_components_amount_usd_finite" in checks
    assert "ck_deduction_components_amount_native_finite" in checks
    assert "ck_deduction_components_raw_payload_object" in checks
    indexes = {c["name"] for c in inspector.get_indexes("deduction_components")}
    assert "ix_deduction_components_tenant_month" in indexes
    assert "ix_deduction_components_tenant_scope" in indexes
    assert "ix_deduction_components_tenant_month_kind" in indexes


def test_downgrade_drops_table(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "20260529_0001")
    inspector = inspect(fresh_engine)
    assert "deduction_components" not in inspector.get_table_names()


def test_round_trip_idempotency(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "20260529_0001")
    command.upgrade(alembic_config, "head")
    inspector = inspect(fresh_engine)
    assert "deduction_components" in inspector.get_table_names()


def test_duplicate_component_key_rejected_by_unique(alembic_config, fresh_engine):
    """Behavioral: the (tenant_id, component_key) idempotency contract is enforced."""
    from sqlalchemy.exc import IntegrityError

    command.upgrade(alembic_config, "head")
    insert_sql = text(
        "INSERT INTO deduction_components "
        "(tenant_id, month, component_kind, scope_kind, scope_id, amount_usd, "
        "currency_code, source_system, source_table, component_key) VALUES "
        "(:tenant, '2026-04', 'TRANSFER_FEE', 'PAYMENT', 'BANK-1', 3.50, 'USD', "
        "'bank_reconciliation', 'bank_reconciliation_entries', "
        "'bank:2026-04:BANK-1:transfer_fee')"
    )
    tenant = "00000000-0000-0000-0000-0000000000aa"
    with fresh_engine.begin() as conn:
        conn.execute(insert_sql, {"tenant": tenant})
    with pytest.raises(IntegrityError):
        with fresh_engine.begin() as conn:
            conn.execute(insert_sql, {"tenant": tenant})
```

- [ ] **Step 3: Run it to verify it fails**

Run (Postgres must be available — see Step 6): `set UMS_TEST_DATABASE_URL=... ; python -m pytest tests/db/test_deduction_components_migration_postgres.py -q`
Expected: FAIL — `alembic.util.exc.CommandError` / missing revision (the `20260529_0002` migration does not exist yet), or `KeyError: 'deduction_components'`.

- [ ] **Step 4: Write the migration**

Create `backend/ums_smart_revenue/db/alembic/versions/20260529_0002_deduction_components.py`:

```python
"""Create deduction_components (source-reported deduction-evidence substrate).

Revision ID: 20260529_0002
Revises: 20260529_0001
Create Date: 2026-05-29

Spec: Docs/superpowers/specs/2026-05-29-spec-deduction-components-design.md (PR-A)
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260529_0002"
down_revision = "20260529_0001"
branch_labels = None
depends_on = None


# ============================================================================
# Purpose: Create the deduction_components substrate table that ingestion
#   populates with typed, source-labeled deduction evidence.
# Database/ORM: deduction_components / DeductionComponentORM (finance_models.py).
# Standards: idempotent on (tenant_id, component_key); finite NUMERIC + object
#   JSONB guards are Postgres-only (added via dialect check), mirroring
#   google_revenue_source_rows. Downgrade drops indexes then the table.
# Blast Radius: Finance source-of-truth (additive). PostgreSQL is source of
#   truth; no auth/audit/Neo4j schema impact.
# Connections:
#   - File: backend/ums_smart_revenue/db/finance_models.py -> ORM contract.
# ============================================================================
def upgrade() -> None:
    """Create deduction_components with constraints and indexes."""
    op.create_table(
        "deduction_components",
        sa.Column(
            "id", sa.Uuid(), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("month", sa.Text(), nullable=False),
        sa.Column("component_kind", sa.Text(), nullable=False),
        sa.Column("scope_kind", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("amount_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("amount_native", sa.Numeric(20, 6), nullable=True),
        sa.Column("currency_code", sa.Text(), nullable=False),
        sa.Column("source_system", sa.Text(), nullable=False),
        sa.Column("source_table", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column("source_key", sa.Text(), nullable=True),
        sa.Column("source_report_id", sa.Text(), nullable=True),
        sa.Column(
            "raw_payload",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("component_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "component_key", name="uq_deduction_components_key"
        ),
        sa.CheckConstraint(
            "length(month) = 7 AND substr(month, 5, 1) = '-' "
            "AND substr(month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_deduction_components_month_format",
        ),
        sa.CheckConstraint(
            "component_kind IN ('TAX', 'DEDUCTION', 'TRANSFER_FEE', "
            "'FX_VARIANCE', 'UNRESOLVED_PAYMENT_GAP')",
            name="ck_deduction_components_kind",
        ),
        sa.CheckConstraint(
            "scope_kind IN ('CHANNEL', 'ACCOUNT', 'PAYMENT')",
            name="ck_deduction_components_scope_kind",
        ),
        sa.CheckConstraint(
            "length(currency_code) = 3 "
            "AND currency_code = upper(currency_code) "
            "AND substr(currency_code, 1, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(currency_code, 2, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(currency_code, 3, 1) BETWEEN 'A' AND 'Z'",
            name="ck_deduction_components_currency_code",
        ),
        sa.CheckConstraint(
            "length(scope_id) >= 1", name="ck_deduction_components_scope_id_nonempty"
        ),
        sa.CheckConstraint(
            "length(component_key) >= 1",
            name="ck_deduction_components_component_key_nonempty",
        ),
    )
    # Postgres-only guards (invalid SQLite CREATE TABLE syntax), mirroring the
    # ORM's .ddl_if(dialect="postgresql") CHECKs in finance_models.py.
    if op.get_bind().dialect.name == "postgresql":
        op.create_check_constraint(
            "ck_deduction_components_amount_usd_finite",
            "deduction_components",
            "amount_usd > '-Infinity'::numeric AND amount_usd < 'Infinity'::numeric",
        )
        op.create_check_constraint(
            "ck_deduction_components_amount_native_finite",
            "deduction_components",
            "amount_native IS NULL OR (amount_native > '-Infinity'::numeric "
            "AND amount_native < 'Infinity'::numeric)",
        )
        op.create_check_constraint(
            "ck_deduction_components_raw_payload_object",
            "deduction_components",
            "jsonb_typeof(raw_payload) = 'object'",
        )
    op.create_index(
        "ix_deduction_components_tenant_month",
        "deduction_components", ["tenant_id", "month"],
    )
    op.create_index(
        "ix_deduction_components_tenant_scope",
        "deduction_components", ["tenant_id", "scope_kind", "scope_id"],
    )
    op.create_index(
        "ix_deduction_components_tenant_month_kind",
        "deduction_components", ["tenant_id", "month", "component_kind"],
    )


def downgrade() -> None:
    """Drop deduction_components and its indexes."""
    op.drop_index(
        "ix_deduction_components_tenant_month_kind",
        table_name="deduction_components",
    )
    op.drop_index(
        "ix_deduction_components_tenant_scope", table_name="deduction_components"
    )
    op.drop_index(
        "ix_deduction_components_tenant_month", table_name="deduction_components"
    )
    op.drop_table("deduction_components")
```

- [ ] **Step 5: Run the round-trip test to verify it passes** (Postgres available)

Run: `python -m pytest tests/db/test_deduction_components_migration_postgres.py -q`
Expected: PASS — 4 passed.

- [ ] **Step 6: If no local Postgres, start one, then re-run Step 5**

```bash
docker run --rm -d --name ums-mig-pg -p 55432:5432 -e POSTGRES_PASSWORD=ums postgres:18-alpine
# PowerShell: $env:UMS_TEST_DATABASE_URL = 'postgresql+psycopg://postgres:ums@localhost:55432/postgres'
# POSIX:      export UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:ums@localhost:55432/postgres
python -m pytest tests/db/test_deduction_components_migration_postgres.py -q
```

- [ ] **Step 7: Sanity-check the ORM builds on SQLite (no Postgres needed)**

Run: `PYTHONPATH=backend python -c "from ums_smart_revenue.db.finance_models import DeductionComponentORM; print(DeductionComponentORM.__tablename__)"`
Expected: prints `deduction_components` (`PYTHONPATH=backend` is required from the repo root — `ums_smart_revenue` lives under `backend/`; pytest injects this via `pyproject.toml` `pythonpath`, but a bare `python -c` does not). `.ddl_if` keeps the Postgres-only CHECKs off the SQLite path.

- [ ] **Step 8: Lint + commit**

```bash
python -m ruff check backend/ums_smart_revenue/db/finance_models.py backend/ums_smart_revenue/db/alembic/versions/20260529_0002_deduction_components.py tests/db/test_deduction_components_migration_postgres.py
git add backend/ums_smart_revenue/db/finance_models.py backend/ums_smart_revenue/db/alembic/versions/20260529_0002_deduction_components.py tests/db/test_deduction_components_migration_postgres.py
git commit -m "feat(db): deduction_components table + migration"
```
End the commit message with the `Co-Authored-By` trailer.

---

## Task 3: Pure mapping module `finance/deduction_components.py`

**Files:**
- Create: `backend/ums_smart_revenue/finance/deduction_components.py`
- Test: `tests/finance/test_deduction_components.py`

The three mappers are PURE (no DB). Each returns `(list[DeductionComponentInput], skipped_non_usd: int)`. The ingestion service (Task 4) reads source rows from existing repositories and feeds these mappers.

- [ ] **Step 1: Write the failing tests**

Create `tests/finance/test_deduction_components.py`:

```python
"""Tests for the pure deduction-component mappers."""
from datetime import date, datetime
from decimal import Decimal
from importlib import import_module

from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    GoogleRevenueSourceRowEntry,
)
from ums_smart_revenue.finance.adsense_payments import AdSensePaymentEntry
from ums_smart_revenue.finance.bank_reconciliation import BankReconciliationEntry

MONTH = "2026-04"


def _mod():
    return import_module("ums_smart_revenue.finance.deduction_components")


def source_row(*, value_kind, amount, currency="USD", account="pub-1",
               channel=None, system="adsense_management", key=None):
    """Build a GoogleRevenueSourceRowEntry for tests."""
    return GoogleRevenueSourceRowEntry(
        id=f"row-{key or value_kind}-{amount}",
        tenant_id="t",
        source_system=system,
        source_row_key=(key or f"{value_kind}-{amount}").ljust(64, "0")[:64],
        source_account_id=account,
        content_owner_id=None,
        youtube_channel_id=channel,
        report_type="report",
        report_month=MONTH,
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
        metric_key="m",
        value_kind=value_kind,
        amount_native=Decimal(amount),
        currency_code=currency,
        source_report_id="r1",
        raw_file_id=None,
        raw_payload={"k": "v"},
        imported_by=None,
        ingested_at=datetime(2026, 5, 1, 12, 0, 0),
    )


def bank_entry(*, reference, fee="0.00", fx="0.00"):
    """Build a BankReconciliationEntry for tests."""
    return BankReconciliationEntry(
        id=f"bank-{reference}",
        month=MONTH,
        bank_reference=reference,
        bank_received_date=date(2026, 4, 20),
        bank_received_amount=Decimal("1000.00"),
        bank_received_currency="USD",
        bank_received_amount_usd=Decimal("1000.00"),
        transfer_fee_usd=Decimal(fee),
        fx_difference_usd=Decimal(fx),
        notes=None,
        source_report_id=None,
        recorded_by="user",
    )


def payment(*, account, amount, status="PAID", currency="USD", name="p"):
    """Build an AdSensePaymentEntry for tests."""
    return AdSensePaymentEntry(
        id=f"pay-{account}-{name}",
        source_account_id=account,
        month=MONTH,
        payment_name=name,
        payment_date=date(2026, 5, 21),
        payment_amount=Decimal(amount),
        payment_currency=currency,
        payment_status=status,
        raw_payload={},
        source_report_id=None,
        imported_by=None,
    )


# ---- value_kind tax/deduction consumer ----

def test_source_rows_channel_scoped_tax_becomes_channel_component():
    components, skipped = _mod().map_source_rows_to_components(
        [source_row(value_kind="tax", amount="12.00", channel="chan-1")]
    )
    assert skipped == 0
    assert len(components) == 1
    c = components[0]
    assert (c.component_kind, c.scope_kind, c.scope_id) == ("TAX", "CHANNEL", "chan-1")
    assert c.amount_usd == Decimal("12.00")
    assert c.source_system == "adsense_management"
    assert c.component_key == f"srcrow:adsense_management:{components[0].source_key}"


def test_source_rows_account_scoped_deduction_when_no_channel():
    components, _ = _mod().map_source_rows_to_components(
        [source_row(value_kind="deduction", amount="5.00", channel=None, account="pub-9")]
    )
    assert (components[0].component_kind, components[0].scope_kind, components[0].scope_id) == (
        "DEDUCTION", "ACCOUNT", "pub-9",
    )


def test_source_rows_ignores_non_tax_deduction_value_kinds():
    components, skipped = _mod().map_source_rows_to_components(
        [source_row(value_kind="settled", amount="100.00"),
         source_row(value_kind="estimated", amount="50.00")]
    )
    assert components == []
    assert skipped == 0


def test_source_rows_skips_non_usd_and_counts_it():
    components, skipped = _mod().map_source_rows_to_components(
        [source_row(value_kind="tax", amount="9.00", currency="EUR")]
    )
    assert components == []
    assert skipped == 1


# ---- bank fee / FX ----

def test_bank_transfer_fee_is_payment_scoped_deduction():
    components, skipped = _mod().map_bank_entries_to_components(
        [bank_entry(reference="BANK-1", fee="3.50", fx="0.00")], month=MONTH
    )
    assert skipped == 0
    assert len(components) == 1
    c = components[0]
    assert (c.component_kind, c.scope_kind, c.scope_id) == ("TRANSFER_FEE", "PAYMENT", "BANK-1")
    assert c.amount_usd == Decimal("3.50")
    assert c.component_key == f"bank:{MONTH}:BANK-1:transfer_fee"


def test_bank_fx_variance_is_signed_and_not_a_fee():
    components, _ = _mod().map_bank_entries_to_components(
        [bank_entry(reference="BANK-2", fee="0.00", fx="-7.25")], month=MONTH
    )
    assert len(components) == 1
    c = components[0]
    assert c.component_kind == "FX_VARIANCE"
    assert c.amount_usd == Decimal("-7.25")
    assert c.component_key == f"bank:{MONTH}:BANK-2:fx_variance"


def test_bank_zero_fee_and_zero_fx_produce_nothing():
    components, _ = _mod().map_bank_entries_to_components(
        [bank_entry(reference="BANK-3", fee="0.00", fx="0.00")], month=MONTH
    )
    assert components == []


def test_bank_entry_with_both_fee_and_fx_emits_two_components():
    components, _ = _mod().map_bank_entries_to_components(
        [bank_entry(reference="BANK-4", fee="2.00", fx="1.00")], month=MONTH
    )
    assert {c.component_kind for c in components} == {"TRANSFER_FEE", "FX_VARIANCE"}


# ---- AdSense earnings -> payment gap ----

def test_gap_emitted_only_when_settled_and_paid_both_present_and_differ():
    components, skipped = _mod().map_adsense_gap_to_components(
        month=MONTH,
        source_rows=[source_row(value_kind="settled", amount="1000.00", account="pub-1")],
        payments=[payment(account="pub-1", amount="930.00")],
    )
    assert skipped == 0
    assert len(components) == 1
    c = components[0]
    assert (c.component_kind, c.scope_kind, c.scope_id) == (
        "UNRESOLVED_PAYMENT_GAP", "ACCOUNT", "pub-1",
    )
    assert c.amount_usd == Decimal("70.00")  # settled 1000 - paid 930
    assert c.source_system == "adsense_payment_gap"
    assert c.component_key == f"adsense_gap:pub-1:{MONTH}"


def test_gap_signed_when_paid_exceeds_settled():
    components, _ = _mod().map_adsense_gap_to_components(
        month=MONTH,
        source_rows=[source_row(value_kind="settled", amount="900.00", account="pub-1")],
        payments=[payment(account="pub-1", amount="950.00")],
    )
    assert components[0].amount_usd == Decimal("-50.00")


def test_gap_skipped_when_no_settled_rows():
    components, _ = _mod().map_adsense_gap_to_components(
        month=MONTH,
        source_rows=[source_row(value_kind="estimated", amount="1000.00", account="pub-1")],
        payments=[payment(account="pub-1", amount="930.00")],
    )
    assert components == []


def test_gap_skipped_when_no_paid_payment():
    components, _ = _mod().map_adsense_gap_to_components(
        month=MONTH,
        source_rows=[source_row(value_kind="settled", amount="1000.00", account="pub-1")],
        payments=[payment(account="pub-1", amount="930.00", status="PENDING")],
    )
    assert components == []


def test_gap_skips_non_usd_settled_or_paid_and_counts_it():
    components, skipped = _mod().map_adsense_gap_to_components(
        month=MONTH,
        source_rows=[source_row(value_kind="settled", amount="1000.00", account="pub-1", currency="EUR")],
        payments=[payment(account="pub-1", amount="930.00")],
    )
    assert components == []
    assert skipped == 1


def test_gap_zero_difference_produces_nothing():
    components, _ = _mod().map_adsense_gap_to_components(
        month=MONTH,
        source_rows=[source_row(value_kind="settled", amount="930.00", account="pub-1")],
        payments=[payment(account="pub-1", amount="930.00")],
    )
    assert components == []


def test_to_api_excludes_raw_payload_and_serializes_decimal():
    # to_api lives on the persisted read model (DeductionComponent). It omits
    # raw_payload and serializes amounts with the repo's trailing-zero-trimming
    # convention ("3.50" -> "3.5"), matching payment_status._decimal_to_api.
    component = _mod().DeductionComponent(
        id="row-1", month=MONTH, component_kind="TRANSFER_FEE", scope_kind="PAYMENT",
        scope_id="BANK-5", amount_usd=Decimal("3.50"), amount_native=None,
        currency_code="USD", source_system="bank_reconciliation",
        source_table="bank_reconciliation_entries", source_id=None,
        source_key="BANK-5", source_report_id=None, raw_payload={"k": "v"},
        component_key="bank:2026-04:BANK-5:transfer_fee",
    )
    api = component.to_api()
    assert "raw_payload" not in api
    assert api["amount_usd"] == "3.5"
    assert api["component_kind"] == "TRANSFER_FEE"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/finance/test_deduction_components.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ums_smart_revenue.finance.deduction_components'`.

- [ ] **Step 3: Write the module**

Create `backend/ums_smart_revenue/finance/deduction_components.py`:

```python
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    GoogleRevenueSourceRowEntry,
)
from ums_smart_revenue.finance.adsense_payments import AdSensePaymentEntry
from ums_smart_revenue.finance.bank_reconciliation import BankReconciliationEntry

USD = "USD"
COMPONENT_KINDS: tuple[str, ...] = (
    "TAX", "DEDUCTION", "TRANSFER_FEE", "FX_VARIANCE", "UNRESOLVED_PAYMENT_GAP",
)
SCOPE_KINDS: tuple[str, ...] = ("CHANNEL", "ACCOUNT", "PAYMENT")
_TAX_DEDUCTION_VALUE_KINDS: frozenset[str] = frozenset({"tax", "deduction"})
_SETTLED_VALUE_KIND = "settled"
_PAID_STATUS = "PAID"
_ADSENSE_SYSTEM = "adsense_management"
_GAP_SOURCE_SYSTEM = "adsense_payment_gap"


@dataclass(frozen=True)
class DeductionComponentInput:
    """One deduction-evidence component to upsert (no tenant/id/month yet)."""

    component_kind: str
    scope_kind: str
    scope_id: str
    amount_usd: Decimal
    amount_native: Decimal | None
    currency_code: str
    source_system: str
    source_table: str
    source_id: str | None
    source_key: str | None
    source_report_id: str | None
    raw_payload: dict[str, object]
    component_key: str


@dataclass(frozen=True)
class DeductionComponent:
    """Persisted deduction-component read model."""

    id: str
    month: str
    component_kind: str
    scope_kind: str
    scope_id: str
    amount_usd: Decimal
    amount_native: Decimal | None
    currency_code: str
    source_system: str
    source_table: str
    source_id: str | None
    source_key: str | None
    source_report_id: str | None
    raw_payload: dict[str, object]
    component_key: str

    def to_api(self) -> dict[str, object]:
        # raw_payload is intentionally omitted (provenance only; see PR-B endpoint).
        return {
            "id": self.id,
            "month": self.month,
            "component_kind": self.component_kind,
            "scope_kind": self.scope_kind,
            "scope_id": self.scope_id,
            "amount_usd": _decimal_to_api(self.amount_usd),
            "amount_native": (
                None if self.amount_native is None
                else _decimal_to_api(self.amount_native)
            ),
            "currency_code": self.currency_code,
            "source_system": self.source_system,
            "source_table": self.source_table,
            "source_id": self.source_id,
            "source_key": self.source_key,
            "source_report_id": self.source_report_id,
            "component_key": self.component_key,
        }


# ============================================================================
# Purpose: Map source-reported tax/deduction source rows into typed deduction
#   components. Channel-scoped when youtube_channel_id is present, else account.
# Database/ORM: None (pure over already-read GoogleRevenueSourceRowEntry rows).
# Standards: USD-only — non-USD rows are skipped and counted, never converted.
# Blast Radius: Finance read-model only. No writes, no auth.
# ============================================================================
def map_source_rows_to_components(
    rows: Iterable[GoogleRevenueSourceRowEntry],
) -> tuple[list[DeductionComponentInput], int]:
    components: list[DeductionComponentInput] = []
    skipped_non_usd = 0
    for row in rows:
        if row.value_kind not in _TAX_DEDUCTION_VALUE_KINDS:
            continue
        if row.currency_code != USD:
            skipped_non_usd += 1
            continue
        if row.youtube_channel_id:
            scope_kind, scope_id = "CHANNEL", row.youtube_channel_id
        else:
            scope_kind, scope_id = "ACCOUNT", row.source_account_id
        components.append(
            DeductionComponentInput(
                component_kind=row.value_kind.upper(),
                scope_kind=scope_kind,
                scope_id=scope_id,
                amount_usd=row.amount_native,
                amount_native=row.amount_native,
                currency_code=row.currency_code,
                source_system=row.source_system,
                source_table="google_revenue_source_rows",
                source_id=row.id,
                source_key=row.source_row_key,
                source_report_id=row.source_report_id,
                raw_payload={
                    "value_kind": row.value_kind,
                    "metric_key": row.metric_key,
                    "source_row_key": row.source_row_key,
                },
                component_key=f"srcrow:{row.source_system}:{row.source_row_key}",
            )
        )
    return components, skipped_non_usd


# ============================================================================
# Purpose: Map bank reconciliation entries into PAYMENT-scoped components —
#   TRANSFER_FEE (deduction evidence) and signed FX_VARIANCE (variance, not a
#   blind deduction). Keyed by month + bank_reference (bank uniqueness key).
# Database/ORM: None (pure over BankReconciliationEntry).
# Standards: amounts are already USD; nothing is skipped for currency here.
# Blast Radius: Finance read-model only.
# ============================================================================
def map_bank_entries_to_components(
    entries: Iterable[BankReconciliationEntry],
    *,
    month: str,
) -> tuple[list[DeductionComponentInput], int]:
    components: list[DeductionComponentInput] = []
    for entry in entries:
        if entry.transfer_fee_usd > 0:
            components.append(
                _bank_component(
                    entry, month, "TRANSFER_FEE", entry.transfer_fee_usd,
                    "transfer_fee",
                )
            )
        if entry.fx_difference_usd != 0:
            components.append(
                _bank_component(
                    entry, month, "FX_VARIANCE", entry.fx_difference_usd,
                    "fx_variance",
                )
            )
    return components, 0


def _bank_component(
    entry: BankReconciliationEntry,
    month: str,
    kind: str,
    amount_usd: Decimal,
    key_suffix: str,
) -> DeductionComponentInput:
    return DeductionComponentInput(
        component_kind=kind,
        scope_kind="PAYMENT",
        scope_id=entry.bank_reference,
        amount_usd=amount_usd,
        amount_native=None,
        currency_code=USD,
        source_system="bank_reconciliation",
        source_table="bank_reconciliation_entries",
        source_id=entry.id,
        source_key=entry.bank_reference,
        source_report_id=entry.source_report_id,
        raw_payload={"bank_reference": entry.bank_reference, "kind": kind},
        component_key=f"bank:{month}:{entry.bank_reference}:{key_suffix}",
    )


# ============================================================================
# Purpose: Compute the account-level AdSense settled-earnings vs PAID-payment
#   gap as UNRESOLVED_PAYMENT_GAP evidence (signed). Reconciliation evidence
#   only — never labeled tax/withholding/fee. Emitted only when both a settled
#   earnings total and a PAID total exist for the account+month and they differ.
# Database/ORM: None (pure over source rows + payments).
# Standards: USD-only — non-USD settled rows / PAID payments are skipped+counted.
# Blast Radius: Finance read-model only.
# ============================================================================
def map_adsense_gap_to_components(
    *,
    month: str,
    source_rows: Iterable[GoogleRevenueSourceRowEntry],
    payments: Iterable[AdSensePaymentEntry],
) -> tuple[list[DeductionComponentInput], int]:
    skipped_non_usd = 0
    settled: dict[str, Decimal] = {}
    for row in source_rows:
        if row.source_system != _ADSENSE_SYSTEM or row.value_kind != _SETTLED_VALUE_KIND:
            continue
        if row.currency_code != USD:
            skipped_non_usd += 1
            continue
        settled[row.source_account_id] = (
            settled.get(row.source_account_id, Decimal("0")) + row.amount_native
        )
    paid: dict[str, Decimal] = {}
    for pay in payments:
        if pay.payment_status != _PAID_STATUS:
            continue
        if pay.payment_currency != USD:
            skipped_non_usd += 1
            continue
        paid[pay.source_account_id] = (
            paid.get(pay.source_account_id, Decimal("0")) + pay.payment_amount
        )
    components: list[DeductionComponentInput] = []
    for account in sorted(set(settled) & set(paid)):
        gap = settled[account] - paid[account]
        if gap == 0:
            continue
        components.append(
            DeductionComponentInput(
                component_kind="UNRESOLVED_PAYMENT_GAP",
                scope_kind="ACCOUNT",
                scope_id=account,
                amount_usd=gap,
                amount_native=None,
                currency_code=USD,
                source_system=_GAP_SOURCE_SYSTEM,
                source_table="adsense_payment_gap",
                source_id=None,
                source_key=f"{account}:{month}",
                source_report_id=None,
                raw_payload={
                    "settled_earnings_usd": str(settled[account]),
                    "paid_usd": str(paid[account]),
                },
                component_key=f"adsense_gap:{account}:{month}",
            )
        )
    return components, skipped_non_usd


def _decimal_to_api(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f")
    return format(normalized, "f").rstrip("0").rstrip(".")
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/finance/test_deduction_components.py -q`
Expected: PASS — 15 passed.

- [ ] **Step 5: Lint + commit**

```bash
python -m ruff check backend/ums_smart_revenue/finance/deduction_components.py tests/finance/test_deduction_components.py
git add backend/ums_smart_revenue/finance/deduction_components.py tests/finance/test_deduction_components.py
git commit -m "feat(finance): pure deduction-component mappers (tax/deduction, bank fee/FX, AdSense gap)"
```
End the commit message with the `Co-Authored-By` trailer.

---

## Task 4: Repository + ingestion service `finance/deduction_ingestion.py`

**Files:**
- Create: `backend/ums_smart_revenue/finance/deduction_ingestion.py`
- Test: `tests/finance/test_deduction_ingestion.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/finance/test_deduction_ingestion.py`:

```python
"""Repository + service tests for deduction-component ingestion (SQLite)."""
from datetime import date
from decimal import Decimal
from importlib import import_module
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.auth.models import RoleAssignment, UserPrincipal
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.finance_models import (
    AdSensePaymentORM,
    BankReconciliationEntryORM,
    FinanceBase,
    FinanceMonthCloseORM,
)
from ums_smart_revenue.db.source_models import GoogleRevenueSourceRowORM

MONTH = "2026-04"
ACTOR_ID = UUID("00000000-0000-0000-0000-0000000c0001")


def _mod():
    return import_module("ums_smart_revenue.finance.deduction_ingestion")


def _actor() -> UserPrincipal:
    return UserPrincipal(
        user_id=str(ACTOR_ID),
        email="ingest@example.com",
        role_assignments=(
            RoleAssignment(role=RoleKey.FINANCE_VIEWER, scope=AccessScope.global_scope()),
        ),
    )


def _engine(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"
    )
    FinanceBase.metadata.create_all(engine)
    return engine


def _seed(session: Session, *, settled="1000.00", paid="930.00", tax_currency="USD",
          locked=False):
    session.add(
        BankReconciliationEntryORM(
            id=uuid4(), month=MONTH, bank_reference="BANK-1",
            bank_received_date=date(2026, 4, 20),
            bank_received_amount=Decimal("1000.00"), bank_received_currency="USD",
            bank_received_amount_usd=Decimal("1000.00"),
            transfer_fee_usd=Decimal("3.50"), fx_difference_usd=Decimal("-2.00"),
            recorded_by=ACTOR_ID,
        )
    )
    session.add(
        AdSensePaymentORM(
            id=uuid4(), month=MONTH, payment_name="apr", source_account_id="pub-1",
            payment_date=date(2026, 5, 21), payment_amount=Decimal(paid),
            payment_currency="USD", payment_status="PAID", raw_payload={},
            source_report_id=None, imported_by=ACTOR_ID,
        )
    )
    session.add(
        GoogleRevenueSourceRowORM(
            id=uuid4(), tenant_id=_ums_tenant(), source_system="adsense_management",
            source_row_key=("settled-key").ljust(64, "0")[:64],
            source_account_id="pub-1", youtube_channel_id=None, report_type="r",
            report_month=MONTH, period_start=date(2026, 4, 1),
            period_end=date(2026, 4, 30), metric_key="m", value_kind="settled",
            amount_native=Decimal(settled), currency_code="USD",
            source_report_id=None, raw_payload={},
        )
    )
    session.add(
        GoogleRevenueSourceRowORM(
            id=uuid4(), tenant_id=_ums_tenant(), source_system="adsense_management",
            source_row_key=("tax-key").ljust(64, "0")[:64],
            source_account_id="pub-1", youtube_channel_id=None, report_type="r",
            report_month=MONTH, period_start=date(2026, 4, 1),
            period_end=date(2026, 4, 30), metric_key="m", value_kind="tax",
            amount_native=Decimal("11.00"), currency_code=tax_currency,
            source_report_id=None, raw_payload={},
        )
    )
    if locked:
        session.add(
            FinanceMonthCloseORM(
                tenant_id=_ums_tenant(), month=MONTH, status="LOCKED",
                allocation_rule_payload={},
            )
        )
    session.commit()


def _ums_tenant() -> UUID:
    from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
    return UUID(UMS_TENANT_ID)


def _service(session):
    return _mod().DeductionIngestionService(session, audit_sink=InMemoryAuditSink())


def test_ingest_creates_components_from_all_sources(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        sink = InMemoryAuditSink()
        service = _mod().DeductionIngestionService(session, audit_sink=sink)
        result = service.ingest(month=MONTH, actor=_actor(), reason="monthly")
        session.commit()
        repo = _mod().SqlAlchemyDeductionComponentRepository(session)
        kinds = {c.component_kind for c in repo.list_month_components(month=MONTH)}
    assert {"TRANSFER_FEE", "FX_VARIANCE", "UNRESOLVED_PAYMENT_GAP", "TAX"} <= kinds
    assert result.by_kind["UNRESOLVED_PAYMENT_GAP"] == 1
    assert result.total_upserted >= 4
    assert sink.records[-1].event_type == "DEDUCTION_COMPONENTS_INGESTED"


def test_ingest_is_idempotent(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        service = _service(session)
        service.ingest(month=MONTH, actor=_actor(), reason="r1")
        session.commit()
        service.ingest(month=MONTH, actor=_actor(), reason="r2")
        session.commit()
        repo = _mod().SqlAlchemyDeductionComponentRepository(session)
        components = repo.list_month_components(month=MONTH)
    keys = [c.component_key for c in components]
    assert len(keys) == len(set(keys))  # no duplicates after re-ingest


def test_ingest_refuses_locked_month(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session, locked=True)
        service = _service(session)
        with pytest.raises(_mod().DeductionComponentLockedMonthError):
            service.ingest(month=MONTH, actor=_actor(), reason="r")


def test_ingest_refuses_locked_month_even_with_zero_components(tmp_path):
    # Lock the month but seed NO source evidence -> zero mapped components. Live
    # ingestion must still fail closed (no audit write) — the lock check must
    # precede the empty-component short-circuit in upsert_components.
    engine = _engine(tmp_path)
    with Session(engine) as session:
        from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
        session.add(
            FinanceMonthCloseORM(
                tenant_id=UUID(UMS_TENANT_ID), month=MONTH, status="LOCKED",
                allocation_rule_payload={},
            )
        )
        session.commit()
        sink = InMemoryAuditSink()
        service = _mod().DeductionIngestionService(session, audit_sink=sink)
        with pytest.raises(_mod().DeductionComponentLockedMonthError):
            service.ingest(month=MONTH, actor=_actor(), reason="r")
        assert sink.records == []  # no audit written on a refused locked run


def test_ingest_skips_non_usd_and_counts_it(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session, tax_currency="EUR")
        service = _service(session)
        result = service.ingest(month=MONTH, actor=_actor(), reason="r")
        session.commit()
        repo = _mod().SqlAlchemyDeductionComponentRepository(session)
        kinds = [c.component_kind for c in repo.list_month_components(month=MONTH)]
    assert result.skipped_non_usd >= 1
    assert "TAX" not in kinds  # the EUR tax row was skipped, not stored


def test_dry_run_writes_nothing_and_records_no_audit(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        sink = InMemoryAuditSink()
        service = _mod().DeductionIngestionService(session, audit_sink=sink)
        result = service.ingest(month=MONTH, actor=_actor(), reason="r", dry_run=True)
        session.commit()
        repo = _mod().SqlAlchemyDeductionComponentRepository(session)
        components = repo.list_month_components(month=MONTH)
    assert components == []
    assert sink.records == []
    assert result.total_upserted >= 4  # would-upsert count is still reported


def test_audit_details_carry_only_summary_counts(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        sink = InMemoryAuditSink()
        service = _mod().DeductionIngestionService(session, audit_sink=sink)
        service.ingest(month=MONTH, actor=_actor(), reason="r")
        session.commit()
    details = sink.records[-1].details
    assert set(details) == {"month", "total_upserted", "by_kind", "skipped_non_usd"}
    # No amounts/currencies/payloads leak into the audit record.
    assert "amount" not in repr(details).lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/finance/test_deduction_ingestion.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ums_smart_revenue.finance.deduction_ingestion'`.

- [ ] **Step 3: Write the module**

Create `backend/ums_smart_revenue/finance/deduction_ingestion.py`:

```python
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.connectors.google_source_rows.repository import (
    SqlAlchemyGoogleRevenueSourceRowRepository,
)
from ums_smart_revenue.db.finance_models import DeductionComponentORM
from ums_smart_revenue.finance.adsense_payments import SqlAlchemyAdSensePaymentRepository
from ums_smart_revenue.finance.bank_reconciliation import (
    SqlAlchemyBankReconciliationRepository,
)
from ums_smart_revenue.finance.deduction_components import (
    DeductionComponent,
    DeductionComponentInput,
    map_adsense_gap_to_components,
    map_bank_entries_to_components,
    map_source_rows_to_components,
)
from ums_smart_revenue.finance.month_close import get_or_create_month_close_row
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

INGESTION_SOURCES: tuple[str, ...] = ("source_rows", "bank", "gap")
_MONTH_LENGTH = 7


class DeductionComponentError(ValueError):
    """Base error for deduction-component ingestion."""


class DeductionComponentValidationError(DeductionComponentError):
    """Raised for invalid ingestion input."""


class DeductionComponentLockedMonthError(DeductionComponentError):
    """Raised when the target finance month is locked."""


@dataclass(frozen=True)
class DeductionIngestionResult:
    """Summary of one ingestion run (counts only — no amounts)."""

    month: str
    total_upserted: int
    by_kind: dict[str, int]
    skipped_non_usd: int
    dry_run: bool


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    if tenant_id is None:
        return UUID(UMS_TENANT_ID)
    if isinstance(tenant_id, UUID):
        return tenant_id
    try:
        return UUID(str(tenant_id))
    except ValueError as exc:
        raise DeductionComponentValidationError(f"invalid tenant_id: {tenant_id!r}") from exc


def _validate_month(month: str) -> None:
    if len(month) != _MONTH_LENGTH or month[4] != "-":
        raise DeductionComponentValidationError("month must use YYYY-MM")
    year, sep, mm = month[:4], month[4], month[5:]
    if not (year.isdigit() and sep == "-" and mm.isdigit() and 1 <= int(mm) <= 12):
        raise DeductionComponentValidationError("month must use YYYY-MM")


def _dialect_insert(dialect_name: str):
    if dialect_name == "sqlite":
        return sqlite_insert
    if dialect_name == "postgresql":
        return postgresql_insert
    raise DeductionComponentValidationError(
        f"Unsupported database dialect for deduction component upsert: {dialect_name}"
    )


class SqlAlchemyDeductionComponentRepository:
    """Idempotent, tenant-scoped storage for deduction components."""

    # ========================================================================
    # Purpose: Upsert deduction components by (tenant_id, component_key) and
    #   read a month's components. Refuses writes to LOCKED finance months.
    # Database/ORM: deduction_components / DeductionComponentORM.
    # Standards: idempotent ON CONFLICT DO UPDATE; month-lock advisory gate via
    #   get_or_create_month_close_row(for_update=True), mirroring sibling repos.
    # Blast Radius: Finance source-of-truth writes (new table). No auth/Neo4j.
    # ========================================================================
    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    def _require_month_open(self, month: str) -> None:
        close = get_or_create_month_close_row(
            self._session, month, tenant_id=self._tenant_id, for_update=True
        )
        if close.status == "LOCKED":
            raise DeductionComponentLockedMonthError(
                "Finance month is locked for deduction-component ingestion"
            )

    def upsert_components(
        self, *, month: str, components: list[DeductionComponentInput]
    ) -> list[DeductionComponent]:
        _validate_month(month)
        # FIX: refuse LOCKED months even for a zero-component run. Live ingestion
        # must fail closed BEFORE the empty-return (and before the service's audit
        # path), so the lock check precedes the empty-component short-circuit.
        self._require_month_open(month)
        if not components:
            return []
        insert_builder = _dialect_insert(self._session.get_bind().dialect.name)
        entries: list[DeductionComponent] = []
        now = datetime.now(UTC)
        for component in components:
            statement = insert_builder(DeductionComponentORM).values(
                id=uuid4(),
                tenant_id=self._tenant_id,
                month=month,
                component_kind=component.component_kind,
                scope_kind=component.scope_kind,
                scope_id=component.scope_id,
                amount_usd=component.amount_usd,
                amount_native=component.amount_native,
                currency_code=component.currency_code,
                source_system=component.source_system,
                source_table=component.source_table,
                source_id=component.source_id,
                source_key=component.source_key,
                source_report_id=component.source_report_id,
                raw_payload=dict(component.raw_payload),
                component_key=component.component_key,
                updated_at=now,
            ).on_conflict_do_update(
                index_elements=[
                    DeductionComponentORM.tenant_id,
                    DeductionComponentORM.component_key,
                ],
                set_={
                    "month": month,
                    "component_kind": component.component_kind,
                    "scope_kind": component.scope_kind,
                    "scope_id": component.scope_id,
                    "amount_usd": component.amount_usd,
                    "amount_native": component.amount_native,
                    "currency_code": component.currency_code,
                    "source_system": component.source_system,
                    "source_table": component.source_table,
                    "source_id": component.source_id,
                    "source_key": component.source_key,
                    "source_report_id": component.source_report_id,
                    "raw_payload": dict(component.raw_payload),
                    "updated_at": now,
                },
            ).returning(DeductionComponentORM.id)
            row_id = self._session.execute(statement).scalar_one()
            row = self._session.get(DeductionComponentORM, row_id)
            if row is None:
                raise DeductionComponentValidationError("deduction component upsert failed")
            self._session.refresh(row)
            entries.append(self._to_entry(row))
        return entries

    def list_month_components(self, *, month: str) -> list[DeductionComponent]:
        _validate_month(month)
        rows = self._session.scalars(
            select(DeductionComponentORM)
            .where(DeductionComponentORM.tenant_id == self._tenant_id)
            .where(DeductionComponentORM.month == month)
            .order_by(
                DeductionComponentORM.scope_kind,
                DeductionComponentORM.scope_id,
                DeductionComponentORM.component_kind,
                DeductionComponentORM.component_key,
            )
        ).all()
        return [self._to_entry(row) for row in rows]

    @staticmethod
    def _to_entry(row: DeductionComponentORM) -> DeductionComponent:
        return DeductionComponent(
            id=str(row.id),
            month=row.month,
            component_kind=row.component_kind,
            scope_kind=row.scope_kind,
            scope_id=row.scope_id,
            amount_usd=row.amount_usd,
            amount_native=row.amount_native,
            currency_code=row.currency_code,
            source_system=row.source_system,
            source_table=row.source_table,
            source_id=row.source_id,
            source_key=row.source_key,
            source_report_id=row.source_report_id,
            raw_payload=dict(row.raw_payload or {}),
            component_key=row.component_key,
        )


class DeductionIngestionService:
    """Read source-of-truth tables, map to components, upsert + audit."""

    # ========================================================================
    # Purpose: Orchestrate deduction-evidence ingestion for one tenant+month:
    #   read source rows / bank entries / AdSense payments, run the pure
    #   mappers, idempotently upsert, and record one summary-count-only
    #   DEDUCTION_COMPONENTS_INGESTED audit event. No allocation, no net math.
    # Database/ORM: reads google_revenue_source_rows, bank_reconciliation_entries,
    #   adsense_payments; writes deduction_components (via the repository).
    # Standards: USD-only (non-USD skipped+counted); month-lock-gated; audit
    #   details carry ONLY counts (no amounts/payloads).
    # Blast Radius: Finance source-of-truth writes + one audit event.
    # ========================================================================
    def __init__(
        self, session: Session, *, audit_sink: AuditSink,
        tenant_id: UUID | str | None = None,
    ):
        self._session = session
        self._audit_sink = audit_sink
        self._tenant_id = _resolve_tenant_id(tenant_id)
        self._repository = SqlAlchemyDeductionComponentRepository(
            session, tenant_id=self._tenant_id
        )

    def ingest(
        self, *, month: str, actor: UserPrincipal, reason: str,
        source: str | None = None, dry_run: bool = False,
    ) -> DeductionIngestionResult:
        _validate_month(month)
        if source is not None and source not in INGESTION_SOURCES:
            raise DeductionComponentValidationError(
                f"source must be one of {INGESTION_SOURCES} or None"
            )
        payment_repo = SqlAlchemyAdSensePaymentRepository(
            self._session, tenant_id=self._tenant_id
        )
        bank_repo = SqlAlchemyBankReconciliationRepository(
            self._session, tenant_id=self._tenant_id
        )
        source_row_repo = SqlAlchemyGoogleRevenueSourceRowRepository(self._session)

        payments = payment_repo.list_month_payments(month=month)
        bank_entries = bank_repo.list_month_entries(month=month)
        source_rows = source_row_repo.list(self._tenant_id, report_month=month)

        components: list[DeductionComponentInput] = []
        skipped_non_usd = 0
        if source in (None, "source_rows"):
            mapped, skipped = map_source_rows_to_components(source_rows)
            components.extend(mapped)
            skipped_non_usd += skipped
        if source in (None, "bank"):
            mapped, skipped = map_bank_entries_to_components(bank_entries, month=month)
            components.extend(mapped)
            skipped_non_usd += skipped
        if source in (None, "gap"):
            mapped, skipped = map_adsense_gap_to_components(
                month=month, source_rows=source_rows, payments=payments
            )
            components.extend(mapped)
            skipped_non_usd += skipped

        by_kind: dict[str, int] = {}
        for component in components:
            by_kind[component.component_kind] = by_kind.get(component.component_kind, 0) + 1
        total = len(components)

        if not dry_run:
            self._repository.upsert_components(month=month, components=components)
            record_audit_event(
                sink=self._audit_sink,
                actor=actor,
                event_type=AuditEventType.DEDUCTION_COMPONENTS_INGESTED,
                entity_type="deduction_components",
                entity_id=month,
                scope=AccessScope.finance_month(month),
                reason=reason,
                details={
                    "month": month,
                    "total_upserted": total,
                    "by_kind": by_kind,
                    "skipped_non_usd": skipped_non_usd,
                },
            )
        return DeductionIngestionResult(
            month=month, total_upserted=total, by_kind=by_kind,
            skipped_non_usd=skipped_non_usd, dry_run=dry_run,
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/finance/test_deduction_ingestion.py -q`
Expected: PASS — 7 passed.

- [ ] **Step 5: Lint + commit**

```bash
python -m ruff check backend/ums_smart_revenue/finance/deduction_ingestion.py tests/finance/test_deduction_ingestion.py
git add backend/ums_smart_revenue/finance/deduction_ingestion.py tests/finance/test_deduction_ingestion.py
git commit -m "feat(finance): deduction-component repository + ingestion service"
```
End the commit message with the `Co-Authored-By` trailer.

---

## Task 5: Operator CLI `scripts/run_deduction_ingestion.py`

**Files:**
- Create: `scripts/run_deduction_ingestion.py`
- Test: `tests/scripts/test_run_deduction_ingestion_cli.py`

Mirrors `scripts/run_adsense_payment_sync.py` exactly (service-principal actor, exit codes 0/2, dry-run prints counts and never commits).

- [ ] **Step 1: Write the failing CLI test**

Create `tests/scripts/test_run_deduction_ingestion_cli.py`:

```python
"""Tests for the run_deduction_ingestion CLI script."""
import importlib.util
from pathlib import Path

import pytest

from ums_smart_revenue.finance.deduction_ingestion import (
    DeductionComponentLockedMonthError,
    DeductionComponentValidationError,
)

_CLI_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "run_deduction_ingestion.py"
)
TENANT = "00000000-0000-0000-0000-00000000d001"
BASE_ARGV = ["--tenant", TENANT, "--month", "2026-04", "--reason", "r"]


def _load_cli():
    spec = importlib.util.spec_from_file_location("run_deduction_ingestion", _CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeSettings:
    def __init__(self, database_url="sqlite+pysqlite:///:memory:"):
        self.database_url = database_url


class _SpySession:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_result():
    return type(
        "R", (), {"month": "2026-04", "total_upserted": 4,
                  "by_kind": {"TRANSFER_FEE": 1}, "skipped_non_usd": 0, "dry_run": False},
    )()


class _FakeServiceOK:
    def __init__(self, session, *, audit_sink, tenant_id=None):
        pass

    @staticmethod
    def ingest(**kwargs):
        return _fake_result()


def _patch_common(monkeypatch, module, *, session, service, settings=None):
    monkeypatch.setattr(
        module, "load_app_settings",
        lambda: settings if settings is not None else _FakeSettings(),
    )
    monkeypatch.setattr(module, "build_session_factory", lambda _url: (lambda: session))
    monkeypatch.setattr(
        module, "build_connector_service_principal", lambda *, tenant_id: object()
    )
    monkeypatch.setattr(module, "SqlAlchemyAuditSink", lambda _s, *, tenant_id=None: object())
    monkeypatch.setattr(module, "DeductionIngestionService", service)


def test_cli_live_success_commits_and_returns_0(monkeypatch, capsys):
    module = _load_cli()
    session = _SpySession()
    _patch_common(monkeypatch, module, session=session, service=_FakeServiceOK)
    rc = module.main(BASE_ARGV)
    assert rc == 0
    assert session.commits == 1
    assert "INGESTED" in capsys.readouterr().out


def test_cli_dry_run_does_not_commit(monkeypatch, capsys):
    module = _load_cli()
    session = _SpySession()
    _patch_common(monkeypatch, module, session=session, service=_FakeServiceOK)
    rc = module.main(BASE_ARGV + ["--dry-run"])
    assert rc == 0
    assert session.commits == 0
    assert "DRY-RUN" in capsys.readouterr().out


def test_cli_missing_db_config_returns_2(monkeypatch, capsys):
    module = _load_cli()
    session = _SpySession()
    _patch_common(
        monkeypatch, module, session=session, service=_FakeServiceOK,
        settings=_FakeSettings(database_url=""),
    )
    rc = module.main(BASE_ARGV)
    assert rc == 2
    assert "UMS_DATABASE_URL" in capsys.readouterr().err
    assert session.commits == 0


@pytest.mark.parametrize("error", [
    DeductionComponentValidationError("bad"),
    DeductionComponentLockedMonthError("locked"),
])
def test_cli_typed_failure_returns_2(monkeypatch, capsys, error):
    module = _load_cli()
    session = _SpySession()

    class _Raises:
        def __init__(self, session, *, audit_sink, tenant_id=None):
            pass

        @staticmethod
        def ingest(**kwargs):
            raise error

    _patch_common(monkeypatch, module, session=session, service=_Raises)
    rc = module.main(BASE_ARGV)
    assert rc == 2
    assert type(error).__name__ in capsys.readouterr().err
    assert session.commits == 0


def test_cli_malformed_settings_returns_2_before_db_session(monkeypatch, capsys):
    # Settings validation failures are operator input errors -> exit 2 before any
    # DB setup. build_session_factory must NOT run after a settings ValueError.
    module = _load_cli()

    def _bad_settings():
        raise ValueError("malformed operator settings")

    def _unexpected_session_factory(_url):
        raise AssertionError("database setup must not run after settings validation")

    monkeypatch.setattr(module, "load_app_settings", _bad_settings)
    monkeypatch.setattr(module, "build_session_factory", _unexpected_session_factory)
    rc = module.main(BASE_ARGV)
    assert rc == 2
    assert "ValueError" in capsys.readouterr().err


def test_cli_missing_service_actor_returns_2(monkeypatch, capsys):
    # A missing/blank service-actor id raises ValueError before any write -> exit 2.
    module = _load_cli()
    session = _SpySession()
    _patch_common(monkeypatch, module, session=session, service=_FakeServiceOK)

    def _no_actor(*, tenant_id):
        raise ValueError("service actor id is required")

    monkeypatch.setattr(module, "build_connector_service_principal", _no_actor)
    rc = module.main(BASE_ARGV)
    assert rc == 2
    assert "ValueError" in capsys.readouterr().err
    assert session.commits == 0


def test_cli_untyped_error_propagates(monkeypatch):
    # Non-typed errors are NOT caught (no exit-2 swallow); they propagate with a
    # traceback, matching the AdSense sync CLI contract.
    module = _load_cli()
    session = _SpySession()

    class _Boom:
        def __init__(self, session, *, audit_sink, tenant_id=None):
            pass

        @staticmethod
        def ingest(**kwargs):
            raise RuntimeError("unexpected non-typed error")

    _patch_common(monkeypatch, module, session=session, service=_Boom)
    with pytest.raises(RuntimeError):
        module.main(BASE_ARGV)


def test_cli_bad_tenant_uuid_is_argparse_error():
    module = _load_cli()
    with pytest.raises(SystemExit) as excinfo:
        module.main(["--tenant", "nope", "--month", "2026-04", "--reason", "r"])
    assert excinfo.value.code != 0


def test_cli_blank_reason_is_argparse_error():
    module = _load_cli()
    with pytest.raises(SystemExit) as excinfo:
        module.main(["--tenant", TENANT, "--month", "2026-04", "--reason", "  "])
    assert excinfo.value.code != 0
```

> Note: `_fake_result()` returns a throwaway object exposing
> `month / total_upserted / by_kind / skipped_non_usd / dry_run` — a lightweight stand-in
> for `DeductionIngestionResult` so the CLI test stays decoupled from the service.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/scripts/test_run_deduction_ingestion_cli.py -q`
Expected: FAIL — the CLI file does not exist (`FileNotFoundError` / spec load error).

- [ ] **Step 3: Write the CLI**

Create `scripts/run_deduction_ingestion.py`:

```python
#!/usr/bin/env python
"""CLI entrypoint for deduction-component ingestion.

Usage:
    python scripts/run_deduction_ingestion.py \
        --tenant <UUID> --month <YYYY-MM> --reason "<audit reason>" \
        [--source source_rows|bank|gap] [--dry-run]

Exit codes:
    0   -- success (including a clean dry-run).
    2   -- typed/config failure (missing UMS_DATABASE_URL, missing service actor,
           or DeductionComponentError). No commit happened.
    !=0 -- argparse rejection (bad --tenant UUID, blank --reason).
"""
# ============================================================================
# Purpose: Operator CLI driving one deduction-component ingestion run for a
#   single (tenant, month). Translates argparse/config/typed errors into stable
#   exit codes; live mode commits, dry-run never commits.
# Database/ORM: Opens one Session; SQL owned by DeductionIngestionService /
#   SqlAlchemyDeductionComponentRepository / SqlAlchemyAuditSink.
# Standards: thin entrypoint; typed DeductionComponentError -> exit 2; untyped
#   errors propagate. No secret/token printed.
# Blast Radius: Operator surface only. No finance math, no allocation here.
# Connections:
#   - File: backend/ums_smart_revenue/finance/deduction_ingestion.py -> service.
#   - File: backend/ums_smart_revenue/connectors/google/audit.py ->
#     build_connector_service_principal (RUN_CONNECTOR_JOBS service actor).
# ============================================================================
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_PATH = str(_PROJECT_ROOT / "backend")
if _BACKEND_PATH not in sys.path:
    sys.path.insert(0, _BACKEND_PATH)

from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink  # noqa: E402
from ums_smart_revenue.config.settings import load_app_settings  # noqa: E402
from ums_smart_revenue.connectors.google.audit import (  # noqa: E402
    build_connector_service_principal,
)
from ums_smart_revenue.db.session import build_session_factory  # noqa: E402
from ums_smart_revenue.finance.deduction_ingestion import (  # noqa: E402
    DeductionComponentError,
    DeductionIngestionService,
    INGESTION_SOURCES,
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run deduction-component ingestion for one (tenant, month).",
    )
    parser.add_argument("--tenant", required=True, type=UUID, help="Tenant UUID.")
    parser.add_argument("--month", required=True, help="Finance month YYYY-MM.")
    parser.add_argument("--reason", required=True, help="Non-empty audit reason.")
    parser.add_argument(
        "--source", choices=list(INGESTION_SOURCES), default=None,
        help="Limit to one source adapter (default: all).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute counts only: no DB writes, no audit, no commit.",
    )
    args = parser.parse_args(argv)
    if not args.reason.strip():
        parser.error("--reason must not be blank")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        settings = load_app_settings()
    except ValueError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if not settings.database_url:
        print(
            "UMS_DATABASE_URL is required to run deduction ingestion",
            file=sys.stderr,
        )
        return 2
    session_factory = build_session_factory(settings.database_url)
    with session_factory() as session:
        try:
            actor = build_connector_service_principal(tenant_id=args.tenant)
        except ValueError as exc:
            print(f"{type(exc).__name__}: {exc!s}", file=sys.stderr)
            return 2
        audit_sink = SqlAlchemyAuditSink(session, tenant_id=args.tenant)
        service = DeductionIngestionService(
            session, audit_sink=audit_sink, tenant_id=args.tenant
        )
        try:
            result = service.ingest(
                month=args.month, actor=actor, reason=args.reason,
                source=args.source, dry_run=args.dry_run,
            )
        except DeductionComponentError as exc:
            print(f"{type(exc).__name__}: {exc!s}", file=sys.stderr)
            return 2

        if args.dry_run:
            print(
                f"DRY-RUN would_upsert={result.total_upserted} "
                f"by_kind={result.by_kind} skipped_non_usd={result.skipped_non_usd} "
                f"month={result.month}"
            )
            return 0
        session.commit()

    print(
        f"INGESTED upserted={result.total_upserted} by_kind={result.by_kind} "
        f"skipped_non_usd={result.skipped_non_usd} month={result.month}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/scripts/test_run_deduction_ingestion_cli.py -q`
Expected: PASS — 10 passed (8 named + 2 parametrized).

- [ ] **Step 5: Lint + commit**

```bash
python -m ruff check scripts/run_deduction_ingestion.py tests/scripts/test_run_deduction_ingestion_cli.py
git add scripts/run_deduction_ingestion.py tests/scripts/test_run_deduction_ingestion_cli.py
git commit -m "feat(scripts): operator CLI for deduction-component ingestion"
```
End the commit message with the `Co-Authored-By` trailer.

---

## Task 6: Full validation gate

**Files:** none (verification only).

- [ ] **Step 1: Ruff over the standard scope**

Run: `python -m ruff check backend tests scripts`
Expected: `All checks passed!`

- [ ] **Step 2: Full test suite**

Run: `python -m pytest -q`
Expected: PASS — prior count + the new tests (2 audit + 4 Postgres migration [pass only when `UMS_TEST_DATABASE_URL` is set; otherwise they are the pre-existing environment-gated `*_postgres.py` errors, unchanged] + 15 pure-mapper + 7 ingestion + 10 CLI). 0 failed.

- [ ] **Step 3: Whitespace/diff hygiene**

Run: `git diff --check`
Expected: no output.

- [ ] **Step 4 (REQUIRED — this PR adds a migration): Postgres round-trip**

```bash
docker run --rm -d --name ums-mig-pg -p 55432:5432 -e POSTGRES_PASSWORD=ums postgres:18-alpine
# PowerShell: $env:UMS_TEST_DATABASE_URL = 'postgresql+psycopg://postgres:ums@localhost:55432/postgres'
# POSIX:      export UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:ums@localhost:55432/postgres
python -m pytest tests/db/test_deduction_components_migration_postgres.py -q
```
Expected: PASS — 4 passed. Then re-run `python -m pytest -q` under `UMS_TEST_DATABASE_URL` for full-gate parity (the 26 pre-existing `*_postgres.py` migration tests now run too).

---

## Notes for the implementer

- **Do NOT** touch `net_revenue.py` or add any read endpoint — those are PR-B. If you reach for them, stop.
- **Do NOT** add a new `Permission` — reuse `RUN_CONNECTOR_JOBS` (already `sensitive=True`). Adding a `Permission` without a `PermissionDefinition` triggers an import-time `RuntimeError` self-check.
- **Do NOT** modify `google_source_normalizer.py` — the value_kind consumer reads `google_revenue_source_rows` directly via `SqlAlchemyGoogleRevenueSourceRowRepository.list(...)` and filters `value_kind ∈ {tax, deduction}` in the pure mapper. No emitter exists today, so that mapper path is dormant but tested with synthetic rows.
- **USD-only:** never convert. Non-USD evidence is skipped and counted in `skipped_non_usd`.
- **Audit details carry counts only** — `{month, total_upserted, by_kind, skipped_non_usd}`. Never put amounts, currencies, or payloads in the audit record (operator constraint).
- **Migration head:** `down_revision = "20260529_0001"`. If another migration merged to `main` before you start, run `alembic heads`, set `down_revision` to the real head, and renumber the file (`20260529_0003…` etc.).
- **Idempotency:** the unique key is `(tenant_id, component_key)`; re-ingest upserts in place. The bank `component_key` includes `{month}` so a bank reference reused in a later month does not collide.
