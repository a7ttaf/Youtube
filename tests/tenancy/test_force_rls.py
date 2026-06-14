"""Postgres-only proof that the FORCE-RLS migration binds table OWNERS.

Two layers of evidence:

PRIMARY (migration state) — after ``alembic upgrade head`` every table in
:data:`TENANT_SCOPED_TABLES` has ``pg_class.relforcerowsecurity = TRUE``, and a
single-step downgrade flips it back to ``FALSE`` for all of them. This mirrors
the ``relrowsecurity`` loop in ``tests/db/test_tenant_rls_migration.py`` for the
ENABLE migration, one tier up: ENABLE makes non-owner roles subject; FORCE makes
the owner subject too.

BEHAVIORAL (A/B, self-contained) — FORCE is invisible to the postgres superuser
login that owns the real 25 tables (a superuser always bypasses RLS), so the
only way to OBSERVE FORCE is a NON-superuser role that OWNS a FORCEd table. This
test stands up its own throwaway non-superuser owner and two tiny tables with an
identical tenant-style policy: one ENABLE+FORCE, one ENABLE-only. Reading as the
owner, the FORCEd table returns only policy-permitted rows while the ENABLE-only
table returns all rows (owner bypass). The A/B contrast is the proof that FORCE,
not the policy alone, binds the owner. The throwaway objects never touch the real
tenant tables' ownership; the DB is left at head.

Run as the superuser test connection (it must CREATE/DROP ROLE).
"""

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from tests.db._postgres_helpers import require_postgres_url
from ums_smart_revenue.db.rls import TENANT_SCOPED_TABLES

# The revision immediately below the FORCE migration (its down_revision); the
# primary test downgrades to it to assert the FORCE flag is cleared.
_PRE_FORCE_REVISION = "20260612_0001"

_OWNER_ROLE = "force_rls_owner_test"
_OWNER_PW = "pw"
_FORCED_TABLE = "force_rls_demo_forced"
_ENABLE_ONLY_TABLE = "force_rls_demo_enable_only"
_VISIBLE_TENANT = "11111111-1111-1111-1111-111111111111"
_HIDDEN_TENANT = "22222222-2222-2222-2222-222222222222"


def _alembic_config(url: str) -> Config:
    """Build an Alembic config bound to ``url`` without an ini file.

    Mirrors the no-ini pattern in ``test_tenant_rls_migration.py``: env.py only
    touches the logging tree when ``config_file_name`` is set, so configuring
    ``script_location`` + ``sqlalchemy.url`` directly avoids silencing other
    tests' ``caplog`` for the rest of the session.
    """
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option(
        "script_location",
        "backend/ums_smart_revenue/db/alembic",
    )
    return cfg


def _force_flags(conn: sa.Connection) -> dict[str, bool]:
    """Return ``{table: relforcerowsecurity}`` for the tenant-scoped tables."""
    rows = conn.execute(
        sa.text(
            "SELECT c.relname, c.relforcerowsecurity "
            "FROM pg_class c "
            "JOIN pg_namespace n ON c.relnamespace = n.oid "
            "WHERE n.nspname = 'public' AND c.relname = ANY(:names)"
        ),
        {"names": list(TENANT_SCOPED_TABLES)},
    ).all()
    return {name: flag for name, flag in rows}


def test_force_rls_migration_sets_force_flag_on_all_tenant_tables():
    """Verify upgrade FORCEs every tenant table and downgrade clears it."""
    url = require_postgres_url()
    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            flags = _force_flags(conn)
            for table in TENANT_SCOPED_TABLES:
                assert flags.get(table) is True, f"{table} not FORCEd"
        # One step back removes only the FORCE flag; ENABLE/policy stay intact.
        command.downgrade(cfg, _PRE_FORCE_REVISION)
        with engine.connect() as conn:
            flags = _force_flags(conn)
            for table in TENANT_SCOPED_TABLES:
                assert flags.get(table) is False, f"{table} still FORCEd"
    finally:
        engine.dispose()
        # Leave the DB at head for the rest of the PG tier.
        command.upgrade(cfg, "head")


