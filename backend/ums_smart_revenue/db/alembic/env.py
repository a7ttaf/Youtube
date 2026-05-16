import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_engine_from_config

from ums_smart_revenue.db.explanation_models import ExplanationBase
from ums_smart_revenue.db.finance_models import FinanceBase
from ums_smart_revenue.db.org_models import OrgBase
from ums_smart_revenue.db.report_models import ReportBase
from ums_smart_revenue.db.security_models import SecurityBase


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = [
    SecurityBase.metadata,
    OrgBase.metadata,
    FinanceBase.metadata,
    ReportBase.metadata,
    ExplanationBase.metadata,
]


def get_database_url() -> str:
    url = os.environ.get("UMS_DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "Database URL not configured. Set UMS_DATABASE_URL or sqlalchemy.url in alembic.ini."
        )
    return url


def is_async_database_url(url: str) -> bool:
    return make_url(url).drivername in {"postgresql+asyncpg", "sqlite+aiosqlite"}


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


async def run_async_migrations_online(configuration: dict[str, str]) -> None:
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_database_url()

    if is_async_database_url(configuration["sqlalchemy.url"]):
        asyncio.run(run_async_migrations_online(configuration))
        return

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
