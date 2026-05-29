# AdSense Live Payment Sync — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a CLI-triggered live pull of real AdSense payments
(`accounts.payments.list`) into the existing `adsense_payments` PostgreSQL
source-of-truth, fail-closed, preserving Google-reported amount/currency/date/
month + account identity + source-report identity.

**Architecture:** A dedicated payment-sync path fully separate from the
`run_one` source-row connector framework. `GoogleAdSensePaymentClient` fetches
`payments.list`; a pure mapping module converts each `Payment` into the
repository input shape with fail-closed parsing; `AdSensePaymentSyncService`
orchestrates credential resolution → fetch → read-only locked-month prefilter →
strict parse (open months only) → `sync_payments` → audit. A new CLI invokes the
service. PostgreSQL stays the source of truth.

**Tech Stack:** Python 3.x, SQLAlchemy 2.x, Alembic (batch migrations),
PostgreSQL, `httpx` + `google-auth` (existing `GoogleHttpClient`), pytest.

**Source spec:** `Docs/superpowers/specs/2026-05-28-spec-adsense-payment-sync-design.md`
(approved; spec commit `fb9f321`).

---

## Scope hard gates (per operator)

- This plan produces no runtime behavior on its own. Each task below is TDD:
  failing test first, minimal implementation, green, commit.
- No feature code / migration / tests exist yet; the implementation PR creates
  them task-by-task.
- The stale `⏳ PR #50 — awaiting merge` line in `Docs/01`/`Docs/15` is flipped
  to `✅ merged 2026-05-28 (9c884bd)` in **Task 10 of this feature's PR** — not a
  standalone tracker PR.

---

## File Structure

### New files
| Path | Responsibility |
|------|----------------|
| `backend/ums_smart_revenue/connectors/google/adsense_payments_client.py` | `GoogleAdSensePaymentClient.fetch_payments` (single GET, account validation, deterministic `source_report_id`). |
| `backend/ums_smart_revenue/connectors/google/adsense_payment_mapping.py` | Pure `Payment → (eligible inputs, skipped balances)`; month derivation; suffix/date check; allowlist amount/currency parse. |
| `backend/ums_smart_revenue/connectors/google/adsense_payment_sync.py` | `AdSensePaymentSyncService` orchestrating the §5 pipeline + dry-run + audit. |
| `scripts/run_adsense_payment_sync.py` | Operator CLI (`--tenant --account --reason [--dry-run]`). |
| `backend/ums_smart_revenue/db/alembic/versions/20260529_0001_adsense_payment_source_account.py` | Add `source_account_id`, re-key uniqueness, backfill sentinel. |
| `tests/connectors/google/test_adsense_payments_client.py` | Client tests. |
| `tests/connectors/google/test_adsense_payment_mapping.py` | Mapping/parse tests. |
| `tests/connectors/google/test_adsense_payment_sync.py` | Service pipeline tests. |
| `tests/finance/test_month_close_status.py` | `get_month_close_status` tests. |
| `tests/db/test_adsense_payment_source_account_migration_postgres.py` | Postgres round-trip migration test (new constraint). |
| `tests/scripts/test_run_adsense_payment_sync_cli.py` | CLI exit-code tests. |

### Modified files
| Path | Change |
|------|--------|
| `backend/ums_smart_revenue/finance/adsense_payments.py` | `source_account_id` on `AdSensePaymentInput`/`AdSensePaymentEntry`; `audit_entity_id`; `to_api`; within-batch dup key (3-tuple); ON CONFLICT target; `_normalize_payment`. |
| `backend/ums_smart_revenue/db/finance_models.py` | `source_account_id` column + non-empty CHECK; swap unique constraint. |
| `backend/ums_smart_revenue/finance/month_close.py` | New read-only `get_month_close_status(...)`. |
| `backend/ums_smart_revenue/connectors/runs/orchestrator.py` | Promote `_credentials_for_run` → public `resolve_connector_credentials` (private alias kept). |
| `backend/ums_smart_revenue/connectors/google/registry.py` | `ADSENSE_MANAGEMENT_CONNECTOR_KEY = "adsense-management"` constant. |
| `backend/ums_smart_revenue/api/adsense.py` | `source_account_id` required on `AdSensePaymentRequest`, canonicalized via `_validated_account_id` (malformed → 422); thread into `AdSensePaymentInput`. |
| `tests/finance/test_adsense_payments_tenant_scope.py` | Add `source_account_id` to inputs; new cross-account uniqueness test. |
| `tests/db/test_adsense_payment_models.py` | Add `source_account_id` to ORM rows. |
| `tests/api/test_adsense_payments_api.py` | Add `source_account_id`; missing/malformed → validation error. |
| `Docs/01_IMPLEMENTATION_PLAN.md`, `Docs/15_DELIVERY_BACKLOG.md` | Status + PR #50 flip. |

---

## DB / Blast-Radius review

- **Tables affected:** `adsense_payments` (schema: +`source_account_id`, swapped
  unique constraint, +CHECK) and `audit_logs` (one event row per live sync).
- **PostgreSQL remains the source of truth** for payments; the live pull writes
  only through `SqlAlchemyAdSensePaymentRepository.sync_payments`.
- **Could existing migrations/tests/seed/docs break?** The historical
  `20260512_0002` migration is unchanged, so `tests/db/test_adsense_payment_migration.py`
  (which applies that migration in isolation) stays green. Repo/API/ORM tests
  that build `AdSensePaymentInput`/ORM rows must add the new required field
  (Tasks 1, 3). The new constraint is validated by a fresh Postgres round-trip
  test (Task 2).
- **Neo4j / graph projection:** `No graph projection impact detected.` No graph
  reads/writes; nothing here mutates source-of-truth via a graph path.
- **Authorization/audit more permissive?** No. No new `AuditEventType`, no new
  `Permission`; the existing `ADSENSE_PAYMENT_SYNCED` event + `RUN_CONNECTOR_JOBS`
  are reused. The only payload change is a `details` discriminator
  (`trigger="live_pull"`, counts, months) plus **capped, safe** per-entry skip
  evidence (resource name, month/payment_date, raw formatted amount, reason) —
  bounded by `_MAX_SKIP_EVIDENCE`, no secrets/tokens.
- **Finance results / month locks / overrides / payment matching change?** No.
  `payment_matching` and `bank_reconciliation` are untouched; `sync_payments`
  stays strict on locked months (final race guard); the manual endpoint's
  locked-month rejection is unchanged.
- **Migration reversibility:** `Disposable pre-alpha data reset accepted:
  backfill sentinel (or reseed) required for the NOT-NULL source_account_id
  column.` Migration is additive + a reversible unique-constraint swap;
  `downgrade()` restores `(tenant_id, month, payment_name)` and drops the column.
- **No `run_one` / `connector_runs` / `google_revenue_source_rows` involvement.**

---

## Task 1 — `source_account_id` on dataclasses, ORM, repository

**Files:**
- Modify: `backend/ums_smart_revenue/finance/adsense_payments.py`
- Modify: `backend/ums_smart_revenue/db/finance_models.py:356-435`
  (`AdSensePaymentORM`)
- Test: `tests/finance/test_adsense_payments_tenant_scope.py` (extend)

- [ ] **Step 1 — Write failing repo tests** (append to the tenant-scope test
  file `tests/finance/test_adsense_payments_tenant_scope.py`, reusing its
  existing module-level helpers/constants: `build_session()`,
  `DEFAULT_TENANT_ID`, `ACTOR_USER_ID`, and the imported `select`, `pytest`,
  `date`, `Decimal`, `AdSensePaymentORM`, `AdSensePaymentInput`,
  `AdSensePaymentValidationError`, `SqlAlchemyAdSensePaymentRepository`). Also
  add `source_account_id="pub-1"` to the file's existing `_payment_input(...)`
  helper so the pre-existing tenant-scope tests keep constructing valid inputs.

