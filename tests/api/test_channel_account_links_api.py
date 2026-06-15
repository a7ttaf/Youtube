"""Endpoint tests for the channel-account map (auth, audit, payload safety)."""

from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.api.channel_account_links import (
    _iter_months,
    _require_allocation_permission_for_range,
)
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    FinanceBase,
    FinanceMonthCloseORM,
)
from ums_smart_revenue.db.org_models import OrgBase
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM

USER_ID = UUID("00000000-0000-0000-0000-0000000d0401")


def auth_headers(role, scope_type="global", scope_id=None):
    """Return auth headers for the given role and scope."""
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "map-admin@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path):
    """Return a unique SQLite URL under tmp_path."""
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def seed(database_url):
    """Create schema and seed one UNVERIFIED link plus the test user."""
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(UserORM(id=USER_ID, email="map-admin@example.com", display_name="Map Admin"))
        session.add(
            AdsenseContentOwnerLinkORM(
                id=uuid4(),
                tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
                adsense_account_id="pub-1",
                content_owner_id="owner-1",
                verification_status="UNVERIFIED",
                provenance_kind="OPERATOR_ASSERTED",
                provenance_payload={"secret_provenance": "LEAK-1"},
                effective_month_start="2026-01",
            )
        )
        session.commit()


def test_finance_viewer_lists_links_without_provenance_payload(tmp_path):
    """Finance viewer can list links; provenance_payload is never in the response."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        "/revenue/channel-account-links",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total_count"] == 1
    assert body["links"][0]["adsense_account_id"] == "pub-1"
    assert "provenance_payload" not in str(body)
    assert "LEAK" not in str(body)
    assert {e["event_type"] for e in body["audit_events"]} == {"REVENUE_VIEWED", "PAYMENT_VIEWED"}
    engine = create_engine(database_url)
    with Session(engine) as session:
        logs = session.scalars(select(AuditLogORM)).all()
    assert {log.event_type for log in logs} == {"REVENUE_VIEWED", "PAYMENT_VIEWED"}


def test_list_denied_without_finance_view_permissions(tmp_path):
    """GET list returns 403 when the caller lacks finance-view permissions."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    # corporate_admin holds MANAGE_ORG_MAPPING but no finance-view perms -> 403, fail-closed.
    response = client.get(
        "/revenue/channel-account-links",
        headers=auth_headers("corporate_admin", "global"),
    )
    assert response.status_code == 403


