from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.credentials import (
    CONNECTOR_CREDENTIAL_UNIQUE_CONSTRAINT,
    MAX_CREDENTIAL_PAGE_SIZE,
    SECRET_REF_PREFIXES,
    ConnectorCredentialConflictError,
    ConnectorCredentialEntry,
    ConnectorCredentialValidationError,
    SqlAlchemyConnectorCredentialRepository,
    _is_foreign_key_integrity_error,
    is_external_secret_ref,
)
from ums_smart_revenue.db.security_models import (
    ApiConnectorCredentialORM,
    SecurityBase,
    UserORM,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)
OTHER_TENANT_UUID = UUID("00000000-0000-0000-0000-000000081999")
ACTOR_USER_ID = "00000000-0000-0000-0000-000000081001"
OTHER_ACTOR_USER_ID = "00000000-0000-0000-0000-000000081002"


def build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    SecurityBase.metadata.create_all(engine)
    session = Session(engine)
    seed_actor_user(session)
    return session


def tenant(tenant_id: UUID = OTHER_TENANT_UUID) -> Tenant:
    now = datetime.now(UTC)
    return Tenant(
        id=tenant_id,
        slug="other",
        display_name="Other Tenant",
        primary_currency="USD",
        status=TenantStatus.ACTIVE,
        onboarding_at=now,
        created_at=now,
        updated_at=now,
    )


def seed_actor_user(
    session: Session,
    *,
    actor_user_id: str = ACTOR_USER_ID,
    tenant_id: UUID = DEFAULT_TENANT_UUID,
) -> None:
    session.add(
        UserORM(
            id=UUID(actor_user_id),
            tenant_id=tenant_id,
            email=f"connector-{actor_user_id}@example.com",
            display_name="Connector Actor",
        )
    )
    session.commit()


def create_default(
    repo: SqlAlchemyConnectorCredentialRepository,
    *,
    connector_key: str = "youtube_reporting",
    account_id: str = "content-owner-1",
    encrypted_secret_ref: str = "secret-manager://ums/youtube-reporting/content-owner-1",
    actor_user_id: str = ACTOR_USER_ID,
) -> ConnectorCredentialEntry:
    return repo.create_credential(
        connector_key=connector_key,
        account_id=account_id,
        encrypted_secret_ref=encrypted_secret_ref,
        actor_user_id=actor_user_id,
    )


# -----------------------------------------------------------------------------
# is_external_secret_ref
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "secret-manager://ums/youtube-reporting/content-owner-1",
        "gcp-secret-manager://projects/ums/secrets/yt/versions/1",
        "aws-secretsmanager://arn:aws:secretsmanager:eu-west-1:000000:ums",
        "azure-keyvault://ums-kv.vault.azure.net/secrets/yt",
        "vault://secret/data/ums/yt-content-owner-1",
        "kms://projects/ums/locations/eu/keyRings/yt/cryptoKeys/cms",
    ],
)
def test_is_external_secret_ref_accepts_every_allowlisted_prefix(value):
    assert is_external_secret_ref(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "secret-manager://",
        "secret-manager://   ",
        "vault://",
        "https://example.com/secret",
        "file:///etc/passwd",
        "plaintext-secret-value",
        "secret_manager://x",
        "  secret-manager://",
    ],
)
def test_is_external_secret_ref_rejects_blank_or_unsupported_values(value):
    assert is_external_secret_ref(value) is False


def test_is_external_secret_ref_strips_leading_and_trailing_whitespace():
    assert is_external_secret_ref("  secret-manager://ums/yt-1  ") is True


def test_secret_ref_prefixes_const_matches_documented_allowlist():
    assert set(SECRET_REF_PREFIXES) == {
        "secret-manager://",
        "gcp-secret-manager://",
        "aws-secretsmanager://",
        "azure-keyvault://",
        "vault://",
        "kms://",
    }


# -----------------------------------------------------------------------------
# ConnectorCredentialEntry.to_api
# -----------------------------------------------------------------------------


def test_entry_to_api_serializes_all_five_fields():
    entry = ConnectorCredentialEntry(
        id="11111111-1111-1111-1111-111111111111",
        connector_key="youtube_reporting",
        account_id="content-owner-1",
        status="active",
        has_secret_ref=True,
    )

    assert entry.to_api() == {
        "id": "11111111-1111-1111-1111-111111111111",
        "connector_key": "youtube_reporting",
        "account_id": "content-owner-1",
        "status": "active",
        "has_secret_ref": True,
    }


