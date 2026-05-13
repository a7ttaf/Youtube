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
transfer/FX/payment gaps, ingest tax tables, or use Neo4j as a financial source
of truth.

## Acceptance checks

- System can calculate net revenue for one month.
- System can explain the difference between gross revenue and payment.
- System can allocate unknown transfer/FX gap.
- System can lock a month.
- Locked month does not change unless explicitly unlocked.
