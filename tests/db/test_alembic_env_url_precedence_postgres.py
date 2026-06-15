"""End-to-end Postgres proof that the alembic env.py URL footgun is closed.

The pure resolver is unit-tested in ``test_migration_url_resolution.py``. This
test exercises the real Alembic command boundary: with a DECOY ``UMS_DATABASE_URL``
exported to an unreachable database, a programmatically-injected ``sqlalchemy.url``
(every migration round-trip fixture's pattern) must still win, so
``command.upgrade`` targets the configured disposable test DB.

If the footgun were live (env var wins unconditionally), ``command.upgrade`` would
attempt the decoy and raise ``OperationalError`` — so this is a genuine regression
test, not just a smoke check.

``require_postgres_url()`` raises (never skips) when ``UMS_TEST_DATABASE_URL`` is
unset, preserving the no-skip policy; this module runs only in the clean-room PG
gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _pg_schema_helpers import reset_public_schema
from _postgres_helpers import require_postgres_url  # sibling module via pytest prepend mode
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).resolve().parents[2]
# Syntactically valid but unreachable: a live footgun would try this and fail.
DECOY_URL = "postgresql+psycopg://decoy:decoy@127.0.0.1:1/decoy_db"


@pytest.fixture
def postgres_url() -> str:
    return require_postgres_url()


@pytest.fixture
def programmatic_config(postgres_url: str) -> Config:
    """A Config built in code (no ini path) pointed at the disposable test DB."""
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", postgres_url)
    cfg.set_main_option(
        "script_location",
        str(REPO_ROOT / "backend" / "ums_smart_revenue" / "db" / "alembic"),
    )
    assert cfg.config_file_name is None
    return cfg


@pytest.fixture
def ini_config_with_injection(postgres_url: str) -> Config:
    """An ini-backed Config whose url is overridden in code (tenancy-test pattern).

    Mirrors tests/tenancy/test_isolation.py et al.: ``Config(alembic.ini)`` (the
    ini declares the localhost placeholder) followed by an in-code url override.
    """
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", postgres_url)
    assert cfg.config_file_name is not None
    return cfg


def _migrated_table_names(postgres_url: str) -> list[str]:
    engine = create_engine(postgres_url)
    try:
        return inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_decoy_env_var_does_not_retarget_programmatic_migration(
    monkeypatch: pytest.MonkeyPatch,
    postgres_url: str,
    programmatic_config: Config,
) -> None:
    """A decoy UMS_DATABASE_URL must not hijack a no-ini programmatic migration.

    With the fix, ``command.upgrade`` resolves to the injected test URL and the
    security-foundation ``users`` table appears in the test DB. Without the fix
    it would resolve to the unreachable decoy and raise before creating anything.
    """
    monkeypatch.setenv("UMS_DATABASE_URL", DECOY_URL)
    reset_public_schema(postgres_url)

    command.upgrade(programmatic_config, "head")

    # Proof the migrations ran against the configured test DB, not the decoy.
    assert "users" in _migrated_table_names(postgres_url)


def test_decoy_env_var_does_not_retarget_ini_config_with_injection(
    monkeypatch: pytest.MonkeyPatch,
    postgres_url: str,
    ini_config_with_injection: Config,
) -> None:
    """The P1 case: an ini-backed Config whose url is overridden in code.

    This is the pattern the original spec missed (it assumed every migration test
    builds ``Config()`` with no ini). The in-code override differs from the ini
    placeholder, so it must still win over the decoy ``UMS_DATABASE_URL`` and the
    upgrade must hit the configured test DB.
    """
    monkeypatch.setenv("UMS_DATABASE_URL", DECOY_URL)
    reset_public_schema(postgres_url)

    command.upgrade(ini_config_with_injection, "head")

    assert "users" in _migrated_table_names(postgres_url)
