# Net-Revenue Explanation Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `net_revenue_usd` metric to the existing channel-month explain endpoint that explains net revenue with channel-direct + account-allocated deduction provenance, reusing PR-2's net builder, with a `finance_month` finalized-payment gate and a no-drift shared provenance helper.

**Architecture:** A behavior-preserving extraction in `finance/net_revenue.py` yields one shared helper that both the net builder (for totals) and a new explanation builder (for provenance) call. `finance/explanations.py` gains a net-metric branch + a tested confidence-mapping helper. `api/revenue.py`'s explain handler gains a net-metric-only `VIEW_FINALIZED_PAYMENTS@finance_month(month)` gate, gathers allocation inputs, and emits plural `audit_events`. No migration (free-form `components` JSONB).

**Tech Stack:** Python 3, FastAPI, SQLAlchemy 2.x, pytest, ruff. PostgreSQL source of truth; SQLite for unit tests; PG container (`UMS_TEST_DATABASE_URL`) for PG-tier API tests.

**Spec:** `Docs/superpowers/specs/2026-06-01-spec-net-revenue-explanation-design.md`

**Branch:** `spec/net-revenue-explanation` (off `main` `3d317e6`, PR-2 merged). Strict TDD, frequent commits, **no Co-Authored-By / Claude trailer in any commit**. Do NOT push or open a PR until the operator approves.

---

## Verified anchors (read these; they are current as of `3d317e6`)

- `backend/ums_smart_revenue/finance/net_revenue.py`
  - `_applicable_deduction_components(components, *, month, youtube_channel_id, primary_source_kind) -> list[DeductionComponent]` (≈176-193): filters `month`, `scope_kind=="CHANNEL"`, `scope_id==youtube_channel_id`, `component_kind in NET_APPLICABLE_COMPONENT_KINDS`, `SOURCE_SYSTEM_TO_SOURCE_KIND.get(source_system)==primary_source_kind`.
  - `_applicable_account_allocations(allocations, *, youtube_channel_id, primary_source_kind) -> list[AllocationLine]` (≈196-213): `youtube_channel_id` match, `net_applicable`, source-aligned.
  - Inside `build_channel_net_revenue_summary` (≈390-420): `channel_direct = _applicable_deduction_components(...)`, `applied_keys = {c.component_key for c in channel_direct}`, `account_allocated = [l for l in _applicable_account_allocations(...) if l.component_key not in applied_keys]`, then `channel_direct_total = sum((c.amount_usd for c in channel_direct), Decimal("0"))`, `account_allocated_total = sum((l.allocated_amount_usd for l in account_allocated), Decimal("0"))`.
  - `_component_derived_channel_summary(...)` (≈216-252): `component_total = channel_direct_total + account_allocated_total`; `net = adjusted_gross - component_total`; `confidence="D_ESTIMATED"`.
  - Confidence labels emitted: `B_RECONCILED`, `D_ESTIMATED`, `E_MISSING` only. `net_revenue_usd is None` ⟺ `confidence=="E_MISSING"` (lines 275/280 missing-source, 693/698 no-facts).
  - Imports already present: `AllocationLine, UnallocatedIssue` (from allocation), `DeductionComponent` (from deduction_components), `decimal_to_api as _decimal_to_api` (from decimal_formatting), `NET_APPLICABLE_COMPONENT_KINDS, SOURCE_SYSTEM_TO_SOURCE_KIND` (re-exported from deduction_policy). No `__all__`.
- `backend/ums_smart_revenue/finance/deduction_components.py:46-86` — `DeductionComponent` frozen dataclass; provenance fields: `component_kind`, `source_system`, `component_key`, `amount_usd: Decimal`.
- `backend/ums_smart_revenue/finance/decimal_formatting.py` — `decimal_to_api(value: Decimal | None) -> str | None`.
- `backend/ums_smart_revenue/finance/explanations.py`
  - `build_channel_month_revenue_explanation(*, facts, manual_overrides, month, youtube_channel_id, metric) -> NumberExplanationEntry` (110-117); validates `metric == ADJUSTED_GROSS_REVENUE_METRIC` at ≈119 else raises `NumberExplanationValidationError`.
  - `ADJUSTED_GROSS_REVENUE_METRIC` constant; `NumberExplanationEntry` dataclass (22-50); `_confidence(...)` (198-215); `decimal_to_api as _decimal_to_api` (12).
