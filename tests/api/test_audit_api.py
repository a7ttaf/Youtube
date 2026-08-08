import csv
import json
from datetime import UTC, datetime, timedelta
from io import StringIO
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.api.channels import current_audit_sink
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM

USER_ID = UUID("00000000-0000-0000-0000-000000013001")


def auth_headers(role: str) -> dict[str, str]:
    """Build trusted-gateway headers for the requested test role."""
    return {
        "x-user-id": str(USER_ID),
        "x-user-email": f"{role}@example.com",
        "x-role": role,
        "x-scope-type": "global",
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }


def build_database_url(tmp_path) -> str:
    """Build a disposable SQLite database URL for the audit API tests."""
    return f"sqlite+pysqlite:///{(tmp_path / 'audit.db').as_posix()}"


def seed_database(database_url: str) -> None:
    """Seed the audit test database with representative audit rows."""
    engine = create_engine(database_url)
    SecurityBase.metadata.create_all(engine)
    created_at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
    with Session(engine) as session:
        session.add(UserORM(id=USER_ID, email="audit@example.com", display_name="Audit User"))
        session.add_all(
            [
                AuditLogORM(
                    id=uuid4(),
                    user_id=USER_ID,
                    event_type="REVENUE_VIEWED",
                    entity_type="monthly_channel_revenue_fact",
                    entity_id="channel-a:2026-03",
                    scope_type="channel",
                    scope_id="channel-a",
                    reason=None,
                    details={"gross_revenue_usd": "1000", "source": "YOUTUBE_CMS"},
                    is_sensitive=True,
                    created_at=created_at,
                ),
                AuditLogORM(
                    id=uuid4(),
                    user_id=USER_ID,
                    event_type="LOGIN",
                    entity_type="user_session",
                    entity_id=str(USER_ID),
                    scope_type="global",
                    scope_id=None,
                    reason=None,
                    details={"ip": "127.0.0.1"},
                    is_sensitive=False,
                    created_at=created_at - timedelta(minutes=5),
                ),
            ]
        )
        session.commit()


def test_audit_viewer_lists_audit_events_with_sensitive_details_masked(tmp_path):
    """List audit events and keep sensitive payloads masked for viewers."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    with TestClient(create_app(database_url=database_url)) as client:
        response = client.get("/audit/events?limit=10", headers=auth_headers("audit_viewer"))

        engine = create_engine(database_url)
        with Session(engine) as session:
            audit_events = session.scalars(
                select(AuditLogORM).order_by(AuditLogORM.created_at, AuditLogORM.id)
            ).all()

        assert response.status_code == 200
        assert response.json()["items"][0]["event_type"] == "REVENUE_VIEWED"
        assert response.json()["items"][0]["details"] == {}
        assert response.json()["items"][0]["details_redacted"] is True
        assert response.json()["items"][1]["details"] == {"ip": "127.0.0.1"}
        assert response.json()["items"][1]["details_redacted"] is False
        assert response.json()["pagination"] == {
            "limit": 10,
            "returned": 2,
            "has_more": False,
            "next_cursor": None,
        }
        assert response.json()["audit_event"]["event_type"] == "AUDIT_LOG_VIEWED"
        assert any(event.event_type == "AUDIT_LOG_VIEWED" for event in audit_events)

        next_response = client.get("/audit/events?limit=10", headers=auth_headers("audit_viewer"))

        assert next_response.status_code == 200
        assert [item["event_type"] for item in next_response.json()["items"]] == [
            "REVENUE_VIEWED",
            "LOGIN",
        ]


def test_audit_event_cursor_pagination_is_stable_when_new_events_arrive(tmp_path):
    """Keep cursor pagination stable even when newer rows arrive mid-read."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    with TestClient(create_app(database_url=database_url)) as client:
        first_page = client.get("/audit/events?limit=1", headers=auth_headers("audit_viewer"))
        assert first_page.status_code == 200
        assert "pagination" in first_page.json()
        assert "next_cursor" in first_page.json()["pagination"]
        next_cursor = first_page.json()["pagination"]["next_cursor"]
        assert next_cursor is not None

        engine = create_engine(database_url)
        with Session(engine) as session:
            session.add(
                AuditLogORM(
                    id=uuid4(),
                    user_id=USER_ID,
                    event_type="CONNECTOR_JOB_RUN",
                    entity_type="api_connector",
                    entity_id="youtube_reporting",
                    scope_type="connector",
                    scope_id="youtube_reporting",
                    reason=None,
                    details={},
                    is_sensitive=False,
                    created_at=datetime.now(UTC).replace(microsecond=0),
                )
            )
            session.commit()

        second_page = client.get(
            "/audit/events",
            headers=auth_headers("audit_viewer"),
            params={
                "limit": 1,
                "cursor_created_at": next_cursor["created_at"],
                "cursor_id": next_cursor["id"],
            },
        )
        assert second_page.status_code == 200
        assert "pagination" in second_page.json()
        assert "next_cursor" in second_page.json()["pagination"]

        assert [item["event_type"] for item in first_page.json()["items"]] == ["REVENUE_VIEWED"]
        assert [item["event_type"] for item in second_page.json()["items"]] == ["LOGIN"]


