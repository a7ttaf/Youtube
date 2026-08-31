"""U2 country-evidence collection tests for YouTubeAnalyticsRunner."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from ums_smart_revenue.config.settings import AppSettings
from ums_smart_revenue.connectors.google.errors import GoogleApiResponseError
from ums_smart_revenue.connectors.google_source_parsers import YouTubeAnalyticsParser
from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    YOUTUBE_ANALYTICS_COUNTRY_EVIDENCE_REPORT_TYPE,
    YOUTUBE_ANALYTICS_PROJECTING_REPORT_TYPE,
)
from ums_smart_revenue.connectors.runs.orchestrator import (
    ProducedReportFailure,
    YouTubeAnalyticsRunner,
    _DeferredAnalyticsStaleCleanupState,
    _fallback_source_report_types,
    _flush_deferred_stale_cleanup_plans,
)


def _worldwide_response() -> dict[str, object]:
    return {
        "columnHeaders": [
            {"name": "month", "columnType": "DIMENSION", "dataType": "STRING"},
            {"name": "estimatedRevenue", "columnType": "METRIC", "dataType": "FLOAT"},
        ],
        "rows": [["2026-04", 100.0]],
    }


def _country_response() -> dict[str, object]:
    return {
        "columnHeaders": [
            {"name": "country", "columnType": "DIMENSION", "dataType": "STRING"},
            {"name": "estimatedRevenue", "columnType": "METRIC", "dataType": "FLOAT"},
        ],
        "rows": [["US", 40.0], ["EG", 5.0]],
    }


def _collect_reports(*, enabled: bool, country_error: Exception | None = None):
    session = MagicMock(name="session")
    session.in_transaction.return_value = False
    http = MagicMock(name="http")
    client = MagicMock(name="youtube_analytics_client")
    client.fetch_channel_report.return_value = _worldwide_response()
    if country_error is None:
        client.fetch_channel_country_evidence.return_value = _country_response()
    else:
        client.fetch_channel_country_evidence.side_effect = country_error

    with (
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.load_app_settings",
            return_value=AppSettings(
                youtube_analytics_country_evidence_enabled=enabled,
            ),
        ),
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.GoogleHttpClient",
            return_value=http,
        ),
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.YouTubeAnalyticsClient",
            return_value=client,
        ),
        patch(
            "ums_smart_revenue.connectors.runs.orchestrator.list_target_channels",
            return_value=["UC-test"],
        ),
    ):
        reports = list(
            YouTubeAnalyticsRunner.produce_reports(
                session=session,
                run=SimpleNamespace(tenant_id=str(uuid4())),
                credentials=MagicMock(name="credentials"),
                report_month="2026-04",
                account_id="cms-test-1",
            )
        )
    http.close.assert_called_once_with()
    return reports, client


def test_country_collection_defaults_to_no_second_api_request() -> None:
    """The guarded rollout cannot silently double Analytics API volume."""
    reports, client = _collect_reports(enabled=False)
    assert len(reports) == 1
    assert reports[0][0] == "youtube_analytics"
    client.fetch_channel_country_evidence.assert_not_called()


def test_enabled_country_collection_emits_parser_owned_non_projecting_evidence() -> None:
    """The U2 payload preserves source/account/country under an explicit fence."""
    reports, client = _collect_reports(enabled=True)
    assert [report[0] for report in reports] == [
        "youtube_analytics",
        "youtube_analytics_country_evidence",
    ]
    country_payload = reports[1][1]
    parsed = list(YouTubeAnalyticsParser().parse(country_payload, tenant_id=uuid4()))
    assert {row.source_system for row in parsed} == {"youtube_analytics"}
    assert {row.report_type for row in parsed} == {YOUTUBE_ANALYTICS_COUNTRY_EVIDENCE_REPORT_TYPE}
    assert {row.source_account_id for row in parsed} == {"contentOwner==cms-test-1"}
    assert {row.raw_payload["dimensions"]["country"] for row in parsed} == {"US", "EG"}
    assert {row.raw_payload["projection_disposition"] for row in parsed} == {
        "NON_PROJECTING_EVIDENCE"
    }
    client.fetch_channel_country_evidence.assert_called_once_with(
        account_id="cms-test-1",
        channel_id="UC-test",
        report_month="2026-04",
    )


def test_country_failure_is_report_scoped_partial_signal() -> None:
    """A country fetch failure is explicit and cannot masquerade as success."""
    error = GoogleApiResponseError(url="https://redacted.invalid", reason="bad shape")
    reports, _ = _collect_reports(enabled=True, country_error=error)
    assert len(reports) == 2
    assert reports[0][0] == "youtube_analytics"
    assert isinstance(reports[1], ProducedReportFailure)
    assert reports[1].report_type == "youtube_analytics_country_evidence"
    assert reports[1].error is error


def test_empty_country_success_targets_only_the_evidence_cleanup_scope() -> None:
    """An empty U2 replacement can never authorize worldwide-row deletion."""
    assert _fallback_source_report_types(
        parser=YouTubeAnalyticsParser(),
        default_report_type="youtube_analytics_country_evidence",
    ) == (YOUTUBE_ANALYTICS_COUNTRY_EVIDENCE_REPORT_TYPE,)
    assert _fallback_source_report_types(
        parser=YouTubeAnalyticsParser(),
        default_report_type="youtube_analytics",
    ) == (YOUTUBE_ANALYTICS_PROJECTING_REPORT_TYPE,)


def test_flag_off_worldwide_cleanup_cannot_delete_retained_country_evidence() -> None:
    """Enabled-to-disabled rollout keeps the independent evidence scope intact."""
    tenant_id = uuid4()
    source_account_id = "contentOwner==cms-test-1"
    worldwide_key = "w" * 64
    evidence_key = "e" * 64
    state = _DeferredAnalyticsStaleCleanupState(
        keep_source_row_keys_by_scope={
            (
                "youtube_analytics",
                YOUTUBE_ANALYTICS_PROJECTING_REPORT_TYPE,
                "2026-04",
                source_account_id,
            ): {worldwide_key}
        },
        attempted_channel_ids={"UC-test"},
    )
    repo = MagicMock(name="source_row_repo")
    repo.list.return_value = [
        SimpleNamespace(
            source_account_id=source_account_id,
            report_type=YOUTUBE_ANALYTICS_COUNTRY_EVIDENCE_REPORT_TYPE,
            youtube_channel_id="UC-test",
            source_row_key=evidence_key,
        )
    ]
    repo.delete_stale_for_scope.return_value = 0

    assert (
        _flush_deferred_stale_cleanup_plans(
            repo=repo,
            tenant_id=tenant_id,
            deferred_cleanup=state,
        )
        == 0
    )
    repo.delete_stale_for_scope.assert_called_once_with(
        tenant_id,
        source_system="youtube_analytics",
        source_account_id=source_account_id,
        report_type=YOUTUBE_ANALYTICS_PROJECTING_REPORT_TYPE,
        report_month="2026-04",
        keep_source_row_keys={worldwide_key},
    )
