"""Connector audit emitters and the service principal that owns them.

The B2.6 orchestrator (wired in T37) calls these emitters at well-defined
lifecycle edges of one ``run_one`` invocation:

* ``emit_run_started``  / ``emit_run_finished``  -> CONNECTOR_JOB_RUN with
  ``details["lifecycle"]`` set to STARTED or FINISHED.
* ``emit_raw_file_downloaded`` / ``emit_raw_file_parsed`` /
  ``emit_raw_file_failed`` -> REPORT_IMPORTED with
  ``details["lifecycle"]`` set to DOWNLOADED, PARSED, or FAILED.

No new ``AuditEventType`` and no new ``Permission`` are introduced; the
``lifecycle`` key in ``details`` is the discriminator that distinguishes
the lifecycle position within the existing event types. The connector
audit volume sits alongside the existing audit log and is queryable with
the same filters operators already use for other connector events.

The service principal is a tenant-pinned ``UserPrincipal`` with
``Permission.RUN_CONNECTOR_JOBS`` so the audit log shows "this service
identity ran a connector job inside tenant X". The principal does NOT
have to be a real ``users`` row -- ``SqlAlchemyAuditSink.append``
gracefully stashes unknown actor UUIDs in ``details["actor_user_id"]``
rather than failing.

Dry-run skips ``emit_run_started`` entirely; the dry-run path also never
calls ``start_run`` or ``finish_run`` so there is no FINISHED edge either.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.config.settings import (
    GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV,
    GOOGLE_CONNECTOR_SERVICE_ACTOR_PLACEHOLDER_ID,
    load_app_settings,
)

__all__ = [
    "build_connector_service_principal",
    "emit_raw_file_downloaded",
    "emit_raw_file_failed",
    "emit_raw_file_parsed",
    "emit_run_finished",
    "emit_run_started",
]

_SERVICE_ACCOUNT_EMAIL = "google-connectors@service.ums.local"


# ============================================================================
# Purpose: Build the tenant-pinned service ``UserPrincipal`` that owns every
#          connector-emitted audit row for a single run. Identity comes from
#          UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID so audit consumers can group
#          connector traffic by a stable actor across deployments.
# Database/ORM: None. The principal feeds ``record_audit_event`` which writes
#               through ``SqlAlchemyAuditSink``; the actor id is NOT required
#               to be a real ``users.id`` -- the sink stashes unknown actor
#               UUIDs in ``details["actor_user_id"]`` and proceeds.
# Standards: Fail closed -- missing env OR the well-known .env.example
#            placeholder raises ValueError so the orchestrator cannot run
#            with an anonymized or template-published service principal.
#            Note:
#            ``AppSettings.google_connector_service_actor_id`` is lazy (None
#            when env unset, see config/settings.py); the fail-closed
#            boundary lives here at first emit, not at app boot, so
#            non-connector workloads can still load settings without this
#            env set. The principal is a frozen dataclass; immutability
#            mirrors the rest of the authorization layer.
# Blast Radius: Audit actor identity for connector runs. No effect on finance
#               numbers, scope checks (the orchestrator already gated the
#               request), or the Neo4j projection.
# Connections:
#   - File: backend/ums_smart_revenue/config/settings.py ->
#     load_app_settings() supplies google_connector_service_actor_id.
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py
#     (T37) -> calls this builder once per run_one invocation.
# ============================================================================
def build_connector_service_principal(*, tenant_id: UUID) -> UserPrincipal:
    """Return the service ``UserPrincipal`` for one connector run's audit emissions.

    Raises:
        ValueError: ``UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID`` is unset, or is
            set to the well-known ``.env.example`` placeholder — both fail
            closed here rather than mis-attributing audit rows.
    """
    settings = load_app_settings()
    actor_id = settings.google_connector_service_actor_id
    if actor_id is None:
        raise ValueError(
            f"{GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV} must be set to a UUID "
            "before connector audit emitters can build a service principal"
        )
    # FIX: the well-known placeholder UUID ships UNCOMMENTED in the tracked
    # .env.example, so a `cp .env.example .env` deployment reaches here with
    # that exact value. Accepting it attributed real audit rows to a UUID
    # published in a public template — worse than no connector runs at all.
    # Rejected here (use time), not at settings load, so non-connector
    # workloads keep the documented lazy-boot contract.
    if actor_id == GOOGLE_CONNECTOR_SERVICE_ACTOR_PLACEHOLDER_ID:
        raise ValueError(
            f"{GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV} is set to the well-known "
            ".env.example placeholder "
            f"{GOOGLE_CONNECTOR_SERVICE_ACTOR_PLACEHOLDER_ID}; provision a real "
            "service actor UUID before connector audit emitters can build a "
            "service principal"
        )
    return UserPrincipal(
        user_id=actor_id,
        email=_SERVICE_ACCOUNT_EMAIL,
        direct_permissions=(
            PermissionGrant(
                permission=Permission.RUN_CONNECTOR_JOBS,
                scope=AccessScope.global_scope(),
                active=True,
            ),
        ),
        is_service_account=True,
        disabled=False,
        tenant_id=str(tenant_id),
    )


# ============================================================================
# Purpose: Record one ``CONNECTOR_JOB_RUN`` row marking the lifecycle edge
#          at which the orchestrator just started a live run.
# Database/ORM: AuditLogORM (via the sink wired by T37).
# Standards: Dry-run early-returns -- no audit row, no side effects -- to
#            mirror the orchestrator's "dry-run writes nothing" contract.
# Blast Radius: Audit log only. No finance, scope, or graph projection
#               impact; the connector is already executing under the
#               operator's earlier RUN_CONNECTOR_JOBS check (api/connectors.py).
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py
#     -> _run_live calls this immediately after start_run + commit.
# ============================================================================
def emit_run_started(
    *,
    sink: AuditSink,
    actor: UserPrincipal,
    run: Any,
    dry_run: bool,
) -> None:
    """Emit one CONNECTOR_JOB_RUN audit row with lifecycle=STARTED."""
    if dry_run:
        return
    record_audit_event(
        sink=sink,
        actor=actor,
        event_type=AuditEventType.CONNECTOR_JOB_RUN,
        entity_type="connector_run",
        entity_id=str(run.id),
        scope=AccessScope.connector(run.connector_key),
        details={
            "lifecycle": "STARTED",
            "dry_run": False,
            "run_id": str(run.id),
            "connector_key": run.connector_key,
            "account_id": run.account_id,
            "report_month": run.report_month,
        },
    )


# ============================================================================
# Purpose: Record one ``CONNECTOR_JOB_RUN`` row marking the terminal edge
#          of a live run with its status, counts, and a boolean for whether
#          an error summary was produced.
# Database/ORM: AuditLogORM (via the sink wired by T37).
# Standards: ``error_summary`` itself is NOT copied into details to keep
#            audit payloads small and to avoid accidentally surfacing
#            sensitive substrings from upstream exceptions; the boolean
#            ``error_summary_present`` is enough for operator triage and
#            ``connector_runs.error_summary`` remains the source of truth.
# Blast Radius: Audit log only.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py
#     -> _finish_aggregate_live_run / _finish_failed_live_run call this
#     after finish_run + commit.
# ============================================================================
def emit_run_finished(*, sink: AuditSink, actor: UserPrincipal, run: Any) -> None:
    """Emit one CONNECTOR_JOB_RUN audit row with lifecycle=FINISHED."""
    counts = run.counts if run.counts is not None else {}
    # Operator-console context: ``connector_key``, ``account_id``, and
    # ``report_month`` are beyond the plan's minimal lifecycle keys. They
    # surface enough run metadata to render a meaningful lifecycle row in
    # the operator console without joining back to ``connector_runs``.
    # None of these keys are secret material; ``connector_key`` and
    # ``report_month`` are operator-supplied at run-start, and
    # ``account_id`` is the external Google account identifier already
    # visible in connector configuration.
    record_audit_event(
        sink=sink,
        actor=actor,
        event_type=AuditEventType.CONNECTOR_JOB_RUN,
        entity_type="connector_run",
        entity_id=str(run.id),
        scope=AccessScope.connector(run.connector_key),
        details={
            "lifecycle": "FINISHED",
            "run_id": str(run.id),
            "connector_key": run.connector_key,
            "account_id": run.account_id,
            "report_month": run.report_month,
            "status": run.status,
            "counts": dict(counts),
            "error_summary_present": run.error_summary is not None,
        },
    )


# ============================================================================
# Purpose: Record one ``REPORT_IMPORTED`` raw-file lifecycle row with shared
#          run/raw-file metadata, then merge lifecycle-specific details.
# Database/ORM: AuditLogORM (via the sink wired by T37); raw_file persistence
#               and lifecycle transitions are owned by the orchestrator.
# Standards: Reads only explicit attributes from run/raw_file, avoids
#            serializing ORM objects wholesale, and keeps sensitive exception
#            messages out of audit details.
# Blast Radius: Audit log only. No finance, authorization, export, or Neo4j
#               projection impact detected.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     calls the public raw-file lifecycle emitters below.
#   - File: tests/connectors/google/test_audit_wiring.py -> locks the emitted
#     lifecycle payload shape for the operator console.
# ============================================================================
def _emit_raw_file_lifecycle(
    *,
    sink: AuditSink,
    actor: UserPrincipal,
    run: Any,
    raw_file: Any,
    lifecycle: str,
    extra_details: dict[str, object] | None = None,
) -> None:
    """Emit one REPORT_IMPORTED row for a raw_file lifecycle edge."""
    details: dict[str, object] = {
        "lifecycle": lifecycle,
        "run_id": str(run.id),
        "raw_file_id": str(raw_file.id),
        "source": raw_file.source,
        "report_type": raw_file.report_type,
        "report_month": raw_file.report_month,
    }
    if extra_details:
        details.update(extra_details)
    record_audit_event(
        sink=sink,
        actor=actor,
        event_type=AuditEventType.REPORT_IMPORTED,
        entity_type="raw_report_file",
        entity_id=str(raw_file.id),
        scope=AccessScope.connector(run.connector_key),
        details=details,
    )


# ============================================================================
# Purpose: Record one ``REPORT_IMPORTED`` row when the orchestrator persists
#          a raw report file and links it to the active run.
# Database/ORM: AuditLogORM (via the sink wired by T37); the raw_file row
#               itself is already flushed to ``raw_report_files`` by the
#               orchestrator before this emitter is called.
# Standards: Reads only the named attributes -- never serializes the source
#            object as a whole, so future drift adding sensitive fields to
#            RawReportFileORM does not silently leak into the audit log.
# Blast Radius: Audit log only.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py
#     -> _prepare_raw_report_file / _handle_live_produced_report invoke
#     this once per raw file after the row is flushed.
# ============================================================================
def emit_raw_file_downloaded(
    *,
    sink: AuditSink,
    actor: UserPrincipal,
    run: Any,
    raw_file: Any,
) -> None:
    """Emit one REPORT_IMPORTED audit row with lifecycle=DOWNLOADED."""
    _emit_raw_file_lifecycle(
        sink=sink,
        actor=actor,
        run=run,
        raw_file=raw_file,
        lifecycle="DOWNLOADED",
        extra_details={
            "checksum": raw_file.checksum,
            "storage_uri": raw_file.file_url,
        },
    )


# ============================================================================
# Purpose: Record one ``REPORT_IMPORTED`` row when the parser has consumed a
#          raw file and the orchestrator has marked it PARSED.
# Database/ORM: AuditLogORM (via the sink wired by T37).
# Standards: Carries the upsert count from the parser/repository result so
#            operators can correlate audit volume with finance row volume.
# Blast Radius: Audit log only.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py
#     -> _process_one_report invokes this after mark_parsed succeeds.
# ============================================================================
def emit_raw_file_parsed(
    *,
    sink: AuditSink,
    actor: UserPrincipal,
    run: Any,
    raw_file: Any,
    count_upserted: int,
) -> None:
    """Emit one REPORT_IMPORTED audit row with lifecycle=PARSED.

    count_upserted is the REPORT-level upsert count, repeated on each
    per-raw-file PARSED edge when a single report produces multiple
    raw_files (e.g., multi-channel YouTube Analytics reports). Operator-
    console aggregation MUST NOT sum count_upserted across PARSED rows of
    the same (run_id, report_type) tuple -- aggregate at the report or run
    level instead.
    """
    # Operator-console context (``source``, ``report_type``, ``report_month``):
    # see emit_run_finished above for the rationale -- these keys let the
    # operator console render a raw_report_file lifecycle row without
    # joining back to ``connector_run_raw_files`` / ``raw_report_files``.
    _emit_raw_file_lifecycle(
        sink=sink,
        actor=actor,
        run=run,
        raw_file=raw_file,
        lifecycle="PARSED",
        extra_details={
            "count_upserted": int(count_upserted),
        },
    )


# ============================================================================
# Purpose: Record one ``REPORT_IMPORTED`` row when the orchestrator's
#          bucket-B handler transitions a raw file to FAILED.
# Database/ORM: AuditLogORM (via the sink wired by T37).
# Standards: ``error_class`` is the Python exception class name -- a short,
#            non-PII token suitable for the audit log. The exception
#            message itself is NOT copied; the orchestrator's
#            ``error_summary`` (already truncated to 500 chars by the
#            connector_runs check constraint) is the source of truth for
#            operator-readable failure context.
# Blast Radius: Audit log only.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py
#     -> _record_live_report_failure invokes this after mark_failed.
# ============================================================================
def emit_raw_file_failed(
    *,
    sink: AuditSink,
    actor: UserPrincipal,
    run: Any,
    raw_file: Any,
    error_class: str,
) -> None:
    """Emit one REPORT_IMPORTED audit row with lifecycle=FAILED."""
    # Operator-console context (``source``, ``report_type``, ``report_month``):
    # see emit_run_finished above for the rationale -- these keys let the
    # operator console render a raw_report_file lifecycle row without
    # joining back to ``connector_run_raw_files`` / ``raw_report_files``.
    _emit_raw_file_lifecycle(
        sink=sink,
        actor=actor,
        run=run,
        raw_file=raw_file,
        lifecycle="FAILED",
        extra_details={
            "error_class": error_class,
        },
    )