def test_entry_to_api_never_exposes_raw_secret_material():
    entry = ConnectorCredentialEntry(
        id="11111111-1111-1111-1111-111111111111",
        connector_key="youtube_reporting",
        account_id="content-owner-1",
        status="active",
        has_secret_ref=True,
    )

    api = entry.to_api()
    assert "encrypted_secret_ref" not in api
    assert "secret" not in api
    assert "secret_ref" not in api
    for value in api.values():
        if isinstance(value, str):
            assert "secret-manager://" not in value
            assert "vault://" not in value


# -----------------------------------------------------------------------------
# create_credential — happy path
# -----------------------------------------------------------------------------


def test_create_credential_stamps_tenant_and_persists_all_fields():
    with build_session() as session:
        repo = SqlAlchemyConnectorCredentialRepository(session)
        entry = create_default(repo)
        session.commit()

        rows = session.scalars(select(ApiConnectorCredentialORM)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.tenant_id == DEFAULT_TENANT_UUID
        assert row.connector_key == "youtube_reporting"
        assert row.account_id == "content-owner-1"
        assert row.encrypted_secret_ref == (
            "secret-manager://ums/youtube-reporting/content-owner-1"
        )
        assert row.status == "active"
        assert row.created_by == UUID(ACTOR_USER_ID)
        assert row.updated_by == UUID(ACTOR_USER_ID)
        assert row.id == UUID(entry.id)
        assert entry.has_secret_ref is True
        assert entry.status == "active"


def test_create_credential_uses_ambient_tenant_context():
    with build_session() as session:
        seed_actor_user(
            session, actor_user_id=OTHER_ACTOR_USER_ID, tenant_id=OTHER_TENANT_UUID
        )
        token = TENANT_CTX.set(tenant())
        try:
            repo = SqlAlchemyConnectorCredentialRepository(session)
            create_default(repo, actor_user_id=OTHER_ACTOR_USER_ID)
            session.commit()
        finally:
            TENANT_CTX.reset(token)

        row = session.scalars(select(ApiConnectorCredentialORM)).one()
        assert row.tenant_id == OTHER_TENANT_UUID


def test_create_credential_returns_entry_without_exposing_secret_ref_value():
    with build_session() as session:
        repo = SqlAlchemyConnectorCredentialRepository(session)
        entry = create_default(
            repo, encrypted_secret_ref="vault://secret/data/yt/content-owner-1"
        )
        session.commit()

        api = entry.to_api()
        assert api["has_secret_ref"] is True
        for value in api.values():
            if isinstance(value, str):
                assert "vault://" not in value


def test_create_credential_marks_blank_secret_ref_as_no_secret():
    with build_session() as session:
        repo = SqlAlchemyConnectorCredentialRepository(session)
        entry = repo.create_credential(
            connector_key="adsense",
            account_id="account-empty",
            encrypted_secret_ref="",
            actor_user_id=ACTOR_USER_ID,
        )
        session.commit()
        assert entry.has_secret_ref is False


# -----------------------------------------------------------------------------
# create_credential — duplicate detection
# -----------------------------------------------------------------------------


def test_create_credential_raises_conflict_on_same_tenant_pre_check_duplicate():
    with build_session() as session:
        repo = SqlAlchemyConnectorCredentialRepository(session)
        create_default(repo)
        session.commit()

        with pytest.raises(ConnectorCredentialConflictError) as excinfo:
            create_default(repo)
        assert "youtube_reporting/content-owner-1" in str(excinfo.value)


def test_create_credential_for_distinct_connector_and_account_succeeds():
    with build_session() as session:
        repo = SqlAlchemyConnectorCredentialRepository(session)
        create_default(repo, connector_key="youtube_reporting", account_id="account-a")
        create_default(repo, connector_key="youtube_reporting", account_id="account-b")
        create_default(repo, connector_key="adsense", account_id="account-a")
        session.commit()

        assert len(session.scalars(select(ApiConnectorCredentialORM)).all()) == 3


def test_create_credential_same_key_under_different_tenants_does_not_conflict():
    with build_session() as session:
        primary_repo = SqlAlchemyConnectorCredentialRepository(session)
        create_default(primary_repo)
        session.commit()

        other_repo = SqlAlchemyConnectorCredentialRepository(
            session, tenant_id=OTHER_TENANT_UUID
        )
        seed_actor_user(
            session, actor_user_id=OTHER_ACTOR_USER_ID, tenant_id=OTHER_TENANT_UUID
        )
        create_default(other_repo, actor_user_id=OTHER_ACTOR_USER_ID)
        session.commit()

        rows = session.scalars(
            select(ApiConnectorCredentialORM).order_by(
                ApiConnectorCredentialORM.tenant_id
            )
        ).all()
        assert {row.tenant_id for row in rows} == {
            DEFAULT_TENANT_UUID,
            OTHER_TENANT_UUID,
        }
        assert len(rows) == 2


# -----------------------------------------------------------------------------
# create_credential — input validation
# -----------------------------------------------------------------------------


def test_create_credential_rejects_malformed_actor_user_id():
    with build_session() as session:
        repo = SqlAlchemyConnectorCredentialRepository(session)
        with pytest.raises(
            ConnectorCredentialValidationError,
            match="actor_user_id must be a valid UUID",
        ):
            create_default(repo, actor_user_id="not-a-uuid")


def test_create_credential_rejects_empty_actor_user_id():
    with build_session() as session:
        repo = SqlAlchemyConnectorCredentialRepository(session)
        with pytest.raises(ConnectorCredentialValidationError):
            create_default(repo, actor_user_id="")


def test_create_credential_rejects_actor_user_from_another_tenant():
    with build_session() as session:
        seed_actor_user(
            session, actor_user_id=OTHER_ACTOR_USER_ID, tenant_id=OTHER_TENANT_UUID
        )
        repo = SqlAlchemyConnectorCredentialRepository(session)

        with pytest.raises(
            ConnectorCredentialValidationError,
            match="actor_user_id does not reference an existing user",
        ):
            create_default(repo, actor_user_id=OTHER_ACTOR_USER_ID)


# -----------------------------------------------------------------------------
# list_credentials
# -----------------------------------------------------------------------------


def test_list_credentials_returns_default_paged_view_ordered_by_key_account():
    with build_session() as session:
        repo = SqlAlchemyConnectorCredentialRepository(session)
        create_default(repo, connector_key="youtube_reporting", account_id="b")
        create_default(repo, connector_key="adsense", account_id="z")
        create_default(repo, connector_key="adsense", account_id="a")
        create_default(repo, connector_key="youtube_reporting", account_id="a")
        session.commit()

        page = repo.list_credentials()
        keys = [(entry.connector_key, entry.account_id) for entry in page.items]
        assert keys == [
            ("adsense", "a"),
            ("adsense", "z"),
            ("youtube_reporting", "a"),
            ("youtube_reporting", "b"),
        ]
        assert page.limit == 50
        assert page.offset == 0
        assert page.has_more is False


def test_list_credentials_paginates_with_has_more_flag():
    with build_session() as session:
        repo = SqlAlchemyConnectorCredentialRepository(session)
        for index in range(5):
            create_default(
                repo, connector_key="youtube_reporting", account_id=f"acct-{index}"
            )
        session.commit()

        first = repo.list_credentials(limit=2, offset=0)
        assert len(first.items) == 2
        assert first.has_more is True
        assert first.limit == 2

        second = repo.list_credentials(limit=2, offset=2)
        assert len(second.items) == 2
        assert second.has_more is True

        third = repo.list_credentials(limit=2, offset=4)
        assert len(third.items) == 1
        assert third.has_more is False


def test_list_credentials_rejects_bad_limit_and_offset():
    with build_session() as session:
        repo = SqlAlchemyConnectorCredentialRepository(session)
        with pytest.raises(
            ConnectorCredentialValidationError, match="limit must be between"
        ):
            repo.list_credentials(limit=0)
        with pytest.raises(
            ConnectorCredentialValidationError, match="limit must be between"
        ):
            repo.list_credentials(limit=MAX_CREDENTIAL_PAGE_SIZE + 1)
        with pytest.raises(
            ConnectorCredentialValidationError, match="offset must be greater"
        ):
            repo.list_credentials(offset=-1)


def test_list_credentials_does_not_surface_cross_tenant_rows():
    with build_session() as session:
        primary_repo = SqlAlchemyConnectorCredentialRepository(session)
        create_default(primary_repo, connector_key="adsense", account_id="primary-1")
        create_default(primary_repo, connector_key="adsense", account_id="primary-2")
        session.commit()

        other_repo = SqlAlchemyConnectorCredentialRepository(
            session, tenant_id=OTHER_TENANT_UUID
        )
        seed_actor_user(
            session, actor_user_id=OTHER_ACTOR_USER_ID, tenant_id=OTHER_TENANT_UUID
        )
        create_default(
            other_repo,
            connector_key="adsense",
            account_id="foreign-1",
            actor_user_id=OTHER_ACTOR_USER_ID,
        )
        create_default(
            other_repo,
            connector_key="adsense",
            account_id="foreign-2",
            actor_user_id=OTHER_ACTOR_USER_ID,
        )
        create_default(
            other_repo,
            connector_key="adsense",
            account_id="foreign-3",
            actor_user_id=OTHER_ACTOR_USER_ID,
        )
        session.commit()

        primary_page = primary_repo.list_credentials()
        assert {entry.account_id for entry in primary_page.items} == {
            "primary-1",
            "primary-2",
        }

        other_page = other_repo.list_credentials()
        assert {entry.account_id for entry in other_page.items} == {
            "foreign-1",
            "foreign-2",
            "foreign-3",
        }


def test_list_credentials_returns_empty_page_when_no_rows_match():
    with build_session() as session:
        repo = SqlAlchemyConnectorCredentialRepository(session)
        page = repo.list_credentials()
        assert page.items == []
        assert page.has_more is False
        assert page.limit == 50
        assert page.offset == 0


def test_list_credentials_never_exposes_encrypted_secret_ref_in_serialized_output():
    with build_session() as session:
        repo = SqlAlchemyConnectorCredentialRepository(session)
        create_default(
            repo, encrypted_secret_ref="vault://secret/data/yt/content-owner-1"
        )
        session.commit()

        page = repo.list_credentials()
        assert len(page.items) == 1
        api = page.items[0].to_api()
        assert "encrypted_secret_ref" not in api
        assert api["has_secret_ref"] is True
        for value in api.values():
            if isinstance(value, str):
                assert "vault://" not in value


# -----------------------------------------------------------------------------
# Direct ORM integrity test — proves unique constraint protects against
# race-condition writes that bypass the pre-check
# -----------------------------------------------------------------------------


def test_direct_orm_insert_with_duplicate_composite_key_raises_integrity_error():
    from sqlalchemy.exc import IntegrityError

    with build_session() as session:
        session.add(
            ApiConnectorCredentialORM(
                id=uuid4(),
                tenant_id=DEFAULT_TENANT_UUID,
                connector_key="youtube_reporting",
                account_id="content-owner-1",
                encrypted_secret_ref="vault://x",
                status="active",
            )
        )
        session.commit()

        session.add(
            ApiConnectorCredentialORM(
                id=uuid4(),
                tenant_id=DEFAULT_TENANT_UUID,
                connector_key="youtube_reporting",
                account_id="content-owner-1",
                encrypted_secret_ref="vault://y",
                status="active",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


# -----------------------------------------------------------------------------
# Repository defaults & constants
# -----------------------------------------------------------------------------


def test_repository_default_tenant_id_matches_constant():
    with build_session() as session:
        repo = SqlAlchemyConnectorCredentialRepository(session)
        assert repo._tenant_id == DEFAULT_TENANT_UUID  # noqa: SLF001


def test_repository_rejects_malformed_tenant_id():
    with build_session() as session:
        with pytest.raises(
            ConnectorCredentialValidationError, match="tenant_id must be a valid UUID"
        ):
            SqlAlchemyConnectorCredentialRepository(session, tenant_id="not-a-uuid")


@pytest.mark.parametrize(
    "constraint_name",
    [
        "fk_api_connector_credentials_tenant_created_by",
        "fk_api_connector_credentials_tenant_updated_by",
    ],
)
def test_foreign_key_integrity_detection_includes_tenant_scoped_actor_constraints(
    constraint_name,
):
    class _Diag:
        def __init__(self, name: str) -> None:
            self.constraint_name = name

    class _Orig:
        def __init__(self, name: str) -> None:
            self.diag = _Diag(name)

    assert _is_foreign_key_integrity_error(
        IntegrityError("stmt", {}, _Orig(constraint_name))
    )


def test_max_credential_page_size_is_positive_integer():
    assert isinstance(MAX_CREDENTIAL_PAGE_SIZE, int)
    assert MAX_CREDENTIAL_PAGE_SIZE >= 1


def test_unique_constraint_name_is_documented_in_module():
    assert CONNECTOR_CREDENTIAL_UNIQUE_CONSTRAINT == (
        "uq_api_connector_credentials_connector_account"
    )
