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
from typing import Final, List  # noqa: UP035
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
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
    SourceRowUpsertResult,
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
_REQUIRED_TEXT_FIELDS = (
    "source_system",
    "value_kind",
    "source_row_key",
    "currency_code",
    "source_account_id",
    "report_type",
    "report_month",
    "metric_key",
)
_NULLABLE_TEXT_FIELDS = (
    "content_owner_id",
    "youtube_channel_id",
    "source_report_id",
)
"""Module for validating and comparing parsed Google source rows.

Provides helper functions to enforce correct types, formats, and ranges
for various fields of ParsedSourceRow before insertion into the database.
"""

_AMOUNT_NATIVE_SCALE = 6
_AMOUNT_NATIVE_INTEGER_DIGITS = 14


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


def _validate_text_fields(row: ParsedSourceRow) -> None:
    """Ensure required and nullable text fields are of correct types.

    Required text fields must be non-null strings. Nullable text fields may
    be None or a string, but no other types are allowed.
    """
    # Guard text columns before membership checks, len(), set construction, or
    # INSERT binding can leak raw TypeError/driver errors for loose callers.
    for field_name in _REQUIRED_TEXT_FIELDS:
        if not isinstance(getattr(row, field_name), str):
            raise GoogleRevenueSourceRowValidationError(
                f"{field_name} must be a str"
            )

    # FIX: Nullable text columns were previously unchecked. None is allowed,
    # but a non-str/non-None value must still fail at this typed boundary.
    for nullable_field in _NULLABLE_TEXT_FIELDS:
        value = getattr(row, nullable_field)
        if value is not None and not isinstance(value, str):
            raise GoogleRevenueSourceRowValidationError(
                f"{nullable_field} must be a str or None"
            )


def _validate_source_identity(row: ParsedSourceRow) -> None:
    """Validate the source_system, value_kind, and source_row_key attributes.

    Ensures that source_system and value_kind are allowed constants, and that
    source_row_key has the expected fixed length.
    """
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


def _validate_amount_native(row: ParsedSourceRow) -> None:
    """Validate that amount_native is a non-negative finite Decimal within scale and
    precision limits.

    Ensures amount_native is a Decimal, is finite and >= 0, does not exceed
    the _AMOUNT_NATIVE_SCALE fractional digits, and fits within
    _AMOUNT_NATIVE_INTEGER_DIGITS integer digits.
    """
    if not isinstance(row.amount_native, Decimal):
        raise GoogleRevenueSourceRowValidationError(
            "amount_native must be a Decimal"
        )
    if not row.amount_native.is_finite():
        raise GoogleRevenueSourceRowValidationError(
            "amount_native must be a finite Decimal >= 0"
        )
    if row.amount_native < 0:
        raise GoogleRevenueSourceRowValidationError(
            "amount_native must be a finite Decimal >= 0"
        )

    # FIX: amount_native maps to Numeric(20, 6). Reject values the database
    # would round or overflow so source-reported finance values stay exact.
    if row.amount_native.as_tuple().exponent < -_AMOUNT_NATIVE_SCALE:
        raise GoogleRevenueSourceRowValidationError(
            "amount_native must not exceed 6 fractional digits "
            f"(column is Numeric(20, 6)), got {row.amount_native}"
        )
    if (
        not row.amount_native.is_zero()
        and row.amount_native.adjusted() >= _AMOUNT_NATIVE_INTEGER_DIGITS
    ):
        raise GoogleRevenueSourceRowValidationError(
            "amount_native must not exceed 14 integer digits "
            f"(column is Numeric(20, 6)), got {row.amount_native}"
        )


