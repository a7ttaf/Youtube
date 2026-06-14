"""Shared test helpers for committed-allocation API tests.

build_database_url, _client, and _committed_audit_rows are used identically in
test_committed_allocation_api.py and test_recalculation_write_api.py; they live
here to avoid the duplication flagged by Kody. _seed and scenario-specific
helpers remain file-local because each test module seeds a different set of
module-level UUID constants and user data.
"""

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.security_models import AuditLogORM


def build_database_url(tmp_path) -> str:
    """Return a unique SQLite URL under pytest's temp path."""
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def build_test_client(database_url: str, principal_factory) -> TestClient:
    """TestClient with the principal dependency overridden by `principal_factory`."""
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = principal_factory
    return TestClient(app)


def committed_audit_rows(database_url: str) -> list:
    """Return the ALLOCATION_COMMITTED audit rows persisted in the test database."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        return [
            row
            for row in session.scalars(select(AuditLogORM)).all()
            if row.event_type == "ALLOCATION_COMMITTED"
        ]
