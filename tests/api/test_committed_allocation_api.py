"""API tests for POST /revenue/months/{month}/account-allocations/commit."""
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    ContentOwnerChannelLinkORM,
    DeductionComponentORM,
    FinanceBase,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

MONTH = "2026-03"
TENANT = UUID(UMS_TENANT_ID)
SECTOR_ID = UUID("00000000-0000-0000-0000-0000000a0101")
COMPANY_ID = UUID("00000000-0000-0000-0000-0000000a0201")
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-0000000a0301")
USER_ID = UUID("00000000-0000-0000-0000-0000000a0401")
COMMIT_PATH = f"/revenue/months/{MONTH}/account-allocations/commit"


def build_database_url(tmp_path) -> str:
    """Return a unique SQLite URL under pytest's temp path."""
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def _seed(database_url: str, *, mapped: bool = True, status: str = "OPEN") -> None:
    """Seed org/security/finance rows for the commit endpoint.

    chA has ADSENSE gross 1000 and NO source net; an ACCOUNT DEDUCTION (pub-1, 100)
    is the only deduction. mapped=True wires pub-1 -> chA via a VERIFIED Adsense
    link plus an active owner->channel link, so the compute fully allocates with
    zero unallocated. mapped=False omits the links, so pub-1 resolves to no channel
    (one UnallocatedIssue). `status` seeds the finance-month close row.
    """
    engine = create_engine(database_url)
    # `tenants` (FK parent for deduction/link/run rows) lives on TenantBase, a
    # separate base; create it alongside org/security/finance and seed the parent
    # row so create_app's commit resolves the tenant FK regardless of its
    # FK-enforcement setting.
    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            TenantORM(
                id=TENANT, slug="ums", display_name="UMS",
                primary_currency="USD", status="ACTIVE",
            ),
            OrgUnitORM(id=SECTOR_ID, parent_id=None, type="SECTOR", name="S", active=True),
            OrgUnitORM(
                id=COMPANY_ID, parent_id=SECTOR_ID, type="COMPANY", name="C", active=True,
            ),
            YouTubeChannelORM(
                id=CHANNEL_ROW_ID, tenant_id=TENANT, youtube_channel_id="chA",
                channel_name="A", primary_org_unit_id=COMPANY_ID,
                cms_status="INSIDE_CMS", revenue_required=True, active=True,
            ),
            MonthlyChannelRevenueFactORM(
                id=uuid4(), tenant_id=TENANT, month=MONTH, youtube_channel_id="chA",
                source_kind="ADSENSE", source_report_id=None,
                gross_revenue_usd=Decimal("1000.00"), net_revenue_usd=None,
                views=0, watch_time_minutes=Decimal("0"),
                confidence_score=Decimal("0.95"), imported_by=USER_ID,
            ),
            DeductionComponentORM(
                id=uuid4(), tenant_id=TENANT, month=MONTH, component_kind="DEDUCTION",
                scope_kind="ACCOUNT", scope_id="pub-1", amount_usd=Decimal("100.00"),
                currency_code="USD", source_system="adsense_management",
                source_table="google_revenue_source_rows", component_key="ad-1",
                raw_payload={},
            ),
            UserORM(id=USER_ID, email="commit@example.com", display_name="Commit User"),
            FinanceMonthCloseORM(
                tenant_id=TENANT, month=MONTH, status=status, allocation_rule_payload={},
            ),
        ])
        if mapped:
            session.add_all([
                AdsenseContentOwnerLinkORM(
                    id=uuid4(), tenant_id=TENANT, adsense_account_id="pub-1",
                    content_owner_id="owner-1", verification_status="VERIFIED",
                    provenance_kind="OPERATOR_ASSERTED", provenance_payload={},
                    effective_month_start="2026-01",
                ),
                ContentOwnerChannelLinkORM(
                    id=uuid4(), tenant_id=TENANT, content_owner_id="owner-1",
                    youtube_channel_id="chA", provenance_kind="SOURCE_ROW",
                    active=True, effective_month_start="2026-01",
                ),
            ])
        session.commit()


def _principal(*, revenue: bool = True, payments: bool = True, change: bool = True):
    """Global finance principal; flags drop one gate at a time for the 403 cases."""
    grants = []
    if revenue:
        grants.append(PermissionGrant(Permission.VIEW_REVENUE, AccessScope.global_scope()))
        # The reader-untouched regression GETs net-revenue, whose gate also
        # requires VIEW_CONFIDENCE; grant it under the same `revenue` flag so the
        # `revenue=False` 403 case still drops the commit route's VIEW_REVENUE gate.
        grants.append(PermissionGrant(Permission.VIEW_CONFIDENCE, AccessScope.global_scope()))
    if payments:
        grants.append(
            PermissionGrant(Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month(MONTH))
        )
    if change:
        grants.append(
            PermissionGrant(Permission.CHANGE_ALLOCATION_RULE, AccessScope.finance_month(MONTH))
        )
    return UserPrincipal(
        user_id=str(USER_ID), email="commit@example.com",
        direct_permissions=tuple(grants),
    )


def _client(database_url: str, principal_factory) -> TestClient:
    """TestClient with the principal dependency overridden by `principal_factory`."""
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = principal_factory
    return TestClient(app)


