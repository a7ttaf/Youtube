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
