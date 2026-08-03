# ============================================================================
# Purpose: Pure parsing for the bulk channel inventory import. Turns operator
#   CSV text into validated rows plus per-row errors, with no I/O.
# Database/ORM: None. This module performs no I/O and holds no session.
# Standards: Pure functions over frozen dataclasses; every row failure is a
#   typed error carrying its 1-based row number; header problems fail the whole
#   file rather than silently dropping a column -- a mistyped header that is
#   silently ignored would import channels with the wrong cms_status, and the
#   Google connector then skips them with no error at ingest time.
# Blast Radius: Channel registry inventory fields and channel-group membership.
#   No finance totals, no allocation, no connector behaviour.
# Connections:
#   - File: backend/ums_smart_revenue/api/channels.py -> route executes the plan.
#   - File: backend/ums_smart_revenue/connectors/google/youtube_analytics_client.py
#     -> list_target_channels requires cms_status='INSIDE_CMS'.
# ============================================================================
"""Pure CSV parsing for bulk channel inventory import."""

import csv
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from ums_smart_revenue.org.channel_registry import ChannelRegistryEntry

CHANNEL_ID_PATTERN = re.compile(r"^UC[A-Za-z0-9_-]{22}$")

REQUIRED_COLUMNS = frozenset({"youtube_channel_id", "channel_name"})
OPTIONAL_COLUMNS = frozenset({"group_id", "view_revenue"})
KNOWN_COLUMNS = REQUIRED_COLUMNS | OPTIONAL_COLUMNS

_TRUE_TOKENS = frozenset({"yes", "true", "1"})
_FALSE_TOKENS = frozenset({"no", "false", "0"})


class ChannelImportFormatError(ValueError):
    """The file as a whole is unusable (bad or missing header row)."""


@dataclass(frozen=True)
class ChannelImportRow:
    """One validated CSV data row."""

    row_number: int
    youtube_channel_id: str
    channel_name: str
    group_id: str | None
    view_revenue: bool | None


@dataclass(frozen=True)
class ChannelImportRowError:
    """One rejected CSV data row and why it was rejected."""

    row_number: int
    reason: str


@dataclass(frozen=True)
class ParsedChannelImport:
    """Validated rows plus the rows that failed validation."""

    rows: tuple[ChannelImportRow, ...] = ()
    errors: tuple[ChannelImportRowError, ...] = ()


def parse_channel_import_csv(text: str) -> ParsedChannelImport:
    """Parse operator CSV text into validated rows plus per-row errors."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    try:
        raw_header = next(reader)
    except StopIteration as exc:
        raise ChannelImportFormatError("CSV is empty") from exc

    index = _header_index(raw_header)
    rows: list[ChannelImportRow] = []
    errors: list[ChannelImportRowError] = []

    for row_number, raw_row in enumerate(reader, start=1):
        if not any(cell.strip() for cell in raw_row):
            continue
        parsed = _parse_row(row_number, raw_row, index)
        if isinstance(parsed, ChannelImportRowError):
            errors.append(parsed)
        else:
            rows.append(parsed)

    kept, duplicate_errors = _flag_duplicates(rows)
    errors.extend(duplicate_errors)
    errors.sort(key=lambda item: item.row_number)
    return ParsedChannelImport(rows=tuple(kept), errors=tuple(errors))


def _header_index(raw_header: list[str]) -> dict[str, int]:
    """Validate the header row and map known column names to positions."""
    header = [name.strip().lstrip("﻿").lower() for name in raw_header]
    missing = sorted(REQUIRED_COLUMNS - set(header))
    if missing:
        raise ChannelImportFormatError(f"missing required column(s): {', '.join(missing)}")
    unknown = sorted(set(header) - KNOWN_COLUMNS)
    if unknown:
        raise ChannelImportFormatError(f"unknown column(s): {', '.join(unknown)}")
    return {name: position for position, name in enumerate(header)}


def _cell(raw_row: list[str], index: dict[str, int], name: str) -> str | None:
    """Return a trimmed-optional cell value, or None when the column is absent."""
    position = index.get(name)
    if position is None or position >= len(raw_row):
        return None
    return raw_row[position]


def _parse_view_revenue(raw: str | None) -> bool | None:
    """Map a view_revenue token to a bool, or None when the column is absent."""
    if raw is None:
        return None
    token = raw.strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    raise ValueError(f"unrecognised view_revenue value: {raw.strip()!r}")


def _parse_row(
    row_number: int, raw_row: list[str], index: dict[str, int]
) -> ChannelImportRow | ChannelImportRowError:
    """Validate one data row into a typed row or a typed row error."""
    channel_id = (_cell(raw_row, index, "youtube_channel_id") or "").strip()
    if not CHANNEL_ID_PATTERN.match(channel_id):
        return ChannelImportRowError(
            row_number=row_number,
            reason=f"invalid youtube_channel_id: {channel_id!r}",
        )
    channel_name = (_cell(raw_row, index, "channel_name") or "").strip()
    if not channel_name:
        return ChannelImportRowError(row_number=row_number, reason="channel_name is empty")

    group_raw = _cell(raw_row, index, "group_id")
    group_id = group_raw.strip() if group_raw and group_raw.strip() else None

    view_revenue_raw = _cell(raw_row, index, "view_revenue")
    if "view_revenue" in index and (view_revenue_raw is None or not view_revenue_raw.strip()):
        return ChannelImportRowError(
            row_number=row_number, reason="view_revenue is present but blank"
        )
    try:
        view_revenue = _parse_view_revenue(view_revenue_raw)
    except ValueError as exc:
        return ChannelImportRowError(row_number=row_number, reason=str(exc))

    return ChannelImportRow(
        row_number=row_number,
        youtube_channel_id=channel_id,
        channel_name=channel_name,
        group_id=group_id,
        view_revenue=view_revenue,
    )


def _flag_duplicates(
    rows: list[ChannelImportRow],
) -> tuple[list[ChannelImportRow], list[ChannelImportRowError]]:
    """Reject every copy of a channel id that appears more than once."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.youtube_channel_id] = counts.get(row.youtube_channel_id, 0) + 1
    kept: list[ChannelImportRow] = []
    errors: list[ChannelImportRowError] = []
    for row in rows:
        if counts[row.youtube_channel_id] > 1:
            errors.append(
                ChannelImportRowError(
                    row_number=row.row_number,
                    reason=f"duplicate youtube_channel_id in file: {row.youtube_channel_id}",
                )
            )
        else:
            kept.append(row)
    return kept, errors


