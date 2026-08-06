# ============================================================================
# Purpose: Pin the HTTP-free CMS group-sync core (run_group_sync) — the dry-run
#   early return + rollback, typed-error propagation (never HTTPException, and a
#   fetch failure stays distinguishable from a credential failure), and the
#   caller-owned summary rule (per-group GROUP_UPDATED rows come from apply, the
#   run-level GROUPS_SYNCED summary is NOT this module's to write).
# Database/ORM: in-memory ChannelRegistry / ChannelGroupRegistry stand in for
#   the SQL stores; a MagicMock session records rollback/commit; a fake groups
#   client returns a canned snapshot. No database, no network.
# Standards: assert behaviour AND its evidence — a dry run asserts execution is
#   None AND the session rolled back; the no-HTTPException claim greps the
#   module source (the repo's static-contract test style).
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/group_sync.py -> subject.
# ============================================================================
"""Unit tests for the HTTP-free CMS group-sync core (run_group_sync)."""

import ast
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.connectors.google.errors import (
    CredentialNotFoundError,
    GoogleApiResponseError,
    GoogleConnectorError,
)
from ums_smart_revenue.connectors.google.youtube_groups_client import (
    CmsGroup,
    CmsGroupMembers,
)
from ums_smart_revenue.connectors.runs.group_sync import (
    GroupSyncFetchError,
    GroupSyncRunResult,
    run_group_sync,
)
from ums_smart_revenue.org.channel_groups import ChannelGroupRegistry
from ums_smart_revenue.org.channel_registry import ChannelRegistry, ChannelRegistryEntry

CHANNEL_ONE = "UCB6sc84dcg6VQGB_d89sx2g"
CONTENT_OWNER = "TestOwnerAAAAAAAAAAAAA"
TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")
_ACTOR = UserPrincipal(user_id="sync-actor", email="sync@example.com")

_MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "backend"
    / "ums_smart_revenue"
    / "connectors"
    / "runs"
    / "group_sync.py"
)


class _FakeGroupsClient:
    """Canned stand-in for YouTubeGroupsClient; never performs any I/O."""

    def __init__(
        self,
        snapshot: list[tuple[str, str, tuple[str, ...], int]],
        *,
        groups_error: Exception | None = None,
        items_error_for: str | None = None,
    ) -> None:
        """Hold the canned snapshot plus optional failures to raise."""
        self._snapshot = snapshot
        self._groups_error = groups_error
        self._items_error_for = items_error_for
        self.item_calls: list[str] = []
        self.closed = False

    def list_groups(self, *, account_id: str) -> list[CmsGroup]:
        """Return the canned group list, or raise the configured failure."""
        if self._groups_error is not None:
            raise self._groups_error
        assert account_id == CONTENT_OWNER
        return [CmsGroup(cms_group_id=key, title=title) for key, title, _ids, _n in self._snapshot]

    def list_group_items(self, *, group_id: str, account_id: str) -> CmsGroupMembers:
        """Return the canned members of one group, or raise for the marked id."""
        assert account_id == CONTENT_OWNER
        self.item_calls.append(group_id)
        if self._items_error_for == group_id:
            raise GoogleApiResponseError(url="https://example/groupItems", reason="leaky-items")
        for key, _title, channel_ids, non_channel in self._snapshot:
            if key == group_id:
                return CmsGroupMembers(channel_ids=channel_ids, non_channel_count=non_channel)
        raise AssertionError(f"unexpected group_id: {group_id}")

    def close(self) -> None:
        """Record that the core closed the client."""
        self.closed = True


def _fake_resolver(**_kwargs: object) -> object:
    """Stand-in credential resolver: returns an opaque credentials object."""
    return MagicMock(name="credentials")


def _known_registry() -> ChannelRegistry:
    """Registry holding the one channel the CMS snapshots reference."""
    return ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id=CHANNEL_ONE,
                channel_name="Alpha News",
                primary_company_id="company-tv",
                cms_status="INSIDE_CMS",
                revenue_required=True,
                content_owner_id=CONTENT_OWNER,
            )
        ]
    )


