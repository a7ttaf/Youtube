# ============================================================================
# Purpose: Verify the Google connector run_one orchestration path, including
#   credential loading, report download/parse/upsert, lifecycle auditing, and
#   failure handling.
# Database/ORM: SQLite test database covering connector_runs, raw_report_files,
#   google_revenue_source_rows, audit_logs, credentials, and org/finance rows.
# Standards: Tests use faked Google/blob dependencies while preserving real
#   repository/session transitions and audit lifecycle assertions.
# Blast Radius: Test coverage only for connector ingestion, normalization,
#   audit lifecycle, and source-row projection behavior.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py -> public
#     run_one orchestration surface under test.
#   - File: backend/ums_smart_revenue/connectors/runs/normalization.py -> post
#     run projection and skipped-row audit behavior.
# ============================================================================
"""run_one orchestrator tests (B2.4 happy path + failure handlers, T27 & T28).

The happy-path test stubs the YouTube Reporting client and blob backend so
the full pipeline (load credential -> resolve secret -> build OAuth ->
start_run -> per-report blob/raw_file/parse/upsert/mark_parsed -> finish_run)
runs against an in-memory SQLite without reaching the network.

T28 adds coverage for the spec §6 failure buckets:
- Bucket A (pre-``start_run``): typed credential errors bubble; no connector_runs row.
- Bucket B (per-report inner ``except``): a single failed report flips that
  report's raw_file DOWNLOADED -> FAILED and lets the run finish as PARTIAL.
- Bucket C (post-``start_run``, escaped the loop): any escaping exception
  marks the run FAILED, commits the terminal row, and returns a FAILED outcome.
Dry-run lands in T29 with its own tests.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path as _Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google import (
    local_secret_resolver,
    secret_resolver,
)
from ums_smart_revenue.connectors.google.errors import (
    CredentialNotFoundError,
    GoogleApiResponseError,
    GoogleApiServerError,
    InactiveCredentialError,
    OAuthRefreshError,
)
from ums_smart_revenue.connectors.google.youtube_analytics_client import (
    _DIMENSIONS as _ANALYTICS_DIMENSIONS,
)
from ums_smart_revenue.connectors.google.youtube_analytics_client import (
    _METRICS as _ANALYTICS_METRICS,
)
from ums_smart_revenue.connectors.google_source_parsers.base import ParserError
from ums_smart_revenue.connectors.runs import orchestrator as orchestrator_module
from ums_smart_revenue.connectors.runs.blob_storage import compute_checksum
from ums_smart_revenue.connectors.runs.orchestrator import (
    ConnectorRunOutcome,
    ProducedReport,
    ProducedReportFailure,
    ProducedReportSuccess,
    YouTubeReportingRunner,
    _csv_to_parser_payload,  # noqa: PLC2701
    run_one,
)
from ums_smart_revenue.connectors.runs.repository import ConnectorRunValidationError
from ums_smart_revenue.db.connector_models import (
    ConnectorRunORM,
    ConnectorRunRawFileORM,
)
from ums_smart_revenue.db.org_models import YouTubeChannelORM
from ums_smart_revenue.db.report_models import RawReportFileORM, ReportBase
from ums_smart_revenue.db.security_models import (
    ApiConnectorCredentialORM,
    AuditLogORM,
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
DEFAULT_RESOLVER_REF = "local-secret://yt-creds"
# Stable service-actor UUID used by orchestrator live-path tests so the
# T36 connector audit emitters can build a service principal (the live
# path's Bucket A fail-closed check requires UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID).
# Tests that exercise the fail-closed path delenv this value explicitly.
_SERVICE_ACTOR_ID = "ddddeeee-ffff-0000-1111-222222222222"


def _next_produced_report(
    produced_iter: Iterator[ProducedReport],
) -> ProducedReport:
    for produced in produced_iter:
        return produced
    raise AssertionError("expected one aggregated YouTube report")


def _assert_produced_reports_exhausted(produced_iter: Iterator[ProducedReport]) -> None:
    for extra_report in produced_iter:
        raise AssertionError(f"expected produced reports to be exhausted, got {extra_report!r}")


def _close_produced_reports(produced_iter: Iterator[ProducedReport]) -> None:
    close = getattr(produced_iter, "close", None)
    if callable(close):
        close()


def _runner_credentials() -> Credentials:
    return Credentials(token="test-token")


@pytest.fixture(autouse=True)
def _service_actor_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Set UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID for orchestrator live-path tests.

    The autouse on this fixture lets the existing test suite continue to call
    ``run_one(..., dry_run=False)`` without each test wiring the env itself.
    Tests that explicitly verify fail-closed-on-missing-env can use
    ``monkeypatch.delenv(...)`` to override this default.

    Calls load_app_settings.cache_clear() so a cached settings object from
    an earlier-running test (different module, different env state) cannot
    poison the actor lookup.
    """
    from ums_smart_revenue.config.settings import (
        GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV,
        load_app_settings,
    )

    monkeypatch.setenv(GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV, _SERVICE_ACTOR_ID)
    load_app_settings.cache_clear()
    try:
        yield _SERVICE_ACTOR_ID
    finally:
        load_app_settings.cache_clear()


@pytest.fixture(name="session")
def _session_fixture() -> Iterator[Session]:
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
def _stub_secret_resolver():
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
    saved_registry = dict(secret_resolver._REGISTRY)
    secret_resolver._REGISTRY.clear()
    secret_resolver.register_resolver(
        scheme="local-secret",
        resolver=local_secret_resolver.LocalSecretResolver(mapping=mapping),
    )
    yield
    secret_resolver._REGISTRY.clear()
    secret_resolver._REGISTRY.update(saved_registry)


def _make_credential_row(
    session: Session,
    *,
    tenant_id: UUID,
    connector_key: str,
    account_id: str,
    encrypted_secret_ref: str | None = None,
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
    resolver_ref = encrypted_secret_ref or DEFAULT_RESOLVER_REF
    row = ApiConnectorCredentialORM(
        id=uuid4(),
        tenant_id=tenant_id,
        connector_key=connector_key,
        account_id=account_id,
        encrypted_secret_ref=resolver_ref,
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


def test_csv_adapter_aggregates_daily_breakdowns_to_monthly_channel_totals() -> None:
    """YouTube Reporting estimated-revenue CSVs are daily and may include
    video/country/metric breakdowns; the C1 normalizer consumes monthly
    channel totals, so the adapter must aggregate before parser handoff.
    """
    payload = _csv_to_parser_payload(
        raw_bytes=(
            b"date,channel_id,content_owner,video_id,country_code,"
            b"estimated_partner_revenue,estimated_monetized_playbacks,"
            b"ad_impressions,currencyCode\n"
            b"2026-05-01,UC_orch_alpha,cms-orch-1,V1,US,1.10,10,100,USD\n"
            b"2026-05-02,UC_orch_alpha,cms-orch-1,V2,EG,2.20,20,200,USD\n"
        ),
        report_id="r-monthly",
        report_type="content_owner_estimated_revenue_a1",
        month="2026-05",
    )

    assert payload["report_metadata"] == {
        "report_id": "r-monthly",
        "report_type": "content_owner_estimated_revenue_a1",
    }
    assert payload["rows"] == [
        {
            "line_index": 0,
            "date_range": {"start": "2026-05-01", "end": "2026-05-31"},
            "dimensions": {
                "channel": "UC_orch_alpha",
                "content_owner": "cms-orch-1",
            },
            "metrics": {
                "estimatedRevenue": "3.30",
                "currencyCode": "USD",
            },
        }
    ]


def test_csv_adapter_defaults_missing_reporting_currency_to_usd() -> None:
    """The whitelisted YouTube Reporting CSV schema does not include a
    currency column, so the adapter must hand the parser an explicit USD
    currency instead of rejecting the otherwise-valid export.
    """
    payload = _csv_to_parser_payload(
        raw_bytes=(
            b"date,channel_id,content_owner,video_id,country_code,"
            b"estimated_partner_revenue,ad_impressions\n"
            b"2026-05-01,UC_orch_alpha,cms-orch-1,V1,US,1.10,100\n"
            b"2026-05-02,UC_orch_alpha,cms-orch-1,V2,EG,2.20,200\n"
        ),
        report_id="r-no-currency",
        report_type="content_owner_estimated_revenue_a1",
        month="2026-05",
    )

    assert payload["rows"] == [
        {
            "line_index": 0,
            "date_range": {"start": "2026-05-01", "end": "2026-05-31"},
            "dimensions": {
                "channel": "UC_orch_alpha",
                "content_owner": "cms-orch-1",
            },
            "metrics": {
                "estimatedRevenue": "3.30",
                "currencyCode": "USD",
            },
        }
    ]


def test_csv_adapter_rejects_blank_present_currency() -> None:
    """A missing Google Reporting currency column defaults to USD, but a
    present blank currency field is malformed evidence and must fail closed.
    """
    with pytest.raises(GoogleApiResponseError, match="blank currency"):
        _csv_to_parser_payload(
            raw_bytes=(
                b"date,channel,content_owner,estimatedRevenue,currencyCode\n"
                b"2026-05-01,UC_orch_alpha,cms-orch-1,12.345600, \n"
            ),
            report_id="r-blank-currency",
            report_type="content_owner_estimated_revenue_a1",
            month="2026-05",
        )


def test_csv_adapter_rejects_non_zero_padded_report_month() -> None:
    """Non-zero-padded months (e.g. ``2026-5``) must be rejected before any HTTP call."""
    with pytest.raises(GoogleApiResponseError, match="not YYYY-MM"):
        _csv_to_parser_payload(
            raw_bytes=_csv_for_one_row(),
            report_id="r-bad-month",
            report_type="content_owner_estimated_revenue_a1",
            month="2026-5",
        )


@pytest.mark.parametrize(
    "raw_bytes",
    [
        b"",
        b"2026-05-01,UC_orch_alpha,12.345600,USD\n",
    ],
)
def test_csv_adapter_rejects_empty_or_headerless_csv(raw_bytes: bytes) -> None:
    """Empty or header-only CSVs must be flagged as a structural failure."""
    with pytest.raises(GoogleApiResponseError, match="csv missing"):
        _csv_to_parser_payload(
            raw_bytes=raw_bytes,
            report_id="r-bad-csv",
            report_type="content_owner_estimated_revenue_a1",
            month="2026-05",
        )


def test_youtube_reporting_runner_aggregates_daily_reports_before_parser_handoff(
    session: Session,
) -> None:
    """The runner must aggregate the month's daily CSVs before yielding to the parser."""
    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "content_owner_estimated_revenue_a1"}
        ]
        client.list_reports_for_month.return_value = [
            {"id": "r-day-1", "downloadUrl": "https://yt/r-day-1"},
            {"id": "r-day-2", "downloadUrl": "https://yt/r-day-2"},
        ]
        client.fetch_report.side_effect = [
            (
                b"date,channel_id,estimated_partner_revenue,currency_code\n"
                b"2026-05-01,UC_orch_alpha,1.25,USD\n"
            ),
            (
                b"date,channel_id,estimated_partner_revenue,currency_code\n"
                b"2026-05-02,UC_orch_alpha,2.75,USD\n"
            ),
        ]

        produced_iter = YouTubeReportingRunner().produce_reports(
            session=session,
            run=None,
            credentials=_runner_credentials(),
            report_month="2026-05",
            account_id=ACCOUNT_ID,
        )
        try:
            produced = _next_produced_report(produced_iter)
            assert isinstance(produced, ProducedReportSuccess)
            assert produced.report_type == "content_owner_estimated_revenue_a1"
            assert produced.parser_payload["report_metadata"] == {
                "report_id": "combined:r-day-1,r-day-2",
                "report_type": "content_owner_estimated_revenue_a1",
            }
            assert produced.parser_payload["rows"] == [
                {
                    "line_index": 0,
                    "date_range": {"start": "2026-05-01", "end": "2026-05-31"},
                    "dimensions": {
                        "channel": "UC_orch_alpha",
                        "content_owner": ACCOUNT_ID,
                    },
                    "metrics": {
                        "estimatedRevenue": "4.00",
                        "currencyCode": "USD",
                    },
                }
            ]
            assert [report.report_id for report in produced.raw_reports] == [
                "r-day-1",
                "r-day-2",
            ]
            assert produced.raw_reports[0].read_bytes() == (
                b"date,channel_id,estimated_partner_revenue,currency_code\n"
                b"2026-05-01,UC_orch_alpha,1.25,USD\n"
            )
            assert produced.raw_reports[1].read_bytes() == (
                b"date,channel_id,estimated_partner_revenue,currency_code\n"
                b"2026-05-02,UC_orch_alpha,2.75,USD\n"
            )
            _assert_produced_reports_exhausted(produced_iter)
        finally:
            _close_produced_reports(produced_iter)


def test_youtube_reporting_runner_preserves_prior_downloads_when_later_csv_fails(
    session: Session,
) -> None:
    """Mid-run CSV failures must keep the already-downloaded CSVs attached for replay."""
    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "content_owner_estimated_revenue_a1"}
        ]
        client.list_reports_for_month.return_value = [
            {"id": "r-day-1", "downloadUrl": "https://yt/r-day-1"},
            {"id": "r-day-bad", "downloadUrl": "https://yt/r-day-bad"},
        ]
        client.fetch_report.side_effect = [
            (
                b"date,channel_id,estimated_partner_revenue,currency_code\n"
                b"2026-05-01,UC_orch_alpha,1.25,USD\n"
            ),
            (b"date,channel_id,currency_code\n2026-05-02,UC_orch_alpha,USD\n"),
        ]

        produced_iter = YouTubeReportingRunner().produce_reports(
            session=session,
            run=None,
            credentials=_runner_credentials(),
            report_month="2026-05",
            account_id=ACCOUNT_ID,
        )
        try:
            produced = _next_produced_report(produced_iter)
            assert isinstance(produced, ProducedReportFailure)
            assert produced.report_type == "content_owner_estimated_revenue_a1"
            assert [report.report_id for report in produced.raw_reports] == [
                "r-day-1",
                "r-day-bad",
            ]
            assert produced.raw_reports[0].read_bytes() == (
                b"date,channel_id,estimated_partner_revenue,currency_code\n"
                b"2026-05-01,UC_orch_alpha,1.25,USD\n"
            )
            assert produced.raw_reports[1].read_bytes() == (
                b"date,channel_id,currency_code\n2026-05-02,UC_orch_alpha,USD\n"
            )
            _assert_produced_reports_exhausted(produced_iter)
        finally:
            _close_produced_reports(produced_iter)


def test_youtube_reporting_runner_deduplicates_jobs_by_report_type(
    session: Session,
) -> None:
    """Two jobs publishing the same report_type collapse to a single produced report."""
    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-primary", "reportTypeId": "content_owner_estimated_revenue_a1"},
            {"id": "job-duplicate", "reportTypeId": "content_owner_estimated_revenue_a1"},
        ]
        client.list_reports_for_month.return_value = [
            {"id": "r-day-1", "downloadUrl": "https://yt/r-day-1"}
        ]
        client.fetch_report.return_value = (
            b"date,channel_id,estimated_partner_revenue,currency_code\n"
            b"2026-05-01,UC_orch_alpha,4.00,USD\n"
        )

        produced_iter = YouTubeReportingRunner().produce_reports(
            session=session,
            run=None,
            credentials=_runner_credentials(),
            report_month="2026-05",
            account_id=ACCOUNT_ID,
        )
        try:
            produced = _next_produced_report(produced_iter)
            assert isinstance(produced, ProducedReportSuccess)
            assert produced.report_type == "content_owner_estimated_revenue_a1"
            assert client.list_reports_for_month.call_count == 1
            assert client.list_reports_for_month.call_args.kwargs["job_id"] == ("job-primary")
            _assert_produced_reports_exhausted(produced_iter)
        finally:
            _close_produced_reports(produced_iter)


def test_run_one_happy_path_writes_run_raw_file_and_source_rows(
    session: Session, _stub_secret_resolver
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

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
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
        client.list_reports_for_month.return_value = [{"id": "r1", "downloadUrl": "https://yt/r1"}]
        client.fetch_report.return_value = csv_bytes

        backend = local_cls.return_value
        store: dict[str, bytes] = {}

        def fake_upload(*, storage_uri, content):
            """Stash uploaded bytes in the in-memory blob store."""
            store[storage_uri] = content

        def fake_get(*, storage_uri):
            """Read bytes back from the in-memory blob store."""
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
    run_row = session.scalar(select(ConnectorRunORM).where(ConnectorRunORM.tenant_id == TENANT_ID))
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
        select(ConnectorRunRawFileORM).where(ConnectorRunRawFileORM.tenant_id == TENANT_ID)
    ).all()
    assert len(links) == 1
    assert links[0].ordering_index == 0

    source_rows = session.scalars(
        select(GoogleRevenueSourceRowORM).where(GoogleRevenueSourceRowORM.tenant_id == TENANT_ID)
    ).all()
    assert len(source_rows) == counts["rows_upserted_total"]
    assert source_rows[0].source_system == "youtube_reporting"
    assert source_rows[0].report_type == "channel_basic_a2"
    assert source_rows[0].report_month == "2026-05"
    assert source_rows[0].currency_code == "USD"
    # raw_file_id provenance survives the upsert: the COALESCE-on-conflict
    # behaviour in the existing repo preserves it on re-runs too.
    assert source_rows[0].raw_file_id == raw_files[0].id


def _run_missing_youtube_reporting_report(session: Session) -> tuple[ConnectorRunOutcome, int]:
    """Run the no-report YouTube path and return the outcome plus upload calls."""
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"}
        ]
        client.list_reports_for_month.return_value = []

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    assert isinstance(outcome, ConnectorRunOutcome)
    return outcome, local_cls.return_value.upload.call_count


def _assert_missing_report_failed_outcome(
    outcome: ConnectorRunOutcome,
    upload_call_count: int,
) -> None:
    """Verify the in-memory outcome for a missing YouTube report."""
    assert outcome.run is not None
    assert outcome.run.status == "FAILED"
    assert outcome.counts["reports_attempted"] == 1
    assert outcome.counts["reports_succeeded"] == 0
    assert outcome.counts["reports_failed"] == 1
    assert outcome.counts["rows_upserted_total"] == 0
    assert outcome.per_report_failures == [("channel_basic_a2", "GoogleApiResponseError")]
    assert upload_call_count == 0


def _assert_missing_report_persisted_state(session: Session) -> None:
    """Verify the stored connector state for a missing YouTube report."""
    run_row = session.scalar(select(ConnectorRunORM).where(ConnectorRunORM.tenant_id == TENANT_ID))
    assert run_row is not None
    assert run_row.status == "FAILED"
    assert run_row.error_summary is not None
    assert "GoogleApiResponseError" in run_row.error_summary
    assert "missing YouTube Reporting report for 2026-05" in run_row.error_summary
    assert (
        session.scalar(select(RawReportFileORM).where(RawReportFileORM.tenant_id == TENANT_ID))
        is None
    )
    assert (
        session.scalar(
            select(GoogleRevenueSourceRowORM).where(
                GoogleRevenueSourceRowORM.tenant_id == TENANT_ID
            )
        )
        is None
    )


