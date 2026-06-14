import pytest

from ums_smart_revenue.connectors.google.adsense_payments_client import (
    GoogleAdSensePaymentClient,
)
from ums_smart_revenue.connectors.google.errors import (
    GoogleApiResponseError,
    MalformedAdsenseAccountIdError,
)


class _FakeHttp:
    """Capture AdSense payment-client requests and return a fixed response."""

    def __init__(self, response):
        """Store the fake JSON response and initialize the request log."""
        self.response = response
        self.calls = []

    def request(self, *, method, url, params=None, json_body=None):
        """Record the request tuple and return the configured response."""
        self.calls.append((method, url, params))
        return self.response


def test_fetch_payments_calls_correct_endpoint() -> None:
    """Fetches the canonical AdSense payments endpoint and stamps provenance."""
    http = _FakeHttp({"payments": []})
    client = GoogleAdSensePaymentClient(http=http)
    result = client.fetch_payments(account_id="pub-123")
    method, url, _ = http.calls[0]
    assert method == "GET"
    assert url == "https://adsense.googleapis.com/v2/accounts/pub-123/payments"
    assert "report_id" in result and isinstance(result["report_id"], str)
    assert result["payments"] == []


def test_fetch_payments_strips_accounts_prefix() -> None:
    """Normalizes a stored account resource name to exactly one path prefix."""
    http = _FakeHttp({"payments": []})
    GoogleAdSensePaymentClient(http=http).fetch_payments(account_id="accounts/pub-123")
    assert http.calls[0][1] == ("https://adsense.googleapis.com/v2/accounts/pub-123/payments")


def test_fetch_payments_rejects_blank_account() -> None:
    """Rejects blank account ids before URL construction."""
    http = _FakeHttp({"payments": []})
    with pytest.raises(MalformedAdsenseAccountIdError):
        GoogleAdSensePaymentClient(http=http).fetch_payments(account_id="  ")


def test_fetch_payments_rejects_non_object_response() -> None:
    """Rejects a non-object payments-list response."""
    http = _FakeHttp([])  # list, not an object
    with pytest.raises(GoogleApiResponseError):
        GoogleAdSensePaymentClient(http=http).fetch_payments(account_id="pub-1")


def test_fetch_payments_treats_missing_payments_as_empty_list() -> None:
    """Treats Google's omitted repeated payments field as an empty account."""
    http = _FakeHttp({"notpayments": []})  # object, but no payments list
    result = GoogleAdSensePaymentClient(http=http).fetch_payments(account_id="pub-1")
    assert result["payments"] == []


def test_fetch_payments_rejects_non_list_payments() -> None:
    """Rejects a present payments field when it is not a list."""
    http = _FakeHttp({"payments": {"oops": 1}})  # payments present but not a list
    with pytest.raises(GoogleApiResponseError):
        GoogleAdSensePaymentClient(http=http).fetch_payments(account_id="pub-1")


def test_report_id_is_deterministic_per_account() -> None:
    """Builds a stable synthetic report id for repeat pulls of one account."""
    http = _FakeHttp({"payments": []})
    a = GoogleAdSensePaymentClient(http=http).fetch_payments(account_id="pub-1")
    b = GoogleAdSensePaymentClient(http=http).fetch_payments(account_id="pub-1")
    assert a["report_id"] == b["report_id"]
