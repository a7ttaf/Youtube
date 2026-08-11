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

from ums_smart_revenue.org.channel_registry import (
    ChannelRegistryEntry,
    derive_revenue_source_status,
)

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


# ============================================================================
# Purpose: The file-level DUPLICATE boundary — decide which copies of a
#   repeated youtube_channel_id may survive parsing, and reject the rest as
#   row errors before any of them reach the planner.
# Database/ORM: None — pure over the already-parsed rows; the parser performs
#   no I/O and holds no session.
# Standards: A roster legitimately repeats a channel once per DISTINCT group,
#   because CMS membership is many-to-many and the singular group_id column
#   carries one association per row. Two ways to break that, both fail closed:
#   copies that DISAGREE on the inventory fields are ambiguous about what to
#   persist and fail every copy (no copy is privileged, so there is no
#   non-arbitrary winner), and copies that RESTATE one (channel, group) pair
#   say nothing the first copy did not. Conflict is reported ahead of
#   repetition on purpose: a channel whose copies disagree has no settled
#   inventory to attach a group to, so naming the repeat first would send the
#   operator to fix the lesser defect. Every rejected row is reported with its
#   own 1-based row number, per the module's fail-per-row rule.
# Blast Radius: Which rosters are admissible at all, and therefore whether the
#   preview can overstate group work. Any surviving ERROR row rejects the whole
#   apply. No writes of its own, no audit, no finance totals.
#   BREAKING for one roster shape, stated plainly because the rest of this
#   branch only adds refusals to races: a file restating a (channel, group)
#   pair used to return 200 and now returns 422, so existing rosters carrying
#   a restated line must be deduped before they apply again. What is preserved
#   is the REGISTRY result — the restated row is NOT a no-op (UNCHANGED rows
#   write through the boundary), but it only re-wrote the values its first
#   copy had already installed, so the deduped file lands the same channel
#   rows and the same memberships and nothing already stored changes. One
#   persisted thing DOES change, and it is the point: the restated row also
#   incremented the durable CHANNEL_IMPORTED counts, so the deduped file's
#   summary is one lower. The double count this rule removes was in the audit
#   tally, not only in the preview.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_import.py ->
#     plan_channel_import consumes the surviving rows; its repeated-channel
#     shortcut relies on every copy naming a distinct group.
#   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
#     _group_write_batches collapses a repeated pair at write time, which is
#     the divergence the repeat rule prevents for pairs that CARRY a group;
#     _apply_inventory_writes tallies per plan entry, which is why a repeat
#     double-counts the durable summary whether or not it names a group.
# ============================================================================
def _flag_duplicates(
    rows: list[ChannelImportRow],
) -> tuple[list[ChannelImportRow], list[ChannelImportRowError]]:
    """Reject CONFLICTING copies of a repeated channel id, and exact repeats.

    CMS group membership is many-to-many, so a roster legitimately repeats a
    ``youtube_channel_id`` once per DISTINCT group (the singular ``group_id``
    column carries one association per row). Two things break that contract
    and both fail closed:

    * copies that disagree on the inventory fields (``channel_name``,
      ``view_revenue``) are ambiguous about what to persist, and fail every
      copy — no copy is privileged, so there is no non-arbitrary winner;
    * copies that repeat the SAME ``(youtube_channel_id, group_id)`` pair —
      including two rows carrying no group at all — say nothing the first
      copy did not already say.

    Rejecting the exact repeat keeps the import honest, for a reason that
    DIFFERS BY SHAPE — the two halves are not the same defect:

    * a pair that names a GROUP diverges plan from write. The write side
      collapses it (``_group_write_batches`` refuses to hand one channel to
      ``add_members`` twice) but planning does not, so the second copy repeats
      the first's ``group_action`` and the dry run promises the group work
      twice for ONE association — which the write performs at most once, and
      not at all when the channel is already in that group
      (``_attach_group_memberships`` filters the batch against the existing
      members, so a re-import writes and audits nothing). The divergence is
      therefore 2-vs-1 at best and 2-vs-0 on a re-import;
    * a pair carrying NO group never reaches the batcher at all —
      ``group_action`` is None on both copies and ``_group_write_target``
      filters them out. It is refused because the second copy is a phantom
      UNCHANGED row: it makes the preview AND the durable CHANNEL_IMPORTED
      tally report two outcomes for a channel the roster named once.

    Deliberately NOT claimed: that the apply performs the work once while the
    plan counts it twice. ``_apply_inventory_writes`` tallies per plan entry,
    so the applied counts equal the plan counts for both shapes — the repeat
    is double-counted consistently, which is why the fix belongs in the parser
    rather than in a reconciliation between the two tallies.

    This rejection IS a breaking change; the contract block above states the
    compatibility position in full.
    """
    conflicted = _conflicting_channel_ids(rows)
    repeated = _repeated_associations(rows)
    kept: list[ChannelImportRow] = []
    errors: list[ChannelImportRowError] = []
    for row in rows:
        reason = _duplicate_reason(row, conflicted=conflicted, repeated=repeated)
        if reason is None:
            kept.append(row)
        else:
            errors.append(ChannelImportRowError(row_number=row.row_number, reason=reason))
    return kept, errors