def test_run_one_marks_missing_youtube_reporting_report_as_failed(
    session: Session, _stub_secret_resolver
) -> None:
    """A configured YouTube job with no monthly report must finish FAILED."""
    outcome, upload_call_count = _run_missing_youtube_reporting_report(
        session,
    )

    _assert_missing_report_failed_outcome(outcome, upload_call_count)
    _assert_missing_report_persisted_state(session)


def test_run_one_reuses_raw_file_inserted_by_racing_worker(
    session: Session, _stub_secret_resolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent worker can win the raw_report_files unique insert after
    our lookup but before our insert; this run should reuse that row instead
    of failing the report.
    """
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    csv_bytes = _csv_for_one_row()
    race_winner_id = uuid4()
    original_find = orchestrator_module._find_existing_raw_file
    calls = {"n": 0}

    def racing_find(db_session, *, tenant_id, source, report_type, report_month, checksum):
        """Race the duplicate-row lookup with a sibling insert
        to exercise the IntegrityError fallback."""
        calls["n"] += 1
        if calls["n"] == 1:
            db_session.add(
                RawReportFileORM(
                    id=race_winner_id,
                    tenant_id=tenant_id,
                    source=source,
                    report_type=report_type,
                    report_month=report_month,
                    file_url="file-store://race-winner/raw.csv",
                    checksum=checksum,
                    parse_status="DOWNLOADED",
                )
            )
            db_session.flush()
            return None
        return original_find(
            db_session,
            tenant_id=tenant_id,
            source=source,
            report_type=report_type,
            report_month=report_month,
            checksum=checksum,
        )

    monkeypatch.setattr(orchestrator_module, "_find_existing_raw_file", racing_find)

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"}
        ]
        client.list_reports_for_month.return_value = [{"id": "r1", "downloadUrl": "https://yt/r1"}]
        client.fetch_report.return_value = csv_bytes

        backend = local_cls.return_value
        store: dict[str, bytes] = {}

        def fake_upload(*, storage_uri, content):
            """Stash uploaded bytes in the in-memory blob store."""
            store[storage_uri] = content

        def fake_get(*, storage_uri):
            """Read bytes back from the in-memory blob store."""
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

    assert outcome.run is not None
    assert outcome.run.status == "SUCCEEDED"
    assert outcome.per_report_failures == []
    assert calls["n"] >= 2

    raw_files = session.scalars(
        select(RawReportFileORM).where(RawReportFileORM.tenant_id == TENANT_ID)
    ).all()
    assert [raw_file.id for raw_file in raw_files] == [race_winner_id]
    assert raw_files[0].parse_status == "PARSED"

    link = session.scalars(
        select(ConnectorRunRawFileORM).where(ConnectorRunRawFileORM.tenant_id == TENANT_ID)
    ).one()
    assert link.raw_report_file_id == race_winner_id
    assert _audit_lifecycles(_connector_audit_events(session)) == [
        "STARTED",
        "PARSED",
        "FINISHED",
    ]


def test_run_one_real_local_file_store_backend_round_trips(
    session: Session, _stub_secret_resolver, tmp_path, monkeypatch
) -> None:
    """End-to-end ingestion against the REAL LocalFileStoreBackend.

    Every other orchestrator test patches
    ``ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend``
    with a MagicMock whose ``upload`` swallows any URI string, so the real
    ``LocalFileStoreBackend._path_for`` never executes against an emitted
    URI. That mask hid Concern A: ``deterministic_blob_path`` hardcoded
    ``gs://`` regardless of which backend was selected, so the default
    file-store backend rejected every URI at upload time with
    ``ValueError("LocalFileStoreBackend only handles file-store:// URIs,
    got 'gs://...'")``.

    This test does NOT patch LocalFileStoreBackend. It points the real
    backend at ``tmp_path`` via ``UMS_LOCAL_STORE_ROOT`` and ``UMS_LOCAL_BLOB_BUCKET``,
    drives ``run_one`` through to SUCCEEDED, then asserts the report's bytes
    landed on disk at the deterministic path and the persisted
    ``RawReportFileORM.file_url`` carries the ``file-store://`` scheme.
    """
    monkeypatch.setenv("UMS_BLOB_BACKEND", "file-store")
    monkeypatch.setenv("UMS_LOCAL_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("UMS_LOCAL_BLOB_BUCKET", "testbucket")

    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    csv_bytes = _csv_for_one_row()
    expected_checksum = hashlib.sha256(csv_bytes).hexdigest()

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        # NOTE: LocalFileStoreBackend is intentionally NOT patched here -- the
        # real backend writes to ``tmp_path`` so we can prove deterministic_blob_path
        # emits a scheme the backend will accept.
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"}
        ]
        client.list_reports_for_month.return_value = [{"id": "r1", "downloadUrl": "https://yt/r1"}]
        client.fetch_report.return_value = csv_bytes

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    # Outcome must be SUCCEEDED: a pre-fix ValueError from
    # LocalFileStoreBackend._path_for would have driven this to FAILED.
    assert isinstance(outcome, ConnectorRunOutcome)
    assert outcome.run is not None
    assert outcome.run.status == "SUCCEEDED"
    assert outcome.per_report_failures == []
    assert outcome.counts["reports_succeeded"] == 1

    # The deterministic path layout:
    # {root}/{bucket}/{tenant_id}/{report_type}/{month}/{checksum}.{ext}
    # NOTE: the file-store URI shape is
    #   file-store://{bucket}/{tenant_id}/{connector_key}/{report_type}/{month}/{checksum}.{ext}
    # and LocalFileStoreBackend strips the scheme and treats the remainder
    # as a relative path under root. So on disk we expect:
    expected_path = (
        tmp_path
        / "testbucket"
        / str(TENANT_ID)
        / CONNECTOR_KEY
        / "channel_basic_a2"
        / "2026-05"
        / f"{expected_checksum}.csv"
    )
    assert expected_path.exists(), (
        f"raw blob did not land on disk at {expected_path}; "
        f"tmp_path tree: {list(tmp_path.rglob('*'))}"
    )
    assert expected_path.read_bytes() == csv_bytes

    # The persisted file_url must carry the file-store scheme + bucket so
    # later re-reads dispatch to the right backend.
    raw_file = session.scalar(
        select(RawReportFileORM).where(RawReportFileORM.tenant_id == TENANT_ID)
    )
    assert raw_file is not None
    assert raw_file.file_url.startswith("file-store://testbucket/"), (
        f"expected file-store:// scheme + testbucket prefix; got {raw_file.file_url!r}"
    )
    assert raw_file.file_url.endswith(f"{expected_checksum}.csv")
    assert raw_file.checksum == expected_checksum


@pytest.mark.parametrize(
    ("run_connector_key", "stored_connector_key"),
    [
        pytest.param(
            CONNECTOR_KEY,
            "youtube_reporting",
            id="hyphenated-run-key-uses-underscore-credential",
        ),
        pytest.param(
            "youtube_reporting",
            CONNECTOR_KEY,
            id="underscore-run-key-uses-hyphenated-credential",
        ),
    ],
)
def test_run_one_accepts_youtube_reporting_credential_aliases(
    session: Session,
    _stub_secret_resolver,
    run_connector_key: str,
    stored_connector_key: str,
) -> None:
    """``run_one`` accepts both YouTube Reporting key spellings.

    The admin credential API may persist either the public connector key
    ``youtube-reporting`` or the B1 source-system key ``youtube_reporting``;
    orchestration must find the row from either dispatch spelling.
    """
    account_id = f"{ACCOUNT_ID}-{run_connector_key.replace('-', 'dash').replace('_', 'under')}"
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=stored_connector_key,
        account_id=account_id,
    )
    csv_bytes = _csv_for_one_row()

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"}
        ]
        client.list_reports_for_month.return_value = [{"id": "r1", "downloadUrl": "https://yt/r1"}]
        client.fetch_report.return_value = csv_bytes

        backend = local_cls.return_value
        store: dict[str, bytes] = {}
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=run_connector_key,
            account_id=account_id,
            report_month="2026-05",
        )

    assert outcome.run is not None
    assert outcome.run.status == "SUCCEEDED"
    raw_file = session.scalar(
        select(RawReportFileORM).where(RawReportFileORM.tenant_id == TENANT_ID)
    )
    assert raw_file is not None
    assert raw_file.source == "youtube_reporting"


def test_run_one_normalizes_secret_ref_before_resolving(
    session: Session, _stub_secret_resolver
) -> None:
    """The orchestrator must call the resolver with the normalised secret URI."""
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
        encrypted_secret_ref=f"  {DEFAULT_RESOLVER_REF}  ",
    )
    csv_bytes = _csv_for_one_row()

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"}
        ]
        client.list_reports_for_month.return_value = [{"id": "r1", "downloadUrl": "https://yt/r1"}]
        client.fetch_report.return_value = csv_bytes

        backend = local_cls.return_value
        store: dict[str, bytes] = {}
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    assert outcome.run is not None
    assert outcome.run.status == "SUCCEEDED"


def test_run_one_rejects_malformed_month_before_starting_run(session: Session) -> None:
    """Malformed ``report_month`` rejects the run before any state mutation."""
    with pytest.raises(ConnectorRunValidationError, match="report_month"):
        run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="٢٠٢٦-٠٤",
        )

    assert session.scalar(select(ConnectorRunORM)) is None


def test_run_one_reuses_existing_parsed_raw_file_for_same_checksum(
    session: Session, _stub_secret_resolver
) -> None:
    """Retrying the same Google payload should not violate the raw-file
    checksum unique constraint. A PARSED existing raw file is linked to the
    new run and still parsed so account-scoped source rows are not skipped.
    """
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    csv_bytes = _csv_for_one_row()
    checksum = compute_checksum(csv_bytes)
    existing_raw_file = RawReportFileORM(
        id=uuid4(),
        tenant_id=TENANT_ID,
        source="youtube_reporting",
        report_type="channel_basic_a2",
        report_month="2026-05",
        file_url="file-store://local/existing.csv",
        checksum=checksum,
        parse_status="PARSED",
    )
    session.add(existing_raw_file)
    session.commit()

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"}
        ]
        client.list_reports_for_month.return_value = [{"id": "r1", "downloadUrl": "https://yt/r1"}]
        client.fetch_report.return_value = csv_bytes

        backend = local_cls.return_value
        store: dict[str, bytes] = {}
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    assert outcome.run is not None
    assert outcome.run.status == "SUCCEEDED"
    assert outcome.counts["reports_succeeded"] == 1
    assert outcome.counts["rows_upserted_total"] == 1
    assert len(session.scalars(select(RawReportFileORM)).all()) == 1
    link = session.scalars(select(ConnectorRunRawFileORM)).one()
    assert link.raw_report_file_id == existing_raw_file.id
    source_rows = session.scalars(select(GoogleRevenueSourceRowORM)).all()
    assert len(source_rows) == 1
    assert source_rows[0].raw_file_id == existing_raw_file.id


def test_run_one_reopens_failed_raw_file_with_current_download_metadata(
    session: Session, _stub_secret_resolver
) -> None:
    """A failed raw_file is reopened in-place with fresh download metadata on retry."""
    credential = _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    csv_bytes = _csv_for_one_row()
    checksum = compute_checksum(csv_bytes)
    existing_raw_file = RawReportFileORM(
        id=uuid4(),
        tenant_id=TENANT_ID,
        source="youtube_reporting",
        report_type="channel_basic_a2",
        report_month="2026-05",
        file_url="file-store://old-stale-location/raw.csv",
        checksum=checksum,
        parse_status="FAILED",
        downloaded_by=None,
    )
    session.add(existing_raw_file)
    session.commit()

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"}
        ]
        client.list_reports_for_month.return_value = [{"id": "r1", "downloadUrl": "https://yt/r1"}]
        client.fetch_report.return_value = csv_bytes

        backend = local_cls.return_value
        store: dict[str, bytes] = {}
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
            triggered_by_user_id=credential.created_by,
        )

    session.refresh(existing_raw_file)
    assert outcome.run is not None
    assert outcome.run.status == "SUCCEEDED"
    assert existing_raw_file.parse_status == "PARSED"
    assert existing_raw_file.file_url != "file-store://old-stale-location/raw.csv"
    assert existing_raw_file.file_url in store
    assert existing_raw_file.downloaded_by == credential.created_by


def test_run_one_handles_duplicate_empty_daily_reports_in_one_run(
    session: Session, _stub_secret_resolver
) -> None:
    """Repeated empty daily reports in the same run must not double-attribute counts."""
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    header_only_csv = b"date,channel,estimatedRevenue,currencyCode\n"
    populated_csv = (
        b"date,channel,estimatedRevenue,currencyCode\n2026-05-01,UC_orch_alpha,7.500000,USD\n"
    )

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "content_owner_estimated_revenue_a1"}
        ]
        client.list_reports_for_month.return_value = [
            {"id": "r-empty-1", "downloadUrl": "https://yt/r-empty-1"},
            {"id": "r-empty-2", "downloadUrl": "https://yt/r-empty-2"},
            {"id": "r-populated-1", "downloadUrl": "https://yt/r-populated-1"},
            {"id": "r-populated-2", "downloadUrl": "https://yt/r-populated-2"},
        ]
        client.fetch_report.side_effect = [
            header_only_csv,
            header_only_csv,
            populated_csv,
            populated_csv,
        ]

        backend = local_cls.return_value
        store: dict[str, bytes] = {}
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    assert outcome.run is not None
    assert outcome.run.status == "SUCCEEDED"
    assert outcome.counts["reports_failed"] == 0
    raw_files = session.scalars(select(RawReportFileORM).order_by(RawReportFileORM.checksum)).all()
    assert len(raw_files) == 2
    assert len(session.scalars(select(ConnectorRunRawFileORM)).all()) == 2
    source_rows = session.scalars(select(GoogleRevenueSourceRowORM)).all()
    assert len(source_rows) == 1
    assert source_rows[0].amount_native == Decimal("7.500000")
    assert source_rows[0].raw_file_id is None


def test_run_one_persists_invalid_csv_evidence_before_shape_failure(
    session: Session, _stub_secret_resolver
) -> None:
    """Invalid CSV evidence is persisted before the shape-validation failure is recorded."""
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    headerless_csv = b"2026-05-01,UC_orch_alpha,7.500000,USD\n"

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "content_owner_estimated_revenue_a1"}
        ]
        client.list_reports_for_month.return_value = [
            {"id": "r-headerless", "downloadUrl": "https://yt/r-headerless"}
        ]
        client.fetch_report.return_value = headerless_csv

        backend = local_cls.return_value
        store: dict[str, bytes] = {}
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    assert outcome.run is not None
    assert outcome.run.status == "FAILED"
    assert outcome.per_report_failures == [
        ("content_owner_estimated_revenue_a1", "GoogleApiResponseError")
    ]
    raw_files = session.scalars(select(RawReportFileORM)).all()
    assert len(raw_files) == 1
    assert raw_files[0].parse_status == "FAILED"
    assert store[raw_files[0].file_url] == headerless_csv
    assert session.scalars(select(GoogleRevenueSourceRowORM)).all() == []


def test_run_one_removes_stale_source_rows_when_replacement_omits_them(
    session: Session, _stub_secret_resolver
) -> None:
    """Stale rows whose keys are absent from the new parse are removed under the scope."""
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    stale_key = "st" * 32
    session.add(
        GoogleRevenueSourceRowORM(
            id=uuid4(),
            tenant_id=TENANT_ID,
            source_system="youtube_reporting",
            source_row_key=stale_key,
            source_account_id=ACCOUNT_ID,
            content_owner_id=ACCOUNT_ID,
            youtube_channel_id="UC_stale",
            report_type="content_owner_estimated_revenue_a1",
            report_month="2026-05",
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            metric_key="estimatedRevenue",
            value_kind="estimated",
            amount_native=Decimal("1.000000"),
            currency_code="USD",
            source_report_id="old-report",
            raw_file_id=None,
            raw_payload={"sample": "stale"},
            imported_by=None,
        )
    )
    session.commit()
    replacement_csv = (
        b"date,channel,content_owner,estimatedRevenue,currencyCode\n"
        b"2026-05-01,UC_replacement,content-owner-1,7.500000,USD\n"
    )

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "content_owner_estimated_revenue_a1"}
        ]
        client.list_reports_for_month.return_value = [
            {"id": "r-new", "downloadUrl": "https://yt/r-new"}
        ]
        client.fetch_report.return_value = replacement_csv

        backend = local_cls.return_value
        store: dict[str, bytes] = {}
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    assert outcome.run is not None
    assert outcome.run.status == "SUCCEEDED"
    source_rows = session.scalars(
        select(GoogleRevenueSourceRowORM).where(GoogleRevenueSourceRowORM.tenant_id == TENANT_ID)
    ).all()
    assert [row.youtube_channel_id for row in source_rows] == ["UC_replacement"]
    assert stale_key not in {row.source_row_key for row in source_rows}


def test_run_one_skips_monthly_aggregate_when_daily_download_fails(
    session: Session, _stub_secret_resolver
) -> None:
    """A daily download failure must skip the monthly aggregate without partial writes."""
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    csv_bytes = (
        b"date,channel,estimatedRevenue,currencyCode\n2026-05-01,UC_orch_alpha,7.500000,USD\n"
    )

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "content_owner_estimated_revenue_a1"}
        ]
        client.list_reports_for_month.return_value = [
            {"id": "r-ok", "downloadUrl": "https://yt/r-ok"},
            {"id": "r-fail", "downloadUrl": "https://yt/r-fail"},
        ]
        client.fetch_report.side_effect = [
            csv_bytes,
            GoogleApiServerError(
                method="GET",
                url="https://yt/r-fail",
                status=503,
                attempts=3,
            ),
        ]

        local_cls.return_value.upload.side_effect = AssertionError(
            "partial monthly aggregate must not be uploaded"
        )

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    assert outcome.run is not None
    assert outcome.run.status == "FAILED"
    assert outcome.counts["reports_attempted"] == 1
    assert outcome.counts["reports_succeeded"] == 0
    assert outcome.counts["reports_failed"] == 1
    assert session.scalars(select(RawReportFileORM)).all() == []
    assert session.scalars(select(GoogleRevenueSourceRowORM)).all() == []


def test_run_one_marks_run_failed_when_blob_backend_configuration_is_invalid(
    session: Session, _stub_secret_resolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-start blob backend setup failures must finish the run FAILED,
    not leave connector_runs stuck in RUNNING.
    """
    monkeypatch.setenv("UMS_BLOB_BACKEND", "not-a-backend")
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )

    with patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh:
        refresh.return_value = None
        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    assert outcome.run is not None
    assert outcome.run.status == "FAILED"
    assert outcome.counts["reports_attempted"] == 0
    run_row = session.scalar(select(ConnectorRunORM).where(ConnectorRunORM.tenant_id == TENANT_ID))
    assert run_row is not None
    assert run_row.status == "FAILED"
    assert "BlobStorageConfigurationError" in (run_row.error_summary or "")


def test_run_one_rejects_csv_rows_without_currency(session: Session, _stub_secret_resolver) -> None:
    """The CSV adapter must not invent USD when Google omits currency
    metadata; that would make downstream revenue provenance ambiguous.
    """
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    csv_bytes = (
        b"date,channel,content_owner,estimatedRevenue\n"
        b"2026-05-01,UC_orch_alpha,cms-orch-1,12.345600\n"
    )

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"}
        ]
        client.list_reports_for_month.return_value = [{"id": "r1", "downloadUrl": "https://yt/r1"}]
        client.fetch_report.return_value = csv_bytes

        backend = local_cls.return_value
        store: dict[str, bytes] = {}
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    assert outcome.run is not None
    assert outcome.run.status == "FAILED"
    run_row = session.scalar(select(ConnectorRunORM).where(ConnectorRunORM.tenant_id == TENANT_ID))
    assert run_row is not None
    assert "currency" in (run_row.error_summary or "")
    raw_files = session.scalars(select(RawReportFileORM)).all()
    assert len(raw_files) == 1
    assert raw_files[0].parse_status == "FAILED"
    assert store[raw_files[0].file_url] == csv_bytes
    assert session.scalars(select(GoogleRevenueSourceRowORM)).all() == []


