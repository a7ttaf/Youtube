# Account-Level Deduction Allocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Distribute ACCOUNT-grain `deduction_components` evidence across the verified channel↔account map by source-aligned raw-gross-proportional share, exposed via a read-only month endpoint; unmappable/incomplete accounts stay UNALLOCATED with blocking issues. No persistence, no migration, no `net_revenue` change.

**Architecture:** A pure DB-free service (`finance/allocation.py`) computes the split; a thin route (`api/allocation.py`, mounted in `app.py`) gathers inputs via existing repositories (a new ACCOUNT-only `list_account_components` query, the Spec 2a map read contract, and monthly revenue facts) and serializes the result. Read-only; PostgreSQL stays source of truth; no graph projection impact.

**Tech Stack:** FastAPI, SQLAlchemy 2.x, Pydantic, Decimal arithmetic, pytest + SQLite for tests.

**Spec:** `Docs/superpowers/specs/2026-05-31-spec-account-allocation-design.md` (approved + patched). Branch `spec/account-allocation` off `main` (`714fde8`).

**Standing constraints:** strict TDD; complete code in every step; NO `Co-Authored-By`/Claude trailer in any commit; do NOT push or open a PR (a separate finishing step handles that after approval). No Postgres tier needed (no migration, no advisory lock) — SQLite suffices.

---

### Verified anchors (do not re-derive; quote-checked against `714fde8`)

- `DeductionComponent` dataclass + `to_api()` (omits `raw_payload`): `backend/ums_smart_revenue/finance/deduction_components.py:46`. Fields: `id, month, component_kind, scope_kind, scope_id, amount_usd, amount_native, currency_code, source_system, source_table, source_id, source_key, source_report_id, raw_payload, component_key`.
- Repository `SqlAlchemyDeductionComponentRepository(session, *, tenant_id=None)`, `_validate_month`, `DeductionComponentValidationError`, `_to_entry`: `backend/ums_smart_revenue/finance/deduction_ingestion.py:143,110,54,413`.
- Constants `NET_APPLICABLE_COMPONENT_KINDS = frozenset({"TAX","DEDUCTION"})`, `SOURCE_SYSTEM_TO_SOURCE_KIND = {"adsense_management":"ADSENSE","youtube_reporting":"YOUTUBE_CMS","youtube_analytics":"YOUTUBE_ANALYTICS"}`: `backend/ums_smart_revenue/finance/net_revenue.py:21`.
- `RevenueFactEntry` (`youtube_channel_id, source_kind, gross_revenue_usd, month, ...`) + `SqlAlchemyRevenueFactRepository.list_month_facts(*, month, ...)` which **JOINs `YouTubeChannelORM` and filters `active IS TRUE`**: `backend/ums_smart_revenue/finance/revenue_facts.py:36,196`.
- `decimal_to_api`: `backend/ums_smart_revenue/finance/decimal_formatting.py` (`from ums_smart_revenue.finance.decimal_formatting import decimal_to_api`).
- Map read contract + `_resolve_tenant_id` + `self._tenant_id`: `backend/ums_smart_revenue/finance/channel_account_links.py:129,204,675`. `list_verified_adsense_account_channels(self, *, tenant_id, month, adsense_account_id) -> list[str]`.
- Auth: `AccessScope.global_scope()`, `AccessScope.finance_month(month)` (`auth/scopes.py`); `Permission.VIEW_REVENUE`, `Permission.VIEW_FINALIZED_PAYMENTS` (`auth/permissions.py`); `AuditEventType.REVENUE_VIEWED`, `PAYMENT_VIEWED` (`auth/audit.py`); `record_audit_event(*, sink, actor, event_type, entity_type=None, entity_id=None, scope=None, details=None, ...)` (`auth/audit_service.py`).
- Route/test wiring template: `api/channel_account_links.py` (providers, `_require_permission`, GET handler), `api/channels.py` (`current_audit_sink`, `audit_record_to_api`), `api/dependencies.py` (`current_db_session`, `current_principal_from_headers`), `app.py` (router mounting).
- Seed ORMs (all under `db/finance_models.py` / `db/org_models.py`): `MonthlyChannelRevenueFactORM` (108), `AdsenseContentOwnerLinkORM` (712), `ContentOwnerChannelLinkORM` (803), `DeductionComponentORM` (588), `YouTubeChannelORM` (`org_models.py:76`).

---

## Task 1: ACCOUNT-only repository query

**Files:**
- Modify: `backend/ums_smart_revenue/finance/deduction_ingestion.py` (add method to `SqlAlchemyDeductionComponentRepository`, after `list_month_components_page`)
- Test: `tests/finance/test_deduction_ingestion.py` (append tests + a small insert helper)

- [ ] **Step 1: Write the failing tests**

Append to `tests/finance/test_deduction_ingestion.py`:

```python
def _add_component(
    session,
    *,
    scope_kind,
    scope_id,
    component_kind="DEDUCTION",
    amount="10.00",
    source_system="adsense_management",
    key=None,
):
    """Insert one DeductionComponentORM row for the shared MONTH."""
    session.add(
        DeductionComponentORM(
            id=uuid4(),
            tenant_id=_ums_tenant(),
            month=MONTH,
            component_kind=component_kind,
            scope_kind=scope_kind,
            scope_id=scope_id,
            amount_usd=Decimal(amount),
            currency_code="USD",
            source_system=source_system,
            source_table="google_revenue_source_rows",
            component_key=key or f"{scope_kind}:{scope_id}:{component_kind}",
            raw_payload={},
        )
    )


def test_list_account_components_returns_only_account_scope(tmp_path):
    """Only scope_kind == ACCOUNT rows are returned; CHANNEL/PAYMENT excluded."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _add_component(session, scope_kind="ACCOUNT", scope_id="pub-1")
        _add_component(session, scope_kind="CHANNEL", scope_id="chan-1")
        _add_component(
            session, scope_kind="PAYMENT", scope_id="BANK-1",
            source_system="bank_reconciliation",
        )
        session.commit()
        repo = _mod().SqlAlchemyDeductionComponentRepository(session)
        rows = repo.list_account_components(month=MONTH)
    assert [r.scope_kind for r in rows] == ["ACCOUNT"]
    assert rows[0].scope_id == "pub-1"


def test_list_account_components_filters_by_account(tmp_path):
    """The optional adsense_account_id narrows to one account's scope_id."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _add_component(session, scope_kind="ACCOUNT", scope_id="pub-1", key="a")
        _add_component(session, scope_kind="ACCOUNT", scope_id="pub-2", key="b")
        session.commit()
        repo = _mod().SqlAlchemyDeductionComponentRepository(session)
        rows = repo.list_account_components(month=MONTH, adsense_account_id="pub-2")
    assert [r.scope_id for r in rows] == ["pub-2"]


def test_list_account_components_rejects_malformed_month(tmp_path):
    """A malformed month raises DeductionComponentValidationError."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = _mod().SqlAlchemyDeductionComponentRepository(session)
        with pytest.raises(_mod().DeductionComponentValidationError):
            repo.list_account_components(month="2026-13")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/finance/test_deduction_ingestion.py -k list_account_components -v`
