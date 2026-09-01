# ============================================================================
# Purpose: Prove the PR #228 revision is a single-head PostgreSQL round trip
#   with enforced tenant RLS and the intended least-privilege app-role grants.
# Database/ORM: users, external_identities, us_withholding_rate_configs; app roles.
# Standards: Disposable PostgreSQL only, real Alembic graph, and live role switching.
# Blast Radius: Test-only proof for authorization and finance tenant isolation.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260828_0001_external_identity_and_withholding.py -> Migration under test.
#   - File: tests/db/_postgres_helpers.py -> Explicit disposable database gate.
# ============================================================================
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import date
from decimal import Decimal
from threading import Event
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.orm import Session
from tests.db._pg_schema_helpers import reset_public_schema
from tests.db._postgres_helpers import require_postgres_url

from ums_smart_revenue.db.rls import APP_PLATFORM_ROLE, APP_TENANT_ROLE
from ums_smart_revenue.finance.us_withholding_config import (
    SqlAlchemyUsWithholdingConfigRepository,
)

_TENANT_A = "00000000-0000-0000-0000-000000000001"
_TENANT_B = "00000000-0000-0000-0000-000000088101"
_USER_A = "00000000-0000-0000-0000-000000088102"
_USER_B = "00000000-0000-0000-0000-000000088103"
_ORG_A = "00000000-0000-0000-0000-000000088104"
_ORG_B = "00000000-0000-0000-0000-000000088105"
_ACCOUNT_A = "pub-pr228-a"
_ACCOUNT_B = "pub-pr228-b"


def _alembic_config(url: str) -> Config:
    """Build an Alembic Config aimed at the disposable database URL."""
    config = Config()
    config.set_main_option("sqlalchemy.url", url)
    config.set_main_option("script_location", "backend/ums_smart_revenue/db/alembic")
    return config


# ============================================================================
# Purpose: Refuse the destructive public-schema reset unless PostgreSQL itself
#   reports a database name reserved for tests.
# Database/ORM: PostgreSQL current_database(); no application tables.
# Standards: Live preflight before DROP SCHEMA, with engine disposal on errors.
# Blast Radius: Test database safety only; prevents production/operator data loss.
# Connections:
#   - File: tests/db/_pg_schema_helpers.py -> Destructive reset called after guard.
#   - File: tests/db/_postgres_helpers.py -> Disposable URL setup contract.
# ============================================================================
def _require_disposable_database_name(url: str) -> None:
    """Fail closed unless the connected database has the test-name contract."""
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            database_name = connection.execute(sa.text("SELECT current_database()")).scalar_one()
    finally:
        engine.dispose()
    if not (
        isinstance(database_name, str)
        and (database_name.startswith("test_") or database_name.endswith("_test"))
    ):
        raise RuntimeError(
            "Refusing destructive PostgreSQL schema reset: database name must start "
            "with 'test_' or end with '_test'"
        )


def _has_privilege(
    connection: sa.Connection,
    *,
    role: str,
    table: str,
    privilege: str,
) -> bool:
    """Return whether PostgreSQL reports one table privilege for a role."""
    return bool(
        connection.execute(
            sa.text("SELECT has_table_privilege(:role, :table, :privilege)"),
            {"role": role, "table": f"public.{table}", "privilege": privilege},
        ).scalar_one()
    )


def _direct_privileges(
    connection: sa.Connection,
    *,
    role: str,
    table: str,
) -> set[str]:
    """Return the directly granted privilege types one role holds on a table."""
    return set(
        connection.execute(
            sa.text(
                "SELECT privilege_type FROM information_schema.table_privileges "
                "WHERE table_schema = 'public' AND table_name = :table "
                "AND grantee = :role"
            ),
            {"role": role, "table": table},
        ).scalars()
    )


