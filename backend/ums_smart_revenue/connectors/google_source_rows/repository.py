"""Storage repositories for Google revenue source rows + ISO currencies.

SqlAlchemyCurrenciesRepository is intentionally read-only — flipping the
is_supported flag belongs to a later admin API with its own audit story
(spec section 6). SqlAlchemyGoogleRevenueSourceRowRepository exposes
storage primitives only: idempotent upsert, tenant-scoped list,
channel/month list, exact source-key lookup. No conversion, no provider
chain.
"""

import json
import re
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select
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
from ums_smart_revenue.db.report_models import RawReportFileORM
from ums_smart_revenue.db.source_models import (
    CurrencyORM,
    GoogleRevenueSourceRowORM,
)
from ums_smart_revenue.db.tenant_models import TenantORM

# YYYY-MM with a calendar-valid month (01-12). Mirrors the DB CHECK
# ck_google_revenue_source_rows_report_month_format at the typed-validation
# boundary so a malformed report_month surfaces as a typed error, not a raw
# DB IntegrityError on flush.
# FIX: [0-9] (ASCII) not \d. \d also matches Unicode decimal digits (e.g.
# the Arabic-Indic "٢٠٢٦-٠٤"), which would pass this guard but fail the DB
# CHECK (substr BETWEEN '0' AND '9', ASCII-only) — leaking a raw
# IntegrityError instead of the typed GoogleRevenueSourceRowValidationError.
_REPORT_MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])\Z")