Expected: FAIL — `AttributeError: 'SqlAlchemyDeductionComponentRepository' object has no attribute 'list_account_components'`.

- [ ] **Step 3: Implement the method**

In `backend/ums_smart_revenue/finance/deduction_ingestion.py`, add to `SqlAlchemyDeductionComponentRepository` (immediately after `list_month_components_page`, before `_to_entry`):

```python
    # ========================================================================
    # Purpose: Return all ACCOUNT-scoped deduction components for one finance
    #   month — the allocation engine's input domain. Filters scope_kind ==
    #   "ACCOUNT" in SQL so PAYMENT/CHANNEL/bank-grain rows are never fetched.
    # Database/ORM: Reads deduction_components / DeductionComponentORM.
    # Standards: deterministic ORDER BY; tenant-scoped; no write path touched.
    # Blast Radius: Finance read only. No auth/Neo4j/ingestion impact.
    # Connections:
    #   - File: backend/ums_smart_revenue/api/allocation.py -> account allocation.
    # ========================================================================
    def list_account_components(
        self, *, month: str, adsense_account_id: str | None = None
    ) -> list[DeductionComponent]:
        """List ACCOUNT-scoped deduction components for one finance month.

        When adsense_account_id is provided, restricts to that account's
        scope_id. PAYMENT/CHANNEL-grain rows are excluded at the query layer.

        Raises:
            DeductionComponentValidationError: If the month is malformed.
        """
        _validate_month(month)
        query = (
            select(DeductionComponentORM)
            .where(DeductionComponentORM.tenant_id == self._tenant_id)
            .where(DeductionComponentORM.month == month)
            .where(DeductionComponentORM.scope_kind == "ACCOUNT")
        )
        if adsense_account_id is not None:
            query = query.where(DeductionComponentORM.scope_id == adsense_account_id)
        rows = self._session.scalars(
            query.order_by(
                DeductionComponentORM.scope_id,
                DeductionComponentORM.component_kind,
                DeductionComponentORM.component_key,
            )
        ).all()
        return [self._to_entry(row) for row in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/finance/test_deduction_ingestion.py -k list_account_components -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/deduction_ingestion.py tests/finance/test_deduction_ingestion.py
git commit -m "feat(finance): ACCOUNT-only deduction-component query for allocation"
```

---

## Task 2: Allocation service foundations

**Files:**
- Create: `backend/ums_smart_revenue/finance/allocation.py`
- Test: `tests/finance/test_allocation.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/finance/test_allocation.py`:

```python
from decimal import Decimal

from ums_smart_revenue.finance import allocation


def test_basis_source_kind_maps_known_systems():
    """source_system resolves to the matching raw-gross source kind."""
    assert allocation._basis_source_kind("adsense_management") == "ADSENSE"
    assert allocation._basis_source_kind("youtube_reporting") == "YOUTUBE_CMS"
    assert allocation._basis_source_kind("youtube_analytics") == "YOUTUBE_ANALYTICS"


def test_basis_source_kind_payment_gap_uses_adsense():
    """The AdSense payment-gap source maps to ADSENSE gross."""
    assert allocation._basis_source_kind("adsense_payment_gap") == "ADSENSE"


def test_basis_source_kind_unknown_returns_none():
    """An unresolvable source_system returns None (caller fails closed)."""
    assert allocation._basis_source_kind("bank_reconciliation") is None


def test_proportional_allocation_conserves_amount_exactly():
    """Largest-remainder split sums back to the input amount to 1e-6."""
    weights = [("a", Decimal("2")), ("b", Decimal("1"))]
    result = allocation._proportional_allocation(Decimal("9.000000"), weights)
    assert result["a"] == Decimal("6.000000")
    assert result["b"] == Decimal("3.000000")
    assert sum(result.values()) == Decimal("9.000000")


def test_proportional_allocation_residual_is_deterministic():
    """1/3 split: residual micro-unit goes to the deterministic tiebreak."""
    weights = [("c3", Decimal("1")), ("c1", Decimal("1")), ("c2", Decimal("1"))]
    result = allocation._proportional_allocation(Decimal("1.000000"), weights)
    assert sum(result.values()) == Decimal("1.000000")
    # equal remainders -> channel_id ascending wins the leftover unit
    assert result["c1"] == Decimal("0.333334")
    assert result["c2"] == Decimal("0.333333")
    assert result["c3"] == Decimal("0.333333")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/finance/test_allocation.py -v`
Expected: FAIL — `ModuleNotFoundError`/`AttributeError` (module/functions not defined).

- [ ] **Step 3: Create the module foundations**

Create `backend/ums_smart_revenue/finance/allocation.py`:

```python
"""Pure account-level deduction allocation (Phase 4 Spec 2b PR-1).

Distributes ACCOUNT-grain deduction components across the verified
channel↔account map by source-aligned raw-gross-proportional share. No
database access: the caller resolves every input. See
Docs/superpowers/specs/2026-05-31-spec-account-allocation-design.md.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from ums_smart_revenue.finance.deduction_components import DeductionComponent
from ums_smart_revenue.finance.net_revenue import (
    NET_APPLICABLE_COMPONENT_KINDS,
    SOURCE_SYSTEM_TO_SOURCE_KIND,
)

ALLOCATION_METHOD = "gross_revenue_proportional"
_SCALE = Decimal("0.000001")  # 6dp, matches deduction_components.amount_usd
_PAYMENT_GAP_SOURCE_SYSTEM = "adsense_payment_gap"


class AllocationError(Exception):
    """Base class for allocation errors."""


class AllocationValidationError(AllocationError):
    """Raised for malformed allocation input."""


def _basis_source_kind(source_system: str) -> str | None:
    """Resolve the raw-gross source kind that weights this component's split.

    Mirrors net_revenue's source alignment; the AdSense payment-gap source has
    no entry in the map and is special-cased to ADSENSE. Returns None when the
    source_system is unresolvable (the caller records BASIS_MISSING).
    """
    if source_system in SOURCE_SYSTEM_TO_SOURCE_KIND:
        return SOURCE_SYSTEM_TO_SOURCE_KIND[source_system]
    if source_system == _PAYMENT_GAP_SOURCE_SYSTEM:
        return "ADSENSE"
    return None


def _proportional_allocation(
    amount: Decimal, weights: list[tuple[str, Decimal]]
) -> dict[str, Decimal]:
    """Split `amount` across (channel, basis) weights, conserving to 1e-6.

    Largest-remainder (Hamilton) apportionment: floor each share to 6dp, then
    hand the leftover micro-units to the largest fractional remainders
    (channel_id ascending as the deterministic tiebreak). Requires
    basis_total > 0; conserves exactly: sum(result.values()) == amount.
    """
    basis_total = sum((weight for _, weight in weights), Decimal("0"))
    floors: dict[str, Decimal] = {}
    remainders: list[tuple[Decimal, str]] = []
    allocated = Decimal("0")
    for channel_id, weight in weights:
        exact = amount * weight / basis_total
        floor_value = exact.quantize(_SCALE, rounding=ROUND_FLOOR)
        floors[channel_id] = floor_value
        remainders.append((exact - floor_value, channel_id))
        allocated += floor_value
    leftover_units = int(((amount - allocated) / _SCALE).to_integral_value())
    order = sorted(remainders, key=lambda item: (-item[0], item[1]))
    for index in range(leftover_units):
        floors[order[index][1]] += _SCALE
    return floors
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/finance/test_allocation.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/allocation.py tests/finance/test_allocation.py
git commit -m "feat(finance): allocation service foundations (basis resolver + apportionment)"
```

