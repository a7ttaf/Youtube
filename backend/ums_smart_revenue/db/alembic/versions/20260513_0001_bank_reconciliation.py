"""Create bank reconciliation entries table.

Revision ID: 20260513_0001
Revises: 20260512_0002
Create Date: 2026-05-13
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260513_0001"
down_revision = "20260512_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bank_reconciliation_entries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("month", sa.Text(), nullable=False),
        sa.Column("bank_reference", sa.Text(), nullable=False),
        sa.Column("bank_received_date", sa.Date(), nullable=False),
        sa.Column("bank_received_amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("bank_received_currency", sa.Text(), nullable=False),
        sa.Column("bank_received_amount_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column(
            "transfer_fee_usd",
            sa.Numeric(18, 6),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "fx_difference_usd",
            sa.Numeric(18, 6),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("source_report_id", sa.Text(), nullable=True),
        sa.Column("recorded_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "recorded_at",
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
            "month",
            "bank_reference",
            name="uq_bank_reconciliation_month_reference",
        ),
        sa.CheckConstraint(
            _month_format_check_sql(),
            name="ck_bank_reconciliation_month_format",
        ),
        sa.CheckConstraint(
            "bank_received_amount >= 0",
            name="ck_bank_reconciliation_received_nonnegative",
        ),
        sa.CheckConstraint(
            "bank_received_amount_usd >= 0",
            name="ck_bank_reconciliation_received_usd_nonnegative",
        ),
        sa.CheckConstraint(
            "transfer_fee_usd >= 0",
            name="ck_bank_reconciliation_transfer_fee_nonnegative",
        ),
        sa.CheckConstraint(
            "length(bank_received_currency) = 3 "
            "AND bank_received_currency = upper(bank_received_currency) "
            "AND substr(bank_received_currency, 1, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(bank_received_currency, 2, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(bank_received_currency, 3, 1) BETWEEN 'A' AND 'Z'",
            name="ck_bank_reconciliation_currency_code",
        ),
    )
    op.create_index(
        "ix_bank_reconciliation_entries_month",
        "bank_reconciliation_entries",
        ["month"],
    )
    op.create_index(
        "ix_bank_reconciliation_entries_source_report",
        "bank_reconciliation_entries",
        ["source_report_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_bank_reconciliation_entries_source_report",
        table_name="bank_reconciliation_entries",
    )
    op.drop_index(
        "ix_bank_reconciliation_entries_month",
        table_name="bank_reconciliation_entries",
    )
    op.drop_table("bank_reconciliation_entries")


def _month_format_check_sql() -> str:
    bind = op.get_bind()
    dialect_name = bind.dialect.name if bind is not None else "postgresql"
    if dialect_name == "sqlite":
        return (
            "month GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]' "
            "AND substr(month, 6, 2) BETWEEN '01' AND '12'"
        )
    return "month ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'"
