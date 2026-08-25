# ============================================================================
# Purpose: Cover the P0.8/P0.9 operator bootstrap end to end on SQLite — account
#   creation, idempotency, the loud server-generated-id output (audit H3), the
#   optional global role grant, tenant-context hygiene, and the org skeleton's
#   SHAPE: a mapped channel must produce NEITHER MISSING_COMPANY NOR
#   MISSING_SECTOR, with a negative control proving the assertion can fail.
# Database/ORM: TenantORM, UserORM, OrgUnitORM, YouTubeChannelORM,
#   RolePermissionAssignmentORM and the rest of the security catalog, against a
#   real on-disk SQLite database driven through the script's own ``main``.
# Standards: The script is loaded by path (scripts/ is not an importable
#   package), mirroring tests/scripts/test_run_deduction_ingestion_cli.py.
# Blast Radius: Test-only.
# Connections:
#   - File: scripts/bootstrap_operator.py -> subject.
#   - File: backend/ums_smart_revenue/org/channel_issues.py -> the issue codes.
#   - File: Docs/21_BETA_IMPLEMENTATION_PLAN.md -> P0.8 / P0.9.
# ============================================================================
"""End-to-end guards for scripts/bootstrap_operator.py on SQLite."""

import importlib.util
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.api.registry_dependencies import (
    current_channel_registry,
    sql_channel_registry_from_session,
)
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.db.security_models import (
    AuditLogORM,
    PermissionORM,
    RoleORM,
    RolePermissionAssignmentORM,
    SecurityBase,
    UserORM,
    UserRoleAssignmentORM,
)
from ums_smart_revenue.db.session import dispose_cached_engine
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _PROJECT_ROOT / "scripts" / "bootstrap_operator.py"
_MIGRATION_PATH = (
    _PROJECT_ROOT
    / "backend/ums_smart_revenue/db/alembic/versions/"
    / "20260825_0001_security_role_permission_seed.py"
)
_TENANT_ID = UUID(UMS_TENANT_ID)
_OPERATOR_EMAIL = "ops@example.com"


def _load_script() -> ModuleType:
    """Load bootstrap_operator.py by path (scripts/ is not an import package).

    The module is registered in ``sys.modules`` BEFORE execution because the
    script declares dataclasses under ``from __future__ import annotations``:
    ``dataclasses`` resolves those string annotations through
    ``sys.modules[cls.__module__]``, which is ``None`` for a module that is
    executed without being registered first.
    """
    spec = importlib.util.spec_from_file_location("bootstrap_operator", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_database(tmp_path: Path, *, with_org_schema: bool = True) -> str:
    """Create a fresh SQLite database carrying the tenant/security/org schema.

    ``with_org_schema=False`` leaves ``org_units`` absent, which is how the
    direct-ORM write in ``_ensure_org_unit`` is made to raise a real
    ``SQLAlchemyError`` without stubbing anything.
    """
    database_url = f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"
    engine = create_engine(database_url)
    TenantBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    if with_org_schema:
        OrgBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            TenantORM(
                id=_TENANT_ID,
                slug="ums",
                display_name="UMS",
                primary_currency="USD",
                status="ACTIVE",
            )
        )
        session.commit()
    engine.dispose()
    return database_url


def _seed_role_catalog(database_url: str) -> None:
    """Apply the P0.7 seed migration so role assignment has its FK parents."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    spec = importlib.util.spec_from_file_location("m_20260825_0001", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine(database_url)
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
    engine.dispose()


def _run(module: ModuleType, database_url: str, *args: str) -> int:
    """Invoke the script's main with the shared database and tenant arguments."""
    try:
        return module.main(["--database-url", database_url, *args])
    finally:
        dispose_cached_engine(database_url)


def _users(database_url: str) -> list[UserORM]:
    """Return every user row in the database, ordered by email."""
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            return list(session.scalars(select(UserORM).order_by(UserORM.email)).all())
    finally:
        engine.dispose()


def _org_units(database_url: str) -> list[OrgUnitORM]:
    """Return every org unit row in the database, ordered by type."""
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            return list(session.scalars(select(OrgUnitORM).order_by(OrgUnitORM.type)).all())
    finally:
        engine.dispose()


def test_bootstrap_creates_the_operator_and_prints_the_server_generated_id(tmp_path, capsys):
    """The first run creates one account and prints its id as the X-User-ID value."""
    module = _load_script()
    database_url = _make_database(tmp_path)

    exit_code = _run(module, database_url, "--email", _OPERATOR_EMAIL)

    assert exit_code == 0
    users = _users(database_url)
    assert [user.email for user in users] == [_OPERATOR_EMAIL]
    assert users[0].tenant_id == _TENANT_ID
    assert users[0].display_name == "ops"

    output = capsys.readouterr().out
    assert f"X-User-ID: {users[0].id}" in output
    assert "COPY THESE IDS EXACTLY" in output
    assert "generated by the SERVER" in output
    # No role was requested, so the run must say so rather than imply access.
    assert "NO ROLE ASSIGNED." in output


def test_bootstrap_is_idempotent_and_reports_the_existing_id(tmp_path, capsys):
    """A second run creates nothing and still reports the original id."""
    module = _load_script()
    database_url = _make_database(tmp_path)

    assert _run(module, database_url, "--email", _OPERATOR_EMAIL) == 0
    first_id = _users(database_url)[0].id
    capsys.readouterr()

    assert _run(module, database_url, "--email", _OPERATOR_EMAIL) == 0

    users = _users(database_url)
    assert len(users) == 1
    assert users[0].id == first_id
    output = capsys.readouterr().out
    assert "EXISTING" in output
    assert f"X-User-ID: {first_id}" in output


