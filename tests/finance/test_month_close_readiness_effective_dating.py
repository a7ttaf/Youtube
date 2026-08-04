"""Effective-dated readiness for LOCKED months (PR #159 review r3712948674).

Month-close readiness evaluates the CURRENT registry, so a channel created (or
made revenue-required) after a month was locked would otherwise retroactively
make that finalized month report MISSING_REVENUE_FACTS. For LOCKED months the
missing-fact count is effective-dated: only channels with ``created_at <=
locked_at`` are evaluated. The lock-time recheck runs while the month is still
OPEN, so the cutoff never weakens the lock gate itself.
"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceBase, FinanceMonthCloseORM
from ums_smart_revenue.db.org_models import OrgBase, YouTubeChannelORM
from ums_smart_revenue.finance.month_close_readiness import (
    SqlAlchemyFinanceCloseReadinessService,
)

MONTH = "2026-05"
LOCKED_AT = datetime(2026, 6, 5, 12, 0, tzinfo=UTC)
BEFORE_LOCK = datetime(2026, 5, 1, tzinfo=UTC)
AFTER_LOCK = datetime(2026, 7, 1, tzinfo=UTC)


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    return Session(engine)


def _add_channel(session: Session, channel_id: str, *, created_at: datetime) -> None:
    session.add(
        YouTubeChannelORM(
            id=uuid4(),
            youtube_channel_id=channel_id,
            channel_name=channel_id,
            primary_org_unit_id=None,
            cms_status="INSIDE_CMS",
            revenue_required=True,
            active=True,
            created_at=created_at,
        )
    )


def test_channel_created_after_lock_does_not_break_locked_month_readiness() -> None:
    """A post-lock channel is excluded from the LOCKED month's missing count."""
    session = _session()
    session.add(FinanceMonthCloseORM(month=MONTH, status="LOCKED", locked_at=LOCKED_AT))
    _add_channel(session, "channel-created-after-lock", created_at=AFTER_LOCK)
    session.commit()

    readiness = SqlAlchemyFinanceCloseReadinessService(session).check_month(MONTH)

    assert readiness.ready is True
    assert readiness.blockers == []


def test_channel_created_before_lock_still_counts_for_locked_month() -> None:
    """The cutoff excludes only post-lock channels, not pre-existing ones."""
    session = _session()
    session.add(FinanceMonthCloseORM(month=MONTH, status="LOCKED", locked_at=LOCKED_AT))
    _add_channel(session, "channel-created-before-lock", created_at=BEFORE_LOCK)
    session.commit()

    readiness = SqlAlchemyFinanceCloseReadinessService(session).check_month(MONTH)

    blocker_types = [blocker.blocker_type for blocker in readiness.blockers]
    assert "MISSING_REVENUE_FACTS" in blocker_types


def test_open_month_readiness_evaluates_every_current_channel() -> None:
    """No cutoff while the month is OPEN: locking evaluates the present state."""
    session = _session()
    session.add(FinanceMonthCloseORM(month=MONTH, status="OPEN"))
    _add_channel(session, "channel-created-after-lock", created_at=AFTER_LOCK)
    session.commit()

    readiness = SqlAlchemyFinanceCloseReadinessService(session).check_month(MONTH)

    blocker_types = [blocker.blocker_type for blocker in readiness.blockers]
    assert "MISSING_REVENUE_FACTS" in blocker_types


def test_lock_and_create_share_one_database_clock_source() -> None:
    """created_at and locked_at must come from the SAME clock, not two hosts.

    The LOCKED-month cutoff compares them directly, so independent
    application-host wall clocks (or a host clock stepping backward) could
    place a post-lock create before locked_at and recreate the race the
    advisory guard prevents (review #159 r3715073210). Both sides call
    ``serialization_timestamp``; on PostgreSQL that is clock_timestamp(),
    which — unlike now() — advances within a transaction, so a value read
    after the guard wait genuinely reflects post-guard ordering.
    """
    import inspect

    from ums_smart_revenue.finance import month_close, month_close_locks
    from ums_smart_revenue.org import sql_channel_registry

    assert "serialization_timestamp(self._session)" in inspect.getsource(
        month_close.SqlAlchemyFinanceMonthCloseRepository.lock_month
    )
    assert "serialization_timestamp(self._session)" in inspect.getsource(
        sql_channel_registry.SqlAlchemyChannelRegistry.create_channel
    )
    # The Postgres branch reads the database clock, never the app clock.
    source = inspect.getsource(month_close_locks.serialization_timestamp)
    assert "clock_timestamp" in source


def test_serialization_timestamp_falls_back_to_the_app_clock_off_postgres() -> None:
    """SQLite has no clock_timestamp(); the helper still returns a timestamp."""
    from ums_smart_revenue.finance.month_close_locks import serialization_timestamp

    session = _session()
    stamped = serialization_timestamp(session)

    assert stamped.tzinfo is not None
