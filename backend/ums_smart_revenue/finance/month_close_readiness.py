from dataclasses import dataclass
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import MonthlyChannelRevenueFactORM, RevenueManualOverrideORM
from ums_smart_revenue.finance.reconciliation import build_revenue_reconciliation_issue_queue
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry


MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@dataclass(frozen=True)
class FinanceCloseBlocker:
    blocker_type: str
    severity: str
    count: int
    message: str

    def to_api(self) -> dict[str, object]:
        return {
            "blocker_type": self.blocker_type,
            "severity": self.severity,
            "count": self.count,
            "message": self.message,
        }


@dataclass(frozen=True)
class FinanceCloseReadiness:
    month: str
    blockers: list[FinanceCloseBlocker]

    @property
    def ready(self) -> bool:
        return not self.blockers

    def to_api(self) -> dict[str, object]:
        return {
            "month": self.month,
            "ready": self.ready,
            "blockers": [blocker.to_api() for blocker in self.blockers],
        }

    def to_lock_error_detail(self) -> dict[str, object]:
        return {
            "message": "Finance month has unresolved close blockers",
            "blockers": [blocker.to_api() for blocker in self.blockers],
        }


class SqlAlchemyFinanceCloseReadinessService:
    def __init__(self, session: Session):
        self._session = session

    def check_month(self, month: str) -> FinanceCloseReadiness:
        _validate_month(month)
        blockers: list[FinanceCloseBlocker] = []
        pending_override_count = self._pending_manual_override_count(month)
        if pending_override_count:
            blockers.append(
                FinanceCloseBlocker(
                    blocker_type="PENDING_MANUAL_OVERRIDES",
                    severity="HIGH",
                    count=pending_override_count,
                    message=_pending_override_message(month, pending_override_count),
                )
            )

        issue_count = len(build_revenue_reconciliation_issue_queue(self._month_facts(month), month=month).items)
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

    def _pending_manual_override_count(self, month: str) -> int:
        return int(
            self._session.scalar(
                select(func.count()).select_from(RevenueManualOverrideORM).where(
                    RevenueManualOverrideORM.month == month,
                    RevenueManualOverrideORM.status == "PENDING",
                )
            )
            or 0
        )

    def _month_facts(self, month: str) -> list[RevenueFactEntry]:
        rows = self._session.scalars(
            select(MonthlyChannelRevenueFactORM)
            .where(MonthlyChannelRevenueFactORM.month == month)
            .order_by(MonthlyChannelRevenueFactORM.youtube_channel_id, MonthlyChannelRevenueFactORM.source_kind)
        ).all()
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
    if not MONTH_PATTERN.fullmatch(month):
        raise ValueError("month must use YYYY-MM with a calendar month from 01 to 12")


def _pending_override_message(month: str, count: int) -> str:
    subject = "manual override" if count == 1 else "manual overrides"
    verb = "requires" if count == 1 else "require"
    return f"{count} pending {subject} {verb} approval before locking {month}."


def _reconciliation_issue_message(month: str, count: int) -> str:
    subject = "channel has" if count == 1 else "channels have"
    return f"{count} {subject} unresolved reconciliation issues for {month}."
