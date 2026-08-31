# ============================================================================
# Purpose: Verify tenant-scoped external-identity repository lookups without
#   implying trusted-gateway or principal-loader integration.
# Database/ORM: SQLite SecurityBase; users and external_identities.
# Standards: Explicit tenant IDs, exact provider subjects, and absent-map proof.
# Blast Radius: Test-only authorization repository coverage.
# Connections:
#   - File: backend/ums_smart_revenue/auth/external_identities.py -> Subject.
#   - File: tests/db/test_external_identity_withholding_migration_postgres.py -> RLS proof.
# ============================================================================
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.external_identities import (
    ExternalIdentityStorageError,
    InvalidExternalIdentityClaimError,
    SqlAlchemyExternalIdentityRepository,
)
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
    assert (
        repo.resolve_user_id(
            tenant_id=TENANT_ID,
            provider="google",
            provider_subject="google-sub-123",
        )
        == USER_ID
    )
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
    assert (
        repo.resolve_by_email(
            tenant_id=TENANT_ID,
            provider="google",
            normalized_email="operator@example.com",
        )
        == USER_ID
    )


# ============================================================================
# Purpose: Prove the review-P2 fail-closed claim contract: malformed identities
#   must never resolve a real user, at the repository boundary AND in storage.
# Connections:
#   - File: backend/ums_smart_revenue/auth/external_identities.py -> Guards.
#   - File: backend/ums_smart_revenue/db/security_models.py -> CHECK mirrors.
# ============================================================================
@pytest.mark.parametrize(
    ("provider", "provider_subject"),
    [
        ("", "google-sub-123"),
        ("google", ""),
        ("google", "google-sub-123 "),
        (" google", "google-sub-123"),
        ("google", "google-sub-123\n"),
        ("goo gle", "google-sub-123"),
    ],
)
def test_resolve_user_id_rejects_malformed_claims(
    session: Session, provider: str, provider_subject: str
) -> None:
    repo = SqlAlchemyExternalIdentityRepository(session)
    with pytest.raises(InvalidExternalIdentityClaimError):
        repo.resolve_user_id(
            tenant_id=TENANT_ID,
            provider=provider,
            provider_subject=provider_subject,
        )


@pytest.mark.parametrize(
    ("provider", "normalized_email"),
    [
        ("", "operator@example.com"),
        ("google", ""),
        ("google", "operator@example.com "),
        ("google", " operator@example.com"),
        ("google", "operator @example.com"),
        ("google", "operator@example.com\t"),
    ],
)
def test_resolve_by_email_rejects_malformed_claims(
    session: Session, provider: str, normalized_email: str
) -> None:
    repo = SqlAlchemyExternalIdentityRepository(session)
    with pytest.raises(InvalidExternalIdentityClaimError):
        repo.resolve_by_email(
            tenant_id=TENANT_ID,
            provider=provider,
            normalized_email=normalized_email,
        )


def test_malformed_claims_do_not_query_storage(session: Session) -> None:
    repo = SqlAlchemyExternalIdentityRepository(session)
    original_scalar = session.scalar

    def guarded_scalar(*_args, **_kwargs):
        raise AssertionError("malformed claims must fail before any storage read")

    session.scalar = guarded_scalar
    try:
        with pytest.raises(InvalidExternalIdentityClaimError):
            repo.resolve_user_id(tenant_id=TENANT_ID, provider="", provider_subject="")
        with pytest.raises(InvalidExternalIdentityClaimError):
            repo.resolve_by_email(tenant_id=TENANT_ID, provider="google", normalized_email="")
    finally:
        session.scalar = original_scalar


def test_resolve_user_id_translates_storage_error(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = SqlAlchemyExternalIdentityRepository(session)

    def raise_storage_error(*_args, **_kwargs):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(session, "scalar", raise_storage_error)
    with pytest.raises(ExternalIdentityStorageError, match="Unable to load"):
        repo.resolve_user_id(
            tenant_id=TENANT_ID, provider="google", provider_subject="google-sub-1"
        )


def test_resolve_by_email_translates_storage_error(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = SqlAlchemyExternalIdentityRepository(session)

    def raise_storage_error(*_args, **_kwargs):
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(session, "scalar", raise_storage_error)
    with pytest.raises(ExternalIdentityStorageError, match="Unable to load"):
        repo.resolve_by_email(
            tenant_id=TENANT_ID, provider="google", normalized_email="operator@example.com"
        )


def test_storage_rejects_blank_claim_rows(session: Session) -> None:
    # The review counterexample: an owner-loaded row with provider="" and
    # provider_subject="" must be impossible to store, not merely ignored.
    session.add(
        ExternalIdentityORM(
            id=uuid4(),
            tenant_id=TENANT_ID,
            provider="",
            provider_subject="",
            normalized_email="operator@example.com",
            user_id=USER_ID,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
