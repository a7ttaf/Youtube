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

    The post-run normalize stage also emits ``REPORT_IMPORTED`` rows for
    written facts; those carry ``entity_type="monthly_channel_revenue_fact"``
    and are filtered out here so this helper stays scoped to the
    connector-lifecycle rows. Assertion 4b checks the normalize-audit
    rows separately.
    """
    return list(
        session.scalars(
            select(AuditLogORM)
            .where(AuditLogORM.tenant_id == TENANT_ID)
            .where(AuditLogORM.event_type.in_(["CONNECTOR_JOB_RUN", "REPORT_IMPORTED"]))
            .where(AuditLogORM.entity_type != "monthly_channel_revenue_fact")
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


def _wire_mock_file_store(local_cls_mock) -> dict[str, bytes]:
    """Wire an in-memory dict behind a mocked ``LocalFileStoreBackend``.

    Returns the dict so callers can inspect uploads if needed. Each connector
    run block repeats this store wiring, so isolating it keeps the per-run
    blocks short and avoids three copies of the same ``side_effect`` lambdas.
    """
    store: dict[str, bytes] = {}
    backend = local_cls_mock.return_value
    backend.upload.side_effect = lambda *, storage_uri, content: store.__setitem__(
        storage_uri, content
    )
    backend.get_bytes.side_effect = lambda *, storage_uri: store[storage_uri]
    return store


def _load_source_rows(session: Session) -> list[GoogleRevenueSourceRowORM]:
    """Load all tenant source rows for the end-to-end gate's source-row assertion."""
    return list(
        session.scalars(
            select(GoogleRevenueSourceRowORM).where(
                GoogleRevenueSourceRowORM.tenant_id == TENANT_ID
            )
        )
    )


def _assert_source_rows_shape(
    source_rows: list[GoogleRevenueSourceRowORM],
) -> list[GoogleRevenueSourceRowORM]:
    """Assertion 1: all three source systems present, exact row count, channel-less AdSense."""
    source_systems = {row.source_system for row in source_rows}
    assert source_systems == {
        "youtube_reporting",
        "youtube_analytics",
        "adsense_management",
    }, f"expected source rows from all three mock connectors; got source_systems={source_systems!r}"
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
    adsense_source_rows = [r for r in source_rows if r.source_system == "adsense_management"]
    assert adsense_source_rows, "AdSense run should have produced source rows"
    assert all(r.youtube_channel_id is None for r in adsense_source_rows), (
        "AdSense source rows must be channel-less; channel allocation is a future spec"
    )
    return adsense_source_rows


def _assert_facts_and_skip_semantics(
    session: Session,
    channel_id: str,
    adsense_source_rows: list[GoogleRevenueSourceRowORM],
) -> None:
    """Assertions 2 + 3: YT facts projected, AdSense skipped as MISSING_CHANNEL_ID."""
    facts = list(
        session.scalars(
            select(MonthlyChannelRevenueFactORM).where(
                MonthlyChannelRevenueFactORM.tenant_id == TENANT_ID
            )
        )
    )
    fact_source_kinds = {fact.source_kind for fact in facts}
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

    # Direct normalizer-semantics coverage: the AdSense rows must classify as
    # MISSING_CHANNEL_ID (the wiring's NormalizationResult is internal to the
    # orchestrator, so re-run normalize_month explicitly to inspect skips). This
    # re-run is idempotent against the facts the wiring already wrote -- the two
    # YT facts come back UNCHANGED -- so it does not perturb the count above.
    result = GoogleSourceNormalizer(session, tenant_id=TENANT_ID).normalize_month(
        month=REPORT_MONTH,
        actor_user_id=ACTOR_USER_ID,
    )
    assert not result.created, (
        "re-running normalize after the wiring must not create new facts; "
        f"result.created={result.created!r}"
    )
    assert {fact.youtube_channel_id for fact in result.unchanged} == {channel_id}, (
        "the wiring-written YT facts must re-classify as UNCHANGED on re-run"
    )
    missing_channel_skips = [s for s in result.skipped if s.reason == SkipReason.MISSING_CHANNEL_ID]
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
        "every AdSense source row must appear in result.skipped with MISSING_CHANNEL_ID"
    )


