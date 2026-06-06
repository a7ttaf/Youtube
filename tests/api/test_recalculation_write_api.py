"""API tests for the committed write path of POST /revenue/recalculate.

dry_run=false delegates to the SAME committed-allocation service as
POST /revenue/months/{month}/account-allocations/commit (same advisory lock,
idempotency, audit, version chain). The preview runs first as a pre-flight gate;
the commit service stays the final authority. The seeds mirror
test_committed_allocation_api.py because the write path needs deduction
components + verified account->channel links + a close row that the legacy
recalculation seed lacks.
"""
from decimal import Decimal
from uuid import UUID, uuid4

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
    CommittedAllocationRunORM,
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
SECTOR_ID = UUID("00000000-0000-0000-0000-0000000b0101")
COMPANY_ID = UUID("00000000-0000-0000-0000-0000000b0201")
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-0000000b0301")
USER_ID = UUID("00000000-0000-0000-0000-0000000b0401")
RECALC_PATH = "/revenue/recalculate"


def build_database_url(tmp_path) -> str:
    """Return a unique SQLite URL under pytest's temp path."""
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def _seed(database_url: str, *, mapped: bool = True, status: str = "OPEN") -> None:
    """Seed org/security/finance rows for the recalculate write path.

    chA has ADSENSE gross 1000 and NO source net; an ACCOUNT DEDUCTION (pub-1, 100)
    is the only deduction. mapped=True wires pub-1 -> chA via a VERIFIED Adsense
    link plus an active owner->channel link, so the compute fully allocates with
    zero unallocated. chA's primary_org_unit_id is the COMPANY (under a SECTOR) so
    the org index maps it for company_level. `status` seeds the close row.
    """
    engine = create_engine(database_url)
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
            UserORM(id=USER_ID, email="recalc@example.com", display_name="Recalc User"),
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


def _set_source_net(database_url: str, net: Decimal) -> None:
    """Set chA's source net (post_tax needs non-null source net to allocate)."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.execute(
            MonthlyChannelRevenueFactORM.__table__.update()
            .where(MonthlyChannelRevenueFactORM.youtube_channel_id == "chA")
            .values(net_revenue_usd=net)
        )
        session.commit()


def _principal(
    *, revenue: bool = True, payments: bool = True, change: bool = True, month: str = MONTH
):
    """Global finance principal; flags drop one gate at a time for the 403 cases.

    `month` scopes the two finance-month grants; tests targeting a different
    month (e.g. a factless month for the pre-flight 409) must pass it so the
    write-only VIEW_FINALIZED_PAYMENTS gate is satisfied before the pre-flight.
    """
    grants = []
    if revenue:
        grants.append(PermissionGrant(Permission.VIEW_REVENUE, AccessScope.global_scope()))
    if payments:
        grants.append(
            PermissionGrant(Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month(month))
        )
    if change:
        grants.append(
            PermissionGrant(Permission.CHANGE_ALLOCATION_RULE, AccessScope.finance_month(month))
        )
    return UserPrincipal(
        user_id=str(USER_ID), email="recalc@example.com",
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


def _run_rows(database_url: str):
    """Return all persisted committed_allocation_runs rows."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        return list(session.scalars(select(CommittedAllocationRunORM)).all())


def _write_payload(**overrides) -> dict[str, object]:
    """Return a default dry_run=false global gross recalculate body with overrides."""
    payload: dict[str, object] = {
        "month": MONTH,
        "scope_type": "global",
        "scope_id": None,
        "allocation_method": "gross_revenue_proportional",
        "currency": "USD",
        "dry_run": False,
        "idempotency_key": "rk-1",
        "reason": "Recalculate and commit March allocation",
    }
    payload.update(overrides)
    return payload


def test_dry_run_true_response_unchanged_and_no_run_row(tmp_path):
    """dry_run=true is byte-identical: NO_WRITES_PERFORMED + zero committed runs."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(RECALC_PATH, json=_write_payload(dry_run=True, idempotency_key=None))
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["write_status"] == "NO_WRITES_PERFORMED"
    assert "committed_run" not in body
    assert _run_rows(db) == []


def test_write_gross_commits_run_and_audit_201(tmp_path):
    """dry_run=false gross -> 201, version-1 run, COMMITTED, ALLOCATION_COMMITTED audit."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(RECALC_PATH, json=_write_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert body["dry_run"] is False
    assert body["write_status"] == "COMMITTED"
    assert body["committed_run"]["commit_version"] == 1
    assert body["commit_audit_event"]["event_type"] == "ALLOCATION_COMMITTED"
    assert body["audit_event"]["event_type"] == "RECALCULATION_REQUESTED"
    runs = _run_rows(db)
    assert len(runs) == 1
    assert runs[0].commit_version == 1
    assert len(_committed_audit_rows(db)) == 1


