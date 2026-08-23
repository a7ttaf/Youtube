# Payment-gap narrative pass — month gap explanation (Hard Problem #3)

Ruled 2026-08-23: **new composed endpoint** + **Command Center panel**, one
full-stack PR (the #195 shape). Month-grain only — the PAYMENT-grain bridge is
repo-proven absent (spec 2026-06-03) and nothing here pretends otherwise.

## Goal

Docs/15 Hard Problem #3's named remainder: "a dedicated reconciling
explanation/narrative pass tying gaps to receipts/fees/currency effects."
Sibling constraint from Hard Problem #4: reconcile Google/AdSense reported
money, bank receipts, transfer fees, and bank-side currency effects **without
treating public FX rates as official revenue** (Docs/18 rules 1–4).

The substrate already computes both month gaps; nothing composes them into one
explained story, the payment leg has no provenance, and `fx_difference_usd`
reaches the Command Center and is dropped unrendered. This pass adds the
composition, not new arithmetic sources.

## The chain and its two legs

```
youtube_revenue_total_usd  ──(payment leg)──▶  adsense_paid_amount_usd  ──(bank leg)──▶  bank_received_amount_usd
        payment_gap_usd = youtube_total − adsense_paid          bank_gap_usd = adsense_paid − bank_received
```

Each leg is decomposed as `gap = Σ(evidence-backed components) + unexplained
residual`:

- **Payment leg** components: `non_paid_adsense_payments` — the summed
  `payment_amount` of this month's USD AdSense payment rows whose
  `payment_status != "PAID"` (PENDING/UNPAID; CANCELLED excluded — a cancelled
  payment is not money in flight). Evidence: the payment rows themselves.
  Residual: `payment_gap − non_paid_amount`.
- **Bank leg** components: `transfer_fee_usd` and `fx_difference_usd` — the
  month sums of the operator-entered bank-row evidence (signed FX). Residual:
  `bank_gap − transfer_fee − fx_difference`.

Non-USD payment rows are counted and warned about, never converted (no FX
conversion anywhere on this path — Docs/18 rule 2). CANCELLED rows are counted
in a warning when present, never summed into a component.

### Leg status (per leg) and month status

- `INCOMPLETE` — the leg's gap is `None` (an operand side is missing:
  `NO_YOUTUBE_REVENUE` / `MISSING_ADSENSE_PAYMENT` / `MISSING_BANK_RECEIPT`).
- `MATCHED` — |gap| ≤ tolerance (0.01, the shared default).
- `FULLY_EXPLAINED` — gap beyond tolerance, |residual| ≤ tolerance.
- `PARTIALLY_EXPLAINED` — gap beyond tolerance, components non-zero, residual
  beyond tolerance.
- `UNEXPLAINED` — gap beyond tolerance and no non-zero component.

Month `status` = the worst of the two leg statuses, ordered
`MATCHED < FULLY_EXPLAINED < PARTIALLY_EXPLAINED < UNEXPLAINED < INCOMPLETE`
(INCOMPLETE worst: finance cannot even see the whole chain).

### Confidence (explain wire shape, PR #69 vocabulary)

Every component and residual carries `{label, score}` with the established
constants (`explanations.py::_NET_CONFIDENCE_TO_EXPLAIN`):

