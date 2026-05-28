"""AdSense Management v2 accounts.payments.list client (live payment sync).

Endpoint: GET https://adsense.googleapis.com/v2/accounts/{account}/payments

Unlike reports.generate, accounts.payments.list is NOT paginated: the full
list of unpaid/scheduled payment rows comes back in a single GET wrapped in a
top-level `payments` array. The downstream live payment sync (Task 8) calls
fetch_payments(account_id=...) exactly once per account.

accounts.payments.list does not echo a stable report id, so this client stamps
a deterministic SHA-256 of (account_id, locked-report-key) as `report_id`. The
stamp is per-account stable, so re-pulls map to the same source_report_id
provenance downstream.
"""
from __future__ import annotations

import hashlib

from ums_smart_revenue.connectors.google.adsense_management_client import (
    _validated_account_id,
)
from ums_smart_revenue.connectors.google.errors import GoogleApiResponseError
from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient

_PAYMENTS_URL = "https://adsense.googleapis.com/v2/accounts/{account}/payments"
_REPORT_KEY = "accounts.payments.list"


class GoogleAdSensePaymentClient:
    """Thin wrapper around GoogleHttpClient for AdSense accounts.payments.list."""

    def __init__(self, *, http: GoogleHttpClient) -> None:
        """Bind the shared HTTP client (auth + retry + JSON decode)."""
        self._http = http

    # ========================================================================
    # Purpose: Fetch one AdSense account's accounts.payments.list body (the
    #   whole, unpaginated payment list) and fail closed on any unexpected
    #   shape, then stamp a deterministic per-account report_id used downstream
    #   as source_report_id.
    # Database/ORM: None (pure API call + response-shape validation).
    # Standards: Fail-closed shape validation. A non-object response, a missing
    #   `payments` field, or a non-list `payments` is treated as a schema gap
    #   (not an empty account) and raises GoogleApiResponseError. Typed
    #   keyword-only boundary; MalformedAdsenseAccountIdError on bad account id.
    # Blast Radius: Feeds the live payment sync. A wrong shape accepted here
    #   would ingest a partial or garbage payment set into the sync pipeline,
    #   so we refuse to ingest anything but a validated `payments` list.
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/google/
    #     adsense_payment_mapping.py -> consumes the validated `payments` rows.
    #   - File: backend/ums_smart_revenue/connectors/google/
    #     adsense_payment_sync.py -> orchestrates the single fetch per account
    #     and persists using the stamped report_id as source_report_id.
    # ========================================================================
    def fetch_payments(self, *, account_id: str) -> dict[str, object]:
        """Fetch one AdSense account's payments list, fail-closed + stamped.

        Raises:
            MalformedAdsenseAccountIdError: If ``account_id`` is blank or unsafe.
            GoogleApiResponseError: If the response is not an object, is missing
                the ``payments`` field, or ``payments`` is not a list.
        """
        account = _validated_account_id(account_id)
        url = _PAYMENTS_URL.format(account=account)
        response = self._http.request(method="GET", url=url)
        # GoogleHttpClient.request already guarantees a dict on the real path,
        # but a fake/transport that bypasses it must still fail closed here.
        if not isinstance(response, dict):
            raise GoogleApiResponseError(
                url=url, reason="payments response is not an object"
            )
        # accounts.payments.list wraps the full (unpaginated) list in
        # `payments`. A missing or non-list field is a schema gap, not an
        # empty account, so fail closed rather than ingest a partial shape.
        payments = response.get("payments")
        if not isinstance(payments, list):
            raise GoogleApiResponseError(
                url=url,
                reason=f"expected 'payments' list, got {type(payments).__name__}",
            )
        report_id = hashlib.sha256(
            f"{account}|{_REPORT_KEY}".encode()
        ).hexdigest()
        return {**response, "account_id": account, "report_id": report_id}
