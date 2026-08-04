from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google.youtube_analytics_client import list_target_channels
from ums_smart_revenue.db.finance_models import (
    FinanceBase,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.org.access_index import load_org_access_index_from_session
from ums_smart_revenue.org.channel_registry import (
    ChannelMappingLockedMonthError,
    ChannelRegistryConflictError,
    ChannelRegistryValidationError,
    ChannelRevenueRequirementLockedMonthError,
)
from ums_smart_revenue.org.sql_channel_groups import SqlAlchemyChannelGroupRegistry
from ums_smart_revenue.org.sql_channel_registry import SqlAlchemyChannelRegistry
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import TENANT_CTX, TenantContextMissing
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

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


def _tenant(tenant_id: UUID, *, slug: str) -> Tenant:
    now = datetime.now(UTC)
    return Tenant(
        id=tenant_id,
        slug=slug,
        display_name=slug.upper(),
        primary_currency="USD",
        status=TenantStatus.ACTIVE,
        onboarding_at=now,
        created_at=now,
        updated_at=now,
    )


def build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    OrgBase.metadata.create_all(engine)
    return Session(engine)


def build_finance_session() -> Session:
    """Build an in-memory session with both org and finance tables created.

    The mapping month-lock guard reads finance tables, so the unit tests for it
    need the finance schema in addition to the org schema.
    """
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    return Session(engine)


def seed_org(session: Session) -> None:
    session.add_all(
        [
            OrgUnitORM(id=SECTOR_TV_ID, parent_id=None, type="SECTOR", name="TV", active=True),
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
    session = build_finance_session()
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
            YouTubeChannelORM.youtube_channel_id == "channel-tv-b",
        )
    ).one()
    assert created.youtube_channel_id == "channel-tv-b"
    assert updated.primary_company_id == str(COMPANY_NEWS_ID)
    assert persisted.primary_org_unit_id == COMPANY_NEWS_ID
    assert [channel.youtube_channel_id for channel in registry.list_channels()] == [
        "channel-tv-a",
        "channel-tv-b",
    ]


def test_sql_channel_registry_preserves_outside_cms_revenue_metadata():
    session = build_session()
    seed_org(session)
    session.add(
        YouTubeChannelORM(
            id=UUID("00000000-0000-0000-0000-000000000303"),
            youtube_channel_id="channel-outside-manual",
            channel_name="Outside Manual",
            primary_org_unit_id=COMPANY_TV_ID,
            cms_status="OUTSIDE_CMS",
            content_owner_id="content-owner-7",
            revenue_required=True,
            revenue_source_status="OFFICIAL_MANUAL_IMPORT",
            active=True,
        )
    )
    session.commit()
    registry = SqlAlchemyChannelRegistry(session)

    channel = registry.get_channel("channel-outside-manual")

    assert channel is not None
    assert channel.content_owner_id == "content-owner-7"
    assert channel.revenue_source_status == "OFFICIAL_MANUAL_IMPORT"
    assert channel.to_api()["content_owner_id"] == "content-owner-7"
    assert channel.to_api()["revenue_source_status"] == "OFFICIAL_MANUAL_IMPORT"


def test_sql_channel_registry_create_persists_content_owner_id():
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)

    created = registry.create_channel(
        youtube_channel_id="channel-cms",
        channel_name="CMS Channel",
        primary_company_id=str(COMPANY_TV_ID),
        cms_status="INSIDE_CMS",
        revenue_required=True,
        content_owner_id="owner-cms",
    )

    persisted = session.scalars(
        select(YouTubeChannelORM).where(
            YouTubeChannelORM.tenant_id == DEFAULT_TENANT_ID,
            YouTubeChannelORM.youtube_channel_id == "channel-cms",
        )
    ).one()
    assert created.content_owner_id == "owner-cms"
    assert persisted.content_owner_id == "owner-cms"


def test_sql_channel_registry_update_content_owner_sets_and_clears():
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)

    updated = registry.update_content_owner(
        youtube_channel_id="channel-tv-a", content_owner_id="owner-x"
    )
    persisted = session.scalars(
        select(YouTubeChannelORM).where(
            YouTubeChannelORM.tenant_id == DEFAULT_TENANT_ID,
            YouTubeChannelORM.youtube_channel_id == "channel-tv-a",
        )
    ).one()
    assert updated.content_owner_id == "owner-x"
    assert persisted.content_owner_id == "owner-x"

    cleared = registry.update_content_owner(
        youtube_channel_id="channel-tv-a", content_owner_id=None
    )
    assert cleared.content_owner_id is None


