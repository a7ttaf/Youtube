"""FastAPI route handlers for connector credential management and test-connection probing."""
from datetime import datetime
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
from ums_smart_revenue.connectors.runs.repository import (
    MAX_CONNECTOR_RUN_PAGE_SIZE,
    ConnectorRunValidationError,
    list_runs,
)
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
    repository: Annotated[
        SqlAlchemyConnectorCredentialRepository, Depends(current_connector_repository)
    ],
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


@router.get("/runs")
def list_connector_runs(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    session: Annotated[Session, Depends(current_db_session)],
    connector_key: str | None = None,
    account_id: str | None = None,
    cursor_started_at: datetime | None = None,
    cursor_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_CONNECTOR_RUN_PAGE_SIZE)] = 50,
) -> dict[str, object]:
    """Return a newest-first page of tenant-scoped connector run history."""
    # ========================================================================
    # Purpose: Surface read-only connector run history for the dashboard, with
    #   optional connector/account filters and a both-or-neither keyset cursor.
    #   Fail-closed behind VIEW_CONNECTOR_HEALTH at global scope; no audit
    #   emission (operational metadata read, mirrors the credential-list route).
    # Database/ORM: ConnectorRunORM via connectors.runs.repository.list_runs
    #   (read only).
    # Standards: Boundary permission gate; typed ConnectorRunValidationError ->
    #   HTTP 422; FastAPI Query bounds the limit at 1..MAX page size.
    # Blast Radius: Connector run read surface only. Finance/auth/audit/Neo4j
    #   untouched.
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/runs/repository.py ->
    #     list_runs.
    #   - File: backend/ums_smart_revenue/api/audit.py -> mirrored envelope.
    # ========================================================================
    _require_connector_health(user)

    # FIX: Mirror the test-connection route's tenant resolution so a truthy but
    # non-UUID tenant_id falls back to the bootstrap tenant instead of raising a
    # raw ValueError that would surface as an unhandled 500.
    try:
        tenant_uuid = UUID(user.tenant_id) if user.tenant_id else UUID(UMS_TENANT_ID)
    except ValueError:
        tenant_uuid = UUID(UMS_TENANT_ID)

    try:
        page = list_runs(
            session,
            tenant_id=tenant_uuid,
            connector_key=connector_key,
            account_id=account_id,
            cursor_started_at=cursor_started_at,
            cursor_id=cursor_id,
            limit=limit,
        )
    except ConnectorRunValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    items = [entry.to_api() for entry in page.items]
    return {
        "items": items,
        "pagination": {
            "limit": page.limit,
            "returned": len(items),
            "has_more": page.next_cursor is not None,
            "next_cursor": page.next_cursor,
        },
    }


@router.post("/credentials", status_code=status.HTTP_201_CREATED)
def create_connector_credential(
    payload: ConnectorCredentialCreateRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyConnectorCredentialRepository, Depends(current_connector_repository)
    ],
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
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ConnectorCredentialConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Connector credential already exists",
        ) from exc

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


@router.post("/credentials/{connector_key}/{account_id}/test")
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
    except InactiveCredentialError:
        # FIX: str(exc) embeds the raw DB credential UUID; safe canned message only.
        conn_status = "inactive_credential"
        detail = (
            "Credential is inactive or revoked;"
            " contact an administrator to re-register it."
        )
    except OAuthRefreshError:
        # FIX: str(exc) exposes the inner exception class name; safe canned message only.
        conn_status = "auth_failed"
        detail = (
            "OAuth token refresh failed;"
            " check that the credential secret is current."
        )
    except GoogleConnectorError:
        # FIX: str(exc) on GoogleConnectorError subclasses embeds full Google API URLs.
        conn_status = "error"
        detail = (
            "Connector probe returned an error;"
            " check connector configuration and account access."
        )

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


def _require_connector_permission(
    user: UserPrincipal, permission: Permission, scope: AccessScope
) -> None:
    """Raise 403 if the user lacks the given permission at the connector scope."""
    if not has_permission(user, permission, scope):
        _raise_missing_connector_permission(permission)


def _require_connector_health(user: UserPrincipal) -> None:
    """Raise 403 unless the user holds VIEW_CONNECTOR_HEALTH at global scope."""
    if not has_permission(
        user, Permission.VIEW_CONNECTOR_HEALTH, AccessScope.global_scope()
    ):
        _raise_missing_connector_permission(Permission.VIEW_CONNECTOR_HEALTH)


def _raise_missing_connector_permission(permission: Permission) -> None:
    """Raise 403 for a missing connector permission; always raises, never returns.

    :raises HTTPException: HTTP 403 with the permission name in the detail field.
    """
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
