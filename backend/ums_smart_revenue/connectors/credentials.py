# ============================================================================
# Purpose: Connector credential value objects, repository helpers, read-only
# health labels, and live-run admission rules derived from persisted credential
# refresh telemetry.
# Database/ORM: ApiConnectorCredentialORM reads/writes through SQLAlchemy.
# Standards: Tenant-scoped repository access, safe external-secret reference
# validation, pure health/admission helpers, and no secret payload exposure.
# Blast Radius: Connector credential management and live connector preflight
# only. No finance math, schema, graph projection, or Phantom contract change.
# Connections:
#   - File: backend/ums_smart_revenue/api/connectors.py -> credential API
#     routes and live job preflight.
#   - File: scripts/run_google_connector.py -> operator live-run preflight.
# ============================================================================
"""Connector credential repository and live-run admission helpers."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Self
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.security_models import ApiConnectorCredentialORM, UserORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

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
_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)


@dataclass(frozen=True)
class ConnectorCredentialEntry:
    id: str
    connector_key: str
    account_id: str
    status: str
    has_secret_ref: bool
    last_refresh_attempt_at: datetime | None = None
    token_expiry_at: datetime | None = None
    last_refresh_status: str | None = None
    last_refresh_error_class: str | None = None

    def to_api(self) -> dict[str, object]:
        return {
            "id": self.id,
            "connector_key": self.connector_key,
            "account_id": self.account_id,
            "status": self.status,
            "has_secret_ref": self.has_secret_ref,
            "last_refresh_attempt_at": (
                self.last_refresh_attempt_at.isoformat()
                if self.last_refresh_attempt_at is not None
                else None
            ),
            "token_expiry_at": (
                self.token_expiry_at.isoformat() if self.token_expiry_at is not None else None
            ),
            "last_refresh_status": self.last_refresh_status,
            "last_refresh_error_class": self.last_refresh_error_class,
        }


# ============================================================================
# Purpose: Derive a coarse, read-only health label for a connector credential
#   from its already-persisted refresh telemetry. Pure and side-effect free so
#   the route can pass an explicit as_of and the rules stay unit-testable.
# Database/ORM: None (operates on a ConnectorCredentialEntry value object).
# Standards: Returns a fixed literal set; never raises on naive/aware datetime
#   mismatch (token_expiry_at is normalized to UTC before comparison so the
#   SQLite read-back tz-naive value compares safely against an aware as_of).
# Blast Radius: Connector credential read surface only. No finance, auth-
#   mutation, audit, or graph projection impact.
# Connections:
#   - File: backend/ums_smart_revenue/api/connectors.py -> the health route
#     appends this label to each credential's to_api() shape.
# ============================================================================
CREDENTIAL_EXPIRY_WINDOW = timedelta(hours=24)
LIVE_CREDENTIAL_SMOKE_REQUIRED_DETAIL = (
    "Connector credential must pass credential smoke before live jobs"
)


def derive_credential_health_state(entry: ConnectorCredentialEntry, *, as_of: datetime) -> str:
    """Return one of healthy/expiring/auth_failed/missing/unknown for a credential.

    Rules (evaluated in order):
      - unknown: the credential status is not 'active' (disabled, rotating,
        failed_auth, or any future non-active status).
      - auth_failed: last_refresh_status is a failure or last_refresh_error_class
        is set.
      - missing: no stored secret reference.
      - expiring: token_expiry_at is at or before as_of + 24h.
      - healthy: last refresh succeeded and the token is not expiring.
      - unknown: none of the above can be determined.
    """
    as_of = _as_aware_utc(as_of)
    # FIX: reject any non-active credential — the orchestrator
    # (orchestrator.py:684) only accepts status == "active", so the health
    # surface must not label rotating/failed_auth/disabled as usable.
    if entry.status != "active":
        return "unknown"
    if entry.last_refresh_status == "failed" or entry.last_refresh_error_class:
        return "auth_failed"
    if not entry.has_secret_ref:
        return "missing"
    if entry.token_expiry_at is not None:
        expiry = _as_aware_utc(entry.token_expiry_at)
        if expiry <= as_of + CREDENTIAL_EXPIRY_WINDOW:
            return "expiring"
    if entry.last_refresh_status == "succeeded":
        return "healthy"
    return "unknown"


# ============================================================================
# Purpose: Decide whether persisted credential refresh telemetry is strong
#   enough to permit a stateful live connector run. This is intentionally
#   stricter than the read-only health label because live ingestion must prove
#   the owner-approved credential smoke path has completed a successful refresh
#   before a live run is allowed to start.
# Database/ORM: None (operates on a ConnectorCredentialEntry value object).
# Standards: Pure, side-effect free, safe operator-facing rejection text only.
# Blast Radius: Connector live-run admission control only; no credential writes,
#   finance calculations, audit mutation, or schema changes.
# Connections:
#   - File: backend/ums_smart_revenue/api/connectors.py -> live job preflight.
#   - File: scripts/run_google_connector.py -> operator CLI live preflight.
# ============================================================================
def live_credential_rejection_detail(
    entry: ConnectorCredentialEntry,
    *,
    as_of: datetime,
) -> str | None:
    if entry.status != "active":
        return "Connector credential is not active"
    if not entry.has_secret_ref:
        return "Connector credential secret reference is missing"
    missing_successful_smoke = (
        entry.last_refresh_status != "succeeded"
        or bool(entry.last_refresh_error_class)
        or entry.last_refresh_attempt_at is None
        or entry.token_expiry_at is None
    )
    if missing_successful_smoke:
        return LIVE_CREDENTIAL_SMOKE_REQUIRED_DETAIL
    token_expiry_at = entry.token_expiry_at
    if token_expiry_at is None:
        return LIVE_CREDENTIAL_SMOKE_REQUIRED_DETAIL
    expiry = _as_aware_utc(token_expiry_at)
    if expiry <= _as_aware_utc(as_of):
        return LIVE_CREDENTIAL_SMOKE_REQUIRED_DETAIL
    return None


def _as_aware_utc(value: datetime) -> datetime:
    """Normalize a tz-naive datetime (SQLite read-back) to UTC for comparison."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


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
    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        """Bind connector credential reads and writes to one tenant."""
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    # ============================================================================
    # Purpose: Rebind the request-scoped credential repository to the resolved
    #   tenant without changing the SQLAlchemy session or transaction lifecycle.
    # Database/ORM: ApiConnectorCredentialORM through the repository session.
    # Standards: Keeps tenant scope explicit at the route boundary and
    #   repository-owned for credential reads and writes.
    # Blast Radius: Connector credential reads/writes only; no finance, audit,
    #   authorization mutation, or Neo4j projection impact.
    # Connections:
    #   - File: backend/ums_smart_revenue/api/connectors.py -> Health route
    #     binds reads to the authenticated principal tenant before listing.
    #   - File: tests/api/test_connectors_api.py -> Covers non-bootstrap
    #     tenant binding for credential health reads.
    # ============================================================================
    def for_tenant(self, tenant_id: UUID | str) -> Self:
        """Return a same-session repository bound to an explicit tenant."""
        return type(self)(self._session, tenant_id=tenant_id)

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
        statement = (
            select(ApiConnectorCredentialORM)
            .where(ApiConnectorCredentialORM.tenant_id == self._tenant_id)
            .order_by(
                ApiConnectorCredentialORM.connector_key,
                ApiConnectorCredentialORM.account_id,
            )
        )
        if connector_keys is not None:
            statement = statement.where(
                ApiConnectorCredentialORM.connector_key.in_(sorted(connector_keys))
            )
        rows = self._session.scalars(statement.limit(limit + 1).offset(offset)).all()
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
        actor_exists = self._session.scalar(
            select(UserORM.id).where(
                UserORM.id == actor_uuid,
                UserORM.tenant_id == self._tenant_id,
            )
        )
        if actor_exists is None:
            raise ConnectorCredentialValidationError(
                "actor_user_id does not reference an existing user"
            )
        existing = self._session.scalars(
            select(ApiConnectorCredentialORM).where(
                ApiConnectorCredentialORM.tenant_id == self._tenant_id,
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
            tenant_id=self._tenant_id,
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

    # ========================================================================
    # Purpose: Tenant-scoped point read of one connector credential row for the
    #   pre-submission validation in POST /connectors/jobs (cheap; NO OAuth
    #   refresh). Returns the serialized entry (status visible) or None.
    # Database/ORM: ApiConnectorCredentialORM (read only).
    # Standards: tenant_id always filtered; reuses _to_entry so the read shape
    #   stays single-sourced.
    # Blast Radius: Connector credential read surface only. No finance, audit,
    #   auth-mutation, or graph projection impact.
    # Connections:
    #   - File: backend/ums_smart_revenue/api/connectors.py ->
    #     request_connector_job validates credential existence/status.
    # ========================================================================
    def get_credential(
        self,
        *,
        connector_key: str,
        account_id: str,
    ) -> ConnectorCredentialEntry | None:
        """Return the tenant-scoped credential entry for the scope, or None."""
        row = self._session.scalars(
            select(ApiConnectorCredentialORM).where(
                ApiConnectorCredentialORM.tenant_id == self._tenant_id,
                ApiConnectorCredentialORM.connector_key == connector_key,
                ApiConnectorCredentialORM.account_id == account_id,
            )
        ).one_or_none()
        if row is None:
            return None
        return self._to_entry(row)

    @staticmethod
    def _to_entry(row: ApiConnectorCredentialORM) -> ConnectorCredentialEntry:
        return ConnectorCredentialEntry(
            id=str(row.id),
            connector_key=row.connector_key,
            account_id=row.account_id,
            status=row.status,
            has_secret_ref=bool(row.encrypted_secret_ref),
            last_refresh_attempt_at=row.last_refresh_attempt_at,
            token_expiry_at=row.token_expiry_at,
            last_refresh_status=row.last_refresh_status,
            last_refresh_error_class=row.last_refresh_error_class,
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


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    """Resolve tenant id from explicit param, request context, or bootstrap."""
    if tenant_id is not None:
        return _parse_tenant_uuid(tenant_id)
    current_tenant = get_current_tenant()
    if current_tenant is not None:
        return current_tenant.id
    return _DEFAULT_TENANT_UUID


def _parse_tenant_uuid(tenant_id: UUID | str) -> UUID:
    """Normalize tenant constructor input into a UUID object."""
    if isinstance(tenant_id, UUID):
        return tenant_id
    try:
        return UUID(tenant_id.strip())
    except (AttributeError, ValueError) as exc:
        raise ConnectorCredentialValidationError("tenant_id must be a valid UUID") from exc


def _is_duplicate_credential_integrity_error(exc: IntegrityError) -> bool:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    constraint_name = getattr(diag, "constraint_name", None)
    if constraint_name == CONNECTOR_CREDENTIAL_UNIQUE_CONSTRAINT:
        return True

    # PostgreSQL exposes the named constraint via diag.constraint_name; SQLite does not,
    # so keep an explicit fallback for its unique-constraint error text.
    error_text = f"{exc.orig!s} {exc!s}"
    tenant_scoped_sqlite_constraint = (
        "UNIQUE constraint failed: api_connector_credentials.tenant_id, "
        "api_connector_credentials.connector_key, "
        "api_connector_credentials.account_id"
    )
    legacy_sqlite_constraint = (
        "UNIQUE constraint failed: api_connector_credentials.connector_key, "
        "api_connector_credentials.account_id"
    )
    return (
        CONNECTOR_CREDENTIAL_UNIQUE_CONSTRAINT in error_text
        or tenant_scoped_sqlite_constraint in error_text
        or legacy_sqlite_constraint in error_text
    )


_ACTOR_FK_CONSTRAINTS = frozenset(
    {
        "fk_api_connector_credentials_created_by",
        "fk_api_connector_credentials_updated_by",
        "fk_api_connector_credentials_tenant_created_by",
        "fk_api_connector_credentials_tenant_updated_by",
    }
)


def _is_foreign_key_integrity_error(exc: IntegrityError) -> bool:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    constraint_name = getattr(diag, "constraint_name", None)
    if constraint_name in _ACTOR_FK_CONSTRAINTS:
        return True
    error_text = f"{exc.orig!s} {exc!s}".lower()
    if any(name in error_text for name in _ACTOR_FK_CONSTRAINTS):
        return True
    # SQLite surfaces FK failures as "FOREIGN KEY constraint failed" without a
    # constraint name; catch that form so test environments behave correctly.
    return "foreign key constraint failed" in error_text or (
        "foreign key" in error_text and "constraint failed" in error_text
    )