def _validate_raw_payload(row: ParsedSourceRow) -> None:
    """Validate raw_payload is a dict with string keys and JSON-serialisable finite values.

    Ensures raw_payload is a dict, all keys are strings, and the structure
    can be serialized to JSON without NaN or infinite numbers.
    """
    if not isinstance(row.raw_payload, dict):
        raise GoogleRevenueSourceRowValidationError(
            "raw_payload must be a dict"
        )
    # FIX: raw_payload is written to JSON/JSONB; require string keys and finite,
    # serialisable values before the DB adapter can mutate or reject the shape.
    _require_str_keys(row.raw_payload)
    try:
        json.dumps(row.raw_payload, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise GoogleRevenueSourceRowValidationError(
            "raw_payload must be JSON-serialisable with finite numbers"
        ) from exc


def _is_date_only(value: object) -> bool:
    """Return True if the value is a date instance without a time component.

    Distinguishes plain date objects from datetime instances with time data.
    """
    return isinstance(value, date) and not isinstance(value, datetime)


# Content fields that the upsert SET clause writes from the parser-owned input.
# Provenance (raw_file_id, imported_by) is intentionally excluded: a re-import
# from a fresh raw file that carries the same values is "unchanged content,
# refreshed evidence" — not a value update. Conflict keys (source_system,
# source_row_key) are excluded because the existing row was matched on them.
_CONTENT_COMPARISON_FIELDS = (
    "source_account_id",
    "content_owner_id",
    "youtube_channel_id",
    "report_type",
    "report_month",
    "period_start",
    "period_end",
    "metric_key",
    "value_kind",
    "amount_native",
    "currency_code",
    "source_report_id",
    "raw_payload",
)

# Upper bound on how many duplicate keys are named in the batch-duplicate
# error. Keeps the message (and any log line carrying it) bounded for a
# pathological batch while still naming the offenders; a real parser batch
# for one (account, month, report_type) holds at most a handful of dupes.
_MAX_DUPLICATE_KEYS_IN_ERROR: Final[int] = 10


def _content_matches_existing(
    row: ParsedSourceRow, existing: GoogleRevenueSourceRowORM
) -> bool:
    """Return True when every parser-owned content field matches the persisted row.

    Used by SqlAlchemyGoogleRevenueSourceRowRepository.upsert_many to classify
    an input row as ``unchanged`` (this returns True) versus ``updated`` (False
    when the row already exists). Decimal equality is numeric (so 100 == 100.0),
    and dict equality is deep (so raw_payload differences are detected).
    """
    return all(getattr(row, field) == getattr(existing, field) for field in _CONTENT_COMPARISON_FIELDS)


def _validate_report_period(row: ParsedSourceRow) -> None:
    """Validate report_month and period_start/period_end alignment and ordering.

    Ensures report_month matches YYYY-MM, period_start and period_end are date-only,
    period_end is not before period_start, and the month fields align to a single
    calendar month period.
    """
    if not _REPORT_MONTH_RE.match(row.report_month):
        raise GoogleRevenueSourceRowValidationError(
            f"report_month must be YYYY-MM with month 01-12, got {row.report_month!r}"
        )
    # FIX: datetime is a date subclass, but DATE columns would silently truncate
    # timestamps. The repository contract requires date-only period bounds.
    if not all(_is_date_only(value) for value in (row.period_start, row.period_end)):
        raise GoogleRevenueSourceRowValidationError(
            "period_start and period_end must be date values (not datetime)"
        )
    if row.period_end < row.period_start:
        raise GoogleRevenueSourceRowValidationError(
            "period_end must be on or after period_start"
        )

    period_month = f"{row.period_start.year:04d}-{row.period_start.month:02d}"
    period_end_month = (row.period_end.year, row.period_end.month)
    if row.report_month != period_month or period_end_month != (
        row.period_start.year,
        row.period_start.month,
    ):
        raise GoogleRevenueSourceRowValidationError(
            f"report_month {row.report_month!r} must match a single calendar-month "
            f"period starting {row.period_start.isoformat()}"
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
    """Read-only repository for ISO 4217 currency reference data using SQLAlchemy.

    Provides methods to list all currencies, list supported currencies,
    and retrieve a currency by its ISO code.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_all(self) -> list[IsoCurrency]:
        """Return all ISO currency entries from the database, ordered by code."""
        rows = self._session.scalars(
            select(CurrencyORM).order_by(CurrencyORM.code)
        ).all()
        return [self._to_entry(row) for row in rows]

    def list_supported(self) -> list[IsoCurrency]:
        """Return only supported ISO currency entries from the database, ordered by code."""
        rows = self._session.scalars(
            select(CurrencyORM)
            .where(CurrencyORM.is_supported.is_(True))
            .order_by(CurrencyORM.code)
        ).all()
        return [self._to_entry(row) for row in rows]

    def get(self, code: str) -> IsoCurrency | None:
        """Retrieve a single ISO currency entry by code, or None if not found."""
        row = self._session.get(CurrencyORM, code)
        return self._to_entry(row) if row is not None else None

    @staticmethod
    def _to_entry(row: CurrencyORM) -> IsoCurrency:
        """Convert a CurrencyORM row into an IsoCurrency domain object."""
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
#            This module implements the SqlAlchemy repository for Google revenue source rows,
#            providing methods to upsert parsed rows into the database.
#            string column types, source_row_key length (== 64),
#            source_system membership, value_kind membership, finite
#            non-negative amount within Numeric(20, 6) scale, report_month
#            ASCII format + agreement with the period's calendar month,
#            date-only (not datetime) period bounds, JSON-serialisable
#            raw_payload dict, and currency_code existence in the
#            currencies reference table. Provenance (raw_file_id/imported_by)
#            is preserved via COALESCE on conflict by default so a later
#            replay that omits it cannot erase audit lineage; orchestrator
#            aggregate replacements can opt into clearing stale raw_file_id.
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


    class SqlAlchemyGoogleRevenueSourceRowRepository:
        """
        Repository for managing Google revenue source rows using SQLAlchemy.

        Provides methods to interact with the database through a given
        SQLAlchemy session.
        """

        def __init__(self, session: Session) -> None:
            """Initialize the repository with a SQLAlchemy session."""
            self._session = session

    def upsert_many(
        self,
        tenant_id: UUID,
        rows: Iterable[ParsedSourceRow],
        *,
        raw_file_id: UUID | None,
        imported_by: UUID | None,
        replace_raw_file_id: bool = False,
    ) -> SourceRowUpsertResult:
        """Upsert multiple parsed source rows for a tenant.

        Handles conflict resolution and audit lineage.
        """
        materialised = list(rows)
        if not materialised:
            return SourceRowUpsertResult(
                entries=[], created=0, updated=0, unchanged=0
            )
        # Defensive copy of raw_payload via _validate prevents caller mutation
        # from racing the pending flush; the reference exchange_rates.py pipeline
        # follows the same pattern in _normalize_rate_input.
        validated = [self._validate(r) for r in materialised]
        # ====================================================================
        # Purpose: Fail closed on a duplicate (source_system, source_row_key)
        #          within a single batch. Two rows that collide on the upsert
        #          conflict target are ambiguous source evidence — letting
        #          ON CONFLICT DO UPDATE silently last-write-wins would persist
        #          one row while the created/updated/unchanged split counts the
        #          collapsed key twice, so the persisted total and the reported
        #          counts disagree. A single parser batch must carry at most one
        #          row per (source_system, source_row_key).
        # Database/ORM: None — in-memory check on the validated batch; mirrors
        #               the google_revenue_source_rows unique index
        #               (tenant_id, source_system, source_row_key).
        # Standards: Typed GoogleRevenueSourceRowValidationError raised BEFORE
        #            any classification read or write (nothing is persisted). The
        #            message names only the offending (source_system,
        #            source_row_key) pairs — never raw_payload or other row
        #            contents — so it cannot leak source evidence into logs, and
        #            the count of named keys is bounded.
        # Blast Radius: connector_runs.counts_json accuracy + finance source-row
        #               integrity; rejects the whole batch (fail closed).
        # ====================================================================
        self._require_no_duplicate_keys(validated)
        # Pre-check FK references at the typed-validation boundary so a bad
        # tenant, currency, or raw file surfaces as the repository's typed
        # GoogleRevenueSourceRowValidationError instead of the opaque DB FK
        # violation raised on flush (same contract the docstring promises for
        # currency_code). _require_raw_file additionally enforces tenant scope
        # so a row can never be linked to another tenant's raw evidence file.
        self._require_tenant(tenant_id)
        self._require_currencies({r.currency_code for r in validated})
        self._require_raw_file(raw_file_id, tenant_id)

        # ============================================================================
        # Purpose: Pre-fetch existing source rows that overlap the input batch keys
        #          so the upsert can classify each input as created / updated /
        #          unchanged before mutating state. Replaces the prior placeholder
        #          where rows_upserted_created/updated/unchanged were always 0.
        # Database/ORM: SELECT-only read against google_revenue_source_rows scoped
        #               to this tenant and the (source_system, source_row_key)
        #               tuples that appear in the input batch.
        # Standards: Read happens BEFORE any write, against a single tenant filter
        #            plus the batch's source_system/source_row_key sets, so the
        #            query plan stays bounded by the input size and respects
        #            tenant isolation.
        # Blast Radius: connector_runs.counts_json accuracy only; the upsert path
        #               itself is unchanged.
        # ============================================================================
        batch_keys = {(r.source_system, r.source_row_key) for r in validated}
        existing_by_key = self._fetch_existing_for_classification(
            tenant_id=tenant_id, batch_keys=batch_keys
        )
        created_count = 0
        updated_count = 0
        unchanged_count = 0
        for row in validated:
            existing = existing_by_key.get((row.source_system, row.source_row_key))
            if existing is None:
                created_count += 1
            elif _content_matches_existing(row, existing):
                unchanged_count += 1
            else:
                updated_count += 1

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
                    # FIX: Preserve provenance by default for replay/import
                    # paths that lack raw evidence, but let the orchestrator
                    # explicitly clear stale single-file lineage when a fresh
                    # multi-file aggregate replaces the row.
                    "raw_file_id": (
                        insert_stmt.excluded.raw_file_id
                        if replace_raw_file_id
                        else func.coalesce(
                            insert_stmt.excluded.raw_file_id,
                            GoogleRevenueSourceRowORM.raw_file_id,
                        )
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
        return SourceRowUpsertResult(
            entries=written,
            created=created_count,
            updated=updated_count,
            unchanged=unchanged_count,
        )

    @staticmethod
    def _require_no_duplicate_keys(
        validated: list[ParsedSourceRow],
    ) -> None:
        """Reject duplicate rows in the validated batch for the same (source_system, source_row_key).

        Raises:
            GoogleRevenueSourceRowValidationError: If duplicate key pairs are
                found, listing each duplicate.
        """
        # ====================================================================
        # Purpose: Reject a batch that carries more than one row per
        #          (source_system, source_row_key) — the upsert conflict target.
        #          See the caller's contract block for why this fails closed.
        # Database/ORM: None — operates on the in-memory validated batch.
        # Standards: Raises GoogleRevenueSourceRowValidationError naming only the
        #            duplicate (source_system, source_row_key) pairs, capped at
        #            _MAX_DUPLICATE_KEYS_IN_ERROR with a "(+N more)" suffix; never
        #            includes raw_payload or other row contents.
        # Blast Radius: finance source-row integrity; no writes.
        # ====================================================================
        seen: set[tuple[str, str]] = set()
        duplicates: set[tuple[str, str]] = set()
        for row in validated:
            key = (row.source_system, row.source_row_key)
            if key in seen:
                duplicates.add(key)
            else:
                seen.add(key)
        if not duplicates:
            return
        ordered = sorted(duplicates)
        shown = ordered[:_MAX_DUPLICATE_KEYS_IN_ERROR]
        named = ", ".join(f"{system}:{row_key}" for system, row_key in shown)
        if len(ordered) > len(shown):
            named += f", (+{len(ordered) - len(shown)} more)"
        raise GoogleRevenueSourceRowValidationError(
            "duplicate (source_system, source_row_key) within a single upsert "
            f"batch: {named}"
        )

    # ========================================================================
    # Purpose: Load the existing rows whose (source_system, source_row_key) pairs
    #          appear in the input upsert batch so the caller can classify each
    #          input as created / updated / unchanged before the upsert runs.
    # Database/ORM: google_revenue_source_rows read-only SELECT scoped to one
    #               tenant + the batch's source_systems + the batch's row keys.
    # Standards: Tenant-explicit; bounded by batch size; one round-trip per
    #            upsert call. Returns the ORM rows directly so the comparison
    #            helper can read fields (including raw_payload) without an
    #            extra _to_entry copy.
    # Blast Radius: connector_runs.counts_json accuracy; no writes.
    # ========================================================================
    def _fetch_existing_for_classification(
        self,
        *,
        tenant_id: UUID,
        batch_keys: set[tuple[str, str]],
    ) -> dict[tuple[str, str], GoogleRevenueSourceRowORM]:
        """Load existing rows for classification based on batch keys.

        Returns a dictionary mapping each (source_system, source_row_key) tuple
        to its ORM row object.
        """
        if not batch_keys:
            return {}
        source_systems = {system for system, _ in batch_keys}
        source_row_keys = {key for _, key in batch_keys}
        rows = self._session.scalars(
            select(GoogleRevenueSourceRowORM).where(
                GoogleRevenueSourceRowORM.tenant_id == tenant_id,
                GoogleRevenueSourceRowORM.source_system.in_(source_systems),
                GoogleRevenueSourceRowORM.source_row_key.in_(source_row_keys),
            )
        ).all()
        return {
            (row.source_system, row.source_row_key): row
            for row in rows
            if (row.source_system, row.source_row_key) in batch_keys
        }

    # ========================================================================
    # Purpose: Remove source rows that disappeared from a replacement report
    #          while keeping the delete scoped to one tenant/account/month/type.
    # Database/ORM: google_revenue_source_rows (GoogleRevenueSourceRowORM).
    # Standards: Tenant-explicit repository method; typed validation before
    #            mutation; callers pass the keys parsed from the replacement.
    # Blast Radius: Source-of-truth source-row cleanup only. No graph projection
    #               impact detected.
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
    #     calls after a successful parser/upsert pass for a report scope.
    # ========================================================================
    def delete_stale_for_scope(
        self,
        tenant_id: UUID,
        *,
        source_system: str,
        source_account_id: str,
        report_type: str,
        report_month: str,
        keep_source_row_keys: Iterable[str],
    ) -> int:
        """Delete source rows that are no longer present for a given report scope.

        Validates inputs and removes rows not in keep_source_row_keys for the
        specified tenant, source system, account, report type, and month.
        Returns the number of rows deleted.
        """
        self._require_tenant(tenant_id)
        for field_name, value in (
            ("source_system", source_system),
            ("source_account_id", source_account_id),
            ("report_type", report_type),
            ("report_month", report_month),
        ):
            if not isinstance(value, str) or not value.strip():
                raise GoogleRevenueSourceRowValidationError(
                    f"{field_name} must be a non-empty str"
                )
        if source_system not in ALLOWED_SOURCE_SYSTEMS:
            raise GoogleRevenueSourceRowValidationError(
                f"unknown source_system: {source_system!r}"
            )
        if not _REPORT_MONTH_RE.match(report_month):
            raise GoogleRevenueSourceRowValidationError(
                f"report_month must be YYYY-MM with month 01-12, got {report_month!r}"
            )

        keep_keys = set(keep_source_row_keys)
        for key in keep_keys:
            if not isinstance(key, str) or len(key) != SOURCE_ROW_KEY_LENGTH:
                raise GoogleRevenueSourceRowValidationError(
                    f"keep_source_row_keys must contain {SOURCE_ROW_KEY_LENGTH}-char strings"
                )

        stmt = delete(GoogleRevenueSourceRowORM).where(
            GoogleRevenueSourceRowORM.tenant_id == tenant_id,
            GoogleRevenueSourceRowORM.source_system == source_system,
            GoogleRevenueSourceRowORM.source_account_id == source_account_id,
            GoogleRevenueSourceRowORM.report_type == report_type,
            GoogleRevenueSourceRowORM.report_month == report_month,
        )
        if keep_keys:
            stmt = stmt.where(
                ~GoogleRevenueSourceRowORM.source_row_key.in_(sorted(keep_keys))
            )
        result = self._session.execute(stmt)
        self._session.flush()
        return int(result.rowcount or 0)

    def list(
        self,
        tenant_id: UUID,
        *,
        report_month: str | None = None,
        source_system: str | None = None,
    ) -> List[GoogleRevenueSourceRowEntry]:  # noqa: UP006
        """
        Retrieve list of Google revenue source rows for a tenant,
        optionally filtering by report month and source system.
        """
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
    ) -> List[GoogleRevenueSourceRowEntry]:  # noqa: UP006
        """Retrieve list of entries for a specific YouTube channel and report month."""
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
        """
        Retrieve a single Google revenue source row entry by exact source
        and row key, or None if not found.
        """
        row = self._session.scalar(
            select(GoogleRevenueSourceRowORM).where(
                GoogleRevenueSourceRowORM.tenant_id == tenant_id,
                GoogleRevenueSourceRowORM.source_system == source_system,
                GoogleRevenueSourceRowORM.source_row_key == source_row_key,
            )
        )
        return self._to_entry(row) if row is not None else None

    @staticmethod
    def _validate(row: ParsedSourceRow) -> ParsedSourceRow:
        """Validate and deep-copy a parsed source row to ensure data integrity before insertion."""
        _validate_text_fields(row)
        _validate_source_identity(row)
        _validate_amount_native(row)
        _validate_raw_payload(row)
        _validate_report_period(row)
        # Deep-copy: raw_payload holds nested dicts (date_range, dimensions,
        # metrics), so a shallow dict() copy would still let a caller mutate
        # those nested objects after upsert_many and change what was persisted.
        return replace(row, raw_payload=deepcopy(row.raw_payload))

    def _require_currencies(self, codes: set[str]) -> None:
        """Ensure that the given currency codes exist in the database,
        raising an error for unknown codes."""
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
        """Ensure that the tenant exists before proceeding, raising a validation error if not."""
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
        """Ensure that the raw report file exists for the given tenant,
        or allow None if not provided."""
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
        """Return the appropriate SQLAlchemy insert function for the given SQL dialect."""
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
        """Convert an ORM row to a GoogleRevenueSourceRowEntry domain object,
        deep-copying payloads."""
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
