"""Shared finance allocation-method values and CHECK-expression helpers."""

ALLOCATION_METHOD_VALUES = (
    "gross_revenue_proportional",
    "post_tax_revenue_proportional",
    "company_level",
    "manual",
    "no_allocation",
)
ALLOCATION_METHODS = frozenset(ALLOCATION_METHOD_VALUES)


def allocation_method_check_expression(column_name: str, *, nullable: bool = False) -> str:
    """Build a SQL CHECK expression for the shared allocation-method allowlist."""
    allowed_values = ", ".join(f"'{method}'" for method in ALLOCATION_METHOD_VALUES)
    expression = f"{column_name} IN ({allowed_values})"
    if nullable:
        return f"{column_name} IS NULL OR {expression}"
    return expression
