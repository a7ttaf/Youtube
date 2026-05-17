from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID


class OrgBase(DeclarativeBase):
    pass


# Shared server_default and Python-side default for the tenant_id column added
# in migration 20260517_0001. See security_models.py for the rationale.
# Both required so new ORM instances carry the UUID before flush.
_TENANT_ID_DEFAULT = text(f"'{UMS_TENANT_ID}'")
_TENANT_ID_DEFAULT_VALUE = UUID(UMS_TENANT_ID)


class OrgUnitORM(OrgBase):
    __tablename__ = "org_units"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    parent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("org_units.id", ondelete="RESTRICT"), nullable=True
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE,
        server_default=_TENANT_ID_DEFAULT,
    )

    __table_args__ = (
        CheckConstraint(
            "type IN ('HOLDING', 'SECTOR', 'COMPANY')", name="ck_org_units_type"
        ),
        Index("ix_org_units_parent_id", "parent_id"),
        Index("ix_org_units_tenant_id", "tenant_id"),
    )


class YouTubeChannelORM(OrgBase):
    __tablename__ = "youtube_channels"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    youtube_channel_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    channel_name: Mapped[str] = mapped_column(Text, nullable=False)
    primary_org_unit_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("org_units.id", ondelete="RESTRICT"),
        nullable=True,
    )
    cms_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'UNKNOWN'")
    )
    content_owner_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    revenue_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    revenue_source_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'MISSING_REVENUE_SOURCE'")
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE,
        server_default=_TENANT_ID_DEFAULT,
    )

    __table_args__ = (
        CheckConstraint(
            "cms_status IN ('INSIDE_CMS', 'OUTSIDE_CMS', 'UNKNOWN')",
            name="ck_youtube_channels_cms_status",
        ),
        CheckConstraint(
            "revenue_source_status IN ("
            "'OFFICIAL_CMS_REVENUE', "
            "'OFFICIAL_MANUAL_IMPORT', "
            "'ALLOCATED_FROM_PAYMENT_POOL', "
            "'PERFORMANCE_ONLY', "
            "'MISSING_REVENUE_SOURCE'"
            ")",
            name="ck_youtube_channels_revenue_source_status",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_youtube_channels_tenant_id_id",
        ),
        Index("ix_youtube_channels_primary_org_unit_id", "primary_org_unit_id"),
        Index("ix_youtube_channels_tenant_id", "tenant_id"),
    )


class ChannelGroupORM(OrgBase):
    __tablename__ = "channel_groups"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    group_type: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE,
        server_default=_TENANT_ID_DEFAULT,
    )

    __table_args__ = (
        CheckConstraint(
            "group_type IN ("
            "'HOLDING', 'SECTOR', 'COMPANY', 'TV_BRAND', 'NEWS_BRAND', "
            "'CUSTOM_GROUP', 'FINANCE_GROUP', 'SEASONAL_GROUP'"
            ")",
            name="ck_channel_groups_group_type",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_channel_groups_tenant_id_id",
        ),
        Index("ix_channel_groups_tenant_id", "tenant_id"),
    )


class ChannelGroupMemberORM(OrgBase):
    __tablename__ = "channel_group_members"

    group_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
    )
    channel_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        default=_TENANT_ID_DEFAULT_VALUE,
        server_default=_TENANT_ID_DEFAULT,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "group_id"],
            ["channel_groups.tenant_id", "channel_groups.id"],
            name="fk_channel_group_members_tenant_group",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "channel_id"],
            ["youtube_channels.tenant_id", "youtube_channels.id"],
            name="fk_channel_group_members_tenant_channel",
            ondelete="CASCADE",
        ),
        Index("ix_channel_group_members_tenant_id", "tenant_id"),
    )