def test_write_post_tax_commits_201(tmp_path):
    """dry_run=false post_tax with non-null source net commits 201."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    _set_source_net(db, Decimal("800.00"))
    client = _client(db, _principal)
    resp = client.post(
        RECALC_PATH,
        json=_write_payload(allocation_method="post_tax_revenue_proportional"),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["write_status"] == "COMMITTED"
    assert body["committed_run"]["allocation_method"] == "post_tax_revenue_proportional"


def test_write_company_level_commits_201(tmp_path):
    """dry_run=false company_level commits via the real org channel->company index."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(RECALC_PATH, json=_write_payload(allocation_method="company_level"))
    assert resp.status_code == 201
    body = resp.json()
    assert body["write_status"] == "COMMITTED"
    assert body["committed_run"]["allocation_method"] == "company_level"


def test_write_no_allocation_commits_withheld_set_201(tmp_path):
    """dry_run=false no_allocation -> 201, zero lines, INTENTIONAL_NO_ALLOCATION set."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(RECALC_PATH, json=_write_payload(allocation_method="no_allocation"))
    assert resp.status_code == 201
    body = resp.json()
    assert body["write_status"] == "COMMITTED"
    assert body["committed_run"]["allocation_method"] == "no_allocation"
    assert body["committed_run"]["summary"]["allocated_component_count"] == 0
    assert body["committed_run"]["summary"]["unallocated_component_count"] == 1


def test_write_replay_same_key_returns_200_idempotent(tmp_path):
    """Replaying the same key + body -> 200 IDEMPOTENT_REPLAY, no second audit row."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    first = client.post(RECALC_PATH, json=_write_payload())
    second = client.post(RECALC_PATH, json=_write_payload())
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["write_status"] == "IDEMPOTENT_REPLAY"
    assert second.json()["commit_audit_event"] is None
    assert second.json()["committed_run"]["run_id"] == first.json()["committed_run"]["run_id"]
    assert len(_committed_audit_rows(db)) == 1
    assert len(_run_rows(db)) == 1


def test_write_same_key_different_reason_conflicts_409(tmp_path):
    """Same key, different reason (different fingerprint) -> 409."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    client.post(RECALC_PATH, json=_write_payload(reason="first reason"))
    conflict = client.post(RECALC_PATH, json=_write_payload(reason="second reason"))
    assert conflict.status_code == 409


def test_write_manual_method_rejected_422_with_redirect(tmp_path):
    """dry_run=false manual -> 422 redirecting to the explicit-lines commit endpoint."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(RECALC_PATH, json=_write_payload(allocation_method="manual"))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "manual allocation requires explicit lines" in detail
    assert "account-allocations/commit" in detail
    assert _run_rows(db) == []


def test_write_without_idempotency_key_rejected_422(tmp_path):
    """dry_run=false with no idempotency_key -> 422, no run row, no audit."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(RECALC_PATH, json=_write_payload(idempotency_key=None))
    assert resp.status_code == 422
    assert "idempotency_key" in resp.json()["detail"]
    assert _run_rows(db) == []


def test_write_non_global_scope_rejected_422(tmp_path):
    """dry_run=false with scope_type=company -> 422 (the snapshot is whole-month)."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    resp = client.post(
        RECALC_PATH,
        json=_write_payload(scope_type="company", scope_id=str(COMPANY_ID)),
    )
    assert resp.status_code == 422
    assert "scope_type=global" in resp.json()["detail"]
    assert _run_rows(db) == []