def _drop_owner(conn: sa.Connection) -> None:
    """Drop the throwaway owner role and everything it owns, idempotently."""
    exists = conn.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"),
        {"r": _OWNER_ROLE},
    ).first()
    if exists is None:
        return
    # DROP OWNED clears the demo tables (and any grant) so DROP ROLE succeeds.
    conn.execute(sa.text(f'DROP OWNED BY "{_OWNER_ROLE}"'))
    conn.execute(sa.text(f'DROP ROLE IF EXISTS "{_OWNER_ROLE}"'))


def _build_owned_table(conn: sa.Connection, table: str, force: bool) -> None:
    """Create ``table`` owned by the non-superuser role with a tenant policy.

    Both demo tables get the same ``tenant_id = :visible`` USING policy and the
    same two seed rows; only the FORCE flag differs between them, so a read as
    the owner isolates exactly what FORCE changes.
    """
    conn.execute(
        sa.text(
            f"CREATE TABLE {table} (id int PRIMARY KEY, tenant_id uuid NOT NULL)"
        )
    )
    conn.execute(sa.text(f'ALTER TABLE {table} OWNER TO "{_OWNER_ROLE}"'))
    conn.execute(
        sa.text(f"INSERT INTO {table} VALUES (1, :t)"),
        {"t": _VISIBLE_TENANT},
    )
    conn.execute(
        sa.text(f"INSERT INTO {table} VALUES (2, :t)"),
        {"t": _HIDDEN_TENANT},
    )
    conn.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    if force:
        conn.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    conn.execute(
        sa.text(
            f"CREATE POLICY {table}_iso ON {table} "
            f"USING (tenant_id = '{_VISIBLE_TENANT}'::uuid)"
        )
    )


def _owner_visible_ids(conn: sa.Connection, table: str) -> set[int]:
    """Return the ids ``table`` exposes when read as the owner role."""
    conn.execute(sa.text(f'SET ROLE "{_OWNER_ROLE}"'))
    try:
        return set(
            conn.execute(sa.text(f"SELECT id FROM {table} ORDER BY id")).scalars()
        )
    finally:
        conn.execute(sa.text("RESET ROLE"))


def test_force_binds_non_superuser_owner_enable_only_does_not():
    """Verify FORCE subjects a non-superuser owner; ENABLE-only lets it bypass."""
    url = require_postgres_url()
    # The migration must be applied so FORCE semantics are exercised against the
    # same head the PG tier runs on; the demo objects below are independent of
    # the real tenant tables and prove the Postgres FORCE behaviour directly.
    command.upgrade(_alembic_config(url), "head")
    # AUTOCOMMIT: CREATE/DROP ROLE and OWNER changes are not transaction-friendly.
    engine = sa.create_engine(url)
    try:
        with engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as conn:
            _drop_owner(conn)
            conn.execute(
                sa.text(
                    f"CREATE ROLE \"{_OWNER_ROLE}\" LOGIN PASSWORD '{_OWNER_PW}' "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
                )
            )
            # The owner needs schema USAGE to resolve its own relations once we
            # SET ROLE into it; without it the table reads as "does not exist".
            conn.execute(
                sa.text(f'GRANT USAGE ON SCHEMA public TO "{_OWNER_ROLE}"')
            )
            try:
                _build_owned_table(conn, _FORCED_TABLE, force=True)
                _build_owned_table(conn, _ENABLE_ONLY_TABLE, force=False)

                # FORCEd table: owner is subject -> only the permitted row.
                forced_ids = _owner_visible_ids(conn, _FORCED_TABLE)
                assert forced_ids == {1}, (
                    "FORCE did not bind the non-superuser owner; "
                    f"saw {sorted(forced_ids)} instead of [1]"
                )

                # ENABLE-only table: owner bypasses RLS -> sees ALL rows.
                enable_only_ids = _owner_visible_ids(conn, _ENABLE_ONLY_TABLE)
                assert enable_only_ids == {1, 2}, (
                    "ENABLE-only owner unexpectedly filtered; the A/B contrast "
                    f"is broken (saw {sorted(enable_only_ids)})"
                )
            finally:
                _drop_owner(conn)
    finally:
        engine.dispose()
