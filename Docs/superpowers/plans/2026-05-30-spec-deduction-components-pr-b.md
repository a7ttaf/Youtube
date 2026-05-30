# Deduction Components — PR-B Implementation Plan (net-revenue wiring + read endpoint)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consume channel-scoped deduction evidence in net-revenue (only when source net is missing) and expose all deduction evidence through a read-only endpoint — over the merged + hardened PR-A substrate.

**Architecture:** Extend the pure `net_revenue` builders to derive a component-based net **only** when the primary fact has no source net, using only source-aligned CHANNEL TAX/DEDUCTION components (anti-double-count, anti-cross-source). Add a read-only `GET /revenue/months/{month}/deduction-components` endpoint that surfaces the typed evidence with smart-alerts-style four-permission auth and sensitive-view audit, excluding `raw_payload`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x (read-only here), pytest, SQLite (tests), ruff.

**Spec:** `Docs/superpowers/specs/2026-05-29-spec-deduction-components-design.md` (§6 net wiring, §7 read endpoint). This is **PR-B**; PR-A (substrate + ingestion + CLI) is merged (main `723765e`).

**Hard non-goals:** NO allocation; ACCOUNT/PAYMENT components and TRANSFER_FEE/FX_VARIANCE/UNRESOLVED_PAYMENT_GAP must NEVER affect channel net; no Neo4j/graph authority; no UI finance calculation. PostgreSQL remains source of truth.

---

## Anchors in the merged code (verified at main `723765e`)

- `backend/ums_smart_revenue/finance/net_revenue.py`:
  - `build_channel_net_revenue_summary(*, facts, manual_overrides, month=None, youtube_channel_id=None)` → `ChannelNetRevenueSummary`. The `if primary.net_revenue_usd is None:` branch is **lines 167-193** (returns `status="NET_REVENUE_SOURCE_MISSING"`, `net_revenue_usd=None`, `confidence="E_MISSING"`). `adjusted_gross = primary.gross_revenue_usd + approved_total` is line 166. The net-present path is lines 195-216.
  - `build_month_net_revenue_summary(*, month, facts, manual_overrides)` groups by channel (lines 219-286) and calls the channel builder per channel (244-252).
  - `_deduction_percentage(*, deduction_amount, gross_revenue_usd)` exists (line 367). `decimal_to_api` is imported as `_decimal_to_api` (line 6).
- `backend/ums_smart_revenue/finance/deduction_components.py`: `DeductionComponent` read model with fields `id, month, component_kind, scope_kind, scope_id, amount_usd, amount_native, currency_code, source_system, source_table, source_id, source_key, source_report_id, raw_payload, component_key`; `.to_api()` **excludes `raw_payload`** (line 67).
- `backend/ums_smart_revenue/finance/deduction_ingestion.py`: `SqlAlchemyDeductionComponentRepository(session, *, tenant_id=None)` with `list_month_components(*, month) -> list[DeductionComponent]` (raises `DeductionComponentValidationError` on malformed month).
- `backend/ums_smart_revenue/api/revenue.py`: dependency providers at 217-259 (`current_revenue_fact_repository`, `current_manual_override_repository`, `current_revenue_audit_sink`, etc.; **no deduction provider yet**); `_require_permission` at 1146; `get_month_smart_alerts` at 652 (four-perm auth 679-682 + three sensitive-view audit events 729-755); `get_month_net_revenue` at 765 (builds via `build_month_net_revenue_summary` at 799, audits `REVENUE_VIEWED`).

---

## File Structure

- **Modify** `backend/ums_smart_revenue/finance/net_revenue.py` — add `SOURCE_SYSTEM_TO_SOURCE_KIND` + `_NET_APPLICABLE_COMPONENT_KINDS`; add a `deduction_components` parameter to both builders; add the `COMPONENT_DERIVED` path in the missing-net branch.
- **Modify** `backend/ums_smart_revenue/api/revenue.py` — add `current_deduction_component_repository` provider; wire deduction components into `get_month_net_revenue`; add the `get_month_deduction_components` read endpoint.
- **Create** `tests/finance/test_net_revenue_deduction_components.py` — pure builder tests.
- **Create** `tests/api/test_deduction_components_api.py` — endpoint auth/shape/audit tests + the net-revenue-consumes-components test.

**Conventions:** USD-only; `to_api()` is the only serializer for components (it already drops `raw_payload`). Mirror the smart-alerts auth + audit pattern exactly. Tests use `auth_headers(role, scope_type, scope_id)` with `x-ums-trusted-gateway-token: pytest-trusted-gateway-token`, `build_database_url(tmp_path)`, ORM seeding, `TestClient(create_app(database_url=...))`.

---

## Task 1: Channel-direct deduction consumption in `net_revenue`

**Files:**
- Modify: `backend/ums_smart_revenue/finance/net_revenue.py`
- Modify: `backend/ums_smart_revenue/api/revenue.py` (wire components into `/net-revenue`)
- Test: `tests/finance/test_net_revenue_deduction_components.py`

- [ ] **Step 1: Write the failing pure-builder tests**

Create `tests/finance/test_net_revenue_deduction_components.py` with this exact content:

```python
"""Channel-direct deduction consumption in net-revenue (only when source net missing)."""
from datetime import date, datetime
from decimal import Decimal
from importlib import import_module

from ums_smart_revenue.finance.deduction_components import DeductionComponent
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry

MONTH = "2026-04"
CHANNEL = "chan-1"


def _mod():
    return import_module("ums_smart_revenue.finance.net_revenue")


def fact(*, source_kind="ADSENSE", gross="1000.00", net=None):
    """Build a RevenueFactEntry for the fixed test channel/month."""
    return RevenueFactEntry(
        id=f"{source_kind}-{CHANNEL}",
        month=MONTH,
        youtube_channel_id=CHANNEL,
        source_kind=source_kind,
        source_report_id=f"{source_kind}-{MONTH}",
        gross_revenue_usd=Decimal(gross),
        net_revenue_usd=None if net is None else Decimal(net),
        views=0,
        watch_time_minutes=Decimal("0"),
        confidence_score=Decimal("1"),
        imported_by=None,
    )


def component(*, kind="DEDUCTION", scope_kind="CHANNEL", scope_id=CHANNEL,
              amount="120.00", source_system="adsense_management"):
    """Build a persisted DeductionComponent read-model row for tests."""
    return DeductionComponent(
        id=f"dc-{kind}-{scope_id}-{amount}",
        month=MONTH,
        component_kind=kind,
        scope_kind=scope_kind,
        scope_id=scope_id,
        amount_usd=Decimal(amount),
        amount_native=None,
        currency_code="USD",
        source_system=source_system,
        source_table="google_revenue_source_rows",
        source_id=None,
        source_key=f"k-{kind}-{amount}",
        source_report_id=None,
        raw_payload={"k": "v"},
        component_key=f"srcrow:{source_system}:{kind}-{amount}",
    )


def _channel(*, facts, components=()):
    return _mod().build_channel_net_revenue_summary(
        facts=facts, manual_overrides=[], month=MONTH,
        youtube_channel_id=CHANNEL, deduction_components=components,
    )


def test_net_present_path_unchanged_components_ignored_for_net():
    # Source net present -> official net path untouched; components do NOT subtract.
    summary = _channel(
        facts=[fact(net="900.00")],
        components=[component(amount="120.00")],
    )
    assert summary.status == "CALCULATED"
    assert summary.net_revenue_usd == Decimal("900.00")
    assert summary.confidence == "B_RECONCILED"


def test_missing_net_with_channel_components_is_component_derived():
    summary = _channel(
        facts=[fact(net=None, gross="1000.00")],
        components=[component(kind="DEDUCTION", amount="120.00"),
                   component(kind="TAX", amount="30.00", source_system="adsense_management")],
    )
    assert summary.status == "COMPONENT_DERIVED"
    assert summary.confidence == "D_ESTIMATED"
    assert summary.net_revenue_usd == Decimal("850.00")  # 1000 - (120 + 30)
    assert summary.deduction_amount_usd == Decimal("150.00")


def test_missing_net_without_applicable_components_stays_missing():
    summary = _channel(facts=[fact(net=None)], components=[])
    assert summary.status == "NET_REVENUE_SOURCE_MISSING"
    assert summary.net_revenue_usd is None
    assert summary.confidence == "E_MISSING"


def test_cross_source_components_excluded_from_derived_net():
    # Primary is ADSENSE; a youtube_reporting (YOUTUBE_CMS) component must NOT apply.
    summary = _channel(
        facts=[fact(source_kind="ADSENSE", net=None)],
        components=[component(amount="120.00", source_system="youtube_reporting")],
    )
    assert summary.status == "NET_REVENUE_SOURCE_MISSING"
    assert summary.net_revenue_usd is None


def test_account_scoped_components_never_affect_net():
    summary = _channel(
        facts=[fact(net=None)],
        components=[component(scope_kind="ACCOUNT", scope_id="pub-1", amount="120.00")],
    )
    assert summary.status == "NET_REVENUE_SOURCE_MISSING"


def test_payment_and_fee_fx_gap_components_never_affect_net():
    summary = _channel(
        facts=[fact(net=None)],
        components=[
            component(kind="TRANSFER_FEE", scope_kind="PAYMENT", scope_id="BANK-1", amount="5.00"),
            component(kind="FX_VARIANCE", scope_kind="PAYMENT", scope_id="BANK-1", amount="-2.00"),
            component(kind="UNRESOLVED_PAYMENT_GAP", scope_kind="ACCOUNT", scope_id="pub-1", amount="70.00"),
        ],
    )
    assert summary.status == "NET_REVENUE_SOURCE_MISSING"


def test_other_channel_components_excluded():
    summary = _channel(
        facts=[fact(net=None)],
        components=[component(scope_id="other-chan", amount="120.00")],
    )
    assert summary.status == "NET_REVENUE_SOURCE_MISSING"


def test_month_summary_includes_component_derived_channel_in_totals():
    mod = _mod()
    summary = mod.build_month_net_revenue_summary(
        month=MONTH,
        facts=[fact(net=None, gross="1000.00")],
        manual_overrides=[],
        deduction_components=[component(kind="DEDUCTION", amount="120.00")],
    )
    channel = summary.channels[0]
    assert channel.status == "COMPONENT_DERIVED"
    assert summary.total_net_revenue_usd == Decimal("880.00")  # 1000 - 120
    assert summary.missing_net_source_count == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/finance/test_net_revenue_deduction_components.py -q`
Expected: FAIL — `TypeError: build_channel_net_revenue_summary() got an unexpected keyword argument 'deduction_components'`.

- [ ] **Step 3: Add the mapping constants + import in `net_revenue.py`**

In `backend/ums_smart_revenue/finance/net_revenue.py`, add to the import block (after line 9, the `revenue_facts` import):

```python
from ums_smart_revenue.finance.deduction_components import DeductionComponent
```

Then, immediately after the imports (before `class ChannelNetRevenueSummary`), add:

```python
# ============================================================================
# Purpose: Map a Google source_system to the RevenueFactSourceKind it backs, so
#   a channel-scoped deduction component is only applied to a net derived from
#   the SAME source (no cross-source mixing). Used only on the missing-net path.
# Database/ORM: None.
# Standards: explicit, closed map; unknown source_system -> no match -> ignored.
# Blast Radius: Finance net-revenue derivation (missing-net path only).
# ============================================================================
SOURCE_SYSTEM_TO_SOURCE_KIND: dict[str, str] = {
    "adsense_management": "ADSENSE",
    "youtube_reporting": "YOUTUBE_CMS",
    "youtube_analytics": "YOUTUBE_ANALYTICS",
}
_NET_APPLICABLE_COMPONENT_KINDS: frozenset[str] = frozenset({"TAX", "DEDUCTION"})
```

- [ ] **Step 4: Add the `deduction_components` parameter + COMPONENT_DERIVED path**

In `build_channel_net_revenue_summary`, change the signature to add the new keyword-only parameter (default empty so existing callers are unaffected):

```python
def build_channel_net_revenue_summary(
    *,
    facts: Iterable[RevenueFactEntry],
    manual_overrides: Iterable[RevenueManualOverrideEntry],
    month: str | None = None,
    youtube_channel_id: str | None = None,
    deduction_components: Iterable[DeductionComponent] = (),
) -> ChannelNetRevenueSummary:
```

Then replace the missing-net branch (the current `if primary.net_revenue_usd is None:` block, lines 167-193) with:

```python
    if primary.net_revenue_usd is None:
        applicable = [
            component
            for component in deduction_components
            if component.scope_kind == "CHANNEL"
            and component.scope_id == resolved_channel_id
            and component.component_kind in _NET_APPLICABLE_COMPONENT_KINDS
            and SOURCE_SYSTEM_TO_SOURCE_KIND.get(component.source_system)
            == primary.source_kind
        ]
        if applicable:
            component_total = sum(
                (component.amount_usd for component in applicable),
                Decimal("0"),
            )
            component_derived_net = adjusted_gross - component_total
            return ChannelNetRevenueSummary(
                month=resolved_month,
                youtube_channel_id=resolved_channel_id,
                status="COMPONENT_DERIVED",
                primary_source_kind=primary.source_kind,
                baseline_gross_revenue_usd=primary.gross_revenue_usd,
                baseline_net_revenue_usd=None,
                approved_manual_override_total_usd=approved_total,
                adjusted_gross_revenue_usd=adjusted_gross,
                net_revenue_usd=component_derived_net,
                deduction_amount_usd=component_total,
                deduction_percentage=_deduction_percentage(
                    deduction_amount=component_total,
                    gross_revenue_usd=adjusted_gross,
                ),
                confidence="D_ESTIMATED",
                approved_manual_override_count=len(approved),
                pending_manual_override_count=len(pending),
                issues=[],
            )
        return ChannelNetRevenueSummary(
            month=resolved_month,
            youtube_channel_id=resolved_channel_id,
            status="NET_REVENUE_SOURCE_MISSING",
            primary_source_kind=primary.source_kind,
            baseline_gross_revenue_usd=primary.gross_revenue_usd,
            baseline_net_revenue_usd=None,
            approved_manual_override_total_usd=approved_total,
            adjusted_gross_revenue_usd=adjusted_gross,
            net_revenue_usd=None,
            deduction_amount_usd=None,
            deduction_percentage=None,
            confidence="E_MISSING",
            approved_manual_override_count=len(approved),
            pending_manual_override_count=len(pending),
            issues=[
                {
                    "issue_type": "NET_REVENUE_SOURCE_MISSING",
                    "severity": "HIGH",
                    "message": (
                        f"Primary revenue source {primary.source_kind} has no "
                        f"net revenue for {resolved_channel_id} in {resolved_month}."
                    ),
                }
            ],
        )
```

(The net-present path below — `adjusted_net = primary.net_revenue_usd + approved_total` onward — is UNCHANGED. Components are never subtracted when source net exists.)

- [ ] **Step 5: Thread `deduction_components` through the month builder**

In `build_month_net_revenue_summary`, change the signature:

```python
def build_month_net_revenue_summary(
    *,
    month: str,
    facts: Iterable[RevenueFactEntry],
    manual_overrides: Iterable[RevenueManualOverrideEntry],
    deduction_components: Iterable[DeductionComponent] = (),
) -> MonthNetRevenueSummary:
```

After the existing `overrides_by_channel` population loop (just before `channel_ids = sorted(...)`), add a grouping of CHANNEL-scoped components by channel:

```python
    components_by_channel: dict[str, list[DeductionComponent]] = defaultdict(list)
    for component in deduction_components:
        if component.scope_kind == "CHANNEL":
            components_by_channel[component.scope_id].append(component)
```

Then pass each channel's components into the per-channel call (the existing list comprehension):

```python
    channels = [
        build_channel_net_revenue_summary(
            facts=facts_by_channel[channel_id],
            manual_overrides=overrides_by_channel[channel_id],
            month=month,
            youtube_channel_id=channel_id,
            deduction_components=components_by_channel.get(channel_id, ()),
        )
        for channel_id in channel_ids
    ]
```

- [ ] **Step 6: Run to verify the pure tests pass**

Run: `python -m pytest tests/finance/test_net_revenue_deduction_components.py -q`
Expected: PASS — 8 passed.

- [ ] **Step 7: Wire deduction components into the `/net-revenue` endpoint**

In `backend/ums_smart_revenue/api/revenue.py`, add the import (with the other finance imports near line 51):

```python
from ums_smart_revenue.finance.deduction_ingestion import (
    DeductionComponentValidationError,
    SqlAlchemyDeductionComponentRepository,
)
```