---

## Task 3: `build_account_allocation` (full allocator)

**Files:**
- Modify: `backend/ums_smart_revenue/finance/allocation.py` (add dataclasses + `build_account_allocation`)
- Test: `tests/finance/test_allocation.py` (append behavior tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/finance/test_allocation.py`:

```python
def _component(
    *,
    scope_id="pub-1",
    component_kind="DEDUCTION",
    amount="100.00",
    source_system="adsense_management",
    scope_kind="ACCOUNT",
    key="k1",
):
    """Build a DeductionComponent for the allocator (only used fields matter)."""
    return DeductionComponent(
        id="id-" + key,
        month="2026-04",
        component_kind=component_kind,
        scope_kind=scope_kind,
        scope_id=scope_id,
        amount_usd=Decimal(amount),
        amount_native=None,
        currency_code="USD",
        source_system=source_system,
        source_table="google_revenue_source_rows",
        source_id=None,
        source_key=None,
        source_report_id=None,
        raw_payload={},
        component_key=key,
    )


from ums_smart_revenue.finance.allocation import build_account_allocation  # noqa: E402
from ums_smart_revenue.finance.deduction_components import DeductionComponent  # noqa: E402


def test_allocates_by_source_aligned_gross_and_conserves():
    """A $100 ADSENSE deduction splits 70/30 by ADSENSE gross; sum == 100."""
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="100.00")],
        verified_channels={"pub-1": ["chA", "chB"]},
        gross_basis={("chA", "ADSENSE"): Decimal("700"), ("chB", "ADSENSE"): Decimal("300")},
    )
    by_channel = {ln.youtube_channel_id: ln.allocated_amount_usd for ln in result.lines}
    assert by_channel == {"chA": Decimal("70.000000"), "chB": Decimal("30.000000")}
    assert all(ln.net_applicable for ln in result.lines)
    assert result.summary.allocated_total_usd == Decimal("100.000000")
    assert result.summary.unallocated_total_usd == Decimal("0")


def test_single_channel_gets_full_amount():
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="42.50")],
        verified_channels={"pub-1": ["only"]},
        gross_basis={("only", "ADSENSE"): Decimal("5")},
    )
    assert result.lines[0].allocated_amount_usd == Decimal("42.500000")


def test_source_alignment_ignores_other_source_kind_gross():
    """An adsense_management component must not weight by YOUTUBE_CMS gross."""
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="10.00", source_system="adsense_management")],
        verified_channels={"pub-1": ["chA"]},
        gross_basis={("chA", "YOUTUBE_CMS"): Decimal("999")},  # wrong source kind
    )
    assert result.lines == ()
    assert result.unallocated[0].issue_code == "BASIS_MISSING"


def test_net_applicable_buckets_split_correctly():
    """TAX/DEDUCTION are net-applicable; UNRESOLVED_PAYMENT_GAP is reconciliation."""
    result = build_account_allocation(
        month="2026-04",
        components=[
            _component(component_kind="DEDUCTION", amount="10.00", key="d"),
            _component(
                component_kind="UNRESOLVED_PAYMENT_GAP", amount="4.00",
                source_system="adsense_payment_gap", key="g",
            ),
        ],
        verified_channels={"pub-1": ["chA"]},
        gross_basis={("chA", "ADSENSE"): Decimal("100")},
    )
    assert result.summary.net_applicable_total_usd == Decimal("10.000000")
    assert result.summary.reconciliation_total_usd == Decimal("4.000000")
    assert (
        result.summary.net_applicable_total_usd
        + result.summary.reconciliation_total_usd
        == result.summary.allocated_total_usd
    )


def test_zero_amount_component_allocated_with_no_lines():
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="0.00")],
        verified_channels={"pub-1": ["chA"]},
        gross_basis={("chA", "ADSENSE"): Decimal("100")},
    )
    assert result.lines == ()
    assert result.summary.allocated_component_count == 1
    assert result.summary.unallocated_component_count == 0


def test_unmapped_account_is_unallocated():
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="10.00", scope_id="pub-x")],
        verified_channels={},
        gross_basis={},
    )
    assert result.unallocated[0].issue_code == "ACCOUNT_UNMAPPED_OR_UNVERIFIED"
    assert result.summary.unallocated_total_usd == Decimal("10.000000")


def test_incomplete_basis_fails_closed():
    """One verified channel lacks source-aligned gross -> whole component blocked."""
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="10.00")],
        verified_channels={"pub-1": ["chA", "chB"]},
        gross_basis={("chA", "ADSENSE"): Decimal("100")},  # chB absent
    )
    assert result.lines == ()
    assert result.unallocated[0].issue_code == "BASIS_INCOMPLETE"


def test_present_zero_basis_is_valid_when_total_positive():
    """A present-zero channel is valid (gets 0); only absent triggers incomplete."""
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="10.00")],
        verified_channels={"pub-1": ["chA", "chB"]},
        gross_basis={("chA", "ADSENSE"): Decimal("100"), ("chB", "ADSENSE"): Decimal("0")},
    )
    by_channel = {ln.youtube_channel_id: ln.allocated_amount_usd for ln in result.lines}
    assert by_channel == {"chA": Decimal("10.000000"), "chB": Decimal("0.000000")}