# ============================================================================
# Purpose: Decide which repeated channel ids are CONFLICTED — copies that
#   disagree on the inventory fields — so the caller can fail every copy of
#   each rather than pick one.
# Database/ORM: None — pure over the parsed rows, no I/O.
# Standards: The signature is (channel_name, view_revenue), the two fields a
#   copy can disagree about; group_id is deliberately absent because differing
#   groups are the LEGAL repeat this module exists to permit. Verdict is per
#   channel id, not per row, and it condemns EVERY copy: the roster states two
#   incompatible things about one channel and no copy is privileged, so there
#   is no non-arbitrary winner and healing by picking the first would persist a
#   value the operator never singled out. `view_revenue` is finance-sensitive —
#   it drives revenue_required and the derived revenue_source_status — which is
#   why a disagreement about it fails closed rather than defaulting.
# Blast Radius: Turns a whole channel's rows into ERRORs, and any surviving
#   ERROR row rejects the entire all-or-nothing apply. No writes, no audit.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_import.py -> _duplicate_reason
#     reports this verdict AHEAD of the repeat rule, because a channel with no
#     settled inventory has no group to attach.
#   - File: backend/ums_smart_revenue/org/channel_registry.py ->
#     derive_revenue_source_status consumes the revenue flag this guards.
# ============================================================================
def _conflicting_channel_ids(rows: list[ChannelImportRow]) -> set[str]:
    """Channel ids whose copies disagree about what to persist."""
    first_signature: dict[str, tuple[str, bool | None]] = {}
    conflicted: set[str] = set()
    for row in rows:
        signature = (row.channel_name, row.view_revenue)
        if first_signature.setdefault(row.youtube_channel_id, signature) != signature:
            conflicted.add(row.youtube_channel_id)
    return conflicted


# ============================================================================
# Purpose: Find the (channel id, group key) associations a roster states more
#   than once — the parser-level rule that decides a repeated channel row is
#   redundant rather than an additional membership.
# Database/ORM: None — pure over the parsed rows, no I/O.
# Standards: The key is the PAIR, never the channel alone. Repeating a channel
#   is only meaningful ACROSS groups, so keying on the channel would refuse
#   every legitimate many-to-many roster, and keying on the group would refuse
#   unrelated channels sharing one. ``group_id`` of None participates as an
#   ordinary value: a channel listed twice with no group is a repeat like any
#   other, and treating absence as a wildcard would let exactly that case
#   through. The ASSOCIATION identified here is the one _group_write_target
#   names at write time, which is what makes the guarantee hold: a pair
#   refused here is a pair the batcher would otherwise have collapsed. The two
#   TUPLES are not interchangeable and must never be compared or reused
#   directly — this one is (channel, group) and that one is (group, channel),
#   and that one additionally drops rows with a falsy group key or no
#   channel_name. Rows carrying NO group never reach the batcher at all; they
#   are refused here for the other half of the reason, the phantom UNCHANGED
#   row that inflates the counts.
# Blast Radius: Which rosters are admissible, and therefore whether the dry-run
#   preview can promise group work the apply performs once. No writes, no
#   audit, no finance totals.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_import.py -> _duplicate_reason
#     turns a hit here into the operator-facing row error.
#   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
#     _group_write_target keys the write pass on the same pair; the two must
#     not drift apart.
# ============================================================================
def _repeated_associations(rows: list[ChannelImportRow]) -> set[tuple[str, str | None]]:
    """The ``(channel id, group key)`` pairs the file states more than once.

    ``group_id`` is part of the key because repeating a channel is only
    meaningful ACROSS groups; ``None`` participates as an ordinary value, so a
    channel listed twice with no group is a repeat like any other.
    """
    seen: set[tuple[str, str | None]] = set()
    repeated: set[tuple[str, str | None]] = set()
    for row in rows:
        association = (row.youtube_channel_id, row.group_id)
        if association in seen:
            repeated.add(association)
        seen.add(association)
    return repeated