def test_oauth_refresh_error_during_report_fetch_ends_run_in_bucket_c(
    session: Session, _stub_secret_resolver
) -> None:
    """OAuth refresh failures can happen inside google-auth during any HTTP
    request. Those are terminal auth/runtime failures, not per-report data
    failures, so the runner must let them reach run-level Bucket C.
    """
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"}
        ]
        client.list_reports_for_month.return_value = [{"id": "r1", "downloadUrl": "https://yt/r1"}]
        client.fetch_report.side_effect = OAuthRefreshError(inner=RefreshError("token revoked"))
        local_cls.return_value.upload.side_effect = AssertionError(
            "blob upload must not run after OAuth failure"
        )

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    assert outcome.run is not None
    assert outcome.run.status == "FAILED"
    assert outcome.counts["reports_attempted"] == 0
    assert outcome.counts["reports_failed"] == 0
    assert outcome.per_report_failures == []
    run_row = session.scalar(select(ConnectorRunORM).where(ConnectorRunORM.tenant_id == TENANT_ID))
    assert run_row is not None
    assert "OAuthRefreshError" in (run_row.error_summary or "")
    assert session.scalars(select(RawReportFileORM)).all() == []


def test_run_one_sweeps_running_to_failed_on_untyped_error(
    session: Session, _stub_secret_resolver
) -> None:
    """Defence-in-depth: even when the typed bucket-C handler itself can't
    record the FAILED status (e.g. the in-process ``finish_run`` call
    raises), the outer ``finally`` sweeps the connector_runs row from
    RUNNING to FAILED.

    Originally written for T27 against the narrow
    ``except GoogleConnectorError`` handler (where an untyped ValueError
    from ``_process_one_report`` would escape both ``except`` blocks). T28
    widened both per-report and bucket-C handlers to ``except Exception``,
    so a ValueError from ``_process_one_report`` is now caught at the
    per-report level. To keep this test exercising the *fail-safe*
    ``finally`` (rather than the now-broader bucket-C handler), we
    simulate the only remaining path that can reach it: the post-loop
    happy-path ``finish_run`` itself raises before ``finished = True`` is
    set. The fail-safe runs, rolls back, and re-issues ``finish_run`` so
    the operator console doesn't see a row stuck in RUNNING.

    side_effect alternates between ``RuntimeError`` (post-loop happy-path
    call) and a no-op (fail-safe call) so the cleanup path can succeed.
    """
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    csv_bytes = _csv_for_one_row()

    # Per-call side_effect: first call (post-loop happy-path finish_run)
    # raises so ``finished`` stays False and the finally fires; second
    # call (fail-safe finish_run) succeeds. The fail-safe writes the
    # 'orchestrator aborted unexpectedly' summary the assertion below
    # checks for.
    call_count = {"n": 0}

    def flaky_finish_run(session, *, tenant_id, connector_run_id, status, counts, error_summary):
        """Inject a transient failure on the finish-run hook to exercise the retry path."""
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Simulate a transient DB failure on the main-path commit so
            # the fail-safe finally has something to clean up. Roll the
            # session back first so the fail-safe's own finish_run isn't
            # blocked by a pending transaction error.
            session.rollback()
            raise RuntimeError("simulated finish_run failure on main path")
        # Fall through to the real implementation for the fail-safe call
        # so the connector_runs row actually moves to FAILED.
        from ums_smart_revenue.connectors.runs.repository import (
            finish_run as real_finish_run,
        )

        return real_finish_run(
            session,
            tenant_id=tenant_id,
            connector_run_id=connector_run_id,
            status=status,
            counts=counts,
            error_summary=error_summary,
        )

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.finish_run",
            side_effect=flaky_finish_run,
        ),
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"}
        ]
        client.list_reports_for_month.return_value = [{"id": "r1", "downloadUrl": "https://yt/r1"}]
        client.fetch_report.return_value = csv_bytes

        backend = local_cls.return_value
        store: dict[str, bytes] = {}
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

        with pytest.raises(RuntimeError, match="simulated finish_run failure"):
            run_one(
                session,
                tenant_id=TENANT_ID,
                connector_key=CONNECTOR_KEY,
                account_id=ACCOUNT_ID,
                report_month="2026-05",
            )

    # Both finish_run calls must have run: the failing main-path call and
    # the rescue fail-safe call. If only the first ran, the fail-safe
    # never executed.
    assert call_count["n"] == 2

    # Re-fetch the run row from the DB and verify the fail-safe ran:
    # the run must NOT be left in RUNNING; it must be swept to FAILED
    # with the generic fail-safe error_summary.
    run_row = session.scalar(select(ConnectorRunORM).where(ConnectorRunORM.tenant_id == TENANT_ID))
    assert run_row is not None
    assert run_row.status == "FAILED"
    assert "orchestrator aborted" in (run_row.error_summary or "")


# ============================================================================
# T28: Failure handlers A/B/C
# ============================================================================


def test_bucket_a_no_credential_raises_and_no_run_row(
    session: Session, _stub_secret_resolver
) -> None:
    """Bucket A: missing credential surfaces ``CredentialNotFoundError`` and
    must NOT create a connector_runs row.

    The orchestrator's pre-``start_run`` guards are forensic-critical: a
    half-created RUNNING row with no credential context would lie to the
    operator console (no audit, no traceability). The credential lookup is
    tenant + connector_key + account_id; with no row seeded, ``_load_credential``
    returns ``None`` and ``run_one`` raises before any DB write.
    """
    # No credential row seeded -> _load_credential returns None.
    with pytest.raises(CredentialNotFoundError):
        run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id="missing-account",
            report_month="2026-05",
        )

    # No connector_runs row should exist: Bucket A never reaches start_run.
    assert (
        session.scalar(select(ConnectorRunORM).where(ConnectorRunORM.tenant_id == TENANT_ID))
        is None
    )


def test_bucket_a_inactive_credential_raises_and_no_run_row(
    session: Session, _stub_secret_resolver
) -> None:
    """Bucket A: a credential row that exists but is not ``active`` must
    surface ``InactiveCredentialError`` and must NOT create a connector_runs
    row.

    Disabled/rotated credentials should fail closed so an operator who
    disabled an account can't accidentally drive a live ingestion against
    that account. Mirrors the no-credential test for the run-row absence.
    """
    cred = _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    cred.status = "disabled"
    session.flush()

    with pytest.raises(InactiveCredentialError):
        run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    assert (
        session.scalar(select(ConnectorRunORM).where(ConnectorRunORM.tenant_id == TENANT_ID))
        is None
    )


def test_bucket_b_parser_error_on_second_report_marks_raw_file_failed_and_status_partial(
    session: Session, _stub_secret_resolver
) -> None:
    """Bucket B: per-report failure AFTER the raw_file row is inserted.

    Setup: YT client returns 2 reports. The parser succeeds on the first
    call and raises ``ParserError`` on the second. ``ParserError`` is a
    subclass of ``ValueError`` (NOT ``GoogleConnectorError``), so this test
    pins the T28 widening of the inner per-report ``except`` from
    ``GoogleConnectorError`` to ``Exception`` — without that widening, the
    untyped ParserError would escape Bucket B and land in Bucket C, leaving
    the run FAILED instead of the spec-required PARTIAL.

    Failure point is at step (e) ``parser.parse(...)`` inside
    ``_process_one_report``, which is AFTER step (c) inserts the raw_file
    row in DOWNLOADED. Bucket B must mark that raw_file FAILED so the
    operator console doesn't show a perpetually-DOWNLOADED orphan.

    Expected end-state:
    - outcome.run.status == "PARTIAL"
    - counts.reports_succeeded == 1, reports_failed == 1
    - raw_file #1 parse_status == "PARSED"
    - raw_file #2 parse_status == "FAILED"
    - no exception escapes (Bucket B contained it)
    """
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    csv_bytes_a = (
        b"date,channel,content_owner,estimatedRevenue,currencyCode\n"
        b"2026-05-01,UC_orch_alpha,cms-orch-1,10.000000,USD\n"
    )
    csv_bytes_b = (
        b"date,channel,content_owner,estimatedRevenue,currencyCode\n"
        b"2026-05-02,UC_orch_beta,cms-orch-1,20.000000,USD\n"
    )

    # Wrap the real parser so the first call passes through (real parsed
    # rows for the upsert) and the second call raises ``ParserError``.
    # We patch the bare ``YouTubeReportingParser`` symbol the orchestrator
    # imports so ``_parser_for_connector`` returns this stub instance.
    from ums_smart_revenue.connectors.google_source_parsers import (
        YouTubeReportingParser as RealParser,
    )

    real_parser = RealParser()
    call_state = {"n": 0}

    class FlakyParser:
        """Parser that fails the first call and succeeds on retry;
        exercises parser retry semantics."""

        @staticmethod
        def parse(payload, *, tenant_id):
            """Raise once, then return the captured payload (mirrors the parser retry contract)."""
            call_state["n"] += 1
            if call_state["n"] == 2:
                raise ParserError("simulated parser failure on report 2")
            return list(real_parser.parse(payload, tenant_id=tenant_id))

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator._parser_for_connector",
            return_value=FlakyParser(),
        ),
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        # Two jobs -> two reports yielded by the runner.
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"},
            {"id": "job-2", "reportTypeId": "content_owner_basic_a3"},
        ]
        # One report per job; the runner walks job_id -> reports.
        reports_by_job = {
            "job-1": [{"id": "r1", "downloadUrl": "https://yt/r1"}],
            "job-2": [{"id": "r2", "downloadUrl": "https://yt/r2"}],
        }
        client.list_reports_for_month.side_effect = lambda *, account_id, job_id, report_month: (
            reports_by_job[job_id]
        )
        bytes_by_url = {"https://yt/r1": csv_bytes_a, "https://yt/r2": csv_bytes_b}
        client.fetch_report.side_effect = lambda *, download_url: bytes_by_url[download_url]

        backend = local_cls.return_value
        store: dict[str, bytes] = {}
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

        # Bucket B must contain the failure -- no exception escapes run_one.
        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    assert isinstance(outcome, ConnectorRunOutcome)
    assert outcome.run is not None
    assert outcome.run.status == "PARTIAL"
    assert outcome.counts["reports_attempted"] == 2
    assert outcome.counts["reports_succeeded"] == 1
    assert outcome.counts["reports_failed"] == 1
    # per_report_failures lists (report_type_id, error_class_name) for the
    # failed report. The failing report was the second yielded
    # (content_owner_basic_a3) and the error class is ParserError.
    assert outcome.per_report_failures == [("content_owner_basic_a3", "ParserError")]

    # Durable side-effects: raw_file #1 (channel_basic_a2) is PARSED;
    # raw_file #2 (content_owner_basic_a3) was inserted before parser.parse
    # raised and must have been marked FAILED by Bucket B.
    raw_files = session.scalars(
        select(RawReportFileORM)
        .where(RawReportFileORM.tenant_id == TENANT_ID)
        .order_by(RawReportFileORM.report_type)
    ).all()
    assert len(raw_files) == 2
    statuses = {rf.report_type: rf.parse_status for rf in raw_files}
    assert statuses == {
        "channel_basic_a2": "PARSED",
        "content_owner_basic_a3": "FAILED",
    }

    # Run row reflects PARTIAL with the failures captured in error_summary.
    run_row = session.scalar(select(ConnectorRunORM).where(ConnectorRunORM.tenant_id == TENANT_ID))
    assert run_row is not None
    assert run_row.status == "PARTIAL"
    assert run_row.error_summary is not None
    assert "ParserError" in run_row.error_summary


def test_bucket_c_generator_error_marks_run_failed_without_cli_bucket_a_exit(
    session: Session, _stub_secret_resolver
) -> None:
    """Bucket C: a failure that escapes the per-report loop (pre-yield
    failure inside ``runner.produce_reports``) must:
    1. mark the connector_runs row FAILED via ``finish_run`` (status +
       error_summary with the typed class name);
    2. commit so the FAILED row is durable even if the caller crashes;
    3. return a FAILED outcome so the CLI exits 1 for a post-start run
       failure instead of misclassifying it as Bucket A exit 2.

    Setup: patch ``list_supported_jobs`` to raise ``GoogleApiServerError``
    on the first call. The runner's generator raises before yielding any
    report, so no raw_file rows exist. The outer Bucket C except (widened
    to ``Exception`` in T28) catches the typed error and finishes the run
    FAILED.
    """
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.side_effect = GoogleApiServerError(
            method="GET",
            url="https://youtubereporting.googleapis.com/v1/jobs",
            status=503,
            attempts=3,
        )

        # Backend wired but never invoked: failure happens before any report
        # is yielded so no upload/download takes place.
        backend = local_cls.return_value
        backend.upload.side_effect = AssertionError(
            "upload must not be called on a pre-yield generator failure"
        )
        backend.get_bytes.side_effect = AssertionError(
            "get_bytes must not be called on a pre-yield generator failure"
        )

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    assert outcome.run is not None
    assert outcome.run.status == "FAILED"
    assert outcome.counts["reports_attempted"] == 0
    assert outcome.per_report_failures == []

    # Run row must be FAILED with the typed class name in error_summary.
    run_row = session.scalar(select(ConnectorRunORM).where(ConnectorRunORM.tenant_id == TENANT_ID))
    assert run_row is not None
    assert run_row.status == "FAILED"
    assert run_row.finished_at is not None
    assert "GoogleApiServerError" in (run_row.error_summary or "")
    # The Bucket C error_summary is NOT the generic fail-safe text -- the
    # typed handler ran, not the belt-and-suspenders finally.
    assert "orchestrator aborted" not in (run_row.error_summary or "")

    # No raw_files: failure was pre-yield.
    assert (
        session.scalar(select(RawReportFileORM).where(RawReportFileORM.tenant_id == TENANT_ID))
        is None
    )


def test_bucket_b_pre_flush_failure_on_second_report_preserves_first_report(
    session: Session, _stub_secret_resolver
) -> None:
    """M6 regression: report #1 succeeds; report #2 fails BEFORE its raw_file
    row is flushed (``upload_and_verify`` raises in step 2 of
    ``_process_one_report``, before the ``RawReportFileORM`` is added in
    step 3). The Bucket B handler's ``session.rollback()`` in the
    no-raw-file branch must NOT wipe report #1's already-flushed-but-
    uncommitted state. Each successful report is committed individually so
    prior successes are durable across later failures.

    Without the per-report commit, a single rollback in the no-raw-file
    branch erases report #1's RawReportFileORM, ConnectorRunRawFileORM,
    and GoogleRevenueSourceRowORM writes — even though the in-memory
    ``counts["reports_succeeded"]`` still reports 1, the on-disk state is
    a lie. This test asserts the durable side-effect (raw_files in the
    DB) survives the rollback.
    """
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    csv_bytes_a = (
        b"date,channel,content_owner,estimatedRevenue,currencyCode\n"
        b"2026-05-01,UC_orch_alpha,cms-orch-1,10.000000,USD\n"
    )
    csv_bytes_b = (
        b"date,channel,content_owner,estimatedRevenue,currencyCode\n"
        b"2026-05-02,UC_orch_beta,cms-orch-1,20.000000,USD\n"
    )

    # Patch ``upload_and_verify`` on the orchestrator module so call #1
    # behaves normally (delegates to the real backend so report #1's
    # raw_file flush + upsert + mark_parsed all succeed) and call #2
    # raises ``GoogleApiServerError`` BEFORE ``_process_one_report`` adds
    # the report #2 raw_file row. This drives the no-raw-file branch of
    # Bucket B — the branch that originally called ``session.rollback()``
    # and wiped report #1's flushed-but-uncommitted state.
    from ums_smart_revenue.connectors.runs import blob_storage as _blob_storage

    real_upload_and_verify = _blob_storage.upload_and_verify
    call_count = {"n": 0}

    def flaky_upload_and_verify(*, backend, storage_uri, content):
        """Wrap upload+verify with a transient checksum mismatch to drive the verify-retry path."""
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise GoogleApiServerError(
                method="PUT",
                url=storage_uri,
                status=503,
                attempts=1,
            )
        return real_upload_and_verify(backend=backend, storage_uri=storage_uri, content=content)

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.upload_and_verify",
            side_effect=flaky_upload_and_verify,
        ),
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"},
            {"id": "job-2", "reportTypeId": "content_owner_basic_a3"},
        ]
        reports_by_job = {
            "job-1": [{"id": "r1", "downloadUrl": "https://yt/r1"}],
            "job-2": [{"id": "r2", "downloadUrl": "https://yt/r2"}],
        }
        client.list_reports_for_month.side_effect = lambda *, account_id, job_id, report_month: (
            reports_by_job[job_id]
        )
        bytes_by_url = {"https://yt/r1": csv_bytes_a, "https://yt/r2": csv_bytes_b}
        client.fetch_report.side_effect = lambda *, download_url: bytes_by_url[download_url]

        backend = local_cls.return_value
        store: dict[str, bytes] = {}
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    # Outcome: PARTIAL — report #1 succeeded, report #2 failed pre-flush.
    assert isinstance(outcome, ConnectorRunOutcome)
    assert outcome.run is not None
    assert outcome.run.status == "PARTIAL"
    assert outcome.counts["reports_attempted"] == 2
    assert outcome.counts["reports_succeeded"] == 1
    assert outcome.counts["reports_failed"] == 1
    assert outcome.per_report_failures == [("content_owner_basic_a3", "GoogleApiServerError")]

    # Critical M6 assertion: report #1's RawReportFileORM row must still
    # exist after the run completes — proving the per-report commit made
    # it durable BEFORE report #2's no-raw-file-branch ``session.rollback()``
    # could wipe it. If this assertion fails with len == 0, the M6 bug is
    # back: the rollback wiped the flushed-but-uncommitted report #1 state
    # and the run row is now lying to operators (``reports_succeeded == 1``
    # but no raw_file evidence).
    raw_files = session.scalars(
        select(RawReportFileORM)
        .where(RawReportFileORM.tenant_id == TENANT_ID)
        .order_by(RawReportFileORM.downloaded_at)
    ).all()
    assert len(raw_files) == 1, (
        f"Expected exactly 1 raw_file (report #1's, since report #2 failed "
        f"pre-flush). Got {len(raw_files)} — likely a session.rollback() "
        f"wiped report #1's flushed-but-uncommitted state (M6 regression)."
    )
    assert raw_files[0].parse_status == "PARSED"
    assert raw_files[0].report_type == "channel_basic_a2"

    # Source rows for report #1 must also be durable.
    source_rows = session.scalars(
        select(GoogleRevenueSourceRowORM).where(GoogleRevenueSourceRowORM.tenant_id == TENANT_ID)
    ).all()
    assert len(source_rows) >= 1, (
        "Expected report #1's GoogleRevenueSourceRowORM rows to survive "
        "report #2's rollback (M6 regression)."
    )

    # Run row reflects PARTIAL with the failing report's error in
    # error_summary.
    run_row = session.scalar(select(ConnectorRunORM).where(ConnectorRunORM.tenant_id == TENANT_ID))
    assert run_row is not None
    assert run_row.status == "PARTIAL"
    assert "GoogleApiServerError" in (run_row.error_summary or "")


