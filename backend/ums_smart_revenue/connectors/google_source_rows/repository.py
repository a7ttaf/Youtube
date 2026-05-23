"""Storage repositories for Google revenue source rows + ISO currencies.

SqlAlchemyCurrenciesRepository is intentionally read-only — flipping the
is_supported flag belongs to a later admin API with its own audit story
(spec section 6). SqlAlchemyGoogleRevenueSourceRowRepository exposes
storage primitives only: idempotent upsert, tenant-scoped list,
channel/month list, exact source-key lookup. No conversion, no provider
chain.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    IsoCurrency,
)
from ums_smart_revenue.db.source_models import CurrencyORM


# ============================================================================
# Purpose: Read-only access to platform-wide ISO 4217 currency reference data.
# Database/ORM: currencies (CurrencyORM).
# Standards: Pure read repository; mutation paths intentionally absent —
#            future admin write API will own its own audit/permission story
#            per spec section 6 and must NOT be retrofitted here.
# Blast Radius: Reference data only. No graph projection impact detected.
# Connections:
#   - File: backend/ums_smart_revenue/db/source_models.py -> CurrencyORM.
#   - Spec: Docs/superpowers/specs/2026-05-23-spec-b1-google-revenue-source-ingestion-design.md
#     -> read-only contract rationale (section 6).
# ============================================================================
class SqlAlchemyCurrenciesRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_all(self) -> list[IsoCurrency]:
        rows = self._session.scalars(
            select(CurrencyORM).order_by(CurrencyORM.code)
        ).all()
        return [self._to_entry(row) for row in rows]

    def list_supported(self) -> list[IsoCurrency]:
        rows = self._session.scalars(
            select(CurrencyORM)
            .where(CurrencyORM.is_supported.is_(True))
            .order_by(CurrencyORM.code)
        ).all()
        return [self._to_entry(row) for row in rows]

    def get(self, code: str) -> IsoCurrency | None:
        row = self._session.get(CurrencyORM, code)
        return self._to_entry(row) if row is not None else None

    @staticmethod
    def _to_entry(row: CurrencyORM) -> IsoCurrency:
        return IsoCurrency(
            code=row.code,
            numeric_code=row.numeric_code,
            name=row.name,
            minor_unit=row.minor_unit,
            is_supported=row.is_supported,
            activated_at=row.activated_at,
        )
