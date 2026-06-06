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
    """A method outside the committable allowlist is rejected with 422."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(
        COMMIT_PATH,
        json={
            "idempotency_key": "k", "reason": "r",
            "allocation_method": "definitely_not_a_method",
        },
    )
    assert resp.status_code == 422


def test_manual_method_rejected_by_service_allowlist_422(tmp_path):
    """'manual' is DB-pre-cleared but the service allowlist MUST still reject it.

    Migration 20260606_0001 widened the DB CHECK to five methods so the paired
    manual-allocation PR needs no second migration; until that PR lands, the
    service-layer COMMITTABLE_ALLOCATION_METHODS allowlist is the ONLY guard
    against an uncomputed 'manual' commit. This test pins that guard (422, no
    run persisted, no audit). It stays valid after the paired PR: a manual
    commit without explicit lines must still 422 there.
    """
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(
        COMMIT_PATH,
        json={"idempotency_key": "k-man", "reason": "r", "allocation_method": "manual"},
    )
    assert resp.status_code == 422
    assert "manual" in resp.json()["detail"]
    assert not _committed_audit_rows(db)


def test_commit_post_tax_returns_201_with_basis_amount(tmp_path):
    """POST commit with post_tax persists the method + the renamed basis key."""
    database_url = build_database_url(tmp_path)
    _seed(database_url, mapped=True)
    # post_tax needs non-null source net (the seed leaves chA net = None).
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.execute(
            MonthlyChannelRevenueFactORM.__table__.update()
            .where(MonthlyChannelRevenueFactORM.youtube_channel_id == "chA")
            .values(net_revenue_usd=Decimal("800.00"))
        )
        session.commit()
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = _principal
    client = TestClient(app)
    response = client.post(
        COMMIT_PATH,
        json={"idempotency_key": "k-pt", "reason": "post-tax close",
              "allocation_method": "post_tax_revenue_proportional"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["run"]["allocation_method"] == "post_tax_revenue_proportional"
    assert body["allocations"]
    assert all("basis_amount_usd" in a for a in body["allocations"])
    assert all("basis_gross_usd" not in a for a in body["allocations"])


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


def test_commit_no_allocation_snapshots_withheld_components(tmp_path):
    """no_allocation commits 201 with zero lines and the full withheld set as evidence."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(
        COMMIT_PATH,
        json={"idempotency_key": "k-na", "reason": "withhold this month",
              "allocation_method": "no_allocation"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["run"]["allocation_method"] == "no_allocation"
    assert body["allocations"] == []
    assert len(body["unallocated"]) == 1
    issue = body["unallocated"][0]
    assert issue["issue_code"] == "INTENTIONAL_NO_ALLOCATION"
    assert issue["component_key"] == "ad-1"
    assert Decimal(issue["amount_usd"]) == Decimal("100")
    assert body["audit_event"]["event_type"] == "ALLOCATION_COMMITTED"
    assert len(_committed_audit_rows(db)) == 1


def test_commit_no_allocation_idempotent_replay_200(tmp_path):
    """Replaying a no_allocation commit returns 200 without a second audit row."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    payload = {"idempotency_key": "k-na", "reason": "withhold",
               "allocation_method": "no_allocation"}
    first = client.post(COMMIT_PATH, json=payload)
    second = client.post(COMMIT_PATH, json=payload)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["audit_event"] is None
    assert second.json()["unallocated"] == first.json()["unallocated"]
    assert len(_committed_audit_rows(db)) == 1


def test_commit_no_allocation_needs_no_channel_map(tmp_path):
    """no_allocation commits even when the account has no verified channels.

    The intentional withhold needs neither facts nor links; the issue stays
    INTENTIONAL_NO_ALLOCATION (not ACCOUNT_UNMAPPED) because the short-circuit
    runs before the verified-channel check.
    """
    db = build_database_url(tmp_path)
    _seed(db, mapped=False)
    client = _client(db, _principal)
    resp = client.post(
        COMMIT_PATH,
        json={"idempotency_key": "k", "reason": "r", "allocation_method": "no_allocation"},
    )
    assert resp.status_code == 201
    assert resp.json()["unallocated"][0]["issue_code"] == "INTENTIONAL_NO_ALLOCATION"


def test_commit_company_level_returns_201(tmp_path):
    """company_level commits via the real org channel->company index (201)."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(
        COMMIT_PATH,
        json={"idempotency_key": "k-cl", "reason": "company close",
              "allocation_method": "company_level"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["run"]["allocation_method"] == "company_level"
    assert body["unallocated"] == []
    assert len(body["allocations"]) == 1
    line = body["allocations"][0]
    assert line["youtube_channel_id"] == "chA"
    assert Decimal(line["allocated_amount_usd"]) == Decimal("100")
    # Single channel in a single company: company gross 1000 / 1 = full weight.
    assert Decimal(line["basis_share"]) == Decimal("1")


def test_commit_company_level_unmapped_channel_rejected_422(tmp_path):
    """A verified channel outside the company index fails the commit closed (422)."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    # chB is verified for pub-1 but has NO primary_org_unit_id, so the org index
    # cannot map it to a company -> COMPANY_UNMAPPED -> reject-on-unallocated.
    engine = create_engine(db)
    with Session(engine) as session:
        session.add_all([
            YouTubeChannelORM(
                id=uuid4(), tenant_id=TENANT, youtube_channel_id="chB",
                channel_name="B", primary_org_unit_id=None,
                cms_status="INSIDE_CMS", revenue_required=True, active=True,
            ),
            ContentOwnerChannelLinkORM(
                id=uuid4(), tenant_id=TENANT, content_owner_id="owner-1",
                youtube_channel_id="chB", provenance_kind="SOURCE_ROW",
                active=True, effective_month_start="2026-01",
            ),
        ])
        session.commit()
    client = _client(db, _principal)
    resp = client.post(
        COMMIT_PATH,
        json={"idempotency_key": "k", "reason": "r", "allocation_method": "company_level"},
    )
    assert resp.status_code == 422
    assert "unallocated" in resp.json()["detail"]


def test_locked_month_get_returns_no_allocation_snapshot(tmp_path):
    """PR-6 read-switch proof: a LOCKED month's GET serves the committed
    no_allocation snapshot (zero lines, the withheld set, committed provenance).
    """
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    commit = client.post(
        COMMIT_PATH,
        json={"idempotency_key": "k-na", "reason": "withhold",
              "allocation_method": "no_allocation"},
    )
    assert commit.status_code == 201
    engine = create_engine(db)
    with Session(engine) as session:
        session.execute(
            FinanceMonthCloseORM.__table__.update()
            .where(FinanceMonthCloseORM.month == MONTH)
            .values(status="LOCKED")
        )
        session.commit()
    read = client.get(f"/revenue/months/{MONTH}/account-allocations")
    assert read.status_code == 200
    body = read.json()
    assert body["allocation_source"] == "committed_snapshot"
    assert body["allocation_method"] == "no_allocation"
    assert body["allocations"] == []
    assert body["unallocated"][0]["issue_code"] == "INTENTIONAL_NO_ALLOCATION"


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


# The seed component "ad-1" (pub-1, DEDUCTION 100) is verified to chA only, so a
# single full-amount line is the canonical valid manual split for these tests.
def _manual_body(*, key: str = "k-man", lines=None, reason: str = "manual close"):
    """Build a manual commit body; `lines` defaults to the full ad-1 -> chA split."""
    return {
        "idempotency_key": key,
        "reason": reason,
        "allocation_method": "manual",
        "manual_lines": lines
        if lines is not None
        else [{"component_key": "ad-1", "youtube_channel_id": "chA", "amount_usd": "100.00"}],
    }


def test_manual_commit_happy_path_201_with_posted_split(tmp_path):
    """A valid manual split commits 201 with MANUAL-basis lines + one audit row."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(COMMIT_PATH, json=_manual_body())
    assert resp.status_code == 201
    body = resp.json()
    assert body["run"]["allocation_method"] == "manual"
    assert body["unallocated"] == []
    assert len(body["allocations"]) == 1
    line = body["allocations"][0]
    assert line["youtube_channel_id"] == "chA"
    assert line["component_key"] == "ad-1"
    assert line["basis_source_kind"] == "MANUAL"
    assert Decimal(line["allocated_amount_usd"]) == Decimal("100")
    assert body["audit_event"]["event_type"] == "ALLOCATION_COMMITTED"
    assert len(_committed_audit_rows(db)) == 1


def test_manual_lines_with_gross_method_rejected_422(tmp_path):
    """Supplying manual_lines with a non-manual method is rejected with 422."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(
        COMMIT_PATH,
        json={
            "idempotency_key": "k", "reason": "r",
            "allocation_method": "gross_revenue_proportional",
            "manual_lines": [
                {"component_key": "ad-1", "youtube_channel_id": "chA", "amount_usd": "100.00"}
            ],
        },
    )
    assert resp.status_code == 422
    assert not _committed_audit_rows(db)


def test_manual_sum_mismatch_rejected_422(tmp_path):
    """A manual split that does not sum to the component amount is rejected 422."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(
        COMMIT_PATH,
        json=_manual_body(
            lines=[{"component_key": "ad-1", "youtube_channel_id": "chA", "amount_usd": "90.00"}]
        ),
    )
    assert resp.status_code == 422
    assert not _committed_audit_rows(db)


def test_manual_unknown_component_key_rejected_422(tmp_path):
    """A manual line for an unknown component_key is rejected with 422."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(
        COMMIT_PATH,
        json=_manual_body(
            lines=[
                {"component_key": "ad-1", "youtube_channel_id": "chA", "amount_usd": "100.00"},
                {"component_key": "ghost", "youtube_channel_id": "chA", "amount_usd": "0.00"},
            ]
        ),
    )
    assert resp.status_code == 422
    assert not _committed_audit_rows(db)


def test_manual_channel_not_verified_rejected_422(tmp_path):
    """A manual line whose channel is not verified for the account is rejected 422."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(
        COMMIT_PATH,
        json=_manual_body(
            lines=[{"component_key": "ad-1", "youtube_channel_id": "chZ", "amount_usd": "100.00"}]
        ),
    )
    assert resp.status_code == 422
    assert not _committed_audit_rows(db)


