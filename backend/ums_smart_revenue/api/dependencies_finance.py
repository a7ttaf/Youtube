"""Shared FastAPI dependency providers for finance-layer repositories.

Extracted here so both api.allocation and api.revenue can import these
providers without the api.allocation -> api.revenue -> api.allocation cycle
that would arise if they were defined (and imported) from api.revenue alone.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import current_db_session
from ums_smart_revenue.auth.scopes import OrgAccessIndex
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
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyRevenueFactRepository:
    """Build the revenue-fact repository bound to the current database session."""
    return SqlAlchemyRevenueFactRepository(session)


def current_deduction_component_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyDeductionComponentRepository:
    """Build the tenant-aware deduction-component repository for a request."""
    return SqlAlchemyDeductionComponentRepository(session)


def current_committed_allocation_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyCommittedAllocationRepository:
    """Build the committed-allocation repository bound to the request session.

    Defined in dependencies_finance (not api.revenue or api.allocation) so
    both modules can import it without introducing a cross-module cycle.
    """
    return SqlAlchemyCommittedAllocationRepository(session)


def current_channel_account_link_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyChannelAccountLinkRepository:
    """Build the tenant-aware channel-account-link repository for a request."""
    return SqlAlchemyChannelAccountLinkRepository(session)
