# ============================================================================
# Purpose: Prove the P0.8/P0.9 bootstrap works against a REAL migrated
#   PostgreSQL database with FORCE ROW LEVEL SECURITY enabled — the exact
#   surface a SQLite-only test cannot reach. Halves: the script's writes succeed
#   end to end; the same write REJECTS when TENANT_CTX is absent, so the
#   tenant-context handling is proven load-bearing rather than decorative; a
#   seeded org unit deactivated in the real database is REFUSED instead of being
#   announced as healthy; a URL carrying its password as a query parameter
#   connects for real and still prints masked; and a NON-email IntegrityError
#   inside the shared bootstrap session stays a typed conflict (PostgreSQL
#   aborts the transaction after a failed INSERT, which SQLite does not — the
#   exact surface where the savepoint retry rework misreported conflicts as
#   "storage unavailable").
# Database/ORM: The full Alembic schema; UserORM, OrgUnitORM,
#   UserRoleAssignmentORM writes through the app's own tenant-lane session
#   factory, so the after_begin RLS hook in db/session.py is in the path.
# Standards: ``require_postgres_url`` raises (never skips) when
#   UMS_TEST_DATABASE_URL is unset, honouring the no-skip policy gate. Row
#   verification uses a raw connection that never opens an ORM Session, so it
#   does not SET ROLE and reads as the migration login.
# Blast Radius: Test-only. Destructively resets the disposable database's public
#   schema, which is why the URL must name a test_* / *_test database.
# Connections:
#   - File: scripts/bootstrap_operator.py -> subject.
#   - File: backend/ums_smart_revenue/db/session.py -> the after_begin hook.
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260612_0002_force_tenant_rls.py -> the FORCE flag that makes this bite.
# ============================================================================
"""PostgreSQL RLS guards for scripts/bootstrap_operator.py."""

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError

from tests.db._pg_schema_helpers import reset_public_schema
from tests.db._postgres_helpers import require_postgres_url
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.db.org_models import OrgUnitORM
from ums_smart_revenue.db.session import build_session_factory, dispose_cached_engine
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.repository import SqlAlchemyTenantRepository

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _PROJECT_ROOT / "scripts" / "bootstrap_operator.py"
_TENANT_ID = UUID(UMS_TENANT_ID)
_OPERATOR_EMAIL = "pg-ops@example.com"
_ORPHAN_UNIT_ID = UUID("00000000-0000-0000-0000-00000000abcd")


def _load_script() -> ModuleType:
    """Load bootstrap_operator.py by path, registered so dataclasses resolve."""
    spec = importlib.util.spec_from_file_location("bootstrap_operator_pg", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _alembic_config(url: str) -> Config:
    """Build an Alembic config bound to ``url`` without touching the logging tree."""
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", "backend/ums_smart_revenue/db/alembic")
    return cfg


# ============================================================================
# Purpose: Give each test in this module a freshly migrated database. The public
#   schema is dropped and recreated, then `alembic upgrade head` rebuilds it, so
#   the RLS roles, policies, FORCE flags and the P0.7 catalog seed are all real.
# Database/ORM: The whole schema.
# Standards: Engine caches are disposed on teardown so a later SQLite test does
#   not inherit a live PostgreSQL pool keyed on the same URL.
# Blast Radius: Test harness only.
# Connections:
#   - File: tests/db/_pg_schema_helpers.py -> reset_public_schema.
# ============================================================================
@pytest.fixture
def migrated_url() -> Iterator[str]:
    """Yield a freshly migrated disposable PostgreSQL URL."""
    url = require_postgres_url()
    reset_public_schema(url)
    command.upgrade(_alembic_config(url), "head")
    try:
        yield url
    finally:
        dispose_cached_engine(url)


def _scalar(url: str, statement: str, **params: object) -> object:
    """Run one read on a raw connection that never opens an ORM Session.

    A raw ``Connection`` does not trigger the ``after_begin`` hook that switches
    into ``app_tenant``, so this reads as the migration login and can see rows
    regardless of tenant context — which is what makes it a trustworthy verifier
    of what the script actually wrote.
    """
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            return connection.execute(sa.text(statement), params).scalar()
    finally:
        engine.dispose()


def _execute(url: str, statement: str, **params: object) -> None:
    """Run one write on a raw connection, as the migration login.

    Raw ``Connection`` again: it never opens an ORM Session, so it does not
    ``SET ROLE app_tenant`` and the write is not filtered by the tenant policy.
    That is what lets the fixture plant a state the script itself refuses to
    create.
    """
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(statement), params)
    finally:
        engine.dispose()


def _keys(url: str, table: str) -> set[str]:
    """Return the ``key`` column of one platform-wide catalog table.

    ``table`` is never operator input — it is a literal at each call site — so
    interpolating it into the statement cannot carry untrusted text.
    """
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            return set(connection.execute(sa.text(f"SELECT key FROM {table}")).scalars())
    finally:
        engine.dispose()


