# ============================================================================
# Purpose: PostgreSQL-tier proof that every audited user-management mutation
#   commits or rolls back with its USER_*_CHANGED audit row.
# Database/ORM: users, access_scopes, user_role_assignments,
#   user_permission_grants, and audit_logs on disposable PostgreSQL only.
# Standards: Real app wiring and RLS roles; the request transaction is rolled
#   back where its dependency would commit to simulate a lost commit after the
#   handler succeeds. In-flight probes prevent vacuous rollback assertions.
# Blast Radius: Test-only authorization and audit atomicity. No finance,
#   connector, export, or production database behavior changes.
# Connections:
#   - File: backend/ums_smart_revenue/api/users.py -> the six mutation routes.
#   - File: backend/ums_smart_revenue/api/dependencies_audit.py ->
#     current_atomic_audit_sink and its same-session SQL wiring.
#   - File: tests/api/test_owner_stamp_recovery_postgres.py -> established
#     lost-commit and in-transaction anti-vacuity pattern.
# ============================================================================
"""PostgreSQL commit-failure regressions for audited user mutations."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.db._postgres_helpers import require_postgres_url
from ums_smart_revenue.api.dependencies import current_db_session
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.audit_service import AuditRecord
from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink
from ums_smart_revenue.db.session import build_session_factory, dispose_cached_engine
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

TENANT_ID = UUID(UMS_TENANT_ID)
ACTOR_ID = UUID("00000000-0000-0000-0000-0000f223a001")
TARGET_ID = UUID("00000000-0000-0000-0000-0000f223a002")
ROLE_ASSIGNMENT_ID = UUID("00000000-0000-0000-0000-0000f223a003")
PERMISSION_GRANT_ID = UUID("00000000-0000-0000-0000-0000f223a004")
GLOBAL_SCOPE_SENTINEL_ID = UUID("00000000-0000-0000-0000-0000f223a005")

ACTOR_EMAIL = "atomic-actor-f223@example.com"
TARGET_EMAIL = "atomic-target-f223@example.com"
CREATED_EMAIL = "atomic-created-f223@example.com"
TARGET_ORIGINAL_NAME = "Atomic Target Original"
ROLE_ORIGINAL_REASON = "PG atomicity role seed f223"
PERMISSION_ORIGINAL_REASON = "PG atomicity permission seed f223"


@dataclass(frozen=True)
class MutationCase:
    """One user mutation and the state expected before and after lost commit."""

    key: str
    method: str
    path: str
    payload: Mapping[str, object]
    expected_status: int
    event_type: str
    entity_type: str
    response_fields: Mapping[str, object]
    state_sql: str
    in_flight_state: tuple[object, ...]
    durable_state: tuple[object, ...] | None

    @property
    def reason(self) -> str:
        """Return the mutation's required audit reason."""
        return str(self.payload["reason"])


