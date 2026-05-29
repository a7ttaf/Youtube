# AdSense Payment Paid/Unpaid Status Breakdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only per-month, per-currency, per-account AdSense payment
settlement-status breakdown (`PAID` / outstanding = `PENDING`+`UNPAID` /
`CANCELLED`) over the existing `adsense_payments` source-of-truth.

**Architecture:** A new pure aggregation module
`backend/ums_smart_revenue/finance/payment_status.py` (frozen dataclasses +
`.to_api()` + a `build_monthly_payment_status_summary` builder, no DB) consumed by
a thin read endpoint `GET /adsense/payments/status?month=YYYY-MM` added to the
existing `api/adsense.py` router. The endpoint reuses the existing
`SqlAlchemyAdSensePaymentRepository.list_month_payments`, the
`VIEW_FINALIZED_PAYMENTS` permission on `finance_month(month)`, and the
`PAYMENT_VIEWED` audit event. No schema change, no migration, no new persistence,
no FX.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (read-only), pytest,
SQLite (test DB), ruff.

**Spec:** `Docs/superpowers/specs/2026-05-29-spec-adsense-payment-status-design.md`

---

## File Structure

- **Create** `backend/ums_smart_revenue/finance/payment_status.py` — pure
  aggregation: `CurrencyAmount`, `PaymentStatusBucket`, `AccountPaymentStatus`,
  `MonthlyPaymentStatusSummary`, `build_monthly_payment_status_summary`. No DB,
  no I/O.
- **Create** `tests/finance/test_payment_status.py` — service unit tests.
- **Modify** `backend/ums_smart_revenue/api/adsense.py` — add one import and one
  `GET /adsense/payments/status` route handler. No other change.
- **Create** `tests/api/test_adsense_payment_status_api.py` — endpoint auth +
  shape + audit tests.
- **Modify** `Docs/01_IMPLEMENTATION_PLAN.md` and `Docs/15_DELIVERY_BACKLOG.md` —
  correct the stale Phase 3 markers.

## Conventions this plan follows (do not deviate)

- **Test invocation indirection:** service tests import the module under test via
  `importlib.import_module(...)` inside a helper (so the test file still collects
  before the module exists and fails at call time, not collection time) — this
  matches `tests/finance/test_payment_matching.py`.
- **Decimal serialization:** copy the exact `_decimal_to_api` helper already used
  in `finance/payment_matching.py` / `finance/adsense_payments.py` (string,
  trailing zeros trimmed, no scientific notation).
- **API auth headers / seeding:** mirror `tests/api/test_adsense_payments_api.py`
  — `auth_headers(role, scope_type, scope_id)` with
  `x-ums-trusted-gateway-token: pytest-trusted-gateway-token`; seed with
  `SecurityBase` + `FinanceBase` `create_all` + one `UserORM`; insert
  `AdSensePaymentORM` rows directly (tenant_id defaults to the UMS tenant, as in
  `tests/api/test_payment_match_api.py`).
- **Scope header strings:** global = `x-scope-type: global`; finance-month =
  `x-scope-type: finance-month` + `x-scope-id: YYYY-MM`.
- Run tests with the repo's `pytest` (strict config). Commit after each task.

---

## Task 1: Pure status-breakdown module

**Files:**
- Create: `backend/ums_smart_revenue/finance/payment_status.py`
- Test: `tests/finance/test_payment_status.py`

- [ ] **Step 1: Write the failing service tests**

Create `tests/finance/test_payment_status.py` with this exact content:

```python
"""Tests for the monthly AdSense payment paid/unpaid status breakdown."""
from datetime import date
from decimal import Decimal
from importlib import import_module

from ums_smart_revenue.finance.adsense_payments import AdSensePaymentEntry

MONTH = "2026-04"


def adsense_payment(
    *,
    name: str,
    amount: str,
    status: str = "PAID",
    currency: str = "USD",
    account: str = "pub-111",
    month: str = MONTH,
) -> AdSensePaymentEntry:
    """Create an AdSense payment read-model row for tests."""
    return AdSensePaymentEntry(
        id=f"{account}-{name}",
        source_account_id=account,
        month=month,
        payment_name=name,
        payment_date=date(2026, 5, 21),
        payment_amount=Decimal(amount),
        payment_currency=currency,
        payment_status=status,
        raw_payload={"paymentId": name},
        source_report_id="adsense-payment-2026-04",
        imported_by=None,
    )


def build(payments, *, month=MONTH):
    """Invoke the builder under test (import_module so collection precedes impl)."""
    module = import_module("ums_smart_revenue.finance.payment_status")
    return module.build_monthly_payment_status_summary(month=month, payments=payments)


def _bucket(summary, status):
    """Return the month-rollup bucket for a status."""
    return next(b for b in summary.status_totals if b.status == status)


def test_month_rollup_lists_all_four_statuses_in_canonical_order():
    summary = build([adsense_payment(name="p1", amount="8400.00")])
    assert [b.status for b in summary.status_totals] == [
        "PAID",
        "PENDING",
        "UNPAID",
        "CANCELLED",
    ]
    assert summary.total_payment_count == 1


def test_status_bucket_groups_amounts_per_currency_alphabetically():
    summary = build(
        [
            adsense_payment(name="p1", amount="1200.00", status="PENDING", currency="USD"),
            adsense_payment(name="p2", amount="300.00", status="PENDING", currency="EUR"),
        ]
    )
    pending = _bucket(summary, "PENDING")
    assert pending.count == 2
    assert [(c.currency, c.amount) for c in pending.currency_totals] == [
        ("EUR", Decimal("300.00")),
        ("USD", Decimal("1200.00")),
    ]


def test_cancelled_amount_appears_in_cancelled_currency_totals():
    # Operator-pinned case 1.
    summary = build(
        [adsense_payment(name="c1", amount="99.00", status="CANCELLED", currency="USD")]
    )
    cancelled = _bucket(summary, "CANCELLED")
    assert cancelled.count == 1
    assert [(c.currency, c.amount) for c in cancelled.currency_totals] == [
        ("USD", Decimal("99.00"))
    ]


def test_cancelled_is_excluded_from_outstanding_totals():
    # Operator-pinned case 2.
    summary = build(
        [
            adsense_payment(name="c1", amount="99.00", status="CANCELLED"),
            adsense_payment(name="u1", amount="500.00", status="UNPAID", currency="GBP"),
        ]
    )
    assert [(c.currency, c.amount) for c in summary.outstanding_totals] == [
        ("GBP", Decimal("500.00"))
    ]


def test_non_usd_payments_are_accepted_and_grouped_not_errored():
    # Operator-pinned case 3.
    summary = build(
        [
            adsense_payment(name="e1", amount="300.00", status="PENDING", currency="EUR"),
            adsense_payment(name="g1", amount="500.00", status="UNPAID", currency="GBP"),
        ]
    )
    assert [(c.currency, c.amount) for c in summary.outstanding_totals] == [
        ("EUR", Decimal("300.00")),
        ("GBP", Decimal("500.00")),
    ]


def test_outstanding_totals_sum_pending_and_unpaid_only():
    summary = build(
        [
            adsense_payment(name="paid", amount="8400.00", status="PAID"),
            adsense_payment(name="pend", amount="1200.00", status="PENDING"),
            adsense_payment(name="unp", amount="500.00", status="UNPAID"),
            adsense_payment(name="canc", amount="99.00", status="CANCELLED"),
        ]
    )
    assert [(c.currency, c.amount) for c in summary.outstanding_totals] == [
        ("USD", Decimal("1700.00"))
    ]


def test_all_paid_month_has_no_outstanding():
    summary = build([adsense_payment(name="p1", amount="8400.00", status="PAID")])
    assert summary.outstanding_totals == []


def test_empty_month_reports_zeroed_rollup_and_no_accounts():
    summary = build([])
    assert summary.total_payment_count == 0
    assert [(b.status, b.count, b.currency_totals) for b in summary.status_totals] == [
        ("PAID", 0, []),
        ("PENDING", 0, []),
        ("UNPAID", 0, []),
        ("CANCELLED", 0, []),
    ]
    assert summary.outstanding_totals == []
    assert summary.accounts == []


def test_per_account_breakdown_splits_by_source_account_id():
    summary = build(
        [
            adsense_payment(name="p1", amount="8400.00", status="PAID", account="pub-111"),
            adsense_payment(name="pend", amount="1200.00", status="PENDING", account="pub-222"),
            adsense_payment(name="unp", amount="500.00", status="UNPAID", currency="GBP", account="pub-222"),
        ]
    )
    assert [a.source_account_id for a in summary.accounts] == ["pub-111", "pub-222"]
    pub111 = summary.accounts[0]
    assert pub111.total_payment_count == 1
    assert [b.status for b in pub111.status_totals] == ["PAID"]
    assert pub111.outstanding_totals == []
    pub222 = summary.accounts[1]
    assert pub222.total_payment_count == 2
    assert [b.status for b in pub222.status_totals] == ["PENDING", "UNPAID"]
    assert [(c.currency, c.amount) for c in pub222.outstanding_totals] == [
        ("GBP", Decimal("500.00")),
        ("USD", Decimal("1200.00")),
    ]


def test_rollup_equals_sum_of_account_breakdowns():
    payments = [
        adsense_payment(name="p1", amount="8400.00", status="PAID", account="pub-111"),
        adsense_payment(name="pend", amount="1200.00", status="PENDING", account="pub-222"),
        adsense_payment(name="unp", amount="500.00", status="UNPAID", currency="GBP", account="pub-222"),
    ]
    summary = build(payments)
    assert summary.total_payment_count == sum(
        a.total_payment_count for a in summary.accounts
    )
    assert [(c.currency, c.amount) for c in summary.outstanding_totals] == [
        ("GBP", Decimal("500.00")),
        ("USD", Decimal("1200.00")),
    ]


def test_determinism_independent_of_input_order():
    payments = [
        adsense_payment(name="g1", amount="500.00", status="UNPAID", currency="GBP", account="pub-222"),
        adsense_payment(name="p1", amount="8400.00", status="PAID", account="pub-111"),
        adsense_payment(name="e1", amount="300.00", status="PENDING", currency="EUR", account="pub-222"),
    ]
    first = build(payments)
    second = build(list(reversed(payments)))
    assert first.to_api() == second.to_api()
    assert [a.source_account_id for a in first.accounts] == ["pub-111", "pub-222"]


def test_amount_serialization_preserves_precision_without_scientific_notation():
    summary = build([adsense_payment(name="p1", amount="1234.5678", status="PENDING")])
    assert summary.to_api()["outstanding_totals"] == [
        {"currency": "USD", "amount": "1234.5678"}
    ]


def test_payments_from_other_months_are_ignored():
    summary = build(
        [
            adsense_payment(name="this", amount="100.00", status="PENDING", month="2026-04"),
            adsense_payment(name="other", amount="999.00", status="PENDING", month="2026-03"),
        ],
        month="2026-04",
    )
    assert summary.total_payment_count == 1
    assert [(c.currency, c.amount) for c in summary.outstanding_totals] == [
        ("USD", Decimal("100.00"))
    ]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/finance/test_payment_status.py -q`
