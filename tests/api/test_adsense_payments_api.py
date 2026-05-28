from decimal import Decimal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.finance_models import FinanceBase, FinanceMonthCloseORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM

USER_ID = UUID("00000000-0000-0000-0000-000000009001")


def auth_headers(
    role: str,
    scope_type: str = "global",
    scope_id: str | None = None,
    user_id: str | UUID = USER_ID,
) -> dict[str, str]:
    headers = {
        "x-user-id": str(user_id),
        "x-user-email": "adsense-payments@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def seed_database(database_url: str, *, locked_month: bool = False) -> None:
    engine = create_engine(database_url)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            UserORM(
                id=USER_ID,
                email="adsense-payments@example.com",
                display_name="AdSense Payment User",
            )
        )
        if locked_month:
            session.add(
                FinanceMonthCloseORM(
                    month="2026-03",
                    status="LOCKED",
                    locked_by=USER_ID,
                )
            )
        session.commit()


def payment_payload(amount: str = "930.00") -> dict[str, object]:
    return {
        "connector_key": "adsense",
        "source_report_id": "raw-adsense-payment-2026-03",
        "reason": "Sync official AdSense payment record for March close",
        "payments": [
            {
                "source_account_id": "pub-1",
                "month": "2026-03",
                "payment_name": "AdSense payment March 2026",
                "payment_date": "2026-04-21",
                "payment_amount": amount,
                "payment_currency": "USD",
                "payment_status": "PAID",
                "raw_payload": {"paymentId": "pay_2026_03", "source": "adsense"},
            }
        ],
    }


def test_system_integration_user_syncs_adsense_payment_with_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/adsense/sync-payments",
        headers=auth_headers("system_integration_user", "connector", "adsense"),
        json=payment_payload(),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        payment_row = (
            session.execute(text("SELECT * FROM adsense_payments")).mappings().one()
        )
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 200
    assert response.json()["synced_count"] == 1
    assert response.json()["items"][0]["payment_amount"] == "930"
    assert response.json()["items"][0]["payment_status"] == "PAID"
    assert response.json()["audit_event"]["event_type"] == "ADSENSE_PAYMENT_SYNCED"
    assert response.json()["audit_event"]["sensitive"] is True
    assert payment_row["month"] == "2026-03"
    assert payment_row["payment_amount"] == Decimal("930.000000")
    assert audit_log.event_type == "ADSENSE_PAYMENT_SYNCED"
    assert audit_log.scope_type == "connector"
    assert audit_log.scope_id == "adsense"
    assert audit_log.sensitive is True


