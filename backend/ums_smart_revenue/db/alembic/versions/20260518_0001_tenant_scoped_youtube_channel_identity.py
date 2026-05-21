"""Scope YouTube channel identity to tenant.

Revision ID: 20260518_0001
Revises: 20260517_0001
Create Date: 2026-05-18
"""

from alembic import op

revision = "20260518_0001"
down_revision = "20260517_0001"
branch_labels = None
depends_on = None

GLOBAL_CHANNEL_UNIQUE_POSTGRES = "youtube_channels_youtube_channel_id_key"
GLOBAL_CHANNEL_UNIQUE_SQLITE = "uq_youtube_channels_youtube_channel_id"
TENANT_CHANNEL_UNIQUE = "uq_youtube_channels_tenant_channel_id"

SQLITE_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "ix": "ix_%(column_0_label)s",
    "pk": "pk_%(table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
}


def upgrade() -> None:
    _drop_single_column_channel_fks()
    with _batch_alter_table("youtube_channels") as batch_op:
        batch_op.drop_constraint(_global_channel_unique_name(), type_="unique")
        batch_op.create_unique_constraint(
            TENANT_CHANNEL_UNIQUE,
            ["tenant_id", "youtube_channel_id"],
        )
    _create_tenant_channel_fks()


def downgrade() -> None:
    _drop_tenant_channel_fks()
    with _batch_alter_table("youtube_channels") as batch_op:
        batch_op.drop_constraint(TENANT_CHANNEL_UNIQUE, type_="unique")
        batch_op.create_unique_constraint(
            _global_channel_unique_name(),
            ["youtube_channel_id"],
        )
    _create_single_column_channel_fks()


def _drop_single_column_channel_fks() -> None:
    with _batch_alter_table("monthly_channel_revenue_facts") as batch_op:
        batch_op.drop_constraint(
            "fk_monthly_channel_revenue_facts_youtube_channel_id",
            type_="foreignkey",
        )
    with _batch_alter_table("revenue_manual_overrides") as batch_op:
        batch_op.drop_constraint(
            "fk_revenue_manual_overrides_youtube_channel_id",
            type_="foreignkey",
        )


def _create_tenant_channel_fks() -> None:
    with _batch_alter_table("monthly_channel_revenue_facts") as batch_op:
        batch_op.create_foreign_key(
            "fk_monthly_channel_revenue_facts_tenant_channel",
            "youtube_channels",
            ["tenant_id", "youtube_channel_id"],
            ["tenant_id", "youtube_channel_id"],
            ondelete="RESTRICT",
        )
    with _batch_alter_table("revenue_manual_overrides") as batch_op:
        batch_op.create_foreign_key(
            "fk_revenue_manual_overrides_tenant_channel",
            "youtube_channels",
            ["tenant_id", "youtube_channel_id"],
            ["tenant_id", "youtube_channel_id"],
            ondelete="RESTRICT",
        )


def _drop_tenant_channel_fks() -> None:
    with _batch_alter_table("monthly_channel_revenue_facts") as batch_op:
        batch_op.drop_constraint(
            "fk_monthly_channel_revenue_facts_tenant_channel",
            type_="foreignkey",
        )
    with _batch_alter_table("revenue_manual_overrides") as batch_op:
        batch_op.drop_constraint(
            "fk_revenue_manual_overrides_tenant_channel",
            type_="foreignkey",
        )


def _create_single_column_channel_fks() -> None:
    with _batch_alter_table("monthly_channel_revenue_facts") as batch_op:
        batch_op.create_foreign_key(
            "fk_monthly_channel_revenue_facts_youtube_channel_id",
            "youtube_channels",
            ["youtube_channel_id"],
            ["youtube_channel_id"],
            ondelete="RESTRICT",
        )
    with _batch_alter_table("revenue_manual_overrides") as batch_op:
        batch_op.create_foreign_key(
            "fk_revenue_manual_overrides_youtube_channel_id",
            "youtube_channels",
            ["youtube_channel_id"],
            ["youtube_channel_id"],
            ondelete="RESTRICT",
        )


def _global_channel_unique_name() -> str:
    if op.get_bind().dialect.name == "sqlite":
        return GLOBAL_CHANNEL_UNIQUE_SQLITE
    return GLOBAL_CHANNEL_UNIQUE_POSTGRES


def _batch_alter_table(table_name: str):
    if op.get_bind().dialect.name == "sqlite":
        return op.batch_alter_table(
            table_name,
            naming_convention=SQLITE_NAMING_CONVENTION,
        )
    return op.batch_alter_table(table_name)
