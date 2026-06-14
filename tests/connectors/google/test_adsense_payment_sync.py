from datetime import date
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.connectors.google.adsense_payment_mapping import (
    AdSensePaymentMappingError,
)
from ums_smart_revenue.connectors.google.adsense_payment_sync import (
    AdSensePaymentSyncService,
)
from ums_smart_revenue.connectors.google.errors import CredentialNotFoundError
from ums_smart_revenue.db.finance_models import (
    AdSensePaymentORM,
    FinanceBase,
)
from ums_smart_revenue.finance.month_close import get_or_create_month_close_row

TENANT_ID = UUID("00000000-0000-0000-0000-000000031001")
ACTOR = UserPrincipal(
    user_id="00000000-0000-0000-0000-000000031001",
    email="connector-service@ums.example",
)


def test_connector_key_constant_is_canonical() -> None:
    """Documents the connector key used for live AdSense management calls."""
    from ums_smart_revenue.connectors.google.registry import (
        ADSENSE_MANAGEMENT_CONNECTOR_KEY,
    )

    assert ADSENSE_MANAGEMENT_CONNECTOR_KEY == "adsense-management"


def test_resolve_connector_credentials_is_public() -> None:
    """Keeps the sync service wired to the shared credential resolver."""
    from ums_smart_revenue.connectors.runs.orchestrator import (
        resolve_connector_credentials,
    )

    assert callable(resolve_connector_credentials)