| Case | label | score |
| --- | --- | --- |
| Evidence-backed component on a leg whose residual ≤ tolerance | HIGH | 0.95 |
| Evidence-backed component on a leg with residual beyond tolerance | MEDIUM | 0.80 |
| Any `unexplained_residual` beyond tolerance; any component on an INCOMPLETE leg | LOW | 0 |
| Component with ZERO evidence rows, on any leg (amended in-PR: "evidence-backed" is earned per component — the badge must never contradict the component's own MISSING_SOURCE provenance) | LOW | 0 |

Deterministic table, no scoring heuristics. Residual confidence is carried on
the wire as `unexplained_residual_confidence` on each leg (the review round
caught the draft JSON omitting what this prose already promised). The
PARTIALLY/UNEXPLAINED split checks whether ANY component is nonzero, not the
components' net sum — offsetting evidence (fee `+5`, FX `-5`) is still
evidence. `money_provenance` covers **every**
numeric field in the response (`{source, formula, confidence, export_value}`,
the bank-recon idiom extended to the payment leg — closes the flagged
provenance gap on payment-match numbers as *stated by this endpoint*; the
payment-match endpoint itself is unchanged).

### Prose

Deterministic narrative, no LLM — the `reconciliation_explanation.py` idiom:
one narrative string per leg plus one month summary line. 2dp money rendering;
signed values keep their sign in prose via `_signed_money` (`-$5.00` /
`$5.00`), so a negative FX difference or residual reads negative. (Amended
in-PR from the draft's `− $X FX` format note: the build renders components as
`"$X in <label> (N bank entries)"` with the sign carried by the amount
itself.) Prose helpers are module-local (short), with a comment pointing at
the reconciliation-explanation twin.

## Endpoint

`GET /revenue/months/{month}/gap-explanation?currency=USD`

- **Permissions** — the union of both source reads, mirroring the client-side
  composition the Command Center already does: `VIEW_REVENUE` @ global,
  `VIEW_FINALIZED_PAYMENTS` @ finance_month, `VIEW_BANK_RECONCILIATION` @
  finance_month.
- **Currency**: `normalize_payment_match_currency` (USD-only hard gate, same
  422 message as payment-match).
- **Audit**: `REVENUE_VIEWED` + `PAYMENT_VIEWED` + `BANK_RECONCILIATION_VIEWED`
  (it discloses all three surfaces' numbers — the smart-alerts triple-audit
  precedent), `entity_type="month_gap_explanation"`, `entity_id=month`. The
  three appends land inside ONE `AuditSink.transaction()` boundary — a late
  append failure retracts the accepted prefix on every tier, so no partial
  triple can describe a response that was never returned.
- **Response model**: the wire shape is validated by a Pydantic
  `MonthGapExplanationResponse` (field order = wire order), the
  smart-alerts idiom — serializer drift cannot reach clients as a 200.
- **Month close**: read-only path, no close guard (consistent with both source
  GETs); `close_status` (OPEN/LOCKED) included read-only, the smart-alerts
  precedent, so finance sees the lock context next to the story.
- **Service**: new module `backend/ums_smart_revenue/finance/gap_explanation.py`
  — a pure builder taking the two existing summaries plus the month's payment
  rows and close status. No new repository, no schema change, no migration.

## Wire shape (draft — the build must match this or amend this file in-PR)

```json
{
  "month": "2026-03",
  "currency": "USD",
  "close_status": "OPEN",
  "status": "PARTIALLY_EXPLAINED",
  "tolerance_usd": "0.01",
  "payment_leg": {
    "status": "FULLY_EXPLAINED",
    "youtube_revenue_total_usd": "930",
    "adsense_paid_amount_usd": "900",
    "payment_gap_usd": "30",
    "payment_match_status": "PAYMENT_VARIANCE",
    "components": [
      {"key": "non_paid_adsense_payments", "label": "AdSense payments not yet PAID",
       "amount_usd": "30", "evidence_count": 1,
       "confidence": {"label": "HIGH", "score": "0.95"}}
    ],
    "unexplained_residual_usd": "0",
    "unexplained_residual_confidence": {"label": "HIGH", "score": "0.95"},
    "narrative": "..."
  },
  "bank_leg": {
    "status": "PARTIALLY_EXPLAINED",
    "adsense_paid_amount_usd": "900",
    "bank_received_amount_usd": "880",
    "bank_gap_usd": "20",
    "bank_reconciliation_status": "BANK_VARIANCE",
    "components": [
      {"key": "transfer_fee", "label": "Bank transfer fees",
       "amount_usd": "12", "evidence_count": 2,
       "confidence": {"label": "MEDIUM", "score": "0.80"}},
      {"key": "fx_difference", "label": "Bank-side FX difference",
       "amount_usd": "5", "evidence_count": 2,
       "confidence": {"label": "MEDIUM", "score": "0.80"}}
    ],
    "unexplained_residual_usd": "3",
    "unexplained_residual_confidence": {"label": "LOW", "score": "0"},
    "narrative": "..."
  },
  "warnings": [{"code": "...", "message": "..."}],
  "money_provenance": {"<field>": {"source": "...", "formula": "...",
                       "confidence": "...", "export_value": "..."}},
  "narrative": "...",
  "audit_events": [...]
}
```

`warnings` codes (existing vocabulary where it exists):
`UNSUPPORTED_PAYMENT_CURRENCY` (count attached), `CANCELLED_ADSENSE_PAYMENTS`
(new, count attached), and `CHANNELS_WITHOUT_YOUTUBE_SOURCE` (channels whose
only facts are non-YouTube). (Amended in-PR from the draft's
"leg-incompleteness mirrors" idea: an incomplete leg is already first-class
state — leg `status: INCOMPLETE`, the leg's `*_status` source field, and the
incompleteness narrative — so mirroring it as a warning would state the same
fact in a third place and create a de-sync surface; warnings stay reserved
for row-level data caveats the leg fields do not carry.)
`evidence_count` = number of source rows behind the component (payment rows /
bank entries).

## Frontend (same PR)

Command Center panel "Gap narrative" adjacent to the #127 bank cards
(`CommandView.tsx`): the two legs as compact rows (operands → gap), component
rows with confidence badges (`confidenceDisplay`), residual row, and the month
narrative line. This finally renders `fx_difference_usd`. New hook
`useGapExplanation` (the `useBankReconciliation` shape), types in
`types.ts`, permission-gated client-side by the same three-way composition —
whose revenue term is the new GLOBAL-scope session capability
`canViewRevenueGlobal` (`_can(VIEW_REVENUE)` in `session.py`), not the
scope-aware `canViewRevenue` hint: the endpoint gates VIEW_REVENUE @ global,
and a company/sector/channel-scoped revenue viewer must see the restricted
band, not fire a guaranteed-403 fetch. Tests mirror the CommandView test
idioms.

## Docs deltas (same PR)

- Docs/12: the new endpoint section (route list + contract paragraph incl. the
  no-FX-conversion and month-grain-only statements).
- Docs/15: Hard Problem #3 → shipped-with-residuals wording; #4 → evidence
  surfaced, "finance adoption" remainder stays. (Rebase-sensitive: Mahmoud has
  an out-of-PC Docs/15 fix inbound — fetch+rebase before push, merge by hand
  on conflict.)
- Docs/01: delivery entry, no-migration disposition.
- Docs/07 corrections (2026-06-16 docs-correctness precedent — code is
  authoritative): the `expected_payment`/`payment_gap` formula matches
  the shipped bare `youtube_total − adsense_paid` (no adjustments/withholding
  term exists); close states are OPEN|LOCKED; the net formula naming must not
  imply TRANSFER_FEE/FX_VARIANCE/UNRESOLVED_PAYMENT_GAP reduce net
  (deduction_policy.py:31).

## Out of scope (named, deliberate)

- PAYMENT-grain anything (blocked; receipt→account assertion model is its own
  future spec).
- Persistence of the explanation (compute-on-read; close-time snapshotting is
  a future decision).
- Non-USD matching or any FX conversion.
- Changing payment-match / bank-reconciliation payloads (both untouched).
- Unifying the three confidence vocabularies (flagged, separate concern).
- The Track F per-channel narrative's UI reachability (separate concern).

## Test plan (backend)

Pure-builder tests (no DB): both legs' five statuses; residual arithmetic incl.
signed FX; CANCELLED exclusion + warning; non-USD count + warning; tolerance
edges (exactly 0.01); month-status worst-of ordering; confidence table;
provenance completeness (every numeric field has an entry — walked
programmatically); narrative determinism. API tests (sqlite tier): permission
denials (each of the three gates), currency 422, audit triple, close_status
passthrough, response-shape golden. PG tier: none needed (no new SQL; both
source reads are already PG-proven) — state this in the PR body.
