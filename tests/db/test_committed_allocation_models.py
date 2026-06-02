"""SQLite model + constraint coverage for the committed-allocation tables."""
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event
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
    table + a tenant row, and FK enforcement on.
    """
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # noqa: ANN001
        """Enable SQLite FK enforcement on each new DBAPI connection."""
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
        # The unallocated table is empty in v1 but ships for snapshot-schema
        # fidelity; assert a row persists/reads back so all four tables are covered.
        session.add(CommittedAllocationUnallocatedORM(
            run_id=run.id, scope_id="chA", component_kind="DEDUCTION",
            component_key="k1", amount_usd=Decimal("0"),
            issue_code="UNALLOCATED", detail="x",
        ))
        session.commit()
        assert session.query(CommittedAllocationLineORM).count() == 1
        assert session.query(CommittedAllocationNoteORM).count() == 1
        assert session.query(CommittedAllocationUnallocatedORM).count() == 1


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


def test_idempotency_key_nonempty_check_rejects_empty(tmp_path):
    """idempotency_key DB-level nonempty CHECK rejects "" (independent of Pydantic 422)."""
    engine = _engine(tmp_path)
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(_run(idempotency_key=""))
        session.commit()


def test_reason_nonempty_check_rejects_empty(tmp_path):
    """reason DB-level nonempty CHECK rejects "" (independent of Pydantic 422)."""
    engine = _engine(tmp_path)
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(_run(reason=""))
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
        # FIX: Delete via the ORM query (not raw SQL bound with str(run_id)).
        # SQLAlchemy stores Uuid PKs on SQLite as 32-char hex WITHOUT dashes, so a
        # raw `WHERE id = :id` bound with the dashed str(run_id) matched zero rows —
        # the run was never deleted, the DB CASCADE never fired, and the line
        # survived (a silent no-op delete). The ORM query encodes the UUID
        # correctly, so the real DB-level ON DELETE CASCADE is exercised.
        session.query(CommittedAllocationRunORM).filter_by(id=run_id).delete(
            synchronize_session=False
        )
        session.commit()
        assert session.query(CommittedAllocationLineORM).count() == 0


def test_all_children_cascade_on_run_delete(tmp_path):
    """Deleting a run cascades to ALL three children (lines + notes + unallocated).

    The line-only cascade is covered above; this pins the notes and unallocated
    FK ondelete=CASCADE behaviorally too, so no orphan snapshot child survives a
    run delete. Uses the same UUID-safe ORM delete (raw str(run_id) matches zero
    rows on SQLite's dashless hex UUID storage — see the line-cascade FIX note).
    """
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
        session.add(CommittedAllocationNoteORM(
            run_id=run_id, note_code="CHANNEL_IN_MULTIPLE_ACCOUNTS",
            youtube_channel_id="chA", detail="x",
        ))
        session.add(CommittedAllocationUnallocatedORM(
            run_id=run_id, scope_id="chA", component_kind="DEDUCTION",
            component_key="k1", amount_usd=Decimal("0"),
            issue_code="UNALLOCATED", detail="x",
        ))
        session.commit()
        session.query(CommittedAllocationRunORM).filter_by(id=run_id).delete(
            synchronize_session=False
        )
        session.commit()
        assert session.query(CommittedAllocationLineORM).count() == 0
        assert session.query(CommittedAllocationNoteORM).count() == 0
        assert session.query(CommittedAllocationUnallocatedORM).count() == 0