def test_write_no_facts_blocked_by_issues_409(tmp_path):
    """dry_run=false gross on a factless month -> 409 BLOCKED_BY_ISSUES (pre-flight)."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)

    def _april_principal():
        """Principal whose finance-month grants cover the factless 2026-04 month."""
        return _principal(month="2026-04")

    client = _client(db, _april_principal)
    resp = client.post(RECALC_PATH, json=_write_payload(month="2026-04"))
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["write_status"] == "BLOCKED_BY_ISSUES"
    assert detail["blocking_issues"]
    assert _run_rows(db) == []


def test_write_locked_month_conflicts_409_via_service(tmp_path):
    """A LOCKED month with valid facts rejects with 409.

    LAYER: the recalculation preview has no month-lock awareness (it reads only
    facts/overrides/channel_company), so a LOCKED month with valid gross facts
    previews READY (no blocking issues) and passes the pre-flight gate. The
    committed-allocation SERVICE then raises CommittedAllocationLockedMonthError,
    which the route translates to 409. So the LOCKED rejection lands at the
    service layer, not the pre-flight BLOCKED_BY_ISSUES path.
    """
    db = build_database_url(tmp_path)
    _seed(db, mapped=True, status="LOCKED")
    client = _client(db, _principal)
    resp = client.post(RECALC_PATH, json=_write_payload())
    assert resp.status_code == 409
    # Not the pre-flight BLOCKED_BY_ISSUES dict; the service emits a string detail.
    assert isinstance(resp.json()["detail"], str)
    assert _run_rows(db) == []


def test_write_missing_view_finalized_payments_403_but_dry_run_ok(tmp_path):
    """Missing VIEW_FINALIZED_PAYMENTS 403s the write; dry_run stays 200 (write-only gate)."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)

    def _no_payments():
        """Principal that holds VIEW_REVENUE + CHANGE_ALLOCATION_RULE but not payments."""
        return _principal(payments=False)

    client = _client(db, _no_payments)
    write = client.post(RECALC_PATH, json=_write_payload())
    assert write.status_code == 403
    assert _run_rows(db) == []
    dry = client.post(RECALC_PATH, json=_write_payload(dry_run=True, idempotency_key=None))
    assert dry.status_code == 200


def test_write_missing_change_allocation_rule_403(tmp_path):
    """Missing CHANGE_ALLOCATION_RULE 403s the write."""
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)

    def _no_change():
        """Principal without CHANGE_ALLOCATION_RULE."""
        return _principal(change=False)

    client = _client(db, _no_change)
    resp = client.post(RECALC_PATH, json=_write_payload())
    assert resp.status_code == 403
    assert _run_rows(db) == []


def test_cross_endpoint_idempotency_commit_then_recalc_replays_same_run(tmp_path):
    """A commit with key K replays as the SAME run when recalc-write reuses K+method+reason.

    The shared commit_request_fingerprint gives both endpoints idempotency
    coherence: same month + key + method + reason -> the recalc write replays the
    existing committed run (200 IDEMPOTENT_REPLAY, same run id), no second audit.
    """
    db = build_database_url(tmp_path)
    _seed(db, mapped=True)
    client = _client(db, _principal)
    commit_path = f"/revenue/months/{MONTH}/account-allocations/commit"
    reason = "shared idempotency reason"
    commit = client.post(
        commit_path,
        json={"idempotency_key": "shared-k", "reason": reason,
              "allocation_method": "gross_revenue_proportional"},
    )
    assert commit.status_code == 201
    recalc = client.post(
        RECALC_PATH,
        json=_write_payload(idempotency_key="shared-k", reason=reason),
    )
    assert recalc.status_code == 200
    assert recalc.json()["write_status"] == "IDEMPOTENT_REPLAY"
    assert recalc.json()["committed_run"]["run_id"] == commit.json()["run"]["run_id"]
    assert len(_run_rows(db)) == 1
    assert len(_committed_audit_rows(db)) == 1


def test_recalculate_openapi_documents_both_success_statuses(tmp_path):
    """The published OpenAPI contract advertises BOTH 201 (commit) and 200 (replay)."""
    client = _client(build_database_url(tmp_path), _principal)
    schema = client.get("/openapi.json").json()
    responses = schema["paths"]["/revenue/recalculate"]["post"]["responses"]
    assert "201" in responses, "recalculate write must document the 201 create response"
    assert "200" in responses, "recalculate write must document the 200 replay response"


def test_recalculate_openapi_documents_409_conflict(tmp_path):
    """The published OpenAPI contract advertises the 409 the write path can return.

    The write path 409s in two client-relevant shapes: the pre-flight
    BLOCKED_BY_ISSUES dict and the service-layer LOCKED-month / idempotency-key
    plain-string detail. A schema-generated client must see 409 to handle them.
    """
    client = _client(build_database_url(tmp_path), _principal)
    schema = client.get("/openapi.json").json()
    responses = schema["paths"]["/revenue/recalculate"]["post"]["responses"]
    assert "409" in responses, "recalculate write must document the 409 conflict response"
