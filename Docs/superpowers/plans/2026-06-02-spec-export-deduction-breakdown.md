# Export Deduction Breakdown Columns Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the per-channel channel-direct vs account-allocated deduction split (already computed by the PR-2 net builder) across the XLSX/PDF/PPTX finance exports, plus a matching month-level aggregate wherever the month total already appears.

**Architecture:** Add two additive, finance-layer aggregate fields to `MonthNetRevenueSummary` (summed over the `calculated` subset with `None`→0 coalescing, serialized in `to_api()`), then render the existing per-channel breakdown fields and the new aggregates in the three report builders. Read-surface only: no migration, no auth/audit change, no allocation-math change.

**Tech Stack:** Python 3, dataclasses, openpyxl (XLSX), reportlab (PDF), python-pptx (PPTX), pytest.

**Source spec:** `Docs/superpowers/specs/2026-06-02-spec-export-deduction-breakdown-design.md`

**Branch:** `spec/export-deduction-breakdown` (off `main` `3d1916c`; spec commits `4d31482` + `5552bed`).

---

## Critical context for the implementer (read before Task 1)

**The semantic invariant (do not break it).** On the COMPONENT_DERIVED path (`net_revenue.py:216-252`), `deduction_amount_usd = channel_direct + account_allocated` exactly. On the source-net / CALCULATED path, `deduction_amount_usd` is the source-reported deduction and **both breakdown fields are `None`**. So at the month level the new aggregates capture only the component-derived split: `total_deduction_amount_usd >= total_channel_direct + total_account_allocated`, equal only when nothing in scope is source-net. **Exports keep all three numbers — never replace the total with the split.**

**Rendering rules.**
- Per-channel `None` cells render via `_decimal_to_api(None) → None` (blank). No special-casing.
- Month aggregates are never `None`: coalesce `None`→`Decimal("0")` in the sum.
- XLSX headers use the full API field names (matching the existing `approved_manual_override_total_usd` header). PDF/PPTX use concise human labels.
- `_decimal_to_api` normalizes: `Decimal("30.00")→"30"`, `Decimal("100.000000")→"100"`, `Decimal("130.000000")→"130"`, `Decimal("0.00")→"0"`.

**Parity surfaces (each finance "executive summary" payload that exposes `total_deduction_amount_usd` also gains the two new fields, for serialization parity):** the XLSX `_executive_summary` + `_scope_breakdown`, the PDF `_executive_summary`, and the PPTX `_executive_summary`. The PDF `_summary_table` cherry-picks keys (Month, Total Net Revenue, statuses) and is **not** changed — the PDF's rendered split lives only in `_gross_net_table`; the PDF `_executive_summary` change is payload-only (it surfaces in `report.to_api()["executive_summary"]`).

**Verified anchors (re-confirm with a quick read before editing — line numbers may drift):**
- `backend/ums_smart_revenue/finance/net_revenue.py`: `MonthNetRevenueSummary` dataclass + `to_api()` (`84-122`); `build_month_net_revenue_summary` (`600-698`), with the `calculated` subset at `:557` and the `total_deduction_amount_usd=sum(...)` at `:691-693`; `_component_derived_channel_summary` (`216-252`). The `MonthNetRevenueSummary` field order is `month, status, channel_count, calculated_channel_count, missing_net_source_count, pending_manual_override_count, total_adjusted_gross_revenue_usd, total_net_revenue_usd, total_deduction_amount_usd, unallocated_account_deduction_total_usd, unallocated_account_issues, channels` — **no field has a default**. Import at `:13`: `from ums_smart_revenue.finance.decimal_formatting import decimal_to_api as _decimal_to_api`.
- `backend/ums_smart_revenue/reports/finance_workbook.py`: Channel Breakdown sheet (`200-235`), Deductions sheet (`236-257`), `_executive_summary` (`335-370`), `_scope_breakdown` (`373-397`) + Company/Sector call sites (`192-199`), Raw Appendix (`297-303`), `FinanceWorkbookPreview.to_api()` (`83-113`). Helpers: `_decimal_to_api` (imported), `_write_table_sheet` (`412-424`), `_write_key_value_sheet` (`400-409`).
- `backend/ums_smart_revenue/reports/executive_pdf.py`: `_gross_net_table` (`277-293`), `_summary_table` (`262-274`, cherry-picks — leave unchanged), `_channel_ranking_table` (`313-335`, **leave unchanged**), `_executive_summary` (`226-259`), `build_executive_pdf_bytes` (`146-201`).
- `backend/ums_smart_revenue/reports/branded_slide_pack.py`: deduction slide (`229-238`), `_executive_summary` (`444-475`), `_add_content_slide` (`303-331`).
- `backend/ums_smart_revenue/api/revenue.py`: month net-revenue endpoint returns `summary.to_api()` at `:1166`.
- `tests/api/test_export_preview_api.py`: seed (`:83`) has only source-net channels (no `DeductionComponentORM`), so the preview's breakdown aggregates are `"0"`; the finance-admin preview test is `test_finance_admin_previews_finance_workbook_with_sensitive_audit` (`238-279`); the preview route returns the full `preview.to_api()` (so `payload["source_summaries"]["net_revenue"]` is present).

