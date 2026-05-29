# AdSense Payment Paid/Unpaid Status Breakdown — Design Spec

**Status:** Approved by operator (Mahmoud) on 2026-05-29. Not yet implemented.

**Goal:** Give finance a per-month, per-currency, per-account breakdown of AdSense
payment *settlement status* — how much is `PAID`, how much is still outstanding
(`PENDING` + `UNPAID`), and how much was `CANCELLED` — reading the existing
`adsense_payments` PostgreSQL source-of-truth, with **no FX conversion**, **no
schema change**, and **no new persistence**.

**Architecture:** A new pure aggregation module `finance/payment_status.py`
(frozen dataclasses + `.to_api()` + a `build_monthly_payment_status_summary`
builder, no DB access) plus a thin read endpoint
`GET /adsense/payments/status?month=YYYY-MM` on the existing AdSense router. The
endpoint resolves the existing `SqlAlchemyAdSensePaymentRepository`, enforces
`VIEW_FINALIZED_PAYMENTS` on `finance_month(month)`, calls the existing
`list_month_payments(month)`, runs the pure builder, records one reused
`PAYMENT_VIEWED` audit event, and returns the summary. PostgreSQL stays the
source of truth; the path is strictly read-only.

**Tech stack:** Python 3.x, FastAPI (existing surfaces only), SQLAlchemy 2.x
(read-only, no model/migration change), PostgreSQL. No new dependencies.

---

## 1. Problem statement and current state

The Phase 3 plan/backlog lines "Payment month matcher — not started" and
"Payment-vs-YouTube comparison — not driven" are **stale**. Verified state
(read firsthand, 2026-05-29):

- **The month-total YouTube↔AdSense matcher already ships.**
  `finance/payment_matching.py:62` `build_monthly_payment_match_summary` compares
  the month's YouTube `gross_revenue_usd` against PAID, USD AdSense
  `payment_amount` and returns `PAYMENT_MATCHED`/`PAYMENT_VARIANCE`. It is wired
  to `GET /revenue/months/{month}/payment-match` (`api/revenue.py:579`) and
  tested (`tests/finance/test_payment_matching.py`,
  `tests/api/test_payment_match_api.py`). The Phase 3 acceptance gate is met.
- **No per-status breakdown exists.** Both `payment_matching.py` and
  `bank_reconciliation.py` only compute a binary PAID-vs-non-PAID split, and
  only over USD. Nothing reports separate `PENDING` / `UNPAID` / `CANCELLED`
  counts or amounts, and nothing reports outstanding (owed-but-unsettled) money.
  `smart_alerts.py` has only an advisory `PAYMENT_NOT_MATCHED` code.
- **The status domain is fixed and validated on write.**
  `finance/adsense_payments.py:19`
  `ALLOWED_PAYMENT_STATUSES = {"PAID", "PENDING", "UNPAID", "CANCELLED"}`.
- **Payments are stored in their reported ISO currency** (`_normalize_currency`,
  any `^[A-Z]{3}$`). Only the matcher forces USD. A status view must therefore
  handle multiple currencies without dropping non-USD money.
- **The read surface already exists.** `SqlAlchemyAdSensePaymentRepository`
  exposes `list_month_payments(month=...) -> list[AdSensePaymentEntry]`
  (`finance/adsense_payments.py:233`), tenant-scoped and total-ordered. No
  per-status or per-account aggregate exists yet.

"Paid/unpaid status pass" therefore means: a new **read-only aggregation** over
the already-persisted payments that answers *"which settlements are paid vs still
outstanding, in which currency, for which account, this month."* It complements
(does not modify) the existing matcher.

---

## 2. Scope and non-goals

**In scope:**
- New pure module `backend/ums_smart_revenue/finance/payment_status.py`.
- New thin endpoint `GET /adsense/payments/status?month=YYYY-MM` in
  `backend/ums_smart_revenue/api/adsense.py`.
- Per-month status×currency rollup **plus** a per-`source_account_id` breakdown.
- `outstanding = PENDING + UNPAID` per currency.
- `CANCELLED` reported in the status breakdown (for evidence), excluded from
  `outstanding`.