def test_system_integration_user_sync_accepts_non_uuid_gateway_actor(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/adsense/sync-payments",
        headers=auth_headers(
            "system_integration_user",
            "connector",
            "adsense",
            user_id="gateway-subject-adsense",
        ),
        json=payment_payload(),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        payment_row = (
            session.execute(text("SELECT * FROM adsense_payments")).mappings().one()
        )
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 200
    assert payment_row["imported_by"] is None
    assert audit_log.user_id is None
    assert audit_log.details["actor_user_id"] == "gateway-subject-adsense"


def test_adsense_payment_sync_is_idempotent_for_same_month_payment_name(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    first = client.post(
        "/adsense/sync-payments",
        headers=auth_headers("system_integration_user", "connector", "adsense"),
        json=payment_payload("930.00"),
    )
    second = client.post(
        "/adsense/sync-payments",
        headers=auth_headers("system_integration_user", "connector", "adsense"),
        json=payment_payload("931.25"),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        payment_count = session.execute(
            text("SELECT COUNT(*) FROM adsense_payments")
        ).scalar_one()
        payment_amount = session.execute(
            text("SELECT payment_amount FROM adsense_payments")
        ).scalar_one()

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["items"][0]["payment_amount"] == "931.25"
    assert payment_count == 1
    assert payment_amount == Decimal("931.250000")


def test_adsense_payment_sync_rejects_duplicate_keys_within_batch(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    payload = payment_payload("930.00")
    payload["payments"].append(
        {
            **payload["payments"][0],
            "payment_amount": "931.25",
            "raw_payload": {"paymentId": "pay_2026_03_duplicate", "source": "adsense"},
        }
    )

    response = client.post(
        "/adsense/sync-payments",
        headers=auth_headers("system_integration_user", "connector", "adsense"),
        json=payload,
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        payment_count = session.execute(
            text("SELECT COUNT(*) FROM adsense_payments")
        ).scalar_one()

    assert response.status_code == 422
    assert "duplicate AdSense payment in batch" in response.json()["detail"]
    assert payment_count == 0


def test_finance_viewer_lists_adsense_payments_with_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    create_response = client.post(
        "/adsense/sync-payments",
        headers=auth_headers("system_integration_user", "connector", "adsense"),
        json=payment_payload(),
    )

    response = client.get(
        "/adsense/payments?month=2026-03",
        headers=auth_headers("finance_viewer", "global"),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_logs = session.scalars(
            select(AuditLogORM).order_by(AuditLogORM.created_at, AuditLogORM.id)
        ).all()

    assert create_response.status_code == 200
    assert response.status_code == 200
    assert response.json()["items"][0]["month"] == "2026-03"
    assert response.json()["items"][0]["payment_name"] == "AdSense payment March 2026"
    assert response.json()["audit_event"]["event_type"] == "PAYMENT_VIEWED"
    assert [log.event_type for log in audit_logs] == [
        "ADSENSE_PAYMENT_SYNCED",
        "PAYMENT_VIEWED",
    ]
    assert audit_logs[-1].scope_type == "finance-month"
    assert audit_logs[-1].scope_id == "2026-03"
    assert audit_logs[-1].sensitive is True


def test_finance_month_scoped_viewer_lists_matching_adsense_payments(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    create_response = client.post(
        "/adsense/sync-payments",
        headers=auth_headers("system_integration_user", "connector", "adsense"),
        json=payment_payload(),
    )

    response = client.get(
        "/adsense/payments?month=2026-03",
        headers=auth_headers("finance_viewer", "finance-month", "2026-03"),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_logs = session.scalars(
            select(AuditLogORM).order_by(AuditLogORM.created_at, AuditLogORM.id)
        ).all()

    assert create_response.status_code == 200
    assert response.status_code == 200
    assert response.json()["items"][0]["month"] == "2026-03"
    assert audit_logs[-1].scope_type == "finance-month"
    assert audit_logs[-1].scope_id == "2026-03"


def test_finance_month_scoped_viewer_cannot_list_another_month(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/adsense/payments?month=2026-04",
        headers=auth_headers("finance_viewer", "finance-month", "2026-03"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Missing permission: finance.view_finalized_payments"
    )


def test_assistant_cannot_view_adsense_payments(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/adsense/payments?month=2026-03",
        headers=auth_headers("assistant_analyst", "global"),
    )

    assert response.status_code == 403
    assert (
        response.json()["detail"]
        == "Missing permission: finance.view_finalized_payments"
    )


def test_sync_requires_adsense_connector_scope(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/adsense/sync-payments",
        headers=auth_headers(
            "system_integration_user",
            "connector",
            "youtube_reporting",
        ),
        json=payment_payload(),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: connectors.run_jobs"


def test_sync_rejects_locked_finance_month(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url, locked_month=True)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/adsense/sync-payments",
        headers=auth_headers("system_integration_user", "connector", "adsense"),
        json=payment_payload(),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        payment_count = session.execute(
            text("SELECT COUNT(*) FROM adsense_payments")
        ).scalar_one()

    assert response.status_code == 409
    assert (
        response.json()["detail"] == "Finance month is locked for AdSense payment sync"
    )
    assert payment_count == 0


def test_manual_sync_requires_source_account_id(tmp_path) -> None:
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    body = payment_payload()
    del body["payments"][0]["source_account_id"]
    response = client.post(
        "/adsense/sync-payments",
        headers=auth_headers("system_integration_user", "connector", "adsense"),
        json=body,
    )
    assert response.status_code == 422  # Pydantic rejects before any DB write


def test_manual_sync_rejects_blank_source_account_id(tmp_path) -> None:
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    body = payment_payload()
    body["payments"][0]["source_account_id"] = "   "
    response = client.post(
        "/adsense/sync-payments",
        headers=auth_headers("system_integration_user", "connector", "adsense"),
        json=body,
    )
    assert response.status_code == 422


def test_manual_sync_canonicalizes_accounts_prefix(tmp_path) -> None:
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    body = payment_payload()
    body["payments"][0]["source_account_id"] = "accounts/pub-1"
    response = client.post(
        "/adsense/sync-payments",
        headers=auth_headers("system_integration_user", "connector", "adsense"),
        json=body,
    )
    assert response.status_code == 200
    # The `accounts/` prefix is stripped by the shared normalizer, so the
    # persisted/returned identity is the canonical bare publisher id.
    assert response.json()["items"][0]["source_account_id"] == "pub-1"


def test_manual_sync_rejects_malformed_account_path_chars(tmp_path) -> None:
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    body = payment_payload()
    body["payments"][0]["source_account_id"] = "pub/../etc"  # reserved '/'
    response = client.post(
        "/adsense/sync-payments",
        headers=auth_headers("system_integration_user", "connector", "adsense"),
        json=body,
    )
    assert response.status_code == 422  # MalformedAdsenseAccountIdError -> 422
