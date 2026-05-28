"""Mock end-to-end ingestion gate (spec §9.3 B2.6, plan Task 39).

Three mock Google backends -- YouTube Reporting, YouTube Analytics, and
AdSense Management -- are wired into ``run_one`` and the C1 normalizer in
one test so the full ingest -> normalize -> audit chain is exercised
together. This test is the heaviest single assertion bundle in B2.6: it
exists to prove the slice fits before B2 closes out.

Patch surface (Option A, mirrors T37/T38): all three connector clients,
``LocalFileStoreBackend``, ``refresh_credentials``, and ``GoogleHttpClient``
are patched at orchestrator module scope so no live HTTP / OAuth / disk
traffic fires. The plan's Option B (httpx.MockTransport + real local-secret
resolver + real LocalFileStoreBackend) would require deeper inspection of
the wire-level shape; the existing fixtures are parser-ready, not raw API
responses, so a full transport mock would force re-deriving the YouTube
Reporting CSV bytes / YouTube Analytics JSON columnHeaders / AdSense JSON
shape from scratch. T39's value is the audit + source-row + C1 chain --
file-store correctness lives in B2.4 and OAuth correctness lives in B2.4
tests, so mirroring the T37/T38 patch pattern keeps the test focused.

Assertions:
1. All three connectors populate ``google_revenue_source_rows`` with the
   expected ``source_system`` discriminator.
2. C1 ``GoogleSourceNormalizer.normalize_month`` produces
   ``MonthlyChannelRevenueFactORM`` entries for the YouTube Reporting +
   YouTube Analytics rows.
3. AdSense rows are skipped in C1 as ``SkipReason.MISSING_CHANNEL_ID``
   (AdSense is account-scoped; channel allocation -> facts lives in a
   future spec).
4. Audit log carries the full STARTED -> DOWNLOADED -> PARSED -> FINISHED
   sequence for each of the three runs, with the connector service
   principal recorded on every row.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
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
from ums_smart_revenue.connectors.runs.orchestrator import run_one
from ums_smart_revenue.db.connector_models import (  # noqa: F401  (table registration)
    ConnectorRunORM,
    ConnectorRunRawFileORM,
)
from ums_smart_revenue.db.finance_models import (
    FinanceBase,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import YouTubeChannelORM
from ums_smart_revenue.db.report_models import ReportBase
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
from ums_smart_revenue.finance.google_source_normalizer import (
    GoogleSourceNormalizer,
    SkipReason,
)

# Tenant + connector + actor constants. ``TENANT_ID`` and the service-actor
# UUID below are file-local so this test does not import from
# tests/connectors/google/test_orchestrator.py (which would couple two large
# test files).
TENANT_ID = UUID("00000000-0000-0000-0000-000000839001")
YT_REPORTING_KEY = "youtube-reporting"
YT_ANALYTICS_KEY = "youtube-analytics"
ADSENSE_KEY = "adsense-management"
YT_REPORTING_ACCOUNT = "cms-1"
YT_ANALYTICS_ACCOUNT = "cms-1"
ADSENSE_ACCOUNT = "pub-1"
REPORT_MONTH = "2026-04"
SERVICE_ACTOR_ID = "ddddeeee-ffff-0000-1111-222222222222"
ACTOR_USER_ID = "00000000-0000-0000-0000-000000839900"
RESOLVER_REF = "local-secret://yt-creds"


@pytest.fixture(autouse=True)
def _service_actor_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Set ``UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID`` for the live-path run_one.

    Mirrors the autouse fixture in ``tests/connectors/google/test_orchestrator.py``
    so the orchestrator's Bucket A fail-closed check (the env must be a valid
    UUID before connector audit emitters can build a service principal) does
    not block the test. ``load_app_settings.cache_clear()`` is called so a
    stale cached settings object from an earlier test cannot poison the
    actor lookup.
    """
    from ums_smart_revenue.config.settings import (
        GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV,
        load_app_settings,
    )

    monkeypatch.setenv(GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV, SERVICE_ACTOR_ID)
    load_app_settings.cache_clear()
    try:
        yield SERVICE_ACTOR_ID
    finally:
        load_app_settings.cache_clear()


