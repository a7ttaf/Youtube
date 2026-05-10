# Confidence and Explainability

## Purpose
Make every number understandable and trustworthy.

## Confidence levels

| Code | Label | Meaning |
|---|---|---|
| A | Official | Direct from official YouTube/CMS/system report |
| B | Reconciled | Official gross + payment/deduction reconciliation |
| C | Allocated | Payment is real, but channel split uses allocation rule |
| D | Estimated | Based on estimated report, not finalized/locked |
| E | Missing | Required source missing or unresolved |

## Number explanation object

```json
{
  "metric": "net_revenue",
  "entity_type": "channel",
  "entity_id": "UCxxxx",
  "month": "2026-03",
  "value": 184250.00,
  "currency": "USD",
  "confidence": "B_RECONCILED",
  "formula": "gross + adjustments - tax - allocated_deductions",
  "components": [
    {"name": "gross_revenue", "value": 195000.00, "source": "youtube_report", "confidence": "A_OFFICIAL"},
    {"name": "tax", "value": -7200.00, "source": "tax_report", "confidence": "A_OFFICIAL"},
    {"name": "fx_transfer_allocation", "value": -3550.00, "source": "allocation_engine", "confidence": "B_RECONCILED"}
  ],
  "warnings": []
}
```

## UI rules

- Every money number has an **Explain** action.
- Every table row shows a confidence badge.
- Missing data appears as a visible warning, not hidden.
- Allocated values must be labeled as allocated.
- Locked values must show lock date and locking user.

## Smart alerts

```text
MISSING_REVENUE_SOURCE
PAYMENT_NOT_MATCHED
BANK_AMOUNT_MISSING
TAX_REPORT_MISSING
OUTSIDE_CMS_REVENUE_REQUIRED
UNEXPLAINED_GAP_HIGH
MONTH_NOT_LOCKED
MANUAL_OVERRIDE_USED
```

## Acceptance checks

- Clicking a number shows source, formula, confidence, and warnings.
- User can filter table by confidence level.
- User can export confidence notes with finance report.

## Foundation implementation note

The first backend explain-number endpoint supports channel-month `adjusted_gross_revenue_usd`. It derives the value from persisted revenue facts and approved manual overrides only, records a `number_explanations` snapshot, and audits the read as sensitive revenue access. Pending overrides appear as warnings and are not applied.