def _seed_two_tenants(connection: sa.Connection) -> None:
    """Insert one user, identity, and withholding row for each tenant."""
    connection.execute(
        sa.text(
            "INSERT INTO tenants (id, slug, display_name, primary_currency, status) "
            "VALUES (CAST(:id AS uuid), 'pr228-tenant-b', 'PR228 Tenant B', 'USD', 'ACTIVE')"
        ),
        {"id": _TENANT_B},
    )
    connection.execute(
        sa.text(
            "INSERT INTO users (id, tenant_id, email, display_name) VALUES "
            "(CAST(:user_a AS uuid), CAST(:tenant_a AS uuid), 'pr228-a@example.com', 'A'), "
            "(CAST(:user_b AS uuid), CAST(:tenant_b AS uuid), 'pr228-b@example.com', 'B')"
        ),
        {
            "user_a": _USER_A,
            "tenant_a": _TENANT_A,
            "user_b": _USER_B,
            "tenant_b": _TENANT_B,
        },
    )
    connection.execute(
        sa.text(
            "INSERT INTO external_identities "
            "(tenant_id, provider, provider_subject, normalized_email, user_id) VALUES "
            "(CAST(:tenant_a AS uuid), 'google', 'pr228-sub-a', "
            "'pr228-a@example.com', CAST(:user_a AS uuid)), "
            "(CAST(:tenant_b AS uuid), 'google', 'pr228-sub-b', "
            "'pr228-b@example.com', CAST(:user_b AS uuid))"
        ),
        {
            "tenant_a": _TENANT_A,
            "user_a": _USER_A,
            "tenant_b": _TENANT_B,
            "user_b": _USER_B,
        },
    )
    connection.execute(
        sa.text(
            "INSERT INTO us_withholding_rate_configs "
            "(tenant_id, source_account_id, effective_from, revision, rate, account_type, "
            "confirmed_by_user_id) VALUES "
            "(CAST(:tenant_a AS uuid), :account_a, DATE '2026-01-01', 1, 0.10, 'business', "
            "CAST(:user_a AS uuid)), "
            "(CAST(:tenant_b AS uuid), :account_b, DATE '2026-01-01', 1, 0.20, 'business', "
            "CAST(:user_b AS uuid))"
        ),
        {
            "tenant_a": _TENANT_A,
            "user_a": _USER_A,
            "tenant_b": _TENANT_B,
            "user_b": _USER_B,
            "account_a": _ACCOUNT_A,
            "account_b": _ACCOUNT_B,
        },
    )


# ============================================================================
# Purpose: Prove all three composite tenant foreign keys reject cross-tenant
#   references under the PostgreSQL owner, independent of RLS filtering.
# Database/ORM: users, org_units, external_identities, withholding configs.
# Standards: Savepoint-isolated counterexamples with parameterized values.
# Blast Radius: Authorization identity/home scope and finance confirmer integrity.
# Connections:
#   - File: backend/ums_smart_revenue/db/security_models.py -> ORM FK mirrors.
#   - File: tests/db/test_external_identity_withholding_migration.py -> SQLite DDL parity.
# ============================================================================
def _assert_composite_foreign_keys_reject_cross_tenant(connection: sa.Connection) -> None:
    """Assert each composite foreign key rejects a cross-tenant reference."""
    connection.execute(
        sa.text(
            "INSERT INTO org_units (id, tenant_id, type, name) VALUES "
            "(CAST(:org_a AS uuid), CAST(:tenant_a AS uuid), 'COMPANY', 'Org A'), "
            "(CAST(:org_b AS uuid), CAST(:tenant_b AS uuid), 'COMPANY', 'Org B')"
        ),
        {
            "org_a": _ORG_A,
            "tenant_a": _TENANT_A,
            "org_b": _ORG_B,
            "tenant_b": _TENANT_B,
        },
    )
    counterexamples = (
        (
            "INSERT INTO users (id, tenant_id, email, display_name, home_org_unit_id) "
            "VALUES (gen_random_uuid(), CAST(:tenant_a AS uuid), "
            "'pr228-cross-home@example.com', 'Cross Home', CAST(:org_b AS uuid))",
            {"tenant_a": _TENANT_A, "org_b": _ORG_B},
        ),
        (
            "INSERT INTO external_identities "
            "(tenant_id, provider, provider_subject, normalized_email, user_id) "
            "VALUES (CAST(:tenant_a AS uuid), 'google', 'pr228-cross-user', "
            "'pr228-cross-user@example.com', CAST(:user_b AS uuid))",
            {"tenant_a": _TENANT_A, "user_b": _USER_B},
        ),
        (
            "INSERT INTO us_withholding_rate_configs "
            "(tenant_id, source_account_id, effective_from, revision, rate, account_type, "
            "confirmed_by_user_id) VALUES "
            "(CAST(:tenant_a AS uuid), :account_a, DATE '2026-04-01', 1, 0.10, "
            "'business', CAST(:user_b AS uuid))",
            {"tenant_a": _TENANT_A, "account_a": _ACCOUNT_A, "user_b": _USER_B},
        ),
    )
    for statement, params in counterexamples:
        savepoint = connection.begin_nested()
        try:
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(sa.text(statement), params)
        finally:
            savepoint.rollback()


