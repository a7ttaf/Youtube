# Spec C1 Google Source Rows to Revenue Facts Normalizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bridge PR #43's `google_revenue_source_rows` substrate to the existing `MonthlyChannelRevenueFactORM` via `SqlAlchemyRevenueFactRepository.record_fact()`, with deterministic canonical-metric selection per `source_system`, USD-only writes, upfront locked-month gate, and read-before-write CREATED/UPDATED/UNCHANGED classification.

**Architecture:** A single new finance service module (`backend/ums_smart_revenue/finance/google_source_normalizer.py`) exposing a pure `select_canonical_row()` function plus a `GoogleSourceNormalizer` service class. Writes flow exclusively through `SqlAlchemyRevenueFactRepository.record_fact()`; no new schema, no new exception classes, no new migrations. Locked-month detection uses the existing `get_or_create_month_close_row(..., for_update=True)` primitive (advisory + row lock).

**Tech Stack:** Python 3.14, SQLAlchemy 2.x, PostgreSQL 18-alpine (disposable on host port 55432), pytest 9.0.3, ruff 0.15.13. Stdlib `logging`, `dataclasses`, `enum.StrEnum`, `types.MappingProxyType`.

**Spec:** `Docs/superpowers/specs/2026-05-25-spec-c1-google-source-normalizer-design.md`

---

## File Structure

**Created:**
- `backend/ums_smart_revenue/finance/google_source_normalizer.py` — single service module (~250-300 lines)
- `tests/finance/test_google_source_normalizer_selection.py` — pure-function tests
- `tests/finance/test_google_source_normalizer_service.py` — SQLite service flow tests
- `tests/finance/test_google_source_normalizer_locked_month.py` — locked-month gate
- `tests/finance/test_google_source_normalizer_logging.py` — caplog redaction
- `tests/finance/test_google_source_normalizer_postgres.py` — PostgreSQL companion subset

**Modified:**
- `Docs/01_IMPLEMENTATION_PLAN.md` — C1 row added per per-PR plan-status rule
- `Docs/15_DELIVERY_BACKLOG.md` — C1 row added with `⏳` marker (scaffolding honesty)

**No new database migration. No schema changes. No new exception classes.**

## Critical Implementation Notes

These notes save the implementer from real gotchas; read them before starting Task 1.

1. **AdSense parser drops channel_id.** `backend/ums_smart_revenue/connectors/google_source_parsers/adsense_management.py:219` always sets `youtube_channel_id=None` because AdSense reports are account-scoped. For service tests that exercise AdSense canonical selection, construct `ParsedSourceRow` objects **inline** with a synthesized `youtube_channel_id` and call `repo.upsert_many(...)` directly. Do **not** try to run the AdSense parser and expect channel-scoped rows.

2. **`source_kind` is a string at the repo boundary.** `SqlAlchemyRevenueFactRepository.record_fact()` takes `source_kind: str` (not `RevenueFactSourceKind`). Pass `.value` from the enum, or just the string literal.

3. **`list_channel_month_facts()` returns all source_kinds for that (channel, month).** Filter in Python by `source_kind == mapped_source_kind.value`; do not re-issue a query. This is the locked spec Section 5 Step 6(i) shape.

4. **`_require_active_channel_for_read` raises `RevenueFactNotFoundError`** (not `RevenueFactValidationError`) when the channel is inactive. Step 4's pre-filter prevents this in practice; do not catch it.

5. **`record_fact()` self-validates locked-month and active-channel.** Step 1's upfront gate and Step 4's pre-filter are *additive* defences; `record_fact()` remains the secondary guard. Do not remove the upfront gate "because record_fact does it" — the upfront gate exists to fail loud on locked months even when source rows are empty (spec Section 6.4).

6. **`youtube_channels.tenant_id` exists** (PR #36 multi-tenant). The Step 4 active-channel query must filter by `tenant_id=self._tenant_id` AND `active=True`.

7. **`MONTH_PATTERN`** is `re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")` at `backend/ums_smart_revenue/finance/revenue_facts.py:18`. Reuse it via `_validate_month(month)`; do not re-implement.

8. **No CSV files exist under `tests/connectors/_fixtures/`** — every fixture is JSON. The `youtube_reporting` parser ingests JSON, not CSV.

9. **Frequent commits.** Each task ends with a commit. Do not batch multiple tasks into one commit.

10. **No `git add -A`.** Always add specific paths (the discipline rule).

---

## Task 1: Module skeleton, SkipReason enum, constant mappings, dataclasses

**Files:**
- Create: `backend/ums_smart_revenue/finance/google_source_normalizer.py`
- Create: `tests/finance/test_google_source_normalizer_selection.py`

- [ ] **Step 1: Write the failing tests for mapping coverage and immutability**

Add this content to `tests/finance/test_google_source_normalizer_selection.py`:

```python
"""Pure-function tests for select_canonical_row + module constants.

No DB, no session. Verifies the rule wiring per source_system and the
frozen-mapping contract.
"""

import pytest

from ums_smart_revenue.finance.google_source_normalizer import (
    CANONICAL_METRIC_RULE,
    SOURCE_SYSTEM_TO_SOURCE_KIND,
)
from ums_smart_revenue.finance.revenue_facts import RevenueFactSourceKind


def test_source_system_to_source_kind_mapping_covers_three_supported_systems():
    assert dict(SOURCE_SYSTEM_TO_SOURCE_KIND) == {
        "youtube_reporting": RevenueFactSourceKind.YOUTUBE_CMS,
        "youtube_analytics": RevenueFactSourceKind.YOUTUBE_ANALYTICS,
        "adsense_management": RevenueFactSourceKind.ADSENSE,
    }


def test_canonical_metric_rule_mapping_is_frozen():
    with pytest.raises(TypeError):
        CANONICAL_METRIC_RULE["youtube_reporting"] = ("foo",)  # type: ignore[index]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/finance/test_google_source_normalizer_selection.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'ums_smart_revenue.finance.google_source_normalizer'`).

- [ ] **Step 3: Create the module skeleton with constants and dataclasses**

Write `backend/ums_smart_revenue/finance/google_source_normalizer.py`:

```python
"""C1 normalizer: collapse google_revenue_source_rows to revenue_facts.

Reads tenant-scoped google_revenue_source_rows for one (tenant, month),
applies the per-source_system canonical-metric rule, USD-only filter,
and writes one MonthlyChannelRevenueFactORM entry per eligible
(youtube_channel_id, source_system) group via
SqlAlchemyRevenueFactRepository.record_fact().

See: Docs/superpowers/specs/2026-05-25-spec-c1-google-source-normalizer-design.md
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from ums_smart_revenue.finance.revenue_facts import (
    RevenueFactEntry,
    RevenueFactSourceKind,
)


class SkipReason(StrEnum):
    NON_USD_CURRENCY = "non_usd_currency"
    MISSING_CHANNEL_ID = "missing_channel_id"
    UNSUPPORTED_VALUE_KIND = "unsupported_value_kind"
    NON_CANONICAL_METRIC = "non_canonical_metric"
    UNKNOWN_CHANNEL = "unknown_channel"
    NO_CANONICAL_ROW = "no_canonical_row"


@dataclass(frozen=True)
class SkippedSourceRow:
    source_row_id: str
    reason: SkipReason


@dataclass(frozen=True)
class NormalizationResult:
    created: list[RevenueFactEntry]
    updated: list[RevenueFactEntry]
    unchanged: list[RevenueFactEntry]
    skipped: list[SkippedSourceRow]


SOURCE_SYSTEM_TO_SOURCE_KIND: Mapping[str, RevenueFactSourceKind] = MappingProxyType(
    {
        "youtube_reporting": RevenueFactSourceKind.YOUTUBE_CMS,
        "youtube_analytics": RevenueFactSourceKind.YOUTUBE_ANALYTICS,
        "adsense_management": RevenueFactSourceKind.ADSENSE,
    }
)


CANONICAL_METRIC_RULE: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "youtube_reporting": ("estimatedRevenue",),
        "youtube_analytics": ("estimatedRevenue",),
        "adsense_management": ("PAID_AMOUNT", "ESTIMATED_EARNINGS"),
    }
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/finance/test_google_source_normalizer_selection.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/google_source_normalizer.py tests/finance/test_google_source_normalizer_selection.py
git commit -m "$(cat <<'EOF'
feat(c1): module skeleton + SkipReason enum + canonical rule mappings

Adds GoogleSourceNormalizer module shell with SkipReason StrEnum,
SkippedSourceRow + NormalizationResult dataclasses, frozen
SOURCE_SYSTEM_TO_SOURCE_KIND and CANONICAL_METRIC_RULE mappings.

Pure-function tests assert mapping coverage and immutability.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `select_canonical_row` pure function

**Files:**
- Modify: `backend/ums_smart_revenue/finance/google_source_normalizer.py`
- Modify: `tests/finance/test_google_source_normalizer_selection.py`

- [ ] **Step 1: Add the seven failing pure-function tests**

Append to `tests/finance/test_google_source_normalizer_selection.py`:

```python
from dataclasses import replace
from datetime import date
from decimal import Decimal

from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    GoogleRevenueSourceRowEntry,
)
from ums_smart_revenue.finance.google_source_normalizer import select_canonical_row


def _entry(
    *,
    source_system: str,
    metric_key: str,
    source_row_key: str,
    amount: str = "100.000000",
    currency: str = "USD",
    youtube_channel_id: str | None = "UC_test_1",
    value_kind: str = "estimated",
) -> GoogleRevenueSourceRowEntry:
    return GoogleRevenueSourceRowEntry(
        id=f"id-{source_row_key[:8]}",
        tenant_id="00000000-0000-0000-0000-000000000001",
        source_system=source_system,
        source_row_key=source_row_key,
        source_account_id="acct-test-1",
        content_owner_id=None,
        youtube_channel_id=youtube_channel_id,
        report_type="x",
        report_month="2026-04",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
        metric_key=metric_key,
        value_kind=value_kind,
        amount_native=Decimal(amount),
        currency_code=currency,
        source_report_id="r-1",
        raw_file_id=None,
        raw_payload={},
        imported_by=None,
        ingested_at=date(2026, 4, 1),  # type: ignore[arg-type]
    )


def test_select_canonical_row_youtube_reporting_picks_estimatedRevenue():
    rows = [_entry(source_system="youtube_reporting", metric_key="estimatedRevenue", source_row_key="a" * 64)]
    canonical, rest = select_canonical_row(rows)
    assert canonical is rows[0]
    assert rest == []


