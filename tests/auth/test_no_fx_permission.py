"""Guard test proving B1 did not add Permission.MANAGE_FX_RATES.

Spec section 3 explicitly excludes this permission. If a future spec
introduces FX rate management, this test will fail and the FX-spec
author must remove it as part of that introduction.
"""

from ums_smart_revenue.auth.permissions import Permission


def test_manage_fx_rates_permission_does_not_exist() -> None:
    forbidden_names = {p.name for p in Permission}
    assert "MANAGE_FX_RATES" not in forbidden_names, (
        "B1 spec section 3 prohibits Permission.MANAGE_FX_RATES. If a later spec "
        "introduces this permission, remove this guard as part of that spec."
    )


def test_no_finance_manage_fx_rates_value_in_permissions() -> None:
    values = {p.value for p in Permission}
    assert "finance.manage_fx_rates" not in values, (
        "B1 spec section 3 prohibits the finance.manage_fx_rates permission value. "
        "If a later spec introduces FX management, remove this guard as part of that spec."
    )