Expected: FAIL — every test errors with
`ModuleNotFoundError: No module named 'ums_smart_revenue.finance.payment_status'`
raised inside `build(...)`.

- [ ] **Step 3: Write the module**

Create `backend/ums_smart_revenue/finance/payment_status.py` with this exact
content:

```python
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from ums_smart_revenue.finance.adsense_payments import AdSensePaymentEntry

# Fixed display + iteration order for the four write-validated statuses
# (see ALLOWED_PAYMENT_STATUSES in finance/adsense_payments.py).
CANONICAL_PAYMENT_STATUSES: tuple[str, ...] = ("PAID", "PENDING", "UNPAID", "CANCELLED")
OUTSTANDING_STATUSES: frozenset[str] = frozenset({"PENDING", "UNPAID"})


@dataclass(frozen=True)
class CurrencyAmount:
    """One currency's summed amount (no FX; amounts only added within a currency)."""

    currency: str
    amount: Decimal

    def to_api(self) -> dict[str, object]:
        return {"currency": self.currency, "amount": _decimal_to_api(self.amount)}


@dataclass(frozen=True)
class PaymentStatusBucket:
    """Count + per-currency totals for one payment status."""

    status: str
    count: int
    currency_totals: list[CurrencyAmount]

    def to_api(self) -> dict[str, object]:
        return {
            "status": self.status,
            "count": self.count,
            "currency_totals": [total.to_api() for total in self.currency_totals],
        }


@dataclass(frozen=True)
class AccountPaymentStatus:
    """Per-source_account_id status breakdown."""

    source_account_id: str
    total_payment_count: int
    status_totals: list[PaymentStatusBucket]
    outstanding_totals: list[CurrencyAmount]

    def to_api(self) -> dict[str, object]:
        return {
            "source_account_id": self.source_account_id,
            "total_payment_count": self.total_payment_count,
            "status_totals": [bucket.to_api() for bucket in self.status_totals],
            "outstanding_totals": [
                total.to_api() for total in self.outstanding_totals
            ],
        }


@dataclass(frozen=True)
class MonthlyPaymentStatusSummary:
    """Month-wide rollup plus per-account breakdown of AdSense payment status."""

    month: str
    total_payment_count: int
    status_totals: list[PaymentStatusBucket]
    outstanding_totals: list[CurrencyAmount]
    accounts: list[AccountPaymentStatus]

    def to_api(self) -> dict[str, object]:
        return {
            "month": self.month,
            "total_payment_count": self.total_payment_count,
            "status_totals": [bucket.to_api() for bucket in self.status_totals],
            "outstanding_totals": [
                total.to_api() for total in self.outstanding_totals
            ],
            "accounts": [account.to_api() for account in self.accounts],
        }


# ============================================================================
# Purpose: Aggregate one month's AdSense payments into a paid/unpaid status
#   breakdown (month rollup + per-account), grouping amounts by reported
#   currency with no FX. outstanding = PENDING + UNPAID; CANCELLED is reported
#   for evidence but excluded from outstanding.
# Database/ORM: None (pure function over already-read AdSensePaymentEntry rows).
# Standards: Pure/total; deterministic ordering (canonical status order,
#   alphabetical currencies, ascending source_account_id) so output is testable.
# Blast Radius: Finance read-model only. No writes, no auth, no Neo4j.
# Connections:
#   - File: backend/ums_smart_revenue/finance/adsense_payments.py -> consumes
#     AdSensePaymentEntry; status domain guaranteed by ALLOWED_PAYMENT_STATUSES.
#   - File: backend/ums_smart_revenue/api/adsense.py -> GET /adsense/payments/status.
# ============================================================================
def build_monthly_payment_status_summary(
    *,
    month: str,
    payments: Iterable[AdSensePaymentEntry],
) -> MonthlyPaymentStatusSummary:
    month_payments = [payment for payment in payments if payment.month == month]
    return MonthlyPaymentStatusSummary(
        month=month,
        total_payment_count=len(month_payments),
        status_totals=_status_buckets(month_payments, include_all_statuses=True),
        outstanding_totals=_outstanding_totals(month_payments),
        accounts=_accounts(month_payments),
    )


def _status_buckets(
    payments: list[AdSensePaymentEntry],
    *,
    include_all_statuses: bool,
) -> list[PaymentStatusBucket]:
    by_status: dict[str, list[AdSensePaymentEntry]] = {}
    for payment in payments:
        by_status.setdefault(payment.payment_status, []).append(payment)
    statuses = (
        CANONICAL_PAYMENT_STATUSES
        if include_all_statuses
        else tuple(
            status for status in CANONICAL_PAYMENT_STATUSES if status in by_status
        )
    )
    return [
        PaymentStatusBucket(
            status=status,
            count=len(by_status.get(status, [])),
            currency_totals=_currency_totals(by_status.get(status, [])),
        )
        for status in statuses
    ]


def _outstanding_totals(payments: list[AdSensePaymentEntry]) -> list[CurrencyAmount]:
    return _currency_totals(
        [
            payment
            for payment in payments
            if payment.payment_status in OUTSTANDING_STATUSES
        ]
    )


def _accounts(payments: list[AdSensePaymentEntry]) -> list[AccountPaymentStatus]:
    by_account: dict[str, list[AdSensePaymentEntry]] = {}
    for payment in payments:
        by_account.setdefault(payment.source_account_id, []).append(payment)
    return [
        AccountPaymentStatus(
            source_account_id=source_account_id,
            total_payment_count=len(by_account[source_account_id]),
            status_totals=_status_buckets(
                by_account[source_account_id], include_all_statuses=False
            ),
            outstanding_totals=_outstanding_totals(by_account[source_account_id]),
        )
        for source_account_id in sorted(by_account)
    ]


def _currency_totals(payments: list[AdSensePaymentEntry]) -> list[CurrencyAmount]:
    sums: dict[str, Decimal] = {}
    for payment in payments:
        sums[payment.payment_currency] = (
            sums.get(payment.payment_currency, Decimal("0")) + payment.payment_amount
        )
    return [
        CurrencyAmount(currency=currency, amount=sums[currency])
        for currency in sorted(sums)
    ]


def _decimal_to_api(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f")
    return format(normalized, "f").rstrip("0").rstrip(".")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/finance/test_payment_status.py -q`