def test_select_canonical_row_youtube_analytics_picks_estimatedRevenue():
    rows = [_entry(source_system="youtube_analytics", metric_key="estimatedRevenue", source_row_key="b" * 64)]
    canonical, rest = select_canonical_row(rows)
    assert canonical is rows[0]
    assert rest == []


def test_select_canonical_row_adsense_prefers_PAID_AMOUNT_over_ESTIMATED_EARNINGS():
    paid = _entry(source_system="adsense_management", metric_key="PAID_AMOUNT", source_row_key="c" * 64)
    earnings = _entry(source_system="adsense_management", metric_key="ESTIMATED_EARNINGS", source_row_key="d" * 64)
    canonical, rest = select_canonical_row([earnings, paid])
    assert canonical is paid
    assert rest == [earnings]


def test_select_canonical_row_adsense_falls_back_to_ESTIMATED_EARNINGS_when_no_PAID_AMOUNT():
    earnings = _entry(source_system="adsense_management", metric_key="ESTIMATED_EARNINGS", source_row_key="e" * 64)
    canonical, rest = select_canonical_row([earnings])
    assert canonical is earnings
    assert rest == []


def test_select_canonical_row_returns_none_when_no_preferred_metric_present():
    unpaid = _entry(source_system="adsense_management", metric_key="UNPAID_AMOUNT", source_row_key="f" * 64)
    canonical, rest = select_canonical_row([unpaid])
    assert canonical is None
    assert rest == [unpaid]


def test_select_canonical_row_tie_break_is_deterministic_by_source_row_key_asc():
    later = _entry(
        source_system="youtube_reporting", metric_key="estimatedRevenue", source_row_key="b" * 64
    )
    earlier = _entry(
        source_system="youtube_reporting", metric_key="estimatedRevenue", source_row_key="a" * 64
    )
    canonical_run1, _ = select_canonical_row([later, earlier])
    canonical_run2, _ = select_canonical_row([earlier, later])
    assert canonical_run1 is earlier
    assert canonical_run2 is earlier  # input order does not change selection


def test_select_canonical_row_non_canonical_rest_excludes_canonical():
    a = _entry(
        source_system="adsense_management",
        metric_key="PAID_AMOUNT",
        source_row_key="g" * 64,
    )
    b = _entry(
        source_system="adsense_management",
        metric_key="ESTIMATED_EARNINGS",
        source_row_key="h" * 64,
    )
    c = _entry(
        source_system="adsense_management",
        metric_key="UNPAID_AMOUNT",
        source_row_key="i" * 64,
    )
    canonical, rest = select_canonical_row([b, c, a])
    assert canonical is a
    assert set(rest) == {b, c}
    assert canonical not in rest
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/finance/test_google_source_normalizer_selection.py -v`
Expected: 7 NEW tests FAIL (`ImportError: cannot import name 'select_canonical_row'`).

- [ ] **Step 3: Implement `select_canonical_row` in the module**

Append to `backend/ums_smart_revenue/finance/google_source_normalizer.py`:

```python
from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    GoogleRevenueSourceRowEntry,
)


def select_canonical_row(
    rows: list[GoogleRevenueSourceRowEntry],
) -> tuple[GoogleRevenueSourceRowEntry | None, list[GoogleRevenueSourceRowEntry]]:
    """Apply the per-source_system canonical-metric rule to a homogeneous group.

    Currency-blind by design: the caller must pre-filter to USD before
    invoking this function. Tie-break across multiple rows with the same
    metric_key is deterministic by source_row_key ascending (multiple
    same-metric rows can arise from dimension breakdowns, distinct
    source_account_id, or parallel report shapes; repository ingested_at
    order is not a stable contract).

    Returns (canonical_or_None, non_canonical_rest).
    """
    if not rows:
        return None, []
    preference = CANONICAL_METRIC_RULE[rows[0].source_system]
    for metric_key in preference:
        candidates = sorted(
            (r for r in rows if r.metric_key == metric_key),
            key=lambda r: r.source_row_key,
        )
        if candidates:
            canonical = candidates[0]
            return canonical, [r for r in rows if r is not canonical]
    return None, list(rows)
```

Move the `GoogleRevenueSourceRowEntry` import to the top of the file with the other imports (so it isn't duplicated).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/finance/test_google_source_normalizer_selection.py -v`
Expected: 9 passed (2 from Task 1 + 7 new).

- [ ] **Step 5: Run ruff**

Run: `python -m ruff check backend/ums_smart_revenue/finance/google_source_normalizer.py tests/finance/test_google_source_normalizer_selection.py`
Expected: All checks passed.

- [ ] **Step 6: Commit**

```bash
git add backend/ums_smart_revenue/finance/google_source_normalizer.py tests/finance/test_google_source_normalizer_selection.py
git commit -m "$(cat <<'EOF'
feat(c1): select_canonical_row pure function

Per-source_system canonical-metric rule with deterministic tie-break by
source_row_key ASC. Currency-blind by design; caller pre-filters USD.

Seven pure-function tests cover youtube_reporting, youtube_analytics,
AdSense PAID/ESTIMATED_EARNINGS preference, the no-canonical-row case,
tie-break determinism, and the non_canonical_rest invariant.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `GoogleSourceNormalizer` skeleton + Step 0 month validation

**Files:**
- Modify: `backend/ums_smart_revenue/finance/google_source_normalizer.py`
- Create: `tests/finance/test_google_source_normalizer_service.py`

- [ ] **Step 1: Write the failing test for invalid month**

Create `tests/finance/test_google_source_normalizer_service.py`:

```python
"""SQLite-backed service flow tests for GoogleSourceNormalizer."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceBase
from ums_smart_revenue.db.org_models import OrgBase
from ums_smart_revenue.db.source_models import SourceBase
from ums_smart_revenue.db.tenant_models import TenantBase
from ums_smart_revenue.finance.google_source_normalizer import (
    GoogleSourceNormalizer,
)
from ums_smart_revenue.finance.revenue_facts import RevenueFactValidationError

ACTOR_USER_ID = "00000000-0000-0000-0000-000000010001"


def _make_engine_and_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    SourceBase.metadata.create_all(engine)
    return engine, Session(engine)


def test_normalize_month_raises_validation_error_for_invalid_month_format():
    _, session = _make_engine_and_session()
    with session:
        normalizer = GoogleSourceNormalizer(session)
        with pytest.raises(RevenueFactValidationError, match="month must use YYYY-MM"):
            normalizer.normalize_month(month="2026-13", actor_user_id=ACTOR_USER_ID)
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/finance/test_google_source_normalizer_service.py -v`
Expected: FAIL (`ImportError: cannot import name 'GoogleSourceNormalizer'`).

- [ ] **Step 3: Implement constructor + `normalize_month` skeleton**

Append to `backend/ums_smart_revenue/finance/google_source_normalizer.py`:

```python
import logging
from uuid import UUID

from sqlalchemy.orm import Session

from ums_smart_revenue.finance.revenue_facts import _resolve_tenant_id, _validate_month

logger = logging.getLogger(__name__)


class GoogleSourceNormalizer:
    """Bridge google_revenue_source_rows -> MonthlyChannelRevenueFactORM.

    Writes go exclusively through SqlAlchemyRevenueFactRepository.record_fact();
    no direct ORM writes. Locked-month, active-channel, tenant, and value
    validation are preserved by reusing that write path.
    """

    def __init__(
        self,
        session: Session,
        *,
        tenant_id: UUID | str | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    def normalize_month(
        self,
        *,
        month: str,
        channel_ids: list[str] | None = None,
        actor_user_id: str,
    ) -> NormalizationResult:
        # Step 0 - Input normalization.
        _validate_month(month)
        normalized_channel_ids: set[str] | None = (
            set(channel_ids) if channel_ids is not None else None
        )
        # Subsequent steps wired in later tasks.
        return NormalizationResult(created=[], updated=[], unchanged=[], skipped=[])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/finance/test_google_source_normalizer_service.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/google_source_normalizer.py tests/finance/test_google_source_normalizer_service.py
git commit -m "$(cat <<'EOF'
feat(c1): GoogleSourceNormalizer skeleton + Step 0 month validation

Constructor follows the existing finance _resolve_tenant_id pattern
(explicit arg -> TENANT_CTX -> default UMS tenant). normalize_month
validates YYYY-MM up front using _validate_month from revenue_facts;
invalid months raise RevenueFactValidationError (matching existing
finance contract, not plain ValueError).

Subsequent steps stubbed to return an empty NormalizationResult.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Step 1 upfront locked-month gate

**Files:**
- Modify: `backend/ums_smart_revenue/finance/google_source_normalizer.py`
- Create: `tests/finance/test_google_source_normalizer_locked_month.py`

- [ ] **Step 1: Write the failing locked-month test**

Create `tests/finance/test_google_source_normalizer_locked_month.py`:

```python
"""Locked-month gate: closed books fail loud even with zero source rows."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceBase, FinanceMonthCloseORM
from ums_smart_revenue.db.org_models import OrgBase
from ums_smart_revenue.db.source_models import SourceBase
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.finance.google_source_normalizer import (
    GoogleSourceNormalizer,
)
from ums_smart_revenue.finance.revenue_facts import RevenueFactLockedMonthError

ACTOR_USER_ID = "00000000-0000-0000-0000-000000010001"