- Stale Phase 3 tracker corrections in `Docs/01_IMPLEMENTATION_PLAN.md` and
  `Docs/15_DELIVERY_BACKLOG.md`.

**Non-goals (explicitly out):**
- **No FX / currency conversion.** Per `Docs/18`, public/provider FX is not an
  official finance source. Amounts are grouped by reported currency, never
  converted or summed across currencies.
- **No persistence / snapshot table / status row.** The breakdown is computed
  on read, like the matcher.
- **No month-close gating.** A `PENDING`/`UNPAID` balance does not block
  `lock_month`. (Operator de-scoped month-close integration.)
- **No per-payment list duplication.** Row-level drill-down stays on the
  existing `GET /adsense/payments`.
- **No channel↔AdSense-account revenue link / per-account *matching*.** This is
  a payment-only status view; revenue is not read here.
- **No new `AuditEventType`, no new `Permission`, no ORM/Alembic change.**

---

## 3. Module design — `finance/payment_status.py` (pure, no DB)

Mirrors the established `payment_matching.py` / `bank_reconciliation.py` pattern:
frozen dataclasses with `.to_api()`, a pure `build_*` function, module-local
Decimal serializer.

```python
CANONICAL_PAYMENT_STATUSES: tuple[str, ...] = ("PAID", "PENDING", "UNPAID", "CANCELLED")
OUTSTANDING_STATUSES: frozenset[str] = frozenset({"PENDING", "UNPAID"})


@dataclass(frozen=True)
class CurrencyAmount:
    currency: str          # 3-letter ISO, as stored
    amount: Decimal        # exact sum, no FX, no rounding beyond display trim
    def to_api(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class PaymentStatusBucket:
    status: str                          # one of CANONICAL_PAYMENT_STATUSES
    count: int
    currency_totals: list[CurrencyAmount]  # alphabetical by currency
    def to_api(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class AccountPaymentStatus:
    source_account_id: str
    total_payment_count: int
    status_totals: list[PaymentStatusBucket]   # only statuses this account has
    outstanding_totals: list[CurrencyAmount]    # PENDING+UNPAID, alphabetical
    def to_api(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class MonthlyPaymentStatusSummary:
    month: str
    total_payment_count: int
    status_totals: list[PaymentStatusBucket]    # ALL 4 canonical statuses, always
    outstanding_totals: list[CurrencyAmount]     # PENDING+UNPAID, alphabetical
    accounts: list[AccountPaymentStatus]         # by source_account_id ascending
    def to_api(self) -> dict[str, object]: ...


def build_monthly_payment_status_summary(
    *,
    month: str,
    payments: Iterable[AdSensePaymentEntry],
) -> MonthlyPaymentStatusSummary: ...
```