def test_bucket_b_download_failure_on_second_report_preserves_first_report(
    session: Session, _stub_secret_resolver
) -> None:
    """Regression: report-download failures occur after report enumeration but
    before the runner can yield parser payloads. They are still per-report
    failures and must land in Bucket B, not the run-level Bucket C.
    """
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    csv_bytes_a = (
        b"date,channel,content_owner,estimatedRevenue,currencyCode\n"
        b"2026-05-01,UC_orch_alpha,cms-orch-1,10.000000,USD\n"
    )

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"},
            {"id": "job-2", "reportTypeId": "content_owner_basic_a3"},
        ]
        reports_by_job = {
            "job-1": [{"id": "r1", "downloadUrl": "https://yt/r1"}],
            "job-2": [{"id": "r2", "downloadUrl": "https://yt/r2"}],
        }
        client.list_reports_for_month.side_effect = lambda *, account_id, job_id, report_month: (
            reports_by_job[job_id]
        )

        def fetch_report(*, download_url: str) -> bytes:
            """Return the canned single-CSV bytes for the requested report_id."""
            if download_url == "https://yt/r2":
                raise GoogleApiServerError(
                    method="GET",
                    url=download_url,
                    status=503,
                    attempts=4,
                )
            return csv_bytes_a

        client.fetch_report.side_effect = fetch_report

        backend = local_cls.return_value
        store: dict[str, bytes] = {}
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    assert outcome.run is not None
    assert outcome.run.status == "PARTIAL"
    assert outcome.counts["reports_attempted"] == 2
    assert outcome.counts["reports_succeeded"] == 1
    assert outcome.counts["reports_failed"] == 1
    assert outcome.per_report_failures == [("content_owner_basic_a3", "GoogleApiServerError")]

    raw_files = session.scalars(
        select(RawReportFileORM).where(RawReportFileORM.tenant_id == TENANT_ID)
    ).all()
    assert len(raw_files) == 1
    assert raw_files[0].parse_status == "PARSED"
    assert raw_files[0].report_type == "channel_basic_a2"

    source_rows = session.scalars(
        select(GoogleRevenueSourceRowORM).where(GoogleRevenueSourceRowORM.tenant_id == TENANT_ID)
    ).all()
    assert len(source_rows) == 1


def test_run_one_counts_rows_only_after_mark_parsed_and_commit(
    session: Session, _stub_secret_resolver
) -> None:
    """If a post-upsert lifecycle step fails, the failed report must not
    inflate rows_upserted_total or leave source rows behind.
    """
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    csv_bytes = _csv_for_one_row()

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.mark_parsed",
            side_effect=RuntimeError("mark_parsed failed"),
        ),
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"},
        ]
        client.list_reports_for_month.return_value = [
            {"id": "r1", "downloadUrl": "https://yt/r1"},
        ]
        client.fetch_report.return_value = csv_bytes

        backend = local_cls.return_value
        store: dict[str, bytes] = {}
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    assert outcome.run is not None
    assert outcome.run.status == "FAILED"
    assert outcome.counts["reports_attempted"] == 1
    assert outcome.counts["reports_succeeded"] == 0
    assert outcome.counts["reports_failed"] == 1
    assert outcome.counts["rows_upserted_total"] == 0
    assert outcome.per_report_failures == [("channel_basic_a2", "RuntimeError")]

    raw_file = session.scalars(
        select(RawReportFileORM).where(RawReportFileORM.tenant_id == TENANT_ID)
    ).one()
    assert raw_file.parse_status == "FAILED"

    source_rows = session.scalars(
        select(GoogleRevenueSourceRowORM).where(GoogleRevenueSourceRowORM.tenant_id == TENANT_ID)
    ).all()
    assert source_rows == []


# ============================================================================
# T29: Dry-run
# ============================================================================


def test_dry_run_writes_nothing_returns_outcome_with_run_none(
    session: Session, _stub_secret_resolver
) -> None:
    """Dry-run contract (spec §5.4): writes NOTHING to the database (no
    connector_runs row, no raw_file row, no source-row upsert, no audit) and
    performs NO blob upload, but DOES exercise the runner's
    ``produce_reports`` (which fetches CSV bytes via the YT API) and DOES
    run the parser to validate and count rows.

    Bucket A still runs (credential lookup + OAuth refresh) so a missing or
    inactive credential fails dry-run the same way it fails a live run -- if
    you can't even authenticate, the dry-run can't report anything useful.

    Expected end-state:
    - outcome.run is None (no connector_runs row was started)
    - counts["reports_attempted"] == 2 (both reports were enumerated)
    - counts["reports_succeeded"] == 2 (both parsed cleanly)
    - counts["rows_upserted_total"] == 2 (dry-run validates parser output and
      returns the would-upsert row count, but writes no source rows)
    - zero rows in connector_runs and raw_report_files (defence-in-depth:
      the SAVEPOINT-rollback in the dry-run branch reverts any writes a
      future runner might accidentally make)
    """
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    # Two reports yielded with the documented column set so the real parser
    # (invoked for row-count validation) accepts them. Keeps the dry-run
    # success path symmetric with the happy-path test fixture.
    csv_bytes_a = (
        b"date,channel,content_owner,estimatedRevenue,currencyCode\n"
        b"2026-05-01,UC_dry_alpha,cms-orch-1,1.230000,USD\n"
    )
    csv_bytes_b = (
        b"date,channel,content_owner,estimatedRevenue,currencyCode\n"
        b"2026-05-02,UC_dry_beta,cms-orch-1,4.560000,USD\n"
    )

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        # Patch the backend even though dry-run never uploads -- this
        # prevents the real LocalFileStoreBackend from being instantiated
        # and touching the filesystem / env vars for hygiene. Also assert
        # neither upload nor get_bytes is called: dry-run must skip blob I/O.
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"},
            {"id": "job-2", "reportTypeId": "content_owner_basic_a3"},
        ]
        reports_by_job = {
            "job-1": [{"id": "r1", "downloadUrl": "https://yt/r1"}],
            "job-2": [{"id": "r2", "downloadUrl": "https://yt/r2"}],
        }
        client.list_reports_for_month.side_effect = lambda *, account_id, job_id, report_month: (
            reports_by_job[job_id]
        )
        bytes_by_url = {"https://yt/r1": csv_bytes_a, "https://yt/r2": csv_bytes_b}
        client.fetch_report.side_effect = lambda *, download_url: bytes_by_url[download_url]

        backend = local_cls.return_value
        backend.upload.side_effect = AssertionError("blob upload must not be called in dry-run")
        backend.get_bytes.side_effect = AssertionError(
            "blob get_bytes must not be called in dry-run"
        )

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
            dry_run=True,
        )

    # ----- outcome shape: dry-run returns run=None -----
    assert isinstance(outcome, ConnectorRunOutcome)
    assert outcome.run is None
    assert outcome.per_report_failures == []

    # ----- counts: both reports attempted + parsed cleanly -----
    counts = outcome.counts
    assert counts["reports_attempted"] == 2
    assert counts["reports_succeeded"] == 2
    assert counts["reports_failed"] == 0
    # Dry-run returns the would-upsert total while leaving the split at zero
    # because no source-row write/classification occurs.
    assert counts["rows_upserted_total"] == 2
    assert counts["rows_upserted_created"] == 0
    assert counts["rows_upserted_updated"] == 0
    assert counts["rows_upserted_unchanged"] == 0

    # ----- no DB writes: defence-in-depth via SAVEPOINT rollback -----
    assert session.query(ConnectorRunORM).count() == 0
    assert session.query(RawReportFileORM).count() == 0
    assert (
        session.query(ConnectorRunRawFileORM)
        .filter(ConnectorRunRawFileORM.tenant_id == TENANT_ID)
        .count()
        == 0
    )
    assert (
        session.query(GoogleRevenueSourceRowORM)
        .filter(GoogleRevenueSourceRowORM.tenant_id == TENANT_ID)
        .count()
        == 0
    )


def test_dry_run_missing_credential_raises_credential_not_found(
    session: Session, _stub_secret_resolver
) -> None:
    """Dry-run shares Bucket A with the live path: a missing credential
    raises ``CredentialNotFoundError`` BEFORE the dry-run branch runs.

    Without this guarantee an operator could "dry-run" against an account
    that has no live credential and get a misleadingly clean outcome; the
    spec instead surfaces the credential gap so the operator fixes it
    before scheduling the live run.
    """
    with pytest.raises(CredentialNotFoundError):
        run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id="missing-account",
            report_month="2026-05",
            dry_run=True,
        )
    # Bucket A still gates dry-run -- no connector_runs row materialised.
    assert session.query(ConnectorRunORM).count() == 0


def test_dry_run_savepoint_reverts_runner_side_writes(
    session: Session, _stub_secret_resolver
) -> None:
    """SAVEPOINT defence-in-depth: any unflushed writes a runner accidentally
    makes inside ``produce_reports`` must be reverted before
    ``run_one(dry_run=True)`` returns.

    The current ``YouTubeReportingRunner`` doesn't write to the session, so
    ``test_dry_run_writes_nothing_returns_outcome_with_run_none`` passes
    whether the SAVEPOINT engages or not -- it can't tell the difference
    between "no write happened" and "a write happened but was rolled back".
    This test injects a custom runner whose ``produce_reports`` adds and
    flushes a marker ``TenantORM`` row before yielding, then asserts the row
    is GONE after ``run_one`` returns. If a future refactor removes
    ``savepoint.rollback()`` from the dry-run branch (or replaces the
    SAVEPOINT pattern with something that doesn't actually defend), the
    marker row would leak and this assertion would fire.
    """
    tenant_id = TENANT_ID
    _make_credential_row(
        session,
        tenant_id=tenant_id,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )

    # Runner stub: inserts a TenantORM marker row inside produce_reports
    # and flushes it so the SAVEPOINT actually has state to roll back, then
    # yields one parser-valid payload (real ``YouTubeReportingParser`` will
    # accept ``report_metadata={"report_id":..,"report_type":..}`` plus
    # ``rows=[]`` and emit zero ParsedSourceRow instances). The orchestrator
    # then commits ``reports_succeeded`` for that single yielded report.
    class WritingRunner:
        """Runner that emits one canned success payload; used to exercise non-YouTube branches."""

        last_marker_id: UUID | None = None

        @staticmethod
        def produce_reports(
            *,
            session,
            run,
            credentials,
            report_month,
            account_id,
        ):
            """Yield one ``ProducedReportSuccess`` carrying the canned parser payload\
            + raw report."""
            _ = (run, credentials, report_month, account_id)
            marker_id = uuid4()
            session.add(
                TenantORM(
                    id=marker_id,
                    slug=f"dry-run-marker-{marker_id}",
                    display_name="Dry-Run Marker",
                )
            )
            session.flush()
            WritingRunner.last_marker_id = marker_id
            yield (
                "channel_basic_a2",
                {
                    "report_metadata": {
                        "report_id": "r-marker",
                        "report_type": "channel_basic_a2",
                    },
                    "rows": [],
                },
                b"",
            )

    # Swap the registry entry for the duration of this test; mirrors the
    # snapshot/restore pattern in test_registry.py::_reset_registry. Without
    # this, the orchestrator would dispatch the module-load-registered
    # YouTubeReportingRunner (which doesn't write and wouldn't exercise the
    # SAVEPOINT defence we're trying to lock).
    from ums_smart_revenue.connectors.google import registry

    saved_registry = dict(registry._REGISTRY)
    registry._REGISTRY[CONNECTOR_KEY] = WritingRunner()

    try:
        with patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh:
            refresh.return_value = None
            outcome = run_one(
                session,
                tenant_id=tenant_id,
                connector_key=CONNECTOR_KEY,
                account_id=ACCOUNT_ID,
                report_month="2026-05",
                dry_run=True,
            )
    finally:
        registry._REGISTRY.clear()
        registry._REGISTRY.update(saved_registry)

    # Core assertion: the marker row inserted+flushed inside the runner must
    # NOT exist after ``run_one`` returns. If the SAVEPOINT defence regresses
    # (e.g. someone removes ``savepoint.rollback()``), this scalar fetch
    # returns the row and the assertion fails loudly.
    assert WritingRunner.last_marker_id is not None
    marker = session.scalar(select(TenantORM).where(TenantORM.id == WritingRunner.last_marker_id))
    assert marker is None, (
        "SAVEPOINT defence regressed: a runner's side-write to the session "
        "leaked out of the dry-run branch. The session.begin_nested() / "
        "savepoint.rollback() pattern in run_one's dry-run path must roll back "
        "any unflushed writes made by produce_reports."
    )

    # Outcome shape is still the spec-required dry-run shape: run is None,
    # one report was attempted (the single yield), and per_report_failures is
    # empty (dry-run path returns this hardcoded).
    assert outcome.run is None
    assert outcome.counts["reports_attempted"] == 1
    assert outcome.counts["reports_succeeded"] == 1
    assert outcome.counts["reports_failed"] == 0
    assert outcome.per_report_failures == []


def test_dry_run_parser_failure_increments_reports_failed_and_keeps_per_report_failures_empty(
    session: Session, _stub_secret_resolver
) -> None:
    """Dry-run with a parser failure on report #2 of two locks the symmetry
    between the dry-run branch's per-report ``except`` and the live Bucket B
    handler's ``reports_failed`` counter.

    The dry-run branch's ``except Exception: counts["reports_failed"] += 1``
    has no existing coverage -- ``test_dry_run_writes_nothing_returns_outcome
    _with_run_none`` only exercises the happy path where both reports parse
    cleanly. This test pins:
    - reports_attempted == 2, reports_succeeded == 1, reports_failed == 1
    - per_report_failures stays empty (spec §5.4: dry-run returns counts only;
      the per-report failure list is intentionally not surfaced from dry-run)
    - no DB rows written (the SAVEPOINT defence still cleans up around the
      runner's iteration even though the runner itself doesn't write)
    - no blob upload (defensive backend stub asserts neither upload nor
      get_bytes is invoked)
    """
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    csv_bytes_a = (
        b"date,channel,content_owner,estimatedRevenue,currencyCode\n"
        b"2026-05-01,UC_dry_alpha,cms-orch-1,1.230000,USD\n"
    )
    csv_bytes_b = (
        b"date,channel,content_owner,estimatedRevenue,currencyCode\n"
        b"2026-05-02,UC_dry_beta,cms-orch-1,4.560000,USD\n"
    )

    # Wrap the real parser: call #1 passes through (a real row to count);
    # call #2 raises ParserError to drive the dry-run branch's per-report
    # except. Patching ``_parser_for_connector`` to return this FlakyParser
    # mirrors the existing Bucket B test pattern (the orchestrator calls the
    # helper once and reuses the returned instance for every report).
    from ums_smart_revenue.connectors.google_source_parsers import (
        YouTubeReportingParser as RealParser,
    )

    real_parser = RealParser()
    call_state = {"n": 0}

    class FlakyParser:
        """Parser that fails on the first call and returns a parsed payload on the second."""

        @staticmethod
        def parse(payload, *, tenant_id):
            """Raise once, then return the canned parsed payload (mirrors retry semantics)."""
            call_state["n"] += 1
            if call_state["n"] == 2:
                raise ParserError("simulated parser failure on dry-run report 2")
            return list(real_parser.parse(payload, tenant_id=tenant_id))

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator._parser_for_connector",
            return_value=FlakyParser(),
        ),
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        # Two jobs -> two reports yielded by the runner.
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"},
            {"id": "job-2", "reportTypeId": "content_owner_basic_a3"},
        ]
        reports_by_job = {
            "job-1": [{"id": "r1", "downloadUrl": "https://yt/r1"}],
            "job-2": [{"id": "r2", "downloadUrl": "https://yt/r2"}],
        }
        client.list_reports_for_month.side_effect = lambda *, account_id, job_id, report_month: (
            reports_by_job[job_id]
        )
        bytes_by_url = {"https://yt/r1": csv_bytes_a, "https://yt/r2": csv_bytes_b}
        client.fetch_report.side_effect = lambda *, download_url: bytes_by_url[download_url]

        # Defensive: dry-run must never instantiate a real backend or call
        # upload/get_bytes. The dry-run branch in run_one never invokes
        # ``_build_blob_backend()``, but patching the bare LocalFileStoreBackend
        # symbol guards against a regression that flips ordering.
        backend = local_cls.return_value
        backend.upload.side_effect = AssertionError("blob upload must not be called in dry-run")
        backend.get_bytes.side_effect = AssertionError(
            "blob get_bytes must not be called in dry-run"
        )

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
            dry_run=True,
        )

    # ----- outcome contract: counts capture the failure AND per-report failures are populated -----
    assert isinstance(outcome, ConnectorRunOutcome)
    assert outcome.run is None
    assert outcome.counts["reports_attempted"] == 2
    assert outcome.counts["reports_succeeded"] == 1
    assert outcome.counts["reports_failed"] == 1
    # FIX: the dry-run outcome now carries per-report failures (report_type,
    # error_class) so the executor's job_dry_run_completed audit row (the
    # only durable record of a dry-run, since connector_runs is empty) can
    # show operators which reports would fail. Each failing report appears
    # once with the canned exception class name.
    assert outcome.per_report_failures == [
        ("content_owner_basic_a3", "ParserError"),
    ]

    # ----- no DB writes: SAVEPOINT still cleans up around the iteration -----
    assert session.query(ConnectorRunORM).count() == 0
    assert session.query(RawReportFileORM).count() == 0
    assert (
        session.query(ConnectorRunRawFileORM)
        .filter(ConnectorRunRawFileORM.tenant_id == TENANT_ID)
        .count()
        == 0
    )
    assert (
        session.query(GoogleRevenueSourceRowORM)
        .filter(GoogleRevenueSourceRowORM.tenant_id == TENANT_ID)
        .count()
        == 0
    )


# ============================================================================
# B2.5 Task 33: YouTubeAnalyticsRunner orchestrator integration test.
# One CMS-owned channel and one outside-CMS channel are seeded. Only the
# CMS-owned channel is eligible for B2.5, so the runner fetches one targeted
# content-owner report and the orchestrator should SUCCEED with one report.
# ============================================================================

_ANALYTICS_CONNECTOR_KEY = "youtube-analytics"
_ANALYTICS_ACCOUNT_ID = "cms-orch-owner"


