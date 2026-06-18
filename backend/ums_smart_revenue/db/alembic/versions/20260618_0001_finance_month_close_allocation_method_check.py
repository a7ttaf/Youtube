"""Constrain finance_month_close allocation_method to the shared allowlist.

Revision ID: 20260618_0001
Revises: 20260612_0002
Create Date: 2026-06-18

The finance-close allocation-rule endpoint stores metadata only, but its
allocation_method value still feeds audit trails and operator decisions. This
migration adds the database backstop that was already present on committed
allocation runs: nullable for not-yet-configured close rows, allowlisted for
all persisted non-null methods.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import bindparam

revision = "20260618_0001"
down_revision = "20260612_0002"
branch_labels = None
depends_on = None

_CONSTRAINT_NAME = "ck_finance_month_close_allocation_method"
_ALLOCATION_METHOD_VALUES = (
    "gross_revenue_proportional",
    "post_tax_revenue_proportional",
    "company_level",
    "manual",
    "no_allocation",
)
_ALLOCATION_METHOD_CHECK = (
    "allocation_method IS NULL OR allocation_method IN ("
    "'gross_revenue_proportional', 'post_tax_revenue_proportional', "
    "'company_level', 'manual', 'no_allocation')"
)


def _normalize_legacy_allocation_method_casing() -> int:
    """Lowercase existing rows whose lower-cased value is in the allowlist.

    The pre-PR endpoint stripped whitespace but did not lowercase the value,
    so a row persisted as ``POST_TAX_REVENUE_PROPORTIONAL`` (or any other
    upper/mixed-case variant of a canonical method) would be counted as
    invalid by the preflight and abort this migration. The new route accepts
    and normalizes such input, so a legacy row whose lower-cased form is in
    the allowlist is functionally a valid canonical method and must be
    migrated forward to its lowercase form before the CHECK is added.
    """
    bind = op.get_bind()
    with bind.begin() as conn:
        result = conn.execute(
            sa.text(
                "UPDATE finance_month_close "
                "SET allocation_method = lower(allocation_method) "
                "WHERE allocation_method IS NOT NULL "
                "AND lower(allocation_method) IN :allowed_methods "
                "AND allocation_method <> lower(allocation_method)"
            ).bindparams(
                bindparam(
                    "allowed_methods",
                    value=tuple(_ALLOCATION_METHOD_VALUES),
                    expanding=True,
                )
            ),
        )
    return int(result.rowcount or 0)


def _invalid_allocation_method_count() -> int:
    """Return existing close rows that would violate the new CHECK.

    Assumes :func:`_normalize_legacy_allocation_method_casing` has already
    folded any case-variant canonical methods into their lowercase form.
    """
    bind = op.get_bind()
    with bind.begin() as conn:
        return int(
            conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM finance_month_close "
                    "WHERE allocation_method IS NOT NULL "
                    "AND allocation_method NOT IN :allowed_methods"
                ).bindparams(
                    bindparam(
                        "allowed_methods",
                        value=tuple(_ALLOCATION_METHOD_VALUES),
                        expanding=True,
                    )
                ),
            ).scalar_one()
        )


# ============================================================================
# Purpose: Add a nullable allowlist CHECK to finance_month_close allocation
#   metadata so direct SQL/backfill paths cannot persist unknown methods.
# Database/ORM: finance_month_close / FinanceMonthCloseORM
#   (ck_finance_month_close_allocation_method only; no column/data rewrite).
# Standards: batch_alter_table keeps SQLite compatibility; the preflight count
#   raises RuntimeError before ALTER TABLE when historical invalid values need
#   operator cleanup. PostgreSQL remains the source of truth.
# Blast Radius: Finance close metadata and audit interpretation only. No auth,
#   official finance calculations, exports, RLS policy, or tenancy broadening.
# Connections:
#   - File: backend/ums_smart_revenue/api/finance_close.py -> route now
#     normalizes/allowlists allocation_method before repository writes.
#   - File: backend/ums_smart_revenue/db/finance_models.py -> ORM metadata
#     mirrors this migration constraint for create_all/test schemas.
# ============================================================================
def upgrade() -> None:
    """Add the allocation_method allowlist CHECK."""
    _normalize_legacy_allocation_method_casing()
    invalid_count = _invalid_allocation_method_count()
    if invalid_count:
        raise RuntimeError(
            f"Cannot add {_CONSTRAINT_NAME}: {invalid_count} finance_month_close "
            "row(s) use an allocation_method outside the shared allowlist. "
            "Clean or null those values before retrying the migration."
        )
    with op.batch_alter_table("finance_month_close") as batch:
        batch.create_check_constraint(_CONSTRAINT_NAME, _ALLOCATION_METHOD_CHECK)


def downgrade() -> None:
    """Drop the allocation_method allowlist CHECK."""
    with op.batch_alter_table("finance_month_close") as batch:
        batch.drop_constraint(_CONSTRAINT_NAME, type_="check")
