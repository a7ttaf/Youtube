# ============================================================================
# Purpose: PostgreSQL-tier proof that the beta manual-revenue fact and its
#   REPORT_IMPORTED audit row share one platform request transaction.
# Database/ORM: users, youtube_channels, finance_month_close,
#   monthly_channel_revenue_facts, and audit_logs on PostgreSQL.
# Standards: Real route, repository, sink, and RLS wiring; the platform request
#   dependency rolls back where it would commit to simulate a lost final
#   commit. In-flight state assertions prevent a vacuous no-row result.
# Blast Radius: Test-only finance provenance and audit atomicity. No production
#   finance calculation, authorization, export, or database behavior changes.
# Connections:
#   - File: backend/ums_smart_revenue/api/revenue.py -> POST /revenue/facts.
#   - File: backend/ums_smart_revenue/api/dependencies_finance.py -> the fact
#     repository and revenue audit sink share current_platform_db_session.
#   - File: tests/api/test_user_mutation_atomicity_postgres.py -> sibling
#     request-boundary lost-commit proof.
# ============================================================================
"""PostgreSQL lost-commit proof for the beta manual-revenue workflow."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from unittest.mock import patch
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.db._postgres_helpers import require_postgres_url
from ums_smart_revenue.api.dependencies import current_platform_db_session
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.audit_service import AuditRecord
from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink
from ums_smart_revenue.db.session import build_platform_session_factory, dispose_cached_engine
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

TENANT_ID = UUID(UMS_TENANT_ID)
ACTOR_ID = UUID("00000000-0000-0000-0000-0000f223b001")
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-0000f223b002")
ACTOR_EMAIL = "revenue-atomic-actor-f223@example.com"
CHANNEL_ID = "channel-revenue-atomic-f223"
MONTH = "2098-07"
CONNECTOR_KEY = "manual-upload"
SOURCE_REPORT_ID = "manual-revenue-atomic-f223"
REASON = "PG lost-commit manual revenue fact f223"
AUDIT_ENTITY_ID = f"{CHANNEL_ID}:{MONTH}:MANUAL_UPLOAD"


def _alembic_config(url: str) -> Config:
    """Build an Alembic config bound to the operator-supplied database."""
    config = Config()
    config.set_main_option("sqlalchemy.url", url)
    config.set_main_option("script_location", "backend/ums_smart_revenue/db/alembic")
    return config


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    """Require PostgreSQL, migrate to head, and release cached engines."""
    url = require_postgres_url()
    command.upgrade(_alembic_config(url), "head")
    try:
        yield url
    finally:
        dispose_cached_engine(url)


def _purge_sentinel_rows(engine: sa.Engine) -> None:
    """Delete only this module's fixed finance, audit, channel, and user rows."""
    params = {
        "tenant_id": TENANT_ID,
        "actor_id": ACTOR_ID,
        "channel_id": CHANNEL_ID,
        "month": MONTH,
        "reason": REASON,
    }
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "DELETE FROM audit_logs WHERE tenant_id = :tenant_id "
                "AND (reason = :reason OR (user_id = :actor_id "
                "AND entity_id = :audit_entity_id))"
            ),
            {**params, "audit_entity_id": AUDIT_ENTITY_ID},
        )
        connection.execute(
            sa.text(
                "DELETE FROM monthly_channel_revenue_facts "
                "WHERE tenant_id = :tenant_id AND month = :month "
                "AND youtube_channel_id = :channel_id"
            ),
            params,
        )
        connection.execute(
            sa.text(
                "DELETE FROM finance_month_close WHERE tenant_id = :tenant_id AND month = :month"
            ),
            params,
        )
        connection.execute(
            sa.text(
                "DELETE FROM youtube_channels "
                "WHERE tenant_id = :tenant_id AND youtube_channel_id = :channel_id"
            ),
            params,
        )
        connection.execute(
            sa.text("DELETE FROM users WHERE tenant_id = :tenant_id AND id = :actor_id"),
            params,
        )


@pytest.fixture
def owner_engine(pg_url: str) -> Iterator[sa.Engine]:
    """Yield an owner verifier with exact sentinel cleanup on both sides."""
    engine = sa.create_engine(pg_url)
    try:
        _purge_sentinel_rows(engine)
        yield engine
    finally:
        _purge_sentinel_rows(engine)
        engine.dispose()


def _seed_prerequisites(engine: sa.Engine) -> None:
    """Seed the actor, active channel, and explicitly OPEN finance month."""
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO users "
                "(id, tenant_id, email, display_name, status, is_service_account) "
                "VALUES (:actor_id, :tenant_id, :email, "
                "'Revenue Atomicity Actor', 'active', false)"
            ),
            {"actor_id": ACTOR_ID, "tenant_id": TENANT_ID, "email": ACTOR_EMAIL},
        )
        connection.execute(
            sa.text(
                "INSERT INTO youtube_channels "
                "(id, tenant_id, youtube_channel_id, channel_name, cms_status, "
                "revenue_required, revenue_source_status, active) "
                "VALUES (:row_id, :tenant_id, :channel_id, "
                "'Revenue Atomicity Channel', 'INSIDE_CMS', true, "
                "'OFFICIAL_MANUAL_IMPORT', true)"
            ),
            {
                "row_id": CHANNEL_ROW_ID,
                "tenant_id": TENANT_ID,
                "channel_id": CHANNEL_ID,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO finance_month_close "
                "(tenant_id, month, status, allocation_rule_payload) "
                "VALUES (:tenant_id, :month, 'OPEN', '{}'::jsonb)"
            ),
            {"tenant_id": TENANT_ID, "month": MONTH},
        )