def _assert_run_lifecycle_sequence(events: list[AuditLogORM]) -> None:
    """Assertion 4: full STARTED -> DOWNLOADED -> PARSED -> FINISHED sequence per run."""
    # The post-run normalize stage also emits ROWS_SKIPPED summary edges (same
    # CONNECTOR_JOB_RUN event_type) for rows dropped from the fact projection.
    # Assertion 4 only checks the run lifecycle sequence, so exclude them here
    # and assert them explicitly in _assert_skip_summary_edges.
    lifecycle_events = [e for e in events if e.details.get("lifecycle") != "ROWS_SKIPPED"]
    assert len(lifecycle_events) == 12, (
        f"expected 12 lifecycle audit events (4 per run x 3 runs); "
        f"got {len(lifecycle_events)}: "
        f"{[(e.event_type, e.details.get('lifecycle')) for e in lifecycle_events]}"
    )
    grouped = _group_events_by_run(lifecycle_events)
    assert len(grouped) == 3, f"expected 3 distinct run_ids; got {len(grouped)}"
    for run_id, run_events in grouped.items():
        lifecycles = [event.details["lifecycle"] for event in run_events]
        assert lifecycles == ["STARTED", "DOWNLOADED", "PARSED", "FINISHED"], (
            f"run_id={run_id} lifecycle mismatch: expected STARTED -> "
            f"DOWNLOADED -> PARSED -> FINISHED, got {lifecycles!r}"
        )


def _assert_skip_summary_edges(
    events: list[AuditLogORM],
    adsense_source_rows: list[GoogleRevenueSourceRowORM],
) -> None:
    """Assertion 4c: skipped rows surface as a finance-month-scoped ROWS_SKIPPED edge."""
    skip_edges = [e for e in events if e.details.get("lifecycle") == "ROWS_SKIPPED"]
    assert skip_edges, (
        "post-run normalize must surface dropped source rows as ROWS_SKIPPED "
        "summary edges instead of silently discarding them"
    )
    missing_channel_skipped = sum(
        e.details["skipped_by_reason"].get("missing_channel_id", 0) for e in skip_edges
    )
    assert missing_channel_skipped == len(adsense_source_rows), (
        "the AdSense rows skipped as MISSING_CHANNEL_ID must be reflected in a "
        f"ROWS_SKIPPED edge; got {missing_channel_skipped}, "
        f"expected {len(adsense_source_rows)}"
    )
    for edge in skip_edges:
        assert edge.scope_id == REPORT_MONTH, (
            f"ROWS_SKIPPED edge must be finance-month scoped; got {edge.scope_id!r}"
        )
        assert edge.details["skipped_count"] == sum(edge.details["skipped_by_reason"].values()), (
            "skipped_count must equal the sum of the per-reason counts"
        )


def _assert_fact_audit_and_principal(
    session: Session,
    events: list[AuditLogORM],
) -> None:
    """Assertions 4b + principal: fact CREATED edges + one service principal on all rows."""
    fact_audit_events = list(
        session.scalars(
            select(AuditLogORM)
            .where(AuditLogORM.tenant_id == TENANT_ID)
            .where(AuditLogORM.event_type == "REPORT_IMPORTED")
            .where(AuditLogORM.entity_type == "monthly_channel_revenue_fact")
            .order_by(AuditLogORM.created_at, AuditLogORM.id)
        )
    )
    assert len(fact_audit_events) == 2, (
        f"expected 2 fact-audit events (YT Reporting + YT Analytics); "
        f"got {len(fact_audit_events)}: "
        f"{[(e.entity_id, e.details.get('lifecycle')) for e in fact_audit_events]}"
    )
    fact_audit_lifecycles = [e.details["lifecycle"] for e in fact_audit_events]
    assert fact_audit_lifecycles == ["CREATED", "CREATED"], (
        f"expected both normalize CREATED edges; got {fact_audit_lifecycles!r}"
    )
    fact_audit_actors = {e.details.get("actor_user_id") for e in fact_audit_events}
    assert fact_audit_actors == {SERVICE_ACTOR_ID}, (
        f"expected the connector service principal on every fact-audit row; "
        f"got {fact_audit_actors!r}"
    )
    # Every audit row must carry the same connector service principal. The
    # SqlAlchemyAuditSink stores the raw actor UUID in
    # ``details['actor_user_id']`` when the UUID is not a real users.id, and
    # sets ``user_id=None`` on the row. Both the column and the details key
    # must agree across all 12 events: a single principal for all three runs.
    assert {event.user_id for event in events} == {None}, (
        "expected user_id=None on all rows (service principal is not a users.id)"
    )
    assert {event.details.get("actor_user_id") for event in events} == {SERVICE_ACTOR_ID}, (
        "expected the connector service principal on all rows"
    )


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
def _run_yt_reporting(session: Session, channel_id: str):
    """Run the YT Reporting connector against an in-memory mock backend."""
    yt_reporting_csv = _yt_reporting_csv_bytes(
        channel_id=channel_id, account_id=YT_REPORTING_ACCOUNT
    )
    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeReportingClient"
        ) as yt_rep_cls,
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend"
        ) as local_cls_rep,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh_rep,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls_rep,
    ):
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
        _wire_mock_file_store(local_cls_rep)

        return run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=YT_REPORTING_KEY,
            account_id=YT_REPORTING_ACCOUNT,
            report_month=REPORT_MONTH,
        )