# ============================================================================
# Purpose: Decide which duplicate verdict a row earns — the harsher CONFLICT,
#   the softer REPEAT, or none — and word it as the remediation the operator
#   needs. This is where the two duplicate rules are ORDERED against each
#   other, which neither set builder can express on its own.
# Database/ORM: None — a pure classification over parsed rows. Nothing is read
#   from the registry; both sets are derived from the file alone.
# Standards: Conflict is reported AHEAD of repetition because a channel whose
#   copies disagree has no settled inventory to attach a group to, so naming
#   the repeat first would send the operator to fix the lesser defect and hit
#   the greater one on the next upload. The two repeat messages are separate
#   because the remedies are: a pair carrying NO group can only be fixed by
#   dropping the copy, while a pair naming a group can be fixed by giving the
#   copy a DISTINCT group. Returning None — not raising — is what lets the
#   caller keep an admissible row.
# Blast Radius: This is the BREAKING duplicate rule. Every string returned here
#   becomes a ChannelImportRowError, and a single surviving ERROR row refuses
#   the ENTIRE all-or-nothing apply — a roster that used to import with a 200
#   now gets a 422. The text is the operator's only remediation, so a wrong
#   verdict here is a wrong instruction, not just a wrong label. No finance
#   math and no write; it decides whether the write happens at all.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_import.py -> _flag_duplicates,
#     the only caller, which turns a non-None result into the row error.
#   - File: backend/ums_smart_revenue/org/channel_import.py ->
#     _conflicting_channel_ids and _repeated_associations, the two sets whose
#     precedence this function decides.
#   - File: Docs/12_BACKEND_API_SPEC.md -> the documented 422 contract and the
#     duplicate rule this enforces.
# ============================================================================
def _duplicate_reason(
    row: ChannelImportRow,
    *,
    conflicted: set[str],
    repeated: set[tuple[str, str | None]],
) -> str | None:
    """The row error this duplicate earns, or None if the row is admissible.

    Conflict is reported ahead of repetition: a channel whose copies disagree
    has no settled inventory to attach a group to, so naming the repeat first
    would send the operator to fix the lesser defect.
    """
    if row.youtube_channel_id in conflicted:
        return (
            "conflicting duplicate youtube_channel_id in file: "
            f"{row.youtube_channel_id} (copies disagree on "
            "channel_name/view_revenue)"
        )
    if (row.youtube_channel_id, row.group_id) not in repeated:
        return None
    if row.group_id is None:
        return (
            f"duplicate youtube_channel_id in file: {row.youtube_channel_id} "
            "with no group_id; repeat a channel only to add it to a distinct group"
        )
    return (
        f"duplicate youtube_channel_id in file: {row.youtube_channel_id} "
        f"is already associated with group_id {row.group_id}; "
        "repeat a channel only to add it to a distinct group"
    )


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
    # The revenue_source_status this row's write will leave on the channel,
    # as (from, to) — `from` is None for a CREATE, which has no prior status.
    # DISCLOSED rather than merely derived: the write re-classifies the source
    # whenever revenue_required flips, and that classification feeds the
    # registry's missing-official-revenue state and recommended action, so an
    # operator approving the plan is approving a finance-source mutation the
    # inventory diff never mentions (review #184). None for an ERROR row and
    # for any row whose write leaves the status untouched.
    revenue_source_status: tuple[str | None, str] | None = None
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
# Purpose: Decide whether a row's group key is UNATTACHABLE and say why —
#   the group is archived, belongs to another content owner, or exists with
#   no owner at all — so planning refuses the row instead of leaving it for
#   the apply to reject.
# Database/ORM: None — pure over the three key sets the caller already read in
#   bulk (ChannelGroupORM keys); the reads happen in
#   channel_import_apply.plan_channel_import_with_stores.
# Standards: Fail closed, and fail at PLAN time. All three conditions are
#   knowable from stored state, so a dry run that reports a clean plan for an
#   import the write boundary will reject is a preview that lies — the write
#   boundary still rechecks each one under the group row lock, and the two
#   together are the disclosure and its enforcement. Only keys resolving to an
#   EXISTING group are blocked: an absent key is created here and stamped at
#   birth with the request's owner, which is a claim the request already
#   carries. The owner-NULL case is deliberately a refusal and NOT an
#   adoption — stamping an existing group's content_owner_id decides whose CMS
#   sync governs it from then on, and a CSV cell is not that owner speaking.
#   Returns the operator-facing reason rather than a bool so every refusal
#   names its own remedy.
# Blast Radius: Which rows become ERROR, and therefore whether the import is
#   refused as a whole — any surviving ERROR row rejects the entire apply. No
#   writes of its own, no audit, no finance totals.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
#     plan_channel_import_with_stores supplies the archived/foreign/adoptable
#     key sets, and the group pass rechecks the same conditions under lock.
#   - File: backend/ums_smart_revenue/org/channel_import.py ->
#     _planned_group_action labels only the keys this function has cleared.
# ============================================================================
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