CASES = (
    MutationCase(
        key="create-account",
        method="post",
        path="/users",
        payload={
            "email": CREATED_EMAIL,
            "display_name": "Atomic Created User",
            "reason": "PG lost-commit account create f223",
        },
        expected_status=201,
        event_type="USER_ACCOUNT_CHANGED",
        entity_type="user",
        response_fields={
            "email": CREATED_EMAIL,
            "display_name": "Atomic Created User",
            "status": "active",
        },
        state_sql=(
            "SELECT email, display_name, status FROM users "
            "WHERE tenant_id = :tenant_id AND id = CAST(:entity_id AS uuid)"
        ),
        in_flight_state=(CREATED_EMAIL, "Atomic Created User", "active"),
        durable_state=None,
    ),
    MutationCase(
        key="update-account",
        method="patch",
        path=f"/users/{TARGET_ID}",
        payload={
            "display_name": "Atomic Target Updated",
            "status": "disabled",
            "reason": "PG lost-commit account update f223",
        },
        expected_status=200,
        event_type="USER_ACCOUNT_CHANGED",
        entity_type="user",
        response_fields={"display_name": "Atomic Target Updated", "status": "disabled"},
        state_sql=(
            "SELECT email, display_name, status FROM users "
            "WHERE tenant_id = :tenant_id AND id = CAST(:entity_id AS uuid)"
        ),
        in_flight_state=(TARGET_EMAIL, "Atomic Target Updated", "disabled"),
        durable_state=(TARGET_EMAIL, TARGET_ORIGINAL_NAME, "active"),
    ),
    MutationCase(
        key="assign-role",
        method="post",
        path=f"/users/{TARGET_ID}/roles",
        payload={
            "role_key": "assistant_analyst",
            "scope_type": "global",
            "scope_id": None,
            "reason": "PG lost-commit role assign f223",
        },
        expected_status=201,
        event_type="USER_ROLE_CHANGED",
        entity_type="user_role_assignment",
        response_fields={"role_key": "assistant_analyst", "active": True},
        state_sql=(
            "SELECT role_key, active FROM user_role_assignments "
            "WHERE tenant_id = :tenant_id AND id = CAST(:entity_id AS uuid)"
        ),
        in_flight_state=("assistant_analyst", True),
        durable_state=None,
    ),
    MutationCase(
        key="revoke-role",
        method="post",
        path=f"/users/{TARGET_ID}/roles/{ROLE_ASSIGNMENT_ID}/revoke",
        payload={"reason": "PG lost-commit role revoke f223"},
        expected_status=200,
        event_type="USER_ROLE_CHANGED",
        entity_type="user_role_assignment",
        response_fields={"active": False},
        state_sql=(
            "SELECT active, revoked_by IS NOT NULL, revoked_at IS NOT NULL, reason "
            "FROM user_role_assignments "
            "WHERE tenant_id = :tenant_id AND id = CAST(:entity_id AS uuid)"
        ),
        in_flight_state=(False, True, True, "PG lost-commit role revoke f223"),
        durable_state=(True, False, False, ROLE_ORIGINAL_REASON),
    ),
    MutationCase(
        key="grant-permission",
        method="post",
        path=f"/users/{TARGET_ID}/permissions",
        payload={
            "permission_key": "analytics.view",
            "scope_type": "global",
            "scope_id": None,
            "reason": "PG lost-commit permission grant f223",
        },
        expected_status=201,
        event_type="USER_PERMISSION_CHANGED",
        entity_type="user_permission_grant",
        response_fields={"permission_key": "analytics.view", "active": True},
        state_sql=(
            "SELECT permission_key, active FROM user_permission_grants "
            "WHERE tenant_id = :tenant_id AND id = CAST(:entity_id AS uuid)"
        ),
        in_flight_state=("analytics.view", True),
        durable_state=None,
    ),
    MutationCase(
        key="revoke-permission",
        method="post",
        path=f"/users/{TARGET_ID}/permissions/{PERMISSION_GRANT_ID}/revoke",
        payload={"reason": "PG lost-commit permission revoke f223"},
        expected_status=200,
        event_type="USER_PERMISSION_CHANGED",
        entity_type="user_permission_grant",
        response_fields={"active": False},
        state_sql=(
            "SELECT active, revoked_by IS NOT NULL, revoked_at IS NOT NULL, revoke_reason "
            "FROM user_permission_grants "
            "WHERE tenant_id = :tenant_id AND id = CAST(:entity_id AS uuid)"
        ),
        in_flight_state=(False, True, True, "PG lost-commit permission revoke f223"),
        durable_state=(True, False, False, None),
    ),
)


def _alembic_config(url: str) -> Config:
    """Build an Alembic config for the disposable PostgreSQL database."""
    config = Config()
    config.set_main_option("sqlalchemy.url", url)
    config.set_main_option("script_location", "backend/ums_smart_revenue/db/alembic")
    return config


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    """Require the operator-supplied PostgreSQL database and migrate it to head.

    This module never resets a schema. Its fixture deletes only the fixed
    f223 sentinel rows, matching the non-destructive API PostgreSQL suites.
    """
    url = require_postgres_url()
    command.upgrade(_alembic_config(url), "head")
    try:
        yield url
    finally:
        dispose_cached_engine(url)