def test_super_owner_can_view_sensitive_audit_details(tmp_path):
    """Allow the super owner to see sensitive audit payloads."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)

    with TestClient(create_app(database_url=database_url)) as client:
        response = client.get(
            "/audit/events?event_type=REVENUE_VIEWED",
            headers=auth_headers("super_owner"),
        )

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["details"] == {
        "gross_revenue_usd": "1000",
        "source": "YOUTUBE_CMS",
    }
    assert response.json()["items"][0]["details_redacted"] is False


EXPORT_HEADER = (
    "created_at,event_type,user_id,entity_type,entity_id,scope_type,scope_id,"
    "request_id,reason,sensitive,details_redacted,details"
)


def test_audit_events_export_success_csv_shape(tmp_path):
    """Export CSV rows, shape, and the recorded audit-view permission."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    audit_sink = InMemoryAuditSink()

    app = create_app(database_url=database_url)
    app.dependency_overrides[current_audit_sink] = lambda: audit_sink

    with TestClient(app) as client:
        response = client.get(
            "/audit/events/export?event_type=LOGIN",
            headers=auth_headers("audit_viewer"),
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == 'attachment; filename="audit-events.csv"'
    assert "x-truncated" not in response.headers
    lines = response.text.splitlines()
    assert lines[0] == EXPORT_HEADER
    rows = list(csv.DictReader(StringIO(response.text)))
    assert len(rows) == 1
    assert rows[0]["event_type"] == "LOGIN"
    # created_at must be ISO-8601 (parseable), not a python repr.
    datetime.fromisoformat(rows[0]["created_at"])
    # Non-redacted details serialized as stable compact JSON.
    assert rows[0]["details"] == '{"ip":"127.0.0.1"}'
    assert rows[0]["details_redacted"] == "false"
    assert rows[0]["sensitive"] == "false"
    assert len(audit_sink.records) == 1
    assert audit_sink.records[0].event_type == "EXPORT_DOWNLOADED"
    assert audit_sink.records[0].permission == "audit.view"


def test_audit_events_export_denied_without_permission(tmp_path):
    """Return 403 and avoid export audit writes when permission is missing."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)

    with TestClient(create_app(database_url=database_url)) as client:
        response = client.get(
            "/audit/events/export",
            headers=auth_headers("assistant_analyst"),
        )

        assert response.status_code == 403
        assert response.json()["detail"] == "Missing permission: audit.view"

        # Fail-closed: no EXPORT_DOWNLOADED audit row written on denial.
        engine = create_engine(database_url)
        with Session(engine) as session:
            export_events = session.scalars(
                select(AuditLogORM).where(AuditLogORM.event_type == "EXPORT_DOWNLOADED")
            ).all()
        assert export_events == []


def test_audit_events_export_event_type_filter_narrows_rows(tmp_path):
    """Narrow the CSV export when an event_type filter is supplied."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)

    with TestClient(create_app(database_url=database_url)) as client:
        all_rows = list(
            csv.DictReader(
                StringIO(
                    client.get(
                        "/audit/events/export",
                        headers=auth_headers("audit_viewer"),
                    ).text
                )
            )
        )
        filtered = list(
            csv.DictReader(
                StringIO(
                    client.get(
                        "/audit/events/export?event_type=LOGIN",
                        headers=auth_headers("audit_viewer"),
                    ).text
                )
            )
        )

    assert {r["event_type"] for r in all_rows} == {"REVENUE_VIEWED", "LOGIN"}
    assert [r["event_type"] for r in filtered] == ["LOGIN"]


def test_audit_events_export_redacts_sensitive_for_plain_viewer(tmp_path):
    """Keep sensitive export rows redacted for a plain audit viewer."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)

    with TestClient(create_app(database_url=database_url)) as client:
        viewer = client.get(
            "/audit/events/export?event_type=REVENUE_VIEWED",
            headers=auth_headers("audit_viewer"),
        )
        owner = client.get(
            "/audit/events/export?event_type=REVENUE_VIEWED",
            headers=auth_headers("super_owner"),
        )

    viewer_rows = list(csv.DictReader(StringIO(viewer.text)))
    assert viewer_rows[0]["details"] == ""
    assert viewer_rows[0]["details_redacted"] == "true"
    assert "YOUTUBE_CMS" not in viewer.text
    assert "gross_revenue_usd" not in viewer.text

    owner_rows = list(csv.DictReader(StringIO(owner.text)))
    assert owner_rows[0]["details_redacted"] == "false"
    assert "YOUTUBE_CMS" in owner_rows[0]["details"]


def test_audit_events_export_ignores_cursor_params(tmp_path):
    """Ignore cursor params so CSV exports always start from the beginning."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)

    # Filter to a seeded event_type so accrued EXPORT_DOWNLOADED self-events
    # from earlier calls do not perturb the comparison.
    with TestClient(create_app(database_url=database_url)) as client:
        plain = client.get(
            "/audit/events/export?event_type=REVENUE_VIEWED",
            headers=auth_headers("audit_viewer"),
        )
        with_cursor = client.get(
            "/audit/events/export",
            headers=auth_headers("audit_viewer"),
            params={
                "event_type": "REVENUE_VIEWED",
                "cursor_created_at": "2026-05-10T12:00:00+00:00",
                "cursor_id": str(uuid4()),
            },
        )

    plain_rows = list(csv.DictReader(StringIO(plain.text)))
    cursor_rows = list(csv.DictReader(StringIO(with_cursor.text)))
    # Cursor params are ignored: export starts from the beginning either way.
    assert [r["entity_id"] for r in plain_rows] == [r["entity_id"] for r in cursor_rows]
    assert len(cursor_rows) == 1


def test_audit_events_export_caps_rows_and_sets_truncated(tmp_path, monkeypatch):
    """Cap large CSV exports and surface truncation via the response header."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    monkeypatch.setattr("ums_smart_revenue.api.audit.AUDIT_EXPORT_MAX_ROWS", 1)

    with TestClient(create_app(database_url=database_url)) as client:
        capped = client.get(
            "/audit/events/export",
            headers=auth_headers("audit_viewer"),
        )

    capped_rows = list(csv.DictReader(StringIO(capped.text)))
    assert len(capped_rows) == 1
    assert capped.headers["x-truncated"] == "true"


def test_audit_events_export_no_truncated_header_under_cap(tmp_path):
    """Omit the truncation header when the CSV stays within the row cap."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)

    with TestClient(create_app(database_url=database_url)) as client:
        full = client.get(
            "/audit/events/export",
            headers=auth_headers("audit_viewer"),
        )

    assert len(list(csv.DictReader(StringIO(full.text)))) == 2
    assert "x-truncated" not in full.headers


def test_audit_events_export_snapshot_excludes_own_event(tmp_path):
    """Snapshot the CSV before emitting its own export audit event."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)

    with TestClient(create_app(database_url=database_url)) as client:
        first = client.get(
            "/audit/events/export",
            headers=auth_headers("audit_viewer"),
        )
        # First export's CSV must not contain its own download event.
        assert "audit_events_export" not in first.text
        assert "EXPORT_DOWNLOADED" not in first.text

        # But a privileged list afterward DOES show the first export's event.
        listing = client.get(
            "/audit/events?event_type=EXPORT_DOWNLOADED",
            headers=auth_headers("super_owner"),
        )

    items = listing.json()["items"]
    assert any(item["event_type"] == "EXPORT_DOWNLOADED" for item in items)
    assert any(item["entity_type"] == "audit_events_export" for item in items)


def test_audit_events_export_guards_formula_injection(tmp_path):
    """Escape spreadsheet formula prefixes in the exported CSV."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            AuditLogORM(
                id=uuid4(),
                user_id=USER_ID,
                event_type="LOGIN",
                entity_type="user_session",
                entity_id=str(USER_ID),
                scope_type="global",
                scope_id=None,
                reason="=cmd|'/C calc'!A1",
                details={},
                is_sensitive=False,
                created_at=datetime.now(UTC).replace(microsecond=0),
            )
        )
        session.commit()

    with TestClient(create_app(database_url=database_url)) as client:
        response = client.get(
            "/audit/events/export?event_type=LOGIN",
            headers=auth_headers("audit_viewer"),
        )

    rows = list(csv.DictReader(StringIO(response.text)))
    reasons = [r["reason"] for r in rows]
    assert "'=cmd|'/C calc'!A1" in reasons


