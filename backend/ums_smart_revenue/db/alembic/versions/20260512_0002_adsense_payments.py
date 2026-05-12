"""Create AdSense payment table.

Revision ID: 20260512_0002
Revises: 20260512_0001
Create Date: 2026-05-12
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260512_0002"
down_revision = "20260512_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "adsense_payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("month", sa.Text(), nullable=False),
        sa.Column("payment_name", sa.Text(), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("payment_amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("payment_currency", sa.Text(), nullable=False),
        sa.Column("payment_status", sa.Text(), nullable=False, server_default=sa.text("'PAID'")),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("source_report_id", sa.Text(), nullable=True),
        sa.Column("imported_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("month", "payment_name", name="uq_adsense_payments_month_name"),
        sa.CheckConstraint(
            "length(month) = 7 AND substr(month, 5, 1) = '-' "
            "AND substr(month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_adsense_payments_month_format",
        ),
        sa.CheckConstraint("payment_amount >= 0", name="ck_adsense_payments_amount_nonnegative"),
        sa.CheckConstraint(
            "length(payment_currency) = 3 AND payment_currency = upper(payment_currency)",
            name="ck_adsense_payments_currency_code",
        ),
        sa.CheckConstraint(
            "payment_status IN ('PAID', 'PENDING', 'UNPAID', 'CANCELLED')",
            name="ck_adsense_payments_payment_status",
        ),
    )
    op.create_index("ix_adsense_payments_month_date", "adsense_payments", ["month", "payment_date"])
    op.create_index("ix_adsense_payments_source_report", "adsense_payments", ["source_report_id"])


def downgrade() -> None:
    """Fully reverse upgrade(): drop all indexes and the payment table."""
    op.drop_index("ix_adsense_payments_source_report", table_name="adsense_payments")
    op.drop_index("ix_adsense_payments_month_date", table_name="adsense_payments")
    op.drop_table("adsense_payments")