def _assert_identity_and_withholding_constraints(connection: sa.Connection) -> None:
    """Assert identity uniqueness and withholding checks reject every bad row."""
    counterexamples = (
        (
            "INSERT INTO external_identities "
            "(tenant_id, provider, provider_subject, normalized_email, user_id) "
            "VALUES (CAST(:tenant_a AS uuid), 'google', 'pr228-sub-a', "
            "'pr228-other@example.com', CAST(:user_a AS uuid))",
            {"tenant_a": _TENANT_A, "user_a": _USER_A},
        ),
        (
            "INSERT INTO external_identities "
            "(tenant_id, provider, provider_subject, normalized_email, user_id) "
            "VALUES (CAST(:tenant_a AS uuid), 'google', 'pr228-other-subject', "
            "'PR228-A@EXAMPLE.COM', CAST(:user_a AS uuid))",
            {"tenant_a": _TENANT_A, "user_a": _USER_A},
        ),
        (
            "INSERT INTO us_withholding_rate_configs "
            "(tenant_id, source_account_id, effective_from, revision, rate, account_type, "
            "confirmed_by_user_id) VALUES (CAST(:tenant_a AS uuid), :account_b, "
            "DATE '2026-05-01', 1, -0.01, 'business', CAST(:user_a AS uuid))",
            {"tenant_a": _TENANT_A, "account_b": _ACCOUNT_B, "user_a": _USER_A},
        ),
        (
            "INSERT INTO us_withholding_rate_configs "
            "(tenant_id, source_account_id, effective_from, revision, rate, account_type, "
            "confirmed_by_user_id) VALUES (CAST(:tenant_a AS uuid), :account_b, "
            "DATE '2026-05-01', 1, 0.31, 'business', CAST(:user_a AS uuid))",
            {"tenant_a": _TENANT_A, "account_b": _ACCOUNT_B, "user_a": _USER_A},
        ),
        (
            "INSERT INTO us_withholding_rate_configs "
            "(tenant_id, source_account_id, effective_from, revision, rate, account_type, "
            "confirmed_by_user_id) VALUES (CAST(:tenant_a AS uuid), :account_b, "
            "DATE '2026-05-01', 1, 0.15, 'unknown', CAST(:user_a AS uuid))",
            {"tenant_a": _TENANT_A, "account_b": _ACCOUNT_B, "user_a": _USER_A},
        ),
        (
            "INSERT INTO us_withholding_rate_configs "
            "(tenant_id, source_account_id, effective_from, revision, rate, account_type, "
            "confirmed_by_user_id) VALUES (CAST(:tenant_a AS uuid), :account_b, "
            "DATE '2026-05-01', 0, 0.15, 'business', CAST(:user_a AS uuid))",
            {"tenant_a": _TENANT_A, "account_b": _ACCOUNT_B, "user_a": _USER_A},
        ),
        (
            "INSERT INTO us_withholding_rate_configs "
            "(tenant_id, source_account_id, effective_from, revision, rate, account_type, "
            "confirmed_by_user_id) VALUES (CAST(:tenant_a AS uuid), '   ', "
            "DATE '2026-05-01', 1, 0.15, 'business', CAST(:user_a AS uuid))",
            {"tenant_a": _TENANT_A, "user_a": _USER_A},
        ),
        *(
            (
                "INSERT INTO us_withholding_rate_configs "
                "(tenant_id, source_account_id, effective_from, revision, rate, "
                "account_type, confirmed_by_user_id) VALUES "
                "(CAST(:tenant_a AS uuid), :invalid_account, DATE '2026-05-01', "
                "1, 0.15, 'business', CAST(:user_a AS uuid))",
                {
                    "tenant_a": _TENANT_A,
                    "invalid_account": invalid_account,
                    "user_a": _USER_A,
                },
            )
            for invalid_account in (
                "accounts/pub-pr228-a",
                "pub?pr228-a",
                "pub#pr228-a",
                "pub%pr228-a",
                "\t",
                "\n",
                "\r",
                "\f",
                "\v",
            )
        ),
        (
            "INSERT INTO us_withholding_rate_configs "
            "(tenant_id, source_account_id, effective_from, revision, rate, account_type, "
            "confirmed_by_user_id) VALUES (CAST(:tenant_a AS uuid), :account_a, "
            "DATE '2026-01-01', 1, 0.15, 'business', CAST(:user_a AS uuid))",
            {"tenant_a": _TENANT_A, "account_a": _ACCOUNT_A, "user_a": _USER_A},
        ),
    )
    for statement, params in counterexamples:
        savepoint = connection.begin_nested()
        try:
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(sa.text(statement), params)
        finally:
            savepoint.rollback()