def _require_str_keys(value: object) -> None:
    """Recursively assert every mapping key inside a raw_payload is a str.

    json.dumps silently coerces non-string dict keys (e.g. int 1 -> "1"), which
    would mutate caller-provided audit evidence and can collide two distinct
    keys into one. Enforcing str keys keeps the dict[str, object] contract
    intact before the JSON/JSONB write rather than letting the shape change
    silently on serialisation.
    """
    if isinstance(value, dict):
        for key, sub_value in value.items():
            if not isinstance(key, str):
                raise GoogleRevenueSourceRowValidationError(
                    f"raw_payload keys must be strings, got {type(key).__name__}"
                )
            _require_str_keys(sub_value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _require_str_keys(item)


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
#            CHECK / FK violations. Validation covers: required + nullable
#            string column types, source_row_key length (== 64),
#            source_system membership, value_kind membership, finite
#            non-negative amount within Numeric(20, 6) scale, report_month
#            ASCII format + agreement with the period's calendar month,
#            date-only (not datetime) period bounds, JSON-serialisable
#            raw_payload dict, and currency_code existence in the
#            currencies reference table. Provenance (raw_file_id/imported_by)
#            is preserved via COALESCE on conflict so a later replay that
#            omits it cannot erase audit lineage.
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
        # Pre-check FK references at the typed-validation boundary so a bad
        # tenant, currency, or raw file surfaces as the repository's typed
        # GoogleRevenueSourceRowValidationError instead of the opaque DB FK
        # violation raised on flush (same contract the docstring promises for
        # currency_code). _require_raw_file additionally enforces tenant scope
        # so a row can never be linked to another tenant's raw evidence file.
        self._require_tenant(tenant_id)
        self._require_currencies({r.currency_code for r in validated})
        self._require_raw_file(raw_file_id, tenant_id)

        written: list[GoogleRevenueSourceRowEntry] = []
        dialect_insert = self._dialect_insert(
            self._session.get_bind().dialect.name
        )
        for row in validated:
            # Explicit Python-side UUID keeps SQLite tests (no
            # gen_random_uuid() function) consistent with PostgreSQL
            # production; on conflict the existing id is preserved
            # because id is not in the set_ payload.
            insert_stmt = dialect_insert(GoogleRevenueSourceRowORM).values(
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
            statement = insert_stmt.on_conflict_do_update(
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
                    # FIX: COALESCE(new, existing) for provenance instead of an
                    # unconditional overwrite. A later replay/import path that
                    # lacks raw_file_id/imported_by (passes None) must NOT erase
                    # the audit lineage recorded on the original ingest; a
                    # genuinely new file/importer (non-None) still replaces it.
                    "raw_file_id": func.coalesce(
                        insert_stmt.excluded.raw_file_id,
                        GoogleRevenueSourceRowORM.raw_file_id,
                    ),
                    "raw_payload": row.raw_payload,
                    "imported_by": func.coalesce(
                        insert_stmt.excluded.imported_by,
                        GoogleRevenueSourceRowORM.imported_by,
                    ),
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
        # Guard every required string column first. The first four are also used
        # in type-assuming Python ops (frozenset membership, len(), the
        # {r.currency_code} set comprehension), where a non-str would raise a raw
        # TypeError; the rest are written straight into the INSERT, where a non-str
        # would surface later as a driver/DB error. Either way a malformed caller
        # that ignores the ParsedSourceRow annotations must hit this method's
        # typed-error contract, not a raw exception.
        for field_name in (
            "source_system",
            "value_kind",
            "source_row_key",
            "currency_code",
            "source_account_id",
            "report_type",
            "report_month",
            "metric_key",
        ):
            if not isinstance(getattr(row, field_name), str):
                raise GoogleRevenueSourceRowValidationError(
                    f"{field_name} must be a str"
                )
        # FIX: Nullable text columns were previously unchecked. None is allowed,
        # but a non-str/non-None value (e.g. a list/dict from a loose non-parser
        # caller) would otherwise be written straight into the INSERT and
        # surface as a raw driver/DB error instead of this typed contract.
        for nullable_field in (
            "content_owner_id",
            "youtube_channel_id",
            "source_report_id",
        ):
            value = getattr(row, nullable_field)
            if value is not None and not isinstance(value, str):
                raise GoogleRevenueSourceRowValidationError(
                    f"{nullable_field} must be a str or None"
                )
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
        # FIX: amount_native maps to Numeric(20, 6). PostgreSQL silently ROUNDS
        # a value whose scale exceeds 6 fractional digits on write, which would
        # alter the source-reported amount (CLAUDE.md rule 4: preserve every
        # financial number's source value exactly). Reject >6 fractional digits
        # at the boundary so the stored value can never diverge from the source.
        # (exponent is always int here — the is_finite() guard above rules out
        # the special 'n'/'N'/'F' exponents of NaN/Infinity.)
        exponent = row.amount_native.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -6:
            raise GoogleRevenueSourceRowValidationError(
                "amount_native must not exceed 6 fractional digits "
                f"(column is Numeric(20, 6)), got {row.amount_native}"
            )
        # FIX: bound the integer part too. Numeric(20, 6) allows 20 total
        # significant digits with 6 fractional -> at most 14 integer digits; the
        # scale guard above only caps the fraction. A value like 10**14 would
        # otherwise pass here and fail later as a raw PostgreSQL numeric-overflow
        # on INSERT, escaping this typed contract. Decimal.adjusted() is the
        # exponent of the most-significant digit, so >= 14 means >14 integer
        # digits (i.e. magnitude >= 10**14).
        # FIX: exempt exact zero first. A zero with a positive exponent (e.g.
        # Decimal('0E+20'), which parsers can emit) has adjusted()==20 even
        # though its value is 0 and fits Numeric(20, 6); is_zero() is True for
        # any zero regardless of exponent, so a valid zero is no longer rejected.
        if not row.amount_native.is_zero() and row.amount_native.adjusted() >= 14:
            raise GoogleRevenueSourceRowValidationError(
                "amount_native must not exceed 14 integer digits "
                f"(column is Numeric(20, 6)), got {row.amount_native}"
            )
        if not isinstance(row.raw_payload, dict):
            raise GoogleRevenueSourceRowValidationError(
                "raw_payload must be a dict"
            )
        # FIX: raw_payload is written to a JSON/JSONB column.
        #  - _require_str_keys: json.dumps alone is NOT a sufficient shape check
        #    because it silently coerces non-str keys to strings (and can
        #    key-collide), so enforce the dict[str, object] key contract first.
        #  - allow_nan=False: default json.dumps emits NaN/Infinity tokens that
        #    PostgreSQL JSONB then rejects on write; fail here instead so the
        #    error stays on the typed boundary. The except also catches
        #    non-serialisable values (set, custom object).
        _require_str_keys(row.raw_payload)
        try:
            json.dumps(row.raw_payload, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise GoogleRevenueSourceRowValidationError(
                "raw_payload must be JSON-serialisable with finite numbers"
            ) from exc
        # Mirror the report_month + period-order DB CHECKs at the typed-validation
        # boundary: a malformed ParsedSourceRow from any non-parser caller (or a
        # future parser regression) must raise GoogleRevenueSourceRowValidationError
        # here, not a raw DB IntegrityError on flush.
        if not _REPORT_MONTH_RE.match(row.report_month):
            raise GoogleRevenueSourceRowValidationError(
                f"report_month must be YYYY-MM with month 01-12, got {row.report_month!r}"
            )
        # FIX: datetime is a subclass of date, so a bare isinstance(..., date)
        # lets a timestamp through; the DATE columns would then silently
        # truncate it. period_start/period_end are date-only by contract, so
        # reject datetime explicitly rather than coercing caller-provided values.
        if (
            not isinstance(row.period_start, date)
            or not isinstance(row.period_end, date)
            or isinstance(row.period_start, datetime)
            or isinstance(row.period_end, datetime)
        ):
            raise GoogleRevenueSourceRowValidationError(
                "period_start and period_end must be date values (not datetime)"
            )
        if row.period_end < row.period_start:
            raise GoogleRevenueSourceRowValidationError(
                "period_end must be on or after period_start"
            )
        # report_month must match the period it summarises, and the period must
        # stay within that single calendar month: the parsers derive
        # report_month from period_start and reject cross-month ranges, so a
        # mismatched bucket (e.g. report_month '2026-04' with a May period) means
        # a non-parser caller would misattribute revenue to the wrong month.
        period_month = f"{row.period_start.year:04d}-{row.period_start.month:02d}"
        if row.report_month != period_month or (row.period_end.year, row.period_end.month) != (
            row.period_start.year,
            row.period_start.month,
        ):
            raise GoogleRevenueSourceRowValidationError(
                f"report_month {row.report_month!r} must match a single calendar-month "
                f"period starting {row.period_start.isoformat()}"
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

    # ========================================================================
    # Purpose: Pre-validate the tenant FK before any write so an unknown
    #          tenant_id surfaces as the typed validation error instead of a
    #          raw DB FK IntegrityError on flush.
    # Database/ORM: tenants (TenantORM) — existence read only.
    # Standards: Typed-error contract; no mutation. Echoes only the caller's
    #            own tenant_id (no other-tenant data) in the message.
    # Blast Radius: Tenant isolation / audit; source-of-truth write gate.
    # ========================================================================
    def _require_tenant(self, tenant_id: UUID) -> None:
        # FIX: tenant_id was written straight into the INSERT with no pre-check,
        # so an unknown tenant failed later as a raw FK IntegrityError rather
        # than this repository's typed validation contract.
        exists = self._session.scalar(
            select(TenantORM.id).where(TenantORM.id == tenant_id)
        )
        if exists is None:
            raise GoogleRevenueSourceRowValidationError(
                f"unknown tenant_id: {tenant_id}"
            )

    # ========================================================================
    # Purpose: Pre-validate the raw-file provenance FK AND its tenant scope
    #          before any write. A None raw_file_id is allowed (provenance is
    #          optional). A present id must reference a raw_report_files row
    #          owned by the SAME tenant.
    # Database/ORM: raw_report_files (RawReportFileORM) — existence read only.
    # Standards: Typed-error contract; no mutation. Missing-file and
    #            wrong-tenant collapse to one "not found for this tenant"
    #            error so the check is not a cross-tenant existence oracle.
    # Blast Radius: Tenant isolation + audit-lineage integrity (a row must not
    #            link to another tenant's raw evidence file).
    # ========================================================================
    def _require_raw_file(self, raw_file_id: UUID | None, tenant_id: UUID) -> None:
        # FIX: upsert_many wrote any provided raw_file_id directly; the schema
        # only checks the file exists, not that it belongs to tenant_id, so a
        # caller could link one tenant's rows to another tenant's raw file
        # (provenance/audit corruption + cross-tenant linkage). Scope the
        # existence check to the tenant and fail closed otherwise.
        if raw_file_id is None:
            return
        match = self._session.scalar(
            select(RawReportFileORM.id).where(
                RawReportFileORM.id == raw_file_id,
                RawReportFileORM.tenant_id == tenant_id,
            )
        )
        if match is None:
            raise GoogleRevenueSourceRowValidationError(
                f"raw_file_id {raw_file_id} not found for this tenant"
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
            # FIX: Deep-copy on the read path too — the previous shallow dict()
            # let a caller mutate entry.raw_payload["dimensions"] (a nested
            # object) and alias it back into the live ORM row in the session.
            raw_payload=deepcopy(row.raw_payload or {}),
            imported_by=str(row.imported_by) if row.imported_by else None,
            ingested_at=row.ingested_at,
        )