def test_bootstrap_writes_through_force_rls(migrated_url):
    """The script creates the user, the role grant, and the org skeleton on PostgreSQL."""
    module = _load_script()

    exit_code = module.main(
        [
            "--database-url",
            migrated_url,
            "--email",
            _OPERATOR_EMAIL,
            "--role",
            "finance_admin",
            "--org-skeleton",
        ]
    )

    assert exit_code == 0
    assert (
        _scalar(
            migrated_url,
            "SELECT count(*) FROM users WHERE lower(email) = :email",
            email=_OPERATOR_EMAIL,
        )
        == 1
    )
    assert _scalar(migrated_url, "SELECT count(*) FROM org_units") == 2
    assert (
        _scalar(
            migrated_url,
            "SELECT count(*) FROM org_units child JOIN org_units parent "
            "ON child.parent_id = parent.id "
            "WHERE child.type = 'COMPANY' AND parent.type = 'SECTOR'",
        )
        == 1
    )
    assert (
        _scalar(
            migrated_url,
            "SELECT count(*) FROM user_role_assignments "
            "WHERE role_key = 'finance_admin' AND active = true",
        )
        == 1
    )
    # The role assignment only resolves because 20260825_0001 seeded the catalog.
    # Compared against the live registries rather than the literals 16 and 26:
    # `alembic upgrade head` on a FRESH PostgreSQL has to produce a security
    # model consistent with the Permission enum, and a set equality says which
    # keys landed where a count only says how many.
    assert _keys(migrated_url, "roles") == {role.value for role in RoleKey}
    assert _keys(migrated_url, "permissions") == {permission.value for permission in Permission}


def test_bootstrap_rerun_is_idempotent_on_postgres(migrated_url):
    """A second identical run adds no rows and still exits 0."""
    module = _load_script()
    argv = [
        "--database-url",
        migrated_url,
        "--email",
        _OPERATOR_EMAIL,
        "--role",
        "finance_admin",
        "--org-skeleton",
    ]

    assert module.main(argv) == 0
    assert module.main(argv) == 0

    assert _scalar(migrated_url, "SELECT count(*) FROM users") == 1
    assert _scalar(migrated_url, "SELECT count(*) FROM org_units") == 2
    assert _scalar(migrated_url, "SELECT count(*) FROM user_role_assignments") == 1


def test_bootstrap_refuses_an_inactive_org_unit_on_postgres(migrated_url, capsys):
    """A seeded COMPANY deactivated in the real database is refused, not blessed.

    ``active`` was missing from ``_org_unit_drift``, so this exact state exited
    0 and printed ``EXISTING COMPANY ... parent=<sector>`` together with the
    unconditional claim that the shape clears both org issues — while the reader
    (``org/access_index.py:84`` filters ``active.is_(True)``) had already
    dropped the row and ``GET /channels/issues`` reported ``MISSING_SECTOR``.
    Run here rather than only on SQLite because this is the database the beta
    bootstraps against, and the re-run has to survive FORCE RLS to reach the
    drift check at all.
    """
    module = _load_script()
    argv = ["--database-url", migrated_url, "--email", _OPERATOR_EMAIL, "--org-skeleton"]
    assert module.main(argv) == 0
    _execute(migrated_url, "UPDATE org_units SET active = false WHERE type = 'COMPANY'")
    capsys.readouterr()

    exit_code = module.main(argv)

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "active=false" in captured.err
    assert "MISSING_SECTOR" in captured.err
    assert "Org units" not in captured.out
    assert "clears BOTH" not in captured.out
    # Refused means refused: the run reactivates nothing on its way out, and
    # adds no second skeleton beside the one it declined to adopt.
    assert _scalar(migrated_url, "SELECT count(*) FROM org_units") == 2
    assert _scalar(migrated_url, "SELECT count(*) FROM org_units WHERE active = false") == 1


def test_summary_withholds_a_query_string_password_on_postgres(migrated_url, capsys):
    """The live-proved bypass: ``?password=`` connects, and must print masked.

    ``postgresql+psycopg://user@host:port/db?password=...`` is a legitimate
    psycopg/SQLAlchemy URL — not malformed input. The previous redaction masked
    only ``urlsplit``'s userinfo password, so this URL connected, exited 0, and
    printed the credential into the operator's console and into any captured
    runbook log. Rebuilt here from the fixture's own URL so the run is a real
    connection rather than a rehearsal against a string nothing accepts.
    """
    module = _load_script()
    source = make_url(migrated_url)
    # When the fixture URL carries no password the server is not asking for one,
    # so any value connects and the assertion below is still about a secret the
    # summary must not echo. The leak SHAPE is asserted rather than the bare
    # value: a short container password like "ums" also occurs inside the
    # database name, so a substring test would fail for the wrong reason.
    secret = source.password or "probe-secret-not-in-output"
    # A second, deliberately distinctive value carried by a real libpq parameter
    # that is NOT on the allow-list. It proves the default-deny masking with no
    # substring collision anywhere in the summary.
    sentinel = "ums-bootstrap-probe-9f3a2c"
    probe_url = URL.create(
        drivername=source.drivername,
        username=source.username,
        password=None,
        host=source.host,
        port=source.port,
        database=source.database,
        query={**source.query, "password": secret, "application_name": sentinel},
    ).render_as_string(hide_password=False)

    try:
        exit_code = module.main(["--database-url", probe_url, "--email", _OPERATOR_EMAIL])
    finally:
        dispose_cached_engine(probe_url)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert _scalar(migrated_url, "SELECT count(*) FROM users") == 1
    assert f"password={secret}" not in output
    assert sentinel not in output
    assert "password=REDACTED" in output
    assert "application_name=REDACTED" in output


