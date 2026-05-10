from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
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
    ConnectorCredentialEntry,
    ConnectorCredentialConflictError,
    ConnectorCredentialValidationError,
    SqlAlchemyConnectorCredentialRepository,
    is_external_secret_ref,
)


router = APIRouter(prefix="/connectors", tags=["connectors"])


class NonBlankRequestModel(BaseModel):
    @field_validator("*", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("must not be blank")
            return stripped
        return value


class ConnectorCredentialCreateRequest(NonBlankRequestModel):
    connector_key: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    encrypted_secret_ref: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ConnectorJobRequest(NonBlankRequestModel):
    connector_key: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


def current_connector_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyConnectorCredentialRepository:
    return SqlAlchemyConnectorCredentialRepository(session)


@router.get("/credentials")
def list_connector_credentials(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[SqlAlchemyConnectorCredentialRepository, Depends(current_connector_repository)],
) -> list[dict[str, object]]:
    _require_connector_permission(user, Permission.MANAGE_CONNECTORS, AccessScope.global_scope())
    return [credential.to_api() for credential in repository.list_credentials()]


@router.post("/credentials", status_code=status.HTTP_201_CREATED)
def create_connector_credential(
    payload: ConnectorCredentialCreateRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[SqlAlchemyConnectorCredentialRepository, Depends(current_connector_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
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


def _require_connector_permission(user: UserPrincipal, permission: Permission, scope: AccessScope) -> None:
    if not has_permission(user, permission, scope):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {permission.value}",
        )


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
    response = entry.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response