```python
def _payment(**overrides) -> AdSensePaymentInput:
    base = dict(
        source_account_id="pub-1",
        month="2026-04",
        payment_name="2026-04-21",
        payment_date=date(2026, 4, 21),
        payment_amount=Decimal("930.000000"),
        payment_currency="USD",
        payment_status="PAID",
        raw_payload={"name": "accounts/pub-1/payments/2026-04-21"},
    )
    base.update(overrides)
    return AdSensePaymentInput(**base)


def test_sync_allows_same_month_payment_name_across_accounts() -> None:
    session = build_session()
    repo = SqlAlchemyAdSensePaymentRepository(session, tenant_id=DEFAULT_TENANT_ID)
    repo.sync_payments(
        payments=[_payment(source_account_id="pub-1")],
        actor_user_id=ACTOR_USER_ID, source_report_id=None,
    )
    repo.sync_payments(
        payments=[_payment(source_account_id="pub-2")],
        actor_user_id=ACTOR_USER_ID, source_report_id=None,
    )
    session.commit()
    rows = session.scalars(
        select(AdSensePaymentORM).where(
            AdSensePaymentORM.tenant_id == DEFAULT_TENANT_ID,
            AdSensePaymentORM.month == "2026-04",
            AdSensePaymentORM.payment_name == "2026-04-21",
        )
    ).all()
    assert {r.source_account_id for r in rows} == {"pub-1", "pub-2"}
    assert len(rows) == 2  # different accounts are NOT a conflict


def test_sync_is_idempotent_per_account_month_name() -> None:
    session = build_session()
    repo = SqlAlchemyAdSensePaymentRepository(session, tenant_id=DEFAULT_TENANT_ID)
    first = repo.sync_payments(
        payments=[_payment(payment_amount=Decimal("930.000000"))],
        actor_user_id=ACTOR_USER_ID, source_report_id=None,
    )
    second = repo.sync_payments(
        payments=[_payment(payment_amount=Decimal("931.000000"))],
        actor_user_id=ACTOR_USER_ID, source_report_id=None,
    )
    session.commit()
    assert first[0].id == second[0].id  # same row updated in place
    assert second[0].source_account_id == "pub-1"


def test_within_batch_duplicate_keys_on_account_month_name() -> None:
    session = build_session()
    repo = SqlAlchemyAdSensePaymentRepository(session, tenant_id=DEFAULT_TENANT_ID)
    with pytest.raises(AdSensePaymentValidationError, match="duplicate"):
        repo.sync_payments(
            payments=[_payment(source_account_id="pub-1"),
                      _payment(source_account_id="pub-1")],
            actor_user_id=ACTOR_USER_ID, source_report_id=None,
        )
    # Same month+name but different accounts in one batch is allowed:
    repo.sync_payments(
        payments=[_payment(source_account_id="pub-1"),
                  _payment(source_account_id="pub-2")],
        actor_user_id=ACTOR_USER_ID, source_report_id=None,
    )


def test_sync_rejects_blank_source_account_id() -> None:
    session = build_session()
    repo = SqlAlchemyAdSensePaymentRepository(session, tenant_id=DEFAULT_TENANT_ID)
    with pytest.raises(AdSensePaymentValidationError, match="source_account_id"):
        repo.sync_payments(
            payments=[_payment(source_account_id="   ")],
            actor_user_id=ACTOR_USER_ID, source_report_id=None,
        )
    assert session.scalars(select(AdSensePaymentORM)).all() == []  # fail closed
```

- [ ] **Step 2 — Run, expect FAIL**
  Run: `python -m pytest -q tests/finance/test_adsense_payments_tenant_scope.py -x`
  Expected: errors — `AdSensePaymentInput.__init__() got an unexpected keyword
  argument 'source_account_id'` (dataclass lacks the field).

- [ ] **Step 3 — Add the field to the dataclasses** (`finance/adsense_payments.py`).
  `AdSensePaymentInput` gains `source_account_id: str` (place it first).
  `AdSensePaymentEntry` gains `source_account_id: str`; update:

```python
@property
def audit_entity_id(self) -> str:
    return f"{self.source_account_id}:{self.month}:{self.payment_name}"
```
  and add `"source_account_id": self.source_account_id,` to `to_api()`.

- [ ] **Step 4 — Validate + persist the field in the repository**
  (`finance/adsense_payments.py`). Five exact edits:

  **(a)** Rewrite `_normalize_payment` to normalize the new field (the repo only
  enforces non-blank; boundary callers canonicalize the `accounts/` prefix) and
  thread it into the returned input:

```python
def _normalize_payment(payment: AdSensePaymentInput) -> AdSensePaymentInput:
    _validate_month(payment.month)
    source_account_id = _normalize_required_string(
        payment.source_account_id, "source_account_id"
    )
    payment_name = _normalize_required_string(payment.payment_name, "payment_name")
    payment_currency = _normalize_currency(payment.payment_currency)
    payment_status = _normalize_payment_status(payment.payment_status)
    _validate_payment_amount(payment.payment_amount)
    if not isinstance(payment.raw_payload, dict):
        raise AdSensePaymentValidationError("raw_payload must be an object")
    return AdSensePaymentInput(
        source_account_id=source_account_id,
        month=payment.month,
        payment_name=payment_name,
        payment_date=payment.payment_date,
        payment_amount=payment.payment_amount,
        payment_currency=payment_currency,
        payment_status=payment_status,
        raw_payload=dict(payment.raw_payload),
    )
```

  **(b)** In `sync_payments`, widen the within-batch dedupe key to a 3-tuple.
  Change the declaration `seen_payment_keys: set[tuple[str, str]] = set()` to
  `set[tuple[str, str, str]]`, then replace the key build + duplicate error:

```python
payment_key = (
    normalized_payment.source_account_id,
    normalized_payment.month,
    normalized_payment.payment_name,
)
if payment_key in seen_payment_keys:
    raise AdSensePaymentValidationError(
        "duplicate AdSense payment in batch: "
        f"{normalized_payment.source_account_id}:"
        f"{normalized_payment.month}:{normalized_payment.payment_name}"
    )
```

  **(c)** In the insert `.values(...)` call, add the column immediately after the
  `payment_name=normalized_payment.payment_name,` line:

```python
source_account_id=normalized_payment.source_account_id,
```

  **(d)** Change the `on_conflict_do_update` conflict target to the new 4-column
  key (the migration in Task 2 creates the matching unique constraint):

```python
statement = insert_statement.on_conflict_do_update(
    index_elements=[
        AdSensePaymentORM.tenant_id,
        AdSensePaymentORM.source_account_id,
        AdSensePaymentORM.month,
        AdSensePaymentORM.payment_name,
    ],
    set_=update_values,
).returning(AdSensePaymentORM.id)
```

  **(e)** In `_to_entry`, add `source_account_id=row.source_account_id,` to the
  returned `AdSensePaymentEntry(...)`.

- [ ] **Step 5 — Update the ORM** (`db/finance_models.py`, `AdSensePaymentORM`).
  Add the column (after `payment_name`):

```python
source_account_id: Mapped[str] = mapped_column(Text, nullable=False)
```
  In `__table_args__`, replace the unique constraint and add a CHECK:

```python
UniqueConstraint(
    "tenant_id", "source_account_id", "month", "payment_name",
    name="uq_adsense_payments_account_month_name",
),
CheckConstraint(
    "length(source_account_id) >= 1",
    name="ck_adsense_payments_source_account_id_nonempty",
),
```

- [ ] **Step 6 — Run, expect PASS**
  Run: `python -m pytest -q tests/finance/test_adsense_payments_tenant_scope.py`
  Expected: all pass (existing tests now carry `source_account_id`; new tests
  green).
  Run: `python -m ruff check backend tests`
  Expected: clean.

- [ ] **Step 7 — Commit**
```bash
git add backend/ums_smart_revenue/finance/adsense_payments.py \
        backend/ums_smart_revenue/db/finance_models.py \
        tests/finance/test_adsense_payments_tenant_scope.py
git commit -m "feat(adsense-payments): add source_account_id to payment model + repo"
```

---

## Task 2 — Alembic migration + Postgres round-trip test

**Files:**
- Create: `backend/ums_smart_revenue/db/alembic/versions/20260529_0001_adsense_payment_source_account.py`
- Create: `tests/db/test_adsense_payment_source_account_migration_postgres.py`

- [ ] **Step 1 — Confirm the current head**
  Run: `python -m alembic heads` (from repo root with `alembic.ini`).
  Expected: a single head `20260527_0001` (verified 2026-05-29). Use it as
  `down_revision`; if the head has advanced since, use the new value.

- [ ] **Step 2 — Write the failing Postgres round-trip test** (mirror
  `tests/db/test_google_revenue_source_migration_postgres.py`'s
  `postgres_url`/`alembic_config`/`fresh_engine` fixtures via the
  `_postgres_helpers.require_postgres_url` import).

```python
def test_source_account_migration_rekeys_uniqueness(
    alembic_config, fresh_engine
) -> None:
    command.upgrade(alembic_config, "head")
    inspector = inspect(fresh_engine)
    cols = {c["name"]: c for c in inspector.get_columns("adsense_payments")}
    assert cols["source_account_id"]["nullable"] is False
    uniques = {
        c["name"]: tuple(c["column_names"])
        for c in inspector.get_unique_constraints("adsense_payments")
    }
    assert uniques["uq_adsense_payments_account_month_name"] == (
        "tenant_id", "source_account_id", "month", "payment_name",
    )
    assert "uq_adsense_payments_month_name" not in uniques
    checks = {c["name"] for c in inspector.get_check_constraints("adsense_payments")}
    assert "ck_adsense_payments_source_account_id_nonempty" in checks


def test_source_account_migration_downgrade_reverses(
    alembic_config, fresh_engine
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "-1")
    inspector = inspect(fresh_engine)
    cols = {c["name"] for c in inspector.get_columns("adsense_payments")}
    assert "source_account_id" not in cols
    uniques = {c["name"] for c in inspector.get_unique_constraints("adsense_payments")}
    assert "uq_adsense_payments_month_name" in uniques
```

- [ ] **Step 3 — Run, expect FAIL**
  Run: `UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:ums@localhost:55432/test_ums python -m pytest -q tests/db/test_adsense_payment_source_account_migration_postgres.py`
  Expected: FAIL — migration revision not found / column absent.

- [ ] **Step 4 — Write the migration** (batch mode for SQLite compatibility;
  nullable → backfill → NOT NULL):

