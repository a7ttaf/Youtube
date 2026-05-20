"""Direct registry-layer tests for ``SqlAlchemyChannelGroupRegistry``.

These tests prove that ``SqlAlchemyChannelGroupRegistry``:

- stamps the resolved ``tenant_id`` on every inserted group row and on
  every inserted member row (including the IntegrityError recovery path
  in ``add_members`` and the rewrite path in ``_replace_member_rows``),
- filters every read (``list_groups``, ``get_group``, ``_channel_ids_by_group``,
  ``_channel_rows_by_external_ids``) to the resolved ``tenant_id``,
- closes IDOR via ``_get_group_row`` — a cross-tenant ``group_id`` lookup
  returns ``None`` even though ``session.get`` would otherwise resolve it,
- honours the documented precedence on writes: explicit constructor
  argument > ambient ``TENANT_CTX`` > UMS default tenant,
- raises ``KeyError`` on ``update_group``, ``add_members``, and
  ``remove_member`` when the supplied ``group_id`` belongs to another
  tenant (i.e. when ``_require_group_row`` rejects), and
- rejects cross-tenant channel ids in ``_channel_rows_by_external_ids``
  via a ``KeyError`` (the per-tenant external channel id filter).

The plan calls these out as the new-test set for the
``backend/ums_smart_revenue/org/sql_channel_groups.py`` repository that
PR #25 (S2.4b-org-1) wired up alongside the channel registry.

The companion suite in ``tests/org/test_sql_channel_registry.py`` covers
the same shape for ``SqlAlchemyChannelRegistry`` and is the structural
pattern this file follows. The shared
``test_org_sql_repositories_validate_tenant_id_constructor_input`` test
in that file already exercises the constructor validation, so this file
does not re-add that case.
"""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.org_models import (
    ChannelGroupMemberORM,
    ChannelGroupORM,
    OrgBase,
    OrgUnitORM,
    YouTubeChannelORM,
)
from ums_smart_revenue.org.sql_channel_groups import SqlAlchemyChannelGroupRegistry
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

DEFAULT_TENANT_ID = UUID(UMS_TENANT_ID)
OTHER_TENANT_ID = UUID("00000000-0000-0000-0000-000000051999")
SECTOR_DEFAULT_ID = UUID("00000000-0000-0000-0000-000000050101")
COMPANY_DEFAULT_ID = UUID("00000000-0000-0000-0000-000000050201")
SECTOR_OTHER_ID = UUID("00000000-0000-0000-0000-000000050901")
COMPANY_OTHER_ID = UUID("00000000-0000-0000-0000-000000050902")
CHANNEL_DEFAULT_A_ID = UUID("00000000-0000-0000-0000-000000050301")
CHANNEL_DEFAULT_B_ID = UUID("00000000-0000-0000-0000-000000050302")
CHANNEL_INACTIVE_ID = UUID("00000000-0000-0000-0000-000000050303")
CHANNEL_OTHER_ID = UUID("00000000-0000-0000-0000-000000050903")
CHANNEL_OTHER_B_ID = UUID("00000000-0000-0000-0000-000000050904")
CHANNEL_DEFAULT_A_EXTERNAL = "channel-tv-a"
CHANNEL_DEFAULT_B_EXTERNAL = "channel-tv-b"
CHANNEL_INACTIVE_EXTERNAL = "channel-inactive"
CHANNEL_OTHER_EXTERNAL = "channel-other-tenant"
CHANNEL_OTHER_B_EXTERNAL = "channel-other-tenant-b"
CREATED_AT = datetime(2026, 4, 21, 9, 0, tzinfo=UTC)


def build_session() -> Session:
    """Create an isolated in-memory org schema with foreign-key checks on."""
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, connection_record) -> None:
        del connection_record
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    OrgBase.metadata.create_all(engine)
    return Session(engine)


