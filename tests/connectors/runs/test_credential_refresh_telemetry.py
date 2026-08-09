"""Tests for the Part 2 credential refresh telemetry stamp in resolve_*."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from ums_smart_revenue.connectors.google.errors import OAuthRefreshError
from ums_smart_revenue.connectors.runs.orchestrator import (
    resolve_connector_credentials,
)
from ums_smart_revenue.db.org_models import OrgBase
from ums_smart_revenue.db.security_models import (
    ApiConnectorCredentialORM,
    SecurityBase,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

TENANT = UUID(UMS_TENANT_ID)


def _factory(tmp_path) -> sessionmaker:
    url = f"sqlite+pysqlite:///{(tmp_path / 'tel.db').as_posix()}"
    engine = create_engine(url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            ApiConnectorCredentialORM(
                id=uuid4(),
                tenant_id=TENANT,
                connector_key="youtube_reporting",
                account_id="acct-1",
                encrypted_secret_ref="secret-manager://ums/yt/acct-1",
                status="active",
            )
        )
        session.commit()
    return sessionmaker(bind=engine, expire_on_commit=False)


def _fake_credentials(expiry):
    return SimpleNamespace(expiry=expiry)


def test_success_stamp_persists_after_caller_commit(tmp_path) -> None:
    """A successful refresh stamps succeeded + token_expiry and rides the commit."""
    from datetime import UTC, datetime

    factory = _factory(tmp_path)
    expiry = datetime(2026, 6, 1, tzinfo=UTC)
    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.resolve_secret",
            return_value={},
        ),
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.build_credentials_from_payload",
            return_value=_fake_credentials(expiry),
        ),
        patch("ums_smart_revenue.connectors.runs.orchestrator.ensure_default_resolvers"),
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials"),
        factory() as session,
    ):
        resolve_connector_credentials(
            session=session,
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
        )
        session.commit()
    with factory() as session:
        row = session.scalars(select(ApiConnectorCredentialORM)).one()
    assert row.last_refresh_status == "succeeded"
    assert row.last_refresh_error_class is None
    # SQLite strips tzinfo on DateTime(timezone=True) read-back (Postgres keeps
    # it); compare the stored instant rather than the raw aware/naive value.
    stored = row.token_expiry_at
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=UTC)
    assert stored == expiry
    assert row.last_refresh_attempt_at is not None


def test_failure_stamp_persists_and_reraises(tmp_path) -> None:
    """An OAuthRefreshError stamps failed (committed) AND still propagates."""
    factory = _factory(tmp_path)

    def _boom(_creds):
        raise OAuthRefreshError(inner=RuntimeError("revoked"))

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.resolve_secret",
            return_value={},
        ),
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.build_credentials_from_payload",
            return_value=_fake_credentials(None),
        ),
        patch("ums_smart_revenue.connectors.runs.orchestrator.ensure_default_resolvers"),
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials",
            _boom,
        ),
        factory() as session,
        pytest.raises(OAuthRefreshError),
    ):
        resolve_connector_credentials(
            session=session,
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
        )
        # The caller never commits on the failure path.
    # A separate session sees the committed failure stamp.
    with factory() as session:
        row = session.scalars(select(ApiConnectorCredentialORM)).one()
    assert row.last_refresh_status == "failed"
    assert row.last_refresh_error_class == "RuntimeError"
    assert row.token_expiry_at is None


def test_not_found_does_not_stamp(tmp_path) -> None:
    """CredentialNotFoundError (no refresh attempted) leaves telemetry NULL."""
    from ums_smart_revenue.connectors.google.errors import (
        CredentialNotFoundError,
    )

    factory = _factory(tmp_path)
    with factory() as session, pytest.raises(CredentialNotFoundError):
        resolve_connector_credentials(
            session=session,
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="missing",
        )
    with factory() as session:
        row = session.scalars(select(ApiConnectorCredentialORM)).one()
    assert row.last_refresh_status is None
    assert row.last_refresh_attempt_at is None


def test_dry_run_success_not_persisted_without_caller_commit(tmp_path) -> None:
    """Success stamp rides the caller commit; no commit -> not persisted (dry-run)."""
    from datetime import UTC, datetime

    factory = _factory(tmp_path)
    expiry = datetime(2026, 6, 1, tzinfo=UTC)
    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.resolve_secret",
            return_value={},
        ),
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.build_credentials_from_payload",
            return_value=_fake_credentials(expiry),
        ),
        patch("ums_smart_revenue.connectors.runs.orchestrator.ensure_default_resolvers"),
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials"),
        factory() as session,
    ):
        resolve_connector_credentials(
            session=session,
            tenant_id=TENANT,
            connector_key="youtube_reporting",
            account_id="acct-1",
        )
        session.rollback()  # dry-run / CLI never commits the success stamp
    with factory() as session:
        row = session.scalars(select(ApiConnectorCredentialORM)).one()
    assert row.last_refresh_status is None