def test_assistant_cannot_view_audit_events(tmp_path):
    """Reject audit log reads for the assistant analyst role."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)

    with TestClient(create_app(database_url=database_url)) as client:
        response = client.get("/audit/events", headers=auth_headers("assistant_analyst"))

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: audit.view"


def _add_audit_row(
    database_url: str,
    *,
    event_type: str,
    sensitive: bool = False,
    created_at: datetime | None = None,
    tenant_id: UUID | None = None,
) -> None:
    """Append a single audit row to the seeded test database."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        kwargs: dict[str, object] = dict(
            id=uuid4(),
            user_id=USER_ID if tenant_id is None else None,
            event_type=event_type,
            entity_type="user_session",
            entity_id=str(USER_ID),
            scope_type="global",
            scope_id=None,
            reason=None,
            details={},
            is_sensitive=sensitive,
            created_at=created_at or datetime.now(UTC).replace(microsecond=0),
        )
        if tenant_id is not None:
            kwargs["tenant_id"] = tenant_id
        session.add(AuditLogORM(**kwargs))
        session.commit()


def _audit_table_count(database_url: str) -> int:
    """Return the total number of audit rows across all tenants."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        return len(session.scalars(select(AuditLogORM)).all())


def _audit_rows_by_type(database_url: str, event_type: str) -> list[dict[str, object]]:
    """Return every audit row of the given event_type as plain dicts."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        rows = session.scalars(
            select(AuditLogORM).where(AuditLogORM.event_type == event_type)
        ).all()
        return [
            {
                "event_type": row.event_type,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "details": json.loads(row.details)
                if isinstance(row.details, str)
                else dict(row.details or {}),
            }
            for row in rows
        ]


