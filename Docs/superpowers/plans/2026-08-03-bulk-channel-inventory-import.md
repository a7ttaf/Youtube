# Bulk Channel Inventory Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `POST /channels/import` so an operator can load a full CMS channel roster (~169 channels) from a CSV in one call, making a real full-roster monthly revenue ingest possible.

**Architecture:** A pure, DB-free core (`org/channel_import.py`) parses the CSV and diffs it against the registry to produce a plan; a thin route enforces permission, executes the plan in one transaction, and writes audit rows. Storage reuses the existing `ChannelRegistryStore` and `ChannelGroupRegistryStore`, each extended with one new method. Upsert semantics are file-wins; errors are reported in batch but block the whole apply.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2.x, Alembic, pytest, ruff (100-char lines — DeepSource FLK-E501 enforces 100, not the 120 in `.deepsource.toml`).

**Spec:** `Docs/superpowers/specs/2026-08-03-bulk-channel-inventory-import-design.md`

**Branch:** `feat/bulk-channel-inventory-import` (spec committed at `be9aa352`)

---

## Conventions for every task

- **All Python commands must run through `uv run`.** A bare `python -m pytest`
  fails — alembic and the app deps live in the uv-managed environment. So:
  `uv run python -m pytest ...`, `uv run python -m ruff check ...`,
  `uv run python -m alembic -c alembic.ini ...`. Every `python -m` command in
  the tasks below is shorthand for `uv run python -m`.
- Resolved before execution, do not re-derive:
  - The global scope constructor is `AccessScope.global_scope()`.
  - The channel-group dependency is `sql_group_registry_from_session`, and it is
    **already imported** in `backend/ums_smart_revenue/api/channels.py:200`.
  - The current Alembic head is `20260620_0001`.
- Keep every touched Python line ≤ 100 characters.
- Run `uv run python -m ruff check backend tests` before each commit.
- Commit messages are trailer-free. Do not add `Co-Authored-By` or any Claude attribution — this repo's validation scans for it.
- Postgres-tier tests require `UMS_TEST_DATABASE_URL` pointing at a `test_*`-named database. `require_postgres_url` raises rather than skipping.

---

## Task 1: Add `cms_group_id` to the channel-groups schema

**Files:**
- Modify: `backend/ums_smart_revenue/db/org_models.py:142-181`
- Create: `backend/ums_smart_revenue/db/alembic/versions/20260803_0001_channel_group_cms_id.py`
- Test: `tests/db/test_channel_group_cms_id_migration.py`

- [ ] **Step 1: Write the failing test**

Create `tests/db/test_channel_group_cms_id_migration.py`:

```python
"""Schema guard for the additive channel_groups.cms_group_id column."""

from ums_smart_revenue.db.org_models import ChannelGroupORM


def test_channel_group_orm_exposes_cms_group_id() -> None:
    column = ChannelGroupORM.__table__.columns["cms_group_id"]
    assert column.nullable is True


def test_channel_group_cms_id_is_unique_per_tenant() -> None:
    constraints = {
        tuple(column.name for column in constraint.columns)
        for constraint in ChannelGroupORM.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("tenant_id", "cms_group_id") in constraints
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/db/test_channel_group_cms_id_migration.py -v`
Expected: FAIL with `KeyError: 'cms_group_id'`

- [ ] **Step 3: Add the ORM column and constraint**

In `backend/ums_smart_revenue/db/org_models.py`, inside `ChannelGroupORM`, add the column immediately after `group_type`:

```python
    cms_group_id: Mapped[str | None] = mapped_column(Text, nullable=True)
```

And add this to `__table_args__`, after the existing `UniqueConstraint`:

```python
        UniqueConstraint(
            "tenant_id",
            "cms_group_id",
            name="uq_channel_groups_tenant_id_cms_group_id",
        ),
```

- [ ] **Step 4: Write the Alembic migration**

Create `backend/ums_smart_revenue/db/alembic/versions/20260803_0001_channel_group_cms_id.py`:

```python
# ============================================================================
# Purpose: Add the additive, nullable channel_groups.cms_group_id column that
#   links a UMS channel group to its YouTube CMS group key.
# Database/ORM: channel_groups (ChannelGroupORM mirror).
# Standards: Alembic-owned DDL; additive and nullable so existing groups stay
#   valid; unique per tenant only where a CMS key is present.
# Blast Radius: Channel grouping metadata only. No finance totals, no
#   allocation, no connector behaviour.
# Connections:
#   - File: backend/ums_smart_revenue/db/org_models.py -> ORM mirror.
#   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> reader.
# ============================================================================
"""Add channel_groups.cms_group_id.

Revision ID: 20260803_0001
Revises: 20260620_0001
Create Date: 2026-08-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260803_0001"
down_revision = "20260620_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "channel_groups",
        sa.Column("cms_group_id", sa.Text(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_channel_groups_tenant_id_cms_group_id",
        "channel_groups",
        ["tenant_id", "cms_group_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_channel_groups_tenant_id_cms_group_id",
        "channel_groups",
        type_="unique",
    )
    op.drop_column("channel_groups", "cms_group_id")
```

The head was confirmed as `20260620_0001` before execution, so `down_revision`
above is correct as written. Verify it is still the only head:

Run: `uv run python -m alembic -c alembic.ini heads`
Expected: `20260620_0001 (head)`

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/db/test_channel_group_cms_id_migration.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Verify the migration is linear**

Run: `python -m alembic -c alembic.ini heads`
Expected: exactly one head, `20260803_0001`

- [ ] **Step 7: Commit**

```bash
git add backend/ums_smart_revenue/db/org_models.py \
        backend/ums_smart_revenue/db/alembic/versions/20260803_0001_channel_group_cms_id.py \
        tests/db/test_channel_group_cms_id_migration.py
git commit -m "feat(org): add channel_groups.cms_group_id"
```

---

## Task 2: Surface `cms_group_id` through the channel-group store

**Files:**
- Modify: `backend/ums_smart_revenue/org/channel_groups.py:6-51`
- Modify: `backend/ums_smart_revenue/org/sql_channel_groups.py`
- Test: `tests/org/test_channel_group_cms_lookup.py`

`ChannelGroupEntry.channel_ids` holds **`youtube_channel_id` strings**, not UUIDs — the SQL store translates internally. Preserve that.

- [ ] **Step 1: Write the failing test**

Create `tests/org/test_channel_group_cms_lookup.py`:

