# ============================================================================
# Purpose: Expose tenant-scoped export template management while leaving export
# job creation and artifact generation in the existing exports route.
# Database/ORM: ExportTemplateORM through SqlAlchemyExportTemplateRepository.
# Standards: Thin FastAPI routes, fail-closed permissions, typed validation
# errors, and audited writes with required operator reasons.
# Blast Radius: Export configuration and audit logs only.
# Connections:
#   - File: backend/ums_smart_revenue/reports/exports.py -> Template repository.
#   - File: backend/ums_smart_revenue/db/report_models.py -> ExportTemplateORM.
# ============================================================================
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_principal_from_headers,
)
from ums_smart_revenue.api.dependencies_audit import audit_record_to_api, current_audit_sink
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.reports.exports import (
    MAX_EXPORT_TEMPLATE_PAGE_SIZE,
    ExportTemplateEntry,
    ExportTemplateNotFoundError,
    ExportTemplateValidationError,
    SqlAlchemyExportTemplateRepository,
)

router = APIRouter(prefix="/export-templates", tags=["export-templates"])


class ExportTemplateCreateRequest(BaseModel):
    """Request body for creating an export template."""

    name: str = Field(min_length=1)
    export_type: str = Field(min_length=1)
    description: str | None = None
    layout_config: dict[str, object]
    reason: str = Field(min_length=1)

    @field_validator("name", "export_type", "reason", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        """Strip whitespace and reject blank strings for required fields."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("must not be blank")
            return stripped
        return value

    @field_validator("description", mode="before")
    @classmethod
    def strip_optional_string(cls, value):
        """Strip optional strings and treat blank input as null."""
        if value is None:
            return None
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("layout_config")
    @classmethod
    def require_layout_config_object(cls, value):
        """Require the layout config to be a JSON object, not an array or scalar."""
        if not isinstance(value, dict):
            raise ValueError("layout_config must be an object")
        return value


class ExportTemplateUpdateRequest(BaseModel):
    """Request body for updating an export template."""

    name: str | None = None
    description: str | None = None
    layout_config: dict[str, object] | None = None
    is_active: bool | None = None
    reason: str = Field(min_length=1)

    @field_validator("name", "reason", mode="before")
    @classmethod
    def strip_required_when_present(cls, value):
        """Strip whitespace and reject blank strings for provided required-like fields."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("must not be blank")
            return stripped
        return value

    @field_validator("description", mode="before")
    @classmethod
    def strip_optional_string(cls, value):
        """Strip optional strings and treat blank input as null."""
        if value is None:
            return None
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def require_template_update_field(self):
        """Reject no-op updates while allowing description to be explicitly cleared."""
        updated_fields = self.model_fields_set - {"reason"}
        if not updated_fields:
            raise ValueError("at least one template field must be supplied")
        if "name" in updated_fields and self.name is None:
            raise ValueError("name must not be null")
        if "layout_config" in updated_fields and self.layout_config is None:
            raise ValueError("layout_config must be an object")
        if "is_active" in updated_fields and self.is_active is None:
            raise ValueError("is_active must not be null")
        return self


# ============================================================================
# Purpose: Bind each request to the tenant-scoped export-template repository.
# Database/ORM: ExportTemplateORM through the request SQLAlchemy Session.
# Standards: Dependency-only wiring; business rules stay in reports/exports.py.
# Blast Radius: Export configuration request handling only.
# Connections:
#   - File: backend/ums_smart_revenue/api/dependencies.py -> Session provider.
#   - File: backend/ums_smart_revenue/reports/exports.py -> Repository class.
# ============================================================================
def current_export_template_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyExportTemplateRepository:
    """FastAPI dependency that returns a scoped export template repository."""
    return SqlAlchemyExportTemplateRepository(session)


# ============================================================================
# Purpose: Create an audited reusable export-template definition.
# Database/ORM: INSERT export_templates through repository; audit_logs write.
# Standards: Thin route, fail-closed permission check, typed validation errors.
# Blast Radius: Export configuration and audit only.
# Connections:
#   - File: backend/ums_smart_revenue/reports/exports.py -> Create contract.
#   - File: backend/ums_smart_revenue/auth/audit.py -> Audit event enum.
# ============================================================================
@router.post("", status_code=status.HTTP_201_CREATED)
def create_export_template(
    payload: ExportTemplateCreateRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyExportTemplateRepository,
        Depends(current_export_template_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    """Create an active export template after global template-management authorization."""
    _require_manage_export_templates(user, None)
    try:
        template = repository.create_template(
            name=payload.name,
            export_type=payload.export_type,
            description=payload.description,
            layout_config=payload.layout_config,
            actor_user_id=user.user_id,
        )
    except ExportTemplateValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return _template_response_with_audit(
        audit_sink=audit_sink,
        user=user,
        template=template,
        reason=payload.reason,
        action="created",
    )


# ============================================================================
# Purpose: List export templates available to global template managers.
# Database/ORM: SELECT export_templates through repository.
# Standards: Thin route with query bounds enforced by FastAPI and repository.
# Blast Radius: Export configuration reads only.
# Connections:
#   - File: backend/ums_smart_revenue/reports/exports.py -> Pagination bounds.
#   - File: Docs/12_BACKEND_API_SPEC.md -> API response contract.
# ============================================================================
@router.get("")
def list_export_templates(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyExportTemplateRepository,
        Depends(current_export_template_repository),
    ],
    export_type: Annotated[str | None, Query(min_length=1)] = None,
    include_inactive: bool = False,
    limit: Annotated[int, Query(ge=1, le=MAX_EXPORT_TEMPLATE_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    """Return active export templates by default for global template managers."""
    _require_manage_export_templates(user, None)
    try:
        page = repository.list_templates(
            export_type=export_type,
            include_inactive=include_inactive,
            limit=limit,
            offset=offset,
        )
    except ExportTemplateValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return {
        "items": [template.to_api() for template in page.items],
        "pagination": {
            "limit": page.limit,
            "offset": page.offset,
            "returned": len(page.items),
            "has_more": page.has_more,
        },
    }


# ============================================================================
# Purpose: Return a single export template for managers or scoped operators.
# Database/ORM: SELECT export_templates through repository.
# Standards: Thin route with template-scoped permission fallback.
# Blast Radius: Export configuration reads only.
# Connections:
#   - File: backend/ums_smart_revenue/auth/scopes.py -> export(template_id).
#   - File: backend/ums_smart_revenue/reports/exports.py -> Lookup contract.
# ============================================================================
@router.get("/{template_id}")
def get_export_template(
    template_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyExportTemplateRepository,
        Depends(current_export_template_repository),
    ],
) -> dict[str, object]:
    """Return one export template, including inactive templates for management history."""
    _require_manage_export_templates(user, template_id)
    try:
        return repository.get_template(template_id).to_api()
    except ExportTemplateNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ExportTemplateValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


# ============================================================================
# Purpose: Apply audited metadata/layout updates to an existing export template.
# Database/ORM: UPDATE export_templates through repository; audit_logs write.
# Standards: Reject null-only no-ops and translate typed repository errors.
# Blast Radius: Export configuration and audit only.
# Connections:
#   - File: backend/ums_smart_revenue/reports/exports.py -> Update contract.
#   - File: backend/ums_smart_revenue/auth/scopes.py -> export(template_id).
# ============================================================================
@router.patch("/{template_id}")
def update_export_template(
    template_id: str,
    payload: ExportTemplateUpdateRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyExportTemplateRepository,
        Depends(current_export_template_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    """Update export template metadata after template-scoped authorization."""
    _require_manage_export_templates(user, template_id)
    try:
        template = repository.update_template(
            template_id,
            name=payload.name,
            description=payload.description,
            description_provided="description" in payload.model_fields_set,
            layout_config=payload.layout_config,
            layout_config_provided="layout_config" in payload.model_fields_set,
            is_active=payload.is_active,
        )
    except ExportTemplateNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ExportTemplateValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return _template_response_with_audit(
        audit_sink=audit_sink,
        user=user,
        template=template,
        reason=payload.reason,
        action="updated",
    )


# ============================================================================
# Purpose: Soft-delete an export template without breaking historical jobs.
# Database/ORM: UPDATE export_templates.is_active through repository; audit log.
# Standards: Query reason normalization and template-scoped permission fallback.
# Blast Radius: Export configuration and audit only.
# Connections:
#   - File: backend/ums_smart_revenue/db/report_models.py -> Nullable FK.
#   - File: backend/ums_smart_revenue/reports/exports.py -> Deactivate contract.
# ============================================================================
@router.delete("/{template_id}")
def delete_export_template(
    template_id: str,
    reason: Annotated[str, Query(min_length=1)],
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyExportTemplateRepository,
        Depends(current_export_template_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    """Deactivate an export template without deleting historical job references."""
    _require_manage_export_templates(user, template_id)
    normalized_reason = _strip_required_query_string(reason, "reason")
    try:
        template = repository.deactivate_template(template_id)
    except ExportTemplateNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ExportTemplateValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return _template_response_with_audit(
        audit_sink=audit_sink,
        user=user,
        template=template,
        reason=normalized_reason,
        action="deactivated",
    )


# ============================================================================
# Purpose: Enforce global template management or a matching template scope.
# Database/ORM: None.
# Standards: Fail-closed permission check with safe HTTP 403 message.
# Blast Radius: Authorization for export-template management only.
# Connections:
#   - File: backend/ums_smart_revenue/auth/permissions.py -> Permission enum.
#   - File: backend/ums_smart_revenue/auth/policy.py -> Permission evaluator.
# ============================================================================
def _require_manage_export_templates(user: UserPrincipal, template_id: str | None) -> None:
    """Require global template management or a matching export-template scope."""
    permission = Permission.MANAGE_EXPORT_TEMPLATES
    if has_permission(user, permission, AccessScope.global_scope()):
        return
    if template_id is not None and has_permission(
        user, permission, AccessScope.export(template_id)
    ):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Missing permission: {permission.value}",
    )


# ============================================================================
# Purpose: Attach an audit event to successful export-template write responses.
# Database/ORM: audit_logs through AuditSink.
# Standards: Sensitive audit event, scoped entity id, and safe response payload.
# Blast Radius: Audit trail for export-template configuration changes.
# Connections:
#   - File: backend/ums_smart_revenue/auth/audit_service.py -> Audit writer.
#   - File: backend/ums_smart_revenue/api/channels.py -> Audit API serializer.
# ============================================================================
def _template_response_with_audit(
    *,
    audit_sink: AuditSink,
    user: UserPrincipal,
    template: ExportTemplateEntry,
    reason: str,
    action: str,
) -> dict[str, object]:
    """Return a template API payload with the write audit event included."""
    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.EXPORT_TEMPLATE_CHANGED,
        entity_type="export_template",
        entity_id=template.id,
        scope=AccessScope.export(template.id),
        reason=reason,
        permission_override=Permission.MANAGE_EXPORT_TEMPLATES,
        details={
            "action": action,
            "export_type": template.export_type,
            "is_active": template.is_active,
        },
    )
    response = template.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


def _strip_required_query_string(value: str, field_name: str) -> str:
    """Strip a required query string and reject blanks after whitespace trim."""
    stripped = value.strip()
    if not stripped:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field_name} must not be blank",
        )
    return stripped
