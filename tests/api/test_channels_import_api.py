"""API tests for the bulk channel inventory import (POST /channels/import)."""

import dataclasses

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
from ums_smart_revenue.org.channel_groups import (
    ChannelGroupConflictError,
    ChannelGroupEntry,
    ChannelGroupRegistry,
)
from ums_smart_revenue.org.channel_registry import (
    ChannelRegistry,
    ChannelRegistryConflictError,
    ChannelRegistryEntry,
    bootstrap_channel_registry,
)

CHANNEL_ID = "UCB6sc84dcg6VQGB_d89sx2g"
# A second valid channel id, for rows that must differ from CHANNEL_ID.
SECOND_CHANNEL_ID = "UC3Dci3BzZXDo4jw4dU8KqWg"
CONTENT_OWNER = "TestOwnerAAAAAAAAAAAAA"
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
    groups: ChannelGroupRegistry | None = None,
) -> tuple[TestClient, object, ChannelGroupRegistry, InMemoryAuditSink]:
    """Build an import-ready client plus the registries and sink it writes to."""
    app = create_app()
    registry = registry if registry is not None else bootstrap_channel_registry()
    groups = groups if groups is not None else ChannelGroupRegistry()
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
    # No group carries this key yet, so the row mints a NEW SECTOR group.
    assert row["group_action"] == "CREATE"


def test_dry_run_discloses_group_create_versus_join():
    """The preview says WHICH group write a Group_ID implies (review #184).

    ``group_id`` alone cannot: minting a new SECTOR group creates a fresh
    finance-scope object stamped to this content owner, while an existing key
    only attaches a member. Both are audited GROUP_UPDATED, and the operator
    approves an all-or-nothing roster, so the two must be told apart BEFORE
    the write rather than reconstructed from the audit trail after it.
    """
    client, _registry, groups, _sink = create_import_app()
    groups.create_group(
        name="Existing TV",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-tv",
        content_owner_id=CONTENT_OWNER,
    )
    header = "youtube_channel_id,channel_name,group_id,view_revenue"

    response = post_import(
        client,
        import_csv(
            f"{CHANNEL_ID},Alpha News,cms-tv,Yes",
            f"{CHANNEL_ID},Alpha News,cms-brand-new,Yes",
            header=header,
        ),
        dry_run="true",
    )

    assert response.status_code == 200, response.text
    rows = response.json()["rows"]
    assert [row["group_id"] for row in rows] == ["cms-tv", "cms-brand-new"]
    assert [row["group_action"] for row in rows] == ["JOIN", "CREATE"]


def test_apply_rejects_a_plan_that_changed_since_the_preview():
    """The apply is bound to the plan the operator actually reviewed (#184).

    The route re-plans from CURRENT state, so without this the roster row an
    operator approved as a CREATE could commit as an UPDATE that overwrites a
    channel someone else created in the meantime — a different write, never
    reviewed. The 409 carries the REFRESHED plan so approval is re-sought
    against reality.
    """
    client, registry, _groups, audit_sink = create_import_app()
    body = import_csv(f"{CHANNEL_ID},Alpha News,Yes")

    preview = post_import(client, body, dry_run="true")
    assert preview.status_code == 200, preview.text
    assert preview.json()["rows"][0]["outcome"] == "CREATE"
    stale_fingerprint = preview.json()["plan_fingerprint"]

    # A concurrent writer creates the very channel the roster planned to add.
    registry.create_channel(
        youtube_channel_id=CHANNEL_ID,
        channel_name="Someone Else's Name",
        primary_company_id=None,
        cms_status="INSIDE_CMS",
        revenue_required=True,
        content_owner_id=CONTENT_OWNER,
    )

    conflict = post_import(client, body, expected_plan_fingerprint=stale_fingerprint)

    assert conflict.status_code == 409, conflict.text
    refreshed = conflict.json()["detail"]
    # The plan really did change under the operator: CREATE became UPDATE.
    assert refreshed["rows"][0]["outcome"] == "UPDATE"
    assert refreshed["plan_fingerprint"] != stale_fingerprint
    # Nothing was written, and no audit event claims otherwise.
    assert registry.get_channel(CHANNEL_ID).channel_name == "Someone Else's Name"
    assert audit_sink.records == []


def test_apply_proceeds_when_the_plan_still_matches_the_preview():
    """A matching fingerprint is not a gate, only a guard: the apply runs."""
    client, registry, _groups, _sink = create_import_app()
    body = import_csv(f"{CHANNEL_ID},Alpha News,Yes")

    preview = post_import(client, body, dry_run="true")
    fingerprint = preview.json()["plan_fingerprint"]

    applied = post_import(client, body, expected_plan_fingerprint=fingerprint)

    assert applied.status_code == 200, applied.text
    assert applied.json()["counts"]["CREATE"] == 1
    assert registry.get_channel(CHANNEL_ID) is not None


def test_apply_without_a_fingerprint_still_applies():
    """The field is OPTIONAL: a client that never previewed re-approves nothing.

    Pinned so the guard cannot quietly become a required field and break every
    non-SPA caller of this route.
    """
    client, registry, _groups, _sink = create_import_app()

    response = post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,Yes"))

    assert response.status_code == 200, response.text
    assert registry.get_channel(CHANNEL_ID) is not None


def test_plan_fingerprint_ignores_dry_run():
    """`dry_run` is excluded, and that exclusion is load-bearing.

    A preview and its apply differ in it by definition, so folding it in would
    make every fingerprint mismatch and the guard would reject every apply — a
    guard that always fires protects nothing.
    """
    client, _registry, _groups, _sink = create_import_app()
    body = import_csv(f"{CHANNEL_ID},Alpha News,Yes")

    preview = post_import(client, body, dry_run="true")
    applied = post_import(client, body, dry_run="false")

    assert preview.json()["dry_run"] is True
    assert applied.json()["dry_run"] is False
    assert preview.json()["plan_fingerprint"] == applied.json()["plan_fingerprint"]