```python
"""In-memory channel-group store: CMS key round-trip."""

from ums_smart_revenue.org.channel_groups import ChannelGroupRegistry


def test_create_group_records_cms_group_id() -> None:
    registry = ChannelGroupRegistry()
    group = registry.create_group(
        name="TV Sector",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-tv",
    )
    assert group.cms_group_id == "cms-tv"


def test_get_group_by_cms_id_finds_the_group() -> None:
    registry = ChannelGroupRegistry()
    created = registry.create_group(
        name="TV Sector",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-tv",
    )
    assert registry.get_group_by_cms_id("cms-tv") == created


def test_get_group_by_cms_id_returns_none_when_absent() -> None:
    registry = ChannelGroupRegistry()
    assert registry.get_group_by_cms_id("cms-missing") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/org/test_channel_group_cms_lookup.py -v`
Expected: FAIL with `TypeError: create_group() got an unexpected keyword argument 'cms_group_id'`

- [ ] **Step 3: Extend the dataclass, Protocol, and in-memory store**

In `backend/ums_smart_revenue/org/channel_groups.py`, add the field to `ChannelGroupEntry` (last, so existing positional construction keeps working):

```python
    cms_group_id: str | None = None
```

Add it to `to_api`:

```python
            "cms_group_id": self.cms_group_id,
```

Add to the `ChannelGroupRegistryStore` Protocol:

```python
    def get_group_by_cms_id(self, cms_group_id: str) -> ChannelGroupEntry | None:
        pass
```

And change the Protocol's `create_group` signature to:

```python
    def create_group(
        self,
        *,
        name: str,
        group_type: str,
        channel_ids: list[str],
        cms_group_id: str | None = None,
    ) -> ChannelGroupEntry:
        pass
```

In `ChannelGroupRegistry` (in-memory), mirror the same `create_group` keyword, pass `cms_group_id=cms_group_id` into the `ChannelGroupEntry(...)` it builds, and add:

```python
    def get_group_by_cms_id(self, cms_group_id: str) -> ChannelGroupEntry | None:
        for group in self._groups.values():
            if group.cms_group_id == cms_group_id:
                return group
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/org/test_channel_group_cms_lookup.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Mirror in the SQL store**

In `backend/ums_smart_revenue/org/sql_channel_groups.py`:

- Accept `cms_group_id: str | None = None` in `create_group` and set it on the `ChannelGroupORM` row it constructs.
- Include `cms_group_id=row.cms_group_id` in `_to_entry`.
- Add:

```python
    def get_group_by_cms_id(self, cms_group_id: str) -> ChannelGroupEntry | None:
        """Return the tenant's group carrying this CMS key, or None."""
        row = self._session.execute(
            select(ChannelGroupORM).where(
                ChannelGroupORM.tenant_id == self._tenant_id,
                ChannelGroupORM.cms_group_id == cms_group_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return self._to_entry(row, channel_ids=self._channel_ids_by_group([row.id]).get(row.id, ()))
```

- [ ] **Step 6: Run the full org suite**

Run: `python -m pytest tests/org -q`
Expected: PASS, no regressions

- [ ] **Step 7: Commit**

```bash
git add backend/ums_smart_revenue/org/channel_groups.py \
        backend/ums_smart_revenue/org/sql_channel_groups.py \
        tests/org/test_channel_group_cms_lookup.py
git commit -m "feat(org): look up channel groups by CMS group id"
```

---

## Task 3: Add a general channel-inventory update to the registry store

The registry exposes only `update_mapping` and `update_content_owner`. Upsert needs to change `channel_name`, `cms_status`, `content_owner_id`, and `revenue_required` together.

Note `create_channel` derives `revenue_source_status` from `revenue_required` (`"MISSING_REVENUE_SOURCE"` when required, `"PERFORMANCE_ONLY"` otherwise). The update must keep that invariant.

**Files:**
- Modify: `backend/ums_smart_revenue/org/channel_registry.py:62-92`
- Modify: `backend/ums_smart_revenue/org/sql_channel_registry.py`
- Test: `tests/org/test_channel_inventory_update.py`

- [ ] **Step 1: Write the failing test**

Create `tests/org/test_channel_inventory_update.py`:

```python
"""In-memory registry: inventory field update."""

import pytest

from ums_smart_revenue.org.channel_registry import (
    ChannelRegistry,
    ChannelRegistryEntry,
    ChannelRegistryValidationError,
)

CHANNEL_ID = "UCB6sc84dcg6VQGB_d89sx2g"


def _registry() -> ChannelRegistry:
    return ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id=CHANNEL_ID,
                channel_name="Old Name",
                primary_company_id=None,
                cms_status="UNKNOWN",
                revenue_required=False,
                content_owner_id=None,
            )
        ]
    )


def test_update_inventory_replaces_all_four_fields() -> None:
    updated = _registry().update_inventory(
        youtube_channel_id=CHANNEL_ID,
        channel_name="CBC Egypt",
        cms_status="INSIDE_CMS",
        content_owner_id="PlZrS5Fh56RMd9dmSL6XSA",
        revenue_required=True,
    )
    assert updated.channel_name == "CBC Egypt"
    assert updated.cms_status == "INSIDE_CMS"
    assert updated.content_owner_id == "PlZrS5Fh56RMd9dmSL6XSA"
    assert updated.revenue_required is True


def test_update_inventory_keeps_revenue_source_status_consistent() -> None:
    updated = _registry().update_inventory(
        youtube_channel_id=CHANNEL_ID,
        channel_name="CBC Egypt",
        cms_status="INSIDE_CMS",
        content_owner_id="PlZrS5Fh56RMd9dmSL6XSA",
        revenue_required=True,
    )
    assert updated.revenue_source_status == "MISSING_REVENUE_SOURCE"