Expected: PASS — 13 passed.

- [ ] **Step 5: Lint**

Run: `python -m ruff check backend/ums_smart_revenue/finance/payment_status.py tests/finance/test_payment_status.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add backend/ums_smart_revenue/finance/payment_status.py tests/finance/test_payment_status.py
git commit -m "feat(finance): AdSense payment paid/unpaid status breakdown builder"
```

---

## Task 2: `GET /adsense/payments/status` endpoint

**Files:**
- Modify: `backend/ums_smart_revenue/api/adsense.py`
- Test: `tests/api/test_adsense_payment_status_api.py`

- [ ] **Step 1: Write the failing endpoint tests**

Create `tests/api/test_adsense_payment_status_api.py` with this exact content:

```python
"""Tests for the AdSense payment paid/unpaid status endpoint."""
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.api.adsense import get_adsense_payment_status
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.auth.models import RoleAssignment, UserPrincipal
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.finance_models import AdSensePaymentORM, FinanceBase
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM
from ums_smart_revenue.finance.adsense_payments import AdSensePaymentValidationError

USER_ID = UUID("00000000-0000-0000-0000-00000000b401")
MONTH = "2026-04"


def auth_headers(role, scope_type="global", scope_id=None):
    """Build trusted-gateway auth headers."""
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "payment-status@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path):
    """Return a unique SQLite URL under pytest's temp path."""
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def _payment(*, name, amount, status, currency, account):
    """Build one AdSensePaymentORM row (tenant_id defaults to the UMS tenant)."""
    return AdSensePaymentORM(
        id=uuid4(),
        month=MONTH,
        payment_name=name,
        payment_date=date(2026, 5, 21),
        payment_amount=Decimal(amount),
        payment_currency=currency,
        payment_status=status,
        raw_payload={"paymentId": name},
        source_report_id="adsense-payment-2026-04",
        source_account_id=account,
        imported_by=USER_ID,
    )


def seed_database(database_url):
    """Seed one trusted user and a multi-status/currency/account payment set."""
    engine = create_engine(database_url)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            UserORM(
                id=USER_ID,
                email="payment-status@example.com",
                display_name="Status User",
            )
        )
        session.add_all(
            [
                _payment(name="paid-1", amount="8400.00", status="PAID", currency="USD", account="pub-111"),
                _payment(name="pend-usd", amount="1200.00", status="PENDING", currency="USD", account="pub-222"),
                _payment(name="pend-eur", amount="300.00", status="PENDING", currency="EUR", account="pub-222"),
                _payment(name="unp-gbp", amount="500.00", status="UNPAID", currency="GBP", account="pub-222"),
                _payment(name="canc-usd", amount="99.00", status="CANCELLED", currency="USD", account="pub-222"),
            ]
        )
        session.commit()


def test_finance_viewer_reads_payment_status_breakdown_with_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        f"/adsense/payments/status?month={MONTH}",
        headers=auth_headers("finance_viewer", "global"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["month"] == MONTH
    assert body["total_payment_count"] == 5
    assert [b["status"] for b in body["status_totals"]] == [
        "PAID",
        "PENDING",
        "UNPAID",
        "CANCELLED",
    ]
    statuses = {b["status"]: b for b in body["status_totals"]}
    assert statuses["PAID"]["currency_totals"] == [{"currency": "USD", "amount": "8400"}]
    assert statuses["PENDING"]["currency_totals"] == [
        {"currency": "EUR", "amount": "300"},
        {"currency": "USD", "amount": "1200"},
    ]
    assert statuses["CANCELLED"]["currency_totals"] == [
        {"currency": "USD", "amount": "99"}
    ]
    assert body["outstanding_totals"] == [
        {"currency": "EUR", "amount": "300"},
        {"currency": "GBP", "amount": "500"},
        {"currency": "USD", "amount": "1200"},
    ]
    assert [a["source_account_id"] for a in body["accounts"]] == ["pub-111", "pub-222"]
    assert body["audit_event"]["event_type"] == "PAYMENT_VIEWED"

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_logs = session.scalars(select(AuditLogORM)).all()
    assert len(audit_logs) == 1
    assert audit_logs[0].event_type == "PAYMENT_VIEWED"
    assert (audit_logs[0].scope_type, audit_logs[0].scope_id) == ("finance-month", MONTH)
    assert audit_logs[0].sensitive is True


def test_cancelled_amount_present_but_excluded_from_outstanding_via_api(tmp_path):
    # API mirror of operator-pinned cases 1 and 2.
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    body = client.get(
        f"/adsense/payments/status?month={MONTH}",
        headers=auth_headers("finance_viewer", "global"),
    ).json()
    cancelled = next(b for b in body["status_totals"] if b["status"] == "CANCELLED")
    assert cancelled["currency_totals"] == [{"currency": "USD", "amount": "99"}]
    usd_outstanding = next(
        c for c in body["outstanding_totals"] if c["currency"] == "USD"
    )
    assert usd_outstanding["amount"] == "1200"  # PENDING only, never the 99 CANCELLED


def test_non_usd_payment_surfaced_in_api_response(tmp_path):
    # API mirror of operator-pinned case 3.
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    body = client.get(
        f"/adsense/payments/status?month={MONTH}",
        headers=auth_headers("finance_viewer", "global"),
    ).json()
    currencies = {c["currency"] for c in body["outstanding_totals"]}
    assert {"EUR", "GBP"} <= currencies


def test_malformed_month_returns_422(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        "/adsense/payments/status?month=2026-13",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 422


def test_repository_validation_error_maps_to_422():
    user = UserPrincipal(
        user_id=str(USER_ID),
        email="payment-status@example.com",
        role_assignments=(
            RoleAssignment(role=RoleKey.FINANCE_VIEWER, scope=AccessScope.global_scope()),
        ),
    )
    with pytest.raises(HTTPException) as exc_info:
        get_adsense_payment_status(
            month="2026-04",
            user=user,
            repository=_FailingPaymentRepository(),
            audit_sink=InMemoryAuditSink(),
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "invalid payment query"


def test_assistant_cannot_read_payment_status(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/adsense/payments/status?month={MONTH}",
        headers=auth_headers("assistant_analyst", "global"),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_finalized_payments"


def test_finance_month_scoped_viewer_reads_matching_month(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/adsense/payments/status?month={MONTH}",
        headers=auth_headers("finance_viewer", "finance-month", MONTH),
    )
    assert response.status_code == 200


def test_finance_month_scoped_viewer_cannot_read_other_month(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/adsense/payments/status?month={MONTH}",
        headers=auth_headers("finance_viewer", "finance-month", "2026-03"),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_finalized_payments"


class _FailingPaymentRepository:
    """Repository stub that raises validation errors."""

    @staticmethod
    def list_month_payments(*, month: str):
        raise AdSensePaymentValidationError("invalid payment query")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/api/test_adsense_payment_status_api.py -q`
