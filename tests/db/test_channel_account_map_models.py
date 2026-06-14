"""SQLite model + constraint coverage for the channel-account map tables."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    ContentOwnerChannelLinkORM,
    FinanceBase,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

TENANT = UUID(UMS_TENANT_ID)


def _engine(tmp_path):
    """Return a fresh in-memory SQLite engine with the finance schema."""
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}")
    FinanceBase.metadata.create_all(engine)
    return engine


def test_account_owner_link_persists_with_defaults(tmp_path):
    """AdsenseContentOwnerLinkORM row persists with expected defaults."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        session.add(
            AdsenseContentOwnerLinkORM(
                tenant_id=TENANT,
                adsense_account_id="pub-1",
                content_owner_id="owner-1",
                provenance_kind="OPERATOR_ASSERTED",
                effective_month_start="2026-01",
            )
        )
        session.commit()
        row = session.query(AdsenseContentOwnerLinkORM).one()
    assert row.verification_status == "UNVERIFIED"
    assert row.effective_month_end is None
    assert row.provenance_payload == {}


def test_account_owner_link_status_check_rejects_unknown(tmp_path):
    """status CHECK constraint rejects unknown verification_status values."""
    engine = _engine(tmp_path)
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(
            AdsenseContentOwnerLinkORM(
                tenant_id=TENANT,
                adsense_account_id="pub-1",
                content_owner_id="owner-1",
                verification_status="BOGUS",
                provenance_kind="OPERATOR_ASSERTED",
                effective_month_start="2026-01",
            )
        )
        session.commit()


def test_account_owner_link_range_check_rejects_end_before_start(tmp_path):
    """effective_month range CHECK rejects end < start."""
    engine = _engine(tmp_path)
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(
            AdsenseContentOwnerLinkORM(
                tenant_id=TENANT,
                adsense_account_id="pub-1",
                content_owner_id="owner-1",
                provenance_kind="OPERATOR_ASSERTED",
                effective_month_start="2026-06",
                effective_month_end="2026-01",
            )
        )
        session.commit()


def test_owner_channel_link_persists_active_default(tmp_path):
    """ContentOwnerChannelLinkORM row persists with active=True default."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        session.add(
            ContentOwnerChannelLinkORM(
                tenant_id=TENANT,
                content_owner_id="owner-1",
                youtube_channel_id="chan-1",
                provenance_kind="SOURCE_ROW",
                provenance_source_id="row-1",
                effective_month_start="2026-04",
                effective_month_end="2026-04",
            )
        )
        session.commit()
        row = session.query(ContentOwnerChannelLinkORM).one()
    assert row.active is True


@pytest.mark.parametrize("bad_month", ["2026-13", "202601", "2026-1x"])
def test_account_owner_link_month_format_check_rejects_malformed(tmp_path, bad_month):
    """YYYY-MM format CHECK rejects malformed month strings."""
    engine = _engine(tmp_path)
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(
            AdsenseContentOwnerLinkORM(
                tenant_id=TENANT,
                adsense_account_id="pub-1",
                content_owner_id="owner-1",
                provenance_kind="OPERATOR_ASSERTED",
                effective_month_start=bad_month,
            )
        )
        session.commit()


def test_owner_channel_link_provenance_kind_check_rejects_unknown(tmp_path):
    """provenance_kind CHECK rejects unknown values on content_owner_channel_links."""
    engine = _engine(tmp_path)
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(
            ContentOwnerChannelLinkORM(
                tenant_id=TENANT,
                content_owner_id="owner-1",
                youtube_channel_id="chan-1",
                provenance_kind="BOGUS",
                effective_month_start="2026-04",
            )
        )
        session.commit()
