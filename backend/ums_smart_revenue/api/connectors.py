"""FastAPI route handlers for connector credential management and test-connection probing."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ums_smart_revenue.api.channels import audit_record_to_api, current_audit_sink
from ums_smart_revenue.api.dependencies import current_db_session, current_principal_from_headers
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.connectors.credentials import (
    MAX_CREDENTIAL_PAGE_SIZE,
    ConnectorCredentialConflictError,
    ConnectorCredentialEntry,
    ConnectorCredentialValidationError,
    SqlAlchemyConnectorCredentialRepository,
    is_external_secret_ref,
)
from ums_smart_revenue.connectors.google.errors import (
    CredentialNotFoundError,
    GoogleConnectorError,
    InactiveCredentialError,
    OAuthRefreshError,
)
from ums_smart_revenue.connectors.runs.orchestrator import resolve_connector_credentials
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

router = APIRouter(prefix="/connectors", tags=["connectors"])


class NonBlankRequestModel(BaseModel):
    """Base model that strips whitespace and rejects blank string fields."""

    @field_validator("*", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        """Strip whitespace and reject blank values on all string fields."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("must not be blank")
            return stripped
        return value


class ConnectorCredentialCreateRequest(NonBlankRequestModel):
    """Request body for creating a connector credential reference."""

    connector_key: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    encrypted_secret_ref: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ConnectorJobRequest(NonBlankRequestModel):
    """Request body for requesting a connector data-ingest job."""

    connector_key: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ConnectorTestRequest(NonBlankRequestModel):
    """Request body for the test-connection probe endpoint."""

    reason: str = Field(min_length=1)


def current_connector_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyConnectorCredentialRepository:
    """FastAPI dependency providing a request-scoped credential repository."""
    return SqlAlchemyConnectorCredentialRepository(session)