def seed_org(session: Session) -> None:
    """Seed two tenants with parallel sector/company/channel rows."""
    session.add_all(
        [
            OrgUnitORM(
                id=SECTOR_DEFAULT_ID,
                parent_id=None,
                type="SECTOR",
                name="TV",
                active=True,
            ),
            OrgUnitORM(
                id=COMPANY_DEFAULT_ID,
                parent_id=SECTOR_DEFAULT_ID,
                type="COMPANY",
                name="TV Company",
                active=True,
            ),
            OrgUnitORM(
                id=SECTOR_OTHER_ID,
                tenant_id=OTHER_TENANT_ID,
                parent_id=None,
                type="SECTOR",
                name="Other Tenant Sector",
                active=True,
            ),
            OrgUnitORM(
                id=COMPANY_OTHER_ID,
                tenant_id=OTHER_TENANT_ID,
                parent_id=SECTOR_OTHER_ID,
                type="COMPANY",
                name="Other Tenant Company",
                active=True,
            ),
            YouTubeChannelORM(
                id=CHANNEL_DEFAULT_A_ID,
                youtube_channel_id=CHANNEL_DEFAULT_A_EXTERNAL,
                channel_name="TV A",
                primary_org_unit_id=COMPANY_DEFAULT_ID,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                active=True,
            ),
            YouTubeChannelORM(
                id=CHANNEL_DEFAULT_B_ID,
                youtube_channel_id=CHANNEL_DEFAULT_B_EXTERNAL,
                channel_name="TV B",
                primary_org_unit_id=COMPANY_DEFAULT_ID,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                active=True,
            ),
            YouTubeChannelORM(
                id=CHANNEL_INACTIVE_ID,
                youtube_channel_id=CHANNEL_INACTIVE_EXTERNAL,
                channel_name="Inactive",
                primary_org_unit_id=COMPANY_DEFAULT_ID,
                cms_status="INSIDE_CMS",
                revenue_required=False,
                active=False,
            ),
            YouTubeChannelORM(
                id=CHANNEL_OTHER_ID,
                tenant_id=OTHER_TENANT_ID,
                youtube_channel_id=CHANNEL_OTHER_EXTERNAL,
                channel_name="Other Tenant Channel",
                primary_org_unit_id=COMPANY_OTHER_ID,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                active=True,
            ),
            YouTubeChannelORM(
                id=CHANNEL_OTHER_B_ID,
                tenant_id=OTHER_TENANT_ID,
                youtube_channel_id=CHANNEL_OTHER_B_EXTERNAL,
                channel_name="Other Tenant Channel B",
                primary_org_unit_id=COMPANY_OTHER_ID,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                active=True,
            ),
        ]
    )
    session.commit()