class ChannelImportOutcome(StrEnum):
    """What the import will do with a single CSV row."""

    CREATE = "CREATE"
    UPDATE = "UPDATE"
    UNCHANGED = "UNCHANGED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class ChannelImportPlanEntry:
    """The planned outcome for one CSV row, with its field-level diff."""

    row_number: int
    youtube_channel_id: str | None
    outcome: ChannelImportOutcome
    channel_name: str | None = None
    group_id: str | None = None
    revenue_required: bool | None = None
    changes: Mapping[str, tuple[object, object]] = MappingProxyType({})
    reason: str | None = None


@dataclass(frozen=True)
class ChannelImportPlan:
    """Every row's planned outcome plus counts by outcome."""

    entries: tuple[ChannelImportPlanEntry, ...]
    counts: Mapping[str, int]

    @property
    def has_errors(self) -> bool:
        """Return True when any row failed validation, which blocks the apply."""
        return self.counts.get(ChannelImportOutcome.ERROR.value, 0) > 0


def plan_channel_import(
    *,
    rows: tuple[ChannelImportRow, ...],
    errors: tuple[ChannelImportRowError, ...],
    existing: Mapping[str, ChannelRegistryEntry],
    content_owner_id: str,
    cms_status: str,
) -> ChannelImportPlan:
    """Diff parsed rows against the registry into a per-row execution plan."""
    entries: list[ChannelImportPlanEntry] = [
        ChannelImportPlanEntry(
            row_number=error.row_number,
            youtube_channel_id=None,
            outcome=ChannelImportOutcome.ERROR,
            reason=error.reason,
        )
        for error in errors
    ]

    for row in rows:
        revenue_required = True if row.view_revenue is None else row.view_revenue
        current = existing.get(row.youtube_channel_id)
        if current is None:
            outcome = ChannelImportOutcome.CREATE
            changes: dict[str, tuple[object, object]] = {}
        else:
            changes = _inventory_changes(
                current,
                channel_name=row.channel_name,
                cms_status=cms_status,
                content_owner_id=content_owner_id,
                revenue_required=revenue_required,
            )
            outcome = ChannelImportOutcome.UPDATE if changes else ChannelImportOutcome.UNCHANGED
        entries.append(
            ChannelImportPlanEntry(
                row_number=row.row_number,
                youtube_channel_id=row.youtube_channel_id,
                outcome=outcome,
                channel_name=row.channel_name,
                group_id=row.group_id,
                revenue_required=revenue_required,
                changes=MappingProxyType(dict(changes)),
            )
        )

    entries.sort(key=lambda entry: entry.row_number)
    counts = {outcome.value: 0 for outcome in ChannelImportOutcome}
    for entry in entries:
        counts[entry.outcome.value] += 1
    return ChannelImportPlan(entries=tuple(entries), counts=MappingProxyType(counts))


def _inventory_changes(
    current: ChannelRegistryEntry,
    *,
    channel_name: str,
    cms_status: str,
    content_owner_id: str,
    revenue_required: bool,
) -> dict[str, tuple[object, object]]:
    """Return only the inventory fields whose value the import would change."""
    candidates = {
        "channel_name": (current.channel_name, channel_name),
        "cms_status": (current.cms_status, cms_status),
        "content_owner_id": (current.content_owner_id, content_owner_id),
        "revenue_required": (current.revenue_required, revenue_required),
    }
    return {name: pair for name, pair in candidates.items() if pair[0] != pair[1]}