def _make_analytics_parser_payload(
    *,
    channel_id: str,
    report_month: str,
    account_id: str = _ANALYTICS_ACCOUNT_ID,
) -> dict[str, object]:
    """Minimal wire-shape payload for what YouTubeAnalyticsClient returns.

    Mirrors the reports.query response shape the client returns BEFORE the
    orchestrator's `_synthesise_analytics_channel_dimension()` injects the
    `channel` dimension into columnHeaders / rows:

    - query_request carries the request parameters (ids, dates, metrics,
      dimensions) so the parser can build row keys and validate the range.
      dimensions == ``_ANALYTICS_DIMENSIONS`` (currently ``"month"``) — the
      wire-level shape for a single-channel content-owner query.
    - columnHeaders declares the time DIMENSION(s) from _ANALYTICS_DIMENSIONS
      followed by the full locked analytics metric set. ``channel`` is NOT in
      this list; the runner synthesises it from filters=channel==<id>.
    - rows carries one data row: [<YYYY-MM>, <metric>, <metric>, <metric>].
      The runner prepends the channel_id before passing to the parser.
    """
    year, month = report_month.split("-")
    first_day = f"{year}-{month}-01"
    metric_names = _ANALYTICS_METRICS.split(",")
    dimension_names = _ANALYTICS_DIMENSIONS.split(",")
    metric_values = {
        "estimatedRevenue": 12.5,
        "estimatedAdRevenue": 8.0,
        "grossRevenue": 20.5,
    }
    # The wire response carries only the dimensions actually requested. For
    # `dimensions=month` (the B2.5 default), columnHeaders has one DIMENSION
    # header and each row contains [<month>, <metrics...>]. The runner
    # synthesises the `channel` dimension after this returns.
    dimension_cells: dict[str, str] = {"month": f"{year}-{month}"}
    return {
        "query_request": {
            "ids": f"contentOwner=={account_id}",
            "filters": f"channel=={channel_id}",
            "startDate": first_day,
            "endDate": first_day,
            "metrics": _ANALYTICS_METRICS,
            "dimensions": _ANALYTICS_DIMENSIONS,
        },
        "columnHeaders": (
            [{"columnType": "DIMENSION", "name": name} for name in dimension_names]
            + [{"columnType": "METRIC", "name": name} for name in metric_names]
        ),
        "rows": [
            [
                *[dimension_cells[name] for name in dimension_names],
                *[metric_values[name] for name in metric_names],
            ],
        ],
    }


def test_run_one_with_youtube_analytics_succeeds_for_cms_channels_only(
    session: Session, _stub_secret_resolver
) -> None:
    """Drive run_one end-to-end with the youtube-analytics connector.

    Two channels are seeded: one CMS-owned (content_owner_id matches
    _ANALYTICS_ACCOUNT_ID) and one outside-CMS (content_owner_id=None).
    Only the CMS-owned channel is eligible for B2.5.

    YouTubeAnalyticsClient.fetch_channel_report is patched at orchestrator
    module scope to return a parser-ready payload per channel (no network).
    GoogleHttpClient and refresh_credentials are also patched so no OAuth
    traffic occurs.

    Asserts:
    - outcome.run.status == "SUCCEEDED"
    - outcome.counts["reports_succeeded"] == 1
    - outcome.counts["reports_failed"] == 0
    - One raw_report_files row with source == "youtube_analytics"
    - One connector_run_raw_files join row
    - Source rows in google_revenue_source_rows for the tenant
    """
    # ----- seed channels -----
    ch_cms = YouTubeChannelORM(
        id=uuid4(),
        tenant_id=TENANT_ID,
        youtube_channel_id="UC_orch_cms",
        channel_name="CMS Channel",
        content_owner_id=_ANALYTICS_ACCOUNT_ID,
        active=True,
        revenue_required=True,
        cms_status="INSIDE_CMS",
    )
    ch_ext = YouTubeChannelORM(
        id=uuid4(),
        tenant_id=TENANT_ID,
        youtube_channel_id="UC_orch_ext",
        channel_name="OUTSIDE_CMS-tagged Channel (same owner, excluded by cms_status)",
        content_owner_id=_ANALYTICS_ACCOUNT_ID,
        active=True,
        revenue_required=True,
        cms_status="OUTSIDE_CMS",
    )
    session.add_all([ch_cms, ch_ext])
    session.flush()

    # ----- seed credential row -----
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=_ANALYTICS_CONNECTOR_KEY,
        account_id=_ANALYTICS_ACCOUNT_ID,
    )

    report_month = "2026-05"

    # Build the per-channel payloads the stub will return.
    payload_cms = _make_analytics_parser_payload(
        channel_id="UC_orch_cms",
        report_month=report_month,
    )

    def fake_fetch_channel_report(
        *,
        account_id: str,
        channel_id: str,
        report_month: str,
    ) -> dict:
        """Return the CMS payload for the matching channel id; fail loud otherwise."""
        assert account_id == _ANALYTICS_ACCOUNT_ID
        assert report_month == "2026-05"
        if channel_id == "UC_orch_cms":
            return payload_cms
        raise ValueError(f"unexpected channel_id in stub: {channel_id!r}")

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeAnalyticsClient"
        ) as yt_analytics_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        analytics_client = yt_analytics_cls.return_value
        analytics_client.fetch_channel_report.side_effect = fake_fetch_channel_report

        backend = local_cls.return_value
        store: dict[str, bytes] = {}

        def fake_upload(*, storage_uri, content):
            """Stash uploaded bytes in the in-memory blob store."""
            store[storage_uri] = content

        def fake_get(*, storage_uri):
            """Read bytes back from the in-memory blob store."""
            return store[storage_uri]

        backend.upload.side_effect = fake_upload
        backend.get_bytes.side_effect = fake_get

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=_ANALYTICS_CONNECTOR_KEY,
            account_id=_ANALYTICS_ACCOUNT_ID,
            report_month=report_month,
        )

    # ----- outcome shape -----
    assert isinstance(outcome, ConnectorRunOutcome)
    assert outcome.run is not None
    assert outcome.run.status == "SUCCEEDED"
    assert outcome.per_report_failures == []

    # ----- counts: only the CMS-owned channel is fetched -----
    counts = outcome.counts
    expected_row_count = len(_ANALYTICS_METRICS.split(","))
    assert counts["reports_attempted"] == 1
    assert counts["reports_succeeded"] == 1
    assert counts["reports_failed"] == 0
    assert counts["rows_upserted_total"] == expected_row_count

    # ----- durable side effects: raw_report_files -----
    raw_files = session.scalars(
        select(RawReportFileORM).where(RawReportFileORM.tenant_id == TENANT_ID)
    ).all()
    assert len(raw_files) == 1
    for rf in raw_files:
        assert rf.parse_status == "PARSED"
        assert rf.source == "youtube_analytics"
        assert rf.report_month == report_month

    # ----- durable side effects: connector_run_raw_files join rows -----
    links = session.scalars(
        select(ConnectorRunRawFileORM).where(ConnectorRunRawFileORM.tenant_id == TENANT_ID)
    ).all()
    assert len(links) == 1

    # ----- durable side effects: google_revenue_source_rows -----
    source_rows = session.scalars(
        select(GoogleRevenueSourceRowORM).where(GoogleRevenueSourceRowORM.tenant_id == TENANT_ID)
    ).all()
    assert len(source_rows) == expected_row_count
    assert all(r.source_system == "youtube_analytics" for r in source_rows)
    assert {r.metric_key for r in source_rows} == set(_ANALYTICS_METRICS.split(","))
    assert {r.youtube_channel_id for r in source_rows} == {"UC_orch_cms"}


def test_run_one_with_youtube_analytics_contains_channel_fetch_failures(
    session: Session, _stub_secret_resolver
) -> None:
    """A single targeted-channel API failure must stay in Bucket B.

    The failing channel is ordered first so this test proves the runner yields
    a per-report failure and then continues on to the remaining CMS-owned
    channel instead of aborting the whole run.
    """
    session.add_all(
        [
            YouTubeChannelORM(
                id=uuid4(),
                tenant_id=TENANT_ID,
                youtube_channel_id="UC_001_fail",
                channel_name="Fail First",
                content_owner_id=_ANALYTICS_ACCOUNT_ID,
                active=True,
                revenue_required=True,
                cms_status="INSIDE_CMS",
            ),
            YouTubeChannelORM(
                id=uuid4(),
                tenant_id=TENANT_ID,
                youtube_channel_id="UC_002_ok",
                channel_name="Succeed Second",
                content_owner_id=_ANALYTICS_ACCOUNT_ID,
                active=True,
                revenue_required=True,
                cms_status="INSIDE_CMS",
            ),
        ]
    )
    session.flush()
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=_ANALYTICS_CONNECTOR_KEY,
        account_id=_ANALYTICS_ACCOUNT_ID,
    )

    payload_ok = _make_analytics_parser_payload(
        channel_id="UC_002_ok",
        report_month="2026-05",
    )

    def fake_fetch_channel_report(*, account_id: str, channel_id: str, report_month: str) -> dict:
        """Raise 503 for the failing channel; return the success payload for the other."""
        assert account_id == _ANALYTICS_ACCOUNT_ID
        assert report_month == "2026-05"
        if channel_id == "UC_001_fail":
            raise GoogleApiServerError(
                method="GET",
                url="https://youtubeanalytics.googleapis.com/v2/reports",
                status=503,
                attempts=4,
            )
        if channel_id == "UC_002_ok":
            return payload_ok
        raise ValueError(f"unexpected channel_id in stub: {channel_id!r}")

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeAnalyticsClient"
        ) as yt_analytics_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None
        yt_analytics_cls.return_value.fetch_channel_report.side_effect = fake_fetch_channel_report

        backend = local_cls.return_value
        store: dict[str, bytes] = {}
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=_ANALYTICS_CONNECTOR_KEY,
            account_id=_ANALYTICS_ACCOUNT_ID,
            report_month="2026-05",
        )

    assert isinstance(outcome, ConnectorRunOutcome)
    assert outcome.run is not None
    assert outcome.run.status == "PARTIAL"
    assert outcome.counts["reports_attempted"] == 2
    assert outcome.counts["reports_succeeded"] == 1
    assert outcome.counts["reports_failed"] == 1
    assert outcome.per_report_failures == [("youtube_analytics", "GoogleApiServerError")]

    raw_files = session.scalars(
        select(RawReportFileORM).where(RawReportFileORM.tenant_id == TENANT_ID)
    ).all()
    assert len(raw_files) == 1
    assert raw_files[0].source == "youtube_analytics"
    assert raw_files[0].parse_status == "PARSED"


def test_run_one_with_youtube_analytics_empty_success_replaces_existing_rows(
    session: Session, _stub_secret_resolver
) -> None:
    """An empty successful rerun must delete stale rows on the content-owner scope."""
    session.add(
        YouTubeChannelORM(
            id=uuid4(),
            tenant_id=TENANT_ID,
            youtube_channel_id="UC_stale_scope",
            channel_name="Stale Scope Channel",
            content_owner_id=_ANALYTICS_ACCOUNT_ID,
            active=True,
            revenue_required=True,
            cms_status="INSIDE_CMS",
        )
    )
    session.flush()
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=_ANALYTICS_CONNECTOR_KEY,
        account_id=_ANALYTICS_ACCOUNT_ID,
    )

    payload_with_row = _make_analytics_parser_payload(
        channel_id="UC_stale_scope",
        report_month="2026-05",
    )
    payload_empty = {**payload_with_row, "rows": []}

    def _run_with_payload(payload: dict[str, object]) -> ConnectorRunOutcome:
        """Run the analytics ingestion once with the supplied parser payload."""

        def fake_fetch_channel_report(
            *, account_id: str, channel_id: str, report_month: str
        ) -> dict:
            """Return the closure-captured payload for the single attempted channel."""
            assert account_id == _ANALYTICS_ACCOUNT_ID
            assert channel_id == "UC_stale_scope"
            assert report_month == "2026-05"
            return payload

        with (
            patch(
                "ums_smart_revenue.connectors.runs.orchestrator.YouTubeAnalyticsClient"
            ) as yt_analytics_cls,
            patch(
                "ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend"
            ) as local_cls,
            patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
            patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
        ):
            http_cls.return_value.close.return_value = None
            refresh.return_value = None
            yt_analytics_cls.return_value.fetch_channel_report.side_effect = (
                fake_fetch_channel_report
            )

            backend = local_cls.return_value
            store: dict[str, bytes] = {}
            backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
                storage_uri, content
            )
            backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

            return run_one(
                session,
                tenant_id=TENANT_ID,
                connector_key=_ANALYTICS_CONNECTOR_KEY,
                account_id=_ANALYTICS_ACCOUNT_ID,
                report_month="2026-05",
            )

    first_outcome = _run_with_payload(payload_with_row)
    assert first_outcome.run is not None
    assert first_outcome.run.status == "SUCCEEDED"
    initial_rows = session.scalars(
        select(GoogleRevenueSourceRowORM).where(
            GoogleRevenueSourceRowORM.tenant_id == TENANT_ID,
            GoogleRevenueSourceRowORM.source_system == "youtube_analytics",
            GoogleRevenueSourceRowORM.report_type == "reports.query",
            GoogleRevenueSourceRowORM.report_month == "2026-05",
        )
    ).all()
    assert len(initial_rows) >= 1
    assert {row.source_account_id for row in initial_rows} == {
        f"contentOwner=={_ANALYTICS_ACCOUNT_ID}"
    }

    second_outcome = _run_with_payload(payload_empty)
    assert second_outcome.run is not None
    assert second_outcome.run.status == "SUCCEEDED"
    assert second_outcome.counts["reports_attempted"] == 1
    assert second_outcome.counts["reports_succeeded"] == 1
    assert second_outcome.counts["rows_upserted_total"] == 0
    assert second_outcome.counts["rows_deleted_stale"] == len(initial_rows)

    final_rows = session.scalars(
        select(GoogleRevenueSourceRowORM).where(
            GoogleRevenueSourceRowORM.tenant_id == TENANT_ID,
            GoogleRevenueSourceRowORM.source_system == "youtube_analytics",
            GoogleRevenueSourceRowORM.report_type == "reports.query",
            GoogleRevenueSourceRowORM.report_month == "2026-05",
        )
    ).all()
    assert final_rows == []


def test_run_one_with_youtube_analytics_keeps_sibling_cms_rows_on_full_success(
    session: Session, _stub_secret_resolver
) -> None:
    """A full owner-month replacement must retain rows from every CMS sibling."""
    session.add_all(
        [
            YouTubeChannelORM(
                id=uuid4(),
                tenant_id=TENANT_ID,
                youtube_channel_id="UC_scope_a",
                channel_name="Scope A",
                content_owner_id=_ANALYTICS_ACCOUNT_ID,
                active=True,
                revenue_required=True,
                cms_status="INSIDE_CMS",
            ),
            YouTubeChannelORM(
                id=uuid4(),
                tenant_id=TENANT_ID,
                youtube_channel_id="UC_scope_b",
                channel_name="Scope B",
                content_owner_id=_ANALYTICS_ACCOUNT_ID,
                active=True,
                revenue_required=True,
                cms_status="INSIDE_CMS",
            ),
        ]
    )
    session.flush()
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=_ANALYTICS_CONNECTOR_KEY,
        account_id=_ANALYTICS_ACCOUNT_ID,
    )

    payload_by_channel = {
        "UC_scope_a": _make_analytics_parser_payload(
            channel_id="UC_scope_a",
            report_month="2026-05",
        ),
        "UC_scope_b": _make_analytics_parser_payload(
            channel_id="UC_scope_b",
            report_month="2026-05",
        ),
    }

    def fake_fetch_channel_report(*, account_id: str, channel_id: str, report_month: str) -> dict:
        """Return the per-channel payload from the lookup table."""
        assert account_id == _ANALYTICS_ACCOUNT_ID
        assert report_month == "2026-05"
        return payload_by_channel[channel_id]

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeAnalyticsClient"
        ) as yt_analytics_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None
        yt_analytics_cls.return_value.fetch_channel_report.side_effect = fake_fetch_channel_report

        backend = local_cls.return_value
        store: dict[str, bytes] = {}
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=_ANALYTICS_CONNECTOR_KEY,
            account_id=_ANALYTICS_ACCOUNT_ID,
            report_month="2026-05",
        )

    assert outcome.run is not None
    assert outcome.run.status == "SUCCEEDED"
    assert outcome.counts["reports_attempted"] == 2
    assert outcome.counts["reports_succeeded"] == 2
    assert outcome.counts["reports_failed"] == 0
    expected_metric_count = len(_ANALYTICS_METRICS.split(","))
    assert outcome.counts["rows_upserted_total"] == 2 * expected_metric_count

    final_rows = session.scalars(
        select(GoogleRevenueSourceRowORM)
        .where(
            GoogleRevenueSourceRowORM.tenant_id == TENANT_ID,
            GoogleRevenueSourceRowORM.source_system == "youtube_analytics",
            GoogleRevenueSourceRowORM.report_type == "reports.query",
            GoogleRevenueSourceRowORM.report_month == "2026-05",
        )
        .order_by(GoogleRevenueSourceRowORM.youtube_channel_id)
    ).all()
    assert len(final_rows) == 2 * expected_metric_count
    assert {row.youtube_channel_id for row in final_rows} == {
        "UC_scope_a",
        "UC_scope_b",
    }
    assert sum(row.youtube_channel_id == "UC_scope_a" for row in final_rows) == (
        expected_metric_count
    )
    assert sum(row.youtube_channel_id == "UC_scope_b" for row in final_rows) == (
        expected_metric_count
    )
    assert {row.metric_key for row in final_rows} == set(_ANALYTICS_METRICS.split(","))
    assert {row.source_account_id for row in final_rows} == {
        f"contentOwner=={_ANALYTICS_ACCOUNT_ID}"
    }


