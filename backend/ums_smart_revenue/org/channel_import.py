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
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from ums_smart_revenue.org.channel_registry import ChannelRegistryEntry

CHANNEL_ID_PATTERN = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
# Generous bound for CMS group keys; real ids are short, and the value lands
# in the unique B-tree index on (tenant_id, cms_group_id) whose per-entry
# size PostgreSQL enforces at ~2.7KB.
MAX_GROUP_ID_CHARS = 255
# Only four columns are ever valid; a wider header is malformed by definition
# and is rejected before any per-cell scanning.
MAX_HEADER_COLUMNS = 16

REQUIRED_COLUMNS = frozenset({"youtube_channel_id", "channel_name"})
OPTIONAL_COLUMNS = frozenset({"group_id", "view_revenue"})
KNOWN_COLUMNS = REQUIRED_COLUMNS | OPTIONAL_COLUMNS

_TRUE_TOKENS = frozenset({"yes", "true", "1"})
_FALSE_TOKENS = frozenset({"no", "false", "0"})


class ChannelImportError(ValueError):
    """Base for typed bulk-channel-import domain errors.

    Mirrors ChannelRegistryError in channel_registry.py: domain layers raise
    named subclasses of this base (never bare ValueError) so route boundaries
    can translate them precisely.
    """


class ChannelImportFormatError(ChannelImportError):
    """The file as a whole is unusable (bad header, malformed CSV, or too large)."""


@dataclass(frozen=True)
class ChannelImportRow:
    """One validated CSV data row.

    ``view_revenue_raw`` preserves the operator's original token ("Yes",
    "TRUE", "0", ...) so the audit trail can show the source value the
    finance-sensitive ``revenue_required`` flag was derived from; ``None``
    means the column was absent and the default applied.
    """

    row_number: int
    youtube_channel_id: str
    channel_name: str
    group_id: str | None
    view_revenue: bool | None
    view_revenue_raw: str | None = None


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


def parse_channel_import_csv(text: str, *, max_rows: int | None = None) -> ParsedChannelImport:
    """Parse operator CSV text into validated rows plus per-row errors.

    ``max_rows`` caps the number of NON-BLANK data rows and aborts the parse
    the moment the cap is exceeded, so an oversized file fails fast instead of
    paying full per-row validation cost before a post-parse count rejects it.
    Blank records are tolerated (a legitimate export may carry a few trailing
    blank rows) but bounded by the SAME cap: the route's byte cap already
    bounds total scanning, so this bound exists to stop a blank-packed file
    from consuming the whole byte budget as wasted scan work while "never
    exceeding" the row limit — and to keep the cap meaningful for any future
    caller that passes ``max_rows`` without a byte cap in front of it.

    A file whose header is valid but carries NO non-blank data row is rejected
    as a format error: an empty roster is an incomplete export, and reporting
    it as a successful zero-channel import would hide that.
    """
    # strict=True makes csv.reader raise csv.Error on malformed quoting (e.g.
    # an unterminated quoted channel_name) instead of silently folding every
    # following physical row into one field. A damaged roster must reject the
    # whole file, never import one malformed channel and drop the rest.
    reader = csv.reader(io.StringIO(text.lstrip("﻿")), strict=True)
    try:
        raw_header = next(reader)
    except StopIteration as exc:
        raise ChannelImportFormatError("CSV is empty") from exc
    except csv.Error as exc:
        raise ChannelImportFormatError(f"malformed CSV: {exc}") from exc

    index = _header_index(raw_header)
    header_width = len(raw_header)
    rows: list[ChannelImportRow] = []
    errors: list[ChannelImportRowError] = []
    data_rows = 0
    blank_rows = 0

    try:
        for row_number, raw_row in enumerate(reader, start=1):
            if not any(cell.strip() for cell in raw_row):
                blank_rows += 1
                if max_rows is not None and blank_rows > max_rows:
                    raise ChannelImportFormatError(f"CSV exceeds {max_rows} blank rows")
                continue
            data_rows += 1
            if max_rows is not None and data_rows > max_rows:
                raise ChannelImportFormatError(f"CSV exceeds {max_rows} rows")
            parsed = _parse_row(row_number, raw_row, index, header_width=header_width)
            if isinstance(parsed, ChannelImportRowError):
                errors.append(parsed)
            else:
                rows.append(parsed)
    except csv.Error as exc:
        raise ChannelImportFormatError(f"malformed CSV: {exc}") from exc

    # A header-only or blank-only export is an incomplete roster, not a
    # successful zero-channel import: without this the apply would return 200
    # and record a CHANNEL_IMPORTED summary for a file that processed nothing
    # (review #159 r3714884517).
    if data_rows == 0:
        raise ChannelImportFormatError("CSV contains no data rows")

    kept, duplicate_errors = _flag_duplicates(rows)
    errors.extend(duplicate_errors)
    errors.sort(key=lambda item: item.row_number)
    return ParsedChannelImport(rows=tuple(kept), errors=tuple(errors))