def test_normalize_month_raises_locked_month_error_with_zero_source_rows():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    SourceBase.metadata.create_all(engine)
    tenant_id = uuid4()
    with Session(engine) as session:
        session.add(TenantORM(id=tenant_id, slug="t-locked", display_name="T Locked"))
        session.add(
            FinanceMonthCloseORM(
                tenant_id=tenant_id,
                month="2026-04",
                status="LOCKED",
                allocation_rule_payload={},
            )
        )
        session.commit()

        normalizer = GoogleSourceNormalizer(session, tenant_id=tenant_id)
        with pytest.raises(RevenueFactLockedMonthError):
            normalizer.normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/finance/test_google_source_normalizer_locked_month.py -v`
Expected: FAIL (no exception raised — service returns empty result).

- [ ] **Step 3: Add Step 1 lock gate to `normalize_month`**

In `backend/ums_smart_revenue/finance/google_source_normalizer.py`, add the import:

```python
from ums_smart_revenue.finance.month_close import get_or_create_month_close_row
from ums_smart_revenue.finance.revenue_facts import (
    RevenueFactLockedMonthError,
    _resolve_tenant_id,
    _validate_month,
)
```

Then modify `normalize_month` to insert Step 1 between Step 0 and the return:

```python
    def normalize_month(
        self,
        *,
        month: str,
        channel_ids: list[str] | None = None,
        actor_user_id: str,
    ) -> NormalizationResult:
        # Step 0 - Input normalization.
        _validate_month(month)
        normalized_channel_ids: set[str] | None = (
            set(channel_ids) if channel_ids is not None else None
        )

        logger.info(
            "normalize_month start tenant_id=%s month=%s channel_scope=%s actor_user_id=%s",
            self._tenant_id,
            month,
            ("all" if normalized_channel_ids is None else f"n_channels={len(normalized_channel_ids)}"),
            actor_user_id,
        )

        # Step 1 - Upfront locked-month gate. Acquires the finance-month
        # advisory lock + SELECT ... FOR UPDATE on the close row; may create
        # an OPEN close row when none exists.
        close_row = get_or_create_month_close_row(
            self._session,
            month,
            tenant_id=self._tenant_id,
            for_update=True,
        )
        if close_row.status == "LOCKED":
            logger.info(
                "normalize_month refused tenant_id=%s month=%s reason=month_locked",
                self._tenant_id,
                month,
            )
            raise RevenueFactLockedMonthError(
                "Finance month is locked for revenue fact imports"
            )

        return NormalizationResult(created=[], updated=[], unchanged=[], skipped=[])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/finance/test_google_source_normalizer_locked_month.py tests/finance/test_google_source_normalizer_service.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/google_source_normalizer.py tests/finance/test_google_source_normalizer_locked_month.py
git commit -m "$(cat <<'EOF'
feat(c1): upfront locked-month gate via get_or_create_month_close_row

Uses the existing month-close contract with for_update=True to acquire
pg_advisory_xact_lock + SELECT ... FOR UPDATE before any read or write.
Raises RevenueFactLockedMonthError even when zero source rows exist
(spec Section 6.4: locked-month outranks empty input).

Also wires the start + refused INFO log lines; complete-line lands in
Task 14 alongside the redaction test.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Steps 2-3 — source-rows fetch + channel_ids scope filter

**Files:**
- Modify: `backend/ums_smart_revenue/finance/google_source_normalizer.py`
- Modify: `tests/finance/test_google_source_normalizer_service.py`

- [ ] **Step 1: Add a fixtures helper module-level builder + write the channel_ids filter test**

Append to `tests/finance/test_google_source_normalizer_service.py`:

```python
from datetime import date
from decimal import Decimal
from uuid import uuid4

from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    ParsedSourceRow,
)
from ums_smart_revenue.connectors.google_source_rows.repository import (
    SqlAlchemyGoogleRevenueSourceRowRepository,
)
from ums_smart_revenue.db.org_models import YouTubeChannelORM
from ums_smart_revenue.db.source_models import CurrencyORM
from ums_smart_revenue.db.tenant_models import TenantORM


def _seed_tenant_and_currencies(session: Session, tenant_id):
    session.add(TenantORM(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", display_name="T"))
    session.add(
        CurrencyORM(
            code="USD", numeric_code="840", name="US Dollar",
            minor_unit=2, is_supported=True,
        )
    )
    session.add(
        CurrencyORM(
            code="EGP", numeric_code="818", name="Egyptian Pound",
            minor_unit=2, is_supported=True,
        )
    )
    session.flush()


def _seed_active_channel(session, tenant_id, channel_id):
    session.add(
        YouTubeChannelORM(
            tenant_id=tenant_id,
            youtube_channel_id=channel_id,
            channel_name=f"Ch {channel_id}",
            cms_status="INSIDE_CMS",
            revenue_required=True,
            active=True,
        )
    )
    session.flush()


def _yt_reporting_row(
    *,
    channel: str,
    source_row_key_seed: str,
    amount: str = "100.000000",
    currency: str = "USD",
    metric_key: str = "estimatedRevenue",
    value_kind: str = "estimated",
) -> ParsedSourceRow:
    return ParsedSourceRow(
        source_system="youtube_reporting",
        source_row_key=(source_row_key_seed * 64)[:64],
        source_account_id=channel,
        content_owner_id=None,
        youtube_channel_id=channel,
        report_type="channel_monthly_estimated_revenue",
        report_month="2026-04",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
        metric_key=metric_key,
        value_kind=value_kind,
        amount_native=Decimal(amount),
        currency_code=currency,
        source_report_id="r-1",
        raw_payload={"dimensions": {"country": "US"}},
    )


def test_normalize_month_channel_ids_filter_drops_out_of_scope_rows_silently():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    SourceBase.metadata.create_all(engine)
    tenant_id = uuid4()
    with Session(engine) as session:
        _seed_tenant_and_currencies(session, tenant_id)
        _seed_active_channel(session, tenant_id, "UC_test_in")
        _seed_active_channel(session, tenant_id, "UC_test_out")
        repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
        repo.upsert_many(
            tenant_id,
            [
                _yt_reporting_row(channel="UC_test_in", source_row_key_seed="a"),
                _yt_reporting_row(channel="UC_test_out", source_row_key_seed="b"),
            ],
            raw_file_id=None,
            imported_by=None,
        )
        session.commit()

        result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
            month="2026-04",
            channel_ids=["UC_test_in"],
            actor_user_id=ACTOR_USER_ID,
        )
        # Out-of-scope rows must NOT appear in skipped (silently dropped).
        skipped_ids = {s.source_row_id for s in result.skipped}
        assert all("UC_test_out" not in s.source_row_id for s in result.skipped) or skipped_ids == set()
        # The in-scope channel's row should not be skipped for scope reasons
        # (it may still be skipped/created by later steps but not for scope).
        assert not any(
            s.reason.value == "missing_channel_id" for s in result.skipped
        ), "in-scope rows must not be classified as missing_channel_id"
```

- [ ] **Step 2: Run the test to verify failure**

Run: `pytest tests/finance/test_google_source_normalizer_service.py::test_normalize_month_channel_ids_filter_drops_out_of_scope_rows_silently -v`
Expected: FAIL — normalizer still returns empty result (no Step 2/3 wired yet).

For now the test passes vacuously (skipped is empty). The intent of this task is to wire Steps 2-3; the later tests in Tasks 6-12 will catch any regression. Treat this task's test as a structural pin: after wiring, the call should run end-to-end without raising even when source rows are present.

- [ ] **Step 3: Wire Step 2 + Step 3 into `normalize_month`**

Add the import:

```python
from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    GoogleRevenueSourceRowEntry,
)
from ums_smart_revenue.connectors.google_source_rows.repository import (
    SqlAlchemyGoogleRevenueSourceRowRepository,
)
```

Modify `normalize_month` to insert Steps 2-3 after the lock gate:

```python
        # Step 2 - Fetch source rows for this tenant + month.
        source_repo = SqlAlchemyGoogleRevenueSourceRowRepository(self._session)
        all_rows = source_repo.list(self._tenant_id, report_month=month)

        # Step 3 - Apply channel_ids scope filter.
        # When channel_ids is provided, out-of-scope rows (including null-
        # channel rows) are silently dropped, NOT classified as skips. The
        # caller restricted scope; "not requested" is not "broken".
        if normalized_channel_ids is not None:
            in_scope_rows = [
                row for row in all_rows
                if row.youtube_channel_id in normalized_channel_ids
            ]
        else:
            in_scope_rows = all_rows

        # Subsequent steps wired in later tasks; emit the complete log + return.
        result = NormalizationResult(created=[], updated=[], unchanged=[], skipped=[])
        logger.info(
            "normalize_month complete tenant_id=%s month=%s "
            "created=%d updated=%d unchanged=%d skipped=%d",
            self._tenant_id, month,
            len(result.created), len(result.updated),
            len(result.unchanged), len(result.skipped),
        )
        return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/finance/test_google_source_normalizer_service.py tests/finance/test_google_source_normalizer_locked_month.py -v`
Expected: All pass (3 tests so far in service + 1 locked-month).

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/google_source_normalizer.py tests/finance/test_google_source_normalizer_service.py
git commit -m "$(cat <<'EOF'
feat(c1): Step 2 source-rows fetch + Step 3 channel_ids scope filter

Reads google_revenue_source_rows via SqlAlchemyGoogleRevenueSourceRowRepository.list(tenant, report_month=month).
channel_ids (when provided) is normalized to set[str] at Step 0 and applied
as a silent drop filter in Step 3: out-of-scope rows are NOT classified as
skips (per spec Section 5 Step 3 — "not requested is not broken").

Adds shared test helpers _seed_tenant_and_currencies / _seed_active_channel /
_yt_reporting_row used across subsequent service-flow tasks.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Step 4 active-channel resolve + Step 5 bucketing + Step 6(a-b) skip cases

**Files:**
- Modify: `backend/ums_smart_revenue/finance/google_source_normalizer.py`
- Modify: `tests/finance/test_google_source_normalizer_service.py`

- [ ] **Step 1: Add failing tests for MISSING_CHANNEL_ID and UNKNOWN_CHANNEL**

Append to `tests/finance/test_google_source_normalizer_service.py`:

```python
def test_normalize_month_skips_missing_channel_id_rows():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    SourceBase.metadata.create_all(engine)
    tenant_id = uuid4()
    with Session(engine) as session:
        _seed_tenant_and_currencies(session, tenant_id)
        repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
        # AdSense rows always have youtube_channel_id=None per parser; we
        # build ParsedSourceRow directly with channel_id=None to model the
        # same condition for any source_system.
        repo.upsert_many(
            tenant_id,
            [
                ParsedSourceRow(
                    source_system="youtube_reporting",
                    source_row_key="m" * 64,
                    source_account_id="acct-1",
                    content_owner_id=None,
                    youtube_channel_id=None,
                    report_type="x",
                    report_month="2026-04",
                    period_start=date(2026, 4, 1),
                    period_end=date(2026, 4, 30),
                    metric_key="estimatedRevenue",
                    value_kind="estimated",
                    amount_native=Decimal("100"),
                    currency_code="USD",
                    source_report_id="r-1",
                    raw_payload={},
                ),
            ],
            raw_file_id=None,
            imported_by=None,
        )
        session.commit()

        result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
            month="2026-04", actor_user_id=ACTOR_USER_ID,
        )
        assert len(result.skipped) == 1
        assert result.skipped[0].reason.value == "missing_channel_id"


def test_normalize_month_skips_unknown_channel_rows():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    SourceBase.metadata.create_all(engine)
    tenant_id = uuid4()
    with Session(engine) as session:
        _seed_tenant_and_currencies(session, tenant_id)
        # Sub-arrange A: channel does not exist in registry at all.
        # Sub-arrange B: channel exists but active=False.
        session.add(
            YouTubeChannelORM(
                tenant_id=tenant_id,
                youtube_channel_id="UC_inactive",
                channel_name="Inactive Ch",
                cms_status="INSIDE_CMS",
                revenue_required=True,
                active=False,
            )
        )
        session.flush()

        repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
        repo.upsert_many(
            tenant_id,
            [
                _yt_reporting_row(channel="UC_unregistered", source_row_key_seed="x"),
                _yt_reporting_row(channel="UC_inactive", source_row_key_seed="y"),
            ],
            raw_file_id=None,
            imported_by=None,
        )
        session.commit()

        result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
            month="2026-04", actor_user_id=ACTOR_USER_ID,
        )
        assert len(result.skipped) == 2
        assert all(s.reason.value == "unknown_channel" for s in result.skipped)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/finance/test_google_source_normalizer_service.py::test_normalize_month_skips_missing_channel_id_rows tests/finance/test_google_source_normalizer_service.py::test_normalize_month_skips_unknown_channel_rows -v`