**Builder semantics:**
1. Filter to `payment.month == month` (defensive, matches the matcher's pattern).
2. **Month rollup `status_totals`:** for each status in `CANONICAL_PAYMENT_STATUSES`
   (always all four, in that fixed order), emit a `PaymentStatusBucket` with the
   record count and per-currency summed `payment_amount` (currencies sorted
   alphabetically). A status with zero records → `count=0`, empty
   `currency_totals`.
3. **`outstanding_totals`:** sum `payment_amount` of `PENDING` + `UNPAID` records
   per currency, currencies alphabetical. `PAID` and `CANCELLED` never contribute.
4. **`accounts`:** group by `source_account_id` (ascending). Each account carries
   its own `total_payment_count`, `status_totals` (only the statuses that account
   actually has, in canonical order), and `outstanding_totals`.
5. **Invariant:** the month rollup equals the element-wise sum of the per-account
   breakdowns (same total count; same per-(status,currency) and per-currency
   outstanding sums).
6. No FX: amounts are only ever added within the same currency string.

The builder is pure (no DB, no clock, no I/O) and total over any iterable of
already-validated `AdSensePaymentEntry`. The status domain is guaranteed by the
write-side `ALLOWED_PAYMENT_STATUSES` gate (`finance/adsense_payments.py:19`), so
the builder handles exactly the four `CANONICAL_PAYMENT_STATUSES` and builds no
unknown-status branch. Consequently `total_payment_count` equals the sum of the
four rollup bucket counts (no payment is dropped or double-counted). The builder
raises nothing for well-formed input.

---

## 4. API response shape

`GET /adsense/payments/status?month=2026-04` →

```json
{
  "month": "2026-04",
  "total_payment_count": 17,
  "status_totals": [
    {"status": "PAID",      "count": 12, "currency_totals": [{"currency": "USD", "amount": "8400"}]},
    {"status": "PENDING",   "count": 3,  "currency_totals": [{"currency": "EUR", "amount": "300"}, {"currency": "USD", "amount": "1200"}]},
    {"status": "UNPAID",    "count": 1,  "currency_totals": [{"currency": "GBP", "amount": "500"}]},
    {"status": "CANCELLED", "count": 1,  "currency_totals": [{"currency": "USD", "amount": "99"}]}
  ],
  "outstanding_totals": [
    {"currency": "EUR", "amount": "300"},
    {"currency": "GBP", "amount": "500"},
    {"currency": "USD", "amount": "1200"}
  ],
  "accounts": [
    {"source_account_id": "pub-111", "total_payment_count": 12,
     "status_totals": [{"status": "PAID", "count": 12, "currency_totals": [{"currency": "USD", "amount": "8400"}]}],
     "outstanding_totals": []},
    {"source_account_id": "pub-222", "total_payment_count": 5,
     "status_totals": [
       {"status": "PENDING",   "count": 3, "currency_totals": [{"currency": "EUR", "amount": "300"}, {"currency": "USD", "amount": "1200"}]},
       {"status": "UNPAID",    "count": 1, "currency_totals": [{"currency": "GBP", "amount": "500"}]},
       {"status": "CANCELLED", "count": 1, "currency_totals": [{"currency": "USD", "amount": "99"}]}
     ],
     "outstanding_totals": [{"currency": "EUR", "amount": "300"}, {"currency": "GBP", "amount": "500"}, {"currency": "USD", "amount": "1200"}]}
  ],
  "audit_event": { "...": "audit_record_to_api shape" }
}
```

`amount` values are serialized as precision-preserving strings (no scientific
notation, trailing zeros trimmed), reusing the existing
`_decimal_to_api` convention from `adsense_payments.py` / `payment_matching.py`.

---

## 5. Endpoint — `api/adsense.py` (thin route)

`GET /adsense/payments/status` — sits beside the existing
`GET /adsense/payments?month=...` in the `/adsense` resource family. `month` is a
**required** query parameter (the breakdown is month-scoped).

Handler contract:
1. Validate `month` against the existing `ADSENSE_MONTH_PATTERN`; malformed →
   `HTTP 422` with the same message style as `list_adsense_payments`.
2. `_require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS,
   AccessScope.finance_month(month))` — fail-closed. **No `VIEW_REVENUE`** (no
   revenue data is read), which is the explicit reason this is not under
   `/revenue/...`.
3. `payments = repository.list_month_payments(month=month)` via the existing
   `current_adsense_payment_repository` dependency.
4. `summary = build_monthly_payment_status_summary(month=month, payments=payments)`.
5. Map `AdSensePaymentValidationError` → `HTTP 422` (defensive; boundary already
   validated month).
6. Record **one** `record_audit_event(..., event_type=AuditEventType.PAYMENT_VIEWED,
   entity_type="adsense_payment_status", entity_id=month,
   scope=finance_month(month), details={"month": month,
   "total_payment_count": ..., "outstanding_currency_count": ...})`. No secrets,
   no raw payloads in details.
7. Return `summary.to_api()` with the audit event attached
   (`audit_record_to_api`), matching the existing endpoints' response convention.

Read-only: no locks, no `get_or_create_month_close_row`, no writes.

---

## 6. Determinism rules (so tests can pin exact output)

- **Status order:** always `PAID, PENDING, UNPAID, CANCELLED` (the
  `CANONICAL_PAYMENT_STATUSES` tuple) at the month-rollup level; per-account
  status buckets follow the same canonical order over the statuses present.
- **Currency order:** alphabetical by ISO code within every `currency_totals`
  and `outstanding_totals` list.
- **Account order:** `source_account_id` ascending.
- **Month rollup always emits all four statuses** (zero count → empty
  `currency_totals`); per-account emits only statuses that account has.
- Decimal sums are exact; serialization trims trailing zeros without rounding.

---

## 7. Testing

New service tests `tests/finance/test_payment_status.py` and API tests
`tests/api/test_adsense_payment_status_api.py`, mirroring the existing
`test_payment_matching.py` / `test_payment_match_api.py` structure.

**Operator-pinned cases (must exist):**
1. A `CANCELLED` payment's amount **appears** in that account's and the rollup's
   `CANCELLED` `currency_totals`.
2. A `CANCELLED` payment **does not appear** in any `outstanding_totals`.
3. Non-USD payments (e.g. EUR, GBP) are **accepted and grouped** by their
   currency, **not** treated as errors or excluded.
4. Month rollup and per-account totals are **deterministic** (fixed status order,
   alphabetical currency order, ascending account order) regardless of input
   ordering.

**Additional service cases:**
5. `outstanding_totals` = `PENDING` + `UNPAID` only (PAID and CANCELLED excluded).
6. Multi-currency within one status (e.g. `PENDING` USD + EUR) sums per currency.
7. Per-account split: each account's buckets and outstanding are correct, and the
   rollup equals the sum of accounts (invariant from §3.5).
8. All-`PAID` month → empty `outstanding_totals`.
9. Empty month → `total_payment_count = 0`, all four rollup statuses present with
   `count = 0`, empty `accounts`, empty `outstanding_totals`.
10. Decimal precision/serialization: high-scale amounts serialize without
    rounding or scientific notation.

**API cases:**
11. Happy path → `200`, full shape, exactly one `PAYMENT_VIEWED` audit event.
12. Malformed `month` → `422`.
13. Insufficient permission (assistant principal) → `403`.
14. Wrong scope (company-scoped finance viewer on a finance-month read) → `403`.
15. A response containing a non-USD payment surfaces it grouped (API-level mirror
    of pinned case 3).

All run under the standard gate: `ruff`, no-skip/xfail policy, full `pytest`,
`git diff --check`.

---

## 8. Tracker corrections (included in this PR)

Per the per-PR plan-status rule, this PR also corrects the stale Phase 3 markers
(no new tracker file):

- `Docs/01_IMPLEMENTATION_PLAN.md` Phase 3: mark the month-total matcher
  (`build_monthly_payment_match_summary` + `GET /revenue/months/{month}/payment-match`)
  as ✅ shipped (currently mis-marked ⏳ "not started"); add the paid/unpaid
  status pass under its PR number.
- `Docs/15_DELIVERY_BACKLOG.md` Phase 3 "Paid/unpaid status" line: mark the
  status-breakdown endpoint shipped; reconcile the "Payment match status" output
  line.

---

## 9. Validation gate (before push/PR)

- `python -m ruff check backend tests scripts`
- `python -m pytest -q` (full suite; the targeted
  `tests/finance/test_payment_status.py` and
  `tests/api/test_adsense_payment_status_api.py` must pass)
- `git diff --check`
- No migration-specific Postgres round-trip required; run with
  `UMS_TEST_DATABASE_URL` if available for full-gate parity.

---

## 10. Database / blast-radius statement

- **Tables/ORM touched:** reads `AdSensePaymentORM` (`adsense_payments`) only,
  via the existing `SqlAlchemyAdSensePaymentRepository.list_month_payments`. **No
  new column, constraint, index, or migration.** PostgreSQL remains the finance
  source of truth.
- **Authorization:** reuses `Permission.VIEW_FINALIZED_PAYMENTS` on
  `finance_month(month)`; fail-closed via `_require_permission`. No new
  permission, no weakening of scope checks.
- **Audit:** reuses `AuditEventType.PAYMENT_VIEWED`; no new event type; no
  secrets/raw payloads in `details`.
- **Finance correctness:** read-only aggregation; no writes, no locks, no
  override of source-of-truth values; no FX (faithful to `Docs/18`).
- **Graph projection:** No graph projection impact detected (Neo4j retired in
  PR #12; no projection code reads this path).
- **Migration/rollback:** none required (no schema delta).