def test_run_one_with_youtube_analytics_partial_run_preserves_failed_sibling_rows(
    session: Session, _stub_secret_resolver
) -> None:
    """A partial owner-month rerun must not stale-delete rows for failed siblings."""
    session.add_all(
        [
            YouTubeChannelORM(
                id=uuid4(),
                tenant_id=TENANT_ID,
                youtube_channel_id="UC_keep_ok",
                channel_name="Keep OK",
                content_owner_id=_ANALYTICS_ACCOUNT_ID,
                active=True,
                revenue_required=True,
                cms_status="INSIDE_CMS",
            ),
            YouTubeChannelORM(
                id=uuid4(),
                tenant_id=TENANT_ID,
                youtube_channel_id="UC_keep_fail",
                channel_name="Keep Fail",
                content_owner_id=_ANALYTICS_ACCOUNT_ID,
                active=True,
                revenue_required=True,
                cms_status="INSIDE_CMS",
            ),
        ]
    )
    session.flush()
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=_ANALYTICS_CONNECTOR_KEY,
        account_id=_ANALYTICS_ACCOUNT_ID,
    )

    payload_by_channel = {
        "UC_keep_fail": _make_analytics_parser_payload(
            channel_id="UC_keep_fail",
            report_month="2026-05",
        ),
        "UC_keep_ok": _make_analytics_parser_payload(
            channel_id="UC_keep_ok",
            report_month="2026-05",
        ),
    }

    def _run_with_fetch(
        fetch_impl,
    ) -> ConnectorRunOutcome:
        """Run the analytics ingestion once with the supplied fetch implementation."""
        with (
            patch(
                "ums_smart_revenue.connectors.runs.orchestrator.YouTubeAnalyticsClient"
            ) as yt_analytics_cls,
            patch(
                "ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend"
            ) as local_cls,
            patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
            patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
        ):
            http_cls.return_value.close.return_value = None
            refresh.return_value = None
            yt_analytics_cls.return_value.fetch_channel_report.side_effect = fetch_impl

            backend = local_cls.return_value
            store: dict[str, bytes] = {}
            backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
                storage_uri, content
            )
            backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

            return run_one(
                session,
                tenant_id=TENANT_ID,
                connector_key=_ANALYTICS_CONNECTOR_KEY,
                account_id=_ANALYTICS_ACCOUNT_ID,
                report_month="2026-05",
            )

    def first_fetch(*, account_id: str, channel_id: str, report_month: str) -> dict:
        """Initial successful run: every attempted channel returns its payload."""
        assert account_id == _ANALYTICS_ACCOUNT_ID
        assert report_month == "2026-05"
        return payload_by_channel[channel_id]

    first_outcome = _run_with_fetch(first_fetch)
    assert first_outcome.run is not None
    assert first_outcome.run.status == "SUCCEEDED"

    def second_fetch(*, account_id: str, channel_id: str, report_month: str) -> dict:
        """Rerun: UC_keep_fail raises 503 so its sibling rows must be preserved."""
        assert account_id == _ANALYTICS_ACCOUNT_ID
        assert report_month == "2026-05"
        if channel_id == "UC_keep_fail":
            raise GoogleApiServerError(
                method="GET",
                url="https://youtubeanalytics.googleapis.com/v2/reports",
                status=503,
                attempts=4,
            )
        return payload_by_channel[channel_id]

    second_outcome = _run_with_fetch(second_fetch)
    assert second_outcome.run is not None
    assert second_outcome.run.status == "PARTIAL"
    assert second_outcome.counts["reports_attempted"] == 2
    assert second_outcome.counts["reports_succeeded"] == 1
    assert second_outcome.counts["reports_failed"] == 1
    expected_metric_count = len(_ANALYTICS_METRICS.split(","))
    assert second_outcome.counts["rows_upserted_total"] == expected_metric_count
    assert second_outcome.per_report_failures == [("youtube_analytics", "GoogleApiServerError")]

    final_rows = session.scalars(
        select(GoogleRevenueSourceRowORM)
        .where(
            GoogleRevenueSourceRowORM.tenant_id == TENANT_ID,
            GoogleRevenueSourceRowORM.source_system == "youtube_analytics",
            GoogleRevenueSourceRowORM.report_type == "reports.query",
            GoogleRevenueSourceRowORM.report_month == "2026-05",
        )
        .order_by(GoogleRevenueSourceRowORM.youtube_channel_id)
    ).all()
    assert len(final_rows) == 2 * expected_metric_count
    assert {row.youtube_channel_id for row in final_rows} == {
        "UC_keep_fail",
        "UC_keep_ok",
    }
    assert sum(row.youtube_channel_id == "UC_keep_fail" for row in final_rows) == (
        expected_metric_count
    )
    assert sum(row.youtube_channel_id == "UC_keep_ok" for row in final_rows) == (
        expected_metric_count
    )
    assert {row.metric_key for row in final_rows} == set(_ANALYTICS_METRICS.split(","))


def test_run_one_with_youtube_analytics_real_local_file_store_backend_round_trips(
    session: Session, _stub_secret_resolver, tmp_path, monkeypatch
) -> None:
    """End-to-end analytics ingestion against the REAL LocalFileStoreBackend.

    Every other analytics orchestrator test patches LocalFileStoreBackend with
    a MagicMock whose ``upload`` swallows any URI string, so the real
    ``LocalFileStoreBackend._path_for`` never executes. That mask would hide
    any scheme-mismatch bug equivalent to B2.4 Concern A (where
    ``deterministic_blob_path`` emitted a ``gs://`` URI for a file-store
    backend). This test mirrors test_run_one_real_local_file_store_backend_round_trips
    for the youtube-analytics path.

    This test does NOT patch LocalFileStoreBackend. It points the real backend
    at ``tmp_path`` via env-vars, drives ``run_one`` through to SUCCEEDED for
    the CMS-owned channel only, then asserts that channel's bytes landed on disk at the
    deterministic path and each persisted RawReportFileORM carries a
    ``file-store://`` URL.
    """
    monkeypatch.setenv("UMS_BLOB_BACKEND", "file-store")
    monkeypatch.setenv("UMS_LOCAL_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("UMS_LOCAL_BLOB_BUCKET", "testbucket")

    # ----- seed channels -----
    ch_cms = YouTubeChannelORM(
        id=uuid4(),
        tenant_id=TENANT_ID,
        youtube_channel_id="UC_fs_cms",
        channel_name="CMS FS Channel",
        content_owner_id=_ANALYTICS_ACCOUNT_ID,
        active=True,
        revenue_required=True,
        cms_status="INSIDE_CMS",
    )
    ch_ext = YouTubeChannelORM(
        id=uuid4(),
        tenant_id=TENANT_ID,
        youtube_channel_id="UC_fs_ext",
        channel_name="OUTSIDE_CMS-tagged FS Channel",
        content_owner_id=_ANALYTICS_ACCOUNT_ID,
        active=True,
        revenue_required=True,
        cms_status="OUTSIDE_CMS",
    )
    session.add_all([ch_cms, ch_ext])
    session.flush()

    # ----- seed credential row -----
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=_ANALYTICS_CONNECTOR_KEY,
        account_id=_ANALYTICS_ACCOUNT_ID,
    )

    report_month = "2026-05"

    # Build the per-channel payloads the stub will return.
    payload_cms = _make_analytics_parser_payload(
        channel_id="UC_fs_cms",
        report_month=report_month,
    )
    # Pre-compute the raw_bytes the runner will produce so we can assert
    # the exact bytes on disk. The runner spreads the stub response,
    # synthesises the `channel` DIMENSION header / row prefix (since the wire
    # request uses `dimensions=month` only), then OVERWRITES query_request
    # with a freshly constructed dict using the canonical
    # _ANALYTICS_METRICS/_ANALYTICS_DIMENSIONS constants. Mirror that logic
    # here so the expected bytes match what lands on disk.
    _year, _month = report_month.split("-")
    _first_day = f"{_year}-{_month}-01"

    # The parser-payload's endDate is the calendar month end, not the wire
    # first-of-month, so persisted period_end records the actual coverage.
    from calendar import (
        monthrange as _monthrange,
    )  # local import to avoid test churn  # noqa: PLC0415

    _last_day = f"{int(_year):04d}-{int(_month):02d}-{_monthrange(int(_year), int(_month))[1]:02d}"

    def _runner_query_request(channel_id: str) -> dict:
        """Build the runner-side query_request dict (mirrors the wire request layout)."""
        return {
            "ids": f"contentOwner=={_ANALYTICS_ACCOUNT_ID}",
            "filters": f"channel=={channel_id}",
            "startDate": _first_day,
            "endDate": _last_day,
            "metrics": _ANALYTICS_METRICS,
            "dimensions": _ANALYTICS_DIMENSIONS,
        }

    def _synthesise_channel(payload: dict, channel_id: str) -> dict:
        """Inject the `channel` dimension into payload columns/rows (mirrors the runner)."""
        column_headers = payload.get("columnHeaders") or []
        rows = payload.get("rows") or []
        if any(isinstance(h, dict) and h.get("name") == "channel" for h in column_headers):
            return payload
        return {
            **payload,
            "columnHeaders": [
                {"columnType": "DIMENSION", "name": "channel"},
                *column_headers,
            ],
            "rows": [[channel_id, *row] if isinstance(row, list) else row for row in rows],
        }

    augmented_cms = {
        **_synthesise_channel(payload_cms, "UC_fs_cms"),
        "query_request": _runner_query_request("UC_fs_cms"),
    }
    raw_bytes_cms = json.dumps(augmented_cms, sort_keys=True).encode("utf-8")
    checksum_cms = hashlib.sha256(raw_bytes_cms).hexdigest()

    def fake_fetch_channel_report(
        *,
        account_id: str,
        channel_id: str,
        report_month: str,
    ) -> dict:
        """Return the CMS payload for UC_fs_cms; fail loud on unexpected channel ids."""
        assert account_id == _ANALYTICS_ACCOUNT_ID
        assert report_month == "2026-05"
        if channel_id == "UC_fs_cms":
            return payload_cms
        raise ValueError(f"unexpected channel_id in stub: {channel_id!r}")

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeAnalyticsClient"
        ) as yt_analytics_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        # NOTE: LocalFileStoreBackend is intentionally NOT patched here -- the
        # real backend writes to ``tmp_path`` so we can prove deterministic_blob_path
        # emits a file-store:// scheme the backend will accept.
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        analytics_client = yt_analytics_cls.return_value
        analytics_client.fetch_channel_report.side_effect = fake_fetch_channel_report

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=_ANALYTICS_CONNECTOR_KEY,
            account_id=_ANALYTICS_ACCOUNT_ID,
            report_month=report_month,
        )

    # Outcome must be SUCCEEDED: a pre-fix ValueError from
    # LocalFileStoreBackend._path_for would have driven this to FAILED.
    assert isinstance(outcome, ConnectorRunOutcome)
    assert outcome.run is not None
    assert outcome.run.status == "SUCCEEDED"
    assert outcome.per_report_failures == []
    expected_row_count = len(_ANALYTICS_METRICS.split(","))
    assert outcome.counts["reports_succeeded"] == 1
    assert outcome.counts["rows_upserted_total"] == expected_row_count

    # The deterministic path layout for analytics:
    # file-store://{bucket}/{tenant_id}/{connector_key}/{report_type}/{month}/{checksum}.json
    # LocalFileStoreBackend strips the scheme and treats the remainder as a
    # relative path under root.
    def _expected_path(checksum: str) -> _Path:
        """Compute the deterministic on-disk blob path for the given checksum."""
        return (
            tmp_path
            / "testbucket"
            / str(TENANT_ID)
            / _ANALYTICS_CONNECTOR_KEY
            / "youtube_analytics"
            / report_month
            / f"{checksum}.json"
        )

    expected_path = _expected_path(checksum_cms)
    assert expected_path.exists(), (
        f"raw blob for UC_fs_cms did not land on disk at {expected_path}; "
        f"tmp_path tree: {list(tmp_path.rglob('*'))}"
    )
    assert expected_path.read_bytes() == raw_bytes_cms, "blob bytes mismatch for UC_fs_cms"

    # The persisted file_url for each channel must carry file-store scheme + bucket.
    raw_files = session.scalars(
        select(RawReportFileORM).where(RawReportFileORM.tenant_id == TENANT_ID)
    ).all()
    assert len(raw_files) == 1
    for raw_file in raw_files:
        assert raw_file.file_url.startswith("file-store://testbucket/"), (
            f"expected file-store:// scheme + testbucket prefix; got {raw_file.file_url!r}"
        )
        assert raw_file.file_url.endswith(".json")

    source_rows = session.scalars(
        select(GoogleRevenueSourceRowORM).where(GoogleRevenueSourceRowORM.tenant_id == TENANT_ID)
    ).all()
    assert len(source_rows) == expected_row_count
    assert {row.metric_key for row in source_rows} == set(_ANALYTICS_METRICS.split(","))
    assert {row.youtube_channel_id for row in source_rows} == {"UC_fs_cms"}


def test_run_one_with_youtube_analytics_dry_run_succeeds_for_cms_channels_only(
    session: Session, _stub_secret_resolver
) -> None:
    """Regression: dry-run with youtube-analytics must not raise AttributeError.

    Before the str()-cast fix, the dry-run SimpleNamespace proxy stored
    tenant_id as a UUID object.  YouTubeAnalyticsRunner.produce_reports then
    called ``UUID(run.tenant_id)`` which on Python 3.14 raises::

        AttributeError: 'UUID' object has no attribute 'replace'

    because UUID.__init__ calls ``.replace()`` on its first positional arg
    expecting a hex string.  Any dry_run=True call with connector_key
    "youtube-analytics" crashed before entering the channel loop.

    This test exercises the exact code path: two channels seeded (one
    CMS-owned, one outside-CMS), same stub pattern as
    test_run_one_with_youtube_analytics_succeeds_for_cms_channels_only, but
    invoked with dry_run=True.

    Asserts:
    - No AttributeError (regression guard: test must FAIL before the str() fix)
    - outcome.run is None  (dry-run writes no connector_runs row)
    - counts["reports_attempted"] == 1
    - counts["reports_succeeded"] == 1
    - Zero rows in connector_runs and raw_report_files
    """
    # ----- seed channels (mirrors the live-path test) -----
    ch_cms = YouTubeChannelORM(
        id=uuid4(),
        tenant_id=TENANT_ID,
        youtube_channel_id="UC_dry_ana_cms",
        channel_name="CMS Channel (dry)",
        content_owner_id=_ANALYTICS_ACCOUNT_ID,
        active=True,
        revenue_required=True,
        cms_status="INSIDE_CMS",
    )
    ch_ext = YouTubeChannelORM(
        id=uuid4(),
        tenant_id=TENANT_ID,
        youtube_channel_id="UC_dry_ana_ext",
        channel_name="OUTSIDE_CMS-tagged Channel (dry)",
        content_owner_id=_ANALYTICS_ACCOUNT_ID,
        active=True,
        revenue_required=True,
        cms_status="OUTSIDE_CMS",
    )
    session.add_all([ch_cms, ch_ext])
    session.flush()

    # ----- seed credential row -----
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=_ANALYTICS_CONNECTOR_KEY,
        account_id=_ANALYTICS_ACCOUNT_ID,
    )

    report_month = "2026-05"

    payload_cms = _make_analytics_parser_payload(
        channel_id="UC_dry_ana_cms",
        report_month=report_month,
    )

    def fake_fetch_channel_report(
        *,
        account_id: str,
        channel_id: str,
        report_month: str,
    ) -> dict:
        """Return the CMS payload for UC_dry_ana_cms; fail loud on unexpected ids."""
        assert account_id == _ANALYTICS_ACCOUNT_ID
        assert report_month == "2026-05"
        if channel_id == "UC_dry_ana_cms":
            return payload_cms
        raise ValueError(f"unexpected channel_id in stub: {channel_id!r}")

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeAnalyticsClient"
        ) as yt_analytics_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        analytics_client = yt_analytics_cls.return_value
        analytics_client.fetch_channel_report.side_effect = fake_fetch_channel_report

        # Blob backend patched defensively: dry-run must never call upload/get_bytes.
        backend = local_cls.return_value
        backend.upload.side_effect = AssertionError("blob upload must not be called in dry-run")
        backend.get_bytes.side_effect = AssertionError(
            "blob get_bytes must not be called in dry-run"
        )

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=_ANALYTICS_CONNECTOR_KEY,
            account_id=_ANALYTICS_ACCOUNT_ID,
            report_month=report_month,
            dry_run=True,
        )

    # ----- outcome shape: dry-run returns run=None -----
    assert isinstance(outcome, ConnectorRunOutcome)
    assert outcome.run is None
    assert outcome.per_report_failures == []

    # ----- counts: only the CMS-owned channel is attempted + parsed cleanly -----
    counts = outcome.counts
    expected_row_count = len(_ANALYTICS_METRICS.split(","))
    assert counts["reports_attempted"] == 1
    assert counts["reports_succeeded"] == 1
    assert counts["reports_failed"] == 0
    # The parser sees the single CMS payload; dry-run reports the would-upsert
    # total but performs no source-row write/classification.
    assert expected_row_count > 0
    assert counts["rows_upserted_total"] == expected_row_count
    assert counts["rows_upserted_created"] == 0
    assert counts["rows_upserted_updated"] == 0
    assert counts["rows_upserted_unchanged"] == 0

    # ----- no DB writes: SAVEPOINT rollback reverts any runner side-effects -----
    assert session.query(ConnectorRunORM).count() == 0
    assert session.query(RawReportFileORM).count() == 0
    assert (
        session.query(ConnectorRunRawFileORM)
        .filter(ConnectorRunRawFileORM.tenant_id == TENANT_ID)
        .count()
        == 0
    )
    assert (
        session.query(GoogleRevenueSourceRowORM)
        .filter(GoogleRevenueSourceRowORM.tenant_id == TENANT_ID)
        .count()
        == 0
    )


def test_run_one_with_youtube_analytics_no_eligible_channels(
    session: Session, _stub_secret_resolver
) -> None:
    """Zero-eligible-channels path must terminate FAILED with reports_attempted=0.

    Seeds four channels, none of which match the (tenant_id + active +
    revenue_required + content_owner_id == account_id) eligibility filter:
      - UC_no_active: inactive (active=False)
      - UC_no_rev: revenue_required=False
      - UC_other_owner: content_owner_id != account_id
      - UC_other_tenant: belongs to a different tenant

    list_target_channels() returns [] and YouTubeAnalyticsRunner yields no
    reports. The orchestrator's _derive_terminal_status maps reports_attempted=0
    to status='FAILED' so the operator console flags the run, rather than
    leaving it RUNNING or marking SUCCEEDED with zero data.

    Asserts:
    - fetch_channel_report is never invoked
    - outcome.run.status == 'FAILED'
    - counts['reports_attempted'] == 0
    - counts['reports_succeeded'] == 0
    - counts['reports_failed'] == 0
    - No raw_report_files / connector_run_raw_files / source_rows persisted
    """
    other_tenant_id = uuid4()
    session.add_all(
        [
            # FIX: seed the parent TenantORM for the cross-tenant negative case so
            # the fixture works under strict FK enforcement, not just SQLite's
            # permissive default.
            TenantORM(
                id=other_tenant_id,
                slug="tenant-orch-other",
                display_name="Other Tenant",
            ),
            YouTubeChannelORM(
                id=uuid4(),
                tenant_id=TENANT_ID,
                youtube_channel_id="UC_no_active",
                channel_name="Inactive CMS",
                content_owner_id=_ANALYTICS_ACCOUNT_ID,
                active=False,
                revenue_required=True,
                cms_status="INSIDE_CMS",
            ),
            YouTubeChannelORM(
                id=uuid4(),
                tenant_id=TENANT_ID,
                youtube_channel_id="UC_no_rev",
                channel_name="No-revenue CMS",
                content_owner_id=_ANALYTICS_ACCOUNT_ID,
                active=True,
                revenue_required=False,
                cms_status="INSIDE_CMS",
            ),
            YouTubeChannelORM(
                id=uuid4(),
                tenant_id=TENANT_ID,
                youtube_channel_id="UC_other_owner",
                channel_name="Different content owner",
                content_owner_id="some-other-cms-owner",
                active=True,
                revenue_required=True,
                cms_status="INSIDE_CMS",
            ),
            YouTubeChannelORM(
                id=uuid4(),
                tenant_id=other_tenant_id,
                youtube_channel_id="UC_other_tenant",
                channel_name="Different tenant, same owner",
                content_owner_id=_ANALYTICS_ACCOUNT_ID,
                active=True,
                revenue_required=True,
                cms_status="INSIDE_CMS",
            ),
        ]
    )
    session.flush()
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=_ANALYTICS_CONNECTOR_KEY,
        account_id=_ANALYTICS_ACCOUNT_ID,
    )

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeAnalyticsClient"
        ) as yt_analytics_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        analytics_client = yt_analytics_cls.return_value
        analytics_client.fetch_channel_report.side_effect = AssertionError(
            "fetch_channel_report must not be called when no channels are eligible"
        )

        backend = local_cls.return_value
        backend.upload.side_effect = AssertionError(
            "blob upload must not run when no reports are produced"
        )
        backend.get_bytes.side_effect = AssertionError(
            "blob get_bytes must not run when no reports are produced"
        )

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=_ANALYTICS_CONNECTOR_KEY,
            account_id=_ANALYTICS_ACCOUNT_ID,
            report_month="2026-05",
        )

        assert analytics_client.fetch_channel_report.call_count == 0

    assert isinstance(outcome, ConnectorRunOutcome)
    assert outcome.run is not None
    assert outcome.run.status == "FAILED"
    assert outcome.per_report_failures == []

    counts = outcome.counts
    assert counts["reports_attempted"] == 0
    assert counts["reports_succeeded"] == 0
    assert counts["reports_failed"] == 0
    assert counts["rows_upserted_total"] == 0
    assert counts["rows_upserted_created"] == 0
    assert counts["rows_upserted_updated"] == 0
    assert counts["rows_upserted_unchanged"] == 0

    assert (
        session.query(RawReportFileORM).filter(RawReportFileORM.tenant_id == TENANT_ID).count() == 0
    )
    assert (
        session.query(ConnectorRunRawFileORM)
        .filter(ConnectorRunRawFileORM.tenant_id == TENANT_ID)
        .count()
        == 0
    )
    assert (
        session.query(GoogleRevenueSourceRowORM)
        .filter(GoogleRevenueSourceRowORM.tenant_id == TENANT_ID)
        .count()
        == 0
    )