def test_update_inventory_rejects_unknown_channel() -> None:
    with pytest.raises(ChannelRegistryValidationError):
        _registry().update_inventory(
            youtube_channel_id="UCzzzzzzzzzzzzzzzzzzzzzz",
            channel_name="Nope",
            cms_status="INSIDE_CMS",
            content_owner_id=None,
            revenue_required=True,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/org/test_channel_inventory_update.py -v`
Expected: FAIL with `AttributeError: 'ChannelRegistry' object has no attribute 'update_inventory'`

- [ ] **Step 3: Implement on the Protocol and in-memory store**

Add to `ChannelRegistryStore` in `backend/ums_smart_revenue/org/channel_registry.py`:

```python
    def update_inventory(
        self,
        *,
        youtube_channel_id: str,
        channel_name: str,
        cms_status: str,
        content_owner_id: str | None,
        revenue_required: bool,
    ) -> ChannelRegistryEntry:
        pass
```

Implement on `ChannelRegistry` (uses `dataclasses.replace`; import it at the top of the file):

```python
    def update_inventory(
        self,
        *,
        youtube_channel_id: str,
        channel_name: str,
        cms_status: str,
        content_owner_id: str | None,
        revenue_required: bool,
    ) -> ChannelRegistryEntry:
        current = self._channels.get(youtube_channel_id)
        if current is None:
            raise ChannelRegistryValidationError(f"Unknown channel: {youtube_channel_id}")
        updated = replace(
            current,
            channel_name=channel_name,
            cms_status=cms_status,
            content_owner_id=content_owner_id,
            revenue_required=revenue_required,
            revenue_source_status=(
                "MISSING_REVENUE_SOURCE" if revenue_required else "PERFORMANCE_ONLY"
            ),
        )
        self._channels[youtube_channel_id] = updated
        return updated
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/org/test_channel_inventory_update.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Mirror in the SQL store**

Add to `SqlAlchemyChannelRegistry` in `backend/ums_smart_revenue/org/sql_channel_registry.py`, reusing the file's existing `_get_row` and `normalize_optional_content_owner` helpers:

```python
    def update_inventory(
        self,
        *,
        youtube_channel_id: str,
        channel_name: str,
        cms_status: str,
        content_owner_id: str | None,
        revenue_required: bool,
    ) -> ChannelRegistryEntry:
        """Replace a channel's inventory fields from an authoritative import row."""
        row = self._get_row(youtube_channel_id)
        if row is None:
            raise ChannelRegistryValidationError(f"Unknown channel: {youtube_channel_id}")
        row.channel_name = channel_name
        row.cms_status = cms_status
        row.content_owner_id = normalize_optional_content_owner(content_owner_id)
        row.revenue_required = revenue_required
        row.revenue_source_status = (
            "MISSING_REVENUE_SOURCE" if revenue_required else "PERFORMANCE_ONLY"
        )
        self._session.flush()
        return self._to_entry(row)
```

- [ ] **Step 6: Run the org suite**

Run: `python -m pytest tests/org -q`
Expected: PASS, no regressions

- [ ] **Step 7: Commit**

```bash
git add backend/ums_smart_revenue/org/channel_registry.py \
        backend/ums_smart_revenue/org/sql_channel_registry.py \
        tests/org/test_channel_inventory_update.py
git commit -m "feat(org): update channel inventory fields in one call"
```

---

## Task 4: Add the `CHANNEL_IMPORTED` audit event type

**Files:**
- Modify: `backend/ums_smart_revenue/auth/audit.py:18-60`
- Test: `tests/auth/test_channel_imported_audit_event.py`

- [ ] **Step 1: Write the failing test**

Create `tests/auth/test_channel_imported_audit_event.py`:

```python
"""The bulk channel import needs its own summary audit event type."""

from ums_smart_revenue.auth.audit import AuditEventType


def test_channel_imported_event_type_exists() -> None:
    assert AuditEventType.CHANNEL_IMPORTED.value == "CHANNEL_IMPORTED"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/auth/test_channel_imported_audit_event.py -v`
Expected: FAIL with `AttributeError: CHANNEL_IMPORTED`

- [ ] **Step 3: Add the enum value**

In `backend/ums_smart_revenue/auth/audit.py`, add immediately after `CHANNEL_UPDATED`:

```python
    CHANNEL_IMPORTED = "CHANNEL_IMPORTED"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/auth/test_channel_imported_audit_event.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/auth/audit.py \
        tests/auth/test_channel_imported_audit_event.py
git commit -m "feat(auth): add CHANNEL_IMPORTED audit event type"
```

---

## Task 5: CSV parser — types and the happy path

**Files:**
- Create: `backend/ums_smart_revenue/org/channel_import.py`
- Test: `tests/org/test_channel_import_parser.py`

- [ ] **Step 1: Write the failing test**

Create `tests/org/test_channel_import_parser.py`:

```python
"""Pure CSV parsing for the bulk channel import."""

from ums_smart_revenue.org.channel_import import parse_channel_import_csv

CHANNEL_ID = "UCB6sc84dcg6VQGB_d89sx2g"


def test_parses_required_columns() -> None:
    csv_text = f"youtube_channel_id,channel_name\n{CHANNEL_ID},CBC Egypt\n"
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.errors == ()
    assert len(parsed.rows) == 1
    row = parsed.rows[0]
    assert row.row_number == 1
    assert row.youtube_channel_id == CHANNEL_ID
    assert row.channel_name == "CBC Egypt"
    assert row.group_id is None
    assert row.view_revenue is None


def test_parses_optional_columns_in_any_order_and_case() -> None:
    csv_text = f"View_Revenue,Group_ID,CHANNEL_NAME,youtube_channel_id\nYes,cms-tv,CBC,{CHANNEL_ID}\n"
    parsed = parse_channel_import_csv(csv_text)
    row = parsed.rows[0]
    assert row.group_id == "cms-tv"
    assert row.view_revenue is True


def test_tolerates_utf8_bom_and_arabic_names() -> None:
    csv_text = f"﻿youtube_channel_id,channel_name\n{CHANNEL_ID},هاشتاج\n"
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.errors == ()
    assert parsed.rows[0].channel_name == "هاشتاج"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/org/test_channel_import_parser.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ums_smart_revenue.org.channel_import'`

- [ ] **Step 3: Write the parser**

Create `backend/ums_smart_revenue/org/channel_import.py`:

```python
# ============================================================================
# Purpose: Pure parsing and planning for the bulk channel inventory import.
#   Turns operator CSV text into validated rows, then diffs those rows against
#   the existing registry to produce a per-row plan the route can execute.
# Database/ORM: None. This module performs no I/O and holds no session.
# Standards: Pure functions over frozen dataclasses; every row failure is a
#   typed error carrying its 1-based row number; header problems fail the whole
#   file rather than silently dropping a column.
# Blast Radius: Channel registry inventory fields and channel-group membership.
#   No finance totals, no allocation, no connector behaviour.
# Connections:
#   - File: backend/ums_smart_revenue/api/channels.py -> route executes the plan.
#   - File: backend/ums_smart_revenue/org/channel_registry.py -> entry shape.
# ============================================================================
"""Pure CSV parsing and diff planning for bulk channel inventory import."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from enum import StrEnum

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
    row_number: int
    youtube_channel_id: str
    channel_name: str
    group_id: str | None
    view_revenue: bool | None


@dataclass(frozen=True)
class ChannelImportRowError:
    row_number: int
    reason: str


@dataclass(frozen=True)
class ParsedChannelImport:
    rows: tuple[ChannelImportRow, ...] = ()
    errors: tuple[ChannelImportRowError, ...] = ()


def _normalize_header(name: str) -> str:
    return name.strip().lstrip("﻿").lower()


def _parse_view_revenue(raw: str | None) -> bool | None:
    # Returns None when the column is absent; otherwise normalises the cell and
    # matches it against _TRUE_TOKENS / _FALSE_TOKENS, raising ValueError on an
    # unrecognised value. See the shipped implementation in
    # backend/ums_smart_revenue/org/channel_import.py.
    ...


def parse_channel_import_csv(text: str) -> ParsedChannelImport:
    """Parse operator CSV text into validated rows plus per-row errors."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    try:
        raw_header = next(reader)
    except StopIteration as exc:
        raise ChannelImportFormatError("CSV is empty") from exc

    header = [_normalize_header(name) for name in raw_header]
    missing = sorted(REQUIRED_COLUMNS - set(header))
    if missing:
        raise ChannelImportFormatError(f"missing required column(s): {', '.join(missing)}")
    unknown = sorted(set(header) - KNOWN_COLUMNS)
    if unknown:
        raise ChannelImportFormatError(f"unknown column(s): {', '.join(unknown)}")

    index = {name: position for position, name in enumerate(header)}
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

    rows, duplicate_errors = _flag_duplicates(rows)
    errors.extend(duplicate_errors)
    errors.sort(key=lambda item: item.row_number)
    return ParsedChannelImport(rows=tuple(rows), errors=tuple(errors))


def _cell(raw_row: list[str], index: dict[str, int], name: str) -> str | None:
    position = index.get(name)
    if position is None or position >= len(raw_row):
        return None
    return raw_row[position]


def _parse_row(
    row_number: int, raw_row: list[str], index: dict[str, int]
) -> ChannelImportRow | ChannelImportRowError:
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
```

Remove the unused `field` import if ruff flags it.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/org/test_channel_import_parser.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/org/channel_import.py \
        tests/org/test_channel_import_parser.py
git commit -m "feat(org): parse bulk channel import CSV"
```

---

## Task 6: CSV parser — rejection paths

**Files:**
- Test: `tests/org/test_channel_import_parser.py` (append)

- [ ] **Step 1: Append the failing tests**

Add to `tests/org/test_channel_import_parser.py`:

```python
import pytest

from ums_smart_revenue.org.channel_import import ChannelImportFormatError


def test_rejects_unknown_header() -> None:
    csv_text = f"youtube_channel_id,channel_name,revenue_usd\n{CHANNEL_ID},CBC,100\n"
    with pytest.raises(ChannelImportFormatError, match="unknown column"):
        parse_channel_import_csv(csv_text)


def test_rejects_missing_required_header() -> None:
    csv_text = "channel_name\nCBC\n"
    with pytest.raises(ChannelImportFormatError, match="missing required column"):
        parse_channel_import_csv(csv_text)


def test_flags_every_copy_of_a_duplicate_id() -> None:
    csv_text = (
        f"youtube_channel_id,channel_name\n{CHANNEL_ID},First\n{CHANNEL_ID},Second\n"
    )
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.rows == ()
    assert [error.row_number for error in parsed.errors] == [1, 2]


def test_flags_malformed_channel_id() -> None:
    csv_text = "youtube_channel_id,channel_name\nهاشتاج,CBC\n"
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.rows == ()
    assert "invalid youtube_channel_id" in parsed.errors[0].reason


def test_flags_blank_view_revenue_when_column_present() -> None:
    csv_text = f"youtube_channel_id,channel_name,view_revenue\n{CHANNEL_ID},CBC,\n"
    parsed = parse_channel_import_csv(csv_text)
    assert parsed.rows == ()
    assert "view_revenue is present but blank" in parsed.errors[0].reason


def test_skips_fully_blank_lines() -> None:
    csv_text = f"youtube_channel_id,channel_name\n{CHANNEL_ID},CBC\n\n"
    parsed = parse_channel_import_csv(csv_text)
    assert len(parsed.rows) == 1
    assert parsed.errors == ()
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/org/test_channel_import_parser.py -v`
Expected: PASS (9 passed). These paths are already implemented by Task 5; this task proves them. If any fail, fix `channel_import.py` before committing.

- [ ] **Step 3: Commit**

```bash
git add tests/org/test_channel_import_parser.py
git commit -m "test(org): cover bulk import CSV rejection paths"
```

---

## Task 7: The import planner

**Files:**
- Modify: `backend/ums_smart_revenue/org/channel_import.py`
- Test: `tests/org/test_channel_import_planner.py`

- [ ] **Step 1: Write the failing test**

Create `tests/org/test_channel_import_planner.py`:

```python
"""Pure diff planning for the bulk channel import."""

from ums_smart_revenue.org.channel_import import (
    ChannelImportOutcome,
    ChannelImportRow,
    ChannelImportRowError,
    plan_channel_import,
)
from ums_smart_revenue.org.channel_registry import ChannelRegistryEntry

CHANNEL_ID = "UCB6sc84dcg6VQGB_d89sx2g"
CONTENT_OWNER = "PlZrS5Fh56RMd9dmSL6XSA"


def _row(**overrides: object) -> ChannelImportRow:
    defaults = {
        "row_number": 1,
        "youtube_channel_id": CHANNEL_ID,
        "channel_name": "CBC Egypt",
        "group_id": None,
        "view_revenue": None,
    }
    defaults.update(overrides)
    return ChannelImportRow(**defaults)  # type: ignore[arg-type]


def _existing(**overrides: object) -> ChannelRegistryEntry:
    defaults = {
        "youtube_channel_id": CHANNEL_ID,
        "channel_name": "CBC Egypt",
        "primary_company_id": None,
        "cms_status": "INSIDE_CMS",
        "revenue_required": True,
        "content_owner_id": CONTENT_OWNER,
    }
    defaults.update(overrides)
    return ChannelRegistryEntry(**defaults)  # type: ignore[arg-type]


def test_absent_channel_plans_a_create() -> None:
    plan = plan_channel_import(
        rows=(_row(),),
        errors=(),
        existing={},
        content_owner_id=CONTENT_OWNER,
        cms_status="INSIDE_CMS",
    )
    assert plan.entries[0].outcome is ChannelImportOutcome.CREATE
    assert plan.has_errors is False


def test_identical_channel_plans_unchanged() -> None:
    plan = plan_channel_import(
        rows=(_row(),),
        errors=(),
        existing={CHANNEL_ID: _existing()},
        content_owner_id=CONTENT_OWNER,
        cms_status="INSIDE_CMS",
    )
    assert plan.entries[0].outcome is ChannelImportOutcome.UNCHANGED
    assert plan.entries[0].changes == {}


def test_differing_channel_plans_update_with_field_diff() -> None:
    plan = plan_channel_import(
        rows=(_row(channel_name="CBC Masr"),),
        errors=(),
        existing={CHANNEL_ID: _existing()},
        content_owner_id=CONTENT_OWNER,
        cms_status="INSIDE_CMS",
    )
    entry = plan.entries[0]
    assert entry.outcome is ChannelImportOutcome.UPDATE
    assert entry.changes == {"channel_name": ("CBC Egypt", "CBC Masr")}


def test_view_revenue_no_clears_revenue_required() -> None:
    plan = plan_channel_import(
        rows=(_row(view_revenue=False),),
        errors=(),
        existing={CHANNEL_ID: _existing()},
        content_owner_id=CONTENT_OWNER,
        cms_status="INSIDE_CMS",
    )
    assert plan.entries[0].changes == {"revenue_required": (True, False)}


def test_absent_view_revenue_column_defaults_to_required() -> None:
    plan = plan_channel_import(
        rows=(_row(),),
        errors=(),
        existing={},
        content_owner_id=CONTENT_OWNER,
        cms_status="INSIDE_CMS",
    )
    assert plan.entries[0].revenue_required is True


def test_row_errors_surface_and_block() -> None:
    plan = plan_channel_import(
        rows=(),
        errors=(ChannelImportRowError(row_number=2, reason="bad id"),),
        existing={},
        content_owner_id=CONTENT_OWNER,
        cms_status="INSIDE_CMS",
    )
    assert plan.has_errors is True
    assert plan.entries[0].outcome is ChannelImportOutcome.ERROR
    assert plan.counts["ERROR"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/org/test_channel_import_planner.py -v`
Expected: FAIL with `ImportError: cannot import name 'ChannelImportOutcome'`

- [ ] **Step 3: Implement the planner**

Append to `backend/ums_smart_revenue/org/channel_import.py` (and add `from collections.abc import Mapping` plus `from ums_smart_revenue.org.channel_registry import ChannelRegistryEntry` to the imports):

```python
class ChannelImportOutcome(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    UNCHANGED = "UNCHANGED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class ChannelImportPlanEntry:
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
    entries: tuple[ChannelImportPlanEntry, ...]
    counts: Mapping[str, int]

    @property
    def has_errors(self) -> bool:
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
            outcome = (
                ChannelImportOutcome.UPDATE if changes else ChannelImportOutcome.UNCHANGED
            )
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
    candidates = {
        "channel_name": (current.channel_name, channel_name),
        "cms_status": (current.cms_status, cms_status),
        "content_owner_id": (current.content_owner_id, content_owner_id),
        "revenue_required": (current.revenue_required, revenue_required),
    }
    return {name: pair for name, pair in candidates.items() if pair[0] != pair[1]}
```

Add `from types import MappingProxyType` to the imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/org/test_channel_import_planner.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Lint**

Run: `python -m ruff check backend tests`
Expected: no findings

- [ ] **Step 6: Commit**

```bash
git add backend/ums_smart_revenue/org/channel_import.py \
        tests/org/test_channel_import_planner.py
git commit -m "feat(org): plan bulk channel import against the registry"
```

---

## Task 8: The `POST /channels/import` route

**Files:**
- Modify: `backend/ums_smart_revenue/api/channels.py`
- Test: `tests/api/test_channels_import_api.py`

Follow the existing patterns in this file: `current_principal_from_headers`, `current_channel_registry`, `current_org_access_index`, `current_audit_sink`, and `record_audit_event`. Read `create_channel` at `channels.py:345-393` first.

Size and row caps: 2 MiB, 5000 data rows.

**MANDATORY — a required `reason`.** `AUDIT_EVENT_DEFINITIONS` marks
`CHANNEL_UPDATED` as `reason_required=True`, and
`audit_service.py::_normalize_audit_reason` **raises**
`ValueError(f"Audit event {event_type.value} requires a reason")` when one is
missing. Calling `record_audit_event` for an UPDATE row without a reason
therefore 500s. The endpoint takes a required `reason` form field and threads it
into every `record_audit_event` call. This is not optional polish — without it
every upsert-update row crashes the request.

(`AuditEventDefinition.permission` is classification metadata used by
`_is_sensitive_audit_record`, not an authorization check, so the
`CHANNEL_UPDATED` marker naming `MANAGE_ORG_MAPPING` does not conflict with this
route's `MANAGE_CHANNELS` gate.)

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_channels_import_api.py`. Model client/principal setup on the existing `tests/api/test_channels_api.py` — read it first and reuse its fixtures rather than inventing new ones.

```python
"""Bulk channel import route."""

from ums_smart_revenue.auth.audit import AuditEventType

CHANNEL_ID = "UCB6sc84dcg6VQGB_d89sx2g"
CONTENT_OWNER = "PlZrS5Fh56RMd9dmSL6XSA"


def _csv(body: str) -> dict[str, tuple[str, bytes, str]]:
    return {"file": ("roster.csv", body.encode("utf-8"), "text/csv")}


def _form(dry_run: bool) -> dict[str, str]:
    return {
        "content_owner_id": CONTENT_OWNER,
        "cms_status": "INSIDE_CMS",
        "dry_run": "true" if dry_run else "false",
        "reason": "August CMS roster load",
    }


def test_missing_reason_is_rejected(admin_client) -> None:
    response = admin_client.post(
        "/channels/import",
        files=_csv(f"youtube_channel_id,channel_name\n{CHANNEL_ID},CBC\n"),
        data={
            "content_owner_id": CONTENT_OWNER,
            "cms_status": "INSIDE_CMS",
            "dry_run": "false",
        },
    )
    assert response.status_code == 422


def test_import_requires_global_manage_channels(client_without_manage_channels) -> None:
    response = client_without_manage_channels.post(
        "/channels/import",
        files=_csv(f"youtube_channel_id,channel_name\n{CHANNEL_ID},CBC\n"),
        data=_form(dry_run=True),
    )
    assert response.status_code == 403


def test_dry_run_reports_create_and_writes_nothing(admin_client) -> None:
    response = admin_client.post(
        "/channels/import",
        files=_csv(f"youtube_channel_id,channel_name\n{CHANNEL_ID},CBC\n"),
        data=_form(dry_run=True),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["counts"]["CREATE"] == 1
    assert admin_client.get(f"/channels/{CHANNEL_ID}").status_code == 404


def test_apply_creates_the_channel(admin_client) -> None:
    response = admin_client.post(
        "/channels/import",
        files=_csv(f"youtube_channel_id,channel_name,view_revenue\n{CHANNEL_ID},CBC,Yes\n"),
        data=_form(dry_run=False),
    )
    assert response.status_code == 200
    assert response.json()["counts"]["CREATE"] == 1
    created = admin_client.get(f"/channels/{CHANNEL_ID}").json()
    assert created["cms_status"] == "INSIDE_CMS"
    assert created["content_owner_id"] == CONTENT_OWNER
    assert created["revenue_required"] is True


def test_any_row_error_blocks_the_whole_apply(admin_client) -> None:
    body = (
        f"youtube_channel_id,channel_name\n{CHANNEL_ID},CBC\nNOT_A_CHANNEL,Bad\n"
    )
    response = admin_client.post(
        "/channels/import", files=_csv(body), data=_form(dry_run=False)
    )
    assert response.status_code == 422
    assert admin_client.get(f"/channels/{CHANNEL_ID}").status_code == 404


def test_rerunning_the_same_file_is_unchanged(admin_client) -> None:
    body = f"youtube_channel_id,channel_name,view_revenue\n{CHANNEL_ID},CBC,Yes\n"
    admin_client.post("/channels/import", files=_csv(body), data=_form(dry_run=False))
    second = admin_client.post(
        "/channels/import", files=_csv(body), data=_form(dry_run=False)
    )
    assert second.json()["counts"]["UNCHANGED"] == 1
    assert second.json()["counts"]["UPDATE"] == 0


def test_changed_file_updates_the_channel(admin_client) -> None:
    first = f"youtube_channel_id,channel_name,view_revenue\n{CHANNEL_ID},CBC,Yes\n"
    admin_client.post("/channels/import", files=_csv(first), data=_form(dry_run=False))
    second = f"youtube_channel_id,channel_name,view_revenue\n{CHANNEL_ID},CBC Masr,Yes\n"
    response = admin_client.post(
        "/channels/import", files=_csv(second), data=_form(dry_run=False)
    )
    assert response.json()["counts"]["UPDATE"] == 1
    assert admin_client.get(f"/channels/{CHANNEL_ID}").json()["channel_name"] == "CBC Masr"


def test_group_id_creates_group_and_membership(admin_client) -> None:
    body = f"youtube_channel_id,channel_name,group_id\n{CHANNEL_ID},CBC,cms-tv\n"
    admin_client.post("/channels/import", files=_csv(body), data=_form(dry_run=False))
    groups = admin_client.get("/channel-groups").json()["groups"]
    match = [group for group in groups if group.get("cms_group_id") == "cms-tv"]
    assert len(match) == 1
    assert CHANNEL_ID in match[0]["channel_ids"]


def test_apply_writes_a_summary_audit_event(admin_client) -> None:
    body = f"youtube_channel_id,channel_name\n{CHANNEL_ID},CBC\n"
    admin_client.post("/channels/import", files=_csv(body), data=_form(dry_run=False))
    events = admin_client.get("/audit/events").json()["items"]
    types = {event["event_type"] for event in events}
    assert AuditEventType.CHANNEL_IMPORTED.value in types
    assert AuditEventType.CHANNEL_CREATED.value in types


def test_dry_run_writes_no_audit_event(admin_client) -> None:
    body = f"youtube_channel_id,channel_name\n{CHANNEL_ID},CBC\n"
    before = len(admin_client.get("/audit/events").json()["items"])
    admin_client.post("/channels/import", files=_csv(body), data=_form(dry_run=True))
    after = len(admin_client.get("/audit/events").json()["items"])
    assert after == before


def test_invalid_cms_status_is_rejected(admin_client) -> None:
    response = admin_client.post(
        "/channels/import",
        files=_csv(f"youtube_channel_id,channel_name\n{CHANNEL_ID},CBC\n"),
        data={
            "content_owner_id": CONTENT_OWNER,
            "cms_status": "NOT_A_STATUS",
            "dry_run": "true",
        },
    )
    assert response.status_code == 422


def test_row_cap_is_enforced(admin_client) -> None:
    header = "youtube_channel_id,channel_name\n"
    rows = "".join(f"UC{str(index).zfill(22)},Name{index}\n" for index in range(5001))
    response = admin_client.post(
        "/channels/import", files=_csv(header + rows), data=_form(dry_run=True)
    )
    assert response.status_code == 422
```

Adjust the group-listing endpoint path in `test_group_id_creates_group_and_membership` to whatever the repo actually exposes — check `backend/ums_smart_revenue/api/` for the channel-groups router before running.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/api/test_channels_import_api.py -v`
Expected: FAIL with 404 (route not registered)

- [ ] **Step 3: Implement the route**

Add to `backend/ums_smart_revenue/api/channels.py`. Add `File`, `Form`, `UploadFile` to the FastAPI imports, and import the pure core plus the group store dependency.

```python
MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 5000
# Mirrors ck_youtube_channels_cms_status in 20260510_0002_org_registry.
IMPORTABLE_CMS_STATUSES = frozenset({"INSIDE_CMS", "OUTSIDE_CMS", "UNKNOWN"})


# ============================================================================
# Purpose: Load a CMS channel roster from operator CSV in one call, so a full
#   169-channel ingest can run without hand-seeding the registry.
# Database/ORM: YouTubeChannelORM via ChannelRegistryStore; ChannelGroupORM and
#   ChannelGroupMemberORM via ChannelGroupRegistryStore.
# Standards: Thin route over a pure planner. Global MANAGE_CHANNELS, fail-closed.
#   Errors are reported in batch but block the entire apply — a partially
#   imported finance roster is worse than a rejected one. Dry run writes nothing,
#   including no audit row.
# Blast Radius: Channel registry inventory + group membership. A wrong
#   cms_status silently removes a channel from connector ingest, so the dry-run
#   diff is the operator's guard.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_import.py -> parse + plan.
#   - File: backend/ums_smart_revenue/connectors/google/youtube_analytics_client.py
#     -> list_target_channels requires cms_status='INSIDE_CMS' + content_owner_id.
# ============================================================================
@router.post("/import")
def import_channels(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    registry: Annotated[ChannelRegistryStore, Depends(current_channel_registry)],
    groups: Annotated[ChannelGroupRegistryStore, Depends(current_channel_group_registry)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
    file: Annotated[UploadFile, File()],
    content_owner_id: Annotated[str, Form()],
    dry_run: Annotated[bool, Form()],
    reason: Annotated[str, Form()],
    cms_status: Annotated[str, Form()] = "INSIDE_CMS",
) -> dict[str, object]:
    target_scope = AccessScope.global_scope()
    if not has_permission(user, Permission.MANAGE_CHANNELS, target_scope, org_index):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.MANAGE_CHANNELS.value}",
        )

    if cms_status not in IMPORTABLE_CMS_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid cms_status: {cms_status!r}",
        )
    if not content_owner_id.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="content_owner_id is required",
        )
    if not reason.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="reason is required",
        )

    raw = file.file.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"CSV exceeds {MAX_IMPORT_BYTES} bytes",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="CSV must be UTF-8 encoded",
        ) from exc

    try:
        parsed = parse_channel_import_csv(text)
    except ChannelImportFormatError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    if len(parsed.rows) + len(parsed.errors) > MAX_IMPORT_ROWS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"CSV exceeds {MAX_IMPORT_ROWS} rows",
        )

    wanted = {row.youtube_channel_id for row in parsed.rows}
    existing = {entry.youtube_channel_id: entry for entry in registry.list_channels_by_ids(wanted)}
    plan = plan_channel_import(
        rows=parsed.rows,
        errors=parsed.errors,
        existing=existing,
        content_owner_id=content_owner_id,
        cms_status=cms_status,
    )
    payload = _import_plan_to_api(plan, dry_run=dry_run)

    if plan.has_errors:
        if dry_run:
            return payload
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=payload
        )
    if dry_run:
        return payload

    _apply_channel_import(
        plan,
        registry=registry,
        groups=groups,
        audit_sink=audit_sink,
        actor=user,
        scope=target_scope,
        content_owner_id=content_owner_id,
        cms_status=cms_status,
        reason=reason,
    )
    record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.CHANNEL_IMPORTED,
        entity_type="youtube_channel_import",
        entity_id=content_owner_id,
        scope=target_scope,
        reason=reason,
        details={
            "filename": file.filename,
            "content_owner_id": content_owner_id,
            "cms_status": cms_status,
            "counts": dict(plan.counts),
        },
    )
    return payload