def _header_index(raw_header: list[str]) -> dict[str, int]:
    """Validate the header row and map known column names to positions."""
    # Width check FIRST, on the raw cells: only four columns are ever valid, so
    # a 2 MiB single-line header must not even pay per-cell strip/lower/BOM
    # normalization on its way to being rejected.
    if len(raw_header) > MAX_HEADER_COLUMNS:
        raise ChannelImportFormatError(
            f"header has {len(raw_header)} columns; at most {MAX_HEADER_COLUMNS} are valid"
        )
    header = [name.strip().lstrip("﻿").lower() for name in raw_header]
    missing = sorted(REQUIRED_COLUMNS - set(header))
    if missing:
        raise ChannelImportFormatError(f"missing required column(s): {', '.join(missing)}")
    # A duplicated header is an ambiguous schema: the position map below would
    # silently keep the LAST copy's column, importing whichever value happens
    # to sit there. Reject the whole file instead of guessing. Counter keeps
    # this linear — a per-name header.count() scan is quadratic.
    duplicates = sorted({name for name, count in Counter(header).items() if count > 1})
    if duplicates:
        raise ChannelImportFormatError(f"duplicate column(s): {', '.join(duplicates)}")
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


def _parse_text_fields(
    row_number: int, raw_row: list[str], index: dict[str, int]
) -> tuple[str, str | None] | ChannelImportRowError:
    """Extract (channel_name, group_id), rejecting empty or NUL-bearing values.

    PostgreSQL cannot store NUL in a text column; letting it through would
    turn the promised row-level 422 into an uncaught 500 at persistence.
    """
    channel_name = (_cell(raw_row, index, "channel_name") or "").strip()
    if not channel_name:
        return ChannelImportRowError(row_number=row_number, reason="channel_name is empty")
    if "\x00" in channel_name:
        return ChannelImportRowError(
            row_number=row_number, reason="channel_name contains a NUL character"
        )
    group_raw = _cell(raw_row, index, "group_id")
    group_id = group_raw.strip() if group_raw and group_raw.strip() else None
    if group_id is not None and "\x00" in group_id:
        return ChannelImportRowError(
            row_number=row_number, reason="group_id contains a NUL character"
        )
    # The key lands in the unique B-tree index on (tenant_id, cms_group_id);
    # PostgreSQL rejects index entries past its per-entry size limit, so an
    # unbounded key would pass dry-run and then 500 at apply. Real CMS group
    # ids are short; the cap is generous.
    if group_id is not None and len(group_id) > MAX_GROUP_ID_CHARS:
        return ChannelImportRowError(
            row_number=row_number,
            reason=f"group_id exceeds {MAX_GROUP_ID_CHARS} characters",
        )
    return channel_name, group_id


def _parse_row(
    row_number: int, raw_row: list[str], index: dict[str, int], *, header_width: int
) -> ChannelImportRow | ChannelImportRowError:
    """Validate one data row into a typed row or a typed row error."""
    # A row wider than the header usually means an unescaped comma inside a
    # value; reading only the indexed cells would silently persist the
    # truncated prefix (e.g. "Alpha,News" imported as "Alpha"). Fail the row.
    if len(raw_row) > header_width:
        return ChannelImportRowError(
            row_number=row_number,
            reason=(
                f"row has {len(raw_row)} cell(s) but the header defines {header_width} column(s)"
            ),
        )
    channel_id = (_cell(raw_row, index, "youtube_channel_id") or "").strip()
    if not CHANNEL_ID_PATTERN.match(channel_id):
        return ChannelImportRowError(
            row_number=row_number,
            reason=f"invalid youtube_channel_id: {channel_id!r}",
        )
    text_fields = _parse_text_fields(row_number, raw_row, index)
    if isinstance(text_fields, ChannelImportRowError):
        return text_fields
    channel_name, group_id = text_fields

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
        view_revenue_raw=view_revenue_raw.strip() if view_revenue_raw is not None else None,
    )


