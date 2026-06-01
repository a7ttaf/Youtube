# Spec 2b — Export Deduction Breakdown Columns (Design)

**Status:** Approved for planning — 2026-06-02
**Branch:** `spec/export-deduction-breakdown` (off `main` `3d1916c` = Spec 2b PR-3 merged, #60)
**Phase:** 4, Spec 2b (allocation engine), the read-surface follow-up after PR-1 (compute+read), PR-2 (net-revenue integration), PR-3 (net-revenue explanation).

---

## 1. Goal

Surface the per-channel **channel-direct vs account-allocated deduction split** — already computed by the PR-2 net builder — across all three finance export artifacts (XLSX workbook, executive PDF, branded PPTX slide pack), plus the matching month-level aggregate.

Today every export renders only a single collapsed `deduction_amount_usd` (per channel) and `total_deduction_amount_usd` (per month). The breakdown exists on `ChannelNetRevenueSummary` but is never rendered.

## 2. Scope and non-goals

**In scope (read/surface only):**
- Two additive month-level aggregate fields on `MonthNetRevenueSummary`, summed in the finance layer.
- Per-channel breakdown columns in the XLSX Channel Breakdown sheet and Deductions sheet.
- Month-level aggregate rows in the XLSX Executive Summary, the PDF gross-vs-net table, and the PPTX deduction slide.
- Finance-workbook preview JSON parity (preview advertises the same new fields the artifact renders).
- Tests for all of the above.
- `Docs/01_IMPLEMENTATION_PLAN.md` + `Docs/15_DELIVERY_BACKLOG.md` status updates (per-PR convention).

**Explicitly NOT in scope (no behavior change):**
- No change to deduction allocation math (`build_channel_net_revenue_summary` computation, the component-derived path, the allocation engine).
- No `DeductionComponent` / account-map model or migration. **No migration at all.**
- No authorization/permission change — the export routes keep their existing `VIEW_FINALIZED_PAYMENTS` finance-artifact gate.
- No audit change — the artifact export is already `PAYMENT_VIEWED`-audited; the breakdown is a rendering detail (recorded here as a conscious non-change; see §9).
- No PDF **per-channel** ranking columns (decision §7) — only PDF month-level aggregate rows.
- No re-filtering in builders — they consume the already scope-filtered channel list handed in by `api/exports.py` (`filter_account_allocations_to_scope`).

## 3. Verified current state (anchors)

- `ChannelNetRevenueSummary` (`backend/ums_smart_revenue/finance/net_revenue.py:24-44`) already carries, as sibling dataclass attributes:
  - `deduction_amount_usd: Decimal | None` (`:37`) — the collapsed total,
  - `channel_direct_deduction_amount_usd: Decimal | None` (`:38`),
  - `account_allocated_deduction_amount_usd: Decimal | None` (`:39`).
  Its `to_api()` already serializes all three (`:70-75`).
- `MonthNetRevenueSummary` (`net_revenue.py:84-100`) exposes only `total_deduction_amount_usd: Decimal` (`:96`), summed at the `total_deduction_amount_usd=sum((channel.deduction_amount_usd for channel in calculated), Decimal("0"))` assignment inside `build_month_net_revenue_summary` (~`:691-693`). Its `to_api()` is `:101-122`. There is **no** month-level breakdown field.
- The `calculated` subset (channels with `net_revenue_usd is not None`) is defined ~`net_revenue.py:556-558`.
- `decimal_to_api` (`backend/ums_smart_revenue/finance/decimal_formatting.py:25-32`) returns `None` for `None` and a normalized no-exponent string otherwise.
- Export builders read channel **dataclass attributes directly** (not `.to_api()` dicts):
  - XLSX `finance_workbook.py`: Channel Breakdown sheet (`:200-235`, channels at `:233`, total cell at `:227`), Deductions sheet (`:236-257`, channels at `:255`, total cell at `:251`), Executive Summary (`:335-371`, total at `:363-365`), preview manifest (`:394-395`).
  - PDF `executive_pdf.py`: gross-vs-net table (`:277-293`, total row `:287-289`), channel-ranking table (`:313-335`, per-channel deduction `:321`), exec-summary payload (`:252-253`).
  - PPTX `branded_slide_pack.py`: deduction slide (`:229-238`, bullet `:233-234`), manifest payload (`:468-469`).
- Net-revenue API endpoint (`backend/ums_smart_revenue/api/revenue.py`, handler ~`:1069`) builds the summary (`:1145`) and returns `summary.to_api()` (`:1166`). This is the **only** `MonthNetRevenueSummary.to_api()` consumer — `:1295` is bank-reconciliation and `:1657` is adjusted-revenue (different objects).
- Export route auth + audit: `api/exports.py` emits `PAYMENT_VIEWED` on finance-artifact export (`~:1137`); scope filtering via `filter_account_allocations_to_scope` (`~:1096-1098`).

## 4. The critical semantic invariant

On the **component-derived** path (`_component_derived_channel_summary`, `net_revenue.py:216-252`):
- `component_total = channel_direct_total + account_allocated_total` (`:229`),
- `deduction_amount_usd = component_total` (`:241`),
- `channel_direct_deduction_amount_usd = channel_direct_total` (`:247`), `account_allocated_deduction_amount_usd = account_allocated_total` (`:248`).

So **per-channel, on the component-derived path: `deduction_amount_usd == channel_direct + account_allocated` exactly.**

On the **source-net / calculated** path, `deduction_amount_usd` is the source-reported (or override-derived) deduction and **both breakdown fields are `None`**.

Therefore, at the **month level**:

```
total_channel_direct_deduction_amount_usd + total_account_allocated_deduction_amount_usd
    == Σ (component-derived component_totals)
    ≤  total_deduction_amount_usd
```

with equality **only** when every in-scope deduction is component-derived. The two new aggregates capture the **component-derived split**, not the grand total; the difference `total − direct − allocated` is the unsplit source-reported remainder.

**Consequence for rendering:** exports render **all three** numbers (total + direct + allocated), never "replace total with the split". Showing all three keeps the unsplit remainder transparent and avoids a misleading "these don't add up" reading.

## 5. Finance-layer change

Add two fields to `MonthNetRevenueSummary`, inserted immediately after `total_deduction_amount_usd` (keeping the deduction fields grouped, ahead of the `unallocated_*` fields and `channels`):

```python
total_channel_direct_deduction_amount_usd: Decimal
total_account_allocated_deduction_amount_usd: Decimal
```

- Typed `Decimal` (never `None`), **no default value** — matching the existing fields (none have defaults) and forcing every constructor to pass the value explicitly. A missed construction site fails loud at construction (`TypeError`), never silently renders 0.
- Computed in `build_month_net_revenue_summary` alongside the existing total, over the same `calculated` subset, **coalescing `None`→`Decimal("0")`** (breakdown fields are `None` on source-net channels):

```python
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
```

- Serialized additively in `MonthNetRevenueSummary.to_api()` via `decimal_to_api` (always a string, never `None`).

## 6. Rendering decisions

| Decision | Resolution |
|---|---|
| Replace total vs keep all three | **Keep all three** (total + direct + allocated) on every surface (see §4). |
| Per-channel `None` cells | Mirror `decimal_to_api(None) → None` (blank cell), consistent with the existing `deduction_amount_usd` cell. No special-casing. |
| Aggregate `None` | Never `None` — coalesced to `Decimal("0")` in the sum (§5). |
| Column naming | XLSX uses the full API field names (`channel_direct_deduction_amount_usd`, `account_allocated_deduction_amount_usd`), matching its existing `approved_manual_override_total_usd` convention. PDF/PPTX use concise human labels ("Channel-Direct Deduction USD", "Account-Allocated Deduction USD"), matching their existing label style. |
| XLSX column placement | The two new per-channel columns go immediately **after** `deduction_amount_usd`. `net_revenue_usd` precedes it (asserted at column H in `test_finance_workbook_preview.py:101-102`), so column H is unaffected; downstream columns shift deliberately and their assertions are updated. |
| Preview parity | The finance-workbook preview JSON (sheet column manifests + executive-summary payload) gains the new fields so the preview matches the downloaded artifact. |
| Audit | Unchanged (§9). |

## 7. Per-surface changes

### 7.1 XLSX — `reports/finance_workbook.py`
- **Channel Breakdown sheet** (`:200-235`): add `channel_direct_deduction_amount_usd` and `account_allocated_deduction_amount_usd` columns immediately after the existing `deduction_amount_usd` column; per-row values read directly off the channel dataclass via `_decimal_to_api`.
- **Deductions sheet** (`:236-257`): add the same two columns alongside the retained `deduction_amount_usd`.
- **Executive Summary** (`:335-371`): add `total_channel_direct_deduction_amount_usd` and `total_account_allocated_deduction_amount_usd` key-value rows from the new month aggregates, next to the retained `total_deduction_amount_usd`.
- **Preview manifest** (`:394-395`): include the new fields so preview advertises what the artifact renders.

### 7.2 PDF — `reports/executive_pdf.py`
- **Gross-vs-net table** (`:277-293`): add two month-aggregate rows — "Channel-Direct Deduction USD" and "Account-Allocated Deduction USD" — from the new aggregates, next to the retained "Total Deduction Amount USD".
- **Channel-ranking table** (`:313-335`): **unchanged** — stays 4 columns [Channel, Net Revenue USD, Deduction USD, Confidence]. No per-channel split (avoids ReportLab 6-column width/legibility risk; the per-channel detail lives in the XLSX).
- Optionally extend the deductions-explanation text to mention the split (no per-channel table).

### 7.3 PPTX — `reports/branded_slide_pack.py`
- **Deduction slide** (`:229-238`): expand the single "Total deduction amount USD" bullet into three bullets — Total / Channel-direct / Account-allocated — from the month aggregates. Bullets, not a table (no table precedent in this module — YAGNI).
- Manifest payload (`:468-469`): include the new aggregate fields for parity.

## 8. Testing

Reuse the existing fixtures that already seed a real non-zero split — `tests/api/test_exports_account_allocation.py` (channel-direct `30.00`, account-allocated `100.000000`, `:181-200`) and `tests/finance/test_net_revenue_account_allocations.py` (`:137`, asserts at `:198-199`).

New / updated coverage:
- **Finance aggregate** (`tests/finance/test_net_revenue_account_allocations.py` or `test_net_revenue.py`): assert the two new month totals sum the per-channel breakdown over the `calculated` subset, with `None`→0 coalescing; include a **mixed** scenario (one component-derived channel + one source-net channel with a deduction) that pins `total_deduction_amount_usd > total_channel_direct + total_account_allocated` so the §4 invariant is locked.
- **XLSX** (`tests/reports/test_finance_workbook_preview.py`): fixture updated to non-`None` breakdown values; assert the new per-channel columns on both sheets and the two new Executive Summary rows; update the shifted cell-position assertions deliberately.
- **PDF** (`tests/reports/test_executive_pdf.py`): assert the two aggregate rows appear in extracted text. **Do not** assert per-channel split columns (ranking table unchanged).
- **PPTX** (`tests/reports/test_branded_slide_pack.py`): assert the three deduction bullets (total + both splits) and their values.
- **Preview parity** (`tests/api/test_export_preview_api.py`): assert the preview JSON advertises the new fields.
- **Net-revenue API** (`tests/api/test_net_revenue_api.py`): assert the month endpoint response gains the two additive fields.
- **Constructor updates:** every site that *constructs* `MonthNetRevenueSummary(...)` directly — the production builder (`build_month_net_revenue_summary`) plus the test fixtures that build it — gains the two new required fields. Candidate files (to confirm during planning by grepping for `MonthNetRevenueSummary(`, since some only assert on the serialized dict rather than construct it): `test_net_revenue_account_allocations.py`, `test_executive_pdf.py`, `test_finance_workbook_preview.py`, `test_branded_slide_pack.py`, `test_net_revenue.py`, `test_net_revenue_api.py`, `test_export_preview_api.py`, `test_net_revenue_deduction_components.py`.

## 9. Audit (conscious non-change)

`_record_finance_export_artifact_audit` (`api/exports.py ~:1137-1211`) already emits `PAYMENT_VIEWED` for finance-artifact exports. The deduction breakdown is an additional rendering detail inside an already-audited artifact; it does not change *what* is disclosed at the access-control grain (the finance month's deduction data is already covered). The audit detail payload is therefore left unchanged. (Recorded explicitly so the omission is a decision, not an oversight.)

## 10. Blast radius

- **Tables/ORM:** none. No model, no migration.
- **PostgreSQL source of truth:** unchanged.
- **Neo4j / graph projection:** No graph projection impact detected — this touches the finance summary dataclass shape and report/export rendering only.
- **Authorization / audit:** unchanged (export `VIEW_FINALIZED_PAYMENTS` gate + `PAYMENT_VIEWED` audit preserved).
- **Finance results / month locks / overrides / payment matching:** unchanged — the aggregates are a pure sum of already-computed per-channel values; no allocation math changes.
- **API contract:** the month net-revenue endpoint (`revenue.py:1166`) response gains two additive, backward-compatible fields; no field is renamed or removed. The finance-workbook preview JSON gains additive fields. The XLSX/PDF/PPTX artifacts gain columns/rows/bullets.

Statement: **No graph projection impact detected.**

## 11. Validation

Targeted first (note: this repo uses `python -m pytest`, not bare `pytest`):

```
python -m pytest tests/finance/test_net_revenue_account_allocations.py \
  tests/api/test_exports_account_allocation.py \
  tests/reports/test_finance_workbook_preview.py \
  tests/reports/test_executive_pdf.py \
  tests/reports/test_branded_slide_pack.py \
  tests/api/test_export_preview_api.py \
  tests/api/test_net_revenue_api.py -q
```

Then baseline:

```
python -m ruff check backend tests scripts
python -m pytest -q          # full suite; PG-tier tests need UMS_TEST_DATABASE_URL -> disposable ums-mig-pg-test container
git diff --check
```

## 12. Risks

- **XLSX cell-position shift:** inserting columns shifts fixed cell references; the affected assertions (`test_finance_workbook_preview.py`) must be updated deliberately. `net_revenue_usd` (column H) is intentionally upstream of the insertion point and is unaffected.
- **Fixture churn / fail-loud:** the no-default fields force every `MonthNetRevenueSummary` constructor to be updated; a missed site is a construction-time `TypeError` caught immediately (acceptable — fails loud, never silent 0).
- **None-vs-zero readability (per-channel):** source-net channels render blank breakdown cells (by design, N/A). Tests exercise the component-derived path so the populated split is genuinely asserted, not just blanks.
- **Invariant confusion:** `total ≠ direct + allocated` at the month level is intended (§4). Rendering all three keeps it transparent; the mixed-scenario test documents it.
- **Scope leak:** builders must render only the already scope-filtered `channels` list from `api/exports.py`; they must not re-aggregate global allocations. The aggregate is summed in the finance layer over that same in-scope list.