def test_list_malformed_month_returns_422(tmp_path):
    """GET list with a malformed month filter returns 422."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        "/revenue/channel-account-links?month=2026-13",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 422


def test_propose_creates_link_with_org_mapping_permission(tmp_path):
    """POST propose creates an UNVERIFIED link for a caller with MANAGE_ORG_MAPPING."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.post(
        "/revenue/channel-account-links",
        headers=auth_headers("corporate_admin", "global"),
        json={
            "adsense_account_id": "pub-2",
            "content_owner_id": "owner-2",
            "effective_month_start": "2026-02",
            "effective_month_end": None,
            "provenance_kind": "OPERATOR_ASSERTED",
            "provenance_payload": {"note": "from contract"},
            "reason": "operator asserts pub-2 maps to owner-2",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["link"]["verification_status"] == "UNVERIFIED"
    assert "provenance_payload" not in str(body)
    assert body["audit_event"]["event_type"] == "CHANNEL_ACCOUNT_LINK_PROPOSED"


def test_propose_requires_manage_org_mapping(tmp_path):
    """POST propose returns 403 without MANAGE_ORG_MAPPING permission."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.post(
        "/revenue/channel-account-links",
        headers=auth_headers(
            "finance_viewer", "global"
        ),  # has finance view, NOT MANAGE_ORG_MAPPING
        json={
            "adsense_account_id": "pub-2",
            "content_owner_id": "owner-2",
            "effective_month_start": "2026-02",
            "effective_month_end": None,
            "provenance_kind": "OPERATOR_ASSERTED",
            "provenance_payload": {},
            "reason": "x",
        },
    )
    assert response.status_code == 403


def _propose_via_api(client, *, account="pub-9", owner="owner-9", start="2026-01", end=None):
    """POST a proposal via the API and return the created link id."""
    return client.post(
        "/revenue/channel-account-links",
        headers=auth_headers("super_owner", "global"),
        json={
            "adsense_account_id": account,
            "content_owner_id": owner,
            "effective_month_start": start,
            "effective_month_end": end,
            "provenance_kind": "OPERATOR_ASSERTED",
            "provenance_payload": {},
            "reason": "seed",
        },
    ).json()["link"]["id"]


def test_super_owner_can_verify(tmp_path):
    """super_owner can verify a link; audit event type is CHANNEL_ACCOUNT_LINK_VERIFIED."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    link_id = _propose_via_api(client)
    response = client.post(
        f"/revenue/channel-account-links/{link_id}/verify",
        headers=auth_headers("super_owner", "global"),
        json={"reason": "verified against signed contract"},
    )
    assert response.status_code == 200
    assert response.json()["link"]["verification_status"] == "VERIFIED"
    assert response.json()["audit_event"]["event_type"] == "CHANNEL_ACCOUNT_LINK_VERIFIED"


def test_verify_records_full_effective_range_in_audit_details(tmp_path):
    """A multi-month verify records both range bounds in the audit details.

    Regression for PR #57 -GSp: the audit scope is the single start month, so a
    later (now closed) month's audit review would not surface the mutation that
    changed its allocation eligibility unless the full [start, end] range is
    captured in details. Assert against the persisted audit row, not the API
    response (which omits details).
    """
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    link_id = _propose_via_api(
        client, account="pub-9", owner="owner-9", start="2026-01", end="2026-12"
    )
    response = client.post(
        f"/revenue/channel-account-links/{link_id}/verify",
        headers=auth_headers("super_owner", "global"),
        json={"reason": "verified across the full year"},
    )
    assert response.status_code == 200
    engine = create_engine(database_url)
    with Session(engine) as session:
        verified = session.scalars(
            select(AuditLogORM).where(AuditLogORM.event_type == "CHANNEL_ACCOUNT_LINK_VERIFIED")
        ).all()
    assert len(verified) == 1
    details = verified[0].details
    assert details["effective_month_start"] == "2026-01"
    assert details["effective_month_end"] == "2026-12"


def test_verify_requires_both_permissions(tmp_path):
    """Verify returns 403 if either MANAGE_ORG_MAPPING or CHANGE_ALLOCATION_RULE is missing."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    link_id = _propose_via_api(client)
    # corporate_admin: MANAGE_ORG_MAPPING but NOT CHANGE_ALLOCATION_RULE -> 403.
    r1 = client.post(
        f"/revenue/channel-account-links/{link_id}/verify",
        headers=auth_headers("corporate_admin", "global"),
        json={"reason": "x"},
    )
    assert r1.status_code == 403
    # finance_admin: CHANGE_ALLOCATION_RULE but NOT MANAGE_ORG_MAPPING -> 403.
    r2 = client.post(
        f"/revenue/channel-account-links/{link_id}/verify",
        headers=auth_headers("finance_admin", "global"),
        json={"reason": "x"},
    )
    assert r2.status_code == 403


def test_verify_overlap_returns_409(tmp_path):
    """Verifying a link that overlaps an existing VERIFIED range returns 409."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    first = _propose_via_api(
        client, account="pub-9", owner="owner-1", start="2026-01", end="2026-06"
    )
    client.post(
        f"/revenue/channel-account-links/{first}/verify",
        headers=auth_headers("super_owner", "global"),
        json={"reason": "r1"},
    )
    second = _propose_via_api(client, account="pub-9", owner="owner-2", start="2026-03", end=None)
    response = client.post(
        f"/revenue/channel-account-links/{second}/verify",
        headers=auth_headers("super_owner", "global"),
        json={"reason": "r2"},
    )
    assert response.status_code == 409