Add a provider immediately after `current_finance_month_close_repository` (line 247-252 area), mirroring its shape:

```python
def current_deduction_component_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyDeductionComponentRepository:
    """Build the tenant-aware deduction-component repository for a request."""
    return SqlAlchemyDeductionComponentRepository(session)
```

In `get_month_net_revenue` (line 765), add the repository dependency after `override_repository`:

```python
    deduction_component_repository: Annotated[
        SqlAlchemyDeductionComponentRepository,
        Depends(current_deduction_component_repository),
    ],
```

Inside its `try:` block, after `overrides = override_repository.list_month_overrides(...)` and before `summary = build_month_net_revenue_summary(...)`, load the components and pass them:

```python
        deduction_components = deduction_component_repository.list_month_components(
            month=month,
        )
        summary = build_month_net_revenue_summary(
            month=month,
            facts=facts,
            manual_overrides=overrides,
            deduction_components=deduction_components,
        )
```

Add `DeductionComponentValidationError` to the `except (...)` tuple in that endpoint so a malformed month from the deduction read maps to 422:

```python
    except (
        DeductionComponentValidationError,
        ManualOverrideValidationError,
        NetRevenueValidationError,
        RevenueFactValidationError,
    ) as exc:
```

- [ ] **Step 8: Regression-guard the endpoint wiring with the existing net-revenue API tests**

The `/net-revenue` wiring (Step 7) adds a deduction read that returns `[]` when no components are seeded, so the existing endpoint tests must still pass unchanged. (The API-level test that proves component-derived net is added in Task 2, where the deduction-component test harness lives — keeping Task 1 self-contained with no forward file reference.)

Run: `python -m pytest tests/api/test_net_revenue_api.py -q`
Expected: PASS — all existing net-revenue API tests still green (no regression from the new dependency).

- [ ] **Step 9: Lint + commit**

```bash
python -m ruff check backend/ums_smart_revenue/finance/net_revenue.py backend/ums_smart_revenue/api/revenue.py tests/finance/test_net_revenue_deduction_components.py
git add backend/ums_smart_revenue/finance/net_revenue.py backend/ums_smart_revenue/api/revenue.py tests/finance/test_net_revenue_deduction_components.py
git commit -m "feat(finance): channel-direct deduction consumption in net-revenue (missing-net only)"
```

---

## Task 2: Read-only `GET /revenue/months/{month}/deduction-components` endpoint

**Files:**
- Modify: `backend/ums_smart_revenue/api/revenue.py` (the endpoint; provider + import already added in Task 1)
- Test: `tests/api/test_deduction_components_api.py`

The endpoint mirrors `get_month_smart_alerts` (revenue.py:652): four-permission auth, then three sensitive-view audit events. It returns the month's components grouped by scope, never `raw_payload`, with `component_kind`/`scope_kind` filters and offset/limit pagination, and never writes.

- [ ] **Step 1: Write the failing endpoint tests (create the shared test module)**

Create `tests/api/test_deduction_components_api.py` with this exact content (this is the shared module Task 1 Step 8 also appends to — if Task 1 already created it, ensure these helpers + tests are present):

