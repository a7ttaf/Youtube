"""Postgres-only end-to-end proof of the restricted-login deploy model.

The production app connects as a dedicated NON-owner/NON-superuser login that is
granted membership in the lane roles with ``WITH INHERIT FALSE, SET TRUE``. That
means the login holds NO passive privileges and must ``SET ROLE`` into a lane
per transaction. This test stands up exactly that login against the real schema
and proves:

  (a) INHERIT FALSE — without any SET ROLE, a tenant-table query is denied
      (the login passively holds nothing). This is the case the wiring bug hit:
      the resolver ran on the bare login and got permission-denied.
  (b) platform lane — SET ROLE app_platform can read ``tenants`` (what the
      resolver actually does before tenant context exists).
  (c) tenant lane — SET ROLE app_tenant after populating the trusted tenant
      context through the platform lane reads an RLS-scoped table without
      permission error.

Run as the superuser test connection (it must CREATE/DROP ROLE). The throwaway
login is created idempotently and dropped in teardown; the DB is left at head.
"""

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from tests.db._postgres_helpers import require_postgres_url

_LOGIN = "app_login_test"
_LOGIN_PW = "pw"
_UMS_TENANT = "00000000-0000-0000-0000-000000000001"


def _upgrade(url: str) -> None:
    """Apply the tenant-RLS migration to the test database."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


def _login_url(admin_url: str) -> str:
    """Swap admin credentials for the throwaway restricted login."""
    # Swap the userinfo of the admin URL for the throwaway login's credentials,
    # keeping host/port/db identical.
    eng_url = sa.make_url(admin_url)
    return eng_url.set(username=_LOGIN, password=_LOGIN_PW).render_as_string(
        hide_password=False
    )


def _create_login(admin_engine: sa.Engine) -> None:
    """Create the throwaway restricted login and grant its lane memberships."""
    # AUTOCOMMIT: CREATE/DROP ROLE and GRANT are not transactional-friendly here.
    with admin_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as conn:
        _drop_login(conn)
        conn.execute(
            sa.text(
                f"CREATE ROLE {_LOGIN} LOGIN PASSWORD '{_LOGIN_PW}' "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE"
            )
        )
        # PG 16+ membership options: no passive inheritance, but may SET ROLE.
        conn.execute(
            sa.text(
                f"GRANT app_tenant TO {_LOGIN} WITH INHERIT FALSE, SET TRUE"
            )
        )
        conn.execute(
            sa.text(
                f"GRANT app_platform TO {_LOGIN} WITH INHERIT FALSE, SET TRUE"
            )
        )


def _drop_login(conn: sa.Connection) -> None:
    """Drop the throwaway restricted login after revoking its memberships."""
    exists = conn.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": _LOGIN}
    ).first()
    if exists is None:
        return
    # Revoke memberships before DROP ROLE so no dependent privilege blocks it.
    conn.execute(sa.text(f"REVOKE app_tenant FROM {_LOGIN}"))
    conn.execute(sa.text(f"REVOKE app_platform FROM {_LOGIN}"))
    conn.execute(sa.text(f"DROP ROLE IF EXISTS {_LOGIN}"))


def test_restricted_login_lane_model_end_to_end():
    """Verify the restricted login can only act after an explicit SET ROLE."""
    admin_url = require_postgres_url()
    _upgrade(admin_url)
    admin_engine = sa.create_engine(admin_url)
    _create_login(admin_engine)

    login_engine = sa.create_engine(_login_url(admin_url))
    try:
        # (a) INHERIT FALSE: bare login, no SET ROLE -> tenant table not
        # reachable. The login passively holds no lane grants AND no schema
        # USAGE, so Postgres either denies the privilege or cannot resolve the
        # relation ("does not exist"). Either way the bare login reaches
        # nothing, which is exactly the failure the resolver wiring hit.
        denied = False
        with login_engine.connect() as conn:
            try:
                conn.execute(sa.text("SELECT 1 FROM org_units LIMIT 1"))
            except sa.exc.ProgrammingError as exc:
                msg = str(exc).lower()
                assert (
                    "permission denied" in msg or "does not exist" in msg
                ), str(exc)
                denied = True
        assert denied, (
            "bare login (INHERIT FALSE) unexpectedly read a tenant table "
            "without SET ROLE; the restricted model is not in force"
        )

        # (b) platform lane: SET ROLE app_platform -> read tenants (resolver).
        with login_engine.connect() as conn:
            conn.execute(sa.text('SET ROLE "app_platform"'))
            count = conn.execute(
                sa.text("SELECT count(*) FROM tenants")
            ).scalar()
            assert count is not None

        # (c) tenant lane: populate trusted context, then SET ROLE app_tenant.
        with login_engine.connect() as conn:
            conn.execute(sa.text('SET ROLE "app_platform"'))
            conn.execute(
                sa.text(
                    "SELECT set_app_current_tenant_id(:t)"
                ),
                {"t": _UMS_TENANT},
            )
            conn.execute(sa.text('SET ROLE "app_tenant"'))
            count = conn.execute(
                sa.text("SELECT count(*) FROM org_units")
            ).scalar()
            assert count is not None
    finally:
        login_engine.dispose()
        with admin_engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as conn:
            _drop_login(conn)
        admin_engine.dispose()