- `backend/ums_smart_revenue/api/revenue.py`
  - Explain handler (1328-1392). Auth `VIEW_REVENUE`+`VIEW_CONFIDENCE`@`AccessScope.channel(channel_id)`. try/except: `RevenueFactNotFoundError`→404; `(ManualOverrideValidationError, NumberExplanationValidationError, RevenueFactValidationError)`→422. Audit singular `REVENUE_VIEWED`, `entity_type="number_explanation"`, `entity_id=f"{channel_id}:{month}:{metric}"`.
  - `current_deduction_component_repository` (357-361) → `SqlAlchemyDeductionComponentRepository(session)`.
  - `current_channel_account_link_repository` (364-373, LOCAL, cycle-avoiding) → `SqlAlchemyChannelAccountLinkRepository(session)`.
  - Net route (1098-1102) gate; (1127-1152) `compute_month_account_allocation` + `filter_account_allocations_to_scope`; (1165-1194) plural `audit_events`. `audit_record_to_api` local (1733).
  - Imports present: `Permission`, `AccessScope`/`OrgAccessIndex`/`ScopeType`, `record_audit_event`/`AuditSink`/`AuditRecord`, `AuditEventType`. Confirm `compute_month_account_allocation`, `filter_account_allocations_to_scope`, `SqlAlchemyDeductionComponentRepository`, `SqlAlchemyChannelAccountLinkRepository`, `NET_APPLICABLE_COMPONENT_KINDS` imports exist (added by PR-2) at Task 4 Step 0.
- `backend/ums_smart_revenue/finance/deduction_ingestion.py:290` — `list_month_components(self, *, month, youtube_channel_ids: set[str] | None = None, component_kinds: Collection[str] | None = None)`.
- `backend/ums_smart_revenue/auth/policy.py:36-41` — role grants authorize via `index.contains(assignment.scope, target)`, NOT clamped by `PERMISSION_SCOPE_TYPES`. `roles.py:72` `FINANCE_VIEWER.allowed_scope_types=_ORG_SCOPES`. `scopes.py:57-87` `contains` has no org→finance_month rule. ⇒ org-scoped finance viewer is 403'd on net.
- Tests: `tests/api/test_revenue_explanations_api.py` (TestClient + `auth_headers(role, scope_id=...)` + `seed_database`; tests at 131/188/202). `tests/finance/test_explanations.py` (unit; `build_channel_month_revenue_explanation(..., metric=ADJUSTED_GROSS_REVENUE_METRIC)`). `tests/api/test_net_revenue_api.py` (`UserPrincipal(... direct_permissions=(PermissionGrant(Permission.X, AccessScope.Y),))` + dependency-override pattern; `_company_finance_principal` at 54).

---

## File structure

- **Modify** `backend/ums_smart_revenue/finance/net_revenue.py` — add public `resolve_applicable_channel_deductions(...)`; refactor `build_channel_net_revenue_summary` to call it (behavior-preserving).
- **Modify** `backend/ums_smart_revenue/finance/explanations.py` — `NET_REVENUE_METRIC`, `SUPPORTED_METRICS`, `map_net_confidence`, `_build_net_revenue_explanation`, optional builder params, metric dispatch.
- **Modify** `backend/ums_smart_revenue/api/revenue.py` — explain handler: net-metric auth gate, input gathering, plural audit, metric-conditional response.
- **Test** `tests/finance/test_net_revenue.py` (or `test_net_revenue_account_allocations.py`) — shared-helper + no-drift identity tests.
- **Test** `tests/finance/test_explanations.py` — confidence mapping + net builder unit tests.
- **Test** `tests/api/test_revenue_explanations_api.py` — net-metric API tests; fix unsupported-metric test.
- **Modify** `Docs/01_IMPLEMENTATION_PLAN.md`, `Docs/15_DELIVERY_BACKLOG.md` — mark PR-3 shipped.

---

## Task 1: Shared applicable-deduction helper (no-drift extraction)

**Files:**
- Modify: `backend/ums_smart_revenue/finance/net_revenue.py`
- Test: `tests/finance/test_net_revenue_account_allocations.py`

- [ ] **Step 0: Read the exact current block**

Read `backend/ums_smart_revenue/finance/net_revenue.py:330-420` to confirm the exact inline filter+dedup+sum block inside `build_channel_net_revenue_summary` and the exact `month`/channel variable names it passes to `_applicable_deduction_components` / `_applicable_account_allocations`.

- [ ] **Step 1: Write the failing test for the shared helper + no-drift identity**

