from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.security_models import ApiConnectorCredentialORM


SECRET_REF_PREFIXES = (
    "secret-manager://",
    "gcp-secret-manager://",
    "aws-secretsmanager://",
    "azure-keyvault://",
    "vault://",
    "kms://",
)


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


class SqlAlchemyConnectorCredentialRepository:
    def __init__(self, session: Session):
        self._session = session

    def list_credentials(self) -> list[ConnectorCredentialEntry]:
        rows = self._session.scalars(
            select(ApiConnectorCredentialORM).order_by(
                ApiConnectorCredentialORM.connector_key,
                ApiConnectorCredentialORM.account_id,
            )
        ).all()
        return [self._to_entry(row) for row in rows]

    def create_credential(
        self,
        *,
        connector_key: str,
        account_id: str,
        encrypted_secret_ref: str,
        actor_user_id: str,
    ) -> ConnectorCredentialEntry:
        existing = self._session.scalars(
            select(ApiConnectorCredentialORM).where(
                ApiConnectorCredentialORM.connector_key == connector_key,
                ApiConnectorCredentialORM.account_id == account_id,
            )
        ).one_or_none()
        if existing is not None:
            raise ValueError(f"Connector credential already exists: {connector_key}/{account_id}")

        row = ApiConnectorCredentialORM(
            id=uuid4(),
            connector_key=connector_key,
            account_id=account_id,
            encrypted_secret_ref=encrypted_secret_ref,
            status="active",
            created_by=_parse_uuid(actor_user_id),
            updated_by=_parse_uuid(actor_user_id),
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            self._session.rollback()
            raise ValueError(
                f"Connector credential already exists: {connector_key}/{account_id}"
            ) from exc
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
    return value.startswith(SECRET_REF_PREFIXES)


def _parse_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None