def test_sql_channel_registry_update_content_owner_missing_channel_raises():
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)

    with pytest.raises(KeyError):
        registry.update_content_owner(
            youtube_channel_id="missing-channel", content_owner_id="owner-x"
        )


def test_sql_channel_registry_update_inventory_persists_all_four_fields():
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)

    _previous, updated = registry.update_inventory(
        youtube_channel_id="channel-tv-a",
        channel_name="TV A Renamed",
        cms_status="INSIDE_CMS",
        content_owner_id="owner-cms",
        revenue_required=True,
    )

    persisted = session.scalars(
        select(YouTubeChannelORM).where(
            YouTubeChannelORM.tenant_id == DEFAULT_TENANT_ID,
            YouTubeChannelORM.youtube_channel_id == "channel-tv-a",
        )
    ).one()
    assert updated.channel_name == "TV A Renamed"
    assert updated.cms_status == "INSIDE_CMS"
    assert updated.content_owner_id == "owner-cms"
    assert updated.revenue_required is True
    assert persisted.channel_name == "TV A Renamed"
    assert persisted.cms_status == "INSIDE_CMS"
    assert persisted.content_owner_id == "owner-cms"
    assert persisted.revenue_required is True


def test_sql_channel_registry_update_inventory_flips_revenue_source_status():
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)

    _previous, updated = registry.update_inventory(
        youtube_channel_id="channel-tv-a",
        channel_name="TV A",
        cms_status="INSIDE_CMS",
        content_owner_id=None,
        revenue_required=False,
    )

    persisted = session.scalars(
        select(YouTubeChannelORM).where(
            YouTubeChannelORM.tenant_id == DEFAULT_TENANT_ID,
            YouTubeChannelORM.youtube_channel_id == "channel-tv-a",
        )
    ).one()
    assert updated.revenue_source_status == "PERFORMANCE_ONLY"
    assert persisted.revenue_source_status == "PERFORMANCE_ONLY"


def test_sql_channel_registry_update_inventory_preserves_official_status():
    """A name/owner refresh must not clobber a proven official revenue source."""
    session = build_session()
    seed_org(session)
    session.add(
        YouTubeChannelORM(
            id=UUID("00000000-0000-0000-0000-000000000304"),
            youtube_channel_id="channel-official-cms",
            channel_name="Official CMS",
            primary_org_unit_id=COMPANY_TV_ID,
            cms_status="INSIDE_CMS",
            revenue_required=True,
            revenue_source_status="OFFICIAL_CMS_REVENUE",
            active=True,
        )
    )
    session.commit()
    registry = SqlAlchemyChannelRegistry(session)

    _previous, updated = registry.update_inventory(
        youtube_channel_id="channel-official-cms",
        channel_name="Official CMS Renamed",
        cms_status="INSIDE_CMS",
        content_owner_id="owner-cms",
        revenue_required=True,
    )

    persisted = session.scalars(
        select(YouTubeChannelORM).where(
            YouTubeChannelORM.tenant_id == DEFAULT_TENANT_ID,
            YouTubeChannelORM.youtube_channel_id == "channel-official-cms",
        )
    ).one()
    assert updated.revenue_source_status == "OFFICIAL_CMS_REVENUE"
    assert persisted.revenue_source_status == "OFFICIAL_CMS_REVENUE"
    assert persisted.channel_name == "Official CMS Renamed"


def test_sql_channel_registry_lists_inactive_channels_when_asked():
    """include_inactive surfaces archived rows for import planning."""
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)

    default_lookup = registry.list_channels_by_ids({"channel-tv-a", "channel-inactive"})
    inclusive_lookup = registry.list_channels_by_ids(
        {"channel-tv-a", "channel-inactive"}, include_inactive=True
    )

    assert {channel.youtube_channel_id for channel in default_lookup} == {"channel-tv-a"}
    inclusive_by_id = {channel.youtube_channel_id: channel for channel in inclusive_lookup}
    assert set(inclusive_by_id) == {"channel-tv-a", "channel-inactive"}
    assert inclusive_by_id["channel-inactive"].active is False