def _flag_duplicates(
    rows: list[ChannelImportRow],
) -> tuple[list[ChannelImportRow], list[ChannelImportRowError]]:
    """Reject only CONFLICTING copies of a repeated channel id.

    CMS group membership is many-to-many, so a roster legitimately repeats a
    ``youtube_channel_id`` once per group (the singular ``group_id`` column
    carries one association per row). Copies must agree on the inventory
    fields (``channel_name``, ``view_revenue``): a conflicting duplicate is
    ambiguous about what to persist and fails every copy closed, but
    agreeing copies all survive so the planner can attach each row's group.
    """
    first_signature: dict[str, tuple[str, bool | None]] = {}
    conflicted: set[str] = set()
    for row in rows:
        signature = (row.channel_name, row.view_revenue)
        if first_signature.setdefault(row.youtube_channel_id, signature) != signature:
            conflicted.add(row.youtube_channel_id)
    kept: list[ChannelImportRow] = []
    errors: list[ChannelImportRowError] = []
    for row in rows:
        if row.youtube_channel_id in conflicted:
            errors.append(
                ChannelImportRowError(
                    row_number=row.row_number,
                    reason=(
                        "conflicting duplicate youtube_channel_id in file: "
                        f"{row.youtube_channel_id} (copies disagree on "
                        "channel_name/view_revenue)"
                    ),
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


class ChannelImportGroupAction(StrEnum):
    """What a row's ``group_id`` will do to channel-group state.

    ``group_id`` alone cannot say this, and the two differ in kind: JOIN adds
    a channel to a group that already exists under this content owner, while
    CREATE mints a NEW ``SECTOR`` group — a fresh finance-scope object stamped
    to this owner at birth, audited as ``GROUP_UPDATED``/``group_created``.
    An operator approving an all-or-nothing roster has to be able to tell
    those apart before the write (review #184).
    """

    CREATE = "CREATE"
    JOIN = "JOIN"


@dataclass(frozen=True)
class ChannelImportPlanEntry:
    """The planned outcome for one CSV row, with its field-level diff.

    Every write the apply performs for a row is implied by its ``outcome``,
    its ``changes`` diff, and its ``group_id`` membership — the last of which
    carries ``group_action`` to say WHICH group write it implies. Nothing
    rides along invisibly: the import never claims ownership of a group it did
    not create, so there is no ownership write left for an extra field to
    disclose.
    """

    row_number: int
    youtube_channel_id: str | None
    outcome: ChannelImportOutcome
    channel_name: str | None = None
    group_id: str | None = None
    # None exactly when group_id is None (no group write) or the row is an
    # ERROR (nothing is written at all). Never None alongside an attachable
    # group key on a writable row.
    group_action: ChannelImportGroupAction | None = None
    revenue_required: bool | None = None
    view_revenue_raw: str | None = None
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


# ============================================================================
# Purpose: Decide every row's outcome for a bulk channel import — CREATE,
#   UPDATE (with its field diff), UNCHANGED, or ERROR — by diffing the parsed
#   roster against the registry snapshot the caller supplies.
# Database/ORM: None. Pure function over caller-supplied data; the store reads
#   happen in channel_import_apply.plan_channel_import_with_stores. Keeping
#   this I/O-free is what makes the outcome rules unit-testable.
# Standards: Fail closed per row, never per file — every invalid row is
#   reported so an operator fixes one file rather than one row at a time, and
#   the route rejects the whole apply if any ERROR remains. Archived registry
#   rows and archived CMS groups are ERRORs, never silent creates or
#   reactivations, and so is an EXISTING owner-NULL CMS group: only that
#   group's content owner may claim it, and a CSV cell is not that owner
#   speaking. `revenue_required` is finance-sensitive: an absent
#   view_revenue column defaults to required, and turning it ON is separately
#   guarded against LOCKED months at the registry write boundary. A repeated
#   channel id is an ADDITIONAL group membership (many-to-many) when its
#   copies agree on inventory fields — the first copy owns the inventory
#   outcome, later copies plan UNCHANGED to carry their group; an archived
#   channel fails every copy.
# Blast Radius: Every apply-time write decision, and therefore connector
#   ingest targeting via cms_status/content_owner_id. No writes of its own,
#   no audit, no finance totals.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
#     plan_channel_import_with_stores supplies the store state; the apply
#     executes these entries.
#   - File: backend/ums_smart_revenue/api/channels.py -> renders the plan as
#     the dry-run/apply response payload.
def _blocked_group_reason(
    group_id: str | None,
    *,
    archived_group_ids: frozenset[str],
    foreign_owner_group_ids: frozenset[str],
    adoptable_group_ids: frozenset[str],
    content_owner_id: str,
) -> str | None:
    """Return the row-error reason for an unattachable group key, else None.

    All three conditions are knowable from stored state, so they belong to
    PLANNING even though the write boundary rechecks them under a row lock: a
    dry run that reports a clean plan for an import the apply will reject is a
    preview that lies. Only keys that resolve to an EXISTING group are blocked
    — a key with no local group is created here, stamped at birth with the
    request's owner, which is a claim the request already carries.
    """
    if group_id is None:
        return None
    if group_id in archived_group_ids:
        return (
            f"channel group is archived (active=false): {group_id}; reactivate it before importing"
        )
    if group_id in foreign_owner_group_ids:
        return (
            f"channel group belongs to another content owner: {group_id}; "
            "import that group's channels under its own content owner"
        )
    if group_id in adoptable_group_ids:
        return (
            f"channel group {group_id} exists without a content owner; run "
            f"POST /channels/groups/sync for content owner {content_owner_id} "
            "to adopt it, or clear/archive the group if it is stale"
        )
    return None


def _planned_group_action(
    group_id: str | None, *, owned_group_ids: frozenset[str] | None
) -> ChannelImportGroupAction | None:
    """Return the group effect an ATTACHABLE key implies, or None when unknown.

    ``None`` covers two cases that both mean "make no claim": the row has no
    group key at all, or the caller supplied no ``owned_group_ids`` read.

    Only ever called for rows that already cleared ``_blocked_group_reason``,
    so every key reaching here is attachable and the classification is a
    two-way split: a key this owner already holds is a JOIN, and any other
    surviving key resolves to no local group at all — a CREATE. Absence is the
    CREATE signal precisely because the three refusal sets have already
    removed every OTHER way a key can be absent from ``owned_group_ids``
    (archived, another owner's, owner-NULL).

    JOIN describes the GROUP's fate, not the membership row's: a channel
    already in the group writes nothing. Distinguishing that would mean
    loading every roster group's membership, which the bulk lookups
    deliberately avoid for a 5000-row roster; the API spec and the operator
    copy both say so rather than overstating this.
    """
    if group_id is None or owned_group_ids is None:
        return None
    if group_id in owned_group_ids:
        return ChannelImportGroupAction.JOIN
    return ChannelImportGroupAction.CREATE


# ============================================================================
def plan_channel_import(
    *,
    rows: tuple[ChannelImportRow, ...],
    errors: tuple[ChannelImportRowError, ...],
    existing: Mapping[str, ChannelRegistryEntry],
    content_owner_id: str,
    cms_status: str,
    archived_group_ids: frozenset[str] = frozenset(),
    foreign_owner_group_ids: frozenset[str] = frozenset(),
    adoptable_group_ids: frozenset[str] = frozenset(),
    owned_group_ids: frozenset[str] | None = None,
) -> ChannelImportPlan:
    """Diff parsed rows against the registry into a per-row execution plan.

    ``archived_group_ids`` carries the CMS group keys whose existing group is
    archived (active=false); a row targeting one fails closed. Attaching a
    channel to a retired group would audit a membership change that active
    group listings and finance scope selection never surface.

    ``foreign_owner_group_ids`` carries the CMS group keys already stamped to a
    DIFFERENT content owner. The apply refuses those too, but the conflict is
    knowable from stored state, so classifying it here is what keeps
    ``dry_run=true`` honest: a preview that reports a clean plan for an import
    the write boundary will 409 is worse than no preview. Owner-NULL keys are
    deliberately absent from this set — they carry no conflicting claim.

    ``adoptable_group_ids`` carries those owner-NULL keys, and rows targeting
    one are refused as well. Stamping an existing group's ``content_owner_id``
    decides which owner's CMS sync governs that group from then on, and a CSV
    cell is not that owner speaking: the evidence has to come from YouTube, so
    only ``POST /channels/groups/sync`` may make the claim. The row error says
    so, and it blocks the whole apply the way every other row error does.

    ``owned_group_ids`` is the one group set that does NOT refuse anything: it
    carries the keys this content owner already holds, which is what lets a
    surviving row say whether its group is JOINed or CREATEd. The other three
    sets have already removed every other reason a key could be missing from
    it, so absence WITHIN the set means "no local group" — and therefore a new
    SECTOR group, a finance-scope object the operator should see coming.

    It is ``None``-able rather than defaulting to an empty frozenset, and the
    two are NOT the same: an empty set is a real answer (this owner holds none
    of these keys, so every attachable row CREATEs), while ``None`` means the
    caller did not perform the read and every ``group_action`` stays ``None``.
    A default empty set would make a forgetful caller report "creates a new
    group" for every JOIN — a confident falsehood on the exact disclosure this
    field exists to provide. No claim beats a wrong one.
    """
    entries: list[ChannelImportPlanEntry] = [
        ChannelImportPlanEntry(
            row_number=error.row_number,
            youtube_channel_id=None,
            outcome=ChannelImportOutcome.ERROR,
            reason=error.reason,
        )
        for error in errors
    ]

    seen_channels: set[str] = set()
    for row in rows:
        revenue_required = True if row.view_revenue is None else row.view_revenue
        blocked = _blocked_group_reason(
            row.group_id,
            archived_group_ids=archived_group_ids,
            foreign_owner_group_ids=foreign_owner_group_ids,
            adoptable_group_ids=adoptable_group_ids,
            content_owner_id=content_owner_id,
        )
        if blocked is not None:
            entries.append(
                ChannelImportPlanEntry(
                    row_number=row.row_number,
                    youtube_channel_id=row.youtube_channel_id,
                    outcome=ChannelImportOutcome.ERROR,
                    reason=blocked,
                )
            )
            continue
        # Computed only past the block check, so a refused key never carries a
        # group_action: an ERROR row writes nothing at all, and claiming it
        # would create or join a group would be the same lie in reverse.
        group_action = _planned_group_action(row.group_id, owned_group_ids=owned_group_ids)
        current = existing.get(row.youtube_channel_id)
        # The archived check runs BEFORE the repeated-membership shortcut: a
        # repeated archived channel must report EVERY copy as an ERROR with
        # the actionable reactivation reason, not mark the later copies as
        # writable membership rows the apply would never reach (review #159
        # r3714401817).
        if current is None or current.active:
            if row.youtube_channel_id in seen_channels:
                # A repeated channel id carries an ADDITIONAL group membership
                # (many-to-many; one association per row — the parser already
                # rejected copies that disagree on inventory fields). The first
                # copy owns the inventory outcome; membership rows plan as
                # UNCHANGED so the apply attaches their group without a second
                # inventory decision.
                entries.append(
                    ChannelImportPlanEntry(
                        row_number=row.row_number,
                        youtube_channel_id=row.youtube_channel_id,
                        outcome=ChannelImportOutcome.UNCHANGED,
                        channel_name=row.channel_name,
                        group_id=row.group_id,
                        group_action=group_action,
                        revenue_required=revenue_required,
                        view_revenue_raw=row.view_revenue_raw,
                    )
                )
                continue
            seen_channels.add(row.youtube_channel_id)
        if current is not None and not current.active:
            # An archived (active=false) registry row must fail closed: planning
            # it as CREATE would 500 on the create_channel duplicate guard, and
            # silently reactivating it would resurrect a channel an operator
            # deliberately retired. Reactivation is an explicit registry action,
            # not an import side effect.
            entries.append(
                ChannelImportPlanEntry(
                    row_number=row.row_number,
                    youtube_channel_id=row.youtube_channel_id,
                    outcome=ChannelImportOutcome.ERROR,
                    reason=(
                        "channel exists but is archived (active=false): "
                        f"{row.youtube_channel_id}; reactivate it before importing"
                    ),
                )
            )
            continue
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
                group_action=group_action,
                revenue_required=revenue_required,
                view_revenue_raw=row.view_revenue_raw,
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
