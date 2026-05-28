from datetime import date
from decimal import Decimal

import pytest

from ums_smart_revenue.connectors.google.adsense_payment_mapping import (
    AdSensePaymentMappingError,
    classify_payments,
    parse_amount,
)


def _resp(*payments, account="pub-1"):
    return {"payments": list(payments), "account_id": account}


def _p(name, date_obj, amount):
    d = {"name": name, "amount": amount}
    if date_obj is not None:
        d["date"] = {
            "year": date_obj.year, "month": date_obj.month, "day": date_obj.day,
        }
    return d


def test_classify_skips_unpaid_balances() -> None:
    resp = _resp(
        _p("accounts/pub-1/payments/unpaid", None, "$10.00"),
        _p("accounts/pub-1/payments/youtube-unpaid", None, "$5.00"),
    )
    out = classify_payments(resp, account_id="pub-1")
    assert out.paid == []
    assert {b.reason for b in out.skipped_balances} == {"no_payment_date"}
    assert {b.raw_amount for b in out.skipped_balances} == {"$10.00", "$5.00"}


def test_classify_accepts_paid_and_youtube_paid() -> None:
    resp = _resp(
        _p("accounts/pub-1/payments/2026-04-21", date(2026, 4, 21), "£100.00"),
        _p("accounts/pub-1/payments/youtube-2026-04-21", date(2026, 4, 21), "£5.00"),
    )
    out = classify_payments(resp, account_id="pub-1")
    assert {s.month for s in out.paid} == {"2026-04"}
    assert {s.payment_name for s in out.paid} == {"2026-04-21", "youtube-2026-04-21"}
    assert {s.source_account_id for s in out.paid} == {"pub-1"}


def test_classify_fails_when_suffix_date_disagrees_with_payment_date() -> None:
    resp = _resp(_p("accounts/pub-1/payments/2026-04-21", date(2026, 4, 22), "£1.00"))
    with pytest.raises(AdSensePaymentMappingError, match="disagrees"):
        classify_payments(resp, account_id="pub-1")


def test_classify_fails_when_dated_settlement_has_no_date() -> None:
    resp = _resp(_p("accounts/pub-1/payments/2026-04-21", None, "£1.00"))
    with pytest.raises(AdSensePaymentMappingError, match="missing Payment.date"):
        classify_payments(resp, account_id="pub-1")


def test_classify_fails_on_account_mismatch() -> None:
    resp = _resp(_p("accounts/pub-OTHER/payments/unpaid", None, "$1.00"))
    with pytest.raises(AdSensePaymentMappingError, match="account"):
        classify_payments(resp, account_id="pub-1")


def test_classify_fails_on_unrecognized_name_form() -> None:
    resp = _resp(_p("accounts/pub-1/payments/weird-suffix", None, "$1.00"))
    with pytest.raises(AdSensePaymentMappingError, match="unrecognized"):
        classify_payments(resp, account_id="pub-1")


def test_classify_rejects_non_list_payments() -> None:
    with pytest.raises(AdSensePaymentMappingError, match="list"):
        classify_payments({"payments": {"oops": 1}}, account_id="pub-1")


@pytest.mark.parametrize("raw,expected", [
    ("£87.65", (Decimal("87.65"), "GBP")),
    ("€87.65", (Decimal("87.65"), "EUR")),
    ("¥1,235 JPY", (Decimal("1235"), "JPY")),
    ("1,234.57 USD", (Decimal("1234.57"), "USD")),
])
def test_parse_amount_accepts(raw, expected) -> None:
    assert parse_amount(raw) == expected


@pytest.mark.parametrize("raw", [
    "$1,234.57", "¥1,235", "1.2.3 GBP", "-5.00 GBP", "kr 5", "",
    # Regression: in the ISO-code branch, embedded junk inside the number must
    # NOT be silently stripped into a fabricated amount — it must fail closed.
    "1e3 GBP", "100x50 GBP", "1 234 GBP", "1#2#3 GBP", "abc 100 GBP",
])
def test_parse_amount_fails_closed(raw) -> None:
    with pytest.raises(AdSensePaymentMappingError):
        parse_amount(raw)