def test_manual_duplicate_pair_rejected_422(tmp_path):
    """A duplicate (component_key, channel) manual line is rejected with 422."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(
        COMMIT_PATH,
        json=_manual_body(
            lines=[
                {"component_key": "ad-1", "youtube_channel_id": "chA", "amount_usd": "60.00"},
                {"component_key": "ad-1", "youtube_channel_id": "chA", "amount_usd": "40.00"},
            ]
        ),
    )
    assert resp.status_code == 422
    assert not _committed_audit_rows(db)


def test_manual_uncovered_component_rejected_422(tmp_path):
    """A second ACCOUNT component with no manual line is rejected with 422."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    # Seed a second ACCOUNT component; cover only the first.
    engine = create_engine(db)
    with Session(engine) as session:
        session.add(
            DeductionComponentORM(
                id=uuid4(), tenant_id=TENANT, month=MONTH, component_kind="DEDUCTION",
                scope_kind="ACCOUNT", scope_id="pub-1", amount_usd=Decimal("50.00"),
                currency_code="USD", source_system="adsense_management",
                source_table="google_revenue_source_rows", component_key="ad-2",
                raw_payload={},
            )
        )
        session.commit()
    client = _client(db, _principal)
    resp = client.post(COMMIT_PATH, json=_manual_body())
    assert resp.status_code == 422
    assert not _committed_audit_rows(db)


