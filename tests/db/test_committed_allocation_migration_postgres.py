"""PostgreSQL round-trip for 20260602_0001 (committed account allocation)."""
from pathlib import Path

import pytest
from _postgres_helpers import require_postgres_url
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DataError, IntegrityError

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
    """Build the INSERT SQL + params for a committed_allocation_runs row (overridable)."""
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
    # The run->lines cascade is asserted above; the other two children
    # (unallocated, notes) carry the same ON DELETE CASCADE contract so a run
    # delete fully tears down its snapshot. Pin their FK ondelete too.
    unallocated_fks = {
        c["name"]: c
        for c in inspector.get_foreign_keys("committed_allocation_unallocated")
    }
    assert (
        unallocated_fks["fk_committed_allocation_unallocated_run"]["options"]["ondelete"]
        == "CASCADE"
    )
    notes_fks = {
        c["name"]: c for c in inspector.get_foreign_keys("committed_allocation_notes")
    }
    assert (
        notes_fks["fk_committed_allocation_notes_run"]["options"]["ondelete"] == "CASCADE"
    )
    checks = {
        c["name"] for c in inspector.get_check_constraints("committed_allocation_runs")
    }
    assert "ck_committed_allocation_runs_method" in checks
    assert "ck_committed_allocation_runs_version_positive" in checks
    assert "ck_committed_allocation_runs_month_format" in checks
    # PG-only finite (non-NaN, non-Inf) guards on the source-of-truth USD money
    # columns. The migration names these `..._<column>_finite` (the column name
    # carries its `_usd` suffix), so all four runs checks end in `_total_usd_finite`.
    assert "ck_committed_allocation_runs_allocated_total_usd_finite" in checks
    assert "ck_committed_allocation_runs_unallocated_total_usd_finite" in checks
    assert "ck_committed_allocation_runs_net_applicable_total_usd_finite" in checks
    assert "ck_committed_allocation_runs_reconciliation_total_usd_finite" in checks
    # The 20260603_0001 migration renames the line basis column to the
    # method-neutral basis_amount_usd; assert the head schema reflects the rename.
    columns = {c["name"] for c in inspector.get_columns("committed_allocation_lines")}
    assert "basis_amount_usd" in columns
    assert "basis_gross_usd" not in columns
    line_checks = {
        c["name"] for c in inspector.get_check_constraints("committed_allocation_lines")
    }
    assert "ck_committed_allocation_lines_amounts_finite" in line_checks
    # Non-empty identity guards on the line identifier columns (account/channel/
    # component key), mirroring deduction_components' non-empty identifier CHECKs.
    assert "ck_committed_allocation_lines_adsense_account_id_nonempty" in line_checks
    assert "ck_committed_allocation_lines_youtube_channel_id_nonempty" in line_checks
    assert "ck_committed_allocation_lines_component_key_nonempty" in line_checks
    unallocated_checks = {
        c["name"]
        for c in inspector.get_check_constraints("committed_allocation_unallocated")
    }
    # PG-only finite guard + non-empty identity guards on the unallocated table.
    assert "ck_committed_allocation_unallocated_amount_usd_finite" in unallocated_checks
    assert "ck_committed_allocation_unallocated_scope_id_nonempty" in unallocated_checks
    assert (
        "ck_committed_allocation_unallocated_component_key_nonempty"
        in unallocated_checks
    )
    notes_checks = {
        c["name"] for c in inspector.get_check_constraints("committed_allocation_notes")
    }
    assert "ck_committed_allocation_notes_youtube_channel_id_nonempty" in notes_checks
    # Explicit indexes created by the migration (one per table, plus the
    # composite run+channel index on lines). PK-backed indexes are not asserted.
    runs_indexes = {c["name"] for c in inspector.get_indexes("committed_allocation_runs")}
    assert "ix_committed_allocation_runs_tenant_month" in runs_indexes
    lines_indexes = {
        c["name"] for c in inspector.get_indexes("committed_allocation_lines")
    }
    assert "ix_committed_allocation_lines_run" in lines_indexes
    assert "ix_committed_allocation_lines_run_channel" in lines_indexes
    unallocated_indexes = {
        c["name"] for c in inspector.get_indexes("committed_allocation_unallocated")
    }
    assert "ix_committed_allocation_unallocated_run" in unallocated_indexes
    notes_indexes = {
        c["name"] for c in inspector.get_indexes("committed_allocation_notes")
    }
    assert "ix_committed_allocation_notes_run" in notes_indexes


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