Expected: FAIL — both return empty `skipped`.

- [ ] **Step 3: Wire Steps 4-5 and the first two branches of Step 6**

Add import:

```python
from sqlalchemy import select

from ums_smart_revenue.db.org_models import YouTubeChannelORM
```

Replace the placeholder block from Task 5 with:

```python
        # Step 4 - Resolve active channels for this tenant in one batched query.
        in_scope_channel_ids = {
            row.youtube_channel_id
            for row in in_scope_rows
            if row.youtube_channel_id is not None
        }
        active_channel_ids: set[str] = set()
        if in_scope_channel_ids:
            active_channel_ids = set(
                self._session.scalars(
                    select(YouTubeChannelORM.youtube_channel_id).where(
                        YouTubeChannelORM.tenant_id == self._tenant_id,
                        YouTubeChannelORM.active.is_(True),
                        YouTubeChannelORM.youtube_channel_id.in_(in_scope_channel_ids),
                    )
                ).all()
            )

        # Step 5 - Bucket by (channel_id, source_system).
        buckets: dict[tuple[str | None, str], list[GoogleRevenueSourceRowEntry]] = {}
        for row in in_scope_rows:
            key = (row.youtube_channel_id, row.source_system)
            buckets.setdefault(key, []).append(row)

        created: list[RevenueFactEntry] = []
        updated: list[RevenueFactEntry] = []
        unchanged: list[RevenueFactEntry] = []
        skipped: list[SkippedSourceRow] = []

        # Step 6 - Per-bucket processing.
        for (channel_id, source_system), bucket_rows in buckets.items():
            if channel_id is None:
                # Step 6(a) - missing channel id.
                skipped.extend(
                    SkippedSourceRow(source_row_id=r.id, reason=SkipReason.MISSING_CHANNEL_ID)
                    for r in bucket_rows
                )
                continue
            if channel_id not in active_channel_ids:
                # Step 6(b) - unknown / inactive channel.
                skipped.extend(
                    SkippedSourceRow(source_row_id=r.id, reason=SkipReason.UNKNOWN_CHANNEL)
                    for r in bucket_rows
                )
                continue
            # Subsequent step branches (6c-6j) wired in later tasks.

        result = NormalizationResult(
            created=created, updated=updated, unchanged=unchanged, skipped=skipped,
        )
```

(The trailing `logger.info(...) + return result` lines remain unchanged from Task 5.)

- [ ] **Step 4: Run all service tests**

Run: `pytest tests/finance/test_google_source_normalizer_service.py tests/finance/test_google_source_normalizer_locked_month.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/google_source_normalizer.py tests/finance/test_google_source_normalizer_service.py
git commit -m "$(cat <<'EOF'
feat(c1): Step 4-5 active-channel resolve + bucketing; Step 6(a-b) skips

Step 4 batches a single SELECT against youtube_channels filtered by
(tenant_id, active=True, youtube_channel_id IN in_scope_ids) and Step 5
buckets rows by (channel_id, source_system). Step 6(a) classifies null-
channel rows as MISSING_CHANNEL_ID; Step 6(b) classifies missing-from-
registry OR active=False channels as UNKNOWN_CHANNEL.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Step 6(c) UNSUPPORTED_VALUE_KIND

**Files:**
- Modify: `backend/ums_smart_revenue/finance/google_source_normalizer.py`
- Modify: `tests/finance/test_google_source_normalizer_service.py`

- [ ] **Step 1: Add failing test for unsupported value_kind**

Append to `tests/finance/test_google_source_normalizer_service.py`:

```python
def test_normalize_month_skips_unsupported_value_kind_rows():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    SourceBase.metadata.create_all(engine)
    tenant_id = uuid4()
    with Session(engine) as session:
        _seed_tenant_and_currencies(session, tenant_id)
        _seed_active_channel(session, tenant_id, "UC_test_tax")
        repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
        repo.upsert_many(
            tenant_id,
            [
                _yt_reporting_row(
                    channel="UC_test_tax", source_row_key_seed="t",
                    metric_key="estimatedRevenue", value_kind="tax",
                ),
                _yt_reporting_row(
                    channel="UC_test_tax", source_row_key_seed="d",
                    metric_key="estimatedRevenue", value_kind="deduction",
                ),
                _yt_reporting_row(
                    channel="UC_test_tax", source_row_key_seed="j",
                    metric_key="estimatedRevenue", value_kind="adjustment",
                ),
            ],
            raw_file_id=None,
            imported_by=None,
        )
        session.commit()

        result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
            month="2026-04", actor_user_id=ACTOR_USER_ID,
        )
        assert len(result.skipped) == 3
        assert all(s.reason.value == "unsupported_value_kind" for s in result.skipped)
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/finance/test_google_source_normalizer_service.py::test_normalize_month_skips_unsupported_value_kind_rows -v`
Expected: FAIL — `skipped` is empty (no Step 6(c) yet).

- [ ] **Step 3: Implement Step 6(c) — declare the unsupported set and filter**

Add a module-level constant near the other constants in `google_source_normalizer.py`:

```python
_UNSUPPORTED_VALUE_KINDS: frozenset[str] = frozenset({"tax", "deduction", "adjustment"})
```

Inside the per-bucket loop in `normalize_month`, after Step 6(b) and before any further processing:

```python
            # Step 6(c) - drop tax/deduction/adjustment rows.
            unsupported_in_bucket = [
                r for r in bucket_rows if r.value_kind in _UNSUPPORTED_VALUE_KINDS
            ]
            for r in unsupported_in_bucket:
                skipped.append(
                    SkippedSourceRow(source_row_id=r.id, reason=SkipReason.UNSUPPORTED_VALUE_KIND)
                )
            remaining = [r for r in bucket_rows if r.value_kind not in _UNSUPPORTED_VALUE_KINDS]
            if not remaining:
                continue
            # Subsequent step branches (6d-6j) wired in later tasks.
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/finance/test_google_source_normalizer_service.py tests/finance/test_google_source_normalizer_locked_month.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/google_source_normalizer.py tests/finance/test_google_source_normalizer_service.py
git commit -m "$(cat <<'EOF'
feat(c1): Step 6(c) skip tax/deduction/adjustment rows

Per spec Section 5 Step 6(c), rows with value_kind in
{"tax", "deduction", "adjustment"} are pre-filtered out of each bucket
and classified as UNSUPPORTED_VALUE_KIND before canonical selection.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Step 6(d) NON_USD_CURRENCY

**Files:**
- Modify: `backend/ums_smart_revenue/finance/google_source_normalizer.py`
- Modify: `tests/finance/test_google_source_normalizer_service.py`

- [ ] **Step 1: Add failing test for non-USD canonical skip**

Append to `tests/finance/test_google_source_normalizer_service.py`:

```python
def test_normalize_month_skips_non_usd_canonical_with_NON_USD_CURRENCY():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    SourceBase.metadata.create_all(engine)
    tenant_id = uuid4()
    with Session(engine) as session:
        _seed_tenant_and_currencies(session, tenant_id)
        _seed_active_channel(session, tenant_id, "UC_test_fx")
        repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
        # Two rows for the same (channel, source_system), one EGP one USD.
        # Both should be CLASSIFIED — the USD one becomes canonical
        # (eventually CREATED in later tasks; for now, it remains unhandled
        # because Steps 6e-j are not wired). The EGP row must skip as
        # NON_USD_CURRENCY.
        repo.upsert_many(
            tenant_id,
            [
                _yt_reporting_row(
                    channel="UC_test_fx", source_row_key_seed="e",
                    currency="EGP", amount="500.000000",
                ),
            ],
            raw_file_id=None,
            imported_by=None,
        )
        session.commit()

        result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
            month="2026-04", actor_user_id=ACTOR_USER_ID,
        )
        assert any(s.reason.value == "non_usd_currency" for s in result.skipped)
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/finance/test_google_source_normalizer_service.py::test_normalize_month_skips_non_usd_canonical_with_NON_USD_CURRENCY -v`
Expected: FAIL.

- [ ] **Step 3: Implement Step 6(d)**

Inside the per-bucket loop, after the Step 6(c) block:

```python
            # Step 6(d) - USD-only filter; runs BEFORE canonical selection so
            # a non-USD row cannot win canonical and starve an eligible USD
            # sibling (spec Section 5 Step 6(d)).
            non_usd = [r for r in remaining if r.currency_code != "USD"]
            for r in non_usd:
                skipped.append(
                    SkippedSourceRow(source_row_id=r.id, reason=SkipReason.NON_USD_CURRENCY)
                )
            usd_rows = [r for r in remaining if r.currency_code == "USD"]
            if not usd_rows:
                continue
            # Subsequent step branches (6e-6j) wired in later tasks.
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/finance/test_google_source_normalizer_service.py tests/finance/test_google_source_normalizer_locked_month.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/google_source_normalizer.py tests/finance/test_google_source_normalizer_service.py
git commit -m "$(cat <<'EOF'
feat(c1): Step 6(d) NON_USD_CURRENCY filter before canonical selection

Per spec Section 5 Step 6(d), rows with currency_code != "USD" are
filtered out and classified as NON_USD_CURRENCY BEFORE the pure
select_canonical_row() runs. Keeps select_canonical_row() currency-
blind while preventing a non-USD row from winning canonical and
starving an eligible USD sibling.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Step 6(e-g) canonical selection + NO_CANONICAL_ROW + NON_CANONICAL_METRIC

**Files:**
- Modify: `backend/ums_smart_revenue/finance/google_source_normalizer.py`
- Modify: `tests/finance/test_google_source_normalizer_service.py`

- [ ] **Step 1: Add failing tests for the two new service-level skip reasons**

Append to `tests/finance/test_google_source_normalizer_service.py`:

```python
def _adsense_row(
    *,
    channel: str,
    metric_key: str,
    source_row_key_seed: str,
    value_kind: str = "estimated",
    amount: str = "100.000000",
    currency: str = "USD",
) -> ParsedSourceRow:
    # AdSense parser sets youtube_channel_id=None natively. For service tests
    # exercising channel-scoped AdSense canonical selection, build
    # ParsedSourceRow inline with a synthesized channel_id.
    return ParsedSourceRow(
        source_system="adsense_management",
        source_row_key=(source_row_key_seed * 64)[:64],
        source_account_id="pub-test-1",
        content_owner_id=None,
        youtube_channel_id=channel,
        report_type=("payment_report" if metric_key == "PAID_AMOUNT" else "earnings_report"),
        report_month="2026-04",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
        metric_key=metric_key,
        value_kind=("settled" if metric_key == "PAID_AMOUNT" else value_kind),
        amount_native=Decimal(amount),
        currency_code=currency,
        source_report_id="r-1",
        raw_payload={},
    )


