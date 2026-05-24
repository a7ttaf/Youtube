"""Google revenue source ingestion foundation (currencies + google_revenue_source_rows).

Revision ID: 20260523_0001
Revises: 20260521_0001
Create Date: 2026-05-23

Spec: Docs/superpowers/specs/2026-05-23-spec-b1-google-revenue-source-ingestion-design.md
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from ums_smart_revenue.db.iso_4217_2026_05 import ISO_4217_CURRENCIES_2026_05

revision = "20260523_0001"
down_revision = "20260521_0001"
branch_labels = None
depends_on = None

_SUPPORTED_V1_CODES = ("AED", "USD", "EUR", "GBP", "SAR", "EGP")


def upgrade() -> None:
    _create_currencies_table()
    _seed_currencies()
    _flip_v1_supported_set()
    _create_google_revenue_source_rows_table()


def downgrade() -> None:
    op.drop_index(
        "ix_google_revenue_source_rows_tenant_channel_month",
        table_name="google_revenue_source_rows",
    )
    op.drop_index(
        "ix_google_revenue_source_rows_tenant_month_source",
        table_name="google_revenue_source_rows",
    )
    op.drop_table("google_revenue_source_rows")
    op.drop_table("currencies")


def _create_currencies_table() -> None:
    op.create_table(
        "currencies",
        sa.Column("code", sa.Text(), primary_key=True),
        sa.Column("numeric_code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("minor_unit", sa.Integer(), nullable=True),
        sa.Column(
            "is_supported", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(code) = 3 "
            "AND code = upper(code) "
            "AND substr(code, 1, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(code, 2, 1) BETWEEN 'A' AND 'Z' "
            "AND substr(code, 3, 1) BETWEEN 'A' AND 'Z'",
            name="ck_currencies_code_format",
        ),
        sa.CheckConstraint(
            "length(numeric_code) = 3 "
            "AND substr(numeric_code, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(numeric_code, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(numeric_code, 3, 1) BETWEEN '0' AND '9'",
            name="ck_currencies_numeric_code_format",
        ),
        sa.UniqueConstraint("numeric_code", name="uq_currencies_numeric_code"),
        sa.CheckConstraint(
            "minor_unit IS NULL OR (minor_unit BETWEEN 0 AND 6)",
            name="ck_currencies_minor_unit_range",
        ),
        sa.CheckConstraint(
            "is_supported = false OR minor_unit IS NOT NULL",
            name="ck_currencies_supported_minor",
        ),
        sa.CheckConstraint(
            "is_supported = false OR activated_at IS NOT NULL",
            name="ck_currencies_supported_activated",
        ),
    )


def _seed_currencies() -> None:
    currencies_table = sa.table(
        "currencies",
        sa.column("code", sa.Text()),
        sa.column("numeric_code", sa.Text()),
        sa.column("name", sa.Text()),
        sa.column("minor_unit", sa.Integer()),
    )
    # ISO_4217_CURRENCIES_2026_05 entries are MappingProxyType objects; the
    # dict-comprehension below materialises plain dicts for op.bulk_insert so
    # we do not depend on alembic accepting arbitrary Mapping subclasses.
    op.bulk_insert(
        currencies_table,
        [
            {
                "code": row["code"],
                "numeric_code": row["numeric_code"],
                "name": row["name"],
                "minor_unit": row["minor_unit"],
            }
            for row in ISO_4217_CURRENCIES_2026_05
        ],
    )


def _flip_v1_supported_set() -> None:
    # Dialect-safe UPDATE: let SQLAlchemy render now() and bind the IN(...)
    # values per dialect, instead of hard-coding PostgreSQL now() and manually
    # interpolating the code list into raw SQL (which is not portable to the
    # SQLite migration-testing path and bypasses dialect-safe bind rendering).
    currencies = sa.table(
        "currencies",
        sa.column("code", sa.Text()),
        sa.column("is_supported", sa.Boolean()),
        sa.column("activated_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        currencies.update()
        .where(currencies.c.code.in_(_SUPPORTED_V1_CODES))
        .values(is_supported=True, activated_at=sa.func.now())
    )


def _create_google_revenue_source_rows_table() -> None:
    op.create_table(
        "google_revenue_source_rows",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("source_system", sa.Text(), nullable=False),
        sa.Column("source_row_key", sa.Text(), nullable=False),
        sa.Column("source_account_id", sa.Text(), nullable=False),
        sa.Column("content_owner_id", sa.Text(), nullable=True),
        sa.Column("youtube_channel_id", sa.Text(), nullable=True),
        sa.Column("report_type", sa.Text(), nullable=False),
        sa.Column("report_month", sa.Text(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("metric_key", sa.Text(), nullable=False),
        sa.Column("value_kind", sa.Text(), nullable=False),
        sa.Column("amount_native", sa.Numeric(20, 6), nullable=False),
        sa.Column("currency_code", sa.Text(), nullable=False),
        sa.Column("source_report_id", sa.Text(), nullable=True),
        sa.Column("raw_file_id", sa.Uuid(), nullable=True),
        sa.Column(
            "raw_payload",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            # Portable empty-JSON default. PostgreSQL coerces the '{}' literal
            # to jsonb on assignment, and it stays valid under non-Postgres
            # dialects (e.g. SQLite migration testing). Matches the ORM model's
            # server_default in db/source_models.py; a '{}'::jsonb cast here
            # would diverge from the model and break non-Postgres DDL.
            server_default=sa.text("'{}'"),
        ),
        sa.Column("imported_by", sa.Uuid(), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"],
            name="fk_google_revenue_source_rows_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["currency_code"], ["currencies.code"],
            name="fk_google_revenue_source_rows_currency",
        ),
        sa.ForeignKeyConstraint(
            ["raw_file_id"], ["raw_report_files.id"],
            name="fk_google_revenue_source_rows_raw_file",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "tenant_id", "source_system", "source_row_key",
            name="uq_google_revenue_source_rows_source_key",
        ),
        sa.CheckConstraint(
            "amount_native >= 0",
            name="ck_google_revenue_source_rows_nonneg",
        ),
        # A NUMERIC(20,6) column already rejects ±Infinity at the type level, but
        # NaN IS storable and `>= 0` admits it (NaN sorts above every finite
        # value), so a direct-SQL / backfill / future-service writer could land a
        # NaN amount in this source-of-truth table. This finite bound rejects NaN
        # (NaN < 'Infinity' is false), mirroring the repository's is_finite()
        # guard at the schema boundary.
        sa.CheckConstraint(
            "amount_native < 'Infinity'::numeric",
            name="ck_google_revenue_source_rows_amount_finite",
        ),
        sa.CheckConstraint(
            "source_system IN ('youtube_reporting', 'youtube_analytics', 'adsense_management')",
            name="ck_google_revenue_source_rows_source_system",
        ),
        sa.CheckConstraint(
            "value_kind IN ('estimated', 'settled', 'adjustment', 'tax', 'deduction')",
            name="ck_google_revenue_source_rows_value_kind",
        ),
        sa.CheckConstraint(
            "length(report_month) = 7 AND substr(report_month, 5, 1) = '-' "
            "AND substr(report_month, 1, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 2, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 3, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 4, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 6, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 7, 1) BETWEEN '0' AND '9' "
            "AND substr(report_month, 6, 2) BETWEEN '01' AND '12'",
            name="ck_google_revenue_source_rows_report_month_format",
        ),
        sa.CheckConstraint(
            "period_end >= period_start",
            name="ck_google_revenue_source_rows_period_order",
        ),
        sa.CheckConstraint(
            "length(source_row_key) = 64",
            name="ck_google_revenue_source_rows_source_row_key_length",
        ),
    )
    # PostgreSQL is the financial source of truth; enforce that raw_payload is a
    # JSON object (not array/scalar/null) at the DB level. The repository already
    # validates dict shape, but a DB CHECK keeps non-repository write paths
    # (direct SQL, future services, backfills) aligned with the key/value audit-
    # object contract. jsonb_typeof is PostgreSQL-only, so guard by dialect; the
    # ORM mirrors this PG-only via ddl_if(dialect="postgresql") in
    # db/source_models.py, and downgrade drops the table (and this CHECK) wholesale.
    if op.get_bind().dialect.name == "postgresql":
        op.create_check_constraint(
            "ck_google_revenue_source_rows_raw_payload_object",
            "google_revenue_source_rows",
            "jsonb_typeof(raw_payload) = 'object'",
        )
    op.create_index(
        "ix_google_revenue_source_rows_tenant_month_source",
        "google_revenue_source_rows",
        ["tenant_id", "report_month", "source_system"],
    )
    op.create_index(
        "ix_google_revenue_source_rows_tenant_channel_month",
        "google_revenue_source_rows",
        ["tenant_id", "youtube_channel_id", "report_month"],
        postgresql_where=sa.text("youtube_channel_id IS NOT NULL"),
    )
