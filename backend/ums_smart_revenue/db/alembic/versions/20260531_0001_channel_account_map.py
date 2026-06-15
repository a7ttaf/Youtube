"""Create channel-account map tables (account-owner + owner-channel links).

Revision ID: 20260531_0001
Revises: 20260529_0002
Create Date: 2026-05-31

Spec: Docs/superpowers/specs/2026-05-31-spec-channel-account-map-design.md
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260531_0001"
down_revision = "20260529_0002"
branch_labels = None
depends_on = None

UMS_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def _month_format(column: str) -> str:
    """Return a SQL CHECK fragment validating YYYY-MM format for the given column."""
    return (
        f"length({column}) = 7 AND substr({column}, 5, 1) = '-' "
        f"AND substr({column}, 1, 1) BETWEEN '0' AND '9' "
        f"AND substr({column}, 2, 1) BETWEEN '0' AND '9' "
        f"AND substr({column}, 3, 1) BETWEEN '0' AND '9' "
        f"AND substr({column}, 4, 1) BETWEEN '0' AND '9' "
        f"AND substr({column}, 6, 1) BETWEEN '0' AND '9' "
        f"AND substr({column}, 7, 1) BETWEEN '0' AND '9' "
        f"AND substr({column}, 6, 2) BETWEEN '01' AND '12'"
    )


# ============================================================================
# Purpose: Create the two channel-account map tables. account-owner links carry
#   the operator-verified trust decision; owner-channel links are derived.
# Database/ORM: adsense_content_owner_links / content_owner_channel_links.
# Standards: object JSONB CHECK is Postgres-only (dialect guard), mirroring
#   deduction_components. Downgrade drops indexes then tables.
# Blast Radius: Finance source-of-truth (additive). No auth/Neo4j schema impact.
# Connections:
#   - File: backend/ums_smart_revenue/db/finance_models.py -> ORM contract
#     (AdsenseContentOwnerLinkORM, ContentOwnerChannelLinkORM).
#   - File: Docs/superpowers/specs/2026-05-31-spec-channel-account-map-design.md
# ============================================================================
def upgrade() -> None:
    """Create both map tables with constraints and indexes."""
    op.create_table(
        "adsense_content_owner_links",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            server_default=sa.text(f"'{UMS_TENANT_ID}'"),
        ),
        sa.Column("adsense_account_id", sa.Text(), nullable=False),
        sa.Column("content_owner_id", sa.Text(), nullable=False),
        sa.Column(
            "verification_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'UNVERIFIED'"),
        ),
        sa.Column("provenance_kind", sa.Text(), nullable=False),
        sa.Column(
            "provenance_payload",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("verified_by", sa.Uuid(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_reason", sa.Text(), nullable=True),
        sa.Column("effective_month_start", sa.Text(), nullable=False),
        sa.Column("effective_month_end", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "adsense_account_id",
            "content_owner_id",
            "effective_month_start",
            name="uq_adsense_content_owner_links_key",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_adsense_content_owner_links_tenant",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "verification_status IN ('UNVERIFIED', 'VERIFIED', 'REJECTED', 'CONFLICT')",
            name="ck_adsense_content_owner_links_status",
        ),
        sa.CheckConstraint(
            _month_format("effective_month_start"),
            name="ck_adsense_content_owner_links_start_format",
        ),
        sa.CheckConstraint(
            f"effective_month_end IS NULL OR ({_month_format('effective_month_end')})",
            name="ck_adsense_content_owner_links_end_format",
        ),
        sa.CheckConstraint(
            "effective_month_end IS NULL OR effective_month_end >= effective_month_start",
            name="ck_adsense_content_owner_links_range",
        ),
        sa.CheckConstraint(
            "length(adsense_account_id) >= 1",
            name="ck_adsense_content_owner_links_account_nonempty",
        ),
        sa.CheckConstraint(
            "length(content_owner_id) >= 1",
            name="ck_adsense_content_owner_links_owner_nonempty",
        ),
    )
    op.create_table(
        "content_owner_channel_links",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            nullable=False,
            server_default=sa.text(f"'{UMS_TENANT_ID}'"),
        ),
        sa.Column("content_owner_id", sa.Text(), nullable=False),
        sa.Column("youtube_channel_id", sa.Text(), nullable=False),
        sa.Column("provenance_kind", sa.Text(), nullable=False),
        sa.Column("provenance_source_id", sa.Text(), nullable=True),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("effective_month_start", sa.Text(), nullable=False),
        sa.Column("effective_month_end", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "content_owner_id",
            "youtube_channel_id",
            "effective_month_start",
            name="uq_content_owner_channel_links_key",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_content_owner_channel_links_tenant",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "provenance_kind IN ('SOURCE_ROW', 'CHANNEL_REGISTRY', 'MANUAL')",
            name="ck_content_owner_channel_links_provenance_kind",
        ),
        sa.CheckConstraint(
            _month_format("effective_month_start"),
            name="ck_content_owner_channel_links_start_format",
        ),
        sa.CheckConstraint(
            f"effective_month_end IS NULL OR ({_month_format('effective_month_end')})",
            name="ck_content_owner_channel_links_end_format",
        ),
        sa.CheckConstraint(
            "effective_month_end IS NULL OR effective_month_end >= effective_month_start",
            name="ck_content_owner_channel_links_range",
        ),
        sa.CheckConstraint(
            "length(content_owner_id) >= 1",
            name="ck_content_owner_channel_links_owner_nonempty",
        ),
        sa.CheckConstraint(
            "length(youtube_channel_id) >= 1",
            name="ck_content_owner_channel_links_channel_nonempty",
        ),
    )
    # Postgres-only object guard (invalid SQLite CREATE syntax), mirroring the
    # ORM's .ddl_if(dialect="postgresql") CHECK in finance_models.py.
    if op.get_bind().dialect.name == "postgresql":
        op.create_check_constraint(
            "ck_adsense_content_owner_links_provenance_payload_object",
            "adsense_content_owner_links",
            "jsonb_typeof(provenance_payload) = 'object'",
        )
    op.create_index(
        "ix_adsense_content_owner_links_account_status",
        "adsense_content_owner_links",
        ["tenant_id", "adsense_account_id", "verification_status"],
    )
    op.create_index(
        "ix_content_owner_channel_links_owner",
        "content_owner_channel_links",
        ["tenant_id", "content_owner_id", "effective_month_start"],
    )


def downgrade() -> None:
    """Drop both map tables and their indexes."""
    op.drop_index(
        "ix_content_owner_channel_links_owner",
        table_name="content_owner_channel_links",
    )
    op.drop_index(
        "ix_adsense_content_owner_links_account_status",
        table_name="adsense_content_owner_links",
    )
    op.drop_table("content_owner_channel_links")
    op.drop_table("adsense_content_owner_links")
