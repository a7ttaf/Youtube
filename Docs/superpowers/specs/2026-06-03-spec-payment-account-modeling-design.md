# PAYMENT→Account Modeling — Verification & Design Recommendation (Phase 4 Spec 2b)

**Status:** Verification/design only — **no schema, API, migration, or code in this PR** · **Date:** 2026-06-03 · **Branch:** `spec/payment-account-modeling` (off `main` `c6de90e`)

## 1. Purpose

PAYMENT-grain deduction evidence (bank-settlement TRANSFER_FEE / FX_VARIANCE) cannot be account-allocated today. This document is the **verification-first deliverable**: it proves, against the current repo, whether the source data can deterministically map a PAYMENT-grain component to an AdSense account, and — finding it cannot — records the blocker and recommends the future modeling approach. **It builds nothing.** Direct PAYMENT allocation stays deferred until the bridge below exists and real data confirms its shape.

## 2. Verdict (repo-proven): no deterministic `bank_reference → source_account_id` bridge

Confirmed by reading current `main` (`c6de90e`):

- **Bank entries carry no account identity.** `BankReconciliationEntryORM` (`backend/ums_smart_revenue/db/finance_models.py:316-408`) has only `bank_reference`, dates, amounts (native + USD), `transfer_fee_usd`, `fx_difference_usd`, `notes`, `source_report_id`, `recorded_by`, `tenant_id`. No `source_account_id`, no payee/counterparty/memo-structured field. Its uniqueness key is `(tenant_id, month, bank_reference)` (`:370-375`).
- **AdSense payments carry no bank reference.** `AdSensePaymentORM` (`:411-501`) has `source_account_id` (`:423`) but no `bank_reference` / settlement-reference column; its key is `(tenant_id, source_account_id, month, payment_name)`.
- **PAYMENT components are bank-reference-scoped only.** `_bank_component` (`backend/ums_smart_revenue/finance/deduction_components.py:169-191`) creates them as `scope_kind="PAYMENT"`, `scope_id=entry.bank_reference`, `raw_payload={"bank_reference", "kind"}`, `source_id=entry.id`. No account-identifying field is present or derivable.
- **Reconciliation is aggregate / month-level.** `build_month_bank_reconciliation_summary` (`backend/ums_smart_revenue/finance/bank_reconciliation.py:341-458`) and `build_monthly_payment_match_summary` (`backend/ums_smart_revenue/finance/payment_matching.py:78-200`) compare month-level **sums** (total PAID payments vs total bank receipts / YouTube revenue). There is **no per-line** payment↔bank match, so they establish no `bank_reference ↔ source_account_id` correspondence.
- **No resolver exists.** A repo-wide search found no `bank_reference → account/channel` lookup, mapping, or resolver.
- **Allocation only consumes ACCOUNT scope.** `list_account_components` (`backend/ums_smart_revenue/finance/deduction_ingestion.py:413-450`) deliberately filters to `scope_kind == "ACCOUNT"`; PAYMENT/CHANNEL rows are never fed to `compute_month_account_allocation` (`backend/ums_smart_revenue/finance/allocation_inputs.py:38-68`).

**Conclusion:** the first hop (`bank_reference → account`) does not exist in the data and cannot be derived. It requires an explicit modeling layer.

## 3. Reusable second hop (already solved)

Given a `source_account_id`, resolving to verified channels and distributing an amount is already implemented and reusable as-is:

- `SqlAlchemyChannelAccountLinkRepository.list_verified_adsense_account_channels(*, tenant_id, month, adsense_account_id)` (`backend/ums_smart_revenue/finance/channel_account_links.py:680-723`) joins VERIFIED account↔owner links to active owner↔channel links, month- and tenant-scoped, returning distinct channels (empty when unmapped/unverified).
- `gross_revenue_proportional` distribution (`backend/ums_smart_revenue/finance/allocation.py`, via `_proportional_allocation`) splits an amount across those channels by source-aligned gross with exact Decimal conservation.