def insert_member_row_bypassing_fk(
    session: Session,
    *,
    tenant_id: UUID,
    group_id: UUID,
    channel_id: UUID,
) -> None:
    """Insert inconsistent fixture data that normal FK checks would reject."""
    session.commit()
    bind = session.get_bind()
    with bind.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            connection.execute(
                ChannelGroupMemberORM.__table__.insert().values(
                    tenant_id=tenant_id,
                    group_id=group_id,
                    channel_id=channel_id,
                )
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def _tenant(tenant_id: UUID, *, slug: str) -> Tenant:
    """Build an active tenant context object for request-scope assertions."""
    return Tenant(
        id=tenant_id,
        slug=slug,
        display_name=f"{slug.title()} Tenant",
        primary_currency="USD",
        status=TenantStatus.ACTIVE,
        onboarding_at=CREATED_AT,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


# ---------------------------------------------------------------------------
# create_group / writes — tenant stamping + precedence
# ---------------------------------------------------------------------------


def test_create_group_stamps_default_tenant_on_group_and_members() -> None:
    """Bootstrap callers stamp the UMS default tenant on the group + members."""
    session = build_session()
    seed_org(session)

    group = SqlAlchemyChannelGroupRegistry(session).create_group(
        name="TV Holding",
        group_type="HOLDING",
        channel_ids=[CHANNEL_DEFAULT_A_EXTERNAL, CHANNEL_DEFAULT_B_EXTERNAL],
    )

    group_row = session.scalars(
        select(ChannelGroupORM).where(ChannelGroupORM.name == "TV Holding")
    ).one()
    member_tenant_ids = set(
        session.scalars(
            select(ChannelGroupMemberORM.tenant_id).where(
                ChannelGroupMemberORM.group_id == group_row.id
            )
        ).all()
    )

    assert group_row.tenant_id == DEFAULT_TENANT_ID
    assert member_tenant_ids == {DEFAULT_TENANT_ID}
    assert set(group.channel_ids) == {
        CHANNEL_DEFAULT_A_EXTERNAL,
        CHANNEL_DEFAULT_B_EXTERNAL,
    }


def test_create_group_stamps_explicit_constructor_tenant() -> None:
    """Explicit constructor tenant ids are stamped onto group + member rows."""
    session = build_session()
    seed_org(session)

    SqlAlchemyChannelGroupRegistry(session, tenant_id=OTHER_TENANT_ID).create_group(
        name="Other Tenant Group",
        group_type="CUSTOM_GROUP",
        channel_ids=[CHANNEL_OTHER_EXTERNAL],
    )

    group_row = session.scalars(
        select(ChannelGroupORM).where(ChannelGroupORM.name == "Other Tenant Group")
    ).one()
    member_tenant_ids = set(
        session.scalars(
            select(ChannelGroupMemberORM.tenant_id).where(
                ChannelGroupMemberORM.group_id == group_row.id
            )
        ).all()
    )

    assert group_row.tenant_id == OTHER_TENANT_ID
    assert member_tenant_ids == {OTHER_TENANT_ID}


def test_create_group_uses_request_tenant_context_by_default() -> None:
    """Ambient TENANT_CTX scopes writes when no explicit constructor arg is given."""
    session = build_session()
    seed_org(session)
    token = TENANT_CTX.set(_tenant(OTHER_TENANT_ID, slug="other"))
    try:
        SqlAlchemyChannelGroupRegistry(session).create_group(
            name="Ctx Tenant Group",
            group_type="CUSTOM_GROUP",
            channel_ids=[CHANNEL_OTHER_EXTERNAL],
        )
    finally:
        TENANT_CTX.reset(token)

    group_row = session.scalars(
        select(ChannelGroupORM).where(ChannelGroupORM.name == "Ctx Tenant Group")
    ).one()
    assert group_row.tenant_id == OTHER_TENANT_ID


def test_create_group_explicit_tenant_overrides_request_context() -> None:
    """Constructor tenant ids beat any ambient request context on writes."""
    session = build_session()
    seed_org(session)
    token = TENANT_CTX.set(_tenant(OTHER_TENANT_ID, slug="other"))
    try:
        SqlAlchemyChannelGroupRegistry(
            session, tenant_id=DEFAULT_TENANT_ID
        ).create_group(
            name="Explicit Wins Group",
            group_type="CUSTOM_GROUP",
            channel_ids=[CHANNEL_DEFAULT_A_EXTERNAL],
        )
    finally:
        TENANT_CTX.reset(token)

    group_row = session.scalars(
        select(ChannelGroupORM).where(ChannelGroupORM.name == "Explicit Wins Group")
    ).one()
    assert group_row.tenant_id == DEFAULT_TENANT_ID


def test_create_group_rejects_cross_tenant_channel_external_id() -> None:
    """`_channel_rows_by_external_ids` filters channels to the bound tenant."""
    session = build_session()
    seed_org(session)

    registry = SqlAlchemyChannelGroupRegistry(session)

    with pytest.raises(KeyError, match=CHANNEL_OTHER_EXTERNAL):
        registry.create_group(
            name="Cross Tenant Group",
            group_type="CUSTOM_GROUP",
            channel_ids=[CHANNEL_OTHER_EXTERNAL],
        )


def test_create_group_allows_empty_channel_id_list_without_member_rows() -> None:
    """Empty create_group input creates a tenant-stamped group and no members."""
    session = build_session()
    seed_org(session)

    group = SqlAlchemyChannelGroupRegistry(session).create_group(
        name="Empty Group",
        group_type="CUSTOM_GROUP",
        channel_ids=[],
    )

    group_row = session.scalars(
        select(ChannelGroupORM).where(ChannelGroupORM.name == "Empty Group")
    ).one()
    member_rows = session.scalars(
        select(ChannelGroupMemberORM).where(
            ChannelGroupMemberORM.group_id == group_row.id
        )
    ).all()

    assert group.channel_ids == ()
    assert group_row.tenant_id == DEFAULT_TENANT_ID
    assert member_rows == []


# ---------------------------------------------------------------------------
# list_groups / reads
# ---------------------------------------------------------------------------


def test_list_groups_filters_to_bound_tenant_only() -> None:
    """list_groups never surfaces another tenant's groups."""
    session = build_session()
    seed_org(session)

    SqlAlchemyChannelGroupRegistry(session).create_group(
        name="Default Group",
        group_type="CUSTOM_GROUP",
        channel_ids=[CHANNEL_DEFAULT_A_EXTERNAL],
    )
    SqlAlchemyChannelGroupRegistry(session, tenant_id=OTHER_TENANT_ID).create_group(
        name="Other Group",
        group_type="CUSTOM_GROUP",
        channel_ids=[CHANNEL_OTHER_EXTERNAL],
    )

    default_names = [
        entry.name for entry in SqlAlchemyChannelGroupRegistry(session).list_groups()
    ]
    other_names = [
        entry.name
        for entry in SqlAlchemyChannelGroupRegistry(
            session, tenant_id=OTHER_TENANT_ID
        ).list_groups()
    ]

    assert default_names == ["Default Group"]
    assert other_names == ["Other Group"]


def test_list_groups_excludes_inactive_groups() -> None:
    """Inactive groups never appear in list_groups output."""
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelGroupRegistry(session)
    registry.create_group(
        name="Active Group",
        group_type="CUSTOM_GROUP",
        channel_ids=[CHANNEL_DEFAULT_A_EXTERNAL],
    )
    inactive_group = registry.create_group(
        name="Inactive Group",
        group_type="CUSTOM_GROUP",
        channel_ids=[CHANNEL_DEFAULT_B_EXTERNAL],
    )
    registry.update_group(group_id=inactive_group.id, name=None, active=False)

    assert [entry.name for entry in registry.list_groups()] == ["Active Group"]


def test_list_groups_uses_request_tenant_context_by_default() -> None:
    """Ambient TENANT_CTX scopes reads when no explicit constructor arg is given."""
    session = build_session()
    seed_org(session)
    SqlAlchemyChannelGroupRegistry(session).create_group(
        name="Default Group",
        group_type="CUSTOM_GROUP",
        channel_ids=[CHANNEL_DEFAULT_A_EXTERNAL],
    )
    SqlAlchemyChannelGroupRegistry(session, tenant_id=OTHER_TENANT_ID).create_group(
        name="Other Group",
        group_type="CUSTOM_GROUP",
        channel_ids=[CHANNEL_OTHER_EXTERNAL],
    )

    token = TENANT_CTX.set(_tenant(OTHER_TENANT_ID, slug="other"))
    try:
        names = [
            entry.name
            for entry in SqlAlchemyChannelGroupRegistry(session).list_groups()
        ]
    finally:
        TENANT_CTX.reset(token)

    assert names == ["Other Group"]


# ---------------------------------------------------------------------------
# get_group / IDOR
# ---------------------------------------------------------------------------


def test_get_group_returns_none_for_cross_tenant_group_id() -> None:
    """Cross-tenant group ids resolve to None, closing the IDOR vector."""
    session = build_session()
    seed_org(session)

    other_group = SqlAlchemyChannelGroupRegistry(
        session, tenant_id=OTHER_TENANT_ID
    ).create_group(
        name="Other Group",
        group_type="CUSTOM_GROUP",
        channel_ids=[CHANNEL_OTHER_EXTERNAL],
    )

    default_registry = SqlAlchemyChannelGroupRegistry(session)

    assert default_registry.get_group(other_group.id) is None


def test_get_group_returns_none_for_malformed_uuid_string() -> None:
    """`_get_group_row`'s try/except guards malformed group_id input."""
    session = build_session()
    seed_org(session)

    assert SqlAlchemyChannelGroupRegistry(session).get_group("not-a-uuid") is None


# ---------------------------------------------------------------------------
# add_members / remove_member / update_group cross-tenant rejection
# ---------------------------------------------------------------------------


def test_add_members_for_cross_tenant_group_raises_keyerror() -> None:
    """`_require_group_row` rejects add_members on another tenant's group."""
    session = build_session()
    seed_org(session)

    other_group = SqlAlchemyChannelGroupRegistry(
        session, tenant_id=OTHER_TENANT_ID
    ).create_group(
        name="Other Group",
        group_type="CUSTOM_GROUP",
        channel_ids=[CHANNEL_OTHER_EXTERNAL],
    )

    default_registry = SqlAlchemyChannelGroupRegistry(session)
    with pytest.raises(KeyError, match=other_group.id):
        default_registry.add_members(
            group_id=other_group.id,
            channel_ids=[CHANNEL_DEFAULT_A_EXTERNAL],
        )

    no_default_member_for_other_group = session.scalars(
        select(ChannelGroupMemberORM).where(
            ChannelGroupMemberORM.group_id == UUID(other_group.id),
            ChannelGroupMemberORM.tenant_id == DEFAULT_TENANT_ID,
        )
    ).all()
    assert no_default_member_for_other_group == []


def test_add_members_stamps_bound_tenant_on_new_member_rows() -> None:
    """add_members stamps the bound tenant_id on every newly inserted row."""
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelGroupRegistry(session)
    group = registry.create_group(
        name="Default Group",
        group_type="CUSTOM_GROUP",
        channel_ids=[CHANNEL_DEFAULT_A_EXTERNAL],
    )

    registry.add_members(
        group_id=group.id,
        channel_ids=[CHANNEL_DEFAULT_B_EXTERNAL],
    )

    member_rows = session.scalars(
        select(ChannelGroupMemberORM).where(
            ChannelGroupMemberORM.group_id == UUID(group.id)
        )
    ).all()
    assert {row.tenant_id for row in member_rows} == {DEFAULT_TENANT_ID}
    assert {row.channel_id for row in member_rows} == {
        CHANNEL_DEFAULT_A_ID,
        CHANNEL_DEFAULT_B_ID,
    }


def test_add_members_with_empty_channel_id_list_keeps_existing_members() -> None:
    """Empty add_members input is a no-op on the existing member set."""
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelGroupRegistry(session)
    group = registry.create_group(
        name="Default Group",
        group_type="CUSTOM_GROUP",
        channel_ids=[CHANNEL_DEFAULT_A_EXTERNAL],
    )

    updated = registry.add_members(group_id=group.id, channel_ids=[])

    assert updated.channel_ids == (CHANNEL_DEFAULT_A_EXTERNAL,)
    member_rows = session.scalars(
        select(ChannelGroupMemberORM).where(
            ChannelGroupMemberORM.group_id == UUID(group.id)
        )
    ).all()
    assert len(member_rows) == 1
    assert member_rows[0].tenant_id == DEFAULT_TENANT_ID
    assert member_rows[0].channel_id == CHANNEL_DEFAULT_A_ID


def test_add_members_recovers_from_duplicate_member_integrity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The duplicate-member retry path preserves tenant stamping."""
    session = build_session()
    seed_org(session)
    registry = SqlAlchemyChannelGroupRegistry(session)
    group = registry.create_group(
        name="Default Group",
        group_type="CUSTOM_GROUP",
        channel_ids=[CHANNEL_DEFAULT_A_EXTERNAL],
    )

    original_flush = session.flush
    duplicate_error_seen = False

    def flaky_flush(*args, **kwargs):
        nonlocal duplicate_error_seen
        has_pending_new_member = any(
            isinstance(obj, ChannelGroupMemberORM)
            and obj.channel_id == CHANNEL_DEFAULT_B_ID
            for obj in session.new
        )
        if has_pending_new_member and not duplicate_error_seen:
            duplicate_error_seen = True
            raise IntegrityError(
                "INSERT INTO channel_group_members",
                {},
                Exception(
                    "UNIQUE constraint failed: "
                    "channel_group_members.group_id, "
                    "channel_group_members.channel_id"
                ),
            )
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(session, "flush", flaky_flush)

    updated = registry.add_members(
        group_id=group.id,
        channel_ids=[CHANNEL_DEFAULT_B_EXTERNAL],
    )

    member_rows = session.scalars(
        select(ChannelGroupMemberORM).where(
            ChannelGroupMemberORM.group_id == UUID(group.id)
        )
    ).all()

    assert duplicate_error_seen is True
    assert updated.channel_ids == (
        CHANNEL_DEFAULT_A_EXTERNAL,
        CHANNEL_DEFAULT_B_EXTERNAL,
    )
    assert {row.tenant_id for row in member_rows} == {DEFAULT_TENANT_ID}
    assert {row.channel_id for row in member_rows} == {
        CHANNEL_DEFAULT_A_ID,
        CHANNEL_DEFAULT_B_ID,
    }


def test_remove_member_for_cross_tenant_group_raises_keyerror() -> None:
    """`_require_group_row` rejects remove_member on another tenant's group."""
    session = build_session()
    seed_org(session)

    other_group = SqlAlchemyChannelGroupRegistry(
        session, tenant_id=OTHER_TENANT_ID
    ).create_group(
        name="Other Group",
        group_type="CUSTOM_GROUP",
        channel_ids=[CHANNEL_OTHER_EXTERNAL],
    )

    default_registry = SqlAlchemyChannelGroupRegistry(session)
    with pytest.raises(KeyError, match=other_group.id):
        default_registry.remove_member(
            group_id=other_group.id,
            channel_id=CHANNEL_OTHER_EXTERNAL,
        )

    surviving = session.scalars(
        select(ChannelGroupMemberORM).where(
            ChannelGroupMemberORM.group_id == UUID(other_group.id),
        )
    ).all()
    assert len(surviving) == 1
    assert surviving[0].tenant_id == OTHER_TENANT_ID


def test_update_group_for_cross_tenant_group_raises_keyerror() -> None:
    """`_require_group_row` rejects update_group on another tenant's group."""
    session = build_session()
    seed_org(session)

    other_group = SqlAlchemyChannelGroupRegistry(
        session, tenant_id=OTHER_TENANT_ID
    ).create_group(
        name="Other Group",
        group_type="CUSTOM_GROUP",
        channel_ids=[CHANNEL_OTHER_EXTERNAL],
    )

    default_registry = SqlAlchemyChannelGroupRegistry(session)
    with pytest.raises(KeyError, match=other_group.id):
        default_registry.update_group(
            group_id=other_group.id,
            name="Hijacked Name",
            active=None,
        )

    other_group_after = SqlAlchemyChannelGroupRegistry(
        session, tenant_id=OTHER_TENANT_ID
    ).get_group(other_group.id)
    assert other_group_after is not None
    assert other_group_after.name == "Other Group"
    assert other_group_after.active is True


# ---------------------------------------------------------------------------
# read-layer composite filter
# ---------------------------------------------------------------------------


def test_channel_ids_by_group_dual_filter_excludes_cross_tenant_member_rows() -> None:
    """`_channel_ids_by_group` filters member rows on both tenant_id columns."""
    session = build_session()
    seed_org(session)

    default_registry = SqlAlchemyChannelGroupRegistry(session)
    default_group = default_registry.create_group(
        name="Default Group",
        group_type="CUSTOM_GROUP",
        channel_ids=[CHANNEL_DEFAULT_A_EXTERNAL],
    )
    default_group_uuid = UUID(default_group.id)

    other_registry = SqlAlchemyChannelGroupRegistry(session, tenant_id=OTHER_TENANT_ID)
    other_registry.create_group(
        name="Other Group",
        group_type="CUSTOM_GROUP",
        channel_ids=[CHANNEL_OTHER_EXTERNAL],
    )
    # This row is internally consistent for the other tenant and would leak if
    # the member-row tenant predicate were removed.
    insert_member_row_bypassing_fk(
        session,
        tenant_id=OTHER_TENANT_ID,
        group_id=default_group_uuid,
        channel_id=CHANNEL_OTHER_ID,
    )
    # This row has the bound member tenant and a foreign channel tenant; it
    # would leak if the member/channel tenant join predicate were removed.
    insert_member_row_bypassing_fk(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        group_id=default_group_uuid,
        channel_id=CHANNEL_OTHER_B_ID,
    )

    without_member_tenant_filter = session.execute(
        select(YouTubeChannelORM.youtube_channel_id)
        .join(
            ChannelGroupMemberORM,
            (ChannelGroupMemberORM.tenant_id == YouTubeChannelORM.tenant_id)
            & (ChannelGroupMemberORM.channel_id == YouTubeChannelORM.id),
        )
        .where(ChannelGroupMemberORM.group_id == default_group_uuid)
    ).all()
    without_channel_tenant_join = session.execute(
        select(YouTubeChannelORM.youtube_channel_id)
        .join(
            ChannelGroupMemberORM,
            ChannelGroupMemberORM.channel_id == YouTubeChannelORM.id,
        )
        .where(
            ChannelGroupMemberORM.tenant_id == DEFAULT_TENANT_ID,
            ChannelGroupMemberORM.group_id == default_group_uuid,
        )
    ).all()
    assert (CHANNEL_OTHER_EXTERNAL,) in without_member_tenant_filter
    assert (CHANNEL_OTHER_B_EXTERNAL,) in without_channel_tenant_join

    default_view = default_registry._channel_ids_by_group([default_group_uuid])
    assert default_view == {default_group_uuid: (CHANNEL_DEFAULT_A_EXTERNAL,)}

    other_group_uuid = session.scalars(
        select(ChannelGroupORM.id).where(ChannelGroupORM.tenant_id == OTHER_TENANT_ID)
    ).one()
    other_view_from_default = default_registry._channel_ids_by_group([other_group_uuid])
    assert other_view_from_default == {}
