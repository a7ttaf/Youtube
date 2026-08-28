from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.external_identities import SqlAlchemyExternalIdentityRepository
from ums_smart_revenue.db.security_models import ExternalIdentityORM, SecurityBase, UserORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

TENANT_ID = UUID(UMS_TENANT_ID)
USER_ID = UUID("00000000-0000-0000-0000-000000088001")


@pytest.fixture()
def session(tmp_path) -> Session:
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'ext_id.db').as_posix()}")
    SecurityBase.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            UserORM(
                id=USER_ID,
                tenant_id=TENANT_ID,
                email="operator@example.com",
                display_name="Operator",
            )
        )
        db.commit()
        yield db


def test_external_identity_resolves_provider_subject(session: Session) -> None:
    session.add(
        ExternalIdentityORM(
            id=uuid4(),
            tenant_id=TENANT_ID,
            provider="google",
            provider_subject="google-sub-123",
            normalized_email="operator@example.com",
            user_id=USER_ID,
        )
    )
    session.commit()
    repo = SqlAlchemyExternalIdentityRepository(session)
    assert repo.resolve_user_id(
        tenant_id=TENANT_ID,
        provider="google",
        provider_subject="google-sub-123",
    ) == USER_ID
    assert (
        repo.resolve_user_id(
            tenant_id=TENANT_ID,
            provider="google",
            provider_subject="unknown",
        )
        is None
    )


def test_external_identity_resolves_normalized_email(session: Session) -> None:
    session.add(
        ExternalIdentityORM(
            id=uuid4(),
            tenant_id=TENANT_ID,
            provider="google",
            provider_subject="google-sub-456",
            normalized_email="Operator@Example.com",
            user_id=USER_ID,
        )
    )
    session.commit()
    repo = SqlAlchemyExternalIdentityRepository(session)
    assert repo.resolve_by_email(
        tenant_id=TENANT_ID,
        provider="google",
        normalized_email="operator@example.com",
    ) == USER_ID