def _assert_rls_and_grants(connection: sa.Connection) -> None:
    """Assert RLS is forced, policies scope by tenant, and grants stay SELECT-only."""
    for table in ("external_identities", "us_withholding_rate_configs"):
        enabled, forced = connection.execute(
            sa.text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE oid = CAST(:table AS regclass)"
            ),
            {"table": f"public.{table}"},
        ).one()
        assert enabled is True
        assert forced is True
        policy = connection.execute(
            sa.text(
                "SELECT qual, with_check FROM pg_policies "
                "WHERE schemaname = 'public' AND tablename = :table"
            ),
            {"table": table},
        ).one()
        assert "app_current_tenant_id()" in policy.qual
        assert "app_current_tenant_id()" in policy.with_check
        public_grants = connection.execute(
            sa.text(
                "SELECT privilege_type FROM information_schema.table_privileges "
                "WHERE table_schema = 'public' AND table_name = :table "
                "AND grantee = 'PUBLIC'"
            ),
            {"table": table},
        ).all()
        assert public_grants == []

    for role in (APP_TENANT_ROLE, APP_PLATFORM_ROLE):
        assert _direct_privileges(
            connection,
            role=role,
            table="external_identities",
        ) == {"SELECT"}
        assert _direct_privileges(
            connection,
            role=role,
            table="us_withholding_rate_configs",
        ) == {"SELECT"}

    assert _has_privilege(
        connection,
        role=APP_TENANT_ROLE,
        table="external_identities",
        privilege="SELECT",
    )
    for privilege in ("INSERT", "UPDATE", "DELETE"):
        assert not _has_privilege(
            connection,
            role=APP_TENANT_ROLE,
            table="external_identities",
            privilege=privilege,
        )
    assert _has_privilege(
        connection,
        role=APP_TENANT_ROLE,
        table="us_withholding_rate_configs",
        privilege="SELECT",
    )
    for privilege in ("INSERT", "UPDATE", "DELETE"):
        assert not _has_privilege(
            connection,
            role=APP_TENANT_ROLE,
            table="us_withholding_rate_configs",
            privilege=privilege,
        )
    assert _has_privilege(
        connection,
        role=APP_PLATFORM_ROLE,
        table="external_identities",
        privilege="SELECT",
    )
    for privilege in ("INSERT", "UPDATE", "DELETE"):
        assert not _has_privilege(
            connection,
            role=APP_PLATFORM_ROLE,
            table="external_identities",
            privilege=privilege,
        )
    assert _has_privilege(
        connection,
        role=APP_PLATFORM_ROLE,
        table="us_withholding_rate_configs",
        privilege="SELECT",
    )
    for privilege in ("INSERT", "UPDATE", "DELETE"):
        assert not _has_privilege(
            connection,
            role=APP_PLATFORM_ROLE,
            table="us_withholding_rate_configs",
            privilege=privilege,
        )