def _import_plan_to_api(plan: ChannelImportPlan, *, dry_run: bool) -> dict[str, object]:
    return {
        "dry_run": dry_run,
        "counts": dict(plan.counts),
        "rows": [
            {
                "row_number": entry.row_number,
                "youtube_channel_id": entry.youtube_channel_id,
                "outcome": entry.outcome.value,
                "changes": {
                    name: {"from": pair[0], "to": pair[1]}
                    for name, pair in entry.changes.items()
                },
                "reason": entry.reason,
            }
            for entry in plan.entries
        ],
    }


def _apply_channel_import(
    plan: ChannelImportPlan,
    *,
    registry: ChannelRegistryStore,
    groups: ChannelGroupRegistryStore,
    audit_sink: AuditSink,
    actor: UserPrincipal,
    scope: AccessScope,
    content_owner_id: str,
    cms_status: str,
    reason: str,
) -> None:
    for entry in plan.entries:
        if entry.outcome is ChannelImportOutcome.CREATE:
            registry.create_channel(
                youtube_channel_id=entry.youtube_channel_id,
                channel_name=entry.channel_name,
                primary_company_id=None,
                cms_status=cms_status,
                revenue_required=bool(entry.revenue_required),
                content_owner_id=content_owner_id,
            )
            event_type = AuditEventType.CHANNEL_CREATED
        elif entry.outcome is ChannelImportOutcome.UPDATE:
            registry.update_inventory(
                youtube_channel_id=entry.youtube_channel_id,
                channel_name=entry.channel_name,
                cms_status=cms_status,
                content_owner_id=content_owner_id,
                revenue_required=bool(entry.revenue_required),
            )
            event_type = AuditEventType.CHANNEL_UPDATED
        else:
            continue
        record_audit_event(
            sink=audit_sink,
            actor=actor,
            event_type=event_type,
            entity_type="youtube_channel",
            entity_id=entry.youtube_channel_id,
            scope=scope,
            reason=reason,
            details={
                "content_owner_id": content_owner_id,
                "cms_status": cms_status,
                "revenue_required": entry.revenue_required,
                "source": "bulk_import",
            },
        )
        if entry.group_id:
            _attach_group_membership(
                groups, cms_group_id=entry.group_id, channel_id=entry.youtube_channel_id
            )