**Validation commands (this repo uses `python -m`, not bare `pytest`):**
- Targeted: `python -m pytest <files> -q`
- Lint: `python -m ruff check backend tests scripts`
- Whitespace: `git diff --check`
- Full suite: `python -m pytest -q` (PG-tier tests need `UMS_TEST_DATABASE_URL` → disposable `ums-mig-pg-test` container).

**Commit discipline:** every commit message MUST NOT contain any `Co-Authored-By` trailer or Claude footer. Do NOT push or open a PR.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `backend/ums_smart_revenue/finance/net_revenue.py` | Finance-layer month summary | Add 2 aggregate fields + sum + `to_api` |
| `backend/ums_smart_revenue/reports/finance_workbook.py` | XLSX builder | Per-channel columns (2 sheets) + aggregate rows (`_executive_summary`, `_scope_breakdown`) |
| `backend/ums_smart_revenue/reports/executive_pdf.py` | PDF builder | Aggregate rows in `_gross_net_table` + 2 fields in `_executive_summary` payload (ranking + summary tables untouched) |
| `backend/ums_smart_revenue/reports/branded_slide_pack.py` | PPTX builder | 3-bullet deduction slide + `_executive_summary` payload |
| `tests/finance/test_net_revenue_account_allocations.py` | Finance aggregate tests | New aggregate + mixed-invariant + coalescing tests |
| `tests/api/test_net_revenue_api.py` | API additive-field test | Assert 2 new response fields |
| `tests/api/test_export_preview_api.py` | Preview-contract parity test | Assert 2 fields in `executive_summary` + `source_summaries.net_revenue` |
| `tests/reports/test_finance_workbook_preview.py` | XLSX tests | Fixture +2 fields; new breakdown test; updated exec-summary assertion |
| `tests/reports/test_executive_pdf.py` | PDF tests | Fixture +2 fields; new aggregate-rows test; exec-summary payload assertions |
| `tests/reports/test_branded_slide_pack.py` | PPTX tests | Fixture +2 fields; new bullets test |
| `Docs/01_IMPLEMENTATION_PLAN.md`, `Docs/15_DELIVERY_BACKLOG.md` | Status | Mark export-breakdown shipped |

---

## Task 1: Finance-layer aggregate fields

Add the two month-level aggregate fields, compute them in the builder, serialize them, and keep the suite compiling by updating the three report fixtures that construct `MonthNetRevenueSummary` directly.

**Files:**
- Modify: `backend/ums_smart_revenue/finance/net_revenue.py` (`MonthNetRevenueSummary` `84-122`, builder `600-698`)
- Modify: `tests/reports/test_finance_workbook_preview.py:149-162`, `tests/reports/test_executive_pdf.py:178-191`, `tests/reports/test_branded_slide_pack.py:184-197` (fixture constructors)
- Test: `tests/finance/test_net_revenue_account_allocations.py`, `tests/api/test_net_revenue_api.py:225-272`

- [ ] **Step 1: Write the failing finance tests**

Append to `tests/finance/test_net_revenue_account_allocations.py`:

```python
def test_month_aggregate_breakdown_totals_sum_component_split():
    """Month aggregates sum the per-channel component-derived split."""
    channel_direct = DeductionComponent(
        id="dc-mixed", month=MONTH, component_kind="DEDUCTION", scope_kind="CHANNEL",
        scope_id="chA", amount_usd=Decimal("30.00"), amount_native=None,
        currency_code="USD", source_system="adsense_management",
        source_table="google_revenue_source_rows", source_id=None,
        source_key=None, source_report_id=None, raw_payload={}, component_key="cd-mixed",
    )
    summary = build_month_net_revenue_summary(
        month=MONTH,
        facts=[
            _fact(channel="chA", net=None, gross="1000.00"),
            _fact(channel="chB", net="880.00", gross="1000.00"),
        ],
        manual_overrides=[],
        deduction_components=[channel_direct],
        account_allocations=[_alloc(channel="chA", amount="100.000000")],
    )
    # chA -> COMPONENT_DERIVED: direct 30 + allocated 100 = 130 deduction.
    # chB -> source-net CALCULATED: deduction 120, breakdown fields None.
    assert summary.total_channel_direct_deduction_amount_usd == Decimal("30.00")
    assert summary.total_account_allocated_deduction_amount_usd == Decimal("100.000000")
    assert summary.total_deduction_amount_usd == Decimal("250.000000")  # 130 + 120
    # Invariant: month total exceeds the component-derived split because chB's
    # source-reported deduction has no direct/allocated breakdown.
    assert (
        summary.total_deduction_amount_usd
        > summary.total_channel_direct_deduction_amount_usd
        + summary.total_account_allocated_deduction_amount_usd
    )


def test_month_aggregate_breakdown_coalesces_none_to_zero():
    """An all-source-net month yields 0 aggregates, never None."""
    summary = build_month_net_revenue_summary(
        month=MONTH,
        facts=[_fact(channel="chA", net="880.00", gross="1000.00")],
        manual_overrides=[],
    )
    assert summary.total_channel_direct_deduction_amount_usd == Decimal("0")
    assert summary.total_account_allocated_deduction_amount_usd == Decimal("0")
```

- [ ] **Step 2: Run the finance tests to verify they fail**

Run: `python -m pytest tests/finance/test_net_revenue_account_allocations.py::test_month_aggregate_breakdown_totals_sum_component_split tests/finance/test_net_revenue_account_allocations.py::test_month_aggregate_breakdown_coalesces_none_to_zero -q`
Expected: FAIL with `AttributeError: 'MonthNetRevenueSummary' object has no attribute 'total_channel_direct_deduction_amount_usd'`.