Expected: FAIL — collection errors with
`ImportError: cannot import name 'get_adsense_payment_status' from 'ums_smart_revenue.api.adsense'`.

- [ ] **Step 3: Add the import to `api/adsense.py`**

In `backend/ums_smart_revenue/api/adsense.py`, immediately after the existing
import block

```python
from ums_smart_revenue.finance.adsense_payments import (
    MAX_ADSENSE_PAYMENT_PAGE_SIZE,
    AdSensePaymentInput,
    AdSensePaymentLockedMonthError,
    AdSensePaymentValidationError,
    SqlAlchemyAdSensePaymentRepository,
)
```

add:

```python
from ums_smart_revenue.finance.payment_status import (
    build_monthly_payment_status_summary,
)
```

- [ ] **Step 4: Add the route handler to `api/adsense.py`**

Insert this handler immediately after the `list_adsense_payments` function ends
(just before `def _require_permission(`):

```python
# ============================================================================
# Purpose: Read-only per-month AdSense payment paid/unpaid status breakdown
#   (month rollup + per-account, per-currency; outstanding = PENDING+UNPAID).
# Database/ORM: Reads AdSensePaymentORM via SqlAlchemyAdSensePaymentRepository;
#   no writes.
# Standards: Thin route; boundary month validation -> 422; fail-closed
#   permission check; single reused PAYMENT_VIEWED audit event; no secrets in
#   audit details.
# Blast Radius: Finance read (payment status). No finance mutation, no Neo4j.
# Connections:
#   - File: backend/ums_smart_revenue/finance/payment_status.py -> pure builder.
#   - File: backend/ums_smart_revenue/auth/permissions.py -> VIEW_FINALIZED_PAYMENTS.
# ============================================================================
@router.get("/payments/status")
def get_adsense_payment_status(
    month: Annotated[str, Query(min_length=1)],
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyAdSensePaymentRepository,
        Depends(current_adsense_payment_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    normalized_month = month.strip()
    if not ADSENSE_MONTH_PATTERN.fullmatch(normalized_month):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="month must use YYYY-MM with a calendar month from 01 to 12",
        )
    scope = AccessScope.finance_month(normalized_month)
    _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, scope)
    try:
        payments = repository.list_month_payments(month=normalized_month)
    except AdSensePaymentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    summary = build_monthly_payment_status_summary(
        month=normalized_month, payments=payments
    )
    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.PAYMENT_VIEWED,
        entity_type="adsense_payment_status",
        entity_id=normalized_month,
        scope=scope,
        details={
            "month": normalized_month,
            "total_payment_count": summary.total_payment_count,
            "outstanding_currency_count": len(summary.outstanding_totals),
        },
    )
    result = summary.to_api()
    result["audit_event"] = audit_record_to_api(record)
    return result
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/api/test_adsense_payment_status_api.py -q`
Expected: PASS — 8 passed.

