# tests/api/test_tenants_api.py
"""Endpoint tests for GET /tenants/me (Spec A).

Cases 1-9 from Docs/superpowers/specs/2026-05-22-spec-a-frontend-tenant-header-design.md
section 9.1. Fixtures mirror tests/api/test_database_principals.py.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.security_models import (
    AccessScopeORM,
    RoleORM,
    SecurityBase,
    UserORM,
    UserRoleAssignmentORM,
)
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.models import TenantStatus

TEST_USER_ID = UUID("00000000-0000-0000-0000-0000000000aa")
BOOTSTRAP_TENANT_ID = UUID(UMS_TENANT_ID)
BOOTSTRAP_DISPLAY = "UMS"


def _gateway_headers(user_id: UUID = TEST_USER_ID) -> dict[str, str]:
    return {
        "X-User-ID": str(user_id),
        "X-UMS-Trusted-Gateway-Token": os.environ["UMS_TRUSTED_GATEWAY_TOKEN"],
    }


@pytest.fixture
def db_engine_url(tmp_path):
    db_path = tmp_path / "spec_a.sqlite"
    return f"sqlite:///{db_path}"


@pytest.fixture
def seeded_engine(db_engine_url):
    engine = create_engine(db_engine_url, future=True)
    TenantBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, future=True)
    with session_factory() as session:
        _seed_bootstrap_tenant(session)
        _seed_enabled_user(session)
        session.commit()
    yield engine
    engine.dispose()


def _seed_bootstrap_tenant(session: Session) -> None:
    now = datetime.now(UTC)
    session.add(
        TenantORM(
            id=BOOTSTRAP_TENANT_ID,
            slug="ums",
            display_name=BOOTSTRAP_DISPLAY,
            primary_currency="USD",
            status=TenantStatus.ACTIVE.value,
            onboarding_at=now,
            created_at=now,
            updated_at=now,
        )
    )


SCOPE_ID = UUID("00000000-0000-0000-0000-0000000000b1")
ROLE_KEY = "assistant_analyst"


def _seed_enabled_user(session: Session) -> None:
    """Seed ONE enabled SQL principal so the principal loader can hydrate it.

    Column set reconciled against backend/ums_smart_revenue/db/security_models.py:
    - RoleORM PK is ``key`` (Text), not ``id``; no ``tenant_id``/``status`` columns.
    - UserORM status values are lowercase ('active'); bool field is ``is_service_account``.
    - UserRoleAssignmentORM uses ``role_key``, ``scope_id`` (FK → AccessScopeORM.id),
      and ``active`` (bool); no ``status`` column.
    - AccessScopeORM row is required because the loader joins on ``scope_id``.
    """
    now = datetime.now(UTC)
    role = RoleORM(
        key=ROLE_KEY,
        label="Assistant Analyst",
        description="Assigned-scope analyst for analytics without default finance access.",
        service_only=False,
        created_at=now,
    )
    scope = AccessScopeORM(
        id=SCOPE_ID,
        scope_type="global",
        scope_id=None,
        label="Global",
        created_at=now,
        tenant_id=BOOTSTRAP_TENANT_ID,
    )
    user = UserORM(
        id=TEST_USER_ID,
        tenant_id=BOOTSTRAP_TENANT_ID,
        email="spec-a@example.invalid",
        display_name="Spec A Test User",
        status="active",
        is_service_account=False,
        created_at=now,
        updated_at=now,
    )
    assignment = UserRoleAssignmentORM(
        id=UUID("00000000-0000-0000-0000-0000000000a2"),
        tenant_id=BOOTSTRAP_TENANT_ID,
        user_id=TEST_USER_ID,
        role_key=ROLE_KEY,
        scope_id=SCOPE_ID,
        active=True,
        assigned_at=now,
    )
    session.add_all([role, scope, user, assignment])


@pytest.fixture
def app_db_mode(seeded_engine):
    app = create_app(database_url=str(seeded_engine.url), authz_source="database")
    return app


@pytest.fixture
def client_db_mode(app_db_mode):
    with TestClient(app_db_mode) as client:
        yield client


# ---------------------------------------------------------------------------
# Case 1 — happy path
# ---------------------------------------------------------------------------


def test_tenants_me_returns_bootstrap_tenant_for_resolved_slug(
    client_db_mode,
):
    response = client_db_mode.get(
        "/tenants/me",
        headers={"X-UMS-Tenant": "ums", **_gateway_headers()},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload == {
        "id": str(BOOTSTRAP_TENANT_ID),
        "slug": "ums",
        "display_name": BOOTSTRAP_DISPLAY,
    }
    assert response.headers.get("Vary") and "X-UMS-Tenant" in response.headers["Vary"]
