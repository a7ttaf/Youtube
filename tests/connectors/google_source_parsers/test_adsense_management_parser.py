import json
from datetime import date
from decimal import Decimal
from importlib import resources
from uuid import uuid4

from ums_smart_revenue.connectors.google_source_parsers import AdSenseManagementParser

TENANT_ID = uuid4()


def _load(name: str) -> dict[str, object]:
    ref = resources.files("tests.connectors._fixtures.adsense_management").joinpath(name)
    with ref.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def test_earnings_report_emits_estimated_rows() -> None:
    payload = _load("sample_earnings_report_2026_04.json")
    rows = list(AdSenseManagementParser().parse(payload, tenant_id=TENANT_ID))
    assert len(rows) == 3
    for row in rows:
        assert row.source_system == "adsense_management"
        assert row.value_kind == "estimated"
        assert row.report_type == "earnings_report"
        assert row.report_month == "2026-04"
        assert row.period_start == date(2026, 4, 1)
        assert row.period_end == date(2026, 4, 30)


def test_payment_report_emits_settled_row() -> None:
    payload = _load("sample_payment_report_2026_04.json")
    rows = list(AdSenseManagementParser().parse(payload, tenant_id=TENANT_ID))
    assert len(rows) == 1
    row = rows[0]
    assert row.value_kind == "settled"
    assert row.report_type == "payment_report"
    assert row.amount_native == Decimal("847.130000")
    assert row.currency_code == "USD"


def test_account_id_is_normalized_from_request() -> None:
    payload = _load("sample_earnings_report_2026_04.json")
    rows = list(AdSenseManagementParser().parse(payload, tenant_id=TENANT_ID))
    for row in rows:
        assert row.source_account_id == "pub-test-001"


def test_source_row_key_stable_across_reruns_for_earnings() -> None:
    a = list(AdSenseManagementParser().parse(_load("sample_earnings_report_2026_04.json"), tenant_id=TENANT_ID))
    b = list(AdSenseManagementParser().parse(_load("sample_earnings_report_2026_04_rerun.json"), tenant_id=TENANT_ID))
    assert sorted(r.source_row_key for r in a) == sorted(r.source_row_key for r in b)


def test_source_row_key_stable_across_reruns_for_payments() -> None:
    a = list(AdSenseManagementParser().parse(_load("sample_payment_report_2026_04.json"), tenant_id=TENANT_ID))
    b = list(AdSenseManagementParser().parse(_load("sample_payment_report_2026_04_rerun.json"), tenant_id=TENANT_ID))
    assert sorted(r.source_row_key for r in a) == sorted(r.source_row_key for r in b)


def test_earnings_and_payment_keys_differ() -> None:
    e = list(AdSenseManagementParser().parse(_load("sample_earnings_report_2026_04.json"), tenant_id=TENANT_ID))
    p = list(AdSenseManagementParser().parse(_load("sample_payment_report_2026_04.json"), tenant_id=TENANT_ID))
    assert not (set(r.source_row_key for r in e) & set(r.source_row_key for r in p))