- [ ] **Step 6: Lint**

Run: `python -m ruff check backend/ums_smart_revenue/api/adsense.py tests/api/test_adsense_payment_status_api.py`
Expected: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add backend/ums_smart_revenue/api/adsense.py tests/api/test_adsense_payment_status_api.py
git commit -m "feat(adsense): GET /adsense/payments/status paid/unpaid breakdown endpoint"
```

---

## Task 3: Correct stale Phase 3 trackers

**Files:**
- Modify: `Docs/01_IMPLEMENTATION_PLAN.md`
- Modify: `Docs/15_DELIVERY_BACKLOG.md`

> The PR number is the only value filled at PR-open time. Until the PR is opened,
> use `(this PR)`; replace with `PR #N` immediately after opening, in the same
> commit cycle as the push.

- [ ] **Step 1: Update `Docs/01_IMPLEMENTATION_PLAN.md` Phase 3 Build items**

Replace the line:

```
- ⏳ Paid/unpaid status — remaining: status field exists in the payment
  ORM; reconciliation pass not driven.
```

with:

```
- ✅ Paid/unpaid status — shipped (this PR): per-month, per-account,
  per-currency settlement-status breakdown (`finance/payment_status.py` +
  `GET /adsense/payments/status`); outstanding = PENDING + UNPAID; CANCELLED
  reported for evidence, excluded from outstanding; no FX.
```

