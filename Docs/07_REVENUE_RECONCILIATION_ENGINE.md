# Revenue Reconciliation Engine

## Purpose
Calculate gross, deductions, net revenue, and payment gaps by channel, company, sector, and holding.

## Core formula

```text
channel_net_revenue =
    channel_gross_revenue
  + channel_adjustments
  - channel_tax_withholding
  - allocated_transfer_fee
  - allocated_fx_difference
  - allocated_unresolved_payment_gap
  + manual_adjustments
```

## Deduction formula

```text
deduction_amount = gross_revenue - net_revenue

deduction_percentage = deduction_amount / gross_revenue * 100
```

When `gross_revenue` is `0`, `deduction_amount` is still `gross_revenue - net_revenue`, but `deduction_percentage` must be returned as `0.0000` because the percentage is not computable from a zero denominator. Persist and return money values as fixed-precision decimals and round percentages to four fractional digits.

## Monthly reconciliation formula

```text
expected_payment =
    total_gross_revenue
  + total_adjustments
  - total_tax_withholding

payment_gap = expected_payment - adsense_payment_amount

bank_gap = adsense_payment_amount - bank_received_amount_normalized
```

## Allocation methods

### 1. Gross revenue proportional allocation

```text
channel_share = channel_gross_revenue / total_gross_revenue
channel_allocated_deduction = total_deduction * channel_share
```

### 2. Post-tax revenue proportional allocation

```text
channel_share = channel_post_tax_revenue / total_post_tax_revenue
channel_allocated_deduction = total_deduction * channel_share
```

### 3. Company-level allocation
Deduction is first assigned to company, then split across company channels.

### 4. Manual allocation
Finance manually assigns a deduction amount to a channel/company.

### 5. No allocation
Deduction remains at holding level.

## Monthly close states

```text
OPEN
ESTIMATED
FINALIZED
PAYMENT_MATCHED
BANK_CONFIRMED
RECONCILED
LOCKED
```

## Manual override rules

Every override requires:

```text
month
entity_type
entity_id
field_name
old_value
new_value
reason
created_by
approved_by (required when status == APPROVED)
created_at
```

Pending overrides do not require `approved_by`; approval identity and approval timestamp become mandatory only when the override status transitions to `APPROVED`.

## Outputs

```text
channel_net_revenue
company_net_revenue
sector_net_revenue
holding_net_revenue
monthly_reconciliation_summary
```

## Foundation implementation note

The backend now exposes a read-only net-revenue foundation for month summaries.
It calculates channel net revenue only when the selected primary SQL revenue
fact already includes official `net_revenue_usd`. Approved manual revenue
overrides are added to both adjusted gross and net revenue so the source
deduction amount remains traceable. Pending overrides lower confidence but are
not applied. If a primary source has no net value, the API returns
`NET_REVENUE_SOURCE_MISSING` instead of inventing tax, deductions, or allocated
bank/payment gaps.

This foundation does not yet persist `channel_net_revenue` rows, allocate
transfer/FX/payment gaps, ingest tax tables, or depend on a graph database.

Monthly revenue facts may include official Shorts, longform, and subscription
revenue component values when the source report provides them. These fields are
stored and exposed for analysis, issue review, and export context, but they do
not change gross/net calculations in this phase. Missing component fields remain
null and must not be backfilled from gross revenue.

The backend exposes `POST /revenue/recalculate` as both a dry-run preview and a
committed write endpoint. It accepts the finance month, allocation method, scoped
data selection, currency, and reason; requires revenue visibility and
allocation-rule permission for all callers. `allocation_method=manual` is
accepted for dry-run previews (`dry_run=true`) but rejected with HTTP 422 on
committed writes (`dry_run=false`); manual allocations require explicit lines and
must use the dedicated commit endpoint
(`POST /revenue/months/{month}/account-allocations/commit`). With `dry_run=true`
the endpoint returns source coverage and blockers only, tagged
`NO_WRITES_PERFORMED`, and never creates financial rows. With `dry_run=false` the
endpoint additionally requires `finance.view_finalized_payments` at the
`finance_month` scope, enforces `scope_type=global` and an `idempotency_key`,
then commits a versioned allocation snapshot (persisting committed allocation
rows). On a fresh commit (no existing run for the idempotency key) the
blocking-issues pre-flight gate runs and on success emits an `ALLOCATION_COMMITTED`
audit event, returning HTTP 201. On idempotent replay (same key and fingerprint)
the pre-flight is bypassed, no second `ALLOCATION_COMMITTED` audit event is
written (though `RECALCULATION_REQUESTED` is still recorded), and HTTP 200 is
returned. Pre-flight-blocked writes (409) are not audited with
`RECALCULATION_REQUESTED` because the HTTP 409 is raised before the audit call.
It must not invent transfer, FX, tax, or payment-gap values.

## Acceptance checks

- System can calculate net revenue for one month from existing official SQL
  revenue facts only.
- `POST /revenue/recalculate` with `dry_run=true` is a read-only preview: it
  returns source coverage and blockers tagged `NO_WRITES_PERFORMED`, audits
  `RECALCULATION_REQUESTED`, and never creates `channel_net_revenue`, allocation,
  transfer/FX, payment-gap, or tax rows.
- `POST /revenue/recalculate` with `dry_run=false` is a committed finance write:
  it additionally requires `finance.view_finalized_payments` at the `finance_month`
  scope, enforces `scope_type=global` and an `idempotency_key`; on a fresh commit
  (no existing run for the key) it runs a blocking-issues pre-flight, then persists
  a versioned committed allocation snapshot and emits an `ALLOCATION_COMMITTED`
  audit event, returning HTTP 201. On idempotent replay the pre-flight is bypassed,
  no second `ALLOCATION_COMMITTED` audit event is written (though
  `RECALCULATION_REQUESTED` is still recorded), and HTTP 200 is returned.
  `allocation_method=manual` is rejected with HTTP 422 on committed writes; manual
  allocations require explicit lines via the dedicated commit endpoint.
- Recalculation dry-run access requires revenue visibility and the
  allocation-rule management permission at the `finance_month` scope. The write path
  additionally requires the `finance.view_finalized_payments` permission at the
  `finance_month` scope.
- System can lock a month.
- Locked month does not change unless explicitly unlocked.
- Allocation of unknown transfer/FX gap, tax-table ingestion, and bank/payment
  gap allocation are deferred until the full allocation engine ships and are
  not part of this foundation's acceptance.