def _assert_tenant_a_rls_visibility_and_checks(connection: sa.Connection) -> None:
    """Assert tenant A's lane sees only its rows and blocks cross-tenant writes."""
    # The production grant is SELECT-only until the audited writer ships. Add
    # transaction-local probe grants only after exact ACL assertions so the
    # policy's WITH CHECK behavior is still proven independently of the ACL.
    for table in ("external_identities", "us_withholding_rate_configs"):
        connection.execute(sa.text(f'GRANT INSERT ON public."{table}" TO "{APP_TENANT_ROLE}"'))
    connection.execute(sa.text(f'SET LOCAL ROLE "{APP_PLATFORM_ROLE}"'))
    connection.execute(
        sa.text("SELECT set_app_current_tenant_id(CAST(:tenant_id AS uuid))"),
        {"tenant_id": _TENANT_A},
    )
    connection.execute(sa.text(f'SET LOCAL ROLE "{APP_TENANT_ROLE}"'))

    subjects = set(
        connection.execute(sa.text("SELECT provider_subject FROM external_identities")).scalars()
    )
    rates = set(
        connection.execute(sa.text("SELECT rate FROM us_withholding_rate_configs")).scalars()
    )
    assert subjects == {"pr228-sub-a"}
    assert rates == {Decimal("0.100000")}

    connection.execute(
        sa.text(
            "INSERT INTO external_identities "
            "(tenant_id, provider, provider_subject, normalized_email, user_id) "
            "VALUES (CAST(:tenant_a AS uuid), 'google', 'pr228-sub-a-2', "
            "'pr228-a-2@example.com', CAST(:user_a AS uuid))"
        ),
        {"tenant_a": _TENANT_A, "user_a": _USER_A},
    )
    connection.execute(
        sa.text(
            "INSERT INTO us_withholding_rate_configs "
            "(tenant_id, source_account_id, effective_from, revision, rate, account_type, "
            "confirmed_by_user_id) "
            "VALUES (CAST(:tenant_a AS uuid), :account_a, DATE '2026-02-01', 1, 0.15, "
            "'business', CAST(:user_a AS uuid))"
        ),
        {"tenant_a": _TENANT_A, "account_a": _ACCOUNT_A, "user_a": _USER_A},
    )
    assert (
        connection.execute(sa.text("SELECT count(*) FROM us_withholding_rate_configs")).scalar_one()
        == 2
    )

    cross_tenant_inserts = (
        (
            "INSERT INTO external_identities "
            "(tenant_id, provider, provider_subject, normalized_email, user_id) "
            "VALUES (CAST(:tenant_b AS uuid), 'google', 'pr228-sub-b-2', "
            "'pr228-b-2@example.com', CAST(:user_b AS uuid))",
            {"tenant_b": _TENANT_B, "user_b": _USER_B},
        ),
        (
            "INSERT INTO us_withholding_rate_configs "
            "(tenant_id, source_account_id, effective_from, revision, rate, account_type, "
            "confirmed_by_user_id) "
            "VALUES (CAST(:tenant_b AS uuid), :account_b, DATE '2026-02-01', 1, 0.25, "
            "'business', CAST(:user_b AS uuid))",
            {"tenant_b": _TENANT_B, "account_b": _ACCOUNT_B, "user_b": _USER_B},
        ),
    )
    for statement, params in cross_tenant_inserts:
        savepoint = connection.begin_nested()
        try:
            with pytest.raises(sa.exc.DBAPIError):
                connection.execute(sa.text(statement), params)
        finally:
            savepoint.rollback()


# ============================================================================
# Purpose: Prove same-key PostgreSQL writers serialize into chronological,
#   append-only revisions while the first transaction deliberately holds lock.
# Database/ORM: us_withholding_rate_configs; PostgreSQL advisory transaction lock.
# Standards: Independent sessions, bounded event waits, and committed row proof.
# Blast Radius: Finance estimate history ordering under concurrent writes.
# Connections:
#   - File: backend/ums_smart_revenue/finance/us_withholding_config.py -> Lock allocator.
#   - File: backend/ums_smart_revenue/db/security_models.py -> Revision unique key.
# ============================================================================
def _assert_concurrent_rate_revisions(engine: sa.Engine) -> None:
    """Assert same-key writers serialize into chronological committed revisions."""
    first_has_lock = Event()
    second_started = Event()
    release_first = Event()

    def record_first() -> int:
        """Record revision one and hold its transaction lock until released."""
        with Session(engine) as session:
            snapshot = SqlAlchemyUsWithholdingConfigRepository(session).record_confirmed_rate(
                tenant_id=UUID(_TENANT_A),
                source_account_id=_ACCOUNT_A,
                effective_from=date(2026, 3, 1),
                rate=Decimal("0.11"),
                account_type="business",
                confirmed_by_user_id=UUID(_USER_A),
            )
            first_has_lock.set()
            assert release_first.wait(timeout=10)
            session.commit()
            return snapshot.revision

    def record_second() -> int:
        """Record revision two only after the first writer releases its lock."""
        assert first_has_lock.wait(timeout=10)
        second_started.set()
        with Session(engine) as session:
            snapshot = SqlAlchemyUsWithholdingConfigRepository(session).record_confirmed_rate(
                tenant_id=UUID(_TENANT_A),
                source_account_id=_ACCOUNT_A,
                effective_from=date(2026, 3, 1),
                rate=Decimal("0.12"),
                account_type="business",
                confirmed_by_user_id=UUID(_USER_A),
            )
            session.commit()
            return snapshot.revision

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(record_first)
        assert first_has_lock.wait(timeout=10)
        second = executor.submit(record_second)
        assert second_started.wait(timeout=10)
        try:
            with pytest.raises(FutureTimeoutError):
                second.result(timeout=0.25)
        finally:
            release_first.set()
        assert first.result(timeout=10) == 1
        assert second.result(timeout=10) == 2

    with engine.connect() as connection:
        persisted = list(
            connection.execute(
                sa.text(
                    "SELECT revision FROM us_withholding_rate_configs "
                    "WHERE tenant_id = CAST(:tenant_id AS uuid) "
                    "AND source_account_id = :source_account_id "
                    "AND effective_from = DATE '2026-03-01' ORDER BY revision"
                ),
                {"tenant_id": _TENANT_A, "source_account_id": _ACCOUNT_A},
            ).scalars()
        )
    assert persisted == [1, 2]