def _auth_headers() -> dict[str, str]:
    """Authorize the seeded actor as the narrow global beta operator."""
    return {
        "x-user-id": str(ACTOR_ID),
        "x-user-email": ACTOR_EMAIL,
        "x-role": "beta_operator",
        "x-scope-type": "global",
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }


def _request_payload() -> dict[str, object]:
    """Build one valid manual-upload fact with stable provenance."""
    return {
        "month": MONTH,
        "youtube_channel_id": CHANNEL_ID,
        "source_kind": "MANUAL_UPLOAD",
        "connector_key": CONNECTOR_KEY,
        "source_report_id": SOURCE_REPORT_ID,
        "gross_revenue_usd": "1234.56",
        "net_revenue_usd": "987.65",
        "views": 250000,
        "watch_time_minutes": "7200.50",
        "confidence_score": "0.95",
        "reason": REASON,
    }


def _fact_state(session: Session) -> tuple[object, ...] | None:
    """Read the sentinel fact through the specified transaction."""
    row = session.execute(
        sa.text(
            "SELECT source_kind, source_report_id, gross_revenue_usd, "
            "net_revenue_usd, imported_by "
            "FROM monthly_channel_revenue_facts "
            "WHERE tenant_id = :tenant_id AND month = :month "
            "AND youtube_channel_id = :channel_id AND source_kind = 'MANUAL_UPLOAD'"
        ),
        {"tenant_id": TENANT_ID, "month": MONTH, "channel_id": CHANNEL_ID},
    ).first()
    return None if row is None else tuple(row)


def _matching_audits(session: Session) -> list[tuple[object, ...]]:
    """Read only the REPORT_IMPORTED row uniquely matching this request."""
    rows = session.execute(
        sa.text(
            "SELECT details ->> 'permission', details ->> 'connector_key', "
            "scope_type, scope_id "
            "FROM audit_logs WHERE tenant_id = :tenant_id "
            "AND event_type = 'REPORT_IMPORTED' "
            "AND entity_type = 'monthly_channel_revenue_fact' "
            "AND entity_id = :entity_id AND reason = :reason"
        ),
        {"tenant_id": TENANT_ID, "entity_id": AUDIT_ENTITY_ID, "reason": REASON},
    ).all()
    return [tuple(row) for row in rows]


def test_manual_revenue_fact_and_audit_share_lost_commit_fate_on_postgres(
    pg_url: str,
    owner_engine: sa.Engine,
) -> None:
    """A lost platform-request commit leaves neither manual fact nor audit.

    The route completes successfully. The overridden platform dependency then
    rolls back where production would commit. The real audit append is probed
    after its flush, proving the fact and one canonical audit row existed in
    the same Session before the rollback.
    """
    _seed_prerequisites(owner_engine)
    factory = build_platform_session_factory(pg_url)
    request_sessions: list[Session] = []

    def rollback_instead_of_commit() -> Iterator[Session]:
        """Yield the real platform request session, then lose its final commit."""
        with factory() as session:
            request_sessions.append(session)
            try:
                yield session
            finally:
                session.rollback()

    app = create_app(database_url=pg_url, authz_source="headers")
    app.dependency_overrides[current_platform_db_session] = rollback_instead_of_commit
    original_append = SqlAlchemyAuditSink.append
    observations: list[tuple[bool, tuple[object, ...] | None, list[tuple[object, ...]]]] = []

    def append_and_observe(sink: SqlAlchemyAuditSink, record: AuditRecord) -> None:
        """Run the real append, then capture same-transaction domain and audit state."""
        original_append(sink, record)
        assert record.event_type == "REPORT_IMPORTED"
        assert record.entity_id == AUDIT_ENTITY_ID
        observations.append(
            (
                bool(request_sessions) and sink._session is request_sessions[0],  # noqa: SLF001
                _fact_state(sink._session),  # noqa: SLF001
                _matching_audits(sink._session),  # noqa: SLF001
            )
        )

    with (
        patch.object(SqlAlchemyAuditSink, "append", append_and_observe),
        TestClient(app) as client,
    ):
        response = client.post(
            "/revenue/facts",
            headers=_auth_headers(),
            json=_request_payload(),
        )

    assert response.status_code == 201, response.text
    assert response.json()["source_kind"] == "MANUAL_UPLOAD"
    assert response.json()["gross_revenue_usd"] == "1234.56"
    assert response.json()["audit_event"]["event_type"] == "REPORT_IMPORTED"
    assert observations == [
        (
            True,
            (
                "MANUAL_UPLOAD",
                SOURCE_REPORT_ID,
                Decimal("1234.560000"),
                Decimal("987.650000"),
                ACTOR_ID,
            ),
            [
                (
                    "finance.import_manual_revenue",
                    CONNECTOR_KEY,
                    "connector",
                    CONNECTOR_KEY,
                )
            ],
        )
    ]

    with Session(owner_engine) as verifier:
        assert _fact_state(verifier) is None
        assert _matching_audits(verifier) == []