```python
"""Tests for the read-only deduction-components endpoint + net-revenue consumption."""
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.finance_models import (
    DeductionComponentORM,
    FinanceBase,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM

SECTOR_ID = UUID("00000000-0000-0000-0000-0000000e0101")
COMPANY_ID = UUID("00000000-0000-0000-0000-0000000e0201")
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-0000000e0301")
USER_ID = UUID("00000000-0000-0000-0000-0000000e0401")
MONTH = "2026-04"
CHANNEL = "channel-tv-a"


def auth_headers(role, scope_type="global", scope_id=None):
    """Build trusted-gateway auth headers."""
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "deduction-view@example.com",
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


def seed_database(database_url, *, net_revenue_usd="900.00"):
    """Seed org/security/finance rows; net_revenue_usd=None forces missing-net."""
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            OrgUnitORM(id=SECTOR_ID, parent_id=None, type="SECTOR", name="TV", active=True),
            OrgUnitORM(id=COMPANY_ID, parent_id=SECTOR_ID, type="COMPANY", name="TV Co", active=True),
            YouTubeChannelORM(
                id=CHANNEL_ROW_ID, youtube_channel_id=CHANNEL, channel_name="TV A",
                primary_org_unit_id=COMPANY_ID, cms_status="INSIDE_CMS",
                revenue_required=True, active=True,
            ),
            MonthlyChannelRevenueFactORM(
                id=uuid4(), month=MONTH, youtube_channel_id=CHANNEL,
                source_kind="ADSENSE", source_report_id="adsense-2026-04",
                gross_revenue_usd=Decimal("1000.00"),
                net_revenue_usd=(None if net_revenue_usd is None else Decimal(net_revenue_usd)),
                views=1000, watch_time_minutes=Decimal("100.00"),
                confidence_score=Decimal("0.95"), imported_by=USER_ID,
            ),
            UserORM(id=USER_ID, email="deduction-view@example.com", display_name="Deduction Viewer"),
        ])
        session.commit()


def _component(*, kind, scope_kind, scope_id, amount, source_system, key_suffix,
               source_table="google_revenue_source_rows"):
    """Build one DeductionComponentORM row."""
    return DeductionComponentORM(
        id=uuid4(), month=MONTH, component_kind=kind, scope_kind=scope_kind,
        scope_id=scope_id, amount_usd=Decimal(amount), amount_native=None,
        currency_code="USD", source_system=source_system, source_table=source_table,
        source_id=None, source_key=f"k-{key_suffix}", source_report_id=None,
        raw_payload={"secret_provenance": f"LEAK-{key_suffix}"},
        component_key=f"key:{key_suffix}",
    )


def _seed_components(database_url):
    """Seed one component of each scope so grouping/audit branches are exercised."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add_all([
            _component(kind="DEDUCTION", scope_kind="CHANNEL", scope_id=CHANNEL,
                       amount="120.00", source_system="adsense_management", key_suffix="chan"),
            _component(kind="UNRESOLVED_PAYMENT_GAP", scope_kind="ACCOUNT", scope_id="pub-1",
                       amount="70.00", source_system="adsense_payment_gap",
                       key_suffix="acct", source_table="adsense_payment_gap"),
            _component(kind="TRANSFER_FEE", scope_kind="PAYMENT", scope_id="BANK-1",
                       amount="5.00", source_system="bank_reconciliation",
                       key_suffix="pay", source_table="bank_reconciliation_entries"),
        ])
        session.commit()


def _seed_channel_component(database_url, *, amount, source_system):
    """Seed a single CHANNEL DEDUCTION component (used by the net-revenue test)."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            _component(kind="DEDUCTION", scope_kind="CHANNEL", scope_id=CHANNEL,
                       amount=amount, source_system=source_system, key_suffix="net")
        )
        session.commit()


def test_finance_viewer_reads_components_grouped_with_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_components(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/deduction-components",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["month"] == MONTH
    assert body["total_count"] == 3
    scopes = {group["scope_kind"]: group for group in body["scopes"]}
    assert set(scopes) == {"CHANNEL", "ACCOUNT", "PAYMENT"}
    assert scopes["CHANNEL"]["components"][0]["component_kind"] == "DEDUCTION"
    # raw_payload must never appear anywhere in the response.
    assert "raw_payload" not in str(body)
    assert "secret_provenance" not in str(body)
    assert "LEAK" not in str(body)
    event_types = {e["event_type"] for e in body["audit_events"]}
    assert event_types == {"REVENUE_VIEWED", "PAYMENT_VIEWED", "BANK_RECONCILIATION_VIEWED"}

    engine = create_engine(database_url)
    with Session(engine) as session:
        from sqlalchemy import select
        logs = session.scalars(select(AuditLogORM)).all()
    assert {log.event_type for log in logs} == {
        "REVENUE_VIEWED", "PAYMENT_VIEWED", "BANK_RECONCILIATION_VIEWED",
    }
    assert all(log.sensitive is True for log in logs)


def test_component_kind_filter(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_components(database_url)
    client = TestClient(create_app(database_url=database_url))
    body = client.get(
        f"/revenue/months/{MONTH}/deduction-components?component_kind=TRANSFER_FEE",
        headers=auth_headers("finance_viewer", "global"),
    ).json()
    assert body["total_count"] == 1
    assert body["scopes"][0]["scope_kind"] == "PAYMENT"
    assert body["scopes"][0]["components"][0]["component_kind"] == "TRANSFER_FEE"


def test_scope_kind_filter(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_components(database_url)
    client = TestClient(create_app(database_url=database_url))
    body = client.get(
        f"/revenue/months/{MONTH}/deduction-components?scope_kind=CHANNEL",
        headers=auth_headers("finance_viewer", "global"),
    ).json()
    assert body["total_count"] == 1
    assert {g["scope_kind"] for g in body["scopes"]} == {"CHANNEL"}


def test_pagination_limit_and_offset(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_components(database_url)
    client = TestClient(create_app(database_url=database_url))
    body = client.get(
        f"/revenue/months/{MONTH}/deduction-components?limit=1&offset=0",
        headers=auth_headers("finance_viewer", "global"),
    ).json()
    # total_count reflects the full match set; returned rows are paginated.
    assert body["total_count"] == 3
    returned = sum(len(g["components"]) for g in body["scopes"])
    assert returned == 1


def test_malformed_month_returns_422(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/2026-13/deduction-components",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 422


def test_net_revenue_endpoint_uses_channel_component_when_net_missing(tmp_path):
    # PR-B integration: a channel whose only fact has net=NULL plus a CHANNEL
    # DEDUCTION component -> /net-revenue derives COMPONENT_DERIVED net.
    database_url = build_database_url(tmp_path)
    seed_database(database_url, net_revenue_usd=None)
    _seed_channel_component(database_url, amount="120.00", source_system="adsense_management")
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/net-revenue",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 200
    channel = response.json()["channels"][0]
    assert channel["status"] == "COMPONENT_DERIVED"
    assert channel["net_revenue_usd"] == "880"  # 1000 - 120, trimmed
    assert channel["confidence"] == "D_ESTIMATED"


def test_assistant_without_view_revenue_is_403(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/deduction-components",
        headers=auth_headers("assistant_analyst", "global"),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"


def test_finance_admin_global_reads_components(tmp_path):
    # finance_admin holds all four permissions globally -> 200 (the positive
    # gate companion to the assistant 403). Mirrors the smart-alerts sibling's
    # all-permissions read path; both roles are real in auth/roles.py.
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_components(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/deduction-components",
        headers=auth_headers("finance_admin", "global"),
    )
    assert response.status_code == 200


def test_missing_trusted_gateway_token_is_401(tmp_path):
    # Trusted-gateway enforcement runs before route auth; a dropped token -> 401
    # (matches tests/api/test_guarded_routes.py:85-90).
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    headers = auth_headers("finance_admin", "global")
    headers.pop("x-ums-trusted-gateway-token")
    response = client.get(
        f"/revenue/months/{MONTH}/deduction-components", headers=headers,
    )
    assert response.status_code == 401


def test_invalid_trusted_gateway_token_is_401(tmp_path):
    # A wrong gateway token -> 401 (matches tests/api/test_database_principals.py:569-573).
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    headers = auth_headers("finance_admin", "global")
    headers["x-ums-trusted-gateway-token"] = "invalid-token"
    response = client.get(
        f"/revenue/months/{MONTH}/deduction-components", headers=headers,
    )
    assert response.status_code == 401


def test_company_scoped_finance_viewer_is_403_on_global_revenue_gate(tmp_path):
    # The endpoint checks VIEW_REVENUE on global_scope() with no org_index (like
    # smart-alerts, revenue.py:679), so it requires a GLOBAL grant. A
    # company-scoped finance_viewer has only a company grant -> first gate fails
    # -> 403 "Missing permission: finance.view_revenue". Confirms the global vs
    # finance-month scope split is enforced, not bypassed by an org-scoped grant.
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/{MONTH}/deduction-components",
        headers=auth_headers("finance_viewer", "company", str(COMPANY_ID)),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"
```