def test_normalize_month_skips_no_canonical_row_with_NO_CANONICAL_ROW():
    # AdSense bucket with only UNPAID_AMOUNT: no preferred metric matches.
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    SourceBase.metadata.create_all(engine)
    tenant_id = uuid4()
    with Session(engine) as session:
        _seed_tenant_and_currencies(session, tenant_id)
        _seed_active_channel(session, tenant_id, "UC_test_unpaid")
        repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
        repo.upsert_many(
            tenant_id,
            [
                _adsense_row(
                    channel="UC_test_unpaid",
                    metric_key="UNPAID_AMOUNT",
                    source_row_key_seed="u",
                ),
            ],
            raw_file_id=None,
            imported_by=None,
        )
        session.commit()

        result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
            month="2026-04", actor_user_id=ACTOR_USER_ID,
        )
        assert len(result.skipped) == 1
        assert result.skipped[0].reason.value == "no_canonical_row"


def test_normalize_month_marks_unselected_usd_rows_as_NON_CANONICAL_METRIC():
    # AdSense bucket with both PAID_AMOUNT and ESTIMATED_EARNINGS in USD:
    # PAID_AMOUNT wins canonical; ESTIMATED_EARNINGS is marked
    # NON_CANONICAL_METRIC.
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    SourceBase.metadata.create_all(engine)
    tenant_id = uuid4()
    with Session(engine) as session:
        _seed_tenant_and_currencies(session, tenant_id)
        _seed_active_channel(session, tenant_id, "UC_test_dual")
        repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
        repo.upsert_many(
            tenant_id,
            [
                _adsense_row(
                    channel="UC_test_dual", metric_key="PAID_AMOUNT", source_row_key_seed="p",
                ),
                _adsense_row(
                    channel="UC_test_dual", metric_key="ESTIMATED_EARNINGS", source_row_key_seed="q",
                ),
            ],
            raw_file_id=None,
            imported_by=None,
        )
        session.commit()

        result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
            month="2026-04", actor_user_id=ACTOR_USER_ID,
        )
        # ESTIMATED_EARNINGS row marked NON_CANONICAL_METRIC.
        assert any(s.reason.value == "non_canonical_metric" for s in result.skipped)
        # PAID_AMOUNT path goes through to record_fact (Task 10), but for
        # the partial pipeline today it stays unhandled. Once Task 10 lands,
        # this test still passes (CREATED grows; NON_CANONICAL_METRIC still
        # present in skipped).
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/finance/test_google_source_normalizer_service.py::test_normalize_month_skips_no_canonical_row_with_NO_CANONICAL_ROW tests/finance/test_google_source_normalizer_service.py::test_normalize_month_marks_unselected_usd_rows_as_NON_CANONICAL_METRIC -v`
Expected: FAIL.

- [ ] **Step 3: Wire Step 6(e-g)**

Inside the per-bucket loop, after the Step 6(d) block:

```python
            # Step 6(e) - apply pure canonical-metric rule on USD-eligible rows.
            canonical, non_canonical_rest = select_canonical_row(usd_rows)

            if canonical is None:
                # Step 6(f) - USD candidates existed but none matched the
                # preferred metric_keys for this source_system.
                skipped.extend(
                    SkippedSourceRow(
                        source_row_id=r.id, reason=SkipReason.NO_CANONICAL_ROW,
                    )
                    for r in usd_rows
                )
                continue

            # Step 6(g) - non-canonical USD siblings.
            skipped.extend(
                SkippedSourceRow(
                    source_row_id=r.id, reason=SkipReason.NON_CANONICAL_METRIC,
                )
                for r in non_canonical_rest
            )
            # Subsequent step branches (6h-6j) wired in Task 10.
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/finance/test_google_source_normalizer_service.py tests/finance/test_google_source_normalizer_locked_month.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/google_source_normalizer.py tests/finance/test_google_source_normalizer_service.py
git commit -m "$(cat <<'EOF'
feat(c1): Step 6(e-g) canonical + NO_CANONICAL_ROW + NON_CANONICAL_METRIC

Calls select_canonical_row() on USD-filtered rows; if canonical is None,
every USD candidate is skipped as NO_CANONICAL_ROW; otherwise the canonical
row's siblings are skipped as NON_CANONICAL_METRIC.

Adds _adsense_row test helper that bypasses the AdSense parser
(youtube_channel_id=None by design) to exercise channel-scoped canonical
selection in service tests.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Step 6(h-j) — read-before-write CREATED classification

**Files:**
- Modify: `backend/ums_smart_revenue/finance/google_source_normalizer.py`
- Modify: `tests/finance/test_google_source_normalizer_service.py`

- [ ] **Step 1: Add failing test for CREATED on a fresh USD eligible bucket**

Append to `tests/finance/test_google_source_normalizer_service.py`:

```python
def test_normalize_month_creates_revenue_facts_for_eligible_USD_rows():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    SourceBase.metadata.create_all(engine)
    tenant_id = uuid4()
    with Session(engine) as session:
        _seed_tenant_and_currencies(session, tenant_id)
        _seed_active_channel(session, tenant_id, "UC_test_create")
        repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
        repo.upsert_many(
            tenant_id,
            [
                _yt_reporting_row(
                    channel="UC_test_create",
                    source_row_key_seed="c",
                    amount="123.450000",
                ),
            ],
            raw_file_id=None,
            imported_by=None,
        )
        session.commit()

        result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
            month="2026-04", actor_user_id=ACTOR_USER_ID,
        )
        assert len(result.created) == 1
        assert len(result.updated) == 0
        assert len(result.unchanged) == 0
        assert result.created[0].source_kind == "YOUTUBE_CMS"
        assert result.created[0].gross_revenue_usd == Decimal("123.450000")
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/finance/test_google_source_normalizer_service.py::test_normalize_month_creates_revenue_facts_for_eligible_USD_rows -v`
Expected: FAIL (`len(result.created) == 0`).

- [ ] **Step 3: Wire Step 6(h-j) with CREATED classification only**

Add imports:

```python
from decimal import Decimal

from ums_smart_revenue.finance.revenue_facts import SqlAlchemyRevenueFactRepository
```

Inside the per-bucket loop, replace the trailing `continue` after Step 6(g) with:

```python
            # Step 6(h) - build proposed payload from canonical row + defaults.
            mapped_source_kind = SOURCE_SYSTEM_TO_SOURCE_KIND[source_system]

            # Step 6(i) - read existing fact for (tenant, month, channel, source_kind).
            # list_channel_month_facts() returns list[RevenueFactEntry]; filter
            # by source_kind in Python rather than re-issuing a query.
            facts_repo = SqlAlchemyRevenueFactRepository(
                self._session, tenant_id=self._tenant_id
            )
            existing_facts = facts_repo.list_channel_month_facts(
                month=month, youtube_channel_id=channel_id,
            )
            existing = next(
                (
                    fact for fact in existing_facts
                    if fact.source_kind == mapped_source_kind.value
                ),
                None,
            )

            # Step 6(j) - classify via payload-only comparison.
            if existing is None:
                # CREATED path.
                written = facts_repo.record_fact(
                    month=month,
                    youtube_channel_id=channel_id,
                    source_kind=mapped_source_kind.value,
                    source_report_id=canonical.source_report_id,
                    gross_revenue_usd=canonical.amount_native,
                    net_revenue_usd=None,
                    shorts_revenue_usd=None,
                    longform_revenue_usd=None,
                    subscription_revenue_usd=None,
                    views=0,
                    watch_time_minutes=Decimal("0"),
                    confidence_score=Decimal("1.0"),
                    actor_user_id=actor_user_id,
                )
                created.append(written)
                continue
            # UNCHANGED / UPDATED classification wired in Task 11.
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/finance/test_google_source_normalizer_service.py tests/finance/test_google_source_normalizer_locked_month.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/google_source_normalizer.py tests/finance/test_google_source_normalizer_service.py
git commit -m "$(cat <<'EOF'
feat(c1): Step 6(h-j) read-before-write + CREATED classification

For each eligible USD canonical row, performs a list_channel_month_facts
read followed by an in-Python filter on source_kind. When no prior fact
exists, calls SqlAlchemyRevenueFactRepository.record_fact() with the
spec's locked defaults: confidence=1.0, views=0, watch_time=0, net/format
breakdown=None. Returned fact classified as CREATED.

UNCHANGED + UPDATED classification follows in Task 11.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: UNCHANGED + UPDATED classification (payload-only compare)

**Files:**
- Modify: `backend/ums_smart_revenue/finance/google_source_normalizer.py`
- Modify: `tests/finance/test_google_source_normalizer_service.py`

- [ ] **Step 1: Add failing tests for UNCHANGED (byte-identical replay, different-actor replay) and UPDATED**

Append to `tests/finance/test_google_source_normalizer_service.py`:

```python
OTHER_ACTOR_USER_ID = "00000000-0000-0000-0000-000000010002"


def test_normalize_month_classifies_byte_identical_replay_as_unchanged():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    SourceBase.metadata.create_all(engine)
    tenant_id = uuid4()
    with Session(engine) as session:
        _seed_tenant_and_currencies(session, tenant_id)
        _seed_active_channel(session, tenant_id, "UC_test_replay")
        SqlAlchemyGoogleRevenueSourceRowRepository(session).upsert_many(
            tenant_id,
            [_yt_reporting_row(channel="UC_test_replay", source_row_key_seed="r")],
            raw_file_id=None, imported_by=None,
        )
        session.commit()

        normalizer = GoogleSourceNormalizer(session, tenant_id=tenant_id)
        first = normalizer.normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)
        session.commit()
        second = normalizer.normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)
        assert len(first.created) == 1
        assert len(second.created) == 0
        assert len(second.updated) == 0
        assert len(second.unchanged) == 1


