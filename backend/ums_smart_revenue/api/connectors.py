# ============================================================================
# Purpose: FastAPI route handlers for Google connector credential management,
# credential health surfacing, test-connection probing, and bounded in-process
# connector job submission. Keeps authorization, token-refresh, and audit writes
# behind typed helpers and fail-closed permission gates.
# Database/ORM: ApiConnectorCredentialORM plus connector run/audit reads/writes
# via SqlAlchemyConnectorCredentialRepository and the audit sink.
# Standards: Thin FastAPI routes, fail-closed MANAGE_CONNECTORS / VIEW_CONNECTOR_HEALTH
# permissions, canned safe error messages (no inner-exception leakage), and typed
# audit emission on credential probes.
# Blast Radius: Connector credential read/write surface, connector job dispatch,
# audit trail, and admin-facing health telemetry. Finance/auth-mutation and
# schema untouched.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/credentials.py ->
#     Credential resolution, health-state derivation, and listing.
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     ConnectorJobExecutor run_one (deferred to worker thread).
#   - File: backend/ums_smart_revenue/auth/policy.py ->
#     Permission gating and connector-scoped id resolution.
# ============================================================================
"""FastAPI route handlers for connector credential management and test-connection probing."""

from __future__ import annotations

# pylint: disable=too-many-arguments, too-many-positional-arguments
import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from ums_smart_revenue.api.channels import audit_record_to_api, current_audit_sink
from ums_smart_revenue.api.dependencies import current_db_session, current_principal_from_headers
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import (
    connector_health_connector_ids,
    has_permission,
)
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.config.settings import load_app_settings
from ums_smart_revenue.connectors.credentials import (
    MAX_CREDENTIAL_PAGE_SIZE,
    ConnectorCredentialConflictError,
    ConnectorCredentialEntry,
    ConnectorCredentialValidationError,
    SqlAlchemyConnectorCredentialRepository,
    derive_credential_health_state,
    is_external_secret_ref,
    live_credential_rejection_detail,
)
from ums_smart_revenue.connectors.google.errors import (
    CredentialNotFoundError,
    GoogleConnectorError,
    InactiveCredentialError,
    OAuthRefreshError,
)
from ums_smart_revenue.connectors.google.registry import known_keys
from ums_smart_revenue.connectors.keys import (
    credential_key_candidates,
    source_system_for_connector,
)
from ums_smart_revenue.connectors.runs.executor import ConnectorJobActor
from ums_smart_revenue.connectors.runs.orchestrator import resolve_connector_credentials
from ums_smart_revenue.connectors.runs.repository import (
    MAX_CONNECTOR_RUN_PAGE_SIZE,
    ConnectorRunValidationError,
    find_active_runs_for_scope,
    finish_run,
    list_runs,
    validate_report_month,
)
from ums_smart_revenue.db.security_models import UserORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

if TYPE_CHECKING:
    from ums_smart_revenue.connectors.runs.executor import (
        ConnectorJobExecutor,
        _SlotReservation,
    )

logger = logging.getLogger(__name__)

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
    report_month: str = Field(min_length=1)
    dry_run: bool = False
    reason: str = Field(min_length=1)


class ConnectorTestRequest(NonBlankRequestModel):
    """Request body for the test-connection probe endpoint."""

    reason: str = Field(min_length=1)


def current_connector_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyConnectorCredentialRepository:
    """FastAPI dependency providing a request-scoped credential repository."""
    return SqlAlchemyConnectorCredentialRepository(session)