Replace the line:

```
- ⏳ Payment month matcher — remaining: not started.
```

with:

```
- ✅ Payment month matcher — shipped earlier and verified live:
  `build_monthly_payment_match_summary` + `GET /revenue/months/{month}/payment-match`
  (month-total YouTube↔AdSense match; prior backlog "not started" was stale).
```

Replace the lines:

```
- ⏳ Payment-vs-YouTube comparison — remaining: bank reconciliation repo
  (PR #29) is the substrate; comparison logic not driven.
```

with:

```
- ✅ Payment-vs-YouTube comparison — shipped via the payment-match endpoint
  (YouTube gross USD vs PAID AdSense USD → gap + PAYMENT_MATCHED/PAYMENT_VARIANCE);
  bank reconciliation (PR #29) remains a separate downstream leg.
```

- [ ] **Step 2: Update `Docs/01_IMPLEMENTATION_PLAN.md` Phase 3 Outputs + Status**

Replace the line:

```
- ⏳ Payment match status — not driven.
```

with:

```
- ✅ Payment match status — driven by the payment-match endpoint plus the
  paid/unpaid status breakdown (`GET /adsense/payments/status`).
```

Replace the Phase 3 `### Status (2026-05-29)` paragraph:

```
Live AdSense payment pull now runs through the dedicated operator CLI and
persists account-scoped settlements into the PostgreSQL `adsense_payments`
source-of-truth table. Matching remains outstanding and must start from
AdSense-reported payment amounts/currencies, not market FX-derived amounts.
```