def _committed_audit_rows(database_url: str):
    """Return the ALLOCATION_COMMITTED audit rows persisted in the test database."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        return [
            row
            for row in session.scalars(select(AuditLogORM)).all()
            if row.event_type == "ALLOCATION_COMMITTED"
        ]


def test_commit_creates_run_and_summary_only_audit(tmp_path):
    """First commit -> 201 with one allocation, summary-only audit, one audit row."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(COMMIT_PATH, json={"idempotency_key": "k1", "reason": "month close"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["run"]["commit_version"] == 1
    assert body["allocations"]  # one fully-allocated line
    assert body["unallocated"] == []
    assert body["audit_event"]["event_type"] == "ALLOCATION_COMMITTED"
    assert "details" not in body["audit_event"]  # API-surface audit is summary-only
    rows = _committed_audit_rows(db)
    assert len(rows) == 1
    detail = rows[0].details
    assert detail["run_id"] == body["run"]["run_id"]
    assert detail["commit_version"] == 1
    assert "allocated_total_usd" in detail
    assert "allocations" not in detail and "lines" not in detail  # no per-line dump


def test_commit_openapi_documents_both_success_statuses(tmp_path):
    """The published OpenAPI contract advertises BOTH commit success statuses --
    201 (a fresh versioned snapshot) and 200 (idempotent replay) -- so clients
    generated from the schema see the create response, matching runtime behavior.
    """
    client = _client(build_database_url(tmp_path), _principal)
    schema = client.get("/openapi.json").json()
    path_template = "/revenue/months/{month}/account-allocations/commit"
    responses = schema["paths"][path_template]["post"]["responses"]
    assert "201" in responses, "commit must document the 201 create response"
    assert "200" in responses, "commit must document the 200 idempotent-replay response"


def test_idempotent_replay_returns_200_without_second_audit(tmp_path):
    """Re-POST with the same key + identical body -> 200, no second audit row."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    payload = {"idempotency_key": "dup", "reason": "month close"}
    first = client.post(COMMIT_PATH, json=payload)
    second = client.post(COMMIT_PATH, json=payload)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["audit_event"] is None
    assert second.json()["run"]["run_id"] == first.json()["run"]["run_id"]
    assert len(_committed_audit_rows(db)) == 1


def test_same_key_different_reason_conflicts_409(tmp_path):
    """Same key, different reason (different fingerprint) -> 409."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    client.post(COMMIT_PATH, json={"idempotency_key": "dup", "reason": "first"})
    conflict = client.post(COMMIT_PATH, json={"idempotency_key": "dup", "reason": "second"})
    assert conflict.status_code == 409


def test_locked_month_conflicts_409(tmp_path):
    """A LOCKED month rejects the commit with 409."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True, status="LOCKED")
    client = _client(db, _principal)
    resp = client.post(COMMIT_PATH, json={"idempotency_key": "k", "reason": "r"})
    assert resp.status_code == 409


def test_unallocated_month_rejected_422(tmp_path):
    """An unmapped account (one unallocated issue) rejects the commit with 422."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=False)
    client = _client(db, _principal)
    resp = client.post(COMMIT_PATH, json={"idempotency_key": "k", "reason": "r"})
    assert resp.status_code == 422


def test_malformed_month_rejected_422(tmp_path):
    """A malformed month path segment is rejected with 422 before any compute."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(
        "/revenue/months/2026-13/account-allocations/commit",
        json={"idempotency_key": "k", "reason": "r"},
    )
    assert resp.status_code == 422


def test_unsupported_method_rejected_422(tmp_path):
    """A non-gross_revenue_proportional method is rejected with 422."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(
        COMMIT_PATH,
        json={"idempotency_key": "k", "reason": "r", "allocation_method": "company_level"},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"idempotency_key": "k", "reason": ""}, id="empty_reason"),
        pytest.param({"idempotency_key": "k", "reason": "   "}, id="whitespace_reason"),
        pytest.param({"idempotency_key": "", "reason": "r"}, id="empty_idempotency_key"),
        pytest.param(
            {"idempotency_key": "   ", "reason": "r"}, id="whitespace_idempotency_key"
        ),
    ],
)
def test_blank_request_field_rejected_422(tmp_path, body):
    """Empty/whitespace-only idempotency_key or reason is rejected at the Pydantic
    boundary with 422 (before any DB write or audit call), not a DB-CHECK/ValueError
    500. Matches every sibling write-request model's Field(min_length=1) + strip guard.
    """
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(COMMIT_PATH, json=body)
    assert resp.status_code == 422
    assert not _committed_audit_rows(db)  # no audit write on a rejected body


@pytest.mark.parametrize("missing", ["revenue", "payments", "change"])
def test_missing_permission_forbidden_403(tmp_path, missing):
    """Dropping any one of the three required gates yields 403."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)

    def _drop_one():
        """Principal with exactly the parametrized `missing` gate dropped."""
        return _principal(**{missing: False})

    client = _client(db, _drop_one)
    resp = client.post(COMMIT_PATH, json={"idempotency_key": "k", "reason": "r"})
    assert resp.status_code == 403


def test_net_revenue_unchanged_by_commit(tmp_path):
    """READER-UNTOUCHED REGRESSION: live net-revenue is identical (modulo the
    volatile audit_events block) before and after a commit; the snapshot drives no
    reader number.
    """
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    net_path = f"/revenue/months/{MONTH}/net-revenue?scope_type=global"

    before = client.get(net_path)
    assert before.status_code == 200
    commit = client.post(COMMIT_PATH, json={"idempotency_key": "k", "reason": "r"})
    assert commit.status_code == 201
    after = client.get(net_path)
    assert after.status_code == 200

    def _stable(payload: dict) -> dict:
        """Drop the volatile `audit_events` block so two payloads compare equal."""
        return {k: v for k, v in payload.items() if k != "audit_events"}

    assert _stable(before.json()) == _stable(after.json())