def test_bootstrap_refuses_a_disabled_existing_account(tmp_path, capsys):
    """A disabled row must not become EXISTING and must not receive --role."""
    module = _load_script()
    database_url = _make_database(tmp_path)
    _seed_role_catalog(database_url)

    assert _run(module, database_url, "--email", _OPERATOR_EMAIL) == 0
    users = _users(database_url)
    assert len(users) == 1
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            row = session.get(UserORM, users[0].id)
            assert row is not None
            row.status = "disabled"
            session.commit()
    finally:
        engine.dispose()
    capsys.readouterr()

    exit_code = _run(
        module,
        database_url,
        "--email",
        _OPERATOR_EMAIL,
        "--role",
        "finance_admin",
    )

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "UserAccountValidationError" in err
    assert "status=disabled" in err
    assert "Nothing was changed" in err
    final_users = _users(database_url)
    assert len(final_users) == 1
    assert final_users[0].status == "disabled"
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            assignments = list(session.scalars(select(UserRoleAssignmentORM)).all())
            assert assignments == []
    finally:
        engine.dispose()


def test_bootstrap_creates_a_second_user_in_one_run(tmp_path, capsys):
    """Repeating --email creates every requested account with its own id."""
    module = _load_script()
    database_url = _make_database(tmp_path)

    exit_code = _run(
        module,
        database_url,
        "--email",
        _OPERATOR_EMAIL,
        "--email",
        "finance@example.com",
        "--display-name",
        "Operations",
        "--display-name",
        "Finance",
    )

    assert exit_code == 0
    users = _users(database_url)
    assert [user.email for user in users] == ["finance@example.com", _OPERATOR_EMAIL]
    assert {user.display_name for user in users} == {"Operations", "Finance"}
    assert len({user.id for user in users}) == 2
    output = capsys.readouterr().out
    for user in users:
        assert f"X-User-ID: {user.id}" in output


def test_bootstrap_keeps_earlier_accounts_after_transient_storage_retry(
    tmp_path, monkeypatch, capsys
):
    """A retryable flush on the second account must not drop the first flush.

    Codex P1: repository storage retries used session.rollback(), which discarded
    earlier accounts in the shared bootstrap transaction while outcomes still
    reported CREATED. Savepoint-scoped retries keep sibling writes.
    """
    from sqlalchemy.exc import OperationalError

    module = _load_script()
    database_url = _make_database(tmp_path)
    flush_calls = {"n": 0}
    real_flush = Session.flush

    def flaky_flush(self: Session, objects: Iterable[Any] | None = None) -> None:
        """Fail the second flush once, then delegate to the real Session.flush."""
        flush_calls["n"] += 1
        if flush_calls["n"] == 2:
            raise OperationalError("INSERT", {}, Exception("simulated transient failure"))
        real_flush(self, objects)

    monkeypatch.setattr(Session, "flush", flaky_flush)
    exit_code = _run(
        module,
        database_url,
        "--email",
        _OPERATOR_EMAIL,
        "--email",
        "finance@example.com",
        "--display-name",
        "Operations",
        "--display-name",
        "Finance",
    )

    assert exit_code == 0
    assert flush_calls["n"] >= 3
    users = _users(database_url)
    assert [user.email for user in users] == ["finance@example.com", _OPERATOR_EMAIL]
    output = capsys.readouterr().out
    assert "CREATED" in output
    for user in users:
        assert f"X-User-ID: {user.id}" in output