So **only the first hop is missing**; once an account (or accounts) is known for a receipt, the rest of the path is already built.

## 4. Cardinality is UNVERIFIED

It is **not** established whether one bank receipt (one `(tenant_id, month, bank_reference)`) settles exactly one AdSense account (1:1) or bundles several (1:N). The repo cannot answer this — bank entries hold no account linkage, and reconciliation is aggregate. Confirming the real shape requires live AdSense remittance + bank data (the B2 connector, still remaining per Docs/15). **The future model must therefore tolerate both 1:1 and 1:N** until real remittance/bank evidence proves the actual shape; no cardinality assumption is baked in.

## 5. Recommended future model (NOT built in this PR)

A **shares-capable, operator-asserted receipt → account(s) assertion**, analogous to the Spec 2a channel↔account map:

- **Identity:** keyed by **`(tenant_id, month, bank_reference)` → account(s)** — never bare `bank_reference`, because `bank_reference` is unique only within a tenant-month (per the `BankReconciliationEntryORM` uniqueness key). Each assertion maps a specific tenant-month receipt to one or more `source_account_id`s, each with an optional **share** (defaulting to a single 100% share for the 1:1 case; explicit shares for the 1:N bundled case).
- **Governance:** versioned, audited, with a propose/verify/reject lifecycle + a read contract — mirroring the Spec 2a `AdsenseContentOwnerLink` / `ContentOwnerChannelLink` substrate (operator-asserted, fail-closed when unmapped/unverified).
- **Consumption (a later chunk, not now):** PAYMENT allocation would (1) split a receipt's PAYMENT-grain cost across its asserted accounts by share, then (2) reuse the §3 second hop (account→verified channels + `gross_revenue_proportional`) — and fail-closed to UNALLOCATED for any receipt without a VERIFIED assertion, exactly as ACCOUNT allocation does for unmapped accounts.

**Why not build it now:** the receipt cardinality and the source remittance payload shape are unverified, and operators cannot populate the assertions without real remittance data. Building the table/API now would be speculative; it is deferred until the prerequisites in §6 are met.

## 6. Prerequisites before building

1. **Live AdSense remittance + bank evidence** (the B2 connector) to (a) confirm the real receipt↔account cardinality and (b) give operators the data to assert mappings.
2. A confirmation pass on whether the live remittance payload carries any settlement↔account signal that ingestion could capture directly (which would reduce or remove the operator-assertion burden) — today's `AdSensePaymentORM.raw_payload` is opaque test data; this can only be checked against real connector output.

## 7. Separately: "other allocation methods" is its own substantial chunk (not a quick pivot)

Recorded so it is not mistaken for a small alternative: committed allocation is **hard-gated to `gross_revenue_proportional`** across layers — the API/service path and a DB CHECK + tests enforce it (`backend/ums_smart_revenue/finance/committed_allocation.py:141`; the `allocation_method = 'gross_revenue_proportional'` CHECK on `committed_allocation_runs`), and `POST /revenue/recalculate` is dry-run-only and **rejects committed writes** for alternate methods. Supporting additional methods (the dry-run engine already recognizes `company_level` / `manual` / `post_tax_revenue_proportional`) requires un-gating the commit path across API/service/DB/tests plus a recalculate→commit path — a substantial chunk of its own, not a pivot from this one.

## 8. Documentation updates in this PR

- `Docs/15_DELIVERY_BACKLOG.md` and `Docs/01_IMPLEMENTATION_PLAN.md`: mark PAYMENT-grain allocation **blocked — pending real remittance/bank evidence + an operator-asserted `(tenant_id, month, bank_reference) → account(s)` receipt assertion model** (this doc), and note that "other allocation methods" is a substantial separate chunk per §7.

## 9. Non-goals

No new table, ORM model, migration, API route, service, or allocation code. No change to existing readers, the commit/write path, auth, or `/revenue/recalculate`. No PAYMENT allocation. PostgreSQL remains the source of truth; no Neo4j/graph impact. This PR is documentation only.
