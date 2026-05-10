from uuid import UUID

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.org.access_index import load_org_access_index_from_session
from ums_smart_revenue.org.channel_registry import ChannelRegistryValidationError
from ums_smart_revenue.org.sql_channel_registry import SqlAlchemyChannelRegistry


SECTOR_TV_ID = UUID("00000000-0000-0000-0000-000000000101")
COMPANY_TV_ID = UUID("00000000-0000-0000-0000-000000000201")
COMPANY_NEWS_ID = UUID("00000000-0000-0000-0000-000000000202")
COMPANY_INACTIVE_ID = UUID("00000000-0000-0000-0000-000000000203")
COMPANY_MISSING_ID = UUID("00000000-0000-0000-0000-000000000204")
CHANNEL_TV_ROW_ID = UUID("00000000-0000-0000-0000-000000000301")
CHANNEL_INACTIVE_ROW_ID = UUID("00000000-0000-0000-0000-000000000302")


def build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, connection_record):
        del connection_record
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    OrgBase.metadata.create_all(engine)
    return Session(engine)


def seed_org(session: Session) -> None:
    session.add_all(
        [
            OrgUnitORM(id=SECTOR_TV_ID, parent_id=None, type="SECTOR", name="TV", active=True),
            OrgUnitORM(id=COMPANY_TV_ID, parent_id=SECTOR_TV_ID, type="COMPANY", name="TV Company", active=True),
            OrgUnitORM(id=COMPANY_NEWS_ID, parent_id=SECTOR_TV_ID, type="COMPANY", name="News Company", active=True),
            OrgUnitORM(
                id=COMPANY_INACTIVE_ID,
                parent_id=SECTOR_TV_ID,
                type="COMPANY",
                name="Inactive Company",
                active=False,
            ),
            YouTubeChannelORM(
                id=CHANNEL_TV_ROW_ID,
                youtube_channel_id="channel-tv-a",
                channel_name="TV A",
                primary_org_unit_id=COMPANY_TV_ID,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                active=True,
            ),
            YouTubeChannelORM(
                id=CHANNEL_INACTIVE_ROW_ID,
                youtube_channel_id="channel-inactive",
                channel_name="Inactive Channel",
                primary_org_unit_id=COMPANY_TV_ID,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                active=False,
            ),
        ]
    )
    session.commit()


def test_sql_channel_registry_reads_and_writes_channel_rows():
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)

    created = registry.create_channel(
        youtube_channel_id="channel-tv-b",
        channel_name="TV B",
        primary_company_id=str(COMPANY_TV_ID),
        cms_status="UNKNOWN",
        revenue_required=False,
    )
    updated = registry.update_mapping(
        youtube_channel_id="channel-tv-b",
        primary_company_id=str(COMPANY_NEWS_ID),
    )

    persisted = session.scalars(
        select(YouTubeChannelORM).where(YouTubeChannelORM.youtube_channel_id == "channel-tv-b")
    ).one()
    assert created.youtube_channel_id == "channel-tv-b"
    assert updated.primary_company_id == str(COMPANY_NEWS_ID)
    assert persisted.primary_org_unit_id == COMPANY_NEWS_ID
    assert [channel.youtube_channel_id for channel in registry.list_channels()] == ["channel-tv-a", "channel-tv-b"]


def test_sql_channel_registry_rejects_malformed_primary_company_id():
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)

    with pytest.raises(ValueError, match="primary_company_id must be a valid UUID"):
        registry.create_channel(
            youtube_channel_id="channel-bad-company",
            channel_name="Bad Company",
            primary_company_id="not-a-uuid",
            cms_status="UNKNOWN",
            revenue_required=False,
        )


def test_sql_channel_registry_rejects_missing_company_id_on_create():
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)

    with pytest.raises(ChannelRegistryValidationError, match="primary_company_id must reference an existing org unit"):
        registry.create_channel(
            youtube_channel_id="channel-missing-company",
            channel_name="Missing Company",
            primary_company_id=str(COMPANY_MISSING_ID),
            cms_status="UNKNOWN",
            revenue_required=False,
        )


def test_sql_channel_registry_rejects_missing_company_id_on_update_and_rolls_back():
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)

    with pytest.raises(ChannelRegistryValidationError, match="primary_company_id must reference an existing org unit"):
        registry.update_mapping(
            youtube_channel_id="channel-tv-a",
            primary_company_id=str(COMPANY_MISSING_ID),
        )

    persisted = session.scalars(
        select(YouTubeChannelORM).where(YouTubeChannelORM.youtube_channel_id == "channel-tv-a")
    ).one()
    assert persisted.primary_org_unit_id == COMPANY_TV_ID


def test_load_org_access_index_from_session_uses_active_sql_rows():
    session = build_session()
    seed_org(session)

    index = load_org_access_index_from_session(session)

    assert index.company_sector == {str(COMPANY_TV_ID): str(SECTOR_TV_ID), str(COMPANY_NEWS_ID): str(SECTOR_TV_ID)}
    assert index.channel_company == {"channel-tv-a": str(COMPANY_TV_ID)}
    assert index.channel_sector == {"channel-tv-a": str(SECTOR_TV_ID)}
    assert str(COMPANY_INACTIVE_ID) not in index.company_sector
    assert "channel-inactive" not in index.channel_company