def _seed_not_required_channel(session: Session) -> None:
    """Persist a performance-only channel the flip-guard tests mutate."""
    session.add(
        YouTubeChannelORM(
            id=UUID("00000000-0000-0000-0000-000000000305"),
            youtube_channel_id="channel-perf-only",
            channel_name="Perf Only",
            primary_org_unit_id=COMPANY_TV_ID,
            cms_status="INSIDE_CMS",
            revenue_required=False,
            revenue_source_status="PERFORMANCE_ONLY",
            active=True,
        )
    )
    session.commit()


def test_update_inventory_rejects_required_flip_when_locked_month_lacks_fact():
    """Enabling revenue_required must not retroactively break a LOCKED close."""
    session = build_finance_session()
    seed_org(session)
    _seed_not_required_channel(session)
    session.add(FinanceMonthCloseORM(month="2026-09", status="LOCKED"))
    session.commit()
    registry = SqlAlchemyChannelRegistry(session)

    with pytest.raises(ChannelRevenueRequirementLockedMonthError, match="2026-09"):
        registry.update_inventory(
            youtube_channel_id="channel-perf-only",
            channel_name="Perf Only",
            cms_status="INSIDE_CMS",
            content_owner_id=None,
            revenue_required=True,
        )

    persisted = session.scalars(
        select(YouTubeChannelORM).where(
            YouTubeChannelORM.tenant_id == DEFAULT_TENANT_ID,
            YouTubeChannelORM.youtube_channel_id == "channel-perf-only",
        )
    ).one()
    assert persisted.revenue_required is False
    assert persisted.revenue_source_status == "PERFORMANCE_ONLY"


def test_update_inventory_allows_required_flip_when_locked_month_has_fact():
    """A locked month with a fact for the channel does not block the flip."""
    session = build_finance_session()
    seed_org(session)
    session.add(FinanceMonthCloseORM(month="2026-09", status="LOCKED"))
    session.add(
        YouTubeChannelORM(
            id=UUID("00000000-0000-0000-0000-000000000306"),
            youtube_channel_id="channel-perf-flip",
            channel_name="Perf Flip",
            primary_org_unit_id=COMPANY_TV_ID,
            cms_status="INSIDE_CMS",
            revenue_required=False,
            revenue_source_status="PERFORMANCE_ONLY",
            active=True,
        )
    )
    session.commit()
    session.add(
        MonthlyChannelRevenueFactORM(
            id=uuid4(),
            month="2026-09",
            youtube_channel_id="channel-perf-flip",
            source_kind="YOUTUBE_CMS",
            gross_revenue_usd=100,
        )
    )
    session.commit()
    registry = SqlAlchemyChannelRegistry(session)

    _previous, updated = registry.update_inventory(
        youtube_channel_id="channel-perf-flip",
        channel_name="Perf Flip",
        cms_status="INSIDE_CMS",
        content_owner_id=None,
        revenue_required=True,
    )

    assert updated.revenue_required is True
    assert updated.revenue_source_status == "MISSING_REVENUE_SOURCE"


def test_update_inventory_returns_the_write_boundary_previous_entry():
    """The previous entry reflects what the write replaced, for audit diffs."""
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)

    previous, updated = registry.update_inventory(
        youtube_channel_id="channel-tv-a",
        channel_name="TV A Renamed",
        cms_status="INSIDE_CMS",
        content_owner_id="owner-cms",
        revenue_required=True,
    )

    assert previous.channel_name == "TV A"
    assert previous.content_owner_id is None
    assert updated.channel_name == "TV A Renamed"
    assert updated.content_owner_id == "owner-cms"


def test_update_inventory_flip_acquires_revenue_requirement_guard(monkeypatch):
    """The OFF->ON flip serializes with lock-time readiness via the shared key."""
    import ums_smart_revenue.org.sql_channel_registry as registry_module
    from ums_smart_revenue.finance.month_close_locks import (
        REVENUE_REQUIREMENT_GUARD_MONTH,
    )

    session = build_finance_session()
    seed_org(session)
    _seed_not_required_channel(session)
    guard_calls: list[str] = []
    monkeypatch.setattr(
        registry_module,
        "acquire_finance_month_advisory_lock",
        lambda _session, month, *, tenant_id=None: guard_calls.append(month),
    )
    registry = SqlAlchemyChannelRegistry(session)

    registry.update_inventory(
        youtube_channel_id="channel-perf-only",
        channel_name="Perf Only",
        cms_status="INSIDE_CMS",
        content_owner_id=None,
        revenue_required=True,
    )
    registry.update_inventory(
        youtube_channel_id="channel-tv-a",
        channel_name="TV A",
        cms_status="INSIDE_CMS",
        content_owner_id=None,
        revenue_required=True,  # already required: no flip, no guard
    )

    assert guard_calls == [REVENUE_REQUIREMENT_GUARD_MONTH]