# ============================================================================
# Purpose: Label the group EFFECT a row's group_id implies — mint a new SECTOR
#   group (CREATE) or attach to one this owner already holds (JOIN) — so the
#   preview can promise which, and the write boundary can re-check it.
# Database/ORM: None — pure decision over the group-key sets the caller
#   already loaded (ChannelGroupORM keys, read in bulk during planning).
# Standards: Returns None for rows that write no group at all (no key, or an
#   ERROR row), so a null label never means "unknown" — it means "no effect".
#   The label is a PLAN-TIME observation and can be raced, which is exactly
#   why the write boundary re-checks it under the group row lock rather than
#   trusting it; the two together are the disclosure and its enforcement.
# Blast Radius: The preview's "new group" / "adds to existing" claim, and —
#   via the write-boundary recheck — whether a diverged effect 409s the whole
#   import. No writes of its own.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
#     _require_planned_group_action enforces this label under the row lock.
#   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx ->
#     GroupCell renders it.
# ============================================================================
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
# Purpose: The import's whole decision boundary. Diffs parsed rows against the
#   registry into a per-row plan: the outcome (CREATE/UPDATE/UNCHANGED/ERROR),
#   the field-level diff the operator reviews, the group effect each Group_ID
#   implies, and the revenue-source transition the write would perform.
#   Everything the apply later executes is decided HERE; the apply adds no
#   judgement of its own.
# Database/ORM: None — pure over the caller's already-loaded snapshots
#   (``existing`` plus the four group-id sets). Keeping the reads outside is
#   what bounds the route to a FIXED number of bulk queries regardless of
#   roster size, and it is why this function can be exercised without a DB.
# Standards: Fails CLOSED on every ambiguity, as ERROR rows rather than
#   guesses: an archived channel (reactivation is an explicit registry action,
#   never an import side effect), an archived group, a cross-owner group, an
#   existing group with a NULL content_owner_id (the import never ADOPTS —
#   only the owner's own CMS sync may claim one), and duplicate ids that
#   disagree. One ERROR row 422s the entire file, because the apply is
#   all-or-nothing — which is also what lets the apply's write-boundary guards
#   assume every planned row was reviewed as valid and active.
#   The group action is decided from the plan ALONE — no membership read — so
#   `outcome` speaks only about channel inventory. That limit is load-bearing
#   for the frontend: it is why a re-plan cannot verify a group-bearing
#   roster, and it must not be quietly widened.
#   The revenue-source transition is re-derived on exactly one trigger, a
#   revenue_required flip, and disclosed in the plan so the operator approves
#   the classification change rather than discovering it in the audit trail.
# Blast Radius: FINANCE-SCOPE. The plan digested here becomes
#   plan_fingerprint, which binds the apply; a row planned wrong is a row
#   written wrong under an approval the operator did give. Group effects mint
#   or join SECTOR groups, which move revenue rollups, and
#   revenue_source_status drives missing_official_revenue and the
#   outside-CMS recommendation downstream.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_import_apply.py -> executes
#       this plan and re-checks the reviewed pre-state under the row lock.
#   - File: backend/ums_smart_revenue/api/channels.py -> loads the snapshots,
#       fingerprints the plan, and serves it as the preview.
#   - File: Docs/12_BACKEND_API_SPEC.md -> the operator-facing contract for
#       every outcome and error above.
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
                # (many-to-many; one association per row). The parser rejected
                # both copies that disagree on inventory fields and copies that
                # restate one (channel, group) pair, so every copy reaching
                # here names a DISTINCT group — which is what makes this row's
                # group_action a promise the apply actually keeps rather than a
                # second claim on work the batcher has already collapsed. The
                # first copy owns the inventory outcome; membership rows plan
                # as UNCHANGED so the apply attaches their group without a
                # second inventory decision.
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
            source_status: tuple[str | None, str] | None = (
                None,
                _created_revenue_source_status(revenue_required),
            )
        else:
            changes = _inventory_changes(
                current,
                channel_name=row.channel_name,
                cms_status=cms_status,
                content_owner_id=content_owner_id,
                revenue_required=revenue_required,
            )
            outcome = ChannelImportOutcome.UPDATE if changes else ChannelImportOutcome.UNCHANGED
            source_status = _planned_revenue_source_status(current, revenue_required)
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
                revenue_source_status=source_status,
            )
        )

    entries.sort(key=lambda entry: entry.row_number)
    counts = {outcome.value: 0 for outcome in ChannelImportOutcome}
    for entry in entries:
        counts[entry.outcome.value] += 1
    return ChannelImportPlan(entries=tuple(entries), counts=MappingProxyType(counts))