# ============================================================================
# Purpose: Remove committed concurrency-test rows before the migration
#   downgrade so the shared disposable database returns to a clean head.
# Database/ORM: withholding configs, external identities, users, tenants.
# Standards: Dependency-ordered, parameterized cleanup under the test superuser.
# Blast Radius: Test database hygiene only.
# Connections:
#   - File: tests/db/_pg_schema_helpers.py -> Fresh-schema setup at test start.
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260828_0001_external_identity_and_withholding.py -> Downgrade under test.
# ============================================================================
def _cleanup_seeded_rows(engine: sa.Engine) -> None:
    """Delete the seeded rows so the downgrade starts from a clean head."""
    with engine.begin() as connection:
        params = {"tenant_a": _TENANT_A, "tenant_b": _TENANT_B}
        for table in ("us_withholding_rate_configs", "external_identities"):
            connection.execute(
                sa.text(
                    f'DELETE FROM public."{table}" '
                    "WHERE tenant_id IN (CAST(:tenant_a AS uuid), CAST(:tenant_b AS uuid))"
                ),
                params,
            )
        connection.execute(
            sa.text("DELETE FROM users WHERE id IN (CAST(:user_a AS uuid), CAST(:user_b AS uuid))"),
            {"user_a": _USER_A, "user_b": _USER_B},
        )
        connection.execute(
            sa.text("DELETE FROM tenants WHERE id = CAST(:tenant_b AS uuid)"),
            {"tenant_b": _TENANT_B},
        )


def test_postgres_revision_is_single_head_roundtrips_and_enforces_rls() -> None:
    """Prove the revision is single-head and its round trip enforces tenant RLS."""
    url = require_postgres_url()
    config = _alembic_config(url)
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    revision = script.get_revision("20260828_0001")
    assert revision is not None
    assert revision.down_revision == "20260825_0002"

    _require_disposable_database_name(url)
    reset_public_schema(url)
    command.upgrade(config, "20260828_0001")
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                _assert_rls_and_grants(connection)
                _seed_two_tenants(connection)
                _assert_composite_foreign_keys_reject_cross_tenant(connection)
                _assert_identity_and_withholding_constraints(connection)
                _assert_tenant_a_rls_visibility_and_checks(connection)
            finally:
                transaction.rollback()
    finally:
        engine.dispose()

    concurrent_engine = sa.create_engine(url)
    try:
        with concurrent_engine.begin() as connection:
            _seed_two_tenants(connection)
        try:
            _assert_concurrent_rate_revisions(concurrent_engine)
        finally:
            _cleanup_seeded_rows(concurrent_engine)
    finally:
        concurrent_engine.dispose()

    command.downgrade(config, "20260825_0002")
    downgraded_engine = sa.create_engine(url)
    try:
        with downgraded_engine.connect() as connection:
            inspector = sa.inspect(connection)
            assert "external_identities" not in inspector.get_table_names()
            assert "us_withholding_rate_configs" not in inspector.get_table_names()
            assert "home_org_unit_id" not in {
                column["name"] for column in inspector.get_columns("users")
            }
            assert "uq_users_email_lower" in {
                index["name"] for index in inspector.get_indexes("users")
            }
    finally:
        downgraded_engine.dispose()
        command.upgrade(config, "head")
