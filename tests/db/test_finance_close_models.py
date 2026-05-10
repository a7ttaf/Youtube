from ums_smart_revenue.db.finance_models import FinanceBase


def test_finance_metadata_contains_month_close_table():
    assert set(FinanceBase.metadata.tables) >= {"finance_month_close"}


def test_finance_month_close_has_control_columns():
    table = FinanceBase.metadata.tables["finance_month_close"]

    assert {
        "month",
        "status",
        "allocation_method",
        "allocation_rule_payload",
        "locked_by",
        "locked_at",
        "unlocked_by",
        "unlocked_at",
    } <= set(table.columns.keys())