def test_plan_fingerprint_covers_the_content_owner_and_cms_status():
    """The digest binds the TARGET too, not just the row plan (review #184).

    An all-CREATE roster's rows carry no owner — a CREATE's `changes` is empty
    by design — so without these fields two different content owners produce
    byte-identical row plans. An operator who previewed owner A and applied
    against B would then sail through the guard and commit channels and groups
    under the wrong owner.
    """
    client, _registry, _groups, _sink = create_import_app()
    body = import_csv(f"{CHANNEL_ID},Alpha News,Yes")

    for_owner_a = post_import(client, body, dry_run="true").json()
    for_owner_b = post_import(
        client, body, dry_run="true", content_owner_id="OtherOwnerBBBBBBBBBBB"
    ).json()
    for_outside_cms = post_import(client, body, dry_run="true", cms_status="OUTSIDE_CMS").json()

    # The row plans really are identical — the fingerprint is the only thing
    # standing between them.
    assert for_owner_a["counts"] == for_owner_b["counts"]
    assert [row["outcome"] for row in for_owner_a["rows"]] == [
        row["outcome"] for row in for_owner_b["rows"]
    ]
    assert for_owner_a["plan_fingerprint"] != for_owner_b["plan_fingerprint"]
    assert for_owner_a["plan_fingerprint"] != for_outside_cms["plan_fingerprint"]


def test_apply_under_a_swapped_content_owner_is_rejected():
    """End to end: preview owner A, apply owner B -> 409, nothing written."""
    client, registry, _groups, audit_sink = create_import_app()
    body = import_csv(f"{CHANNEL_ID},Alpha News,Yes")

    reviewed = post_import(client, body, dry_run="true").json()["plan_fingerprint"]
    swapped = post_import(
        client,
        body,
        content_owner_id="OtherOwnerBBBBBBBBBBB",
        expected_plan_fingerprint=reviewed,
    )

    assert swapped.status_code == 409, swapped.text
    assert registry.get_channel(CHANNEL_ID) is None
    assert audit_sink.records == []


def test_group_created_before_the_apply_replans_is_caught_by_the_fingerprint():
    """The FIRST of the two windows: preview -> the apply's own re-plan.

    The apply re-plans from current state, so a group created here is already
    visible to it and the row re-plans as JOIN. Nothing in the write path
    would notice, but the fingerprint does: JOIN digests differently from the
    reviewed CREATE, so the operator re-approves instead of silently joining a
    group they never saw.
    """
    client, registry, groups, audit_sink = create_import_app()
    header = "youtube_channel_id,channel_name,group_id,view_revenue"
    body = import_csv(f"{CHANNEL_ID},Alpha News,cms-tv,Yes", header=header)

    preview = post_import(client, body, dry_run="true").json()
    assert preview["rows"][0]["group_action"] == "CREATE"

    groups.create_group(
        name="cms-tv",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-tv",
        content_owner_id=CONTENT_OWNER,
    )

    response = post_import(client, body, expected_plan_fingerprint=preview["plan_fingerprint"])

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["rows"][0]["group_action"] == "JOIN"
    assert registry.get_channel(CHANNEL_ID) is None
    assert audit_sink.records == []


class _GroupAppearsAtWriteBoundary(ChannelGroupRegistry):
    """Create the group between the apply's re-plan and its locked read.

    Planning reads group state through the bulk ``list_*`` lookups; only the
    write boundary takes ``for_update``. Creating the group inside that locked
    read reproduces the SECOND window exactly — the one the fingerprint cannot
    see, because the plan it digested was already computed.
    """

    def __init__(self, *, cms_group_id: str, content_owner_id: str) -> None:
        super().__init__()
        self._race_key = cms_group_id
        self._race_owner = content_owner_id
        self._raced = False

    def get_group_by_cms_id(
        self, cms_group_id: str, *, for_update: bool = False
    ) -> ChannelGroupEntry | None:
        """Race the group into existence once, just before the locked read."""
        if for_update and not self._raced and cms_group_id == self._race_key:
            self._raced = True
            super().create_group(
                name=self._race_key,
                group_type="SECTOR",
                channel_ids=[],
                cms_group_id=self._race_key,
                content_owner_id=self._race_owner,
            )
        return super().get_group_by_cms_id(cms_group_id, for_update=for_update)


def test_group_created_after_the_apply_replans_is_rejected_at_the_write_boundary():
    """The SECOND window: the apply's re-plan -> the group row lock.

    The fingerprint compare happens before ``apply_channel_import`` takes any
    group lock, so this owner's own CMS sync can still create the group in
    between and turn a reviewed "creates a new SECTOR group" into a silent
    join — a different finance-scope and audit effect, with no second 409 to
    catch it. The reviewed action is therefore re-checked under the SAME lock
    that performs the write.
    """
    client, _registry, _groups, audit_sink = create_import_app()
    racing = _GroupAppearsAtWriteBoundary(cms_group_id="cms-tv", content_owner_id=CONTENT_OWNER)
    client.app.dependency_overrides[sql_group_registry_from_session] = lambda: racing
    header = "youtube_channel_id,channel_name,group_id,view_revenue"
    body = import_csv(f"{CHANNEL_ID},Alpha News,cms-tv,Yes", header=header)

    preview = post_import(client, body, dry_run="true").json()
    assert preview["rows"][0]["group_action"] == "CREATE"

    response = post_import(client, body, expected_plan_fingerprint=preview["plan_fingerprint"])

    assert response.status_code == 409, response.text
    assert "was created during the import" in response.json()["detail"]
    # No GROUP write and no group audit event: the divergence is caught under
    # the row lock, before either branch of _attach_group_membership runs.
    assert racing.get_group_by_cms_id("cms-tv").channel_ids == ()
    assert not [r for r in audit_sink.records if r.entity_type == "channel_group"]
    # Rollback of the CHANNEL rows this row already wrote is the transaction's
    # job, and the in-memory registry has no transaction — the raised error is
    # what triggers it. tests/api/test_channels_import_postgres.py owns that
    # proof against a real database.