```python
"""Add source_account_id to adsense_payments and re-key uniqueness.

Revision ID: 20260529_0001
Revises: 20260527_0001
Create Date: 2026-05-29
"""
import sqlalchemy as sa
from alembic import op

revision = "20260529_0001"
down_revision = "20260527_0001"  # current head, verified via `alembic heads`
branch_labels = None
depends_on = None

_LEGACY_SENTINEL = "__legacy_manual__"  # documented non-production backfill


def upgrade() -> None:
    # 1. Add nullable so existing pre-alpha rows can be backfilled.
    with op.batch_alter_table("adsense_payments") as batch:
        batch.add_column(sa.Column("source_account_id", sa.Text(), nullable=True))
    # 2. Backfill existing rows (manual uploads predating the live pull).
    op.execute(
        "UPDATE adsense_payments SET source_account_id = '__legacy_manual__' "
        "WHERE source_account_id IS NULL"
    )
    # 3. Enforce NOT NULL + non-empty, and re-key uniqueness to include account.
    with op.batch_alter_table("adsense_payments") as batch:
        batch.alter_column(
            "source_account_id", existing_type=sa.Text(), nullable=False
        )
        batch.create_check_constraint(
            "ck_adsense_payments_source_account_id_nonempty",
            "length(source_account_id) >= 1",
        )
        batch.drop_constraint("uq_adsense_payments_month_name", type_="unique")
        batch.create_unique_constraint(
            "uq_adsense_payments_account_month_name",
            ["tenant_id", "source_account_id", "month", "payment_name"],
        )


def downgrade() -> None:
    with op.batch_alter_table("adsense_payments") as batch:
        batch.drop_constraint(
            "uq_adsense_payments_account_month_name", type_="unique"
        )
        batch.create_unique_constraint(
            "uq_adsense_payments_month_name",
            ["tenant_id", "month", "payment_name"],
        )
        batch.drop_constraint(
            "ck_adsense_payments_source_account_id_nonempty", type_="check"
        )
        batch.drop_column("source_account_id")
```

- [ ] **Step 5 — Run, expect PASS**
  Run: `UMS_TEST_DATABASE_URL=... python -m pytest -q tests/db/test_adsense_payment_source_account_migration_postgres.py`
  Expected: both tests pass.
  Run: `python -m pytest -q tests/db/test_adsense_payment_migration.py`
  Expected: still passes (historical migration unchanged).

- [ ] **Step 6 — Commit**
```bash
git add backend/ums_smart_revenue/db/alembic/versions/20260529_0001_adsense_payment_source_account.py \
        tests/db/test_adsense_payment_source_account_migration_postgres.py
git commit -m "feat(adsense-payments): migration adds source_account_id + re-keys uniqueness"
```

---

## Task 3 — Manual endpoint requires `source_account_id`

**Files:**
- Modify: `backend/ums_smart_revenue/api/adsense.py` (`AdSensePaymentRequest`,
  handler)
- Test: `tests/api/test_adsense_payments_api.py` (extend)

- [ ] **Step 1 — Write failing API tests** (this file builds requests with the
  module-level `payment_payload()`, `auth_headers(...)`, `build_database_url`,
  `seed_database`, and `TestClient(create_app(database_url=...))`; mirror those
  exactly — there is no `client`/`sync_headers` fixture). First, add
  `"source_account_id": "pub-1"` to the single payment dict in the existing
  `payment_payload()` helper so the pre-existing endpoint tests keep passing.

```python
def test_manual_sync_requires_source_account_id(tmp_path) -> None:
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    body = payment_payload()
    del body["payments"][0]["source_account_id"]
    response = client.post(
        "/adsense/sync-payments",
        headers=auth_headers("system_integration_user", "connector", "adsense"),
        json=body,
    )
    assert response.status_code == 422  # Pydantic rejects before any DB write


def test_manual_sync_rejects_blank_source_account_id(tmp_path) -> None:
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    body = payment_payload()
    body["payments"][0]["source_account_id"] = "   "
    response = client.post(
        "/adsense/sync-payments",
        headers=auth_headers("system_integration_user", "connector", "adsense"),
        json=body,
    )
    assert response.status_code == 422


def test_manual_sync_canonicalizes_accounts_prefix(tmp_path) -> None:
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    body = payment_payload()
    body["payments"][0]["source_account_id"] = "accounts/pub-1"
    response = client.post(
        "/adsense/sync-payments",
        headers=auth_headers("system_integration_user", "connector", "adsense"),
        json=body,
    )
    assert response.status_code == 200
    # The `accounts/` prefix is stripped by the shared normalizer, so the
    # persisted/returned identity is the canonical bare publisher id.
    assert response.json()["items"][0]["source_account_id"] == "pub-1"


def test_manual_sync_rejects_malformed_account_path_chars(tmp_path) -> None:
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    body = payment_payload()
    body["payments"][0]["source_account_id"] = "pub/../etc"  # reserved '/'
    response = client.post(
        "/adsense/sync-payments",
        headers=auth_headers("system_integration_user", "connector", "adsense"),
        json=body,
    )
    assert response.status_code == 422  # MalformedAdsenseAccountIdError -> 422
```

- [ ] **Step 2 — Run, expect FAIL**
  Run: `python -m pytest -q tests/api/test_adsense_payments_api.py -x`
  Expected: FAIL — `payment_payload()` now sends a field the model rejects as
  extra (or the new canonicalization/reject tests get the wrong status).

- [ ] **Step 3 — Add the field + canonicalizing validator + handler thread.**
  In `api/adsense.py`, import the shared normalizer and its typed error at the
  top of the module:

```python
from ums_smart_revenue.connectors.google.adsense_management_client import (
    _validated_account_id,
)
from ums_smart_revenue.connectors.google.errors import (
    MalformedAdsenseAccountIdError,
)
```
  Add the field to `AdSensePaymentRequest` and canonicalize it at the boundary so
  a malformed id becomes a safe Pydantic `ValueError` (FastAPI renders 422) and a
  valid `accounts/{id}` is stored as the bare `{id}`:

```python
class AdSensePaymentRequest(BaseModel):
    source_account_id: str = Field(min_length=1)  # NEW (placed first)
    month: str
    payment_name: str = Field(min_length=1)
    payment_date: date
    payment_amount: Decimal = Field(ge=0)
    payment_currency: str = Field(min_length=1)
    payment_status: str = Field(default="PAID", min_length=1)
    raw_payload: dict[str, object] = Field(default_factory=dict)

    @field_validator(
        "month",
        "payment_name",
        "payment_currency",
        "payment_status",
        mode="before",
    )
    @classmethod
    def strip_required_strings(cls, value):
        return _strip_required_string(value)

    @field_validator("source_account_id", mode="before")  # NEW validator
    @classmethod
    def canonicalize_source_account_id(cls, value):
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("source_account_id must not be blank")
        try:
            # Same canonical convention as the live pull and Google source rows:
            # strip `accounts/`, reject blank/whitespace-padded/reserved chars.
            return _validated_account_id(stripped)
        except MalformedAdsenseAccountIdError as exc:
            raise ValueError(str(exc)) from exc
```
  In the handler's per-payment `AdSensePaymentInput(...)` construction
  (`api/adsense.py:104`), add `source_account_id=payment.source_account_id,` as
  the first argument. (No `strip_required_strings` entry is needed for this field
  — `canonicalize_source_account_id` already strips and validates it.)

- [ ] **Step 4 — Run, expect PASS**
  Run: `python -m pytest -q tests/api/test_adsense_payments_api.py`
  Expected: all pass (existing endpoint tests now carry `source_account_id`; the
  four new tests green).
  Run: `python -m ruff check backend tests`
  Expected: clean.

- [ ] **Step 5 — Commit**
```bash
git add backend/ums_smart_revenue/api/adsense.py tests/api/test_adsense_payments_api.py
git commit -m "feat(adsense-payments): require + canonicalize source_account_id on manual sync"
```

---

## Task 4 — Connector-key constant + public credential resolver

**Files:**
- Modify: `backend/ums_smart_revenue/connectors/google/registry.py`
- Modify: `backend/ums_smart_revenue/connectors/runs/orchestrator.py:442`
- Test: `tests/connectors/google/test_adsense_payment_sync.py` (asserts the
  constant value + that the public name resolves)

- [ ] **Step 1 — Write a failing import/identity test** (new test file, first
  case):

```python
def test_connector_key_constant_is_canonical() -> None:
    from ums_smart_revenue.connectors.google.registry import (
        ADSENSE_MANAGEMENT_CONNECTOR_KEY,
    )
    assert ADSENSE_MANAGEMENT_CONNECTOR_KEY == "adsense-management"


def test_resolve_connector_credentials_is_public() -> None:
    from ums_smart_revenue.connectors.runs.orchestrator import (
        resolve_connector_credentials,
    )
    assert callable(resolve_connector_credentials)
```

- [ ] **Step 2 — Run, expect FAIL** (ImportError).
  Run: `python -m pytest -q tests/connectors/google/test_adsense_payment_sync.py -x`

- [ ] **Step 3 — Add the constant** to `registry.py`:
```python
ADSENSE_MANAGEMENT_CONNECTOR_KEY = "adsense-management"
```

- [ ] **Step 4 — Promote the resolver** in `orchestrator.py`. Rename the
  existing function definition at `orchestrator.py:442` from
  `_credentials_for_run` to `resolve_connector_credentials`, leaving its
  keyword-only signature, type hints, docstring, and body byte-for-byte
  identical — the only edit on that line is the name:

```python
def resolve_connector_credentials(
    *,
    session: Session,
    tenant_id: UUID,
    connector_key: str,
    account_id: str,
) -> Credentials:
    """Resolve and validate the credential row for a given tenant/connector/account."""
    credential = _load_credential(
        session,
        tenant_id=tenant_id,
        connector_key=connector_key,
        account_id=account_id,
    )
    if credential is None:
        raise CredentialNotFoundError(
            connector_key=connector_key, account_id=account_id
        )
    if credential.status != "active":
        raise InactiveCredentialError(
            credential_id=str(credential.id), status=credential.status
        )

    ensure_default_resolvers()
    # FIX: Admin/API-created credentials may persist surrounding whitespace in
    # the secret URI. Normalize before resolver dispatch so valid refs do not
    # fail scheme lookup.
    payload = resolve_secret(credential.encrypted_secret_ref.strip())
    credentials = build_credentials_from_payload(payload)
    refresh_credentials(credentials)
    return credentials
```
  (This is the current `_credentials_for_run` body verbatim — only the `def`
  name changes.) Then, immediately after the function, add a backwards-compatible
  alias so the
  existing internal call site(s) (e.g. `orchestrator.py:390`) keep working with
  zero edits:

```python
# Backwards-compatible internal alias (existing call sites unchanged).
_credentials_for_run = resolve_connector_credentials
```

- [ ] **Step 5 — Run, expect PASS** (the two cases) and confirm no regression:
  Run: `python -m pytest -q tests/connectors/ tests/connectors/runs -q`
  Expected: pass (orchestrator behavior unchanged).

- [ ] **Step 6 — Commit**
```bash
git add backend/ums_smart_revenue/connectors/google/registry.py \
        backend/ums_smart_revenue/connectors/runs/orchestrator.py \
        tests/connectors/google/test_adsense_payment_sync.py
git commit -m "refactor(connectors): canonical adsense key constant + public credential resolver"
```

---

## Task 5 — `GoogleAdSensePaymentClient`

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google/adsense_payments_client.py`
- Test: `tests/connectors/google/test_adsense_payments_client.py`

- [ ] **Step 1 — Write failing client tests** (use a fake `GoogleHttpClient`
  exposing `request(*, method, url, params=None, json_body=None)` returning a
  canned dict):

```python
class _FakeHttp:
    def __init__(self, response): self.response = response; self.calls = []
    def request(self, *, method, url, params=None, json_body=None):
        self.calls.append((method, url, params)); return self.response


def test_fetch_payments_calls_correct_endpoint() -> None:
    http = _FakeHttp({"payments": []})
    client = GoogleAdSensePaymentClient(http=http)
    result = client.fetch_payments(account_id="pub-123")
    method, url, _ = http.calls[0]
    assert method == "GET"
    assert url == "https://adsense.googleapis.com/v2/accounts/pub-123/payments"
    assert "report_id" in result and isinstance(result["report_id"], str)
    assert result["payments"] == []


def test_fetch_payments_strips_accounts_prefix() -> None:
    http = _FakeHttp({"payments": []})
    GoogleAdSensePaymentClient(http=http).fetch_payments(
        account_id="accounts/pub-123"
    )
    assert "accounts/pub-123/payments" in http.calls[0][1]


def test_fetch_payments_rejects_blank_account() -> None:
    http = _FakeHttp({"payments": []})
    with pytest.raises(MalformedAdsenseAccountIdError):
        GoogleAdSensePaymentClient(http=http).fetch_payments(account_id="  ")


def test_fetch_payments_rejects_non_object_response() -> None:
    http = _FakeHttp([])  # list, not an object
    with pytest.raises(GoogleApiResponseError):
        GoogleAdSensePaymentClient(http=http).fetch_payments(account_id="pub-1")


def test_fetch_payments_treats_missing_payments_as_empty_list() -> None:
    http = _FakeHttp({"notpayments": []})  # object, but no payments list
    result = GoogleAdSensePaymentClient(http=http).fetch_payments(account_id="pub-1")
    assert result["payments"] == []


def test_fetch_payments_rejects_non_list_payments() -> None:
    http = _FakeHttp({"payments": {"oops": 1}})  # payments present but not a list
    with pytest.raises(GoogleApiResponseError):
        GoogleAdSensePaymentClient(http=http).fetch_payments(account_id="pub-1")


def test_report_id_is_deterministic_per_account() -> None:
    http = _FakeHttp({"payments": []})
    a = GoogleAdSensePaymentClient(http=http).fetch_payments(account_id="pub-1")
    b = GoogleAdSensePaymentClient(http=http).fetch_payments(account_id="pub-1")
    assert a["report_id"] == b["report_id"]
```

- [ ] **Step 2 — Run, expect FAIL** (module does not exist).

- [ ] **Step 3 — Implement the client** (reuse the canonical
  `_validated_account_id` from `adsense_management_client.py`):

```python
import hashlib
from ums_smart_revenue.connectors.google.adsense_management_client import (
    _validated_account_id,
)
from ums_smart_revenue.connectors.google.errors import GoogleApiResponseError
from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient

_PAYMENTS_URL = "https://adsense.googleapis.com/v2/accounts/{account}/payments"
_REPORT_KEY = "accounts.payments.list"


class GoogleAdSensePaymentClient:
    def __init__(self, *, http: GoogleHttpClient) -> None:
        self._http = http

    def fetch_payments(self, *, account_id: str) -> dict[str, object]:
        account = _validated_account_id(account_id)
        url = _PAYMENTS_URL.format(account=account)
        response = self._http.request(method="GET", url=url)
        # GoogleHttpClient.request already guarantees a dict on the real path,
        # but a fake/transport that bypasses it must still fail closed here.
        if not isinstance(response, dict):
            raise GoogleApiResponseError(
                url=url, reason="payments response is not an object"
            )
        # Google may omit repeated fields when empty; normalize that omission
        # to [] while still rejecting a present field with the wrong type.
        payments = response.get("payments", [])
        if not isinstance(payments, list):
            raise GoogleApiResponseError(
                url=url,
                reason=f"expected 'payments' list, got {type(payments).__name__}",
            )
        report_id = hashlib.sha256(
            f"{account}|{_REPORT_KEY}".encode()
        ).hexdigest()
        return {
            **response,
            "payments": payments,
            "account_id": account,
            "report_id": report_id,
        }
```

- [ ] **Step 4 — Run, expect PASS**, then `python -m ruff check backend tests`.

- [ ] **Step 5 — Commit**
```bash
git add backend/ums_smart_revenue/connectors/google/adsense_payments_client.py \
        tests/connectors/google/test_adsense_payments_client.py
git commit -m "feat(adsense-payments): add GoogleAdSensePaymentClient (payments.list)"
```

---

## Task 6 — Pure mapping module (classify, month, parse)

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google/adsense_payment_mapping.py`
- Test: `tests/connectors/google/test_adsense_payment_mapping.py`

Pure module (no I/O, no DB). A paid settlement has resource name
`accounts/{account}/payments/<suffix>` where `<suffix>` strips an optional
`youtube-` prefix and the remainder parses as `YYYY-MM-DD` equal to
`Payment.date`; otherwise it is a balance (`unpaid`/`youtube-unpaid`, no date).
`month` = `Payment.date`'s `YYYY-MM`. `parse_amount` is invoked by the service
for **open-month** settlements only (Task 8), never inside `classify_payments`.

- [ ] **Step 1 — Write failing mapping tests** (new file
  `tests/connectors/google/test_adsense_payment_mapping.py`):

```python
from datetime import date
from decimal import Decimal

import pytest

from ums_smart_revenue.connectors.google.adsense_payment_mapping import (
    AdSensePaymentMappingError,
    classify_payments,
    parse_amount,
)


def _resp(*payments, account="pub-1"):
    return {"payments": list(payments), "account_id": account}


def _p(name, date_obj, amount):
    d = {"name": name, "amount": amount}
    if date_obj is not None:
        d["date"] = {
            "year": date_obj.year, "month": date_obj.month, "day": date_obj.day,
        }
    return d


def test_classify_skips_unpaid_balances() -> None:
    resp = _resp(
        _p("accounts/pub-1/payments/unpaid", None, "$10.00"),
        _p("accounts/pub-1/payments/youtube-unpaid", None, "$5.00"),
    )
    out = classify_payments(resp, account_id="pub-1")
    assert out.paid == []
    assert {b.reason for b in out.skipped_balances} == {"no_payment_date"}
    assert {b.raw_amount for b in out.skipped_balances} == {"$10.00", "$5.00"}


def test_classify_accepts_paid_and_youtube_paid() -> None:
    resp = _resp(
        _p("accounts/pub-1/payments/2026-04-21", date(2026, 4, 21), "£100.00"),
        _p("accounts/pub-1/payments/youtube-2026-04-21", date(2026, 4, 21), "£5.00"),
    )
    out = classify_payments(resp, account_id="pub-1")
    assert {s.month for s in out.paid} == {"2026-04"}
    assert {s.payment_name for s in out.paid} == {"2026-04-21", "youtube-2026-04-21"}
    assert {s.source_account_id for s in out.paid} == {"pub-1"}


def test_classify_fails_when_suffix_date_disagrees_with_payment_date() -> None:
    resp = _resp(_p("accounts/pub-1/payments/2026-04-21", date(2026, 4, 22), "£1.00"))
    with pytest.raises(AdSensePaymentMappingError, match="disagrees"):
        classify_payments(resp, account_id="pub-1")


def test_classify_fails_when_dated_settlement_has_no_date() -> None:
    resp = _resp(_p("accounts/pub-1/payments/2026-04-21", None, "£1.00"))
    with pytest.raises(AdSensePaymentMappingError, match="missing Payment.date"):
        classify_payments(resp, account_id="pub-1")


def test_classify_fails_on_account_mismatch() -> None:
    resp = _resp(_p("accounts/pub-OTHER/payments/unpaid", None, "$1.00"))
    with pytest.raises(AdSensePaymentMappingError, match="account"):
        classify_payments(resp, account_id="pub-1")


def test_classify_fails_on_unrecognized_name_form() -> None:
    resp = _resp(_p("accounts/pub-1/payments/weird-suffix", None, "$1.00"))
    with pytest.raises(AdSensePaymentMappingError, match="unrecognized"):
        classify_payments(resp, account_id="pub-1")


def test_classify_rejects_non_list_payments() -> None:
    with pytest.raises(AdSensePaymentMappingError, match="list"):
        classify_payments({"payments": {"oops": 1}}, account_id="pub-1")


@pytest.mark.parametrize("raw,expected", [
    ("£87.65", (Decimal("87.65"), "GBP")),
    ("€87.65", (Decimal("87.65"), "EUR")),
    ("¥1,235 JPY", (Decimal("1235"), "JPY")),
    ("1,234.57 USD", (Decimal("1234.57"), "USD")),
])
def test_parse_amount_accepts(raw, expected) -> None:
    assert parse_amount(raw) == expected


@pytest.mark.parametrize("raw", ["$1,234.57", "¥1,235", "1.2.3 GBP", "-5.00 GBP", "kr 5", ""])
def test_parse_amount_fails_closed(raw) -> None:
    with pytest.raises(AdSensePaymentMappingError):
        parse_amount(raw)
```

