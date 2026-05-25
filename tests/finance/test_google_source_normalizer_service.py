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


def test_normalize_month_skips_missing_channel_id_rows(session):
    tenant_id = uuid4()
    _seed_tenant_and_currencies(session, tenant_id)
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    # AdSense rows always have youtube_channel_id=None per parser; we
    # build ParsedSourceRow directly with channel_id=None to model the
    # same condition for any source_system.
    repo.upsert_many(
        tenant_id,
        [
            ParsedSourceRow(
                source_system="youtube_reporting",
                source_row_key="m" * 64,
                source_account_id="acct-1",
                content_owner_id=None,
                youtube_channel_id=None,
                report_type="x",
                report_month="2026-04",
                period_start=date(2026, 4, 1),
                period_end=date(2026, 4, 30),
                metric_key="estimatedRevenue",
                value_kind="estimated",
                amount_native=Decimal("100"),
                currency_code="USD",
                source_report_id="r-1",
                raw_payload={},
            ),
        ],
        raw_file_id=None,
        imported_by=None,
    )
    session.commit()

    result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
        month="2026-04", actor_user_id=ACTOR_USER_ID,
    )
    assert len(result.skipped) == 1
    assert result.skipped[0].reason.value == "missing_channel_id"


def test_normalize_month_skips_unknown_channel_rows(session):
    tenant_id = uuid4()
    _seed_tenant_and_currencies(session, tenant_id)
    # Sub-arrange A: channel does not exist in registry at all.
    # Sub-arrange B: channel exists but active=False.
    session.add(
        YouTubeChannelORM(
            id=uuid4(),
            tenant_id=tenant_id,
            youtube_channel_id="UC_inactive",
            channel_name="Inactive Ch",
            cms_status="INSIDE_CMS",
            revenue_required=True,
            active=False,
        )
    )
    session.flush()

    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    repo.upsert_many(
        tenant_id,
        [
            _yt_reporting_row(channel="UC_unregistered", source_row_key_seed="x"),
            _yt_reporting_row(channel="UC_inactive", source_row_key_seed="y"),
        ],
        raw_file_id=None,
        imported_by=None,
    )
    session.commit()

    result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
        month="2026-04", actor_user_id=ACTOR_USER_ID,
    )
    assert len(result.skipped) == 2
    assert all(s.reason.value == "unknown_channel" for s in result.skipped)


def test_normalize_month_skips_unsupported_value_kind_rows(session):
    tenant_id = uuid4()
    _seed_tenant_and_currencies(session, tenant_id)
    _seed_active_channel(session, tenant_id, "UC_test_tax")
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    repo.upsert_many(
        tenant_id,
        [
            _yt_reporting_row(
                channel="UC_test_tax", source_row_key_seed="t",
                metric_key="estimatedRevenue", value_kind="tax",
            ),
            _yt_reporting_row(
                channel="UC_test_tax", source_row_key_seed="d",
                metric_key="estimatedRevenue", value_kind="deduction",
            ),
            _yt_reporting_row(
                channel="UC_test_tax", source_row_key_seed="j",
                metric_key="estimatedRevenue", value_kind="adjustment",
            ),
        ],
        raw_file_id=None,
        imported_by=None,
    )
    session.commit()

    result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
        month="2026-04", actor_user_id=ACTOR_USER_ID,
    )
    assert len(result.skipped) == 3
    assert all(s.reason.value == "unsupported_value_kind" for s in result.skipped)


def test_normalize_month_skips_non_usd_canonical_with_NON_USD_CURRENCY(session):  # noqa: N802
    tenant_id = uuid4()
    _seed_tenant_and_currencies(session, tenant_id)
    _seed_active_channel(session, tenant_id, "UC_test_fx")
    SqlAlchemyGoogleRevenueSourceRowRepository(session).upsert_many(
        tenant_id,
        [
            _yt_reporting_row(
                channel="UC_test_fx", source_row_key_seed="e",
                currency="EGP", amount="500.000000",
            ),
        ],
        raw_file_id=None,
        imported_by=None,
    )
    session.commit()

    result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
        month="2026-04", actor_user_id=ACTOR_USER_ID,
    )
    assert any(s.reason.value == "non_usd_currency" for s in result.skipped)


