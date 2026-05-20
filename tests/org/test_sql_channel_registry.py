from uuid import UUID

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.org.access_index import load_org_access_index_from_session
from ums_smart_revenue.org.channel_registry import ChannelRegistryValidationError
from ums_smart_revenue.org.sql_channel_groups import SqlAlchemyChannelGroupRegistry
from ums_smart_revenue.org.sql_channel_registry import SqlAlchemyChannelRegistry
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

DEFAULT_TENANT_ID = UUID(UMS_TENANT_ID)
SECTOR_TV_ID = UUID("00000000-0000-0000-0000-000000000101")
COMPANY_TV_ID = UUID("00000000-0000-0000-0000-000000000201")
COMPANY_NEWS_ID = UUID("00000000-0000-0000-0000-000000000202")
COMPANY_INACTIVE_ID = UUID("00000000-0000-0000-0000-000000000203")
COMPANY_MISSING_ID = UUID("00000000-0000-0000-0000-000000000204")
CHANNEL_TV_ROW_ID = UUID("00000000-0000-0000-0000-000000000301")
CHANNEL_INACTIVE_ROW_ID = UUID("00000000-0000-0000-0000-000000000302")
OTHER_TENANT_ID = UUID("00000000-0000-0000-0000-000000000999")
OTHER_TENANT_SECTOR_ID = UUID("00000000-0000-0000-0000-000000000901")
OTHER_TENANT_COMPANY_ID = UUID("00000000-0000-0000-0000-000000000902")
OTHER_TENANT_CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-000000000903")
OTHER_TENANT_CHANNEL_ID = "channel-shared-across-tenants"


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
            OrgUnitORM(
                id=SECTOR_TV_ID, parent_id=None, type="SECTOR", name="TV", active=True
            ),
            OrgUnitORM(
                id=COMPANY_TV_ID,
                parent_id=SECTOR_TV_ID,
                type="COMPANY",
                name="TV Company",
                active=True,
            ),
            OrgUnitORM(
                id=COMPANY_NEWS_ID,
                parent_id=SECTOR_TV_ID,
                type="COMPANY",
                name="News Company",
                active=True,
            ),
            OrgUnitORM(
                id=COMPANY_INACTIVE_ID,
                parent_id=SECTOR_TV_ID,
                type="COMPANY",
                name="Inactive Company",
                active=False,
            ),
            OrgUnitORM(
                id=OTHER_TENANT_SECTOR_ID,
                tenant_id=OTHER_TENANT_ID,
                parent_id=None,
                type="SECTOR",
                name="Other Tenant TV",
                active=True,
            ),
            OrgUnitORM(
                id=OTHER_TENANT_COMPANY_ID,
                tenant_id=OTHER_TENANT_ID,
                parent_id=OTHER_TENANT_SECTOR_ID,
                type="COMPANY",
                name="Other Tenant Company",
                active=True,
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
            YouTubeChannelORM(
                id=OTHER_TENANT_CHANNEL_ROW_ID,
                tenant_id=OTHER_TENANT_ID,
                youtube_channel_id=OTHER_TENANT_CHANNEL_ID,
                channel_name="Other Tenant Channel",
                primary_org_unit_id=OTHER_TENANT_COMPANY_ID,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                active=True,
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
        select(YouTubeChannelORM).where(
            YouTubeChannelORM.tenant_id == DEFAULT_TENANT_ID,
            YouTubeChannelORM.youtube_channel_id == "channel-tv-b"
        )
    ).one()
    assert created.youtube_channel_id == "channel-tv-b"
    assert updated.primary_company_id == str(COMPANY_NEWS_ID)
    assert persisted.primary_org_unit_id == COMPANY_NEWS_ID
    assert {channel.youtube_channel_id for channel in registry.list_channels()} == {
        "channel-tv-a",
        "channel-tv-b",
    }


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


def test_org_sql_repositories_validate_tenant_id_constructor_input():
    with pytest.raises(ChannelRegistryValidationError, match="tenant_id"):
        SqlAlchemyChannelRegistry(object(), tenant_id="not-a-uuid")
    with pytest.raises(ValueError, match="tenant_id must be a valid UUID"):
        SqlAlchemyChannelGroupRegistry(object(), tenant_id=" ")


def test_sql_channel_registry_rejects_missing_company_id_on_create():
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)

    with pytest.raises(
        ChannelRegistryValidationError,
        match="primary_company_id must reference an existing org unit",
    ):
        registry.create_channel(
            youtube_channel_id="channel-missing-company",
            channel_name="Missing Company",
            primary_company_id=str(COMPANY_MISSING_ID),
            cms_status="UNKNOWN",
            revenue_required=False,
        )