- [ ] **Step 2 — Run, expect FAIL** (module missing).
  Run: `python -m pytest -q tests/connectors/google/test_adsense_payment_mapping.py -x`
  Expected: `ModuleNotFoundError: ums_smart_revenue.connectors.google.adsense_payment_mapping`.

- [ ] **Step 3 — Implement `adsense_payment_mapping.py`** verbatim:

```python
"""Pure AdSense ``payments.list`` -> repository-input mapping (no I/O, no DB).

Splits Google ``Payment[]`` into paid settlements and retained balances,
derives the settlement month, enforces the resource-name-date / ``Payment.date``
agreement, and parses the formatted amount string into ``(Decimal, ISO)`` with a
fail-closed currency allowlist. ``parse_amount`` is called by the sync service
for OPEN-month settlements only, never inside ``classify_payments``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

# accounts/{account}/payments/{suffix}; the suffix carries the type/status.
_RESOURCE_NAME_RE = re.compile(
    r"^accounts/(?P<account>[^/]+)/payments/(?P<suffix>.+)$"
)
# Paid settlement suffix: [youtube-]YYYY-MM-DD.
_DATE_SUFFIX_RE = re.compile(r"^(?:youtube-)?(?P<date>\d{4}-\d{2}-\d{2})$")
# Running balance suffix: [youtube-]unpaid (no date).
_BALANCE_SUFFIX_RE = re.compile(r"^(?:youtube-)?unpaid$")
# An explicit ISO 4217 code anywhere in the string wins over symbols.
_ISO_CODE_RE = re.compile(r"\b([A-Z]{3})\b")
# Only unambiguous symbols are accepted; $, ¥, kr, etc. are intentionally absent.
_SYMBOL_CURRENCIES: dict[str, str] = {"£": "GBP", "€": "EUR"}
# Plain decimal: optional 3-digit thousands groups, optional fractional part.
_NUMBER_RE = re.compile(r"^(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?$")


class AdSensePaymentMappingError(ValueError):
    """Raised when a Payment cannot be safely mapped (fail-closed)."""


@dataclass(frozen=True)
class SkippedBalance:
    resource_name: str
    raw_amount: str
    reason: str  # "no_payment_date" for unpaid / youtube-unpaid balances


@dataclass(frozen=True)
class PaidSettlement:
    source_account_id: str
    month: str          # YYYY-MM derived from Payment.date
    payment_name: str   # raw resource-name suffix, e.g. "2026-04-21"
    payment_date: date
    raw_amount: str     # raw formatted string, preserved into raw_payload
    resource_name: str  # full "accounts/{account}/payments/{suffix}"


@dataclass(frozen=True)
class ClassifiedPayments:
    paid: list[PaidSettlement]
    skipped_balances: list[SkippedBalance]


# ============================================================================
# Purpose: Classify each Google Payment into a paid settlement or a retained
#   balance, deriving the settlement month and enforcing fail-closed identity
#   and date-agreement invariants before any persistence.
# Database/ORM: None (pure mapping; no I/O).
# Standards: Typed AdSensePaymentMappingError on every unsafe shape; no silent
#   skips of dated settlements; raw formatted amount preserved for raw_payload.
# Blast Radius: Feeds the AdSense payment sync service inputs only. A drift here
#   would mis-attribute or drop a real payment from the source of truth.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/adsense_payment_sync.py
#     -> consumes ClassifiedPayments and calls parse_amount for open months.
# ============================================================================
def classify_payments(
    response: dict[str, object], *, account_id: str
) -> ClassifiedPayments:
    """Split payments into paid settlements vs retained balances (fail-closed)."""
    if not isinstance(response, dict):
        raise AdSensePaymentMappingError("payments response must be an object")
    raw_payments = response.get("payments")
    if not isinstance(raw_payments, list):
        raise AdSensePaymentMappingError("payments field must be a list")

    paid: list[PaidSettlement] = []
    skipped: list[SkippedBalance] = []
    for entry in raw_payments:
        if not isinstance(entry, dict):
            raise AdSensePaymentMappingError("each payment must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise AdSensePaymentMappingError("payment.name must be a non-empty string")
        raw_amount = entry.get("amount")
        if not isinstance(raw_amount, str):
            raise AdSensePaymentMappingError(
                f"payment.amount must be a string for {name!r}"
            )
        suffix = _resource_suffix(name, account_id)

        if _BALANCE_SUFFIX_RE.fullmatch(suffix):
            # unpaid / youtube-unpaid carry no settlement date -> never a row.
            skipped.append(
                SkippedBalance(
                    resource_name=name,
                    raw_amount=raw_amount,
                    reason="no_payment_date",
                )
            )
            continue

        date_match = _DATE_SUFFIX_RE.fullmatch(suffix)
        if date_match is None:
            raise AdSensePaymentMappingError(
                f"unrecognized payment name form: {name!r}"
            )
        payment_date = _parse_google_date(entry.get("date"), name)
        suffix_date = _parse_iso_date(date_match.group("date"), name)
        if suffix_date != payment_date:
            raise AdSensePaymentMappingError(
                f"resource-name date {suffix_date} disagrees with "
                f"Payment.date {payment_date} for {name!r}"
            )
        paid.append(
            PaidSettlement(
                source_account_id=account_id,
                month=f"{payment_date.year:04d}-{payment_date.month:02d}",
                payment_name=suffix,
                payment_date=payment_date,
                raw_amount=raw_amount,
                resource_name=name,
            )
        )

    return ClassifiedPayments(paid=paid, skipped_balances=skipped)


# ============================================================================
# Purpose: Parse a Google formatted amount string into a non-negative Decimal
#   and an ISO 4217 currency, fail-closed. Explicit ISO code wins; otherwise an
#   unambiguous allowlisted symbol; bare ambiguous symbols ($, ¥, kr, ...) fail.
# Database/ORM: None.
# Standards: No global "$ -> USD" assumption; deterministic Decimal; negatives
#   and malformed/multi-separator numbers fail closed.
# Blast Radius: Determines the stored amount/currency of a real payment.
# ============================================================================
def parse_amount(raw_amount: str) -> tuple[Decimal, str]:
    """Return ``(Decimal amount, ISO currency)`` or raise (fail-closed)."""
    if not isinstance(raw_amount, str):
        raise AdSensePaymentMappingError(
            f"amount must be a string, got {type(raw_amount).__name__}"
        )
    text = raw_amount.strip()
    if not text:
        raise AdSensePaymentMappingError("amount string is empty")
    if "-" in text:
        # Negative settlements are not valid paid rows; reject before parsing.
        raise AdSensePaymentMappingError(
            f"amount must be non-negative: {raw_amount!r}"
        )

    iso = _ISO_CODE_RE.search(text)
    if iso is not None:
        currency = iso.group(1)
        remainder = (text[: iso.start()] + text[iso.end():]).strip()
        # The explicit ISO code is authoritative. Strip at most ONE leading
        # currency symbol (e.g. the "¥" in "¥1,235 JPY") plus surrounding
        # whitespace -- do NOT delete embedded chars, so junk like "1e3"/"1 234"
        # reaches _NUMBER_RE below and fails closed. (Corrected during
        # implementation: an earlier re.sub(r"[^0-9.,]", "", remainder) deleted
        # all non-numeric chars and silently fabricated amounts.)
        number = re.sub(r"^[^\d.,\s]?\s*", "", remainder)
    else:
        currency = ""
        number = ""
        for symbol, code in _SYMBOL_CURRENCIES.items():
            if text.startswith(symbol):
                currency = code
                number = text[len(symbol):].strip()
                break
        if not currency:
            # No ISO code and no allowlisted symbol -> ambiguous ($, ¥, kr, ...).
            raise AdSensePaymentMappingError(
                f"unresolved/ambiguous currency in amount: {raw_amount!r}"
            )

    if not _NUMBER_RE.fullmatch(number):
        raise AdSensePaymentMappingError(
            f"unparseable amount number: {raw_amount!r}"
        )
    try:
        amount = Decimal(number.replace(",", ""))
    except InvalidOperation as exc:
        raise AdSensePaymentMappingError(
            f"unparseable amount: {raw_amount!r}"
        ) from exc
    if not amount.is_finite() or amount < 0:
        raise AdSensePaymentMappingError(
            f"amount must be a finite value >= 0: {raw_amount!r}"
        )
    return amount, currency


def _resource_suffix(name: str, account_id: str) -> str:
    """Return the resource-name suffix after a fail-closed account match."""
    match = _RESOURCE_NAME_RE.fullmatch(name)
    if match is None:
        raise AdSensePaymentMappingError(
            f"payment.name is not a valid resource name: {name!r}"
        )
    if match.group("account") != account_id:
        # The resource name's account must equal the account we pulled for,
        # else the row's identity cannot be trusted.
        raise AdSensePaymentMappingError(
            f"payment.name account {match.group('account')!r} "
            f"!= requested account {account_id!r}"
        )
    return match.group("suffix")


def _parse_google_date(value: object, name: str) -> date:
    """Parse the ``google.type.Date`` object ({year,month,day}) for a settlement."""
    if not isinstance(value, dict):
        raise AdSensePaymentMappingError(
            f"paid settlement {name!r} is missing Payment.date"
        )
    try:
        return date(int(value["year"]), int(value["month"]), int(value["day"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise AdSensePaymentMappingError(
            f"invalid Payment.date for {name!r}"
        ) from exc


def _parse_iso_date(text: str, name: str) -> date:
    """Parse the YYYY-MM-DD resource-name date suffix."""
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise AdSensePaymentMappingError(
            f"invalid date suffix for {name!r}"
        ) from exc
```

