"""Add credential refresh telemetry columns to api_connector_credentials.

Revision ID: 20260612_0001
Revises: 20260609_0002
Create Date: 2026-06-12
"""
import sqlalchemy as sa
from alembic import op

# ============================================================================
# Purpose: Additive Part 2 telemetry columns on api_connector_credentials so
#   credential refresh outcome (attempt time, status, exception class name,
#   token expiry) persists. Four NULLABLE columns + a CHECK on
#   last_refresh_status (IS NULL escape so existing rows pass). No backfill.
# Database/ORM: api_connector_credentials (tenant-scoped, tenant-writable; NOT
#   in TENANT_PLATFORM_ONLY_WRITE_TABLES, so no grant-pin impact).
# Standards: batch_alter_table so the SQLite test tier round-trips; CHECK name
#   ck_connector_last_refresh_status MUST match the ORM + tests. downgrade
#   drops the constraint before the columns. error_class stores the class name
#   only, never message text.
# Blast Radius: Connector credential read surface (new fields). No finance,
#   auth, audit, or graph projection impact.
# Connections:
#   - File: backend/ums_smart_revenue/db/security_models.py ->
#     ApiConnectorCredentialORM mirrors these columns + CHECK.
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     resolve_connector_credentials stamps these columns.
# ============================================================================

revision = "20260612_0001"
down_revision = "20260609_0002"
branch_labels = None
depends_on = None

_STATUS_CHECK = (
    "last_refresh_status IS NULL "
    "OR last_refresh_status IN ('succeeded', 'failed')"
)


def upgrade() -> None:
    """Add the four nullable telemetry columns + the status CHECK."""
    with op.batch_alter_table("api_connector_credentials") as batch:
        batch.add_column(
            sa.Column(
                "last_refresh_attempt_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "token_expiry_at", sa.DateTime(timezone=True), nullable=True
            )
        )
        batch.add_column(
            sa.Column("last_refresh_status", sa.Text(), nullable=True)
        )
        batch.add_column(
            sa.Column("last_refresh_error_class", sa.Text(), nullable=True)
        )
        batch.create_check_constraint(
            "ck_connector_last_refresh_status", _STATUS_CHECK
        )


def downgrade() -> None:
    """Drop the status CHECK then the four telemetry columns."""
    with op.batch_alter_table("api_connector_credentials") as batch:
        batch.drop_constraint(
            "ck_connector_last_refresh_status", type_="check"
        )
        batch.drop_column("last_refresh_error_class")
        batch.drop_column("last_refresh_status")
        batch.drop_column("token_expiry_at")
        batch.drop_column("last_refresh_attempt_at")
