# Channel↔Account Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the canonical, provenance-tracked, operator-verified channel↔account map substrate (two tables + repository + audited API) so a later allocation engine (Spec 2b) can read only *verified* account→channel mappings per month.

**Architecture:** Two SQLAlchemy tables on `FinanceBase` — `adsense_content_owner_links` (operator-verified `adsense_account_id ↔ content_owner_id`) and `content_owner_channel_links` (idempotently derived `content_owner_id ↔ youtube_channel_id`). A `SqlAlchemyChannelAccountLinkRepository` exposes propose/verify/reject (verify guarded by a transaction-scoped PostgreSQL advisory lock + fail-closed overlap check), an idempotent source-row derivation, and the read contract `list_verified_adsense_account_channels`. A new thin FastAPI module mounts under `/revenue`, gated by the existing permission/audit system.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, PostgreSQL (prod/migration tier) + SQLite (unit tier), pytest, ruff.

**Spec:** `Docs/superpowers/specs/2026-05-31-spec-channel-account-map-design.md` (approved).

**Branch:** `spec/channel-account-map` (off `main` `4a8c4b5`). Do NOT push or open a PR during execution.

**Commit discipline:** Every commit message MUST NOT contain a `Co-Authored-By` trailer or any Claude/AI footer. Backend slice only.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `backend/ums_smart_revenue/auth/audit.py` | Modify | +3 `AuditEventType` + `AUDIT_EVENT_DEFINITIONS` entries |
| `backend/ums_smart_revenue/db/finance_models.py` | Modify | +`_month_format_check` helper; +`AdsenseContentOwnerLinkORM`, `ContentOwnerChannelLinkORM` |
| `backend/ums_smart_revenue/db/alembic/versions/20260531_0001_channel_account_map.py` | Create | Migration: both tables, constraints, indexes, PG-only object CHECK |
| `backend/ums_smart_revenue/finance/channel_account_links.py` | Create | Read models, typed errors, helpers, repository (lock, overlap, propose/verify/reject, list, derivation, read contract) |
| `backend/ums_smart_revenue/api/channel_account_links.py` | Create | Router, Pydantic models, provider, `_require_permission`, endpoints |
| `backend/ums_smart_revenue/app.py` | Modify | Import + mount `channel_account_links_router` |
| `tests/db/test_channel_account_map_models.py` | Create | SQLite model/constraint tests |
| `tests/db/test_channel_account_map_migration_postgres.py` | Create | Postgres round-trip + concurrency/lock-path tests |
| `tests/finance/test_channel_account_links.py` | Create | Repository: lock-key, overlap, propose/verify/reject, list, derivation, read contract |
| `tests/api/test_channel_account_links_api.py` | Create | Endpoint auth matrix, audit, 422/409, no `provenance_payload` |
| `Docs/01_IMPLEMENTATION_PLAN.md`, `Docs/15_DELIVERY_BACKLOG.md` | Modify | Phase 4 status: map substrate shipped; allocation = Spec 2b |

**Naming contract (use these exact identifiers across all tasks):**
- ORM: `AdsenseContentOwnerLinkORM` (`adsense_content_owner_links`), `ContentOwnerChannelLinkORM` (`content_owner_channel_links`)
- Repository: `SqlAlchemyChannelAccountLinkRepository`
- Read models: `AccountOwnerLink`, `AccountOwnerLinkPage`
- Errors: `ChannelAccountLinkError`, `ChannelAccountLinkValidationError`, `ChannelAccountLinkConflictError`, `ChannelAccountLinkNotFoundError`
- Helpers: `_account_owner_lock_key`, `_ranges_overlap`, `_validate_month`, `_resolve_tenant_id`
- Methods: `propose_account_owner_link`, `verify_account_owner_link`, `reject_account_owner_link`, `list_account_owner_links`, `upsert_owner_channel_links_from_source`, `list_verified_adsense_account_channels`, `_acquire_account_owner_lock`
- Audit events: `CHANNEL_ACCOUNT_LINK_PROPOSED`, `CHANNEL_ACCOUNT_LINK_VERIFIED`, `CHANNEL_ACCOUNT_LINK_REJECTED`
- API: module `api/channel_account_links.py`, `router` (prefix `/revenue`), provider `current_channel_account_link_repository`
- Migration: `revision = "20260531_0001"`, `down_revision = "20260529_0002"`