- [ ] **Step 4 — Run, expect PASS**
  Run: `python -m pytest -q tests/connectors/google/test_adsense_payment_mapping.py`
  Expected: all pass.
  Run: `python -m ruff check backend tests`
  Expected: clean.

- [ ] **Step 5 — Commit**
```bash
git add backend/ums_smart_revenue/connectors/google/adsense_payment_mapping.py \
        tests/connectors/google/test_adsense_payment_mapping.py
git commit -m "feat(adsense-payments): pure payments mapping + fail-closed amount parse"
```

---

## Task 7 — Read-only `get_month_close_status`

**Files:**
- Modify: `backend/ums_smart_revenue/finance/month_close.py`
- Test: `tests/finance/test_month_close_status.py`

The close ORM is `FinanceMonthCloseORM` (`db/finance_models.py`), keyed
`(tenant_id, month)` with a `status` column whose values are `"OPEN"` / `"LOCKED"`.
The module already imports `select` and `FinanceMonthCloseORM` and defines the
`_resolve_tenant_id(...)` helper and `get_or_create_month_close_row(...)`.

- [ ] **Step 1 — Write failing tests** (new file
  `tests/finance/test_month_close_status.py`):

```python
from uuid import UUID

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceBase, FinanceMonthCloseORM
from ums_smart_revenue.finance.month_close import (
    get_month_close_status,
    get_or_create_month_close_row,
)

TENANT_ID = UUID("00000000-0000-0000-0000-000000031001")


def build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)
    return Session(engine)


def _lock_month(session: Session, month: str, *, tenant_id: UUID) -> None:
    # Drive the close row straight to LOCKED for a read-only accessor test,
    # bypassing the readiness-gated lock_month() machinery on purpose.
    row = get_or_create_month_close_row(
        session, month, tenant_id=tenant_id, for_update=False
    )
    row.status = "LOCKED"
    session.flush()


def test_status_none_when_no_row() -> None:
    session = build_session()
    assert get_month_close_status(session, "2026-04", tenant_id=TENANT_ID) is None
    # Read-only: the accessor must NOT have created a close row.
    assert session.scalars(select(FinanceMonthCloseORM)).all() == []


def test_status_open_when_row_open() -> None:
    session = build_session()
    get_or_create_month_close_row(
        session, "2026-04", tenant_id=TENANT_ID, for_update=False
    )
    assert get_month_close_status(session, "2026-04", tenant_id=TENANT_ID) == "OPEN"


def test_status_reflects_locked() -> None:
    session = build_session()
    _lock_month(session, "2026-04", tenant_id=TENANT_ID)
    assert get_month_close_status(session, "2026-04", tenant_id=TENANT_ID) == "LOCKED"
```

- [ ] **Step 2 — Run, expect FAIL** (function missing).
  Run: `python -m pytest -q tests/finance/test_month_close_status.py -x`
  Expected: `ImportError: cannot import name 'get_month_close_status'`.

- [ ] **Step 3 — Implement** a pure SELECT in `finance/month_close.py`, placed
  directly after `get_or_create_month_close_row` (no row creation, no
  `FOR UPDATE`):

```python
# ============================================================================
# Purpose: Read-only finance month-close status lookup for the AdSense live
#   payment prefilter. Returns the close status or None for an absent row.
# Database/ORM: FinanceMonthCloseORM (SELECT only).
# Standards: Pure SELECT — no row creation, no advisory/FOR UPDATE lock, so the
#   prefilter never mutates close state or contends with month writers.
# Blast Radius: Finance month locks (read-only). The authoritative locked-month
#   write guard remains _require_month_open(..., for_update=True) in the repo.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/adsense_payment_sync.py
#     -> step-2 prefilter classifies OPEN vs LOCKED settlement months.
# ============================================================================
def get_month_close_status(
    session: Session, month: str, *, tenant_id: UUID | str | None = None
) -> str | None:
    """Return the finance close status for ``month`` (``"OPEN"``/``"LOCKED"``) or None."""
    resolved_tenant_id = _resolve_tenant_id(tenant_id)
    return session.scalar(
        select(FinanceMonthCloseORM.status).where(
            FinanceMonthCloseORM.tenant_id == resolved_tenant_id,
            FinanceMonthCloseORM.month == month,
        )
    )
```

- [ ] **Step 4 — Run, expect PASS**
  Run: `python -m pytest -q tests/finance/test_month_close_status.py`
  Expected: all pass.
  Run: `python -m ruff check backend tests`
  Expected: clean.

- [ ] **Step 5 — Commit**
```bash
git add backend/ums_smart_revenue/finance/month_close.py tests/finance/test_month_close_status.py
git commit -m "feat(finance): read-only get_month_close_status accessor"
```

---

## Task 8 — `AdSensePaymentSyncService`

**Files:**
- Create: `backend/ums_smart_revenue/connectors/google/adsense_payment_sync.py`
- Test: `tests/connectors/google/test_adsense_payment_sync.py` (extend Task 4 file)

Pipeline (spec §5), inside one DB transaction scope: (1) resolve credentials
under `ADSENSE_MANAGEMENT_CONNECTOR_KEY`; (2) single `fetch_payments` GET;
(3) `classify_payments`; (4) read-only `get_month_close_status` prefilter —
`None`/`"OPEN"` → open, `"LOCKED"` → skip with evidence; (5) `parse_amount` for
open settlements only (any failure raises → abort, zero writes), then
`sync_payments` (skipped when nothing remains) and one `ADSENSE_PAYMENT_SYNCED`
audit. `dry_run=True` returns the would-sync counts with **no** persistence and
**no** audit. `sync_payments` stays the authoritative locked-month race guard.

- [ ] **Step 1 — Write failing service tests** (extend the Task 4 file
  `tests/connectors/google/test_adsense_payment_sync.py` with these helpers +
  tests; uses a real in-memory `FinanceBase` session and the
  `InMemoryAuditSink`):

