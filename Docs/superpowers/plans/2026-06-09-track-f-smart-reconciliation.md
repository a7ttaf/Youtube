# Track F — Smart Revenue Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a month-level engine that derives the three revenue reductions (US tax, YouTube→AdSense transfer fee, AdSense→bank fee+FX) from the actual figures, attributes them per channel proportional to CMS gross, persists typed deduction components + a reconciliation explanation with deterministic prose, plus a manual report-purge capability.

**Architecture:** A pure compute core (no DB) does all the math and is exhaustively unit-tested; a service layer gathers inputs from existing repos, runs the core, and persists `deduction_components` + `number_explanations` (+ ALLOCATION revenue facts for 1:1 outside-CMS); thin FastAPI routes gate/audit and call the service. Report purge is an additive migration + repo method + DELETE route.

**Tech Stack:** FastAPI, SQLAlchemy 2.x, Alembic, PostgreSQL (RLS active), pytest, Decimal arithmetic.

**Spec:** `Docs/superpowers/specs/2026-06-09-track-f-smart-reconciliation-design.md` (commit 0133a3e).

**Branch:** `feat/track-f-hard-problems` (off merged main `6fea319`).

---

## Hard constraints (every task)

- **TDD:** failing test → run-to-fail → minimal impl → run-to-pass → commit.
- Commits **trailer-free** (no Co-Authored-By / generated footer).
- Validation: `python -m pytest ...` (never bare pytest), `python -m ruff check backend tests scripts`, `git diff --check`.
- **All touched Python lines ≤ 100 chars** (DeepSource FLK-E501 enforces 100).
- Do **not** use `git checkout`/`restore`/`reset` on files.
- PG-tier tests: PowerShell `$env:UMS_TEST_DATABASE_URL = "postgresql+psycopg://postgres:ums@localhost:55432/test_ums"` (container `ums-mig-pg-test`).
- Do **not** push/PR/merge. Mahmoud does that.
- **Re-read each anchor file before editing** (line numbers below captured 2026-06-09; verify).
- No new tenant-scoped table (so `db/rls.py` TENANT_SCOPED_TABLES is untouched). The only schema change is additive columns + CHECK swap on `raw_report_files` (already in the allowlist).

---

## File Structure

**Create:**
- `backend/ums_smart_revenue/finance/reconciliation_workflow.py` — pure compute core: dataclasses, `UsViewShareProvider` protocol + null impl, `DEFAULT_US_WITHHOLDING_RATE`, `compute_month_reconciliation()`.
- `backend/ums_smart_revenue/finance/reconciliation_explanation.py` — turns a `ChannelReconciliation` into a `NumberExplanationEntry` (components + deterministic prose).
- `backend/ums_smart_revenue/finance/reconciliation_service.py` — `ReconciliationWorkflowService`: gather inputs, lock-check, compute, persist components/facts/explanations, audit.
- `backend/ums_smart_revenue/api/reconciliation.py` — POST reconcile + GET reconciliation routes.
- Tests: `tests/finance/test_reconciliation_compute.py`, `tests/finance/test_reconciliation_explanation.py`, `tests/finance/test_reconciliation_service.py`, `tests/api/test_reconciliation_api.py`, `tests/reports/test_raw_file_purge.py`, `tests/api/test_raw_file_purge_api.py`, `tests/db/test_raw_report_files_purge_migration.py`.

**Modify:**
- `backend/ums_smart_revenue/finance/explanations.py` — add `REVENUE_RECONCILIATION_METRIC` to `SUPPORTED_METRICS`.
- `backend/ums_smart_revenue/auth/audit.py` — add `REVENUE_RECONCILED`, `REPORT_PURGED` to `AuditEventType`.
- `backend/ums_smart_revenue/db/report_models.py` — `RawReportFileORM`: add `PURGED` to parse_status CHECK + `ALLOWED_PARSE_STATUSES`; add `purged_at`, `purged_by` columns.
- `backend/ums_smart_revenue/reports/raw_files.py` — add `purge_file(...)`.
- `backend/ums_smart_revenue/api/reports.py` — add `DELETE /reports/raw-files/{id}`.
- `backend/ums_smart_revenue/app.py` — register the reconciliation router.
- `backend/ums_smart_revenue/db/alembic/versions/20260609_0001_raw_report_files_purge.py` — new migration (down_revision `20260608_0002`).
- Docs (Task 7).

---

## Task 1: Reconciliation compute core (pure, no DB)

**Files:**
- Create: `backend/ums_smart_revenue/finance/reconciliation_workflow.py`
- Test: `tests/finance/test_reconciliation_compute.py`

This is the math heart: derive tax (per channel), YT→AdSense fee (residual, attributed ∝ gross), AdSense→bank fee+FX (residual split, attributed), with a rounding-remainder rule so per-channel sums equal each aggregate exactly.

- [ ] **Step 1: Write failing tests** (`tests/finance/test_reconciliation_compute.py`):