def _attach_group_membership(
    groups: ChannelGroupRegistryStore, *, cms_group_id: str, channel_id: str
) -> None:
    group = groups.get_group_by_cms_id(cms_group_id)
    if group is None:
        groups.create_group(
            name=cms_group_id,
            group_type="SECTOR",
            channel_ids=[channel_id],
            cms_group_id=cms_group_id,
        )
        return
    if channel_id not in group.channel_ids:
        groups.add_members(group_id=group.id, channel_ids=[channel_id])
```

Both previously-open names are resolved: `AccessScope.global_scope()` is correct
as written, and the group dependency is `sql_group_registry_from_session`, which
`channels.py` already imports (see its use at line 200). Do not re-derive either.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/api/test_channels_import_api.py -v`
Expected: PASS (11 passed)

- [ ] **Step 5: Lint**

Run: `python -m ruff check backend tests`
Expected: no findings

- [ ] **Step 6: Commit**

```bash
git add backend/ums_smart_revenue/api/channels.py \
        tests/api/test_channels_import_api.py
git commit -m "feat(api): add POST /channels/import for bulk roster load"
```

---

## Task 9: Postgres-tier tenant and rollback tests

**Files:**
- Test: `tests/api/test_channels_import_postgres.py`

Requires `UMS_TEST_DATABASE_URL` against a `test_*` database. Use the repo's existing `require_postgres_url` helper — grep `tests/` for it and follow an existing Postgres-tier test's fixture setup.

