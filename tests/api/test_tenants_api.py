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

from ums_smart_revenue.app import (
    TrustedGatewayTenantResolverMiddleware,  # noqa: F401  (used in Task 1.4 fixture)
    create_app,
)
from ums_smart_revenue.db.security_models import (
    RoleORM,  # noqa: F401  (used in Task 1.2 _seed_enabled_user)
    SecurityBase,
    UserORM,  # noqa: F401  (used in Task 1.2 _seed_enabled_user)
    UserRoleAssignmentORM,  # noqa: F401  (used in Task 1.2 _seed_enabled_user)
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


def _seed_enabled_user(session: Session) -> None:
    # Insert ONE enabled SQL principal with at least a placeholder role
    # so current_principal_from_database can hydrate the principal for
    # X-User-ID=TEST_USER_ID. Match the exact column set in
    # backend/ums_smart_revenue/db/security_models.py UserORM and RoleORM.
    raise NotImplementedError(
        "Fill in _seed_enabled_user in Task 1.2 against the real "
        "UserORM/RoleORM column set."
    )


@pytest.fixture
def app_db_mode(seeded_engine):
    app = create_app(database_url=str(seeded_engine.url), authz_source="database")
    return app


@pytest.fixture
def client_db_mode(app_db_mode):
    with TestClient(app_db_mode) as client:
        yield client
