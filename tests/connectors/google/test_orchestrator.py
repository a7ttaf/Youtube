"""run_one orchestrator tests (B2.4 happy path, T27).

The happy-path test stubs the YouTube Reporting client and blob backend so
the full pipeline (load credential -> resolve secret -> build OAuth ->
start_run -> per-report blob/raw_file/parse/upsert/mark_parsed -> finish_run)
runs against an in-memory SQLite without reaching the network. Failure
handlers (buckets B/C) and dry-run land in T28 / T29 with their own tests.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google import (
    local_secret_resolver,
    secret_resolver,
)
from ums_smart_revenue.connectors.runs.orchestrator import (
    ConnectorRunOutcome,
    run_one,
)
from ums_smart_revenue.db.connector_models import (
    ConnectorRunORM,
    ConnectorRunRawFileORM,
)
from ums_smart_revenue.db.report_models import RawReportFileORM, ReportBase
from ums_smart_revenue.db.security_models import (
    ApiConnectorCredentialORM,
    SecurityBase,
    UserORM,
)
from ums_smart_revenue.db.source_models import (
    CurrencyORM,
    GoogleRevenueSourceRowORM,
)
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM

TENANT_ID = UUID("00000000-0000-0000-0000-000000827001")
ACCOUNT_ID = "content-owner-1"
CONNECTOR_KEY = "youtube-reporting"


@pytest.fixture
def session() -> Session:
    """In-memory SQLite with the multi-base schema and FK seeds the
    orchestrator's source-row upsert + credential lookup need.

    Mirrors test_google_source_ingestion_flow.py's seed pattern: tenant +
    USD currency are required by ``SqlAlchemyGoogleRevenueSourceRowRepository
    .upsert_many``'s tenant FK pre-check and currency existence guard
    respectively. SecurityBase carries the ``ApiConnectorCredentialORM``
    table the orchestrator's ``_load_credential`` reads from.
    """
    engine = create_engine("sqlite+pysqlite:///:memory:")
    SecurityBase.metadata.create_all(engine)
    TenantBase.metadata.create_all(engine)
    ReportBase.metadata.create_all(engine)
    # FinanceBase is imported transitively by source_models so the
    # google_revenue_source_rows table is registered via the import above.
    from ums_smart_revenue.db.finance_models import FinanceBase

    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        now = datetime.now(UTC)
        session.add_all(
            [
                TenantORM(id=TENANT_ID, slug="tenant-orch", display_name="Orch Tenant"),
                CurrencyORM(
                    code="USD",
                    numeric_code="840",
                    name="US Dollar",
                    minor_unit=2,
                    is_supported=True,
                    activated_at=now,
                ),
            ]
        )
        session.flush()
        yield session
        # Test isolation: a left-over user/credential row would leak
        # between tests in the same module; engine.dispose() on the with-
        # block exit handles the schema drop implicitly.


@pytest.fixture
def stub_secret_resolver():
    """Register a ``local-secret://yt-creds`` resolver for the orchestrator's
    secret-resolve call.

    The mapping holds a JSON-encoded OAuth payload that
    ``build_credentials_from_payload`` will accept; the actual refresh path
    is patched in the test so no network call leaks. Registry state is
    cleared on teardown so unrelated tests can't see this resolver.
    """
    mapping = {
        "yt-creds": json.dumps(
            {
                "refresh_token": "rt",
                "client_id": "cid",
                "client_secret": "cs",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    }
    secret_resolver.register_resolver(
        scheme="local-secret",
        resolver=local_secret_resolver.LocalSecretResolver(mapping=mapping),
    )
    yield
    secret_resolver._REGISTRY.clear()


def _make_credential_row(
    session: Session,
    *,
    tenant_id: UUID,
    connector_key: str,
    account_id: str,
) -> ApiConnectorCredentialORM:
    """Seed the credential row the orchestrator will load.

    Persists a parent ``UserORM`` so the FK on ``created_by`` /
    ``updated_by`` doesn't fail; the SecurityBase tables enforce both
    via composite tenant FKs.
    """
    actor_id = uuid4()
    session.add(
        UserORM(
            id=actor_id,
            tenant_id=tenant_id,
            email=f"orch-{actor_id}@example.com",
            display_name="Orchestrator Actor",
        )
    )
    session.flush()
    row = ApiConnectorCredentialORM(
        id=uuid4(),
        tenant_id=tenant_id,
        connector_key=connector_key,
        account_id=account_id,
        encrypted_secret_ref="local-secret://yt-creds",
        status="active",
        created_by=actor_id,
        updated_by=actor_id,
    )
    session.add(row)
    session.flush()
    return row


def _csv_for_one_row() -> bytes:
    """CSV bytes the runner's ``_csv_to_parser_payload`` will convert to a
    single parser row.

    Uses the documented Google column names plus a currencyCode so the
    parser doesn't have to default; keeps the test independent of
    default-currency behaviour. ``2026-05-01`` keeps the row inside the
    requested ``2026-05`` month so the parser's calendar-month bucketing
    accepts it.
    """
    return (
        b"date,channel,content_owner,estimatedRevenue,currencyCode\n"
        b"2026-05-01,UC_orch_alpha,cms-orch-1,12.345600,USD\n"
    )


def test_run_one_happy_path_writes_run_raw_file_and_source_rows(
    session: Session, stub_secret_resolver
) -> None:
    """Drive the orchestrator end-to-end against a single-report fixture.

    Mocks ``YouTubeReportingClient`` (network out) and
    ``LocalFileStoreBackend`` (disk out) at the orchestrator's module
    scope so the import-time-bound symbols in
    ``ums_smart_revenue.connectors.runs.orchestrator`` get replaced; the
    runner instantiates them by bare name so the patches take effect.

    Also patches ``refresh_credentials`` so the OAuth refresh doesn't try
    to contact Google. Asserts: outcome shape (immutable, run is the
    finished ``ConnectorRunEntry``), status SUCCEEDED, counts contract,
    and durable side-effects on connector_runs / raw_report_files /
    connector_run_raw_files / google_revenue_source_rows.
    """
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    csv_bytes = _csv_for_one_row()

    with patch(
        "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
    ) as yt_client_cls, patch(
        "ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend"
    ) as local_cls, patch(
        "ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials"
    ) as refresh, patch(
        "ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient"
    ) as http_cls:
        # GoogleHttpClient is patched so the runner doesn't try to build
        # a real httpx.Client (which would attempt google-auth refresh on
        # the stub credentials). The patched class is referenced only via
        # close() in the runner's finally block.
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"}
        ]
        client.list_reports_for_month.return_value = [
            {"id": "r1", "downloadUrl": "https://yt/r1"}
        ]
        client.fetch_report.return_value = csv_bytes

        backend = local_cls.return_value
        store: dict[str, bytes] = {}

        def fake_upload(*, storage_uri, content):
            store[storage_uri] = content

        def fake_get(*, storage_uri):
            return store[storage_uri]

        backend.upload.side_effect = fake_upload
        backend.get_bytes.side_effect = fake_get

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    # ----- outcome shape -----
    assert isinstance(outcome, ConnectorRunOutcome)
    assert outcome.run is not None
    assert outcome.run.status == "SUCCEEDED"
    assert outcome.per_report_failures == []

    # ----- counts contract -----
    counts = outcome.counts
    assert counts["reports_attempted"] == 1
    assert counts["reports_succeeded"] == 1
    assert counts["reports_failed"] == 0
    assert counts["rows_upserted_total"] >= 1

    # ----- durable side effects -----
    run_row = session.scalar(
        select(ConnectorRunORM).where(ConnectorRunORM.tenant_id == TENANT_ID)
    )
    assert run_row is not None
    assert run_row.status == "SUCCEEDED"
    assert run_row.finished_at is not None
    assert run_row.error_summary is None

    raw_files = session.scalars(
        select(RawReportFileORM).where(RawReportFileORM.tenant_id == TENANT_ID)
    ).all()
    assert len(raw_files) == 1
    assert raw_files[0].parse_status == "PARSED"
    assert raw_files[0].source == "youtube_reporting"
    assert raw_files[0].report_type == "channel_basic_a2"
    assert raw_files[0].report_month == "2026-05"
    # Checksum is hex SHA-256 of the CSV bytes (64 chars); the orchestrator
    # delegates to compute_checksum so identical bytes always hash the same.
    assert len(raw_files[0].checksum) == 64

    links = session.scalars(
        select(ConnectorRunRawFileORM).where(
            ConnectorRunRawFileORM.tenant_id == TENANT_ID
        )
    ).all()
    assert len(links) == 1
    assert links[0].ordering_index == 0

    source_rows = session.scalars(
        select(GoogleRevenueSourceRowORM).where(
            GoogleRevenueSourceRowORM.tenant_id == TENANT_ID
        )
    ).all()
    assert len(source_rows) == counts["rows_upserted_total"]
    assert source_rows[0].source_system == "youtube_reporting"
    assert source_rows[0].report_type == "channel_basic_a2"
    assert source_rows[0].report_month == "2026-05"
    assert source_rows[0].currency_code == "USD"
    # raw_file_id provenance survives the upsert: the COALESCE-on-conflict
    # behaviour in the existing repo preserves it on re-runs too.
    assert source_rows[0].raw_file_id == raw_files[0].id