def test_runs_method_check_accepts_post_tax_rejects_third(alembic_config, fresh_engine):
    """After upgrade to head, the runs method CHECK allows post_tax and rejects a third."""
    command.upgrade(alembic_config, "head")
    ok_sql, ok_params = _insert_run_sql(method="post_tax_revenue_proportional", key="k-pt")
    with fresh_engine.begin() as conn:
        conn.execute(ok_sql, ok_params)  # post_tax is allowlisted -> succeeds
    bad_sql, bad_params = _insert_run_sql(method="company_level", key="k-bad")
    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(bad_sql, bad_params)  # violates ck_committed_allocation_runs_method


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
                "source_system, component_key, basis_source_kind, basis_amount_usd, "
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


def _insert_line_sql(allocated_amount: str = "100.000000", **ov) -> tuple[str, dict]:
    """Build a line INSERT under :rid with overridable raw NUMERIC literals.

    Amounts are interpolated as bare SQL (so 'NaN'::numeric / 'Infinity'::numeric
    can be exercised on the raw-SQL path that bypasses the repository guards).
    """
    cols = dict(
        basis_gross="1000.000000", basis_share="1.000000",
        allocated_amount=allocated_amount,
    )
    cols.update(ov)
    sql = text(
        "INSERT INTO committed_allocation_lines "
        "(run_id, adsense_account_id, youtube_channel_id, component_kind, "
        "source_system, component_key, basis_source_kind, basis_amount_usd, "
        "basis_share, allocated_amount_usd, net_applicable) VALUES "
        "(:rid, 'pub-1', 'chA', 'DEDUCTION', 'adsense_management', 'k1', 'ADSENSE', "
        f"{cols['basis_gross']}::numeric, {cols['basis_share']}::numeric, "
        f"{cols['allocated_amount']}::numeric, true)"
    )
    return sql, {}


def test_runs_total_nan_rejected_by_finite_check(alembic_config, fresh_engine):
    """A direct-SQL run INSERT with allocated_total_usd = NaN is rejected.

    NaN IS storable in NUMERIC(20,6), so the PG-only finite CHECK is what rejects
    it on the raw path that bypasses the repository's is_finite() guard.
    """
    command.upgrade(alembic_config, "head")
    # Bare-SQL NaN literal (parameter binding would coerce the Python float),
    # otherwise reusing the valid-run column contract.
    sql = text(
        f"INSERT INTO committed_allocation_runs ({_RUN_COLS}) VALUES "
        "(:tenant, :month, :version, :method, :key, :fp, 1, 1, 0, "
        "'NaN'::numeric, 0, 100.000000, 0, :tenant, :reason)"
    )
    params = dict(
        tenant=UMS_TENANT_ID, month="2026-04", version=1,
        method="gross_revenue_proportional", key="nan-run", fp="fp1",
        reason="close",
    )
    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(sql, params)


def test_runs_total_infinity_rejected_by_numeric_type(alembic_config, fresh_engine):
    """+Infinity is rejected by the NUMERIC(20,6) column type before the CHECK."""
    command.upgrade(alembic_config, "head")
    sql = text(
        f"INSERT INTO committed_allocation_runs ({_RUN_COLS}) VALUES "
        "(:tenant, :month, :version, :method, :key, :fp, 1, 1, 0, "
        "'Infinity'::numeric, 0, 100.000000, 0, :tenant, :reason)"
    )
    params = dict(
        tenant=UMS_TENANT_ID, month="2026-04", version=1,
        method="gross_revenue_proportional", key="inf-run", fp="fp1",
        reason="close",
    )
    with pytest.raises(DataError), fresh_engine.begin() as conn:
        conn.execute(sql, params)