def _run_yt_analytics(session: Session, channel_id: str):
    """Run the YT Analytics connector against an in-memory mock backend."""
    yt_analytics_payload = _yt_analytics_payload(
        channel_id=channel_id, account_id=YT_ANALYTICS_ACCOUNT
    )
    with (
        patch("ums_smart_revenue.connectors.runs.orchestrator.YouTubeAnalyticsClient") as yt_an_cls,
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend"
        ) as local_cls_an,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh_an,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls_an,
    ):
        http_cls_an.return_value.close.return_value = None
        refresh_an.return_value = None

        an_client = yt_an_cls.return_value
        an_client.fetch_channel_report.return_value = yt_analytics_payload
        _wire_mock_file_store(local_cls_an)

        return run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=YT_ANALYTICS_KEY,
            account_id=YT_ANALYTICS_ACCOUNT,
            report_month=REPORT_MONTH,
        )


def _run_adsense(session: Session):
    """Run the AdSense connector against an in-memory mock backend."""
    adsense_payload = _adsense_payload(account_id=ADSENSE_ACCOUNT)
    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.AdSenseManagementClient"
        ) as adsense_cls,
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.LocalFileStoreBackend"
        ) as local_cls_ads,
        patch("ums_smart_revenue.connectors.runs.orchestrator.refresh_credentials") as refresh_ads,
        patch("ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient") as http_cls_ads,
    ):
        http_cls_ads.return_value.close.return_value = None
        refresh_ads.return_value = None

        ads_client = adsense_cls.return_value
        ads_client.fetch_monthly_report.return_value = adsense_payload
        _wire_mock_file_store(local_cls_ads)

        return run_one(
            session,
            tenant_id=TENANT_ID,
            connector_key=ADSENSE_KEY,
            account_id=ADSENSE_ACCOUNT,
            report_month=REPORT_MONTH,
        )


def test_three_connectors_end_to_end_on_mocks(session: Session, _stub_secret_resolver) -> None:
    """Mock end-to-end ingestion gate -- B2.6 §9.3 closing assertion."""
    # ----- seed credentials for all three connectors -----
    _make_credential_row(session, connector_key=YT_REPORTING_KEY, account_id=YT_REPORTING_ACCOUNT)
    _make_credential_row(session, connector_key=YT_ANALYTICS_KEY, account_id=YT_ANALYTICS_ACCOUNT)
    _make_credential_row(session, connector_key=ADSENSE_KEY, account_id=ADSENSE_ACCOUNT)

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

    # ----- run YT Reporting -----
    outcome_reporting = _run_yt_reporting(session, channel_id)
    assert outcome_reporting.run is not None
    assert outcome_reporting.run.status == "SUCCEEDED"

    # ----- run YT Analytics -----
    outcome_analytics = _run_yt_analytics(session, channel_id)
    assert outcome_analytics.run is not None
    assert outcome_analytics.run.status == "SUCCEEDED"

    # ----- run AdSense -----
    outcome_adsense = _run_adsense(session)
    assert outcome_adsense.run is not None
    assert outcome_adsense.run.status == "SUCCEEDED"

    # ----- Assertions (delegated to focused helpers) -----
    source_rows = _load_source_rows(session)
    adsense_source_rows = _assert_source_rows_shape(source_rows)
    _assert_facts_and_skip_semantics(session, channel_id, adsense_source_rows)

    events = _connector_audit_events(session)
    _assert_run_lifecycle_sequence(events)
    _assert_skip_summary_edges(events, adsense_source_rows)
    _assert_fact_audit_and_principal(session, events)