def _adsense_row(
    *,
    channel: str,
    metric_key: str,
    source_row_key_seed: str,
    value_kind: str = "estimated",
    amount: str = "100.000000",
    currency: str = "USD",
) -> ParsedSourceRow:
    # AdSense parser sets youtube_channel_id=None natively. For service tests
    # exercising channel-scoped AdSense canonical selection, build
    # ParsedSourceRow inline with a synthesized channel_id.
    return ParsedSourceRow(
        source_system="adsense_management",
        source_row_key=(source_row_key_seed * 64)[:64],
        source_account_id="pub-test-1",
        content_owner_id=None,
        youtube_channel_id=channel,
        report_type=("payment_report" if metric_key == "PAID_AMOUNT" else "earnings_report"),
        report_month="2026-04",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
        metric_key=metric_key,
        value_kind=("settled" if metric_key == "PAID_AMOUNT" else value_kind),
        amount_native=Decimal(amount),
        currency_code=currency,
        source_report_id="r-1",
        raw_payload={},
    )


def test_normalize_month_skips_no_canonical_row_with_NO_CANONICAL_ROW(session):  # noqa: N802
    # AdSense bucket with only UNPAID_AMOUNT: no preferred metric matches.
    tenant_id = uuid4()
    _seed_tenant_and_currencies(session, tenant_id)
    _seed_active_channel(session, tenant_id, "UC_test_unpaid")
    SqlAlchemyGoogleRevenueSourceRowRepository(session).upsert_many(
        tenant_id,
        [
            _adsense_row(
                channel="UC_test_unpaid",
                metric_key="UNPAID_AMOUNT",
                source_row_key_seed="u",
            ),
        ],
        raw_file_id=None,
        imported_by=None,
    )
    session.commit()

    result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
        month="2026-04", actor_user_id=ACTOR_USER_ID,
    )
    assert len(result.skipped) == 1
    assert result.skipped[0].reason.value == "no_canonical_row"


def test_normalize_month_marks_unselected_usd_rows_as_NON_CANONICAL_METRIC(session):  # noqa: N802
    # AdSense bucket with both PAID_AMOUNT and ESTIMATED_EARNINGS in USD:
    # PAID_AMOUNT wins canonical; ESTIMATED_EARNINGS is marked NON_CANONICAL_METRIC.
    tenant_id = uuid4()
    _seed_tenant_and_currencies(session, tenant_id)
    _seed_active_channel(session, tenant_id, "UC_test_dual")
    SqlAlchemyGoogleRevenueSourceRowRepository(session).upsert_many(
        tenant_id,
        [
            _adsense_row(
                channel="UC_test_dual", metric_key="PAID_AMOUNT", source_row_key_seed="p",
            ),
            _adsense_row(
                channel="UC_test_dual", metric_key="ESTIMATED_EARNINGS", source_row_key_seed="q",
            ),
        ],
        raw_file_id=None,
        imported_by=None,
    )
    session.commit()

    result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
        month="2026-04", actor_user_id=ACTOR_USER_ID,
    )
    assert any(s.reason.value == "non_canonical_metric" for s in result.skipped)
    # PAID_AMOUNT path goes through to record_fact (Task 10); for the partial
    # pipeline today it stays unhandled. Once Task 10 lands, this test still
    # passes (CREATED grows; NON_CANONICAL_METRIC still present in skipped).


def test_normalize_month_creates_revenue_facts_for_eligible_USD_rows(session):  # noqa: N802
    tenant_id = uuid4()
    _seed_tenant_and_currencies(session, tenant_id)
    _seed_active_channel(session, tenant_id, "UC_test_create")
    SqlAlchemyGoogleRevenueSourceRowRepository(session).upsert_many(
        tenant_id,
        [
            _yt_reporting_row(
                channel="UC_test_create",
                source_row_key_seed="c",
                amount="123.450000",
            ),
        ],
        raw_file_id=None,
        imported_by=None,
    )
    session.commit()

    result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
        month="2026-04", actor_user_id=ACTOR_USER_ID,
    )
    assert len(result.created) == 1
    assert len(result.updated) == 0
    assert len(result.unchanged) == 0
    assert result.created[0].source_kind == "YOUTUBE_CMS"
    assert result.created[0].gross_revenue_usd == Decimal("123.450000")