def _insert_org_unit(session) -> None:
    """Insert one detached org unit through the tenant-lane session."""
    session.add(
        OrgUnitORM(
            id=_ORPHAN_UNIT_ID,
            tenant_id=_TENANT_ID,
            parent_id=None,
            type="SECTOR",
            name="RLS probe",
            active=True,
        )
    )
    session.flush()


def test_tenant_scoped_insert_is_rejected_without_tenant_context(migrated_url):
    """Without TENANT_CTX the identical insert is rejected by the RLS policy.

    This is the reject half of the matrix: it proves the tenant-context handling
    in bootstrap_operator.py is load-bearing, not decoration. ``seed_demo_month``
    omits it, which is why copying that script would have failed here.
    """
    session_factory = build_session_factory(migrated_url)

    assert TENANT_CTX.get() is None
    with session_factory() as session, pytest.raises(SQLAlchemyError) as excinfo:
        _insert_org_unit(session)

    assert "row-level security" in str(excinfo.value).lower()
    assert _scalar(migrated_url, "SELECT count(*) FROM org_units") == 0


def test_non_email_integrity_error_stays_a_typed_conflict_on_postgres(migrated_url, monkeypatch):
    """A real non-email IntegrityError maps to the typed conflict, keeping siblings.

    Codex P1 regression (3416d8d46): the storage-retry savepoint rework left the
    failed INSERT's ABORTED PostgreSQL transaction in place while
    ``create_user``'s diagnosis ran its ``_email_exists`` SELECT, so every
    non-email IntegrityError surfaced as ``UserAccountStorageError`` ("storage
    unavailable") instead of ``UserAccountConflictError``. The write now runs in
    its own savepoint, rolled back BEFORE the diagnosis queries, so the typed
    contract holds on the backend that actually aborts transactions — and the
    sibling account flushed EARLIER on the same session survives the conflict,
    which is the sibling-preservation property the savepoint rework exists to
    protect. Only the id GENERATOR is forged (to collide on the primary key);
    the write, the failure, and the diagnosis all run against the real database.
    """
    import ums_smart_revenue.auth.users as auth_users

    planted_id = UUID("00000000-0000-0000-0000-00000000c011")
    _execute(
        migrated_url,
        "INSERT INTO users (id, tenant_id, email, display_name) "
        "VALUES (:id, :tenant_id, :email, :display_name)",
        id=planted_id,
        tenant_id=_TENANT_ID,
        email="planted-pg@example.com",
        display_name="Planted User",
    )
    session_factory = build_session_factory(migrated_url)
    with session_factory() as lookup_session:
        tenant = SqlAlchemyTenantRepository(lookup_session).get_by_id(_TENANT_ID)

    token = TENANT_CTX.set(tenant)
    try:
        with session_factory() as session:
            repository = auth_users.SqlAlchemyUserAccountRepository(session, tenant_id=_TENANT_ID)
            repository.create_user(
                email="sibling-pg@example.com",
                display_name="Sibling User",
                is_service_account=False,
            )
            monkeypatch.setattr(auth_users, "uuid4", lambda: planted_id)
            with pytest.raises(
                auth_users.UserAccountConflictError,
                match="User account violates database constraints",
            ):
                repository.create_user(
                    email="victim-pg@example.com",
                    display_name="Victim User",
                    is_service_account=False,
                )
            monkeypatch.undo()
            session.commit()
    finally:
        TENANT_CTX.reset(token)

    assert (
        _scalar(
            migrated_url,
            "SELECT count(*) FROM users WHERE email = :email",
            email="sibling-pg@example.com",
        )
        == 1
    )
    assert (
        _scalar(
            migrated_url,
            "SELECT count(*) FROM users WHERE email = :email",
            email="victim-pg@example.com",
        )
        == 0
    )


def test_tenant_scoped_insert_is_accepted_with_tenant_context(migrated_url):
    """With TENANT_CTX set — as the script sets it — the same insert succeeds."""
    session_factory = build_session_factory(migrated_url)
    with session_factory() as lookup_session:
        tenant = SqlAlchemyTenantRepository(lookup_session).get_by_id(_TENANT_ID)

    token = TENANT_CTX.set(tenant)
    try:
        with session_factory() as session:
            _insert_org_unit(session)
            session.commit()
    finally:
        TENANT_CTX.reset(token)

    assert _scalar(migrated_url, "SELECT count(*) FROM org_units") == 1
    assert TENANT_CTX.get() is None