> **403/200 roles — use only roles confirmed real in `auth/roles.py`.** This plan uses `assistant_analyst` (lacks `VIEW_REVENUE` → 403 "Missing permission: finance.view_revenue", exactly as the sibling net-revenue/smart-alerts 403 tests), `finance_admin` (all four permissions globally → 200), and `finance_viewer` scoped to a company (passes the gateway but fails the endpoint's GLOBAL `VIEW_REVENUE` gate → 403, since the endpoint checks `global_scope()` with no `org_index`, like smart-alerts at revenue.py:679). It deliberately does NOT assert an isolated `VIEW_BANK_RECONCILIATION`-only failure: there is no verified single RoleKey with revenue+confidence+payments-but-not-bank (`revenue_auditor`/`payment_auditor` are NOT RoleKeys), and a global-vs-finance-month mixed-scope endpoint cannot cleanly isolate the bank gate with one role header. Never invent a role name.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/api/test_deduction_components_api.py -q`
Expected: FAIL — the 7 deduction-endpoint tests 404 (route not defined yet); the net-revenue integration test also fails until Task 1's wiring is in. (If Task 1 already merged its wiring, that one test passes — the 7 endpoint tests still fail.)

- [ ] **Step 3: Add a filtered + paginated repository page method (DB-layer, not in-memory)**

Filtering, pagination, and `total_count` belong in the repository — the API route must not load a whole month and slice in Python (a finance evidence table can grow large; the count + page must be computed in SQL). Add a `DeductionComponentPage` result + a `list_month_components_page` method to `SqlAlchemyDeductionComponentRepository` in `backend/ums_smart_revenue/finance/deduction_ingestion.py`.

First, write the failing repository tests. Create `tests/finance/test_deduction_component_page.py`:

```python
"""Repository-layer filtering + pagination for deduction components (SQLite)."""
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import DeductionComponentORM, FinanceBase
from ums_smart_revenue.finance.deduction_ingestion import (
    DeductionComponentValidationError,
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

MONTH = "2026-04"
TENANT = UUID(UMS_TENANT_ID)


def _engine(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"
    )
    FinanceBase.metadata.create_all(engine)
    return engine


def _add(session, *, kind, scope_kind, scope_id, key):
    session.add(
        DeductionComponentORM(
            id=uuid4(), tenant_id=TENANT, month=MONTH, component_kind=kind,
            scope_kind=scope_kind, scope_id=scope_id, amount_usd=Decimal("10.00"),
            amount_native=None, currency_code="USD",
            source_system="adsense_management", source_table="google_revenue_source_rows",
            source_id=None, source_key=key, source_report_id=None,
            raw_payload={"k": "v"}, component_key=key,
        )
    )


def _seed(session):
    _add(session, kind="DEDUCTION", scope_kind="CHANNEL", scope_id="chan-1", key="k-chan")
    _add(session, kind="UNRESOLVED_PAYMENT_GAP", scope_kind="ACCOUNT", scope_id="pub-1", key="k-acct")
    _add(session, kind="TRANSFER_FEE", scope_kind="PAYMENT", scope_id="BANK-1", key="k-pay")
    session.commit()


def test_page_returns_all_with_total_when_unfiltered(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        repo = SqlAlchemyDeductionComponentRepository(session)
        page = repo.list_month_components_page(month=MONTH, limit=100, offset=0)
    assert page.total_count == 3
    assert len(page.components) == 3
    # deterministic order: scope_kind, scope_id, component_kind, component_key
    assert [c.scope_kind for c in page.components] == ["ACCOUNT", "CHANNEL", "PAYMENT"]


def test_page_component_kind_filter_counts_only_matches(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        repo = SqlAlchemyDeductionComponentRepository(session)
        page = repo.list_month_components_page(
            month=MONTH, component_kind="TRANSFER_FEE", limit=100, offset=0
        )
    assert page.total_count == 1
    assert page.components[0].component_kind == "TRANSFER_FEE"


def test_page_scope_kind_filter_counts_only_matches(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        repo = SqlAlchemyDeductionComponentRepository(session)
        page = repo.list_month_components_page(
            month=MONTH, scope_kind="CHANNEL", limit=100, offset=0
        )
    assert page.total_count == 1
    assert page.components[0].scope_kind == "CHANNEL"


def test_page_limit_offset_paginates_but_total_is_full_match_count(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        repo = SqlAlchemyDeductionComponentRepository(session)
        page = repo.list_month_components_page(month=MONTH, limit=1, offset=0)
        page2 = repo.list_month_components_page(month=MONTH, limit=1, offset=2)
    assert page.total_count == 3
    assert len(page.components) == 1
    assert page.components[0].scope_kind == "ACCOUNT"  # first in deterministic order
    assert page2.components[0].scope_kind == "PAYMENT"  # third


def test_page_malformed_month_raises(tmp_path):
    engine = _engine(tmp_path)
    with Session(engine) as session:
        repo = SqlAlchemyDeductionComponentRepository(session)
        with pytest.raises(DeductionComponentValidationError):
            repo.list_month_components_page(month="2026-13", limit=100, offset=0)
```

Run: `python -m pytest tests/finance/test_deduction_component_page.py -q`
Expected: FAIL — `AttributeError: 'SqlAlchemyDeductionComponentRepository' object has no attribute 'list_month_components_page'`.

Add the result dataclass next to `DeductionIngestionResult` in `deduction_ingestion.py` (after its definition, ~line 69):

```python
@dataclass(frozen=True)
class DeductionComponentPage:
    """One page of deduction components plus the full-match total count."""

    total_count: int
    components: list[DeductionComponent]
```

Add the method to `SqlAlchemyDeductionComponentRepository`, immediately after `list_month_components` (the existing read), reusing the same deterministic ordering. It computes the total via a SQL `COUNT(*)` over the filtered set and returns only the requested page:

```python
    def list_month_components_page(
        self, *,
        month: str,
        component_kind: str | None = None,
        scope_kind: str | None = None,
        limit: int,
        offset: int,
    ) -> DeductionComponentPage:
        """Return a filtered, paginated page of components + the full match count.

        Raises:
            DeductionComponentValidationError: If the month is malformed.
        """
        _validate_month(month)
        filters = [
            DeductionComponentORM.tenant_id == self._tenant_id,
            DeductionComponentORM.month == month,
        ]
        if component_kind is not None:
            filters.append(DeductionComponentORM.component_kind == component_kind)
        if scope_kind is not None:
            filters.append(DeductionComponentORM.scope_kind == scope_kind)
        total_count = self._session.scalar(
            select(func.count()).select_from(DeductionComponentORM).where(*filters)
        )
        rows = self._session.scalars(
            select(DeductionComponentORM)
            .where(*filters)
            .order_by(
                DeductionComponentORM.scope_kind,
                DeductionComponentORM.scope_id,
                DeductionComponentORM.component_kind,
                DeductionComponentORM.component_key,
            )
            .limit(limit)
            .offset(offset)
        ).all()
        return DeductionComponentPage(
            total_count=int(total_count or 0),
            components=[self._to_entry(row) for row in rows],
        )
```

Add `func` to the SQLAlchemy import at the top of `deduction_ingestion.py` (currently `from sqlalchemy import delete, select`):

```python
from sqlalchemy import delete, func, select
```

Run: `python -m pytest tests/finance/test_deduction_component_page.py -q`
Expected: PASS — 5 passed.

Lint + commit the repository method:

```bash
python -m ruff check backend/ums_smart_revenue/finance/deduction_ingestion.py tests/finance/test_deduction_component_page.py
git add backend/ums_smart_revenue/finance/deduction_ingestion.py tests/finance/test_deduction_component_page.py
git commit -m "feat(finance): filtered+paginated deduction-component repository page method"
```

> **Scope note:** this is the one allowed touch to `deduction_ingestion.py` (PR-A file) — a purely additive read method + result dataclass, no change to ingestion/upsert/audit behavior. The "Don't change PR-A" rule in the implementer notes refers to *behavioral* changes; an additive read query for the new endpoint is in-scope for PR-B.

- [ ] **Step 4: Add the read endpoint to `revenue.py`**

Insert this handler immediately AFTER `get_month_smart_alerts` ends (after revenue.py:761, before `@router.get("/months/{month}/net-revenue")`). The provider, `SqlAlchemyDeductionComponentRepository`, and `DeductionComponentValidationError` imports were added in Task 1 Step 7.

```python
# ============================================================================
# Purpose: Read-only per-month deduction-evidence view, grouped by scope
#   (CHANNEL/ACCOUNT/PAYMENT). Surfaces the typed components PR-A ingested; never
#   writes, never triggers ingestion, never returns raw_payload.
# Database/ORM: Reads deduction_components via SqlAlchemyDeductionComponentRepository.
# Standards: smart-alerts four-permission auth; sensitive-view audit (revenue +
#   payment + bank); month validation -> 422; offset/limit pagination.
# Blast Radius: Finance read (deduction evidence). No finance mutation, no Neo4j.
# Connections:
#   - File: backend/ums_smart_revenue/finance/deduction_ingestion.py -> repo.
#   - File: backend/ums_smart_revenue/finance/deduction_components.py -> to_api().
# ============================================================================
@router.get("/months/{month}/deduction-components")
def get_month_deduction_components(
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyDeductionComponentRepository,
        Depends(current_deduction_component_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
    component_kind: str | None = None,
    scope_kind: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    """Return one month's deduction evidence grouped by scope for finance review."""
    global_scope = AccessScope.global_scope()
    month_scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.VIEW_REVENUE, global_scope)
    _require_permission(user, Permission.VIEW_CONFIDENCE, global_scope)
    _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, month_scope)
    _require_permission(user, Permission.VIEW_BANK_RECONCILIATION, month_scope)
    try:
        page = repository.list_month_components_page(
            month=month,
            component_kind=component_kind,
            scope_kind=scope_kind,
            limit=limit,
            offset=offset,
        )
    except DeductionComponentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    grouped: dict[str, list[dict[str, object]]] = {}
    for component in page.components:
        grouped.setdefault(component.scope_kind, []).append(component.to_api())
    scopes = [
        {"scope_kind": kind, "components": grouped[kind]}
        for kind in sorted(grouped)
    ]

    audit_details = {
        "month": month,
        "total_count": page.total_count,
        "returned_count": len(page.components),
    }
    revenue_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="monthly_deduction_components",
        entity_id=month,
        scope=global_scope,
        details=audit_details,
    )
    payment_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.PAYMENT_VIEWED,
        entity_type="monthly_deduction_components",
        entity_id=month,
        scope=month_scope,
        details=audit_details,
    )
    bank_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.BANK_RECONCILIATION_VIEWED,
        entity_type="monthly_deduction_components",
        entity_id=month,
        scope=month_scope,
        details=audit_details,
    )
    return {
        "month": month,
        "total_count": page.total_count,
        "returned_count": len(page.components),
        "scopes": scopes,
        "audit_events": [
            audit_record_to_api(revenue_record),
            audit_record_to_api(payment_record),
            audit_record_to_api(bank_record),
        ],
    }
```

(`Query`, `status`, `HTTPException`, `AuditSink`, `record_audit_event`, `audit_record_to_api`, `AccessScope`, `Permission`, `AuditEventType`, `UserPrincipal`, `current_principal_from_headers` are all already imported in `revenue.py` — they back the sibling endpoints. Confirm before adding new imports.)

- [ ] **Step 5: Run to verify the endpoint tests pass**

Run: `python -m pytest tests/api/test_deduction_components_api.py -q`
Expected: PASS — 11 passed (1 grouped-read+audit, 2 filter, 1 pagination, 1 malformed-month 422, 1 net-revenue component-derived integration, 1 assistant 403, 1 company-scoped-viewer 403, 1 missing-gateway-token 401, 1 invalid-gateway-token 401, 1 finance_admin 200).

- [ ] **Step 6: Lint + commit**

```bash
python -m ruff check backend/ums_smart_revenue/api/revenue.py tests/api/test_deduction_components_api.py
git add backend/ums_smart_revenue/api/revenue.py tests/api/test_deduction_components_api.py
git commit -m "feat(api): read-only GET /revenue/months/{month}/deduction-components"
```

---

## Task 3: Full validation gate

**Files:** none (verification only).

- [ ] **Step 1: Ruff over the standard scope**

Run: `python -m ruff check backend tests scripts`
Expected: `All checks passed!`

- [ ] **Step 2: Full test suite**

Run: `python -m pytest -q`
Expected: PASS — prior count + the new tests (8 pure net-revenue + 5 repository-page + 11 API). 0 failed.
If `UMS_TEST_DATABASE_URL` is unset, the pre-existing `*_postgres.py` tests fail-fast (unchanged env-gating) — this PR adds NO migration, so a SQLite-only run otherwise green is acceptable; for full parity run under the disposable `test_*`-named Postgres.

- [ ] **Step 3: Whitespace/diff hygiene**

Run: `git diff --check`
Expected: no output.

- [ ] **Step 4 (parity): full suite under Postgres**

```bash
# Postgres already runs at :55432 (container ums-mig-pg-test). Use a test_*-named DB.
# PowerShell: $env:UMS_TEST_DATABASE_URL = 'postgresql+psycopg://postgres:ums@localhost:55432/test_ums'
# POSIX:      export UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:ums@localhost:55432/test_ums
python -m pytest -q
```
Expected: PASS — full green (no new migration; the existing `*_postgres.py` tests run under the `test_*` DB).

---

## Notes for the implementer

- **No allocation, ever.** PR-B only consumes CHANNEL TAX/DEDUCTION components on the missing-net path. ACCOUNT/PAYMENT components and TRANSFER_FEE/FX_VARIANCE/UNRESOLVED_PAYMENT_GAP are surfaced by the read endpoint but MUST NOT affect any channel's net. If you find yourself distributing an account/payment amount to channels, stop — that is Spec 2.
- **Anti-double-count:** when `primary.net_revenue_usd` is present, the net-present path is unchanged and components are NOT subtracted. Only the missing-net branch derives from components.
- **Anti-cross-source:** a component applies only if `SOURCE_SYSTEM_TO_SOURCE_KIND[component.source_system] == primary.source_kind`. Unknown `source_system` → `.get()` returns None → never matches → ignored.
- **No `raw_payload` in any response.** `DeductionComponent.to_api()` already excludes it; the endpoint returns only `to_api()` dicts. The API test asserts the seeded `secret_provenance`/`LEAK` marker never appears.
- **Don't change PR-A.** Do not touch `deduction_components.py`, `deduction_ingestion.py`, the migration, the ORM, the CLI, or the audit event. PR-B is purely additive consumption + a read endpoint.
- **Existing callers unaffected:** the new `deduction_components` parameter defaults to `()`, so every current `build_channel_net_revenue_summary` / `build_month_net_revenue_summary` caller keeps working. Run the existing `tests/finance/test_net_revenue.py` + `tests/api/test_net_revenue_api.py` to confirm no regression.
- **Roles (verified real in `auth/roles.py`):** use only `assistant_analyst` (lacks `VIEW_REVENUE` → 403 "Missing permission: finance.view_revenue") and `finance_admin` (all four permissions → 200), exactly as the smart-alerts sibling test does. Do NOT use `revenue_auditor`/`payment_auditor` — those are not RoleKeys. If isolating the bank-permission 403 is wanted, confirm the role→permission map in `auth/seed.py` first or use a finance-month-scoped mismatch; never invent a role name.