# ============================================================================
# Purpose: Resolve a trusted tenant UUID from the authenticated principal
#          headers and fall back to the bootstrap tenant when parsing fails.
# Database/ORM: None.
# Standards: Fail closed on malformed tenant strings by using the bootstrap
#            tenant UUID rather than surfacing a raw ValueError at the route
#            boundary.
# Blast Radius: Authorization boundary only; no finance, audit, or graph
#               projection impact.
# Connections:
#   - File: backend/ums_smart_revenue/tenancy/constants.py -> UMS_TENANT_ID.
#   - File: backend/ums_smart_revenue/connectors/runs/repository.py -> tenant-scoped read.
# ============================================================================
def _resolve_tenant_uuid(user: UserPrincipal) -> UUID:
    """Resolve a trusted tenant UUID from the principal headers, falling back closed."""
    try:
        return UUID(user.tenant_id) if user.tenant_id else UUID(UMS_TENANT_ID)
    except ValueError:
        return UUID(UMS_TENANT_ID)


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
    #   Fail-closed behind VIEW_CONNECTOR_HEALTH at global or connector scope;
    #   connector-scoped callers are narrowed to their granted connector IDs.
    #   No audit emission (operational metadata read, mirrors the
    #   credential-list route).
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
    allowed_connector_ids = connector_health_connector_ids(user)
    _require_connector_health(allowed_connector_ids)

    # FIX: Mirror the test-connection route's tenant resolution so a truthy but
    # non-UUID tenant_id falls back to the bootstrap tenant instead of raising a
    # raw ValueError that would surface as an unhandled 500.
    # FIX: Resolve the connector filter to explicit values and pass them as
    # typed keyword arguments. The previous dict[str, object] + **unpack form
    # defeated mypy (TYP-050): the unpacked values are typed object, so none of
    # them match list_runs' concrete parameter types. Building the three
    # mutually-exclusive filter shapes first keeps the runtime contract
    # identical (connector_key vs connector_keys still cannot both be set)
    # while letting the static type-check verify the call.
    try:
        resolved_connector_key: str | None
        resolved_connector_keys: Iterable[str] | None
        if allowed_connector_ids is None:
            resolved_connector_key = connector_key
            resolved_connector_keys = None
        elif connector_key is not None:
            if connector_key not in allowed_connector_ids:
                _raise_missing_connector_permission(Permission.VIEW_CONNECTOR_HEALTH)
            resolved_connector_key = connector_key
            resolved_connector_keys = None
        else:
            resolved_connector_key = None
            resolved_connector_keys = allowed_connector_ids
        page = list_runs(
            session,
            tenant_id=_resolve_tenant_uuid(user),
            connector_key=resolved_connector_key,
            connector_keys=resolved_connector_keys,
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


@router.get("/credentials/health")
def list_connector_credential_health(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyConnectorCredentialRepository, Depends(current_connector_repository)
    ],
    limit: Annotated[int, Query(ge=1, le=MAX_CREDENTIAL_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    """Return credential refresh telemetry plus a derived health_state per credential."""
    # ========================================================================
    # Purpose: Surface read-only Google credential token health (the four already-
    #   persisted refresh-telemetry columns plus a derived coarse health_state)
    #   for the dashboard. Fail-closed behind VIEW_CONNECTOR_HEALTH at global or
    #   connector scope; connector-scoped callers are narrowed to their granted
    #   connector ids (no foreign-credential leak). Distinct from the
    #   MANAGE_CONNECTORS-gated GET /connectors/credentials list, which is
    #   unchanged. No audit emission (operational metadata read, mirrors the
    #   credential-list and run-history routes).
    # Database/ORM: ApiConnectorCredentialORM via
    #   SqlAlchemyConnectorCredentialRepository.for_tenant + list_credentials
    #   (read only).
    # Standards: Boundary permission gate via connector_health_connector_ids +
    #   _require_connector_health; typed ConnectorCredentialValidationError ->
    #   HTTP 422; health_state is additive (all raw to_api fields preserved);
    #   as_of is an explicit aware-UTC instant passed to the pure deriver;
    #   reads are bound to the resolved request tenant before listing.
    # Blast Radius: Connector credential read surface only. Finance/auth-
    #   mutation/audit/Neo4j untouched.
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/credentials.py ->
    #     derive_credential_health_state + list_credentials.
    #   - File: backend/ums_smart_revenue/auth/policy.py ->
    #     connector_health_connector_ids.
    # ========================================================================
    allowed_connector_ids = connector_health_connector_ids(user)
    _require_connector_health(allowed_connector_ids)
    tenant_repository = repository.for_tenant(_resolve_tenant_uuid(user))

    try:
        page = tenant_repository.list_credentials(
            limit=limit,
            offset=offset,
            connector_keys=allowed_connector_ids,
        )
    except ConnectorCredentialValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    as_of = datetime.now(UTC)
    credentials: list[dict[str, object]] = []
    for credential in page.items:
        record = credential.to_api()
        record["health_state"] = derive_credential_health_state(credential, as_of=as_of)
        credentials.append(record)
    return {
        "credentials": credentials,
        "pagination": {
            "limit": page.limit,
            "offset": page.offset,
            "returned": len(credentials),
            "has_more": page.has_more,
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


def _require_connector_job_permission(user: UserPrincipal, connector_key: str) -> None:
    """Authorize connector-job submission against the submitted key and aliases."""
    try:
        candidate_keys = credential_key_candidates(connector_key)
    except ValueError:
        candidate_keys = (connector_key,)
    alias_scopes = [AccessScope.connector(key) for key in candidate_keys]
    if any(has_permission(user, Permission.RUN_CONNECTOR_JOBS, scope) for scope in alias_scopes):
        return
    _raise_missing_connector_permission(Permission.RUN_CONNECTOR_JOBS)


# ============================================================================
# Purpose: Keep POST /connectors/jobs thin by grouping local preflight decisions
#   while preserving the route's rejection order and audit responses.
# Database/ORM: ApiConnectorCredentialORM reads through repository methods only.
# Standards: no direct commits; typed JSONResponse rejections keep audit rows.
# Blast Radius: Authorization, connector-job audit, and executor reservation.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py -> reservation.
#   - File: tests/api/test_connectors_api.py -> route rejection and submit matrix.
# ============================================================================
def _canonicalize_connector_job_payload(
    *,
    audit_sink: AuditSink,
    user: UserPrincipal,
    payload: ConnectorJobRequest,
) -> ConnectorJobRequest | JSONResponse:
    """Reject unknown connector keys or return the canonical source-system key."""
    if payload.connector_key not in known_keys():
        return _reject_connector_job(
            audit_sink=audit_sink,
            user=user,
            payload=payload,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            rejection="unknown_connector",
            detail="Unknown connector key",
        )
    return payload.model_copy(
        update={"connector_key": source_system_for_connector(payload.connector_key)}
    )


def _connector_job_preflight_rejection(
    *,
    audit_sink: AuditSink,
    user: UserPrincipal,
    payload: ConnectorJobRequest,
    executor: ConnectorJobExecutor | None,
    service_actor_configured: bool,
) -> JSONResponse | None:
    """Return the audited preflight rejection for disabled executor/config/month."""
    if executor is None:
        return _reject_connector_job(
            audit_sink=audit_sink,
            user=user,
            payload=payload,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            rejection="executor_disabled",
            detail="Connector job executor is disabled",
        )
    if not payload.dry_run and not service_actor_configured:
        return _reject_connector_job(
            audit_sink=audit_sink,
            user=user,
            payload=payload,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            rejection="service_principal_unavailable",
            detail=(
                "Connector service principal is not configured;"
                " set UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID"
            ),
        )
    try:
        validate_report_month(payload.report_month)
    except ConnectorRunValidationError:
        return _reject_connector_job(
            audit_sink=audit_sink,
            user=user,
            payload=payload,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            rejection="invalid_month",
            detail="report_month must use YYYY-MM",
        )
    return None


def _find_connector_job_credential(
    *,
    repository: SqlAlchemyConnectorCredentialRepository,
    payload: ConnectorJobRequest,
) -> ConnectorCredentialEntry | None:
    """Resolve a credential using the same alias-aware keys as the worker."""
    for candidate_key in credential_key_candidates(payload.connector_key):
        credential = repository.get_credential(
            connector_key=candidate_key,
            account_id=payload.account_id,
        )
        if credential is not None:
            return credential
    return None


def _connector_job_credential_rejection(
    *,
    audit_sink: AuditSink,
    user: UserPrincipal,
    payload: ConnectorJobRequest,
    credential: ConnectorCredentialEntry | None,
) -> JSONResponse | None:
    """Return the audited credential rejection, if credential state blocks submit."""
    if credential is None:
        return _reject_connector_job(
            audit_sink=audit_sink,
            user=user,
            payload=payload,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            rejection="credential_not_found",
            detail="Connector credential not found",
        )
    if credential.status != "active":
        return _reject_connector_job(
            audit_sink=audit_sink,
            user=user,
            payload=payload,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            rejection="credential_inactive",
            detail="Connector credential is not active",
        )
    if not payload.dry_run:
        rejection_detail = live_credential_rejection_detail(credential, as_of=datetime.now(UTC))
        if rejection_detail is not None:
            return _reject_connector_job(
                audit_sink=audit_sink,
                user=user,
                payload=payload,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                rejection="credential_smoke_required",
                detail=rejection_detail,
            )
    return None


def _reserve_connector_job_or_reject(
    *,
    audit_sink: AuditSink,
    user: UserPrincipal,
    session: Session,
    executor: ConnectorJobExecutor,
    tenant_id: UUID,
    payload: ConnectorJobRequest,
) -> _SlotReservation | JSONResponse:
    """Reserve the executor slot or return the audited duplicate rejection."""
    triggered_by = _resolve_triggered_by_user_id(session, user, tenant_id)
    actor_identity = ConnectorJobActor(user_id=user.user_id, email=user.email)
    reservation = executor.submit_if_absent(
        tenant_id=tenant_id,
        connector_key=payload.connector_key,
        account_id=payload.account_id,
        report_month=payload.report_month,
        dry_run=payload.dry_run,
        triggered_by_user_id=triggered_by,
        actor_identity=actor_identity,
    )
    if reservation is not None:
        return reservation
    return _reject_connector_job(
        audit_sink=audit_sink,
        user=user,
        payload=payload,
        status_code=status.HTTP_409_CONFLICT,
        rejection="duplicate_in_flight",
        detail="A connector job for this scope is already in flight",
    )


def _submitted_connector_job_response(
    *,
    audit_sink: AuditSink,
    user: UserPrincipal,
    payload: ConnectorJobRequest,
    superseded_run_id: str | None,
) -> dict[str, object]:
    """Build the successful route response and matching audit details."""
    details: dict[str, object] = {
        "action": "job_submitted",
        "report_month": payload.report_month,
        "dry_run": payload.dry_run,
    }
    if superseded_run_id is not None:
        details["superseded_run_id"] = superseded_run_id
    record = _audit_connector_change(
        audit_sink=audit_sink,
        user=user,
        event_type=AuditEventType.CONNECTOR_JOB_RUN,
        connector_key=payload.connector_key,
        account_id=payload.account_id,
        reason=payload.reason,
        details=details,
    )
    return {
        "connector_key": payload.connector_key,
        "account_id": payload.account_id,
        "report_month": payload.report_month,
        "dry_run": payload.dry_run,
        "execution_status": "submitted",
        "audit_event": audit_record_to_api(record),
    }


# ============================================================================
# Purpose: Submit a real Google ingest pull to the module-owned
#   ConnectorJobExecutor and return 202 "submitted" immediately. Cheap
#   in-request validation only (no live token refresh, no run_one inline). Control
#   flow order is load-bearing and fail-closed: authz 403 -> 503 disabled ->
#   503 service_principal_unavailable -> 422 unknown-key -> 422 bad-month ->
#   422 missing/inactive cred -> 409 dup / orphan-supersede -> 202 submit.
#   Exactly ONE route-owned audit row (job_submitted / job_rejected);
#   run_one emits its own STARTED/FINISHED edges later on the worker session.
#
#   Submission is the reserve-then-activate pattern (see
#   ``ConnectorJobExecutor.submit_if_absent`` + ``activate``): the route
#   holds a registry slot for the scope BEFORE the audit row commits and
#   enqueues the worker only AFTER the audit row commits (via a
#   ``session.after_commit`` hook). If the audit commit fails the worker
#   is never enqueued, so the route's 202 is the authoritative
#   acceptance record.
#
# Database/ORM: ApiConnectorCredentialORM + ConnectorRunORM (reads via
#   get_credential / find_active_runs_for_scope; finish_run rewrites a
#   superseded orphan), AuditLogORM (one row), UserORM (existence probe).
# Standards: RUN_CONNECTOR_JOBS@connector gate first (HTTPException 403, no
#   audit). All non-2xx-but-audited responses use JSONResponse (not
#   HTTPException) so the request session commits the audit row (mirrors the
#   test-route 404 pattern). Audit details carry only machine tokens, never
#   str(exc). triggered_by_user_id degrades to None unless a users row exists.
# Blast Radius: Authorization (unchanged gate), audit (additive details
#   actions), connector run lifecycle (orphan supersede via finish_run on the
#   tenant lane). No finance math change; no graph projection impact.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py ->
#     submit_if_absent + activate + cancel_reservation.
#   - File: backend/ums_smart_revenue/app.py -> app.state.connector_job_executor.
#   - File: Docs/12_BACKEND_API_SPEC.md -> POST /connectors/jobs contract.
# ============================================================================
@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED, response_model=None)
def request_connector_job(
    payload: ConnectorJobRequest,
    request: Request,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    session: Annotated[Session, Depends(current_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object] | JSONResponse:
    """Validate cheaply, reserve an executor slot, write the audit, and return 202 submitted."""
    _require_connector_job_permission(user, payload.connector_key)

    # Canonicalize so credential lookup, the executor registry key, and the
    # downstream orchestrator all agree on the source-system underscore key.
    payload_or_rejection = _canonicalize_connector_job_payload(
        audit_sink=audit_sink,
        user=user,
        payload=payload,
    )
    if isinstance(payload_or_rejection, JSONResponse):
        return payload_or_rejection
    payload = payload_or_rejection

    settings = load_app_settings()
    tenant_id = _resolve_tenant_uuid(user)
    executor = getattr(request.app.state, "connector_job_executor", None)
    preflight_rejection = _connector_job_preflight_rejection(
        audit_sink=audit_sink,
        user=user,
        payload=payload,
        executor=executor,
        service_actor_configured=settings.google_connector_service_actor_id is not None,
    )
    if preflight_rejection is not None:
        return preflight_rejection
    assert executor is not None

    repository = SqlAlchemyConnectorCredentialRepository(session, tenant_id=tenant_id)
    credential = _find_connector_job_credential(
        repository=repository,
        payload=payload,
    )
    credential_rejection = _connector_job_credential_rejection(
        audit_sink=audit_sink,
        user=user,
        payload=payload,
        credential=credential,
    )
    if credential_rejection is not None:
        return credential_rejection

    # FIX: atomic dedup. The previous has_active_job() -> submit() pair was a
    # check-then-act race: two concurrent requests for the same scope could
    # both pass the check before either reached submit(). submit_if_absent
    # holds the executor's registry lock across the membership check and the
    # insert, so the second concurrent caller sees the in-flight slot and
    # returns None -> 409.
    reservation_or_rejection = _reserve_connector_job_or_reject(
        audit_sink=audit_sink,
        user=user,
        session=session,
        executor=executor,
        tenant_id=tenant_id,
        payload=payload,
    )
    if isinstance(reservation_or_rejection, JSONResponse):
        return reservation_or_rejection
    reservation = reservation_or_rejection

    # FIX: register the after_rollback hook IMMEDIATELY after submit_if_absent
    # so a DB error in find_active_runs_for_scope() / finish_run() / the route
    # audit write never leaves the reservation wedged. The companion
    # after_commit hook is attached after the audit row is written (see
    # _enqueue_after_commit below); splitting the two attachments means a
    # pre-audit exception still cleans up the slot.
    _attach_after_rollback_hook(session=session, executor=executor, reservation=reservation)

    superseded_run_id = _supersede_or_block_running_runs(
        session,
        tenant_id=tenant_id,
        payload=payload,
        stale_hours=settings.connector_job_stale_running_hours,
        audit_sink=audit_sink,
        user=user,
    )
    if isinstance(superseded_run_id, _DuplicateRunBlock):
        executor.cancel_reservation(reservation)
        return _reject_connector_job(
            audit_sink=audit_sink,
            user=user,
            payload=payload,
            status_code=status.HTTP_409_CONFLICT,
            rejection="duplicate_in_flight",
            detail="A connector job for this scope is already in flight",
        )

    response = _submitted_connector_job_response(
        audit_sink=audit_sink,
        user=user,
        payload=payload,
        superseded_run_id=superseded_run_id,
    )

    # FIX: defer the worker enqueue to AFTER the request session commits the
    # audit row. The previous design called executor.submit() inline; if the
    # session.commit() at end-of-request failed during dependency cleanup,
    # the worker had already mutated connector_runs / raw_report_files while
    # the accepted-job audit rolled back -- leaving a successful 202 in the
    # client with no durable record. The after_commit hook closes that
    # window: a failed commit never invokes the worker, and the reservation
    # is dropped on rollback so the executor slot is reclaimed.
    _enqueue_after_commit(
        session=session,
        executor=executor,
        reservation=reservation,
    )

    return response


@router.post("/credentials/{connector_key}/{account_id}/test", response_model=None)
def test_connector_connection(
    connector_key: str,
    account_id: str,
    payload: ConnectorTestRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    session: Annotated[Session, Depends(current_db_session)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object] | JSONResponse:
    """Probe Google connector credential token health; every probe is audited."""
    # ============================================================================
    # Purpose: Probe the stored credential for (connector_key, account_id) by
    #   resolving its secret URI and performing an official Google token refresh. No live
    #   data is fetched. Operators use this to verify that a registered credential
    #   is still valid before relying on it for a connector run.
    # Database/ORM: ApiConnectorCredentialORM (read only via resolve_connector_credentials).
    # Standards: Requires MANAGE_CONNECTORS@connector(connector_key). Every probe
    #   is audited (CONNECTOR_TESTED) including not-found; credential/token failures
    #   return 200 with a machine-readable status field; not-found returns 404 with
    #   the audit already committed (JSONResponse, not HTTPException, so the session
    #   commits rather than rolls back).
    # Blast Radius: None (read-only; token refresh touches Google but does not
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
    conn_status: str = "ok"
    detail: str | None = None
    not_found = False

    try:
        resolve_connector_credentials(
            session=session,
            tenant_id=_resolve_tenant_uuid(user),
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
        detail = "Credential is inactive or revoked; contact an administrator to re-register it."
    except OAuthRefreshError:
        # FIX: str(exc) exposes the inner exception class name; safe canned message only.
        conn_status = "auth_failed"
        detail = (
            "Google credential token refresh failed; check that the credential secret is current."
        )
    except GoogleConnectorError:
        # FIX: str(exc) on GoogleConnectorError subclasses embeds full Google API URLs.
        conn_status = "error"
        detail = (
            "Connector probe returned an error; check connector configuration and account access."
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


def _require_connector_health(
    allowed_connector_ids: frozenset[str] | None = None,
) -> None:
    """Raise 403 unless the user holds VIEW_CONNECTOR_HEALTH at any allowed scope.

    Raises:
        HTTPException: If the caller has neither global nor connector-scoped
            VIEW_CONNECTOR_HEALTH access.
    """
    if allowed_connector_ids is None:
        return
    if allowed_connector_ids:
        return
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


class _DuplicateRunBlock:
    """Sentinel: a fresh RUNNING row exists for the scope -> reject as duplicate."""


def _as_aware_utc(value: datetime) -> datetime:
    """Treat a tz-naive started_at (SQLite read-back) as UTC for safe comparison.

    Postgres returns tz-aware ``DateTime(timezone=True)`` values; the SQLite test
    tier strips the tzinfo on read-back. Normalizing a naive value to UTC keeps
    the stale-threshold comparison valid on both backends without changing the
    stored value.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# ============================================================================
# Purpose: Secondary cross-process duplicate guard. Inspect RUNNING
#   connector_runs for the exact scope; the youngest younger than stale_hours
#   blocks (returns _DuplicateRunBlock); an older row is an orphan from a dead
#   process -> rewrite it FAILED via finish_run (tenant-lane; connector_runs is
#   tenant-writable) and return its id so the route records superseded_run_id.
# Database/ORM: ConnectorRunORM (read via find_active_runs_for_scope; FAILED
#   rewrite via finish_run, which requires RUNNING + flush-only).
# Standards: TOCTOU is code-level only (the index is non-unique); accepted at
#   max_workers=1; partial-unique-index deferred (documented in the spec).
# Blast Radius: Connector run lifecycle only. No finance, audit (the route
#   owns its row), or graph projection impact.
# Connections:
#   - Function: find_active_runs_for_scope -> the RUNNING reader.
#   - Function: finish_run -> the RUNNING->FAILED transition.
# ============================================================================
def _supersede_or_block_running_runs(
    session: Session,
    *,
    tenant_id: UUID,
    payload: ConnectorJobRequest,
    stale_hours: int,
    audit_sink: AuditSink,
    user: UserPrincipal,
) -> str | _DuplicateRunBlock | None:
    """Block on a fresh RUNNING run; supersede a stale one and return its id.

    FIX: when a stale RUNNING row is rewritten to FAILED, emit a matching
    ``CONNECTOR_JOB_RUN`` terminal audit edge with
    ``action=run_superseded`` so the stale run's lifecycle is complete
    (STARTED -> run_superseded) in the operator's audit log. Previously
    the database status changed but only the new job's ``job_submitted``
    row was written, so operators inspecting the stale run's lifecycle
    would see STARTED with no matching terminal edge even though the
    connector_runs row was FAILED. Attributed to the submitting user
    (the one who triggered the supersede) via the route-owned audit
    sink -- not the connector service principal, which may not be
    available in every environment.
    """
    running = find_active_runs_for_scope(
        session,
        tenant_id=tenant_id,
        connector_key=payload.connector_key,
        account_id=payload.account_id,
        report_month=payload.report_month,
    )
    if not running:
        return None
    threshold = datetime.now(UTC) - timedelta(hours=stale_hours)
    youngest = running[0]
    if _as_aware_utc(youngest.started_at) >= threshold:
        return _DuplicateRunBlock()
    superseded: str | None = None
    for entry in running:
        if _as_aware_utc(entry.started_at) < threshold:
            finish_run(
                session,
                tenant_id=tenant_id,
                connector_run_id=UUID(entry.id),
                status="FAILED",
                counts=dict(entry.counts),
                error_summary="orphaned RUNNING run superseded by new job",
            )
            # Terminal audit edge: run was STARTED at some point (the row
            # was RUNNING before finish_run), and is now FAILED because a
            # new job superseded it. Attribute to the operator who
            # triggered the supersede.
            _audit_connector_change(
                audit_sink=audit_sink,
                user=user,
                event_type=AuditEventType.CONNECTOR_JOB_RUN,
                connector_key=payload.connector_key,
                account_id=payload.account_id,
                reason=(f"orphaned RUNNING run {entry.id} superseded by new job"),
                details={
                    "action": "run_superseded",
                    "run_id": entry.id,
                    "report_month": payload.report_month,
                    "lifecycle": "FINISHED",
                    "status": "FAILED",
                    "error_summary_present": True,
                },
            )
            superseded = entry.id
    return superseded


def _resolve_triggered_by_user_id(
    session: Session, user: UserPrincipal, tenant_id: UUID
) -> UUID | None:
    """Return the principal UUID only if it is a real users row for the tenant."""
    try:
        candidate = UUID(user.user_id)
    except (ValueError, TypeError):
        return None
    exists = session.scalar(
        select(UserORM.id).where(UserORM.id == candidate, UserORM.tenant_id == tenant_id)
    )
    return candidate if exists is not None else None


# ============================================================================
# Purpose: Write the single route-owned CONNECTOR_JOB_RUN job_rejected audit
#   row, then return a JSONResponse with the given status so the request
#   session COMMITS the audit row (HTTPException would roll it back). Audit
#   details carry only machine tokens.
# Database/ORM: AuditLogORM (one row via the request-scoped sink).
# Standards: JSONResponse-commits-the-audit pattern (mirrors the test-route
#   404 at connectors.py:370-380). No str(exc) interpolation.
# Blast Radius: Audit (additive job_rejected action). No finance, auth, or
#   graph projection impact.
# Connections:
#   - File: backend/ums_smart_revenue/api/connectors.py -> request_connector_job.
# ============================================================================
def _reject_connector_job(
    *,
    audit_sink: AuditSink,
    user: UserPrincipal,
    payload: ConnectorJobRequest,
    status_code: int,
    rejection: str,
    detail: str,
) -> JSONResponse:
    """Audit a rejected job submission and return a committing JSONResponse."""
    _audit_connector_change(
        audit_sink=audit_sink,
        user=user,
        event_type=AuditEventType.CONNECTOR_JOB_RUN,
        connector_key=payload.connector_key,
        account_id=payload.account_id,
        reason=payload.reason,
        details={
            "action": "job_rejected",
            "rejection": rejection,
            "report_month": payload.report_month,
        },
    )
    return JSONResponse(status_code=status_code, content={"detail": detail})


# ============================================================================
# Purpose: Defer the executor's worker enqueue to AFTER the request session
#   commits the route-owned job_submitted audit row. This closes the window
#   where a failed commit (during dependency cleanup) would leave a worker
#   mutating connector run/source tables while the accepted-job audit rolled
#   back and the client may see a failed request. The companion
#   after_rollback hook drops the reservation so a failed commit never
#   wedges the executor's registry on a slot that was never activated.
#
#   Hooks are attached with a one-shot guard (a flag on ``session.info``)
#   so a request that has multiple commit/rollback cycles (e.g. inner
#   savepoints committing) does not double-enqueue the worker.
#
# Database/ORM: None directly; the session hook is the SQLAlchemy
#               ``after_commit`` / ``after_rollback`` event API.
# Standards: No bare except; hook errors are logged but never raised into
#            the request lifecycle (the audit row is already committed).
# Blast Radius: Connector run lifecycle. No finance, audit, or graph
#               projection impact.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py -> activate
#     + cancel_reservation.
#   - File: backend/ums_smart_revenue/api/connectors.py ->
#     request_connector_job attaches the hooks.
# ============================================================================
_AFTER_COMMIT_FLAG_KEY = object()
_AFTER_ROLLBACK_FLAG_KEY = object()


def _attach_after_rollback_hook(
    *,
    session: Session,
    executor: ConnectorJobExecutor,
    reservation: _SlotReservation,
) -> None:
    """Attach the after_rollback hook immediately after submit_if_absent.

    The hook is the safety net for any exception between
    ``submit_if_absent`` and ``_enqueue_after_commit`` (the DB-level
    supersede check, the route-owned audit write, etc.). Without it a
    raised exception would leave the reservation wedged in the executor
    registry and block future requests for the same scope. Idempotent
    via a one-shot flag on ``session.info``.
    """
    if not session.info.get(_AFTER_ROLLBACK_FLAG_KEY):
        event.listen(session, "after_rollback", _make_after_rollback_handler(executor, reservation))
        session.info[_AFTER_ROLLBACK_FLAG_KEY] = True


def _enqueue_after_commit(
    *,
    session: Session,
    executor: ConnectorJobExecutor,
    reservation: _SlotReservation,
) -> None:
    """Attach a one-shot after_commit hook to the request session.

    The companion after_rollback hook is attached right after
    ``submit_if_absent`` (see :func:`_attach_after_rollback_hook`) so a
    pre-audit exception still cleans up the slot. This function only
    handles the activate-on-commit side. Idempotent via a one-shot flag
    on ``session.info``.
    """
    if not session.info.get(_AFTER_COMMIT_FLAG_KEY):
        event.listen(session, "after_commit", _make_after_commit_handler(executor, reservation))
        session.info[_AFTER_COMMIT_FLAG_KEY] = True


def _make_after_commit_handler(executor: ConnectorJobExecutor, reservation: _SlotReservation):
    """Return a hook that activates the reservation after the session commits.

    If activation fails (e.g. the ThreadPoolExecutor rejects new work
    while the app is shutting down), the hook MUST persist a
    ``job_failed_before_start`` audit row on a fresh session so the
    committed 202 has a matching failure row in the operator's audit
    log; otherwise the accepted job disappears from the audited run
    lifecycle. The reservation is also dropped so a future request for
    the same scope is not blocked by a stale slot.
    """

    def _after_commit(_session: Session) -> None:
        try:
            executor.activate(reservation)
        except Exception as exc:  # noqa: BLE001 — best-effort, never raise
            executor.cancel_reservation(reservation)
            logger.exception("Failed to activate connector job reservation after commit")
            # Persist a job_failed_before_start audit row so the accepted
            # 202 has a matching failure row. The original request session
            # is already committed/closed, so this opens a fresh session
            # via the executor's own Bucket-A helper. The audit is
            # best-effort: any error here is logged and never raised
            # into the request lifecycle.
            try:
                executor.audit_failed_before_start(
                    tenant_id=reservation.tenant_id,
                    connector_key=reservation.connector_key,
                    account_id=reservation.account_id,
                    report_month=reservation.report_month,
                    error_class=type(exc).__name__,
                    actor_identity=reservation.actor_identity,
                )
            except Exception:  # noqa: BLE001 — best-effort audit
                logger.exception("Failed to persist activation-failure audit for reservation")

    return _after_commit


def _make_after_rollback_handler(executor: ConnectorJobExecutor, reservation: _SlotReservation):
    """Return a hook that drops the reservation when the session rolls back."""

    def _after_rollback(_session: Session) -> None:
        executor.cancel_reservation(reservation)

    return _after_rollback