class _ChannelDriftsAtWriteBoundary(ChannelRegistry):
    """Move a channel's stored name between the apply's re-plan and its lock.

    Planning reads through ``list_channels_by_ids``; the write boundary calls
    ``update_inventory``. Drifting the row inside that second call reproduces
    the window the fingerprint cannot see, because the plan it digested was
    already computed.
    """

    def __init__(self, entries: list[ChannelRegistryEntry], *, drift_to: str) -> None:
        super().__init__(entries)
        self._drift_to = drift_to
        self._drifted = False

    def update_inventory(
        self,
        *,
        youtube_channel_id: str,
        channel_name: str,
        cms_status: str,
        content_owner_id: str | None,
        revenue_required: bool,
    ) -> tuple[ChannelRegistryEntry, ChannelRegistryEntry]:
        """Land a concurrent rename once, just before the locked write."""
        if not self._drifted:
            self._drifted = True
            current = super().get_channel(youtube_channel_id)
            assert current is not None
            super().update_inventory(
                youtube_channel_id=current.youtube_channel_id,
                channel_name=self._drift_to,
                cms_status=current.cms_status,
                content_owner_id=current.content_owner_id,
                revenue_required=current.revenue_required,
            )
        return super().update_inventory(
            youtube_channel_id=youtube_channel_id,
            channel_name=channel_name,
            cms_status=cms_status,
            content_owner_id=content_owner_id,
            revenue_required=revenue_required,
        )


def _seeded_registry(channel_name: str) -> ChannelRegistry:
    """One existing channel, so a roster rename plans as an UPDATE."""
    return ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id=CHANNEL_ID,
                channel_name=channel_name,
                primary_company_id=None,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                content_owner_id=CONTENT_OWNER,
            )
        ]
    )


def test_plan_bound_apply_refuses_a_row_whose_pre_state_drifted():
    """A bound apply means "the diff I reviewed, or none of it" (review #184).

    The fingerprint compare happens before each channel row lock, so a writer
    committing in that gap turns a reviewed "Old Name -> Alpha News" into an
    unreviewed "Someone Else -> Alpha News". The values written would be the
    approved ones, but the operator approved neither the other value nor the
    decision to overwrite it.
    """
    drifting = _ChannelDriftsAtWriteBoundary(
        list(_seeded_registry("Old Name").list_channels_by_ids({CHANNEL_ID})),
        drift_to="Someone Else",
    )
    client, registry, _groups, audit_sink = create_import_app(drifting)
    body = import_csv(f"{CHANNEL_ID},Alpha News,Yes")

    preview = post_import(client, body, dry_run="true").json()
    assert preview["rows"][0]["changes"]["channel_name"] == {
        "from": "Old Name",
        "to": "Alpha News",
    }

    response = post_import(client, body, expected_plan_fingerprint=preview["plan_fingerprint"])

    assert response.status_code == 409, response.text
    assert "changed during the import" in response.json()["detail"]
    assert "Someone Else" in response.json()["detail"]
    # No audit event claims a mutation the operator never approved. The row
    # write itself is undone by the surrounding transaction, which this tier
    # does not have (the in-memory registry commits as it goes) — the durable
    # rollback proof is
    # tests/api/test_channels_import_postgres.py::
    # test_drifted_pre_state_rolls_the_bound_apply_back_on_postgres.
    assert audit_sink.records == []
    assert registry is drifting


def test_unbound_apply_still_lets_the_file_win_over_drift():
    """The #159 default is untouched for a caller that bound no plan.

    This is the whole reason the guard is opt-in: an API client that never
    previewed is re-approving nothing, so the roster stays authoritative and
    the audit records the REAL diff.
    """
    drifting = _ChannelDriftsAtWriteBoundary(
        list(_seeded_registry("Old Name").list_channels_by_ids({CHANNEL_ID})),
        drift_to="Someone Else",
    )
    client, _registry, _groups, audit_sink = create_import_app(drifting)

    response = post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,Yes"))

    assert response.status_code == 200, response.text
    assert drifting.get_channel(CHANNEL_ID).channel_name == "Alpha News"
    updated = [r for r in audit_sink.records if r.event_type == "CHANNEL_UPDATED"]
    assert len(updated) == 1
    assert updated[0].details["changes"] == {
        "channel_name": {"from": "Someone Else", "to": "Alpha News"}
    }


class _RevenueFlagDriftsAtWriteBoundary(ChannelRegistry):
    """Flip a field the preview showed as UNCHANGED, at the write boundary.

    The gap this pins: ``entry.changes`` omits a field when planning found it
    already matching the roster, but ``update_inventory`` writes all four
    fields regardless. Drifting an omitted field is the case a changes-only
    guard cannot see.
    """

    def __init__(self, entries: list[ChannelRegistryEntry]) -> None:
        super().__init__(entries)
        self._drifted = False

    def update_inventory(
        self,
        *,
        youtube_channel_id: str,
        channel_name: str,
        cms_status: str,
        content_owner_id: str | None,
        revenue_required: bool,
    ) -> tuple[ChannelRegistryEntry, ChannelRegistryEntry]:
        """Turn revenue_required off once, just before the locked write."""
        if not self._drifted:
            self._drifted = True
            current = super().get_channel(youtube_channel_id)
            assert current is not None
            super().update_inventory(
                youtube_channel_id=current.youtube_channel_id,
                channel_name=current.channel_name,
                cms_status=current.cms_status,
                content_owner_id=current.content_owner_id,
                revenue_required=False,
            )
        return super().update_inventory(
            youtube_channel_id=youtube_channel_id,
            channel_name=channel_name,
            cms_status=cms_status,
            content_owner_id=content_owner_id,
            revenue_required=revenue_required,
        )