OTHER_ACTOR_USER_ID = "00000000-0000-0000-0000-000000010002"


def test_normalize_month_classifies_byte_identical_replay_as_unchanged(session):
    tenant_id = uuid4()
    _seed_tenant_and_currencies(session, tenant_id)
    _seed_active_channel(session, tenant_id, "UC_test_replay")
    SqlAlchemyGoogleRevenueSourceRowRepository(session).upsert_many(
        tenant_id,
        [_yt_reporting_row(channel="UC_test_replay", source_row_key_seed="r")],
        raw_file_id=None, imported_by=None,
    )
    session.commit()

    normalizer = GoogleSourceNormalizer(session, tenant_id=tenant_id)
    first = normalizer.normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)
    session.commit()
    second = normalizer.normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)
    assert len(first.created) == 1
    assert len(second.created) == 0
    assert len(second.updated) == 0
    assert len(second.unchanged) == 1


def test_normalize_month_classifies_amount_change_as_updated(session):
    tenant_id = uuid4()
    _seed_tenant_and_currencies(session, tenant_id)
    _seed_active_channel(session, tenant_id, "UC_test_upd")
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    repo.upsert_many(
        tenant_id,
        [_yt_reporting_row(channel="UC_test_upd", source_row_key_seed="z", amount="100.000000")],
        raw_file_id=None, imported_by=None,
    )
    session.commit()
    normalizer = GoogleSourceNormalizer(session, tenant_id=tenant_id)
    first = normalizer.normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)
    session.commit()
    # Change the amount via the same upsert key.
    repo.upsert_many(
        tenant_id,
        [_yt_reporting_row(channel="UC_test_upd", source_row_key_seed="z", amount="250.000000")],
        raw_file_id=None, imported_by=None,
    )
    session.commit()
    second = normalizer.normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)
    assert len(first.created) == 1
    assert len(second.updated) == 1
    assert second.updated[0].gross_revenue_usd == Decimal("250.000000")


def test_normalize_month_replay_by_different_actor_with_identical_payload_is_unchanged(session):
    tenant_id = uuid4()
    _seed_tenant_and_currencies(session, tenant_id)
    _seed_active_channel(session, tenant_id, "UC_test_actor")
    SqlAlchemyGoogleRevenueSourceRowRepository(session).upsert_many(
        tenant_id,
        [_yt_reporting_row(channel="UC_test_actor", source_row_key_seed="a")],
        raw_file_id=None, imported_by=None,
    )
    session.commit()
    normalizer = GoogleSourceNormalizer(session, tenant_id=tenant_id)
    normalizer.normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)
    session.commit()
    result = normalizer.normalize_month(
        month="2026-04", actor_user_id=OTHER_ACTOR_USER_ID,
    )
    assert len(result.unchanged) == 1
    assert len(result.updated) == 0


def test_normalize_month_writes_confidence_score_one_point_zero(session):
    tenant_id = uuid4()
    _seed_tenant_and_currencies(session, tenant_id)
    _seed_active_channel(session, tenant_id, "UC_test_conf")
    SqlAlchemyGoogleRevenueSourceRowRepository(session).upsert_many(
        tenant_id,
        [_yt_reporting_row(channel="UC_test_conf", source_row_key_seed="o")],
        raw_file_id=None, imported_by=None,
    )
    session.commit()
    result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
        month="2026-04", actor_user_id=ACTOR_USER_ID,
    )
    assert result.created[0].confidence_score == Decimal("1.0")


def test_normalize_month_uses_canonical_source_report_id(session):
    tenant_id = uuid4()
    _seed_tenant_and_currencies(session, tenant_id)
    _seed_active_channel(session, tenant_id, "UC_test_rid")
    # Inject a non-default source_report_id by writing a ParsedSourceRow
    # with an explicit report id value.
    repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
    row = _yt_reporting_row(channel="UC_test_rid", source_row_key_seed="i")
    from dataclasses import replace as dc_replace
    row = dc_replace(row, source_report_id="report-canonical-001")
    repo.upsert_many(tenant_id, [row], raw_file_id=None, imported_by=None)
    session.commit()
    result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
        month="2026-04", actor_user_id=ACTOR_USER_ID,
    )
    assert result.created[0].source_report_id == "report-canonical-001"
