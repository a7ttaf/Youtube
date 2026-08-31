# ============================================================================
# Purpose: Load operator-confirmed, account/effective-dated US withholding rates.
#   No tenant fallback — absent exact account config suppresses estimates (Docs/24 U3).
# Database/ORM: us_withholding_rate_configs.
# Standards: Backend-only account-scoped estimate source; 0 <= rate <= 0.30
#   validated at the persistence boundary.
# Blast Radius: Estimate-config repository only; reconciliation remains unchanged.
# Connections:
#   - File: Docs/24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md -> U3 / D-U1.
# ============================================================================
"""Withholding-rate configuration repository foundation.

No API or estimate surface consumes this repository in this slice. In
particular, this module does not change official reconciliation calculations,
exports, locked months, or the legacy reconciliation fallback.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google.adsense_management_client import (
    _validated_account_id,
)
from ums_smart_revenue.connectors.google.errors import MalformedAdsenseAccountIdError
from ums_smart_revenue.db.security_models import UsWithholdingRateConfigORM

MAX_US_WITHHOLDING_RATE = Decimal("0.30")


class UsWithholdingConfigError(Exception):
    """Base error for withholding configuration repository operations."""


class InvalidUsWithholdingConfigError(UsWithholdingConfigError, ValueError):
    """Raised when a proposed withholding configuration is invalid."""


class UsWithholdingConfigConflictError(UsWithholdingConfigError):
    """Raised when a withholding configuration conflicts with stored state."""


class UsWithholdingConfigStorageError(UsWithholdingConfigError):
    """Raised when withholding configuration storage is unavailable."""


# FIX (review P3): Numeric(8,6) storage re-represents accepted
# trailing-zero excess (0.1500000 -> 0.150000); quantizing at the write and
# snapshot boundaries keeps in-memory and reloaded representations identical.
_RATE_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True)
class UsWithholdingRateSnapshot:
    """Immutable view of one effective withholding-rate revision."""

    source_account_id: str
    rate: Decimal
    account_type: str
    effective_from: date
    revision: int
    confirmed_by_user_id: UUID


# ============================================================================
# Purpose: Validate a display-rate value exactly against the Numeric(8,6)
#   persistence contract before SQLAlchemy or PostgreSQL can round it.
# Database/ORM: us_withholding_rate_configs.rate.
# Standards: Reject non-Decimal, non-finite, over-scale, and out-of-range input.
# Blast Radius: Finance estimate configuration only; official amounts unchanged.
# Connections:
#   - File: backend/ums_smart_revenue/db/security_models.py -> Numeric(8,6) mirror.
#   - File: tests/finance/test_us_withholding_config.py -> Boundary coverage.
# ============================================================================
def validate_us_withholding_rate(rate: Decimal) -> None:
    """Reject rates that cannot be stored exactly as bounded Numeric(8,6)."""
    if not isinstance(rate, Decimal):
        raise InvalidUsWithholdingConfigError("US withholding display rate must be a Decimal")
    if not rate.is_finite():
        raise InvalidUsWithholdingConfigError("US withholding display rate must be finite")
    if rate < 0 or rate > MAX_US_WITHHOLDING_RATE:
        raise InvalidUsWithholdingConfigError(
            f"US withholding display rate must be between 0 and {MAX_US_WITHHOLDING_RATE}"
        )
    _sign, digits, exponent = rate.as_tuple()
    if not isinstance(exponent, int):
        raise InvalidUsWithholdingConfigError("US withholding display rate must be finite")
    if exponent < -6:
        excess_places = -6 - exponent
        excess_digits = digits if excess_places >= len(digits) else digits[-excess_places:]
        if any(excess_digits):
            raise InvalidUsWithholdingConfigError(
                "US withholding display rate supports at most 6 decimal places"
            )


def _canonical_source_account_id(source_account_id: str) -> str:
    """Return the canonical account key shared with payment ingestion."""
    if not isinstance(source_account_id, str):
        raise InvalidUsWithholdingConfigError(
            "source_account_id must be a nonblank canonical account identifier"
        )
    try:
        canonical_id = _validated_account_id(source_account_id)
    except MalformedAdsenseAccountIdError as exc:
        raise InvalidUsWithholdingConfigError(
            "source_account_id must be a nonblank canonical account identifier"
        ) from exc
    # FIX: The shared normalizer validates outer whitespace before stripping
    # `accounts/`; recheck the remainder so `accounts/ pub-1` cannot become a
    # distinct noncanonical configuration key or a misleading DB conflict.
    if canonical_id != canonical_id.strip():
        raise InvalidUsWithholdingConfigError(
            "source_account_id must be a nonblank canonical account identifier"
        )
    return canonical_id


class SqlAlchemyUsWithholdingConfigRepository:
    """Account/effective-dated withholding config reads for display estimates."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ========================================================================
    # Purpose: Read the latest confirmed revision effective for one tenant,
    #   source account, and date.
    # Database/ORM: us_withholding_rate_configs.
    # Standards: Explicit tenant/account filters and deterministic revision order.
    # Blast Radius: Estimate configuration read only; official finance unchanged.
    # Connections:
    #   - File: backend/ums_smart_revenue/db/security_models.py -> Covering index.
    # ========================================================================
    def get_effective_rate(
        self,
        *,
        tenant_id: UUID,
        source_account_id: str,
        as_of: date,
    ) -> UsWithholdingRateSnapshot | None:
        """Return the latest revision for one explicit tenant/account/date."""
        source_account_id = _canonical_source_account_id(source_account_id)
        try:
            row = self._session.scalar(
                select(UsWithholdingRateConfigORM)
                .where(
                    UsWithholdingRateConfigORM.tenant_id == tenant_id,
                    UsWithholdingRateConfigORM.source_account_id == source_account_id,
                    UsWithholdingRateConfigORM.effective_from <= as_of,
                )
                .order_by(
                    UsWithholdingRateConfigORM.effective_from.desc(),
                    UsWithholdingRateConfigORM.revision.desc(),
                )
                .limit(1)
            )
        except SQLAlchemyError as exc:
            raise UsWithholdingConfigStorageError(
                "Unable to load US withholding configuration"
            ) from exc
        if row is None:
            return None
        return UsWithholdingRateSnapshot(
            source_account_id=row.source_account_id,
            rate=Decimal(row.rate).quantize(_RATE_QUANTUM),
            account_type=row.account_type,
            effective_from=row.effective_from,
            revision=row.revision,
            confirmed_by_user_id=row.confirmed_by_user_id,
        )

    # ========================================================================
    # Purpose: Allocate the next append-only revision for one tenant/account/
    #   effective date, serializing PostgreSQL writers on a transaction lock.
    # Database/ORM: us_withholding_rate_configs; pg_advisory_xact_lock.
    # Standards: Parameterized lock key, tenant/account filter, and DB unique fallback.
    # Blast Radius: Finance estimate history ordering and concurrent writes.
    # Connections:
    #   - File: backend/ums_smart_revenue/db/security_models.py -> Revision unique key.
    #   - File: tests/finance/test_us_withholding_config.py -> Latest-wins proof.
    # ========================================================================
    def _next_revision(
        self,
        *,
        tenant_id: UUID,
        source_account_id: str,
        effective_from: date,
    ) -> int:
        """Allocate the next same-date revision in the caller transaction."""
        if self._session.get_bind().dialect.name == "postgresql":
            lock_key = (
                f"ums:withholding-rate:{tenant_id}:{source_account_id}:{effective_from.isoformat()}"
            )
            self._session.execute(
                select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
            )
            # FIX (review P2): MAX(revision) is a separate statement, so it only
            # observes the peer writer's committed row under READ COMMITTED.
            # Under REPEATABLE READ/SERIALIZABLE this transaction's snapshot
            # predates the lock wait, the stale MAX allocates a duplicate, and
            # only the DB unique key converts it to a conflict. Refuse the
            # higher isolation levels explicitly instead of silently depending
            # on the constraint for correctness.
            isolation = self._session.scalar(
                select(func.current_setting("transaction_isolation"))
            )
            if isolation != "read committed":
                raise UsWithholdingConfigStorageError(
                    "US withholding revision allocation requires READ COMMITTED isolation"
                )
        current_revision = self._session.scalar(
            select(func.max(UsWithholdingRateConfigORM.revision)).where(
                UsWithholdingRateConfigORM.tenant_id == tenant_id,
                UsWithholdingRateConfigORM.source_account_id == source_account_id,
                UsWithholdingRateConfigORM.effective_from == effective_from,
            )
        )
        return int(current_revision or 0) + 1

    # ========================================================================
    # Purpose: Append one operator-confirmed rate revision without overwriting
    #   prior evidence.
    # Database/ORM: INSERT into us_withholding_rate_configs in caller transaction.
    # Standards: Numeric/type validation, explicit tenant/account, typed errors.
    # Blast Radius: Estimate config history only; API/audit wiring is a non-goal.
    # Connections:
    #   - File: tests/finance/test_us_withholding_config.py -> Revision/validation proof.
    # ========================================================================
    def record_confirmed_rate(
        self,
        *,
        tenant_id: UUID,
        source_account_id: str,
        effective_from: date,
        rate: Decimal,
        account_type: str,
        confirmed_by_user_id: UUID,
    ) -> UsWithholdingRateSnapshot:
        """Append and flush one validated, operator-confirmed rate revision."""
        source_account_id = _canonical_source_account_id(source_account_id)
        validate_us_withholding_rate(rate)
        rate = rate.quantize(_RATE_QUANTUM)
        if account_type not in {"business", "individual"}:
            raise InvalidUsWithholdingConfigError("account_type must be 'business' or 'individual'")
        try:
            revision = self._next_revision(
                tenant_id=tenant_id,
                source_account_id=source_account_id,
                effective_from=effective_from,
            )
            row = UsWithholdingRateConfigORM(
                id=uuid4(),
                tenant_id=tenant_id,
                source_account_id=source_account_id,
                effective_from=effective_from,
                revision=revision,
                rate=rate,
                account_type=account_type,
                confirmed_by_user_id=confirmed_by_user_id,
            )
            self._session.add(row)
            self._session.flush()
        except IntegrityError as exc:
            raise UsWithholdingConfigConflictError(
                "US withholding configuration conflicts with stored state"
            ) from exc
        except SQLAlchemyError as exc:
            raise UsWithholdingConfigStorageError(
                "Unable to store US withholding configuration"
            ) from exc
        return UsWithholdingRateSnapshot(
            source_account_id=row.source_account_id,
            rate=Decimal(row.rate).quantize(_RATE_QUANTUM),
            account_type=row.account_type,
            effective_from=row.effective_from,
            revision=row.revision,
            confirmed_by_user_id=row.confirmed_by_user_id,
        )