def test_audit_summary_returns_counts_for_viewer(tmp_path):
    """Return aggregate counts with the four contract fields for a viewer."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)

    with TestClient(create_app(database_url=database_url)) as client:
        response = client.get("/audit/summary", headers=auth_headers("audit_viewer"))

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "total_events",
        "sensitive_events",
        "recent_count",
        "window_hours",
    }
    assert isinstance(body["total_events"], int)
    assert isinstance(body["sensitive_events"], int)
    assert isinstance(body["recent_count"], int)
    assert isinstance(body["window_hours"], int)
    # Seed: one sensitive REVENUE_VIEWED + one LOGIN, both recent.
    assert body["total_events"] == 2
    assert body["sensitive_events"] == 1
    assert body["recent_count"] == 2
    assert body["window_hours"] == 24


def test_audit_summary_denied_for_assistant_analyst(tmp_path):
    """Return 403 fail-closed and write no audit row for the analyst role."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    before = _audit_table_count(database_url)

    with TestClient(create_app(database_url=database_url)) as client:
        response = client.get("/audit/summary", headers=auth_headers("assistant_analyst"))

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: audit.view"
    # Fail-closed: denial writes no audit row.
    assert _audit_table_count(database_url) == before


def test_audit_summary_counts_sensitive_rows(tmp_path):
    """Count only sensitive rows in the sensitive_events aggregate."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _add_audit_row(database_url, event_type="OVERRIDE_APPLIED", sensitive=True)

    with TestClient(create_app(database_url=database_url)) as client:
        body = client.get("/audit/summary", headers=auth_headers("audit_viewer")).json()

    # Two sensitive rows (seeded REVENUE_VIEWED + added OVERRIDE_APPLIED).
    assert body["total_events"] == 3
    assert body["sensitive_events"] == 2


def test_audit_summary_recent_count_respects_window_hours(tmp_path):
    """Exclude rows outside the window, include them under a wider window."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    old_created = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=48)
    _add_audit_row(database_url, event_type="LOGIN", created_at=old_created)

    with TestClient(create_app(database_url=database_url)) as client:
        narrow = client.get(
            "/audit/summary?window_hours=24", headers=auth_headers("audit_viewer")
        ).json()
        wide = client.get(
            "/audit/summary?window_hours=168", headers=auth_headers("audit_viewer")
        ).json()

    # Total lifetime is window-independent: 2 seeded + 1 old = 3.
    assert narrow["total_events"] == 3
    assert wide["total_events"] == 3
    # The 48h-old row is excluded at 24h, included at 168h.
    assert narrow["recent_count"] == 2
    assert narrow["window_hours"] == 24
    assert wide["recent_count"] == 3
    assert wide["window_hours"] == 168