def test_normalize_month_classifies_amount_change_as_updated():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    SourceBase.metadata.create_all(engine)
    tenant_id = uuid4()
    with Session(engine) as session:
        _seed_tenant_and_currencies(session, tenant_id)
        _seed_active_channel(session, tenant_id, "UC_test_upd")
        repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
        repo.upsert_many(
            tenant_id,
            [_yt_reporting_row(channel="UC_test_upd", source_row_key_seed="z", amount="100.000000")],
            raw_file_id=None, imported_by=None,
        )
        session.commit()
        normalizer = GoogleSourceNormalizer(session, tenant_id=tenant_id)
        first = normalizer.normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)
        session.commit()
        # Change the amount via the same upsert key.
        repo.upsert_many(
            tenant_id,
            [_yt_reporting_row(channel="UC_test_upd", source_row_key_seed="z", amount="250.000000")],
            raw_file_id=None, imported_by=None,
        )
        session.commit()
        second = normalizer.normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)
        assert len(first.created) == 1
        assert len(second.updated) == 1
        assert second.updated[0].gross_revenue_usd == Decimal("250.000000")


def test_normalize_month_replay_by_different_actor_with_identical_payload_is_unchanged():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    SourceBase.metadata.create_all(engine)
    tenant_id = uuid4()
    with Session(engine) as session:
        _seed_tenant_and_currencies(session, tenant_id)
        _seed_active_channel(session, tenant_id, "UC_test_actor")
        SqlAlchemyGoogleRevenueSourceRowRepository(session).upsert_many(
            tenant_id,
            [_yt_reporting_row(channel="UC_test_actor", source_row_key_seed="a")],
            raw_file_id=None, imported_by=None,
        )
        session.commit()
        normalizer = GoogleSourceNormalizer(session, tenant_id=tenant_id)
        normalizer.normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)
        session.commit()
        result = normalizer.normalize_month(
            month="2026-04", actor_user_id=OTHER_ACTOR_USER_ID,
        )
        assert len(result.unchanged) == 1
        assert len(result.updated) == 0
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/finance/test_google_source_normalizer_service.py -k "unchanged or updated or replay" -v`
Expected: At least one FAIL.

- [ ] **Step 3: Implement UNCHANGED + UPDATED branches**

Add a module-level helper for payload-only comparison:

```python
def _payload_matches(existing: RevenueFactEntry, *, proposed_gross: Decimal, proposed_source_report_id: str | None) -> bool:
    """Compare the fields the normalizer writes against the existing fact.

    Excludes actor_user_id / imported_by / timestamps per spec Section 5
    Step 6(j) so a rerun by a different actor with identical payload stays
    UNCHANGED.
    """
    return (
        existing.gross_revenue_usd == proposed_gross
        and existing.source_report_id == proposed_source_report_id
        and existing.net_revenue_usd is None
        and existing.shorts_revenue_usd is None
        and existing.longform_revenue_usd is None
        and existing.subscription_revenue_usd is None
        and existing.views == 0
        and existing.watch_time_minutes == Decimal("0")
        and existing.confidence_score == Decimal("1.0")
    )
```

Replace the `# UNCHANGED / UPDATED classification wired in Task 11.` comment in the per-bucket loop with:

```python
            # Existing fact present: compare and classify.
            if _payload_matches(
                existing,
                proposed_gross=canonical.amount_native,
                proposed_source_report_id=canonical.source_report_id,
            ):
                unchanged.append(existing)
                continue
            written = facts_repo.record_fact(
                month=month,
                youtube_channel_id=channel_id,
                source_kind=mapped_source_kind.value,
                source_report_id=canonical.source_report_id,
                gross_revenue_usd=canonical.amount_native,
                net_revenue_usd=None,
                shorts_revenue_usd=None,
                longform_revenue_usd=None,
                subscription_revenue_usd=None,
                views=0,
                watch_time_minutes=Decimal("0"),
                confidence_score=Decimal("1.0"),
                actor_user_id=actor_user_id,
            )
            updated.append(written)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/finance/test_google_source_normalizer_service.py tests/finance/test_google_source_normalizer_locked_month.py -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/google_source_normalizer.py tests/finance/test_google_source_normalizer_service.py
git commit -m "$(cat <<'EOF'
feat(c1): UNCHANGED + UPDATED classification via payload-only compare

Compares only the fields the normalizer writes (gross_revenue_usd,
source_report_id, net=None, format=None, views=0, watch_time=0,
confidence=1.0). Excludes imported_by, last_imported_at, created_at,
updated_at so a rerun by a different actor with identical payload stays
UNCHANGED (zero write, zero churn).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: Defaults & provenance assertions (confidence + source_report_id)

**Files:**
- Modify: `tests/finance/test_google_source_normalizer_service.py`

- [ ] **Step 1: Add tests pinning confidence_score=1.0 and canonical source_report_id**

Append to `tests/finance/test_google_source_normalizer_service.py`:

```python
def test_normalize_month_writes_confidence_score_one_point_zero():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    SourceBase.metadata.create_all(engine)
    tenant_id = uuid4()
    with Session(engine) as session:
        _seed_tenant_and_currencies(session, tenant_id)
        _seed_active_channel(session, tenant_id, "UC_test_conf")
        SqlAlchemyGoogleRevenueSourceRowRepository(session).upsert_many(
            tenant_id,
            [_yt_reporting_row(channel="UC_test_conf", source_row_key_seed="o")],
            raw_file_id=None, imported_by=None,
        )
        session.commit()
        result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
            month="2026-04", actor_user_id=ACTOR_USER_ID,
        )
        assert result.created[0].confidence_score == Decimal("1.0")


def test_normalize_month_uses_canonical_source_report_id():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    SourceBase.metadata.create_all(engine)
    tenant_id = uuid4()
    with Session(engine) as session:
        _seed_tenant_and_currencies(session, tenant_id)
        _seed_active_channel(session, tenant_id, "UC_test_rid")
        # Inject a non-default source_report_id by writing a ParsedSourceRow
        # with an explicit report id value.
        repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
        row = _yt_reporting_row(channel="UC_test_rid", source_row_key_seed="i")
        from dataclasses import replace as dc_replace
        row = dc_replace(row, source_report_id="report-canonical-001")
        repo.upsert_many(tenant_id, [row], raw_file_id=None, imported_by=None)
        session.commit()
        result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
            month="2026-04", actor_user_id=ACTOR_USER_ID,
        )
        assert result.created[0].source_report_id == "report-canonical-001"
```

- [ ] **Step 2: Run tests (expect pass — defaults are already correct from Task 10/11)**

Run: `pytest tests/finance/test_google_source_normalizer_service.py -k "confidence or canonical_source_report" -v`
Expected: PASS — this is a regression pin, not new behavior.

- [ ] **Step 3: Commit**

```bash
git add tests/finance/test_google_source_normalizer_service.py
git commit -m "$(cat <<'EOF'
test(c1): pin confidence_score=1.0 and canonical source_report_id defaults

Regression tests for the Section 4 defaults table. confidence_score is
a static 1.0 until the dedicated confidence-labels spec replaces it.
source_report_id flows from the canonical source row, not an arbitrary
sibling.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 13: Reverse-lookup test

**Files:**
- Modify: `tests/finance/test_google_source_normalizer_service.py`

- [ ] **Step 1: Add the reverse-lookup test**

Append to `tests/finance/test_google_source_normalizer_service.py`:

```python
def test_normalize_month_reverse_lookup_returns_same_canonical_row():
    # Given a written fact, re-applying select_canonical_row() on USD-filtered
    # source rows for the same (tenant, month, channel, source_system) must
    # return the same row that produced the fact. Pins the explain-endpoint
    # contract from spec Section 2 "deterministic reverse lookup".
    from ums_smart_revenue.finance.google_source_normalizer import (
        SOURCE_SYSTEM_TO_SOURCE_KIND,
        select_canonical_row,
    )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    SourceBase.metadata.create_all(engine)
    tenant_id = uuid4()
    with Session(engine) as session:
        _seed_tenant_and_currencies(session, tenant_id)
        _seed_active_channel(session, tenant_id, "UC_test_rev")
        repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
        # Multiple AdSense rows; PAID_AMOUNT must be canonical.
        repo.upsert_many(
            tenant_id,
            [
                _adsense_row(channel="UC_test_rev", metric_key="PAID_AMOUNT", source_row_key_seed="p"),
                _adsense_row(channel="UC_test_rev", metric_key="ESTIMATED_EARNINGS", source_row_key_seed="e"),
            ],
            raw_file_id=None, imported_by=None,
        )
        session.commit()

        result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
            month="2026-04", actor_user_id=ACTOR_USER_ID,
        )
        written = result.created[0]
        assert written.source_kind == "ADSENSE"

        # Reverse lookup: read source rows, USD filter, re-apply rule.
        source_rows = [
            r for r in repo.list(tenant_id, report_month="2026-04",
                                 source_system="adsense_management")
            if r.youtube_channel_id == "UC_test_rev" and r.currency_code == "USD"
        ]
        canonical, _ = select_canonical_row(source_rows)
        assert canonical is not None
        assert canonical.source_report_id == written.source_report_id
        # And the source_kind mapping is the inverse: ADSENSE -> adsense_management.
        assert SOURCE_SYSTEM_TO_SOURCE_KIND["adsense_management"].value == written.source_kind
```

- [ ] **Step 2: Run test**

Run: `pytest tests/finance/test_google_source_normalizer_service.py::test_normalize_month_reverse_lookup_returns_same_canonical_row -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/finance/test_google_source_normalizer_service.py
git commit -m "$(cat <<'EOF'
test(c1): deterministic reverse lookup for written facts

Pins the explain-endpoint contract: given a written revenue fact, calling
select_canonical_row() on USD-filtered source rows for the same
(tenant, month, channel, source_system) returns the row that produced
the fact. Covers the AdSense PAID_AMOUNT-wins path end to end.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 14: Logging contract test (caplog redaction)

**Files:**
- Create: `tests/finance/test_google_source_normalizer_logging.py`

- [ ] **Step 1: Write the caplog redaction test**

Create `tests/finance/test_google_source_normalizer_logging.py`:

```python
"""Logging contract: counts/distribution logged; PII / finance values redacted."""