def test_zero_total_basis_is_unallocated():
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="10.00")],
        verified_channels={"pub-1": ["chA"]},
        gross_basis={("chA", "ADSENSE"): Decimal("0")},
    )
    assert result.unallocated[0].issue_code == "ZERO_GROSS_BASIS"


def test_unsupported_scope_is_guarded():
    """A non-ACCOUNT component handed to the service is surfaced, not dropped."""
    result = build_account_allocation(
        month="2026-04",
        components=[_component(scope_kind="PAYMENT", scope_id="BANK-1", amount="5.00")],
        verified_channels={},
        gross_basis={},
    )
    assert result.unallocated[0].issue_code == "UNSUPPORTED_SCOPE"
    assert result.summary.unallocated_total_usd == Decimal("5.000000")


def test_channel_in_multiple_accounts_emits_informational_note():
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="10.00", scope_id="pub-1", key="a")],
        verified_channels={"pub-1": ["shared"], "pub-2": ["shared"]},
        gross_basis={("shared", "ADSENSE"): Decimal("100")},
    )
    assert any(n.note_code == "CHANNEL_IN_MULTIPLE_ACCOUNTS" for n in result.notes)
    # allocation still proceeds for pub-1
    assert result.lines[0].allocated_amount_usd == Decimal("10.000000")


def test_aggregate_conservation_across_components():
    result = build_account_allocation(
        month="2026-04",
        components=[
            _component(amount="100.00", scope_id="pub-1", key="a"),
            _component(amount="7.00", scope_id="pub-2", key="b"),
            _component(amount="3.00", scope_id="pub-x", key="c"),  # unmapped
        ],
        verified_channels={"pub-1": ["chA", "chB"], "pub-2": ["chC"]},
        gross_basis={
            ("chA", "ADSENSE"): Decimal("1"),
            ("chB", "ADSENSE"): Decimal("1"),
            ("chC", "ADSENSE"): Decimal("9"),
        },
    )
    total_in = Decimal("110.00")
    assert (
        result.summary.allocated_total_usd + result.summary.unallocated_total_usd
        == total_in
    )


def test_allocates_negative_amount_preserving_sign_and_conservation():
    """A negative UNRESOLVED_PAYMENT_GAP (settled - paid) splits with sign + exact sum."""
    result = build_account_allocation(
        month="2026-04",
        components=[
            _component(
                component_kind="UNRESOLVED_PAYMENT_GAP", amount="-9.00",
                source_system="adsense_payment_gap", key="neg",
            )
        ],
        verified_channels={"pub-1": ["chA", "chB"]},
        gross_basis={("chA", "ADSENSE"): Decimal("2"), ("chB", "ADSENSE"): Decimal("1")},
    )
    by_channel = {ln.youtube_channel_id: ln.allocated_amount_usd for ln in result.lines}
    assert by_channel == {"chA": Decimal("-6.000000"), "chB": Decimal("-3.000000")}
    assert sum(by_channel.values()) == Decimal("-9.000000")
    assert all(not ln.net_applicable for ln in result.lines)  # reconciliation bucket
    assert result.summary.reconciliation_total_usd == Decimal("-9.000000")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/finance/test_allocation.py -k "allocat or basis or scope or note or conserv or single or net_applicable or unmapped or zero" -v`
Expected: FAIL — `ImportError: cannot import name 'build_account_allocation'`.

- [ ] **Step 3: Implement the dataclasses + `build_account_allocation`**

Append to `backend/ums_smart_revenue/finance/allocation.py`:

```python
@dataclass(frozen=True)
class AllocationLine:
    """One ACCOUNT component's allocated share for one channel."""

    adsense_account_id: str
    youtube_channel_id: str
    component_kind: str
    source_system: str
    component_key: str
    basis_source_kind: str
    basis_gross_usd: Decimal
    basis_share: Decimal
    allocated_amount_usd: Decimal
    net_applicable: bool


@dataclass(frozen=True)
class UnallocatedIssue:
    """An ACCOUNT component (or guarded non-ACCOUNT row) that was not allocated."""

    scope_id: str
    component_kind: str
    component_key: str
    amount_usd: Decimal
    issue_code: str
    detail: str


@dataclass(frozen=True)
class AllocationNote:
    """Informational, non-blocking observation."""

    note_code: str
    youtube_channel_id: str
    detail: str


@dataclass(frozen=True)
class AllocationSummary:
    """Conserved roll-up over all input components."""

    component_count: int
    allocated_component_count: int
    unallocated_component_count: int
    allocated_total_usd: Decimal
    unallocated_total_usd: Decimal
    net_applicable_total_usd: Decimal
    reconciliation_total_usd: Decimal


@dataclass(frozen=True)
class AccountAllocationResult:
    """Full allocation result for one month."""

    month: str
    allocation_method: str
    lines: tuple[AllocationLine, ...]
    unallocated: tuple[UnallocatedIssue, ...]
    notes: tuple[AllocationNote, ...]
    summary: AllocationSummary


