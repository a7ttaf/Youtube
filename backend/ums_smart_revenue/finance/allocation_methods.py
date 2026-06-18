"""Shared finance allocation-method values and CHECK-expression helpers."""

ALLOCATION_METHOD_VALUES = (
    "gross_revenue_proportional",
    "post_tax_revenue_proportional",
    "company_level",
    "manual",
    "no_allocation",
)
ALLOCATION_METHODS = frozenset(ALLOCATION_METHOD_VALUES)


# ============================================================================
# Purpose: Build a SQL CHECK expression over the shared allocation-method
#   allowlist so ORM metadata and runtime validation cannot drift apart.
# Database/ORM: SQL CHECK expression consumed by FinanceMonthCloseORM and
#   CommittedAllocationRunORM; column_name must be a quoted SQL identifier.
# Standards: The live allowlist is the single source of truth; the Alembic
#   migration intentionally retains literal strings as a pinned historical
#   schema snapshot and is not derived from this helper.
# Blast Radius: ORM CHECK metadata and migration-derived constraints only.
#   No allocation math, official finance totals, exports, or auth.
# Connections:
#   - File: backend/ums_smart_revenue/finance/recalculation.py -> Imports
#     ALLOCATION_METHODS for runtime normalization.
#   - File: backend/ums_smart_revenue/db/finance_models.py -> Uses this
#     helper to generate CheckConstraint SQL for both close and committed
#     allocation tables.
# ============================================================================
def allocation_method_check_expression(column_name: str, *, nullable: bool = False) -> str:
    """Build a SQL CHECK expression for the shared allocation-method allowlist."""
    allowed_values = ", ".join(f"'{method}'" for method in ALLOCATION_METHOD_VALUES)
    expression = f"{column_name} IN ({allowed_values})"
    if nullable:
        return f"{column_name} IS NULL OR {expression}"
    return expression
