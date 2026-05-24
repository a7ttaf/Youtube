import json
from datetime import date
from decimal import Decimal
from importlib import resources
from uuid import uuid4

import pytest

from ums_smart_revenue.connectors.google_source_parsers import (
    AdSenseManagementParser,
    ParserError,
)

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


def test_source_row_key_ignores_run_specific_report_id() -> None:
    """A regenerated report (new report_id) for the same month/account/
    dimensions must yield identical source_row_keys, so the rerun updates in
    place instead of inserting duplicate revenue rows. report_id stays as
    provenance on source_report_id but must not enter the dedup key.
    """
    base = _load("sample_earnings_report_2026_04.json")
    regenerated = _load("sample_earnings_report_2026_04.json")
    regenerated["report_id"] = f"{base['report_id']}-regenerated"
    a = list(AdSenseManagementParser().parse(base, tenant_id=TENANT_ID))
    b = list(AdSenseManagementParser().parse(regenerated, tenant_id=TENANT_ID))
    assert sorted(r.source_row_key for r in a) == sorted(r.source_row_key for r in b)
    # report_id is preserved as provenance, just not folded into the key.
    assert all(r.source_report_id == f"{base['report_id']}-regenerated" for r in b)


def test_earnings_and_payment_keys_differ() -> None:
    e = list(AdSenseManagementParser().parse(_load("sample_earnings_report_2026_04.json"), tenant_id=TENANT_ID))
    p = list(AdSenseManagementParser().parse(_load("sample_payment_report_2026_04.json"), tenant_id=TENANT_ID))
    assert not (set(r.source_row_key for r in e) & set(r.source_row_key for r in p))


def test_mixed_settled_and_estimated_metrics_are_labeled_per_metric() -> None:
    """A single AdSense report containing both a settled (PAID_AMOUNT) and an
    estimated metric must label each emitted row by its own metric.

    Regression: the old report-level any()-based derivation tagged every row
    'settled'/'payment_report' as soon as one settled metric was present,
    mislabeling the estimated rows.
    """
    payload = {
        "request": {
            "accountId": "accounts/pub-test-001",
            "dateRange": {
                "startDate": {"year": 2026, "month": 4, "day": 1},
                "endDate": {"year": 2026, "month": 4, "day": 30},
            },
            "currencyCode": "USD",
        },
        "report_id": "adsense-mixed-2026-04",
        "headers": [
            {"name": "PAID_AMOUNT", "type": "METRIC_CURRENCY", "currencyCode": "USD"},
            {"name": "ESTIMATED_EARNINGS", "type": "METRIC_CURRENCY", "currencyCode": "USD"},
        ],
        "rows": [
            {"cells": [{"value": "500.000000"}, {"value": "123.456789"}]},
        ],
    }
    rows = list(AdSenseManagementParser().parse(payload, tenant_id=TENANT_ID))
    # Pin the exact contract before collapsing into a lookup so an extra or
    # duplicated row cannot pass silently.
    assert len(rows) == 2
    assert {r.metric_key for r in rows} == {"PAID_AMOUNT", "ESTIMATED_EARNINGS"}
    by_metric = {r.metric_key: r for r in rows}
    assert by_metric["PAID_AMOUNT"].value_kind == "settled"
    assert by_metric["PAID_AMOUNT"].report_type == "payment_report"
    assert by_metric["PAID_AMOUNT"].amount_native == Decimal("500.000000")
    assert by_metric["ESTIMATED_EARNINGS"].value_kind == "estimated"
    assert by_metric["ESTIMATED_EARNINGS"].report_type == "earnings_report"
    assert by_metric["ESTIMATED_EARNINGS"].amount_native == Decimal("123.456789")


def test_rejects_non_finite_amount() -> None:
    """NaN Decimal strings must fail at the parser, not downstream."""
    payload = _load("sample_earnings_report_2026_04.json")
    bad = {**payload}
    # headers order is [PRODUCT_CODE, COUNTRY_CODE, ESTIMATED_EARNINGS];
    # mutate the first row's metric cell (index 2) value to NaN.
    first_row = {**bad["rows"][0]}
    first_row["cells"] = [*bad["rows"][0]["cells"]]
    first_row["cells"][2] = {**first_row["cells"][2], "value": "NaN"}
    bad["rows"] = [first_row, *bad["rows"][1:]]
    with pytest.raises(ParserError):
        list(AdSenseManagementParser().parse(bad, tenant_id=TENANT_ID))