# ============================================================================
# Purpose: Allocate ACCOUNT-grain deduction evidence across each account's
#   verified channels by source-aligned raw-gross-proportional share. Fails
#   closed (UNALLOCATED) on unmapped accounts or missing/incomplete basis;
#   never substitutes a different basis. Pure compute — no DB access.
# Database/ORM: None (caller resolves components, map, and gross basis).
# Standards: Decimal arithmetic; exact per-component + aggregate conservation;
#   net_applicable from net_revenue's NET_APPLICABLE_COMPONENT_KINDS.
# Blast Radius: Finance read-model only. No persistence, no net_revenue change,
#   no auth, no Neo4j.
# Connections:
#   - File: backend/ums_smart_revenue/finance/net_revenue.py -> shared constants.
#   - File: backend/ums_smart_revenue/api/allocation.py -> read endpoint.
# ============================================================================
def build_account_allocation(
    *,
    month: str,
    components: Iterable[DeductionComponent],
    verified_channels: Mapping[str, Sequence[str]],
    gross_basis: Mapping[tuple[str, str], Decimal],
) -> AccountAllocationResult:
    """Compute per-channel allocation + unallocated issues for one month."""
    lines: list[AllocationLine] = []
    unallocated: list[UnallocatedIssue] = []
    notes: list[AllocationNote] = []

    accounts_by_channel: dict[str, set[str]] = {}
    for account, channels in verified_channels.items():
        for channel_id in channels:
            accounts_by_channel.setdefault(channel_id, set()).add(account)
    for channel_id, accounts in sorted(accounts_by_channel.items()):
        if len(accounts) > 1:
            notes.append(
                AllocationNote(
                    note_code="CHANNEL_IN_MULTIPLE_ACCOUNTS",
                    youtube_channel_id=channel_id,
                    detail=f"channel reachable from {len(accounts)} accounts",
                )
            )

    component_count = 0
    allocated_component_count = 0
    for component in components:
        component_count += 1
        if component.scope_kind != "ACCOUNT":
            unallocated.append(
                UnallocatedIssue(
                    scope_id=component.scope_id,
                    component_kind=component.component_kind,
                    component_key=component.component_key,
                    amount_usd=component.amount_usd,
                    issue_code="UNSUPPORTED_SCOPE",
                    detail=f"scope_kind {component.scope_kind} is not allocatable",
                )
            )
            continue

        account = component.scope_id
        amount = component.amount_usd
        net_applicable = component.component_kind in NET_APPLICABLE_COMPONENT_KINDS

        if amount == 0:
            allocated_component_count += 1
            continue

        channels = list(verified_channels.get(account) or [])
        if not channels:
            unallocated.append(
                _issue(component, "ACCOUNT_UNMAPPED_OR_UNVERIFIED",
                       "no verified channels for account-month")
            )
            continue

        source_kind = _basis_source_kind(component.source_system)
        if source_kind is None:
            unallocated.append(
                _issue(component, "BASIS_MISSING",
                       f"unresolved source kind for {component.source_system}")
            )
            continue

        present = [
            (channel_id, gross_basis[(channel_id, source_kind)])
            for channel_id in channels
            if (channel_id, source_kind) in gross_basis
        ]
        if not present:
            unallocated.append(
                _issue(component, "BASIS_MISSING",
                       "no source-aligned gross for any verified channel")
            )
            continue
        if len(present) != len(channels):
            unallocated.append(
                _issue(component, "BASIS_INCOMPLETE",
                       "some verified channels missing source-aligned gross")
            )
            continue

        basis_total = sum((gross for _, gross in present), Decimal("0"))
        if basis_total == 0:
            unallocated.append(
                _issue(component, "ZERO_GROSS_BASIS",
                       "verified channels have zero source-aligned gross")
            )
            continue

        allocated = _proportional_allocation(amount, present)
        for channel_id, gross in present:
            lines.append(
                AllocationLine(
                    adsense_account_id=account,
                    youtube_channel_id=channel_id,
                    component_kind=component.component_kind,
                    source_system=component.source_system,
                    component_key=component.component_key,
                    basis_source_kind=source_kind,
                    basis_gross_usd=gross,
                    basis_share=(gross / basis_total).quantize(_SCALE),
                    allocated_amount_usd=allocated[channel_id],
                    net_applicable=net_applicable,
                )
            )
        allocated_component_count += 1

    allocated_total = sum((ln.allocated_amount_usd for ln in lines), Decimal("0"))
    unallocated_total = sum((iss.amount_usd for iss in unallocated), Decimal("0"))
    net_total = sum(
        (ln.allocated_amount_usd for ln in lines if ln.net_applicable), Decimal("0")
    )
    reconciliation_total = sum(
        (ln.allocated_amount_usd for ln in lines if not ln.net_applicable), Decimal("0")
    )
    summary = AllocationSummary(
        component_count=component_count,
        allocated_component_count=allocated_component_count,
        unallocated_component_count=len(unallocated),
        allocated_total_usd=allocated_total,
        unallocated_total_usd=unallocated_total,
        net_applicable_total_usd=net_total,
        reconciliation_total_usd=reconciliation_total,
    )
    return AccountAllocationResult(
        month=month,
        allocation_method=ALLOCATION_METHOD,
        lines=tuple(lines),
        unallocated=tuple(unallocated),
        notes=tuple(notes),
        summary=summary,
    )