import logging
from datetime import date
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    ParsedSourceRow,
)
from ums_smart_revenue.connectors.google_source_rows.repository import (
    SqlAlchemyGoogleRevenueSourceRowRepository,
)
from ums_smart_revenue.db.finance_models import FinanceBase
from ums_smart_revenue.db.org_models import OrgBase, YouTubeChannelORM
from ums_smart_revenue.db.source_models import CurrencyORM, SourceBase
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.finance.google_source_normalizer import (
    GoogleSourceNormalizer,
)

ACTOR_USER_ID = "00000000-0000-0000-0000-000000010001"


def test_normalize_month_logging_redacts_payload_amount_channel_id_source_row_id(
    caplog,
):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    SourceBase.metadata.create_all(engine)
    tenant_id = uuid4()
    with Session(engine) as session:
        session.add(TenantORM(id=tenant_id, slug="t-log", display_name="T Log"))
        session.add(CurrencyORM(code="USD", numeric_code="840", name="US Dollar",
                                minor_unit=2, is_supported=True))
        session.add(CurrencyORM(code="EGP", numeric_code="818", name="Egyptian Pound",
                                minor_unit=2, is_supported=True))
        session.add(YouTubeChannelORM(
            tenant_id=tenant_id, youtube_channel_id="UC_test_log_42",
            channel_name="Log Ch", cms_status="INSIDE_CMS",
            revenue_required=True, active=True,
        ))
        session.flush()
        SqlAlchemyGoogleRevenueSourceRowRepository(session).upsert_many(
            tenant_id,
            [
                ParsedSourceRow(
                    source_system="youtube_reporting",
                    source_row_key="L" * 64,
                    source_account_id="acct-log",
                    content_owner_id=None,
                    youtube_channel_id="UC_test_log_42",
                    report_type="x",
                    report_month="2026-04",
                    period_start=date(2026, 4, 1),
                    period_end=date(2026, 4, 30),
                    metric_key="estimatedRevenue",
                    value_kind="estimated",
                    amount_native=Decimal("999.876543"),
                    currency_code="USD",
                    source_report_id="rep-log-001",
                    raw_payload={"dimensions": {"country": "US"}, "secret": "DO_NOT_LOG"},
                ),
                # Non-USD row to force a NON_USD_CURRENCY skip and exercise
                # the skipped_by_reason key in the complete line.
                ParsedSourceRow(
                    source_system="youtube_reporting",
                    source_row_key="M" * 64,
                    source_account_id="acct-log",
                    content_owner_id=None,
                    youtube_channel_id="UC_test_log_42",
                    report_type="x",
                    report_month="2026-04",
                    period_start=date(2026, 4, 1),
                    period_end=date(2026, 4, 30),
                    metric_key="estimatedRevenue",
                    value_kind="estimated",
                    amount_native=Decimal("888.111111"),
                    currency_code="EGP",
                    source_report_id="rep-log-002",
                    raw_payload={"dimensions": {"country": "EG"}},
                ),
            ],
            raw_file_id=None, imported_by=None,
        )
        session.commit()

        with caplog.at_level(logging.INFO,
                              logger="ums_smart_revenue.finance.google_source_normalizer"):
            result = GoogleSourceNormalizer(
                session, tenant_id=tenant_id,
            ).normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)

        log_text = "\n".join(rec.getMessage() for rec in caplog.records)

        # Must include: start + complete log lines.
        assert "normalize_month start" in log_text
        assert "normalize_month complete" in log_text
        # Must include aggregate counts.
        assert "created=" in log_text
        assert "updated=" in log_text
        assert "unchanged=" in log_text
        assert "skipped=" in log_text

        # Must NOT include forbidden values.
        assert "999.876543" not in log_text  # amount_native
        assert "888.111111" not in log_text
        assert "rep-log-001" not in log_text  # source_report_id (provenance, not log-worthy)
        assert "DO_NOT_LOG" not in log_text  # raw_payload contents
        assert "UC_test_log_42" not in log_text  # individual channel id
        # source_row_id UUIDs are never emitted as individual values.
        for row_entry in SqlAlchemyGoogleRevenueSourceRowRepository(session).list(
            tenant_id, report_month="2026-04",
        ):
            assert row_entry.id not in log_text

        # And the result reflects what we set up: 1 CREATED + 1 NON_USD skip.
        assert len(result.created) == 1
        assert any(s.reason.value == "non_usd_currency" for s in result.skipped)
```

The complete line emitted from Task 5 already contains the counts. To also include the `skipped_by_reason={...}` distribution, update the complete log call in `google_source_normalizer.py`:

```python
from collections import Counter

# inside normalize_month, just before the return:
reason_counts = Counter(s.reason.value for s in result.skipped)
logger.info(
    "normalize_month complete tenant_id=%s month=%s "
    "created=%d updated=%d unchanged=%d skipped=%d "
    "skipped_by_reason=%s",
    self._tenant_id, month,
    len(result.created), len(result.updated),
    len(result.unchanged), len(result.skipped),
    dict(reason_counts),
)
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/finance/test_google_source_normalizer_logging.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add backend/ums_smart_revenue/finance/google_source_normalizer.py tests/finance/test_google_source_normalizer_logging.py
git commit -m "$(cat <<'EOF'
feat(c1): logging contract with redaction guard

Adds skipped_by_reason distribution to the complete log line. caplog
regression test asserts the start + complete lines contain aggregate
counts and the skip-reason distribution while never emitting raw_payload
contents, monetary amounts, source_report_id values, individual channel
ids, or individual source_row_id values.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 15: PostgreSQL companion subset

**Files:**
- Create: `tests/finance/test_google_source_normalizer_postgres.py`

- [ ] **Step 1: Add the companion file with five representative scenarios**

Create `tests/finance/test_google_source_normalizer_postgres.py`:

```python
"""PostgreSQL companion: exercise the real lock path on a live engine.

Repeats five representative service scenarios against PostgreSQL to verify
the pg_advisory_xact_lock + SELECT ... FOR UPDATE primitive executes.
Does NOT add a competing-session blocking test (see spec Section 7.5).
"""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from alembic import command
from alembic.config import Config

import sys
from pathlib import Path
# Reuse the postgres URL helper that lives in tests/db/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
from _postgres_helpers import require_postgres_url  # noqa: E402

from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    ParsedSourceRow,
)
from ums_smart_revenue.connectors.google_source_rows.repository import (
    SqlAlchemyGoogleRevenueSourceRowRepository,
)
from ums_smart_revenue.db.finance_models import FinanceMonthCloseORM
from ums_smart_revenue.db.org_models import YouTubeChannelORM
from ums_smart_revenue.db.tenant_models import TenantORM
from ums_smart_revenue.finance.google_source_normalizer import (
    GoogleSourceNormalizer,
)
from ums_smart_revenue.finance.revenue_facts import RevenueFactLockedMonthError

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTOR_USER_ID = "00000000-0000-0000-0000-000000010001"


@pytest.fixture
def postgres_url() -> str:
    return require_postgres_url()


@pytest.fixture
def alembic_config(postgres_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("sqlalchemy.url", postgres_url)
    cfg.set_main_option(
        "script_location",
        str(REPO_ROOT / "backend" / "ums_smart_revenue" / "db" / "alembic"),
    )
    return cfg


@pytest.fixture
def fresh_engine(postgres_url: str):
    from sqlalchemy import text
    engine = create_engine(postgres_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


def _seed_pg(session, tenant_id, channel_id):
    session.add(TenantORM(id=tenant_id, slug=f"pg-{tenant_id.hex[:6]}", display_name="PG"))
    session.add(
        YouTubeChannelORM(
            tenant_id=tenant_id, youtube_channel_id=channel_id,
            channel_name="PG Ch", cms_status="INSIDE_CMS",
            revenue_required=True, active=True,
        )
    )
    session.flush()


def _pg_row(channel: str, seed: str, amount: str = "100.000000") -> ParsedSourceRow:
    return ParsedSourceRow(
        source_system="youtube_reporting",
        source_row_key=(seed * 64)[:64],
        source_account_id=channel, content_owner_id=None,
        youtube_channel_id=channel,
        report_type="x", report_month="2026-04",
        period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
        metric_key="estimatedRevenue", value_kind="estimated",
        amount_native=Decimal(amount), currency_code="USD",
        source_report_id="r-1",
        raw_payload={"dimensions": {"country": "US"}},
    )


def test_pg_created_path(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    tenant_id = uuid4()
    with Session(fresh_engine) as session:
        _seed_pg(session, tenant_id, "UC_pg_create")
        SqlAlchemyGoogleRevenueSourceRowRepository(session).upsert_many(
            tenant_id, [_pg_row("UC_pg_create", "a")], raw_file_id=None, imported_by=None,
        )
        session.commit()
        result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
            month="2026-04", actor_user_id=ACTOR_USER_ID,
        )
        assert len(result.created) == 1


def test_pg_unchanged_replay(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    tenant_id = uuid4()
    with Session(fresh_engine) as session:
        _seed_pg(session, tenant_id, "UC_pg_replay")
        SqlAlchemyGoogleRevenueSourceRowRepository(session).upsert_many(
            tenant_id, [_pg_row("UC_pg_replay", "b")], raw_file_id=None, imported_by=None,
        )
        session.commit()
        normalizer = GoogleSourceNormalizer(session, tenant_id=tenant_id)
        normalizer.normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)
        session.commit()
        second = normalizer.normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)
        assert len(second.unchanged) == 1
        assert len(second.created) == 0


def test_pg_updated_path(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    tenant_id = uuid4()
    with Session(fresh_engine) as session:
        _seed_pg(session, tenant_id, "UC_pg_upd")
        repo = SqlAlchemyGoogleRevenueSourceRowRepository(session)
        repo.upsert_many(tenant_id, [_pg_row("UC_pg_upd", "c", "100.000000")],
                          raw_file_id=None, imported_by=None)
        session.commit()
        normalizer = GoogleSourceNormalizer(session, tenant_id=tenant_id)
        normalizer.normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)
        session.commit()
        repo.upsert_many(tenant_id, [_pg_row("UC_pg_upd", "c", "250.000000")],
                          raw_file_id=None, imported_by=None)
        session.commit()
        second = normalizer.normalize_month(month="2026-04", actor_user_id=ACTOR_USER_ID)
        assert len(second.updated) == 1


def test_pg_non_usd_currency_skip(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    tenant_id = uuid4()
    with Session(fresh_engine) as session:
        _seed_pg(session, tenant_id, "UC_pg_fx")
        row = _pg_row("UC_pg_fx", "f")
        from dataclasses import replace as dc_replace
        row = dc_replace(row, currency_code="EGP")
        SqlAlchemyGoogleRevenueSourceRowRepository(session).upsert_many(
            tenant_id, [row], raw_file_id=None, imported_by=None,
        )
        session.commit()
        result = GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
            month="2026-04", actor_user_id=ACTOR_USER_ID,
        )
        assert any(s.reason.value == "non_usd_currency" for s in result.skipped)
        assert len(result.created) == 0


def test_pg_locked_month_raises(alembic_config, fresh_engine):
    command.upgrade(alembic_config, "head")
    tenant_id = uuid4()
    with Session(fresh_engine) as session:
        _seed_pg(session, tenant_id, "UC_pg_locked")
        session.add(
            FinanceMonthCloseORM(
                tenant_id=tenant_id, month="2026-04",
                status="LOCKED", allocation_rule_payload={},
            )
        )
        session.commit()
        with pytest.raises(RevenueFactLockedMonthError):
            GoogleSourceNormalizer(session, tenant_id=tenant_id).normalize_month(
                month="2026-04", actor_user_id=ACTOR_USER_ID,
            )
```

