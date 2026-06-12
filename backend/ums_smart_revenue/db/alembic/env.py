import os
from importlib import import_module

from alembic import context
from sqlalchemy import engine_from_config, pool

from ums_smart_revenue.db.explanation_models import ExplanationBase
from ums_smart_revenue.db.finance_models import FinanceBase
from ums_smart_revenue.db.migration_url import resolve_database_url
from ums_smart_revenue.db.org_models import OrgBase
from ums_smart_revenue.db.report_models import ReportBase
from ums_smart_revenue.db.security_models import SecurityBase
from ums_smart_revenue.db.tenant_models import TenantBase

config = context.config


# ============================================================================
# Purpose: Import model modules whose table registrations live on shared
#          metadata bases used by Alembic autogenerate and upgrade commands.
# Database/ORM: connector_models registers connector run tables on ReportBase;
#               source_models registers Google source rows on FinanceBase.
# Standards: Explicit side-effect imports through import_module so analyzers do
#            not mistake registration imports for dead code.
# Blast Radius: Alembic migration metadata only; runtime API behavior unchanged.
# Connections:
#   - File: backend/ums_smart_revenue/db/connector_models.py -> B2 connector
#     run tables on ReportBase.metadata.
#   - File: backend/ums_smart_revenue/db/source_models.py -> Google source row
#     tables on FinanceBase.metadata.
# ============================================================================
def _register_migration_tables() -> None:
    import_module("ums_smart_revenue.db.connector_models")
    import_module("ums_smart_revenue.db.source_models")


_register_migration_tables()

target_metadata = [
    SecurityBase.metadata,
    OrgBase.metadata,
    FinanceBase.metadata,
    ReportBase.metadata,
    ExplanationBase.metadata,
    TenantBase.metadata,
]


# ============================================================================
# Purpose: Resolve the database URL Alembic migrates against, delegating the
#          precedence rule to the pure, unit-tested resolver seam.
# Database/ORM: None — returns a connection URL string.
# Standards: Thin delegate; the precedence contract (and its tests) live in
#            db/migration_url.py so this import-time module stays trivial.
# Blast Radius: Migration entry point for prod + tests. No schema/data change.
# Connections:
#   - File: backend/ums_smart_revenue/db/migration_url.py -> resolve_database_url
#     owns the config_file_name-gated precedence (FIX: an ambient
#     UMS_DATABASE_URL no longer silently overrides a programmatically-injected
#     sqlalchemy.url, which was retargeting migration tests to the wrong DB).
# ============================================================================
def get_database_url() -> str:
    return resolve_database_url(config, os.environ)


def run_migrations_offline() -> None:
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_database_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        do_run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
