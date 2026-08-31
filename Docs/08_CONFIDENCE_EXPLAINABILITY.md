# Confidence and Explainability

## Purpose
Make every number understandable and trustworthy.

## Confidence levels

| Code | Label | Meaning |
|---|---|---|
| A_OFFICIAL | Official | Direct from official YouTube/CMS/system report |
| B_RECONCILED | Reconciled | Official gross + payment/deduction reconciliation |
| C_ALLOCATED | Allocated | Payment is real, but channel split uses allocation rule |
| D_ESTIMATED | Estimated | Based on estimated report, not finalized/locked |
| E_MISSING | Missing | Required source missing or unresolved |

Legacy short labels map as `A -> A_OFFICIAL`, `B -> B_RECONCILED`, `C -> C_ALLOCATED`, `D -> D_ESTIMATED`, and `E -> E_MISSING`. New API payloads and stored examples should use the expanded wire tokens.

## Number explanation object

```json
{
  "metric": "adjusted_gross_revenue_usd",
  "entity_type": "channel",
  "entity_id": "UCxxxx",
  "month": "2026-03",
  "value": "184250.00",
  "currency": "USD",
  "confidence": {"label": "HIGH", "score": "0.95"},
  "formula": "baseline_gross_revenue_usd + approved_manual_override_total_usd",
  "components": [
    {"key": "baseline_gross_revenue_usd", "label": "Baseline gross revenue", "value": "184000.00", "source_kind": "YOUTUBE_CMS", "source_report_id": "rpt-001"},
    {"key": "approved_manual_override_total_usd", "label": "Approved manual overrides", "value": "250.00", "count": 1}
  ],
  "warnings": []
}
```

The explain endpoint serializes `confidence` as an object `{"label": ..., "score": ...}`,
where `label` is `HIGH`, `MEDIUM`, or `LOW`. The `A_OFFICIAL`…`E_MISSING` values in the
Confidence levels table are the domain-level tier tokens; the net-revenue API
(`GET /revenue/months/{month}/net-revenue`) and the month-level smart-alert summary
emit the bare tier code string (e.g. `B_RECONCILED`, `D_ESTIMATED`, `E_MISSING`),
whereas the explain endpoint reports the `{label, score}` object shown above.

For `adjusted_gross_revenue_usd`, the label bands the primary fact's
`confidence_score` at `>= 0.9000` (HIGH) and `>= 0.7000` (MEDIUM), and a warned
explanation can never be HIGH: when `warnings` is non-empty the score is capped
at `0.9000` **and** the label is capped at MEDIUM. The returned `label` is
authoritative for display; clients must not derive a replacement label from
`score`, because a warned score of `0.9000` intentionally carries MEDIUM. This
rule guarantees only `warnings => label != HIGH`: clean and warned explanations
can still share MEDIUM or LOW, so warning presence must be read from `warnings`.

## UI rules

- Every money number has an **Explain** action.
- Every table row shows a confidence badge.
- Missing data appears as a visible warning, not hidden.
- Allocated values must be labeled as allocated.
- Locked values must show lock date and locking user.

## Smart alerts

```text
MISSING_REVENUE_SOURCE
CHANNELS_MISSING_REVENUE_FACTS
PAYMENT_NOT_MATCHED
BANK_AMOUNT_MISSING
BANK_RECONCILIATION_NOT_CONFIRMED
UNEXPLAINED_GAP_HIGH
REVENUE_TREND_ANOMALY
MONTH_NOT_LOCKED
MANUAL_OVERRIDE_USED
```

## Acceptance checks

- Clicking a number shows source, formula, confidence, and warnings.
- User can filter table by confidence level.
- User can export confidence notes with finance report.

## Foundation implementation note

The first backend explain-number endpoint supports channel-month `adjusted_gross_revenue_usd`. It derives the value from persisted revenue facts and approved manual overrides only, records a `number_explanations` snapshot, and audits the read as sensitive revenue access. Pending overrides appear as warnings and are not applied.

The backend now also exposes a month-level smart-alert engine for the internal
finance command center. It derives alerts from stored SQL facts, official
AdSense payment metadata, finance-entered bank reconciliation receipt rows,
manual overrides, and finance month-close state. It does not invent missing
money values or calculate net revenue. Alert reads are permission-checked for
revenue, confidence, finalized payments, and bank reconciliation, then audited
as sensitive finance access.