with:

```
Live AdSense payment pull persists account-scoped settlements into the
PostgreSQL `adsense_payments` source-of-truth. The month-total YouTube↔AdSense
matcher already ships (`GET /revenue/months/{month}/payment-match`); this PR adds
the per-account, per-currency paid/unpaid status breakdown
(`GET /adsense/payments/status`). Both read AdSense-reported amounts/currencies
only — no market FX. Remaining Phase 3 depth (per-account *matching* needing a
channel↔account map, and multi-currency FX) stays out per Docs/18.
```

- [ ] **Step 3: Update `Docs/15_DELIVERY_BACKLOG.md`**

Immediately after the AdSense payment sync entry (the block ending with the
`...an explicit-currency resolution before they can sync.` line), insert:

```
- ✅ AdSense payment matching + paid/unpaid status — month-total YouTube↔AdSense
  matcher (`GET /revenue/months/{month}/payment-match`, verified pre-existing)
  and per-account/per-currency settlement-status breakdown
  (`GET /adsense/payments/status`, `finance/payment_status.py`, this PR).
  Payment-match remains USD-only; the paid/unpaid status view groups
  AdSense-reported amounts by currency. Outstanding = PENDING + UNPAID;
  CANCELLED shown for evidence; no FX, per Docs/18.
```

- [ ] **Step 4: Verify doc hygiene**

Run: `git diff --check`
Expected: no whitespace errors, no output.

- [ ] **Step 5: Commit**

```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "docs(plan): mark Phase 3 payment matcher live + add paid/unpaid status"
```

---

## Task 4: Full validation gate

**Files:** none (verification only).

- [ ] **Step 1: Ruff over the standard scope**

Run: `python -m ruff check backend tests scripts`
Expected: `All checks passed!`

- [ ] **Step 2: Full test suite**

Run: `python -m pytest -q`
Expected: PASS — prior suite count + 21 new tests (13 service + 8 API), 0 failed,
0 skipped/xfailed.

- [ ] **Step 3: Whitespace/diff hygiene**

Run: `git diff --check`
Expected: no output.

- [ ] **Step 4 (optional parity): Postgres-backed run**

No migration-specific Postgres round-trip is required (no schema change). If
`UMS_TEST_DATABASE_URL` is set, run `python -m pytest -q` again under it for
full-gate parity.

---

## Notes for the implementer

- Do **not** add a new `AuditEventType`, `Permission`, ORM column, or Alembic
  migration. If you find yourself reaching for one, stop — re-read the spec §2
  non-goals.
- Do **not** apply any currency conversion. Amounts are only ever summed within a
  single currency string.
- Keep the route thin: no business logic in the handler beyond month validation,
  permission, repo call, builder call, audit, serialize.
- If any existing test outside this feature breaks, it is a real regression —
  investigate; do not edit unrelated tests to pass.