**Validation gate (run after each task's final edit, and before any push):**
```bash
python -m ruff check backend tests
pytest -q
git diff --check
```
The Postgres-tier tests (Tasks 3 & 14) require the disposable container:
```bash
docker run --rm -d --name ums-mig-pg-test -e POSTGRES_PASSWORD=ums -e POSTGRES_DB=test_ums -p 55432:5432 postgres:18-alpine
# URL: postgresql+psycopg://postgres:ums@localhost:55432/test_ums
```

---

### Task 1: Audit event types for link decisions

**Files:**
- Modify: `backend/ums_smart_revenue/auth/audit.py`
- Test: `tests/auth/test_channel_account_link_audit_events.py`

- [ ] **Step 1: Write the failing test**

Create `tests/auth/test_channel_account_link_audit_events.py`:
```python
"""The three channel-account-link audit events are defined, sensitive, reason-required."""
from ums_smart_revenue.auth.audit import (
    AUDIT_EVENT_DEFINITIONS,
    AuditEventType,
)
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.permissions import SENSITIVE_PERMISSIONS


def test_link_audit_events_exist_with_reason_and_permissions():
    proposed = AUDIT_EVENT_DEFINITIONS[AuditEventType.CHANNEL_ACCOUNT_LINK_PROPOSED]
    verified = AUDIT_EVENT_DEFINITIONS[AuditEventType.CHANNEL_ACCOUNT_LINK_VERIFIED]
    rejected = AUDIT_EVENT_DEFINITIONS[AuditEventType.CHANNEL_ACCOUNT_LINK_REJECTED]

    assert proposed.reason_required is True
    assert proposed.permission == Permission.MANAGE_ORG_MAPPING
    assert verified.reason_required is True
    assert verified.permission == Permission.CHANGE_ALLOCATION_RULE
    assert rejected.reason_required is True
    assert rejected.permission == Permission.CHANGE_ALLOCATION_RULE
    # All three are sensitive because their permissions are sensitive.
    assert Permission.MANAGE_ORG_MAPPING in SENSITIVE_PERMISSIONS
    assert Permission.CHANGE_ALLOCATION_RULE in SENSITIVE_PERMISSIONS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/auth/test_channel_account_link_audit_events.py -q`
Expected: FAIL — `AttributeError: CHANNEL_ACCOUNT_LINK_PROPOSED` (member missing).

- [ ] **Step 3: Add the enum members**

In `backend/ums_smart_revenue/auth/audit.py`, inside `class AuditEventType(StrEnum)`, after `AUDIT_LOG_VIEWED = "AUDIT_LOG_VIEWED"`:
```python
    CHANNEL_ACCOUNT_LINK_PROPOSED = "CHANNEL_ACCOUNT_LINK_PROPOSED"
    CHANNEL_ACCOUNT_LINK_VERIFIED = "CHANNEL_ACCOUNT_LINK_VERIFIED"
    CHANNEL_ACCOUNT_LINK_REJECTED = "CHANNEL_ACCOUNT_LINK_REJECTED"
```

- [ ] **Step 4: Add the definitions**

In the same file, inside the `AUDIT_EVENT_DEFINITIONS` dict, after the `AuditEventType.AUDIT_LOG_VIEWED` entry:
```python
    AuditEventType.CHANNEL_ACCOUNT_LINK_PROPOSED: AuditEventDefinition(
        AuditEventType.CHANNEL_ACCOUNT_LINK_PROPOSED,
        reason_required=True,
        permission=Permission.MANAGE_ORG_MAPPING,
    ),
    AuditEventType.CHANNEL_ACCOUNT_LINK_VERIFIED: AuditEventDefinition(
        AuditEventType.CHANNEL_ACCOUNT_LINK_VERIFIED,
        reason_required=True,
        permission=Permission.CHANGE_ALLOCATION_RULE,
    ),
    AuditEventType.CHANNEL_ACCOUNT_LINK_REJECTED: AuditEventDefinition(
        AuditEventType.CHANNEL_ACCOUNT_LINK_REJECTED,
        reason_required=True,
        permission=Permission.CHANGE_ALLOCATION_RULE,
    ),
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/auth/test_channel_account_link_audit_events.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/ums_smart_revenue/auth/audit.py tests/auth/test_channel_account_link_audit_events.py
git commit -m "feat(audit): channel-account-link proposed/verified/rejected events"
```

---

### Task 2: ORM models for the two link tables

**Files:**
- Modify: `backend/ums_smart_revenue/db/finance_models.py`
- Test: `tests/db/test_channel_account_map_models.py`

- [ ] **Step 1: Write the failing test**

Create `tests/db/test_channel_account_map_models.py`:
```python
"""SQLite model + constraint coverage for the channel-account map tables."""
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    ContentOwnerChannelLinkORM,
    FinanceBase,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

TENANT = UMS_TENANT_ID


def _engine(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}")
    FinanceBase.metadata.create_all(engine)
    return engine


def test_account_owner_link_persists_with_defaults(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        session.add(
            AdsenseContentOwnerLinkORM(
                tenant_id=TENANT, adsense_account_id="pub-1", content_owner_id="owner-1",
                provenance_kind="OPERATOR_ASSERTED", effective_month_start="2026-01",
            )
        )
        session.commit()
        row = session.query(AdsenseContentOwnerLinkORM).one()
    assert row.verification_status == "UNVERIFIED"
    assert row.effective_month_end is None
    assert row.provenance_payload == {}


def test_account_owner_link_status_check_rejects_unknown(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(
            AdsenseContentOwnerLinkORM(
                tenant_id=TENANT, adsense_account_id="pub-1", content_owner_id="owner-1",
                verification_status="BOGUS", provenance_kind="OPERATOR_ASSERTED",
                effective_month_start="2026-01",
            )
        )
        session.commit()


def test_account_owner_link_range_check_rejects_end_before_start(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(
            AdsenseContentOwnerLinkORM(
                tenant_id=TENANT, adsense_account_id="pub-1", content_owner_id="owner-1",
                provenance_kind="OPERATOR_ASSERTED",
                effective_month_start="2026-06", effective_month_end="2026-01",
            )
        )
        session.commit()


def test_owner_channel_link_persists_active_default(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        session.add(
            ContentOwnerChannelLinkORM(
                tenant_id=TENANT, content_owner_id="owner-1", youtube_channel_id="chan-1",
                provenance_kind="SOURCE_ROW", provenance_source_id="row-1",
                effective_month_start="2026-04", effective_month_end="2026-04",
            )
        )
        session.commit()
        row = session.query(ContentOwnerChannelLinkORM).one()
    assert row.active is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/db/test_channel_account_map_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'AdsenseContentOwnerLinkORM'`.

- [ ] **Step 3: Add the month-format helper**

In `backend/ums_smart_revenue/db/finance_models.py`, after the `_TENANT_ID_DEFAULT_VALUE` definition (around line 39), add:
```python
def _month_format_check(column: str) -> str:
    """Return a SQLite/Postgres-portable CHECK expression validating YYYY-MM."""
    return (
        f"length({column}) = 7 AND substr({column}, 5, 1) = '-' "
        f"AND substr({column}, 1, 1) BETWEEN '0' AND '9' "
        f"AND substr({column}, 2, 1) BETWEEN '0' AND '9' "
        f"AND substr({column}, 3, 1) BETWEEN '0' AND '9' "
        f"AND substr({column}, 4, 1) BETWEEN '0' AND '9' "
        f"AND substr({column}, 6, 1) BETWEEN '0' AND '9' "
        f"AND substr({column}, 7, 1) BETWEEN '0' AND '9' "
        f"AND substr({column}, 6, 2) BETWEEN '01' AND '12'"
    )
```

- [ ] **Step 4: Add `Boolean` to the imports**

In the `from sqlalchemy import (...)` block at the top of `finance_models.py`, add `Boolean,` (alphabetical, before `CheckConstraint`).

- [ ] **Step 5: Add the two ORM classes**

Append to `backend/ums_smart_revenue/db/finance_models.py`:
```python
# ============================================================================
# Purpose: Operator-verified link between an AdSense publisher account and a
#   YouTube CMS content owner. Verification is the money-gating trust decision
#   the allocation engine (Spec 2b) consumes; effective ranges make historical
#   allocation reproducible.
# Database/ORM: adsense_content_owner_links / AdsenseContentOwnerLinkORM.
# Standards: tenant FK RESTRICT; status CHECK; YYYY-MM CHECKs; object-only
#   JSONB provenance CHECK (Postgres-only). No amounts; no auth/Neo4j schema.
# Blast Radius: Finance source-of-truth (new, additive). Read by Spec 2b.
# Connections:
#   - File: backend/ums_smart_revenue/finance/channel_account_links.py -> repo.
#   - File: Docs/superpowers/specs/2026-05-31-spec-channel-account-map-design.md
# ============================================================================
class AdsenseContentOwnerLinkORM(FinanceBase):
    """Operator-verified AdSense-account ↔ content-owner link."""

    __tablename__ = "adsense_content_owner_links"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE, server_default=_TENANT_ID_DEFAULT,
    )
    adsense_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    content_owner_id: Mapped[str] = mapped_column(Text, nullable=False)
    verification_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'UNVERIFIED'")
    )
    provenance_kind: Mapped[str] = mapped_column(Text, nullable=False)
    provenance_payload: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"),
        nullable=False, server_default=text("'{}'"),
    )
    verified_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verification_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_month_start: Mapped[str] = mapped_column(Text, nullable=False)
    effective_month_end: Mapped[str | None] = mapped_column(Text, nullable=True)
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
            name="fk_adsense_content_owner_links_tenant", ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id", "adsense_account_id", "content_owner_id",
            "effective_month_start", name="uq_adsense_content_owner_links_key",
        ),
        CheckConstraint(
            "verification_status IN ('UNVERIFIED', 'VERIFIED', 'REJECTED', 'CONFLICT')",
            name="ck_adsense_content_owner_links_status",
        ),
        CheckConstraint(
            _month_format_check("effective_month_start"),
            name="ck_adsense_content_owner_links_start_format",
        ),
        CheckConstraint(
            f"effective_month_end IS NULL OR ({_month_format_check('effective_month_end')})",
            name="ck_adsense_content_owner_links_end_format",
        ),
        CheckConstraint(
            "effective_month_end IS NULL OR effective_month_end >= effective_month_start",
            name="ck_adsense_content_owner_links_range",
        ),
        CheckConstraint(
            "length(adsense_account_id) >= 1",
            name="ck_adsense_content_owner_links_account_nonempty",
        ),
        CheckConstraint(
            "length(content_owner_id) >= 1",
            name="ck_adsense_content_owner_links_owner_nonempty",
        ),
        CheckConstraint(
            "jsonb_typeof(provenance_payload) = 'object'",
            name="ck_adsense_content_owner_links_provenance_payload_object",
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_adsense_content_owner_links_account_status",
            "tenant_id", "adsense_account_id", "verification_status",
        ),
    )


# ============================================================================
# Purpose: Derived link between a content owner and a YouTube channel, sourced
#   from observed (content_owner_id, youtube_channel_id) co-occurrence in
#   google_revenue_source_rows. Trusted by provenance; no human verification.
# Database/ORM: content_owner_channel_links / ContentOwnerChannelLinkORM.
# Standards: tenant FK RESTRICT; YYYY-MM CHECKs; idempotent upsert key. No
#   amounts; never derived from source_account_id.
# Blast Radius: Finance source-of-truth (new, additive). Read by Spec 2b.
# ============================================================================
class ContentOwnerChannelLinkORM(FinanceBase):
    """Derived content-owner ↔ YouTube-channel link."""

    __tablename__ = "content_owner_channel_links"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE, server_default=_TENANT_ID_DEFAULT,
    )
    content_owner_id: Mapped[str] = mapped_column(Text, nullable=False)
    youtube_channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    provenance_kind: Mapped[str] = mapped_column(Text, nullable=False)
    provenance_source_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    effective_month_start: Mapped[str] = mapped_column(Text, nullable=False)
    effective_month_end: Mapped[str | None] = mapped_column(Text, nullable=True)
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
            name="fk_content_owner_channel_links_tenant", ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id", "content_owner_id", "youtube_channel_id",
            "effective_month_start", name="uq_content_owner_channel_links_key",
        ),
        CheckConstraint(
            "provenance_kind IN ('SOURCE_ROW', 'CHANNEL_REGISTRY', 'MANUAL')",
            name="ck_content_owner_channel_links_provenance_kind",
        ),
        CheckConstraint(
            _month_format_check("effective_month_start"),
            name="ck_content_owner_channel_links_start_format",
        ),
        CheckConstraint(
            f"effective_month_end IS NULL OR ({_month_format_check('effective_month_end')})",
            name="ck_content_owner_channel_links_end_format",
        ),
        CheckConstraint(
            "effective_month_end IS NULL OR effective_month_end >= effective_month_start",
            name="ck_content_owner_channel_links_range",
        ),
        Index(
            "ix_content_owner_channel_links_owner",
            "tenant_id", "content_owner_id", "effective_month_start",
        ),
    )
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/db/test_channel_account_map_models.py -q`
Expected: PASS (4 tests).

- [ ] **Step 7: Commit**

```bash
git add backend/ums_smart_revenue/db/finance_models.py tests/db/test_channel_account_map_models.py
git commit -m "feat(db): channel-account map ORM tables (account-owner + owner-channel)"
```

---

### Task 3: Alembic migration

**Files:**
- Create: `backend/ums_smart_revenue/db/alembic/versions/20260531_0001_channel_account_map.py`
- Test: `tests/db/test_channel_account_map_migration_postgres.py`

- [ ] **Step 1: Write the failing test**

Create `tests/db/test_channel_account_map_migration_postgres.py`:
```python
"""PostgreSQL round-trip for 20260531_0001 (channel-account map tables)."""
from pathlib import Path

from _postgres_helpers import require_postgres_url
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

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


def test_upgrade_creates_both_tables_with_constraints(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    inspector = inspect(fresh_engine)
    assert "adsense_content_owner_links" in inspector.get_table_names()
    assert "content_owner_channel_links" in inspector.get_table_names()
    checks = {
        c["name"] for c in inspector.get_check_constraints("adsense_content_owner_links")
    }
    assert "ck_adsense_content_owner_links_status" in checks
    assert "ck_adsense_content_owner_links_range" in checks
    assert "ck_adsense_content_owner_links_provenance_payload_object" in checks
    uniques = {
        c["name"]: tuple(c["column_names"])
        for c in inspector.get_unique_constraints("adsense_content_owner_links")
    }
    assert uniques["uq_adsense_content_owner_links_key"] == (
        "tenant_id", "adsense_account_id", "content_owner_id", "effective_month_start",
    )
    fks = {c["name"] for c in inspector.get_foreign_keys("content_owner_channel_links")}
    assert "fk_content_owner_channel_links_tenant" in fks


def test_downgrade_drops_both_tables(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "20260529_0002")
    inspector = inspect(fresh_engine)
    assert "adsense_content_owner_links" not in inspector.get_table_names()
    assert "content_owner_channel_links" not in inspector.get_table_names()


def test_round_trip_idempotency(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "20260529_0002")
    command.upgrade(alembic_config, "head")
    assert "adsense_content_owner_links" in inspect(fresh_engine).get_table_names()


def test_provenance_payload_object_check_rejects_array(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    insert_sql = text(
        "INSERT INTO adsense_content_owner_links "
        "(tenant_id, adsense_account_id, content_owner_id, provenance_kind, "
        "provenance_payload, effective_month_start) VALUES "
        "(:tenant, 'pub-1', 'owner-1', 'OPERATOR_ASSERTED', '[]'::jsonb, '2026-01')"
    )
    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(insert_sql, {"tenant": UMS_TENANT_ID})
```

- [ ] **Step 2: Run test to verify it fails**

Start the disposable Postgres container (see Validation gate), then:
Run: `set UMS_TEST_POSTGRES_URL=postgresql+psycopg://postgres:ums@localhost:55432/test_ums && pytest tests/db/test_channel_account_map_migration_postgres.py -q` (PowerShell: `$env:UMS_TEST_POSTGRES_URL="postgresql+psycopg://postgres:ums@localhost:55432/test_ums"`)
Expected: FAIL — `KeyError`/`alembic ... Can't locate revision` (migration file absent) or table-not-found.

> Note: confirm the env var name `require_postgres_url()` reads by opening `tests/db/_postgres_helpers.py` (it is the same helper `test_deduction_components_migration_postgres.py` uses). Use exactly that variable.

- [ ] **Step 3: Write the migration**

Create `backend/ums_smart_revenue/db/alembic/versions/20260531_0001_channel_account_map.py`:
```python
"""Create channel-account map tables (account-owner + owner-channel links).

Revision ID: 20260531_0001
Revises: 20260529_0002
Create Date: 2026-05-31

Spec: Docs/superpowers/specs/2026-05-31-spec-channel-account-map-design.md
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260531_0001"
down_revision = "20260529_0002"
branch_labels = None
depends_on = None

UMS_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def _month_format(column: str) -> str:
    return (
        f"length({column}) = 7 AND substr({column}, 5, 1) = '-' "
        f"AND substr({column}, 1, 1) BETWEEN '0' AND '9' "
        f"AND substr({column}, 2, 1) BETWEEN '0' AND '9' "
        f"AND substr({column}, 3, 1) BETWEEN '0' AND '9' "
        f"AND substr({column}, 4, 1) BETWEEN '0' AND '9' "
        f"AND substr({column}, 6, 1) BETWEEN '0' AND '9' "
        f"AND substr({column}, 7, 1) BETWEEN '0' AND '9' "
        f"AND substr({column}, 6, 2) BETWEEN '01' AND '12'"
    )


# ============================================================================
# Purpose: Create the two channel-account map tables. account-owner links carry
#   the operator-verified trust decision; owner-channel links are derived.
# Database/ORM: adsense_content_owner_links / content_owner_channel_links.
# Standards: object JSONB CHECK is Postgres-only (dialect guard), mirroring
#   deduction_components. Downgrade drops indexes then tables.
# Blast Radius: Finance source-of-truth (additive). No auth/Neo4j schema impact.
# ============================================================================
def upgrade() -> None:
    """Create both map tables with constraints and indexes."""
    op.create_table(
        "adsense_content_owner_links",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Uuid(), nullable=False, server_default=sa.text(f"'{UMS_TENANT_ID}'")),
        sa.Column("adsense_account_id", sa.Text(), nullable=False),
        sa.Column("content_owner_id", sa.Text(), nullable=False),
        sa.Column("verification_status", sa.Text(), nullable=False, server_default=sa.text("'UNVERIFIED'")),
        sa.Column("provenance_kind", sa.Text(), nullable=False),
        sa.Column(
            "provenance_payload",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False, server_default=sa.text("'{}'"),
        ),
        sa.Column("verified_by", sa.Uuid(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_reason", sa.Text(), nullable=True),
        sa.Column("effective_month_start", sa.Text(), nullable=False),
        sa.Column("effective_month_end", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "tenant_id", "adsense_account_id", "content_owner_id", "effective_month_start",
            name="uq_adsense_content_owner_links_key",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_adsense_content_owner_links_tenant", ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "verification_status IN ('UNVERIFIED', 'VERIFIED', 'REJECTED', 'CONFLICT')",
            name="ck_adsense_content_owner_links_status",
        ),
        sa.CheckConstraint(_month_format("effective_month_start"), name="ck_adsense_content_owner_links_start_format"),
        sa.CheckConstraint(
            f"effective_month_end IS NULL OR ({_month_format('effective_month_end')})",
            name="ck_adsense_content_owner_links_end_format",
        ),
        sa.CheckConstraint(
            "effective_month_end IS NULL OR effective_month_end >= effective_month_start",
            name="ck_adsense_content_owner_links_range",
        ),
        sa.CheckConstraint("length(adsense_account_id) >= 1", name="ck_adsense_content_owner_links_account_nonempty"),
        sa.CheckConstraint("length(content_owner_id) >= 1", name="ck_adsense_content_owner_links_owner_nonempty"),
    )
    op.create_table(
        "content_owner_channel_links",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Uuid(), nullable=False, server_default=sa.text(f"'{UMS_TENANT_ID}'")),
        sa.Column("content_owner_id", sa.Text(), nullable=False),
        sa.Column("youtube_channel_id", sa.Text(), nullable=False),
        sa.Column("provenance_kind", sa.Text(), nullable=False),
        sa.Column("provenance_source_id", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("effective_month_start", sa.Text(), nullable=False),
        sa.Column("effective_month_end", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "tenant_id", "content_owner_id", "youtube_channel_id", "effective_month_start",
            name="uq_content_owner_channel_links_key",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_content_owner_channel_links_tenant", ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "provenance_kind IN ('SOURCE_ROW', 'CHANNEL_REGISTRY', 'MANUAL')",
            name="ck_content_owner_channel_links_provenance_kind",
        ),
        sa.CheckConstraint(_month_format("effective_month_start"), name="ck_content_owner_channel_links_start_format"),
        sa.CheckConstraint(
            f"effective_month_end IS NULL OR ({_month_format('effective_month_end')})",
            name="ck_content_owner_channel_links_end_format",
        ),
        sa.CheckConstraint(
            "effective_month_end IS NULL OR effective_month_end >= effective_month_start",
            name="ck_content_owner_channel_links_range",
        ),
    )
    # Postgres-only object guard (invalid SQLite CREATE syntax), mirroring
    # finance_models.py .ddl_if(dialect="postgresql").
    if op.get_bind().dialect.name == "postgresql":
        op.create_check_constraint(
            "ck_adsense_content_owner_links_provenance_payload_object",
            "adsense_content_owner_links",
            "jsonb_typeof(provenance_payload) = 'object'",
        )
    op.create_index(
        "ix_adsense_content_owner_links_account_status",
        "adsense_content_owner_links",
        ["tenant_id", "adsense_account_id", "verification_status"],
    )
    op.create_index(
        "ix_content_owner_channel_links_owner",
        "content_owner_channel_links",
        ["tenant_id", "content_owner_id", "effective_month_start"],
    )


def downgrade() -> None:
    """Drop both map tables and their indexes."""
    op.drop_index("ix_content_owner_channel_links_owner", table_name="content_owner_channel_links")
    op.drop_index("ix_adsense_content_owner_links_account_status", table_name="adsense_content_owner_links")
    op.drop_table("content_owner_channel_links")
    op.drop_table("adsense_content_owner_links")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/db/test_channel_account_map_migration_postgres.py -q` (with `UMS_TEST_POSTGRES_URL` set)
Expected: PASS (4 tests).

- [ ] **Step 5: Confirm the SQLite model path still matches the migration**

Run: `pytest tests/db/test_channel_account_map_models.py -q`
Expected: PASS (FinanceBase metadata and migration agree on columns/CHECKs).

- [ ] **Step 6: Commit**

```bash
git add backend/ums_smart_revenue/db/alembic/versions/20260531_0001_channel_account_map.py tests/db/test_channel_account_map_migration_postgres.py
git commit -m "feat(db): migration for channel-account map tables"
```

---

### Task 4: Read model, typed errors, and validation helpers

**Files:**
- Create: `backend/ums_smart_revenue/finance/channel_account_links.py`
- Test: `tests/finance/test_channel_account_links.py`

- [ ] **Step 1: Write the failing test**

Create `tests/finance/test_channel_account_links.py`:
```python
"""Channel↔account map: repository + helpers (SQLite unit tier).

NOTE — imports accrete across Tasks 4–10 in this file. Add each symbol in the
task that first uses it so every commit stays ruff-clean (no unused imports):
  Task 5 → `import pytest`; `from sqlalchemy.orm import Session`;
           add `SqlAlchemyChannelAccountLinkRepository, _account_owner_lock_key,
           _ranges_overlap` to the channel_account_links import.
  Task 6 → add `ChannelAccountLinkValidationError`.
  Task 7 → add `ChannelAccountLinkConflictError, ChannelAccountLinkNotFoundError`;
           `from datetime import UTC, datetime`.
  Task 9 → `from sqlalchemy import select`;
           `from ums_smart_revenue.db.finance_models import
           (AdsenseContentOwnerLinkORM, ContentOwnerChannelLinkORM)`;
           `from ums_smart_revenue.db.source_models import GoogleRevenueSourceRowORM`.
"""
from uuid import UUID, uuid4

from sqlalchemy import create_engine

from ums_smart_revenue.db.finance_models import FinanceBase
from ums_smart_revenue.finance.channel_account_links import AccountOwnerLink
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

TENANT = UUID(UMS_TENANT_ID)
VERIFIER = UUID("00000000-0000-0000-0000-0000000c0001")


def _engine(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}")
    FinanceBase.metadata.create_all(engine)
    return engine


def test_to_api_excludes_provenance_payload():
    link = AccountOwnerLink(
        id="x", adsense_account_id="pub-1", content_owner_id="owner-1",
        verification_status="UNVERIFIED", provenance_kind="OPERATOR_ASSERTED",
        provenance_payload={"secret": "LEAK"}, verified_by=None, verified_at=None,
        verification_reason=None, effective_month_start="2026-01",
        effective_month_end=None,
    )
    api = link.to_api()
    assert "provenance_payload" not in api
    assert "LEAK" not in str(api)
    assert api["adsense_account_id"] == "pub-1"
    assert api["verification_status"] == "UNVERIFIED"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/finance/test_channel_account_links.py::test_to_api_excludes_provenance_payload -q`
Expected: FAIL — `ModuleNotFoundError: ums_smart_revenue.finance.channel_account_links`.

- [ ] **Step 3: Create the module with read model, errors, helpers**

Create `backend/ums_smart_revenue/finance/channel_account_links.py`:
```python
"""Channel↔account map: read models, typed errors, and the repository.

Two layers: operator-verified adsense_account_id ↔ content_owner_id, and derived
content_owner_id ↔ youtube_channel_id. The repository exposes propose/verify/
reject (verify guarded by a per-account advisory lock + fail-closed overlap
check), an idempotent source-row derivation, and the verified read contract that
the allocation engine (Spec 2b) consumes.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    ContentOwnerChannelLinkORM,
)
from ums_smart_revenue.db.source_models import GoogleRevenueSourceRowORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)
_MONTH_LENGTH = 7
_OPEN_END = "9999-12"  # sentinel for open-ended ranges in overlap comparison


class ChannelAccountLinkError(ValueError):
    """Base error for channel-account map operations."""


class ChannelAccountLinkValidationError(ChannelAccountLinkError):
    """Raised on malformed input (month, bounds, status)."""


class ChannelAccountLinkConflictError(ChannelAccountLinkError):
    """Raised when verifying would overlap an existing VERIFIED link."""


class ChannelAccountLinkNotFoundError(ChannelAccountLinkError):
    """Raised when a link id does not exist for the tenant."""


@dataclass(frozen=True)
class AccountOwnerLink:
    """Account↔owner link read model. provenance_payload is never serialized."""

    id: str
    adsense_account_id: str
    content_owner_id: str
    verification_status: str
    provenance_kind: str
    provenance_payload: dict[str, object]
    verified_by: str | None
    verified_at: datetime | None
    verification_reason: str | None
    effective_month_start: str
    effective_month_end: str | None

    def to_api(self) -> dict[str, object]:
        """Return the API payload, excluding raw provenance_payload."""
        # provenance_payload is intentionally omitted (raw evidence; see spec §3).
        return {
            "id": self.id,
            "adsense_account_id": self.adsense_account_id,
            "content_owner_id": self.content_owner_id,
            "verification_status": self.verification_status,
            "provenance_kind": self.provenance_kind,
            "verified_by": self.verified_by,
            "verified_at": (
                None if self.verified_at is None else self.verified_at.isoformat()
            ),
            "verification_reason": self.verification_reason,
            "effective_month_start": self.effective_month_start,
            "effective_month_end": self.effective_month_end,
        }


@dataclass(frozen=True)
class AccountOwnerLinkPage:
    """One page of account-owner links plus the full-match count."""

    total_count: int
    links: list[AccountOwnerLink]


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    """Resolve explicit, ambient, or default tenant UUID for repository scoping.

    Raises:
        ChannelAccountLinkValidationError: If an explicit tenant id is invalid.
    """
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
        raise ChannelAccountLinkValidationError(f"invalid tenant_id: {tenant_id!r}") from exc


def _validate_month(month: str) -> None:
    """Validate YYYY-MM month input.

    Raises:
        ChannelAccountLinkValidationError: If the month is malformed.
    """
    if len(month) != _MONTH_LENGTH or month[4] != "-":
        raise ChannelAccountLinkValidationError("month must use YYYY-MM")
    year, mm = month[:4], month[5:]
    if not (
        all("0" <= char <= "9" for char in year)
        and all("0" <= char <= "9" for char in mm)
        and 1 <= int(mm) <= 12
    ):
        raise ChannelAccountLinkValidationError("month must use YYYY-MM")


def _ranges_overlap(
    start_a: str, end_a: str | None, start_b: str, end_b: str | None
) -> bool:
    """Return True if two YYYY-MM ranges overlap (None end = open-ended)."""
    ea = end_a if end_a is not None else _OPEN_END
    eb = end_b if end_b is not None else _OPEN_END
    return start_a <= eb and start_b <= ea


def _account_owner_lock_key(tenant_id: UUID, adsense_account_id: str) -> int:
    """Return a stable signed-bigint advisory-lock key for one (tenant, account).

    Mirrors connectors/google_source_rows/repository.py: blake2b of a \\0-joined
    discriminator, shifted into the positive signed-bigint range. Never includes
    payload, amounts, or credentials.
    """
    payload = (
        f"adsense_content_owner_links\0{tenant_id}\0{adsense_account_id}"
    ).encode()
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return int.from_bytes(digest, "big") >> 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/finance/test_channel_account_links.py::test_to_api_excludes_provenance_payload -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/channel_account_links.py tests/finance/test_channel_account_links.py
git commit -m "feat(finance): channel-account map read model, errors, helpers"
```

---

### Task 5: Advisory-lock key + overlap predicate tests (Mahmoud requirement)

**Files:**
- Test: `tests/finance/test_channel_account_links.py` (add)

- [ ] **Step 1: Write the failing tests**

Append to `tests/finance/test_channel_account_links.py`:
```python
def test_lock_key_is_deterministic_and_signed_bigint():
    a = _account_owner_lock_key(TENANT, "pub-1")
    b = _account_owner_lock_key(TENANT, "pub-1")
    assert a == b
    assert 0 <= a < 2**63  # fits PostgreSQL signed bigint


def test_lock_key_differs_by_account_and_tenant():
    other_tenant = UUID("00000000-0000-0000-0000-0000000c00ff")
    assert _account_owner_lock_key(TENANT, "pub-1") != _account_owner_lock_key(TENANT, "pub-2")
    assert _account_owner_lock_key(TENANT, "pub-1") != _account_owner_lock_key(other_tenant, "pub-1")


@pytest.mark.parametrize(
    ("sa", "ea", "sb", "eb", "expected"),
    [
        ("2026-01", "2026-06", "2026-03", None, True),    # B open, overlaps tail
        ("2026-01", "2026-02", "2026-03", "2026-04", False),  # disjoint
        ("2026-01", None, "2030-01", "2030-02", True),     # A open, covers everything later
        ("2026-05", "2026-05", "2026-05", "2026-05", True),   # same single month
        ("2026-01", "2026-02", "2026-02", "2026-03", True),   # touch at 2026-02
    ],
)
def test_ranges_overlap_truth_table(sa, ea, sb, eb, expected):
    assert _ranges_overlap(sa, ea, sb, eb) is expected


def test_acquire_lock_is_noop_on_sqlite(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        # On SQLite this must return without error (no advisory-lock primitive).
        repo._acquire_account_owner_lock("pub-1")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/finance/test_channel_account_links.py -q -k "lock_key or ranges_overlap or noop"`
Expected: FAIL — `SqlAlchemyChannelAccountLinkRepository` not defined; `_acquire_account_owner_lock` missing.

- [ ] **Step 3: Add the repository skeleton + lock helper**

Append to `backend/ums_smart_revenue/finance/channel_account_links.py`:
```python
class SqlAlchemyChannelAccountLinkRepository:
    """Tenant-scoped storage for the channel↔account map."""

    # ========================================================================
    # Purpose: Manage account↔owner link lifecycle (propose/verify/reject) with
    #   a per-account advisory lock guarding a fail-closed overlap invariant,
    #   derive owner↔channel links from source rows, and serve the verified
    #   read contract for allocation.
    # Database/ORM: adsense_content_owner_links, content_owner_channel_links,
    #   read-only google_revenue_source_rows.
    # Standards: tenant-explicit; pg_advisory_xact_lock on PostgreSQL (SQLite
    #   no-op); typed errors; reads never write.
    # Blast Radius: Finance source-of-truth writes (new tables). No Neo4j.
    # ========================================================================
    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    def _acquire_account_owner_lock(self, adsense_account_id: str) -> None:
        """Serialize verify/reject for one account via a transaction advisory lock.

        PostgreSQL-only; SQLite has no comparable primitive and no-ops (unit
        tests run serially, so the overlap check is still exercised).
        """
        if self._session.get_bind().dialect.name != "postgresql":
            return
        lock_key = _account_owner_lock_key(self._tenant_id, adsense_account_id)
        self._session.execute(select(func.pg_advisory_xact_lock(lock_key)))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/finance/test_channel_account_links.py -q -k "lock_key or ranges_overlap or noop"`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/channel_account_links.py tests/finance/test_channel_account_links.py
git commit -m "feat(finance): per-account advisory-lock key + overlap predicate"
```

---

### Task 6: `propose_account_owner_link`

**Files:**
- Modify: `backend/ums_smart_revenue/finance/channel_account_links.py`
- Test: `tests/finance/test_channel_account_links.py` (add)

- [ ] **Step 1: Write the failing test**

Append to `tests/finance/test_channel_account_links.py`:
```python
def test_propose_creates_unverified_link(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        link = repo.propose_account_owner_link(
            adsense_account_id="pub-1", content_owner_id="owner-1",
            effective_month_start="2026-01", effective_month_end=None,
            provenance_kind="OPERATOR_ASSERTED", provenance_payload={"note": "manual"},
        )
        session.commit()
    assert link.verification_status == "UNVERIFIED"
    assert link.adsense_account_id == "pub-1"
    assert link.effective_month_end is None


def test_propose_rejects_bad_month(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        with pytest.raises(ChannelAccountLinkValidationError):
            repo.propose_account_owner_link(
                adsense_account_id="pub-1", content_owner_id="owner-1",
                effective_month_start="2026-13", effective_month_end=None,
                provenance_kind="OPERATOR_ASSERTED", provenance_payload={},
            )
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/finance/test_channel_account_links.py -q -k propose`
Expected: FAIL — `propose_account_owner_link` missing.

- [ ] **Step 3: Implement `propose_account_owner_link` + `_to_account_owner_link`**

Append inside `SqlAlchemyChannelAccountLinkRepository`:
```python
    def propose_account_owner_link(
        self, *,
        adsense_account_id: str,
        content_owner_id: str,
        effective_month_start: str,
        effective_month_end: str | None,
        provenance_kind: str,
        provenance_payload: dict[str, object],
    ) -> AccountOwnerLink:
        """Insert an UNVERIFIED account↔owner candidate.

        Raises:
            ChannelAccountLinkValidationError: If a month is malformed or end <
                start.
        """
        _validate_month(effective_month_start)
        if effective_month_end is not None:
            _validate_month(effective_month_end)
            if effective_month_end < effective_month_start:
                raise ChannelAccountLinkValidationError(
                    "effective_month_end must be >= effective_month_start"
                )
        row = AdsenseContentOwnerLinkORM(
            tenant_id=self._tenant_id,
            adsense_account_id=adsense_account_id,
            content_owner_id=content_owner_id,
            verification_status="UNVERIFIED",
            provenance_kind=provenance_kind,
            provenance_payload=dict(provenance_payload or {}),
            effective_month_start=effective_month_start,
            effective_month_end=effective_month_end,
        )
        self._session.add(row)
        self._session.flush()
        return self._to_account_owner_link(row)

    @staticmethod
    def _to_account_owner_link(row: AdsenseContentOwnerLinkORM) -> AccountOwnerLink:
        """Convert an ORM row into the read-model dataclass."""
        return AccountOwnerLink(
            id=str(row.id),
            adsense_account_id=row.adsense_account_id,
            content_owner_id=row.content_owner_id,
            verification_status=row.verification_status,
            provenance_kind=row.provenance_kind,
            provenance_payload=dict(row.provenance_payload or {}),
            verified_by=None if row.verified_by is None else str(row.verified_by),
            verified_at=row.verified_at,
            verification_reason=row.verification_reason,
            effective_month_start=row.effective_month_start,
            effective_month_end=row.effective_month_end,
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/finance/test_channel_account_links.py -q -k propose`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/channel_account_links.py tests/finance/test_channel_account_links.py
git commit -m "feat(finance): propose account-owner link (UNVERIFIED)"
```

---

### Task 7: `verify_account_owner_link` + `reject_account_owner_link` (lock + overlap)

**Files:**
- Modify: `backend/ums_smart_revenue/finance/channel_account_links.py`
- Test: `tests/finance/test_channel_account_links.py` (add)

- [ ] **Step 1: Write the failing tests**

Append to `tests/finance/test_channel_account_links.py`:
```python
def _propose(repo, *, account="pub-1", owner="owner-1", start="2026-01", end=None):
    return repo.propose_account_owner_link(
        adsense_account_id=account, content_owner_id=owner,
        effective_month_start=start, effective_month_end=end,
        provenance_kind="OPERATOR_ASSERTED", provenance_payload={},
    )


def test_verify_marks_verified_and_stamps(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        link = _propose(repo)
        out = repo.verify_account_owner_link(
            link.id, verified_by=VERIFIER, reason="confirmed via contract"
        )
        session.commit()
    assert out.verification_status == "VERIFIED"
    assert out.verified_by == str(VERIFIER)
    assert out.verification_reason == "confirmed via contract"
    assert out.verified_at is not None


def test_verify_overlapping_verified_raises_conflict(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        first = _propose(repo, owner="owner-1", start="2026-01", end="2026-06")
        repo.verify_account_owner_link(first.id, verified_by=VERIFIER, reason="r1")
        second = _propose(repo, owner="owner-2", start="2026-03", end=None)
        with pytest.raises(ChannelAccountLinkConflictError):
            repo.verify_account_owner_link(second.id, verified_by=VERIFIER, reason="r2")


def test_verify_non_overlapping_succeeds(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        first = _propose(repo, owner="owner-1", start="2026-01", end="2026-06")
        repo.verify_account_owner_link(first.id, verified_by=VERIFIER, reason="r1")
        second = _propose(repo, owner="owner-2", start="2026-07", end=None)
        out = repo.verify_account_owner_link(second.id, verified_by=VERIFIER, reason="r2")
    assert out.verification_status == "VERIFIED"


def test_reject_then_reverify_competitor_succeeds(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        first = _propose(repo, owner="owner-1", start="2026-01", end=None)
        repo.verify_account_owner_link(first.id, verified_by=VERIFIER, reason="r1")
        second = _propose(repo, owner="owner-2", start="2026-01", end=None)
        repo.reject_account_owner_link(first.id, verified_by=VERIFIER, reason="superseded")
        out = repo.verify_account_owner_link(second.id, verified_by=VERIFIER, reason="r2")
    assert out.verification_status == "VERIFIED"


def test_verify_missing_id_raises_not_found(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        with pytest.raises(ChannelAccountLinkNotFoundError):
            repo.verify_account_owner_link(str(uuid4()), verified_by=VERIFIER, reason="x")
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/finance/test_channel_account_links.py -q -k "verify or reject"`
Expected: FAIL — `verify_account_owner_link` / `reject_account_owner_link` missing.

- [ ] **Step 3: Implement verify/reject + the private transition helper**

Append inside `SqlAlchemyChannelAccountLinkRepository`:
```python
    def _load_owned(self, link_id: str) -> AdsenseContentOwnerLinkORM:
        """Load a tenant-owned account↔owner row by id or raise NotFound."""
        try:
            uuid_id = UUID(str(link_id))
        except ValueError as exc:
            raise ChannelAccountLinkNotFoundError(f"unknown link: {link_id!r}") from exc
        row = self._session.scalars(
            select(AdsenseContentOwnerLinkORM).where(
                AdsenseContentOwnerLinkORM.id == uuid_id,
                AdsenseContentOwnerLinkORM.tenant_id == self._tenant_id,
            )
        ).one_or_none()
        if row is None:
            raise ChannelAccountLinkNotFoundError(f"unknown link: {link_id!r}")
        return row

    def verify_account_owner_link(
        self, link_id: str, *, verified_by: UUID, reason: str
    ) -> AccountOwnerLink:
        """Transition a link to VERIFIED, enforcing the per-account overlap invariant.

        Acquires the per-account advisory lock first so concurrent verifies for
        one account cannot both commit overlapping VERIFIED rows.

        Raises:
            ChannelAccountLinkNotFoundError: If the link id is unknown.
            ChannelAccountLinkConflictError: If a VERIFIED link already overlaps.
        """
        row = self._load_owned(link_id)
        self._acquire_account_owner_lock(row.adsense_account_id)
        existing = self._session.scalars(
            select(AdsenseContentOwnerLinkORM).where(
                AdsenseContentOwnerLinkORM.tenant_id == self._tenant_id,
                AdsenseContentOwnerLinkORM.adsense_account_id == row.adsense_account_id,
                AdsenseContentOwnerLinkORM.verification_status == "VERIFIED",
                AdsenseContentOwnerLinkORM.id != row.id,
            )
        ).all()
        for other in existing:
            if _ranges_overlap(
                row.effective_month_start, row.effective_month_end,
                other.effective_month_start, other.effective_month_end,
            ):
                raise ChannelAccountLinkConflictError(
                    "a verified link already covers an overlapping month range "
                    f"for account {row.adsense_account_id}"
                )
        row.verification_status = "VERIFIED"
        row.verified_by = verified_by
        row.verified_at = datetime.now(UTC)
        row.verification_reason = reason
        self._session.flush()
        return self._to_account_owner_link(row)

    def reject_account_owner_link(
        self, link_id: str, *, verified_by: UUID, reason: str
    ) -> AccountOwnerLink:
        """Transition a link to REJECTED (money-affecting; same gate as verify).

        Raises:
            ChannelAccountLinkNotFoundError: If the link id is unknown.
        """
        row = self._load_owned(link_id)
        self._acquire_account_owner_lock(row.adsense_account_id)
        row.verification_status = "REJECTED"
        row.verified_by = verified_by
        row.verified_at = datetime.now(UTC)
        row.verification_reason = reason
        self._session.flush()
        return self._to_account_owner_link(row)
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/finance/test_channel_account_links.py -q -k "verify or reject"`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/channel_account_links.py tests/finance/test_channel_account_links.py
git commit -m "feat(finance): verify/reject account-owner link with overlap guard"
```

---

### Task 8: `list_account_owner_links` (paginated, fail-closed)

**Files:**
- Modify: `backend/ums_smart_revenue/finance/channel_account_links.py`
- Test: `tests/finance/test_channel_account_links.py` (add)

- [ ] **Step 1: Write the failing tests**

Append to `tests/finance/test_channel_account_links.py`:
```python
def test_list_filters_paginates_and_counts(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        _propose(repo, account="pub-1", owner="owner-1", start="2026-01")
        _propose(repo, account="pub-1", owner="owner-2", start="2026-02")
        _propose(repo, account="pub-2", owner="owner-9", start="2026-01")
        session.flush()
        page = repo.list_account_owner_links(
            adsense_account_id="pub-1", limit=1, offset=0
        )
        all_pub1 = repo.list_account_owner_links(adsense_account_id="pub-1", limit=50, offset=0)
    assert page.total_count == 2
    assert len(page.links) == 1
    assert {link.adsense_account_id for link in all_pub1.links} == {"pub-1"}


def test_list_status_and_month_filter(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        a = _propose(repo, account="pub-1", owner="owner-1", start="2026-01", end="2026-03")
        _propose(repo, account="pub-1", owner="owner-2", start="2026-08", end=None)
        repo.verify_account_owner_link(a.id, verified_by=VERIFIER, reason="r")
        session.flush()
        verified = repo.list_account_owner_links(status="VERIFIED", limit=50, offset=0)
        feb = repo.list_account_owner_links(month="2026-02", limit=50, offset=0)
    assert {link.verification_status for link in verified.links} == {"VERIFIED"}
    assert all(link.effective_month_start <= "2026-02" for link in feb.links)
    assert feb.total_count == 1


def test_list_rejects_bad_bounds(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        with pytest.raises(ChannelAccountLinkValidationError, match="limit"):
            repo.list_account_owner_links(limit=0, offset=0)
        with pytest.raises(ChannelAccountLinkValidationError, match="offset"):
            repo.list_account_owner_links(limit=10, offset=-1)


def test_list_rejects_bad_month(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        with pytest.raises(ChannelAccountLinkValidationError):
            repo.list_account_owner_links(month="2026-13", limit=10, offset=0)
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/finance/test_channel_account_links.py -q -k list`
Expected: FAIL — `list_account_owner_links` missing.

- [ ] **Step 3: Implement `list_account_owner_links`**

Append inside `SqlAlchemyChannelAccountLinkRepository`:
```python
    def list_account_owner_links(
        self, *,
        status: str | None = None,
        adsense_account_id: str | None = None,
        content_owner_id: str | None = None,
        month: str | None = None,
        limit: int,
        offset: int,
    ) -> AccountOwnerLinkPage:
        """Return a filtered, paginated page of account↔owner links + full count.

        ``month`` filters to links valid for that month (start <= month <=
        coalesce(end, month)).

        Raises:
            ChannelAccountLinkValidationError: If month is malformed, limit < 1,
                or offset < 0.
        """
        if limit < 1:
            raise ChannelAccountLinkValidationError("limit must be >= 1")
        if offset < 0:
            raise ChannelAccountLinkValidationError("offset must be >= 0")
        filters = [AdsenseContentOwnerLinkORM.tenant_id == self._tenant_id]
        if status is not None:
            filters.append(AdsenseContentOwnerLinkORM.verification_status == status)
        if adsense_account_id is not None:
            filters.append(AdsenseContentOwnerLinkORM.adsense_account_id == adsense_account_id)
        if content_owner_id is not None:
            filters.append(AdsenseContentOwnerLinkORM.content_owner_id == content_owner_id)
        if month is not None:
            _validate_month(month)
            filters.append(AdsenseContentOwnerLinkORM.effective_month_start <= month)
            filters.append(
                (AdsenseContentOwnerLinkORM.effective_month_end.is_(None))
                | (AdsenseContentOwnerLinkORM.effective_month_end >= month)
            )
        total_count = self._session.scalar(
            select(func.count()).select_from(AdsenseContentOwnerLinkORM).where(*filters)
        )
        rows = self._session.scalars(
            select(AdsenseContentOwnerLinkORM)
            .where(*filters)
            .order_by(
                AdsenseContentOwnerLinkORM.adsense_account_id,
                AdsenseContentOwnerLinkORM.content_owner_id,
                AdsenseContentOwnerLinkORM.effective_month_start,
            )
            .limit(limit)
            .offset(offset)
        ).all()
        return AccountOwnerLinkPage(
            total_count=int(total_count or 0),
            links=[self._to_account_owner_link(row) for row in rows],
        )
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/finance/test_channel_account_links.py -q -k list`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/channel_account_links.py tests/finance/test_channel_account_links.py
git commit -m "feat(finance): list account-owner links (filtered, paginated, fail-closed)"
```

---

### Task 9: `upsert_owner_channel_links_from_source` (idempotent derivation)

**Files:**
- Modify: `backend/ums_smart_revenue/finance/channel_account_links.py`
- Test: `tests/finance/test_channel_account_links.py` (add)

- [ ] **Step 1: Write the failing tests**

Append to `tests/finance/test_channel_account_links.py`:
```python
def _source_row(session, *, owner, channel, account="pub-x", month="2026-04", key="k1"):
    session.add(
        GoogleRevenueSourceRowORM(
            id=uuid4(), tenant_id=TENANT, source_system="youtube_reporting",
            source_row_key=key, source_account_id=account, content_owner_id=owner,
            youtube_channel_id=channel, report_month=month,
            period_start=datetime(2026, 4, 1, tzinfo=UTC).date(),
            period_end=datetime(2026, 4, 30, tzinfo=UTC).date(),
            metric_key="estimated_partner_revenue", value_kind="revenue",
            amount_native=0, currency_code="USD", raw_payload={},
        )
    )


def test_derivation_only_uses_rows_with_both_owner_and_channel(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _source_row(session, owner="owner-1", channel="chan-1", key="k1")
        _source_row(session, owner=None, channel="chan-2", key="k2")     # no owner
        _source_row(session, owner="owner-3", channel=None, key="k3")    # no channel
        session.commit()
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        created = repo.upsert_owner_channel_links_from_source()
        session.commit()
        rows = session.scalars(select(ContentOwnerChannelLinkORM)).all()
    assert created == 1
    assert len(rows) == 1
    assert rows[0].content_owner_id == "owner-1"
    assert rows[0].youtube_channel_id == "chan-1"
    assert rows[0].provenance_kind == "SOURCE_ROW"
    assert rows[0].effective_month_start == "2026-04"
    assert rows[0].effective_month_end == "2026-04"


def test_derivation_is_idempotent(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _source_row(session, owner="owner-1", channel="chan-1", key="k1")
        session.commit()
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        repo.upsert_owner_channel_links_from_source()
        session.commit()
        again = repo.upsert_owner_channel_links_from_source()
        session.commit()
        rows = session.scalars(select(ContentOwnerChannelLinkORM)).all()
    assert again == 0
    assert len(rows) == 1
```

> Column note: `GoogleRevenueSourceRowORM` has NOT NULL columns and CHECK
> constraints (e.g. on `metric_key`/`value_kind`/`report_month`). Before running,
> open `backend/ums_smart_revenue/db/source_models.py` and confirm the `_source_row`
> helper sets every NOT NULL column with a CHECK-valid value (mirror an existing
> source-row construction in `tests/db/test_source_models.py` if a CHECK rejects a
> value). The derivation itself reads only `content_owner_id`, `youtube_channel_id`,
> `report_month`, `source_row_key`, `tenant_id`.

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/finance/test_channel_account_links.py -q -k derivation`
Expected: FAIL — `upsert_owner_channel_links_from_source` missing.

- [ ] **Step 3: Implement the derivation**

Append inside `SqlAlchemyChannelAccountLinkRepository`:
```python
    def upsert_owner_channel_links_from_source(self) -> int:
        """Idempotently derive owner↔channel links from source-row co-occurrence.

        Only rows where BOTH content_owner_id and youtube_channel_id are present
        produce links. source_account_id is never read (it must not infer the
        account↔owner link). Returns the count of newly inserted links.
        """
        observed = self._session.execute(
            select(
                GoogleRevenueSourceRowORM.content_owner_id,
                GoogleRevenueSourceRowORM.youtube_channel_id,
                GoogleRevenueSourceRowORM.report_month,
                func.min(GoogleRevenueSourceRowORM.source_row_key),
            )
            .where(
                GoogleRevenueSourceRowORM.tenant_id == self._tenant_id,
                GoogleRevenueSourceRowORM.content_owner_id.is_not(None),
                GoogleRevenueSourceRowORM.youtube_channel_id.is_not(None),
            )
            .group_by(
                GoogleRevenueSourceRowORM.content_owner_id,
                GoogleRevenueSourceRowORM.youtube_channel_id,
                GoogleRevenueSourceRowORM.report_month,
            )
        ).all()
        created = 0
        for owner_id, channel_id, month, source_key in observed:
            exists = self._session.scalar(
                select(func.count())
                .select_from(ContentOwnerChannelLinkORM)
                .where(
                    ContentOwnerChannelLinkORM.tenant_id == self._tenant_id,
                    ContentOwnerChannelLinkORM.content_owner_id == owner_id,
                    ContentOwnerChannelLinkORM.youtube_channel_id == channel_id,
                    ContentOwnerChannelLinkORM.effective_month_start == month,
                )
            )
            if exists:
                continue
            self._session.add(
                ContentOwnerChannelLinkORM(
                    tenant_id=self._tenant_id,
                    content_owner_id=owner_id,
                    youtube_channel_id=channel_id,
                    provenance_kind="SOURCE_ROW",
                    provenance_source_id=source_key,
                    active=True,
                    effective_month_start=month,
                    effective_month_end=month,
                )
            )
            created += 1
        self._session.flush()
        return created
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/finance/test_channel_account_links.py -q -k derivation`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/channel_account_links.py tests/finance/test_channel_account_links.py
git commit -m "feat(finance): derive owner-channel links from source rows (idempotent)"
```

---

### Task 10: `list_verified_adsense_account_channels` (read contract for Spec 2b)

**Files:**
- Modify: `backend/ums_smart_revenue/finance/channel_account_links.py`
- Test: `tests/finance/test_channel_account_links.py` (add)

- [ ] **Step 1: Write the failing tests**

Append to `tests/finance/test_channel_account_links.py`:
```python
def test_read_contract_returns_verified_month_scoped_channels(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        link = _propose(repo, account="pub-1", owner="owner-1", start="2026-01", end=None)
        repo.verify_account_owner_link(link.id, verified_by=VERIFIER, reason="r")
        _source_row(session, owner="owner-1", channel="chan-1", month="2026-04", key="s1")
        session.commit()
        repo.upsert_owner_channel_links_from_source()
        session.commit()
        got = repo.list_verified_adsense_account_channels(
            tenant_id=TENANT, month="2026-04", adsense_account_id="pub-1"
        )
        wrong_month = repo.list_verified_adsense_account_channels(
            tenant_id=TENANT, month="2026-05", adsense_account_id="pub-1"
        )
    assert got == ["chan-1"]
    assert wrong_month == []  # owner-channel link only observed for 2026-04


def test_read_contract_excludes_unverified_account(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=TENANT)
        _propose(repo, account="pub-1", owner="owner-1", start="2026-01", end=None)  # never verified
        _source_row(session, owner="owner-1", channel="chan-1", month="2026-04", key="s1")
        session.commit()
        repo.upsert_owner_channel_links_from_source()
        session.commit()
        got = repo.list_verified_adsense_account_channels(
            tenant_id=TENANT, month="2026-04", adsense_account_id="pub-1"
        )
    assert got == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/finance/test_channel_account_links.py -q -k read_contract`
Expected: FAIL — `list_verified_adsense_account_channels` missing.

- [ ] **Step 3: Implement the read contract**

Append inside `SqlAlchemyChannelAccountLinkRepository`:
```python
    def list_verified_adsense_account_channels(
        self, *, tenant_id: UUID | str, month: str, adsense_account_id: str
    ) -> list[str]:
        """Return channels for an account in a month via VERIFIED+valid links only.

        Joins VERIFIED account↔owner links valid for ``month`` to active
        owner↔channel links valid for ``month``. Empty when the account is
        unmapped/unverified (Spec 2b turns that into UNALLOCATED + a blocking
        issue). Pure read — no derivation, no writes.

        Raises:
            ChannelAccountLinkValidationError: If ``month`` is malformed.
        """
        _validate_month(month)
        resolved_tenant = _resolve_tenant_id(tenant_id)
        owner_subquery = (
            select(AdsenseContentOwnerLinkORM.content_owner_id)
            .where(
                AdsenseContentOwnerLinkORM.tenant_id == resolved_tenant,
                AdsenseContentOwnerLinkORM.adsense_account_id == adsense_account_id,
                AdsenseContentOwnerLinkORM.verification_status == "VERIFIED",
                AdsenseContentOwnerLinkORM.effective_month_start <= month,
                (AdsenseContentOwnerLinkORM.effective_month_end.is_(None))
                | (AdsenseContentOwnerLinkORM.effective_month_end >= month),
            )
        )
        rows = self._session.scalars(
            select(ContentOwnerChannelLinkORM.youtube_channel_id)
            .where(
                ContentOwnerChannelLinkORM.tenant_id == resolved_tenant,
                ContentOwnerChannelLinkORM.content_owner_id.in_(owner_subquery),
                ContentOwnerChannelLinkORM.active.is_(True),
                ContentOwnerChannelLinkORM.effective_month_start <= month,
                (ContentOwnerChannelLinkORM.effective_month_end.is_(None))
                | (ContentOwnerChannelLinkORM.effective_month_end >= month),
            )
            .order_by(ContentOwnerChannelLinkORM.youtube_channel_id)
            .distinct()
        ).all()
        return list(rows)
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/finance/test_channel_account_links.py -q -k read_contract`
Expected: PASS (2 tests). Then run the whole repo file: `pytest tests/finance/test_channel_account_links.py -q` → all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/channel_account_links.py tests/finance/test_channel_account_links.py
git commit -m "feat(finance): verified account-channel read contract for allocation"
```

---

### Task 11: API module — GET list endpoint + mount

**Files:**
- Create: `backend/ums_smart_revenue/api/channel_account_links.py`
- Modify: `backend/ums_smart_revenue/app.py`
- Test: `tests/api/test_channel_account_links_api.py`

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_channel_account_links_api.py`:
```python
"""Endpoint tests for the channel-account map (auth, audit, payload safety)."""
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.finance_models import AdsenseContentOwnerLinkORM, FinanceBase
from ums_smart_revenue.db.org_models import OrgBase
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM

USER_ID = UUID("00000000-0000-0000-0000-0000000d0401")


def auth_headers(role, scope_type="global", scope_id=None):
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "map-admin@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path):
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def seed(database_url):
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(UserORM(id=USER_ID, email="map-admin@example.com", display_name="Map Admin"))
        session.add(
            AdsenseContentOwnerLinkORM(
                id=uuid4(), tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
                adsense_account_id="pub-1", content_owner_id="owner-1",
                verification_status="UNVERIFIED", provenance_kind="OPERATOR_ASSERTED",
                provenance_payload={"secret_provenance": "LEAK-1"},
                effective_month_start="2026-01",
            )
        )
        session.commit()


def test_finance_viewer_lists_links_without_provenance_payload(tmp_path):
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        "/revenue/channel-account-links",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_count"] == 1
    assert body["links"][0]["adsense_account_id"] == "pub-1"
    assert "provenance_payload" not in str(body)
    assert "LEAK" not in str(body)
    assert {e["event_type"] for e in body["audit_events"]} == {"REVENUE_VIEWED", "PAYMENT_VIEWED"}
    engine = create_engine(database_url)
    with Session(engine) as session:
        logs = session.scalars(select(AuditLogORM)).all()
    assert {log.event_type for log in logs} == {"REVENUE_VIEWED", "PAYMENT_VIEWED"}


def test_list_requires_view_finalized_payments(tmp_path):
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    # analytics_viewer lacks VIEW_FINALIZED_PAYMENTS -> 403, fail-closed.
    response = client.get(
        "/revenue/channel-account-links",
        headers=auth_headers("analytics_viewer", "global"),
    )
    assert response.status_code == 403


def test_list_malformed_month_returns_422(tmp_path):
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        "/revenue/channel-account-links?month=2026-13",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 422
```

> Before running: confirm the roles `finance_viewer` (holds VIEW_REVENUE + VIEW_FINALIZED_PAYMENTS) and `analytics_viewer` (lacks VIEW_FINALIZED_PAYMENTS) exist in `backend/ums_smart_revenue/auth/seed.py` / `db/security_seed.sql`. If a role name differs, pick the seeded role that holds exactly the needed/omitted permission (the deduction-components API test uses `finance_viewer` for the full finance read).

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/api/test_channel_account_links_api.py -q`
Expected: FAIL — 404 (route not mounted) / import error.

- [ ] **Step 3: Create the API module**

Create `backend/ums_smart_revenue/api/channel_account_links.py`:
```python
"""Read/write API for the channel↔account map (Phase 4 Spec 2a).

Thin routes: parse input, resolve the repository, enforce boundary permissions,
call the repository, translate typed errors, and record sensitive audit events.
provenance_payload is never serialized. Audit persistence reuses the shared
``current_audit_sink`` (create_app overrides it to a SQL sink).
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ums_smart_revenue.api.channels import audit_record_to_api, current_audit_sink
from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_principal_from_headers,
)
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.finance.channel_account_links import (
    ChannelAccountLinkConflictError,
    ChannelAccountLinkNotFoundError,
    ChannelAccountLinkValidationError,
    SqlAlchemyChannelAccountLinkRepository,
)

router = APIRouter(prefix="/revenue", tags=["channel-account-links"])


def current_channel_account_link_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyChannelAccountLinkRepository:
    """Build the tenant-aware channel-account-link repository for a request."""
    return SqlAlchemyChannelAccountLinkRepository(session)


class AccountOwnerLinksListResponse(BaseModel):
    """Typed list response for account↔owner links (no provenance_payload)."""

    total_count: int
    returned_count: int
    links: list[dict[str, object]]
    pagination: dict[str, object]
    audit_events: list[dict[str, object]]


def _require_permission(
    user: UserPrincipal, permission: Permission, scope: AccessScope
) -> None:
    """Raise HTTP 403 if the principal lacks the permission for the scope."""
    if not has_permission(user, permission, scope):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {permission.value}",
        )


# ============================================================================
# Purpose: List account↔owner links (global-scoped management view). The
#   AdSense account id is finalized-payment context, so the gate requires both
#   VIEW_REVENUE and VIEW_FINALIZED_PAYMENTS at global scope.
# Database/ORM: adsense_content_owner_links (read-only).
# Standards: thin route; typed 422 on malformed month; provenance_payload never
#   serialized; sensitive audit (REVENUE_VIEWED + PAYMENT_VIEWED).
# Blast Radius: Authorization (fail-closed add); audit. No finance mutation.
# ============================================================================
@router.get("/channel-account-links", response_model=AccountOwnerLinksListResponse)
def list_channel_account_links(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyChannelAccountLinkRepository,
        Depends(current_channel_account_link_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
    status_filter: Annotated[str | None, Query(alias="status", min_length=1)] = None,
    adsense_account_id: Annotated[str | None, Query(min_length=1)] = None,
    content_owner_id: Annotated[str | None, Query(min_length=1)] = None,
    month: Annotated[str | None, Query(min_length=1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AccountOwnerLinksListResponse:
    """List account↔owner links for operator review (global-scoped)."""
    global_scope = AccessScope.global_scope()
    _require_permission(user, Permission.VIEW_REVENUE, global_scope)
    _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, global_scope)
    try:
        page = repository.list_account_owner_links(
            status=status_filter,
            adsense_account_id=adsense_account_id,
            content_owner_id=content_owner_id,
            month=month,
            limit=limit,
            offset=offset,
        )
    except ChannelAccountLinkValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    details = {"total_count": page.total_count, "returned_count": len(page.links)}
    audit_events = [
        audit_record_to_api(
            record_audit_event(
                sink=audit_sink, actor=user,
                event_type=AuditEventType.REVENUE_VIEWED,
                entity_type="channel_account_links", entity_id="list",
                scope=global_scope, details=details,
            )
        ),
        audit_record_to_api(
            record_audit_event(
                sink=audit_sink, actor=user,
                event_type=AuditEventType.PAYMENT_VIEWED,
                entity_type="channel_account_links", entity_id="list",
                scope=global_scope, details=details,
            )
        ),
    ]
    has_more = offset + len(page.links) < page.total_count
    return AccountOwnerLinksListResponse(
        total_count=page.total_count,
        returned_count=len(page.links),
        links=[link.to_api() for link in page.links],
        pagination={
            "limit": limit, "offset": offset,
            "next_offset": (offset + limit) if has_more else None,
            "has_more": has_more,
        },
        audit_events=audit_events,
    )
```

- [ ] **Step 4: Mount the router in `app.py`**

In `backend/ums_smart_revenue/app.py`, add the import alongside the other `api.*` imports (after the `channels` imports, ~line 23):
```python
from ums_smart_revenue.api.channel_account_links import (
    router as channel_account_links_router,
)
```
And in the `app.include_router(...)` block (after `app.include_router(channels_router)`, ~line 114):
```python
    app.include_router(channel_account_links_router)
```

- [ ] **Step 5: Run to verify it passes**

Run: `pytest tests/api/test_channel_account_links_api.py -q -k "lists_links or view_finalized or malformed_month"`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add backend/ums_smart_revenue/api/channel_account_links.py backend/ums_smart_revenue/app.py tests/api/test_channel_account_links_api.py
git commit -m "feat(api): GET channel-account-links list endpoint"
```

---

### Task 12: API — POST propose

**Files:**
- Modify: `backend/ums_smart_revenue/api/channel_account_links.py`
- Test: `tests/api/test_channel_account_links_api.py` (add)

- [ ] **Step 1: Write the failing tests**

Append to `tests/api/test_channel_account_links_api.py`:
```python
def test_propose_creates_link_with_org_mapping_permission(tmp_path):
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.post(
        "/revenue/channel-account-links",
        headers=auth_headers("data_steward", "global"),
        json={
            "adsense_account_id": "pub-2", "content_owner_id": "owner-2",
            "effective_month_start": "2026-02", "effective_month_end": None,
            "provenance_kind": "OPERATOR_ASSERTED",
            "provenance_payload": {"note": "from contract"},
            "reason": "operator asserts pub-2 maps to owner-2",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["link"]["verification_status"] == "UNVERIFIED"
    assert "provenance_payload" not in str(body)
    assert body["audit_event"]["event_type"] == "CHANNEL_ACCOUNT_LINK_PROPOSED"


def test_propose_requires_manage_org_mapping(tmp_path):
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.post(
        "/revenue/channel-account-links",
        headers=auth_headers("finance_viewer", "global"),  # lacks MANAGE_ORG_MAPPING
        json={
            "adsense_account_id": "pub-2", "content_owner_id": "owner-2",
            "effective_month_start": "2026-02", "effective_month_end": None,
            "provenance_kind": "OPERATOR_ASSERTED", "provenance_payload": {},
            "reason": "x",
        },
    )
    assert response.status_code == 403
```

> Confirm `data_steward` holds `registry.manage_org_mapping` (it does per `security_seed.sql:164`). If you prefer, use `corporate_admin`.

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/api/test_channel_account_links_api.py -q -k propose`
Expected: FAIL — route missing (404/405).

- [ ] **Step 3: Add request/response models + the propose route**

Append to `backend/ums_smart_revenue/api/channel_account_links.py`:
```python
class ProposeAccountOwnerLinkRequest(BaseModel):
    """Validated payload to propose an UNVERIFIED account↔owner link."""

    adsense_account_id: str = Field(min_length=1)
    content_owner_id: str = Field(min_length=1)
    effective_month_start: str = Field(min_length=7, max_length=7)
    effective_month_end: str | None = None
    provenance_kind: str = Field(min_length=1)
    provenance_payload: dict[str, object] = Field(default_factory=dict)
    reason: str = Field(min_length=1)

    @field_validator(
        "adsense_account_id", "content_owner_id", "provenance_kind", "reason",
        mode="before",
    )
    @classmethod
    def _strip(cls, value):
        return value.strip() if isinstance(value, str) else value


class AccountOwnerLinkMutationResponse(BaseModel):
    """Typed response for a single-link mutation."""

    link: dict[str, object]
    audit_event: dict[str, object]


# ============================================================================
# Purpose: Propose an UNVERIFIED account↔owner link. A proposal is a mapping
#   assertion (not money-affecting until verified), gated by MANAGE_ORG_MAPPING.
# Database/ORM: adsense_content_owner_links (insert).
# Standards: thin route; 422 on malformed month; reason-required audit; no
#   provenance_payload in the response.
# Blast Radius: Authorization (fail-closed); audit; finance map write.
# ============================================================================
@router.post(
    "/channel-account-links",
    response_model=AccountOwnerLinkMutationResponse,
    status_code=status.HTTP_201_CREATED,
)
def propose_channel_account_link(
    payload: ProposeAccountOwnerLinkRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyChannelAccountLinkRepository,
        Depends(current_channel_account_link_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> AccountOwnerLinkMutationResponse:
    """Propose an UNVERIFIED account↔owner link (operator-asserted)."""
    _require_permission(user, Permission.MANAGE_ORG_MAPPING, AccessScope.global_scope())
    try:
        link = repository.propose_account_owner_link(
            adsense_account_id=payload.adsense_account_id,
            content_owner_id=payload.content_owner_id,
            effective_month_start=payload.effective_month_start,
            effective_month_end=payload.effective_month_end,
            provenance_kind=payload.provenance_kind,
            provenance_payload=payload.provenance_payload,
        )
    except ChannelAccountLinkValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    record = record_audit_event(
        sink=audit_sink, actor=user,
        event_type=AuditEventType.CHANNEL_ACCOUNT_LINK_PROPOSED,
        entity_type="adsense_content_owner_link", entity_id=link.id,
        scope=AccessScope.global_scope(), reason=payload.reason,
        details={"adsense_account_id": link.adsense_account_id,
                 "content_owner_id": link.content_owner_id},
    )
    return AccountOwnerLinkMutationResponse(
        link=link.to_api(), audit_event=audit_record_to_api(record)
    )
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/api/test_channel_account_links_api.py -q -k propose`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/api/channel_account_links.py tests/api/test_channel_account_links_api.py
git commit -m "feat(api): POST propose channel-account link"
```

---

### Task 13: API — POST verify + reject (dual permission, 409 overlap)

**Files:**
- Modify: `backend/ums_smart_revenue/api/channel_account_links.py`
- Test: `tests/api/test_channel_account_links_api.py` (add)

- [ ] **Step 1: Write the failing tests**

Append to `tests/api/test_channel_account_links_api.py`:
```python
def _propose_via_api(client, *, account="pub-9", owner="owner-9", start="2026-01", end=None):
    return client.post(
        "/revenue/channel-account-links",
        headers=auth_headers("super_owner", "global"),
        json={
            "adsense_account_id": account, "content_owner_id": owner,
            "effective_month_start": start, "effective_month_end": end,
            "provenance_kind": "OPERATOR_ASSERTED", "provenance_payload": {}, "reason": "seed",
        },
    ).json()["link"]["id"]


def test_super_owner_can_verify(tmp_path):
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    link_id = _propose_via_api(client)
    response = client.post(
        f"/revenue/channel-account-links/{link_id}/verify",
        headers=auth_headers("super_owner", "global"),
        json={"reason": "verified against signed contract"},
    )
    assert response.status_code == 200
    assert response.json()["link"]["verification_status"] == "VERIFIED"
    assert response.json()["audit_event"]["event_type"] == "CHANNEL_ACCOUNT_LINK_VERIFIED"


def test_verify_requires_both_permissions(tmp_path):
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    link_id = _propose_via_api(client)
    # data_steward has MANAGE_ORG_MAPPING but NOT CHANGE_ALLOCATION_RULE -> 403.
    r1 = client.post(
        f"/revenue/channel-account-links/{link_id}/verify",
        headers=auth_headers("data_steward", "global"),
        json={"reason": "x"},
    )
    assert r1.status_code == 403
    # finance_admin has CHANGE_ALLOCATION_RULE but NOT MANAGE_ORG_MAPPING -> 403.
    r2 = client.post(
        f"/revenue/channel-account-links/{link_id}/verify",
        headers=auth_headers("finance_admin", "global"),
        json={"reason": "x"},
    )
    assert r2.status_code == 403


def test_verify_overlap_returns_409(tmp_path):
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    first = _propose_via_api(client, account="pub-9", owner="owner-1", start="2026-01", end="2026-06")
    client.post(
        f"/revenue/channel-account-links/{first}/verify",
        headers=auth_headers("super_owner", "global"),
        json={"reason": "r1"},
    )
    second = _propose_via_api(client, account="pub-9", owner="owner-2", start="2026-03", end=None)
    response = client.post(
        f"/revenue/channel-account-links/{second}/verify",
        headers=auth_headers("super_owner", "global"),
        json={"reason": "r2"},
    )
    assert response.status_code == 409


def test_reject_sets_rejected(tmp_path):
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    link_id = _propose_via_api(client)
    response = client.post(
        f"/revenue/channel-account-links/{link_id}/reject",
        headers=auth_headers("super_owner", "global"),
        json={"reason": "operator rejects unverified mapping"},
    )
    assert response.status_code == 200
    assert response.json()["link"]["verification_status"] == "REJECTED"
    assert response.json()["audit_event"]["event_type"] == "CHANNEL_ACCOUNT_LINK_REJECTED"
```

> Confirm seeded roles: `finance_admin` holds `finance.change_allocation_rule` (not `registry.manage_org_mapping`); `data_steward` holds `registry.manage_org_mapping` (not `finance.change_allocation_rule`); `super_owner` holds both. (Verified in `security_seed.sql`.)

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/api/test_channel_account_links_api.py -q -k "verify or reject"`
Expected: FAIL — verify/reject routes missing.

- [ ] **Step 3: Add the verify + reject routes**

Append to `backend/ums_smart_revenue/api/channel_account_links.py`:
```python
class LinkDecisionRequest(BaseModel):
    """Validated payload for a verify/reject decision (reason required)."""

    reason: str = Field(min_length=1)

    @field_validator("reason", mode="before")
    @classmethod
    def _strip_reason(cls, value):
        return value.strip() if isinstance(value, str) else value


def _require_link_decision_permissions(
    user: UserPrincipal, effective_month_start: str
) -> None:
    """Require BOTH org-mapping trust and allocation authority for the decision."""
    _require_permission(user, Permission.MANAGE_ORG_MAPPING, AccessScope.global_scope())
    _require_permission(
        user, Permission.CHANGE_ALLOCATION_RULE,
        AccessScope.finance_month(effective_month_start),
    )


def _decide_link(
    *, link_id: str, reason: str, verify: bool,
    user: UserPrincipal, repository: SqlAlchemyChannelAccountLinkRepository,
    audit_sink: AuditSink,
) -> AccountOwnerLinkMutationResponse:
    """Shared verify/reject handler: load, authorize on the link month, mutate, audit."""
    try:
        existing = repository.list_account_owner_links(limit=500, offset=0)
    except ChannelAccountLinkValidationError as exc:  # pragma: no cover - fixed args
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    match = next((link for link in existing.links if link.id == link_id), None)
    if match is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown link")
    _require_link_decision_permissions(user, match.effective_month_start)
    try:
        if verify:
            link = repository.verify_account_owner_link(
                link_id, verified_by=user.user_id, reason=reason
            )
            event_type = AuditEventType.CHANNEL_ACCOUNT_LINK_VERIFIED
        else:
            link = repository.reject_account_owner_link(
                link_id, verified_by=user.user_id, reason=reason
            )
            event_type = AuditEventType.CHANNEL_ACCOUNT_LINK_REJECTED
    except ChannelAccountLinkNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ChannelAccountLinkConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    record = record_audit_event(
        sink=audit_sink, actor=user, event_type=event_type,
        entity_type="adsense_content_owner_link", entity_id=link.id,
        scope=AccessScope.finance_month(link.effective_month_start), reason=reason,
        details={"adsense_account_id": link.adsense_account_id,
                 "verification_status": link.verification_status},
    )
    return AccountOwnerLinkMutationResponse(
        link=link.to_api(), audit_event=audit_record_to_api(record)
    )


# ============================================================================
# Purpose: Verify/reject an account↔owner link — the money-gating trust
#   decision. Requires BOTH MANAGE_ORG_MAPPING (global) and CHANGE_ALLOCATION_
#   RULE (finance month of the link's start). Verify enforces the overlap
#   invariant (409). reason-required sensitive audit.
# Database/ORM: adsense_content_owner_links (update).
# Blast Radius: Authorization (fail-closed, dual); audit; finance map state.
# ============================================================================
@router.post(
    "/channel-account-links/{link_id}/verify",
    response_model=AccountOwnerLinkMutationResponse,
)
def verify_channel_account_link(
    link_id: str,
    payload: LinkDecisionRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyChannelAccountLinkRepository,
        Depends(current_channel_account_link_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> AccountOwnerLinkMutationResponse:
    """Verify an account↔owner link (dual-permission, overlap-guarded)."""
    return _decide_link(
        link_id=link_id, reason=payload.reason, verify=True,
        user=user, repository=repository, audit_sink=audit_sink,
    )


@router.post(
    "/channel-account-links/{link_id}/reject",
    response_model=AccountOwnerLinkMutationResponse,
)
def reject_channel_account_link(
    link_id: str,
    payload: LinkDecisionRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyChannelAccountLinkRepository,
        Depends(current_channel_account_link_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> AccountOwnerLinkMutationResponse:
    """Reject an account↔owner link (dual-permission)."""
    return _decide_link(
        link_id=link_id, reason=payload.reason, verify=False,
        user=user, repository=repository, audit_sink=audit_sink,
    )
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/api/test_channel_account_links_api.py -q`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/api/channel_account_links.py tests/api/test_channel_account_links_api.py
git commit -m "feat(api): POST verify/reject channel-account link (dual gate, 409 overlap)"
```

---

### Task 14: Postgres-tier concurrency / lock-path validation (Mahmoud requirement)

**Files:**
- Test: `tests/db/test_channel_account_map_migration_postgres.py` (add)

- [ ] **Step 1: Write the failing test**

Append to `tests/db/test_channel_account_map_migration_postgres.py`:
```python
def test_advisory_lock_blocks_concurrent_verify(alembic_config, fresh_engine):
    """A held per-account advisory lock blocks a second verify path (proves the lock runs).

    Connection A holds pg_advisory_xact_lock for (tenant, account); connection B,
    with a short lock_timeout, attempts the same lock and must error out — proving
    verify serializes on the per-account key rather than racing.
    """
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    from ums_smart_revenue.finance.channel_account_links import _account_owner_lock_key
    from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

    command.upgrade(alembic_config, "head")
    from uuid import UUID
    tenant = UUID(UMS_TENANT_ID)
    key = _account_owner_lock_key(tenant, "pub-lock")

    conn_a = fresh_engine.connect()
    conn_b = fresh_engine.connect()
    try:
        conn_a.execute(text("BEGIN"))
        conn_a.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
        conn_b.execute(text("SET lock_timeout = '500ms'"))
        conn_b.execute(text("BEGIN"))
        raised = False
        try:
            conn_b.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
        except OperationalError:
            raised = True
        assert raised, "second connection should block on the held advisory lock"
    finally:
        conn_a.execute(text("ROLLBACK"))
        conn_b.execute(text("ROLLBACK"))
        conn_a.close()
        conn_b.close()


def test_repo_verify_runs_advisory_lock_on_postgres(alembic_config, fresh_engine):
    """The repository verify path executes against live Postgres (lock path exercised)."""
    from sqlalchemy.orm import Session

    from ums_smart_revenue.finance.channel_account_links import (
        SqlAlchemyChannelAccountLinkRepository,
    )
    from uuid import UUID, uuid4
    from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

    command.upgrade(alembic_config, "head")
    tenant = UUID(UMS_TENANT_ID)
    with Session(fresh_engine) as session:
        repo = SqlAlchemyChannelAccountLinkRepository(session, tenant_id=tenant)
        link = repo.propose_account_owner_link(
            adsense_account_id="pub-pg", content_owner_id="owner-pg",
            effective_month_start="2026-01", effective_month_end=None,
            provenance_kind="OPERATOR_ASSERTED", provenance_payload={},
        )
        out = repo.verify_account_owner_link(
            link.id, verified_by=uuid4(), reason="postgres path"
        )
        session.commit()
    assert out.verification_status == "VERIFIED"
```

- [ ] **Step 2: Run to verify it fails (or passes meaningfully)**

Run: `pytest tests/db/test_channel_account_map_migration_postgres.py -q -k "advisory_lock or runs_advisory" ` (with `UMS_TEST_POSTGRES_URL` set)
Expected: with the implementation from Tasks 5–7 present, the lock-path test should PASS. If `pg_advisory_xact_lock` were missing from the verify path, the blocking test would fail (second connection would NOT block). Confirm it fails when you temporarily comment out `_acquire_account_owner_lock(...)` in `verify_account_owner_link` (then restore it).

- [ ] **Step 3: No new implementation required**

The advisory lock from Task 5/7 is the implementation. This task only adds the Postgres-tier proof.

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/db/test_channel_account_map_migration_postgres.py -q` (with `UMS_TEST_POSTGRES_URL` set)
Expected: PASS (all migration + concurrency tests).

- [ ] **Step 5: Commit**

```bash
git add tests/db/test_channel_account_map_migration_postgres.py
git commit -m "test(db): Postgres concurrency/lock-path validation for verify"
```

---

### Task 15: Plan/backlog status update

**Files:**
- Modify: `Docs/01_IMPLEMENTATION_PLAN.md`
- Modify: `Docs/15_DELIVERY_BACKLOG.md`

- [ ] **Step 1: Update Docs/01 Phase 4 section**

In `Docs/01_IMPLEMENTATION_PLAN.md`, in the Phase 4 block (around lines 442–444 / 477), mark the map substrate shipped and clarify allocation remains Spec 2b. Replace the "Allocation rules — remaining: not started." line context with, e.g.:
```markdown
- ⏳ Allocation rules (Spec 2b) — remaining: not started. Prerequisite shipped:
  canonical channel↔account map (this PR) — adsense_content_owner_links (operator-
  verified) + content_owner_channel_links (derived) + audited propose/verify/reject
  API + list_verified_adsense_account_channels read contract. Allocation consumes
  only VERIFIED links; unmapped/unverified accounts stay UNALLOCATED.
```
Update the Phase 4 `Status:` date to `2026-05-31`.

- [ ] **Step 2: Update Docs/15 backlog**

In `Docs/15_DELIVERY_BACKLOG.md`, add under the Phase 4 area:
```markdown
- ⏳ Channel↔account map — shipped (this PR): two-layer canonical map
  (adsense_content_owner_links operator-verified + content_owner_channel_links
  derived from source rows), audited propose/verify/reject API behind dual
  MANAGE_ORG_MAPPING + CHANGE_ALLOCATION_RULE gates, per-account advisory-lock
  overlap invariant, and list_verified_adsense_account_channels for Spec 2b.
- ⏳ Allocation engine (Spec 2b) — remaining: not started; consumes the map.
```

- [ ] **Step 3: Validate doc hygiene**

Run: `git diff --check`
Expected: no whitespace errors.

- [ ] **Step 4: Commit**

```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "docs(plan): channel-account map shipped; allocation = Spec 2b remaining"
```

---

## Final validation (run before declaring the branch ready)

```bash
python -m ruff check backend tests
# Unit tier (SQLite) — fast, no container:
pytest -q
# Postgres tier — start the container first (see Validation gate):
$env:UMS_TEST_POSTGRES_URL="postgresql+psycopg://postgres:ums@localhost:55432/test_ums"
pytest -q tests/db/test_channel_account_map_migration_postgres.py
git diff --check
```
Expected: ruff clean; full suite green (existing + new); Postgres migration/concurrency green; diff-check clean. Confirm no commit carries a `Co-Authored-By` trailer (`git log main..HEAD | git interpret-trailers --parse` → empty). Do NOT push or open a PR until Mahmoud approves.

## Blast-radius summary

- Two **new** tables; `finance_models.py` gains two ORM classes + one helper. No existing model/column/finance/auth path changed.
- Authorization strictly **added** (fail-closed); three **new** sensitive, reason-required audit events. No permission relaxed.
- PostgreSQL remains source of truth. **No graph projection impact** (no projection reads these tables).
- Allocation/finance numbers unchanged (substrate only; consumption is Spec 2b).
- Migration additive (up/down + idempotency proven); pre-alpha data disposable.
- `CONFLICT` is a reserved `verification_status` (schema-supported, excluded from the read contract since it consumes only `VERIFIED`); no API setter ships this PR — the §2 API scope is propose/verify/reject/list only. A detection/setter path is future work.
