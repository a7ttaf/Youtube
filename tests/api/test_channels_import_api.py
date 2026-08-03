"""API tests for the bulk channel inventory import (POST /channels/import)."""

from fastapi.testclient import TestClient

from ums_smart_revenue.api.channels import (
    current_audit_sink,
    current_channel_registry,
)
from ums_smart_revenue.api.registry_dependencies import sql_group_registry_from_session
from ums_smart_revenue.api.revenue import current_org_access_index
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.org.bootstrap_registry import (
    BOOTSTRAP_COMPANY_TV_ID,
    BOOTSTRAP_ORG_INDEX,
)
from ums_smart_revenue.org.channel_groups import ChannelGroupRegistry
from ums_smart_revenue.org.channel_registry import bootstrap_channel_registry

CHANNEL_ID = "UCB6sc84dcg6VQGB_d89sx2g"
CONTENT_OWNER = "PlZrS5Fh56RMd9dmSL6XSA"
DEFAULT_HEADER = "youtube_channel_id,channel_name,view_revenue"


def auth_headers(role: str, scope_type: str, scope_id: str | None = None) -> dict[str, str]:
    """Build request headers for a principal with one role assignment."""
    headers = {
        "x-user-id": "user-1",
        "x-user-email": "user@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def create_import_app() -> tuple[TestClient, object, ChannelGroupRegistry, InMemoryAuditSink]:
    """Build an import-ready client plus the registries and sink it writes to."""
    app = create_app()
    registry = bootstrap_channel_registry()
    groups = ChannelGroupRegistry()
    audit_sink = InMemoryAuditSink()
    app.dependency_overrides[current_channel_registry] = lambda: registry
    app.dependency_overrides[sql_group_registry_from_session] = lambda: groups
    app.dependency_overrides[current_audit_sink] = lambda: audit_sink
    app.dependency_overrides[current_org_access_index] = lambda: BOOTSTRAP_ORG_INDEX
    return TestClient(app), registry, groups, audit_sink


def import_csv(*rows: str, header: str = DEFAULT_HEADER) -> str:
    """Assemble a CSV body from a header line and its data rows."""
    return "\n".join([header, *rows]) + "\n"


def post_import(client: TestClient, csv_text: str, **overrides) -> object:
    """POST one CSV to /channels/import as a global admin unless overridden.

    Any form field passed as ``None`` is omitted from the multipart body so a
    test can prove the route rejects a missing required field.
    """
    headers = overrides.pop("headers", None) or auth_headers("super_owner", "global")
    form: dict[str, object] = {
        "content_owner_id": CONTENT_OWNER,
        "cms_status": "INSIDE_CMS",
        "dry_run": "false",
        "reason": "Quarterly CMS roster load",
    }
    form.update(overrides)
    return client.post(
        "/channels/import",
        headers=headers,
        files={"file": ("roster.csv", csv_text, "text/csv")},
        data={key: value for key, value in form.items() if value is not None},
    )


def test_import_requires_global_manage_channels():
    """A company-scoped manager cannot run a roster-wide import."""
    client, registry, _groups, _sink = create_import_app()

    response = post_import(
        client,
        import_csv(f"{CHANNEL_ID},Alpha News,Yes"),
        headers=auth_headers("data_steward", "company", BOOTSTRAP_COMPANY_TV_ID),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: registry.manage_channels"
    assert registry.get_channel(CHANNEL_ID) is None


def test_dry_run_reports_create_and_writes_nothing():
    """A dry run reports the planned CREATE without touching the registry."""
    client, registry, _groups, _sink = create_import_app()

    response = post_import(
        client,
        import_csv(f"{CHANNEL_ID},Alpha News,Yes"),
        dry_run="true",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["counts"]["CREATE"] == 1
    assert registry.get_channel(CHANNEL_ID) is None


def test_dry_run_writes_no_audit_event():
    """A dry run is a preview, so it must not append any audit record."""
    client, _registry, _groups, audit_sink = create_import_app()

    response = post_import(
        client,
        import_csv(f"{CHANNEL_ID},Alpha News,Yes"),
        dry_run="true",
    )

    assert response.status_code == 200
    assert audit_sink.records == []


def test_apply_creates_the_channel():
    """Applying the import creates the channel with the requested inventory."""
    client, registry, _groups, _sink = create_import_app()

    response = post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,Yes"))

    assert response.status_code == 200
    assert response.json()["counts"]["CREATE"] == 1
    created = registry.get_channel(CHANNEL_ID)
    assert created is not None
    assert created.channel_name == "Alpha News"
    assert created.cms_status == "INSIDE_CMS"
    assert created.content_owner_id == CONTENT_OWNER
    assert created.revenue_required is True


def test_view_revenue_no_sets_revenue_not_required():
    """view_revenue=No imports the channel as performance-only."""
    client, registry, _groups, _sink = create_import_app()

    response = post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,No"))

    assert response.status_code == 200
    created = registry.get_channel(CHANNEL_ID)
    assert created is not None
    assert created.revenue_required is False


def test_any_row_error_blocks_the_whole_apply():
    """One malformed row rejects the file; no other row is written."""
    client, registry, _groups, audit_sink = create_import_app()

    response = post_import(
        client,
        import_csv(
            f"{CHANNEL_ID},Alpha News,Yes",
            "not-a-channel-id,Beta News,Yes",
        ),
    )

    assert response.status_code == 422
    assert response.json()["detail"]["counts"]["ERROR"] == 1
    assert registry.get_channel(CHANNEL_ID) is None
    assert audit_sink.records == []


def test_rerunning_the_same_file_is_unchanged():
    """Re-importing an identical roster is a no-op, not a rewrite."""
    client, _registry, _groups, _sink = create_import_app()
    csv_text = import_csv(f"{CHANNEL_ID},Alpha News,Yes")

    first = post_import(client, csv_text)
    second = post_import(client, csv_text)

    assert first.status_code == 200
    assert first.json()["counts"]["CREATE"] == 1
    assert second.status_code == 200
    assert second.json()["counts"]["UNCHANGED"] == 1
    assert second.json()["counts"]["UPDATE"] == 0


def test_changed_file_updates_the_channel():
    """A changed channel_name is applied as an UPDATE and persists."""
    client, registry, _groups, _sink = create_import_app()

    post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,Yes"))
    response = post_import(client, import_csv(f"{CHANNEL_ID},Alpha News HD,Yes"))

    assert response.status_code == 200
    assert response.json()["counts"]["UPDATE"] == 1
    updated = registry.get_channel(CHANNEL_ID)
    assert updated is not None
    assert updated.channel_name == "Alpha News HD"


def test_group_id_creates_group_and_membership():
    """A group_id column creates the CMS-keyed group and attaches the channel."""
    client, _registry, _groups, _sink = create_import_app()

    response = post_import(
        client,
        import_csv(
            f"{CHANNEL_ID},Alpha News,cms-group-7,Yes",
            header="youtube_channel_id,channel_name,group_id,view_revenue",
        ),
    )

    assert response.status_code == 200
    listing = client.get("/groups", headers=auth_headers("super_owner", "global"))
    assert listing.status_code == 200
    matches = [group for group in listing.json() if group["cms_group_id"] == "cms-group-7"]
    assert len(matches) == 1
    assert matches[0]["channel_ids"] == [CHANNEL_ID]


def test_group_membership_attaches_on_rerun_of_unchanged_rows():
    """A group_id added on re-import still attaches even when inventory is UNCHANGED."""
    client, registry, _groups, _sink = create_import_app()

    first = post_import(
        client,
        import_csv(
            f"{CHANNEL_ID},Alpha News",
            header="youtube_channel_id,channel_name",
        ),
    )

    assert first.status_code == 200
    assert first.json()["counts"]["CREATE"] == 1
    assert registry.get_channel(CHANNEL_ID) is not None

    second = post_import(
        client,
        import_csv(
            f"{CHANNEL_ID},Alpha News,cms-tv",
            header="youtube_channel_id,channel_name,group_id",
        ),
    )

    assert second.status_code == 200
    assert second.json()["counts"]["UNCHANGED"] == 1

    listing = client.get("/groups", headers=auth_headers("super_owner", "global"))
    assert listing.status_code == 200
    matches = [group for group in listing.json() if group["cms_group_id"] == "cms-tv"]
    assert len(matches) == 1
    assert matches[0]["channel_ids"] == [CHANNEL_ID]


def test_apply_writes_summary_and_per_channel_audit():
    """An applied import records both the file summary and each channel write."""
    client, _registry, _groups, audit_sink = create_import_app()

    response = post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,Yes"))

    assert response.status_code == 200
    event_types = {record.event_type for record in audit_sink.records}
    assert "CHANNEL_IMPORTED" in event_types
    assert "CHANNEL_CREATED" in event_types


def test_missing_reason_is_rejected():
    """The audit reason is required, so omitting it fails validation."""
    client, registry, _groups, _sink = create_import_app()

    response = post_import(
        client,
        import_csv(f"{CHANNEL_ID},Alpha News,Yes"),
        reason=None,
    )

    assert response.status_code == 422
    assert registry.get_channel(CHANNEL_ID) is None


def test_invalid_cms_status_is_rejected():
    """An unknown cms_status is rejected before any row is written."""
    client, registry, _groups, _sink = create_import_app()

    response = post_import(
        client,
        import_csv(f"{CHANNEL_ID},Alpha News,Yes"),
        cms_status="NOT_A_STATUS",
    )

    assert response.status_code == 422
    assert registry.get_channel(CHANNEL_ID) is None


def test_unknown_csv_header_is_rejected():
    """An unknown column fails the whole file instead of being dropped."""
    client, registry, _groups, _sink = create_import_app()

    response = post_import(
        client,
        import_csv(
            f"{CHANNEL_ID},Alpha News,1234.56",
            header="youtube_channel_id,channel_name,revenue_usd",
        ),
    )

    assert response.status_code == 422
    assert "revenue_usd" in response.json()["detail"]
    assert registry.get_channel(CHANNEL_ID) is None


def test_row_cap_is_enforced():
    """A file above the row cap is rejected rather than partially applied."""
    client, registry, _groups, _sink = create_import_app()
    rows = [f"UC{index:022d},Channel {index},Yes" for index in range(5001)]

    response = post_import(client, import_csv(*rows))

    assert response.status_code == 422
    assert "5000" in response.json()["detail"]
    assert registry.get_channel("UC" + "0" * 22) is None