def test_dry_run_returns_no_execution_and_rolls_back() -> None:
    """A dry run previews the plan, applies nothing, and rolls the session back."""
    session = MagicMock()
    client = _FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)])
    groups = ChannelGroupRegistry()

    result = run_group_sync(
        session,
        tenant_id=TENANT_ID,
        content_owner_id=CONTENT_OWNER,
        registry=_known_registry(),
        groups=groups,
        audit_sink=InMemoryAuditSink(),
        actor=_ACTOR,
        reason="preview",
        dry_run=True,
        client_factory=lambda _creds: client,
        resolver=_fake_resolver,
    )

    assert isinstance(result, GroupSyncRunResult)
    assert result.execution is None
    assert result.plan.counts["CREATE"] == 1
    # Persists nothing: rolled back, no group applied, and never committed.
    assert session.rollback.called
    session.commit.assert_not_called()
    assert groups.list_synced_groups() == []
    assert client.closed is True


def test_module_never_imports_or_raises_httpexception() -> None:
    """HTTP mapping stays in the route; the core code must not import or use HTTPException.

    AST-based (like tests/connectors/google/test_orchestrator_static_contract.py) so a
    docstring or contract-comment that merely NAMES the ban does not trip it — only a
    real import or reference in code does.
    """
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    referenced = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert "HTTPException" not in imported
    assert "HTTPException" not in referenced


def test_credential_errors_propagate_untranslated() -> None:
    """A credential-phase typed error escapes as itself, before the fetch runs."""
    session = MagicMock()
    client = _FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)])

    def _raising_resolver(**_kwargs: object) -> object:
        raise CredentialNotFoundError(connector_key="youtube-analytics", account_id="acct")

    with pytest.raises(CredentialNotFoundError):
        run_group_sync(
            session,
            tenant_id=TENANT_ID,
            content_owner_id=CONTENT_OWNER,
            registry=_known_registry(),
            groups=ChannelGroupRegistry(),
            audit_sink=InMemoryAuditSink(),
            actor=_ACTOR,
            reason="apply",
            dry_run=False,
            client_factory=lambda _creds: client,
            resolver=_raising_resolver,
        )
    # The credential phase failed before the fetch: no group items were listed.
    assert client.item_calls == []


def test_fetch_errors_are_wrapped_in_a_distinguishable_typed_error() -> None:
    """A fetch-phase GoogleConnectorError becomes GroupSyncFetchError, cause preserved."""
    session = MagicMock()
    client = _FakeGroupsClient(
        [("cms-a", "News", (CHANNEL_ONE,), 0)],
        groups_error=GoogleApiResponseError(url="https://example/groups", reason="leaky-groups"),
    )

    with pytest.raises(GroupSyncFetchError) as excinfo:
        run_group_sync(
            session,
            tenant_id=TENANT_ID,
            content_owner_id=CONTENT_OWNER,
            registry=_known_registry(),
            groups=ChannelGroupRegistry(),
            audit_sink=InMemoryAuditSink(),
            actor=_ACTOR,
            reason="apply",
            dry_run=False,
            client_factory=lambda _creds: client,
            resolver=_fake_resolver,
        )

    # Distinguishable from a credential-phase GoogleConnectorError (so the route
    # can map fetch -> 502 vs credential -> 503), and it carries the cause rather
    # than translating to HTTP.
    assert not isinstance(excinfo.value, GoogleConnectorError)
    assert isinstance(excinfo.value.__cause__, GoogleApiResponseError)
    assert client.closed is True


def test_apply_returns_execution_and_writes_no_run_summary() -> None:
    """The apply returns the execution and per-group rows; the summary is the caller's."""
    session = MagicMock()
    client = _FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)])
    groups = ChannelGroupRegistry()
    sink = InMemoryAuditSink()

    result = run_group_sync(
        session,
        tenant_id=TENANT_ID,
        content_owner_id=CONTENT_OWNER,
        registry=_known_registry(),
        groups=groups,
        audit_sink=sink,
        actor=_ACTOR,
        reason="apply now",
        dry_run=False,
        client_factory=lambda _creds: client,
        resolver=_fake_resolver,
    )

    assert result.execution is not None
    assert result.execution.counts["CREATE"] == 1
    created = groups.get_group_by_cms_id("cms-a")
    assert created is not None
    assert created.name == "News"
    event_types = [record.event_type for record in sink.records]
    # apply_group_sync wrote the per-group GROUP_UPDATED row...
    assert AuditEventType.GROUP_UPDATED.value in event_types
    # ...but the run-level GROUPS_SYNCED summary is the caller's to write.
    assert AuditEventType.GROUPS_SYNCED.value not in event_types
    # Apply mode commits the credential transaction; it never rolls back.
    assert session.commit.called
    assert session.rollback.called is False
    assert client.closed is True
