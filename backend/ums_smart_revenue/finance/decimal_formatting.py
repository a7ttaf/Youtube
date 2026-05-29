from decimal import Decimal
from typing import overload


@overload
def decimal_to_api(value: Decimal) -> str: ...


@overload
def decimal_to_api(value: None) -> None: ...


# ============================================================================
# Purpose: Serialize finance Decimal values into the canonical API/report string
#   form without exponent notation or insignificant trailing fractional zeros.
# Database/ORM: None.
# Standards: Shared typed boundary helper; no logging or error translation.
# Blast Radius: Finance exports/API serialization consistency.
# Connections:
#   - File: backend/ums_smart_revenue/finance/deduction_components.py -> Uses
#     the shared formatting contract for deduction-component API payloads.
#   - File: backend/ums_smart_revenue/reports/finance_workbook.py -> Uses the
#     same formatting contract for workbook export cells.
# ============================================================================
def decimal_to_api(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f")
    return format(normalized, "f").rstrip("0").rstrip(".")
