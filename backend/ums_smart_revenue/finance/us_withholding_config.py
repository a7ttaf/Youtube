# ============================================================================
# Purpose: Load operator-confirmed, effective-dated US withholding display rates.
#   No silent default — absent config suppresses estimate surfaces (Docs/24 U3).
# Database/ORM: us_withholding_rate_configs.
# Standards: Backend-only finance estimate source; 0 <= rate <= 0.30 validated at
#   persistence boundary.
# Blast Radius: Display-estimate only; recon DEFAULT_US_WITHHOLDING_RATE unchanged.
# Connections:
#   - File: Docs/24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md -> U3 / D-U1.
# ============================================================================
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.security_models import UsWithholdingRateConfigORM

MAX_US_WITHHOLDING_RATE = Decimal("0.30")


@dataclass(frozen=True)
class UsWithholdingRateSnapshot:
    rate: Decimal
    account_type: str
    effective_from: date
    confirmed_by_user_id: UUID


def validate_us_withholding_rate(rate: Decimal) -> None:
    if rate < 0 or rate > MAX_US_WITHHOLDING_RATE:
        raise ValueError(
            f"US withholding display rate must be between 0 and {MAX_US_WITHHOLDING_RATE}"
        )


class SqlAlchemyUsWithholdingConfigRepository:
    """Effective-dated withholding config reads for display estimates."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_effective_rate(
        self,
        *,
        tenant_id: UUID,
        as_of: date,
    ) -> UsWithholdingRateSnapshot | None:
        row = self._session.scalar(
            select(UsWithholdingRateConfigORM)
            .where(
                UsWithholdingRateConfigORM.tenant_id == tenant_id,
                UsWithholdingRateConfigORM.effective_from <= as_of,
            )
            .order_by(
                UsWithholdingRateConfigORM.effective_from.desc(),
                UsWithholdingRateConfigORM.created_at.desc(),
            )
            .limit(1)
        )
        if row is None:
            return None
        return UsWithholdingRateSnapshot(
            rate=Decimal(row.rate),
            account_type=row.account_type,
            effective_from=row.effective_from,
            confirmed_by_user_id=row.confirmed_by_user_id,
        )

    def record_confirmed_rate(
        self,
        *,
        tenant_id: UUID,
        effective_from: date,
        rate: Decimal,
        account_type: str,
        confirmed_by_user_id: UUID,
    ) -> UsWithholdingRateSnapshot:
        validate_us_withholding_rate(rate)
        if account_type not in {"business", "individual"}:
            raise ValueError("account_type must be 'business' or 'individual'")
        row = UsWithholdingRateConfigORM(
            id=uuid4(),
            tenant_id=tenant_id,
            effective_from=effective_from,
            rate=rate,
            account_type=account_type,
            confirmed_by_user_id=confirmed_by_user_id,
        )
        self._session.add(row)
        self._session.flush()
        return UsWithholdingRateSnapshot(
            rate=Decimal(row.rate),
            account_type=row.account_type,
            effective_from=row.effective_from,
            confirmed_by_user_id=row.confirmed_by_user_id,
        )