- [ ] **Step 1: Write the failing tests**

Create `tests/api/test_channels_import_postgres.py`:

```python
"""Postgres-tier guarantees for the bulk channel import."""

CHANNEL_ID = "UCB6sc84dcg6VQGB_d89sx2g"
CONTENT_OWNER = "PlZrS5Fh56RMd9dmSL6XSA"


def test_imported_channels_are_tenant_isolated(pg_admin_client, pg_other_tenant_client) -> None:
    body = f"youtube_channel_id,channel_name\n{CHANNEL_ID},CBC\n"
    pg_admin_client.post(
        "/channels/import",
        files={"file": ("roster.csv", body.encode("utf-8"), "text/csv")},
        data={
            "content_owner_id": CONTENT_OWNER,
            "cms_status": "INSIDE_CMS",
            "dry_run": "false",
        },
    )
    assert pg_other_tenant_client.get(f"/channels/{CHANNEL_ID}").status_code == 404


def test_failed_apply_rolls_back_every_row(pg_admin_client) -> None:
    good = "UCB6sc84dcg6VQGB_d89sx2g"
    body = (
        f"youtube_channel_id,channel_name\n{good},CBC\nNOT_A_CHANNEL,Bad\n"
    )
    response = pg_admin_client.post(
        "/channels/import",
        files={"file": ("roster.csv", body.encode("utf-8"), "text/csv")},
        data={
            "content_owner_id": CONTENT_OWNER,
            "cms_status": "INSIDE_CMS",
            "dry_run": "false",
        },
    )
    assert response.status_code == 422
    assert pg_admin_client.get(f"/channels/{good}").status_code == 404
```

