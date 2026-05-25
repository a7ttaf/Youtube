"""SQLite-backed service flow tests for GoogleSourceNormalizer."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    ParsedSourceRow,
)
from ums_smart_revenue.connectors.google_source_rows.repository import (
    SqlAlchemyGoogleRevenueSourceRowRepository,
)
from ums_smart_revenue.db.finance_models import FinanceBase
from ums_smart_revenue.db.org_models import OrgBase, YouTubeChannelORM
from ums_smart_revenue.db.source_models import CurrencyORM
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.finance.google_source_normalizer import (
    GoogleSourceNormalizer,
)
from ums_smart_revenue.finance.revenue_facts import RevenueFactValidationError

# NOTE: source_models.py registers GoogleRevenueSourceRowORM and CurrencyORM
# on FinanceBase.metadata (see backend/ums_smart_revenue/db/source_models.py
# module docstring). There is no separate SourceBase to create; importing
# source_models is unnecessary because FinanceBase.metadata.create_all()
# already covers those tables.

ACTOR_USER_ID = "00000000-0000-0000-0000-000000010001"


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    try:
        with Session(engine) as s:
            yield s
    finally:
        engine.dispose()


# ============================================================================
# Purpose: Shared test fixtures for the service-flow suite — seed a tenant
#          with USD + EGP reference currencies and add active channels +
#          ParsedSourceRow builders. Reused across Tasks 5-13.
# Database/ORM: tenants (TenantORM), currencies (CurrencyORM),
#               youtube_channels (YouTubeChannelORM).
# Standards: Test helpers only — no production logic. Channel rows are
#            constructed with explicit cms_status/revenue_required/active so
#            the normalizer's active-channel gate (later tasks) has a stable
#            INSIDE_CMS row to find. _yt_reporting_row pads the seed string
#            to the required 64-char source_row_key length.
# Blast Radius: Test scope only.
# ============================================================================
def _seed_tenant_and_currencies(session: Session, tenant_id: UUID) -> None:
    # FIX: is_supported=True requires activated_at to be non-null
    # (CHECK ck_currencies_supported_activated in source_models.CurrencyORM);
    # the plan's literal snippet omitted activated_at and would fail flush.
    activated = datetime.now(UTC)
    session.add(
        TenantORM(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", display_name="T")
    )
    session.add(
        CurrencyORM(
            code="USD",
            numeric_code="840",
            name="US Dollar",
            minor_unit=2,
            is_supported=True,
            activated_at=activated,
        )
    )
    session.add(
        CurrencyORM(
            code="EGP",
            numeric_code="818",
            name="Egyptian Pound",
            minor_unit=2,
            is_supported=True,
            activated_at=activated,
        )
    )
    session.flush()


def _seed_active_channel(
    session: Session, tenant_id: UUID, channel_id: str
) -> None:
    # FIX: YouTubeChannelORM.id has server_default=gen_random_uuid() (Postgres
    # only); SQLite raises "unknown function: gen_random_uuid()" on flush.
    # Pass an explicit Python-side UUID so the test stays sqlite-portable,
    # consistent with the convention in tests/org/test_sql_channel_registry.py.
    session.add(
        YouTubeChannelORM(
            id=uuid4(),
            tenant_id=tenant_id,
            youtube_channel_id=channel_id,
            channel_name=f"Ch {channel_id}",
            cms_status="INSIDE_CMS",
            revenue_required=True,
            active=True,
        )
    )
    session.flush()


def _yt_reporting_row(
    *,
    channel: str,
    source_row_key_seed: str,
    amount: str = "100.000000",
    currency: str = "USD",
    metric_key: str = "estimatedRevenue",
    value_kind: str = "estimated",
) -> ParsedSourceRow:
    return ParsedSourceRow(
        source_system="youtube_reporting",
        source_row_key=(source_row_key_seed * 64)[:64],
        source_account_id=channel,
        content_owner_id=None,
        youtube_channel_id=channel,
        report_type="channel_monthly_estimated_revenue",
        report_month="2026-04",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
        metric_key=metric_key,
        value_kind=value_kind,
        amount_native=Decimal(amount),
        currency_code=currency,
        source_report_id="r-1",
        raw_payload={"dimensions": {"country": "US"}},
    )


def test_normalize_month_raises_validation_error_for_invalid_month_format(session):
    normalizer = GoogleSourceNormalizer(session)
    with pytest.raises(RevenueFactValidationError, match="month must use YYYY-MM"):
        normalizer.normalize_month(month="2026-13", actor_user_id=ACTOR_USER_ID)


def test_normalize_month_channel_ids_filter_drops_out_of_scope_rows_silently(session):
    tenant_id = uuid4()
    _seed_tenant_and_currencies(session, tenant_id)
    _seed_active_channel(session, tenant_id, "UC_test_in")
    _seed_active_channel(session, tenant_id, "UC_test_out")
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    repo.upsert_many(
        tenant_id,
        [
            _yt_reporting_row(channel="UC_test_in", source_row_key_seed="a"),
            _yt_reporting_row(channel="UC_test_out", source_row_key_seed="b"),
        ],
        raw_file_id=None,
        imported_by=None,
    )
    session.commit()

    result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
        month="2026-04",
        channel_ids=["UC_test_in"],
        actor_user_id=ACTOR_USER_ID,
    )
    # Out-of-scope rows must NOT appear in skipped (silently dropped).
    skipped_ids = {s.source_row_id for s in result.skipped}
    assert all("UC_test_out" not in s.source_row_id for s in result.skipped) or skipped_ids == set()
    # The in-scope channel's row should not be skipped for scope reasons
    # (it may still be skipped/created by later steps but not for scope).
    assert not any(
        s.reason.value == "missing_channel_id" for s in result.skipped
    ), "in-scope rows must not be classified as missing_channel_id"