def test_plan_bound_apply_refuses_drift_in_a_field_the_preview_showed_unchanged():
    """ "No change to revenue_required" is a reviewed claim too (review #184).

    The preview here promises exactly one effect — the rename — and implicitly
    promises the other three fields stay put. A writer who turns
    revenue_required OFF in the plan-to-apply window would have that decision
    silently reverted by the roster's Yes, under an apply the operator bound
    to a diff that never mentioned the flag. Reverting a revenue requirement
    nobody reviewed is a finance-visible effect, not a cosmetic one.
    """
    drifting = _RevenueFlagDriftsAtWriteBoundary(
        list(_seeded_registry("Old Name").list_channels_by_ids({CHANNEL_ID}))
    )
    client, _registry, _groups, audit_sink = create_import_app(drifting)
    body = import_csv(f"{CHANNEL_ID},Alpha News,Yes")

    preview = post_import(client, body, dry_run="true").json()
    # The reviewed diff mentions ONLY the name: the flag matched at plan time,
    # so a changes-only guard has nothing to compare it against.
    assert set(preview["rows"][0]["changes"]) == {"channel_name"}

    response = post_import(client, body, expected_plan_fingerprint=preview["plan_fingerprint"])

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "no change to revenue_required" in detail
    assert audit_sink.records == []


def test_bound_apply_does_not_409_a_channel_repeated_across_groups():
    """The guard must not read the import's OWN first write as concurrent drift.

    A roster may list one channel once per group, and the extra copies plan as
    UNCHANGED with an empty diff. The apply writes the channel once per copy,
    so by the second copy the locked pre-state is what copy 1 just wrote. A
    guard that compared an empty diff against the ORIGINAL stored state would
    fire on the plan doing exactly what the operator approved — the same shape
    of self-inflicted 409 the group ledger had to fix (review #184).
    """
    client, registry, groups, _sink = create_import_app()
    header = "youtube_channel_id,channel_name,group_id,view_revenue"
    # Copy 1 renames (a real UPDATE); copy 2 is membership-only (UNCHANGED).
    body = import_csv(
        f"{CHANNEL_ID},Alpha News,cms-tv,Yes",
        f"{CHANNEL_ID},Alpha News,cms-radio,Yes",
        header=header,
    )
    seed = post_import(client, import_csv(f"{CHANNEL_ID},Old Name,Yes"))
    assert seed.status_code == 200

    preview = post_import(client, body, dry_run="true").json()
    # Pin the SCENARIO, not just the outcome: this only exercises the guard if
    # copy 2 really is the empty-diff UNCHANGED row whose expected pre-state
    # has to resolve to what copy 1 wrote. Without these, a planning change
    # (say, duplicates becoming ERROR rows) could quietly stop covering it
    # while the final-state assertions below still passed.
    first, second = preview["rows"]
    assert (first["outcome"], first["group_id"]) == ("UPDATE", "cms-tv")
    assert first["changes"] == {"channel_name": {"from": "Old Name", "to": "Alpha News"}}
    assert (second["outcome"], second["group_id"]) == ("UNCHANGED", "cms-radio")
    assert second["changes"] == {}

    response = post_import(client, body, expected_plan_fingerprint=preview["plan_fingerprint"])

    assert response.status_code == 200, response.text
    stored = registry.get_channel(CHANNEL_ID)
    assert stored is not None and stored.channel_name == "Alpha News"
    # Both memberships landed, so the second copy really was executed.
    for key in ("cms-tv", "cms-radio"):
        group = groups.get_group_by_cms_id(key)
        assert group is not None and group.channel_ids == (CHANNEL_ID,)


def test_plan_bound_apply_proceeds_when_nothing_drifted():
    """Anti-vacuity: the opt-in guard must not reject the ordinary bound apply."""
    client, registry, _groups, _sink = create_import_app(_seeded_registry("Old Name"))
    body = import_csv(f"{CHANNEL_ID},Alpha News,Yes")

    preview = post_import(client, body, dry_run="true").json()
    response = post_import(client, body, expected_plan_fingerprint=preview["plan_fingerprint"])

    assert response.status_code == 200, response.text
    assert registry.get_channel(CHANNEL_ID).channel_name == "Alpha News"


def test_several_rows_can_populate_one_newly_created_group():
    """The common shape: N channels imported into a group that does not exist.

    Planning labels EVERY such row CREATE, because the group was absent for
    all of them. The first row then creates it and the rest observe it inside
    the same transaction — which the write-boundary action check must read as
    the plan's own handiwork, not as a concurrent creation. Getting this wrong
    409s a perfectly valid import.
    """
    client, registry, groups, _sink = create_import_app()
    second_id = "UC3Dci3BzZXDo4jw4dU8KqWg"
    header = "youtube_channel_id,channel_name,group_id,view_revenue"
    body = import_csv(
        f"{CHANNEL_ID},Alpha News,cms-new,Yes",
        f"{second_id},Beta News,cms-new,Yes",
        header=header,
    )

    preview = post_import(client, body, dry_run="true").json()
    # Both rows really are planned as CREATE — that is what makes this a trap.
    assert [row["group_action"] for row in preview["rows"]] == ["CREATE", "CREATE"]

    response = post_import(client, body, expected_plan_fingerprint=preview["plan_fingerprint"])

    assert response.status_code == 200, response.text
    assert registry.get_channel(CHANNEL_ID) is not None
    assert registry.get_channel(second_id) is not None
    # One group, both channels in it.
    created = groups.get_group_by_cms_id("cms-new")
    assert created is not None
    assert sorted(created.channel_ids) == sorted([CHANNEL_ID, second_id])


def test_group_join_still_applies_when_the_group_is_still_there():
    """Anti-vacuity: the divergence guard must not reject the normal path."""
    client, registry, groups, _sink = create_import_app()
    groups.create_group(
        name="Existing TV",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-tv",
        content_owner_id=CONTENT_OWNER,
    )
    header = "youtube_channel_id,channel_name,group_id,view_revenue"
    body = import_csv(f"{CHANNEL_ID},Alpha News,cms-tv,Yes", header=header)

    assert post_import(client, body, dry_run="true").json()["rows"][0]["group_action"] == "JOIN"
    response = post_import(client, body)

    assert response.status_code == 200, response.text
    assert registry.get_channel(CHANNEL_ID) is not None