Add to `tests/finance/test_net_revenue_account_allocations.py` (reuse the module's existing fixtures/builders for `DeductionComponent`, `AllocationLine`, `RevenueFactEntry`; match their current call shapes):

```python
from decimal import Decimal

from ums_smart_revenue.finance.net_revenue import (
    build_channel_net_revenue_summary,
    resolve_applicable_channel_deductions,
)


def test_resolve_applicable_channel_deductions_filters_dedups_and_matches_totals():
    # A CHANNEL-direct TAX component and an ACCOUNT allocation, both ADSENSE-aligned,
    # for the same channel/month; plus one cross-source line that must be excluded.
    components = [
        _deduction_component(
            component_key="cd-1",
            scope_kind="CHANNEL",
            scope_id="channel-tv-a",
            month="2026-03",
            component_kind="TAX",
            source_system="adsense_management",
            amount_usd="30.00",
        ),
    ]
    allocations = [
        _allocation_line(
            component_key="acct-1",
            youtube_channel_id="channel-tv-a",
            source_system="adsense_management",
            allocated_amount_usd="100.00",
            net_applicable=True,
        ),
        # dedup: shares a key already applied as channel-direct -> excluded
        _allocation_line(
            component_key="cd-1",
            youtube_channel_id="channel-tv-a",
            source_system="adsense_management",
            allocated_amount_usd="999.00",
            net_applicable=True,
        ),
        # wrong channel -> excluded
        _allocation_line(
            component_key="acct-2",
            youtube_channel_id="channel-other",
            source_system="adsense_management",
            allocated_amount_usd="500.00",
            net_applicable=True,
        ),
    ]

    channel_direct, account_allocated = resolve_applicable_channel_deductions(
        deduction_components=components,
        account_allocations=allocations,
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        primary_source_kind="ADSENSE",
    )

    assert [c.component_key for c in channel_direct] == ["cd-1"]
    assert [l.component_key for l in account_allocated] == ["acct-1"]

    # No-drift: the helper's sums equal the builder's COMPONENT_DERIVED breakdown.
    summary = build_channel_net_revenue_summary(
        facts=[_fact_without_net(source_kind="ADSENSE", gross_revenue_usd="1000.00")],
        manual_overrides=[],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        deduction_components=components,
        account_allocations=allocations,
    )
    assert summary.status == "COMPONENT_DERIVED"
    assert summary.channel_direct_deduction_amount_usd == sum(
        (c.amount_usd for c in channel_direct), Decimal("0")
    )
    assert summary.account_allocated_deduction_amount_usd == sum(
        (l.allocated_amount_usd for l in account_allocated), Decimal("0")
    )
```

If the module lacks `_deduction_component` / `_allocation_line` / `_fact_without_net` builders, add minimal ones mirroring the existing test fixtures in this file (a fact with `net_revenue_usd=None` triggers the COMPONENT_DERIVED path).

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/finance/test_net_revenue_account_allocations.py::test_resolve_applicable_channel_deductions_filters_dedups_and_matches_totals -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_applicable_channel_deductions'`.

- [ ] **Step 3: Add the shared helper and refactor the builder to call it**

In `net_revenue.py`, add the public helper (place it directly above `build_channel_net_revenue_summary`):

```python
def resolve_applicable_channel_deductions(
    *,
    deduction_components: Iterable[DeductionComponent],
    account_allocations: Iterable[AllocationLine],
    month: str,
    youtube_channel_id: str,
    primary_source_kind: str,
) -> tuple[list[DeductionComponent], list[AllocationLine]]:
    # ========================================================================
    # Purpose: Single source of truth for a channel's source-aligned,
    #   net-applicable channel-direct deduction components and account-allocation
    #   lines, with account lines deduped against channel-direct component_keys.
    #   Both the net builder (totals) and the explanation builder (provenance)
    #   call this so the explained breakdown cannot drift from the computed total.
    # Database/ORM: None (operates on already-loaded read models).
    # Standards: Pure function; deterministic; no I/O.
    # Blast Radius: Finance net-revenue totals + explanation provenance.
    # Connections:
    #   - File: backend/ums_smart_revenue/finance/explanations.py -> net provenance.
    # ========================================================================
    channel_direct = _applicable_deduction_components(
        deduction_components,
        month=month,
        youtube_channel_id=youtube_channel_id,
        primary_source_kind=primary_source_kind,
    )
    applied_keys = {component.component_key for component in channel_direct}
    account_allocated = [
        line
        for line in _applicable_account_allocations(
            account_allocations,
            youtube_channel_id=youtube_channel_id,
            primary_source_kind=primary_source_kind,
        )
        if line.component_key not in applied_keys
    ]
    return channel_direct, account_allocated
```

Then replace the inline filter+dedup block inside `build_channel_net_revenue_summary` (the lines from Step 0, currently computing `channel_direct` / `applied_keys` / `account_allocated`) with:

```python
        channel_direct, account_allocated = resolve_applicable_channel_deductions(
            deduction_components=deduction_components,
            account_allocations=account_allocations,
            month=month,
            youtube_channel_id=resolved_channel_id,
            primary_source_kind=primary.source_kind,
        )
```

Use the **identical** `month`/`resolved_channel_id`/`primary.source_kind` expressions the current code passes (confirmed in Step 0). Leave the `channel_direct_total` / `account_allocated_total` sums exactly as-is.

- [ ] **Step 4: Run the new test + full net-revenue regression**

Run: `python -m pytest tests/finance/test_net_revenue.py tests/finance/test_net_revenue_account_allocations.py tests/finance/test_net_revenue_deduction_components.py -q`
Expected: PASS (new test green; all existing net tests still green — proves behavior-preserving).

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/net_revenue.py tests/finance/test_net_revenue_account_allocations.py
git commit -m "refactor(finance): shared resolve_applicable_channel_deductions helper (no-drift provenance source)"
```

---

## Task 2: Net-confidence mapping helper

**Files:**
- Modify: `backend/ums_smart_revenue/finance/explanations.py`
- Test: `tests/finance/test_explanations.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/finance/test_explanations.py`:

```python
from ums_smart_revenue.finance.explanations import map_net_confidence


def test_map_net_confidence_pins_each_net_label_and_defaults_low():
    assert map_net_confidence("B_RECONCILED") == {"label": "HIGH", "score": "0.95"}
    assert map_net_confidence("D_ESTIMATED") == {"label": "MEDIUM", "score": "0.80"}
    assert map_net_confidence("E_MISSING") == {"label": "LOW", "score": "0"}
    # Defensive default for any unexpected/future label.
    assert map_net_confidence("SOMETHING_NEW") == {"label": "LOW", "score": "0"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/finance/test_explanations.py::test_map_net_confidence_pins_each_net_label_and_defaults_low -v`
Expected: FAIL — `ImportError: cannot import name 'map_net_confidence'`.

- [ ] **Step 3: Implement the helper**

In `explanations.py` (module-level, near the metric constants):

```python
NET_REVENUE_METRIC = "net_revenue_usd"

_NET_CONFIDENCE_TO_EXPLAIN: dict[str, dict[str, str]] = {
    "B_RECONCILED": {"label": "HIGH", "score": "0.95"},
    "D_ESTIMATED": {"label": "MEDIUM", "score": "0.80"},
    "E_MISSING": {"label": "LOW", "score": "0"},
}


def map_net_confidence(net_confidence_label: str) -> dict[str, str]:
    # ========================================================================
    # Purpose: Map a net-revenue confidence label (B_RECONCILED/D_ESTIMATED/
    #   E_MISSING) to the explain HIGH/MEDIUM/LOW+score shape. Net explanations
    #   reject E_MISSING before persistence; the E_MISSING/default rows fail safe.
    # Database/ORM: None.
    # Standards: Pure; total function (unknown labels -> LOW/0).
    # Blast Radius: Finance confidence labels on persisted net explanations.
    # ========================================================================
    return _NET_CONFIDENCE_TO_EXPLAIN.get(
        net_confidence_label, {"label": "LOW", "score": "0"}
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/finance/test_explanations.py::test_map_net_confidence_pins_each_net_label_and_defaults_low -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/explanations.py tests/finance/test_explanations.py
git commit -m "feat(finance): map_net_confidence helper for net-revenue explanations"
```

---

## Task 3: Net-revenue explanation builder

**Files:**
- Modify: `backend/ums_smart_revenue/finance/explanations.py`
- Test: `tests/finance/test_explanations.py`

- [ ] **Step 0: Read**

Read `explanations.py:100-185` (the current `build_channel_month_revenue_explanation` body, `NumberExplanationEntry`, `ADJUSTED_GROSS_REVENUE_METRIC`, and the existing component-building style) to match the exact `NumberExplanationEntry` constructor field order and the `_decimal_to_api` usage.

- [ ] **Step 1: Write failing unit tests (source-net, component-derived w/ provenance, indeterminate, gross regression)**

Add to `tests/finance/test_explanations.py` (reuse this file's `revenue_fact` / `manual_override` fixtures; add small `deduction_component` / `allocation_line` builders mirroring `tests/finance/test_net_revenue_account_allocations.py`):

```python
import pytest

from ums_smart_revenue.finance.explanations import (
    NET_REVENUE_METRIC,
    NumberExplanationValidationError,
    build_channel_month_revenue_explanation,
)


def test_net_explanation_source_net_path_single_source_deduction_component():
    entry = build_channel_month_revenue_explanation(
        facts=[revenue_fact(source_kind="YOUTUBE_CMS",
                            gross_revenue_usd="1000.00",
                            net_revenue_usd="900.00")],
        manual_overrides=[],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=NET_REVENUE_METRIC,
    )
    assert entry.metric == NET_REVENUE_METRIC
    assert entry.value == Decimal("900.00")
    assert entry.confidence == {"label": "HIGH", "score": "0.95"}  # B_RECONCILED
    keys = [c["key"] for c in entry.components]
    assert "source_reported_deduction_usd" in keys
    assert "account_allocated_deduction_usd" not in keys  # None on source-net path
    src = next(c for c in entry.components if c["key"] == "source_reported_deduction_usd")
    assert src["value"] == "100"  # 1000 - 900
    assert src["source_kind"] == "YOUTUBE_CMS"


def test_net_explanation_component_derived_path_with_full_provenance_and_sum_identity():
    components = [deduction_component(component_key="cd-1", scope_kind="CHANNEL",
                                      scope_id="channel-tv-a", month="2026-03",
                                      component_kind="TAX",
                                      source_system="adsense_management",
                                      amount_usd="30.00")]
    allocations = [allocation_line(component_key="acct-1",
                                   youtube_channel_id="channel-tv-a",
                                   adsense_account_id="pub-1",
                                   component_kind="DEDUCTION",
                                   source_system="adsense_management",
                                   basis_source_kind="ADSENSE",
                                   basis_share="0.5",
                                   allocated_amount_usd="100.00",
                                   net_applicable=True)]
    entry = build_channel_month_revenue_explanation(
        facts=[revenue_fact(source_kind="ADSENSE", gross_revenue_usd="1000.00")],
        manual_overrides=[],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric=NET_REVENUE_METRIC,
        deduction_components=components,
        account_allocations=allocations,
    )
    assert entry.value == Decimal("870.00")  # 1000 - 30 - 100
    assert entry.confidence == {"label": "MEDIUM", "score": "0.80"}  # D_ESTIMATED
    by_key = {c["key"]: c for c in entry.components}
    cd = by_key["channel_direct_deduction_usd"]
    assert cd["value"] == "30"
    assert cd["count"] == 1
    assert cd["components"] == [{
        "component_kind": "TAX", "source_system": "adsense_management",
        "component_key": "cd-1", "amount_usd": "30",
    }]
    aa = by_key["account_allocated_deduction_usd"]
    assert aa["value"] == "100"
    assert aa["count"] == 1
    assert aa["allocations"] == [{
        "adsense_account_id": "pub-1", "component_kind": "DEDUCTION",
        "source_system": "adsense_management", "component_key": "acct-1",
        "basis_source_kind": "ADSENSE", "basis_share": "0.5",
        "allocated_amount_usd": "100",
    }]
    # No-drift: provenance amounts sum to the component values.
    assert Decimal(cd["value"]) == sum(Decimal(x["amount_usd"]) for x in cd["components"])
    assert Decimal(aa["value"]) == sum(
        Decimal(x["allocated_amount_usd"]) for x in aa["allocations"]
    )


def test_net_explanation_indeterminate_net_raises():
    with pytest.raises(NumberExplanationValidationError):
        build_channel_month_revenue_explanation(
            facts=[],  # NO_FACTS -> net None -> E_MISSING
            manual_overrides=[],
            month="2026-03",
            youtube_channel_id="channel-tv-a",
            metric=NET_REVENUE_METRIC,
        )


def test_gross_metric_unchanged_when_net_params_omitted():
    entry = build_channel_month_revenue_explanation(
        facts=[revenue_fact(source_kind="YOUTUBE_CMS", gross_revenue_usd="1000.00")],
        manual_overrides=[],
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        metric="adjusted_gross_revenue_usd",
    )
    assert entry.metric == "adjusted_gross_revenue_usd"
    assert [c["key"] for c in entry.components][0] == "baseline_gross_revenue_usd"
```

Match `revenue_fact` / `deduction_component` / `allocation_line` builder kwargs to the real fixtures; `revenue_fact` must support `net_revenue_usd=None` default (source-net vs missing).

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/finance/test_explanations.py -k "net_explanation or gross_metric_unchanged" -v`
Expected: FAIL — net metric currently rejected at the metric validation.

- [ ] **Step 3: Implement the net builder branch**

In `explanations.py`:

1. Add imports near the top:

```python
from collections.abc import Iterable

from ums_smart_revenue.finance.allocation import AllocationLine
from ums_smart_revenue.finance.deduction_components import DeductionComponent
from ums_smart_revenue.finance.net_revenue import (
    build_channel_net_revenue_summary,
    resolve_applicable_channel_deductions,
)
```

2. Extend the public builder signature with optional, default-empty params and dispatch on metric. Replace the current single-metric validation with:

```python
SUPPORTED_METRICS = frozenset({ADJUSTED_GROSS_REVENUE_METRIC, NET_REVENUE_METRIC})


def build_channel_month_revenue_explanation(
    *,
    facts: list[RevenueFactEntry],
    manual_overrides: list[RevenueManualOverrideEntry],
    month: str,
    youtube_channel_id: str,
    metric: str,
    deduction_components: Iterable[DeductionComponent] = (),
    account_allocations: Iterable[AllocationLine] = (),
) -> NumberExplanationEntry:
    if metric == NET_REVENUE_METRIC:
        return _build_net_revenue_explanation(
            facts=facts,
            manual_overrides=manual_overrides,
            month=month,
            youtube_channel_id=youtube_channel_id,
            deduction_components=deduction_components,
            account_allocations=account_allocations,
        )
    if metric != ADJUSTED_GROSS_REVENUE_METRIC:
        raise NumberExplanationValidationError(
            f"Unsupported metric: {metric}. Supported: {sorted(SUPPORTED_METRICS)}."
        )
    # ... existing adjusted-gross body unchanged ...
```

3. Add the net builder. The provenance arrays come ONLY from `resolve_applicable_channel_deductions` (the same helper the summary used), guaranteeing no drift:

```python
def _build_net_revenue_explanation(
    *,
    facts: list[RevenueFactEntry],
    manual_overrides: list[RevenueManualOverrideEntry],
    month: str,
    youtube_channel_id: str,
    deduction_components: Iterable[DeductionComponent],
    account_allocations: Iterable[AllocationLine],
) -> NumberExplanationEntry:
    # ========================================================================
    # Purpose: Explain net_revenue_usd for one channel-month, reusing the PR-2
    #   net builder for the value/status/confidence and the shared applicable-
    #   deduction helper for channel-direct + account-allocated provenance.
    #   Indeterminate net (E_MISSING) is rejected (422) -- no fabricated value.
    # Database/ORM: None (operates on loaded read models; persisted by caller).
    # Standards: Typed; raises NumberExplanationValidationError on indeterminate net.
    # Blast Radius: Finance number explanations; net provenance + confidence.
    # ========================================================================
    deduction_components = list(deduction_components)
    account_allocations = list(account_allocations)
    summary = build_channel_net_revenue_summary(
        facts=facts,
        manual_overrides=manual_overrides,
        month=month,
        youtube_channel_id=youtube_channel_id,
        deduction_components=deduction_components,
        account_allocations=account_allocations,
    )
    if summary.net_revenue_usd is None:
        raise NumberExplanationValidationError(
            f"net_revenue_usd is indeterminate for {youtube_channel_id} in {month} "
            f"(status {summary.status}); no net explanation is emitted."
        )

    components: list[dict[str, object]] = [
        {
            "key": "baseline_gross_revenue_usd",
            "label": "Baseline gross revenue",
            "value": _decimal_to_api(summary.baseline_gross_revenue_usd),
            "source_kind": summary.primary_source_kind,
        },
        {
            "key": "approved_manual_override_total_usd",
            "label": "Approved manual overrides",
            "value": _decimal_to_api(summary.approved_manual_override_total_usd),
            "count": summary.approved_manual_override_count,
        },
    ]

    if summary.status == "COMPONENT_DERIVED":
        channel_direct, account_allocated = resolve_applicable_channel_deductions(
            deduction_components=deduction_components,
            account_allocations=account_allocations,
            month=month,
            youtube_channel_id=youtube_channel_id,
            primary_source_kind=summary.primary_source_kind,
        )
        channel_direct = sorted(
            channel_direct, key=lambda c: (c.source_system, c.component_key)
        )
        account_allocated = sorted(
            account_allocated, key=lambda l: (l.adsense_account_id, l.component_key)
        )
        components.append({
            "key": "channel_direct_deduction_usd",
            "label": "Channel-direct deductions",
            "value": _decimal_to_api(summary.channel_direct_deduction_amount_usd),
            "count": len(channel_direct),
            "components": [
                {
                    "component_kind": c.component_kind,
                    "source_system": c.source_system,
                    "component_key": c.component_key,
                    "amount_usd": _decimal_to_api(c.amount_usd),
                }
                for c in channel_direct
            ],
        })
        components.append({
            "key": "account_allocated_deduction_usd",
            "label": "Account-allocated deductions",
            "value": _decimal_to_api(summary.account_allocated_deduction_amount_usd),
            "count": len(account_allocated),
            "allocations": [
                {
                    "adsense_account_id": l.adsense_account_id,
                    "component_kind": l.component_kind,
                    "source_system": l.source_system,
                    "component_key": l.component_key,
                    "basis_source_kind": l.basis_source_kind,
                    "basis_share": _decimal_to_api(l.basis_share),
                    "allocated_amount_usd": _decimal_to_api(l.allocated_amount_usd),
                }
                for l in account_allocated
            ],
        })
        formula = (
            "net_revenue_usd = adjusted_gross_revenue_usd "
            "- channel_direct_deduction_amount_usd "
            "- account_allocated_deduction_amount_usd"
        )
    else:
        components.append({
            "key": "source_reported_deduction_usd",
            "label": "Source-reported deductions",
            "value": _decimal_to_api(summary.deduction_amount_usd),
            "source_kind": summary.primary_source_kind,
        })
        formula = (
            "net_revenue_usd = source-reported net "
            "(deduction_amount_usd = adjusted_gross_revenue_usd - net_revenue_usd)"
        )

    warnings: list[dict[str, object]] = [
        {"code": issue.get("issue_type", "ISSUE"), "message": issue.get("detail", "")}
        for issue in summary.issues
    ]
    if summary.pending_manual_override_count:
        warnings.append({
            "code": "PENDING_MANUAL_OVERRIDES",
            "message": (
                f"{summary.pending_manual_override_count} pending manual override(s) "
                f"not included in {NET_REVENUE_METRIC}."
            ),
        })

    return NumberExplanationEntry(
        month=month,
        entity_type="channel",
        entity_id=youtube_channel_id,
        metric=NET_REVENUE_METRIC,
        value=summary.net_revenue_usd,
        currency="USD",
        formula=formula,
        confidence=map_net_confidence(summary.confidence),
        components=components,
        warnings=warnings,
    )
```

Adjust the `issues` warning mapping to the real `issues` dict keys confirmed in Step 0 (the summary `issues` entries use `issue_type` / `severity` / a message key — match them exactly; do not invent keys).

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/finance/test_explanations.py -q`
Expected: PASS (new net tests + all existing gross tests green).

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/explanations.py tests/finance/test_explanations.py
git commit -m "feat(finance): net_revenue_usd explanation builder with channel-direct + account-allocated provenance"
```

---

## Task 4: Route wiring (auth gate, inputs, plural audit, metric-conditional response)

**Files:**
- Modify: `backend/ums_smart_revenue/api/revenue.py`
- Test: `tests/api/test_revenue_explanations_api.py`

- [ ] **Step 0: Read**

Read `revenue.py:1-60` (imports) to confirm `compute_month_account_allocation`, `filter_account_allocations_to_scope`, `SqlAlchemyDeductionComponentRepository`, `SqlAlchemyChannelAccountLinkRepository`, `NET_APPLICABLE_COMPONENT_KINDS`, `AuditEventType` are imported (PR-2 added them); add any missing import. Read `tests/api/test_revenue_explanations_api.py:1-60` for `auth_headers` / `seed_database` / `COMPANY_ID` and which seeded channel/month yields a determinate net (source net present) vs none. Read `tests/api/test_net_revenue_api.py:40-120` for the dependency-override principal pattern.

- [ ] **Step 1: Write failing API tests (net happy path, plural audit, auth boundary, 422, gross unchanged, unsupported-metric fix)**

Add to `tests/api/test_revenue_explanations_api.py` (use the existing harness — `TestClient(create_app(database_url))`, `auth_headers`, `seed_database`; for a global finalized-payment principal use the dependency-override pattern from `test_net_revenue_api.py` if no seeded global finance role exists). Choose `CHANNEL_DET` / `MONTH` to a channel-month with a determinate net (from Step 0); if the default seed has none, seed one in this test's setup.

```python
def test_net_revenue_explanation_global_finance_returns_value_provenance_and_plural_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    # principal holding VIEW_FINALIZED_PAYMENTS at finance_month (+VIEW_REVENUE/CONFIDENCE@channel)
    response = _post_net_explain(client, channel="channel-tv-a", month="2026-03",
                                 principal=_finance_month_net_principal())

    assert response.status_code == 200
    body = response.json()
    assert body["metric"] == "net_revenue_usd"
    assert isinstance(body["value"], str)
    assert "audit_event" not in body
    assert [e["event_type"] for e in body["audit_events"]] == [
        "REVENUE_VIEWED", "PAYMENT_VIEWED",
    ]
    # persisted once under the net metric key
    engine = create_engine(database_url)
    with Session(engine) as session:
        rows = session.scalars(
            select(NumberExplanationORM).where(
                NumberExplanationORM.metric == "net_revenue_usd"
            )
        ).all()
    assert len(rows) == 1


def test_net_revenue_explanation_org_scoped_finance_viewer_403_on_net_200_on_gross(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    headers = auth_headers("finance_viewer", scope_id=str(COMPANY_ID))

    net = client.post(
        "/revenue/channels/channel-tv-a/months/2026-03/explain?metric=net_revenue_usd",
        headers=headers,
    )
    gross = client.post(
        "/revenue/channels/channel-tv-a/months/2026-03/explain"
        "?metric=adjusted_gross_revenue_usd",
        headers=headers,
    )
    assert net.status_code == 403
    assert gross.status_code == 200


def test_net_revenue_explanation_no_facts_channel_returns_422(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = _post_net_explain(client, channel="channel-with-no-facts",
                                 month="2026-03", principal=_finance_month_net_principal())
    assert response.status_code == 422


def test_net_revenue_explanation_idempotent_upsert_coexists_with_gross(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    _post_net_explain(client, "channel-tv-a", "2026-03", _finance_month_net_principal())
    _post_net_explain(client, "channel-tv-a", "2026-03", _finance_month_net_principal())
    client.post(  # gross row under finance_viewer
        "/revenue/channels/channel-tv-a/months/2026-03/explain"
        "?metric=adjusted_gross_revenue_usd",
        headers=auth_headers("finance_viewer", scope_id=str(COMPANY_ID)),
    )
    engine = create_engine(database_url)
    with Session(engine) as session:
        metrics = sorted(
            session.scalars(select(NumberExplanationORM.metric)).all()
        )
    assert metrics == ["adjusted_gross_revenue_usd", "net_revenue_usd"]
```

Implement `_finance_month_net_principal()` (a `UserPrincipal` with `VIEW_REVENUE`/`VIEW_CONFIDENCE@channel("channel-tv-a")` + `VIEW_FINALIZED_PAYMENTS@finance_month("2026-03")`) and `_post_net_explain(...)` (installs the principal via `app.dependency_overrides[current_principal_from_headers]` then POSTs `?metric=net_revenue_usd`), mirroring `test_net_revenue_api.py`. Then **fix** `test_revenue_explanation_rejects_unsupported_metric` (line ≈202) to use a genuinely-unsupported metric such as `"bogus_metric"` (NOT `net_revenue_usd`, which is now supported) and assert 422.

- [ ] **Step 2: Run to verify they fail**

Run (with PG container env set): `python -m pytest tests/api/test_revenue_explanations_api.py -q`
Expected: FAIL — net metric not wired (likely 422/500 and missing `audit_events`).

- [ ] **Step 3: Wire the handler**

In `explain_channel_month_revenue_metric`:

1. Add two dependencies after `explanation_repository`:

```python
    deduction_component_repository: Annotated[
        SqlAlchemyDeductionComponentRepository,
        Depends(current_deduction_component_repository),
    ],
    link_repository: Annotated[
        SqlAlchemyChannelAccountLinkRepository,
        Depends(current_channel_account_link_repository),
    ],
```

2. After the existing two `_require_permission` calls, add the net-metric gate and input gathering:

```python
    is_net_metric = metric == NET_REVENUE_METRIC
    if is_net_metric:
        _require_permission(
            user, Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month(month)
        )
```

3. Inside the `try`, before `build_channel_month_revenue_explanation`, gather net inputs only for the net metric:

```python
        deduction_components: list[DeductionComponent] = []
        account_allocations: list[AllocationLine] = []
        if is_net_metric:
            deduction_components = deduction_component_repository.list_month_components(
                month=month,
                youtube_channel_ids={channel_id},
                component_kinds=NET_APPLICABLE_COMPONENT_KINDS,
            )
            account_allocations = list(
                compute_month_account_allocation(
                    month=month,
                    deduction_repository=deduction_component_repository,
                    revenue_repository=revenue_repository,
                    link_repository=link_repository,
                ).lines
            )
```

Pass them through (the gross path leaves them empty so the call is unchanged in effect):

```python
        explanation = build_channel_month_revenue_explanation(
            facts=facts,
            manual_overrides=overrides,
            month=month,
            youtube_channel_id=channel_id,
            metric=metric,
            deduction_components=deduction_components,
            account_allocations=account_allocations,
        )
```

4. Replace the single audit + response build with a metric-conditional block:

```python
    if is_net_metric:
        revenue_record = record_audit_event(
            sink=audit_sink,
            actor=user,
            event_type=AuditEventType.REVENUE_VIEWED,
            entity_type="number_explanation",
            entity_id=f"{channel_id}:{month}:{metric}",
            scope=target_scope,
            details={"metric": metric, "warning_count": len(explanation.warnings)},
        )
        payment_record = record_audit_event(
            sink=audit_sink,
            actor=user,
            event_type=AuditEventType.PAYMENT_VIEWED,
            entity_type="number_explanation",
            entity_id=f"{channel_id}:{month}:{metric}",
            scope=AccessScope.finance_month(month),
            details={"metric": metric},
        )
        response = explanation.to_api()
        response["audit_events"] = [
            audit_record_to_api(revenue_record),
            audit_record_to_api(payment_record),
        ]
        return response

    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="number_explanation",
        entity_id=f"{channel_id}:{month}:{metric}",
        scope=target_scope,
        details={"metric": metric, "warning_count": len(explanation.warnings)},
    )
    response = explanation.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response
```

Add module imports if missing: `NET_REVENUE_METRIC` from `finance.explanations`, `DeductionComponent`, `AllocationLine`.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/api/test_revenue_explanations_api.py -q`
Expected: PASS (all net tests + the fixed unsupported-metric test + unchanged gross tests).

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/api/revenue.py tests/api/test_revenue_explanations_api.py
git commit -m "feat(api): explain net_revenue_usd metric with finance_month gate, account-allocation provenance, plural audit"
```

---

## Task 5: Docs status updates

**Files:**
- Modify: `Docs/01_IMPLEMENTATION_PLAN.md`
- Modify: `Docs/15_DELIVERY_BACKLOG.md`

- [ ] **Step 1: Update the plan + backlog**

In `Docs/01_IMPLEMENTATION_PLAN.md` (the Spec 2b lines ≈445-500) and `Docs/15_DELIVERY_BACKLOG.md` (the allocation-engine lines ≈317-333): mark Spec 2b PR-3 (net-revenue explanation extension) shipped; under "Remaining" drop "explain-path provenance" and keep PAYMENT-grain, persisted/committed, other methods, export breakdown columns. Set status date `2026-06-01`. Edit sequentially (single pass per file); do not use `git checkout`/`restore`/`reset` on files.

- [ ] **Step 2: Verify doc hygiene**

Run: `git diff --check`
Expected: no whitespace errors.

- [ ] **Step 3: Commit**

```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "docs(plan): mark Phase 4 Spec 2b PR-3 (net-revenue explanation) shipped"
```

---

## Final validation gate (run before any push/PR — operator-gated)

- [ ] `python -m ruff check backend tests`
- [ ] `python -m pytest -q` with `UMS_TEST_DATABASE_URL` (Postgres container) set — 0 failed, 0 errors.
- [ ] `git diff --check`
- [ ] `git log --format='%(trailers)' origin/main..HEAD` empty (no Co-Authored-By / Claude trailers on any commit).
- [ ] Re-read the full diff; confirm no migration, no schema change, no weakened auth, gross-metric path byte-identical.

## Self-review notes (spec coverage)

- §5.2 auth gate → Task 4 Step 3.2 + boundary tests Task 4 Step 1.
- §5.3 plural audit → Task 4 Step 3.4.
- §5.4 shared helper (no drift) → Task 1 + Task 3 Step 3.
- §5.5 provenance shape → Task 3 Step 1/3.
- §5.6 confidence (3 labels) → Task 2.
- §5.7 indeterminate → 422 → Task 3 (raise) + Task 4 (422 test).
- §5.8 persistence/no migration → reuses `record_explanation`; no migration task.
- §8 tests → Tasks 1-4 test steps (incl. both auth-boundary sides).
- §9 files → Tasks 1-5.
