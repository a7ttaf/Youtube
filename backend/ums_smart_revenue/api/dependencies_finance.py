"""Shared FastAPI dependency providers for the finance route modules.

Extracted here so the finance route modules (api.revenue, api.allocation,
api.reconciliation, api.exports, ...) and the app factory can import the
org-index, repository, and revenue-audit-sink providers without one route
module importing another's internals — the cycle-free home for providers
that more than one router depends on.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_platform_db_session,
)
from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.auth.scopes import OrgAccessIndex
from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink
from ums_smart_revenue.finance.channel_account_links import (
    SqlAlchemyChannelAccountLinkRepository,
)
from ums_smart_revenue.finance.committed_allocation import (
    SqlAlchemyCommittedAllocationRepository,
)
from ums_smart_revenue.finance.deduction_ingestion import (
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.revenue_facts import SqlAlchemyRevenueFactRepository
from ums_smart_revenue.org.access_index import load_org_access_index_from_session


def current_org_access_index(
    session: Annotated[Session, Depends(current_db_session)],
) -> OrgAccessIndex:
    """Load the org-access index from the current database session for scope resolution."""
    return load_org_access_index_from_session(session)


def current_revenue_fact_repository(
    session: Annotated[Session, Depends(current_platform_db_session)],
) -> SqlAlchemyRevenueFactRepository:
    """Build the revenue-fact repository bound to the current database session."""
    return SqlAlchemyRevenueFactRepository(session)


def current_deduction_component_repository(
    session: Annotated[Session, Depends(current_platform_db_session)],
) -> SqlAlchemyDeductionComponentRepository:
    """Build the tenant-aware deduction-component repository for a request."""
    return SqlAlchemyDeductionComponentRepository(session)


def current_committed_allocation_repository(
    session: Annotated[Session, Depends(current_platform_db_session)],
) -> SqlAlchemyCommittedAllocationRepository:
    """Build the committed-allocation repository bound to the request session.

    Defined in dependencies_finance (not api.revenue or api.allocation) so
    both modules can import it without introducing a cross-module cycle.
    """
    return SqlAlchemyCommittedAllocationRepository(session)


def current_channel_account_link_repository(
    session: Annotated[Session, Depends(current_platform_db_session)],
) -> SqlAlchemyChannelAccountLinkRepository:
    """Build the tenant-aware channel-account-link repository for a request."""
    return SqlAlchemyChannelAccountLinkRepository(session)


_AUDIT_SINK = InMemoryAuditSink()


def current_revenue_audit_sink() -> InMemoryAuditSink:
    """Return the module-level in-memory audit sink for revenue route events.

    Fail-safe default only; the app factory overrides this with
    sql_revenue_audit_sink_from_session. Defined here (not api.revenue) so
    the revenue, reconciliation, and export route modules can all depend on
    it without importing another route module's internals.
    """
    return _AUDIT_SINK


def sql_revenue_audit_sink_from_session(
    session: Annotated[Session, Depends(current_platform_db_session)],
) -> SqlAlchemyAuditSink:
    """Build a SQLAlchemy-backed audit sink bound to the current database session."""
    return SqlAlchemyAuditSink(session)