def _build_session() -> Session:
    """Create an isolated in-memory finance schema for sync-service tests."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)
    return Session(engine)


def _resp(*payments, account="pub-1"):
    """Build the fake payment-client response consumed by the sync service."""
    # Mirrors GoogleAdSensePaymentClient.fetch_payments output (account_id +
    # report_id already stamped) so the fake stands in for the real client.
    return {"payments": list(payments), "account_id": account, "report_id": "rep-abc"}


def _p(name, date_obj, amount):
    """Build one Google Payment-shaped dict for mapping tests."""
    d = {"name": name, "amount": amount}
    if date_obj is not None:
        d["date"] = {
            "year": date_obj.year,
            "month": date_obj.month,
            "day": date_obj.day,
        }
    return d


class _FakeClient:
    """Return a fixed payment response while recording requested accounts."""

    def __init__(self, response):
        """Store the response and initialize the account call log."""
        self._response = response
        self.account_calls = []

    def fetch_payments(self, *, account_id):
        """Record the account id and return the configured response."""
        self.account_calls.append(account_id)
        return self._response


def _lock_month(session, month):
    """Mark a finance month LOCKED without invoking the full lock workflow."""
    row = get_or_create_month_close_row(session, month, tenant_id=TENANT_ID, for_update=False)
    row.status = "LOCKED"
    session.flush()


def _service(session, client, audit):
    """Build the sync service with fake credentials and a fake client."""
    return AdSensePaymentSyncService(
        session,
        audit_sink=audit,
        credential_resolver=lambda **_: object(),  # no real OAuth
        client_factory=lambda _creds: client,  # no real HTTP
    )


def test_sync_upserts_open_month_settlements() -> None:
    """Persists open paid settlements and audits retained-balance skips."""
    session = _build_session()
    audit = InMemoryAuditSink()
    client = _FakeClient(
        _resp(
            _p("accounts/pub-1/payments/2026-04-10", date(2026, 4, 10), "£60.00"),
            _p("accounts/pub-1/payments/unpaid", None, "£5.00"),
        )
    )
    result = _service(session, client, audit).sync(
        tenant_id=TENANT_ID,
        account_id="pub-1",
        actor=ACTOR,
        reason="live pull",
    )
    session.commit()
    assert result.synced_count == 1
    assert result.skipped_balance_count == 1
    rows = session.scalars(select(AdSensePaymentORM)).all()
    assert len(rows) == 1
    assert rows[0].source_account_id == "pub-1"
    assert rows[0].payment_status == "PAID"
    assert rows[0].payment_currency == "GBP"
    assert rows[0].source_report_id == "rep-abc"
    # raw_payload retains the raw formatted amount + the Google resource name.
    assert rows[0].raw_payload["amount"] == "£60.00"
    assert rows[0].raw_payload["name"] == "accounts/pub-1/payments/2026-04-10"
    # audit carries the live-pull discriminator + capped skip evidence.
    assert len(audit.records) == 1
    details = audit.records[0].details
    assert details["trigger"] == "live_pull"
    assert details["source_account_id"] == "pub-1"
    assert details["skipped_balances"][0]["resource_name"] == ("accounts/pub-1/payments/unpaid")


def test_sync_is_idempotent() -> None:
    """Repeating the same live pull updates the existing account-scoped row."""
    session = _build_session()
    audit = InMemoryAuditSink()
    client = _FakeClient(
        _resp(
            _p("accounts/pub-1/payments/2026-04-10", date(2026, 4, 10), "£60.00"),
        )
    )
    svc = _service(session, client, audit)
    svc.sync(tenant_id=TENANT_ID, account_id="pub-1", actor=ACTOR, reason="r")
    svc.sync(tenant_id=TENANT_ID, account_id="pub-1", actor=ACTOR, reason="r")
    session.commit()
    assert len(session.scalars(select(AdSensePaymentORM)).all()) == 1


def test_sync_canonicalizes_account_before_credentials_and_fetch() -> None:
    """Normalizes accounts/pub-* before credential lookup and Google fetch."""
    session = _build_session()
    audit = InMemoryAuditSink()
    client = _FakeClient(_resp())
    credential_accounts = []

    def _resolver(**kwargs):
        """Capture the account id used for credential resolution."""
        credential_accounts.append(kwargs["account_id"])
        return object()

    svc = AdSensePaymentSyncService(
        session,
        audit_sink=audit,
        credential_resolver=_resolver,
        client_factory=lambda _creds: client,
    )

    svc.sync(
        tenant_id=TENANT_ID,
        account_id="accounts/pub-1",
        actor=ACTOR,
        reason="canonical account",
    )

    assert credential_accounts == ["pub-1"]
    assert client.account_calls == ["pub-1"]
    assert audit.records[0].details["source_account_id"] == "pub-1"


def test_locked_month_is_skipped_not_aborted() -> None:
    """Skips locked-month settlements before parsing ambiguous currency."""
    session = _build_session()
    audit = InMemoryAuditSink()
    # one paid row in a LOCKED month ('$' ambiguous) + one in an OPEN month (GBP)
    client = _FakeClient(
        _resp(
            _p("accounts/pub-1/payments/2026-03-10", date(2026, 3, 10), "$50.00"),
            _p("accounts/pub-1/payments/2026-04-10", date(2026, 4, 10), "£60.00"),
        )
    )
    _lock_month(session, "2026-03")
    result = _service(session, client, audit).sync(
        tenant_id=TENANT_ID,
        account_id="pub-1",
        actor=ACTOR,
        reason="r",
    )
    session.commit()
    assert result.skipped_locked_count == 1  # the '$' locked row never parsed
    assert result.synced_count == 1  # only the open GBP row
    rows = session.scalars(select(AdSensePaymentORM)).all()
    assert {r.month for r in rows} == {"2026-04"}
    locked_meta = audit.records[0].details["skipped_locked"][0]
    assert locked_meta["month"] == "2026-03"
    assert locked_meta["reason"] == "month_locked"
    assert locked_meta["raw_amount"] == "$50.00"  # raw preserved, never parsed


def test_nothing_remains_audits_zero_synced() -> None:
    """Audits a successful live pull even when only balances are returned."""
    session = _build_session()
    audit = InMemoryAuditSink()
    client = _FakeClient(
        _resp(
            _p("accounts/pub-1/payments/unpaid", None, "£5.00"),
            _p("accounts/pub-1/payments/youtube-unpaid", None, "£3.00"),
        )
    )
    result = _service(session, client, audit).sync(
        tenant_id=TENANT_ID,
        account_id="pub-1",
        actor=ACTOR,
        reason="r",
    )
    session.commit()
    assert result.synced_count == 0
    assert result.skipped_balance_count == 2
    assert session.scalars(select(AdSensePaymentORM)).all() == []  # no payment rows
    assert len(audit.records) == 1  # still audited
    assert audit.records[0].details["synced_count"] == 0


def test_credential_failure_writes_nothing() -> None:
    """Credential failures abort before payment writes or audit events."""
    session = _build_session()
    audit = InMemoryAuditSink()

    def _boom(**_):
        """Simulate a missing connector credential before any writes."""
        raise CredentialNotFoundError(connector_key="adsense-management", account_id="pub-1")

    svc = AdSensePaymentSyncService(
        session,
        audit_sink=audit,
        credential_resolver=_boom,
        client_factory=lambda _creds: _FakeClient(_resp()),
    )
    with pytest.raises(CredentialNotFoundError):
        svc.sync(tenant_id=TENANT_ID, account_id="pub-1", actor=ACTOR, reason="r")
    assert session.scalars(select(AdSensePaymentORM)).all() == []
    assert audit.records == []


def test_open_month_dollar_amount_aborts_with_zero_writes() -> None:
    """Open-month ambiguous dollar amounts fail closed with zero writes."""
    session = _build_session()
    audit = InMemoryAuditSink()
    client = _FakeClient(
        _resp(
            _p("accounts/pub-1/payments/2026-04-10", date(2026, 4, 10), "$60.00"),
        )
    )
    with pytest.raises(AdSensePaymentMappingError):
        _service(session, client, audit).sync(
            tenant_id=TENANT_ID,
            account_id="pub-1",
            actor=ACTOR,
            reason="r",
        )
    assert session.scalars(select(AdSensePaymentORM)).all() == []  # fail closed
    assert audit.records == []


def test_dry_run_writes_no_rows_and_no_audit() -> None:
    """Dry-run validates and counts would-sync rows without persistence."""
    session = _build_session()
    audit = InMemoryAuditSink()
    client = _FakeClient(
        _resp(
            _p("accounts/pub-1/payments/2026-04-10", date(2026, 4, 10), "£60.00"),
        )
    )
    result = _service(session, client, audit).sync(
        tenant_id=TENANT_ID,
        account_id="pub-1",
        actor=ACTOR,
        reason="r",
        dry_run=True,
    )
    session.commit()
    assert result.synced_count == 1  # would-sync count
    assert session.scalars(select(AdSensePaymentORM)).all() == []  # no rows
    assert audit.records == []  # no audit