```python
from datetime import date
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.connectors.google.adsense_payment_mapping import (
    AdSensePaymentMappingError,
)
from ums_smart_revenue.connectors.google.adsense_payment_sync import (
    AdSensePaymentSyncService,
)
from ums_smart_revenue.connectors.google.errors import CredentialNotFoundError
from ums_smart_revenue.db.finance_models import (
    AdSensePaymentORM,
    FinanceBase,
)
from ums_smart_revenue.finance.month_close import get_or_create_month_close_row

TENANT_ID = UUID("00000000-0000-0000-0000-000000031001")
ACTOR = UserPrincipal(
    user_id="00000000-0000-0000-0000-000000031001",
    email="connector-service@ums.example",
)


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)
    return Session(engine)


def _resp(*payments, account="pub-1"):
    # Mirrors GoogleAdSensePaymentClient.fetch_payments output (account_id +
    # report_id already stamped) so the fake stands in for the real client.
    return {"payments": list(payments), "account_id": account, "report_id": "rep-abc"}


def _p(name, date_obj, amount):
    d = {"name": name, "amount": amount}
    if date_obj is not None:
        d["date"] = {
            "year": date_obj.year, "month": date_obj.month, "day": date_obj.day,
        }
    return d


class _FakeClient:
    def __init__(self, response):
        self._response = response

    def fetch_payments(self, *, account_id):
        return self._response


def _lock_month(session, month):
    row = get_or_create_month_close_row(
        session, month, tenant_id=TENANT_ID, for_update=False
    )
    row.status = "LOCKED"
    session.flush()


def _service(session, client, audit):
    return AdSensePaymentSyncService(
        session,
        audit_sink=audit,
        credential_resolver=lambda **_: object(),  # no real OAuth
        client_factory=lambda _creds: client,       # no real HTTP
    )


def test_sync_upserts_open_month_settlements() -> None:
    session = _build_session()
    audit = InMemoryAuditSink()
    client = _FakeClient(_resp(
        _p("accounts/pub-1/payments/2026-04-10", date(2026, 4, 10), "£60.00"),
        _p("accounts/pub-1/payments/unpaid", None, "£5.00"),
    ))
    result = _service(session, client, audit).sync(
        tenant_id=TENANT_ID, account_id="pub-1", actor=ACTOR, reason="live pull",
    )
    session.commit()
    assert result.synced_count == 1
    assert result.skipped_balance_count == 1
    rows = session.scalars(select(AdSensePaymentORM)).all()
    assert len(rows) == 1
    assert rows[0].source_account_id == "pub-1"
    assert rows[0].payment_status == "PAID"
    assert rows[0].payment_currency == "GBP"
    assert rows[0].source_report_id == "rep-abc"
    # raw_payload retains the raw formatted amount + the Google resource name.
    assert rows[0].raw_payload["amount"] == "£60.00"
    assert rows[0].raw_payload["name"] == "accounts/pub-1/payments/2026-04-10"
    # audit carries the live-pull discriminator + capped skip evidence.
    assert len(audit.records) == 1
    details = audit.records[0].details
    assert details["trigger"] == "live_pull"
    assert details["source_account_id"] == "pub-1"
    assert details["skipped_balances"][0]["resource_name"] == (
        "accounts/pub-1/payments/unpaid"
    )


def test_sync_is_idempotent() -> None:
    session = _build_session()
    audit = InMemoryAuditSink()
    client = _FakeClient(_resp(
        _p("accounts/pub-1/payments/2026-04-10", date(2026, 4, 10), "£60.00"),
    ))
    svc = _service(session, client, audit)
    svc.sync(tenant_id=TENANT_ID, account_id="pub-1", actor=ACTOR, reason="r")
    svc.sync(tenant_id=TENANT_ID, account_id="pub-1", actor=ACTOR, reason="r")
    session.commit()
    assert len(session.scalars(select(AdSensePaymentORM)).all()) == 1


def test_locked_month_is_skipped_not_aborted() -> None:
    session = _build_session()
    audit = InMemoryAuditSink()
    # one paid row in a LOCKED month ('$' ambiguous) + one in an OPEN month (GBP)
    client = _FakeClient(_resp(
        _p("accounts/pub-1/payments/2026-03-10", date(2026, 3, 10), "$50.00"),
        _p("accounts/pub-1/payments/2026-04-10", date(2026, 4, 10), "£60.00"),
    ))
    _lock_month(session, "2026-03")
    result = _service(session, client, audit).sync(
        tenant_id=TENANT_ID, account_id="pub-1", actor=ACTOR, reason="r",
    )
    session.commit()
    assert result.skipped_locked_count == 1   # the '$' locked row never parsed
    assert result.synced_count == 1           # only the open GBP row
    rows = session.scalars(select(AdSensePaymentORM)).all()
    assert {r.month for r in rows} == {"2026-04"}
    locked_meta = audit.records[0].details["skipped_locked"][0]
    assert locked_meta["month"] == "2026-03"
    assert locked_meta["reason"] == "month_locked"
    assert locked_meta["raw_amount"] == "$50.00"   # raw preserved, never parsed


def test_nothing_remains_audits_zero_synced() -> None:
    session = _build_session()
    audit = InMemoryAuditSink()
    client = _FakeClient(_resp(
        _p("accounts/pub-1/payments/unpaid", None, "£5.00"),
        _p("accounts/pub-1/payments/youtube-unpaid", None, "£3.00"),
    ))
    result = _service(session, client, audit).sync(
        tenant_id=TENANT_ID, account_id="pub-1", actor=ACTOR, reason="r",
    )
    session.commit()
    assert result.synced_count == 0
    assert result.skipped_balance_count == 2
    assert session.scalars(select(AdSensePaymentORM)).all() == []  # no payment rows
    assert len(audit.records) == 1                                 # still audited
    assert audit.records[0].details["synced_count"] == 0


def test_credential_failure_writes_nothing() -> None:
    session = _build_session()
    audit = InMemoryAuditSink()

    def _boom(**_):
        raise CredentialNotFoundError(
            connector_key="adsense-management", account_id="pub-1"
        )

    svc = AdSensePaymentSyncService(
        session,
        audit_sink=audit,
        credential_resolver=_boom,
        client_factory=lambda _creds: _FakeClient(_resp()),
    )
    with pytest.raises(CredentialNotFoundError):
        svc.sync(tenant_id=TENANT_ID, account_id="pub-1", actor=ACTOR, reason="r")
    assert session.scalars(select(AdSensePaymentORM)).all() == []
    assert audit.records == []


def test_open_month_dollar_amount_aborts_with_zero_writes() -> None:
    session = _build_session()
    audit = InMemoryAuditSink()
    client = _FakeClient(_resp(
        _p("accounts/pub-1/payments/2026-04-10", date(2026, 4, 10), "$60.00"),
    ))
    with pytest.raises(AdSensePaymentMappingError):
        _service(session, client, audit).sync(
            tenant_id=TENANT_ID, account_id="pub-1", actor=ACTOR, reason="r",
        )
    assert session.scalars(select(AdSensePaymentORM)).all() == []  # fail closed
    assert audit.records == []


def test_dry_run_writes_no_rows_and_no_audit() -> None:
    session = _build_session()
    audit = InMemoryAuditSink()
    client = _FakeClient(_resp(
        _p("accounts/pub-1/payments/2026-04-10", date(2026, 4, 10), "£60.00"),
    ))
    result = _service(session, client, audit).sync(
        tenant_id=TENANT_ID, account_id="pub-1", actor=ACTOR, reason="r",
        dry_run=True,
    )
    session.commit()
    assert result.synced_count == 1                                # would-sync count
    assert session.scalars(select(AdSensePaymentORM)).all() == []  # no rows
    assert audit.records == []                                     # no audit
```

- [ ] **Step 2 — Run, expect FAIL** (module missing).
  Run: `python -m pytest -q tests/connectors/google/test_adsense_payment_sync.py -x`
  Expected: `ModuleNotFoundError: ums_smart_revenue.connectors.google.adsense_payment_sync`.

- [ ] **Step 3 — Implement `adsense_payment_sync.py`** verbatim:

```python
"""CLI-triggered AdSense live payment sync service (spec §5).

Pipeline: resolve credentials -> fetch payments.list -> classify -> read-only
locked-month prefilter -> strict parse (open months only) -> sync_payments ->
audit. Fully separate from the run_one source-row framework.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.connectors.google.adsense_payment_mapping import (
    PaidSettlement,
    SkippedBalance,
    classify_payments,
    parse_amount,
)
from ums_smart_revenue.connectors.google.adsense_payments_client import (
    GoogleAdSensePaymentClient,
)
from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient
from ums_smart_revenue.connectors.google.registry import (
    ADSENSE_MANAGEMENT_CONNECTOR_KEY,
)
from ums_smart_revenue.connectors.runs.orchestrator import (
    resolve_connector_credentials,
)
from ums_smart_revenue.finance.adsense_payments import (
    AdSensePaymentInput,
    SqlAlchemyAdSensePaymentRepository,
)
from ums_smart_revenue.finance.month_close import get_month_close_status

# Bound the per-entry skip evidence folded into the audit payload so a large
# full-history pull cannot write an unbounded blob to audit_logs.details.
_MAX_SKIP_EVIDENCE = 50


@dataclass(frozen=True)
class SkippedLockedSettlement:
    resource_name: str
    month: str
    payment_date: str  # ISO date string
    raw_amount: str
    reason: str  # always "month_locked"


@dataclass(frozen=True)
class AdSensePaymentSyncResult:
    synced_count: int
    skipped_balance_count: int
    skipped_locked_count: int
    months: list[str]
    skipped_balances: list[SkippedBalance]
    skipped_locked: list[SkippedLockedSettlement]


def _default_client_factory(credentials: object) -> GoogleAdSensePaymentClient:
    """Build the live payments client from resolved Google credentials."""
    return GoogleAdSensePaymentClient(http=GoogleHttpClient(credentials=credentials))


class AdSensePaymentSyncService:
    """Orchestrate one AdSense live payment pull into adsense_payments."""

    def __init__(
        self,
        session: Session,
        *,
        audit_sink: AuditSink,
        credential_resolver=resolve_connector_credentials,
        client_factory=_default_client_factory,
    ) -> None:
        self._session = session
        self._audit_sink = audit_sink
        self._credential_resolver = credential_resolver
        self._client_factory = client_factory

    # ========================================================================
    # Purpose: Execute the spec §5 payment-sync pipeline for one
    #   (tenant, account): resolve credentials, fetch the full payments list,
    #   classify, skip locked months read-only, strict-parse open months, then
    #   persist + audit (or dry-run with neither).
    # Database/ORM: AdSensePaymentORM (via SqlAlchemyAdSensePaymentRepository),
    #   FinanceMonthCloseORM (read-only prefilter), audit_logs (one event).
    # Standards: Fail-closed typed errors propagate to the caller (CLI exit 2);
    #   no $->USD guess; locked months skipped before parsing; sync_payments is
    #   the authoritative race guard. No secret/token leaves this method.
    # Blast Radius: Finance payment source of truth + audit. No run_one,
    #   connector_runs, source rows, graph, or reconciliation writes.
    # Connections:
    #   - File: backend/ums_smart_revenue/finance/adsense_payments.py ->
    #     sync_payments upsert + locked-month write guard.
    #   - File: backend/ums_smart_revenue/finance/month_close.py ->
    #     get_month_close_status read-only prefilter.
    # ========================================================================
    def sync(
        self,
        *,
        tenant_id,
        account_id: str,
        actor: UserPrincipal,
        reason: str,
        dry_run: bool = False,
    ) -> AdSensePaymentSyncResult:
        """Run the live payment pull; return counts + capped skip evidence."""
        # Step 1: resolve credentials (typed CredentialNotFoundError /
        # InactiveCredentialError / OAuthRefreshError propagate -> CLI exit 2).
        credentials = self._credential_resolver(
            session=self._session,
            tenant_id=tenant_id,
            connector_key=ADSENSE_MANAGEMENT_CONNECTOR_KEY,
            account_id=account_id,
        )
        # Step 2: single GET of the full payments list (no pagination).
        client = self._client_factory(credentials)
        response = client.fetch_payments(account_id=account_id)
        canonical_account = str(response["account_id"])
        report_id = str(response["report_id"])

        # Step 3: classify into paid settlements vs retained balances.
        classified = classify_payments(response, account_id=canonical_account)

        # Step 4: read-only locked-month prefilter (no row creation, no lock).
        open_settlements: list[PaidSettlement] = []
        skipped_locked: list[SkippedLockedSettlement] = []
        status_by_month: dict[str, str | None] = {}
        for settlement in classified.paid:
            if settlement.month not in status_by_month:
                status_by_month[settlement.month] = get_month_close_status(
                    self._session, settlement.month, tenant_id=tenant_id,
                )
            if status_by_month[settlement.month] == "LOCKED":
                skipped_locked.append(
                    SkippedLockedSettlement(
                        resource_name=settlement.resource_name,
                        month=settlement.month,
                        payment_date=settlement.payment_date.isoformat(),
                        raw_amount=settlement.raw_amount,
                        reason="month_locked",
                    )
                )
            else:
                open_settlements.append(settlement)

        # Step 5a: strict parse for OPEN-month settlements only. Any parse error
        # raises AdSensePaymentMappingError here -> abort, zero DB writes.
        inputs: list[AdSensePaymentInput] = []
        for settlement in open_settlements:
            amount, currency = parse_amount(settlement.raw_amount)
            inputs.append(
                AdSensePaymentInput(
                    source_account_id=settlement.source_account_id,
                    month=settlement.month,
                    payment_name=settlement.payment_name,
                    payment_date=settlement.payment_date,
                    payment_amount=amount,
                    payment_currency=currency,
                    payment_status="PAID",
                    raw_payload={
                        "name": settlement.resource_name,
                        "amount": settlement.raw_amount,
                    },
                )
            )

        result = AdSensePaymentSyncResult(
            synced_count=len(inputs),
            skipped_balance_count=len(classified.skipped_balances),
            skipped_locked_count=len(skipped_locked),
            months=sorted({s.month for s in open_settlements}),
            skipped_balances=list(classified.skipped_balances),
            skipped_locked=skipped_locked,
        )

        # dry-run: validated + parsed, but NO persistence and NO audit event.
        if dry_run:
            return result

        # Step 5b: skip sync_payments entirely when nothing remains (it rejects
        # an empty batch); otherwise upsert. sync_payments' own per-month
        # FOR UPDATE locked-month gate is the authoritative race guard.
        if inputs:
            repo = SqlAlchemyAdSensePaymentRepository(
                self._session, tenant_id=tenant_id
            )
            repo.sync_payments(
                payments=inputs,
                actor_user_id=actor.user_id,
                source_report_id=report_id,
            )

        # Step 5c: always audit a live pull, even when synced_count == 0.
        self._emit_audit(
            actor=actor,
            reason=reason,
            account_id=canonical_account,
            report_id=report_id,
            result=result,
        )
        return result

    def _emit_audit(
        self,
        *,
        actor: UserPrincipal,
        reason: str,
        account_id: str,
        report_id: str,
        result: AdSensePaymentSyncResult,
    ) -> None:
        """Emit ADSENSE_PAYMENT_SYNCED with counts + capped safe skip evidence."""
        record_audit_event(
            sink=self._audit_sink,
            actor=actor,
            event_type=AuditEventType.ADSENSE_PAYMENT_SYNCED,
            entity_type="adsense_payment_pull",
            entity_id=report_id,
            scope=AccessScope.connector(ADSENSE_MANAGEMENT_CONNECTOR_KEY),
            reason=reason,
            details={
                "trigger": "live_pull",
                "source_account_id": account_id,
                "synced_count": result.synced_count,
                "skipped_balance_count": result.skipped_balance_count,
                "skipped_locked_count": result.skipped_locked_count,
                "months": result.months,
                "skipped_balances": [
                    {
                        "resource_name": b.resource_name,
                        "raw_amount": b.raw_amount,
                        "reason": b.reason,
                    }
                    for b in result.skipped_balances[:_MAX_SKIP_EVIDENCE]
                ],
                "skipped_locked": [
                    {
                        "resource_name": s.resource_name,
                        "month": s.month,
                        "payment_date": s.payment_date,
                        "raw_amount": s.raw_amount,
                        "reason": s.reason,
                    }
                    for s in result.skipped_locked[:_MAX_SKIP_EVIDENCE]
                ],
            },
        )
```

- [ ] **Step 4 — Run, expect PASS**
  Run: `python -m pytest -q tests/connectors/google/test_adsense_payment_sync.py`
  Expected: all pass.
  Run: `python -m ruff check backend tests`
  Expected: clean.

- [ ] **Step 5 — Commit**
```bash
git add backend/ums_smart_revenue/connectors/google/adsense_payment_sync.py \
        tests/connectors/google/test_adsense_payment_sync.py
git commit -m "feat(adsense-payments): live sync service (locked-skip, fail-closed, audit)"
```

---

## Task 9 — CLI `scripts/run_adsense_payment_sync.py`

**The full literal Task 9 lives in a companion plan file** (split out so this
plan stays under the repo's 2,000-line reading limit):

> **`Docs/superpowers/plans/2026-05-29-spec-adsense-payment-sync-task9-cli.md`**

That companion contains the complete `scripts/run_adsense_payment_sync.py` body,
the complete `tests/scripts/test_run_adsense_payment_sync_cli.py` body (importlib
script loader, fake service/session fixtures, exact stdout/stderr assertions),
the exit-code matrix (`0` success / clean dry-run / `synced_count=0` no-op; `2`
for missing `UMS_DATABASE_URL`, missing service-actor `ValueError`, and the typed
set `GoogleConnectorError` / `AdSensePaymentError` /
`AdSensePaymentMappingError`; untyped errors propagate), live-success-commits vs
dry-run-no-commit, the exact pytest/ruff FAIL→PASS commands, and the commit
checkpoint. **Execute that file as Task 9 after Task 8 is green.**

---

## Task 10 — Docs, backlog status, PR #50 flip, final gate

**Files:**
- Modify: `Docs/01_IMPLEMENTATION_PLAN.md`, `Docs/15_DELIVERY_BACKLOG.md`

- [ ] **Step 1 — Flip the stale PR #50 line** in both docs:
  `⏳ PR #50 — remaining: awaiting merge — Concern C…` →
  `✅ PR #50 (merged 2026-05-28 as commit 9c884bd) — Concern C…`.
- [ ] **Step 2 — Update the AdSense payment sync backlog line** (`Docs/15`, the
  `real pull not built.` line — `15:135` at time of writing) to the shipped
  live-pull summary (CLI + client + mapping
  + service + `source_account_id` re-key), keeping the documented `$`-currency
  follow-up limitation explicit.
- [ ] **Step 3 — Run the full validation gate**
```bash
python -m ruff check backend tests scripts
UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:ums@localhost:55432/test_ums python -m pytest -q
git diff --check
```
  Expected: ruff clean; full suite green (new client/mapping/service/CLI/repo/
  migration/API tests + the whole existing suite); `git diff --check` clean.
- [ ] **Step 4 — Commit**
```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "docs: mark AdSense live payment sync shipped + flip PR #50 status"
```

---

## Validation summary (whole PR)

- `python -m ruff check backend tests scripts` — clean.
- Targeted: `tests/connectors/google/test_adsense_payments_client.py`,
  `…/test_adsense_payment_mapping.py`, `…/test_adsense_payment_sync.py`,
  `tests/finance/test_adsense_payments_tenant_scope.py`,
  `tests/finance/test_month_close_status.py`,
  `tests/api/test_adsense_payments_api.py`,
  `tests/db/test_adsense_payment_source_account_migration_postgres.py`,
  `tests/scripts/test_run_adsense_payment_sync_cli.py`.
- Full gate with `UMS_TEST_DATABASE_URL` (real Postgres migration + upsert).
- `git diff --check`.

## Spec ↔ plan coverage map

| Spec requirement | Task |
|---|---|
| `GoogleAdSensePaymentClient` (no pagination, account validation, report_id) | 5 |
| Pure mapping; eligibility; month from `Payment.date`; suffix/date check | 6 |
| Allowlist amount/currency parse; `$`/`¥` abort; raw preserved | 6 (+ raw_payload wired in 8) |
| Locked-month skip-before-parse; read-only prefilter | 7, 8 |
| `sync_payments` strict race guard unchanged | (unchanged; asserted in 8 mid-pull test) |
| Manual endpoint locked-month unchanged; +canonicalized `source_account_id` | 3 |
| `source_account_id` column + re-keyed uniqueness + migration | 1, 2 |
| Credential key `adsense-management` via constant + resolver reuse | 4, 8 |
| CLI `--tenant/--account/--reason/--dry-run`; exit 0/2 | 9 |
| Audit `ADSENSE_PAYMENT_SYNCED` + `live_pull` discriminator + counts + capped skip evidence | 8 |
| Docs/backlog + PR #50 flip | 10 |
| PostgreSQL source of truth; no graph/run_one/connector_runs/source_rows | DB/Blast-Radius §, all tasks |