def test_sql_channel_registry_does_not_mask_non_duplicate_integrity_error(
    monkeypatch: pytest.MonkeyPatch,
):
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)
    lookups = 0

    def _get_row(channel_id: str):
        nonlocal lookups
        assert channel_id == "channel-racing-failure"
        lookups += 1
        return None if lookups == 1 else object()

    def _raise_foreign_key_failure():
        raise IntegrityError(
            "insert youtube channel",
            {},
            Exception("foreign key constraint failed"),
        )

    monkeypatch.setattr(registry, "_get_row", _get_row)
    monkeypatch.setattr(session, "flush", _raise_foreign_key_failure)

    with pytest.raises(
        ChannelRegistryValidationError,
        match="primary_company_id must reference an existing org unit",
    ):
        registry.create_channel(
            youtube_channel_id="channel-racing-failure",
            channel_name="Racing Failure",
            primary_company_id=None,
            cms_status="UNKNOWN",
            revenue_required=False,
        )

    assert lookups == 1


def test_sql_channel_registry_rejects_missing_company_id_on_update_and_rolls_back():
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)

    with pytest.raises(
        ChannelRegistryValidationError,
        match="primary_company_id must reference an existing org unit",
    ):
        registry.update_mapping(
            youtube_channel_id="channel-tv-a",
            primary_company_id=str(COMPANY_MISSING_ID),
        )

    persisted = session.scalars(
        select(YouTubeChannelORM).where(
            YouTubeChannelORM.tenant_id == DEFAULT_TENANT_ID,
            YouTubeChannelORM.youtube_channel_id == "channel-tv-a"
        )
    ).one()
    assert persisted.primary_org_unit_id == COMPANY_TV_ID


def test_sql_channel_registry_filters_every_read_to_default_tenant():
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)

    channels_by_id = registry.list_channels_by_ids(
        {"channel-tv-a", OTHER_TENANT_CHANNEL_ID}
    )

    assert registry.get_channel(OTHER_TENANT_CHANNEL_ID) is None
    assert {channel.youtube_channel_id for channel in registry.list_channels()} == {
        "channel-tv-a"
    }
    assert {channel.youtube_channel_id for channel in channels_by_id} == {
        "channel-tv-a"
    }


def test_sql_channel_registry_allows_same_external_channel_id_in_another_tenant():
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)

    created = registry.create_channel(
        youtube_channel_id=OTHER_TENANT_CHANNEL_ID,
        channel_name="Default Tenant Shared Channel",
        primary_company_id=str(COMPANY_TV_ID),
        cms_status="UNKNOWN",
        revenue_required=False,
    )

    assert created.youtube_channel_id == OTHER_TENANT_CHANNEL_ID
    assert created.primary_company_id == str(COMPANY_TV_ID)


def test_sql_channel_registry_explicit_tenant_writes_are_isolated():
    session = build_session()
    seed_org(session)
    default_registry = SqlAlchemyChannelRegistry(session)
    other_registry = SqlAlchemyChannelRegistry(session, tenant_id=OTHER_TENANT_ID)

    created = other_registry.create_channel(
        youtube_channel_id="channel-other-tenant-created",
        channel_name="Other Tenant Created",
        primary_company_id=str(OTHER_TENANT_COMPANY_ID),
        cms_status="UNKNOWN",
        revenue_required=False,
    )

    assert created.youtube_channel_id == "channel-other-tenant-created"
    assert default_registry.get_channel("channel-other-tenant-created") is None
    assert {
        channel.youtube_channel_id for channel in default_registry.list_channels()
    } == {"channel-tv-a"}
    assert {
        channel.youtube_channel_id for channel in other_registry.list_channels()
    } == {"channel-other-tenant-created", OTHER_TENANT_CHANNEL_ID}


def test_sql_channel_registry_rejects_cross_tenant_company_id():
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)

    with pytest.raises(
        ChannelRegistryValidationError,
        match="primary_company_id must reference an existing org unit",
    ):
        registry.create_channel(
            youtube_channel_id="channel-cross-tenant",
            channel_name="Cross Tenant",
            primary_company_id=str(OTHER_TENANT_COMPANY_ID),
            cms_status="UNKNOWN",
            revenue_required=False,
        )


def test_load_org_access_index_from_session_uses_active_sql_rows():
    session = build_session()
    seed_org(session)

    index = load_org_access_index_from_session(session)

    assert index.company_sector == {
        str(COMPANY_TV_ID): str(SECTOR_TV_ID),
        str(COMPANY_NEWS_ID): str(SECTOR_TV_ID),
    }
    assert index.channel_company == {"channel-tv-a": str(COMPANY_TV_ID)}
    assert index.channel_sector == {"channel-tv-a": str(SECTOR_TV_ID)}
    assert str(COMPANY_INACTIVE_ID) not in index.company_sector
    assert "channel-inactive" not in index.channel_company
    assert str(OTHER_TENANT_COMPANY_ID) not in index.company_sector
    assert OTHER_TENANT_CHANNEL_ID not in index.channel_company