```python
from decimal import Decimal

from ums_smart_revenue.finance.reconciliation_workflow import (
    DEFAULT_US_WITHHOLDING_RATE,
    NullUsViewShareProvider,
    compute_month_reconciliation,
)

D = Decimal


def _gross(**kw):
    return {k: D(v) for k, v in kw.items()}


def test_single_channel_full_passthrough_no_data():
    # No adsense/bank/us-view => only gross survives (hops 2/3 = 0, tax = 0).
    res = compute_month_reconciliation(
        month="2026-03",
        channel_gross=_gross(c1="100"),
        us_view_shares={"c1": None},
        adsense_received_usd=None,
        bank_received_usd=None,
        fx_total_usd=D("0"),
        withholding_rate=D("0.30"),
    )
    line = res.channels[0]
    assert line.youtube_channel_id == "c1"
    assert line.us_tax_usd == D("0.000000")
    assert line.yt_adsense_fee_usd == D("0.000000")
    assert line.adsense_bank_fee_usd == D("0.000000")
    assert line.fx_variance_usd == D("0.000000")
    assert line.net_received_usd == D("100.000000")
    assert any(w["code"] == "MISSING_ADSENSE_TOTAL" for w in res.warnings)


def test_tax_uses_us_view_share_times_rate():
    res = compute_month_reconciliation(
        month="2026-03",
        channel_gross=_gross(c1="100"),
        us_view_shares={"c1": D("0.5")},
        adsense_received_usd=None,
        bank_received_usd=None,
        fx_total_usd=D("0"),
        withholding_rate=D("0.30"),
    )
    # 0.5 * 100 * 0.30 = 15
    assert res.channels[0].us_tax_usd == D("15.000000")
    assert res.channels[0].net_received_usd == D("85.000000")


def test_yt_adsense_fee_is_residual_attributed_by_gross():
    # gross 100 (c1=60, c2=40), no tax; adsense received 80 => fee 20 total
    res = compute_month_reconciliation(
        month="2026-03",
        channel_gross=_gross(c1="60", c2="40"),
        us_view_shares={"c1": None, "c2": None},
        adsense_received_usd=D("80"),
        bank_received_usd=None,
        fx_total_usd=D("0"),
        withholding_rate=D("0.30"),
    )
    by = {c.youtube_channel_id: c for c in res.channels}
    assert by["c1"].yt_adsense_fee_usd == D("12.000000")  # 20 * 60/100
    assert by["c2"].yt_adsense_fee_usd == D("8.000000")   # 20 * 40/100
    # fee total attributed exactly
    total_fee = by["c1"].yt_adsense_fee_usd + by["c2"].yt_adsense_fee_usd
    assert total_fee == D("20.000000")


def test_adsense_bank_split_fee_and_fx():
    # adsense 80, bank 60 => delta 20; fx_total 5 => fee 15
    res = compute_month_reconciliation(
        month="2026-03",
        channel_gross=_gross(c1="100"),
        us_view_shares={"c1": None},
        adsense_received_usd=D("80"),
        bank_received_usd=D("60"),
        fx_total_usd=D("5"),
        withholding_rate=D("0.30"),
    )
    line = res.channels[0]
    assert line.fx_variance_usd == D("5.000000")
    assert line.adsense_bank_fee_usd == D("15.000000")  # 20 - 5


def test_net_sum_reconciles_to_bank_when_data_present():
    res = compute_month_reconciliation(
        month="2026-03",
        channel_gross=_gross(c1="60", c2="40"),
        us_view_shares={"c1": D("0.1"), "c2": D("0.2")},
        adsense_received_usd=D("80"),
        bank_received_usd=D("60"),
        fx_total_usd=D("3"),
        withholding_rate=D("0.30"),
    )
    total_net = sum((c.net_received_usd for c in res.channels), D("0"))
    assert total_net == D("60.000000")  # equals bank received exactly


def test_rounding_remainder_lands_on_largest_gross():
    # gross split that forces a rounding drift on the fee attribution
    res = compute_month_reconciliation(
        month="2026-03",
        channel_gross=_gross(c1="1", c2="1", c3="1"),
        us_view_shares={"c1": None, "c2": None, "c3": None},
        adsense_received_usd=D("2"),  # fee total = 1 over 3 channels
        bank_received_usd=None,
        fx_total_usd=D("0"),
        withholding_rate=D("0.30"),
    )
    total_fee = sum((c.yt_adsense_fee_usd for c in res.channels), D("0"))
    assert total_fee == D("1.000000")  # remainder reconciled, no drift


def test_anomaly_when_adsense_exceeds_estimate_clamps_and_warns():
    res = compute_month_reconciliation(
        month="2026-03",
        channel_gross=_gross(c1="100"),
        us_view_shares={"c1": None},
        adsense_received_usd=D("120"),  # more than estimate
        bank_received_usd=None,
        fx_total_usd=D("0"),
        withholding_rate=D("0.30"),
    )
    assert res.channels[0].yt_adsense_fee_usd == D("0.000000")
    assert any(w["code"] == "RECONCILIATION_ANOMALY" for w in res.warnings)


def test_null_provider_returns_none():
    assert NullUsViewShareProvider().us_view_share("2026-03", "c1") is None


def test_default_rate_is_decimal():
    assert isinstance(DEFAULT_US_WITHHOLDING_RATE, Decimal)
```