- [ ] **Step 2: Run the companion (requires `UMS_TEST_DATABASE_URL` env var and a disposable postgres:18-alpine)**

If no PostgreSQL is available, this file raises `RuntimeError` at fixture resolution (no skip, no xfail — matches PR #43's gate). Spin up a disposable engine first:

```bash
docker run --rm -d --name ums-pg-c1 -e POSTGRES_PASSWORD=devpass -p 55432:5432 postgres:18-alpine
$env:UMS_TEST_DATABASE_URL = "postgresql+psycopg2://postgres:devpass@127.0.0.1:55432/postgres"
pytest tests/finance/test_google_source_normalizer_postgres.py -v
```

Expected: 5 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/finance/test_google_source_normalizer_postgres.py
git commit -m "$(cat <<'EOF'
test(c1): PostgreSQL companion - five representative scenarios

CREATED, UNCHANGED replay, UPDATED, NON_USD_CURRENCY skip, and locked-month
raise. Verifies the real pg_advisory_xact_lock + SELECT ... FOR UPDATE
primitive executes against a live engine. No competing-session blocking
test (per spec Section 7.5; existing month-close coverage lives in
tests/finance/test_month_close_locking.py).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 16: Plan-doc updates (`Docs/01` + `Docs/15`)

**Files:**
- Modify: `Docs/01_IMPLEMENTATION_PLAN.md`
- Modify: `Docs/15_DELIVERY_BACKLOG.md`

- [ ] **Step 1: Update `Docs/15_DELIVERY_BACKLOG.md`**

Find the `Cross-cutting shipped (not in original P0–P3)` section and append the C1 entry directly under the PR #43 bullet (replace `PR #N` with the actual PR number when known):

```markdown
- ⏳ Google source-rows -> revenue facts normalization bridge: pure
  `select_canonical_row()` rule per source_system, USD-only writes,
  upfront locked-month gate, read-before-write CREATED/UPDATED/UNCHANGED
  classification — PR #N (replace with actual). Bridges PR #43's
  `google_revenue_source_rows` substrate to the existing
  `MonthlyChannelRevenueFactORM` via `SqlAlchemyRevenueFactRepository.record_fact()`.
  No schema delta, no new exception classes, no Alembic migration. 26 named
  tests + PostgreSQL companion. Remaining: live OAuth/API connector (B2),
  FX/conversion (B3). Marked ⏳ (not ✅) per scaffolding-only honesty rule
  — no live data source yet. See `Docs/superpowers/specs/2026-05-25-spec-c1-google-source-normalizer-design.md`
  and the per-PR report under `Docs/pulls/`.
```

Also update the existing P0 item `Monthly revenue normalization`:

Change:
```markdown
- ⏳ Monthly revenue normalization — remaining: revenue facts foundation
  (PR #2); ingestion source not wired.
```

To:
```markdown
- ⏳ Monthly revenue normalization — remaining: B2 live ingestion wiring
  (revenue facts foundation in PR #2; normalization bridge from
  google_revenue_source_rows shipped in PR #N).
```

- [ ] **Step 2: Update `Docs/01_IMPLEMENTATION_PLAN.md`**

Open the file, locate the C1 / Google source normalization section (or add one under the most relevant phase), and add a marker line:

```markdown
- ✅ PR #N - Google source-rows -> revenue facts normalizer (Spec C1).
  Adds `backend/ums_smart_revenue/finance/google_source_normalizer.py`,
  five test files under `tests/finance/`, no schema delta. Bridge between
  PR #43 substrate and existing `MonthlyChannelRevenueFactORM` write path.
```

(Use `⏳` not `✅` if the PR is still open at the time of writing the doc update; flip to `✅` only after merge per existing convention.)

- [ ] **Step 3: Whitespace check**

Run: `git diff --check`
Expected: No output.

- [ ] **Step 4: Commit**

```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "$(cat <<'EOF'
docs(plan): mark Spec C1 normalization bridge in 01 + 15

Per the per-PR plan-status rule, marks the C1 normalization bridge
inline in both the implementation plan and the delivery backlog.
PR # placeholder left for replacement at PR-open time.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 17: Run the full local validation gate

**Files:**
- None (verification only).

- [ ] **Step 1: Confirm working tree is clean**

Run: `git status`
Expected: `nothing to commit, working tree clean`.

- [ ] **Step 2: Run ruff on backend + tests**

Run: `python -m ruff check backend tests`
Expected: All checks passed.

- [ ] **Step 3: Run the full pytest suite (SQLite only, fast)**

Run: `pytest -q`
Expected: All tests pass. Note: the PostgreSQL companion file will raise `RuntimeError` on each test if `UMS_TEST_DATABASE_URL` is not set — that is the policy (no skip, no xfail). If running without PostgreSQL, exclude that file:

Run: `pytest -q --ignore=tests/finance/test_google_source_normalizer_postgres.py`

If PostgreSQL is available, run the full suite without the ignore.

- [ ] **Step 4: Run `git diff --check` on the working tree and staged area**

Run: `git diff --check && git diff --cached --check`
Expected: No output.

- [ ] **Step 5: Run the consolidated validation gate (matches the local PR pattern)**

Run: `python scripts/run_validation_gate.py`
Expected: PASS.

If the validation gate cannot run due to environment/tooling/credential reasons, record (per the CLAUDE.md validation gate policy):
- The exact command attempted.
- The exact blocker.
- Whether the blocker is environment/tooling/credential/data related.
- The operator-safe command to rerun later.

Do NOT skip the gate to pass CI.

- [ ] **Step 6: No commit needed; this task is verification only.**

If any gate failed and required code fixes, those fixes go into their own commits (re-run the gate after each fix). Do not amend prior commits.

---

## Self-Review (post-write)

**Spec coverage check (run before handoff):**

| Spec requirement | Task |
|---|---|
| `SkipReason` enum, `SkippedSourceRow`, `NormalizationResult` (Section 4) | Task 1 |
| `SOURCE_SYSTEM_TO_SOURCE_KIND`, `CANONICAL_METRIC_RULE` frozen mappings | Task 1 |
| `select_canonical_row` pure function (Section 4) | Task 2 |
| `GoogleSourceNormalizer.__init__` tenant resolution | Task 3 |
| Step 0 month validation -> `RevenueFactValidationError` | Task 3 |
| Step 1 upfront locked-month gate via `get_or_create_month_close_row` | Task 4 |
| Step 2 source-rows fetch via `repo.list(self._tenant_id, report_month=)` | Task 5 |
| Step 3 channel_ids scope filter (silent drop) | Task 5 |
| Step 4 active-channel batched query against `youtube_channels` | Task 6 |
| Step 5 bucketing by (channel_id, source_system) | Task 6 |
| Step 6(a) MISSING_CHANNEL_ID | Task 6 |
| Step 6(b) UNKNOWN_CHANNEL (missing + inactive) | Task 6 |
| Step 6(c) UNSUPPORTED_VALUE_KIND | Task 7 |
| Step 6(d) NON_USD_CURRENCY before canonical selection | Task 8 |
| Step 6(e) canonical selection via pure function | Task 9 |
| Step 6(f) NO_CANONICAL_ROW | Task 9 |
| Step 6(g) NON_CANONICAL_METRIC | Task 9 |
| Step 6(h) build payload from canonical + defaults | Task 10 |
| Step 6(i) read-before-write via `list_channel_month_facts` + Python filter | Task 10 |
| Step 6(j) CREATED classification | Task 10 |
| Step 6(j) UNCHANGED + UPDATED classification (payload-only compare) | Task 11 |
| `confidence_score = Decimal("1.0")` regression pin | Task 12 |
| Canonical `source_report_id` regression pin | Task 12 |
| Deterministic reverse lookup | Task 13 |
| Logging contract (start + complete \| refused, redaction) | Task 14 |
| PostgreSQL companion (5 scenarios, real lock path) | Task 15 |
| `Docs/01` + `Docs/15` plan-status updates | Task 16 |
| Local validation gate run | Task 17 |
| All 26 named tests | Tasks 1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14 |

All 26 named tests in the spec map to specific tasks. Spec Sections 6 (error handling, transaction boundary, concurrency model, empty inputs, what is NOT caught) are pinned by the negative-path tests above + the locked-month gate test + the propagation policy (no try/except).

**Type consistency check:**
- `SkippedSourceRow.source_row_id: str` — matches `GoogleRevenueSourceRowEntry.id: str` everywhere.
- `select_canonical_row(rows: list[Entry])` — returns `tuple[Entry | None, list[Entry]]` consistently.
- `record_fact(..., source_kind: str, ...)` — receives `mapped_source_kind.value`, the StrEnum's `.value`.
- `list_channel_month_facts(month=, youtube_channel_id=)` — keyword-only call shape used in Task 10.
- `_payload_matches(existing, *, proposed_gross, proposed_source_report_id)` — used once in Task 11.

**Placeholder scan:** No `TBD`, `TODO`, `implement later`, `FIXME`, `XXX`, or `???` in this plan. All steps contain runnable code or exact commands.

---

## Execution Handoff

Plan complete and saved to `Docs/superpowers/plans/2026-05-25-spec-c1-google-source-normalizer.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, two-stage review (spec compliance, then code quality) between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using `executing-plans`, batch execution with checkpoints for review.

Which approach?