- [ ] **Step 3: Implement the finance-layer aggregate (dataclass + builder + to_api), and update the 3 report fixtures so the suite still compiles**

In `backend/ums_smart_revenue/finance/net_revenue.py`, add the two fields to the `MonthNetRevenueSummary` dataclass immediately after `total_deduction_amount_usd`:

```python
    total_deduction_amount_usd: Decimal
    total_channel_direct_deduction_amount_usd: Decimal
    total_account_allocated_deduction_amount_usd: Decimal
    unallocated_account_deduction_total_usd: Decimal | None
```

Add the two keys to `MonthNetRevenueSummary.to_api()` immediately after the `total_deduction_amount_usd` entry:

```python
            "total_deduction_amount_usd": _decimal_to_api(
                self.total_deduction_amount_usd
            ),
            "total_channel_direct_deduction_amount_usd": _decimal_to_api(
                self.total_channel_direct_deduction_amount_usd
            ),
            "total_account_allocated_deduction_amount_usd": _decimal_to_api(
                self.total_account_allocated_deduction_amount_usd
            ),
            "unallocated_account_deduction_total_usd": _decimal_to_api(
                self.unallocated_account_deduction_total_usd
            ),
```

In `build_month_net_revenue_summary`, add the two aggregate computations to the returned `MonthNetRevenueSummary(...)` immediately after `total_deduction_amount_usd=sum(...)`:

```python
        total_deduction_amount_usd=sum(
            (channel.deduction_amount_usd for channel in calculated),
            Decimal("0"),
        ),
        # Breakdown fields are None on source-net channels, so coalesce to 0;
        # these aggregates therefore capture only the component-derived split and
        # do NOT necessarily equal total_deduction_amount_usd (see spec section 4).
        total_channel_direct_deduction_amount_usd=sum(
            (
                (channel.channel_direct_deduction_amount_usd or Decimal("0"))
                for channel in calculated
            ),
            Decimal("0"),
        ),
        total_account_allocated_deduction_amount_usd=sum(
            (
                (channel.account_allocated_deduction_amount_usd or Decimal("0"))
                for channel in calculated
            ),
            Decimal("0"),
        ),
        unallocated_account_deduction_total_usd=unallocated_total,
```

Then update the three report fixtures' `MonthNetRevenueSummary(...)` constructors so the suite compiles. In **each** of `tests/reports/test_finance_workbook_preview.py:149-162`, `tests/reports/test_executive_pdf.py:178-191`, and `tests/reports/test_branded_slide_pack.py:184-197`, add the two fields immediately after `total_deduction_amount_usd=Decimal("120.00"),` (their single channel is source-net, so the aggregates are 0):

```python
        total_deduction_amount_usd=Decimal("120.00"),
        total_channel_direct_deduction_amount_usd=Decimal("0.00"),
        total_account_allocated_deduction_amount_usd=Decimal("0.00"),
        unallocated_account_deduction_total_usd=None,
```

- [ ] **Step 4: Run the finance + report-fixture tests to verify they pass**

Run: `python -m pytest tests/finance/test_net_revenue_account_allocations.py tests/reports/test_finance_workbook_preview.py tests/reports/test_executive_pdf.py tests/reports/test_branded_slide_pack.py -q`
Expected: PASS (the new finance tests pass; the report tests still pass because the fixtures now supply the required fields and the builders do not yet read them).

- [ ] **Step 5: Add the additive net-revenue API assertion**

In `tests/api/test_net_revenue_api.py`, inside `test_net_revenue_endpoint_derives_component_net_for_missing_net_channel` (`:225-272`), add these assertions immediately after `assert channel_b["deduction_amount_usd"] == "20"` (`:267`):

```python
    assert channel_b["deduction_amount_usd"] == "20"
    assert body["total_channel_direct_deduction_amount_usd"] == "20"
    assert body["total_account_allocated_deduction_amount_usd"] == "0"
```

(channel-tv-b is a CHANNEL-scope component → `channel_direct=20`, `account_allocated=0`; tv-a is source-net → contributes 0 to both.)

- [ ] **Step 6: Run the API test to verify it passes**

Run: `python -m pytest tests/api/test_net_revenue_api.py::test_net_revenue_endpoint_derives_component_net_for_missing_net_channel -q`
Expected: PASS (confirms the two fields are additively present on the month endpoint response via `to_api()`).

- [ ] **Step 7: Commit**

```bash
git add backend/ums_smart_revenue/finance/net_revenue.py tests/finance/test_net_revenue_account_allocations.py tests/api/test_net_revenue_api.py tests/reports/test_finance_workbook_preview.py tests/reports/test_executive_pdf.py tests/reports/test_branded_slide_pack.py
git commit -m "feat(finance): month-level channel-direct/account-allocated deduction aggregates"
```

---

## Task 2: XLSX workbook breakdown columns + aggregate rows + preview-contract parity

Add per-channel breakdown columns to the Channel Breakdown and Deductions sheets, the two aggregate rows to `_executive_summary` (Executive Summary sheet + preview JSON) and `_scope_breakdown` (Company/Sector breakdown sheets), and direct preview-API assertions. The Raw Appendix auto-propagates via `to_api()`.

