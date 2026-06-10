import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from tests.db._postgres_helpers import require_postgres_url

from ums_smart_revenue.db.rls import (
    APP_PLATFORM_ROLE,
    APP_TENANT_ROLE,
    TENANT_CONTEXT_CLEARER,
    TENANT_CONTEXT_TABLE,
    TENANT_SCOPED_TABLES,
    tenant_rls_policy_name,
)


def _alembic_config(url: str) -> Config:
    """Build an Alembic config bound to the supplied database URL.

    Built without an alembic.ini path: env.py only configures logging when
    ``config.config_file_name`` is set, and ``fileConfig`` defaults to
    ``disable_existing_loggers=True``, which would silence pytest's
    ``caplog`` for every later test in the session. Setting
    ``script_location`` and ``sqlalchemy.url`` directly gives alembic
    everything it needs without touching the logging tree.
    """
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option(
        "script_location",
        "backend/ums_smart_revenue/db/alembic",
    )
    return cfg


def _function_exists(conn, function_name: str) -> bool:
    """Return True when ``function_name`` is present in the current DB.

    ``to_regprocedure`` resolves a function by its full signature, so the
    arg-types must be appended (``()`` for an argument-less void helper).
    The function body is owned by the migration that created it; this
    helper just reports presence.
    """
    return bool(
        conn.execute(
            sa.text("SELECT to_regprocedure(:name) IS NOT NULL"),
            {"name": f"{function_name}()"},
        ).scalar()
    )


def _drop_public_schema(url: str) -> None:
    """Drop and recreate the public schema for a truly fresh test DB."""
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("DROP SCHEMA public CASCADE"))
            conn.execute(sa.text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


def test_rls_migration_creates_roles_policies_and_grants():
    """Verify the migration creates the RLS roles, policies, and grants."""
    url = require_postgres_url()
    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            roles = set(
                conn.execute(
                    sa.text("SELECT rolname FROM pg_roles")
                ).scalars()
            )
            assert APP_TENANT_ROLE in roles
            assert APP_PLATFORM_ROLE in roles
            bypass = dict(
                conn.execute(
                    sa.text("SELECT rolname, rolbypassrls FROM pg_roles")
                ).all()
            )
            assert bypass[APP_PLATFORM_ROLE] is False
            assert bypass[APP_TENANT_ROLE] is False
            # Every allowlisted table has RLS enabled + the isolation policy.
            for table in TENANT_SCOPED_TABLES:
                enabled = conn.execute(
                    sa.text(
                        "SELECT relrowsecurity FROM pg_class "
                        "WHERE relname = :t"
                    ),
                    {"t": table},
                ).scalar()
                assert enabled is True, f"{table} RLS not enabled"
                policy = conn.execute(
                    sa.text(
                        "SELECT policyname FROM pg_policies "
                        "WHERE tablename = :t AND policyname = :p"
                    ).bindparams(t=table, p=tenant_rls_policy_name(table))
                ).first()
                assert policy is not None, f"{table} missing policy"
    finally:
        engine.dispose()


def test_rls_migration_downgrade_drops_roles_then_upgrade_restores():
    """Verify downgrade removes roles and upgrade restores the schema state."""
    # Downgrade must revoke every dependent privilege before DROP ROLE; if the
    # blanket grants are not revoked, DROP ROLE fails. Leave the DB at head.
    url = require_postgres_url()
    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "20260606_0001")
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            roles = set(
                conn.execute(
                    sa.text("SELECT rolname FROM pg_roles")
                ).scalars()
            )
            assert APP_TENANT_ROLE not in roles
            assert APP_PLATFORM_ROLE not in roles
    finally:
        engine.dispose()
    # Restore head so the rest of the PG tier sees the expected schema/roles.
    command.upgrade(cfg, "head")


def test_tenant_context_clearer_is_owned_only_by_20260609_0002():
    """Verify the privileged clearer is installed/removed by exactly one revision.

    Codex P2 review on PR #88 flagged the original code as installing
    ``clear_app_current_tenant_id`` in BOTH ``20260608_0001`` and
    ``20260609_0002``. A downgrade past ``20260609_0002`` would drop a helper
    that an earlier revision still claims to have installed, leaving the
    migration graph in an inconsistent state. The fix is to make
    ``20260609_0002`` the sole owner.
    """
    url = require_postgres_url()
    cfg = _alembic_config(url)
    # Reset the public schema so the test is order-independent and does not
    # inherit state from sibling tests in this file.
    _drop_public_schema(url)

    # 1) Fresh DB at 20260608_0001: helper MUST NOT exist (this revision
    #    deliberately does not own it). The session hook tolerates the
    #    missing-helper state via its `to_regprocedure` probe.
    command.upgrade(cfg, "20260608_0001")
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            assert _function_exists(conn, TENANT_CONTEXT_CLEARER) is False, (
                "20260608_0001 must not install clear_app_current_tenant_id; "
                "its sole owner is 20260609_0002."
            )
            # app_platform must hold DELETE on the context table so the
            # session hook can fall back to a direct DELETE during a
            # rolling-migration gap (helper is not installed yet). The
            # helper itself is SECURITY DEFINER and bypasses this grant,
            # but the fallback runs as the caller role.
            delete_grants = set(
                conn.execute(
                    sa.text(
                        "SELECT grantee FROM information_schema.role_table_grants "
                        "WHERE table_name = :t AND privilege_type = 'DELETE'"
                    ),
                    {"t": TENANT_CONTEXT_TABLE},
                ).scalars()
            )
            assert APP_PLATFORM_ROLE in delete_grants, (
                "app_platform must hold DELETE on app_tenant_context at "
                "20260608_0001 so the missing-helper fallback in the "
                "session hook can clear stale rows."
            )
    finally:
        engine.dispose()

    # 2) Upgrade to head (20260609_0002): helper IS installed and GRANTed to
    #    app_platform.
    command.upgrade(cfg, "head")
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            assert _function_exists(conn, TENANT_CONTEXT_CLEARER) is True, (
                "head must expose clear_app_current_tenant_id; "
                "20260609_0002 is its sole owner."
            )
            grant_holder = conn.execute(
                sa.text(
                    "SELECT grantee FROM information_schema.routine_privileges "
                    "WHERE routine_name = :fn AND privilege_type = 'EXECUTE'"
                ),
                {"fn": TENANT_CONTEXT_CLEARER},
            ).scalars()
            grants = set(grant_holder)
            assert APP_PLATFORM_ROLE in grants, (
                "app_platform must hold EXECUTE on clear_app_current_tenant_id "
                "so the elevated session hook can call it."
            )
    finally:
        engine.dispose()

    # 3) Downgrade to 20260609_0001: helper is dropped by 20260609_0002's
    #    downgrade (its sole-owner contract). The DB still has the trusted
    #    context table and the setter/getter (owned by 20260608_0001).
    command.downgrade(cfg, "20260609_0001")
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            assert _function_exists(conn, TENANT_CONTEXT_CLEARER) is False, (
                "Downgrading 20260609_0002 must drop clear_app_current_tenant_id "
                "— that is its sole-owner contract."
            )
    finally:
        engine.dispose()

    # 4) Re-upgrade to head: helper is reinstalled cleanly (no stale object).
    command.upgrade(cfg, "head")
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            assert _function_exists(conn, TENANT_CONTEXT_CLEARER) is True
    finally:
        engine.dispose()
    # Leave the DB at head for any subsequent test that depends on the
    # baseline schema/role state.
