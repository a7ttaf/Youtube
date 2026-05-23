"""Smoke tests for the immutable ISO 4217 snapshot module.

The snapshot is intentionally frozen: future ISO updates land as a new
dated module (e.g. iso_4217_2027_03.py) plus a new migration, not by
mutating this file.
"""

import pytest

from ums_smart_revenue.db.iso_4217_2026_05 import ISO_4217_CURRENCIES_2026_05

SUPPORTED_V1 = ("AED", "USD", "EUR", "GBP", "SAR", "EGP")


def test_snapshot_contains_v1_supported_set() -> None:
    codes = {row["code"] for row in ISO_4217_CURRENCIES_2026_05}
    for expected in SUPPORTED_V1:
        assert expected in codes, f"missing v1 supported code: {expected}"


def test_all_codes_are_three_uppercase_letters() -> None:
    for row in ISO_4217_CURRENCIES_2026_05:
        code = row["code"]
        assert isinstance(code, str)
        assert len(code) == 3
        assert code == code.upper()
        assert code.isalpha()


def test_all_numeric_codes_are_three_digit_strings_and_unique() -> None:
    numeric_codes = [row["numeric_code"] for row in ISO_4217_CURRENCIES_2026_05]
    assert len(numeric_codes) == len(set(numeric_codes)), "numeric codes must be unique"
    for numeric_code in numeric_codes:
        assert isinstance(numeric_code, str)
        assert len(numeric_code) == 3
        assert numeric_code.isdigit()


def test_codes_are_unique() -> None:
    codes = [row["code"] for row in ISO_4217_CURRENCIES_2026_05]
    assert len(codes) == len(set(codes)), "ISO 4217 codes must be unique"


def test_minor_unit_is_in_range_or_none() -> None:
    for row in ISO_4217_CURRENCIES_2026_05:
        minor_unit = row["minor_unit"]
        assert minor_unit is None or (isinstance(minor_unit, int) and 0 <= minor_unit <= 6)


def test_v1_supported_codes_have_known_minor_unit() -> None:
    by_code = {row["code"]: row for row in ISO_4217_CURRENCIES_2026_05}
    for code in SUPPORTED_V1:
        assert by_code[code]["minor_unit"] is not None, (
            f"v1 supported currency {code} must declare minor_unit so it can be flipped is_supported"
        )


def test_row_count_smoke() -> None:
    # Sanity check that the snapshot is the full ISO list, not just the v1 set.
    assert len(ISO_4217_CURRENCIES_2026_05) >= 150


def test_snapshot_entries_are_read_only() -> None:
    first_entry = ISO_4217_CURRENCIES_2026_05[0]
    with pytest.raises(TypeError):
        first_entry["code"] = "XXX"  # type: ignore[index]