@pytest.fixture(name="session")
def _session_fixture() -> Session:
    """In-memory SQLite spanning every base the three connectors touch.

    ``FinanceBase.metadata`` is shared with ``OrgBase.metadata`` (see
    backend/ums_smart_revenue/db/finance_models.py), so the single
    ``FinanceBase.metadata.create_all`` covers ``youtube_channels``,
    ``finance_month_close``, ``monthly_channel_revenue_facts``, and the
    ``google_revenue_source_rows`` upsert target. ``ReportBase`` brings
    ``connector_runs`` + ``raw_report_files``; ``SecurityBase`` brings
    ``api_connector_credentials`` + ``users`` + ``audit_logs``;
    ``TenantBase`` carries the tenant FK target.
    """
    engine = create_engine("sqlite+pysqlite:///:memory:")
    SecurityBase.metadata.create_all(engine)
    TenantBase.metadata.create_all(engine)
    ReportBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        now = datetime.now(UTC)
        session.add_all(
            [
                TenantORM(
                    id=TENANT_ID,
                    slug="tenant-ingestion-gate",
                    display_name="Ingestion Gate Tenant",
                ),
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


@pytest.fixture
def _stub_secret_resolver():
    """Register a ``local-secret://yt-creds`` resolver for all three runs.

    The same resolver ref is shared across the three credential rows because
    the test patches ``refresh_credentials`` -- the OAuth payload never has
    to be exchanged for a real token. The mapping holds a JSON-encoded shape
    that ``build_credentials_from_payload`` accepts so the orchestrator's
    secret-resolve step succeeds before the patched refresh short-circuits
    the network round-trip.
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
    connector_key: str,
    account_id: str,
) -> ApiConnectorCredentialORM:
    """Seed one ``api_connector_credentials`` row for the given (key, account).

    Each credential row needs a parent ``UserORM`` to satisfy the
    ``created_by`` / ``updated_by`` FKs the SecurityBase enforces. Reuses
    the same resolver ref ``local-secret://yt-creds`` across all three
    connectors since the patched ``refresh_credentials`` never touches the
    secret payload in this test.
    """
    actor_id = uuid4()
    session.add(
        UserORM(
            id=actor_id,
            tenant_id=TENANT_ID,
            email=f"gate-{actor_id}@example.com",
            display_name="Ingestion Gate Actor",
        )
    )
    session.flush()
    row = ApiConnectorCredentialORM(
        id=uuid4(),
        tenant_id=TENANT_ID,
        connector_key=connector_key,
        account_id=account_id,
        encrypted_secret_ref=RESOLVER_REF,
        status="active",
        created_by=actor_id,
        updated_by=actor_id,
    )
    session.add(row)
    session.flush()
    return row


def _make_youtube_channel(
    session: Session,
    *,
    youtube_channel_id: str,
    content_owner_id: str,
) -> YouTubeChannelORM:
    """Seed one CMS-owned active channel.

    The YT Analytics runner's ``list_target_channels`` filter requires
    ``active is True``, ``revenue_required is True``, ``cms_status =
    INSIDE_CMS``, and the channel's ``content_owner_id`` to match the
    run's ``account_id``. C1's UNKNOWN_CHANNEL gate also requires
    ``active is True``.
    """
    channel = YouTubeChannelORM(
        id=uuid4(),
        tenant_id=TENANT_ID,
        youtube_channel_id=youtube_channel_id,
        channel_name=f"Channel {youtube_channel_id}",
        content_owner_id=content_owner_id,
        active=True,
        revenue_required=True,
        cms_status="INSIDE_CMS",
    )
    session.add(channel)
    session.flush()
    return channel


def _yt_reporting_csv_bytes(*, channel_id: str, account_id: str) -> bytes:
    """One-row YouTube Reporting CSV the runner will adapt + persist.

    Uses the documented Google column names plus an explicit ``currencyCode``
    so the CSV adapter does not have to default. The single ``2026-04-15``
    date keeps the row inside ``2026-04`` so the runner's calendar-month
    bucketing accepts it.
    """
    header = b"date,channel,content_owner,estimatedRevenue,currencyCode\n"
    row = f"2026-04-15,{channel_id},{account_id},123.450000,USD\n".encode()
    return header + row


def _yt_analytics_payload(*, channel_id: str, account_id: str) -> dict[str, object]:
    """Parser-ready YouTube Analytics payload shaped like the wire response.

    Mirrors the shape returned by ``YouTubeAnalyticsClient.fetch_channel_report``
    BEFORE the runner's ``_synthesise_analytics_channel_dimension`` injects
    the ``channel`` dimension. ``dimensions=month`` matches the locked
    B2.5 single-channel content-owner query so the parser produces one row
    per metric in ``_ANALYTICS_METRICS``.
    """
    from ums_smart_revenue.connectors.google.youtube_analytics_client import (
        _DIMENSIONS as _ANALYTICS_DIMENSIONS,
    )
    from ums_smart_revenue.connectors.google.youtube_analytics_client import (
        _METRICS as _ANALYTICS_METRICS,
    )

    year, month = REPORT_MONTH.split("-")
    metric_names = _ANALYTICS_METRICS.split(",")
    dimension_names = _ANALYTICS_DIMENSIONS.split(",")
    metric_values = {
        "estimatedRevenue": 12.5,
        "estimatedAdRevenue": 8.0,
        "grossRevenue": 20.5,
    }
    dimension_cells = {"month": f"{year}-{month}"}
    return {
        "query_request": {
            "ids": f"contentOwner=={account_id}",
            "filters": f"channel=={channel_id}",
            "startDate": f"{year}-{month}-01",
            "endDate": f"{year}-{month}-01",
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
            ]
        ],
    }


def _adsense_payload(*, account_id: str) -> dict[str, object]:
    """Parser-ready AdSense payload with ESTIMATED_EARNINGS + TOTAL_EARNINGS.

    AdSense is account-scoped, so the runner yields exactly one report per
    run. One MONTH dimension + both canonical-metric columns produces two
    ParsedSourceRow entries per input row (the parser splits per-metric:
    both metrics become earnings_report/estimated rows).
    """
    from calendar import monthrange as _monthrange

    year_s, month_s = REPORT_MONTH.split("-")
    year_i, month_i = int(year_s), int(month_s)
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
                    {"value": "789.120000"},
                    {"value": "67.890000"},
                ],
            },
        ],
        "report_id": f"deterministic-stub-{account_id}-{REPORT_MONTH}",
    }


def _connector_audit_events(session: Session) -> list[AuditLogORM]:
    """Return audit rows the connector lifecycle emitters write, in order.

    The orchestrator's lifecycle emitters set ``event_type`` to
    ``CONNECTOR_JOB_RUN`` (run-level: STARTED, FINISHED) or
    ``REPORT_IMPORTED`` (raw-file-level: DOWNLOADED, PARSED, FAILED).
    Sorting by ``created_at`` then by ``id`` preserves the lifecycle
    ordering across the three sequential runs.
    """
    return list(
        session.scalars(
            select(AuditLogORM)
            .where(AuditLogORM.tenant_id == TENANT_ID)
            .where(
                AuditLogORM.event_type.in_(
                    ["CONNECTOR_JOB_RUN", "REPORT_IMPORTED"]
                )
            )
            .order_by(AuditLogORM.created_at, AuditLogORM.id)
        )
    )


def _group_events_by_run(
    events: list[AuditLogORM],
) -> dict[str, list[AuditLogORM]]:
    """Group audit events by ``details["run_id"]`` preserving insertion order."""
    grouped: dict[str, list[AuditLogORM]] = {}
    for event in events:
        run_id = event.details["run_id"]
        grouped.setdefault(run_id, []).append(event)
    return grouped


# ============================================================================
# Purpose: Mock end-to-end ingestion gate -- three connectors run via run_one,
#          source rows persist, C1 normalizes YT rows to facts and skips
#          AdSense as MISSING_CHANNEL_ID, audit log carries 12 lifecycle
#          events under one service principal.
# Database/ORM: ApiConnectorCredentialORM, YouTubeChannelORM, ConnectorRunORM,
#               ConnectorRunRawFileORM, RawReportFileORM,
#               GoogleRevenueSourceRowORM, MonthlyChannelRevenueFactORM,
#               AuditLogORM.
# Standards: Each mock client patched at orchestrator module scope; no live
#            HTTP, OAuth, or disk traffic. Service principal env honored via
#            autouse fixture so Bucket A fail-closed does not trigger.
# Blast Radius: Audit lifecycle, source-row repository, C1 fact creation, and
#               the connector dispatcher all participate -- any regression
#               in B2.4-B2.6 surfaces here.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py -> run_one.
#   - File: backend/ums_smart_revenue/finance/google_source_normalizer.py -> C1.
#   - File: tests/connectors/google/test_orchestrator.py -> mirrors T37/T38
#     patch surface so this test stays consistent with the per-connector tests.
# ============================================================================
def test_three_connectors_end_to_end_on_mocks(
    session: Session, _stub_secret_resolver
) -> None:
    """Mock end-to-end ingestion gate -- B2.6 §9.3 closing assertion."""
    # ----- seed credentials for all three connectors -----
    _make_credential_row(
        session, connector_key=YT_REPORTING_KEY, account_id=YT_REPORTING_ACCOUNT
    )
    _make_credential_row(
        session, connector_key=YT_ANALYTICS_KEY, account_id=YT_ANALYTICS_ACCOUNT
    )
    _make_credential_row(
        session, connector_key=ADSENSE_KEY, account_id=ADSENSE_ACCOUNT
    )

    # ----- seed one CMS-owned channel matching the YT runs -----
    # YT Reporting writes source rows keyed on the CSV's ``channel`` column;
    # YT Analytics's list_target_channels picks this channel as the only
    # target. The same channel powers C1's per-channel fact creation.
    channel_id = "UC_e2e_1"
    _make_youtube_channel(
        session,
        youtube_channel_id=channel_id,
        content_owner_id=YT_ANALYTICS_ACCOUNT,
    )

    # ----- build the per-runner mock payloads -----
    yt_reporting_csv = _yt_reporting_csv_bytes(
        channel_id=channel_id, account_id=YT_REPORTING_ACCOUNT
    )
    yt_analytics_payload = _yt_analytics_payload(
        channel_id=channel_id, account_id=YT_ANALYTICS_ACCOUNT
    )
    adsense_payload = _adsense_payload(account_id=ADSENSE_ACCOUNT)

    # ----- run YT Reporting -----
    # Patched at orchestrator module scope so the runner instantiates the
    # mock instead of the live client; ``LocalFileStoreBackend`` is replaced
    # with an in-memory dict so no disk I/O fires.
    with patch(
        "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
    ) as yt_rep_cls, patch(
        "ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend"
    ) as local_cls_rep, patch(
        "ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials"
    ) as refresh_rep, patch(
        "ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient"
    ) as http_cls_rep:
        http_cls_rep.return_value.close.return_value = None
        refresh_rep.return_value = None

        rep_client = yt_rep_cls.return_value
        rep_client.list_supported_jobs.return_value = [
            {"id": "job-1", "reportTypeId": "channel_basic_a2"}
        ]
        rep_client.list_reports_for_month.return_value = [
            {"id": "r1", "downloadUrl": "https://yt/r1"}
        ]
        rep_client.fetch_report.return_value = yt_reporting_csv

        store_rep: dict[str, bytes] = {}
        backend_rep = local_cls_rep.return_value
        backend_rep.upload.side_effect = (
            lambda *, storage_uri, content: store_rep.__setitem__(
                storage_uri, content
            )
        )
        backend_rep.get_bytes.side_effect = lambda *, storage_uri: store_rep[
            storage_uri
        ]

        outcome_reporting = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=YT_REPORTING_KEY,
            account_id=YT_REPORTING_ACCOUNT,
            report_month=REPORT_MONTH,
        )

    assert outcome_reporting.run is not None
    assert outcome_reporting.run.status == "SUCCEEDED"

    # ----- run YT Analytics -----
    with patch(
        "ums_smart_revenue.connectors.runs.orchestrator.YouTubeAnalyticsClient"
    ) as yt_an_cls, patch(
        "ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend"
    ) as local_cls_an, patch(
        "ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials"
    ) as refresh_an, patch(
        "ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient"
    ) as http_cls_an:
        http_cls_an.return_value.close.return_value = None
        refresh_an.return_value = None

        an_client = yt_an_cls.return_value
        an_client.fetch_channel_report.return_value = yt_analytics_payload

        store_an: dict[str, bytes] = {}
        backend_an = local_cls_an.return_value
        backend_an.upload.side_effect = (
            lambda *, storage_uri, content: store_an.__setitem__(
                storage_uri, content
            )
        )
        backend_an.get_bytes.side_effect = lambda *, storage_uri: store_an[
            storage_uri
        ]

        outcome_analytics = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=YT_ANALYTICS_KEY,
            account_id=YT_ANALYTICS_ACCOUNT,
            report_month=REPORT_MONTH,
        )

    assert outcome_analytics.run is not None
    assert outcome_analytics.run.status == "SUCCEEDED"

    # ----- run AdSense -----
    with patch(
        "ums_smart_revenue.connectors.runs.orchestrator.AdSenseManagementClient"
    ) as adsense_cls, patch(
        "ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend"
    ) as local_cls_ads, patch(
        "ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials"
    ) as refresh_ads, patch(
        "ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient"
    ) as http_cls_ads:
        http_cls_ads.return_value.close.return_value = None
        refresh_ads.return_value = None

        ads_client = adsense_cls.return_value
        ads_client.fetch_monthly_report.return_value = adsense_payload

        store_ads: dict[str, bytes] = {}
        backend_ads = local_cls_ads.return_value
        backend_ads.upload.side_effect = (
            lambda *, storage_uri, content: store_ads.__setitem__(
                storage_uri, content
            )
        )
        backend_ads.get_bytes.side_effect = lambda *, storage_uri: store_ads[
            storage_uri
        ]

        outcome_adsense = run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=ADSENSE_KEY,
            account_id=ADSENSE_ACCOUNT,
            report_month=REPORT_MONTH,
        )

    assert outcome_adsense.run is not None
    assert outcome_adsense.run.status == "SUCCEEDED"

    # ============================================================================
    # Assertion 1: all three source-systems are present in google_revenue_source_rows.
    # ============================================================================
    source_rows = list(
        session.scalars(
            select(GoogleRevenueSourceRowORM).where(
                GoogleRevenueSourceRowORM.tenant_id == TENANT_ID
            )
        )
    )
    source_systems = {row.source_system for row in source_rows}
    assert source_systems == {
        "youtube_reporting",
        "youtube_analytics",
        "adsense_management",
    }, (
        "expected source rows from all three mock connectors; "
        f"got source_systems={source_systems!r}"
    )
    # FIX: strict row-count pin so a double-write regression (e.g., upsert ON
    # CONFLICT regression, or a per-report dedup skip that stops firing) is
    # caught by this gate -- set-equality alone would silently absorb
    # duplicates. Expected = 1 (YT Reporting CSV: 1 row x 1 channel)
    # + 3 (YT Analytics: 1 data row x 3 monetary metrics in _MONETARY_METRICS
    # -- estimatedRevenue, estimatedAdRevenue, grossRevenue)
    # + 2 (AdSense: 1 input row x 2 METRIC_CURRENCY columns --
    # ESTIMATED_EARNINGS, TOTAL_EARNINGS) = 6.
    assert len(source_rows) == 6, (
        f"expected exactly 6 source rows across the three connectors "
        f"(YT Reporting=1, YT Analytics=3, AdSense=2); got {len(source_rows)}"
    )

    # AdSense rows must carry NULL youtube_channel_id (account-scoped).
    adsense_source_rows = [
        r for r in source_rows if r.source_system == "adsense_management"
    ]
    assert adsense_source_rows, "AdSense run should have produced source rows"
    assert all(r.youtube_channel_id is None for r in adsense_source_rows), (
        "AdSense source rows must be channel-less; channel allocation is a "
        "future spec"
    )

    # ============================================================================
    # Assertion 2 + 3: C1 normalize_month produces YT facts and skips AdSense.
    # ============================================================================
    result = GoogleSourceNormalizer(session, tenant_id=TENANT_ID).normalize_month(
        month=REPORT_MONTH, actor_user_id=ACTOR_USER_ID,
    )
    facts = list(
        session.scalars(
            select(MonthlyChannelRevenueFactORM).where(
                MonthlyChannelRevenueFactORM.tenant_id == TENANT_ID
            )
        )
    )
    fact_source_kinds = {fact.source_kind for fact in facts}
    # YT Reporting + YT Analytics each emit one (channel, source_kind) bucket
    # so two facts are expected: one ``YOUTUBE_CMS`` and one ``YOUTUBE_ANALYTICS``.
    # source_kind values are persisted in the uppercase RevenueFactSourceKind
    # member form (see backend/ums_smart_revenue/finance/revenue_facts.py).
    assert fact_source_kinds == {"YOUTUBE_CMS", "YOUTUBE_ANALYTICS"}, (
        f"expected YT-only fact source_kinds; got {fact_source_kinds!r}"
    )
    # FIX: strict count pin so a per-(channel, source_kind) double-write
    # regression cannot pass under the set-equality check above. One CMS
    # channel x two YT source_kinds = exactly 2 facts; AdSense is skipped
    # as MISSING_CHANNEL_ID (asserted below) and emits no facts.
    assert len(facts) == 2, (
        f"expected exactly 2 MonthlyChannelRevenueFactORM rows from YT-only "
        f"(1 channel x {{YOUTUBE_CMS, YOUTUBE_ANALYTICS}}); got {len(facts)}"
    )
    assert {fact.youtube_channel_id for fact in facts} == {channel_id}
    # The C1 normalizer flags the AdSense rows as MISSING_CHANNEL_ID because
    # the AdSense parser leaves ``youtube_channel_id=None``.
    missing_channel_skips = [
        s for s in result.skipped if s.reason == SkipReason.MISSING_CHANNEL_ID
    ]
    assert missing_channel_skips, (
        "AdSense source rows should have been skipped as MISSING_CHANNEL_ID; "
        f"result.skipped={result.skipped!r}"
    )
    # SkippedSourceRow.source_row_id is a str; GoogleRevenueSourceRowORM.id
    # is a UUID. Compare on the string form so the test stays portable across
    # SQLAlchemy's UUID-as-object vs UUID-as-string dialect quirks.
    skipped_ids = {s.source_row_id for s in missing_channel_skips}
    expected_skipped_ids = {str(r.id) for r in adsense_source_rows}
    assert skipped_ids == expected_skipped_ids, (
        "every AdSense source row must appear in result.skipped with "
        "MISSING_CHANNEL_ID"
    )

    # ============================================================================
    # Assertion 4: audit log carries the full STARTED -> DOWNLOADED -> PARSED ->
    # FINISHED sequence for each run, with the connector service principal on
    # every row.
    # ============================================================================
    events = _connector_audit_events(session)
    assert len(events) == 12, (
        f"expected 12 audit events (4 per run x 3 runs); got {len(events)}: "
        f"{[(e.event_type, e.details.get('lifecycle')) for e in events]}"
    )
    grouped = _group_events_by_run(events)
    assert len(grouped) == 3, f"expected 3 distinct run_ids; got {len(grouped)}"
    for run_id, run_events in grouped.items():
        lifecycles = [event.details["lifecycle"] for event in run_events]
        assert lifecycles == ["STARTED", "DOWNLOADED", "PARSED", "FINISHED"], (
            f"run_id={run_id} lifecycle mismatch: expected STARTED -> "
            f"DOWNLOADED -> PARSED -> FINISHED, got {lifecycles!r}"
        )

    # Every audit row must carry the same connector service principal. The
    # SqlAlchemyAuditSink stores the raw actor UUID in
    # ``details['actor_user_id']`` when the UUID is not a real users.id, and
    # sets ``user_id=None`` on the row. Both the column and the details key
    # must agree across all 12 events: a single principal for all three runs.
    principal_user_ids = {event.user_id for event in events}
    assert principal_user_ids == {None}, (
        f"expected user_id=None on all rows (service principal is not a "
        f"users.id); got {principal_user_ids!r}"
    )
    actor_user_ids = {event.details.get("actor_user_id") for event in events}
    assert actor_user_ids == {SERVICE_ACTOR_ID}, (
        f"expected the connector service principal on all rows; got "
        f"{actor_user_ids!r}"
    )
