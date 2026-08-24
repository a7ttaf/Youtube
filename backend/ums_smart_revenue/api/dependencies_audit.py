# ============================================================================
# Purpose: Shared FastAPI audit-sink providers and the audit-record API
#   serializer — the cycle-free home for the audit wiring that nearly every
#   route module depends on. Extracted from api/channels.py (the
#   api-layering follow-up) so route modules stop importing another route
#   module's internals for their audit dependencies.
# Database/ORM: None here; the SQL-backed sinks these providers build write
#   audit_logs through auth/sql_audit_sink.
# Standards: The in-memory sink is the fail-safe default only — create_app
#   overrides current_audit_sink with sql_audit_sink_from_session and
#   current_atomic_audit_sink with sql_atomic_audit_sink_from_session.
#   Atomic (tenant-lane) vs platform-lane sink selection is a recorded
#   all-or-nothing ruling; see the function contracts below.
# Blast Radius: Audit capture for every route wired through these
#   providers; a wiring change here changes which transaction audit rows
#   join across the API surface.
# Connections:
#   - File: backend/ums_smart_revenue/auth/audit_service.py ->
#     InMemoryAuditSink / AuditSink / AuditRecord, the sink contract.
#   - File: backend/ums_smart_revenue/auth/sql_audit_sink.py ->
#     SqlAlchemyAuditSink (platform lane) and PlatformLaneAuditSink
#     (tenant-transaction lane) built here.
#   - File: backend/ums_smart_revenue/app.py -> the app factory's
#     dependency overrides that swap in the SQL-backed sinks.
#   - Consumers: the route modules that previously imported these from
#     api.channels (channels, groups, exports, reconciliation, adsense,
#     allocation, audit, channel_account_links, connectors, exchange_rates,
#     export_templates, finance_close, reports, users). api/revenue.py is
#     NOT one of them — its sink providers live in dependencies_finance and
#     it keeps its own response serialization.
# ============================================================================
"""Shared audit-sink dependency providers and the audit-record API serializer."""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_platform_db_session,
)
from ums_smart_revenue.auth.audit_service import (
    AuditRecord,
    AuditSink,
    InMemoryAuditSink,
)
from ums_smart_revenue.auth.sql_audit_sink import (
    PlatformLaneAuditSink,
    SqlAlchemyAuditSink,
)

_AUDIT_SINK = InMemoryAuditSink()


# ============================================================================
# Purpose: Fail-safe default audit sink for routes; create_app overrides it
#   with sql_audit_sink_from_session so production audit rows persist.
# Database/ORM: None; module-level in-memory sink.
# Standards: Tests override THIS dependency to capture audit records; the
#   atomic provider below depends on it so one override reaches both.
# Blast Radius: Audit capture default for every non-atomic route.
# Connections:
#   - File: backend/ums_smart_revenue/app.py -> overridden by the factory.
# ============================================================================
def current_audit_sink() -> InMemoryAuditSink:
    """Return the module-level in-memory audit sink (fail-safe default)."""
    return _AUDIT_SINK


# ============================================================================
# Purpose: Build the platform-lane SQL audit sink the app factory swaps in
#   for current_audit_sink.
# Database/ORM: Writes audit_logs on the independently committed platform
#   session.
# Standards: Platform lane by design — audit rows persist even when the
#   tenant transaction rolls back (the default audit posture); routes that
#   promise all-or-nothing semantics use the atomic pair instead.
# Blast Radius: Production audit persistence for every non-atomic route.
# Connections:
#   - File: backend/ums_smart_revenue/auth/sql_audit_sink.py ->
#     SqlAlchemyAuditSink built here.
# ============================================================================
def sql_audit_sink_from_session(
    session: Annotated[Session, Depends(current_platform_db_session)],
) -> SqlAlchemyAuditSink:
    """Build a SQLAlchemy-backed audit sink bound to the current platform session."""
    return SqlAlchemyAuditSink(session)


# ============================================================================
# Purpose: Audit sink for all-or-nothing routes; passes through the default
#   sink until the app factory overrides it with the tenant-lane builder.
# Database/ORM: None directly; delegates to the injected sink.
# Standards: Depending on current_audit_sink keeps the in-memory test
#   wiring working — an override of that dependency still reaches every
#   route wired here.
# Blast Radius: Audit capture for the all-or-nothing routes (bulk channel
#   import, CMS group sync).
# Connections:
#   - File: backend/ums_smart_revenue/app.py -> overridden with
#     sql_atomic_audit_sink_from_session by the factory.
# ============================================================================
def current_atomic_audit_sink(
    sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> AuditSink:
    """Audit sink for all-or-nothing routes; passes through until SQL wiring overrides it.

    create_app overrides this with sql_atomic_audit_sink_from_session so the
    route's audit rows join the tenant transaction (all-or-nothing with the
    domain writes) instead of committing on the independent platform session.
    Depending on current_audit_sink keeps the in-memory test wiring working:
    an override of that dependency still reaches every route wired here.
    """
    return sink


# ============================================================================
# Purpose: Bind an all-or-nothing route's audit writes to the request's
#   tenant transaction so audit rows and domain writes commit or roll back
#   together.
# Database/ORM: Writes audit_logs inside the request's tenant session via
#   PlatformLaneAuditSink (elevating per append — audit_logs is
#   platform-only writable).
# Standards: The all-or-nothing ruling: the app-wide platform-lane sink
#   commits BEFORE the tenant session tears down, so a tenant commit
#   failure would leave audit rows claiming work that never happened;
#   this builder closes that window for the routes that promise atomicity.
# Blast Radius: Audit integrity for the bulk import and CMS group sync.
# Connections:
#   - File: backend/ums_smart_revenue/auth/sql_audit_sink.py ->
#     PlatformLaneAuditSink, the tenant-transaction writer.
# ============================================================================
def sql_atomic_audit_sink_from_session(
    session: Annotated[Session, Depends(current_db_session)],
) -> PlatformLaneAuditSink:
    """Bind an all-or-nothing route's audit writes to the request's tenant session.

    The bulk import and the CMS group sync both promise all-or-nothing
    semantics. The app-wide audit sink runs on the independently committed
    platform session, which FastAPI tears down (and commits) BEFORE the tenant
    session; a tenant commit failure would then leave audit rows permanently
    claiming work that never happened. PlatformLaneAuditSink writes audit_logs
    inside the SAME transaction as the domain writes, elevating per append
    because audit_logs is platform-only writable.
    """
    return PlatformLaneAuditSink(session)


# ============================================================================
# Purpose: Serialize one AuditRecord into the API response shape shared by
#   every route that echoes audit records to callers.
# Database/ORM: None; pure field mapping.
# Standards: Field set is the recorded API contract — additions or renames
#   here change every audit-echoing response at once.
# Blast Radius: Response shape of every route returning audit records.
# Connections:
#   - File: backend/ums_smart_revenue/auth/audit_service.py -> AuditRecord,
#     the source dataclass.
#   - File: Docs/12_BACKEND_API_SPEC.md -> audit-record response contract.
# ============================================================================
def audit_record_to_api(record: AuditRecord) -> dict[str, object]:
    """Serialize an AuditRecord into the shared API response mapping."""
    return {
        "event_type": record.event_type,
        "entity_type": record.entity_type,
        "entity_id": record.entity_id,
        "scope_type": record.scope_type,
        "scope_id": record.scope_id,
        "reason": record.reason,
        "sensitive": record.sensitive,
    }
