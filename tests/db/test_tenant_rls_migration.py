import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from tests.db._postgres_helpers import require_postgres_url

from ums_smart_revenue.db.rls import (
    APP_PLATFORM_ROLE,
    APP_TENANT_ROLE,
    TENANT_SCOPED_TABLES,
    tenant_rls_policy_name,
)


def _alembic_config(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_rls_migration_creates_roles_policies_and_grants():
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
            # app_platform bypasses RLS; app_tenant does not.
            bypass = dict(
                conn.execute(
                    sa.text("SELECT rolname, rolbypassrls FROM pg_roles")
                ).all()
            )
            assert bypass[APP_PLATFORM_ROLE] is True
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
