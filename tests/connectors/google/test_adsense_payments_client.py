import pytest

from ums_smart_revenue.connectors.google.adsense_payments_client import (
    GoogleAdSensePaymentClient,
)
from ums_smart_revenue.connectors.google.errors import (
    GoogleApiResponseError,
    MalformedAdsenseAccountIdError,
)


class _FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, *, method, url, params=None, json_body=None):
        self.calls.append((method, url, params))
        return self.response


def test_fetch_payments_calls_correct_endpoint() -> None:
    http = _FakeHttp({"payments": []})
    client = GoogleAdSensePaymentClient(http=http)
    result = client.fetch_payments(account_id="pub-123")
    method, url, _ = http.calls[0]
    assert method == "GET"
    assert url == "https://adsense.googleapis.com/v2/accounts/pub-123/payments"
    assert "report_id" in result and isinstance(result["report_id"], str)
    assert result["payments"] == []


def test_fetch_payments_strips_accounts_prefix() -> None:
    http = _FakeHttp({"payments": []})
    GoogleAdSensePaymentClient(http=http).fetch_payments(
        account_id="accounts/pub-123"
    )
    assert "accounts/pub-123/payments" in http.calls[0][1]


def test_fetch_payments_rejects_blank_account() -> None:
    http = _FakeHttp({"payments": []})
    with pytest.raises(MalformedAdsenseAccountIdError):
        GoogleAdSensePaymentClient(http=http).fetch_payments(account_id="  ")


def test_fetch_payments_rejects_non_object_response() -> None:
    http = _FakeHttp([])  # list, not an object
    with pytest.raises(GoogleApiResponseError):
        GoogleAdSensePaymentClient(http=http).fetch_payments(account_id="pub-1")


def test_fetch_payments_rejects_missing_payments_field() -> None:
    http = _FakeHttp({"notpayments": []})  # object, but no payments list
    with pytest.raises(GoogleApiResponseError):
        GoogleAdSensePaymentClient(http=http).fetch_payments(account_id="pub-1")


def test_fetch_payments_rejects_non_list_payments() -> None:
    http = _FakeHttp({"payments": {"oops": 1}})  # payments present but not a list
    with pytest.raises(GoogleApiResponseError):
        GoogleAdSensePaymentClient(http=http).fetch_payments(account_id="pub-1")


def test_report_id_is_deterministic_per_account() -> None:
    http = _FakeHttp({"payments": []})
    a = GoogleAdSensePaymentClient(http=http).fetch_payments(account_id="pub-1")
    b = GoogleAdSensePaymentClient(http=http).fetch_payments(account_id="pub-1")
    assert a["report_id"] == b["report_id"]
