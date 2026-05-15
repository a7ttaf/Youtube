from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.security_models import ApiConnectorCredentialORM, UserORM

SECRET_REF_PREFIXES = (
    "secret-manager://",
    "gcp-secret-manager://",
    "aws-secretsmanager://",
    "azure-keyvault://",
    "vault://",
    "kms://",
)
CONNECTOR_CREDENTIAL_UNIQUE_CONSTRAINT = "uq_api_connector_credentials_connector_account"
MAX_CREDENTIAL_PAGE_SIZE = 100


@dataclass(frozen=True)
class ConnectorCredentialEntry:
    id: str
    connector_key: str
    account_id: str
    status: str
    has_secret_ref: bool

    def to_api(self) -> dict[str, object]:
        return {
            "id": self.id,
            "connector_key": self.connector_key,
            "account_id": self.account_id,
            "status": self.status,
            "has_secret_ref": self.has_secret_ref,
        }


@dataclass(frozen=True)
class ConnectorCredentialPage:
    items: list[ConnectorCredentialEntry]
    limit: int
    offset: int
    has_more: bool


class ConnectorCredentialError(ValueError):
    pass


class ConnectorCredentialConflictError(ConnectorCredentialError):
    pass


class ConnectorCredentialValidationError(ConnectorCredentialError):
    pass


class SqlAlchemyConnectorCredentialRepository:
    def __init__(self, session: Session):
        self._session = session

    def list_credentials(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        connector_keys: frozenset[str] | None = None,
    ) -> ConnectorCredentialPage:
        if limit < 1 or limit > MAX_CREDENTIAL_PAGE_SIZE:
            raise ConnectorCredentialValidationError(
                f"limit must be between 1 and {MAX_CREDENTIAL_PAGE_SIZE}"
            )
        if offset < 0:
            raise ConnectorCredentialValidationError("offset must be greater than or equal to 0")
        if connector_keys is not None and not connector_keys:
            return ConnectorCredentialPage(items=[], limit=limit, offset=offset, has_more=False)
        statement = select(ApiConnectorCredentialORM).order_by(
            ApiConnectorCredentialORM.connector_key,
            ApiConnectorCredentialORM.account_id,
        )
        if connector_keys is not None:
            statement = statement.where(
                ApiConnectorCredentialORM.connector_key.in_(sorted(connector_keys))
            )
        rows = self._session.scalars(
            statement.limit(limit + 1).offset(offset)
        ).all()
        visible_rows = rows[:limit]
        return ConnectorCredentialPage(
            items=[self._to_entry(row) for row in visible_rows],
            limit=limit,
            offset=offset,
            has_more=len(rows) > limit,
        )

    def create_credential(
        self,
        *,
        connector_key: str,
        account_id: str,
        encrypted_secret_ref: str,
        actor_user_id: str,
    ) -> ConnectorCredentialEntry:
        actor_uuid = _parse_uuid(actor_user_id)
        if self._session.get(UserORM, actor_uuid) is None:
            raise ConnectorCredentialValidationError(
                "actor_user_id does not reference an existing user"
            )
        existing = self._session.scalars(
            select(ApiConnectorCredentialORM).where(
                ApiConnectorCredentialORM.connector_key == connector_key,
                ApiConnectorCredentialORM.account_id == account_id,
            )
        ).one_or_none()
        if existing is not None:
            raise ConnectorCredentialConflictError(
                f"Connector credential already exists: {connector_key}/{account_id}"
            )

        row = ApiConnectorCredentialORM(
            id=uuid4(),
            connector_key=connector_key,
            account_id=account_id,
            encrypted_secret_ref=encrypted_secret_ref,
            status="active",
            created_by=actor_uuid,
            updated_by=actor_uuid,
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            if _is_duplicate_credential_integrity_error(exc):
                raise ConnectorCredentialConflictError(
                    f"Connector credential already exists: {connector_key}/{account_id}"
                ) from exc
            if _is_foreign_key_integrity_error(exc):
                raise ConnectorCredentialValidationError(
                    "actor_user_id does not reference an existing user"
                ) from exc
            raise
        return self._to_entry(row)

    @staticmethod
    def _to_entry(row: ApiConnectorCredentialORM) -> ConnectorCredentialEntry:
        return ConnectorCredentialEntry(
            id=str(row.id),
            connector_key=row.connector_key,
            account_id=row.account_id,
            status=row.status,
            has_secret_ref=bool(row.encrypted_secret_ref),
        )


def is_external_secret_ref(value: str) -> bool:
    normalized = value.strip()
    if not normalized:
        return False
    return any(
        normalized.startswith(prefix) and bool(normalized[len(prefix) :].strip())
        for prefix in SECRET_REF_PREFIXES
    )


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise ConnectorCredentialValidationError("actor_user_id must be a valid UUID") from exc


def _is_duplicate_credential_integrity_error(exc: IntegrityError) -> bool:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    constraint_name = getattr(diag, "constraint_name", None)
    if constraint_name == CONNECTOR_CREDENTIAL_UNIQUE_CONSTRAINT:
        return True

    # PostgreSQL exposes the named constraint via diag.constraint_name; SQLite does not,
    # so keep an explicit fallback for its unique-constraint error text.
    error_text = f"{exc.orig!s} {exc!s}"
    return (
        CONNECTOR_CREDENTIAL_UNIQUE_CONSTRAINT in error_text
        or "UNIQUE constraint failed: api_connector_credentials.connector_key, api_connector_credentials.account_id"
        in error_text
    )


_ACTOR_FK_CONSTRAINTS = frozenset({
    "fk_api_connector_credentials_created_by",
    "fk_api_connector_credentials_updated_by",
})


def _is_foreign_key_integrity_error(exc: IntegrityError) -> bool:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    constraint_name = getattr(diag, "constraint_name", None)
    if constraint_name in _ACTOR_FK_CONSTRAINTS:
        return True
    error_text = f"{exc.orig!s} {exc!s}".lower()
    return any(name in error_text for name in _ACTOR_FK_CONSTRAINTS)