def test_audit_summary_excludes_audit_log_viewed(tmp_path):
    """Exclude AUDIT_LOG_VIEWED rows from every summary count."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _add_audit_row(database_url, event_type="AUDIT_LOG_VIEWED", sensitive=True)

    with TestClient(create_app(database_url=database_url)) as client:
        body = client.get("/audit/summary", headers=auth_headers("audit_viewer")).json()

    # The AUDIT_LOG_VIEWED row is not counted in any aggregate.
    assert body["total_events"] == 2
    assert body["sensitive_events"] == 1
    assert body["recent_count"] == 2


def test_audit_summary_rejects_out_of_range_window_hours(tmp_path):
    """Return 422 for window_hours below 1 or above 8760."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)

    with TestClient(create_app(database_url=database_url)) as client:
        too_small = client.get(
            "/audit/summary?window_hours=0", headers=auth_headers("audit_viewer")
        )
        too_large = client.get(
            "/audit/summary?window_hours=8761",
            headers=auth_headers("audit_viewer"),
        )

    assert too_small.status_code == 422
    assert too_large.status_code == 422


def test_audit_summary_writes_excluded_audit_log_viewed_row(tmp_path):
    """Record a self-audit row that the count query excludes from itself.

    The summary endpoint must remain auditable (audit reads are themselves
    recorded), but the just-appended AUDIT_LOG_VIEWED row is excluded from
    the count_summary WHERE filter so the snapshot is not polluted by the
    read it just performed. This is the same self-audit / exclusion pattern
    used by the /audit/events list endpoint.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    before = _audit_table_count(database_url)

    with TestClient(create_app(database_url=database_url)) as client:
        response = client.get("/audit/summary", headers=auth_headers("audit_viewer"))
        body = response.json()
        # Capture the new row's identity before the request session closes.
        new_rows = _audit_rows_by_type(database_url, "AUDIT_LOG_VIEWED")

    assert response.status_code == 200
    # Exactly one AUDIT_LOG_VIEWED row was appended for this read.
    assert _audit_table_count(database_url) == before + 1
    assert len(new_rows) == 1
    new_row = new_rows[0]
    assert new_row["event_type"] == "AUDIT_LOG_VIEWED"
    assert new_row["entity_type"] == "audit_log_summary"
    assert new_row["entity_id"] == "summary"
    # The recorded counts reflect the snapshot taken BEFORE the row was
    # written, so the just-appended AUDIT_LOG_VIEWED row is not double-counted
    # in any aggregate it is itself a witness of.
    assert body["total_events"] == 2
    assert body["sensitive_events"] == 1
    assert body["recent_count"] == 2
    assert new_row["details"] == {
        "window_hours": 24,
        "total_events": 2,
        "sensitive_events": 1,
        "recent_count": 2,
        # The SQL sink persists the effective permission in details because
        # audit_logs has no permission column (PR #159 review: permission-
        # based filtering must work against the durable rows too).
        "permission": "audit.view",
    }


def test_audit_summary_is_tenant_scoped(tmp_path):
    """Exclude rows owned by a different tenant from the summary counts."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    other_tenant = UUID("00000000-0000-0000-0000-0000000000ff")
    _add_audit_row(
        database_url,
        event_type="LOGIN",
        sensitive=True,
        tenant_id=other_tenant,
    )

    with TestClient(create_app(database_url=database_url)) as client:
        body = client.get("/audit/summary", headers=auth_headers("audit_viewer")).json()

    # Cross-tenant row is invisible: counts stay at the seeded tenant totals.
    assert body["total_events"] == 2
    assert body["sensitive_events"] == 1
    assert body["recent_count"] == 2