def _purge_sentinel_rows(engine: sa.Engine) -> None:
    """Delete only this module's fixed users and their dependent rows."""
    with engine.begin() as connection:
        params = {
            "tenant_id": TENANT_ID,
            "actor_id": ACTOR_ID,
            "target_id": TARGET_ID,
            "created_email": CREATED_EMAIL,
        }
        connection.execute(
            sa.text(
                "DELETE FROM audit_logs WHERE tenant_id = :tenant_id "
                "AND (user_id = :actor_id OR reason LIKE 'PG lost-commit % f223')"
            ),
            params,
        )
        connection.execute(
            sa.text(
                "DELETE FROM user_role_assignments WHERE tenant_id = :tenant_id "
                "AND (user_id = :target_id OR assigned_by = :actor_id)"
            ),
            params,
        )
        connection.execute(
            sa.text(
                "DELETE FROM user_permission_grants WHERE tenant_id = :tenant_id "
                "AND (user_id = :target_id OR granted_by = :actor_id)"
            ),
            params,
        )
        connection.execute(
            sa.text(
                "DELETE FROM users WHERE tenant_id = :tenant_id "
                "AND (id IN (:actor_id, :target_id) OR email = :created_email)"
            ),
            params,
        )


@pytest.fixture
def owner_engine(pg_url: str) -> Iterator[sa.Engine]:
    """Yield an owner-lane verifier after exact sentinel cleanup."""
    engine = sa.create_engine(pg_url)
    try:
        _purge_sentinel_rows(engine)
        yield engine
    finally:
        _purge_sentinel_rows(engine)
        engine.dispose()


def _seed_case(engine: sa.Engine, case: MutationCase) -> None:
    """Seed the common users, global scope, and any revoke baseline row."""
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO access_scopes (id, tenant_id, scope_type, scope_id, label) "
                "VALUES (:scope_id, :tenant_id, 'global', NULL, 'PG atomicity global') "
                "ON CONFLICT DO NOTHING"
            ),
            {"scope_id": GLOBAL_SCOPE_SENTINEL_ID, "tenant_id": TENANT_ID},
        )
        global_scope_id = connection.execute(
            sa.text(
                "SELECT id FROM access_scopes "
                "WHERE tenant_id = :tenant_id AND scope_type = 'global' AND scope_id IS NULL"
            ),
            {"tenant_id": TENANT_ID},
        ).scalar_one()
        connection.execute(
            sa.text(
                "INSERT INTO users (id, tenant_id, email, display_name, status, "
                "is_service_account) VALUES "
                "(:actor_id, :tenant_id, :actor_email, 'Atomic Actor', 'active', false), "
                "(:target_id, :tenant_id, :target_email, :target_name, 'active', false)"
            ),
            {
                "actor_id": ACTOR_ID,
                "target_id": TARGET_ID,
                "tenant_id": TENANT_ID,
                "actor_email": ACTOR_EMAIL,
                "target_email": TARGET_EMAIL,
                "target_name": TARGET_ORIGINAL_NAME,
            },
        )
        if case.key == "revoke-role":
            connection.execute(
                sa.text(
                    "INSERT INTO user_role_assignments "
                    "(id, tenant_id, user_id, role_key, scope_id, assigned_by, reason, active) "
                    "VALUES (:id, :tenant_id, :target_id, 'assistant_analyst', :scope_id, "
                    ":actor_id, :reason, true)"
                ),
                {
                    "id": ROLE_ASSIGNMENT_ID,
                    "tenant_id": TENANT_ID,
                    "target_id": TARGET_ID,
                    "scope_id": global_scope_id,
                    "actor_id": ACTOR_ID,
                    "reason": ROLE_ORIGINAL_REASON,
                },
            )
        elif case.key == "revoke-permission":
            connection.execute(
                sa.text(
                    "INSERT INTO user_permission_grants "
                    "(id, tenant_id, user_id, permission_key, scope_id, granted_by, "
                    "reason, active) VALUES (:id, :tenant_id, :target_id, 'analytics.view', "
                    ":scope_id, :actor_id, :reason, true)"
                ),
                {
                    "id": PERMISSION_GRANT_ID,
                    "tenant_id": TENANT_ID,
                    "target_id": TARGET_ID,
                    "scope_id": global_scope_id,
                    "actor_id": ACTOR_ID,
                    "reason": PERMISSION_ORIGINAL_REASON,
                },
            )


