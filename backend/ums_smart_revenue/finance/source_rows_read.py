"""Read-only, tenant-scoped access to google_revenue_source_rows.

Mirrors the connector-runs keyset read pattern. raw_payload is never projected
into the API entry (spec §3.3: never returned in this PR for any caller).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session, load_only

from ums_smart_revenue.db.source_models import GoogleRevenueSourceRowORM

MAX_SOURCE_ROW_PAGE_SIZE = 100
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

_VALID_SOURCE_SYSTEMS = frozenset(
    {"youtube_reporting", "youtube_analytics", "adsense_management"}
)
_SOURCE_ROW_LOAD_ONLY = load_only(
    GoogleRevenueSourceRowORM.id,
    GoogleRevenueSourceRowORM.source_system,
    GoogleRevenueSourceRowORM.source_account_id,
    GoogleRevenueSourceRowORM.content_owner_id,
    GoogleRevenueSourceRowORM.youtube_channel_id,
    GoogleRevenueSourceRowORM.report_type,
    GoogleRevenueSourceRowORM.report_month,
    GoogleRevenueSourceRowORM.period_start,
    GoogleRevenueSourceRowORM.period_end,
    GoogleRevenueSourceRowORM.metric_key,
    GoogleRevenueSourceRowORM.value_kind,
    GoogleRevenueSourceRowORM.amount_native,
    GoogleRevenueSourceRowORM.currency_code,
    GoogleRevenueSourceRowORM.source_report_id,
    GoogleRevenueSourceRowORM.ingested_at,
)


class SourceRowReadError(Exception):
    """Base error for source-row reads."""


class SourceRowValidationError(SourceRowReadError):
    """Invalid filter, limit, or cursor for a source-row read."""


# ============================================================================
# Purpose: Normalize and validate source-row month filters once at the data-
#   access boundary so both the API and direct repository callers fail closed
#   before any SQL is built.
# Database/ORM: None.
# Standards: Typed validation; returns canonical YYYY-MM or raises a typed
#   repository error; no DB side effects.
# Blast Radius: Finance read input validation only.
# Connections:
#   - File: backend/ums_smart_revenue/api/source_rows.py -> route boundary.
# ============================================================================
def normalize_source_row_month(month: str) -> str:
    """Return a stripped YYYY-MM month or raise a typed validation error."""
    normalized = month.strip()
    if not MONTH_PATTERN.fullmatch(normalized):
        raise SourceRowValidationError(
            "month must use YYYY-MM with a calendar month from 01 to 12"
        )
    return normalized


@dataclass(frozen=True)
class SourceRowEntry:
    """Immutable source row projected into the read API (no raw_payload)."""

    id: str
    source_system: str
    source_account_id: str
    content_owner_id: str | None
    youtube_channel_id: str | None
    report_type: str
    report_month: str
    period_start: date
    period_end: date
    metric_key: str
    value_kind: str
    amount_native: str
    currency_code: str
    source_report_id: str | None
    ingested_at: datetime

    def to_api(self) -> dict[str, object]:
        """Serialize to the stable API shape; raw_payload always redacted."""
        return {
            "id": self.id,
            "source_system": self.source_system,
            "source_account_id": self.source_account_id,
            "content_owner_id": self.content_owner_id,
            "youtube_channel_id": self.youtube_channel_id,
            "report_type": self.report_type,
            "report_month": self.report_month,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "metric_key": self.metric_key,
            "value_kind": self.value_kind,
            "amount_native": self.amount_native,
            "currency_code": self.currency_code,
            "source_report_id": self.source_report_id,
            "ingested_at": self.ingested_at.isoformat(),
            "raw_payload_redacted": True,
        }


def _to_entry(row: GoogleRevenueSourceRowORM) -> SourceRowEntry:
    """Project an ORM row into the immutable entry (decimal -> str)."""
    return SourceRowEntry(
        id=str(row.id),
        source_system=row.source_system,
        source_account_id=row.source_account_id,
        content_owner_id=row.content_owner_id,
        youtube_channel_id=row.youtube_channel_id,
        report_type=row.report_type,
        report_month=row.report_month,
        period_start=row.period_start,
        period_end=row.period_end,
        metric_key=row.metric_key,
        value_kind=row.value_kind,
        amount_native=format(row.amount_native, "f"),
        currency_code=row.currency_code,
        source_report_id=row.source_report_id,
        ingested_at=row.ingested_at,
    )


@dataclass(frozen=True)
class SourceRowPage:
    """One page of source rows plus its keyset cursor."""

    items: list[SourceRowEntry]
    limit: int
    next_cursor: dict[str, str] | None


def _parse_cursor_dt(value: str | datetime) -> datetime:
    """Parse the cursor ingested_at (ISO str or datetime) or raise."""
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise SourceRowValidationError("cursor_ingested_at must be ISO-8601") from exc


def _parse_cursor_uuid(value: str) -> UUID:
    """Parse the cursor id UUID or raise."""
    try:
        return UUID(value)
    except ValueError as exc:
        raise SourceRowValidationError("cursor_id must be a valid UUID") from exc


def _next_cursor(items: list[SourceRowEntry]) -> dict[str, str] | None:
    """Build the next cursor from the last entry of a full page."""
    if not items:
        return None
    last = items[-1]
    return {"ingested_at": last.ingested_at.isoformat(), "id": last.id}


def list_source_rows(
    session: Session,
    *,
    tenant_id: UUID,
    month: str,
    source_system: str | None = None,
    cursor_ingested_at: str | datetime | None = None,
    cursor_id: str | None = None,
    limit: int,
) -> SourceRowPage:
    """List tenant-scoped source rows for a month, newest-first, keyset-paged."""
    month = normalize_source_row_month(month)
    if limit < 1 or limit > MAX_SOURCE_ROW_PAGE_SIZE:
        raise SourceRowValidationError(
            f"limit must be between 1 and {MAX_SOURCE_ROW_PAGE_SIZE}"
        )
    if (cursor_ingested_at is None) != (cursor_id is None):
        raise SourceRowValidationError(
            "cursor_ingested_at and cursor_id must be provided together"
        )
    if source_system is not None and source_system not in _VALID_SOURCE_SYSTEMS:
        raise SourceRowValidationError("invalid source_system")

    orm = GoogleRevenueSourceRowORM
    # The keyset sort is backed by the composite source-row index on
    # (tenant_id, report_month, ingested_at, id), so the newest-first page
    # can stay index-friendly without re-reading the whole month slice.
    stmt = (
        sa.select(orm)
        .options(_SOURCE_ROW_LOAD_ONLY)
        .where(orm.tenant_id == tenant_id, orm.report_month == month)
        .order_by(orm.ingested_at.desc(), orm.id.desc())
    )
    if source_system is not None:
        stmt = stmt.where(orm.source_system == source_system)
    if cursor_ingested_at is not None and cursor_id is not None:
        cur_dt = _parse_cursor_dt(cursor_ingested_at)
        cur_id = _parse_cursor_uuid(cursor_id)
        stmt = stmt.where(
            sa.or_(
                orm.ingested_at < cur_dt,
                sa.and_(orm.ingested_at == cur_dt, orm.id < cur_id),
            )
        )
    rows = session.scalars(stmt.limit(limit + 1)).all()
    items = [_to_entry(r) for r in rows[:limit]]
    has_more = len(rows) > limit
    return SourceRowPage(
        items=items, limit=limit,
        next_cursor=_next_cursor(items) if has_more else None,
    )


def get_source_row(
    session: Session, *, tenant_id: UUID, row_id: str
) -> SourceRowEntry | None:
    """Return one tenant-scoped source row, or None if absent/cross-tenant."""
    try:
        parsed = UUID(row_id)
    except ValueError as exc:
        raise SourceRowValidationError("id must be a valid UUID") from exc
    orm = GoogleRevenueSourceRowORM
    row = session.scalars(
        sa.select(orm)
        .options(_SOURCE_ROW_LOAD_ONLY)
        .where(orm.id == parsed, orm.tenant_id == tenant_id)
    ).first()
    return _to_entry(row) if row is not None else None