def test_create_channel_revenue_required_acquires_revenue_requirement_guard(monkeypatch):
    """A revenue-required CREATE serializes with month locking like a flip does.

    Without the guard, a create committing inside a lock's readiness/commit
    window would carry created_at <= locked_at and retroactively fail the
    LOCKED month's effective-dated readiness (review #159 r3712948674).
    """
    import ums_smart_revenue.org.sql_channel_registry as registry_module
    from ums_smart_revenue.finance.month_close_locks import (
        REVENUE_REQUIREMENT_GUARD_MONTH,
    )

    session = build_finance_session()
    seed_org(session)
    guard_calls: list[str] = []
    monkeypatch.setattr(
        registry_module,
        "acquire_finance_month_advisory_lock",
        lambda _session, month, *, tenant_id=None: guard_calls.append(month),
    )
    registry = SqlAlchemyChannelRegistry(session)

    registry.create_channel(
        youtube_channel_id="channel-created-required",
        channel_name="Required Create",
        primary_company_id=None,
        cms_status="INSIDE_CMS",
        revenue_required=True,
    )
    registry.create_channel(
        youtube_channel_id="channel-created-perf",
        channel_name="Performance Create",
        primary_company_id=None,
        cms_status="INSIDE_CMS",
        revenue_required=False,  # performance-only: no readiness impact, no guard
    )

    assert guard_calls == [REVENUE_REQUIREMENT_GUARD_MONTH]


def test_update_inventory_allows_flip_when_lock_predates_the_channel():
    """A month locked BEFORE the channel existed never blocks the flip.

    Readiness effective-dates LOCKED months (created_at <= locked_at), so a
    pre-creation close can never evaluate this channel; demanding a fact for
    it would make a channel created after historical closes permanently
    un-flippable (review #159 r3713449090).
    """
    session = build_finance_session()
    seed_org(session)
    _seed_not_required_channel(session)
    session.add(
        FinanceMonthCloseORM(
            month="2020-01",
            status="LOCKED",
            locked_at=datetime(2020, 2, 1, tzinfo=UTC),  # long before the channel
        )
    )
    session.commit()
    registry = SqlAlchemyChannelRegistry(session)

    _previous, updated = registry.update_inventory(
        youtube_channel_id="channel-perf-only",
        channel_name="Perf Only",
        cms_status="INSIDE_CMS",
        content_owner_id=None,
        revenue_required=True,
    )

    assert updated.revenue_required is True
    assert updated.revenue_source_status == "MISSING_REVENUE_SOURCE"


def test_create_channel_revenue_required_stamps_created_at_after_the_guard(monkeypatch):
    """The guarded create's created_at is the post-guard app wall clock.

    The column's server default now() is the transaction START time, which
    predates any month lock the create waited on and would defeat the
    created_at <= locked_at readiness cutoff (review #159 r3713449080).
    """
    import ums_smart_revenue.org.sql_channel_registry as registry_module

    sentinel = datetime(2031, 5, 6, 12, 0, tzinfo=UTC)
    call_order: list[str] = []

    class _FixedDatetime:
        @classmethod
        def now(cls, tz=None):
            call_order.append("stamp")
            return sentinel

    session = build_finance_session()
    seed_org(session)
    monkeypatch.setattr(
        registry_module,
        "acquire_finance_month_advisory_lock",
        lambda _session, month, *, tenant_id=None: call_order.append("guard"),
    )
    monkeypatch.setattr(registry_module, "datetime", _FixedDatetime)
    registry = SqlAlchemyChannelRegistry(session)

    registry.create_channel(
        youtube_channel_id="channel-created-required",
        channel_name="Required Create",
        primary_company_id=None,
        cms_status="INSIDE_CMS",
        revenue_required=True,
    )

    assert call_order == ["guard", "stamp"]
    persisted = session.scalars(
        select(YouTubeChannelORM).where(
            YouTubeChannelORM.tenant_id == DEFAULT_TENANT_ID,
            YouTubeChannelORM.youtube_channel_id == "channel-created-required",
        )
    ).one()
    assert persisted.created_at.replace(tzinfo=None) == sentinel.replace(tzinfo=None)