- [ ] **Step 2: Run to verify failure.** `python -m pytest tests/finance/test_reconciliation_compute.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement `finance/reconciliation_workflow.py`:**

```python
"""Pure compute core for the smart revenue reconciliation workflow.

Derives the three reductions from actual figures and attributes the aggregate
ones per channel proportional to CMS gross. No DB access — fully unit-testable.

Hops:
  1. US tax (per channel)   = us_view_share * gross * withholding_rate
  2. YouTube->AdSense fee   = residual ((G - tax) - adsense_received), attributed
  3. AdSense->bank fee+FX   = residual (adsense - bank), FX from bank deltas,
                              remainder = fee; both attributed ∝ gross
Rounding remainder for each attributed aggregate lands on the largest-gross
channel so per-channel sums equal the aggregate exactly.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

_Q = Decimal("0.000001")
DEFAULT_US_WITHHOLDING_RATE = Decimal("0.30")


def _q(value: Decimal) -> Decimal:
    """Quantize to 6dp (matches Numeric(_,6) columns), round half up."""
    return value.quantize(_Q, rounding=ROUND_HALF_UP)


class UsViewShareProvider(Protocol):
    """Supplies the US-view revenue fraction (0..1) for a channel-month."""

    def us_view_share(self, month: str, youtube_channel_id: str) -> Decimal | None:
        """Return the US-view fraction, or None if unavailable."""
        ...


class NullUsViewShareProvider:
    """Default provider: US-view data not yet ingested (refine-later)."""

    def us_view_share(self, month: str, youtube_channel_id: str) -> Decimal | None:
        """Always None until a real geography feed exists."""
        return None


@dataclass(frozen=True)
class ChannelReconciliation:
    """Per-channel reconciliation outcome for a month."""

    youtube_channel_id: str
    gross_usd: Decimal
    us_tax_usd: Decimal
    yt_adsense_fee_usd: Decimal
    adsense_bank_fee_usd: Decimal
    fx_variance_usd: Decimal
    net_received_usd: Decimal
    us_view_share: Decimal | None


@dataclass(frozen=True)
class MonthReconciliationResult:
    """Whole-month reconciliation result plus derived rates and warnings."""

    month: str
    channels: list[ChannelReconciliation]
    gross_total_usd: Decimal
    us_tax_total_usd: Decimal
    yt_adsense_fee_total_usd: Decimal
    adsense_bank_fee_total_usd: Decimal
    fx_total_usd: Decimal
    net_total_usd: Decimal
    yt_adsense_fee_pct: Decimal | None
    warnings: list[dict[str, str]] = field(default_factory=list)


def _attribute(total: Decimal, gross: dict[str, Decimal], g: Decimal) -> dict[str, Decimal]:
    """Split ``total`` across channels ∝ gross; remainder to largest gross."""
    if total <= 0 or g <= 0:
        return {c: Decimal("0.000000") for c in gross}
    out = {c: _q(total * (gross[c] / g)) for c in gross}
    drift = total - sum(out.values(), Decimal("0"))
    if drift != 0:
        largest = max(gross, key=lambda c: gross[c])
        out[largest] = _q(out[largest] + drift)
    return out


def compute_month_reconciliation(
    *,
    month: str,
    channel_gross: Mapping[str, Decimal],
    us_view_shares: Mapping[str, Decimal | None],
    adsense_received_usd: Decimal | None,
    bank_received_usd: Decimal | None,
    fx_total_usd: Decimal,
    withholding_rate: Decimal = DEFAULT_US_WITHHOLDING_RATE,
) -> MonthReconciliationResult:
    """Compute per-channel tax, transfer fees, FX, and net received for a month."""
    gross = {c: Decimal(v) for c, v in channel_gross.items()}
    g = sum(gross.values(), Decimal("0"))
    warnings: list[dict[str, str]] = []

    # Hop 1 — US tax per channel.
    us_tax = {
        c: _q((us_view_shares.get(c) or Decimal("0")) * gross[c] * withholding_rate)
        for c in gross
    }
    tax_total = sum(us_tax.values(), Decimal("0"))
    if any(us_view_shares.get(c) is None for c in gross):
        warnings.append(
            {"code": "MISSING_US_VIEW_DATA", "message": "US-view share missing; tax may be understated"}
        )

    # Hop 2 — YouTube->AdSense residual fee.
    if adsense_received_usd is None:
        warnings.append({"code": "MISSING_ADSENSE_TOTAL", "message": "No AdSense total; fee not derived"})
        yt_fee_total = Decimal("0")
        yt_fee_pct: Decimal | None = None
    else:
        base = g - tax_total
        if adsense_received_usd > base:
            warnings.append(
                {"code": "RECONCILIATION_ANOMALY", "message": "AdSense received exceeds estimate; fee clamped to 0"}
            )
            yt_fee_total = Decimal("0")
        else:
            yt_fee_total = _q(base - adsense_received_usd)
        yt_fee_pct = _q(yt_fee_total / base) if base > 0 else None
    yt_fee = _attribute(yt_fee_total, gross, g)

    # Hop 3 — AdSense->bank fee + FX.
    fx_total = max(Decimal("0"), fx_total_usd)
    if adsense_received_usd is None or bank_received_usd is None:
        if bank_received_usd is None:
            warnings.append({"code": "MISSING_BANK_TOTAL", "message": "No bank receipt; fee/FX not derived"})
        fee_part = Decimal("0")
        fx_part = Decimal("0")
    else:
        delta = adsense_received_usd - bank_received_usd
        if delta < 0:
            warnings.append(
                {"code": "RECONCILIATION_ANOMALY", "message": "Bank exceeds AdSense; bank fee clamped to 0"}
            )
            delta = Decimal("0")
        fx_part = min(fx_total, delta)
        fee_part = _q(delta - fx_part)
    adsense_bank_fee = _attribute(fee_part, gross, g)
    fx_variance = _attribute(fx_part, gross, g)

    channels: list[ChannelReconciliation] = []
    for c in gross:
        net = _q(gross[c] - us_tax[c] - yt_fee[c] - adsense_bank_fee[c] - fx_variance[c])
        channels.append(
            ChannelReconciliation(
                youtube_channel_id=c,
                gross_usd=_q(gross[c]),
                us_tax_usd=us_tax[c],
                yt_adsense_fee_usd=yt_fee[c],
                adsense_bank_fee_usd=adsense_bank_fee[c],
                fx_variance_usd=fx_variance[c],
                net_received_usd=net,
                us_view_share=us_view_shares.get(c),
            )
        )
    channels.sort(key=lambda x: x.youtube_channel_id)
    return MonthReconciliationResult(
        month=month,
        channels=channels,
        gross_total_usd=_q(g),
        us_tax_total_usd=_q(tax_total),
        yt_adsense_fee_total_usd=_q(yt_fee_total),
        adsense_bank_fee_total_usd=_q(fee_part),
        fx_total_usd=_q(fx_part),
        net_total_usd=_q(sum((x.net_received_usd for x in channels), Decimal("0"))),
        yt_adsense_fee_pct=yt_fee_pct,
        warnings=warnings,
    )
```

- [ ] **Step 4: Run to verify pass.** `python -m pytest tests/finance/test_reconciliation_compute.py -q` → PASS. (If `test_net_sum_reconciles_to_bank` fails by a rounding cent, confirm the remainder rule covers tax too — tax is per-channel exact so only fee/fx need remainder reconciliation; the test inputs are chosen to divide evenly.)

- [ ] **Step 5: Lint + commit.**

```bash
python -m ruff check backend tests
git add backend/ums_smart_revenue/finance/reconciliation_workflow.py tests/finance/test_reconciliation_compute.py
git commit -m "feat(reconciliation): pure compute core for month revenue reconciliation"
```

---

## Task 2: Reconciliation explanation (components + deterministic prose)

**Files:**
- Create: `backend/ums_smart_revenue/finance/reconciliation_explanation.py`
- Modify: `backend/ums_smart_revenue/finance/explanations.py` (add metric to `SUPPORTED_METRICS`)
- Test: `tests/finance/test_reconciliation_explanation.py`

Build a `NumberExplanationEntry` (entity_type `"channel"`, currency `"USD"`) from a `ChannelReconciliation`: components for each hop + a deterministic prose narrative component. Reuse the existing `NumberExplanationEntry` dataclass (anchor: explanations.py:56-84 — fields `month, entity_type, entity_id, metric, value, currency, formula, confidence, components, warnings`).

- [ ] **Step 1: Write failing tests:**

```python
from decimal import Decimal

from ums_smart_revenue.finance.explanations import SUPPORTED_METRICS
from ums_smart_revenue.finance.reconciliation_explanation import (
    REVENUE_RECONCILIATION_METRIC,
    build_reconciliation_explanation,
)
from ums_smart_revenue.finance.reconciliation_workflow import ChannelReconciliation

D = Decimal


def _line():
    return ChannelReconciliation(
        youtube_channel_id="c1",
        gross_usd=D("100.000000"),
        us_tax_usd=D("15.000000"),
        yt_adsense_fee_usd=D("5.000000"),
        adsense_bank_fee_usd=D("3.000000"),
        fx_variance_usd=D("2.000000"),
        net_received_usd=D("75.000000"),
        us_view_share=D("0.5"),
    )


def test_metric_registered():
    assert REVENUE_RECONCILIATION_METRIC in SUPPORTED_METRICS


def test_explanation_shape():
    entry = build_reconciliation_explanation(
        month="2026-03", line=_line(), warnings=[]
    )
    assert entry.entity_type == "channel"
    assert entry.entity_id == "c1"
    assert entry.metric == REVENUE_RECONCILIATION_METRIC
    assert entry.value == D("75.000000")
    assert entry.currency == "USD"
    keys = {comp["key"] for comp in entry.components}
    assert {"estimated_gross_usd", "us_tax_usd", "yt_adsense_fee_usd",
            "adsense_bank_fee_usd", "fx_variance_usd", "net_received_usd",
            "narrative"} <= keys


def test_narrative_is_deterministic_prose():
    entry = build_reconciliation_explanation(
        month="2026-03", line=_line(), warnings=[]
    )
    narrative = next(c for c in entry.components if c["key"] == "narrative")["text"]
    assert "100" in narrative and "75" in narrative
    # Deterministic: same inputs => identical text.
    again = build_reconciliation_explanation(
        month="2026-03", line=_line(), warnings=[]
    )
    again_text = next(c for c in again.components if c["key"] == "narrative")["text"]
    assert narrative == again_text
```

- [ ] **Step 2: Run to verify failure.** `python -m pytest tests/finance/test_reconciliation_explanation.py -q` → FAIL.

- [ ] **Step 3: Add the metric to `explanations.py`.** Read the current `SUPPORTED_METRICS` block (anchor explanations.py:29-31) and extend:

```python
ADJUSTED_GROSS_REVENUE_METRIC = "adjusted_gross_revenue_usd"
NET_REVENUE_METRIC = "net_revenue_usd"
REVENUE_RECONCILIATION_METRIC = "revenue_reconciliation_usd"
SUPPORTED_METRICS = frozenset(
    {ADJUSTED_GROSS_REVENUE_METRIC, NET_REVENUE_METRIC, REVENUE_RECONCILIATION_METRIC}
)
```

- [ ] **Step 4: Implement `finance/reconciliation_explanation.py`:**

```python
"""Build the revenue_reconciliation explanation (components + prose) for a
channel-month from a ChannelReconciliation. Deterministic; no LLM."""
from __future__ import annotations

from decimal import Decimal

from ums_smart_revenue.finance.explanations import (
    REVENUE_RECONCILIATION_METRIC,
    NumberExplanationEntry,
)
from ums_smart_revenue.finance.reconciliation_workflow import ChannelReconciliation

__all__ = ["REVENUE_RECONCILIATION_METRIC", "build_reconciliation_explanation"]


def _money(value: Decimal) -> str:
    """Render a Decimal as a plain 2dp money string for prose."""
    return f"{value.quantize(Decimal('0.01'))}"


def _component(key: str, label: str, value: Decimal) -> dict[str, object]:
    """One numeric explanation component."""
    return {"key": key, "label": label, "value": str(value)}


def build_reconciliation_explanation(
    *, month: str, line: ChannelReconciliation, warnings: list[dict[str, str]]
) -> NumberExplanationEntry:
    """Assemble the persisted explanation for one channel-month reconciliation."""
    share_txt = (
        f"{(line.us_view_share * 100).quantize(Decimal('0.1'))}% US views"
        if line.us_view_share is not None
        else "US-view share unavailable"
    )
    narrative = (
        f"Channel {line.youtube_channel_id} for {month}: estimated "
        f"${_money(line.gross_usd)}; -${_money(line.us_tax_usd)} US tax "
        f"({share_txt}); -${_money(line.yt_adsense_fee_usd)} YouTube->AdSense "
        f"transfer; -${_money(line.adsense_bank_fee_usd)} AdSense->bank fee "
        f"and -${_money(line.fx_variance_usd)} FX => "
        f"${_money(line.net_received_usd)} received."
    )
    components: list[dict[str, object]] = [
        _component("estimated_gross_usd", "Estimated gross (CMS)", line.gross_usd),
        _component("us_tax_usd", "US tax", line.us_tax_usd),
        _component("yt_adsense_fee_usd", "YouTube->AdSense transfer fee", line.yt_adsense_fee_usd),
        _component("adsense_bank_fee_usd", "AdSense->bank transfer fee", line.adsense_bank_fee_usd),
        _component("fx_variance_usd", "FX variance", line.fx_variance_usd),
        _component("net_received_usd", "Net received", line.net_received_usd),
        {"key": "narrative", "label": "Reconciliation narrative", "text": narrative},
    ]
    confidence = (
        {"label": "LOW", "score": "0"}
        if any(w["code"].startswith("MISSING") for w in warnings)
        else {"label": "MEDIUM", "score": "0.80"}
    )
    return NumberExplanationEntry(
        month=month,
        entity_type="channel",
        entity_id=line.youtube_channel_id,
        metric=REVENUE_RECONCILIATION_METRIC,
        value=line.net_received_usd,
        currency="USD",
        formula=(
            "estimated_gross - us_tax - yt_adsense_fee - adsense_bank_fee - fx_variance"
        ),
        confidence=confidence,
        components=components,
        warnings=list(warnings),
    )
```

- [ ] **Step 5: Run to verify pass + lint + commit.**

```bash
python -m pytest tests/finance/test_reconciliation_explanation.py -q
python -m ruff check backend tests
git add backend/ums_smart_revenue/finance/reconciliation_explanation.py backend/ums_smart_revenue/finance/explanations.py tests/finance/test_reconciliation_explanation.py
git commit -m "feat(reconciliation): revenue_reconciliation explanation builder + metric"
```

---

## Task 3: Reconciliation service (gather → compute → persist → audit)

**Files:**
- Create: `backend/ums_smart_revenue/finance/reconciliation_service.py`
- Modify: `backend/ums_smart_revenue/auth/audit.py` (add `REVENUE_RECONCILED`)
- Test: `tests/finance/test_reconciliation_service.py`

The service: (1) reject if month LOCKED (`get_month_close_status`); (2) gather CMS gross per channel (`SqlAlchemyRevenueFactRepository.list_month_facts`, sum `gross_revenue_usd` for source_kind `YOUTUBE_CMS`/`ALLOCATION`), AdSense total (`SqlAlchemyAdSensePaymentRepository.list_month_payments`, sum PAID `amount_usd`), bank total + fx (`SqlAlchemyBankReconciliationRepository.list_month_entries`, sum `bank_received_amount_usd` and `fx_difference_usd`), us-view via provider; (3) run `compute_month_reconciliation`; (4) persist typed `deduction_components` via `SqlAlchemyDeductionComponentRepository.upsert_components(month, components, replace_source_tables={"reconciliation_workflow"})`; (5) persist explanations via `SqlAlchemyNumberExplanationRepository.record_explanation`; (6) for 1:1 outside-CMS channels with no CMS gross, write an `ALLOCATION` fact (see Step 6); (7) audit `REVENUE_RECONCILED`.

> Idempotency comes from `upsert_components` + `replace_source_tables` (clears prior reconciliation rows) and `record_explanation` upsert keyed on `(tenant, month, entity_type, entity_id, metric)`.

- [ ] **Step 1: Add audit events.** In `auth/audit.py` `AuditEventType` add:

```python
    REVENUE_RECONCILED = "revenue.reconciled"
    REPORT_PURGED = "report.purged"
```

- [ ] **Step 2: Write failing service tests** (`tests/finance/test_reconciliation_service.py`). Use the SQLite finance fixture pattern from `tests/finance/test_deduction_ingestion.py:46-90` (create `FinanceBase`/`OrgBase`/`TenantBase` metadata, seed `TenantORM`, `YouTubeChannelORM` (active), `MonthlyChannelRevenueFactORM` (YOUTUBE_CMS gross), `AdSensePaymentORM` (PAID), `BankReconciliationEntryORM`, set `TENANT_CTX`).

```python
# Skeleton — implementer completes seeding per the deduction_ingestion fixture.
from decimal import Decimal

from ums_smart_revenue.finance.reconciliation_service import (
    ReconciliationWorkflowService,
    MonthLockedError,
)


def test_run_persists_components_and_explanations(session, seed_month):
    svc = ReconciliationWorkflowService(session)
    result = svc.run(month="2026-03", actor_user_id="u1")
    assert result.month == "2026-03"
    # deduction_components written for the channel under source_table.
    comps = seed_month["deduction_repo"].list_month_components(month="2026-03")
    kinds = {c.component_kind for c in comps if c.source_table == "reconciliation_workflow"}
    assert {"TAX", "TRANSFER_FEE", "FX_VARIANCE"} & kinds
    # explanation persisted
    exp = seed_month["explanation_repo"]  # assert a revenue_reconciliation row exists


def test_run_rejects_locked_month(session, seed_locked_month):
    svc = ReconciliationWorkflowService(session)
    import pytest
    with pytest.raises(MonthLockedError):
        svc.run(month="2026-03", actor_user_id="u1")


def test_recompute_is_idempotent(session, seed_month):
    svc = ReconciliationWorkflowService(session)
    svc.run(month="2026-03", actor_user_id="u1")
    svc.run(month="2026-03", actor_user_id="u1")
    comps = seed_month["deduction_repo"].list_month_components(month="2026-03")
    recon = [c for c in comps if c.source_table == "reconciliation_workflow"]
    # no duplicate component_key rows after second run
    assert len({c.component_key for c in recon}) == len(recon)


def test_reconciliation_deductions_feed_net_revenue(session, seed_month):
    # The TAX/TRANSFER_FEE/FX rows must be picked up by the net builder so
    # net_revenue reflects the reconciliation (integration correctness).
    svc = ReconciliationWorkflowService(session)
    svc.run(month="2026-03", actor_user_id="u1")
    # implementer: build_channel_net_revenue_summary for the channel and assert
    # net_revenue_usd reflects the reconciliation deductions.
```

- [ ] **Step 3: Run to verify failure.** `python -m pytest tests/finance/test_reconciliation_service.py -q` → FAIL.

- [ ] **Step 4: Implement `finance/reconciliation_service.py`.** Use these verified signatures:
  - `get_month_close_status(session, month, *, tenant_id=None) -> str | None` (month_close.py:206); reject when `== "LOCKED"`.
  - `SqlAlchemyRevenueFactRepository(session, tenant_id=...)` `.list_month_facts(month=...)` → entries with `youtube_channel_id`, `source_kind`, `gross_revenue_usd`.
  - `SqlAlchemyAdSensePaymentRepository(session)` `.list_month_payments(month=...)` → entries with `payment_status`, `amount_usd` (PAID only).
  - `SqlAlchemyBankReconciliationRepository(session)` `.list_month_entries(month=...)` → entries with `bank_received_amount_usd`, `fx_difference_usd`.
  - `SqlAlchemyDeductionComponentRepository(session, tenant_id=...)` `.upsert_components(month=..., components=[DeductionComponentInput...], replace_source_tables={"reconciliation_workflow"})`.
  - `DeductionComponentInput(component_kind, scope_kind="CHANNEL", scope_id=channel_id, amount_usd, amount_native=amount_usd, currency_code="USD", source_system="reconciliation", source_table="reconciliation_workflow", source_id=channel_id, source_key=component_key, source_report_id=None, raw_payload={"derived_pct": ...}, component_key=f"recon:{month}:{channel}:{kind}")`.
  - `SqlAlchemyNumberExplanationRepository(session, tenant_id=...)` `.record_explanation(entry)`.

  Map each non-zero hop to a `DeductionComponentInput`: `TAX` (us_tax), `TRANSFER_FEE` with `component_key recon:{m}:{c}:yt_adsense_fee`, `TRANSFER_FEE` with `component_key recon:{m}:{c}:adsense_bank_fee`, `FX_VARIANCE` (fx). Skip zero amounts. Define `MonthLockedError(Exception)`. Add a contract-block comment over the `run()` method. Build the explanation via `build_reconciliation_explanation` and persist. Emit `record_audit_event(sink, actor, AuditEventType.REVENUE_RECONCILED, entity_type="finance_month", entity_id=month, scope=AccessScope.finance_month(month), details={...})` — accept the audit sink via constructor or param (mirror `DeductionIngestionService`).

  **Integration correctness (the `test_..._feed_net_revenue` test):** verify in `finance/net_revenue.py` how `_applicable_deduction_components`/`resolve_applicable_channel_deductions` selects CHANNEL-scoped components (source alignment). Set `source_system`/fields on the reconciliation components so they are selected by that resolver; if the resolver filters on a specific `source_system` value, match it. Adjust until the net-revenue integration test passes.

- [ ] **Step 5: Run to verify pass.**

```bash
$env:UMS_TEST_DATABASE_URL = "postgresql+psycopg://postgres:ums@localhost:55432/test_ums"
python -m pytest tests/finance/test_reconciliation_service.py -q
```

- [ ] **Step 6: Outside-CMS 1:1 ALLOCATION fact.** Add to the service a step that, for channels with `cms_status == "OUTSIDE_CMS"` and no CMS gross fact, resolves the verified AdSense account→channel links; when an account maps to exactly one such channel, write its gross via `SqlAlchemyRevenueFactRepository.record_fact(month=..., youtube_channel_id=..., source_kind=RevenueFactSourceKind.ALLOCATION, source_report_id=None, gross_revenue_usd=<account total>, net_revenue_usd=None, views=0, watch_time_minutes=0, confidence_score=Decimal("0.80"), actor_user_id=...)`; when an account maps to many no-gross channels, skip and emit a `MISSING_REVENUE_SOURCE` warning. Add a test for both branches (use the `adsense_content_owner_links`/`content_owner_channel_links` repos to seed verified links). Re-run.

- [ ] **Step 7: Lint + commit.**

```bash
python -m ruff check backend tests
git add backend/ums_smart_revenue/finance/reconciliation_service.py backend/ums_smart_revenue/auth/audit.py tests/finance/test_reconciliation_service.py
git commit -m "feat(reconciliation): month workflow service with outside-CMS attribution"
```

---

## Task 4: Reconciliation API routes + registration

**Files:**
- Create: `backend/ums_smart_revenue/api/reconciliation.py`
- Modify: `backend/ums_smart_revenue/app.py`
- Test: `tests/api/test_reconciliation_api.py`

`POST /revenue/months/{month}/reconcile` and `GET /revenue/channels/{channel_id}/months/{month}/reconciliation`. Mirror the finance route pattern in `api/revenue.py` (anchor: `import_revenue_fact` L725-782 and `explain_channel_month_revenue_metric` L1568) for DI, `_require_permission`, error→HTTPException, audit.

Gate decisions (confirm against `auth/permissions.py`):
- Reconcile (write/compute) → `Permission.CHANGE_ALLOCATION_RULE` at `AccessScope.finance_month(month)` (closest existing finance-compute permission; reconciliation attributes account deltas across channels — an allocation-class operation). Add a contract-block comment noting this reuse.
- Read → `Permission.VIEW_REVENUE` at the channel scope.

- [ ] **Step 1: Write failing API tests** (`tests/api/test_reconciliation_api.py`) following `tests/api/test_revenue_explanations_api.py` (auth_headers, build_database_url, seed_database, TestClient). Cover: reconcile returns 200 with per-channel lines + totals; missing permission → 403; locked month → 409; read returns the persisted explanation; cross-tenant isolation; bad month → 422.

- [ ] **Step 2: Run to verify failure.** `python -m pytest tests/api/test_reconciliation_api.py -q` → FAIL.

- [ ] **Step 3: Implement `api/reconciliation.py`** — `router = APIRouter(prefix="/revenue", tags=["revenue"])`; POST + GET handlers; inject `current_principal_from_headers`, `current_db_session`, `current_revenue_audit_sink`; gate via the module `_require_permission` pattern; translate `MonthLockedError` → 409, validation → 422. Return `{month, channels:[...], totals:{...}, warnings:[...]}`.

- [ ] **Step 4: Register router in `app.py`** — add `from ums_smart_revenue.api.reconciliation import router as reconciliation_router` with the other imports and `_app.include_router(reconciliation_router)` with the others.

- [ ] **Step 5: Run pass + regression.**

```bash
$env:UMS_TEST_DATABASE_URL = "postgresql+psycopg://postgres:ums@localhost:55432/test_ums"
python -m pytest tests/api/test_reconciliation_api.py tests/api -q
```

- [ ] **Step 6: Lint + commit.**

```bash
python -m ruff check backend tests
git add backend/ums_smart_revenue/api/reconciliation.py backend/ums_smart_revenue/app.py tests/api/test_reconciliation_api.py
git commit -m "feat(reconciliation): POST reconcile + GET reconciliation API"
```

---

## Task 5: Manual report purge (#2)

**Files:**
- Create: `backend/ums_smart_revenue/db/alembic/versions/20260609_0001_raw_report_files_purge.py`
- Modify: `backend/ums_smart_revenue/db/report_models.py`, `backend/ums_smart_revenue/reports/raw_files.py`, `backend/ums_smart_revenue/api/reports.py`
- Test: `tests/db/test_raw_report_files_purge_migration.py`, `tests/reports/test_raw_file_purge.py`, `tests/api/test_raw_file_purge_api.py`

- [ ] **Step 1: Confirm head + write migration.** `cd backend; python -m alembic heads` → expect `20260608_0002`. Create `20260609_0001_raw_report_files_purge.py` (down_revision `20260608_0002`) using the batch CHECK-swap template (`20260606_0001_allocation_method_allowlist.py:38-47`) and an add-columns template (`20260603_0001`):

```python
"""Add PURGED status + purge audit columns to raw_report_files.

