"""Logging contract: counts/distribution logged; PII / finance values redacted."""

import logging
from datetime import date
from decimal import Decimal
from uuid import uuid4

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

ACTOR_USER_ID = "00000000-0000-0000-0000-000000010001"


def test_normalize_month_logging_redacts_payload_amount_channel_id_source_row_id(  # noqa: N802
    caplog,
):
    from datetime import UTC, datetime

    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    # source_models registers on FinanceBase.metadata -- no separate base required.
    tenant_id = uuid4()
    with Session(engine) as session:
        session.add(TenantORM(id=tenant_id, slug="t-log", display_name="T Log"))
        session.add(
            CurrencyORM(
                code="USD",
                numeric_code="840",
                name="US Dollar",
                minor_unit=2,
                is_supported=True,
                activated_at=datetime.now(UTC),
            )
        )
        session.add(
            CurrencyORM(
                code="EGP",
                numeric_code="818",
                name="Egyptian Pound",
                minor_unit=2,
                is_supported=True,
                activated_at=datetime.now(UTC),
            )
        )
        session.add(
            YouTubeChannelORM(
                id=uuid4(),
                tenant_id=tenant_id,
                youtube_channel_id="UC_test_log_42",
                channel_name="Log Ch",
                cms_status="INSIDE_CMS",
                revenue_required=True,
                active=True,
            )
        )
        session.flush()
        SqlAlchemyGoogleRevenueSourceRowRepository(session).upsert_many(
            tenant_id,
            [
                ParsedSourceRow(
                    source_system="youtube_reporting",
                    source_row_key="L" * 64,
                    source_account_id="acct-log",
                    content_owner_id=None,
                    youtube_channel_id="UC_test_log_42",
                    report_type="x",
                    report_month="2026-04",
                    period_start=date(2026, 4, 1),
                    period_end=date(2026, 4, 30),
                    metric_key="estimatedRevenue",
                    value_kind="estimated",
                    amount_native=Decimal("999.876543"),
                    currency_code="USD",
                    source_report_id="rep-log-001",
                    raw_payload={"dimensions": {"country": "US"}, "secret": "DO_NOT_LOG"},
                ),
                # Non-USD row to force a NON_USD_CURRENCY skip and exercise
                # the skipped_by_reason key in the complete line.
                ParsedSourceRow(
                    source_system="youtube_reporting",
                    source_row_key="M" * 64,
                    source_account_id="acct-log",
                    content_owner_id=None,
                    youtube_channel_id="UC_test_log_42",
                    report_type="x",
                    report_month="2026-04",
                    period_start=date(2026, 4, 1),
                    period_end=date(2026, 4, 30),
                    metric_key="estimatedRevenue",
                    value_kind="estimated",
                    amount_native=Decimal("888.111111"),
                    currency_code="EGP",
                    source_report_id="rep-log-002",
                    raw_payload={"dimensions": {"country": "EG"}},
                ),
            ],
            raw_file_id=None,
            imported_by=None,
        )
        session.commit()

        with caplog.at_level(
            logging.INFO, logger="ums_smart_revenue.finance.google_source_normalizer"
        ):
            result = GoogleSourceNormalizer(
                session,
                tenant_id=tenant_id,
            ).normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)

        log_text = "\n".join(rec.getMessage() for rec in caplog.records)

        # Must include: start + complete log lines.
        assert "normalize_month start" in log_text
        assert "normalize_month complete" in log_text
        # Must include aggregate counts.
        assert "created=" in log_text
        assert "updated=" in log_text
        assert "unchanged=" in log_text
        assert "skipped=" in log_text

        # Must NOT include forbidden values.
        assert "999.876543" not in log_text  # amount_native
        assert "888.111111" not in log_text
        assert "rep-log-001" not in log_text  # source_report_id (provenance, not log-worthy)
        assert "DO_NOT_LOG" not in log_text  # raw_payload contents
        assert "UC_test_log_42" not in log_text  # individual channel id
        # source_row_id UUIDs are never emitted as individual values.
        for row_entry in SqlAlchemyGoogleRevenueSourceRowRepository(session).list(
            tenant_id,
            report_month="2026-04",
        ):
            assert row_entry.id not in log_text

        # And the result reflects what we set up: 1 CREATED + 1 NON_USD skip.
        assert len(result.created) == 1
        assert any(s.reason.value == "non_usd_currency" for s in result.skipped)