def test_run_one_with_youtube_analytics_preserves_rows_for_deactivated_channels(
    session: Session, _stub_secret_resolver
) -> None:
    """A channel that falls out of the target set keeps its historical rows.

    Scenario: tenant has channels A and B, both active and revenue-required.
    Run 1 ingests both. Then B is deactivated (active=False). Run 2 only
    fetches A. The deferred stale-row cleanup scopes by
    (tenant, source, source_account_id, report_type, report_month) which is
    content-owner-wide, so a naive "delete except keep_keys" would erase B's
    historical rows because B contributes no keep keys in run 2. The fix
    preserves rows whose youtube_channel_id is not in the run's attempted set.

    Asserts (after run 2):
    - A's rows are fully replaced (current run's keys, expected metric count)
    - B's rows from run 1 are still present (preserved historical revenue)
    - outcome.run.status == 'SUCCEEDED'
    """
    ch_a = YouTubeChannelORM(
        id=uuid4(),
        tenant_id=TENANT_ID,
        youtube_channel_id="UC_preserve_a",
        channel_name="Channel A",
        content_owner_id=_ANALYTICS_ACCOUNT_ID,
        active=True,
        revenue_required=True,
        cms_status="INSIDE_CMS",
    )
    ch_b = YouTubeChannelORM(
        id=uuid4(),
        tenant_id=TENANT_ID,
        youtube_channel_id="UC_preserve_b",
        channel_name="Channel B (will be deactivated)",
        content_owner_id=_ANALYTICS_ACCOUNT_ID,
        active=True,
        revenue_required=True,
        cms_status="INSIDE_CMS",
    )
    session.add_all([ch_a, ch_b])
    session.flush()
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=_ANALYTICS_CONNECTOR_KEY,
        account_id=_ANALYTICS_ACCOUNT_ID,
    )

    payload_by_channel_run1 = {
        "UC_preserve_a": _make_analytics_parser_payload(
            channel_id="UC_preserve_a",
            report_month="2026-05",
        ),
        "UC_preserve_b": _make_analytics_parser_payload(
            channel_id="UC_preserve_b",
            report_month="2026-05",
        ),
    }

    def fake_fetch_run1(*, account_id: str, channel_id: str, report_month: str) -> dict:
        """Run 1: both channels are still active and return their payloads."""
        assert account_id == _ANALYTICS_ACCOUNT_ID
        assert report_month == "2026-05"
        return payload_by_channel_run1[channel_id]

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeAnalyticsClient"
        ) as yt_analytics_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None
        yt_analytics_cls.return_value.fetch_channel_report.side_effect = fake_fetch_run1
        backend = local_cls.return_value
        store: dict[str, bytes] = {}
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

        outcome_1 = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=_ANALYTICS_CONNECTOR_KEY,
            account_id=_ANALYTICS_ACCOUNT_ID,
            report_month="2026-05",
        )

    assert outcome_1.run is not None
    assert outcome_1.run.status == "SUCCEEDED"
    expected_metric_count = len(_ANALYTICS_METRICS.split(","))

    run1_rows = session.scalars(
        select(GoogleRevenueSourceRowORM).where(
            GoogleRevenueSourceRowORM.tenant_id == TENANT_ID,
            GoogleRevenueSourceRowORM.source_system == "youtube_analytics",
        )
    ).all()
    assert {row.youtube_channel_id for row in run1_rows} == {
        "UC_preserve_a",
        "UC_preserve_b",
    }
    b_row_keys_before = sorted(
        row.source_row_key for row in run1_rows if row.youtube_channel_id == "UC_preserve_b"
    )
    assert len(b_row_keys_before) == expected_metric_count

    ch_b.active = False
    session.flush()

    payload_by_channel_run2 = {
        "UC_preserve_a": _make_analytics_parser_payload(
            channel_id="UC_preserve_a",
            report_month="2026-05",
        ),
    }

    def fake_fetch_run2(*, account_id: str, channel_id: str, report_month: str) -> dict:
        """Run 2: only UC_preserve_a is attempted; assert any other channel id is unreachable."""
        assert account_id == _ANALYTICS_ACCOUNT_ID
        assert report_month == "2026-05"
        if channel_id not in payload_by_channel_run2:
            raise AssertionError(f"run 2 fetched unexpected channel: {channel_id!r}")
        return payload_by_channel_run2[channel_id]

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeAnalyticsClient"
        ) as yt_analytics_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None
        yt_analytics_cls.return_value.fetch_channel_report.side_effect = fake_fetch_run2
        backend = local_cls.return_value
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

        outcome_2 = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=_ANALYTICS_CONNECTOR_KEY,
            account_id=_ANALYTICS_ACCOUNT_ID,
            report_month="2026-05",
        )

    assert outcome_2.run is not None
    assert outcome_2.run.status == "SUCCEEDED"
    assert outcome_2.counts["reports_attempted"] == 1
    assert outcome_2.counts["reports_succeeded"] == 1

    final_rows = session.scalars(
        select(GoogleRevenueSourceRowORM).where(
            GoogleRevenueSourceRowORM.tenant_id == TENANT_ID,
            GoogleRevenueSourceRowORM.source_system == "youtube_analytics",
        )
    ).all()
    assert {row.youtube_channel_id for row in final_rows} == {
        "UC_preserve_a",
        "UC_preserve_b",
    }
    a_count = sum(row.youtube_channel_id == "UC_preserve_a" for row in final_rows)
    b_count = sum(row.youtube_channel_id == "UC_preserve_b" for row in final_rows)
    assert a_count == expected_metric_count
    assert b_count == expected_metric_count
    b_row_keys_after = sorted(
        row.source_row_key for row in final_rows if row.youtube_channel_id == "UC_preserve_b"
    )
    assert b_row_keys_after == b_row_keys_before, (
        "B's historical row keys must be identical after a deactivation rerun"
    )


# ---------------------------------------------------------------------------
# T37: audit emitters wired into run_one orchestrator (spec B2.6 §8.4).
#
# These tests assert the connector audit lifecycle:
#   STARTED -> DOWNLOADED -> PARSED -> (DOWNLOADED -> FAILED)? -> FINISHED.
# Transaction semantics: STARTED commits with start_run, FINISHED commits
# with finish_run, and per-raw-file edges stage inside the main per-report
# transaction. Dry-run emits zero audit rows. The orchestrator fails closed
# in Bucket A when ``UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID`` is unset.
# ---------------------------------------------------------------------------


def _connector_audit_events(session: Session) -> list[AuditLogORM]:
    """Return audit rows tied to the connector audit lifecycle in insertion order.

    Excludes the post-run normalize ``ROWS_SKIPPED`` summary edge -- it is also a
    ``CONNECTOR_JOB_RUN`` event but is a projection-skip summary, not part of the
    run/raw-file STARTED->...->FINISHED lifecycle these tests assert. That edge is
    covered by ``test_normalize_wiring`` and ``test_ingestion_gate`` (Assertion 4c).
    """
    rows = list(
        session.scalars(
            select(AuditLogORM)
            .where(AuditLogORM.tenant_id == TENANT_ID)
            .where(AuditLogORM.event_type.in_(["CONNECTOR_JOB_RUN", "REPORT_IMPORTED"]))
            .order_by(AuditLogORM.created_at, AuditLogORM.id)
        )
    )
    return [row for row in rows if _audit_details(row).get("lifecycle") != "ROWS_SKIPPED"]


def _audit_details(event: AuditLogORM) -> dict[str, object]:
    """Return the JSON audit details as a mapping for typed test assertions."""
    details = event.details
    assert isinstance(details, dict)
    return details


def _audit_lifecycles(events: list[AuditLogORM]) -> list[str]:
    """Extract the ordered ``details["lifecycle"]`` discriminator chain."""
    lifecycles: list[str] = []
    for event in events:
        lifecycle = _audit_details(event).get("lifecycle")
        assert isinstance(lifecycle, str)
        lifecycles.append(lifecycle)
    return lifecycles


def _assert_clean_run_audit_sequence(
    events: list[AuditLogORM],
    *,
    rows_upserted_total: int,
) -> None:
    """Verify the audit chain and terminal details for a clean one-report run."""
    assert _audit_lifecycles(events) == [
        "STARTED",
        "DOWNLOADED",
        "PARSED",
        "FINISHED",
    ]
    assert [event.event_type for event in events] == [
        "CONNECTOR_JOB_RUN",
        "REPORT_IMPORTED",
        "REPORT_IMPORTED",
        "CONNECTOR_JOB_RUN",
    ]

    finished_details = _audit_details(events[-1])
    finished_counts = finished_details["counts"]
    assert isinstance(finished_counts, dict)
    assert {
        "status": finished_details["status"],
        "reports_succeeded": finished_counts["reports_succeeded"],
        "reports_failed": finished_counts["reports_failed"],
        "count_upserted": _audit_details(events[2])["count_upserted"],
    } == {
        "status": "SUCCEEDED",
        "reports_succeeded": 1,
        "reports_failed": 0,
        "count_upserted": rows_upserted_total,
    }


def _assert_partial_run_audit_sequence(events: list[AuditLogORM]) -> None:
    """Verify the audit chain and terminal details for a one-failure partial run."""
    assert _audit_lifecycles(events) == [
        "STARTED",
        "DOWNLOADED",
        "PARSED",
        "DOWNLOADED",
        "FAILED",
        "FINISHED",
    ]
    failed_details = _audit_details(events[4])
    finished_details = _audit_details(events[-1])
    finished_counts = finished_details["counts"]
    assert isinstance(finished_counts, dict)
    assert {
        "failed_event_type": events[4].event_type,
        "failed_error_class": failed_details["error_class"],
        "finished_event_type": events[-1].event_type,
        "finished_status": finished_details["status"],
        "reports_succeeded": finished_counts["reports_succeeded"],
        "reports_failed": finished_counts["reports_failed"],
        "error_summary_present": finished_details["error_summary_present"],
    } == {
        "failed_event_type": "REPORT_IMPORTED",
        "failed_error_class": "ParserError",
        "finished_event_type": "CONNECTOR_JOB_RUN",
        "finished_status": "PARTIAL",
        "reports_succeeded": 1,
        "reports_failed": 1,
        "error_summary_present": True,
    }


def test_run_one_emits_audit_started_finished_for_clean_run(
    session: Session, _stub_secret_resolver
) -> None:
    """A clean 1-report SUCCEEDED run emits STARTED -> DOWNLOADED -> PARSED -> FINISHED."""
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    csv_bytes = _csv_for_one_row()

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"}
        ]
        client.list_reports_for_month.return_value = [{"id": "r1", "downloadUrl": "https://yt/r1"}]
        client.fetch_report.return_value = csv_bytes

        backend = local_cls.return_value
        store: dict[str, bytes] = {}
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    assert outcome.run is not None
    assert outcome.run.status == "SUCCEEDED"

    events = _connector_audit_events(session)
    _assert_clean_run_audit_sequence(
        events,
        rows_upserted_total=outcome.counts["rows_upserted_total"],
    )


def test_run_one_emits_audit_event_sequence_for_partial_run(
    session: Session, _stub_secret_resolver
) -> None:
    """A 2-report run with 1 success + 1 parse failure emits 6 lifecycle audit rows."""
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    csv_bytes_a = (
        b"date,channel,content_owner,estimatedRevenue,currencyCode\n"
        b"2026-05-01,UC_audit_alpha,cms-orch-1,10.000000,USD\n"
    )
    csv_bytes_b = (
        b"date,channel,content_owner,estimatedRevenue,currencyCode\n"
        b"2026-05-02,UC_audit_beta,cms-orch-1,20.000000,USD\n"
    )

    from ums_smart_revenue.connectors.google_source_parsers import (
        YouTubeReportingParser as RealParser,
    )

    real_parser = RealParser()
    call_state = {"n": 0}

    class FlakyParser:
        """Parser that succeeds on the first parse and raises ParserError on the second."""

        @staticmethod
        def parse(payload, *, tenant_id):
            """Pass the first call through and raise on the second."""
            call_state["n"] += 1
            if call_state["n"] == 2:
                raise ParserError("simulated parser failure for audit ordering test")
            return list(real_parser.parse(payload, tenant_id=tenant_id))

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator._parser_for_connector",
            return_value=FlakyParser(),
        ),
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"},
            {"id": "job-2", "reportTypeId": "content_owner_basic_a3"},
        ]
        reports_by_job = {
            "job-1": [{"id": "r1", "downloadUrl": "https://yt/r1"}],
            "job-2": [{"id": "r2", "downloadUrl": "https://yt/r2"}],
        }
        client.list_reports_for_month.side_effect = lambda *, account_id, job_id, report_month: (
            reports_by_job[job_id]
        )
        bytes_by_url = {"https://yt/r1": csv_bytes_a, "https://yt/r2": csv_bytes_b}
        client.fetch_report.side_effect = lambda *, download_url: bytes_by_url[download_url]

        backend = local_cls.return_value
        store: dict[str, bytes] = {}
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    assert outcome.run is not None
    assert outcome.run.status == "PARTIAL"

    events = _connector_audit_events(session)
    _assert_partial_run_audit_sequence(events)


def test_run_one_dry_run_emits_zero_audit_events(session: Session, _stub_secret_resolver) -> None:
    """``dry_run=True`` writes no audit rows -- the dry-run path skips emitters entirely."""
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    csv_bytes = _csv_for_one_row()

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_client_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        client = yt_client_cls.return_value
        client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"}
        ]
        client.list_reports_for_month.return_value = [{"id": "r1", "downloadUrl": "https://yt/r1"}]
        client.fetch_report.return_value = csv_bytes

        backend = local_cls.return_value
        backend.upload.side_effect = AssertionError("blob upload must not be called in dry-run")
        backend.get_bytes.side_effect = AssertionError(
            "blob get_bytes must not be called in dry-run"
        )

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
            dry_run=True,
        )

    assert outcome.run is None
    assert _connector_audit_events(session) == []