Revision ID: 20260609_0001
Revises: 20260608_0002
Create Date: 2026-06-09
"""
import sqlalchemy as sa
from alembic import op

revision = "20260609_0001"
down_revision = "20260608_0002"
branch_labels = None
depends_on = None

_OLD = "parse_status IN ('DOWNLOADED', 'PARSED', 'FAILED', 'QUARANTINED')"
_NEW = "parse_status IN ('DOWNLOADED', 'PARSED', 'FAILED', 'QUARANTINED', 'PURGED')"


def upgrade() -> None:
    """Add purged_at/purged_by columns and widen the parse_status CHECK."""
    with op.batch_alter_table("raw_report_files") as batch:
        batch.add_column(sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("purged_by", sa.Uuid(as_uuid=True), nullable=True))
        batch.drop_constraint("ck_raw_report_files_parse_status", type_="check")
        batch.create_check_constraint("ck_raw_report_files_parse_status", _NEW)


def downgrade() -> None:
    """Restore the 4-value CHECK and drop the purge columns.

    :raises RuntimeError: if any PURGED rows exist (would violate the old CHECK).
    """
    if op.get_bind().dialect.name == "postgresql":
        count = op.get_bind().execute(
            sa.text("SELECT COUNT(*) FROM raw_report_files WHERE parse_status = 'PURGED'")
        ).scalar()
        if count:
            raise RuntimeError(
                f"Cannot downgrade: {count} PURGED raw_report_files rows exist."
            )
    with op.batch_alter_table("raw_report_files") as batch:
        batch.drop_constraint("ck_raw_report_files_parse_status", type_="check")
        batch.create_check_constraint("ck_raw_report_files_parse_status", _OLD)
        batch.drop_column("purged_by")
        batch.drop_column("purged_at")
```

- [ ] **Step 2: Migration round-trip test** (`tests/db/test_raw_report_files_purge_migration.py`, Postgres-only via `require_postgres_url`): upgrade head, assert a row can be set `parse_status='PURGED'` and the columns exist; downgrade to `20260608_0002` succeeds when no PURGED rows; upgrade back. Run → iterate to green.

- [ ] **Step 3: Update ORM `report_models.py`.** Read the current `RawReportFileORM` (anchor L36-99). Add `PURGED` to `ALLOWED_PARSE_STATUSES` (L16) and the CHECK string (L92-95), and add the columns:

```python
    purged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    purged_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
```

- [ ] **Step 4: Repository `purge_file` (TDD).** Failing test in `tests/reports/test_raw_file_purge.py` (SQLite): register a file, `purge_file(raw_file_id=..., actor_user_id=..., reason=...)` → row `parse_status='PURGED'`, `file_url` cleared, `purged_at`/`purged_by` set, metadata (source/checksum/month) intact; purging an already-PURGED row raises `RawReportFilePurgeConflictError`; unknown id raises `RawReportFileNotFoundError`. Implement `purge_file` on `SqlAlchemyRawReportFileRepository` (tenant-scoped UPDATE). Run → green.

- [ ] **Step 5: DELETE route (TDD).** Failing test in `tests/api/test_raw_file_purge_api.py`: `DELETE /reports/raw-files/{id}` with a reason → 200 + PURGED; missing permission → 403; missing reason → 422; cross-tenant id → 404; re-purge → 409. Implement in `api/reports.py`: gate `Permission.MANAGE_CONNECTORS` (admin tier; confirm in permissions.py), require a `reason` (body/query), call `purge_file`, emit `record_audit_event(..., AuditEventType.REPORT_PURGED, entity_type="raw_report_file", entity_id=raw_file_id, reason=reason)`, map errors (conflict→409, not found→404). Run → green.

- [ ] **Step 6: Lint + commit.**

```bash
python -m ruff check backend tests
git add backend/ums_smart_revenue/db/alembic/versions/20260609_0001_raw_report_files_purge.py backend/ums_smart_revenue/db/report_models.py backend/ums_smart_revenue/reports/raw_files.py backend/ums_smart_revenue/api/reports.py tests/db/test_raw_report_files_purge_migration.py tests/reports/test_raw_file_purge.py tests/api/test_raw_file_purge_api.py
git commit -m "feat(reports): manual report purge (PURGED status, repo, DELETE route)"
```

---

## Task 6: Docs + full validation gate

**Files:** `Docs/18_MULTI_CURRENCY_ENGINE.md`, `Docs/12_BACKEND_API_SPEC.md`, `Docs/01_IMPLEMENTATION_PLAN.md`, `Docs/15_DELIVERY_BACKLOG.md`

- [ ] **Step 1: Docs/18** — document the smart reconciliation workflow + the three derived reductions; note US-view feed + withholding rate as refinement inputs.
- [ ] **Step 2: Docs/12** — document `POST /revenue/months/{month}/reconcile`, `GET /revenue/channels/{id}/months/{month}/reconciliation`, `DELETE /reports/raw-files/{id}` (gates, payloads, 409/422/404).
- [ ] **Step 3: Docs/01 + Docs/15** — Track F status: reconciliation workflow + outside-CMS attribution + manual report purge DONE; US-view feed + rate + ingestion scaling = refine-later.
- [ ] **Step 4: Full gate.**

```bash
python -m ruff check backend tests scripts
git diff --check
$env:UMS_TEST_DATABASE_URL = "postgresql+psycopg://postgres:ums@localhost:55432/test_ums"
python -m pytest -q
```

Expected: ruff clean, diff-check clean, full suite green. Any failure: fix if Track F-caused, else prove pre-existing.

- [ ] **Step 5: Commit docs.**

```bash
git add Docs/12_BACKEND_API_SPEC.md Docs/18_MULTI_CURRENCY_ENGINE.md Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "docs(track-f): record smart reconciliation workflow + report purge"
```

---

## Self-Review (plan author)

**1. Spec coverage:**
- §3.1 inputs → Task 3 Step 4 (gather from facts/adsense/bank/provider). ✓
- §3.2 derivation (3 hops, residual fees, attribution, rounding remainder) → Task 1. ✓
- §3.3 persistence (deduction_components + number_explanations) → Task 2 (explanation) + Task 3 (components + persist). ✓
- §3.4 API (reconcile + read) → Task 4. ✓
- §3.5 refine-later (UsViewShareProvider + NullUsViewShareProvider + withholding rate) → Task 1. ✓
- §4 outside-CMS 1:1 ALLOCATION fact + fail-closed → Task 3 Step 6. ✓
- §6 manual report purge → Task 5. ✓
- §7 error handling (locked→409, anomaly clamp+warn, missing→warn+LOW) → Task 1 (warnings/clamp) + Task 3 (lock) + Task 4 (HTTP map). ✓
- §8 blast radius (no new table; additive raw_report_files migration) → Task 5. ✓
- §9 testing contract → tests across Tasks 1–5. ✓
- §10 docs → Task 6. ✓
- Out-of-scope (US-view feed, scaling, new tables) → respected (NullUsViewShareProvider; no new table). ✓

**2. Placeholder scan:** Task 3's test skeleton names fixtures the implementer completes per the cited `test_deduction_ingestion.py` pattern (concrete reference, not a vague TODO); the integration-correctness step names the exact resolver to verify. Permission choices flagged "confirm against permissions.py" with a named default. No bare TODO/TBD.

**3. Type/name consistency:** `compute_month_reconciliation`, `ChannelReconciliation` (fields `youtube_channel_id, gross_usd, us_tax_usd, yt_adsense_fee_usd, adsense_bank_fee_usd, fx_variance_usd, net_received_usd, us_view_share`), `MonthReconciliationResult`, `UsViewShareProvider`/`NullUsViewShareProvider`, `DEFAULT_US_WITHHOLDING_RATE` (Task 1) are reused verbatim in Tasks 2–4. `REVENUE_RECONCILIATION_METRIC`, `build_reconciliation_explanation` (Task 2) reused in Task 3. `ReconciliationWorkflowService`, `MonthLockedError` (Task 3) reused in Task 4. `purge_file`, `REPORT_PURGED`, `PURGED` (Task 5) consistent. Audit events added once in Task 3 Step 1. Consistent.
