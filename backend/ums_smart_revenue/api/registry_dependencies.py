# ============================================================================
# Purpose: Shared FastAPI registry providers — the channel registry and the
#   channel-group registry — so route modules and the app factory get their
#   registry dependencies from one cycle-free home instead of importing a
#   route module's internals (channel providers moved from api/channels.py
#   in the api-layering follow-up).
# Database/ORM: None here; the SQL-backed registries these providers build
#   read/write YouTubeChannelORM and the channel-group tables.
# Standards: The in-memory registries are fail-safe defaults only —
#   create_app overrides both current_* providers with their SQL builders.
# Blast Radius: Registry wiring for the channel and group route surfaces;
#   a change here re-targets which store those routes read and write.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_registry.py -> the
#     channel registry contract and its bootstrap.
#   - File: backend/ums_smart_revenue/org/channel_groups.py -> the group
#     registry contract.
#   - File: backend/ums_smart_revenue/app.py -> the factory overrides that
#     swap in the SQL-backed registries.
# ============================================================================
"""Shared FastAPI registry providers for channels and channel groups."""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import current_db_session
from ums_smart_revenue.org.channel_groups import ChannelGroupRegistry
from ums_smart_revenue.org.channel_registry import (
    ChannelRegistry,
    bootstrap_channel_registry,
)
from ums_smart_revenue.org.sql_channel_groups import SqlAlchemyChannelGroupRegistry
from ums_smart_revenue.org.sql_channel_registry import SqlAlchemyChannelRegistry

_GROUP_REGISTRY = ChannelGroupRegistry()
_CHANNEL_REGISTRY = bootstrap_channel_registry()


# ============================================================================
# Purpose: Fail-safe default group-registry provider; create_app overrides
#   it with sql_group_registry_from_session.
# Database/ORM: None; module-level in-memory registry.
# Standards: Tests override THIS dependency to inject group fixtures.
# Blast Radius: Group-registry wiring default for the group routes.
# Connections:
#   - File: backend/ums_smart_revenue/app.py -> overridden by the factory.
# ============================================================================
def current_group_registry() -> ChannelGroupRegistry:
    """Return the module-level in-memory group registry (fail-safe default)."""
    return _GROUP_REGISTRY


# ============================================================================
# Purpose: Build the SQL-backed group registry the app factory swaps in for
#   current_group_registry.
# Database/ORM: SqlAlchemyChannelGroupRegistry over the channel-group
#   tables, bound to the request's tenant session.
# Standards: Tenant session (current_db_session) so group reads/writes ride
#   the request transaction.
# Blast Radius: Group-registry store for every group route in production.
# Connections:
#   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> the
#     registry implementation built here.
# ============================================================================
def sql_group_registry_from_session(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyChannelGroupRegistry:
    """Build a SQL-backed channel-group registry bound to the request's session."""
    return SqlAlchemyChannelGroupRegistry(session)


# ============================================================================
# Purpose: Fail-safe default channel-registry provider; create_app overrides
#   it with sql_channel_registry_from_session (moved from api/channels.py in
#   the api-layering follow-up).
# Database/ORM: None; module-level bootstrap in-memory registry.
# Standards: Tests override THIS dependency to inject channel fixtures; the
#   bootstrap registry seeds the default channel set.
# Blast Radius: Channel-registry wiring default for every channel route and
#   the analytics visibility filters.
# Connections:
#   - File: backend/ums_smart_revenue/app.py -> overridden by the factory.
#   - File: backend/ums_smart_revenue/org/channel_registry.py ->
#     bootstrap_channel_registry, the seeded default store.
# ============================================================================
def current_channel_registry() -> ChannelRegistry:
    """Return the module-level in-memory channel registry (fail-safe default)."""
    return _CHANNEL_REGISTRY


# ============================================================================
# Purpose: Build the SQL-backed channel registry the app factory swaps in
#   for current_channel_registry.
# Database/ORM: SqlAlchemyChannelRegistry over YouTubeChannelORM, bound to
#   the request's tenant session.
# Standards: Tenant session (current_db_session) so channel reads/writes
#   ride the request transaction.
# Blast Radius: Channel-registry store for every channel route in
#   production; the store every channel mutation and visibility read hits.
# Connections:
#   - File: backend/ums_smart_revenue/org/sql_channel_registry.py -> the
#     registry implementation built here.
# ============================================================================
def sql_channel_registry_from_session(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyChannelRegistry:
    """Build a SQL-backed channel registry bound to the request's session."""
    return SqlAlchemyChannelRegistry(session)