- [ ] **Step 2: Run the tests**

Run: `UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/test_ums python -m pytest tests/api/test_channels_import_postgres.py -v`

If the container is not running:

```bash
docker run -d --name ums-mig-pg-test -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=test_ums -p 55432:5432 postgres:18-alpine
```

Expected: PASS (2 passed)

- [ ] **Step 3: Commit**

```bash
git add tests/api/test_channels_import_postgres.py
git commit -m "test(api): postgres-tier tenant isolation for channel import"
```

---

## Task 10: Update the trackers

**Files:**
- Modify: `Docs/15_DELIVERY_BACKLOG.md:727-758`
- Modify: `Docs/01_IMPLEMENTATION_PLAN.md:3-16, 366, 373, 406, 421`

- [ ] **Step 1: Correct the two stale Google ingestion items**

In `Docs/15_DELIVERY_BACKLOG.md`, both currently-⏳ items ("Google source-reported revenue ingestion foundation" and "Google source-rows -> revenue facts normalization bridge") carry a remaining note naming "live Google connector credential setup (B2)" and "no live data source has produced facts yet". Both were satisfied by the 2026-06-22 live smoke and PRs #132/#134/#135. Change each `⏳` to `✅` and replace the B2 clause with:

```markdown
  B2 live credentials CLOSED — the 2026-06-22 operator smoke ran
  `run_google_connector.py --month 2026-04` against content owner
  `PlZrS5Fh56RMd9dmSL6XSA` and produced 25 `monthly_channel_revenue_facts`
  totalling $79,057.76, reconciling to the cent against the source rows
  (PRs #132, #134, #135). Remaining: FX/conversion (B3).
```

