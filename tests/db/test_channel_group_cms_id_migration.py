"""Schema guard for the additive channel_groups.cms_group_id column."""

from ums_smart_revenue.db.org_models import ChannelGroupORM


def test_channel_group_orm_exposes_cms_group_id() -> None:
    column = ChannelGroupORM.__table__.columns["cms_group_id"]
    assert column.nullable is True


def test_channel_group_cms_id_is_unique_per_tenant() -> None:
    constraints = {
        tuple(column.name for column in constraint.columns)
        for constraint in ChannelGroupORM.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("tenant_id", "cms_group_id") in constraints