def test_plan_fingerprint_changes_when_a_row_outcome_changes():
    """Anti-vacuity: the digest must actually track plan content."""
    client, registry, _groups, _sink = create_import_app()
    body = import_csv(f"{CHANNEL_ID},Alpha News,Yes")

    before = post_import(client, body, dry_run="true").json()["plan_fingerprint"]
    registry.create_channel(
        youtube_channel_id=CHANNEL_ID,
        channel_name="Alpha News",
        primary_company_id=None,
        cms_status="INSIDE_CMS",
        revenue_required=True,
        content_owner_id=CONTENT_OWNER,
    )
    after = post_import(client, body, dry_run="true").json()

    assert after["rows"][0]["outcome"] == "UNCHANGED"
    assert after["plan_fingerprint"] != before


def test_rows_without_a_group_key_disclose_no_group_action():
    """No Group_ID, no group write — and therefore no claim about one."""
    client, _registry, _groups, _sink = create_import_app()

    response = post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,Yes"), dry_run="true")

    assert response.status_code == 200, response.text
    row = response.json()["rows"][0]
    assert row["group_id"] is None
    assert row["group_action"] is None


def test_multi_group_roster_attaches_every_membership():
    """One row per group: a repeated channel id attaches ALL its groups."""
    client, registry, groups, _sink = create_import_app()

    response = post_import(
        client,
        import_csv(
            f"{CHANNEL_ID},Alpha News,cms-tv,Yes",
            f"{CHANNEL_ID},Alpha News,cms-news,Yes",
            header="youtube_channel_id,channel_name,group_id,view_revenue",
        ),
    )

    assert response.status_code == 200
    assert response.json()["counts"]["CREATE"] == 1
    assert registry.get_channel(CHANNEL_ID) is not None
    for cms_key in ("cms-tv", "cms-news"):
        group = groups.get_group_by_cms_id(cms_key)
        assert group is not None and CHANNEL_ID in group.channel_ids


def test_planned_update_that_became_a_noop_is_not_audited():
    """A concurrent writer landing the same values first leaves no false audit.

    Planning saw the old name and classified UPDATE; by apply time the store
    already holds the roster values, so the write replaces nothing and must
    not record a CHANNEL_UPDATED claiming a mutation that did not occur.
    """
    registry = ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id=CHANNEL_ID,
                # Already the roster values: the concurrent writer won the race.
                channel_name="Alpha News",
                primary_company_id=None,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                content_owner_id=CONTENT_OWNER,
            )
        ]
    )

    class _StaleSnapshotRegistry(ChannelRegistry):
        """Planning sees the OLD name; the write boundary sees current state."""

        def list_channels_by_ids(self, wanted, *, include_inactive=False):
            return [
                dataclasses.replace(entry, channel_name="Old Name")
                for entry in super().list_channels_by_ids(wanted, include_inactive=include_inactive)
            ]

    stale = _StaleSnapshotRegistry(list(registry.list_channels_by_ids({CHANNEL_ID})))
    client, _registry, _groups, audit_sink = create_import_app(stale)

    response = post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,Yes"))

    assert response.status_code == 200
    assert response.json()["counts"]["UPDATE"] == 1  # the PLAN said update
    updated_events = [r for r in audit_sink.records if r.event_type == "CHANNEL_UPDATED"]
    assert updated_events == []


def test_oversized_content_owner_id_is_rejected_422():
    """The owner id lands in the audit entity B-tree index; bound it."""
    client, _registry, _groups, _sink = create_import_app()

    response = post_import(
        client,
        import_csv(f"{CHANNEL_ID},Alpha News,Yes"),
        content_owner_id="x" * 256,
    )

    assert response.status_code == 422
    assert "content_owner_id exceeds 255 characters" in response.json()["detail"]


def test_nul_in_scalar_form_fields_is_rejected_422():
    """NUL-bearing content_owner_id / reason fail the 422 contract, never 500.

    Both values reach PostgreSQL text columns (youtube_channels.
    content_owner_id, audit_logs.reason) that cannot store U+0000; the
    boundary must reject them like the CSV parser rejects per-row NULs
    (review #159 r3713449085).
    """
    client, _registry, _groups, _audit_sink = create_import_app()
    csv_text = import_csv(f"{CHANNEL_ID},Alpha News,Yes")

    owner = post_import(client, csv_text, content_owner_id="own\x00er")
    assert owner.status_code == 422
    assert "content_owner_id contains a NUL character" in owner.json()["detail"]

    reason = post_import(client, csv_text, reason="Roster\x00load")
    assert reason.status_code == 422
    assert "reason contains a NUL character" in reason.json()["detail"]


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


def test_preview_discloses_the_derived_revenue_source_status():
    """The write re-classifies the revenue SOURCE; the plan must say so.

    ``changes`` carries the operator's own field edits, and never this — the
    registry derives it from the revenue_required flip. It feeds
    ``missing_official_revenue`` and the registry's recommended action, so a
    preview that omits it asks the operator to approve a finance-source
    mutation nothing on screen mentions (review #184).
    """
    registry = ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id=CHANNEL_ID,
                channel_name="Alpha News",
                primary_company_id=None,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                content_owner_id=CONTENT_OWNER,
                revenue_source_status="OFFICIAL_CMS_REVENUE",
            )
        ]
    )
    client, _registry, _groups, _sink = create_import_app(registry)

    # Row 1 flips revenue OFF, replacing a proven official classification.
    # Row 2 is a CREATE, which has no prior status at all.
    payload = post_import(
        client,
        import_csv(f"{CHANNEL_ID},Alpha News,No", f"{SECOND_CHANNEL_ID},Beta News,Yes"),
        dry_run="true",
    ).json()

    by_id = {row["youtube_channel_id"]: row for row in payload["rows"]}
    assert by_id[CHANNEL_ID]["revenue_source_status"] == {
        "from": "OFFICIAL_CMS_REVENUE",
        "to": "PERFORMANCE_ONLY",
    }
    # The diff itself still says nothing about it — that is the whole gap.
    assert "revenue_source_status" not in by_id[CHANNEL_ID]["changes"]
    assert by_id[SECOND_CHANNEL_ID]["revenue_source_status"] == {
        "from": None,
        "to": "MISSING_REVENUE_SOURCE",
    }