def test_reject_sets_rejected(tmp_path):
    """POST reject transitions a link to REJECTED with an audit event."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    link_id = _propose_via_api(client)
    response = client.post(
        f"/revenue/channel-account-links/{link_id}/reject",
        headers=auth_headers("super_owner", "global"),
        json={"reason": "operator rejects unverified mapping"},
    )
    assert response.status_code == 200
    assert response.json()["link"]["verification_status"] == "REJECTED"
    assert response.json()["audit_event"]["event_type"] == "CHANNEL_ACCOUNT_LINK_REJECTED"


def test_verify_with_malformed_actor_id_fails_clean(tmp_path):
    """A non-UUID X-User-Id returns 400, never 500."""
    # A non-UUID X-User-Id must produce a clean 4xx, never an uncaught 500,
    # even for an otherwise fully-authorized caller.
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    link_id = _propose_via_api(client)
    headers = auth_headers("super_owner", "global")
    headers["x-user-id"] = "not-a-uuid"
    response = client.post(
        f"/revenue/channel-account-links/{link_id}/verify",
        headers=headers,
        json={"reason": "x"},
    )
    assert response.status_code == 400


def test_verify_locked_month_returns_409(tmp_path):
    """Verify on a LOCKED finance month returns 409 CONFLICT."""
    database_url = build_database_url(tmp_path)
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(UserORM(id=USER_ID, email="map-admin@example.com", display_name="Map Admin"))
        session.add(FinanceMonthCloseORM(month="2026-09", status="LOCKED"))
        session.commit()
    client = TestClient(create_app(database_url=database_url))
    link_id = _propose_via_api(client, account="pub-9", owner="owner-9", start="2026-09")
    response = client.post(
        f"/revenue/channel-account-links/{link_id}/verify",
        headers=auth_headers("super_owner", "global"),
        json={"reason": "attempt on locked month"},
    )
    assert response.status_code == 409
    assert "locked" in response.json()["detail"].lower()


def test_propose_canonicalizes_adsense_account_id_prefix(tmp_path):
    """POST with accounts/ prefix stores the id with the prefix stripped."""
    database_url = build_database_url(tmp_path)
    seed(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.post(
        "/revenue/channel-account-links",
        headers=auth_headers("corporate_admin", "global"),
        json={
            "adsense_account_id": "accounts/pub-99",
            "content_owner_id": "owner-9",
            "effective_month_start": "2026-03",
            "effective_month_end": None,
            "provenance_kind": "OPERATOR_ASSERTED",
            "provenance_payload": {},
            "reason": "canonicalization test",
        },
    )
    assert response.status_code == 201
    assert response.json()["link"]["adsense_account_id"] == "pub-99"

    # Surrounding whitespace + the accounts/ prefix together: must strip then
    # canonicalize (locks in the validator-ordering fix — a padded value reached
    # _validated_account_id un-stripped and was wrongly rejected with 422).
    padded = client.post(
        "/revenue/channel-account-links",
        headers=auth_headers("corporate_admin", "global"),
        json={
            "adsense_account_id": "  accounts/pub-100  ",
            "content_owner_id": "owner-10",
            "effective_month_start": "2026-04",
            "effective_month_end": None,
            "provenance_kind": "OPERATOR_ASSERTED",
            "provenance_payload": {},
            "reason": "whitespace + prefix canonicalization",
        },
    )
    assert padded.status_code == 201
    assert padded.json()["link"]["adsense_account_id"] == "pub-100"


def test_iter_months_inclusive_with_year_rollover():
    """_iter_months yields each YYYY-MM start..end inclusive, rolling over years."""
    assert _iter_months("2026-03", "2026-03") == ["2026-03"]
    assert _iter_months("2026-11", "2027-02") == [
        "2026-11",
        "2026-12",
        "2027-01",
        "2027-02",
    ]


def test_allocation_permission_gated_for_whole_range():
    """CHANGE_ALLOCATION_RULE must cover every month a verified link is consumed.

    A caller scoped to only the start month must not approve a multi-month or
    open-ended mapping; a global allocation grant authorizes any range.
    """
    month_scoped = UserPrincipal(
        user_id=str(USER_ID),
        email="map-admin@example.com",
        direct_permissions=(
            PermissionGrant(
                Permission.CHANGE_ALLOCATION_RULE,
                AccessScope.finance_month("2026-01"),
            ),
        ),
    )
    # Single-month link fully within the grant: allowed (no raise).
    _require_allocation_permission_for_range(month_scoped, "2026-01", "2026-01")
    # Multi-month range extends past the granted month: 403.
    with pytest.raises(HTTPException) as multi:
        _require_allocation_permission_for_range(month_scoped, "2026-01", "2026-06")
    assert multi.value.status_code == 403
    # Open-ended range needs a global grant: 403 for a month-scoped caller.
    with pytest.raises(HTTPException) as open_ended:
        _require_allocation_permission_for_range(month_scoped, "2026-01", None)
    assert open_ended.value.status_code == 403

    global_scoped = UserPrincipal(
        user_id=str(USER_ID),
        email="map-admin@example.com",
        direct_permissions=(
            PermissionGrant(Permission.CHANGE_ALLOCATION_RULE, AccessScope.global_scope()),
        ),
    )
    # Global allocation grant authorizes both a bounded and an open-ended range.
    _require_allocation_permission_for_range(global_scoped, "2026-01", "2026-12")
    _require_allocation_permission_for_range(global_scoped, "2026-01", None)


def test_global_grant_short_circuits_bounded_range_without_iteration(monkeypatch):
    """A global CHANGE_ALLOCATION_RULE grant authorizes a far-future bounded range
    via one check -- the per-month loop must NOT run (no ~95k iterations)."""

    def _boom(start, end):
        raise AssertionError("_iter_months must not run for a global-grant holder (short-circuit)")

    monkeypatch.setattr("ums_smart_revenue.api.channel_account_links._iter_months", _boom)
    global_scoped = UserPrincipal(
        user_id=str(USER_ID),
        email="map-admin@example.com",
        direct_permissions=(
            PermissionGrant(Permission.CHANGE_ALLOCATION_RULE, AccessScope.global_scope()),
        ),
    )
    # Under the old per-month loop this far-future bound would iterate ~95k
    # months; the short-circuit returns without ever touching _iter_months.
    _require_allocation_permission_for_range(global_scoped, "2026-01", "9999-12")


def test_month_scoped_caller_still_iterates_bounded_range(monkeypatch):
    """A non-global caller must NOT be short-circuited: the per-month loop runs
    and a month outside the grant still 403s."""
    calls: list[tuple[str, str]] = []
    real_iter = _iter_months

    def _counting(start, end):
        calls.append((start, end))
        return real_iter(start, end)

    monkeypatch.setattr("ums_smart_revenue.api.channel_account_links._iter_months", _counting)
    month_scoped = UserPrincipal(
        user_id=str(USER_ID),
        email="map-admin@example.com",
        direct_permissions=(
            PermissionGrant(
                Permission.CHANGE_ALLOCATION_RULE,
                AccessScope.finance_month("2026-01"),
            ),
        ),
    )
    with pytest.raises(HTTPException) as exc:
        _require_allocation_permission_for_range(month_scoped, "2026-01", "2026-06")
    assert exc.value.status_code == 403
    # The loop ran (short-circuit did NOT fire for a non-global caller).
    assert calls == [("2026-01", "2026-06")]


def test_missing_and_disabled_global_grant_fail_closed():
    """No grant -> 403; a disabled principal with a global grant falls through the
    short-circuit (has_permission returns False for disabled) and is denied."""
    no_grant = UserPrincipal(
        user_id=str(USER_ID),
        email="map-admin@example.com",
        direct_permissions=(),
    )
    with pytest.raises(HTTPException) as missing:
        _require_allocation_permission_for_range(no_grant, "2026-01", "2026-03")
    assert missing.value.status_code == 403

    disabled_global = UserPrincipal(
        user_id=str(USER_ID),
        email="map-admin@example.com",
        disabled=True,
        direct_permissions=(
            PermissionGrant(Permission.CHANGE_ALLOCATION_RULE, AccessScope.global_scope()),
        ),
    )
    with pytest.raises(HTTPException) as disabled:
        _require_allocation_permission_for_range(disabled_global, "2026-01", "2026-03")
    assert disabled.value.status_code == 403


def test_contiguous_month_grants_cover_bounded_range():
    """A non-global caller granted a contiguous set of months passes a bounded
    range fully inside the grant, and 403s once it extends past."""
    two_month = UserPrincipal(
        user_id=str(USER_ID),
        email="map-admin@example.com",
        direct_permissions=(
            PermissionGrant(
                Permission.CHANGE_ALLOCATION_RULE,
                AccessScope.finance_month("2026-01"),
            ),
            PermissionGrant(
                Permission.CHANGE_ALLOCATION_RULE,
                AccessScope.finance_month("2026-02"),
            ),
        ),
    )
    # Both covered months: no raise.
    _require_allocation_permission_for_range(two_month, "2026-01", "2026-02")
    # Extends to an uncovered month: 403.
    with pytest.raises(HTTPException) as exc:
        _require_allocation_permission_for_range(two_month, "2026-01", "2026-03")
    assert exc.value.status_code == 403