def test_bootstrap_rejects_mismatched_display_name_count(tmp_path):
    """Two emails and one display name is an operator error, not a silent pairing."""
    module = _load_script()
    database_url = _make_database(tmp_path)

    try:
        module.main(
            [
                "--database-url",
                database_url,
                "--email",
                _OPERATOR_EMAIL,
                "--email",
                "finance@example.com",
                "--display-name",
                "Operations",
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("argparse should reject a mismatched --display-name count")
    finally:
        dispose_cached_engine(database_url)


def test_bootstrap_assigns_the_requested_global_role(tmp_path, capsys):
    """--role grants the role at global scope and is idempotent on a re-run."""
    module = _load_script()
    database_url = _make_database(tmp_path)
    _seed_role_catalog(database_url)

    assert _run(module, database_url, "--email", _OPERATOR_EMAIL, "--role", "finance_admin") == 0
    capsys.readouterr()
    assert _run(module, database_url, "--email", _OPERATOR_EMAIL, "--role", "finance_admin") == 0

    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            assignments = list(session.scalars(select(UserRoleAssignmentORM)).all())
            catalog_pairs = session.scalars(
                select(RolePermissionAssignmentORM.permission_key).where(
                    RolePermissionAssignmentORM.role_key == "finance_admin"
                )
            ).all()
            audit_rows = list(
                session.scalars(
                    select(AuditLogORM).where(AuditLogORM.event_type == "USER_ROLE_CHANGED")
                ).all()
            )
    finally:
        engine.dispose()

    assert len(assignments) == 1
    assert assignments[0].role_key == "finance_admin"
    assert assignments[0].active is True
    # The role assignment is only useful because P0.7 seeded the catalog edges.
    assert "finance.view_revenue" in set(catalog_pairs)
    # One audit row for the first grant; re-run must not emit a second event.
    assert len(audit_rows) == 1
    audit = audit_rows[0]
    assert audit.reason == "operator bootstrap"
    assert audit.entity_type == "user_role_assignment"
    assert audit.sensitive is True
    assert audit.user_id == assignments[0].user_id
    assert audit.details.get("action") == "assigned"
    assert audit.details.get("role_key") == "finance_admin"
    assert audit.details.get("scope_type") == "global"
    assert "EXISTING" in capsys.readouterr().out


def test_bootstrap_without_role_emits_no_role_audit(tmp_path):
    """Account-only bootstrap must not write USER_ROLE_CHANGED."""
    module = _load_script()
    database_url = _make_database(tmp_path)

    assert _run(module, database_url, "--email", _OPERATOR_EMAIL) == 0

    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            audit_rows = list(
                session.scalars(
                    select(AuditLogORM).where(AuditLogORM.event_type == "USER_ROLE_CHANGED")
                ).all()
            )
    finally:
        engine.dispose()

    assert audit_rows == []


def test_bootstrap_refuses_a_role_before_the_seed_migration_ran(tmp_path, capsys):
    """Without the P0.7 catalog the run fails loudly instead of an FK error."""
    module = _load_script()
    database_url = _make_database(tmp_path)

    exit_code = _run(module, database_url, "--email", _OPERATOR_EMAIL, "--role", "finance_admin")

    assert exit_code == 2
    assert "alembic upgrade head" in capsys.readouterr().err
    # The whole run is one transaction, so the account is not left half-created.
    assert _users(database_url) == []


def test_bootstrap_rejects_an_unknown_role_key(tmp_path, capsys):
    """An unknown --role names the valid keys instead of failing at the database."""
    module = _load_script()
    database_url = _make_database(tmp_path)
    _seed_role_catalog(database_url)

    exit_code = _run(module, database_url, "--email", _OPERATOR_EMAIL, "--role", "not_a_role")

    assert exit_code == 2
    assert "--role must be one of" in capsys.readouterr().err


def test_bootstrap_rejects_a_missing_tenant(tmp_path, capsys):
    """A tenant id with no row fails closed and points at the migration."""
    module = _load_script()
    database_url = _make_database(tmp_path)

    exit_code = _run(
        module,
        database_url,
        "--email",
        _OPERATOR_EMAIL,
        "--tenant",
        "00000000-0000-0000-0000-0000000000ff",
    )

    assert exit_code == 2
    assert "No tenant with id" in capsys.readouterr().err


def test_bootstrap_rejects_an_unmigrated_database(tmp_path, capsys):
    """Pointing the script at a database with no schema is an actionable error.

    The raw failure is "no such table: tenants" / "relation tenants does not
    exist", which reads as a bug in the script rather than a missing migration.
    """
    module = _load_script()
    database_url = f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"

    exit_code = _run(module, database_url, "--email", _OPERATOR_EMAIL)

    assert exit_code == 2
    stderr = capsys.readouterr().err
    assert "could not read the tenants table" in stderr
    assert "alembic upgrade head" in stderr


def test_bootstrap_does_not_leak_tenant_context_into_the_process(tmp_path):
    """TENANT_CTX is reset on the way out, exactly as the middleware does it."""
    module = _load_script()
    database_url = _make_database(tmp_path)
    assert get_current_tenant() is None

    assert _run(module, database_url, "--email", _OPERATOR_EMAIL, "--org-skeleton") == 0

    assert get_current_tenant() is None


def test_bootstrap_org_skeleton_creates_a_company_parented_to_a_sector(tmp_path, capsys):
    """--org-skeleton writes exactly two rows, with the COMPANY under the SECTOR."""
    module = _load_script()
    database_url = _make_database(tmp_path)

    assert _run(module, database_url, "--email", _OPERATOR_EMAIL, "--org-skeleton") == 0

    units = {unit.type: unit for unit in _org_units(database_url)}
    assert set(units) == {"SECTOR", "COMPANY"}
    assert units["SECTOR"].parent_id is None
    assert units["COMPANY"].parent_id == units["SECTOR"].id
    assert units["COMPANY"].tenant_id == _TENANT_ID
    assert units["SECTOR"].active is True

    output = capsys.readouterr().out
    assert str(units["COMPANY"].id) in output
    assert "no bulk mapping endpoint" in output

    # Re-running must not add a third unit.
    assert _run(module, database_url, "--email", _OPERATOR_EMAIL, "--org-skeleton") == 0
    assert len(_org_units(database_url)) == 2


def test_bootstrap_refuses_a_rename_instead_of_reporting_a_false_one(tmp_path, capsys):
    """A re-run with a different --sector-name is refused, and nothing is renamed.

    The previous revision built the EXISTING outcome from the CLI ARGUMENTS, so
    this exact re-run exited 0 and printed "EXISTING SECTOR name='RENAMED
    SECTOR'" while the database still held 'Default Sector'. The operator was
    told a rename had happened that never had, and every UI screen disagreed
    with the console with no signal anywhere. Refusing is the deliberate choice
    over applying: this script runs before first login, so there is no actor to
    attribute an org-unit rename to, and a later re-run that merely forgot the
    flag would otherwise reset a real name back to the default.
    """
    module = _load_script()
    database_url = _make_database(tmp_path)
    assert _run(module, database_url, "--email", _OPERATOR_EMAIL, "--org-skeleton") == 0
    capsys.readouterr()

    exit_code = _run(
        module,
        database_url,
        "--email",
        _OPERATOR_EMAIL,
        "--org-skeleton",
        "--sector-name",
        "RENAMED SECTOR",
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "never renames them" in captured.err
    assert "Default Sector" in captured.err
    # The refusal names the request in the error, but must never print it in the
    # summary as though the database had taken it.
    assert "RENAMED SECTOR" not in captured.out
    units = {unit.type: unit for unit in _org_units(database_url)}
    assert units["SECTOR"].name == "Default Sector"
    assert units["COMPANY"].name == "Default Company"


def test_bootstrap_refuses_a_company_whose_parent_link_is_wrong(tmp_path, capsys):
    """A stored COMPANY not parented to the seeded SECTOR is refused, not glossed over.

    This is the assertion that fails if the company's parent link is wrong. A
    COMPANY without that SECTOR parent is absent from
    ``OrgAccessIndex.company_sector``, so a mapped channel swaps MISSING_COMPANY
    for MISSING_SECTOR — the same HIGH issue under a different label. The
    previous revision reported ``parent_id`` from the CLI-derived sector id
    rather than from the row, so a database in exactly this state printed a
    healthy-looking parent the row did not have.
    """
    module = _load_script()
    database_url = _make_database(tmp_path)
    assert _run(module, database_url, "--email", _OPERATOR_EMAIL, "--org-skeleton") == 0
    capsys.readouterr()

    # Break exactly the link the org skeleton exists to create.
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            company = session.scalars(select(OrgUnitORM).where(OrgUnitORM.type == "COMPANY")).one()
            company.parent_id = None
            session.commit()
    finally:
        engine.dispose()

    exit_code = _run(module, database_url, "--email", _OPERATOR_EMAIL, "--org-skeleton")

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "parent_id" in captured.err
    assert "MISSING_SECTOR" in captured.err
    # No summary at all, rather than a summary claiming a parent that is absent.
    assert "Org units" not in captured.out


def test_bootstrap_refuses_an_org_unit_belonging_to_another_tenant(tmp_path, capsys):
    """A bootstrap id already held by another tenant is refused, not adopted.

    ``org_units`` is keyed on ``id`` alone (org_models.py:35), so ``session.get``
    is not tenant-filtered and RLS is the only thing normally keeping tenants
    apart — which is nothing at all on SQLite. Treating another tenant's row as
    "already exists" would skip creating THIS tenant's row and still report
    success, leaving the operator with an org skeleton they do not own.
    """
    module = _load_script()
    database_url = _make_database(tmp_path)
    other_tenant = UUID("00000000-0000-0000-0000-0000000000aa")
    # The id this run will look up, planted under a different tenant.
    sector_id = module._bootstrap_uuid(_TENANT_ID, "org", "sector")
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            session.add(
                TenantORM(
                    id=other_tenant,
                    slug="other",
                    display_name="Other",
                    primary_currency="USD",
                    status="ACTIVE",
                )
            )
            session.add(
                OrgUnitORM(
                    id=sector_id,
                    tenant_id=other_tenant,
                    parent_id=None,
                    type="SECTOR",
                    name="Default Sector",
                    active=True,
                )
            )
            session.commit()
    finally:
        engine.dispose()

    exit_code = _run(module, database_url, "--email", _OPERATOR_EMAIL, "--org-skeleton")

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "already exists under tenant" in captured.err
    assert "Org units" not in captured.out
    # Nothing was adopted and nothing new was written.
    units = _org_units(database_url)
    assert [unit.tenant_id for unit in units] == [other_tenant]


def test_org_unit_outcome_is_read_back_from_the_row_not_the_arguments(tmp_path, monkeypatch):
    """The EXISTING outcome describes the stored row even with the refusal switched off.

    The refusal in ``_org_unit_drift`` means a divergence normally never reaches
    the summary at all, which on its own would leave the read-back itself
    untested: relax the policy later to "report as ignored" and the original bug
    — a summary echoing the CLI arguments — comes back silently. This pins the
    read-back independently of the policy by disabling the refusal and asserting
    the outcome still describes the row rather than the arguments.
    """
    module = _load_script()
    database_url = _make_database(tmp_path)
    assert _run(module, database_url, "--email", _OPERATOR_EMAIL, "--org-skeleton") == 0
    sector = next((unit for unit in _org_units(database_url) if unit.type == "SECTOR"), None)
    assert sector is not None

    _deactivate(database_url, "SECTOR")
    monkeypatch.setattr(module, "_org_unit_drift", lambda *args, **kwargs: None)
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            outcome = module._ensure_org_unit(
                session,
                OrgUnitORM,
                unit_id=sector.id,
                tenant_id=_TENANT_ID,
                parent_id=uuid4(),
                unit_type="SECTOR",
                name="RENAMED SECTOR",
                name_flag="--sector-name",
            )
    finally:
        engine.dispose()

    assert outcome.created is False
    assert outcome.name == "Default Sector"
    assert outcome.parent_id is None
    assert outcome.unit_id == str(sector.id)
    assert outcome.unit_type == "SECTOR"
    # active is read back from the row too. Nothing in the CLI arguments can
    # produce False here, so an outcome that echoed the request would report
    # True and re-open the "healthy skeleton the API denies" summary.
    assert outcome.active is False


def test_bootstrap_reports_a_database_failure_instead_of_a_traceback(tmp_path, capsys):
    """A SQLAlchemyError from the direct org_units write exits 2 with no traceback.

    ``_ensure_org_unit`` writes ``org_units`` with direct ORM SQL and is not
    behind a repository, so nothing maps its failures onto a typed domain error,
    and ``UserRoleAssignmentError`` derives from ValueError rather than wrapping
    SQLAlchemy. This run used to die with a raw ``sqlalchemy.exc.OperationalError:
    (sqlite3.OperationalError) no such table: org_units``.
    """
    module = _load_script()
    database_url = _make_database(tmp_path, with_org_schema=False)

    exit_code = _run(module, database_url, "--email", _OPERATOR_EMAIL, "--org-skeleton")

    stderr = capsys.readouterr().err
    assert exit_code == 2
    assert "OperationalError" in stderr
    assert "nothing was committed" in stderr
    # The redaction promise the module states at _load_active_tenant: the
    # exception TEXT can carry the host, port, username or bound parameter
    # values, so it is named by type and never echoed.
    assert "no such table" not in stderr
    assert database_url not in stderr
    # The whole run is one transaction, so the account is not left half-created.
    assert _users(database_url) == []


def test_bootstrap_rejects_a_malformed_database_url(capsys):
    """An unparseable URL is exit 2 with guidance, not an ArgumentError traceback."""
    module = _load_script()

    exit_code = module.main(["--database-url", "not-a-url", "--email", _OPERATOR_EMAIL])

    stderr = capsys.readouterr().err
    assert exit_code == 2
    assert "ArgumentError" in stderr
    assert "not a usable SQLAlchemy URL" in stderr


def test_bootstrap_rejects_a_dialect_whose_driver_is_not_installed(capsys):
    """``postgresql+psycopg2://`` exits 2 with guidance, not a ModuleNotFoundError.

    A REGISTERED dialect whose DBAPI module is absent raises
    ``ModuleNotFoundError`` out of ``create_engine``, which is not a
    ``SQLAlchemyError``, so the handler for a malformed URL never saw it: the
    operator got a raw traceback and exit 1, breaking any runbook that branches
    on the promised exit 2. ``postgresql+psycopg2://`` is the most commonly
    pasted PostgreSQL prefix and this project ships psycopg v3
    (``pyproject.toml`` pins ``psycopg[binary]``, and
    ``tests/test_version_baseline.py`` asserts set-equality on the dependency
    list, so ``psycopg2`` cannot appear in this environment).
    """
    module = _load_script()

    exit_code = module.main(
        [
            "--database-url",
            "postgresql+psycopg2://ums_app:S3cret@db.internal.example:5432/ums",
            "--email",
            _OPERATOR_EMAIL,
        ]
    )

    stderr = capsys.readouterr().err
    assert exit_code == 2
    assert "ModuleNotFoundError" in stderr
    assert "psycopg2" in stderr
    assert "postgresql+psycopg://" in stderr
    # The same line the redaction promise has to keep safe: the message names
    # the missing module, never the URL the operator pasted a password into.
    assert "S3cret" not in stderr
    assert "db.internal.example" not in stderr


def test_bootstrap_rejects_a_non_numeric_port_without_a_traceback(capsys):
    """A non-numeric port exits 2, not a raw ValueError traceback at exit 1.

    SQLAlchemy's URL parser reaches ``int(components["port"])`` and raises a
    BARE ``ValueError`` -- not ``ArgumentError`` -- so neither the
    ``SQLAlchemyError`` nor the ``ImportError`` handler caught it and the
    operator got a traceback and exit 1. Same contract violation as the
    uninstalled-DBAPI case one exception type over: it breaks any runbook that
    branches on the promised exit 2.

    The credential assertions are the load-bearing half. This handler exists
    because the parser surprised us once, so it withholds the exception text by
    default rather than trusting that the password stays absent from it.
    """
    module = _load_script()

    exit_code = module.main(
        [
            "--database-url",
            "postgresql+psycopg://ums_app:S3cretPort@127.0.0.1:notaport/ums",
            "--email",
            _OPERATOR_EMAIL,
        ]
    )

    stderr = capsys.readouterr().err
    assert exit_code == 2
    assert "ValueError" in stderr
    assert "port is numeric" in stderr
    assert "S3cretPort" not in stderr
    assert "ums_app" not in stderr


def test_bootstrap_rejects_an_unknown_dialect_with_the_same_exit_code(capsys):
    """The accept half of C3's matrix: an unknown dialect was already exit 2.

    Without this the fix above could be read as introducing a new exit path
    rather than making an existing one reachable from a second exception type.
    """
    module = _load_script()

    exit_code = module.main(
        [
            "--database-url",
            "postgresql+nosuchdriver://ums_app:S3cret@db.internal.example:5432/ums",
            "--email",
            _OPERATOR_EMAIL,
        ]
    )

    stderr = capsys.readouterr().err
    assert exit_code == 2
    assert "NoSuchModuleError" in stderr
    assert "S3cret" not in stderr


# Every credential-bearing form a SQLAlchemy/libpq URL admits, paired with the
# secret that must not survive redaction. The first two were proved LIVE against
# a real container: ``?password=`` connects, exits 0, and the previous
# implementation printed the credential; a password containing ``/`` left
# ``urlsplit`` with no netloc ``@`` to find, so the URL came back verbatim.
# Replacing the whole function body with ``return database_url`` fails every row.
_CREDENTIAL_URLS = [
    ("postgresql+psycopg://postgres:secretpw@127.0.0.1:5432/test_ums", "secretpw"),
    ("postgresql+psycopg://postgres@127.0.0.1:55491/test_ums?password=verifypw", "verifypw"),
    ("postgresql+psycopg://user:pa/ss@host:5432/db", "pa/ss"),
    ("postgresql+psycopg://user:p%40ss@host:5432/db", "p@ss"),
    ("postgresql+psycopg://user:a%26b@host/db", "a&b"),
    ("postgresql+psycopg://:onlypw@host/db", "onlypw"),
    ("postgresql://postgres@host/db?sslpassword=abc&password=xyz", "xyz"),
    ("postgresql://postgres@host/db?sslpassword=abc&password=xyz", "abc"),
    ("postgresql:///db?host=/var/run&user=u&password=p", "password=p"),
    ("postgresql+psycopg://user:pw@host/db?options=-c%20password%3Dx", "password%3Dx"),
    ("postgresql+psycopg://user:pw@host/db?password=a&password=b", "password=a"),
    ("sqlite+pysqlite:///C:/tmp/x.db?password=leakme", "leakme"),
]


def test_redaction_removes_every_credential_bearing_form():
    """No secret in any working credential-carrying URL survives redaction."""
    module = _load_script()

    for database_url, secret in _CREDENTIAL_URLS:
        redacted = module._redact_database_url(database_url)
        assert secret not in redacted, database_url
        assert redacted != database_url, database_url


def test_redaction_withholds_a_url_whose_userinfo_cannot_be_split():
    """An unescaped ``@`` inside the password makes the whole URL unprintable.

    SQLAlchemy splits the userinfo on the FIRST ``@``, so
    ``user:p@ss@host/db`` parses as password ``p`` and host ``ss@host`` — the
    tail of the credential lands in a component the summary would otherwise
    print. There is nothing to reconstruct safely, so nothing is printed.
    """
    module = _load_script()

    redacted = module._redact_database_url("postgresql+psycopg://user:p@ss@host:5432/db")

    assert redacted == module._UNPRINTABLE_URL
    assert "ss@host" not in redacted


def test_redaction_withholds_an_unparseable_url():
    """A string SQLAlchemy cannot parse is withheld, not echoed on a guess."""
    module = _load_script()

    assert module._redact_database_url("not-a-url") == module._UNPRINTABLE_URL
    assert module._redact_database_url("") == module._UNPRINTABLE_URL


def test_redaction_withholds_a_url_whose_port_is_not_a_number():
    """``make_url`` raises a BARE ValueError here, not the documented ArgumentError.

    Found by fuzzing this function over ~8,000 inputs: ``host:notaport`` reaches
    ``int(components["port"])`` inside SQLAlchemy's ``_parse_url`` and escapes as
    ``invalid literal for int()``. A redaction guard that RAISES is worse than
    one that over-masks — it runs after the bootstrap has already committed, so
    it would turn a successful run into a traceback and exit 1 — which is why
    the undocumented failure lands on the same withheld marker as the
    documented one.
    """
    module = _load_script()

    assert (
        module._redact_database_url("postgresql://user:pw@host:notaport/db")
        == module._UNPRINTABLE_URL
    )


# Inputs that are not URLs an operator would mean to type, kept as a fixed table
# rather than a random fuzz so the suite stays deterministic. Each one was
# produced by fuzzing ``_redact_database_url``; the password is ``pw`` (or
# ``pass``) throughout so one substring assertion covers the table.
_HOSTILE_URLS = [
    "",
    "://",
    "postgresql",
    "postgresql://",
    "postgresql:///",
    "postgresql://user:pw@",
    "postgresql://:@/",
    "postgresql://user:pw@host:notaport/db",
    "postgresql://user:pw@host:99999999999999999999/db",
    "  postgresql+psycopg://user:pw@host/db  ",
    "postgresql+psycopg://user:pw@host/db?password",
    "postgresql+psycopg://user:pw@host/db?%00=%00",
    "postgresql+psycopg://user:p\nw@host/db",
    "postgresql+psycopg://üser:päss@höst/db",
    "postgresql+psycopg://user:pw@host/db?sslmode=require&sslmode=disable",
]


def test_redaction_never_raises_and_never_echoes_a_password_on_hostile_input():
    """The guard degrades to a mask or a withheld marker; it never propagates.

    This function is called AFTER the bootstrap transaction has committed, so an
    exception escaping it converts a completed run into a traceback and exit 1 —
    the failure mode is the opposite of the one it exists to prevent.
    """
    module = _load_script()

    for database_url in _HOSTILE_URLS:
        redacted = module._redact_database_url(database_url)
        assert isinstance(redacted, str), database_url
        assert "pw" not in redacted, database_url
        assert "päss" not in redacted, database_url


def test_redaction_keeps_the_parts_that_identify_the_database():
    """Redaction is not blanking: the operator can still tell which database ran.

    Reconstruction is allow-list shaped, so this is the half that proves the
    allow-list is not empty — drivername, username, host, port and database name
    all survive, and an IPv6 host keeps its brackets (the previous
    ``urlsplit``/``urlunsplit`` round trip dropped them and emitted an
    unusable ``@::1:5432``).
    """
    module = _load_script()

    redacted = module._redact_database_url(
        "postgresql+psycopg://ums_app:S3cret@db.internal.example:5432/ums"
    )

    assert redacted == "postgresql+psycopg://ums_app:REDACTED@db.internal.example:5432/ums"
    assert (
        module._redact_database_url("postgresql+psycopg://user:pw@[::1]:5432/db")
        == "postgresql+psycopg://user:REDACTED@[::1]:5432/db"
    )


def test_redaction_leaves_a_credential_free_sqlite_url_intact():
    """A URL with nothing to hide round-trips unchanged."""
    module = _load_script()

    url = "sqlite+pysqlite:///C:/tmp/x.db"

    assert module._redact_database_url(url) == url


def test_argparse_error_redacts_misspelled_database_url(capsys):
    """Unrecognized --databse-url must not echo the password-bearing value."""
    module = _load_script()
    secret = "postgresql+psycopg://user:s3cret-pass@host:5432/db"
    try:
        module._parse_args(
            ["--email", "ops@example.com", "--databse-url", secret],
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("argparse should reject an unrecognized --databse-url")
    err = capsys.readouterr().err
    assert "s3cret-pass" not in err
    assert secret not in err


def test_argparse_redaction_masks_split_password_fragment():
    """Whitespace-split URL remnants must not echo password fragments.

    Argparse splits error text on spaces, so a broken database URL can leak as
    ``s3cret-pass@host:5432/db`` with no ``://``. Fail-closed masking must
    catch that host-ish ``userinfo@host`` shape.
    """
    module = _load_script()
    secret_fragment = "s3cret-pass@db.internal.example:5432/ums"
    message = (
        "unrecognized arguments: --databse-url postgresql+psycopg://user: "
        f"{secret_fragment}"
    )

    redacted = module._redact_argparse_message(message)

    assert "s3cret-pass" not in redacted
    assert secret_fragment not in redacted
    assert module._UNPRINTABLE_URL in redacted
    assert "password=s3cret" not in module._redact_argparse_message(
        "error: bad option password=s3cret"
    )


def test_redaction_masks_unlisted_query_values_and_keeps_listed_ones():
    """Query values are default-deny: only allow-listed keys keep their value.

    This is the property that makes the fix allow-list shaped rather than a
    longer deny-list. ``options`` is masked because it is not on the list — not
    because anyone enumerated ``options=-c password=...`` as a leak — so a
    parameter nobody anticipated is masked by construction.
    """
    module = _load_script()

    redacted = module._redact_database_url(
        "postgresql+psycopg://user:pw@host/db?options=-c%20password%3Dx&sslmode=require"
    )

    assert "sslmode=require" in redacted
    assert "options=REDACTED" in redacted
    assert "password" not in redacted
    assert "sslmode" in module._PRINTABLE_QUERY_KEYS
    assert "options" not in module._PRINTABLE_QUERY_KEYS


def test_summary_withholds_a_password_carried_in_the_url_query(tmp_path, capsys):
    """End to end: a working URL whose password is a query parameter prints masked.

    The unit tests above pin the function; this pins the CALL SITE. ``main``
    exits 0, the bootstrap really runs, and the ``database:`` line the operator
    reads — and any runbook log that captures it — carries no credential.
    ``?password=`` is accepted by pysqlite exactly as it is by psycopg, so the
    run is a real one rather than a rehearsal of a rejected URL.
    """
    module = _load_script()
    database_url = f"{_make_database(tmp_path)}?password=leakme"

    exit_code = _run(module, database_url, "--email", _OPERATOR_EMAIL)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "leakme" not in output
    assert "password=REDACTED" in output
    assert f"database: {module._redact_database_url(database_url)}" in output


def _deactivate(database_url: str, unit_type: str) -> None:
    """Flip ``active`` to False on the seeded org unit of ``unit_type``."""
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            unit = session.scalars(select(OrgUnitORM).where(OrgUnitORM.type == unit_type)).one()
            unit.active = False
            session.commit()
    finally:
        engine.dispose()


def test_bootstrap_refuses_an_inactive_org_unit_instead_of_claiming_it_is_healthy(tmp_path, capsys):
    """A seeded COMPANY whose ``active`` is false is refused, not reported EXISTING.

    ``active`` was absent from the drift comparison, so this exact state exited
    0 and printed ``EXISTING COMPANY ... parent=<sector>`` plus the
    unconditional claim that the shape "clears BOTH MISSING_COMPANY and
    MISSING_SECTOR" — while ``GET /channels/issues`` returned
    ``MISSING_SECTOR``. Same defect class the lane already closed for
    ``parent_id``, in the same function, and more reachable: ``active`` is an
    ordinary column.
    """
    module = _load_script()
    database_url = _make_database(tmp_path)
    assert _run(module, database_url, "--email", _OPERATOR_EMAIL, "--org-skeleton") == 0
    _deactivate(database_url, "COMPANY")
    capsys.readouterr()

    exit_code = _run(module, database_url, "--email", _OPERATOR_EMAIL, "--org-skeleton")

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "active=false" in captured.err
    assert "MISSING_SECTOR" in captured.err
    # No summary at all, rather than one asserting a skeleton the API denies.
    assert "Org units" not in captured.out
    assert "clears BOTH" not in captured.out
    # Refusing means refusing: the flag is not flipped back on the way out.
    units = {unit.type: unit for unit in _org_units(database_url)}
    assert units["COMPANY"].active is False


def test_bootstrap_refuses_an_inactive_sector_too(tmp_path, capsys):
    """The SECTOR carries the same failure mode and gets the same refusal.

    ``build_org_access_index`` resolves a COMPANY's sector through
    ``active_org_units``, so deactivating the PARENT breaks the same walk. The
    SECTOR is checked first, so this also proves the refusal fires before the
    COMPANY is touched.
    """
    module = _load_script()
    database_url = _make_database(tmp_path)
    assert _run(module, database_url, "--email", _OPERATOR_EMAIL, "--org-skeleton") == 0
    _deactivate(database_url, "SECTOR")
    capsys.readouterr()

    exit_code = _run(module, database_url, "--email", _OPERATOR_EMAIL, "--org-skeleton")

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "SECTOR bootstrap row" in captured.err
    assert "active=false" in captured.err
    assert "Org units" not in captured.out


def test_org_summary_never_calls_an_inactive_skeleton_healthy(capsys):
    """With the refusal switched off the summary still refuses the health claim.

    The refusal means an inactive row normally never reaches the summary, which
    on its own would leave the claim itself untested: relax the policy later to
    "report as ignored" and the original defect — a console asserting a skeleton
    the API contradicts — comes back silently. This pins the second line of
    defence independently of the refusal, exactly as the read-back test above
    pins the read-back independently of it.
    """
    module = _load_script()

    module._print_org_summary(
        [
            module._OrgUnitOutcome(
                unit_type="SECTOR",
                unit_id="11111111-1111-1111-1111-111111111111",
                name="Default Sector",
                parent_id=None,
                created=False,
                active=True,
            ),
            module._OrgUnitOutcome(
                unit_type="COMPANY",
                unit_id="22222222-2222-2222-2222-222222222222",
                name="Default Company",
                parent_id="11111111-1111-1111-1111-111111111111",
                created=False,
                active=False,
            ),
        ]
    )

    output = capsys.readouterr().out
    assert "clears BOTH" not in output
    assert "active=False" in output
    assert "WARNING" in output
    assert "MISSING_SECTOR" in output


def _auth_headers(user_id: str) -> dict[str, str]:
    """Return super_owner global trusted-gateway headers for ``user_id``."""
    return {
        "x-user-id": user_id,
        "x-user-email": _OPERATOR_EMAIL,
        "x-role": "super_owner",
        "x-scope-type": "global",
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }


def _add_channel(database_url: str, *, channel_id: str, org_unit_id: UUID | None) -> None:
    """Insert one active, inside-CMS channel mapped to ``org_unit_id``."""
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            session.add(
                YouTubeChannelORM(
                    id=uuid4(),
                    tenant_id=_TENANT_ID,
                    youtube_channel_id=channel_id,
                    channel_name=channel_id,
                    primary_org_unit_id=org_unit_id,
                    cms_status="INSIDE_CMS",
                    revenue_required=False,
                    active=True,
                )
            )
            session.commit()
    finally:
        engine.dispose()


def _issue_types(database_url: str, user_id: str) -> set[str]:
    """Return the issue types GET /channels/issues reports for the database."""
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_channel_registry] = sql_channel_registry_from_session
    client = TestClient(app)
    response = client.get("/channels/issues", headers=_auth_headers(user_id))
    assert response.status_code == 200
    return {item["issue_type"] for item in response.json()["items"]}


def test_seeded_skeleton_clears_both_missing_company_and_missing_sector(tmp_path):
    """A channel mapped to the seeded COMPANY reports NEITHER org issue code.

    This is the assertion that proves the skeleton is the right *shape*. A
    COMPANY without a SECTOR parent would swap MISSING_COMPANY for
    MISSING_SECTOR — the same HIGH issue under a different label — and would
    look, from the operator's first screen, exactly like the fix not working.
    """
    module = _load_script()
    database_url = _make_database(tmp_path)
    assert _run(module, database_url, "--email", _OPERATOR_EMAIL, "--org-skeleton") == 0
    company = next((unit for unit in _org_units(database_url) if unit.type == "COMPANY"), None)
    assert company is not None
    _add_channel(database_url, channel_id="channel-mapped", org_unit_id=company.id)
    user_id = str(_users(database_url)[0].id)

    try:
        issue_types = _issue_types(database_url, user_id)
    finally:
        dispose_cached_engine(database_url)

    assert "MISSING_COMPANY" not in issue_types
    assert "MISSING_SECTOR" not in issue_types
    assert issue_types == set()


def test_orphan_company_would_report_missing_sector(tmp_path):
    """Negative control: a COMPANY with no SECTOR parent still reports MISSING_SECTOR.

    Without this the assertion above could pass for the wrong reason — e.g. if
    the channel were invisible to the caller, or the issue builder never ran.
    """
    module = _load_script()
    database_url = _make_database(tmp_path)
    assert _run(module, database_url, "--email", _OPERATOR_EMAIL) == 0
    orphan_company_id = uuid4()
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            session.add(
                OrgUnitORM(
                    id=orphan_company_id,
                    tenant_id=_TENANT_ID,
                    parent_id=None,
                    type="COMPANY",
                    name="Orphan Company",
                    active=True,
                )
            )
            session.commit()
    finally:
        engine.dispose()
    _add_channel(database_url, channel_id="channel-orphan", org_unit_id=orphan_company_id)
    user_id = str(_users(database_url)[0].id)

    try:
        issue_types = _issue_types(database_url, user_id)
    finally:
        dispose_cached_engine(database_url)

    assert issue_types == {"MISSING_SECTOR"}


def test_an_inactive_company_reports_missing_sector_so_the_refusal_is_load_bearing(tmp_path):
    """Deactivating the seeded COMPANY makes the API disagree with the console.

    This is why ``active`` had to enter the drift comparison, measured through
    the same endpoint the operator's first screen calls rather than argued from
    ``access_index.py``. The healthy half runs first as the accept side of the
    matrix, so the reject half cannot pass for an unrelated reason — an
    invisible channel, a caller without scope, an issue builder that never ran.
    """
    module = _load_script()
    database_url = _make_database(tmp_path)
    assert _run(module, database_url, "--email", _OPERATOR_EMAIL, "--org-skeleton") == 0
    company = next((unit for unit in _org_units(database_url) if unit.type == "COMPANY"), None)
    assert company is not None
    _add_channel(database_url, channel_id="channel-mapped", org_unit_id=company.id)
    user_id = str(_users(database_url)[0].id)

    try:
        healthy = _issue_types(database_url, user_id)
        _deactivate(database_url, "COMPANY")
        inactive = _issue_types(database_url, user_id)
    finally:
        dispose_cached_engine(database_url)

    assert healthy == set()
    assert inactive == {"MISSING_SECTOR"}


def test_seed_migration_catalog_covers_every_permission(tmp_path):
    """The bootstrap's role grant resolves through a fully seeded catalog."""
    database_url = _make_database(tmp_path)
    _seed_role_catalog(database_url)

    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            role_keys = set(session.scalars(select(RoleORM.key)).all())
            permission_keys = set(session.scalars(select(PermissionORM.key)).all())
    finally:
        engine.dispose()

    assert "finance_admin" in role_keys
    # Derived from the registries, not typed as a literal count. A count only
    # says "26 rows landed"; the set equality says WHICH, so a catalog that
    # seeded the right number of the wrong keys still fails.
    assert role_keys == {role.value for role in RoleKey}
    assert permission_keys == {permission.value for permission in Permission}