def test_manual_idempotent_replay_returns_200_without_second_audit(tmp_path):
    """Re-POST a manual commit with the same key + identical lines -> 200, no audit."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    body = _manual_body(key="dup-man")
    first = client.post(COMMIT_PATH, json=body)
    second = client.post(COMMIT_PATH, json=body)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["audit_event"] is None
    assert second.json()["run"]["run_id"] == first.json()["run"]["run_id"]
    assert len(_committed_audit_rows(db)) == 1


def test_manual_same_key_different_lines_conflicts_409(tmp_path):
    """Same key but a different manual split (different fingerprint) -> 409.

    chB is also verified for pub-1 so the second body is itself a valid split;
    only the fingerprint difference (not a validation error) drives the 409.
    """
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    engine = create_engine(db)
    with Session(engine) as session:
        session.add_all([
            YouTubeChannelORM(
                id=uuid4(), tenant_id=TENANT, youtube_channel_id="chB",
                channel_name="B", primary_org_unit_id=COMPANY_ID,
                cms_status="INSIDE_CMS", revenue_required=True, active=True,
            ),
            ContentOwnerChannelLinkORM(
                id=uuid4(), tenant_id=TENANT, content_owner_id="owner-1",
                youtube_channel_id="chB", provenance_kind="SOURCE_ROW",
                active=True, effective_month_start="2026-01",
            ),
        ])
        session.commit()
    client = _client(db, _principal)
    first = client.post(COMMIT_PATH, json=_manual_body(key="dup-man"))
    assert first.status_code == 201
    conflict = client.post(
        COMMIT_PATH,
        json=_manual_body(
            key="dup-man",
            lines=[
                {"component_key": "ad-1", "youtube_channel_id": "chA", "amount_usd": "70.00"},
                {"component_key": "ad-1", "youtube_channel_id": "chB", "amount_usd": "30.00"},
            ],
        ),
    )
    assert conflict.status_code == 409


def test_manual_locked_month_conflicts_409(tmp_path):
    """A LOCKED month rejects a manual commit with 409."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True, status="LOCKED")
    client = _client(db, _principal)
    resp = client.post(COMMIT_PATH, json=_manual_body())
    assert resp.status_code == 409