def test_update_inventory_allows_unchanged_flag_despite_locked_month():
    """A locked month without facts only blocks the FLIP, not other updates."""
    session = build_finance_session()
    seed_org(session)
    _seed_not_required_channel(session)
    session.add(FinanceMonthCloseORM(month="2026-09", status="LOCKED"))
    session.commit()
    registry = SqlAlchemyChannelRegistry(session)

    _previous, updated = registry.update_inventory(
        youtube_channel_id="channel-perf-only",
        channel_name="Perf Only Renamed",
        cms_status="INSIDE_CMS",
        content_owner_id="owner-cms",
        revenue_required=False,
    )

    assert updated.channel_name == "Perf Only Renamed"
    assert updated.revenue_required is False


def test_sql_channel_registry_update_inventory_missing_channel_raises():
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)

    with pytest.raises(ChannelRegistryValidationError, match="Unknown channel"):
        registry.update_inventory(
            youtube_channel_id="missing-channel",
            channel_name="Nope",
            cms_status="INSIDE_CMS",
            content_owner_id=None,
            revenue_required=True,
        )


def test_setting_content_owner_makes_channel_targetable_for_ingestion():
    """Closed-loop regression for the silent-zero gap: a channel becomes an
    Analytics ingestion target only once its content_owner_id matches the CMS
    account id. Before the write path every channel kept content_owner_id=None,
    so list_target_channels returned nothing and a run reported SUCCEEDED while
    ingesting zero rows."""
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)

    # channel-tv-a is INSIDE_CMS, active, revenue_required, but content_owner_id
    # is unset -> not selected for the account.
    assert list_target_channels(session, tenant_id=DEFAULT_TENANT_ID, account_id="owner-cms") == []

    registry.update_content_owner(youtube_channel_id="channel-tv-a", content_owner_id="owner-cms")

    assert list_target_channels(session, tenant_id=DEFAULT_TENANT_ID, account_id="owner-cms") == [
        "channel-tv-a"
    ]


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

    assert lookups == 2


def test_sql_channel_registry_detects_duplicate_race_after_integrity_error(
    monkeypatch: pytest.MonkeyPatch,
):
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)
    lookups = 0

    def _get_row(channel_id: str):
        nonlocal lookups
        assert channel_id == "channel-racing-duplicate"
        lookups += 1
        return None if lookups == 1 else object()

    def _raise_late_duplicate():
        raise IntegrityError("insert youtube channel", {}, Exception("late insert"))

    monkeypatch.setattr(registry, "_get_row", _get_row)
    monkeypatch.setattr(session, "flush", _raise_late_duplicate)

    with pytest.raises(ChannelRegistryConflictError, match="Channel already exists"):
        registry.create_channel(
            youtube_channel_id="channel-racing-duplicate",
            channel_name="Racing Duplicate",
            primary_company_id=None,
            cms_status="UNKNOWN",
            revenue_required=False,
        )

    assert lookups == 2


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
            YouTubeChannelORM.youtube_channel_id == "channel-tv-a",
        )
    ).one()
    assert persisted.primary_org_unit_id == COMPANY_TV_ID


def test_sql_channel_registry_filters_every_read_to_default_tenant():
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)

    channels_by_id = registry.list_channels_by_ids({"channel-tv-a", OTHER_TENANT_CHANNEL_ID})

    assert registry.get_channel(OTHER_TENANT_CHANNEL_ID) is None
    assert {channel.youtube_channel_id for channel in registry.list_channels()} == {"channel-tv-a"}
    assert {channel.youtube_channel_id for channel in channels_by_id} == {"channel-tv-a"}


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
    assert {channel.youtube_channel_id for channel in default_registry.list_channels()} == {
        "channel-tv-a"
    }
    assert {channel.youtube_channel_id for channel in other_registry.list_channels()} == {
        "channel-other-tenant-created",
        OTHER_TENANT_CHANNEL_ID,
    }


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
    token = TENANT_CTX.set(_tenant(DEFAULT_TENANT_ID, slug="ums"))
    try:
        index = load_org_access_index_from_session(session)
    finally:
        TENANT_CTX.reset(token)

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


