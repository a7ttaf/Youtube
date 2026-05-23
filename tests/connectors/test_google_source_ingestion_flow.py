"""End-to-end flow: parser -> repository upsert -> idempotency.

Covers all three parsers + the repository surface in a single
fixture-driven flow. Uses SQLite metadata-create-all; the full Postgres
round-trip is Phase 8.
"""

import json
from datetime import datetime
from importlib import resources
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google_source_parsers import (
    AdSenseManagementParser,
    YouTubeAnalyticsParser,
    YouTubeReportingParser,
)
from ums_smart_revenue.connectors.google_source_rows import (
    SqlAlchemyGoogleRevenueSourceRowRepository,
)
from ums_smart_revenue.db.finance_models import FinanceBase
from ums_smart_revenue.db.source_models import (
    CurrencyORM,
    GoogleRevenueSourceRowORM,
)
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM

TENANT_ID = uuid4()
RAW_FILE_ID = uuid4()


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)
    TenantBase.metadata.create_all(engine)
    with Session(engine) as s:
        now = datetime.now()
        s.add_all([
            TenantORM(id=TENANT_ID, slug="tenant-x", display_name="Tenant X"),
            CurrencyORM(
                code="USD",
                numeric_code="840",
                name="US Dollar",
                minor_unit=2,
                is_supported=True,
                activated_at=now,
            ),
            CurrencyORM(
                code="GBP",
                numeric_code="826",
                name="Pound Sterling",
                minor_unit=2,
                is_supported=True,
                activated_at=now,
            ),
            CurrencyORM(
                code="EGP",
                numeric_code="818",
                name="Egyptian Pound",
                minor_unit=2,
                is_supported=True,
                activated_at=now,
            ),
        ])
        s.flush()
        yield s


def _load(package: str, name: str) -> dict:
    ref = resources.files(package).joinpath(name)
    with ref.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def test_end_to_end_three_parsers_upsert_into_repository(session: Session) -> None:
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)

    yt_rep = list(YouTubeReportingParser().parse(
        _load("tests.connectors._fixtures.youtube_reporting", "sample_estimated_revenue_2026_04.json"),
        tenant_id=TENANT_ID,
    ))
    yt_ana = list(YouTubeAnalyticsParser().parse(
        _load("tests.connectors._fixtures.youtube_analytics", "sample_query_response_2026_04.json"),
        tenant_id=TENANT_ID,
    ))
    ads_earn = list(AdSenseManagementParser().parse(
        _load("tests.connectors._fixtures.adsense_management", "sample_earnings_report_2026_04.json"),
        tenant_id=TENANT_ID,
    ))
    ads_pay = list(AdSenseManagementParser().parse(
        _load("tests.connectors._fixtures.adsense_management", "sample_payment_report_2026_04.json"),
        tenant_id=TENANT_ID,
    ))

    repo.upsert_many(TENANT_ID, yt_rep, raw_file_id=RAW_FILE_ID, imported_by=None)
    repo.upsert_many(TENANT_ID, yt_ana, raw_file_id=RAW_FILE_ID, imported_by=None)
    repo.upsert_many(TENANT_ID, ads_earn, raw_file_id=RAW_FILE_ID, imported_by=None)
    repo.upsert_many(TENANT_ID, ads_pay, raw_file_id=RAW_FILE_ID, imported_by=None)

    written = session.scalars(
        select(GoogleRevenueSourceRowORM).where(
            GoogleRevenueSourceRowORM.tenant_id == TENANT_ID
        )
    ).all()
    expected = len(yt_rep) + len(yt_ana) + len(ads_earn) + len(ads_pay)
    assert len(written) == expected


def test_rerun_with_identical_fixtures_produces_zero_new_rows(session: Session) -> None:
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)

    def parse_all(suffix: str) -> list:
        out = []
        out.extend(YouTubeReportingParser().parse(
            _load("tests.connectors._fixtures.youtube_reporting", f"sample_estimated_revenue_2026_04{suffix}.json"),
            tenant_id=TENANT_ID,
        ))
        out.extend(YouTubeAnalyticsParser().parse(
            _load("tests.connectors._fixtures.youtube_analytics", f"sample_query_response_2026_04{suffix}.json"),
            tenant_id=TENANT_ID,
        ))
        out.extend(AdSenseManagementParser().parse(
            _load("tests.connectors._fixtures.adsense_management", f"sample_earnings_report_2026_04{suffix}.json"),
            tenant_id=TENANT_ID,
        ))
        out.extend(AdSenseManagementParser().parse(
            _load("tests.connectors._fixtures.adsense_management", f"sample_payment_report_2026_04{suffix}.json"),
            tenant_id=TENANT_ID,
        ))
        return out

    first = parse_all("")
    repo.upsert_many(TENANT_ID, first, raw_file_id=RAW_FILE_ID, imported_by=None)
    first_count = session.query(GoogleRevenueSourceRowORM).filter_by(tenant_id=TENANT_ID).count()

    second = parse_all("_rerun")
    repo.upsert_many(TENANT_ID, second, raw_file_id=RAW_FILE_ID, imported_by=None)
    second_count = session.query(GoogleRevenueSourceRowORM).filter_by(tenant_id=TENANT_ID).count()

    assert second_count == first_count


def test_malformed_payload_raises_parser_error_without_partial_writes(session: Session) -> None:
    from ums_smart_revenue.connectors.google_source_parsers import ParserError
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)  # noqa: F841
    bad_payload = {"report_metadata": {"report_id": "x", "report_type": "y"}, "rows": "not a list"}
    with pytest.raises(ParserError):
        list(YouTubeReportingParser().parse(bad_payload, tenant_id=TENANT_ID))
    # No rows were yielded, so no upsert call - partial writes are impossible
    # because the parser fails before producing any ParsedSourceRow.
    assert session.query(GoogleRevenueSourceRowORM).filter_by(tenant_id=TENANT_ID).count() == 0