**Files:**
- Modify: `backend/ums_smart_revenue/reports/finance_workbook.py` (sheets `200-257`, `_executive_summary` `335-370`, `_scope_breakdown` `373-397`)
- Test: `tests/reports/test_finance_workbook_preview.py`, `tests/api/test_export_preview_api.py`

- [ ] **Step 1: Write the failing tests**

**(a)** In `tests/reports/test_finance_workbook_preview.py`, extend the existing exec-summary assertion in `test_finance_workbook_preview_builds_sheet_manifest_from_source_summaries` (`:44-62`) by adding the two new keys immediately after `"total_deduction_amount_usd": "120",`:

```python
        "total_deduction_amount_usd": "120",
        "total_channel_direct_deduction_amount_usd": "0",
        "total_account_allocated_deduction_amount_usd": "0",
        "payment_gap_usd": "0",
```

Add `import json` to the top-level imports of the file (used by the breakdown test's Raw Appendix assertion). Then add a component-derived fixture and a breakdown test at the end of the file:

```python
def _net_revenue_summary_with_breakdown() -> MonthNetRevenueSummary:
    """Build a summary with a COMPONENT_DERIVED channel carrying a real split."""
    channel = ChannelNetRevenueSummary(
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        status="COMPONENT_DERIVED",
        primary_source_kind="ADSENSE",
        baseline_gross_revenue_usd=Decimal("1000.00"),
        baseline_net_revenue_usd=None,
        approved_manual_override_total_usd=Decimal("0.00"),
        adjusted_gross_revenue_usd=Decimal("1000.00"),
        net_revenue_usd=Decimal("870.00"),
        deduction_amount_usd=Decimal("130.00"),
        channel_direct_deduction_amount_usd=Decimal("30.00"),
        account_allocated_deduction_amount_usd=Decimal("100.00"),
        deduction_percentage=Decimal("13.0000"),
        confidence="D_ESTIMATED",
        approved_manual_override_count=0,
        pending_manual_override_count=0,
        issues=[],
    )
    return MonthNetRevenueSummary(
        month="2026-03",
        status="CALCULATED",
        channel_count=1,
        calculated_channel_count=1,
        missing_net_source_count=0,
        pending_manual_override_count=0,
        total_adjusted_gross_revenue_usd=Decimal("1000.00"),
        total_net_revenue_usd=Decimal("870.00"),
        total_deduction_amount_usd=Decimal("130.00"),
        total_channel_direct_deduction_amount_usd=Decimal("30.00"),
        total_account_allocated_deduction_amount_usd=Decimal("100.00"),
        unallocated_account_deduction_total_usd=None,
        unallocated_account_issues=None,
        channels=[channel],
    )


def test_finance_workbook_renders_deduction_breakdown_columns_and_rows():
    """XLSX renders per-channel split columns, aggregate summary/scope rows, and Raw Appendix."""
    preview = build_finance_workbook_preview(
        export_job=_export_job(export_type="FINANCE_EXCEL"),
        net_revenue=_net_revenue_summary_with_breakdown(),
        payment_match=_payment_match_summary(status="PAYMENT_MATCHED"),
        bank_reconciliation=_bank_summary(status="BANK_CONFIRMED"),
        smart_alerts=_smart_alert_summary(alert_count=0),
    )
    workbook = load_workbook(BytesIO(build_finance_workbook_xlsx(preview)), data_only=True)

    # Channel Breakdown: net_revenue_usd stays at column H; the two new columns
    # (J, K) follow deduction_amount_usd (I).
    channel_breakdown = workbook["Channel Breakdown"]
    assert channel_breakdown["H1"].value == "net_revenue_usd"
    assert channel_breakdown["I1"].value == "deduction_amount_usd"
    assert channel_breakdown["J1"].value == "channel_direct_deduction_amount_usd"
    assert channel_breakdown["K1"].value == "account_allocated_deduction_amount_usd"
    assert channel_breakdown["I2"].value == "130"
    assert channel_breakdown["J2"].value == "30"
    assert channel_breakdown["K2"].value == "100"

    # Deductions sheet: split columns (E, F) follow deduction_amount_usd (D).
    deductions = workbook["Deductions"]
    assert deductions["D1"].value == "deduction_amount_usd"
    assert deductions["E1"].value == "channel_direct_deduction_amount_usd"
    assert deductions["F1"].value == "account_allocated_deduction_amount_usd"
    assert deductions["D2"].value == "130"
    assert deductions["E2"].value == "30"
    assert deductions["F2"].value == "100"

    # Executive Summary aggregate rows (key-value sheet).
    exec_summary = preview.to_api()["executive_summary"]
    assert exec_summary["total_channel_direct_deduction_amount_usd"] == "30"
    assert exec_summary["total_account_allocated_deduction_amount_usd"] == "100"

    # Company + Sector breakdown sheets carry the same aggregates.
    for sheet_name in ("Company Breakdown", "Sector Breakdown"):
        keys = {row[0].value: row[1].value for row in workbook[sheet_name].iter_rows()}
        assert keys["total_channel_direct_deduction_amount_usd"] == "30"
        assert keys["total_account_allocated_deduction_amount_usd"] == "100"

    # Raw Appendix serializes the net_revenue to_api(), which now carries the fields.
    raw = {row[0].value: row[1].value for row in workbook["Raw Appendix"].iter_rows()}
    net_revenue_json = json.loads(raw["net_revenue"])
    assert net_revenue_json["total_channel_direct_deduction_amount_usd"] == "30"
    assert net_revenue_json["total_account_allocated_deduction_amount_usd"] == "100"
```

**(b)** In `tests/api/test_export_preview_api.py`, inside `test_finance_admin_previews_finance_workbook_with_sensitive_audit` (`238-279`), add the preview-contract parity assertions immediately after the existing `assert payload["executive_summary"]["bank_reconciliation_status"] == ("BANK_CONFIRMED")` block:

```python
    assert payload["executive_summary"]["total_channel_direct_deduction_amount_usd"] == "0"
    assert payload["executive_summary"]["total_account_allocated_deduction_amount_usd"] == "0"
    net_revenue_summary = payload["source_summaries"]["net_revenue"]
    assert net_revenue_summary["total_channel_direct_deduction_amount_usd"] == "0"
    assert net_revenue_summary["total_account_allocated_deduction_amount_usd"] == "0"
```

(The preview seed has only source-net channels, so both aggregates are `"0"`; this directly asserts the FastAPI preview contract surfaces the two fields in both `executive_summary` and `source_summaries.net_revenue`.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/reports/test_finance_workbook_preview.py tests/api/test_export_preview_api.py::test_finance_admin_previews_finance_workbook_with_sensitive_audit -q`
Expected: FAIL — the workbook manifest test fails on the missing exec-summary keys; the new workbook test fails because the columns/rows are not rendered yet; the preview-API test fails on the missing `executive_summary` keys (the `source_summaries.net_revenue` keys already exist from Task 1, but `executive_summary` does not until Step 3).

- [ ] **Step 3: Implement the XLSX changes**

In `backend/ums_smart_revenue/reports/finance_workbook.py`, in the Channel Breakdown sheet (`200-235`) insert the two headers after `"deduction_amount_usd"` and the two cell values after `_decimal_to_api(channel.deduction_amount_usd)`:

```python
            "net_revenue_usd",
            "deduction_amount_usd",
            "channel_direct_deduction_amount_usd",
            "account_allocated_deduction_amount_usd",
            "deduction_percentage",
```

```python
                _decimal_to_api(channel.net_revenue_usd),
                _decimal_to_api(channel.deduction_amount_usd),
                _decimal_to_api(channel.channel_direct_deduction_amount_usd),
                _decimal_to_api(channel.account_allocated_deduction_amount_usd),
                _decimal_to_api(channel.deduction_percentage),
```

In the Deductions sheet (`236-257`) insert the two headers after `"deduction_amount_usd"` and the two cell values after `_decimal_to_api(channel.deduction_amount_usd)`:

```python
            "net_revenue_usd",
            "deduction_amount_usd",
            "channel_direct_deduction_amount_usd",
            "account_allocated_deduction_amount_usd",
            "deduction_percentage",
            "approved_manual_override_total_usd",
```

```python
                _decimal_to_api(channel.net_revenue_usd),
                _decimal_to_api(channel.deduction_amount_usd),
                _decimal_to_api(channel.channel_direct_deduction_amount_usd),
                _decimal_to_api(channel.account_allocated_deduction_amount_usd),
                _decimal_to_api(channel.deduction_percentage),
                _decimal_to_api(channel.approved_manual_override_total_usd),
```

In `_executive_summary` (`335-370`) add the two entries after `"total_deduction_amount_usd"`:

```python
        "total_deduction_amount_usd": _decimal_to_api(
            net_revenue.total_deduction_amount_usd
        ),
        "total_channel_direct_deduction_amount_usd": _decimal_to_api(
            net_revenue.total_channel_direct_deduction_amount_usd
        ),
        "total_account_allocated_deduction_amount_usd": _decimal_to_api(
            net_revenue.total_account_allocated_deduction_amount_usd
        ),
        "payment_gap_usd": _decimal_to_api(payment_match.payment_gap_usd),
```

In `_scope_breakdown` (`373-397`) add the two entries after `"total_deduction_amount_usd"`:

```python
        "total_deduction_amount_usd": _decimal_to_api(
            preview.net_revenue.total_deduction_amount_usd
        ),
        "total_channel_direct_deduction_amount_usd": _decimal_to_api(
            preview.net_revenue.total_channel_direct_deduction_amount_usd
        ),
        "total_account_allocated_deduction_amount_usd": _decimal_to_api(
            preview.net_revenue.total_account_allocated_deduction_amount_usd
        ),
    }
```

(Raw Appendix needs no change — it serializes `preview.to_api()["source_summaries"]["net_revenue"]`, already extended in Task 1.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/reports/test_finance_workbook_preview.py tests/api/test_export_preview_api.py -q`
Expected: PASS (updated manifest test, new workbook breakdown test, and the preview-API parity assertions all pass).

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/reports/finance_workbook.py tests/reports/test_finance_workbook_preview.py tests/api/test_export_preview_api.py
git commit -m "feat(reports): XLSX deduction breakdown columns + month aggregate rows + preview parity"
```

---

## Task 3: PDF gross-vs-net aggregate rows + executive-summary payload

Add the two month-aggregate rows to the PDF gross-vs-net table (rendered) and the two fields to the PDF `_executive_summary` payload (parity in `report.to_api()`). The 4-column channel-ranking table and the cherry-picking `_summary_table` are **not** touched.

**Files:**
- Modify: `backend/ums_smart_revenue/reports/executive_pdf.py` (`_gross_net_table` `277-293`, `_executive_summary` `226-259`)
- Test: `tests/reports/test_executive_pdf.py`

- [ ] **Step 1: Write the failing tests**

**(a)** In `tests/reports/test_executive_pdf.py`, extend the existing manifest test `test_executive_pdf_report_builds_section_manifest_from_source_summaries` (`:44-48`) by adding payload-parity assertions after the `bank_reconciliation_status` assertion:

```python
    assert payload["executive_summary"]["bank_reconciliation_status"] == (
        "BANK_CONFIRMED"
    )
    assert payload["executive_summary"]["total_channel_direct_deduction_amount_usd"] == "0"
    assert payload["executive_summary"]["total_account_allocated_deduction_amount_usd"] == "0"
```

**(b)** Add a component-derived fixture and a rendered-aggregate test at the end of the file:

```python
def _net_revenue_summary_with_breakdown() -> MonthNetRevenueSummary:
    """Build a summary with a COMPONENT_DERIVED channel carrying a real split."""
    channel = ChannelNetRevenueSummary(
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        status="COMPONENT_DERIVED",
        primary_source_kind="ADSENSE",
        baseline_gross_revenue_usd=Decimal("1000.00"),
        baseline_net_revenue_usd=None,
        approved_manual_override_total_usd=Decimal("0.00"),
        adjusted_gross_revenue_usd=Decimal("1000.00"),
        net_revenue_usd=Decimal("870.00"),
        deduction_amount_usd=Decimal("130.00"),
        channel_direct_deduction_amount_usd=Decimal("30.00"),
        account_allocated_deduction_amount_usd=Decimal("100.00"),
        deduction_percentage=Decimal("13.0000"),
        confidence="D_ESTIMATED",
        approved_manual_override_count=0,
        pending_manual_override_count=0,
        issues=[],
    )
    return MonthNetRevenueSummary(
        month="2026-03",
        status="CALCULATED",
        channel_count=1,
        calculated_channel_count=1,
        missing_net_source_count=0,
        pending_manual_override_count=0,
        total_adjusted_gross_revenue_usd=Decimal("1000.00"),
        total_net_revenue_usd=Decimal("870.00"),
        total_deduction_amount_usd=Decimal("130.00"),
        total_channel_direct_deduction_amount_usd=Decimal("30.00"),
        total_account_allocated_deduction_amount_usd=Decimal("100.00"),
        unallocated_account_deduction_total_usd=None,
        unallocated_account_issues=None,
        channels=[channel],
    )


def test_executive_pdf_renders_deduction_breakdown_aggregate_rows():
    """PDF gross-vs-net table shows the two month-level aggregate deduction rows."""
    report = build_executive_pdf_report(
        export_job=_export_job(export_type="EXECUTIVE_PDF"),
        net_revenue=_net_revenue_summary_with_breakdown(),
        payment_match=_payment_match_summary(status="PAYMENT_MATCHED"),
        bank_reconciliation=_bank_summary(status="BANK_CONFIRMED"),
        smart_alerts=_smart_alert_summary(),
    )
    text = _extract_pdf_text(build_executive_pdf_bytes(report))

    assert "Channel-Direct Deduction USD" in text
    assert "Account-Allocated Deduction USD" in text
    assert "30" in text
    assert "100" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/reports/test_executive_pdf.py::test_executive_pdf_report_builds_section_manifest_from_source_summaries tests/reports/test_executive_pdf.py::test_executive_pdf_renders_deduction_breakdown_aggregate_rows -q`
Expected: FAIL — the manifest test fails on the missing `executive_summary` keys; the rendered test fails on `assert "Channel-Direct Deduction USD" in text`.

- [ ] **Step 3: Implement the PDF changes**

In `backend/ums_smart_revenue/reports/executive_pdf.py`, in `_gross_net_table` (`277-293`) add the two rendered rows after `"Total Deduction Amount USD"`:

```python
            "Total Deduction Amount USD": _decimal_to_api(
                report.net_revenue.total_deduction_amount_usd
            ),
            "Channel-Direct Deduction USD": _decimal_to_api(
                report.net_revenue.total_channel_direct_deduction_amount_usd
            ),
            "Account-Allocated Deduction USD": _decimal_to_api(
                report.net_revenue.total_account_allocated_deduction_amount_usd
            ),
            "Payment Gap USD": _decimal_to_api(report.payment_match.payment_gap_usd),
```

In `_executive_summary` (`226-259`) add the two payload entries after `"total_deduction_amount_usd"` (payload parity only — do **not** modify `_summary_table`, which cherry-picks keys and must not duplicate the gross-vs-net rows):

```python
        "total_deduction_amount_usd": _decimal_to_api(
            net_revenue.total_deduction_amount_usd
        ),
        "total_channel_direct_deduction_amount_usd": _decimal_to_api(
            net_revenue.total_channel_direct_deduction_amount_usd
        ),
        "total_account_allocated_deduction_amount_usd": _decimal_to_api(
            net_revenue.total_account_allocated_deduction_amount_usd
        ),
        "payment_gap_usd": _decimal_to_api(payment_match.payment_gap_usd),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/reports/test_executive_pdf.py -q`
Expected: PASS (new tests pass; existing PDF tests stay green — the ranking and summary tables are unchanged).

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/reports/executive_pdf.py tests/reports/test_executive_pdf.py
git commit -m "feat(reports): PDF gross-vs-net deduction breakdown rows + summary payload parity"
```

---

## Task 4: PPTX deduction slide bullets + summary payload

Expand the single deduction bullet into three (total + both splits) and add the two fields to the slide-pack `_executive_summary` payload.

**Files:**
- Modify: `backend/ums_smart_revenue/reports/branded_slide_pack.py` (deduction slide `229-238`, `_executive_summary` `444-475`)
- Test: `tests/reports/test_branded_slide_pack.py`

- [ ] **Step 1: Write the failing test**

In `tests/reports/test_branded_slide_pack.py`, add a component-derived fixture and a test at the end of the file:

```python
def _net_revenue_summary_with_breakdown() -> MonthNetRevenueSummary:
    """Build a summary with a COMPONENT_DERIVED channel carrying a real split."""
    channel = ChannelNetRevenueSummary(
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        status="COMPONENT_DERIVED",
        primary_source_kind="ADSENSE",
        baseline_gross_revenue_usd=Decimal("1000.00"),
        baseline_net_revenue_usd=None,
        approved_manual_override_total_usd=Decimal("0.00"),
        adjusted_gross_revenue_usd=Decimal("1000.00"),
        net_revenue_usd=Decimal("870.00"),
        deduction_amount_usd=Decimal("130.00"),
        channel_direct_deduction_amount_usd=Decimal("30.00"),
        account_allocated_deduction_amount_usd=Decimal("100.00"),
        deduction_percentage=Decimal("13.0000"),
        confidence="D_ESTIMATED",
        approved_manual_override_count=0,
        pending_manual_override_count=0,
        issues=[],
    )
    return MonthNetRevenueSummary(
        month="2026-03",
        status="CALCULATED",
        channel_count=1,
        calculated_channel_count=1,
        missing_net_source_count=0,
        pending_manual_override_count=0,
        total_adjusted_gross_revenue_usd=Decimal("1000.00"),
        total_net_revenue_usd=Decimal("870.00"),
        total_deduction_amount_usd=Decimal("130.00"),
        total_channel_direct_deduction_amount_usd=Decimal("30.00"),
        total_account_allocated_deduction_amount_usd=Decimal("100.00"),
        unallocated_account_deduction_total_usd=None,
        unallocated_account_issues=None,
        channels=[channel],
    )


def test_branded_slide_pack_renders_deduction_breakdown_bullets():
    """PPTX deduction slide shows total + channel-direct + account-allocated bullets."""
    report = build_branded_slide_pack_report(
        export_job=_export_job(export_type="BRANDED_SLIDE_PACK"),
        net_revenue=_net_revenue_summary_with_breakdown(),
        payment_match=_payment_match_summary(status="PAYMENT_MATCHED"),
        bank_reconciliation=_bank_summary(status="BANK_CONFIRMED"),
        smart_alerts=_smart_alert_summary(),
    )
    combined_text = "\n".join(
        _slide_texts(Presentation(BytesIO(build_branded_slide_pack_pptx(report))))
    )

    assert "Total deduction amount USD: 130" in combined_text
    assert "Channel-direct deduction USD: 30" in combined_text
    assert "Account-allocated deduction USD: 100" in combined_text

    payload = report.to_api()
    assert payload["executive_summary"]["total_channel_direct_deduction_amount_usd"] == "30"
    assert payload["executive_summary"]["total_account_allocated_deduction_amount_usd"] == "100"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/reports/test_branded_slide_pack.py::test_branded_slide_pack_renders_deduction_breakdown_bullets -q`
Expected: FAIL with `assert "Channel-direct deduction USD: 30" in combined_text` (bullet not yet present).

- [ ] **Step 3: Implement the PPTX change**

In `backend/ums_smart_revenue/reports/branded_slide_pack.py`, expand the deduction slide bullets (`229-238`):

```python
    _add_content_slide(
        presentation,
        "Revenue deduction explanation",
        [
            "Total deduction amount USD: "
            f"{_decimal_to_api(net_revenue.total_deduction_amount_usd)}",
            "Channel-direct deduction USD: "
            f"{_decimal_to_api(net_revenue.total_channel_direct_deduction_amount_usd)}",
            "Account-allocated deduction USD: "
            f"{_decimal_to_api(net_revenue.total_account_allocated_deduction_amount_usd)}",
            "Deductions use SQL revenue facts plus approved manual overrides.",
            "Pending overrides are shown as risk and do not change net revenue.",
        ],
    )
```

Add the two entries to `_executive_summary` (`444-475`) after `"total_deduction_amount_usd"`:

```python
        "total_deduction_amount_usd": _decimal_to_api(
            net_revenue.total_deduction_amount_usd
        ),
        "total_channel_direct_deduction_amount_usd": _decimal_to_api(
            net_revenue.total_channel_direct_deduction_amount_usd
        ),
        "total_account_allocated_deduction_amount_usd": _decimal_to_api(
            net_revenue.total_account_allocated_deduction_amount_usd
        ),
        "payment_gap_usd": _decimal_to_api(payment_match.payment_gap_usd),
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/reports/test_branded_slide_pack.py -q`
Expected: PASS (new test passes; existing slide tests stay green — `BRANDED_SLIDE_NAMES` and the Total bullet are unchanged).

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/reports/branded_slide_pack.py tests/reports/test_branded_slide_pack.py
git commit -m "feat(reports): PPTX deduction breakdown bullets + summary payload"
```

---

## Task 5: Docs status updates + full validation

Update the per-PR tracking docs and run the full validation gate.

**Files:**
- Modify: `Docs/01_IMPLEMENTATION_PLAN.md`, `Docs/15_DELIVERY_BACKLOG.md`

- [ ] **Step 1: Update `Docs/15_DELIVERY_BACKLOG.md`**

In the Spec 2b allocation-engine entry (around `:317-341`), change the `Remaining:` line so export breakdown columns is marked shipped. Replace:

```
  Remaining: PAYMENT-grain (needs a
  payment→account hop); persisted/committed allocation; other allocation methods;
  export breakdown columns.
```

with:

```
  PR-4 shipped (this branch): the channel-direct/account-allocated deduction split now
  renders in all finance exports — per-channel columns in the XLSX Channel Breakdown +
  Deductions sheets, month-level aggregate rows in the XLSX Executive Summary +
  Company/Sector breakdown sheets, the PDF gross-vs-net table, and the PPTX deduction
  slide — backed by two additive total_channel_direct/account_allocated aggregate fields
  on MonthNetRevenueSummary (no migration, no auth/audit/allocation-math change).
  Remaining: PAYMENT-grain (needs a
  payment→account hop); persisted/committed allocation; other allocation methods.
```

- [ ] **Step 2: Update `Docs/01_IMPLEMENTATION_PLAN.md`**

In the Spec 2b allocation-rules entry (around `:444-509`), update the `Remaining:` line. Replace:

```
  Remaining: PAYMENT-grain, persisted/committed writes, other methods, export breakdown columns.
```

with:

```
  PR-4 SHIPPED (this branch): export deduction breakdown — XLSX/PDF/PPTX surface the
  channel-direct vs account-allocated split plus two additive month aggregates on
  MonthNetRevenueSummary; read-surface only.
  Remaining: PAYMENT-grain, persisted/committed writes, other methods.
```

(If the exact `Remaining:` wording differs at implementation time, edit the live line to the same effect — mark export breakdown columns shipped, keep the other three remaining items.)

- [ ] **Step 3: Verify docs whitespace and commit**

Run: `git diff --check`
Expected: no output.

```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "docs(plan): mark Spec 2b export deduction breakdown shipped"
```

- [ ] **Step 4: Run the full validation gate**

Run:
```
python -m ruff check backend tests scripts
python -m pytest tests/finance/test_net_revenue_account_allocations.py tests/api/test_exports_account_allocation.py tests/reports/test_finance_workbook_preview.py tests/reports/test_executive_pdf.py tests/reports/test_branded_slide_pack.py tests/api/test_export_preview_api.py tests/api/test_net_revenue_api.py -q
python -m pytest -q
git diff --check
```
Expected: ruff clean; targeted set green; full suite green (PG container running with `UMS_TEST_DATABASE_URL` set); `git diff --check` no output.

---

## Self-Review

**1. Spec coverage**

| Spec requirement | Task |
|---|---|
| §5 two aggregate fields, no default, None→0 sum, to_api | Task 1 |
| §7.1 XLSX per-channel columns (both sheets) | Task 2 |
| §7.1 XLSX Executive Summary + Company/Sector (`_scope_breakdown`) rows | Task 2 |
| §7.1 Raw Appendix auto-propagation | Task 2 (asserted, no code) |
| §6 preview parity — `executive_summary` + `source_summaries.net_revenue` (preview API) | Task 2 (Step 1b, direct API assertion) |
| §7.2 PDF gross-vs-net aggregate rows (rendered); ranking + summary tables unchanged | Task 3 |
| §7.2 PDF `_executive_summary` payload parity | Task 3 (Step 1a + Step 3) |
| §7.3 PPTX three bullets + `_executive_summary` payload | Task 4 |
| §3/§10 net-revenue API additive fields | Task 1 (Step 5) |
| §4 semantic invariant pinned by a mixed test | Task 1 (Step 1) |
| §6 None→0 coalescing pinned | Task 1 (Step 1) |
| §8 reuse 30/100 values; 3 fixture constructor updates | Tasks 1–4 |
| §2 Docs/01 + Docs/15 status | Task 5 |

No gaps.

**2. Placeholder scan:** No TBD/TODO; every code step shows complete code; every run step shows the command + expected result. Task 5 Step 2 includes a fallback instruction in case the live `Remaining:` wording differs — this is a deliberate guard, not a placeholder (the concrete replacement text is given).

**3. Type consistency:** The two field names `total_channel_direct_deduction_amount_usd` and `total_account_allocated_deduction_amount_usd` (typed `Decimal`, no default) are used identically in the dataclass, `to_api`, the builder sum, all three report builders (gross-vs-net rendered + every `_executive_summary`/`_scope_breakdown` payload), and every fixture/assertion. Rendered string forms (`"30"`, `"100"`, `"130"`, `"0"`) match `decimal_to_api` normalization. XLSX column letters (Channel Breakdown J/K after I; Deductions E/F after D) are consistent with the verified current header order. The PDF `_summary_table` is deliberately left unchanged (cherry-picks keys); the PDF `_executive_summary` change is payload-only.