class _SourceStatusDriftsAtWriteBoundary(ChannelRegistry):
    """Move ONLY the source classification, leaving the four fields alone.

    Models two concurrent imports flipping revenue_required off and back on
    between the route's re-plan and the row lock: the inventory fields return
    to their reviewed values while the classification underneath does not.
    """

    def __init__(self, entries: list[ChannelRegistryEntry], *, drift_to: str) -> None:
        super().__init__(entries)
        self._drift_to = drift_to
        self._drifted = False

    def update_inventory(
        self,
        *,
        youtube_channel_id: str,
        channel_name: str,
        cms_status: str,
        content_owner_id: str | None,
        revenue_required: bool,
    ) -> tuple[ChannelRegistryEntry, ChannelRegistryEntry]:
        """Rewrite the stored classification once, just before the locked write."""
        if not self._drifted:
            self._drifted = True
            current = super().get_channel(youtube_channel_id)
            assert current is not None
            self._channels[youtube_channel_id] = dataclasses.replace(
                current, revenue_source_status=self._drift_to
            )
        return super().update_inventory(
            youtube_channel_id=youtube_channel_id,
            channel_name=channel_name,
            cms_status=cms_status,
            content_owner_id=content_owner_id,
            revenue_required=revenue_required,
        )


def test_plan_bound_apply_refuses_a_drifted_revenue_source_pre_state():
    """The DISCLOSED source transition is reviewed, so it is enforced too.

    All four inventory fields can return to their reviewed values while the
    classification beneath them moves. The apply would then perform
    MISSING_REVENUE_SOURCE -> PERFORMANCE_ONLY where the operator approved
    OFFICIAL_CMS_REVENUE -> PERFORMANCE_ONLY: the same end state, reached from
    a different one, which is a different finance-source mutation.
    """
    drifting = _SourceStatusDriftsAtWriteBoundary(
        [
            ChannelRegistryEntry(
                youtube_channel_id=CHANNEL_ID,
                channel_name="Alpha News",
                primary_company_id=None,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                content_owner_id=CONTENT_OWNER,
                revenue_source_status="OFFICIAL_CMS_REVENUE",
            )
        ],
        drift_to="MISSING_REVENUE_SOURCE",
    )
    client, _registry, _groups, audit_sink = create_import_app(drifting)
    body = import_csv(f"{CHANNEL_ID},Alpha News,No")

    preview = post_import(client, body, dry_run="true").json()
    assert preview["rows"][0]["revenue_source_status"] == {
        "from": "OFFICIAL_CMS_REVENUE",
        "to": "PERFORMANCE_ONLY",
    }

    response = post_import(client, body, expected_plan_fingerprint=preview["plan_fingerprint"])

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "revenue_source_status" in detail
    assert "OFFICIAL_CMS_REVENUE" in detail
    assert audit_sink.records == []


def test_preview_claims_no_source_change_when_the_flag_holds():
    """Anti-noise: only a revenue_required FLIP re-derives the source status.

    A roster refreshing names must not read as a finance reclassification —
    and must not downgrade an OFFICIAL_* classification either, which is
    exactly what derive_revenue_source_status protects.
    """
    registry = ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id=CHANNEL_ID,
                channel_name="Old Name",
                primary_company_id=None,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                content_owner_id=CONTENT_OWNER,
                revenue_source_status="OFFICIAL_CMS_REVENUE",
            )
        ]
    )
    client, registry_ref, _groups, _sink = create_import_app(registry)

    payload = post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,Yes"), dry_run="true").json()

    assert payload["rows"][0]["outcome"] == "UPDATE"
    assert payload["rows"][0]["revenue_source_status"] is None
    # And the apply really does leave the classification alone.
    assert post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,Yes")).status_code == 200
    stored = registry_ref.get_channel(CHANNEL_ID)
    assert stored is not None and stored.revenue_source_status == "OFFICIAL_CMS_REVENUE"


class _CountingGroupStore(ChannelGroupRegistry):
    """Count the two store calls whose COST scales with a group's membership.

    Both `get_group_by_cms_id(for_update=True)` and `add_members` materialize
    the group's full member set in the SQL store, so calling either once per
    roster row is what made a shared-group import quadratic in payload.
    """

    def __init__(self) -> None:
        super().__init__()
        self.locked_lookups = 0
        self.member_adds = 0
        self.added_batch_sizes: list[int] = []

    def get_group_by_cms_id(
        self, cms_group_id: str, *, for_update: bool = False
    ) -> ChannelGroupEntry | None:
        """Count only the LOCKED reads — the write-boundary ones."""
        if for_update:
            self.locked_lookups += 1
        return super().get_group_by_cms_id(cms_group_id, for_update=for_update)

    def add_members(self, *, group_id: str, channel_ids: list[str]) -> ChannelGroupEntry:
        """Count each membership write and the size of the batch it carried."""
        self.member_adds += 1
        self.added_batch_sizes.append(len(channel_ids))
        return super().add_members(group_id=group_id, channel_ids=channel_ids)