def test_lines_amount_nan_rejected_by_finite_check(alembic_config, fresh_engine):
    """A line INSERT with basis_amount_usd = NaN under a valid run is rejected.

    The runs row is committed first so the line FK is satisfied; the NaN then
    trips the PG-only amounts finite CHECK on the lines table (raw-SQL path).
    """
    command.upgrade(alembic_config, "head")
    run_sql, run_params = _insert_run_sql(key="line-nan")
    with fresh_engine.begin() as conn:
        conn.execute(run_sql, run_params)
        run_id = conn.execute(
            text("SELECT id FROM committed_allocation_runs LIMIT 1")
        ).scalar_one()
    line_sql, _ = _insert_line_sql(basis_gross="'NaN'")
    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(line_sql, {"rid": run_id})


def test_lines_amount_infinity_rejected_by_numeric_type(alembic_config, fresh_engine):
    """+Infinity in a line amount is rejected by the NUMERIC(20,6) type."""
    command.upgrade(alembic_config, "head")
    run_sql, run_params = _insert_run_sql(key="line-inf")
    with fresh_engine.begin() as conn:
        conn.execute(run_sql, run_params)
        run_id = conn.execute(
            text("SELECT id FROM committed_allocation_runs LIMIT 1")
        ).scalar_one()
    line_sql, _ = _insert_line_sql(basis_gross="'Infinity'")
    with pytest.raises(DataError), fresh_engine.begin() as conn:
        conn.execute(line_sql, {"rid": run_id})


def _seed_run(fresh_engine, key: str):
    """Insert one valid run and return its id (FK parent for child-row tests)."""
    run_sql, run_params = _insert_run_sql(key=key)
    with fresh_engine.begin() as conn:
        conn.execute(run_sql, run_params)
        return conn.execute(
            text("SELECT id FROM committed_allocation_runs LIMIT 1")
        ).scalar_one()


def test_unallocated_amount_nan_rejected_by_finite_check(alembic_config, fresh_engine):
    """An unallocated INSERT with amount_usd = NaN under a valid run is rejected.

    NaN IS storable in NUMERIC(20,6); the PG-only finite CHECK on the unallocated
    table rejects it on the raw-SQL path that bypasses the repository guard.
    """
    command.upgrade(alembic_config, "head")
    run_id = _seed_run(fresh_engine, key="unalloc-nan")
    # Bare 'NaN'::numeric literal (parameter binding would coerce the Python float).
    sql = text(
        "INSERT INTO committed_allocation_unallocated "
        "(run_id, scope_id, component_kind, component_key, amount_usd, "
        "issue_code, detail) VALUES "
        "(:rid, 'chA', 'DEDUCTION', 'k1', 'NaN'::numeric, 'UNALLOCATED', 'x')"
    )
    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(sql, {"rid": run_id})


def test_line_empty_component_key_rejected_by_nonempty_check(alembic_config, fresh_engine):
    """A line INSERT with an empty component_key is rejected by the migration's
    non-empty CHECK (mirrors deduction_components' non-empty identifier guard).
    """
    command.upgrade(alembic_config, "head")
    run_id = _seed_run(fresh_engine, key="line-empty-key")
    line_sql = text(
        "INSERT INTO committed_allocation_lines "
        "(run_id, adsense_account_id, youtube_channel_id, component_kind, "
        "source_system, component_key, basis_source_kind, basis_amount_usd, "
        "basis_share, allocated_amount_usd, net_applicable) VALUES "
        "(:rid, 'pub-1', 'chA', 'DEDUCTION', 'adsense_management', '', "
        "'ADSENSE', 1000.000000, 1.000000, 100.000000, true)"
    )
    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(line_sql, {"rid": run_id})