- [ ] **Step 2: Mark the bulk-inventory items**

In `Docs/01_IMPLEMENTATION_PLAN.md`, append to each of the four bulk-inventory lines (Phase 0 "At least 300+ channels listed/classified/grouped"; Phase 1 "Channel master table"; Phase 1 "Every active channel assigned or in unmapped list"; Phase 2 "Monthly revenue facts"):

```markdown
  Bulk inventory load SHIPPED — `POST /channels/import` loads a CMS roster from
  operator CSV (upsert, dry-run preview, all-or-nothing apply, CMS group
  membership). Format defined in
  `Docs/superpowers/specs/2026-08-03-bulk-channel-inventory-import-design.md`.
```

Update the Status header date at `Docs/01_IMPLEMENTATION_PLAN.md:3` to `## Status (2026-08-03)` and note reconciliation through this PR.

- [ ] **Step 3: Verify diff hygiene**

Run: `git diff --check`
Expected: no output

- [ ] **Step 4: Commit**

```bash
git add Docs/15_DELIVERY_BACKLOG.md Docs/01_IMPLEMENTATION_PLAN.md
git commit -m "docs(plan): mark bulk inventory load and close the B2 credential note"
```

---

## Task 11: Full-suite validation

- [ ] **Step 1: Lint**

Run: `python -m ruff check backend tests scripts`
Expected: no findings

- [ ] **Step 2: Line-length guard**

DeepSource FLK-E501 enforces 100 characters regardless of `.deepsource.toml`. Check every file this branch touched:

```bash
git diff --name-only origin/main...HEAD -- '*.py' | xargs -r awk 'length > 100 {print FILENAME":"FNR}'
```

Expected: no output

- [ ] **Step 3: Full backend suite**

Run: `python -m pytest -q`
Expected: PASS. Record the counts. Any failure must be fixed or proven pre-existing on `origin/main` with evidence.

- [ ] **Step 4: Postgres tier**

Run: `UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:55432/test_ums python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Migration round-trip**

Run: `python -m alembic -c alembic.ini upgrade head && python -m alembic -c alembic.ini downgrade -1 && python -m alembic -c alembic.ini upgrade head`
Expected: all three succeed; single head throughout

- [ ] **Step 6: Diff hygiene**

Run: `git diff --check`
Expected: no output

---

## Follow-up (NOT this PR)

CMS group sync — `groups.list?onBehalfOfContentOwner=<CO>&mine=true` plus
`groupItems.list` to make YouTube the source of truth for grouping and backfill
real titles into `channel_groups.name`, superseding the manual `group_id`
column. Verified available under `yt-analytics.readonly`, which the existing
credential already holds. See the spec's "Follow-up" section.
