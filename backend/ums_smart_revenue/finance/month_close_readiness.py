import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import (
    MonthlyChannelRevenueFactORM,
    RevenueManualOverrideORM,
)
from ums_smart_revenue.db.org_models import YouTubeChannelORM
from ums_smart_revenue.finance.month_close import acquire_finance_month_advisory_lock
from ums_smart_revenue.finance.reconciliation import (
    build_revenue_reconciliation_issue_queue,
)
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@dataclass(frozen=True)
class FinanceCloseBlocker:
    """A single condition that prevents a finance month from closing."""

    blocker_type: str
    severity: str
    count: int
    message: str

    def to_api(self) -> dict[str, object]:
        """Serialize the blocker to the public API response shape."""
        return {
            "blocker_type": self.blocker_type,
            "severity": self.severity,
            "count": self.count,
            "message": self.message,
        }


@dataclass(frozen=True)
class FinanceCloseReadiness:
    """Readiness result for a finance month close attempt."""

    month: str
    blockers: list[FinanceCloseBlocker]

    @property
    def ready(self) -> bool:
        """Return whether the month has no unresolved blockers."""
        return not self.blockers

    def to_api(self) -> dict[str, object]:
        """Serialize readiness to the public API response shape."""
        return {
            "month": self.month,
            "ready": self.ready,
            "blockers": [blocker.to_api() for blocker in self.blockers],
        }

    def to_lock_error_detail(self) -> dict[str, object]:
        """Serialize readiness as the HTTP 409 detail used by lock attempts."""
        return {
            "message": "Finance month has unresolved close blockers",
            "blockers": [blocker.to_api() for blocker in self.blockers],
        }


class SqlAlchemyFinanceCloseReadinessService:
    """Evaluate close blockers from the SQL-backed finance and channel tables."""

    def __init__(self, session: Session):
        self._session = session

    def check_month(
        self, month: str, *, for_update: bool = False
    ) -> FinanceCloseReadiness:
        """Return readiness, optionally guarding the close attempt transaction."""
        _validate_month(month)
        if for_update:
            acquire_finance_month_advisory_lock(self._session, month)
        blockers: list[FinanceCloseBlocker] = []
        pending_override_count = self._pending_manual_override_count(
            month, for_update=for_update
        )
        if pending_override_count:
            blockers.append(
                FinanceCloseBlocker(
                    blocker_type="PENDING_MANUAL_OVERRIDES",
                    severity="HIGH",
                    count=pending_override_count,
                    message=_pending_override_message(month, pending_override_count),
                )
            )

        missing_fact_count = self._missing_required_revenue_fact_count(
            month, for_update=for_update
        )
        if missing_fact_count:
            blockers.append(
                FinanceCloseBlocker(
                    blocker_type="MISSING_REVENUE_FACTS",
                    severity="HIGH",
                    count=missing_fact_count,
                    message=_missing_fact_message(month, missing_fact_count),
                )
            )

        issue_queue = build_revenue_reconciliation_issue_queue(
            self._month_facts(month, for_update=for_update),
            month=month,
        )
        issue_count = len(issue_queue.items)
        if issue_count:
            blockers.append(
                FinanceCloseBlocker(
                    blocker_type="RECONCILIATION_ISSUES",
                    severity="HIGH",
                    count=issue_count,
                    message=_reconciliation_issue_message(month, issue_count),
                )
            )
        return FinanceCloseReadiness(month=month, blockers=blockers)

    def _pending_manual_override_count(self, month: str, *, for_update: bool) -> int:
        """Count pending overrides, locking matching rows during lock-time rechecks."""
        pending_overrides_statement = select(RevenueManualOverrideORM.id).where(
            RevenueManualOverrideORM.month == month,
            RevenueManualOverrideORM.status == "PENDING",
        )
        if for_update:
            return self._count_locked_rows(
                pending_overrides_statement.with_for_update()
            )
        return self._count_statement_rows(pending_overrides_statement)

    def _missing_required_revenue_fact_count(
        self, month: str, *, for_update: bool
    ) -> int:
        """Count active revenue-required channels missing facts for the month."""
        missing_channels_statement = (
            select(YouTubeChannelORM.youtube_channel_id)
            .select_from(YouTubeChannelORM)
            .outerjoin(
                MonthlyChannelRevenueFactORM,
                (
                    MonthlyChannelRevenueFactORM.youtube_channel_id
                    == YouTubeChannelORM.youtube_channel_id
                )
                & (MonthlyChannelRevenueFactORM.month == month),
            )
            .where(
                YouTubeChannelORM.active.is_(True),
                YouTubeChannelORM.revenue_required.is_(True),
                MonthlyChannelRevenueFactORM.id.is_(None),
            )
        )
        if for_update:
            return self._count_locked_rows(
                missing_channels_statement.with_for_update(of=YouTubeChannelORM)
            )
        return self._count_statement_rows(missing_channels_statement)

    def _count_statement_rows(self, statement) -> int:
        """Count rows returned by a selectable without duplicating its filters."""
        return int(
            self._session.scalar(select(func.count()).select_from(statement.subquery()))
            or 0
        )

    def _count_locked_rows(self, statement) -> int:
        """Count a row-locking scalar query without materializing all rows."""
        return sum(
            1
            for _ in self._session.scalars(statement.execution_options(yield_per=1000))
        )

    def _month_facts(self, month: str, *, for_update: bool) -> list[RevenueFactEntry]:
        """Load month facts used for reconciliation, locking them for close attempts."""
        statement = (
            select(MonthlyChannelRevenueFactORM)
            .where(MonthlyChannelRevenueFactORM.month == month)
            .order_by(
                MonthlyChannelRevenueFactORM.youtube_channel_id,
                MonthlyChannelRevenueFactORM.source_kind,
            )
        )
        if for_update:
            statement = statement.with_for_update()
        rows = self._session.scalars(statement).all()
        return [
            RevenueFactEntry(
                id=str(row.id),
                month=row.month,
                youtube_channel_id=row.youtube_channel_id,
                source_kind=row.source_kind,
                source_report_id=row.source_report_id,
                gross_revenue_usd=row.gross_revenue_usd,
                net_revenue_usd=row.net_revenue_usd,
                views=row.views,
                watch_time_minutes=row.watch_time_minutes,
                confidence_score=row.confidence_score,
                imported_by=str(row.imported_by) if row.imported_by else None,
            )
            for row in rows
        ]


def _validate_month(month: str) -> None:
    """Validate month path parameters before querying close state."""
    if not MONTH_PATTERN.fullmatch(month):
        raise ValueError("month must use YYYY-MM with a calendar month from 01 to 12")


def _pending_override_message(month: str, count: int) -> str:
    """Build a grammatically correct pending-override blocker message."""
    subject = "manual override" if count == 1 else "manual overrides"
    verb = "requires" if count == 1 else "require"
    return f"{count} pending {subject} {verb} approval before locking {month}."


def _missing_fact_message(month: str, count: int) -> str:
    """Build a grammatically correct missing-revenue-facts blocker message."""
    subject = "channel" if count == 1 else "channels"
    verb = "has" if count == 1 else "have"
    return f"{count} revenue-required {subject} {verb} no revenue facts for {month}."


def _reconciliation_issue_message(month: str, count: int) -> str:
    """Build a grammatically correct reconciliation-issue blocker message."""
    subject = "channel has" if count == 1 else "channels have"
    return f"{count} {subject} unresolved reconciliation issues for {month}."
