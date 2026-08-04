"""API tests for the bulk channel inventory import (POST /channels/import)."""

from fastapi.testclient import TestClient

from ums_smart_revenue.api.channels import (
    current_audit_sink,
    current_channel_registry,
)
from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.api.registry_dependencies import sql_group_registry_from_session
from ums_smart_revenue.api.revenue import current_org_access_index
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.audit_service import AuditRecord, InMemoryAuditSink
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.org.bootstrap_registry import (
    BOOTSTRAP_COMPANY_TV_ID,
    BOOTSTRAP_ORG_INDEX,
)
from ums_smart_revenue.org.channel_groups import ChannelGroupRegistry
from ums_smart_revenue.org.channel_registry import (
    ChannelRegistry,
    ChannelRegistryEntry,
    bootstrap_channel_registry,
)

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


def create_import_app(
    registry: ChannelRegistry | None = None,
) -> tuple[TestClient, object, ChannelGroupRegistry, InMemoryAuditSink]:
    """Build an import-ready client plus the registries and sink it writes to."""
    app = create_app()
    registry = registry if registry is not None else bootstrap_channel_registry()
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


def test_malformed_quoted_csv_is_rejected():
    """An unterminated quote rejects the whole file instead of folding rows."""
    client, registry, _groups, _sink = create_import_app()
    second = "UC3Dci3BzZXDo4jw4dU8KqWg"
    csv_text = f'{DEFAULT_HEADER}\n{CHANNEL_ID},"Alpha News,Yes\n{second},Beta,Yes\n'

    response = post_import(client, csv_text)

    assert response.status_code == 422
    assert "malformed CSV" in response.json()["detail"]
    assert registry.get_channel(CHANNEL_ID) is None


def test_archived_channel_is_a_row_error_not_a_500():
    """A roster row matching an archived channel is rejected per row, not crashed."""
    archived = ChannelRegistryEntry(
        youtube_channel_id=CHANNEL_ID,
        channel_name="Alpha News",
        primary_company_id=None,
        cms_status="INSIDE_CMS",
        revenue_required=True,
        content_owner_id=CONTENT_OWNER,
        active=False,
    )
    client, registry, _groups, audit_sink = create_import_app(ChannelRegistry([archived]))

    response = post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,Yes"))

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["counts"]["ERROR"] == 1
    assert "archived" in detail["rows"][0]["reason"]
    assert audit_sink.records == []
    stored = registry.get_channel(CHANNEL_ID)
    assert stored is not None and stored.active is False


def _sole_record(audit_sink: InMemoryAuditSink, event_type: str) -> AuditRecord:
    """Return the exactly-one audit record of this type, failing loudly otherwise."""
    matches = [r for r in audit_sink.records if r.event_type == event_type]
    assert len(matches) == 1, f"expected exactly one {event_type}, got {len(matches)}"
    return matches[0]


def test_group_rows_require_manage_groups_permission():
    """A channels-only principal cannot mutate groups through the import."""
    client, registry, _groups, _sink = create_import_app()
    channels_only = UserPrincipal(
        user_id="user-1",
        email="user@example.com",
        direct_permissions=(
            PermissionGrant(
                permission=Permission.MANAGE_CHANNELS,
                scope=AccessScope.global_scope(),
            ),
        ),
    )
    client.app.dependency_overrides[current_principal_from_headers] = lambda: channels_only

    with_groups = post_import(
        client,
        import_csv(
            f"{CHANNEL_ID},Alpha News,cms-tv,Yes",
            header="youtube_channel_id,channel_name,group_id,view_revenue",
        ),
    )
    without_groups = post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,Yes"))

    assert with_groups.status_code == 403
    assert with_groups.json()["detail"] == "Missing permission: registry.manage_groups"
    assert without_groups.status_code == 200, without_groups.text
    assert registry.get_channel(CHANNEL_ID) is not None


