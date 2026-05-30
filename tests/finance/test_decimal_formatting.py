from decimal import Decimal

from ums_smart_revenue.finance.decimal_formatting import decimal_to_api


def test_decimal_to_api_trims_trailing_fractional_zeros():
    """Trim insignificant fractional zeros while preserving value."""
    assert decimal_to_api(Decimal("3.5000")) == "3.5"


def test_decimal_to_api_preserves_integral_values_without_exponent():
    """Render integral Decimal values without scientific notation."""
    assert decimal_to_api(Decimal("1000.00")) == "1000"


def test_decimal_to_api_preserves_none_for_optional_callers():
    """Pass through None for optional API and export fields."""
    assert decimal_to_api(None) is None