@router.get("/credentials")
def list_connector_credentials(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[SqlAlchemyConnectorCredentialRepository, Depends(current_connector_repository)],
    limit: Annotated[int, Query(ge=1, le=MAX_CREDENTIAL_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    """List connector credentials the requesting user is permitted to manage."""
    connector_keys = _manageable_connector_keys(user)
    if connector_keys is not None and not connector_keys:
        _raise_missing_connector_permission(Permission.MANAGE_CONNECTORS)
    page = repository.list_credentials(
        limit=limit,
        offset=offset,
        connector_keys=connector_keys,
    )
    return {
        "items": [credential.to_api() for credential in page.items],
        "pagination": {
            "limit": page.limit,
            "offset": page.offset,
            "returned": len(page.items),
            "has_more": page.has_more,
        },
    }


@router.post("/credentials", status_code=status.HTTP_201_CREATED)
def create_connector_credential(
    payload: ConnectorCredentialCreateRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[SqlAlchemyConnectorCredentialRepository, Depends(current_connector_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    """Register a new connector credential reference for the given connector and account."""
    connector_scope = AccessScope.connector(payload.connector_key)
    _require_connector_permission(user, Permission.MANAGE_CONNECTORS, connector_scope)
    if not is_external_secret_ref(payload.encrypted_secret_ref):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Connector credentials must use an external encrypted secret reference",
        )
    try:
        credential = repository.create_credential(
            connector_key=payload.connector_key,
            account_id=payload.account_id,
            encrypted_secret_ref=payload.encrypted_secret_ref,
            actor_user_id=user.user_id,
        )
    except ConnectorCredentialValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except ConnectorCredentialConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Connector credential already exists") from exc

    record = _audit_connector_change(
        audit_sink=audit_sink,
        user=user,
        event_type=AuditEventType.CONNECTOR_SETTINGS_CHANGED,
        connector_key=payload.connector_key,
        account_id=payload.account_id,
        reason=payload.reason,
        details={"action": "credential_ref_created", "credential_id": credential.id},
    )
    return _with_audit_event(credential, record)


@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
def request_connector_job(
    payload: ConnectorJobRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    """Enqueue a connector run request and emit a CONNECTOR_JOB_RUN audit event."""
    connector_scope = AccessScope.connector(payload.connector_key)
    _require_connector_permission(user, Permission.RUN_CONNECTOR_JOBS, connector_scope)
    record = _audit_connector_change(
        audit_sink=audit_sink,
        user=user,
        event_type=AuditEventType.CONNECTOR_JOB_RUN,
        connector_key=payload.connector_key,
        account_id=payload.account_id,
        reason=payload.reason,
        details={"action": "job_request_recorded"},
    )
    return {
        "connector_key": payload.connector_key,
        "account_id": payload.account_id,
        "execution_status": "recorded_not_executed",
        "audit_event": audit_record_to_api(record),
    }


@router.post("/credentials/{connector_key}/{account_id:path}/test")
def test_connector_connection(
    connector_key: str,
    account_id: str,
    payload: ConnectorTestRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    session: Annotated[Session, Depends(current_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    """Probe credential OAuth health; every probe (including 404) is audited as CONNECTOR_TESTED."""
    # ============================================================================
    # Purpose: Probe the stored credential for (connector_key, account_id) by
    #   resolving its secret URI and performing an OAuth token refresh. No live
    #   data is fetched. Operators use this to verify that a registered credential
    #   is still valid before relying on it for a connector run.
    # Database/ORM: ApiConnectorCredentialORM (read only via resolve_connector_credentials).
    # Standards: Requires MANAGE_CONNECTORS@connector(connector_key). Every probe
    #   is audited (CONNECTOR_TESTED) including not-found; credential/OAuth failures
    #   return 200 with a machine-readable status field; not-found returns 404 with
    #   the audit already committed (JSONResponse, not HTTPException, so the session
    #   commits rather than rolls back).
    # Blast Radius: None (read-only; OAuth refresh touches Google but does not
    #   mutate stored state).
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py
    #     -> resolve_connector_credentials.
    #   - File: backend/ums_smart_revenue/connectors/google/errors.py -> error taxonomy.
    # ============================================================================
    connector_scope = AccessScope.connector(connector_key)
    _require_connector_permission(user, Permission.MANAGE_CONNECTORS, connector_scope)

    # FIX: Wrap UUID parse so a truthy but non-UUID tenant_id string (e.g. a slug)
    # in headers mode falls back to the bootstrap tenant rather than raising a
    # raw ValueError that would produce an unhandled 500.
    try:
        tenant_uuid = UUID(user.tenant_id) if user.tenant_id else UUID(UMS_TENANT_ID)
    except ValueError:
        tenant_uuid = UUID(UMS_TENANT_ID)

    conn_status: str = "ok"
    detail: str | None = None
    not_found = False

    try:
        resolve_connector_credentials(
            session=session,
            tenant_id=tenant_uuid,
            connector_key=connector_key,
            account_id=account_id,
        )
    except CredentialNotFoundError:
        # FIX: Audit the not-found probe before returning 404 so the audit trail
        # is complete. Use JSONResponse (not HTTPException) so the session commits
        # and the CONNECTOR_TESTED row is persisted despite the 404 status code.
        conn_status = "not_found"
        not_found = True
    except InactiveCredentialError as exc:
        conn_status = "inactive_credential"
        detail = str(exc)
    except OAuthRefreshError as exc:
        conn_status = "auth_failed"
        detail = str(exc)
    except GoogleConnectorError as exc:
        conn_status = "error"
        detail = str(exc)

    record = _audit_connector_change(
        audit_sink=audit_sink,
        user=user,
        event_type=AuditEventType.CONNECTOR_TESTED,
        connector_key=connector_key,
        account_id=account_id,
        reason=payload.reason,
        details={"status": conn_status},
    )

    if not_found:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "connector_key": connector_key,
                "account_id": account_id,
                "status": conn_status,
                "detail": "Connector credential not found",
                "audit_event": audit_record_to_api(record),
            },
        )

    return {
        "connector_key": connector_key,
        "account_id": account_id,
        "status": conn_status,
        "detail": detail,
        "audit_event": audit_record_to_api(record),
    }


def _require_connector_permission(user: UserPrincipal, permission: Permission, scope: AccessScope) -> None:
    """Raise 403 if the user lacks the given permission at the connector scope."""
    if not has_permission(user, permission, scope):
        _raise_missing_connector_permission(permission)


def _raise_missing_connector_permission(permission: Permission) -> None:
    """Raise 403 for a missing connector permission; always raises, never returns."""
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Missing permission: {permission.value}",
    )


def _manageable_connector_keys(user: UserPrincipal) -> frozenset[str] | None:
    """Return the connector keys the user can manage, or None for global-scope access."""
    if user.disabled:
        return frozenset()
    if has_permission(user, Permission.MANAGE_CONNECTORS, AccessScope.global_scope()):
        return None
    connector_keys: set[str] = set()
    for grant in user.direct_permissions:
        if (
            grant.active
            and grant.permission == Permission.MANAGE_CONNECTORS
            and grant.scope.type.value == "connector"
            and grant.scope.id
        ):
            connector_keys.add(grant.scope.id)
    for assignment in user.role_assignments:
        if (
            assignment.active
            and assignment.scope.type.value == "connector"
            and assignment.scope.id
            and has_permission(
                user,
                Permission.MANAGE_CONNECTORS,
                AccessScope.connector(assignment.scope.id),
            )
        ):
            connector_keys.add(assignment.scope.id)
    return frozenset(connector_keys)


def _audit_connector_change(
    *,
    audit_sink: AuditSink,
    user: UserPrincipal,
    event_type: AuditEventType,
    connector_key: str,
    account_id: str,
    reason: str,
    details: dict[str, object],
):
    """Write and return a CONNECTOR_* audit record via the request-scoped sink."""
    return record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=event_type,
        entity_type="api_connector",
        entity_id=f"{connector_key}:{account_id}",
        scope=AccessScope.connector(connector_key),
        reason=reason,
        details=details,
    )


def _with_audit_event(entry: ConnectorCredentialEntry, record) -> dict[str, object]:
    """Return the credential's API shape with an audit_event field appended."""
    response = entry.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response