def test_load_org_access_index_from_session_requires_tenant_context():
    session = build_session()
    seed_org(session)

    with pytest.raises(TenantContextMissing):
        load_org_access_index_from_session(session)


def _seed_channel_fact(session: Session, *, month: str) -> None:
    """Persist a single revenue fact for channel-tv-a in the given month."""
    session.add(
        MonthlyChannelRevenueFactORM(
            id=uuid4(),
            month=month,
            youtube_channel_id="channel-tv-a",
            source_kind="YOUTUBE_CMS",
            gross_revenue_usd=100,
        )
    )
    session.commit()


def test_update_mapping_rejected_when_channel_has_locked_month_fact():
    session = build_finance_session()
    seed_org(session)
    session.add(FinanceMonthCloseORM(month="2026-09", status="LOCKED"))
    _seed_channel_fact(session, month="2026-09")
    registry = SqlAlchemyChannelRegistry(session)

    with pytest.raises(ChannelMappingLockedMonthError, match="2026-09"):
        registry.update_mapping(
            youtube_channel_id="channel-tv-a",
            primary_company_id=str(COMPANY_NEWS_ID),
        )

    persisted = session.scalars(
        select(YouTubeChannelORM).where(
            YouTubeChannelORM.tenant_id == DEFAULT_TENANT_ID,
            YouTubeChannelORM.youtube_channel_id == "channel-tv-a",
        )
    ).one()
    assert persisted.primary_org_unit_id == COMPANY_TV_ID


def test_update_mapping_allowed_when_channel_only_has_open_month_fact():
    session = build_finance_session()
    seed_org(session)
    session.add(FinanceMonthCloseORM(month="2026-09", status="OPEN"))
    _seed_channel_fact(session, month="2026-09")
    registry = SqlAlchemyChannelRegistry(session)

    updated = registry.update_mapping(
        youtube_channel_id="channel-tv-a",
        primary_company_id=str(COMPANY_NEWS_ID),
    )

    assert updated.primary_company_id == str(COMPANY_NEWS_ID)
    persisted = session.scalars(
        select(YouTubeChannelORM).where(
            YouTubeChannelORM.tenant_id == DEFAULT_TENANT_ID,
            YouTubeChannelORM.youtube_channel_id == "channel-tv-a",
        )
    ).one()
    assert persisted.primary_org_unit_id == COMPANY_NEWS_ID


def test_update_mapping_no_op_returns_entry_without_locked_month_check():
    """Idempotent / no-op PATCH must not raise on a channel with LOCKED-month facts.

    The guard is meant to prevent re-parenting that would rewrite a closed
    month's attribution; a request that doesn't change the mapping cannot
    rewrite anything and must short-circuit BEFORE the locked-month lookup.
    """
    session = build_finance_session()
    seed_org(session)
    session.add(FinanceMonthCloseORM(month="2026-09", status="LOCKED"))
    _seed_channel_fact(session, month="2026-09")
    registry = SqlAlchemyChannelRegistry(session)

    updated = registry.update_mapping(
        youtube_channel_id="channel-tv-a",
        primary_company_id=str(COMPANY_TV_ID),  # matches existing primary
    )

    assert updated.primary_company_id == str(COMPANY_TV_ID)
    persisted = session.scalars(
        select(YouTubeChannelORM).where(
            YouTubeChannelORM.tenant_id == DEFAULT_TENANT_ID,
            YouTubeChannelORM.youtube_channel_id == "channel-tv-a",
        )
    ).one()
    assert persisted.primary_org_unit_id == COMPANY_TV_ID


def test_update_mapping_no_op_skips_locked_month_query():
    """No-op PATCH must not hit the finance schema; an org-only session works."""
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelRegistry(session)

    # The locked-month guard would raise OperationalError on this session
    # (no finance schema); the no-op short-circuit must return the entry
    # without ever touching the finance tables.
    updated = registry.update_mapping(
        youtube_channel_id="channel-tv-a",
        primary_company_id=str(COMPANY_TV_ID),  # matches existing primary
    )

    assert updated.primary_company_id == str(COMPANY_TV_ID)
