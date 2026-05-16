from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import current_db_session
from ums_smart_revenue.org.channel_groups import ChannelGroupRegistry
from ums_smart_revenue.org.sql_channel_groups import SqlAlchemyChannelGroupRegistry

_GROUP_REGISTRY = ChannelGroupRegistry()


def current_group_registry() -> ChannelGroupRegistry:
    return _GROUP_REGISTRY


def sql_group_registry_from_session(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyChannelGroupRegistry:
    return SqlAlchemyChannelGroupRegistry(session)
