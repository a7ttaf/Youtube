"""Storage repositories for Google revenue source rows + ISO currencies.

SqlAlchemyCurrenciesRepository is intentionally read-only — flipping the
is_supported flag belongs to a later admin API with its own audit story
(spec section 6). SqlAlchemyGoogleRevenueSourceRowRepository exposes
storage primitives only: idempotent upsert, tenant-scoped list,
channel/month list, exact source-key lookup. No conversion, no provider
chain.
"""

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    ALLOWED_SOURCE_SYSTEMS,
    ALLOWED_VALUE_KINDS,
    SOURCE_ROW_KEY_LENGTH,
    GoogleRevenueSourceRowEntry,
    GoogleRevenueSourceRowError,
    GoogleRevenueSourceRowValidationError,
    IsoCurrency,
    ParsedSourceRow,
)
from ums_smart_revenue.db.source_models import (
    CurrencyORM,
    GoogleRevenueSourceRowORM,
)


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


# ============================================================================
# Purpose: Storage primitives for Google source-reported revenue rows.
# Database/ORM: google_revenue_source_rows (GoogleRevenueSourceRowORM),
#               currencies (FK pre-check via CurrencyORM lookup).
# Standards: Idempotent upsert via dialect-aware ON CONFLICT
#            (sqlite_insert / postgresql_insert) keyed on
#            (tenant_id, source_system, source_row_key). Domain-level
#            validation runs BEFORE any write so the typed error contract
#            (GoogleRevenueSourceRowValidationError) replaces opaque DB
#            CHECK / FK violations. Validation covers: source_row_key
#            length (== 64), source_system membership, value_kind
#            membership, non-negative amount, raw_payload dict type, and
#            currency_code existence in the currencies reference table.
#            Tenant ID is a positional UUID arg on every method; the
#            repository does NOT read TENANT_CTX directly so callers
#            retain explicit control of scope.
# Blast Radius: Source-of-truth writes for downstream finance ingestion.
#               No graph projection impact detected.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google_source_parsers/
#     -> emits ParsedSourceRow instances consumed by upsert_many.
#   - File: backend/ums_smart_revenue/finance/exchange_rates.py
#     -> dialect-insert helper pattern reference (_dialect_insert).
# ============================================================================
class SqlAlchemyGoogleRevenueSourceRowRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_many(
        self,
        tenant_id: UUID,
        rows: Iterable[ParsedSourceRow],
        *,
        raw_file_id: UUID | None,
        imported_by: UUID | None,
    ) -> list[GoogleRevenueSourceRowEntry]:
        materialised = list(rows)
        if not materialised:
            return []
        # Defensive copy of raw_payload via _validate prevents caller mutation
        # from racing the pending flush; the reference exchange_rates.py pipeline
        # follows the same pattern in _normalize_rate_input.
        validated = [self._validate(r) for r in materialised]
        # Pre-check currency existence so callers receive a typed domain
        # error instead of the raw FK violation surfaced by the DB.
        self._require_currencies({r.currency_code for r in validated})

        written: list[GoogleRevenueSourceRowEntry] = []
        dialect_insert = self._dialect_insert(
            self._session.get_bind().dialect.name
        )
        for row in validated:
            # Explicit Python-side UUID keeps SQLite tests (no
            # gen_random_uuid() function) consistent with PostgreSQL
            # production; on conflict the existing id is preserved
            # because id is not in the set_ payload.
            statement = dialect_insert(GoogleRevenueSourceRowORM).values(
                id=uuid4(),
                tenant_id=tenant_id,
                source_system=row.source_system,
                source_row_key=row.source_row_key,
                source_account_id=row.source_account_id,
                content_owner_id=row.content_owner_id,
                youtube_channel_id=row.youtube_channel_id,
                report_type=row.report_type,
                report_month=row.report_month,
                period_start=row.period_start,
                period_end=row.period_end,
                metric_key=row.metric_key,
                value_kind=row.value_kind,
                amount_native=row.amount_native,
                currency_code=row.currency_code,
                source_report_id=row.source_report_id,
                raw_file_id=raw_file_id,
                raw_payload=row.raw_payload,
                imported_by=imported_by,
            )
            statement = statement.on_conflict_do_update(
                index_elements=[
                    GoogleRevenueSourceRowORM.tenant_id,
                    GoogleRevenueSourceRowORM.source_system,
                    GoogleRevenueSourceRowORM.source_row_key,
                ],
                set_={
                    "source_account_id": row.source_account_id,
                    "content_owner_id": row.content_owner_id,
                    "youtube_channel_id": row.youtube_channel_id,
                    "report_type": row.report_type,
                    "report_month": row.report_month,
                    "period_start": row.period_start,
                    "period_end": row.period_end,
                    "metric_key": row.metric_key,
                    "value_kind": row.value_kind,
                    "amount_native": row.amount_native,
                    "currency_code": row.currency_code,
                    "source_report_id": row.source_report_id,
                    "raw_file_id": raw_file_id,
                    "raw_payload": row.raw_payload,
                    "imported_by": imported_by,
                },
            ).returning(GoogleRevenueSourceRowORM)
            orm_row = self._session.execute(statement).scalar_one()
            written.append(self._to_entry(orm_row))
        self._session.flush()
        return written

    def list(
        self,
        tenant_id: UUID,
        *,
        report_month: str | None = None,
        source_system: str | None = None,
    ) -> list[GoogleRevenueSourceRowEntry]:
        stmt = select(GoogleRevenueSourceRowORM).where(
            GoogleRevenueSourceRowORM.tenant_id == tenant_id
        )
        if report_month is not None:
            stmt = stmt.where(
                GoogleRevenueSourceRowORM.report_month == report_month
            )
        if source_system is not None:
            stmt = stmt.where(
                GoogleRevenueSourceRowORM.source_system == source_system
            )
        rows = self._session.scalars(
            stmt.order_by(GoogleRevenueSourceRowORM.ingested_at)
        ).all()
        return [self._to_entry(r) for r in rows]

    def list_for_channel(
        self,
        tenant_id: UUID,
        *,
        youtube_channel_id: str,
        report_month: str,
    ) -> list[GoogleRevenueSourceRowEntry]:
        rows = self._session.scalars(
            select(GoogleRevenueSourceRowORM)
            .where(
                GoogleRevenueSourceRowORM.tenant_id == tenant_id,
                GoogleRevenueSourceRowORM.youtube_channel_id
                == youtube_channel_id,
                GoogleRevenueSourceRowORM.report_month == report_month,
            )
            .order_by(GoogleRevenueSourceRowORM.source_system)
        ).all()
        return [self._to_entry(r) for r in rows]

    def get_exact(
        self,
        tenant_id: UUID,
        *,
        source_system: str,
        source_row_key: str,
    ) -> GoogleRevenueSourceRowEntry | None:
        row = self._session.scalar(
            select(GoogleRevenueSourceRowORM).where(
                GoogleRevenueSourceRowORM.tenant_id == tenant_id,
                GoogleRevenueSourceRowORM.source_system == source_system,
                GoogleRevenueSourceRowORM.source_row_key == source_row_key,
            )
        )
        return self._to_entry(row) if row is not None else None

    def _validate(self, row: ParsedSourceRow) -> ParsedSourceRow:
        if row.source_system not in ALLOWED_SOURCE_SYSTEMS:
            raise GoogleRevenueSourceRowValidationError(
                f"unknown source_system: {row.source_system!r}"
            )
        if row.value_kind not in ALLOWED_VALUE_KINDS:
            raise GoogleRevenueSourceRowValidationError(
                f"unknown value_kind: {row.value_kind!r}"
            )
        if len(row.source_row_key) != SOURCE_ROW_KEY_LENGTH:
            raise GoogleRevenueSourceRowValidationError(
                f"source_row_key must be {SOURCE_ROW_KEY_LENGTH} chars "
                f"(got {len(row.source_row_key)})"
            )
        if not isinstance(row.amount_native, Decimal):
            # Guard the type first: .is_finite()/comparison on a non-Decimal
            # (None, str, ...) would raise AttributeError/TypeError and bypass
            # this typed validation contract.
            raise GoogleRevenueSourceRowValidationError(
                "amount_native must be a Decimal"
            )
        if not row.amount_native.is_finite() or row.amount_native < 0:
            # Guard NaN/Infinity at the repository boundary too: Decimal("NaN")
            # < 0 raises InvalidOperation, which would bypass this typed
            # validation error even though parsers already reject non-finite
            # amounts upstream.
            raise GoogleRevenueSourceRowValidationError(
                "amount_native must be a finite Decimal >= 0"
            )
        if not isinstance(row.raw_payload, dict):
            raise GoogleRevenueSourceRowValidationError(
                "raw_payload must be a dict"
            )
        # Deep-copy: raw_payload holds nested dicts (date_range, dimensions,
        # metrics), so a shallow dict() copy would still let a caller mutate
        # those nested objects after upsert_many and change what was persisted.
        return replace(row, raw_payload=deepcopy(row.raw_payload))

    def _require_currencies(self, codes: set[str]) -> None:
        if not codes:
            return
        present = set(
            self._session.scalars(
                select(CurrencyORM.code).where(CurrencyORM.code.in_(codes))
            ).all()
        )
        missing = codes - present
        if missing:
            raise GoogleRevenueSourceRowValidationError(
                f"unknown currency code(s): {sorted(missing)}"
            )

    @staticmethod
    def _dialect_insert(dialect_name: str):
        if dialect_name == "sqlite":
            return sqlite_insert
        if dialect_name == "postgresql":
            return postgresql_insert
        raise GoogleRevenueSourceRowError(
            f"unsupported dialect for source-row upsert: {dialect_name}"
        )

    @staticmethod
    def _to_entry(
        row: GoogleRevenueSourceRowORM,
    ) -> GoogleRevenueSourceRowEntry:
        return GoogleRevenueSourceRowEntry(
            id=str(row.id),
            tenant_id=str(row.tenant_id),
            source_system=row.source_system,
            source_row_key=row.source_row_key,
            source_account_id=row.source_account_id,
            content_owner_id=row.content_owner_id,
            youtube_channel_id=row.youtube_channel_id,
            report_type=row.report_type,
            report_month=row.report_month,
            period_start=row.period_start,
            period_end=row.period_end,
            metric_key=row.metric_key,
            value_kind=row.value_kind,
            amount_native=row.amount_native,
            currency_code=row.currency_code,
            source_report_id=row.source_report_id,
            raw_file_id=str(row.raw_file_id) if row.raw_file_id else None,
            # Deep-copy on the read path too: a shallow dict() would let a
            # caller mutate entry.raw_payload["dimensions"] (a nested object)
            # and alias it back into the live ORM row in the session.
            raw_payload=deepcopy(row.raw_payload or {}),
            imported_by=str(row.imported_by) if row.imported_by else None,
            ingested_at=row.ingested_at,
        )