def test_dry_run_shows_planned_create_values():
    """The dry run echoes the exact inventory values a CREATE would write."""
    client, _registry, _groups, _sink = create_import_app()

    response = post_import(
        client,
        import_csv(
            f"{CHANNEL_ID},Alpha News,cms-tv,No",
            header="youtube_channel_id,channel_name,group_id,view_revenue",
        ),
        dry_run="true",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["content_owner_id"] == CONTENT_OWNER
    assert payload["cms_status"] == "INSIDE_CMS"
    row = payload["rows"][0]
    assert row["outcome"] == "CREATE"
    assert row["channel_name"] == "Alpha News"
    assert row["group_id"] == "cms-tv"
    assert row["revenue_required"] is False


def test_content_owner_is_normalized_at_the_boundary():
    """A padded owner value is stripped once and used for writes, plan, and audit."""
    client, registry, _groups, audit_sink = create_import_app()
    csv_text = import_csv(f"{CHANNEL_ID},Alpha News,Yes")

    first = post_import(client, csv_text, content_owner_id=f"  {CONTENT_OWNER}  ")
    rerun = post_import(client, csv_text, content_owner_id=f"  {CONTENT_OWNER}  ")

    assert first.status_code == 200
    created = registry.get_channel(CHANNEL_ID)
    assert created is not None and created.content_owner_id == CONTENT_OWNER
    # The rerun diffs the stripped value against the stored one: UNCHANGED,
    # not a phantom UPDATE on every run.
    assert rerun.status_code == 200
    assert rerun.json()["counts"]["UNCHANGED"] == 1
    summaries = [r for r in audit_sink.records if r.event_type == "CHANNEL_IMPORTED"]
    assert len(summaries) == 2, "each apply records one summary event"
    assert all(s.entity_id == CONTENT_OWNER for s in summaries)
    assert all(s.details["content_owner_id"] == CONTENT_OWNER for s in summaries)


def test_per_channel_audit_carries_raw_token_and_authorizing_permission():
    """Audit rows keep the operator's raw view_revenue token and MANAGE_CHANNELS."""
    client, _registry, _groups, audit_sink = create_import_app()

    post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,Yes"))
    post_import(client, import_csv(f"{CHANNEL_ID},Alpha News HD,TRUE"))

    created = _sole_record(audit_sink, "CHANNEL_CREATED")
    assert created.details["view_revenue_raw"] == "Yes"
    assert created.permission == Permission.MANAGE_CHANNELS.value
    updated = _sole_record(audit_sink, "CHANNEL_UPDATED")
    assert updated.details["view_revenue_raw"] == "TRUE"
    # The import authorizes on MANAGE_CHANNELS; the CHANNEL_UPDATED definition
    # default (manage_org_mapping) must be overridden in the record.
    assert updated.permission == Permission.MANAGE_CHANNELS.value


def test_absent_view_revenue_column_audits_raw_token_as_none():
    """Defaulted revenue_required is distinguishable from an explicit token."""
    client, _registry, _groups, audit_sink = create_import_app()

    post_import(
        client,
        import_csv(f"{CHANNEL_ID},Alpha News", header="youtube_channel_id,channel_name"),
    )

    created = _sole_record(audit_sink, "CHANNEL_CREATED")
    assert created.details["revenue_required"] is True
    assert created.details["view_revenue_raw"] is None


def test_group_mutations_performed_by_import_are_audited():
    """Group creation and membership additions each get a GROUP_UPDATED record."""
    client, _registry, _groups, audit_sink = create_import_app()
    header = "youtube_channel_id,channel_name,group_id,view_revenue"
    second = "UC3Dci3BzZXDo4jw4dU8KqWg"

    post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,cms-tv,Yes", header=header))
    post_import(
        client,
        import_csv(
            f"{CHANNEL_ID},Alpha News,cms-tv,Yes",
            f"{second},Beta News,cms-tv,Yes",
            header=header,
        ),
    )

    group_records = [r for r in audit_sink.records if r.event_type == "GROUP_UPDATED"]
    actions = [(r.details["action"], r.details["channel_id"]) for r in group_records]
    assert actions == [("group_created", CHANNEL_ID), ("member_added", second)]
    for record in group_records:
        assert record.entity_type == "channel_group"
        assert record.details["cms_group_id"] == "cms-tv"
        assert record.details["source"] == "bulk_import"
        assert record.reason == "Quarterly CMS roster load"


def test_unchanged_rows_do_not_emit_duplicate_group_audit():
    """Re-importing an unchanged membership adds no phantom GROUP_UPDATED rows."""
    client, _registry, _groups, audit_sink = create_import_app()
    header = "youtube_channel_id,channel_name,group_id,view_revenue"
    csv_text = import_csv(f"{CHANNEL_ID},Alpha News,cms-tv,Yes", header=header)

    post_import(client, csv_text)
    post_import(client, csv_text)

    group_records = [r for r in audit_sink.records if r.event_type == "GROUP_UPDATED"]
    assert [r.details["action"] for r in group_records] == ["group_created"]


def test_archived_group_is_a_row_error_not_a_write():
    """A roster row targeting an archived CMS group fails the file closed."""
    client, registry, groups, audit_sink = create_import_app()
    header = "youtube_channel_id,channel_name,group_id,view_revenue"
    # Create the group through one import, then archive it.
    first = post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,cms-tv,Yes", header=header))
    assert first.status_code == 200
    group = groups.get_group_by_cms_id("cms-tv")
    assert group is not None
    groups.update_group(group_id=group.id, name=None, active=False)
    audit_sink.records.clear()
    second_channel = "UC3Dci3BzZXDo4jw4dU8KqWg"

    response = post_import(
        client, import_csv(f"{second_channel},Beta News,cms-tv,Yes", header=header)
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["counts"]["ERROR"] == 1
    assert "archived" in detail["rows"][0]["reason"]
    assert registry.get_channel(second_channel) is None
    assert audit_sink.records == []
    archived = groups.get_group_by_cms_id("cms-tv")
    assert archived is not None and archived.channel_ids == (CHANNEL_ID,)


def test_channel_audit_details_carry_name_and_field_diff():
    """The durable audit trail records what changed with old and new values."""
    client, _registry, _groups, audit_sink = create_import_app()

    post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,Yes"))
    post_import(client, import_csv(f"{CHANNEL_ID},Alpha News HD,Yes"))

    created = _sole_record(audit_sink, "CHANNEL_CREATED")
    assert created.details["channel_name"] == "Alpha News"
    assert created.details["changes"] == {}
    updated = _sole_record(audit_sink, "CHANNEL_UPDATED")
    assert updated.details["channel_name"] == "Alpha News HD"
    assert updated.details["changes"] == {
        "channel_name": {"from": "Alpha News", "to": "Alpha News HD"}
    }


def test_import_rejects_missing_auth_headers():
    """No trusted-gateway identity means 401 before any parsing or writes."""
    client, registry, _groups, audit_sink = create_import_app()

    response = client.post(
        "/channels/import",
        files={"file": ("roster.csv", import_csv(f"{CHANNEL_ID},Alpha News,Yes"), "text/csv")},
        data={
            "content_owner_id": CONTENT_OWNER,
            "cms_status": "INSIDE_CMS",
            "dry_run": "false",
            "reason": "Quarterly CMS roster load",
        },
    )

    assert response.status_code == 401
    assert registry.get_channel(CHANNEL_ID) is None
    assert audit_sink.records == []


def test_import_rejects_unknown_role_header():
    """An unknown role fails closed as 400 before any parsing or writes."""
    client, registry, _groups, _sink = create_import_app()

    response = post_import(
        client,
        import_csv(f"{CHANNEL_ID},Alpha News,Yes"),
        headers=auth_headers("not_a_role", "global"),
    )

    assert response.status_code == 400
    assert registry.get_channel(CHANNEL_ID) is None


def test_import_rejects_global_role_without_manage_channels():
    """A global finance viewer holds no MANAGE_CHANNELS and gets 403."""
    client, registry, _groups, audit_sink = create_import_app()

    response = post_import(
        client,
        import_csv(f"{CHANNEL_ID},Alpha News,Yes"),
        headers=auth_headers("finance_viewer", "global"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: registry.manage_channels"
    assert registry.get_channel(CHANNEL_ID) is None
    assert audit_sink.records == []