def _issue(component: DeductionComponent, code: str, detail: str) -> UnallocatedIssue:
    """Build an UnallocatedIssue from an ACCOUNT component."""
    return UnallocatedIssue(
        scope_id=component.scope_id,
        component_kind=component.component_kind,
        component_key=component.component_key,
        amount_usd=component.amount_usd,
        issue_code=code,
        detail=detail,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/finance/test_allocation.py -v`
Expected: all passed (foundation + behavior tests).

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/allocation.py tests/finance/test_allocation.py
git commit -m "feat(finance): account-level gross-proportional allocation with fail-closed basis"
```

---

## Task 4: Read endpoint + router mount

**Files:**
- Modify: `backend/ums_smart_revenue/finance/channel_account_links.py` (add public `tenant_id` property)
- Create: `backend/ums_smart_revenue/api/allocation.py`
- Modify: `backend/ums_smart_revenue/app.py` (import + mount router)
- Test: `tests/api/test_allocation_api.py`

- [ ] **Step 1: Add the `tenant_id` property to the link repository**

In `backend/ums_smart_revenue/finance/channel_account_links.py`, inside `SqlAlchemyChannelAccountLinkRepository`, immediately after `self._tenant_id = _resolve_tenant_id(tenant_id)` in `__init__`, add the property (place it as the first method after `__init__`):

```python
    @property
    def tenant_id(self) -> UUID:
        """The tenant UUID this repository is scoped to (read-only)."""
        return self._tenant_id
```

- [ ] **Step 2: Write the failing API tests**

Create `tests/api/test_allocation_api.py`:

```python
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    ContentOwnerChannelLinkORM,
    DeductionComponentORM,
    FinanceBase,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

MONTH = "2026-04"
TENANT = UUID(UMS_TENANT_ID)
USER_ID = UUID("00000000-0000-0000-0000-0000000d0501")


def auth_headers(role, scope_type="global", scope_id=None):
    """Trusted-gateway identity headers for a role/scope."""
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "alloc@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path):
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def seed(database_url, *, add_unmapped=False, add_payment=False):
    """Create schema + one verified-map account with gross and a deduction."""
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(UserORM(id=USER_ID, email="alloc@example.com", display_name="Alloc"))
        session.add(
            YouTubeChannelORM(
                id=uuid4(), tenant_id=TENANT, youtube_channel_id="chA",
                channel_name="Channel A", active=True,
            )
        )
        session.add(
            AdsenseContentOwnerLinkORM(
                id=uuid4(), tenant_id=TENANT, adsense_account_id="pub-1",
                content_owner_id="owner-1", verification_status="VERIFIED",
                provenance_kind="OPERATOR_ASSERTED", provenance_payload={},
                effective_month_start="2026-01",
            )
        )
        session.add(
            ContentOwnerChannelLinkORM(
                id=uuid4(), tenant_id=TENANT, content_owner_id="owner-1",
                youtube_channel_id="chA", provenance_kind="SOURCE_ROW",
                active=True, effective_month_start="2026-01",
            )
        )
        session.add(
            MonthlyChannelRevenueFactORM(
                id=uuid4(), tenant_id=TENANT, month=MONTH,
                youtube_channel_id="chA", source_kind="ADSENSE",
                gross_revenue_usd=Decimal("500.00"),
            )
        )
        session.add(
            DeductionComponentORM(
                id=uuid4(), tenant_id=TENANT, month=MONTH,
                component_kind="DEDUCTION", scope_kind="ACCOUNT", scope_id="pub-1",
                amount_usd=Decimal("100.00"), currency_code="USD",
                source_system="adsense_management",
                source_table="google_revenue_source_rows",
                component_key="acct-ded-1", raw_payload={},
            )
        )
        if add_unmapped:
            session.add(
                DeductionComponentORM(
                    id=uuid4(), tenant_id=TENANT, month=MONTH,
                    component_kind="DEDUCTION", scope_kind="ACCOUNT", scope_id="pub-x",
                    amount_usd=Decimal("9.00"), currency_code="USD",
                    source_system="adsense_management",
                    source_table="google_revenue_source_rows",
                    component_key="acct-ded-x", raw_payload={},
                )
            )
        if add_payment:
            session.add(
                DeductionComponentORM(
                    id=uuid4(), tenant_id=TENANT, month=MONTH,
                    component_kind="TRANSFER_FEE", scope_kind="PAYMENT",
                    scope_id="BANK-1", amount_usd=Decimal("2.50"),
                    currency_code="USD", source_system="bank_reconciliation",
                    source_table="bank_reconciliation_entries",
                    component_key="pay-fee-1", raw_payload={},
                )
            )
        session.commit()


def test_finance_viewer_gets_allocation(tmp_path):
    """finance_viewer sees the single-channel allocation + both view audits."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/account-allocations",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["allocation_method"] == "gross_revenue_proportional"
    assert len(body["allocations"]) == 1
    line = body["allocations"][0]
    assert line["adsense_account_id"] == "pub-1"
    assert line["youtube_channel_id"] == "chA"
    assert line["allocated_amount_usd"] == "100"
    assert line["net_applicable"] is True
    assert body["summary"]["allocated_total_usd"] == "100"
    assert {e["event_type"] for e in body["audit_events"]} == {"REVENUE_VIEWED", "PAYMENT_VIEWED"}
    engine = create_engine(database_url)
    with Session(engine) as session:
        logs = {log.event_type for log in session.scalars(select(AuditLogORM)).all()}
    assert logs == {"REVENUE_VIEWED", "PAYMENT_VIEWED"}


def test_unmapped_account_reports_blocking_issue(tmp_path):
    database_url = build_database_url(tmp_path)
    seed(database_url, add_unmapped=True)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/account-allocations",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 200
    body = response.json()
    codes = {iss["issue_code"] for iss in body["unallocated"]}
    assert "ACCOUNT_UNMAPPED_OR_UNVERIFIED" in codes


def test_account_filter_narrows_results(tmp_path):
    database_url = build_database_url(tmp_path)
    seed(database_url, add_unmapped=True)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/account-allocations",
        params={"adsense_account_id": "pub-1"},
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["unallocated"] == []
    assert len(body["allocations"]) == 1


def test_missing_finance_view_is_forbidden(tmp_path):
    """corporate_admin lacks finance-view permissions -> 403 (fail-closed)."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/account-allocations",
        headers=auth_headers("corporate_admin", "global"),
    )
    assert response.status_code == 403


def test_malformed_month_returns_422(tmp_path):
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        "/revenue/months/2026-13/account-allocations",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 422


def test_finance_month_scope_is_rejected_for_global_read(tmp_path):
    """A finance-month-scoped grant cannot satisfy the VIEW_REVENUE@global target."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/account-allocations",
        headers=auth_headers("finance_viewer", "finance-month", MONTH),
    )
    assert response.status_code == 403


def test_payment_grain_excluded_and_no_bank_audit(tmp_path):
    """A PAYMENT-grain component is never fetched/returned and emits no bank audit."""
    database_url = build_database_url(tmp_path)
    seed(database_url, add_payment=True)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/account-allocations",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 200
    body = response.json()
    surfaced = {ln["adsense_account_id"] for ln in body["allocations"]} | {
        iss["scope_id"] for iss in body["unallocated"]
    }
    assert "BANK-1" not in surfaced  # PAYMENT-grain never fetched or surfaced
    assert all(ln["youtube_channel_id"] == "chA" for ln in body["allocations"])
    engine = create_engine(database_url)
    with Session(engine) as session:
        logs = {log.event_type for log in session.scalars(select(AuditLogORM)).all()}
    assert "BANK_RECONCILIATION_VIEWED" not in logs
    assert logs == {"REVENUE_VIEWED", "PAYMENT_VIEWED"}
```

- [ ] **Step 3: Run the API tests to verify they fail**

Run: `python -m pytest tests/api/test_allocation_api.py -v`
Expected: FAIL — 404 (route not mounted) / import error.

- [ ] **Step 4: Create the route module**

Create `backend/ums_smart_revenue/api/allocation.py`:

```python
"""Read-only account-level deduction allocation endpoint (Phase 4 Spec 2b PR-1)."""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ums_smart_revenue.api.channels import audit_record_to_api, current_audit_sink
from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_principal_from_headers,
)
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.finance.allocation import (
    AccountAllocationResult,
    build_account_allocation,
)
from ums_smart_revenue.finance.channel_account_links import (
    SqlAlchemyChannelAccountLinkRepository,
)
from ums_smart_revenue.finance.decimal_formatting import decimal_to_api
from ums_smart_revenue.finance.deduction_ingestion import (
    DeductionComponentValidationError,
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.revenue_facts import SqlAlchemyRevenueFactRepository

router = APIRouter(prefix="/revenue", tags=["account-allocations"])


def current_deduction_component_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyDeductionComponentRepository:
    """Build the tenant-aware deduction-component repository for a request."""
    return SqlAlchemyDeductionComponentRepository(session)


def current_channel_account_link_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyChannelAccountLinkRepository:
    """Build the tenant-aware channel-account-link repository for a request."""
    return SqlAlchemyChannelAccountLinkRepository(session)


def current_revenue_fact_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyRevenueFactRepository:
    """Build the tenant-aware revenue-fact repository for a request."""
    return SqlAlchemyRevenueFactRepository(session)


def _require_permission(
    user: UserPrincipal, permission: Permission, scope: AccessScope
) -> None:
    """Raise HTTP 403 if the principal lacks the permission for the scope."""
    if not has_permission(user, permission, scope):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {permission.value}",
        )


def _require_valid_month(month: str) -> None:
    """Boundary YYYY-MM validation -> 422 before scope/permission checks."""
    valid = (
        len(month) == 7
        and month[4] == "-"
        and month[:4].isascii()
        and month[:4].isdigit()
        and month[5:].isascii()
        and month[5:].isdigit()
        and 1 <= int(month[5:7]) <= 12
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="month must use YYYY-MM with a calendar month from 01 to 12",
        )


def _result_to_api(result: AccountAllocationResult) -> dict[str, object]:
    """Serialize the allocation result (no secrets, Decimals as strings)."""
    return {
        "month": result.month,
        "allocation_method": result.allocation_method,
        "allocations": [
            {
                "adsense_account_id": ln.adsense_account_id,
                "youtube_channel_id": ln.youtube_channel_id,
                "component_kind": ln.component_kind,
                "source_system": ln.source_system,
                "component_key": ln.component_key,
                "basis_source_kind": ln.basis_source_kind,
                "basis_gross_usd": decimal_to_api(ln.basis_gross_usd),
                "basis_share": decimal_to_api(ln.basis_share),
                "allocated_amount_usd": decimal_to_api(ln.allocated_amount_usd),
                "net_applicable": ln.net_applicable,
            }
            for ln in result.lines
        ],
        "unallocated": [
            {
                "scope_id": iss.scope_id,
                "component_kind": iss.component_kind,
                "component_key": iss.component_key,
                "amount_usd": decimal_to_api(iss.amount_usd),
                "issue_code": iss.issue_code,
                "detail": iss.detail,
            }
            for iss in result.unallocated
        ],
        "notes": [
            {
                "note_code": note.note_code,
                "youtube_channel_id": note.youtube_channel_id,
                "detail": note.detail,
            }
            for note in result.notes
        ],
        "summary": {
            "component_count": result.summary.component_count,
            "allocated_component_count": result.summary.allocated_component_count,
            "unallocated_component_count": result.summary.unallocated_component_count,
            "allocated_total_usd": decimal_to_api(result.summary.allocated_total_usd),
            "unallocated_total_usd": decimal_to_api(result.summary.unallocated_total_usd),
            "net_applicable_total_usd": decimal_to_api(
                result.summary.net_applicable_total_usd
            ),
            "reconciliation_total_usd": decimal_to_api(
                result.summary.reconciliation_total_usd
            ),
        },
    }


# ============================================================================
# Purpose: Read-only month endpoint that allocates ACCOUNT-grain deduction
#   evidence to channels via the verified map (source-aligned raw gross). It
#   reads ACCOUNT-only components (no bank-grain rows fetched), resolves each
#   account's verified channels, builds the source-aligned gross basis, and
#   returns allocations + unallocated blocking issues + a conserved summary.
# Database/ORM: Reads deduction_components, adsense_content_owner_links +
#   content_owner_channel_links (via the map contract), monthly_channel_revenue_facts.
# Standards: thin route; 422 on malformed month before scope checks; fail-closed
#   permission gate; sensitive read audit (REVENUE_VIEWED + PAYMENT_VIEWED); no
#   secrets in responses/audit details.
# Blast Radius: Finance read only. No mutation, no migration, no Neo4j.
# Connections:
#   - File: backend/ums_smart_revenue/finance/allocation.py -> pure builder.
#   - File: Docs/superpowers/specs/2026-05-31-spec-account-allocation-design.md.
# ============================================================================
@router.get("/months/{month}/account-allocations")
def get_account_allocations(
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    deduction_repository: Annotated[
        SqlAlchemyDeductionComponentRepository,
        Depends(current_deduction_component_repository),
    ],
    link_repository: Annotated[
        SqlAlchemyChannelAccountLinkRepository,
        Depends(current_channel_account_link_repository),
    ],
    revenue_repository: Annotated[
        SqlAlchemyRevenueFactRepository, Depends(current_revenue_fact_repository)
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
    adsense_account_id: Annotated[str | None, Query(min_length=1)] = None,
) -> dict[str, object]:
    """Allocate ACCOUNT-grain deduction evidence to channels for one month."""
    _require_valid_month(month)
    revenue_scope = AccessScope.global_scope()
    payment_scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.VIEW_REVENUE, revenue_scope)
    _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, payment_scope)

    try:
        components = deduction_repository.list_account_components(
            month=month, adsense_account_id=adsense_account_id
        )
        facts = revenue_repository.list_month_facts(month=month)
    except DeductionComponentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    gross_basis: dict[tuple[str, str], Decimal] = {}
    for fact in facts:
        key = (fact.youtube_channel_id, fact.source_kind)
        gross_basis[key] = gross_basis.get(key, Decimal("0")) + fact.gross_revenue_usd

    tenant_id = link_repository.tenant_id
    accounts = sorted({component.scope_id for component in components})
    verified_channels = {
        account: link_repository.list_verified_adsense_account_channels(
            tenant_id=tenant_id, month=month, adsense_account_id=account
        )
        for account in accounts
    }

    result = build_account_allocation(
        month=month,
        components=components,
        verified_channels=verified_channels,
        gross_basis=gross_basis,
    )

    details = {
        "month": month,
        "adsense_account_id": adsense_account_id,
        "allocated_line_count": len(result.lines),
        "unallocated_count": len(result.unallocated),
    }
    audit_events = [
        audit_record_to_api(
            record_audit_event(
                sink=audit_sink, actor=user,
                event_type=AuditEventType.REVENUE_VIEWED,
                entity_type="monthly_account_allocations", entity_id=month,
                scope=revenue_scope, details=details,
            )
        ),
        audit_record_to_api(
            record_audit_event(
                sink=audit_sink, actor=user,
                event_type=AuditEventType.PAYMENT_VIEWED,
                entity_type="monthly_account_allocations", entity_id=month,
                scope=payment_scope, details=details,
            )
        ),
    ]

    payload = _result_to_api(result)
    payload["audit_events"] = audit_events
    return payload
```

The `gross_basis` accumulator sums same-`(channel, source_kind)` facts into a `Decimal` total (the basis denominator for that source kind).

- [ ] **Step 5: Mount the router in `app.py`**

In `backend/ums_smart_revenue/app.py`, add the import next to the other API router imports (alphabetical: after `adsense`, before `audit`):

```python
from ums_smart_revenue.api.allocation import router as allocation_router
```

And add the mount call in the `include_router` block (after `adsense_router`, before `audit_router`):

```python
    _app.include_router(allocation_router)
```

- [ ] **Step 6: Run the API tests to verify they pass**

Run: `python -m pytest tests/api/test_allocation_api.py -v`
Expected: 7 passed.

- [ ] **Step 7: Commit**

```bash
git add backend/ums_smart_revenue/finance/channel_account_links.py backend/ums_smart_revenue/api/allocation.py backend/ums_smart_revenue/app.py tests/api/test_allocation_api.py
git commit -m "feat(api): GET account-allocations read endpoint (month-scoped, audited)"
```

---

## Task 5: Documentation status update

**Files:**
- Modify: `Docs/01_IMPLEMENTATION_PLAN.md`
- Modify: `Docs/15_DELIVERY_BACKLOG.md`

- [ ] **Step 1: Update `Docs/15_DELIVERY_BACKLOG.md`**

Replace the line:

```
- ⏳ Allocation engine (Spec 2b) — remaining: not started; consumes the verified map.
```

with:

```
- ⏳ Allocation engine (Spec 2b) — PR-1 shipped (this branch): account-level
  deduction allocation compute + read. `finance/allocation.py` distributes
  ACCOUNT-grain `deduction_components` across each account's verified channels
  (`list_verified_adsense_account_channels`) by source-aligned raw-gross-proportional
  share with exact per-component conservation; `net_applicable` from
  `NET_APPLICABLE_COMPONENT_KINDS`; fail-closed UNALLOCATED on unmapped/missing/
  incomplete basis. Read-only `GET /revenue/months/{month}/account-allocations`
  (ACCOUNT-only query, `VIEW_REVENUE@global` + `VIEW_FINALIZED_PAYMENTS@finance_month`,
  REVENUE_VIEWED + PAYMENT_VIEWED). No persistence, no migration, no net-revenue change.
  Remaining: net-revenue integration of net-applicable lines; PAYMENT-grain (needs a
  payment→account hop); persisted/committed allocation; other allocation methods.
```

- [ ] **Step 2: Update `Docs/01_IMPLEMENTATION_PLAN.md`**

Replace the line (under Phase 4 → Build):

```
- ⏳ Allocation rules (Spec 2b) — remaining: not started. Prerequisite SHIPPED
```

so the status leads with PR-1 shipped, keeping the existing prerequisite sentence:

```
- ⏳ Allocation rules (Spec 2b) — PR-1 SHIPPED (this branch): account-level
  deduction allocation compute + read endpoint
  (`GET /revenue/months/{month}/account-allocations`), source-aligned raw-gross-
  proportional, fail-closed UNALLOCATED, no persistence/net change. Remaining:
  net integration, PAYMENT-grain, persisted/committed writes, other methods.
  Prerequisite SHIPPED
```

(Keep the remainder of the existing bullet — the canonical channel↔account map description — unchanged after "Prerequisite SHIPPED".)

- [ ] **Step 3: Verify doc hygiene**

Run: `git diff --check`
Expected: no whitespace errors. Confirm dates/commands in the edited lines are accurate.

- [ ] **Step 4: Commit**

```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "docs(plan): mark Spec 2b PR-1 account allocation shipped"
```

---

## Final validation (after all tasks)

Run the full baseline gate (must pass before any review-readiness claim):

```bash
python -m ruff check backend tests scripts
python -m pytest -q
git diff --check
```

Expected: ruff clean; full suite green (new tests included); no whitespace errors. Confirm no `Co-Authored-By`/Claude trailer on any commit (`git log --format=%B spec/account-allocation` since `714fde8`). Do NOT push or open a PR until explicitly approved.

---

## Self-review (writing-plans)

**Spec coverage:** §2 scope items → T1 (repo ACCOUNT-only query), T2+T3 (pure service: basis resolver, apportionment, all issue codes, net_applicable, conservation), T4 (route + month-scoped auth + REVENUE/PAYMENT audit + serialization + mount), T5 (docs). §5 basis table → `_basis_source_kind` (T2) + tests. §6 taxonomy → T3 branches + tests (ACCOUNT_UNMAPPED_OR_UNVERIFIED, BASIS_MISSING, BASIS_INCOMPLETE, ZERO_GROSS_BASIS, UNSUPPORTED_SCOPE, CHANNEL_IN_MULTIPLE_ACCOUNTS). §7 conservation invariants → T3 tests (per-component + aggregate). §9 auth/audit → T4 (verified precedent `revenue.py:748-751`). §10 blast radius (no migration/graph) → no schema task exists; reads only. §11 tests → T1/T3/T4 (repository, service, API). §11 baseline gate (`backend tests scripts`) → Final validation.

**Placeholder scan:** every code step contains complete code; commands have expected output; no TBDs.

**Type consistency:** `build_account_allocation` signature, dataclass field names, `AllocationLine`/`UnallocatedIssue`/`AllocationNote`/`AllocationSummary`/`AccountAllocationResult`, `_basis_source_kind`, `_proportional_allocation`, and `list_account_components` are used identically across T2/T3/T4. `decimal_to_api`, `list_month_facts`, `list_verified_adsense_account_channels`, and `tenant_id` match the verified anchors.

**Constructor parity (verified):** `SqlAlchemyRevenueFactRepository.__init__(self, session, *, tenant_id=None)` (`revenue_facts.py:101`) matches the deduction/link repos, so the `current_revenue_fact_repository` provider's `SqlAlchemyRevenueFactRepository(session)` is correct.

---

## Plan review patches (pre-execution, Mahmoud)

1. **Task 4 auth fix.** The endpoint requires `VIEW_REVENUE@global`; a finance-month-scoped grant cannot satisfy a global target (`OrgAccessIndex.contains`, `scopes.py:60-61`), and the real `x-scope-type` header value is `finance-month` (hyphen; `ScopeType.FINANCE_MONTH`, `scopes.py:12`) — not `finance_month`. All happy-path/filter/unmapped/422 tests now authenticate as `finance_viewer` at **`global`** (a global grant satisfies both the global revenue check and the finance-month payment check, `scopes.py:58`); the 403 test uses `corporate_admin` at `global`. Added `test_finance_month_scope_is_rejected_for_global_read` proving a `finance-month`-scoped `finance_viewer` is rejected (403) for this global management read.
2. **Task 3 negative-amount test.** `test_allocates_negative_amount_preserving_sign_and_conservation` covers a negative `UNRESOLVED_PAYMENT_GAP` (settled − paid can be negative): the split preserves sign and conserves exactly, landing in the reconciliation (non-net) bucket.
3. **Task 4 PAYMENT-grain test.** `test_payment_grain_excluded_and_no_bank_audit` seeds a PAYMENT-grain component and asserts it never appears in the response and that only `REVENUE_VIEWED` + `PAYMENT_VIEWED` (no `BANK_RECONCILIATION_VIEWED`) are recorded — proving the SQL-layer ACCOUNT-only boundary.