def test_run_one_fail_closed_when_service_actor_id_missing(
    session: Session,
    _stub_secret_resolver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live run raises typed Bucket-A exception when the service-actor env is unset.

    The orchestrator constructs the connector service principal before
    ``start_run``, so a missing UUID fails CLOSED with no half-created
    RUNNING row and no audit emissions. The orchestrator translates the
    raw ``ValueError`` from ``build_connector_service_principal`` into
    the typed ``ConnectorServicePrincipalUnavailableError`` (a
    ``GoogleConnectorError`` subclass) so the executor's
    ``except GoogleConnectorError`` branch can audit it as a Bucket-A
    ``job_failed_before_start`` row.
    """
    from ums_smart_revenue.config.settings import (
        GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV,
        load_app_settings,
    )
    from ums_smart_revenue.connectors.google.errors import (
        ConnectorServicePrincipalUnavailableError,
    )

    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=CONNECTOR_KEY,
        account_id=ACCOUNT_ID,
    )
    monkeypatch.delenv(GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV, raising=False)
    load_app_settings.cache_clear()

    with (
        patch("ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"),
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend"),
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials",
            return_value=None,
        ),
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient"),
        pytest.raises(ConnectorServicePrincipalUnavailableError) as excinfo,
    ):
        run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=CONNECTOR_KEY,
            account_id=ACCOUNT_ID,
            report_month="2026-05",
        )

    # Error mentions the env name so an operator can act on the message.
    assert GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV in str(excinfo.value)
    # Bucket A semantics: no connector_runs row, no raw_file row, no audit row.
    assert session.query(ConnectorRunORM).count() == 0
    assert session.query(RawReportFileORM).count() == 0
    assert _connector_audit_events(session) == []


# ============================================================================
# B2.6 Task 38: AdSenseManagementRunner orchestrator integration test.
# AdSense is account-scoped (one report per account per month), so the runner
# yields exactly one parser-ready payload per run and the orchestrator should
# SUCCEED with one DOWNLOADED -> PARSED raw_file. AdSense rows skip in C1 as
# MISSING_CHANNEL_ID, but that gate lives in T39's end-to-end test, not here.
# ============================================================================

_ADSENSE_CONNECTOR_KEY = "adsense-management"
_ADSENSE_ACCOUNT_ID = "pub-orch-1"


def _make_adsense_parser_payload(
    *,
    account_id: str = _ADSENSE_ACCOUNT_ID,
    report_month: str = "2026-05",
) -> dict[str, object]:
    """Build the parser-ready AdSense payload the mock client returns.

    Mirrors the shape ``adsense_response_to_parser_payload`` stamps onto the
    wire response: a ``request`` dict carrying ``accountId`` (prefixed with
    ``accounts/``), the ``dateRange``, and the ``currencyCode``; a ``headers``
    list whose entries declare ``type`` (DIMENSION or METRIC_CURRENCY) and
    ``name``; a ``rows`` list of ``{"cells": [...]}`` entries; and the
    deterministic ``report_id`` string.

    One MONTH dimension + the locked ESTIMATED_EARNINGS + TOTAL_EARNINGS
    metric pair produces two ParsedSourceRow rows on the same input row.
    """
    year_s, month_s = report_month.split("-")
    year_i, month_i = int(year_s), int(month_s)
    from calendar import monthrange as _monthrange

    last_day = _monthrange(year_i, month_i)[1]
    return {
        "request": {
            "accountId": f"accounts/{account_id}",
            "dateRange": {
                "startDate": {"year": year_i, "month": month_i, "day": 1},
                "endDate": {"year": year_i, "month": month_i, "day": last_day},
            },
            "currencyCode": "USD",
        },
        "headers": [
            {"type": "DIMENSION", "name": "MONTH"},
            {
                "type": "METRIC_CURRENCY",
                "name": "ESTIMATED_EARNINGS",
                "currencyCode": "USD",
            },
            {
                "type": "METRIC_CURRENCY",
                "name": "TOTAL_EARNINGS",
                "currencyCode": "USD",
            },
        ],
        "rows": [
            {
                "cells": [
                    {"value": f"{year_s}{month_s}"},
                    {"value": "123.450000"},
                    {"value": "67.890000"},
                ],
            },
        ],
        "report_id": f"deterministic-stub-{account_id}-{report_month}",
    }


def _make_adsense_payment_parser_payload(
    *,
    account_id: str = _ADSENSE_ACCOUNT_ID,
    report_month: str = "2026-05",
) -> dict[str, object]:
    """Build an AdSense payload that emits a payment_report row."""
    year_s, month_s = report_month.split("-")
    payload = _make_adsense_parser_payload(
        account_id=account_id,
        report_month=report_month,
    )
    payload["headers"] = [
        {"type": "DIMENSION", "name": "MONTH"},
        {"type": "METRIC_CURRENCY", "name": "PAID_AMOUNT", "currencyCode": "USD"},
    ]
    payload["rows"] = [
        {
            "cells": [
                {"value": f"{year_s}{month_s}"},
                {"value": "9.990000"},
            ],
        },
    ]
    payload["report_id"] = f"deterministic-stub-payment-{account_id}-{report_month}"
    return payload


def _run_adsense_orchestrator_with_payload(
    session: Session,
    *,
    account_id: str,
    report_month: str,
    payload: dict[str, object],
) -> ConnectorRunOutcome:
    """Drive one AdSense run with a parser-ready payload and no network."""
    expected_account_id = account_id
    expected_report_month = report_month

    def fake_fetch_monthly_report(*, account_id: str, report_month: str) -> dict:
        """Return the closure-captured payload for the account/month slice."""
        assert account_id == expected_account_id
        assert report_month == expected_report_month
        return payload

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.AdSenseManagementClient"
        ) as adsense_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None
        adsense_cls.return_value.fetch_monthly_report.side_effect = fake_fetch_monthly_report

        backend = local_cls.return_value
        store: dict[str, bytes] = {}
        backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
            storage_uri, content
        )
        backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]

        return run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=_ADSENSE_CONNECTOR_KEY,
            account_id=expected_account_id,
            report_month=expected_report_month,
        )


_UPSERT_COUNT_KEYS = (
    "rows_upserted_total",
    "rows_upserted_created",
    "rows_upserted_updated",
    "rows_upserted_unchanged",
)


def _assert_successful_upsert_counts(
    outcome: ConnectorRunOutcome,
    expected_counts: dict[str, int],
) -> None:
    """Assert a successful run returned the selected source-row upsert counts."""
    assert outcome.run is not None and outcome.run.status == "SUCCEEDED"
    assert {key: outcome.counts[key] for key in _UPSERT_COUNT_KEYS} == expected_counts


def _adsense_payload_with_estimated_earnings(
    payload: dict[str, object],
    *,
    value: str,
) -> dict[str, object]:
    """Return a payload copy with the ESTIMATED_EARNINGS cell changed."""
    mutated_payload = deepcopy(payload)
    rows = mutated_payload["rows"]
    assert isinstance(rows, list)
    first_row = rows[0]
    assert isinstance(first_row, dict)
    cells = first_row["cells"]
    assert isinstance(cells, list)
    earnings_cell = cells[1]
    assert isinstance(earnings_cell, dict)
    earnings_cell["value"] = value
    return mutated_payload


def _assert_persisted_connector_run_counts(
    session: Session,
    expected_counts: list[dict[str, int]],
) -> None:
    """Assert persisted connector_runs.counts_json carries the run count split."""
    persisted = session.scalars(
        select(ConnectorRunORM)
        .where(ConnectorRunORM.tenant_id == TENANT_ID)
        .order_by(ConnectorRunORM.started_at, ConnectorRunORM.id)
    ).all()
    assert [
        {key: run.counts_json[key] for key in _UPSERT_COUNT_KEYS} for run in persisted
    ] == expected_counts


def test_run_one_with_adsense_management_succeeds_for_account_scoped_run(
    session: Session, _stub_secret_resolver
) -> None:
    """Drive run_one end-to-end with the adsense-management connector.

    AdSenseManagementClient.fetch_monthly_report is patched at orchestrator
    module scope to return a parser-ready payload (no network). AdSense is
    account-scoped, so exactly one report is produced per run regardless of
    channels.

    Asserts:
    - outcome.run.status == "SUCCEEDED"
    - outcome.counts["reports_attempted"] == 1
    - outcome.counts["reports_succeeded"] == 1
    - outcome.counts["reports_failed"] == 0
    - Exactly one RawReportFileORM row exists with source == "adsense_management"
      and parse_status == "PARSED"
    - GoogleRevenueSourceRowORM rows are present in tenant scope, all carry
      source_system == "adsense_management", and youtube_channel_id is None
      (AdSense reports are account-scoped, not channel-scoped).
    - Audit lifecycle is STARTED -> DOWNLOADED -> PARSED -> FINISHED.
    """
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=_ADSENSE_CONNECTOR_KEY,
        account_id=_ADSENSE_ACCOUNT_ID,
    )
    report_month = "2026-05"
    payload = _make_adsense_parser_payload(
        account_id=_ADSENSE_ACCOUNT_ID,
        report_month=report_month,
    )

    def fake_fetch_monthly_report(*, account_id: str, report_month: str) -> dict:
        """Return the parser-ready stub payload; assert the (account, month) slice."""
        assert account_id == _ADSENSE_ACCOUNT_ID
        assert report_month == "2026-05"
        return payload

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.AdSenseManagementClient"
        ) as adsense_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend") as local_cls,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls,
    ):
        http_cls.return_value.close.return_value = None
        refresh.return_value = None

        adsense_client = adsense_cls.return_value
        adsense_client.fetch_monthly_report.side_effect = fake_fetch_monthly_report

        backend = local_cls.return_value
        store: dict[str, bytes] = {}

        def fake_upload(*, storage_uri, content):
            """Stash uploaded bytes in the in-memory blob store."""
            store[storage_uri] = content

        def fake_get(*, storage_uri):
            """Read bytes back from the in-memory blob store."""
            return store[storage_uri]

        backend.upload.side_effect = fake_upload
        backend.get_bytes.side_effect = fake_get

        outcome = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=_ADSENSE_CONNECTOR_KEY,
            account_id=_ADSENSE_ACCOUNT_ID,
            report_month=report_month,
        )

    # ----- outcome shape -----
    assert isinstance(outcome, ConnectorRunOutcome)
    assert outcome.run is not None
    assert outcome.run.status == "SUCCEEDED"
    assert outcome.per_report_failures == []

    # ----- counts: AdSense yields exactly one report per (account, month) -----
    counts = outcome.counts
    assert counts["reports_attempted"] == 1
    assert counts["reports_succeeded"] == 1
    assert counts["reports_failed"] == 0
    # The fixture row carries both ESTIMATED_EARNINGS and TOTAL_EARNINGS metrics,
    # so the parser emits two ParsedSourceRow rows from the same input cells.
    assert counts["rows_upserted_total"] == 2

    # ----- durable side effects: raw_report_files -----
    raw_files = session.scalars(
        select(RawReportFileORM).where(RawReportFileORM.tenant_id == TENANT_ID)
    ).all()
    assert len(raw_files) == 1
    raw_file = raw_files[0]
    assert raw_file.parse_status == "PARSED"
    assert raw_file.source == "adsense_management"
    assert raw_file.report_month == report_month

    # The runner serialises the parser_payload as JSON; the blob bytes the
    # backend received should decode back into the same payload dict.
    stored_bytes = store[raw_file.file_url]
    assert json.loads(stored_bytes.decode("utf-8")) == payload

    # ----- durable side effects: connector_run_raw_files join rows -----
    links = session.scalars(
        select(ConnectorRunRawFileORM).where(ConnectorRunRawFileORM.tenant_id == TENANT_ID)
    ).all()
    assert len(links) == 1

    # ----- durable side effects: google_revenue_source_rows -----
    source_rows = session.scalars(
        select(GoogleRevenueSourceRowORM).where(GoogleRevenueSourceRowORM.tenant_id == TENANT_ID)
    ).all()
    assert len(source_rows) == 2
    assert all(r.source_system == "adsense_management" for r in source_rows)
    # AdSense is account-scoped; the parser leaves youtube_channel_id NULL.
    assert all(r.youtube_channel_id is None for r in source_rows)
    assert all(r.source_account_id == _ADSENSE_ACCOUNT_ID for r in source_rows)
    assert {r.metric_key for r in source_rows} == {
        "ESTIMATED_EARNINGS",
        "TOTAL_EARNINGS",
    }
    assert {r.report_type for r in source_rows} == {"earnings_report"}
    assert {r.value_kind for r in source_rows} == {"estimated"}

    # ----- audit lifecycle -----
    events = _connector_audit_events(session)
    assert _audit_lifecycles(events) == [
        "STARTED",
        "DOWNLOADED",
        "PARSED",
        "FINISHED",
    ]


def test_run_one_with_adsense_management_empty_success_replaces_existing_rows(
    session: Session, _stub_secret_resolver
) -> None:
    """An empty AdSense replacement must delete both account-scoped metric types."""
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=_ADSENSE_CONNECTOR_KEY,
        account_id=_ADSENSE_ACCOUNT_ID,
    )
    report_month = "2026-05"
    payload_with_rows = _make_adsense_parser_payload(
        account_id=_ADSENSE_ACCOUNT_ID,
        report_month=report_month,
    )
    payload_empty = {**payload_with_rows, "rows": []}

    first_outcome = _run_adsense_orchestrator_with_payload(
        session,
        account_id=_ADSENSE_ACCOUNT_ID,
        report_month=report_month,
        payload=payload_with_rows,
    )
    assert first_outcome.run is not None
    assert first_outcome.run.status == "SUCCEEDED"
    assert first_outcome.counts["rows_upserted_total"] == 2

    initial_rows = session.scalars(
        select(GoogleRevenueSourceRowORM).where(
            GoogleRevenueSourceRowORM.tenant_id == TENANT_ID,
            GoogleRevenueSourceRowORM.source_system == "adsense_management",
            GoogleRevenueSourceRowORM.report_month == report_month,
            GoogleRevenueSourceRowORM.source_account_id == _ADSENSE_ACCOUNT_ID,
        )
    ).all()
    assert {row.metric_key for row in initial_rows} == {
        "ESTIMATED_EARNINGS",
        "TOTAL_EARNINGS",
    }
    assert {row.report_type for row in initial_rows} == {"earnings_report"}

    second_outcome = _run_adsense_orchestrator_with_payload(
        session,
        account_id=_ADSENSE_ACCOUNT_ID,
        report_month=report_month,
        payload=payload_empty,
    )
    assert second_outcome.run is not None
    assert second_outcome.run.status == "SUCCEEDED"
    assert second_outcome.counts["reports_attempted"] == 1
    assert second_outcome.counts["reports_succeeded"] == 1
    assert second_outcome.counts["rows_upserted_total"] == 0
    assert second_outcome.counts["rows_deleted_stale"] == 2

    final_rows = session.scalars(
        select(GoogleRevenueSourceRowORM).where(
            GoogleRevenueSourceRowORM.tenant_id == TENANT_ID,
            GoogleRevenueSourceRowORM.source_system == "adsense_management",
            GoogleRevenueSourceRowORM.report_month == report_month,
            GoogleRevenueSourceRowORM.source_account_id == _ADSENSE_ACCOUNT_ID,
        )
    ).all()
    assert final_rows == []


def test_run_one_with_adsense_management_nonempty_success_deletes_missing_scope(
    session: Session, _stub_secret_resolver
) -> None:
    """A nonempty AdSense replacement must delete metric types it no longer emits."""
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=_ADSENSE_CONNECTOR_KEY,
        account_id=_ADSENSE_ACCOUNT_ID,
    )
    report_month = "2026-05"
    payload_payment = _make_adsense_payment_parser_payload(
        account_id=_ADSENSE_ACCOUNT_ID,
        report_month=report_month,
    )
    payload_earnings = _make_adsense_parser_payload(
        account_id=_ADSENSE_ACCOUNT_ID,
        report_month=report_month,
    )

    first_outcome = _run_adsense_orchestrator_with_payload(
        session,
        account_id=_ADSENSE_ACCOUNT_ID,
        report_month=report_month,
        payload=payload_payment,
    )
    assert first_outcome.run is not None
    assert first_outcome.run.status == "SUCCEEDED"
    assert first_outcome.counts["rows_upserted_total"] == 1

    initial_rows = session.scalars(
        select(GoogleRevenueSourceRowORM).where(
            GoogleRevenueSourceRowORM.tenant_id == TENANT_ID,
            GoogleRevenueSourceRowORM.source_system == "adsense_management",
            GoogleRevenueSourceRowORM.report_month == report_month,
            GoogleRevenueSourceRowORM.source_account_id == _ADSENSE_ACCOUNT_ID,
        )
    ).all()
    assert {row.report_type for row in initial_rows} == {"payment_report"}
    assert {row.metric_key for row in initial_rows} == {"PAID_AMOUNT"}

    second_outcome = _run_adsense_orchestrator_with_payload(
        session,
        account_id=_ADSENSE_ACCOUNT_ID,
        report_month=report_month,
        payload=payload_earnings,
    )
    assert second_outcome.run is not None
    assert second_outcome.run.status == "SUCCEEDED"
    assert second_outcome.counts["rows_upserted_total"] == 2

    final_rows = session.scalars(
        select(GoogleRevenueSourceRowORM).where(
            GoogleRevenueSourceRowORM.tenant_id == TENANT_ID,
            GoogleRevenueSourceRowORM.source_system == "adsense_management",
            GoogleRevenueSourceRowORM.report_month == report_month,
            GoogleRevenueSourceRowORM.source_account_id == _ADSENSE_ACCOUNT_ID,
        )
    ).all()
    assert {row.report_type for row in final_rows} == {"earnings_report"}
    assert {row.metric_key for row in final_rows} == {
        "ESTIMATED_EARNINGS",
        "TOTAL_EARNINGS",
    }


def test_run_one_with_adsense_management_full_resource_cleanup_uses_parser_scope(
    session: Session, _stub_secret_resolver
) -> None:
    """AdSense stale cleanup must use the parser-normalized account scope."""
    account_selector = f"accounts/{_ADSENSE_ACCOUNT_ID}"
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=_ADSENSE_CONNECTOR_KEY,
        account_id=account_selector,
    )
    report_month = "2026-05"
    payload_payment = _make_adsense_payment_parser_payload(
        account_id=_ADSENSE_ACCOUNT_ID,
        report_month=report_month,
    )
    payload_earnings = _make_adsense_parser_payload(
        account_id=_ADSENSE_ACCOUNT_ID,
        report_month=report_month,
    )

    first_outcome = _run_adsense_orchestrator_with_payload(
        session,
        account_id=account_selector,
        report_month=report_month,
        payload=payload_payment,
    )
    assert first_outcome.run is not None
    assert first_outcome.run.status == "SUCCEEDED"
    assert first_outcome.counts["rows_upserted_total"] == 1

    second_outcome = _run_adsense_orchestrator_with_payload(
        session,
        account_id=account_selector,
        report_month=report_month,
        payload=payload_earnings,
    )
    assert second_outcome.run is not None
    assert second_outcome.run.status == "SUCCEEDED"
    assert second_outcome.counts["rows_upserted_total"] == 2

    final_rows = session.scalars(
        select(GoogleRevenueSourceRowORM).where(
            GoogleRevenueSourceRowORM.tenant_id == TENANT_ID,
            GoogleRevenueSourceRowORM.source_system == "adsense_management",
            GoogleRevenueSourceRowORM.report_month == report_month,
        )
    ).all()
    assert {row.source_account_id for row in final_rows} == {_ADSENSE_ACCOUNT_ID}
    assert {row.report_type for row in final_rows} == {"earnings_report"}
    assert {row.metric_key for row in final_rows} == {
        "ESTIMATED_EARNINGS",
        "TOTAL_EARNINGS",
    }


@pytest.mark.usefixtures("_stub_secret_resolver")
def test_run_one_per_category_row_counts_plumb_to_connector_run_counts_json(
    session: Session,
) -> None:
    """rows_upserted_created/updated/unchanged reach connector_runs.counts_json.

    Drives three consecutive AdSense runs against the same (account, month)
    and asserts that every classification branch — fresh-insert (CREATED),
    identical rerun (UNCHANGED), and value-change rerun (UPDATED, mixed with
    UNCHANGED) — flows from the repository through ``_process_one_report``
    into the run-level ``counts`` dict written to ``connector_runs.counts_json``
    by ``finish_run``. Sum invariant: created + updated + unchanged == total.
    """
    _make_credential_row(
        session,
        tenant_id=TENANT_ID,
        connector_key=_ADSENSE_CONNECTOR_KEY,
        account_id=_ADSENSE_ACCOUNT_ID,
    )
    report_month = "2026-05"
    base_payload = _make_adsense_parser_payload(
        account_id=_ADSENSE_ACCOUNT_ID,
        report_month=report_month,
    )

    # Run 1: both AdSense source rows are new → CREATED=2.
    first = _run_adsense_orchestrator_with_payload(
        session,
        account_id=_ADSENSE_ACCOUNT_ID,
        report_month=report_month,
        payload=base_payload,
    )
    _assert_successful_upsert_counts(
        first,
        {
            "rows_upserted_total": 2,
            "rows_upserted_created": 2,
            "rows_upserted_updated": 0,
            "rows_upserted_unchanged": 0,
        },
    )

    # Run 2: identical payload → both rows UNCHANGED.
    second = _run_adsense_orchestrator_with_payload(
        session,
        account_id=_ADSENSE_ACCOUNT_ID,
        report_month=report_month,
        payload=base_payload,
    )
    _assert_successful_upsert_counts(
        second,
        {
            "rows_upserted_total": 2,
            "rows_upserted_created": 0,
            "rows_upserted_updated": 0,
            "rows_upserted_unchanged": 2,
        },
    )

    # Run 3: only ESTIMATED_EARNINGS amount changes → 1 UPDATED + 1 UNCHANGED
    # (the TOTAL_EARNINGS row's cells are identical, so its content matches).
    mutated_payload = _adsense_payload_with_estimated_earnings(
        base_payload,
        value="200.000000",
    )
    third = _run_adsense_orchestrator_with_payload(
        session,
        account_id=_ADSENSE_ACCOUNT_ID,
        report_month=report_month,
        payload=mutated_payload,
    )
    _assert_successful_upsert_counts(
        third,
        {
            "rows_upserted_total": 2,
            "rows_upserted_created": 0,
            "rows_upserted_updated": 1,
            "rows_upserted_unchanged": 1,
        },
    )

    # Defence in depth: read the persisted counts_json directly to confirm
    # the run-level write picked up the per-category split (not just the
    # finish_run return value).
    _assert_persisted_connector_run_counts(
        session,
        [
            {
                "rows_upserted_total": 2,
                "rows_upserted_created": 2,
                "rows_upserted_updated": 0,
                "rows_upserted_unchanged": 0,
            },
            {
                "rows_upserted_total": 2,
                "rows_upserted_created": 0,
                "rows_upserted_updated": 0,
                "rows_upserted_unchanged": 2,
            },
            {
                "rows_upserted_total": 2,
                "rows_upserted_created": 0,
                "rows_upserted_updated": 1,
                "rows_upserted_unchanged": 1,
            },
        ],
    )
