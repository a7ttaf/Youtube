"""Add currency exchange rates.

Revision ID: 20260513_0004
Revises: 20260513_0003
Create Date: 2026-05-13
"""

import sqlalchemy as sa
from alembic import op

revision = "20260513_0004"
down_revision = "20260513_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "currency_exchange_rates",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column("base_currency", sa.Text(), nullable=False),
        sa.Column("quote_currency", sa.Text(), nullable=False),
        sa.Column("rate", sa.Numeric(18, 10), nullable=False),
        sa.Column("provider_key", sa.Text(), nullable=False),
        sa.Column("source_report_id", sa.Text(), nullable=True),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("imported_by", sa.Uuid(), nullable=True),
        sa.Column(
            "imported_at",
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
            "rate_date",
            "base_currency",
            "quote_currency",
            "provider_key",
            name="uq_currency_exchange_rate_provider_date_pair",
        ),
        sa.CheckConstraint(
            "length(base_currency) = 3 "
            "AND base_currency = upper(base_currency) "
            "AND substr(base_currency, 1, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(base_currency, 2, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(base_currency, 3, 1) BETWEEN 'A' AND 'Z' "
            "AND length(quote_currency) = 3 "
            "AND quote_currency = upper(quote_currency) "
            "AND substr(quote_currency, 1, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(quote_currency, 2, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(quote_currency, 3, 1) BETWEEN 'A' AND 'Z' "
            "AND base_currency <> quote_currency",
            name="ck_currency_exchange_rates_currency_pair",
        ),
        sa.CheckConstraint(
            "rate > 0",
            name="ck_currency_exchange_rates_rate_positive",
        ),
    )
    op.create_index(
        "ix_currency_exchange_rates_pair_date",
        "currency_exchange_rates",
        ["base_currency", "quote_currency", "rate_date"],
    )
    op.create_index(
        "ix_currency_exchange_rates_provider",
        "currency_exchange_rates",
        ["provider_key"],
    )
    op.create_index(
        "ix_currency_exchange_rates_source_report",
        "currency_exchange_rates",
        ["source_report_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_currency_exchange_rates_source_report",
        table_name="currency_exchange_rates",
    )
    op.drop_index(
        "ix_currency_exchange_rates_provider",
        table_name="currency_exchange_rates",
    )
    op.drop_index(
        "ix_currency_exchange_rates_pair_date",
        table_name="currency_exchange_rates",
    )
    op.drop_table("currency_exchange_rates")