def _auth_headers() -> dict[str, str]:
    """Authorize the sentinel actor through the trusted-header test mode."""
    return {
        "x-user-id": str(ACTOR_ID),
        "x-user-email": ACTOR_EMAIL,
        "x-role": "corporate_admin",
        "x-scope-type": "global",
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }


def _row_tuple(result: sa.Result[Any]) -> tuple[object, ...] | None:
    """Normalize one optional SQL row for stable state assertions."""
    row = result.first()
    return None if row is None else tuple(row)


def _state(session: Session, case: MutationCase, entity_id: str) -> tuple[object, ...] | None:
    """Read the case's domain state through one specified transaction."""
    return _row_tuple(
        session.execute(
            sa.text(case.state_sql),
            {"tenant_id": TENANT_ID, "entity_id": entity_id},
        )
    )


def _durable_state(
    engine: sa.Engine, case: MutationCase, entity_id: str
) -> tuple[object, ...] | None:
    """Read committed domain state through the owner verifier."""
    with Session(engine) as session:
        return _state(session, case, entity_id)


def _audit_count(session: Session, case: MutationCase, entity_id: str) -> int:
    """Count only the audit row uniquely identifying this mutation."""
    return session.execute(
        sa.text(
            "SELECT COUNT(*) FROM audit_logs "
            "WHERE tenant_id = :tenant_id AND event_type = :event_type "
            "AND entity_type = :entity_type AND entity_id = :entity_id "
            "AND reason = :reason"
        ),
        {
            "tenant_id": TENANT_ID,
            "event_type": case.event_type,
            "entity_type": case.entity_type,
            "entity_id": entity_id,
            "reason": case.reason,
        },
    ).scalar_one()


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.key)
def test_user_mutation_and_audit_share_lost_commit_fate_on_postgres(
    pg_url: str,
    owner_engine: sa.Engine,
    case: MutationCase,
) -> None:
    """A lost request commit leaves neither its user mutation nor its audit row.

    The handler is allowed to complete and build a success response. The
    overridden request dependency then rolls back exactly where production
    would commit. The append probe runs after the real flushed audit INSERT and
    proves that both the domain mutation and audit row existed in the same
    request session before that rollback.
    """
    _seed_case(owner_engine, case)
    factory = build_session_factory(pg_url)
    request_sessions: list[Session] = []

    def rollback_instead_of_commit() -> Iterator[Session]:
        """Yield the real tenant session, then simulate a lost final commit."""
        with factory() as session:
            request_sessions.append(session)
            try:
                yield session
            finally:
                session.rollback()

    app = create_app(database_url=pg_url, authz_source="headers")
    app.dependency_overrides[current_db_session] = rollback_instead_of_commit
    original_append = SqlAlchemyAuditSink.append
    observations: list[tuple[str, bool, tuple[object, ...] | None, int]] = []

    def append_and_observe(sink: SqlAlchemyAuditSink, record: AuditRecord) -> None:
        """Use the real append, then prove domain plus audit are in-flight."""
        original_append(sink, record)
        observations.append(
            (
                record.entity_id or "",
                bool(request_sessions) and sink._session is request_sessions[0],  # noqa: SLF001
                _state(sink._session, case, record.entity_id or ""),  # noqa: SLF001
                _audit_count(sink._session, case, record.entity_id or ""),  # noqa: SLF001
            )
        )

    with patch.object(SqlAlchemyAuditSink, "append", append_and_observe), TestClient(app) as client:
        response = client.request(
            case.method,
            case.path,
            headers=_auth_headers(),
            json=dict(case.payload),
        )

    assert response.status_code == case.expected_status, response.text
    response_body = response.json()
    for field, expected in case.response_fields.items():
        assert response_body[field] == expected

    assert len(observations) == 1
    entity_id, same_session, in_flight_state, in_flight_audits = observations[0]
    assert entity_id == response_body["id"]
    assert same_session is True
    assert in_flight_state == case.in_flight_state
    assert in_flight_audits == 1

    assert _durable_state(owner_engine, case, entity_id) == case.durable_state
    with Session(owner_engine) as verifier:
        assert _audit_count(verifier, case, entity_id) == 0
