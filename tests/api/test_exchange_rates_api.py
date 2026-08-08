from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.finance_models import CurrencyExchangeRateORM, FinanceBase
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM

USER_ID = UUID("00000000-0000-0000-0000-00000000e301")


def auth_headers(
    role: str,
    scope_type: str = "global",
    scope_id: str | None = None,
    user_id: str | UUID = USER_ID,
) -> dict[str, str]:
    headers = {
        "x-user-id": str(user_id),
        "x-user-email": "exchange-rates@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def seed_database(database_url: str) -> None:
    engine = create_engine(database_url)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            UserORM(
                id=USER_ID,
                email="exchange-rates@example.com",
                display_name="Exchange Rates User",
            )
        )
        session.commit()


def test_system_integration_user_syncs_exchange_rate_with_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/exchange-rates/sync",
        headers=auth_headers("system_integration_user", "connector", "ecb"),
        json={
            "provider_key": "ecb",
            "source_report_id": "ecb-2026-04-22",
            "reason": "Sync official daily FX rate",
            "rates": [
                {
                    "rate_date": "2026-04-22",
                    "base_currency": "EUR",
                    "quote_currency": "USD",
                    "rate": "1.0845",
                    "raw_payload": {"source": "ecb"},
                }
            ],
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        rate = session.scalars(select(CurrencyExchangeRateORM)).one()
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 200
    assert response.json()["synced_count"] == 1
    assert response.json()["items"][0]["rate"] == "1.0845"
    assert response.json()["items"][0]["raw_payload"] is None
    assert response.json()["audit_event"]["event_type"] == "EXCHANGE_RATE_SYNCED"
    assert rate.provider_key == "ecb"
    assert rate.raw_payload == {"source": "ecb"}
    assert audit_log.event_type == "EXCHANGE_RATE_SYNCED"
    assert audit_log.scope_type == "connector"
    assert audit_log.scope_id == "ecb"
    assert audit_log.is_sensitive is True


def test_system_integration_user_syncs_exchange_rate_with_non_uuid_actor(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/exchange-rates/sync",
        headers=auth_headers(
            "system_integration_user",
            "connector",
            "ecb",
            user_id="gateway-subject-ecb",
        ),
        json={
            "provider_key": "ecb",
            "source_report_id": "ecb-2026-04-22",
            "reason": "Sync official daily FX rate",
            "rates": [
                {
                    "rate_date": "2026-04-22",
                    "base_currency": "EUR",
                    "quote_currency": "USD",
                    "rate": "1.0845",
                    "raw_payload": {"source": "ecb"},
                }
            ],
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        rate = session.scalars(select(CurrencyExchangeRateORM)).one()
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 200
    assert rate.imported_by is None
    assert audit_log.user_id is None
    assert audit_log.details["actor_user_id"] == "gateway-subject-ecb"


def test_finance_viewer_reads_latest_exchange_rate(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add_all(
            [
                CurrencyExchangeRateORM(
                    id=uuid4(),
                    rate_date=date(2026, 4, 20),
                    base_currency="EUR",
                    quote_currency="USD",
                    rate=Decimal("1.0800"),
                    provider_key="ecb",
                    raw_payload={},
                    imported_by=USER_ID,
                ),
                CurrencyExchangeRateORM(
                    id=uuid4(),
                    rate_date=date(2026, 4, 22),
                    base_currency="EUR",
                    quote_currency="USD",
                    rate=Decimal("1.0845"),
                    provider_key="ecb",
                    raw_payload={},
                    imported_by=USER_ID,
                ),
            ]
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/exchange-rates/latest?base_currency=EUR&quote_currency=USD"
        "&as_of_date=2026-04-21&provider_key=ecb",
        headers=auth_headers("finance_viewer", "global"),
    )

    with Session(engine) as session:
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 200
    assert response.json()["rate_date"] == "2026-04-20"
    assert response.json()["rate"] == "1.08"
    assert response.json()["raw_payload"] is None
    assert audit_log.event_type == "REVENUE_VIEWED"
    assert audit_log.entity_type == "currency_exchange_rate"
    assert audit_log.entity_id == response.json()["id"]
    assert audit_log.scope_type == "global"
    assert audit_log.is_sensitive is True


def test_finance_viewer_cannot_sync_exchange_rates(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/exchange-rates/sync",
        headers=auth_headers("finance_viewer", "global"),
        json={
            "provider_key": "ecb",
            "reason": "Should be denied",
            "rates": [
                {
                    "rate_date": "2026-04-22",
                    "base_currency": "EUR",
                    "quote_currency": "USD",
                    "rate": "1.0845",
                }
            ],
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: connectors.run_jobs"


def test_exchange_rate_sync_rejects_unquantizable_rate(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/exchange-rates/sync",
        headers=auth_headers("system_integration_user", "connector", "ecb"),
        json={
            "provider_key": "ecb",
            "source_report_id": "ecb-oversized",
            "reason": "Validate oversized rate",
            "rates": [
                {
                    "rate_date": "2026-04-22",
                    "base_currency": "EUR",
                    "quote_currency": "USD",
                    "rate": "123456789012345678901234567890.1234567890",
                }
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"].startswith("rate has too many significant digits:")


def test_exchange_rate_sync_rejects_rate_that_rounds_to_zero(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/exchange-rates/sync",
        headers=auth_headers("system_integration_user", "connector", "ecb"),
        json={
            "provider_key": "ecb",
            "source_report_id": "ecb-zero-rate",
            "reason": "Validate sub-quantum rate",
            "rates": [
                {
                    "rate_date": "2026-04-22",
                    "base_currency": "EUR",
                    "quote_currency": "USD",
                    "rate": "0.00000000001",
                }
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"].startswith("rate rounds to zero after quantization:")
