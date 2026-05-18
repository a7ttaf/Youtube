from ums_smart_revenue.db.org_models import OrgBase


def test_org_registry_metadata_contains_required_tables():
    assert set(OrgBase.metadata.tables) >= {
        "org_units",
        "youtube_channels",
        "channel_groups",
        "channel_group_members",
    }


def test_youtube_channel_model_has_mapping_and_revenue_source_columns():
    table = OrgBase.metadata.tables["youtube_channels"]

    assert {
        "youtube_channel_id",
        "channel_name",
        "primary_org_unit_id",
        "cms_status",
        "revenue_required",
        "revenue_source_status",
        "active",
    } <= set(table.columns.keys())


def test_youtube_channel_identity_is_unique_per_tenant():
    table = OrgBase.metadata.tables["youtube_channels"]
    unique_constraints = {
        constraint.name: [column.name for column in constraint.columns]
        for constraint in table.constraints
        if constraint.name
    }

    assert unique_constraints["uq_youtube_channels_tenant_channel_id"] == [
        "tenant_id",
        "youtube_channel_id",
    ]
    assert not table.c.youtube_channel_id.unique


def test_channel_group_members_have_composite_primary_key():
    table = OrgBase.metadata.tables["channel_group_members"]

    assert {column.name for column in table.primary_key.columns} == {
        "group_id",
        "channel_id",
    }


def test_channel_groups_track_updated_at():
    table = OrgBase.metadata.tables["channel_groups"]

    assert "updated_at" in table.columns
