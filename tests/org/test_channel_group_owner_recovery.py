# ============================================================================
# Purpose: Pin `clear_group_owner_stamp` — the domain half of the sanctioned
#   owner-stamp eraser — WITHOUT FastAPI. The route-level tier
#   (tests/api/test_groups_api.py) proves the HTTP contract; this tier proves
#   the behaviour is reachable and assertable outside HTTP at all, which is
#   the point of the seam.
# Database/ORM: None. In-memory ChannelGroupRegistry + InMemoryAuditSink; the
#   SQL store's locking behaviour is pinned at the Postgres tier.
# Standards: Both failure modes stay TYPED for the caller to map — KeyError
#   for an unknown group, ChannelGroupNoOwnerStampError when there was no
#   stamp. Neither writes an audit row: a failed clear that still audited
#   would claim an erasure that never happened (#169 invariant).
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_group_owner_recovery.py ->
#     subject.
#   - File: backend/ums_smart_revenue/api/groups.py -> the route that
#     orchestrates it and maps these errors to 404/409.
# ============================================================================
"""Domain-side owner-stamp clear: outcome, audit row, and typed failures."""

import pytest

from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.org.channel_group_owner_recovery import clear_group_owner_stamp
from ums_smart_revenue.org.channel_groups import (
    ChannelGroupNoOwnerStampError,
    ChannelGroupRegistry,
)

ACTOR = UserPrincipal(user_id="user-1", email="user@example.com")
REASON = "Stamped to the wrong content owner during migration"
OWNER_WRONG = "WrongOwnerDDDDDDDDDDDD"


def _registry_with_group(*, content_owner_id: str | None) -> tuple[ChannelGroupRegistry, str]:
    """Build an in-memory registry holding one CMS-keyed group; return its id."""
    registry = ChannelGroupRegistry()
    group = registry.create_group(
        name="Recovery Sector",
        group_type="SECTOR",
        channel_ids=["channel-a"],
        cms_group_id="cms-tv",
        content_owner_id=content_owner_id,
    )
    return registry, group.id


def _clear(registry: ChannelGroupRegistry, group_id: str, sink: InMemoryAuditSink):
    """Invoke the domain function with this module's fixed actor and scope."""
    return clear_group_owner_stamp(
        groups=registry,
        group_id=group_id,
        actor=ACTOR,
        scope=AccessScope.global_scope(),
        reason=REASON,
        audit_sink=sink,
    )


def test_clear_returns_the_cleared_group_and_audits_the_erased_owner() -> None:
    """The stamp is gone, and the audit row names the owner that was removed."""
    registry, group_id = _registry_with_group(content_owner_id=OWNER_WRONG)
    sink = InMemoryAuditSink()

    cleared = _clear(registry, group_id, sink)

    assert cleared.group.content_owner_id is None
    assert cleared.group.name == "Recovery Sector"
    assert cleared.group.channel_ids == ("channel-a",)
    assert cleared.audit_record.event_type == "GROUP_UPDATED"
    assert cleared.audit_record.entity_type == "channel_group"
    assert cleared.audit_record.entity_id == group_id
    assert cleared.audit_record.reason == REASON
    details = cleared.audit_record.details
    assert details["action"] == "content_owner_cleared"
    assert details["cms_group_id"] == "cms-tv"
    assert details["previous_content_owner_id"] == OWNER_WRONG
    assert registry.get_group(group_id).content_owner_id is None


def test_cleared_group_returns_to_the_adoptable_pool() -> None:
    """The whole point of the clear: the right owner's next sync can adopt it."""
    registry, group_id = _registry_with_group(content_owner_id=OWNER_WRONG)
    assert registry.list_adoptable_cms_group_ids({"cms-tv"}) == set()

    _clear(registry, group_id, InMemoryAuditSink())

    assert registry.list_adoptable_cms_group_ids({"cms-tv"}) == {"cms-tv"}


def test_unknown_group_raises_keyerror_and_writes_no_audit_row() -> None:
    """The caller maps this to 404; a failed clear must not audit an erasure."""
    registry, _group_id = _registry_with_group(content_owner_id=OWNER_WRONG)
    sink = InMemoryAuditSink()

    with pytest.raises(KeyError):
        _clear(registry, "missing-group", sink)

    assert sink.records == []


def test_group_without_a_stamp_raises_typed_error_and_writes_no_audit_row() -> None:
    """Clearing nothing is a caller bug (409), not a silent success."""
    registry, group_id = _registry_with_group(content_owner_id=None)
    sink = InMemoryAuditSink()

    with pytest.raises(ChannelGroupNoOwnerStampError):
        _clear(registry, group_id, sink)

    assert sink.records == []