# ============================================================================
# Purpose: Report the revenue_source_status a CREATE row will be born with, so
#   the preview promises the classification the write actually persists.
# Database/ORM: None — pure function. Mirrors the literal both registry stores
#   stamp on insert (ChannelRegistry.create_channel and
#   SqlAlchemyChannelRegistry.create_channel), which is the coupling to keep:
#   if either store's initial value changes, this must change with it or the
#   preview starts promising something the write does not do.
# Standards: Pure and total — no I/O, no store access, defined for both flag
#   values. It DISCLOSES rather than decides: the stores remain the authority
#   on what is written.
# Blast Radius: What the operator is shown before approving a roster. The
#   value feeds missing_official_revenue and the registry's recommended
#   action once persisted, so a wrong disclosure misinforms a finance
#   decision — but this function itself writes nothing.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_registry.py ->
#     create_channel (the in-memory store's initial stamp).
#   - File: backend/ums_smart_revenue/org/sql_channel_registry.py ->
#     create_channel (the SQL store's initial stamp).
#   - File: Docs/12_BACKEND_API_SPEC.md -> the disclosed row field.
# ============================================================================
def _created_revenue_source_status(revenue_required: bool) -> str:
    """The source status a CREATE will be born with.

    Mirrors what BOTH registry stores stamp on create (in-memory
    ``create_channel`` and ``SqlAlchemyChannelRegistry.create_channel``), so
    the preview promises the value the write actually persists.
    """
    return "MISSING_REVENUE_SOURCE" if revenue_required else "PERFORMANCE_ONLY"


# ============================================================================
# Purpose: Report the revenue_source_status transition an UPDATE/UNCHANGED
#   row's write will perform, or None when it leaves the classification alone.
# Database/ORM: None — pure function over the ChannelRegistryEntry the caller
#   already loaded. No lookups of its own.
# Standards: Delegates to derive_revenue_source_status rather than
#   re-implementing the rule, so the preview cannot drift from the write.
#   Returns None on a no-op ON PURPOSE: the status is re-derived only when
#   revenue_required flips, and announcing a reclassification on every roster
#   re-import would be worse than silence — it would train operators to
#   ignore the one disclosure that matters.
# Blast Radius: What the operator is shown before approving a roster, and —
#   because the write-boundary pre-state guard compares the disclosed `from`
#   against the locked row — whether a plan-bound apply is refused as 409.
#   Writes nothing itself.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_registry.py ->
#     derive_revenue_source_status (the shared rule).
#   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
#     _require_reviewed_source_status, which enforces the disclosed `from`.
# ============================================================================
def _planned_revenue_source_status(
    current: ChannelRegistryEntry, revenue_required: bool
) -> tuple[str | None, str] | None:
    """The (from, to) source status an UPDATE/UNCHANGED row's write will leave.

    Returns None when the write leaves the classification alone, which is the
    common case: ``derive_revenue_source_status`` re-derives ONLY when
    ``revenue_required`` flips, precisely so an inventory refresh cannot
    downgrade an established OFFICIAL_CMS_REVENUE / OFFICIAL_MANUAL_IMPORT.
    Disclosing a no-op would make every roster re-import look like a finance
    reclassification.
    """
    planned = derive_revenue_source_status(
        current_status=current.revenue_source_status,
        current_revenue_required=current.revenue_required,
        revenue_required=revenue_required,
    )
    if planned == current.revenue_source_status:
        return None
    return (current.revenue_source_status, planned)


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