def _roster_sharing_one_group(count: int, *, group_id: str = "cms-tv") -> str:
    """A CSV whose `count` distinct channels all name the same group key."""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    rows = []
    for index in range(count):
        tail = "".join(alphabet[(index // len(alphabet) ** p) % len(alphabet)] for p in range(22))
        rows.append(f"UC{tail},Channel {index},{group_id},Yes")
    return import_csv(*rows, header="youtube_channel_id,channel_name,group_id,view_revenue")


def test_shared_group_rows_resolve_the_group_once_regardless_of_roster_size():
    """One lock and one membership write per GROUP, not per row (review #184).

    Resolving per row made the payload quadratic: every locked lookup loaded
    the group's whole membership and every single-channel add read it and
    returned it again, so a 5000-row roster under one group moved tens of
    millions of membership rows inside one transaction while holding every
    channel lock. The fix is per-key batching, and the property that proves it
    is that these counts do NOT grow with the roster.
    """
    counts = {}
    for size in (3, 12):
        store = _CountingGroupStore()
        client, _registry, _groups, _sink = create_import_app(groups=store)

        response = post_import(client, _roster_sharing_one_group(size))

        assert response.status_code == 200, response.text
        assert response.json()["counts"]["CREATE"] == size
        group = store.get_group_by_cms_id("cms-tv")
        assert group is not None and len(group.channel_ids) == size
        counts[size] = (store.locked_lookups, store.member_adds, store.added_batch_sizes)

    # Identical for both roster sizes: one locked read, one bulk add carrying
    # every channel but the one the create already attached.
    assert counts[3] == (1, 1, [2])
    assert counts[12] == (1, 1, [11])


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


def test_group_owned_by_another_content_owner_fails_the_dry_run():
    """A cross-owner group conflict is visible in the PREVIEW, not just the apply.

    The write boundary already refuses it with a 409, but this route's whole
    dry-run contract is that the preview tells the operator what will happen.
    Since the conflict is knowable from stored state, an "all clear" preview
    followed by a 409 would be the preview lying.
    """
    client, registry, groups, audit_sink = create_import_app()
    groups.create_group(
        name="Theirs",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-theirs",
        content_owner_id="SomeOtherOwner",
    )
    header = "youtube_channel_id,channel_name,group_id,view_revenue"
    body = import_csv(f"{CHANNEL_ID},Alpha News,cms-theirs,Yes", header=header)

    # A dry run reports its plan (errors included) with 200 — that IS the
    # preview contract; what matters is that the conflict appears at all.
    preview = post_import(client, body, dry_run="true")

    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["counts"]["ERROR"] == 1
    assert "another content owner" in plan["rows"][0]["reason"]
    assert "cms-theirs" in plan["rows"][0]["reason"]

    # And the apply agrees with the preview: a 422 naming the same conflict,
    # not a 409 from the write boundary that the preview never hinted at.
    applied = post_import(client, body)
    assert applied.status_code == 422, applied.text
    assert "another content owner" in applied.json()["detail"]["rows"][0]["reason"]
    assert registry.get_channel(CHANNEL_ID) is None
    assert groups.get_group_by_cms_id("cms-theirs").channel_ids == ()
    assert audit_sink.records == []


class _ArchivingDuringApplyGroups(ChannelGroupRegistry):
    """Simulate a concurrent archive landing between planning and apply.

    Planning's bulk archived-key lookup sees the group ACTIVE; the apply's
    locked write-boundary lookup (for_update=True) observes it archived.
    """

    def get_group_by_cms_id(self, cms_group_id, *, for_update=False):
        group = super().get_group_by_cms_id(cms_group_id, for_update=for_update)
        if group is not None and for_update:
            return dataclasses.replace(group, active=False)
        return group


def test_group_archived_between_plan_and_apply_returns_409():
    """The write-boundary recheck turns the race into 409, not a silent write."""
    app = create_app()
    registry = bootstrap_channel_registry()
    groups = _ArchivingDuringApplyGroups()
    # Stamped with THIS import's owner: planning must clear the row (an
    # owner-NULL group is its own row error) so the archive race is what the
    # apply actually trips over.
    groups.create_group(
        name="cms-tv",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-tv",
        content_owner_id=CONTENT_OWNER,
    )
    audit_sink = InMemoryAuditSink()
    app.dependency_overrides[current_channel_registry] = lambda: registry
    app.dependency_overrides[sql_group_registry_from_session] = lambda: groups
    app.dependency_overrides[current_audit_sink] = lambda: audit_sink
    app.dependency_overrides[current_org_access_index] = lambda: BOOTSTRAP_ORG_INDEX
    client = TestClient(app)

    response = post_import(
        client,
        import_csv(
            f"{CHANNEL_ID},Alpha News,cms-tv,Yes",
            header="youtube_channel_id,channel_name,group_id,view_revenue",
        ),
    )

    assert response.status_code == 409
    assert "archived during the import" in response.json()["detail"]
    stored = groups.get_group_by_cms_id("cms-tv")
    assert stored is not None and stored.channel_ids == ()


class _UnstampingDuringApplyGroups(ChannelGroupRegistry):
    """Simulate a stamp cleared between planning and apply.

    Planning's bulk adoptable-key lookup sees the group STAMPED, so the row
    plans clean; the apply's locked write-boundary lookup (for_update=True)
    observes it owner-NULL — the window the admin clear-stamp action opens.
    """

    def get_group_by_cms_id(self, cms_group_id, *, for_update=False):
        group = super().get_group_by_cms_id(cms_group_id, for_update=for_update)
        if group is not None and for_update:
            return dataclasses.replace(group, content_owner_id=None)
        return group


def test_group_unstamped_between_plan_and_apply_returns_409():
    """The one adoption race planning cannot catch becomes a 409, not a stamp.

    The preview reported a clean row because the group WAS owned; if the apply
    adopted it anyway, the import would be minting ownership from a CSV cell
    after Path A closed exactly that. The detail is canned — the remedy is
    fixed — and nothing is written.
    """
    app = create_app()
    registry = bootstrap_channel_registry()
    groups = _UnstampingDuringApplyGroups()
    groups.create_group(
        name="cms-tv",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-tv",
        content_owner_id=CONTENT_OWNER,
    )
    audit_sink = InMemoryAuditSink()
    app.dependency_overrides[current_channel_registry] = lambda: registry
    app.dependency_overrides[sql_group_registry_from_session] = lambda: groups
    app.dependency_overrides[current_audit_sink] = lambda: audit_sink
    app.dependency_overrides[current_org_access_index] = lambda: BOOTSTRAP_ORG_INDEX
    client = TestClient(app)

    response = post_import(
        client,
        import_csv(
            f"{CHANNEL_ID},Alpha News,cms-tv,Yes",
            header="youtube_channel_id,channel_name,group_id,view_revenue",
        ),
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "lost its content owner" in detail
    assert "POST /channels/groups/sync" in detail
    stored = groups.get_group_by_cms_id("cms-tv")
    assert stored is not None
    assert stored.channel_ids == ()
    assert stored.content_owner_id == CONTENT_OWNER


class _ConcurrentlyCreatedRegistry(ChannelRegistry):
    """Simulate a channel created by another writer between plan and apply."""

    def create_channel(self, **kwargs):
        raise ChannelRegistryConflictError(
            f"Channel already exists: {kwargs['youtube_channel_id']}"
        )


def test_channel_created_concurrently_between_plan_and_apply_returns_409():
    """A CREATE losing the uniqueness race is a retryable 409, not a 500."""
    client, _registry, _groups, audit_sink = create_import_app(_ConcurrentlyCreatedRegistry([]))

    response = post_import(client, import_csv(f"{CHANNEL_ID},Alpha News,Yes"))

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]
    assert audit_sink.records == []


class _GroupRaceLosingGroups(ChannelGroupRegistry):
    """Simulate losing the cms_group_id INSERT race to a concurrent import."""

    def create_group(self, **kwargs):
        raise ChannelGroupConflictError(
            f"channel group already exists for cms_group_id: {kwargs['cms_group_id']}"
        )


def test_group_created_concurrently_during_apply_returns_409():
    """Two imports racing the same missing CMS key: the loser gets 409."""
    app = create_app()
    registry = bootstrap_channel_registry()
    groups = _GroupRaceLosingGroups()
    audit_sink = InMemoryAuditSink()
    app.dependency_overrides[current_channel_registry] = lambda: registry
    app.dependency_overrides[sql_group_registry_from_session] = lambda: groups
    app.dependency_overrides[current_audit_sink] = lambda: audit_sink
    app.dependency_overrides[current_org_access_index] = lambda: BOOTSTRAP_ORG_INDEX
    client = TestClient(app)

    response = post_import(
        client,
        import_csv(
            f"{CHANNEL_ID},Alpha News,cms-tv,Yes",
            header="youtube_channel_id,channel_name,group_id,view_revenue",
        ),
    )

    assert response.status_code == 409
    assert "already exists for cms_group_id" in response.json()["detail"]


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


def test_owner_null_group_is_a_row_error_in_both_modes():
    """The import refuses to claim an existing unowned group (Path A).

    Stamping ``content_owner_id`` on an existing group decides which content
    owner's CMS sync governs it from then on, and a CSV cell is not that owner
    speaking. Nothing else in this row would have shown the claim — the
    channel is already correct, so the row would read UNCHANGED with an empty
    diff, and the group already contains the channel, so not even a membership
    add would appear. The refusal carries the remedy instead.
    """
    client, registry, groups, audit_sink = create_import_app()
    registry.create_channel(
        youtube_channel_id=CHANNEL_ID,
        channel_name="Alpha News",
        primary_company_id=None,
        cms_status="INSIDE_CMS",
        content_owner_id=CONTENT_OWNER,
        revenue_required=True,
    )
    groups.create_group(
        name="Legacy",
        group_type="SECTOR",
        channel_ids=[CHANNEL_ID],
        cms_group_id="cms-legacy",
        content_owner_id=None,
    )
    header = "youtube_channel_id,channel_name,group_id,view_revenue"
    body = import_csv(f"{CHANNEL_ID},Alpha News,cms-legacy,Yes", header=header)

    preview = post_import(client, body, dry_run="true")

    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["counts"]["ERROR"] == 1
    row = plan["rows"][0]
    assert row["outcome"] == "ERROR"
    assert "exists without a content owner" in row["reason"]
    assert "POST /channels/groups/sync" in row["reason"]
    assert CONTENT_OWNER in row["reason"]
    # The Path B disclosure is gone from the wire contract, not merely false.
    assert "will_adopt_content_owner" not in row
    assert groups.get_group_by_cms_id("cms-legacy").content_owner_id is None
    assert audit_sink.records == []

    applied = post_import(client, body)

    # The apply agrees with the preview: 422 on the row error, never a stamp.
    assert applied.status_code == 422, applied.text
    detail = applied.json()["detail"]
    assert "exists without a content owner" in detail["rows"][0]["reason"]
    assert groups.get_group_by_cms_id("cms-legacy").content_owner_id is None
    assert audit_sink.records == []


def test_group_this_owner_already_holds_imports_cleanly():
    """Only owner-NULL keys are refused; this owner's own group attaches."""
    client, _registry, groups, _audit_sink = create_import_app()
    groups.create_group(
        name="Mine",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-mine",
        content_owner_id=CONTENT_OWNER,
    )
    header = "youtube_channel_id,channel_name,group_id,view_revenue"
    body = import_csv(f"{CHANNEL_ID},Alpha News,cms-mine,Yes", header=header)

    preview = post_import(client, body, dry_run="true")

    assert preview.status_code == 200, preview.text
    row = preview.json()["rows"][0]
    assert row["outcome"] == "CREATE"
    assert row["reason"] is None
    assert "will_adopt_content_owner" not in row

    applied = post_import(client, body)

    assert applied.status_code == 200, applied.text
    assert groups.get_group_by_cms_id("cms-mine").channel_ids == (CHANNEL_ID,)