def test_commit_request_fingerprint_backward_compatible_without_lines():
    """commit_request_fingerprint with manual_lines=None equals the legacy digest.

    The legacy digest hashed only {allocation_method, reason}; the renamed helper
    must stay byte-identical for that two-field input so existing snapshots replay.
    """
    import hashlib
    import json

    from ums_smart_revenue.api.allocation import commit_request_fingerprint

    legacy = hashlib.blake2b(
        json.dumps(
            {"allocation_method": "gross_revenue_proportional", "reason": "month close"},
            sort_keys=True,
        ).encode(),
        digest_size=16,
    ).hexdigest()
    current = commit_request_fingerprint(
        allocation_method="gross_revenue_proportional", reason="month close",
    )
    assert current == legacy


def test_manual_locked_month_get_serves_committed_snapshot(tmp_path):
    """A LOCKED month's GET serves the committed manual snapshot (read-switch).

    PR A proved the resolver is method-agnostic; this pins it for `manual`.
    """
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    commit = client.post(COMMIT_PATH, json=_manual_body())
    assert commit.status_code == 201
    engine = create_engine(db)
    with Session(engine) as session:
        session.execute(
            FinanceMonthCloseORM.__table__.update()
            .where(FinanceMonthCloseORM.month == MONTH)
            .values(status="LOCKED")
        )
        session.commit()
    read = client.get(f"/revenue/months/{MONTH}/account-allocations")
    assert read.status_code == 200
    body = read.json()
    assert body["allocation_source"] == "committed_snapshot"
    assert body["allocation_method"] == "manual"
    assert len(body["allocations"]) == 1
    assert body["allocations"][0]["basis_source_kind"] == "MANUAL"
    assert Decimal(body["allocations"][0]["allocated_amount_usd"]) == Decimal("100")
