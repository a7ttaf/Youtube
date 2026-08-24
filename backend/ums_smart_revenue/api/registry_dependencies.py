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


def current_group_registry() -> ChannelGroupRegistry:
    """Return the module-level in-memory group registry (fail-safe default)."""
    return _GROUP_REGISTRY


def sql_group_registry_from_session(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyChannelGroupRegistry:
    """Build a SQL-backed channel-group registry bound to the request's session."""
    return SqlAlchemyChannelGroupRegistry(session)


def current_channel_registry() -> ChannelRegistry:
    """Return the module-level in-memory channel registry (fail-safe default)."""
    return _CHANNEL_REGISTRY


def sql_channel_registry_from_session(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyChannelRegistry:
    """Build a SQL-backed channel registry bound to the request's session."""
    return SqlAlchemyChannelRegistry(session)
